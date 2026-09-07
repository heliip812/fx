"""``fxgamma/portfolio/risk.py`` -- book pricing, reporting ccy, ladders, decay.

Three things here can be wrong in a way that still *looks* right on a screen, so
they get most of the coverage:

1. **The reporting-currency layer (amendment v1.1 CG-1, ratified by v1.6 CR-2).**
   Adding a JPY theta to a USD theta produces a number two orders of magnitude out
   that a reader has no way to spot.  The amendment settled it: quote-ccy money is
   converted then summed, base-ccy *amounts* are summed natively only within one
   base ccy and come back ``nan`` across bases.
2. **The sticky modes.** Under sticky-delta the vol rides with spot, so the delta on
   the ladder carries the skew term and the effective gamma is not Black-Scholes
   gamma.  Getting the mode wrong mis-sizes every hedge on a risk-reversal book.
3. **The two annualisation bases (trader W-7, ratified in amendment v1.4 ruling 5).**
   ``sqrt(365)`` for the economics, ``sqrt(252)`` for distance and probability.  They
   differ by 17%; conflating them is a silently wrong breakeven or a silently wrong
   pin distance.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from fxgamma import conventions as cv
from fxgamma.portfolio import risk
from fxgamma.types import Book, Greeks, OptionPosition, SpotPosition

pytestmark = pytest.mark.contract

QUOTE_MONEY = ("pv", "vega", "theta", "rho_d", "rho_f", "volga")
BASE_AMOUNTS = ("delta_base", "gamma", "gamma_1pct", "vanna")


# --------------------------------------------------------------------------- #
# CG-1: fx_rate
# --------------------------------------------------------------------------- #
class TestFxRate:
    def test_the_identity_leg_is_exactly_one(self, snapshot):
        for c in ("USD", "EUR", "JPY"):
            assert risk.fx_rate(c, c, snapshot) == 1.0

    def test_a_quote_ccy_leg_is_the_reciprocal_of_the_pair(self, snapshot):
        """JPY -> USD is ``1 / USDJPY``; getting this upside down is a 21,000x error."""
        assert risk.fx_rate("JPY", "USD", snapshot) == \
            pytest.approx(1.0 / snapshot.spot["USDJPY"], rel=1e-12)
        assert risk.fx_rate("EUR", "USD", snapshot) == \
            pytest.approx(snapshot.spot["EURUSD"], rel=1e-12)

    def test_it_is_self_inverse(self, snapshot):
        for a, b in (("EUR", "JPY"), ("JPY", "EUR"), ("GBP", "CHF"), ("AUD", "USD")):
            assert risk.fx_rate(a, b, snapshot) * risk.fx_rate(b, a, snapshot) == \
                pytest.approx(1.0, rel=1e-12)

    def test_a_cross_routes_through_usd_and_agrees_with_the_triangle(self, snapshot):
        """Every cross needs two legs; one transposed leg is a plausible wrong total."""
        eurjpy = risk.fx_rate("EUR", "JPY", snapshot)
        assert eurjpy == pytest.approx(snapshot.spot["EURUSD"] * snapshot.spot["USDJPY"],
                                       rel=1e-12)

    def test_it_raises_naming_the_missing_leg_rather_than_defaulting_to_one(self, snapshot):
        """CG-1 forbids the 1.0 default: it silently prices a foreign book at par."""
        from fxgamma.types import MarketSnapshot
        thin = MarketSnapshot(asof=snapshot.asof, spot={"EURUSD": 1.16},
                              rates=dict(snapshot.rates))
        with pytest.raises(KeyError, match="JPY"):
            risk.fx_rate("JPY", "USD", thin)
        with pytest.raises(KeyError):
            risk.fx_rate("USD", "JPY", thin)


# --------------------------------------------------------------------------- #
# CG-1: price_book / book_greeks
# --------------------------------------------------------------------------- #
class TestReportingCurrency:
    def test_price_book_carries_the_cg_1_columns_for_every_row(self, mixed_book, snapshot):
        df = risk.price_book(mixed_book, snapshot, report_ccy="USD")
        assert len(df) == 3
        for col in ("ccy", "base_ccy", "report_ccy", "fx_to_report", "fx_base_to_report"):
            assert col in df.columns
        for c in QUOTE_MONEY + BASE_AMOUNTS:
            assert f"{c}_rep" in df.columns
        jpy = df[df["ccy"] == "JPY"].iloc[0]
        assert jpy["fx_to_report"] == pytest.approx(1.0 / snapshot.spot["USDJPY"], rel=1e-12)
        assert jpy["theta_rep"] == pytest.approx(jpy["theta"] * jpy["fx_to_report"], rel=1e-12)

    def test_quote_and_base_legs_are_converted_at_different_rates(self, mixed_book,
                                                                  snapshot):
        """``price_book`` multiplies quote-ccy money and base-ccy amounts by *different*
        rates.  Using one rate for both is the most likely CG-1 mistake and it is
        invisible on EURUSD (where the base is USD-quoted) but not on USDJPY."""
        df = risk.price_book(mixed_book, snapshot, report_ccy="USD")
        jpy = df[df["pair"] == "USDJPY"].iloc[0]
        assert jpy["fx_to_report"] != pytest.approx(jpy["fx_base_to_report"])
        assert jpy["fx_base_to_report"] == 1.0                    # base is USD
        assert jpy["delta_base_rep"] == pytest.approx(jpy["delta_base"], rel=1e-12)

    def test_the_naive_cross_ccy_theta_sum_is_wrong_by_two_orders_of_magnitude(
            self, mixed_book, snapshot):
        """Amendment v1.6's headline number, as a standing regression.

        The PM measured -209,974 naive against -3,424 converted on their own book
        (61x).  The multiple depends on the book; what must hold on *any* EURUSD +
        USDJPY book is that the naive sum is dominated by the unconverted JPY leg and
        is therefore wrong by roughly the USDJPY spot handle.  This test fails the
        moment someone sums a native-ccy column across pairs.
        """
        df = risk.price_book(mixed_book, snapshot, report_ccy="USD")
        opts = df[df["kind"] == "option"]
        naive = float(opts["theta"].sum())
        converted = float(opts["theta_rep"].sum())
        assert abs(naive / converted) > 20.0, (
            "the naive sum is supposed to be badly wrong; if it is not, the fixture "
            "no longer has a JPY leg and this test protects nothing")
        g = risk.book_greeks(mixed_book, snapshot, report_ccy="USD")
        assert g.theta == pytest.approx(float(df["theta_rep"].sum()), rel=1e-12)
        assert abs(g.theta) < abs(naive) / 20.0

    def test_book_greeks_returns_nan_for_base_amounts_across_mixed_bases(self, mixed_book,
                                                                        snapshot):
        """CR-2 / the T-2 precedent: "n/a" beats a confident wrong number."""
        g = risk.book_greeks(mixed_book, snapshot, report_ccy="USD")
        for c in BASE_AMOUNTS:
            assert math.isnan(getattr(g, c)), f"{c} summed EUR and USD amounts"
        for c in QUOTE_MONEY:
            assert math.isfinite(getattr(g, c)), c

    def test_base_amounts_aggregate_natively_within_one_base_ccy(self, eur_book, snapshot):
        g = risk.book_greeks(eur_book, snapshot, report_ccy="USD")
        df = risk.price_book(eur_book, snapshot)
        for c in BASE_AMOUNTS:
            g_val = getattr(g, c)
            assert math.isfinite(g_val), c
            assert g_val == pytest.approx(float(df[c].sum()), rel=1e-12)

    def test_base_as_value_converts_instead_of_returning_nan(self, mixed_book, snapshot):
        """CR-2's escape hatch: the aggregate delta card needs one number."""
        g = risk.book_greeks(mixed_book, snapshot, report_ccy="USD", base_as_value=True)
        df = risk.price_book(mixed_book, snapshot)
        for c in BASE_AMOUNTS:
            assert getattr(g, c) == pytest.approx(float(df[f"{c}_rep"].sum()), rel=1e-12)

    def test_the_intensive_greeks_are_always_nan_at_book_level(self, eur_book, snapshot):
        """T-2: ``delta_pct`` and ``dual_delta`` are per-unit; summing them is nonsense."""
        g = risk.book_greeks(eur_book, snapshot)
        assert math.isnan(g.delta_pct) and math.isnan(g.dual_delta)

    @pytest.mark.parametrize("rep", ["USD", "EUR", "JPY"])
    def test_changing_the_reporting_ccy_rescales_the_whole_book_consistently(
            self, mixed_book, snapshot, rep):
        usd = risk.book_greeks(mixed_book, snapshot, report_ccy="USD")
        other = risk.book_greeks(mixed_book, snapshot, report_ccy=rep)
        k = risk.fx_rate("USD", rep, snapshot)
        for c in QUOTE_MONEY:
            assert getattr(other, c) == pytest.approx(getattr(usd, c) * k, rel=1e-10), c

    def test_an_empty_book_returns_zero_greeks_with_the_full_column_set(self, snapshot):
        df = risk.price_book(Book(options=[], spots=[]), snapshot)
        assert len(df) == 0
        for c in ("ccy", "fx_to_report", "theta_rep", "delta_base_rep"):
            assert c in df.columns
        assert risk.book_greeks(Book(options=[], spots=[]), snapshot) == Greeks.zero()

    def test_expired_legs_leave_the_aggregate_but_stay_in_the_row_frame(self, snapshot):
        """T-3.  Their inherited delta is ``zones.pin_risk``'s job, not a silent add."""
        from tests.conftest import in_days
        b = Book(options=[
            OptionPosition(id="dead", pair="EURUSD", cp=+1, strike=1.0,
                           expiry=in_days(-5), notional_base=10e6, direction=+1),
            OptionPosition(id="live", pair="EURUSD", cp=+1, strike=1.18,
                           expiry=in_days(30), notional_base=10e6, direction=+1)], spots=[])
        df = risk.price_book(b, snapshot)
        assert bool(df.set_index("id").loc["dead", "expired"])
        g = risk.book_greeks(b, snapshot)
        g_live = risk.book_greeks(Book(options=[b.options[1]], spots=[]), snapshot)
        assert g.theta == pytest.approx(g_live.theta, rel=1e-12)
        assert risk.book_greeks(b, snapshot, include_expired=True).pv != \
            pytest.approx(g_live.pv)

    def test_a_position_in_a_pair_with_no_spot_raises_rather_than_pricing_at_zero(self,
                                                                                 snapshot):
        from fxgamma.types import MarketSnapshot
        from tests.conftest import in_days
        thin = MarketSnapshot(asof=snapshot.asof, spot={"EURUSD": 1.16},
                              rates=dict(snapshot.rates),
                              surfaces={"EURUSD": snapshot.surfaces["EURUSD"]})
        b = Book(options=[OptionPosition(id="x", pair="USDJPY", cp=+1, strike=150.0,
                                         expiry=in_days(30), notional_base=1e6,
                                         direction=+1)], spots=[])
        with pytest.raises(KeyError, match="USDJPY"):
            risk.price_book(b, thin)

    def test_a_pair_with_no_surface_refuses_to_borrow_another_pairs_vol(self, snapshot):
        """Arch section 7 again: no silent substitution, including from a sibling pair."""
        from fxgamma.types import MarketSnapshot
        from tests.conftest import in_days
        thin = MarketSnapshot(asof=snapshot.asof, spot=dict(snapshot.spot),
                              rates=dict(snapshot.rates),
                              surfaces={"EURUSD": snapshot.surfaces["EURUSD"]})
        b = Book(options=[OptionPosition(id="x", pair="USDJPY", cp=+1, strike=150.0,
                                         expiry=in_days(30), notional_base=1e6,
                                         direction=+1)], spots=[])
        with pytest.raises(KeyError, match="USDJPY"):
            risk.price_book(b, thin)


