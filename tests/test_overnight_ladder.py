"""``fxgamma.portfolio.overnight`` -- the module that produces the orders.

Everything in here protects a number or a ruling that is already written down.  The
module is 1,436 lines that shipped with zero tests and it does not display a figure,
it *emits a trade*, so the bar is the one in ``docs/08_overnight_gamma.md``:

* session **variance** time, not clock time -- 0.382 of a day against 0.583 of the
  clock, theta/variance ratio **1.53** (the amendment that supersedes the PM's 1.72);
* ``p_touch`` is the **reflection principle** ``2 N(-d/sigma)``, roughly twice the
  terminal probability.  This was a live correction and a one-character slip turns
  every rung's fill probability into half of itself;
* the order type is **derived from the local gamma sign** -- long gamma takes limits
  (an unfilled order is safe), short gamma takes stop-markets (an unfilled order *is*
  the loss);
* the cumulative delta is right at **every** rung, including after a snap, because a
  snapped rung whose clip was not recomputed leaves the delta wrong at that rung and
  at every rung beyond it;
* the spacing default now *derives* from ``deltacap.recommend_cap`` rather than
  falling back to ``rule.band_pct`` (the 15%-of-gross default is an intraday number
  and was measured to be strictly dominated overnight);
* outputs are labelled as **conversion, cost and variance reduction**, never as
  capture the ladder created -- under driftless spot the ladder does not change the
  expected P&L, it only subtracts cost and reshapes the distribution.  That last one
  is a null control and it is tested as one.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from fxgamma import conventions as cv
from fxgamma.models import gk

ov = pytest.importorskip("fxgamma.portfolio.overnight")
zones = pytest.importorskip("fxgamma.portfolio.zones")

from conftest import ASOF, _straddle  # noqa: E402

PIP = cv.pair_spec("EURUSD").pip


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def ref_rungs(ref_book, on_mkt):
    return ov.overnight_ladder(ref_book, on_mkt, "EURUSD")


@pytest.fixture(scope="module")
def ref_summary(ref_rungs, ref_book, on_mkt):
    return ov.ladder_summary(ref_rungs, ref_book, on_mkt, "EURUSD")


@pytest.fixture(scope="module")
def weeknight():
    return ov.passive_window(ASOF, pair="EURUSD")


# =========================================================================== #
# 1.  Session variance time -- the measured 0.382 / 0.583 / 1.53
# =========================================================================== #
class TestSessionVarianceTime:
    """docs/10: the window is **0.382** of a day's variance against **0.583** of the
    clock, so the theta/variance ratio is **1.53** (superseding the PM's 1.72, which
    assumed a 0.34 share).  Treating the window as 14/24 of a day's variance misprices
    every rung, which is the brief's own acceptance clause."""

    def test_var_fraction_is_the_measured_0382(self, weeknight):
        assert weeknight.var_fraction == pytest.approx(0.382, abs=0.004)

    def test_clock_fraction_is_14_over_24(self, weeknight):
        assert weeknight.clock_fraction == pytest.approx(14.0 / 24.0, rel=1e-9)

    def test_variance_share_is_well_below_the_clock_share(self, weeknight):
        assert weeknight.var_fraction < weeknight.clock_fraction

    def test_theta_per_variance_ratio_is_153_not_172(self, weeknight):
        """The single number that leads the screen.  1.72 was withdrawn."""
        ratio = weeknight.clock_fraction / weeknight.var_fraction
        assert ratio == pytest.approx(1.53, abs=0.03)
        assert ratio != pytest.approx(1.72, abs=0.02)

    def test_sigma_multiplier_is_1236(self, weeknight):
        """sqrt(0.583/0.382) = 1.236: the amendment states it explicitly."""
        assert math.sqrt(weeknight.clock_fraction / weeknight.var_fraction) == \
            pytest.approx(1.236, abs=0.015)

    def test_summary_reports_the_ratio(self, ref_summary):
        assert ref_summary["theta_per_var_day"] == pytest.approx(1.53, abs=0.03)

    def test_calendar_days_is_14_hours(self, weeknight):
        assert weeknight.calendar_days == pytest.approx(14.0 / 24.0, rel=1e-9)

    @pytest.mark.parametrize("pair", ["EURUSD", "USDJPY", "GBPUSD", "AUDUSD"])
    def test_every_shipped_profile_is_theta_expensive(self, pair):
        """docs/08: the ratio ranges 1.46-1.94 across plausible profiles and the
        qualitative conclusion -- overnight is theta-expensive -- never flips."""
        w = ov.passive_window(ASOF, pair=pair)
        assert 1.0 < w.clock_fraction / w.var_fraction < 2.2

    def test_profile_source_is_badged_as_modelled(self, weeknight):
        """The UI caveat the amendment requires: this is synthetic, not measured."""
        assert weeknight.profile_source == "default"

    def test_hour_profile_normalises_to_a_day(self):
        prof = ov.hour_profile("EURUSD")
        a = np.asarray(prof.array(), float)
        assert a.size == 24
        assert (a > 0).all()
        assert a.sum() == pytest.approx(24.0, rel=1e-9)

    def test_hour_profile_is_peaked_in_the_london_new_york_overlap(self):
        a = np.asarray(ov.hour_profile("EURUSD").array(), float)
        assert a[13:16].mean() > 2.0 * a[1:5].mean()
        assert ov.hour_profile("EURUSD").peak_to_trough > 2.0

    def test_hour_profile_never_silently_substitutes(self):
        """Architecture s7: an unknown pair gets the generic shape and SAYS so."""
        p = ov.hour_profile("ZZZQQQ")
        assert p.source == "default"
        assert "generic" in p.note.lower()

    def test_caller_supplied_profile_is_badged_user(self):
        p = ov.hour_profile("EURUSD", [1.0] * 24)
        assert p.source == "user"
        assert np.asarray(p.array(), float).sum() == pytest.approx(24.0, rel=1e-9)

    def test_flat_profile_makes_variance_time_equal_clock_time(self):
        """The control: with a flat hour profile the 0.382 must collapse to 0.583.
        If it does not, the variance weighting is not doing what it claims."""
        w = ov.passive_window(ASOF, pair="EURUSD", profile=[1.0] * 24)
        assert w.var_fraction == pytest.approx(w.clock_fraction, rel=0.02)

    def test_session_variance_weight_of_a_whole_day_is_one(self):
        s = ASOF.replace(hour=0, minute=0)
        w = ov.session_variance_weight(s, s + timedelta(days=1), "EURUSD")
        assert w == pytest.approx(1.0, abs=0.02)

    def test_session_variance_weight_is_additive_over_a_split(self):
        s = ASOF.replace(hour=16, minute=0)
        e = s + timedelta(hours=14)
        m = s + timedelta(hours=6)
        whole = ov.session_variance_weight(s, e, "EURUSD")
        parts = (ov.session_variance_weight(s, m, "EURUSD")
                 + ov.session_variance_weight(m, e, "EURUSD"))
        assert whole == pytest.approx(parts, rel=1e-9)

    def test_session_variance_weight_is_monotone_in_length(self):
        s = ASOF.replace(hour=16, minute=0)
        prev = 0.0
        for h in (1, 2, 4, 8, 14, 24):
            w = ov.session_variance_weight(s, s + timedelta(hours=h), "EURUSD")
            assert w > prev
            prev = w

    def test_weekend_window_carries_more_theta_than_variance(self):
        """Three calendar days of theta against roughly one day's variance."""
        fri = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        w = ov.passive_window(fri, pair="EURUSD")
        assert w.spans_weekend
        assert w.calendar_days > 2.0
        assert w.calendar_days / w.var_fraction > 4.0

    def test_weekday_window_does_not_claim_a_weekend(self, weeknight):
        assert not weeknight.spans_weekend

    def test_estimated_profile_is_badged_differently(self):
        iy = pytest.importorskip("fxgamma.data.intraday_yahoo")
        bars = iy.synthetic_intraday("EURUSD", days=300, interval="1h", seed=3)
        prof = ov.estimate_hour_profile(bars, "EURUSD")
        w = ov.passive_window(ASOF, pair="EURUSD", profile=prof)
        assert w.profile_source == "estimated"
        assert 0.15 < w.var_fraction < 0.75


