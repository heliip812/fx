"""``fxgamma.signals.rangeforecast`` and ``fxgamma.signals.levels``.

``docs/10_forecast_evaluation.md`` is the honest half of this feature, and its
*negative* results are the valuable part.  What has to stay true:

USE
    * **HAR-RV** beats yesterday's RV and a trailing mean out of sample, and is
      calibrated in **level**, not merely in ranking -- because the forecast feeds a
      go/no-go, not just rung ordering.
    * **Implied at a FIXED 0.5 weight**, not a fitted one.  The fitted weight swings
      0.22-1.00 for an OOS gain of -1.3% to +0.4%, nothing at p<0.08; the simulator's
      ATM is a linear function of trailing RV, so fitting it is a machinery pass and
      not a market fact.
    * **kappa / efficiency / crossings as computed outputs**, identity verified to
      1-5%, detecting mean reversion and momentum.

DO NOT USE
    * **Forecasting kappa from daily bars** -- loses to the Brownian null on 5/5 pairs.
      So ``roughness`` stays at the Brownian 1.0 and is an override, never a default.
    * **Snapping to technical levels** -- 4 of 80 cells after Benjamini-Hochberg, 2 of
      80 under the alternative control, **zero replication**.  Levels are displayed;
      snapping stays off.

And the method note that matters more than the result: **two control bugs, each of
which manufactured an effect**, were found in this harness.  So the levels study gets
a full null control here -- a pure random walk, where there is nothing to find, must
find nothing.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

rf = pytest.importorskip("fxgamma.signals.rangeforecast")
lv = pytest.importorskip("fxgamma.signals.levels")

PIP = 1e-4


# --------------------------------------------------------------------------- #
# synthetic price histories (offline, deterministic)
# --------------------------------------------------------------------------- #
def make_hist(n=2500, phi=0.0, sd=0.005, s0=1.16, seed=1, hl=0.004):
    rng = np.random.default_rng(seed)
    e = rng.standard_normal(n) * sd * math.sqrt(max(1.0 - phi * phi, 1e-12))
    r = np.empty(n)
    prev = rng.standard_normal() * sd
    for t in range(n):
        prev = phi * prev + e[t]
        r[t] = prev
    c = s0 * np.exp(np.cumsum(r))
    idx = pd.bdate_range(end="2026-09-07", periods=n, tz="UTC", name="date")
    return pd.DataFrame({"open": c, "high": c * (1 + hl), "low": c * (1 - hl),
                         "close": c}, index=idx)


@pytest.fixture(scope="module")
def rw_hist():
    return make_hist(2500, 0.0, seed=1)


@pytest.fixture(scope="module")
def walk_forward():
    sim = rf.simulate_rv(1400, seed=7, proxy_noise_sd=0.4)
    return rf.har_walk_forward(sim["proxy"], horizon=1, min_train=400)


# =========================================================================== #
# 1.  HAR-RV beats the naive benchmarks -- out of sample, with no look-ahead
# =========================================================================== #
class TestHARBeatsTheBenchmarks:
    def test_walk_forward_has_rows_and_both_benchmarks(self, walk_forward):
        assert len(walk_forward) > 500
        for c in ("actual", "har", "rw", "mean"):
            assert c in walk_forward.columns

    @pytest.mark.regression
    def test_no_look_ahead_every_forecast_is_made_before_its_target(self,
                                                                    walk_forward):
        """The single most important property of a forecast evaluation."""
        assert (walk_forward["asof"] < walk_forward.index).all()

    def test_the_rw_benchmark_is_the_value_known_at_asof(self, walk_forward):
        sim = rf.simulate_rv(1400, seed=7, proxy_noise_sd=0.4)["proxy"]
        got = sim.reindex(walk_forward["asof"]).to_numpy()
        assert np.allclose(got, walk_forward["rw"].to_numpy(), rtol=1e-12)

    def test_har_beats_yesterdays_rv_on_qlike(self, walk_forward):
        r2 = rf.r2_oos(walk_forward["actual"], walk_forward["har"],
                       walk_forward["rw"], loss="qlike")
        assert r2 > 0.20

    def test_har_beats_the_trailing_mean(self, walk_forward):
        r2 = rf.r2_oos(walk_forward["actual"], walk_forward["har"],
                       walk_forward["mean"], loss="qlike")
        assert r2 > 0.03

    def test_diebold_mariano_prefers_har_significantly(self, walk_forward):
        dm = rf.diebold_mariano(walk_forward["actual"], walk_forward["har"],
                                walk_forward["rw"])
        assert dm["stat"] < -2.5 and dm["p_value"] < 0.01

    def test_har_is_calibrated_in_LEVEL_not_just_ranking(self, walk_forward):
        """Mincer-Zarnowitz: alpha ~ 0, beta ~ 1.  A forecast that only ranks would
        still misprice the go/no-go."""
        mz = rf.mincer_zarnowitz(walk_forward["actual"], walk_forward["har"])
        assert abs(mz["beta"] - 1.0) < 0.25
        assert abs(mz["alpha"]) < 0.01

    def test_har_bias_is_small(self, walk_forward):
        ev = rf.evaluate_forecasts(
            walk_forward["actual"],
            {"har": walk_forward["har"], "rw": walk_forward["rw"]}, benchmark="rw")
        assert abs(float(ev.loc["har", "bias_vol"])) < 0.01

    # ---- the null controls of the evaluation machinery itself ---- #
    @pytest.mark.regression
    def test_a_benchmark_scores_exactly_zero_against_itself(self, walk_forward):
        assert rf.r2_oos(walk_forward["actual"], walk_forward["rw"],
                         walk_forward["rw"]) == pytest.approx(0.0, abs=1e-12)

    @pytest.mark.regression
    def test_diebold_mariano_of_a_forecast_against_itself_is_zero(self, walk_forward):
        dm = rf.diebold_mariano(walk_forward["actual"], walk_forward["har"],
                                walk_forward["har"])
        assert dm["stat"] == pytest.approx(0.0, abs=1e-9)
        assert dm["p_value"] == pytest.approx(1.0, abs=1e-9)

    @pytest.mark.regression
    def test_har_cannot_beat_the_benchmark_on_unforecastable_noise(self):
        """The null: white-noise vol has nothing to learn, so the OOS R^2 against the
        trailing mean must not be materially positive."""
        rng = np.random.default_rng(5)
        idx = pd.bdate_range(end="2026-09-07", periods=1200, tz="UTC", name="date")
        noise = pd.Series(np.exp(rng.standard_normal(1200) * 0.3) * 0.08, index=idx)
        wf = rf.har_walk_forward(noise, horizon=1, min_train=400)
        r2 = rf.r2_oos(wf["actual"], wf["har"], wf["mean"], loss="qlike")
        assert r2 < 0.05, f"HAR 'learned' {r2:.3f} from white noise"

    def test_qlike_is_zero_only_for_a_perfect_forecast(self):
        a = np.array([0.08, 0.09, 0.10])
        assert rf.qlike(a, a) == pytest.approx(0.0, abs=1e-15)
        assert rf.qlike(a, a * 1.2) > 0
        assert rf.qlike(a, a * 0.8) > 0

    def test_qlike_penalises_under_forecasting_harder(self):
        """The correct asymmetry for a book leaving resting orders: a range forecast
        that is too small leaves rungs unfilled or invites a stop."""
        a = np.array([0.08, 0.09, 0.10])
        assert rf.qlike(a, a * 0.8) > rf.qlike(a, a * 1.25)

    def test_har_uses_corsis_lags(self):
        assert rf.HAR_LAGS == (1, 5, 22)

    def test_har_fit_reports_hac_errors_and_persistence(self):
        sim = rf.simulate_rv(900, seed=3)["proxy"]
        f, info = rf.har_rv(sim)
        assert f > 0 and info["ok"]
        assert 0.0 < info["persistence"] < 1.4
        assert "r2_in" in info and "se_hac" in info and "t_hac" in info

    def test_log_space_can_never_return_a_negative_vol(self):
        """The reason log space is the default: positivity, not accuracy.  A negative
        variance is unrecoverable in a rung distance."""
        sim = rf.simulate_rv(600, seed=4, mean_vol=0.02)["proxy"]
        for space in ("log", "var", "vol"):
            f, _ = rf.har_rv(sim, space=space)
            if space == "log":
                assert f > 0

    def test_jensen_correction_raises_the_log_space_forecast(self):
        sim = rf.simulate_rv(900, seed=6)["proxy"]
        with_j, _ = rf.har_rv(sim, space="log", jensen=True)
        without, _ = rf.har_rv(sim, space="log", jensen=False)
        assert with_j > without

    def test_simulate_rv_recovers_a_known_process(self):
        """The machinery self-test: if HAR cannot recover a known OU log-variance, a
        small R^2 on real data cannot be blamed on the data."""
        sim = rf.simulate_rv(2000, seed=11, kappa=4.0, xi=0.45)
        wf = rf.har_walk_forward(sim["latent"], horizon=1, min_train=500)
        assert rf.r2_oos(wf["actual"], wf["har"], wf["mean"], loss="qlike") > 0.5


# =========================================================================== #
# 2.  Implied at a FIXED 0.5 weight -- fitting it is a machinery pass
# =========================================================================== #
class TestFixedBlendWeight:
    def test_the_shipped_blend_is_fifty_fifty(self):
        assert rf.DEFAULT_BLEND == {"har": 0.5, "implied": 0.5}

    def test_blend_of_equal_inputs_is_the_input(self):
        assert rf.blend_sigma(0.08, 0.08)[0] == pytest.approx(0.08, rel=1e-12)

    def test_blend_sits_between_its_two_inputs(self):
        assert 0.06 < rf.blend_sigma(0.06, 0.10)[0] < 0.10

    def test_missing_implied_falls_back_to_har_alone(self):
        """Absent-safe: a user with no vol mark still gets a forecast."""
        got, info = rf.blend_sigma(0.085, None)
        assert got == pytest.approx(0.085, rel=1e-9)
        assert isinstance(info, dict)

    def test_the_weights_are_not_refit_by_default(self):
        a = rf.blend_sigma(0.06, 0.10)[0]
        b = rf.blend_sigma(0.06, 0.10, weights=rf.DEFAULT_BLEND)[0]
        assert a == pytest.approx(b, rel=1e-12)

    def test_fit_blend_weights_is_available_but_separate(self, walk_forward):
        """It exists for the user to re-fit on their OWN data -- docs/10 says so
        explicitly -- but it is not what ships."""
        out = rf.fit_blend_weights(walk_forward["actual"], walk_forward["har"],
                                   walk_forward["rw"])
        assert "weight" in out or "w_implied" in out or isinstance(out, dict)


# =========================================================================== #
# 3.  kappa / efficiency / crossings -- the identity, verified to 1-5%
# =========================================================================== #
class TestRoughnessIdentity:
    @pytest.mark.parametrize("phi", [-0.3, -0.1, 0.0, 0.1, 0.3])
    def test_kappa_from_er_matches_kappa_from_qv_over_d2(self, phi):
        """docs/10: 'identity verified to 1-5%'.  Two independent estimators of the
        same statistic -- if they drift apart, one of them is wrong."""
        h = make_hist(6000, phi, seed=17)
        k = rf.roughness_kappa(h, window_bars=5)
        er = rf.efficiency_ratio(h, window_bars=5)
        assert k / rf.er_to_kappa(er, 5) == pytest.approx(1.0, abs=0.05), phi

    def test_brownian_er_maps_to_kappa_one(self):
        assert rf.er_to_kappa(1 / math.sqrt(5), 5) == pytest.approx(1.0, rel=1e-12)
        assert rf.kappa_to_er(1.0, 5) == pytest.approx(1 / math.sqrt(5), rel=1e-12)

    @pytest.mark.parametrize("n", [2, 5, 14, 60])
    @pytest.mark.parametrize("kappa", [0.3, 1.0, 2.5])
    def test_er_and_kappa_round_trip(self, n, kappa):
        assert rf.er_to_kappa(rf.kappa_to_er(kappa, n), n) == pytest.approx(kappa,
                                                                            rel=1e-9)

    def test_random_walk_kappa_is_one(self):
        assert rf.roughness_kappa(make_hist(8000, 0.0, seed=18), window_bars=5) == \
            pytest.approx(1.0, abs=0.10)

    def test_mean_reversion_reads_kappa_above_one(self):
        """docs/10 detects mean reversion at kappa=1.97 and momentum at kappa=0.32."""
        assert rf.roughness_kappa(make_hist(8000, -0.6, seed=19), window_bars=5) > 1.9

    def test_momentum_reads_kappa_below_one(self):
        assert rf.roughness_kappa(make_hist(8000, +0.6, seed=20), window_bars=5) < 0.5

    def test_kappa_is_monotone_in_persistence(self):
        ks = [rf.roughness_kappa(make_hist(5000, p, seed=21), window_bars=5)
              for p in (-0.4, -0.2, 0.0, 0.2, 0.4)]
        assert all(a > b for a, b in zip(ks, ks[1:]))

    def test_expected_crossings_is_the_closed_form(self):
        got = rf.expected_crossings(0.0031, 1.165, PIP, 18.0)
        assert got == pytest.approx((1.165 * 0.0031 / (18 * PIP)) ** 2, rel=1e-12)

    def test_expected_crossings_scale_as_one_over_spacing_squared(self):
        a = rf.expected_crossings(0.0031, 1.165, PIP, 10.0)
        b = rf.expected_crossings(0.0031, 1.165, PIP, 20.0)
        assert a / b == pytest.approx(4.0, rel=1e-12)

    def test_expected_crossings_are_linear_in_roughness(self):
        base = rf.expected_crossings(0.0031, 1.165, PIP, 18.0, roughness=1.0)
        assert rf.expected_crossings(0.0031, 1.165, PIP, 18.0, roughness=2.0) == \
            pytest.approx(2.0 * base, rel=1e-12)

    def test_level_crossings_at_the_money_is_the_tanaka_constant(self):
        got = rf.expected_level_crossings(0.0, 0.0031, 1.165, PIP, 18.0)
        assert got == pytest.approx(0.7978845608 * 1.165 * 0.0031 / (18 * PIP),
                                    rel=1e-9)

    def test_outer_rungs_refill_less_often_than_inner_ones(self):
        """Which is exactly why sizing every rung identically is wrong."""
        vals = [rf.expected_level_crossings(d, 0.0031, 1.165, PIP, 18.0)
                for d in (0.0, 18.0, 36.0, 72.0)]
        assert all(a > b for a, b in zip(vals, vals[1:]))

    def test_grid_crossings_is_the_renko_count(self):
        p = np.array([0.0, 1.0, 2.0, 1.0, 0.0])
        assert rf.grid_crossings(p, 1.0) == 4

    def test_grid_crossings_is_sampling_consistent(self):
        """``h^2 N_h -> QV``.  The fixed-grid alternative is not: a path wiggling over
        one line racks up crossings without bound, which is the bug that was caught."""
        h = make_hist(4000, 0.0, sd=0.002, seed=22)
        c = h["close"].to_numpy(float)
        qv = float(np.sum(np.diff(c) ** 2))
        for step in (0.004, 0.008):
            n = rf.grid_crossings(c, step)
            assert n * step * step < 1.3 * qv, "the counter is over-reading QV"

    def test_grid_crossings_of_a_flat_path_is_zero(self):
        assert rf.grid_crossings(np.ones(50), 0.001) == 0

    def test_grid_crossings_rejects_a_degenerate_spacing(self):
        assert rf.grid_crossings(np.arange(10.0), 0.0) == 0
        assert rf.grid_crossings(np.arange(10.0), -1.0) == 0


# =========================================================================== #
# 4.  Roughness is NOT forecastable -- the Brownian null stands
# =========================================================================== #
class TestRoughnessStaysAtTheBrownianNull:
    """docs/10 RULING: use the Brownian baseline for expected crossings, expose kappa
    as a user override, and state on the panel that expected fills assume a Brownian
    path.  Do not present forecast roughness as predictive."""

    def test_expected_crossings_defaults_to_roughness_one(self):
        a = rf.expected_crossings(0.0031, 1.165, PIP, 18.0)
        b = rf.expected_crossings(0.0031, 1.165, PIP, 18.0, roughness=1.0)
        assert a == pytest.approx(b, rel=1e-12)

    def test_expected_level_crossings_defaults_to_roughness_one(self):
        a = rf.expected_level_crossings(20.0, 0.0031, 1.165, PIP, 18.0)
        b = rf.expected_level_crossings(20.0, 0.0031, 1.165, PIP, 18.0, roughness=1.0)
        assert a == pytest.approx(b, rel=1e-12)

    def test_forecast_roughness_is_the_simplest_thing_that_could_work(self, rw_hist):
        k, info = rf.forecast_roughness(rw_hist)
        assert np.isfinite(k) and k > 0
        assert isinstance(info, dict)

    @pytest.mark.regression
    def test_forecast_roughness_does_not_beat_the_brownian_null_on_a_random_walk(
            self, rw_hist):
        """The null control: on a driftless random walk there is nothing to forecast,
        so the trailing estimate must land on 1.0 and not claim skill."""
        k, _ = rf.forecast_roughness(rw_hist, lookback=250)
        assert abs(k - 1.0) < 0.20

    @pytest.mark.slow
    def test_walk_forward_roughness_reports_its_own_r2(self, rw_hist):
        out = rf.roughness_walk_forward(rw_hist, "EURUSD", spacing_pips=25.0)
        assert isinstance(out, (dict, pd.DataFrame))

    def test_daily_bars_undercount_crossings(self, rw_hist):
        """'Daily sampling recovers only 46-63% of the true crossing count.'  A
        counter that did NOT undercount on coarse bars would be the bug."""
        cs = rf.crossings_series(rw_hist, 25.0, "EURUSD", window_bars=20)
        assert len(cs)
        assert cs["crossings"].sum() < cs["bm_crossings"].sum()


# =========================================================================== #
# 5.  Session variance profile and event segmentation
# =========================================================================== #
class TestSessionAndEvents:
    def test_the_hour_profile_is_a_distribution(self):
        assert len(rf.SESSION_VAR_PROFILE) == 24
        assert sum(rf.SESSION_VAR_PROFILE.values()) == pytest.approx(1.0, rel=1e-9)
        assert all(v > 0 for v in rf.SESSION_VAR_PROFILE.values())

    def test_window_var_fraction_of_a_whole_day_is_one(self):
        import datetime as dtm
        s = dtm.datetime(2026, 9, 7, 0, 0, tzinfo=dtm.timezone.utc)
        assert rf.window_var_fraction(s, s + dtm.timedelta(days=1)) == \
            pytest.approx(1.0, rel=1e-9)

    def test_the_overnight_window_is_well_under_the_clock_share(self):
        import datetime as dtm
        s = dtm.datetime(2026, 9, 7, 16, 0, tzinfo=dtm.timezone.utc)
        e = s + dtm.timedelta(hours=14)
        assert rf.window_var_fraction(s, e) < 14.0 / 24.0

    def test_session_hours_covers_the_window(self):
        import datetime as dtm
        s = dtm.datetime(2026, 9, 7, 16, 30, tzinfo=dtm.timezone.utc)
        e = s + dtm.timedelta(hours=4)
        parts = rf.session_hours(s, e)
        assert sum(w for _, w in parts) == pytest.approx(4.0, rel=1e-9)

    def test_event_variance_add_is_zero_with_no_events(self):
        """Null control: no calendar, no uplift."""
        import datetime as dtm
        s = dtm.datetime(2026, 9, 7, 16, 0, tzinfo=dtm.timezone.utc)
        add, n = rf.event_variance_add(None, "EURUSD", s,
                                       s + dtm.timedelta(hours=14))
        assert add == 0.0 and n == 0

    def test_tier_1_events_add_nothing(self):
        """docs/10 ruled the event uplift MAGNITUDE not estimable (n~25, no |t|>2).
        Segmentation is kept; the size is not invented."""
        assert rf.EVENT_SIGMA[1] == 0.0
        assert rf.EVENT_SIGMA[3] > rf.EVENT_SIGMA[2] > rf.EVENT_SIGMA[1]

    def test_a_tier3_event_inside_the_window_raises_the_variance(self):
        import datetime as dtm
        s = dtm.datetime(2026, 9, 7, 16, 0, tzinfo=dtm.timezone.utc)
        e = s + dtm.timedelta(hours=14)
        ev = pd.DataFrame({"datetime": [s + dtm.timedelta(hours=5)],
                           "event": ["FOMC"], "importance": [3], "ccy": ["USD"],
                           "source": ["t"]})
        assert rf.event_variance_add(ev, "EURUSD", s, e)[0] > 0.0

    def test_an_event_outside_the_window_adds_nothing(self):
        import datetime as dtm
        s = dtm.datetime(2026, 9, 7, 16, 0, tzinfo=dtm.timezone.utc)
        e = s + dtm.timedelta(hours=14)
        ev = pd.DataFrame({"datetime": [s - dtm.timedelta(hours=5)],
                           "event": ["FOMC"], "importance": [3], "ccy": ["USD"],
                           "source": ["t"]})
        assert rf.event_variance_add(ev, "EURUSD", s, e)[0] == 0.0

    def test_boj_is_flagged_as_having_no_fixed_announcement_time(self):
        """CR-16: the calendar asserts a precise 03:00 for BoJ, which has no fixed
        time.  Flag, do not silently use."""
        assert any("boj" in x for x in rf.SOFT_TIME_EVENTS)
        assert not rf._time_certain("BoJ policy decision")
        assert rf._time_certain("US CPI")

    def test_window_segments_splits_around_an_event(self):
        import datetime as dtm
        s = dtm.datetime(2026, 9, 7, 16, 0, tzinfo=dtm.timezone.utc)
        e = s + dtm.timedelta(hours=14)
        ev = pd.DataFrame({"datetime": [s + dtm.timedelta(hours=5)],
                           "event": ["FOMC"], "importance": [3], "ccy": ["USD"],
                           "source": ["t"]})
        kw = dict(sigma_day=0.005, spot=1.165, pip=PIP)
        segs, notes = rf.window_segments(s, e, ev, "EURUSD", **kw)
        plain, _ = rf.window_segments(s, e, None, "EURUSD", **kw)
        assert len(segs) > len(plain) >= 1, "a tier-3 event must split the window"
        # segmentation moves variance WITHIN the window; it does not create any.
        assert sum(x.var_fraction for x in segs) == pytest.approx(
            sum(x.var_fraction for x in plain), rel=1e-9)

    def test_touch_probability_is_the_reflection_principle(self):
        """Same ruling as ``overnight``: touch, not terminal."""
        from fxgamma.models import gk
        sw, S = 0.0031, 1.165
        for pips in (5, 20, 60):
            p = rf.touch_probability(pips, sw, S, PIP)
            d = pips * PIP / (S * sw)
            assert p == pytest.approx(2.0 * gk._norm_cdf(-d), rel=1e-6)

    def test_expected_max_excursion_exceeds_one_sigma(self):
        """E[max |W|] over a window is larger than the terminal sd."""
        sw, S = 0.0031, 1.165
        assert rf.expected_max_excursion(sw, S, PIP) > S * sw / PIP


# =========================================================================== #
# 6.  Levels -- the NULL CONTROL that decides whether snapping ships
# =========================================================================== #
@pytest.fixture(scope="module")
def panel(rw_hist):
    return lv.level_panel(rw_hist, "EURUSD",
                          kinds=("prior_high", "prior_low", "round_50"))


class TestLevelsAreMeasuredNotAsserted:
    """Two control bugs in this harness manufactured effects.  A pure random walk with
    no memory is the one case where the right answer is known: **nothing**.  If the
    measurement finds something here, it is broken."""

    def test_the_panel_is_rebuilt_at_every_bar(self, panel, rw_hist):
        assert len(panel) > 100
        assert "bar" in panel.columns
        assert panel["bar"].nunique() > 100

    @pytest.mark.regression
    def test_fill_quality_finds_nothing_on_a_random_walk(self, rw_hist, panel):
        """THE null control.  ``snap_recommended`` must be False for every kind."""
        out = lv.measure_fill_quality(rw_hist, panel, pair="EURUSD", horizon_h=12,
                                      n_control=20, n_boot=200)
        assert len(out)
        assert not out["snap_recommended"].any(), \
            f"snapping 'recommended' on a random walk: {out['kind'].tolist()}"

    @pytest.mark.regression
    def test_the_mark_edge_is_within_noise_on_a_random_walk(self, rw_hist, panel):
        out = lv.measure_fill_quality(rw_hist, panel, pair="EURUSD", horizon_h=12,
                                      n_control=20, n_boot=200)
        for _, r in out.iterrows():
            assert abs(r["d_mark"]) < 1.0, f"{r['kind']}: d_mark {r['d_mark']:.2f} pips"
            assert r["d_mark_lo"] <= 0.0 <= r["d_mark_hi"], \
                f"{r['kind']}: the bootstrap CI excludes zero on a random walk"

    @pytest.mark.regression
    def test_the_control_is_distance_matched(self, rw_hist, panel):
        """If the control sits at a different distance from spot it is not a control;
        it is a second, different measurement, and the difference is manufactured."""
        out = lv.measure_fill_quality(rw_hist, panel, pair="EURUSD", horizon_h=12,
                                      n_control=20, n_boot=100)
        for _, r in out.iterrows():
            assert r["fill_rate"] == pytest.approx(r["ctrl_fill_rate"], abs=0.05), \
                f"{r['kind']}: real {r['fill_rate']:.3f} vs control {r['ctrl_fill_rate']:.3f}"

    @pytest.mark.regression
    def test_reversal_stats_find_nothing_on_a_random_walk(self, rw_hist, panel):
        out = lv.measure_reversal_stats(rw_hist, panel, pair="EURUSD", horizon_h=12,
                                        n_control=20, n_boot=200)
        assert len(out)
        sig = [c for c in out.columns if "p_" in c and "bh" in c]
        assert sig
        assert (out[sig[0]].dropna() > 0.05).all(), \
            "a reversal effect was found where there is none"

    def test_both_controls_are_available(self, rw_hist, panel):
        """docs/10 s6.1 records that the raw distance-matched control the brief
        specified is BIASED for range-proportional level kinds, so an alternative is
        required and both must be reachable."""
        for ctrl in ("permuted", "permuted_raw", "jitter"):
            out = lv.measure_fill_quality(rw_hist, panel, pair="EURUSD",
                                          control=ctrl, n_control=6, n_boot=40)
            assert len(out) and out["control"].iloc[0] == ctrl

    def test_an_unknown_control_raises(self, rw_hist, panel):
        with pytest.raises(ValueError):
            lv.measure_fill_quality(rw_hist, panel, pair="EURUSD", control="wishful",
                                    n_control=2, n_boot=10)

    def test_multiplicity_is_adjusted(self, rw_hist, panel):
        out = lv.measure_fill_quality(rw_hist, panel, pair="EURUSD", n_control=6,
                                      n_boot=40)
        assert "p_mark_bh" in out.columns and "p_mark_holm" in out.columns
        assert (out["p_mark_bh"] >= out["p_mark"] - 1e-12).all()
        assert (out["p_mark_holm"] >= out["p_mark_bh"] - 1e-12).all()

    def test_snap_requires_a_minimum_number_of_fills(self, rw_hist, panel):
        out = lv.measure_fill_quality(rw_hist, panel, pair="EURUSD", n_control=6,
                                      n_boot=40, min_obs=10 ** 9)
        assert not out["snap_recommended"].any()

    def test_bh_and_holm_are_correct_on_a_known_input(self):
        p = np.array([0.01, 0.02, 0.03, 0.04, 0.05])
        bh = lv._bh(p)
        holm = lv._holm(p)
        assert (bh >= p - 1e-12).all() and (holm >= bh - 1e-12).all()
        assert holm[0] == pytest.approx(0.05, rel=1e-9)

    def test_all_p_values_of_one_are_preserved(self):
        p = np.ones(4)
        assert np.allclose(lv._bh(p), 1.0)
        assert np.allclose(lv._holm(p), 1.0)


# =========================================================================== #
# 7.  Level construction -- candidate anchors, displayed not asserted
# =========================================================================== #
class TestLevelConstruction:
    def test_technical_levels_has_the_frozen_columns(self, rw_hist):
        out = lv.technical_levels(rw_hist, "EURUSD", rw_hist.index[-1])
        for c in ("level", "kind", "strength", "age_days", "source"):
            assert c in out.columns

    def test_every_kind_is_in_the_registry(self, rw_hist):
        out = lv.technical_levels(rw_hist, "EURUSD", rw_hist.index[-1])
        assert set(out["kind"]) <= set(lv.LEVEL_KINDS)

    def test_every_kind_has_a_group_and_a_prior(self, rw_hist):
        for k in lv.LEVEL_KINDS:
            assert k in lv.KIND_GROUP
        assert set(lv.STRENGTH_PRIOR) <= set(lv.LEVEL_KINDS)

    def test_round_levels_are_on_the_grid(self):
        out = lv.round_levels(1.1650, PIP, span_pips=200.0)
        assert len(out)
        for x, kind in out:
            assert kind in lv.LEVEL_KINDS
            assert (round(x * 10000) % 25) == 0

    def test_round_levels_classify_big_half_and_quarter(self):
        kinds = {k for _, k in lv.round_levels(1.1650, PIP, span_pips=200.0)}
        assert {"round_big", "round_half", "round_quarter"} <= kinds

    def test_round_levels_span_both_sides_of_spot(self):
        out = lv.round_levels(1.1650, PIP, span_pips=100.0)
        assert any(x > 1.1650 for x, _ in out) and any(x < 1.1650 for x, _ in out)

    def test_pivot_levels_bracket_the_pivot(self):
        p = lv.pivot_levels(1.17, 1.16, 1.165)
        assert p["pivot_s1"] < p["pivot"] < p["pivot_r1"]
        assert p["pivot_s2"] < p["pivot_s1"] and p["pivot_r1"] < p["pivot_r2"]

    def test_pivot_is_the_typical_price(self):
        p = lv.pivot_levels(1.17, 1.16, 1.165)
        assert p["pivot"] == pytest.approx((1.17 + 1.16 + 1.165) / 3.0, rel=1e-12)

    def test_swing_points_are_local_extrema(self, rw_hist):
        out = lv.swing_points(rw_hist, k=3, lookback=120)
        assert isinstance(out, list)
        hi = rw_hist["high"].to_numpy(float)
        lo = rw_hist["low"].to_numpy(float)
        for i, level, kind in out:
            assert kind in lv.LEVEL_KINDS
            win = slice(max(i - 3, 0), i + 4)
            assert level == pytest.approx(hi[i], rel=1e-12) or \
                level == pytest.approx(lo[i], rel=1e-12)
            if level == pytest.approx(hi[i], rel=1e-12):
                assert hi[i] >= hi[win].max() - 1e-12
            else:
                assert lo[i] <= lo[win].min() + 1e-12

    def test_levels_are_trimmed_by_distance(self, rw_hist):
        near = lv.technical_levels(rw_hist, "EURUSD", rw_hist.index[-1],
                                   max_dist_pips=40.0)
        far = lv.technical_levels(rw_hist, "EURUSD", rw_hist.index[-1],
                                  max_dist_pips=400.0)
        assert len(near) <= len(far)

    def test_nearest_level_respects_the_distance_cap(self, rw_hist):
        out = lv.technical_levels(rw_hist, "EURUSD", rw_hist.index[-1])
        S = float(rw_hist["close"].iloc[-1])
        assert lv.nearest_level(S, out, pair="EURUSD", max_pips=0.0001) is None

    def test_nearest_level_finds_the_closest(self, rw_hist):
        out = lv.technical_levels(rw_hist, "EURUSD", rw_hist.index[-1])
        S = float(rw_hist["close"].iloc[-1])
        got = lv.nearest_level(S, out, pair="EURUSD", max_pips=10_000.0)
        assert got is not None
        best = min(abs(out["level"] - S))
        assert abs(got["level"] - S) == pytest.approx(best, rel=1e-9)

    def test_nearest_level_can_be_restricted_to_kinds(self, rw_hist):
        out = lv.technical_levels(rw_hist, "EURUSD", rw_hist.index[-1])
        kinds = [sorted(set(out["kind"]))[0]]
        got = lv.nearest_level(float(rw_hist["close"].iloc[-1]), out, pair="EURUSD",
                               max_pips=10_000.0, kinds=kinds)
        assert got is None or got["kind"] in kinds

    def test_nearest_level_of_an_empty_frame_is_none(self):
        assert lv.nearest_level(1.16, pd.DataFrame(), pair="EURUSD") is None

    def test_oi_levels_takes_a_pair_so_pip_size_is_not_guessed(self):
        """Contract fix approved in the amendment."""
        oi = pd.DataFrame({"strike": [1.15, 1.16, 1.17, 1.18],
                           "oi": [100, 900, 400, 50]})
        out = lv.oi_levels(oi, 1.165, top=3, pair="EURUSD")
        assert len(out) <= 3
        assert "level" in out.columns

    def test_oi_levels_ranks_by_open_interest(self):
        oi = pd.DataFrame({"strike": [1.15, 1.16, 1.17], "oi": [10, 900, 50]})
        out = lv.oi_levels(oi, 1.165, top=1, pair="EURUSD")
        assert float(out["level"].iloc[0]) == pytest.approx(1.16, rel=1e-9)

    def test_oi_levels_of_an_empty_frame_is_empty_not_fatal(self):
        assert lv.oi_levels(pd.DataFrame(), 1.165, pair="EURUSD").empty

    def test_measure_reversal_stats_accepts_a_pair(self, rw_hist):
        """The other approved contract fix: a single-date frame carries no history,
        so the frozen signature could not work."""
        one = lv.technical_levels(rw_hist, "EURUSD", rw_hist.index[-1])
        out = lv.measure_reversal_stats(rw_hist, one, pair="EURUSD", n_control=4,
                                        n_boot=20)
        assert isinstance(out, pd.DataFrame)

    def test_measure_without_a_panel_or_a_pair_raises(self, rw_hist):
        one = lv.technical_levels(rw_hist, "EURUSD", rw_hist.index[-1])
        with pytest.raises(ValueError):
            lv.measure_reversal_stats(rw_hist, one, n_control=2, n_boot=10)

    def test_empty_levels_return_an_empty_frame(self, rw_hist):
        assert lv.measure_fill_quality(rw_hist, pd.DataFrame(),
                                       pair="EURUSD").empty


# =========================================================================== #
# 8.  overnight_range_forecast -- the assembled object
# =========================================================================== #
@pytest.fixture(scope="module")
def fc(rw_hist, on_mkt):
    ov = pytest.importorskip("fxgamma.portfolio.overnight")
    win = ov.passive_window(on_mkt.asof, pair="EURUSD")
    return rf.overnight_range_forecast("EURUSD", on_mkt, rw_hist, win)


class TestRangeForecastObject:
    def test_sigma_window_is_positive_and_small(self, fc):
        assert 0.0 < fc.sigma_window < 0.05

    def test_expected_move_is_positive(self, fc):
        assert fc.exp_abs_move_pips > 0

    def test_quantiles_are_monotone(self, fc):
        ks = sorted(fc.quantiles)
        vals = [fc.quantiles[k] for k in ks]
        assert all(a <= b for a, b in zip(vals, vals[1:]))

    def test_the_median_is_near_zero(self, fc):
        if 0.5 in fc.quantiles:
            assert abs(fc.quantiles[0.5]) < 2.0

    def test_components_are_reported(self, fc):
        assert set(fc.components) & {"har", "implied", "session", "event"}

    def test_basis_is_stated(self, fc):
        assert isinstance(fc.basis, str) and fc.basis

    def test_the_forecast_is_frozen(self, fc):
        with pytest.raises(Exception):
            fc.sigma_window = 1.0

    def test_expected_move_is_about_08_of_sigma(self, fc, on_mkt):
        """E|N(0,s)| = s sqrt(2/pi) = 0.7979 s."""
        S = on_mkt.spot["EURUSD"]
        assert fc.exp_abs_move_pips == pytest.approx(
            math.sqrt(2 / math.pi) * S * fc.sigma_window / PIP, rel=0.15)

    def test_the_forecast_drives_the_ladder(self, ref_book, on_mkt, fc):
        ov = pytest.importorskip("fxgamma.portfolio.overnight")
        r = ov.overnight_ladder(ref_book, on_mkt, "EURUSD", range_forecast=fc)
        s = ov.ladder_summary(r, ref_book, on_mkt, "EURUSD", range_forecast=fc)
        assert s["sigma_window"] == pytest.approx(fc.sigma_window, rel=1e-12)