class TestMarkVols:
    """CG-2's side table: a per-position mark always beats the surface, and says so."""

    def test_a_mark_overrides_the_surface_and_is_flagged(self, eur_book, snapshot):
        base = risk.price_book(eur_book, snapshot).set_index("id")
        marked = risk.price_book(eur_book, snapshot, marks={"e1": 0.25}).set_index("id")
        assert marked.loc["e1", "vol"] == 0.25
        assert marked.loc["e1", "vol_source"] == "mark"
        assert marked.loc["e2", "vol_source"] == "surface"
        assert marked.loc["e1", "vega"] != pytest.approx(base.loc["e1", "vega"])

    def test_a_nan_mark_is_ignored_rather_than_poisoning_the_row(self, eur_book, snapshot):
        marked = risk.price_book(eur_book, snapshot,
                                 marks={"e1": float("nan")}).set_index("id")
        assert marked.loc["e1", "vol_source"] == "surface"
        assert np.isfinite(marked.loc["e1", "pv"])


# --------------------------------------------------------------------------- #
# ladder and the sticky modes
# --------------------------------------------------------------------------- #
class TestSpotLadder:
    def test_the_grid_is_the_requested_shape_and_brackets_spot(self, eur_book, snapshot):
        lad = risk.spot_ladder(eur_book, snapshot, "EURUSD", lo_pct=-5, hi_pct=5, n=101)
        assert len(lad) == 101
        assert lad["spot_pct"].iloc[0] == pytest.approx(-5.0)
        assert lad["spot_pct"].iloc[-1] == pytest.approx(5.0)
        mid = lad.iloc[50]
        assert mid["spot"] == pytest.approx(snapshot.spot["EURUSD"], rel=1e-12)
        assert mid["pnl"] == pytest.approx(0.0, abs=1e-6)

    def test_the_ladder_midpoint_reproduces_price_book(self, eur_book, snapshot):
        """One vectorised sweep and one scalar repricing must agree, or the ladder is
        showing a different book from the risk cards."""
        lad = risk.spot_ladder(eur_book, snapshot, "EURUSD", n=101)
        df = risk.price_book(eur_book, snapshot)
        mid = lad.iloc[50]
        for c in ("pv", "delta_base", "gamma", "gamma_1pct", "vega", "theta"):
            assert mid[c] == pytest.approx(float(df[c].sum()), rel=1e-9), c

    @pytest.mark.parametrize("sticky", ["strike", "delta", "none"])
    def test_all_three_sticky_modes_run_and_are_labelled(self, eur_book, snapshot, sticky):
        lad = risk.spot_ladder(eur_book, snapshot, "EURUSD", sticky=sticky, n=61)
        assert (lad["sticky"] == sticky).all()
        assert np.isfinite(lad["delta_base"]).all()

    def test_sticky_none_is_numerically_identical_to_sticky_strike(self, eur_book,
                                                                   snapshot):
        """CR-3: for a strike-parameterised surface they are the same thing; shipping
        ``none`` as a documented pinned-vol fast path is only honest if it really is."""
        a = risk.spot_ladder(eur_book, snapshot, "EURUSD", sticky="strike", n=61)
        b = risk.spot_ladder(eur_book, snapshot, "EURUSD", sticky="none", n=61)
        for c in ("pv", "delta_base", "gamma_1pct", "vega"):
            assert np.allclose(a[c], b[c], rtol=1e-12, atol=0.0), c

    def test_sticky_delta_moves_the_vol_with_spot_and_sticky_strike_does_not(self,
                                                                            snapshot):
        """The whole content of the toggle.  On a skewed book the two ladders must
        disagree; if they do not, the sticky-delta path is not re-reading the smile."""
        from tests.conftest import in_days
        rr_book = Book(options=[
            OptionPosition(id="c", pair="EURUSD", cp=+1, strike=1.2200,
                           expiry=in_days(90), notional_base=10e6, direction=+1),
            OptionPosition(id="p", pair="EURUSD", cp=-1, strike=1.1000,
                           expiry=in_days(90), notional_base=10e6, direction=-1)], spots=[])
        ss = risk.spot_ladder(rr_book, snapshot, "EURUSD", sticky="strike", n=81)
        sd = risk.spot_ladder(rr_book, snapshot, "EURUSD", sticky="delta", n=81)
        assert not np.allclose(ss["delta_base"], sd["delta_base"], rtol=1e-6)
        assert ss["pv"].iloc[40] == pytest.approx(sd["pv"].iloc[40], rel=1e-9)  # same at S0

    def test_the_effective_gamma_column_equals_bs_gamma_under_sticky_strike(self,
                                                                            eur_book,
                                                                            snapshot):
        """``gamma_fd`` is the slope of the ladder's own delta.  Under sticky-strike it
        must reproduce the analytic gamma, which is what makes the sticky-delta gap
        (``skew_gamma_1pct``) meaningful rather than a numerical artefact."""
        lad = risk.spot_ladder(eur_book, snapshot, "EURUSD", sticky="strike",
                               lo_pct=-3, hi_pct=3, n=201)
        core = lad.iloc[20:-20]
        assert np.allclose(core["gamma_fd"], core["gamma"], rtol=2e-3)
        assert np.allclose(core["skew_gamma_1pct"], 0.0,
                           atol=1e-3 * float(np.max(np.abs(core["gamma_1pct"]))))

    def test_skew_gamma_is_non_trivial_under_sticky_delta_on_a_skewed_book(self, snapshot):
        """The v1.6 "recorded finding": a trader hedging off BS gamma under a
        sticky-delta assumption mis-sizes.  Pin that the engine still reports the gap."""
        from tests.conftest import in_days
        rr_book = Book(options=[
            OptionPosition(id="c", pair="EURUSD", cp=+1, strike=1.2200,
                           expiry=in_days(90), notional_base=10e6, direction=+1),
            OptionPosition(id="p", pair="EURUSD", cp=-1, strike=1.1000,
                           expiry=in_days(90), notional_base=10e6, direction=-1)], spots=[])
        sd = risk.spot_ladder(rr_book, snapshot, "EURUSD", sticky="delta",
                              lo_pct=-3, hi_pct=3, n=201).iloc[20:-20]
        gap = float(np.max(np.abs(sd["skew_gamma_1pct"])))
        scale = float(np.max(np.abs(sd["gamma_1pct"])))
        assert gap > 1e-3 * scale, "sticky-delta reported zero skew gamma"

    def test_an_unknown_sticky_mode_raises(self, eur_book, snapshot):
        with pytest.raises(ValueError, match="sticky"):
            risk.spot_ladder(eur_book, snapshot, "EURUSD", sticky="stickey")

    def test_only_the_named_pair_is_shocked(self, mixed_book, snapshot):
        """A EURUSD ladder must not move the USDJPY leg -- that would double-count the
        book's risk on every screen that shows both."""
        lad = risk.spot_ladder(mixed_book, snapshot, "EURUSD", n=21)
        eur_only = risk.price_book(mixed_book.filter("EURUSD"), snapshot)
        assert lad["pv"].iloc[10] == pytest.approx(float(eur_only["pv"].sum()), rel=1e-9)

    def test_the_ladder_is_convex_where_the_book_is_long_gamma(self, eur_book, snapshot):
        lad = risk.spot_ladder(eur_book, snapshot, "EURUSD", n=121)
        assert (lad["gamma_1pct"] > 0).all()
        assert np.all(np.diff(lad["delta_base"].to_numpy(float)) > 0)