# =========================================================================== #
# 2.  p_touch -- the reflection principle.  A live correction; easy to regress.
# =========================================================================== #
class TestReflectionPrinciple:
    """``p_touch`` must be the FIRST-PASSAGE probability ``2 N(-d/sigma)``, not the
    terminal ``N(-d/sigma)``.  Halving every rung's fill probability halves the
    expected number of fills, which is the number the whole ladder is costed on."""

    SIG = 0.08
    T = 0.382 / 252.0

    @pytest.mark.parametrize("pips", [1, 2, 5, 10, 20, 40, 80])
    def test_touch_equals_two_n_minus_d_over_sigma(self, pips):
        S = 1.1650
        B = S + pips * PIP
        p = zones.touch_probability(S, B, self.T, self.SIG, drift=0.0)
        d = math.log(B / S) / (self.SIG * math.sqrt(self.T))
        # the exact GBM first passage carries nu = -sigma^2/2, worth <0.5% over a
        # 14-hour window; the reflection principle is the limit it must sit on.
        assert p == pytest.approx(2.0 * gk._norm_cdf(-abs(d)), rel=6e-3)

    @pytest.mark.parametrize("pips", [5, 10, 20, 40, 80])
    def test_touch_is_about_twice_the_terminal_probability(self, pips):
        S = 1.1650
        B = S + pips * PIP
        p = zones.touch_probability(S, B, self.T, self.SIG, drift=0.0)
        d = math.log(B / S) / (self.SIG * math.sqrt(self.T))
        terminal = gk._norm_cdf(-abs(d))
        assert p / terminal == pytest.approx(2.0, abs=0.02)

    @pytest.mark.parametrize("pips", [5, 20, 60])
    def test_touch_is_symmetric_up_and_down_when_driftless(self, pips):
        S = 1.1650
        up = zones.touch_probability(S, S + pips * PIP, self.T, self.SIG, drift=0.0)
        dn = zones.touch_probability(S, S - pips * PIP, self.T, self.SIG, drift=0.0)
        assert up == pytest.approx(dn, rel=2e-2)

    def test_touch_is_one_at_spot(self):
        assert zones.touch_probability(1.1650, 1.1650, self.T, self.SIG) == 1.0

    @pytest.mark.parametrize("pips", [1, 10, 100])
    def test_touch_is_monotone_in_time(self, pips):
        S = 1.1650
        B = S + pips * PIP
        ps = [zones.touch_probability(S, B, t, self.SIG) for t in
              (self.T / 8, self.T / 4, self.T, 4 * self.T)]
        assert all(a <= b + 1e-12 for a, b in zip(ps, ps[1:]))

    @pytest.mark.parametrize("pips", [5, 20, 60])
    def test_touch_is_monotone_in_distance(self, pips):
        S = 1.1650
        near = zones.touch_probability(S, S + pips * PIP, self.T, self.SIG)
        far = zones.touch_probability(S, S + 2 * pips * PIP, self.T, self.SIG)
        assert far < near

    def test_ladder_p_touch_matches_the_reflection_formula(self, ref_rungs, on_mkt,
                                                           ref_book):
        """The rungs the user actually reads, not just the kernel."""
        bandopt = pytest.importorskip("fxgamma.portfolio.bandopt")
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        S = on_mkt.spot["EURUSD"]
        T_eff = ov.passive_window(on_mkt.asof, pair="EURUSD").var_fraction / 252.0
        for r in ref_rungs:
            d = math.log(r.level / S) / (bg.sigma * math.sqrt(T_eff))
            assert r.p_touch == pytest.approx(2.0 * gk._norm_cdf(-abs(d)), abs=0.02)

    def test_p_touch_decreases_with_rung_index(self, ref_rungs):
        for sgn in (+1, -1):
            side = sorted([r for r in ref_rungs if math.copysign(1, r.pips_from_spot) == sgn],
                          key=lambda r: r.k)
            assert all(a.p_touch > b.p_touch for a, b in zip(side, side[1:]))

    def test_first_rung_p_touch_is_far_above_one_half(self, ref_rungs):
        """At 0.5 sigma the reflection answer is ~0.62; the terminal answer is ~0.31.
        This single assertion separates the two formulas on the shipped book."""
        inner = max(ref_rungs, key=lambda r: r.p_touch)
        assert inner.p_touch > 0.55

    def test_summary_p_touch_any_is_the_inner_rung(self, ref_rungs, ref_summary):
        assert ref_summary["p_touch_any"] == pytest.approx(
            max(r.p_touch for r in ref_rungs), rel=1e-9)


