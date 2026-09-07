"""``fxgamma/signals/`` -- realized vol, cones, richness, listed gamma.

These are the numbers a trader uses to decide *whether* to own gamma, so the
failure mode is not a crash, it is a signal that points the wrong way.  Three
things get the weight:

* **The annualisation basis.** RV annualises on 252 (spot moves on trading days),
  the breakeven on 365 (theta is paid on calendar days).  Trader W-7, ratified by
  amendment v1.4 ruling 5.  A z-score built on one and compared to the other is a
  20% bias that never announces itself.
* **The sample size behind a z-score (amendment v1.3 C-6).** ETF chains give today's
  smile and no history.  A z-score on eleven days is not a z-score, and the honest
  behaviour is to say so rather than to print a confident 2-sigma.
* **The breakeven identity.** ``BE = sigma/sqrt(365)`` closes gamma, theta and the
  breakeven helper against each other -- see ``test_golden_reference_trade.py``.
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from fxgamma.signals import cones, gex, realized, richness

pytestmark = pytest.mark.contract


@pytest.fixture(scope="module")
def history(provider):
    """~18 months of synthetic EURUSD OHLC -- deterministic, offline.

    Long enough for a 63-day cone to have ~300 overlapping observations and short
    enough to keep the suite in seconds; the estimators are all O(n) and the cones
    O(n x horizon).
    """
    return provider.spot_history("EURUSD", dt.date(2025, 3, 1), dt.date(2026, 9, 1))


@pytest.fixture(scope="module")
def cone(history):
    """One cone for the whole module -- ``vol_cone`` re-derives a rolling series per
    horizon, which is the slowest thing in this file."""
    return cones.vol_cone(history, horizons=(5, 21, 63))


# --------------------------------------------------------------------------- #
# realized vol estimators
# --------------------------------------------------------------------------- #
ESTIMATORS = ("close_to_close", "parkinson", "garman_klass", "rogers_satchell",
              "yang_zhang")


class TestRealizedVol:
    def test_the_annualisation_factor_is_252_not_365(self):
        """W-7's first half.  ``sqrt(365/252) = 1.204``: an RV annualised on calendar
        days is 20% too high against a market-quoted implied."""
        assert realized.ANNUAL == 252.0

    @pytest.mark.parametrize("method", ESTIMATORS)
    def test_every_estimator_returns_a_plausible_positive_decimal_vol(self, history,
                                                                     method):
        v = realized.realized_vol(history, method)
        assert np.isfinite(v) and 0.001 < v < 1.0, f"{method} -> {v}"

    @pytest.mark.parametrize("method", ESTIMATORS)
    def test_annual_is_a_pure_scale_factor(self, history, method):
        a = realized.realized_vol(history, method)
        b = realized.realized_vol(history, method, annual=1.0)
        assert a == pytest.approx(b * math.sqrt(252.0), rel=1e-12)

    @pytest.mark.parametrize("method", ESTIMATORS)
    def test_scaling_every_price_by_a_constant_does_not_change_the_vol(self, history,
                                                                      method):
        """Vol is a property of returns.  An estimator that is not scale-invariant is
        reading levels, and would report a different number for USDJPY than for the
        same path quoted in a different handle."""
        scaled = history.copy()
        for c in ("open", "high", "low", "close"):
            if c in scaled.columns:
                scaled[c] = scaled[c] * 3.7
        assert realized.realized_vol(scaled, method) == \
            pytest.approx(realized.realized_vol(history, method), rel=1e-9)

    def test_the_estimators_agree_to_within_a_factor_on_the_same_path(self, history):
        """They differ by design (range estimators are more efficient), but a 2x gap
        means one of them has the wrong constant -- Parkinson's ``1/(4 ln 2)`` and
        Garman-Klass's ``0.5 ln(h/l)^2 - (2 ln 2 - 1) ln(c/o)^2`` are easy to fumble."""
        vals = {m: realized.realized_vol(history, m) for m in ESTIMATORS}
        lo, hi = min(vals.values()), max(vals.values())
        assert hi / lo < 2.0, vals

    def test_a_flat_path_has_zero_close_to_close_vol(self):
        n = 60
        df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0},
                          index=pd.date_range("2026-01-01", periods=n, freq="D"))
        assert realized.close_to_close(df) == pytest.approx(0.0, abs=1e-12)

    def test_a_known_constant_daily_move_recovers_its_own_vol(self):
        """A deterministic +/-x% zig-zag has a closed-form close-to-close vol."""
        n, x = 400, 0.005
        closes = [1.0]
        for i in range(n):
            closes.append(closes[-1] * (1 + x if i % 2 == 0 else 1 / (1 + x)))
        df = pd.DataFrame({"open": closes, "high": closes, "low": closes,
                           "close": closes},
                          index=pd.date_range("2024-01-01", periods=len(closes),
                                              freq="D"))
        want = math.log(1 + x) * math.sqrt(252.0)
        assert realized.close_to_close(df) == pytest.approx(want, rel=1e-3)

    def test_rolling_vol_starts_after_its_warm_up_rather_than_back_filling(self,
                                                                            history):
        """A rolling window that emits a value before it has a full window is reading
        either future data or too few returns; either way the cone built on it lies."""
        w = 21
        r = realized.rolling_vol(history, window=w)
        assert len(r) == len(history) - w
        assert r.index[0] == history.index[w]
        assert np.isfinite(r.to_numpy(float)).all()
        # each point is computable from its own trailing window and nothing later
        j = len(history) - 1
        assert r.iloc[-1] == pytest.approx(
            realized.realized_vol(history.iloc[j - w: j + 1], "close_to_close"),
            rel=1e-12)

    def test_all_estimators_states_its_basis_and_its_assumption_per_row(self, history):
        """W-7 again: every estimator row must print the annualisation it used, so a
        252 number is never read next to a 365 one without a label."""
        df = realized.all_estimators(history, window=21)
        assert set(df["estimator"]) == set(ESTIMATORS)
        assert (df["annualisation"] == "sqrt(252)").all()
        assert df["assumption"].map(bool).all()

    def test_an_unknown_method_raises_rather_than_falling_back(self, history):
        with pytest.raises((ValueError, KeyError)):
            realized.realized_vol(history, "made_up_estimator")