class TestScenarioGrid:
    def test_the_grid_is_the_cartesian_product_and_the_origin_is_flat(self, eur_book,
                                                                     snapshot):
        g = risk.scenario_grid(eur_book, snapshot, "EURUSD",
                               spot_shocks=[-2, -1, 0, 1, 2], vol_shocks=[-1, 0, 1])
        assert len(g) == 15
        origin = g[(g["spot_shock_pct"] == 0) & (g["vol_shock_pts"] == 0)]
        assert len(origin) == 1
        assert float(origin["pnl"].iloc[0]) == pytest.approx(0.0, abs=1e-6)

    def test_a_long_vega_book_gains_on_a_vol_up_shock(self, eur_book, snapshot):
        g = risk.scenario_grid(eur_book, snapshot, "EURUSD", spot_shocks=[0],
                               vol_shocks=[-2.0, 0.0, 2.0]).set_index("vol_shock_pts")
        assert g.loc[2.0, "pnl"] > 0 > g.loc[-2.0, "pnl"]
        # vol shocks are in vol POINTS: +2pts is +0.02 of sigma, not +200%.  To second
        # order the cell is dv*vega + 0.5*dv^2*volga, both per vol point (MISS-11).
        base = g.loc[0.0]
        want = 2.0 * float(base["vega"]) + 0.5 * 4.0 * float(base["volga"])
        assert g.loc[2.0, "pnl"] == pytest.approx(want, rel=0.02)