# =========================================================================== #
# 3.  Expected crossings -- Tanaka local time, and kappa stays at the null
# =========================================================================== #
class TestExpectedCrossings:
    """``E[N(x)] = E[L_T(x)]/h``.  docs/10 rules that roughness is NOT forecastable
    from daily bars, so ``kappa`` ships at the Brownian 1.0 and is an override only."""

    def test_local_time_at_the_money(self):
        sd = 0.0036
        assert ov.expected_local_time(0.0, sd) == pytest.approx(
            2.0 * sd * gk._norm_pdf(0.0), rel=1e-12)

    def test_local_time_integrates_to_the_quadratic_variation(self):
        """int E[L_T(x)] dx = sd^2.  The identity that ties crossings to QV."""
        sd = 0.0036
        xs = np.linspace(-9 * sd, 9 * sd, 4001)
        integral = np.trapezoid(ov.expected_local_time(xs, sd), xs)
        assert integral == pytest.approx(sd * sd, rel=2e-3)

    def test_local_time_is_even(self):
        sd = 0.0036
        for x in (0.0005, 0.002, 0.01):
            assert ov.expected_local_time(x, sd) == pytest.approx(
                ov.expected_local_time(-x, sd), rel=1e-12)

    def test_local_time_decays_with_distance(self):
        sd = 0.0036
        vals = [ov.expected_local_time(u * sd, sd) for u in (0, 1, 2, 3)]
        assert all(a > b for a, b in zip(vals, vals[1:]))

    def test_crossings_scale_inversely_with_spacing(self):
        sd, h = 0.0036, 0.0018
        assert ov.expected_crossings(0.0, sd, h) == pytest.approx(
            2.0 * ov.expected_crossings(0.0, sd, 2 * h), rel=1e-12)

    def test_crossings_scale_linearly_in_kappa(self):
        sd, h = 0.0036, 0.0018
        base = ov.expected_crossings(0.0, sd, h, 1.0)
        assert ov.expected_crossings(0.0, sd, h, 2.5) == pytest.approx(2.5 * base, rel=1e-12)

    def test_kappa_defaults_to_the_brownian_null(self, ref_rungs):
        assert all(r.kappa == 1.0 for r in ref_rungs)

    def test_summary_states_the_brownian_assumption(self, ref_summary):
        assert ref_summary["kappa"] == 1.0

    def test_crossings_are_not_a_probability(self):
        """Documented explicitly: the inner rung of a tight ladder fills repeatedly."""
        assert ov.expected_crossings(0.0, 0.0036, 0.0002) > 1.0


# =========================================================================== #
# 4.  Order type is DERIVED from the local gamma sign
# =========================================================================== #
class TestOrderTypeIsDerived:
    """PM amendment, accepted: long gamma takes **limits** (an unfilled order is
    safe), short gamma takes **stop-markets** (an unfilled order *is* the loss).
    Never chosen by the caller."""

    def test_long_gamma_book_emits_limits(self, ref_rungs):
        assert {r.order_type for r in ref_rungs} == {"limit"}

    def test_short_gamma_book_emits_stops(self, short_book, on_mkt):
        rungs = ov.overnight_ladder(short_book, on_mkt, "EURUSD")
        assert rungs
        assert {r.order_type for r in rungs} == {"stop"}

    def test_order_type_flips_with_direction_and_nothing_else(self, ref_book,
                                                              short_book, on_mkt):
        a = ov.overnight_ladder(ref_book, on_mkt, "EURUSD")
        b = ov.overnight_ladder(short_book, on_mkt, "EURUSD")
        assert [r.order_type for r in a] != [r.order_type for r in b]
        assert len(a) == len(b)

    def test_no_order_type_is_left_blank(self, ref_rungs):
        assert all(r.order_type for r in ref_rungs)

    def test_sell_above_and_buy_below(self, ref_rungs, on_mkt):
        """A long-gamma ladder sells into strength and buys weakness."""
        S = on_mkt.spot["EURUSD"]
        for r in ref_rungs:
            assert r.side == (-1 if r.level > S else +1)


