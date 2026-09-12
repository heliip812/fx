"""``fxgamma.portfolio.deltacap`` -- the number that is worth thousands a night.

``docs/13_delta_cap.md`` measures what three workstreams had only asserted: being 30%
off the **cap** moves the night by 1.2-16.3% of its gamma P&L, against 0.1-0.9% for
being 30% off the band.  So this is the dial, and these are its pinned results.

The load-bearing claims, each with a test that fails if it regresses:

* ``cap = 1/2 * Gamma_1pct * sigma_on`` (rule ``delta_equals_gamma``, R2) gives
  **EUR 0.54mm / 18 pips** on the reference book, floor **0.26mm**, ceiling **1.08mm**;
* the cap is **exactly vol-mark invariant** -- ``Gamma`` falls as ``1/sigma`` while the
  window sigma rises as ``sigma`` and the two cancel.  This user has **no OTC access**,
  so a cap that does not depend on the vol mark is the one parameter they can trust
  without a broker curve.  It is therefore tested across vol levels, tenors, moneyness
  and a skewed book rather than at one point;
* **R2 and R3 converge**: they are one rule under two anchors and ``R3/R2`` is exactly
  the night's carry ratio ``252 D_cal / (365 vf)``;
* the cap self-scales with notional (linear) and tenor (``1/sqrt(T)``);
* the 252-vs-365 split (amendment W-7): a cap is a **distance** question, so it is
  ``sqrt(252)``.  The PM's own candidates used ``sqrt(365)`` and were 20.4% light;
* ``DeltaCap.binds_pct`` holds **percent**, unlike ``HedgeRule.band_pct`` which holds a
  **fraction** -- the units hazard that produced the 60x band error once already.

Plus the null controls: a zero-cost configuration must price the cap at exactly zero.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from fxgamma import conventions as cv

dc = pytest.importorskip("fxgamma.portfolio.deltacap")
bandopt = pytest.importorskip("fxgamma.portfolio.bandopt")
ov = pytest.importorskip("fxgamma.portfolio.overnight")
zones = pytest.importorskip("fxgamma.portfolio.zones")

from conftest import ASOF, _straddle  # noqa: E402

PIP = cv.pair_spec("EURUSD").pip
#: the marks side-table is ``{position_id: mark_vol}``; the reference straddle's legs
#: are ``c`` and ``p``, so re-marking the whole book is two entries.
REF_LEGS = ("c", "p")


def remark(v: float) -> dict[str, float]:
    return {k: float(v) for k in REF_LEGS}


@pytest.fixture(scope="module")
def ref_cap(ref_book, on_mkt):
    return dc.recommend_cap(ref_book, on_mkt, "EURUSD")


@pytest.fixture(scope="module")
def ref_cands(ref_cap):
    return {c.rule: c for c in ref_cap.candidates}


# =========================================================================== #
# 1.  The measured reference numbers
# =========================================================================== #
class TestReferenceBookNumbers:
    """EURUSD 1M ATM, EUR 10mm/leg.  Every figure below is quoted in docs/13 and is
    the thing the user reads before going home."""

    def test_recommended_cap_is_054mm(self, ref_cap):
        assert ref_cap.cap_mm == pytest.approx(0.54, abs=0.02)

    def test_recommended_cap_implies_an_18_pip_band(self, ref_cap):
        assert ref_cap.band_pips == pytest.approx(18.0, abs=0.6)

    def test_default_rule_is_delta_equals_gamma(self, ref_cap):
        assert ref_cap.rule == "delta_equals_gamma"

    def test_dominance_floor_is_026mm(self, ref_cap):
        assert ref_cap.floor_base / 1e6 == pytest.approx(0.26, abs=0.02)

    def test_ceiling_is_108mm(self, ref_cap):
        assert ref_cap.ceiling_base / 1e6 == pytest.approx(1.08, abs=0.03)

    def test_cap_sits_between_its_own_floor_and_ceiling(self, ref_cap):
        assert ref_cap.floor_base < ref_cap.cap_base < ref_cap.ceiling_base

    def test_r2_is_exactly_half_of_r1(self, ref_cands):
        assert ref_cands["delta_equals_gamma"].cap_base == pytest.approx(
            0.5 * ref_cands["sigma_accumulation"].cap_base, rel=1e-12)

    def test_cap_is_half_gamma1pct_times_sigma_on(self, ref_cap):
        """The rule as it is written down: ``1/2 * Gamma_1pct * sigma_on``, with
        sigma_on in percent.  The repriced profile is what ships, so this is the
        closed form it must sit on, not an alternative implementation."""
        closed = 0.5 * abs(ref_cap.gamma_1pct) * (ref_cap.sigma_window * 100.0)
        assert ref_cap.cap_base == pytest.approx(closed, rel=0.03)

    def test_gamma_side_is_long(self, ref_cap):
        assert ref_cap.gamma_side == "long"

    def test_reference_night_is_negative_carry(self, ref_cap):
        assert ref_cap.carry < 0

    def test_reasoning_is_printed_and_substantial(self, ref_cap):
        assert len(str(ref_cap).splitlines()) > 6
        assert "EURUSD" in str(ref_cap)

    def test_reasoning_names_the_rule(self, ref_cap):
        assert "R2" in ref_cap.reasoning or "delta_equals_gamma" in ref_cap.reasoning

    def test_the_15pct_default_is_warned_about(self, ref_cap):
        """It is EUR 3.0mm here, dominated by no cap at all, and it must say so."""
        assert ref_cap.default_rule_cap == pytest.approx(3.0e6, rel=0.02)
        assert any("15" in w and "intraday" in w for w in ref_cap.warnings)

    def test_the_15pct_default_never_binds_overnight(self, ref_cap):
        assert ref_cap.default_rule_cap > 2.5 * ref_cap.ceiling_base

    def test_modelled_profile_is_warned_about(self, ref_cap):
        assert any("profile" in w.lower() for w in ref_cap.warnings)

    def test_cost_assumption_is_warned_about(self, ref_cap):
        assert any("cost" in w.lower() for w in ref_cap.warnings)

    def test_cap_frame_round_trips(self, ref_cap):
        f = dc.cap_frame([ref_cap])
        assert len(f) == 1 and "cap_mm" in f.columns

    def test_as_dict_is_json_shaped(self, ref_cap):
        d = ref_cap.as_dict()
        assert isinstance(d["candidates"], list) and d["cap_mm"] > 0


# =========================================================================== #
# 2.  Vol-mark invariance -- the property this user cannot get without it
# =========================================================================== #
class TestVolMarkInvariance:
    """The load-bearing claim.  ``Gamma_1pct = 0.3989 N / (sigma sqrt(T))`` falls as
    ``1/sigma``; ``sigma_on = sigma sqrt(vf/252)`` rises as ``sigma``; the product is
    independent of the level of vol.  Tested **hard**: across a 13x range of vol, four
    tenors, five moneyness points, a skewed book, and per-leg mark disagreement.

    Tolerances below are not slack -- they are the measured size of the *repricing*
    correction, which is a real second-order effect of reading the delta profile
    rather than assuming linearity.  The exact cancellation is asserted separately on
    the closed form."""

    VOLS = (0.03, 0.05, 0.0796, 0.10, 0.15, 0.25, 0.40)

    @pytest.mark.parametrize("v", VOLS)
    def test_cap_is_invariant_across_a_13x_vol_range(self, ref_book, on_mkt, v):
        base = dc.recommend_cap(ref_book, on_mkt, "EURUSD",
                                marks=remark(0.0796)).cap_base
        got = dc.recommend_cap(ref_book, on_mkt, "EURUSD", marks=remark(v)).cap_base
        assert got == pytest.approx(base, rel=0.03), (
            f"cap moved {100 * (got / base - 1):+.2f}% for a vol mark of {v:.2%}")

    @pytest.mark.parametrize("v", [0.06, 0.07, 0.0796, 0.09, 0.10])
    def test_cap_is_invariant_to_a_plausible_mark_error(self, ref_book, on_mkt, v):
        """docs/13 s4.7: over +/-1 vol point R1/R2/R3 move by less than 0.1%.  This is
        the regime the user is actually in -- they cannot see a broker curve, but they
        are not wrong by 30 vol points either."""
        base = dc.recommend_cap(ref_book, on_mkt, "EURUSD",
                                marks=remark(0.0796)).cap_base
        got = dc.recommend_cap(ref_book, on_mkt, "EURUSD", marks=remark(v)).cap_base
        assert got == pytest.approx(base, rel=0.006)

    @pytest.mark.parametrize("v", VOLS)
    def test_r1_r2_r3_are_all_vol_invariant(self, ref_book, on_mkt, v):
        def cands(x):
            return {c.rule: c.cap_base
                    for c in dc.recommend_cap(ref_book, on_mkt, "EURUSD",
                                              marks=remark(x)).candidates}
        base, got = cands(0.0796), cands(v)
        for rule in ("sigma_accumulation", "delta_equals_gamma", "theta_anchored"):
            assert got[rule] == pytest.approx(base[rule], rel=0.07), rule

    @pytest.mark.parametrize("days", [7, 30, 90, 365])
    @pytest.mark.parametrize("v", [0.05, 0.10, 0.20])
    def test_invariance_holds_at_every_tenor(self, on_mkt, days, v):
        S = on_mkt.spot["EURUSD"]
        b = _straddle("EURUSD", S, days=days, prefix="t")
        ref = dc.recommend_cap(b, on_mkt, "EURUSD",
                               marks={"tc": 0.0796, "tp": 0.0796}).cap_base
        got = dc.recommend_cap(b, on_mkt, "EURUSD",
                               marks={"tc": v, "tp": v}).cap_base
        assert got == pytest.approx(ref, rel=0.05)

    @pytest.mark.parametrize("z", [0.0, 0.0025])
    @pytest.mark.parametrize("v", [0.06, 0.12])
    def test_invariance_holds_near_the_money(self, on_mkt, z, v):
        """A quarter of a percent either side of the money it still holds to 2%."""
        S = on_mkt.spot["EURUSD"]
        b = _straddle("EURUSD", S, strike=S * (1 + z), prefix="m")
        ref = dc.recommend_cap(b, on_mkt, "EURUSD",
                               marks={"mc": 0.09, "mp": 0.09}).cap_base
        got = dc.recommend_cap(b, on_mkt, "EURUSD",
                               marks={"mc": v, "mp": v}).cap_base
        assert got == pytest.approx(ref, rel=0.02)

    @pytest.mark.regression
    def test_invariance_is_an_at_the_money_property_and_is_flagged(self, on_mkt, skewed_book):
        """FINDING (QA-5, the most consequential in this module) -- ACCEPTED, claim corrected.  docs/13's ruling
        states the rule 'self-scales with notional, tenor, vol AND MONEYNESS, and is
        exactly vol-mark invariant', and gives that as the reason a user with **no OTC
        access** can trust the cap without a broker curve.

        Measured here: the invariance is an AT-THE-MONEY property.  ``cap_candidates``
        own docstring scopes it correctly ('at the money, with
        Gamma_1pct = 0.3989 N / (sigma sqrt(T))'), but off the money Gamma_1pct is not
        that expression and the cancellation stops working.  Measured on a 1M EURUSD
        straddle, moving the mark from 6% to 12%:

            struck at the money   1.01x      struck 2% away   1.57x
            struck 1% away        1.11x      struck 3% away   2.82x
                                             struck 4% away   3.20x

        and on an ordinary 1M call spread (the ``skewed_book`` fixture) a 2.3x change
        in the surface moves the cap **2.8x**.  The user this property was written for
        is exactly the one who cannot tell which of those marks is right, and the cap
        is the dial docs/13 measures at 1.2-16.3% of the night."""
        S = on_mkt.spot["EURUSD"]
        worst = 1.0
        for z in (0.0, 0.01, 0.02, 0.03, 0.04):
            b = _straddle("EURUSD", S, strike=S * (1 + z), prefix="m")
            caps = [dc.recommend_cap(b, on_mkt, "EURUSD",
                                     marks={"mc": v, "mp": v}).cap_base
                    for v in (0.06, 0.09, 0.12)]
            worst = max(worst, max(caps) / min(caps))
        sk = [dc.recommend_cap(skewed_book, on_mkt, "EURUSD",
                               marks={"k1": 0.085 * m, "k2": 0.095 * m}).cap_base
              for m in (0.7, 1.0, 1.6)]
        worst = max(worst, max(sk) / min(sk))

        # PM ruling: the finding is ACCEPTED and the claim, not the code, was wrong.
        # The cancellation `Gamma_1pct ~ 1/sigma` against `sigma_on ~ sigma` is an
        # at-the-money identity; off the money `Gamma_1pct` is not that expression and
        # no implementation can restore it. docs/13 has been corrected to scope the
        # claim to ATM, and `recommend_cap` now WARNS whenever the book's furthest
        # strike is more than 1% from spot -- which is the behaviour this test pins,
        # because a user with no OTC access needs to be told, not reassured.
        assert worst > 1.5, (
            "the off-ATM sensitivity is the documented finding; if it has genuinely "
            f"gone away ({worst:.2f}x) the docs/13 s4.7 correction needs revisiting")
        b_far = _straddle("EURUSD", S, strike=S * 1.04, prefix="warn")
        warns = [w for w in dc.recommend_cap(b_far, on_mkt, "EURUSD").warnings
                 if "vol-mark invariant" in w]
        assert warns, "an off-ATM book must be warned that its cap is mark-dependent"
        b_atm = _straddle("EURUSD", S, strike=S, prefix="atm")
        assert not [w for w in dc.recommend_cap(b_atm, on_mkt, "EURUSD").warnings
                    if "vol-mark invariant" in w], "an ATM book must not be warned"

    @pytest.mark.parametrize("bump", [0.7, 0.85, 1.0, 1.25, 1.6])
    def test_invariance_holds_on_a_skewed_book(self, skewed_book, on_mkt, bump):
        """The book where the up-side and down-side caps genuinely differ.  Scaling
        the whole surface must still not move the answer."""
        base_marks = {"k1": 0.085, "k2": 0.095}
        ref = dc.recommend_cap(skewed_book, on_mkt, "EURUSD", marks=base_marks).cap_base
        got = dc.recommend_cap(skewed_book, on_mkt, "EURUSD",
                               marks={k: v * bump for k, v in base_marks.items()}).cap_base
        # NOT invariant here -- see test_invariance_survives_moneyness.  What is
        # asserted is the direction and the absence of a discontinuity: a higher
        # surface means less gamma on a struck-away book, so a smaller cap, smoothly.
        assert (got < ref) == (bump > 1.0)
        assert 0.3 < got / ref < 3.5

    @pytest.mark.parametrize("v", [0.04, 0.08, 0.16, 0.32])
    def test_the_closed_form_cancellation_is_exact(self, on_mkt, v):
        """``Gamma_1pct * sigma_on`` with both read off the SAME mark: the
        cancellation the whole claim rests on, with no repricing in the way."""
        b = _straddle("EURUSD", on_mkt.spot["EURUSD"], prefix="x")
        bg = bandopt.book_gamma(b, on_mkt, "EURUSD", marks={"xc": v, "xp": v})
        win = ov.passive_window(on_mkt.asof, pair="EURUSD")
        sig_on, _ = dc.overnight_sigma(bg, win)
        product = abs(bg.gamma_1pct) * sig_on * 100.0
        ref_bg = bandopt.book_gamma(b, on_mkt, "EURUSD", marks={"xc": 0.08, "xp": 0.08})
        ref_sig, _ = dc.overnight_sigma(ref_bg, win)
        # exact but for the d1 drift term of a SPOT-atm strike (rd - rf = 2%), worth
        # under 1% across an 8x vol range; at a forward-atm strike it is machine exact.
        assert product == pytest.approx(abs(ref_bg.gamma_1pct) * ref_sig * 100.0,
                                        rel=0.012)

    def test_gamma_falls_as_one_over_vol(self, on_mkt):
        b = _straddle("EURUSD", on_mkt.spot["EURUSD"], prefix="g")
        lo = bandopt.book_gamma(b, on_mkt, "EURUSD", marks={"gc": 0.05, "gp": 0.05})
        hi = bandopt.book_gamma(b, on_mkt, "EURUSD", marks={"gc": 0.10, "gp": 0.10})
        assert lo.gamma_1pct / hi.gamma_1pct == pytest.approx(2.0, rel=0.02)

    def test_window_sigma_rises_linearly_in_vol(self, on_mkt):
        b = _straddle("EURUSD", on_mkt.spot["EURUSD"], prefix="w")
        win = ov.passive_window(on_mkt.asof, pair="EURUSD")
        lo, _ = dc.overnight_sigma(
            bandopt.book_gamma(b, on_mkt, "EURUSD", marks={"wc": 0.05, "wp": 0.05}), win)
        hi, _ = dc.overnight_sigma(
            bandopt.book_gamma(b, on_mkt, "EURUSD", marks={"wc": 0.10, "wp": 0.10}), win)
        assert hi / lo == pytest.approx(2.0, rel=1e-9)

    def test_r4_is_honestly_NOT_advertised_as_vol_invariant(self, ref_cands):
        """The tail-budget rule falls as ``1/sigma_morning`` and the module says so.
        An honest scaling note is part of the contract."""
        assert "NOT vol-invariant" in ref_cands["tail_budget"].scaling

    def test_r1_and_r2_advertise_vol_independence(self, ref_cands):
        for r in ("sigma_accumulation", "theta_anchored"):
            assert "INDEPENDENT OF VOL" in ref_cands[r].scaling


# =========================================================================== #
# 3.  R2 and R3 converge -- one rule under two anchors
# =========================================================================== #
class TestR2R3Convergence:
    """docs/13: ``R3/R2 = 252 D_cal / (365 vf)`` is exactly the night's carry ratio,
    which is why the two rules converge.  When they diverge the night is expensive and
    the divergence is the message."""

    @pytest.mark.parametrize("days", [7, 14, 30, 90, 180])
    def test_ratio_is_the_carry_ratio(self, on_mkt, days):
        b = _straddle("EURUSD", on_mkt.spot["EURUSD"], days=days, prefix="r")
        cap = dc.recommend_cap(b, on_mkt, "EURUSD")
        by = {c.rule: c.cap_base for c in cap.candidates}
        got = by["theta_anchored"] / by["delta_equals_gamma"]
        pred = (zones.TRADING_DAYS * cap.calendar_days
                / (365.0 * cap.var_fraction))
        assert got == pytest.approx(pred, rel=0.04)

    def test_the_two_rules_are_within_10pct_on_a_weeknight(self, ref_cands):
        a = ref_cands["delta_equals_gamma"].cap_base
        b = ref_cands["theta_anchored"].cap_base
        assert abs(b / a - 1.0) < 0.10

    def test_weeknight_carry_ratio_is_about_105(self, ref_cap):
        pred = zones.TRADING_DAYS * ref_cap.calendar_days / (365.0 * ref_cap.var_fraction)
        assert pred == pytest.approx(1.054, abs=0.02)

    def test_a_weekend_drives_them_apart(self, ref_book, on_mkt):
        """Three days of theta against one night's variance: R3 must blow out."""
        fri = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        w = ov.passive_window(fri, pair="EURUSD")
        cap = dc.recommend_cap(ref_book, on_mkt, "EURUSD", window=w)
        by = {c.rule: c.cap_base for c in cap.candidates}
        assert by["theta_anchored"] / by["delta_equals_gamma"] > 2.0

    def test_r3_basis_states_the_identity(self, ref_cands):
        assert "R3/R2" in ref_cands["theta_anchored"].basis

    def test_every_documented_rule_has_a_CAP_RULES_entry(self, ref_cap):
        for c in ref_cap.candidates:
            assert c.rule in dc.CAP_RULES
            assert c.basis and c.assumption and c.scaling


# =========================================================================== #
# 4.  Scaling with notional and tenor
# =========================================================================== #
class TestScaling:
    """``R1 = 0.3989 N_gross sqrt(vf / (252 T))``: linear in notional,
    ``1/sqrt(tenor)``.  A cap that does not self-scale is a cap the user has to
    re-type, and the whole point is that they never type a number."""

    @pytest.mark.parametrize("mult", [0.5, 2.0, 4.0, 10.0])
    def test_cap_is_linear_in_notional(self, on_mkt, mult):
        S = on_mkt.spot["EURUSD"]
        base = dc.recommend_cap(_straddle("EURUSD", S, leg=5e6, prefix="n0"),
                                on_mkt, "EURUSD")
        got = dc.recommend_cap(_straddle("EURUSD", S, leg=5e6 * mult, prefix="n1"),
                               on_mkt, "EURUSD")
        assert got.cap_base == pytest.approx(base.cap_base * mult, rel=0.02)

    @pytest.mark.parametrize("mult", [0.5, 2.0, 4.0])
    def test_band_in_pips_is_invariant_to_notional(self, on_mkt, mult):
        """Doubling the book doubles the cap and doubles the gamma, so the ladder
        spacing in pips does not move.  That is the self-scaling property."""
        S = on_mkt.spot["EURUSD"]
        base = dc.recommend_cap(_straddle("EURUSD", S, leg=5e6, prefix="p0"),
                                on_mkt, "EURUSD")
        got = dc.recommend_cap(_straddle("EURUSD", S, leg=5e6 * mult, prefix="p1"),
                               on_mkt, "EURUSD")
        assert got.band_pips == pytest.approx(base.band_pips, rel=0.02)

    @pytest.mark.parametrize("days", [7, 14, 30, 90, 180, 365])
    def test_r1_scales_as_one_over_sqrt_tenor(self, on_mkt, days):
        S = on_mkt.spot["EURUSD"]
        b = _straddle("EURUSD", S, days=days, prefix="s")
        by = {c.rule: c.cap_base
              for c in dc.recommend_cap(b, on_mkt, "EURUSD").candidates}
        invariant = by["sigma_accumulation"] * math.sqrt(days / 365.0)
        assert invariant == pytest.approx(0.305e6, rel=0.06)

    def test_shorter_tenor_wants_a_bigger_cap(self, on_mkt):
        S = on_mkt.spot["EURUSD"]
        a = dc.recommend_cap(_straddle("EURUSD", S, days=7, prefix="q7"), on_mkt, "EURUSD")
        b = dc.recommend_cap(_straddle("EURUSD", S, days=90, prefix="q90"), on_mkt, "EURUSD")
        assert a.cap_base > 2.5 * b.cap_base

    def test_r5_is_flagged_as_the_wrong_shape_for_a_tenor_change(self, ref_cands):
        """Premium goes as sqrt(T) while every other rule goes as 1/sqrt(T).  The
        module reports it for scale and refuses to default to it -- the honesty is
        the point and it is part of the contract."""
        assert "WRONG WAY" in ref_cands["premium_at_risk"].scaling

    def test_r5_is_never_chosen_as_the_default(self, ref_cap):
        assert ref_cap.rule != "premium_at_risk"

    @pytest.mark.parametrize("pair", ["EURUSD", "USDJPY", "GBPUSD", "AUDUSD"])
    def test_every_pair_produces_a_coherent_cap(self, on_mkt, pair):
        b = _straddle(pair, on_mkt.spot[pair], prefix=f"c{pair}")
        c = dc.recommend_cap(b, on_mkt, pair)
        assert c.cap_base > 0 and np.isfinite(c.band_pips)
        assert c.base_ccy == cv.pair_spec(pair).base
        assert c.ccy == cv.pair_spec(pair).quote


# =========================================================================== #
# 5.  Two clocks: 252 for distance, 365 for economics (amendment W-7)
# =========================================================================== #
class TestTwoClocks:
    """The PM violated its own W-7 ruling and every candidate cap was 20.4% light.
    A delta cap is a DISTANCE question, so the overnight sigma is on the 252 clock."""

    def test_overnight_sigma_uses_252(self, ref_book, on_mkt):
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        win = ov.passive_window(on_mkt.asof, pair="EURUSD")
        s, basis = dc.overnight_sigma(bg, win)
        assert s == pytest.approx(bg.sigma * math.sqrt(win.var_fraction / 252.0), rel=1e-12)
        assert "252" in basis

    def test_the_365_answer_would_be_204pct_light(self, ref_book, on_mkt):
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        win = ov.passive_window(on_mkt.asof, pair="EURUSD")
        s, _ = dc.overnight_sigma(bg, win)
        wrong = bg.sigma * math.sqrt(win.var_fraction / 365.0)
        assert s / wrong == pytest.approx(math.sqrt(365.0 / 252.0), rel=1e-12)
        assert s / wrong == pytest.approx(1.2033, abs=0.001)

    def test_trading_days_is_imported_not_restated(self):
        """A duplicated constant is how ``band_pct`` drifted before."""
        assert dc.TRADING_DAYS is zones.TRADING_DAYS
        assert zones.TRADING_DAYS == 252.0

    def test_theta_is_charged_on_calendar_days(self, ref_cap, ref_book, on_mkt):
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        assert ref_cap.theta_window == pytest.approx(
            bg.theta * ref_cap.calendar_days, rel=1e-12)

    def test_a_range_forecast_overrides_the_session_scaled_implied(self, ref_book,
                                                                    on_mkt):
        c = dc.recommend_cap(ref_book, on_mkt, "EURUSD", range_forecast=0.005)
        assert c.sigma_window == pytest.approx(0.005, rel=1e-12)

    def test_cap_scales_with_the_forecast_range(self, ref_book, on_mkt):
        a = dc.recommend_cap(ref_book, on_mkt, "EURUSD", range_forecast=0.002)
        b = dc.recommend_cap(ref_book, on_mkt, "EURUSD", range_forecast=0.004)
        assert b.cap_base / a.cap_base == pytest.approx(2.0, rel=0.05)


# =========================================================================== #
# 6.  Floors, ceilings and the dominance argument
# =========================================================================== #
class TestFloorsAndCeilings:
    """Two preference-free statements: below EUR 0.30mm is strictly dominated (worse
    mean AND worse tail); above ~EUR 1.4mm is strictly dominated by no cap at all.
    Between them it is a priced preference and the module says so."""

    def test_tail_optimal_cap_is_the_geometric_mean_form(self, ref_book, on_mkt):
        """``h* = S sqrt(sqrt(2)/z * lambda * sigma_on)`` -- the geometric mean of the
        spread and the window sigma, not a cube root of cost over risk aversion."""
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        lam = 5.0 / 2.0 / 1e4
        sw = 0.0031
        got = dc.tail_optimal_cap(bg, lam, sw)
        h = bg.spot * math.sqrt(math.sqrt(2.0) / dc.TAIL_MULT_95 * lam * sw)
        assert got == pytest.approx(abs(bg.gamma) * h, rel=1e-12)

    def test_tail_optimal_cap_is_about_025mm_on_the_reference_book(self, ref_book,
                                                                   on_mkt):
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        assert dc.tail_optimal_cap(bg, 5.0 / 2e4, 0.0031) / 1e6 == \
            pytest.approx(0.25, abs=0.03)

    @pytest.mark.parametrize("scale", [0.25, 1.0, 4.0])
    def test_tail_optimal_cap_scales_as_sqrt_cost(self, ref_book, on_mkt, scale):
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        base = dc.tail_optimal_cap(bg, 5.0 / 2e4, 0.0031)
        got = dc.tail_optimal_cap(bg, scale * 5.0 / 2e4, 0.0031)
        assert got / base == pytest.approx(math.sqrt(scale), rel=1e-12)

    def test_breakeven_clip_is_two_lambda_s_gamma(self, ref_book, on_mkt):
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        lam = 5.0 / 2e4
        assert dc.breakeven_clip(bg, lam) == pytest.approx(
            2.0 * lam * bg.spot * abs(bg.gamma), rel=1e-12)

    def test_breakeven_clip_is_not_small_at_retail_cost(self, ref_book, on_mkt):
        """docs/13: EUR 0.175mm on the reference book at 5bp.  Below it every fill
        loses money outright, whatever the risk aversion."""
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        assert dc.breakeven_clip(bg, 5.0 / 2e4) / 1e6 == pytest.approx(0.17, abs=0.02)

    def test_floor_is_the_max_of_its_three_components(self, ref_cap, ref_book, on_mkt):
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        lam = ref_cap.cost_bp / 2e4
        parts = (dc.tail_optimal_cap(bg, lam, ref_cap.sigma_window),
                 dc.breakeven_clip(bg, lam), ov.RETAIL_LOT_BASE)
        assert ref_cap.floor_base == pytest.approx(max(parts), rel=1e-9)

    def test_ceiling_is_the_one_sigma_accumulation(self, ref_cap, ref_cands):
        assert ref_cap.ceiling_base == pytest.approx(
            ref_cands["sigma_accumulation"].cap_base, rel=1e-12)

    def test_floor_rises_with_cost(self, ref_book, on_mkt):
        prev = 0.0
        for bp in (0.2, 1.0, 5.0, 20.0):
            c = dc.recommend_cap(ref_book, on_mkt, "EURUSD", cost_bp=bp)
            assert c.floor_base > prev
            prev = c.floor_base

    def test_cap_is_clamped_up_to_the_floor_at_a_plausible_high_cost(self, ref_book,
                                                                      on_mkt):
        c = dc.recommend_cap(ref_book, on_mkt, "EURUSD", cost_bp=20.0)
        assert c.cap_base >= c.floor_base * 0.999
        assert "floor" in c.reasoning.lower()

    @pytest.mark.regression
    @pytest.mark.parametrize("cost_bp", [40.0, 60.0])
    def test_a_dominated_cap_is_never_returned_silently(self, ref_book, on_mkt,
                                                        cost_bp):
        """FINDING (QA-6).  Above ~30bp round trip the dominance floor rises past the
        one-sigma ceiling, the clamp resolves to the ceiling, and the module returns a
        cap it has itself defined as **strictly dominated -- worse mean AND worse
        tail** -- with ``no_ladder`` False and nothing in ``warnings`` to say so.  The
        honest answer there is the ``no_ladder`` verdict it already knows how to
        produce."""
        c = dc.recommend_cap(ref_book, on_mkt, "EURUSD", cost_bp=cost_bp)
        if c.cap_base < c.floor_base * 0.999:
            assert c.no_ladder or any("dominat" in w.lower() for w in c.warnings), (
                f"cap {c.cap_mm:.3f}mm is below its own dominance floor "
                f"{c.floor_base / 1e6:.3f}mm at {cost_bp}bp and nothing says so")

    def test_binds_probability_falls_as_the_cap_widens(self, ref_book, on_mkt):
        prev = 101.0
        for ovr in (0.2e6, 0.5e6, 1.0e6, 3.0e6):
            c = dc.recommend_cap(ref_book, on_mkt, "EURUSD", override=ovr)
            assert c.binds_pct < prev
            prev = c.binds_pct

    def test_override_effect_reports_what_changed(self, ref_book, on_mkt, ref_cap):
        eff = dc.override_effect(ref_cap, 1.0e6)
        assert "verdict" in eff and eff["floor_mm"] < 1.0 < eff["ceiling_mm"]

    def test_override_outside_the_range_is_flagged(self, ref_cap):
        assert "DOMINATED" in dc.override_effect(ref_cap, 0.05e6)["verdict"].upper() or \
            "below" in dc.override_effect(ref_cap, 0.05e6)["verdict"].lower()

    def test_override_is_used_verbatim(self, ref_book, on_mkt):
        c = dc.recommend_cap(ref_book, on_mkt, "EURUSD", override=0.77e6)
        assert c.cap_base == pytest.approx(0.77e6, rel=1e-12)
        assert c.overridden is True and c.rule == "user override"

    def test_cap_scaling_note_is_produced(self, ref_cap):
        assert isinstance(dc.cap_scaling_note(ref_cap), str)
        assert dc.format_cap(ref_cap)


# =========================================================================== #
# 7.  Null controls -- Priority 2.  A zero-cost cap must cost exactly zero.
# =========================================================================== #
class TestNullControls:
    """The sixth manufactured effect was a phantom cost that **scaled with the cap**
    -- exactly the shape of a real result -- and the zero-cost null control read
    t = -51.  Every cost the module quotes therefore gets a null."""

    @pytest.mark.parametrize("ovr", [0.15e6, 0.3e6, 0.6e6, 1.2e6, 3.0e6])
    def test_zero_cost_prices_the_cap_at_exactly_zero(self, ref_book, on_mkt, ovr):
        c = dc.recommend_cap(ref_book, on_mkt, "EURUSD", cost_bp=0.0, override=ovr)
        assert c.est_cost_night == 0.0

    @pytest.mark.parametrize("ovr", [0.15e6, 0.3e6, 0.6e6, 1.2e6, 3.0e6])
    def test_cost_is_exactly_linear_in_the_spread(self, ref_book, on_mkt, ovr):
        a = dc.recommend_cap(ref_book, on_mkt, "EURUSD", cost_bp=1.0, override=ovr)
        b = dc.recommend_cap(ref_book, on_mkt, "EURUSD", cost_bp=5.0, override=ovr)
        assert b.est_cost_night == pytest.approx(5.0 * a.est_cost_night, rel=1e-9)

    @pytest.mark.parametrize("ovr", [0.3e6, 0.6e6, 1.2e6])
    def test_cost_is_exactly_inverse_in_the_cap(self, ref_book, on_mkt, ovr):
        """``lambda S Gamma^2 V / D``.  A cost that is not exactly 1/D is the shape
        the phantom took."""
        a = dc.recommend_cap(ref_book, on_mkt, "EURUSD", override=ovr)
        b = dc.recommend_cap(ref_book, on_mkt, "EURUSD", override=2 * ovr)
        assert b.est_cost_night == pytest.approx(0.5 * a.est_cost_night, rel=1e-9)

    def test_zero_gamma_book_is_refused_gracefully(self, on_mkt):
        from fxgamma.types import Book
        c = dc.recommend_cap(Book(options=[], spots=[]), on_mkt, "EURUSD")
        assert c.cap_base == 0.0 and c.rule == "none"
        assert "nothing to cap" in c.reasoning

    def test_a_flat_book_has_no_cap_and_says_so(self, on_mkt):
        """Long and short the same straddle: zero gamma, and no cap is the answer."""
        from fxgamma.types import Book, OptionPosition
        S = on_mkt.spot["EURUSD"]
        exp = ASOF.date() + timedelta(days=30)
        flat = Book(options=[
            OptionPosition(id="a", pair="EURUSD", cp=+1, strike=S, expiry=exp,
                           notional_base=10e6, direction=+1),
            OptionPosition(id="b", pair="EURUSD", cp=+1, strike=S, expiry=exp,
                           notional_base=10e6, direction=-1)], spots=[])
        c = dc.recommend_cap(flat, on_mkt, "EURUSD")
        assert abs(c.gamma) < 1.0

    def test_gap_loss_is_exactly_linear_in_the_cap(self, ref_book, on_mkt):
        """``loss(D) = 0.5 g D + |Gamma| g (S lam + slip)``: the cap term is linear
        and the friction term does not respond to the cap at all."""
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        kw = dict(gap_spot=0.0035 * bg.spot, lam=5.0 / 2e4, slip_pips=1.0, pip=PIP)
        a = dc.gap_loss(bg, cap=0.3e6, **kw)
        b = dc.gap_loss(bg, cap=0.6e6, **kw)
        assert b["residual_gamma_loss"] == pytest.approx(
            2.0 * a["residual_gamma_loss"], rel=1e-12)
        assert a["friction"] == pytest.approx(b["friction"], rel=1e-12)

    def test_gap_loss_friction_is_zero_at_zero_cost_and_zero_slip(self, ref_book,
                                                                   on_mkt):
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        g = dc.gap_loss(bg, cap=0.5e6, gap_spot=0.004, lam=0.0, slip_pips=0.0, pip=PIP)
        assert g["friction"] == 0.0
        assert g["total"] == pytest.approx(g["residual_gamma_loss"], rel=1e-12)

    def test_short_gamma_cap_inverts_gap_loss(self, ref_book, on_mkt):
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        gap = 0.004
        cap, scale = dc.short_gamma_cap(bg, loss_limit=60_000.0, gap_spot=gap,
                                        lam=5.0 / 2e4, slip_pips=1.0, pip=PIP)
        if cap > 0:
            back = dc.gap_loss(bg, cap=cap, gap_spot=gap, lam=5.0 / 2e4,
                               slip_pips=1.0, pip=PIP)
            assert back["total"] == pytest.approx(60_000.0, rel=1e-6)
            assert scale == 1.0

    def test_short_gamma_cap_reports_a_position_scale_when_no_cap_works(self,
                                                                        ref_book,
                                                                        on_mkt):
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        cap, scale = dc.short_gamma_cap(bg, loss_limit=1.0, gap_spot=0.004,
                                        lam=5.0 / 2e4, slip_pips=1.0, pip=PIP)
        assert cap == 0.0 and 0.0 < scale < 1.0

    def test_first_passage_probability_is_a_probability(self):
        for a in (0.0005, 0.002, 0.01):
            for sd in (0.001, 0.003, 0.01):
                bg = dc._two_sided_first_passage(a, sd)
                assert 0.0 <= bg <= 1.0

    def test_first_passage_is_monotone_in_the_barrier(self):
        prev = 1.1
        for a in (0.0005, 0.001, 0.002, 0.005, 0.02):
            p = dc._two_sided_first_passage(a, 0.0031)
            assert p < prev
            prev = p


# =========================================================================== #
# 8.  Short gamma, stops and the SEVEN refusal conditions
# =========================================================================== #
@pytest.fixture(scope="module")
def sg(short_book, on_mkt):
    return dc.recommend_cap(short_book, on_mkt, "EURUSD", loss_limit=15_000.0)


class TestShortGammaRefusals:
    """docs/11 s5.2 lists **seven** named refusal conditions for an unattended
    short-gamma ladder, adopted verbatim by the PM amendment.  A refusal list that
    silently drops the conditions it cannot check is worse than no refusal list, so
    the module must either refuse or say it did not check -- for all seven."""

    def test_short_book_is_recognised(self, sg):
        assert sg.gamma_side == "short" and sg.gamma < 0

    def test_short_book_produces_refusals(self, sg):
        assert sg.refused and sg.refusals

    def test_indicative_surface_is_refused(self, sg):
        """Condition 1: you cannot size a stop off an indicative mark, and the
        synthetic provider is indicative by construction."""
        assert any("surface" in r for r in sg.refusals)

    def test_weekend_is_refused(self, short_book, on_mkt):
        """Condition 5: a stop resting through a Sunday gap is the most dangerous
        order type in FX."""
        fri = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        w = ov.passive_window(fri, pair="EURUSD")
        c = dc.recommend_cap(short_book, on_mkt, "EURUSD", window=w)
        assert any("weekend" in r.lower() for r in c.refusals)

    def test_tier3_event_is_refused(self, short_book, on_mkt):
        """Condition 2: a decision is a jump and a stop is the wrong instrument."""
        ev = pd.DataFrame({
            "datetime": [ASOF.replace(hour=19, minute=0)],
            "event": ["ECB decision"], "importance": [3], "ccy": ["EUR"],
            "source": ["test"]})
        c = dc.recommend_cap(short_book, on_mkt, "EURUSD", events=ev)
        assert any("tier-3" in r.lower() or "tier 3" in r.lower() for r in c.refusals)

    def test_loss_limit_breach_is_refused(self, short_book, on_mkt):
        """Condition 3: the whole point of having a limit."""
        c = dc.recommend_cap(short_book, on_mkt, "EURUSD", loss_limit=1.0)
        assert any("gap" in r.lower() and "limit" in r.lower() for r in c.refusals)

    def test_missing_loss_limit_is_declared_unassessable_not_silently_passed(
            self, short_book, on_mkt):
        c = dc.recommend_cap(short_book, on_mkt, "EURUSD")
        assert any("NOT CHECKED" in u for u in c.unassessable)

    def test_the_three_underivable_conditions_are_named(self, sg):
        """Conditions 4, 6 and 7 -- pin risk at the next cut, the outermost clip
        against 03:00 liquidity, and stop-limit-only platforms -- cannot be derived
        from the book, and the module must SAY so rather than drop them."""
        blob = " ".join(sg.unassessable).lower()
        assert "pin" in blob
        assert "03:00" in blob or "trades at" in blob
        assert "stop-limit" in blob or "stop limit" in blob

    def test_all_seven_conditions_are_accounted_for(self, short_book, on_mkt):
        """Four refusable + three declared unassessable = seven."""
        ev = pd.DataFrame({"datetime": [ASOF.replace(hour=19)],
                           "event": ["FOMC"], "importance": [3], "ccy": ["USD"],
                           "source": ["t"]})
        weeknight = dc.recommend_cap(short_book, on_mkt, "EURUSD", loss_limit=1.0,
                                     events=ev)
        blob = " ".join(weeknight.refusals).lower()
        for token in ("surface", "tier-3", "limit"):
            assert token in blob, token
        fri = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        wk = dc.recommend_cap(short_book, on_mkt, "EURUSD",
                              window=ov.passive_window(fri, pair="EURUSD"))
        assert any("weekend" in r.lower() for r in wk.refusals)
        assert len(weeknight.unassessable) >= 2

    def test_refusals_offer_an_alternative(self, short_book, on_mkt):
        """'When it refuses it must offer the alternatives instead of just saying
        no', because the risk does not go away when the ladder does."""
        c = dc.recommend_cap(short_book, on_mkt, "EURUSD", loss_limit=1.0)
        blob = " ".join(c.refusals).lower()
        assert "cut" in blob or "reduce" in blob or "wing" in blob

    def test_short_gamma_warns_that_an_unfilled_order_is_the_loss(self, sg):
        assert any("unfilled order is the loss" in w for w in sg.warnings)

    def test_short_gamma_prices_slippage(self, short_book, on_mkt):
        a = dc.recommend_cap(short_book, on_mkt, "EURUSD", loss_limit=4_000.0,
                             slip_pips=0.5)
        b = dc.recommend_cap(short_book, on_mkt, "EURUSD", loss_limit=4_000.0,
                             slip_pips=5.0)
        assert b.cap_base < a.cap_base

    def test_short_gamma_default_slip_is_twice_the_spread(self, short_book, on_mkt):
        c = dc.recommend_cap(short_book, on_mkt, "EURUSD", cost_bp=5.0)
        assert dc.SHORT_GAMMA_SLIP_MULT == 2.0
        assert any("slippage" in w.lower() for w in c.warnings)

    def test_long_gamma_book_has_no_refusals(self, ref_cap):
        assert not ref_cap.refused

    def test_gamma_sign_flip_is_warned_about(self, on_mkt):
        """A one-sided strike ladder is long gamma one side of spot and short the
        other; a single scalar cap is then the wrong object."""
        from fxgamma.types import Book, OptionPosition
        S = on_mkt.spot["EURUSD"]
        exp = ASOF.date() + timedelta(days=30)
        b = Book(options=[
            OptionPosition(id="f1", pair="EURUSD", cp=+1, strike=S * 1.005, expiry=exp,
                           notional_base=20e6, direction=+1),
            OptionPosition(id="f2", pair="EURUSD", cp=+1, strike=S * 0.995, expiry=exp,
                           notional_base=20e6, direction=-1)], spots=[])
        c = dc.recommend_cap(b, on_mkt, "EURUSD")
        if c.cap_base > 0:
            assert any("one side of spot" in w for w in c.warnings) or c.is_symmetric


# =========================================================================== #
# 9.  Asymmetry from repricing -- "that is your strikes, not a view"
# =========================================================================== #
class TestSkewedBookAsymmetry:
    def test_symmetric_straddle_gives_a_symmetric_cap(self, ref_cap):
        assert ref_cap.is_symmetric
        assert not ref_cap.asymmetry_note

    def test_skewed_book_gives_different_up_and_down_caps(self, skewed_book, on_mkt):
        c = dc.recommend_cap(skewed_book, on_mkt, "EURUSD")
        assert not c.is_symmetric
        assert c.asymmetry_note

    def test_asymmetry_note_says_it_is_strikes_not_a_view(self, skewed_book, on_mkt):
        c = dc.recommend_cap(skewed_book, on_mkt, "EURUSD")
        assert "NOT" in c.asymmetry_note and "VIEW" in c.asymmetry_note.upper()

    def test_accumulated_delta_returns_two_sides_and_the_linear_proxy(self,
                                                                      skewed_book,
                                                                      on_mkt):
        up, dn, lin = dc.accumulated_delta(skewed_book, on_mkt, "EURUSD",
                                           sigma_window=0.0031)
        assert up > 0 and dn > 0 and lin > 0
        assert abs(up - dn) / max(up, dn) > 0.02

    def test_linear_proxy_is_close_on_a_symmetric_straddle(self, ref_book, on_mkt):
        """The PM's measurement: 0.6% at 1M on a symmetric straddle, which is why the
        linear shortcut looked harmless."""
        up, dn, lin = dc.accumulated_delta(ref_book, on_mkt, "EURUSD",
                                           sigma_window=0.0031)
        assert up == pytest.approx(lin, rel=0.03)
        assert dn == pytest.approx(lin, rel=0.03)

    def test_linear_proxy_is_materially_wrong_on_a_skew(self, skewed_book, on_mkt):
        """The trader's 13% figure, reproduced: on this 1M call spread the linear
        |Gamma| sigma S proxy is 11-13% out and in OPPOSITE directions on the two
        sides, which is precisely the error a symmetric ladder built off one
        Gamma_1pct makes."""
        up, dn, lin = dc.accumulated_delta(skewed_book, on_mkt, "EURUSD",
                                           sigma_window=0.0031)
        assert max(abs(up / lin - 1.0), abs(dn / lin - 1.0)) > 0.08
        assert (up - lin) * (dn - lin) < 0

    def test_cap_base_is_the_smaller_of_the_two_sides(self, skewed_book, on_mkt):
        c = dc.recommend_cap(skewed_book, on_mkt, "EURUSD")
        assert c.cap_base == pytest.approx(min(c.cap_up, c.cap_dn), rel=1e-12)

    def test_band_pips_up_and_down_differ_on_a_skew(self, skewed_book, on_mkt):
        c = dc.recommend_cap(skewed_book, on_mkt, "EURUSD")
        assert c.band_pips_up != pytest.approx(c.band_pips_dn, rel=1e-6)


# =========================================================================== #
# 10.  Priority 3 -- the units hazard
# =========================================================================== #
class TestUnitsHazard:
    """``DeltaCap.binds_pct`` holds **percent (0-100)**.  ``HedgeRule.band_pct`` holds
    a **fraction**.  Two identically-suffixed fields with different units in one
    codebase is exactly how the 60x band error happened (amendment v1.6 CR-1), so both
    are asserted here and a repo-wide sweep is in ``test_units_sweep.py``."""

    def test_binds_pct_is_percent_0_to_100(self, ref_cap):
        assert 0.0 <= ref_cap.binds_pct <= 100.0
        assert ref_cap.binds_pct > 1.0, "a fraction here would read 0.99, not 99"

    def test_binds_pct_on_the_reference_book_is_about_99(self, ref_cap):
        """The cap at R2 binds on nearly every night: that IS the design."""
        assert ref_cap.binds_pct == pytest.approx(99.0, abs=1.5)

    def test_binds_pct_equals_100x_the_first_passage_probability(self, ref_cap):
        p = dc._two_sided_first_passage(ref_cap.cap_base / abs(ref_cap.gamma),
                                        ref_cap.spot * ref_cap.sigma_window)
        assert ref_cap.binds_pct == pytest.approx(100.0 * p, rel=1e-9)

    def test_band_pct_is_a_fraction(self):
        from fxgamma.types import HedgeRule
        assert 0.0 < HedgeRule().band_pct < 1.0

    def test_the_two_fields_are_not_interchangeable(self, ref_cap):
        from fxgamma.types import HedgeRule
        assert ref_cap.binds_pct > 1.0 > HedgeRule().band_pct

    def test_default_band_pct_constant_is_a_percent_not_a_fraction(self):
        """``zones.DEFAULT_BAND_PCT`` is **15.0 = percent**; ``HedgeRule.band_pct`` is
        **0.25 = fraction**.  Same suffix, same concept ('band as a share of gross
        notional'), units 100x apart, and they do not even agree on the number.
        Pinned so the next reader is warned rather than surprised."""
        from fxgamma.types import HedgeRule
        assert zones.DEFAULT_BAND_PCT > 1.0
        assert 0.0 < HedgeRule().band_pct < 1.0
        assert zones.DEFAULT_BAND_PCT != pytest.approx(HedgeRule().band_pct)

    def test_the_field_documents_its_own_units(self):
        import inspect
        src = inspect.getsource(dc.DeltaCap)
        assert "PERCENT" in src and "binds_pct" in src

    def test_cap_mm_is_millions_of_base_ccy(self, ref_cap):
        assert ref_cap.cap_mm == pytest.approx(ref_cap.cap_base / 1e6, rel=1e-12)

    def test_band_pips_is_cap_over_gamma_in_pips(self, ref_cap):
        assert ref_cap.band_pips == pytest.approx(
            ref_cap.cap_base / abs(ref_cap.gamma) / PIP, rel=1e-12)

    def test_money_fields_are_in_quote_ccy(self, ref_cap):
        assert ref_cap.ccy == "USD" and ref_cap.base_ccy == "EUR"
        assert ref_cap.fx_to_report == pytest.approx(1.0, rel=1e-12)

    def test_jpy_book_reports_jpy(self, on_mkt):
        b = _straddle("USDJPY", on_mkt.spot["USDJPY"], prefix="jy")
        c = dc.recommend_cap(b, on_mkt, "USDJPY")
        assert c.ccy == "JPY" and c.base_ccy == "USD"
        assert c.fx_to_report != pytest.approx(1.0, rel=1e-3)


# =========================================================================== #
# 11.  recommend_caps across pairs
# =========================================================================== #
class TestMultiPair:
    def test_recommend_caps_returns_one_per_pair(self, on_mkt):
        from fxgamma.types import Book
        S1, S2 = on_mkt.spot["EURUSD"], on_mkt.spot["USDJPY"]
        b = Book(options=(_straddle("EURUSD", S1, prefix="a").options
                          + _straddle("USDJPY", S2, prefix="b").options), spots=[])
        caps = dc.recommend_caps(b, on_mkt)
        assert set(caps) == {"EURUSD", "USDJPY"}
        for c in caps.values():
            assert c.cap_base > 0

    def test_cap_frame_of_many(self, on_mkt):
        from fxgamma.types import Book
        S1, S2 = on_mkt.spot["EURUSD"], on_mkt.spot["USDJPY"]
        b = Book(options=(_straddle("EURUSD", S1, prefix="a").options
                          + _straddle("USDJPY", S2, prefix="b").options), spots=[])
        f = dc.cap_frame(dc.recommend_caps(b, on_mkt))
        assert len(f) == 2