class TestTimeDecay:
    def test_calendar_is_the_default_and_weights_are_all_one(self, eur_book, snapshot):
        """CG-4: "Calendar time stays the default"."""
        d = risk.time_decay(eur_book, snapshot, days=range(0, 8))
        assert (d["calendar"] == "calendar").all()
        assert np.allclose(d["weight"], 1.0)
        assert list(d["day"]) == list(range(8))

    def test_a_long_option_book_loses_pv_as_the_clock_runs(self, eur_book, snapshot):
        d = risk.time_decay(eur_book, snapshot, days=[0, 5, 10, 20])
        assert np.all(np.diff(d["pv_rep"].to_numpy(float)) < 0)
        assert (d["theta_rep"] < 0).all()

    def test_predicted_and_repriced_decay_agree_over_a_short_horizon(self, eur_book,
                                                                     snapshot):
        """``cum_theta_rep`` is the trapezoidal prediction, ``dpv_rep`` the repricing.
        A large gap means theta and the repricer disagree about what a day is."""
        d = risk.time_decay(eur_book, snapshot, days=list(range(0, 8)))
        pred = d["cum_theta_rep"].to_numpy(float)
        act = d["dpv_rep"].to_numpy(float)
        # the gap is the second-order charm term, ~3% of one day on a 1M option; a
        # sign error or a 365/252 slip would show up here as tens of percent
        assert np.allclose(pred[1:], act[1:], rtol=0.05), np.c_[pred, act]

    def test_business_weighting_is_opt_in_preserves_total_time_and_is_labelled(
            self, eur_book, snapshot):
        """CG-4: the *shape* may change (the Friday question), the endpoint must not."""
        cal = risk.time_decay(eur_book, snapshot, days=list(range(0, 15)))
        biz = risk.time_decay(eur_book, snapshot, days=list(range(0, 15)),
                              calendar="business")
        assert (biz["calendar"] == "business").all()
        assert biz["eff_days"].iloc[-1] == pytest.approx(cal["eff_days"].iloc[-1], rel=1e-9)
        assert not np.allclose(biz["eff_days"], cal["eff_days"])

    def test_an_unknown_calendar_raises(self, eur_book, snapshot):
        with pytest.raises(ValueError, match="calendar"):
            risk.time_decay(eur_book, snapshot, days=[0, 1], calendar="lunar")

    def test_mismatched_weights_raise_rather_than_broadcasting(self, eur_book, snapshot):
        with pytest.raises(ValueError, match="weights"):
            risk.time_decay(eur_book, snapshot, days=[0, 1, 2], weights=[1.0, 1.0])

    def test_the_delta_hedged_path_is_flat_when_realized_equals_implied(self, eur_book,
                                                                        snapshot):
        """Amendment v1.4 ruling 4's identity, on the decay path this time."""
        df = risk.price_book(eur_book, snapshot)
        sig = float(np.average(df["vol"], weights=np.abs(df["vega"]) + 1e-12))
        d = risk.time_decay(eur_book, snapshot, days=list(range(0, 11)), realized_vol=sig)
        # not identically zero: the vega-weighted implied drifts as the book decays.
        # It must stay negligible against the theta actually charged over the horizon.
        assert abs(float(d["cum_dh_pnl_rep"].iloc[-1])) < \
            0.02 * abs(float(d["cum_theta_rep"].iloc[-1]))
        hot = risk.time_decay(eur_book, snapshot, days=list(range(0, 11)),
                              realized_vol=sig * 1.5)
        assert hot["cum_dh_pnl_rep"].iloc[-1] > 0