# =========================================================================== #
# 5.  Cumulative delta -- correct at every rung, including after a snap
# =========================================================================== #
class TestCumulativeDelta:
    """Clips come off the **repriced** delta profile, never ``Gamma_1pct x spacing``.
    A snapped rung whose clip is not recomputed leaves the cumulative delta wrong at
    that rung and at every rung beyond it -- the PM amendment says so in terms."""

    def _levels_near(self, rungs, spec_pip, offset_pips=3.0):
        return pd.DataFrame({
            "level": [r.level + offset_pips * spec_pip for r in rungs],
            "kind": ["prior_high"] * len(rungs),
            "strength": [1.0] * len(rungs),
        })

    def test_cum_delta_returns_to_target_at_every_rung(self, ref_rungs):
        """Hedging to target means that after each fill you are AT target -- 0 here.
        A carried (un-repriced) clip shows up immediately as a non-zero residual."""
        for r in ref_rungs:
            assert abs(r.cum_delta_base) < 1.0

    def test_cum_delta_is_right_on_a_skewed_book(self, skewed_book, on_mkt):
        rungs = ov.overnight_ladder(skewed_book, on_mkt, "EURUSD")
        assert rungs
        for r in rungs:
            assert abs(r.cum_delta_base) < 1.0

    def test_clip_equals_the_repriced_delta_difference(self, ref_rungs, ref_book, on_mkt):
        risk = pytest.importorskip("fxgamma.portfolio.risk")
        S = on_mkt.spot["EURUSD"]
        lad = risk.spot_ladder(ref_book, on_mkt, "EURUSD", lo_pct=-2.0, hi_pct=2.0,
                               n=1201, sticky="strike")
        Sg = lad["spot"].to_numpy(float)
        Dg = lad["delta_base"].to_numpy(float)
        for sgn in (+1, -1):
            side = sorted([r for r in ref_rungs
                           if math.copysign(1, r.pips_from_spot) == sgn],
                          key=lambda r: r.k)
            prev = float(np.interp(S, Sg, Dg))
            for r in side:
                here = float(np.interp(r.level, Sg, Dg))
                assert r.clip_base == pytest.approx(abs(here - prev), rel=2e-3)
                prev = here

    def test_clip_differs_from_the_linear_gamma_approximation_on_a_skew(
            self, skewed_book, on_mkt):
        """The whole reason clips are repriced: on a skewed book the linear proxy is
        materially wrong, which is the trader's 13% point."""
        bandopt = pytest.importorskip("fxgamma.portfolio.bandopt")
        rungs = ov.overnight_ladder(skewed_book, on_mkt, "EURUSD")
        bg = bandopt.book_gamma(skewed_book, on_mkt, "EURUSD")
        up = [r for r in rungs if r.pips_from_spot > 0]
        dn = [r for r in rungs if r.pips_from_spot < 0]
        lin_up = abs(bg.gamma) * abs(up[0].spacing_pips) * PIP
        lin_dn = abs(bg.gamma) * abs(dn[0].spacing_pips) * PIP
        err = max(abs(up[0].clip_base / lin_up - 1.0), abs(dn[0].clip_base / lin_dn - 1.0))
        assert err > 0.005, "a repriced clip must not coincide with Gamma*spacing here"

    def test_symmetric_straddle_clips_are_close_to_linear(self, ref_rungs, ref_book,
                                                          on_mkt):
        """...and on a symmetric 1M straddle it IS close (the PM measured 0.6%),
        which is exactly why the error looked harmless."""
        bandopt = pytest.importorskip("fxgamma.portfolio.bandopt")
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        inner = min(ref_rungs, key=lambda r: abs(r.pips_from_spot))
        lin = abs(bg.gamma) * abs(inner.spacing_pips) * PIP
        assert inner.clip_base == pytest.approx(lin, rel=0.04)

    def test_snapping_moves_rungs_and_keeps_cum_delta_exact(self, ref_book, on_mkt,
                                                            ref_rungs):
        """THE regression: a snapped clip must be **repriced**, not carried."""
        lv = self._levels_near(ref_rungs, PIP, 3.0)
        snapped = ov.overnight_ladder(ref_book, on_mkt, "EURUSD", levels=lv,
                                      snap=True, snap_max_pips=8.0)
        assert any(r.anchor for r in snapped), "the fixture should snap something"
        assert [r.level for r in snapped] != [r.level for r in ref_rungs]
        for r in snapped:
            assert abs(r.cum_delta_base) < 1.0

    def test_snapped_clips_change_when_the_level_changes(self, ref_book, on_mkt,
                                                          ref_rungs):
        lv = self._levels_near(ref_rungs, PIP, 6.0)
        snapped = ov.overnight_ladder(ref_book, on_mkt, "EURUSD", levels=lv,
                                      snap=True, snap_max_pips=10.0)
        moved = [(a, b) for a, b in zip(ref_rungs, snapped)
                 if abs(a.level - b.level) > 0.5 * PIP]
        assert moved, "the fixture should move at least one rung"
        assert any(abs(a.clip_base - b.clip_base) > 1.0 for a, b in moved), \
            "a moved rung whose clip did not move is a carried clip"

    def test_snapping_is_off_by_default(self, ref_book, on_mkt, ref_rungs):
        """docs/10 RULED OUT snapping as a default: 4 of 80 cells, zero replication."""
        lv = self._levels_near(ref_rungs, PIP, 3.0)
        same = ov.overnight_ladder(ref_book, on_mkt, "EURUSD", levels=lv)
        assert all(not r.anchor for r in same)
        assert [r.level for r in same] == [r.level for r in ref_rungs]

    def test_snapped_rung_rests_inside_the_anchor(self, ref_book, on_mkt, ref_rungs):
        lv = self._levels_near(ref_rungs, PIP, 3.0)
        snapped = ov.overnight_ladder(ref_book, on_mkt, "EURUSD", levels=lv,
                                      snap=True, snap_max_pips=8.0, snap_inside_pips=1.0)
        S = on_mkt.spot["EURUSD"]
        anchors = set(np.round(lv["level"].to_numpy(float), 10))
        for r in snapped:
            if not r.anchor:
                continue
            nearest = min(anchors, key=lambda a: abs(a - r.level))
            sgn = 1.0 if r.level > S else -1.0
            assert sgn * (r.level - nearest) < 0, "a snapped rung must rest INSIDE"

    def test_cumulative_delta_never_exceeds_the_cap_between_rungs(self, ref_rungs,
                                                                  ref_book, on_mkt):
        bandopt = pytest.importorskip("fxgamma.portfolio.bandopt")
        dc = pytest.importorskip("fxgamma.portfolio.deltacap")
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        cap = dc.recommend_cap(ref_book, on_mkt, "EURUSD").cap_base
        for r in ref_rungs:
            assert abs(bg.gamma) * abs(r.spacing_pips) * PIP <= cap * 1.02