# --------------------------------------------------------------------------- #
# cones
# --------------------------------------------------------------------------- #
class TestVolCones:
    def test_the_cone_has_a_row_per_horizon_with_ordered_percentiles(self, cone):
        c = cone
        assert len(c) == 3
        for _, r in c.iterrows():
            vals = [r[k] for k in ("p5", "p25", "p50", "p75", "p95") if k in c.columns]
            assert vals == sorted(vals), r

    def test_the_cone_reports_its_effective_sample_size(self, cone):
        """C-6 / overlapping samples: 500 daily observations of a 63-day realized vol
        are ~8 independent ones.  A cone that prints n=437 invites a trader to treat
        a 95th percentile as a 1-in-20 event when it is closer to 1-in-8."""
        assert any("n" in col for col in cone.columns), cone.columns.tolist()
        eff = cones.effective_n(500, 63)
        assert eff < 500 / 5, eff
        assert cones.effective_n(500, 1) == pytest.approx(500.0, rel=1e-9)

    def test_a_percentile_lookup_is_bounded_and_monotone(self, history):
        lo = cones.cone_percentile(history, 21, 0.001)
        hi = cones.cone_percentile(history, 21, 5.0)
        mid = cones.cone_percentile(history, 21, float(realized.realized_vol(history)))
        assert 0.0 <= lo <= mid <= hi <= 100.0

    def test_longer_horizons_are_less_dispersed(self, cone):
        """Vol of vol falls with the averaging window; if it does not, the cone is
        being built on non-overlapping garbage."""
        c = cone.set_index("horizon")
        if {"p5", "p95"} <= set(c.columns):
            assert (c.loc[63, "p95"] - c.loc[63, "p5"]) < (c.loc[5, "p95"] - c.loc[5, "p5"])