# --------------------------------------------------------------------------- #
# the two desk identities (W-5) implemented once
# --------------------------------------------------------------------------- #
class TestDeskIdentities:
    def test_gamma_pnl_is_quadratic_and_sign_symmetric(self):
        f = risk.gamma_pnl_pct
        assert f(3.914e6, 1.084, 1.0) == pytest.approx(0.005 * 3.914e6 * 1.084)
        assert f(3.914e6, 1.084, -2.0) == pytest.approx(f(3.914e6, 1.084, 2.0))
        assert f(3.914e6, 1.084, 2.0) == pytest.approx(4 * f(3.914e6, 1.084, 1.0))

    def test_dhedge_pnl_is_flat_at_realized_equals_implied_and_signs_correctly(self):
        f = risk.dhedge_pnl
        assert f(3.914e6, 1.084, 0.09, 0.09, 1 / 365) == pytest.approx(0.0, abs=1e-9)
        assert f(3.914e6, 1.084, 0.09, 0.0705, 1 / 365) > 0
        assert f(3.914e6, 1.084, 0.05, 0.0705, 1 / 365) < 0

    def test_the_req_046_worked_example_reproduces(self):
        """Amendment v1.4 ruling 4: one day of 9% realised against 7.05% implied on the
        reference straddle is ~USD +1,836 (the PM's figure used the trader's rounded
        3.95mm gamma; on the priced 3.914mm it is 1,819)."""
        got = risk.dhedge_pnl(3.914e6, 1.084, 0.09, 0.0705, 1 / 365)
        assert got == pytest.approx(1836.0, rel=0.02), got
        # the factor is 50, not 0.5 -- REQ-046's spelling was out by 100x (W-5)
        assert got == pytest.approx(
            50.0 * 3.914e6 * 1.084 * (0.09 ** 2 - 0.0705 ** 2) / 365, rel=1e-12)