# =========================================================================== #
# 6.  The spacing default now DERIVES from deltacap.recommend_cap
# =========================================================================== #
class TestSpacingDerivesFromTheDeltaCap:
    """docs/13 replaced the shipped 15%-of-gross default: on the reference book it is
    EUR 3.0mm, never binds overnight, costs USD 10/night and improves CVaR-95 by
    exactly zero.  ``overnight_ladder`` now derives its default from
    ``deltacap.recommend_cap``, with the old fallback kept only if that fails."""

    def test_default_spacing_equals_the_recommended_cap(self, ref_rungs, ref_book,
                                                        on_mkt):
        dc = pytest.importorskip("fxgamma.portfolio.deltacap")
        bandopt = pytest.importorskip("fxgamma.portfolio.bandopt")
        cap = dc.recommend_cap(ref_book, on_mkt, "EURUSD").cap_base
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        inner = min(ref_rungs, key=lambda r: abs(r.pips_from_spot))
        assert abs(bg.gamma) * inner.spacing_pips * PIP == pytest.approx(cap, rel=1e-6)

    def test_default_spacing_is_not_the_15pct_of_gross_fallback(self, ref_rungs,
                                                                 ref_book, on_mkt):
        bandopt = pytest.importorskip("fxgamma.portfolio.bandopt")
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        old = zones.DEFAULT_BAND_PCT / 100.0 * bg.gross_notional
        inner = min(ref_rungs, key=lambda r: abs(r.pips_from_spot))
        implied = abs(bg.gamma) * inner.spacing_pips * PIP
        assert implied < 0.5 * old

    def test_derived_default_is_the_18_pip_reference_band(self, ref_rungs):
        """EUR 0.54mm on the reference book is an 18-pip ladder band."""
        inner = min(ref_rungs, key=lambda r: abs(r.pips_from_spot))
        assert inner.spacing_pips == pytest.approx(18.0, abs=1.0)

    def test_explicit_cap_overrides_the_derivation(self, ref_book, on_mkt):
        bandopt = pytest.importorskip("fxgamma.portfolio.bandopt")
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        rungs = ov.overnight_ladder(ref_book, on_mkt, "EURUSD",
                                    max_overnight_delta=0.25e6)
        inner = min(rungs, key=lambda r: abs(r.pips_from_spot))
        assert abs(bg.gamma) * inner.spacing_pips * PIP == pytest.approx(0.25e6, rel=1e-6)

    def test_the_cap_beats_the_analytic_band(self, ref_book, on_mkt):
        """docs/09 RULING: overnight, ignore the band and let the cap bind."""
        bandopt = pytest.importorskip("fxgamma.portfolio.bandopt")
        band = bandopt.optimal_band(ref_book, on_mkt, "EURUSD", horizon_days=0.382)
        rungs = ov.overnight_ladder(ref_book, on_mkt, "EURUSD")
        inner = min(rungs, key=lambda r: abs(r.pips_from_spot))
        assert inner.spacing_pips < band.band_pips
        assert "DELTA CAP" in rungs[0].note

    def test_caller_supplied_band_pips_is_honoured_below_the_cap(self, ref_book, on_mkt):
        rungs = ov.overnight_ladder(ref_book, on_mkt, "EURUSD", band_pips=9.0)
        inner = min(rungs, key=lambda r: abs(r.pips_from_spot))
        assert inner.spacing_pips == pytest.approx(9.0, rel=1e-9)

    def test_clip_floor_binds_before_the_analytic_optimum(self, on_mkt):
        """The trader's point: the clip floor binds before the analytic optimum does."""
        small = _straddle("EURUSD", on_mkt.spot["EURUSD"], leg=0.4e6, prefix="tiny")
        rungs = ov.overnight_ladder(small, on_mkt, "EURUSD")
        assert rungs
        assert "MIN CLIP" in rungs[0].note

    @pytest.mark.regression
    def test_emitted_clips_are_at_least_one_dealable_lot(self, on_mkt):
        """FINDING (QA-1, the most expensive one in this module).  ``min_clip_base``
        is enforced as a **spot distance** (``min_clip_base / |Gamma|``) but the clip
        is then read off the repriced delta profile, where gamma decays away from the
        money.  The emitted clips therefore come out BELOW one standard lot: 36k on a
        0.4mm/leg book and 87k on a 1.0mm/leg book against a 100k floor.  Those are
        orders the platform will reject, on the one output of this project that is a
        trade rather than a number."""
        for leg in (0.4e6, 1.0e6):
            small = _straddle("EURUSD", on_mkt.spot["EURUSD"], leg=leg,
                              prefix=f"lot{leg:.0f}")
            rungs = ov.overnight_ladder(small, on_mkt, "EURUSD",
                                        min_clip_base=ov.RETAIL_LOT_BASE)
            if not rungs:
                continue
            assert min(r.clip_base for r in rungs) >= ov.RETAIL_LOT_BASE * 0.999, (
                f"leg={leg:,.0f}: smallest emitted clip is "
                f"{min(r.clip_base for r in rungs):,.0f}, under one standard lot")

    @pytest.mark.regression
    def test_no_ladder_verdict_is_honoured_by_the_ladder_builder(self, on_mkt):
        """FINDING (QA-2).  ``deltacap.recommend_cap`` returns ``no_ladder=True`` --
        'there is nothing to hedge overnight and no cap will change that' -- while
        ``overnight_ladder`` on the same book still emits eight rungs.  The two
        halves of the feature disagree about whether the user should trade."""
        dc = pytest.importorskip("fxgamma.portfolio.deltacap")
        small = _straddle("EURUSD", on_mkt.spot["EURUSD"], leg=0.4e6, prefix="nl")
        cap = dc.recommend_cap(small, on_mkt, "EURUSD")
        assert cap.no_ladder, "fixture precondition: this book has no overnight ladder"
        assert ov.overnight_ladder(small, on_mkt, "EURUSD") == [], (
            "recommend_cap says there is no ladder tonight; overnight_ladder emits one")

    def test_note_records_which_constraint_bound(self, ref_rungs):
        assert ref_rungs[0].note
        assert "spacing:" in ref_rungs[0].note

    def test_spacing_source_is_reported_in_the_summary(self, ref_summary):
        assert ref_summary["spacing_source"]

    def test_empty_book_returns_no_ladder(self, on_mkt):
        from fxgamma.types import Book
        assert ov.overnight_ladder(Book(options=[], spots=[]), on_mkt, "EURUSD") == []

    @pytest.mark.regression
    def test_default_note_does_not_claim_the_band_pct_fallback(self, ref_rungs):
        """FINDING (QA-3, cosmetic-but-user-facing).  When no cap is supplied the cap
        is now DERIVED from ``recommend_cap``; the note still says it 'defaulted to
        HedgeRule.band_pct x gross notional' and tells the user to type a number they
        no longer need to type.  Pinned as a finding: the derived path must not
        advertise the withdrawn fallback."""
        note = ref_rungs[0].note
        assert "band_pct" not in note, (
            "the ladder note still advertises the withdrawn 15%-of-gross fallback "
            "even though the cap was derived from deltacap.recommend_cap")