# --------------------------------------------------------------------------- #
# richness
# --------------------------------------------------------------------------- #
class TestRichness:
    def test_the_breakeven_identity_holds_at_zero_and_at_realistic_rates(self):
        """The library ships its own acceptance test (REQ-039); run it here so a
        regression in ``gk`` shows up as a QA failure, not only as a silent drift."""
        assert richness.assert_breakeven_identity() < 1e-9

    @pytest.mark.parametrize("sigma", [0.04, 0.0705, 0.12, 0.25])
    def test_the_identity_holds_across_vol_levels(self, sigma):
        assert richness.assert_breakeven_identity(sigma=sigma) < 1e-9

    def test_a_short_gamma_book_has_no_breakeven_and_says_nan(self):
        """"There is no move that pays a theta you are collecting" -- printing a
        number there would be a lie."""
        assert math.isnan(richness.breakeven_pct(-500.0, -3.9e6, 1.084))
        assert math.isnan(richness.breakeven_pct(-500.0, 0.0, 1.084))

    def test_the_breakeven_is_on_the_365_basis_and_labelled(self, eur_book, snapshot):
        be = richness.daily_breakeven(eur_book, snapshot, "EURUSD")
        assert "365" in str(be["be_basis"])
        assert be["be_pct"] == pytest.approx(
            100 * float(be["sigma_used"]) / math.sqrt(365), rel=5e-3)

    def test_days_to_next_mark_scales_the_theta_bill_not_the_breakeven(self, eur_book,
                                                                      snapshot):
        """The Friday question (Q-6): three days of theta, one day of breakeven."""
        one = richness.daily_breakeven(eur_book, snapshot, "EURUSD", days=1)
        three = richness.daily_breakeven(eur_book, snapshot, "EURUSD", days=3)
        assert three["theta_to_next_mark"] == pytest.approx(
            3 * one["theta_to_next_mark"], rel=1e-12)
        assert three["be_pct"] == pytest.approx(one["be_pct"], rel=1e-12)
        assert "3 day" in str(three["words"])

    def test_the_breakeven_card_reports_pips_consistent_with_percent(self, eur_book,
                                                                     snapshot):
        be = richness.daily_breakeven(eur_book, snapshot, "EURUSD")
        S = float(be["spot"])
        assert be["be_pips"] == pytest.approx(be["be_pct"] / 100 * S / 1e-4, rel=1e-9)

    def test_coverage_ratio_is_one_at_breakeven_and_scales_as_the_vol_ratio_squared(self):
        """W-6: dimensionless and comparable across pairs, which Gamma$/theta is not."""
        g1, S, sig = 3.914e6, 1.084, 0.0705
        from fxgamma.signals.richness import gamma_theta
        gamma = g1 / (0.01 * S)
        theta = gamma_theta(gamma, S, sig)
        assert richness.coverage_ratio(g1, S, theta, sig) == pytest.approx(1.0, rel=1e-9)
        assert richness.coverage_ratio(g1, S, theta, 2 * sig) == pytest.approx(4.0,
                                                                               rel=1e-9)

    def test_gamma_carry_expectancy_agrees_with_the_risk_identity(self):
        from fxgamma.portfolio import risk
        a = richness.gamma_carry_expectancy(3.914e6, 1.084, 0.09, 0.0705, days=1)
        b = risk.dhedge_pnl(3.914e6, 1.084, 0.09, 0.0705, 1 / 365)
        assert a == pytest.approx(b, rel=1e-12), "two spellings of the same identity"

    def test_rv_iv_spread_is_signed_the_way_a_trader_reads_it(self, history, snapshot):
        out = richness.rv_iv_spread(history, snapshot, "EURUSD", calendar_days=30)
        assert np.isfinite(out["rv"]) and np.isfinite(out["iv"])
        # "spread > 0 means implied is above realized: gamma looks expensive"
        assert out["spread"] == pytest.approx(out["iv"] - out["rv"], rel=1e-12)
        assert out["spread_pts"] == pytest.approx(out["spread"] * 100.0, rel=1e-12)
        # W-8: a 30-calendar-day option is matched to ~21 business rows, not 30
        assert out["business_rows"] == 21
        assert out["rv_annualisation"] == "sqrt(252)"