# --------------------------------------------------------------------------- #
# W-7 -- sqrt(365) for economics, sqrt(252) for distance.  Never conflated.
# --------------------------------------------------------------------------- #
class TestTheTwoAnnualisationBases:
    def test_the_two_constants_are_what_they_say_they_are(self):
        from fxgamma.portfolio import zones
        assert zones.CALENDAR_DAYS == 365.0
        assert zones.TRADING_DAYS == 252.0

    def test_distance_uses_252_and_the_breakeven_uses_365(self):
        """They differ by 20% at 8 vol: 0.504%/day vs 0.419%/day.  A panel that mixes
        them tells the trader a 0.45% move is both inside and outside a sigma-day."""
        from fxgamma.portfolio import zones
        from fxgamma.signals import richness
        sigma = 0.08
        assert zones.sigma_day_pct(sigma) == pytest.approx(100 * sigma / math.sqrt(252))
        assert zones.sigma_day_pct(sigma) != pytest.approx(100 * sigma / math.sqrt(365))
        # the breakeven identity is the 365 one, built from a real straddle
        assert richness.assert_breakeven_identity(sigma=sigma) < 1e-9

    def test_realized_vol_annualises_on_252_not_365(self, provider):
        """RV is compared to a *quoted* IV, and the market quotes on trading days."""
        from fxgamma.signals import realized
        assert realized.ANNUAL == 252.0
        import datetime as _dt
        h = provider.spot_history("EURUSD", _dt.date(2025, 1, 1), _dt.date(2026, 1, 1))
        rv = realized.close_to_close(h)
        rv_raw = realized.close_to_close(h, annual=1.0)
        assert rv == pytest.approx(rv_raw * math.sqrt(252.0), rel=1e-12)

    def test_the_two_bases_give_visibly_different_pip_numbers_on_one_book(self, eur_book,
                                                                          snapshot):
        """Amendment v1.4 ruling 5 pinned 39.9 vs 48.1 pips on the reference straddle.
        The ratio is fixed by the bases and must hold on any book."""
        from fxgamma.portfolio import zones
        from fxgamma.signals import richness
        be = richness.daily_breakeven(eur_book, snapshot, "EURUSD")
        sig = float(be["sigma_used"])
        assert be["be_pct"] == pytest.approx(100 * sig / math.sqrt(365), rel=5e-3)
        assert zones.sigma_day_pct(sig) / be["be_pct"] == \
            pytest.approx(math.sqrt(365 / 252), rel=5e-3)
        assert "365" in str(be["be_basis"])