# =========================================================================== #
# 7.  Labelling -- conversion, not capture.  This is a NULL CONTROL.
# =========================================================================== #
class TestLabellingAndTheDriftlessNull:
    """PM ruling, binding: under driftless spot the expected P&L is the same with or
    without any ladder.  Hedging subtracts cost and reshapes the distribution.  So at
    **zero cost** the ladder's marginal effect must be **exactly zero at every band**
    -- a zero-cost, zero-edge null control, and the shape of the six manufactured
    effects this project has already caught."""

    @pytest.mark.parametrize("band_pips", [6.0, 10.0, 18.0, 40.0, 90.0])
    def test_zero_cost_ladder_has_exactly_zero_marginal_pnl(self, ref_book, on_mkt,
                                                            band_pips):
        rungs = ov.overnight_ladder(ref_book, on_mkt, "EURUSD", cost_bp=0.0,
                                    band_pips=band_pips)
        s = ov.ladder_summary(rungs, ref_book, on_mkt, "EURUSD")
        assert s["exp_cost"] == pytest.approx(0.0, abs=1e-9)
        assert s["marginal_vs_no_ladder"] == pytest.approx(0.0, abs=1e-9)

    @pytest.mark.parametrize("band_pips", [6.0, 10.0, 18.0, 40.0, 90.0])
    def test_zero_cost_net_is_the_unladdered_carry_at_every_band(self, ref_book,
                                                                  on_mkt, band_pips):
        """The band cannot move the expected night.  If it does, something is
        manufacturing an effect."""
        rungs = ov.overnight_ladder(ref_book, on_mkt, "EURUSD", cost_bp=0.0,
                                    band_pips=band_pips)
        s = ov.ladder_summary(rungs, ref_book, on_mkt, "EURUSD")
        assert s["net"] == pytest.approx(s["carry"], abs=1e-6)

    @pytest.mark.parametrize("cost_bp", [0.2, 1.0, 5.0, 20.0])
    def test_marginal_is_exactly_minus_the_cost(self, ref_book, on_mkt, cost_bp):
        rungs = ov.overnight_ladder(ref_book, on_mkt, "EURUSD", cost_bp=cost_bp)
        s = ov.ladder_summary(rungs, ref_book, on_mkt, "EURUSD")
        assert s["marginal_vs_no_ladder"] == pytest.approx(-s["exp_cost"], rel=1e-12)

    def test_summary_never_calls_it_capture(self, ref_summary):
        """The withdrawn worked example called it 'expected capture'.  It is a
        conversion of mark-to-market into cash."""
        assert "realised_conversion" in ref_summary
        assert "capture" not in ref_summary

    def test_conversion_is_positive_for_a_long_gamma_book(self, ref_summary):
        assert ref_summary["realised_conversion"] > 0

    def test_variance_reduction_is_reported(self, ref_summary):
        assert 0.0 < ref_summary["sd_reduction_pct"] < 100.0
        assert ref_summary["sd_overnight_with_ladder"] < \
            ref_summary["sd_overnight_no_ladder"]

    def test_tighter_ladders_reduce_variance_more(self, ref_book, on_mkt):
        prev = 101.0
        for bp in (6.0, 12.0, 24.0, 60.0):
            r = ov.overnight_ladder(ref_book, on_mkt, "EURUSD", band_pips=bp,
                                    max_overnight_delta=50e6)
            s = ov.ladder_summary(r, ref_book, on_mkt, "EURUSD")
            assert s["sd_reduction_pct"] < prev
            prev = s["sd_reduction_pct"]

    def test_theta_is_pro_rata_not_a_full_day(self, ref_summary):
        """The withdrawn example charged a full day's theta (USD 2,870) against a
        14-hour window; the correct figure was USD 1,673.  Theta must be
        ``theta_per_day * calendar_days``."""
        assert ref_summary["calendar_days"] == pytest.approx(14 / 24, rel=1e-9)
        bandopt = pytest.importorskip("fxgamma.portfolio.bandopt")
        assert abs(ref_summary["theta"]) < abs(ref_summary["theta"]) / (14 / 24) * 1.001

    def test_theta_equals_daily_theta_times_calendar_days(self, ref_book, on_mkt,
                                                          ref_summary):
        bandopt = pytest.importorskip("fxgamma.portfolio.bandopt")
        bg = bandopt.book_gamma(ref_book, on_mkt, "EURUSD")
        assert ref_summary["theta"] == pytest.approx(
            bg.theta * ref_summary["calendar_days"], rel=1e-9)

    def test_carry_is_gamma_pnl_plus_theta(self, ref_summary):
        assert ref_summary["carry"] == pytest.approx(
            ref_summary["gamma_pnl"] + ref_summary["theta"], rel=1e-12)

    def test_reference_night_is_negative_carry(self, ref_summary):
        """The structural fact that leads the screen: on many nights holding gamma
        overnight is negative carry and the ladder is risk control, not monetisation."""
        assert ref_summary["negative_carry"] is True
        assert ref_summary["carry"] < 0


