"""``fxgamma/portfolio/zones.py`` and ``hedging.py`` -- pin risk, zones, hedge bands.

**Pin risk is the headline.** REQ-045's formula was the *inherited-if-ITM* delta, not
the discontinuity, and it is sign-wrong on every put.  Amendment v1.6 settled the
correct behaviour by pricing it: a long put reports ``delta_if_above 0``,
``delta_if_below -N``, ``jump +N`` -- a 20mm error on a 10mm position if you use the
spec's formula, and the wrong way round, so the trader hedges *into* the gap.  The
jump is independent of call/put; the ``cp`` in the old formula is exactly what broke it.

**Hedge bands** carry the amendment v1.6 CR-1 ruling on ``HedgeRule.band_pct`` -- a
fraction of gross notional, not a percent.  This file found it read as a percent here
(a band ~100x too tight); it was fixed in flight and these tests are the fence.  The
backtest engine has *not* been fixed, so the two sites now disagree: see
``docs/05_test_report.md`` F-8 and ``test_backtest.py``.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from fxgamma import conventions as cv
from fxgamma.portfolio import hedging, risk, zones
from fxgamma.types import Book, HedgeRule, OptionPosition, SpotPosition

from tests.conftest import in_days

pytestmark = pytest.mark.contract

N = 10e6


def _one(cp: int, direction: int, *, pair="EURUSD", K=1.1650, days=2):
    return Book(options=[OptionPosition(id="x", pair=pair, cp=cp, strike=K,
                                        expiry=in_days(days), notional_base=N,
                                        direction=direction)], spots=[])


# --------------------------------------------------------------------------- #
# pin risk -- the sign table amendment v1.6 ruled on
# --------------------------------------------------------------------------- #
class TestPinRiskSigns:
    """The four-row truth table, priced.  This is the test the old spec fails."""

    @pytest.mark.parametrize("cp,direction,above,below,jump,name", [
        (+1, +1, +N, 0.0, +N, "long call"),
        (-1, +1, 0.0, -N, +N, "long put"),
        (+1, -1, -N, 0.0, -N, "short call"),
        (-1, -1, 0.0, +N, -N, "short put"),
    ])
    def test_inherited_delta_and_jump(self, snapshot, cp, direction, above, below,
                                      jump, name):
        df = zones.pin_risk(_one(cp, direction), snapshot, "EURUSD", horizon_days=5)
        assert len(df) == 1, name
        r = df.iloc[0]
        assert float(r["delta_if_above"]) == pytest.approx(above, abs=1.0), name
        assert float(r["delta_if_below"]) == pytest.approx(below, abs=1.0), name
        assert float(r["jump_at_strike"]) == pytest.approx(jump, abs=1.0), name

    def test_the_jump_is_above_minus_below_by_construction(self, snapshot):
        for cp in (+1, -1):
            for d in (+1, -1):
                r = zones.pin_risk(_one(cp, d), snapshot, "EURUSD",
                                   horizon_days=5).iloc[0]
                assert float(r["jump_at_strike"]) == pytest.approx(
                    float(r["delta_if_above"]) - float(r["delta_if_below"]), abs=1e-6)

    def test_the_jump_does_not_depend_on_call_versus_put(self, snapshot):
        """Amendment v1.6: "the jump is correctly independent of call/put"."""
        for d in (+1, -1):
            call = zones.pin_risk(_one(+1, d), snapshot, "EURUSD",
                                  horizon_days=5).iloc[0]["jump_at_strike"]
            put = zones.pin_risk(_one(-1, d), snapshot, "EURUSD",
                                 horizon_days=5).iloc[0]["jump_at_strike"]
            assert float(call) == pytest.approx(float(put), abs=1.0)
            assert float(call) == pytest.approx(d * N, abs=1.0)

    def test_the_old_spec_formula_is_wrong_by_2n_on_a_put(self, snapshot):
        """Kept as a column so the difference is visible, not as the answer.

        REQ-045's ``sum(direction * cp * N)`` gives -10mm for a long put where the
        discontinuity is +10mm: a 20mm error on a 10mm position, with the sign that
        makes you hedge into the gap rather than out of it.
        """
        r = zones.pin_risk(_one(-1, +1), snapshot, "EURUSD", horizon_days=5).iloc[0]
        assert float(r["spec_formula_itm_delta"]) == pytest.approx(-N, abs=1.0)
        assert float(r["jump_at_strike"]) == pytest.approx(+N, abs=1.0)
        assert abs(float(r["jump_at_strike"]) - float(r["spec_formula_itm_delta"])) == \
            pytest.approx(2 * N, abs=1.0)

    def test_a_straddle_pins_no_jump_but_inherits_the_full_notional_either_way(self,
                                                                              snapshot):
        """Long call + long put on one strike: you wake up long 10mm above and short
        10mm below, and the *jump* is 20mm.  Two labelled numbers, never one."""
        b = Book(options=[
            OptionPosition(id="c", pair="EURUSD", cp=+1, strike=1.1650,
                           expiry=in_days(2), notional_base=N, direction=+1),
            OptionPosition(id="p", pair="EURUSD", cp=-1, strike=1.1650,
                           expiry=in_days(2), notional_base=N, direction=+1)], spots=[])
        r = zones.pin_risk(b, snapshot, "EURUSD", horizon_days=5).iloc[0]
        assert float(r["delta_if_above"]) == pytest.approx(+N, abs=1.0)
        assert float(r["delta_if_below"]) == pytest.approx(-N, abs=1.0)
        assert float(r["jump_at_strike"]) == pytest.approx(2 * N, abs=1.0)
        assert int(r["n_legs"]) == 2

    def test_a_risk_reversal_nets_to_zero_jump_at_different_strikes(self, snapshot):
        """Long call at 1.20, short put at 1.13: two rows, opposite jumps."""
        b = Book(options=[
            OptionPosition(id="c", pair="EURUSD", cp=+1, strike=1.2000,
                           expiry=in_days(2), notional_base=N, direction=+1),
            OptionPosition(id="p", pair="EURUSD", cp=-1, strike=1.1300,
                           expiry=in_days(2), notional_base=N, direction=-1)], spots=[])
        df = zones.pin_risk(b, snapshot, "EURUSD", horizon_days=5)
        assert len(df) == 2
        assert sorted(np.round(df["jump_at_strike"].to_numpy(float), 0)) == [-N, +N]

    def test_premium_adjusted_pairs_report_the_same_jump(self, snapshot):
        """The pa convention carries an extra ``K/S`` which is identically 1 at the
        crossing point, so the discontinuity is unchanged (and the docstring says so)."""
        r = zones.pin_risk(_one(-1, +1, pair="USDJPY", K=147.50), snapshot, "USDJPY",
                           horizon_days=5).iloc[0]
        assert bool(r["premium_adjusted"])
        assert float(r["jump_at_strike"]) == pytest.approx(+N, abs=1.0)
        assert float(r["delta_if_below"]) == pytest.approx(-N, abs=1.0)


class TestPinRiskFraming:
    def test_only_strikes_inside_the_horizon_are_reported_by_default(self, snapshot):
        b = Book(options=[
            OptionPosition(id="near", pair="EURUSD", cp=+1, strike=1.16,
                           expiry=in_days(2), notional_base=N, direction=+1),
            OptionPosition(id="far", pair="EURUSD", cp=+1, strike=1.20,
                           expiry=in_days(120), notional_base=N, direction=+1)], spots=[])
        assert len(zones.pin_risk(b, snapshot, "EURUSD", horizon_days=3)) == 1
        assert len(zones.pin_risk(b, snapshot, "EURUSD", include_all=True)) == 2

    def test_the_distance_is_measured_in_252_basis_sigma_days(self, snapshot):
        """W-7: distance is a trading-day quantity.  A 365 basis makes every strike
        look ~20% further away than it is."""
        df = zones.pin_risk(_one(+1, +1, K=1.1800), snapshot, "EURUSD", horizon_days=5)
        r = df.iloc[0]
        want = float(r["dist_pct"]) / zones.sigma_day_pct(float(r["vol"]))
        assert float(r["dist_sigma_days"]) == pytest.approx(want, rel=1e-12)
        assert zones.sigma_day_pct(0.08) == pytest.approx(100 * 0.08 / math.sqrt(252))

    def test_a_strike_at_spot_is_flagged_and_a_far_one_is_not(self, snapshot):
        S = snapshot.spot["EURUSD"]
        near = zones.pin_risk(_one(+1, +1, K=S), snapshot, "EURUSD",
                              horizon_days=5).iloc[0]
        far = zones.pin_risk(_one(+1, +1, K=S * 1.05), snapshot, "EURUSD",
                             horizon_days=5).iloc[0]
        assert bool(near["pin_alert"]) and not bool(far["pin_alert"])
        assert abs(float(near["dist_sigma_days"])) < abs(float(far["dist_sigma_days"]))

    def test_finish_probabilities_are_in_range_and_ordered(self, snapshot):
        df = zones.pin_risk(_one(+1, +1), snapshot, "EURUSD", horizon_days=5,
                            ks=(10, 25, 50))
        r = df.iloc[0]
        assert 0.0 <= float(r["p_above"]) <= 1.0
        assert 0.0 <= float(r["p_within_10p"]) <= float(r["p_within_25p"]) \
            <= float(r["p_within_50p"]) <= 1.0

    def test_an_empty_book_returns_an_empty_frame_with_the_key_columns(self, snapshot):
        df = zones.pin_risk(Book(options=[], spots=[]), snapshot, "EURUSD")
        assert len(df) == 0
        for c in ("pair", "strike", "jump_at_strike"):
            assert c in df.columns


# --------------------------------------------------------------------------- #
# touch probability (W-7's other half)
# --------------------------------------------------------------------------- #
class TestTouchProbability:
    def test_a_level_at_spot_is_certain_and_an_unreachable_one_is_not(self):
        assert zones.touch_probability(1.16, 1.16, 0.25, 0.08) == 1.0
        assert zones.touch_probability(1.16, 5.00, 0.02, 0.08) < 1e-6

    def test_it_is_bounded_and_monotone_in_distance(self):
        ps = [zones.touch_probability(1.16, 1.16 * (1 + x / 100), 0.25, 0.08)
              for x in (0.5, 1, 2, 4, 8)]
        assert all(0.0 <= p <= 1.0 for p in ps)
        assert all(a > b for a, b in zip(ps, ps[1:]))

    def test_it_rises_with_vol_and_with_time(self):
        base = zones.touch_probability(1.16, 1.20, 0.25, 0.08)
        assert zones.touch_probability(1.16, 1.20, 0.25, 0.16) > base
        assert zones.touch_probability(1.16, 1.20, 1.00, 0.08) > base

    def test_touch_is_about_twice_finish_for_a_driftless_process(self):
        """The reflection-principle sanity check, and the reason a *finish*
        probability must never be labelled "will I trade this zone"."""
        from fxgamma.models import gk
        S, B, T, sig = 1.16, 1.16 * math.exp(0.5 * 0.08 ** 2 * 0.25), 0.25, 0.08
        touch = zones.touch_probability(S, B, T, sig, drift=0.5 * sig * sig)
        d = math.log(S / B) / (sig * math.sqrt(T)) - 0.5 * sig * math.sqrt(T)
        finish = float(gk._norm_cdf(d))
        assert touch == pytest.approx(2 * finish, rel=0.05)

    def test_degenerate_inputs_do_not_explode(self):
        for kw in ({"T": 0.0}, {"sigma": 0.0}, {"B": -1.0}):
            args = {"S": 1.16, "B": 1.20, "T": 0.25, "sigma": 0.08} | kw
            p = zones.touch_probability(**args)
            assert 0.0 <= p <= 1.0 and math.isfinite(p)


# --------------------------------------------------------------------------- #
# gamma zones
# --------------------------------------------------------------------------- #
class TestGammaZones:
    def test_an_empty_or_optionless_book_has_no_zones(self, snapshot):
        assert zones.gamma_zones(Book(options=[], spots=[]), snapshot, "EURUSD") == []

    def test_a_single_strike_produces_one_long_gamma_zone_around_it(self, snapshot):
        S = snapshot.spot["EURUSD"]
        b = _one(+1, +1, K=S, days=30)
        zs = zones.gamma_zones(b, snapshot, "EURUSD")
        assert len(zs) == 1
        z = zs[0]
        assert isinstance(z, zones.GammaZoneDetail)
        assert z.side == "long" and z.gamma_1pct > 0
        assert z.lo < S < z.hi
        assert z.center == pytest.approx(S, rel=0.02)
        assert 0.0 <= z.share_of_total <= 1.0

    def test_a_short_book_reports_a_short_gamma_zone(self, snapshot):
        S = snapshot.spot["EURUSD"]
        zs = zones.gamma_zones(_one(+1, -1, K=S, days=30), snapshot, "EURUSD")
        assert zs and zs[0].side == "short" and zs[0].gamma_1pct < 0

    def test_two_separated_strike_clusters_report_as_two_zones(self, snapshot):
        S = snapshot.spot["EURUSD"]
        b = Book(options=[
            OptionPosition(id="lo", pair="EURUSD", cp=-1, strike=S * 0.96,
                           expiry=in_days(21), notional_base=N, direction=+1),
            OptionPosition(id="hi", pair="EURUSD", cp=+1, strike=S * 1.04,
                           expiry=in_days(21), notional_base=N, direction=+1)], spots=[])
        zs = zones.gamma_zones(b, snapshot, "EURUSD")
        assert len(zs) >= 2
        centres = sorted(z.center for z in zs)
        assert centres[0] < S < centres[-1]

    def test_the_v1_6_cr_4_detail_fields_are_populated(self, snapshot):
        """CR-4 is binding on dev: the extra fields "are the substance of how
        sensitive each gamma zone is" and must actually be there."""
        S = snapshot.spot["EURUSD"]
        z = zones.gamma_zones(_one(+1, +1, K=S * 1.01, days=30), snapshot, "EURUSD")[0]
        assert math.isfinite(z.distance_sigma_days) and math.isfinite(z.edge_sigma_days)
        assert 0.0 <= z.touch_prob <= 1.0
        assert math.isfinite(z.pnl_to_center) and math.isfinite(z.gamma_pnl_to_center)
        assert z.strikes and z.n_positions >= 1
        assert math.isfinite(z.delta_at_center)
        assert z.report_ccy == "USD" and math.isfinite(z.gamma_1pct_rep)

    def test_the_sigma_day_field_is_on_the_252_basis(self, snapshot):
        S = snapshot.spot["EURUSD"]
        z = zones.gamma_zones(_one(+1, +1, K=S * 1.02, days=30), snapshot, "EURUSD")[0]
        want = z.distance_pct / zones.sigma_day_pct(z.sigma_atm)
        assert z.distance_sigma_days == pytest.approx(want, rel=1e-9)

    def test_the_repriced_and_quadratic_pnl_to_centre_are_the_same_order(self, snapshot):
        """``pnl_to_center`` is a full reprice; ``gamma_pnl_to_center`` the 0.005 G1 S x^2
        approximation.  Wildly different means one of them is in the wrong units."""
        S = snapshot.spot["EURUSD"]
        z = zones.gamma_zones(_one(+1, +1, K=S * 1.02, days=30), snapshot, "EURUSD")[0]
        if abs(z.gamma_pnl_to_center) > 1.0:
            assert 0.2 < abs(z.pnl_to_center / z.gamma_pnl_to_center) < 5.0

    def test_zone_frame_is_a_frame_of_the_same_zones(self, snapshot):
        S = snapshot.spot["EURUSD"]
        zs = zones.gamma_zones(_one(+1, +1, K=S, days=30), snapshot, "EURUSD")
        df = zones.zone_frame(zs)
        assert len(df) == len(zs)
        assert {"pair", "lo", "hi", "center", "gamma_1pct"} <= set(df.columns)

    def test_an_unknown_sticky_mode_raises(self, snapshot):
        with pytest.raises(ValueError, match="sticky"):
            zones.gamma_zones(_one(+1, +1), snapshot, "EURUSD", sticky="wrong")