# --------------------------------------------------------------------------- #
# shift_market -- the primitive the ladder, grid and decay all ride on
# --------------------------------------------------------------------------- #
class TestShiftMarket:
    def test_an_empty_shift_is_the_identity(self, snapshot, eur_book):
        same = risk.shift_market(snapshot)
        a = risk.price_book(eur_book, snapshot)
        b = risk.price_book(eur_book, same)
        assert np.allclose(a["pv"], b["pv"], rtol=1e-12)

    def test_a_spot_shift_touches_only_the_named_pair(self, snapshot):
        sh = risk.shift_market(snapshot, spot_mult={"EURUSD": 1.01})
        assert sh.spot["EURUSD"] == pytest.approx(snapshot.spot["EURUSD"] * 1.01)
        assert sh.spot["USDJPY"] == pytest.approx(snapshot.spot["USDJPY"])

    def test_the_original_snapshot_is_never_mutated(self, snapshot):
        before = dict(snapshot.spot)
        risk.shift_market(snapshot, spot_mult=1.05, vol_add=0.02, days=10)
        assert snapshot.spot == before

    def test_a_days_shift_moves_the_clock_forward(self, snapshot):
        sh = risk.shift_market(snapshot, days=7)
        assert (sh.asof - snapshot.asof).days == 7