class TestZScoresAndSampleSize:
    def test_a_zscore_on_a_known_series_is_exact(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        z = richness.zscore(s, window=5)
        assert z["z_naive"] == pytest.approx((5.0 - 3.0) / s.std(ddof=1), rel=1e-9)
        assert z["n"] == 5 and z["percentile"] == pytest.approx(100.0)

    def test_a_constant_series_gives_nan_not_infinity(self):
        z = richness.zscore(pd.Series([2.0] * 30), window=30)
        assert not np.isfinite(z["z"]) and not np.isfinite(z["z_naive"])

    def test_an_overlapping_series_is_deflated_by_sqrt_of_the_window(self):
        """W-8: a z-score built on a 21-day rolling vol has ~1/21 of the independent
        observations it appears to.  Without the correction every z is inflated by
        about sqrt(21) = 4.6 and a 2-sigma "signal" is noise."""
        rng = np.random.default_rng(11)
        s = pd.Series(0.08 + 0.004 * rng.standard_normal(300))
        naive = richness.zscore(s, window=252, overlap=1)
        corrected = richness.zscore(s, window=252, overlap=21)
        assert corrected["z"] == pytest.approx(naive["z"] / math.sqrt(21), rel=1e-9)
        assert corrected["n_eff"] == pytest.approx(naive["n"] / 21.0, rel=1e-9)
        assert abs(corrected["z"]) < abs(naive["z"]) or naive["z"] == 0.0

    def test_a_zscore_with_almost_no_history_reports_its_sample_size(self):
        """Amendment v1.3 C-6: "A z-score computed on 11 days of self-collected
        history must say so."  The library's half of that contract is to return ``n``
        and ``n_eff`` alongside every z; the UI must print them (binding on dev)."""
        z = richness.zscore(pd.Series(np.linspace(0.07, 0.09, 11)), window=252)
        assert z["n"] == 11 and z["n_eff"] == pytest.approx(11.0)
        assert set(z) >= {"z", "z_naive", "n", "n_eff", "percentile"}

    def test_fewer_than_three_observations_refuses_outright(self):
        z = richness.zscore(pd.Series([0.08, 0.081]), window=252)
        assert not np.isfinite(z["z"]) and z["n"] == 2

    def test_quote_zscores_reports_the_sample_size_it_used(self):
        idx = pd.date_range("2026-01-01", periods=90, freq="D")
        rng = np.random.default_rng(7)
        hist = pd.DataFrame({"pair": "EURUSD",
                             "atm": 0.08 + 0.002 * rng.standard_normal(90),
                             "rr25": -0.002 + 0.0005 * rng.standard_normal(90),
                             "bf25": 0.002 + 0.0002 * rng.standard_normal(90)}, index=idx)
        out = richness.quote_zscores(hist, "EURUSD", window=252)
        for c in ("atm", "rr25", "bf25"):
            assert out[f"{c}_n"] == 90.0
            assert np.isfinite(out[f"{c}_z"])
            assert 0.0 <= out[f"{c}_pctile"] <= 100.0

    def test_a_pair_with_no_quote_history_reports_zero_observations(self):
        idx = pd.date_range("2026-01-01", periods=30, freq="D")
        hist = pd.DataFrame({"pair": "EURUSD", "atm": 0.08}, index=idx)
        out = richness.quote_zscores(hist, "EURUSD")
        assert out["rr25_n"] == 0.0 and math.isnan(out["rr25_z"])


class TestMarketGamma:
    @pytest.fixture()
    def oi(self, provider):
        return provider.open_interest("EURUSD")

    def test_the_profile_has_a_row_per_strike_and_no_nans(self, oi, snapshot):
        p = gex.market_gamma_profile(oi, snapshot, "EURUSD")
        assert len(p) > 0
        assert np.isfinite(p["gamma_1pct"].to_numpy(float)).all()
        assert np.isfinite(p["gamma_1pct_abs"].to_numpy(float)).all()

    def test_the_default_assumption_is_unsigned_and_is_carried_in_the_output(self, oi,
                                                                            snapshot):
        """W-11 / REQ-023: we do not know who is long.  Labelling an unsigned OI
        profile "dealer gamma" is the most common piece of listed-flow nonsense, and
        the assumption must travel with the numbers so a caption cannot lose it."""
        p = gex.market_gamma_profile(oi, snapshot, "EURUSD")
        assert (p["sign_assumption"] == "unsigned").all()
        assert (p["gamma_1pct"].to_numpy(float) >= 0).all(), \
            "an unsigned profile must be |gamma|, not a net position"

    def test_an_unsigned_profile_has_no_flip_level(self, oi, snapshot):
        """"An unsigned curve has no flip, and manufacturing one is exactly the
        fiction this module refuses to sell"."""
        p = gex.market_gamma_profile(oi, snapshot, "EURUSD")
        curve = gex.gamma_profile_curve(p, snapshot, "EURUSD")
        assert math.isnan(gex.gamma_flip_level(curve))

    @pytest.mark.parametrize("assumption", ["short_all", "dealer_short_calls_long_puts",
                                            "dealer_long_calls_short_puts"])
    def test_each_sign_assumption_changes_the_profile_and_says_which_it_used(
            self, oi, snapshot, assumption):
        base = gex.market_gamma_profile(oi, snapshot, "EURUSD")
        p = gex.market_gamma_profile(oi, snapshot, "EURUSD", sign_assumption=assumption)
        assert (p["sign_assumption"] == assumption).all()
        assert len(p) == len(base)
        assert not np.allclose(p["gamma_1pct"], base["gamma_1pct"])
        assert np.allclose(p["gamma_1pct_abs"], base["gamma_1pct_abs"])

    def test_long_all_coincides_with_unsigned_because_long_gamma_is_positive(self, oi,
                                                                            snapshot):
        """Not a bug: a long option has positive gamma whether it is a call or a put,
        so |gamma| and "everyone is long" are the same curve.  Pinned so nobody
        "fixes" the unsigned branch into something asymmetric."""
        a = gex.market_gamma_profile(oi, snapshot, "EURUSD")
        b = gex.market_gamma_profile(oi, snapshot, "EURUSD", sign_assumption="long_all")
        assert np.allclose(a["gamma_1pct"], b["gamma_1pct"], rtol=1e-12)
        assert (a["sign_assumption"] != b["sign_assumption"]).all()

    def test_long_all_and_short_all_are_exact_mirrors(self, oi, snapshot):
        """They bracket the possible profiles; if they are not negatives of each
        other the bracket is not a bracket."""
        a = gex.market_gamma_profile(oi, snapshot, "EURUSD", sign_assumption="long_all")
        b = gex.market_gamma_profile(oi, snapshot, "EURUSD", sign_assumption="short_all")
        assert np.allclose(a["gamma_1pct"], -b["gamma_1pct"], rtol=1e-12)

    def test_a_signed_curve_flips_where_the_flip_level_says_it_does(self, oi, snapshot):
        p = gex.market_gamma_profile(oi, snapshot, "EURUSD",
                                     sign_assumption="dealer_short_calls_long_puts")
        curve = gex.gamma_profile_curve(p, snapshot, "EURUSD")
        flip = gex.gamma_flip_level(curve)
        if np.isfinite(flip):
            lo = curve[curve["spot"] < flip]["gamma_1pct"]
            hi = curve[curve["spot"] > flip]["gamma_1pct"]
            if len(lo) and len(hi):
                assert np.sign(lo.iloc[-1]) != np.sign(hi.iloc[0])

    def test_strike_magnets_are_ranked_unsigned_and_carry_their_distance(self, oi,
                                                                        snapshot):
        p = gex.market_gamma_profile(oi, snapshot, "EURUSD")
        m = gex.strike_magnets(p, snapshot, "EURUSD", top=5)
        assert 0 < len(m) <= 5
        v = m["gamma_1pct_abs"].to_numpy(float)
        assert (np.diff(v) <= 1e-9).all(), "magnets must be sorted by size"
        assert set(m["strike"]) <= set(p["strike"])
        S = snapshot.spot["EURUSD"]
        assert np.allclose(m["dist_pips"], (m["strike"] - S) / 1e-4, rtol=1e-9)
        assert "not net dealer position" in str(m["note"].iloc[0])

    def test_the_expiry_ladder_never_invents_open_interest(self, oi, snapshot):
        """It reports the nearest ``n_expiries``, so its total is a subset of the
        chain's -- never more than it."""
        lad = gex.oi_expiry_ladder(oi, snapshot, "EURUSD", n_expiries=8)
        assert 0 < len(lad) <= 8
        assert float(lad["oi"].sum()) <= float(oi["oi"].sum()) + 1e-6
        full = gex.oi_expiry_ladder(oi, snapshot, "EURUSD", n_expiries=1000)
        assert float(full["oi"].sum()) == pytest.approx(float(oi["oi"].sum()), rel=1e-9)

    def test_the_ladder_names_the_cut_it_used(self, oi, snapshot):
        """W-11: the CME product calendar is the source of truth; this ladder uses the
        OTC cut and must say so rather than implying CME expiry instants."""
        lad = gex.oi_expiry_ladder(oi, snapshot, "EURUSD")
        assert "otc_cut_used" in lad.columns

    def test_an_empty_chain_returns_an_empty_profile_not_an_exception(self, snapshot):
        empty = pd.DataFrame(columns=["strike", "expiry", "cp", "oi"])
        p = gex.market_gamma_profile(empty, snapshot, "EURUSD")
        assert len(p) == 0
        assert len(gex.gamma_profile_curve(p, snapshot, "EURUSD")) == 0
        assert len(gex.strike_magnets(p, snapshot, "EURUSD")) == 0