# --------------------------------------------------------------------------- #
# hedge bands -- amendment v1.6 CR-1
# --------------------------------------------------------------------------- #
class TestHedgeBands:
    def test_an_absolute_band_delta_is_used_verbatim(self, snapshot):
        b = _one(+1, +1, days=60)
        hb = zones.hedge_bands(b, snapshot, "EURUSD",
                               rule=HedgeRule(mode="band", band_delta=2.5e6))
        assert float(hb["band_base"].iloc[0]) == pytest.approx(2.5e6)

    def test_the_frozen_default_rule_gives_the_band_amendment_v1_6_ruled_on(self,
                                                                           snapshot):
        """Amendment v1.6 CR-1, now honoured here (fixed in flight -- see F-8).

        "``HedgeRule.band_pct`` ... is a FRACTION, not a percent ... The intent was
        25%.  ``types.py`` is amended to say so unambiguously; the default value is
        unchanged and is now correct rather than dangerous."  The frozen default of
        ``0.25`` must therefore give a 2.5mm band on a 10mm book, not the 25k the
        withdrawn percent reading produced -- a band ~100x too tight that rehedged a
        1Y synthetic path 190 times instead of 23.
        """
        b = _one(+1, +1, days=60)                       # 10mm gross notional
        hb = zones.hedge_bands(b, snapshot, "EURUSD", rule=HedgeRule())
        assert float(hb["band_base"].iloc[0]) == pytest.approx(0.25 * N, rel=1e-9), (
            "band_pct is a FRACTION per amendment v1.6 CR-1: the frozen default 0.25 "
            "must mean 25% of gross notional (2.5mm on a 10mm book), not 0.25%")

    def test_a_fraction_band_scales_linearly_and_is_labelled(self, snapshot):
        b = _one(+1, +1, days=60)
        for frac in (0.05, 0.15, 0.25, 0.50):
            hb = zones.hedge_bands(b, snapshot, "EURUSD",
                                   rule=HedgeRule(band_pct=frac))
            assert float(hb["band_base"].iloc[0]) == pytest.approx(frac * N, rel=1e-9)
            assert "band_source" in hb.columns and hb["band_source"].iloc[0]

    def test_the_band_scales_with_gross_notional(self, snapshot):
        small = zones.hedge_bands(_one(+1, +1, days=60), snapshot, "EURUSD",
                                  rule=HedgeRule(band_pct=0.25))
        big = Book(options=[OptionPosition(id="x", pair="EURUSD", cp=+1, strike=1.165,
                                           expiry=in_days(60), notional_base=4 * N,
                                           direction=+1)], spots=[])
        big_hb = zones.hedge_bands(big, snapshot, "EURUSD", rule=HedgeRule(band_pct=0.25))
        assert float(big_hb["band_base"].iloc[0]) == \
            pytest.approx(4 * float(small["band_base"].iloc[0]), rel=1e-9)

    def test_the_band_frame_names_its_own_source_and_reporting_ccy(self, snapshot):
        hb = zones.hedge_bands(_one(+1, +1, days=60), snapshot, "EURUSD",
                               rule=HedgeRule(band_pct=0.25))
        assert "band_source" in hb.columns and hb["band_source"].iloc[0]
        assert (hb["report_ccy"] == "USD").all()

    def test_the_per_pair_cost_table_is_not_one_global_number(self, snapshot):
        """W-13 / CR-1: 0.2bp is 10-25x too tight for the Scandies."""
        assert zones.COST_BP["EURUSD"] < zones.COST_BP.get("USDNOK", 99.0)
        assert zones.COST_BP["EURUSD"] < zones.COST_BP.get("USDSEK", 99.0)