# =========================================================================== #
# 8.  Crossover vol -- the go/no-go
# =========================================================================== #
@pytest.fixture(scope="module")
def xo(ref_book, on_mkt):
    return ov.crossover_vol(ref_book, on_mkt, "EURUSD")


class TestCrossoverVol:
    """Required output per the amendment: the ATM at which the window breakeven
    equals the forecast range.  docs/09 measures EURUSD at **7.73%** against 7.96%
    marked, i.e. long gamma is negative overnight carry."""

    def test_eurusd_weeknight_crossover_is_773pct(self, xo):
        assert xo["crossover_vol"] == pytest.approx(0.0773, abs=0.0008)

    def test_marked_vol_is_above_the_crossover(self, xo):
        assert xo["atm_now"] > xo["crossover_vol"]
        assert xo["negative_carry"] is True

    def test_ratio_matches_the_closed_form_calendar_fact(self, xo, weeknight):
        """sigma*/sigma = sqrt(365 vf / (252 D_cal)) -- book-independent."""
        closed = math.sqrt(365.0 * weeknight.var_fraction
                           / (252.0 * weeknight.calendar_days))
        assert xo["ratio"] == pytest.approx(closed, rel=0.01)

    def test_crossover_ratio_is_not_the_stale_092(self, xo):
        """FINDING (QA-4).  ``crossover_vol``'s own docstring still quotes 0.92 and
        '~9% over implied' -- the pre-amendment numbers computed at a 0.34 variance
        share.  At the measured 0.382 the code returns 0.971 / ~3%.  The docstring is
        stale on the module's single most decision-useful output."""
        assert xo["ratio"] == pytest.approx(0.971, abs=0.01)

    def test_crossover_is_book_independent_in_ratio(self, on_mkt):
        a = ov.crossover_vol(_straddle("EURUSD", on_mkt.spot["EURUSD"], leg=5e6,
                                       prefix="a"), on_mkt, "EURUSD")
        b = ov.crossover_vol(_straddle("EURUSD", on_mkt.spot["EURUSD"], leg=40e6,
                                       prefix="b"), on_mkt, "EURUSD")
        assert a["ratio"] == pytest.approx(b["ratio"], rel=0.01)

    def test_weekend_crossover_is_much_lower(self, ref_book, on_mkt):
        fri = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        w = ov.passive_window(fri, pair="EURUSD")
        xo = ov.crossover_vol(ref_book, on_mkt, "EURUSD", window=w)
        assert xo["ratio"] < 0.65

    def test_verdict_names_risk_control_not_monetisation(self, xo):
        assert "RISK CONTROL" in xo["verdict"]

    def test_short_gamma_verdict_is_about_the_tail(self, short_book, on_mkt):
        xo = ov.crossover_vol(short_book, on_mkt, "EURUSD")
        assert "SHORT GAMMA" in xo["verdict"]

    def test_breakeven_move_exceeds_the_forecast_range(self, xo):
        assert xo["breakeven_move_pips"] > xo["sigma_window_pips"]
        assert xo["required_vs_forecast"] > 1.0


# =========================================================================== #
# 9.  Cost sensitivity and the no-OTC default
# =========================================================================== #
class TestCostIsAnExplicitInput:
    """``zones.COST_BP`` is an interbank table and this user has no OTC access; the
    trader puts their real all-in cost 15-40x higher.  The ladder must default to the
    retail tier and show its sensitivity."""

    def test_default_tier_is_retail_not_interbank(self, ref_rungs):
        bandopt = pytest.importorskip("fxgamma.portfolio.bandopt")
        assert ref_rungs[0].cost_bp == pytest.approx(bandopt.cost_bp_for("EURUSD", "retail"))
        assert ref_rungs[0].cost_bp > 10.0 * zones.COST_BP["EURUSD"]

    def test_retail_is_15_to_40x_interbank(self):
        bandopt = pytest.importorskip("fxgamma.portfolio.bandopt")
        for pair in ("EURUSD", "USDJPY", "GBPUSD"):
            r = bandopt.cost_bp_for(pair, "retail") / zones.COST_BP[pair]
            assert 8.0 <= r <= 45.0

    def test_cost_sensitivity_table_spans_both_tiers(self, ref_book, on_mkt):
        t = ov.ladder_cost_sensitivity(ref_book, on_mkt, "EURUSD")
        assert len(t) >= 4
        assert t["cost_bp"].min() <= 0.5 and t["cost_bp"].max() >= 10.0

    def test_cost_rises_monotonically_with_the_tier(self, ref_book, on_mkt):
        t = ov.ladder_cost_sensitivity(ref_book, on_mkt, "EURUSD")
        assert t["exp_cost"].is_monotonic_increasing

    def test_net_falls_monotonically_with_cost(self, ref_book, on_mkt):
        t = ov.ladder_cost_sensitivity(ref_book, on_mkt, "EURUSD")
        assert t["net"].is_monotonic_decreasing

    @pytest.mark.parametrize("cost_bp", [0.2, 1.0, 5.0, 20.0])
    def test_cost_is_linear_in_the_spread_at_fixed_spacing(self, ref_book, on_mkt,
                                                            cost_bp):
        r = ov.overnight_ladder(ref_book, on_mkt, "EURUSD", cost_bp=cost_bp,
                                band_pips=18.0, max_overnight_delta=50e6)
        s = ov.ladder_summary(r, ref_book, on_mkt, "EURUSD")
        base = ov.ladder_summary(
            ov.overnight_ladder(ref_book, on_mkt, "EURUSD", cost_bp=1.0, band_pips=18.0,
                                max_overnight_delta=50e6),
            ref_book, on_mkt, "EURUSD")
        assert s["exp_cost"] == pytest.approx(base["exp_cost"] * cost_bp / 1.0, rel=1e-9)

    def test_rung_costs_sum_to_the_summary_cost(self, ref_rungs, ref_summary):
        assert sum(r.exp_cost for r in ref_rungs) == pytest.approx(
            ref_summary["exp_cost"], rel=1e-9)