# --------------------------------------------------------------------------- #
# the one-line hedge instruction
# --------------------------------------------------------------------------- #
class TestHedgeSuggestion:
    def test_a_flat_book_is_told_it_is_flat_and_trades_nothing(self, snapshot):
        b = Book(options=[], spots=[])
        act = hedging.hedge_suggestion(b, snapshot, "EURUSD",
                                       HedgeRule(mode="band", band_delta=1e6))
        assert act.trade_base == 0.0 and act.pair == "EURUSD"

    def test_a_long_delta_book_is_told_to_sell_base(self, snapshot):
        b = _one(+1, +1, K=1.10, days=60)              # deep ITM call -> long EUR
        act = hedging.hedge_suggestion(b, snapshot, "EURUSD",
                                       HedgeRule(mode="band", band_delta=1e5))
        assert act.current_delta > 0
        assert act.trade_base < 0, act.reason
        assert act.post_delta == pytest.approx(0.0, abs=1.0)
        assert "SELL" in act.reason and "EUR" in act.reason

    def test_a_short_delta_book_is_told_to_buy_base(self, snapshot):
        b = _one(+1, -1, K=1.10, days=60)
        act = hedging.hedge_suggestion(b, snapshot, "EURUSD",
                                       HedgeRule(mode="band", band_delta=1e5))
        assert act.trade_base > 0 and "BUY" in act.reason

    def test_inside_the_band_nothing_is_suggested(self, snapshot):
        b = _one(+1, +1, K=1.10, days=60)
        act = hedging.hedge_suggestion(b, snapshot, "EURUSD",
                                       HedgeRule(mode="band", band_delta=50e6))
        assert act.trade_base == 0.0 and "FLAT ENOUGH" in act.reason

    def test_mode_none_never_suggests_a_trade(self, snapshot):
        b = _one(+1, +1, K=1.10, days=60)
        act = hedging.hedge_suggestion(b, snapshot, "EURUSD", HedgeRule(mode="none"))
        assert act.trade_base == 0.0 and "none" in act.reason

    def test_to_edge_trades_less_than_to_target(self, snapshot):
        b = _one(+1, +1, K=1.10, days=60)
        rule = HedgeRule(mode="band", band_delta=1e6)
        to_t = hedging.hedge_suggestion(b, snapshot, "EURUSD", rule)
        to_e = hedging.hedge_suggestion(b, snapshot, "EURUSD", rule, to_edge=True)
        assert abs(to_e.trade_base) < abs(to_t.trade_base)
        assert abs(to_t.trade_base) - abs(to_e.trade_base) == pytest.approx(1e6, rel=1e-6)

    def test_a_clip_below_the_minimum_is_not_shown_as_an_instruction(self, snapshot):
        b = _one(+1, +1, K=1.10, days=60)
        act = hedging.hedge_suggestion(b, snapshot, "EURUSD",
                                       HedgeRule(mode="band", band_delta=1e5),
                                       min_clip=1e9)
        assert act.trade_base == 0.0 and "min clip" in act.reason

    def test_the_estimated_cost_uses_the_per_pair_table_when_the_rule_is_silent(self,
                                                                               snapshot):
        b = _one(+1, +1, K=145.0, days=60, pair="USDJPY")
        act = hedging.hedge_suggestion(b, snapshot, "USDJPY",
                                       HedgeRule(mode="band", band_delta=1e5, cost_bp=0.0))
        want = abs(act.trade_base) * snapshot.spot["USDJPY"] * \
            zones.COST_BP["USDJPY"] / 1e4
        assert act.est_cost == pytest.approx(want, rel=1e-9)

    def test_the_reason_is_a_printable_sentence(self, snapshot):
        b = _one(+1, +1, K=1.10, days=60)
        act = hedging.hedge_suggestion(b, snapshot, "EURUSD",
                                       HedgeRule(mode="band", band_delta=1e6))
        for token in ("delta", "band", "cost", "USD"):
            assert token in act.reason, act.reason

    def test_an_existing_spot_hedge_reduces_the_suggested_trade(self, snapshot):
        opt = _one(+1, +1, K=1.10, days=60).options[0]
        bare = hedging.hedge_suggestion(Book(options=[opt], spots=[]), snapshot, "EURUSD",
                                        HedgeRule(mode="band", band_delta=1e5))
        hedged = hedging.hedge_suggestion(
            Book(options=[opt], spots=[SpotPosition(id="h", pair="EURUSD",
                                                    notional_base=-5e6,
                                                    entry_rate=1.16)]),
            snapshot, "EURUSD", HedgeRule(mode="band", band_delta=1e5))
        assert hedged.current_delta == pytest.approx(bare.current_delta - 5e6, abs=1.0)
        assert abs(hedged.trade_base) < abs(bare.trade_base)