# =========================================================================== #
# 10.  Shapes, frames, formatting and absent-safety
# =========================================================================== #
class TestLadderShapeAndContract:
    def test_n_rungs_per_side(self, ref_book, on_mkt):
        for n in (1, 2, 4, 6):
            r = ov.overnight_ladder(ref_book, on_mkt, "EURUSD", n_rungs=n)
            assert len(r) == 2 * n

    def test_rungs_are_sorted_high_to_low(self, ref_rungs):
        assert [r.level for r in ref_rungs] == sorted(
            [r.level for r in ref_rungs], reverse=True)

    def test_rungs_straddle_spot(self, ref_rungs, on_mkt):
        S = on_mkt.spot["EURUSD"]
        assert any(r.level > S for r in ref_rungs)
        assert any(r.level < S for r in ref_rungs)

    def test_every_rung_is_finite_and_positive(self, ref_rungs):
        for r in ref_rungs:
            assert r.level > 0 and np.isfinite(r.level)
            assert r.clip_base > 0 and np.isfinite(r.clip_base)
            assert 0.0 <= r.p_touch <= 1.0
            assert np.isfinite(r.exp_pnl)

    def test_rung_is_frozen(self, ref_rungs):
        with pytest.raises(Exception):
            ref_rungs[0].level = 1.0

    def test_ladder_frame_round_trips(self, ref_rungs):
        df = ov.ladder_frame(ref_rungs)
        assert len(df) == len(ref_rungs)
        for c in ("level", "clip_base", "p_touch"):
            assert c in df.columns

    def test_format_ladder_mentions_the_pair_and_the_orders(self, ref_rungs,
                                                             ref_summary):
        txt = ov.format_ladder(ref_rungs, ref_summary)
        assert "EURUSD" in txt
        assert len(txt.splitlines()) > 4

    def test_as_dict_is_json_shaped(self, ref_rungs):
        d = ref_rungs[0].as_dict()
        assert isinstance(d, dict) and "level" in d

    def test_levels_absent_is_safe(self, ref_book, on_mkt):
        assert ov.overnight_ladder(ref_book, on_mkt, "EURUSD", levels=None)

    def test_garbage_levels_are_ignored_not_fatal(self, ref_book, on_mkt):
        junk = pd.DataFrame({"nothing": [1, 2, 3]})
        assert ov.overnight_ladder(ref_book, on_mkt, "EURUSD", levels=junk, snap=True)

    def test_range_forecast_float_is_accepted(self, ref_book, on_mkt):
        r = ov.overnight_ladder(ref_book, on_mkt, "EURUSD", range_forecast=0.004)
        s = ov.ladder_summary(r, ref_book, on_mkt, "EURUSD", range_forecast=0.004)
        assert s["sigma_window"] == pytest.approx(0.004, rel=1e-9)
        assert "forecast" in s["range_basis"].lower()

    def test_range_forecast_duck_type_is_accepted(self, ref_book, on_mkt):
        class RF:
            sigma_window = 0.005
        r = ov.overnight_ladder(ref_book, on_mkt, "EURUSD", range_forecast=RF())
        s = ov.ladder_summary(r, ref_book, on_mkt, "EURUSD", range_forecast=RF())
        assert s["sigma_window"] == pytest.approx(0.005, rel=1e-9)

    def test_usdjpy_pips_are_jpy_pips(self, on_mkt):
        b = _straddle("USDJPY", on_mkt.spot["USDJPY"], prefix="j")
        r = ov.overnight_ladder(b, on_mkt, "USDJPY")
        assert r
        spec = cv.pair_spec("USDJPY")
        for x in r:
            assert x.pips_from_spot == pytest.approx(
                (x.level - on_mkt.spot["USDJPY"]) / spec.pip, rel=1e-9)

    @pytest.mark.parametrize("pair", ["EURUSD", "USDJPY", "GBPUSD", "AUDUSD"])
    def test_every_pair_produces_a_coherent_ladder(self, on_mkt, pair):
        b = _straddle(pair, on_mkt.spot[pair], prefix=f"x{pair}")
        r = ov.overnight_ladder(b, on_mkt, pair)
        assert len(r) == 8
        s = ov.ladder_summary(r, b, on_mkt, pair)
        assert s["exp_cost"] >= 0
        assert s["marginal_vs_no_ladder"] == pytest.approx(-s["exp_cost"], rel=1e-12)

    def test_asymmetric_multipliers_produce_asymmetric_rungs(self, ref_book, on_mkt):
        r = ov.overnight_ladder(ref_book, on_mkt, "EURUSD", band_pips=12.0,
                                up_mult=1.5, down_mult=0.8, max_overnight_delta=50e6)
        up = [x for x in r if x.pips_from_spot > 0]
        dn = [x for x in r if x.pips_from_spot < 0]
        assert up[0].spacing_pips > dn[0].spacing_pips

    def test_gap_scenario_is_reported(self, ref_summary):
        assert any("gap" in k for k in ref_summary)

    def test_summary_of_an_empty_ladder_is_safe(self, ref_book, on_mkt):
        s = ov.ladder_summary([], ref_book, on_mkt, "EURUSD")
        assert isinstance(s, dict)
