"""``fxgamma/backtest/`` -- the delta-hedged gamma engine.

A backtest is the one module in the repo that can be *silently* worthless.  A wrong
Greek shows up as a wrong number on a card; a look-ahead shows up as a beautiful
equity curve, and the trader finds out by losing money.  So the ordering here is:

1. **No look-ahead.**  The engine must read only rows ``0..i`` at step ``i``, its
   result must change when the driving series is shifted, and it must reproduce
   itself exactly when the shift is undone (REQ-060).
2. **The controlled vol experiment.**  With ``sigma_r > sigma_i`` a delta-hedged long
   straddle must make money and a short one must lose it, and both must flip when the
   inequality flips.  That single test exercises the pricer, the Greeks, the hedger
   and the cash accounting end to end -- if the sign is wrong anywhere, it fails.
3. **Costs are actually charged.**  ``pnl_gross - pnl_net`` must equal the costs the
   engine says it charged, and hedging more often must cost more.  A backtest that
   reports gross as net is the second-oldest way to sell a strategy that loses money.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from fxgamma.backtest import engine, metrics, strategies
from fxgamma.types import HedgeRule

pytestmark = pytest.mark.contract


@pytest.fixture(scope="module")
def hot_path():
    """Realized 14% against an 8% mark -- owning gamma must pay."""
    return engine.synthetic_path(n_days=180, sigma_r=0.14, sigma_i=0.08, seed=11)


@pytest.fixture(scope="module")
def cold_path():
    """Realized 5% against a 10% mark -- owning gamma must lose."""
    return engine.synthetic_path(n_days=180, sigma_r=0.05, sigma_i=0.10, seed=11)


# --------------------------------------------------------------------------- #
# 1. no look-ahead
# --------------------------------------------------------------------------- #
class TestNoLookAhead:
    def test_the_view_cannot_see_past_now(self, hot_path):
        """The ``View`` is the contract between the strategy and the future."""
        df = hot_path.df
        i = 40
        v = engine.View(df, i, hot_path.pair, hot_path.meta)
        assert len(v.history) == i + 1
        assert v.history.index[-1] == v.now == df.index[i]
        assert v.spot == pytest.approx(float(df["spot"].iloc[i]))
        assert v.vol == pytest.approx(float(df["vol"].iloc[i]))

    def test_the_view_hands_out_a_copy_so_a_strategy_cannot_edit_the_path(self,
                                                                         hot_path):
        v = engine.View(hot_path.df, 40, hot_path.pair, hot_path.meta)
        h = v.history
        h.iloc[-1, h.columns.get_loc("spot")] = 99.0
        assert float(hot_path.df["spot"].iloc[40]) != 99.0

    def test_indexing_beyond_now_raises_rather_than_returning_the_future(self, hot_path):
        v = engine.View(hot_path.df, 40, hot_path.pair, hot_path.meta)
        with pytest.raises((IndexError, ValueError)):
            v.at(41)
        assert v.at(39) is not None

    def test_the_views_realized_vol_only_uses_history(self, hot_path):
        """A strategy that trades on realized vol is the classic place a backtest
        leaks: computing RV over the *whole* path makes every entry prescient."""
        i = 100
        v = engine.View(hot_path.df, i, hot_path.pair, hot_path.meta)
        rv = v.realized_vol(window=21)
        truncated = engine.PathData(hot_path.pair, hot_path.df.iloc[: i + 1],
                                    hot_path.steps_per_day, hot_path.meta)
        v2 = engine.View(truncated.df, i, hot_path.pair, hot_path.meta)
        assert rv == pytest.approx(v2.realized_vol(window=21), rel=1e-12)

    def test_the_lookahead_report_passes_on_every_shipped_strategy(self, hot_path):
        """REQ-060.  Shift the driving series forward: the result must change (the
        strategy is really reading the data) and must restore exactly when the shift
        is undone (nothing is reading an absolute index or a cached future)."""
        short = engine.synthetic_path(n_days=90, sigma_r=0.14, sigma_i=0.08, seed=3)
        for cfg in (strategies.long_straddle(tenor_days=30),
                    strategies.short_straddle(tenor_days=30),
                    strategies.long_straddle(tenor_days=30,
                                             entry=strategies.gamma_carry())):
            rep = engine.lookahead_report(short, cfg)
            assert rep["changed_under_shift"], f"{cfg.name} ignores the data it is given"
            assert rep["restored_exactly"], f"{cfg.name} is not a pure function of the path"
            assert rep["passed"]

    def test_truncating_the_path_does_not_change_the_earlier_steps(self, hot_path):
        """The strongest form of the no-look-ahead claim: what happened by step 60
        cannot depend on data that arrives after step 60."""
        cfg = strategies.long_straddle(tenor_days=30)
        full = engine.run_backtest(hot_path, cfg)
        cut = engine.PathData(hot_path.pair, hot_path.df.iloc[:61],
                              hot_path.steps_per_day, hot_path.meta)
        part = engine.run_backtest(cut, cfg)
        # every step but the last is bit-identical.  The final bar differs by design:
        # the engine will not open a new structure it cannot mark again (`i < n - 1`).
        assert np.array_equal(full.equity["equity"].to_numpy(float)[:60],
                              part.equity["equity"].to_numpy(float)[:60])

    def test_a_strategy_that_reads_the_future_makes_visibly_more_money(self):
        """The negative control.  An entry rule that looks at the realized vol of the
        *next* 30 days turns a losing honest run (-1.1mm) into a winning one (+2.1mm)
        on the same path.  The gap is what a real leak is worth, and it is the yardstick
        the checks above are measured against.
        """
        path = engine.synthetic_path(n_days=360, sigma_r=0.10, sigma_i=0.10, seed=21,
                                     vol_of_vol=0.5)
        df = path.df

        def cheat(view):
            s = df["spot"].to_numpy(float)[view.i: view.i + 31]
            if s.size < 5:
                return 0
            r = np.diff(np.log(s))
            return +1 if float(np.sqrt(252 * np.mean(r ** 2))) > view.vol else -1

        honest = engine.run_backtest(path, strategies.long_straddle(tenor_days=30))
        peeking = engine.run_backtest(
            path, strategies.long_straddle(tenor_days=30, entry=cheat))
        assert float(peeking.equity["equity"].iloc[-1]) > \
            float(honest.equity["equity"].iloc[-1]) + 1e6

    def test_the_lookahead_report_does_not_catch_a_leak_inside_the_strategy(self):
        """**FINDING (docs/05_test_report.md F-11): scope of the REQ-060 check.**

        ``lookahead_report`` shifts the path, re-runs and shifts back.  A strategy
        that closes over the *whole* DataFrame -- exactly the cheat above -- shifts
        with it, so the report comes back ``passed=True`` on a rule that is reading
        30 days of future prices.  The check is a sound test of the **engine's**
        indexing and a *non*-test of a strategy's own honesty; only the ``View`` is
        stopping the latter, and a rule that ignores its ``View`` bypasses it.

        Not a bug -- a boundary.  It matters because REQ-060's evidence is displayed
        in the Lab, and "look-ahead check: passed" reads as a claim about the
        strategy.  The panel must say which of the two it certifies.  Owner: PM to
        rule; dev to word the panel.
        """
        path = engine.synthetic_path(n_days=180, sigma_r=0.10, sigma_i=0.10, seed=21,
                                     vol_of_vol=0.5)
        df = path.df

        def cheat(view):
            s = df["spot"].to_numpy(float)[view.i: view.i + 31]
            if s.size < 5:
                return 0
            r = np.diff(np.log(s))
            return +1 if float(np.sqrt(252 * np.mean(r ** 2))) > view.vol else -1

        rep = engine.lookahead_report(
            path, strategies.long_straddle(tenor_days=30, entry=cheat))
        assert rep["passed"], (
            "if this now fails, the detector got stronger -- update the report and "
            "this docstring rather than deleting the test")


# --------------------------------------------------------------------------- #
# 2. the controlled vol experiment
# --------------------------------------------------------------------------- #
class TestTheVolExperiment:
    def test_long_gamma_pays_when_realized_beats_implied(self, hot_path):
        r = engine.run_backtest(hot_path, strategies.long_straddle(tenor_days=30))
        assert float(r.equity["equity"].iloc[-1]) > 0

    def test_short_gamma_loses_on_the_same_path(self, hot_path):
        r = engine.run_backtest(hot_path, strategies.short_straddle(tenor_days=30))
        assert float(r.equity["equity"].iloc[-1]) < 0

    def test_both_signs_flip_when_realized_drops_below_implied(self, cold_path):
        """The full sign matrix.  A single sign error anywhere in the pricer, the
        hedger or the cash accounting breaks one of these four assertions."""
        long_ = engine.run_backtest(cold_path, strategies.long_straddle(tenor_days=30))
        short = engine.run_backtest(cold_path, strategies.short_straddle(tenor_days=30))
        assert float(long_.equity["equity"].iloc[-1]) < 0
        assert float(short.equity["equity"].iloc[-1]) > 0

    def test_long_and_short_are_near_mirror_images_before_costs(self, hot_path):
        """Gross of costs the two sides are the same trade with opposite signs; the
        gap is the bid/offer each pays, so it must be small and *both* must lose it."""
        long_ = engine.run_backtest(hot_path, strategies.long_straddle(tenor_days=30))
        short = engine.run_backtest(hot_path, strategies.short_straddle(tenor_days=30))
        gl = float(long_.equity["equity_gross"].iloc[-1])
        gs = float(short.equity["equity_gross"].iloc[-1])
        assert gl > 0 > gs
        assert abs(gl + gs) < 0.10 * abs(gl), (gl, gs)
        assert float(long_.stats["total_cost"]) > 0
        assert float(short.stats["total_cost"]) > 0

    def test_the_captured_vol_lands_near_the_paths_realized_vol(self, hot_path):
        """REQ-056: the sigma_r that would have made gamma pay the theta bill.  On a
        well-hedged long straddle it must recover the path's own 14%."""
        r = engine.run_backtest(hot_path,
                                strategies.long_straddle(tenor_days=30, band_pct=5.0))
        assert float(r.stats["realized_vol_captured"]) == pytest.approx(0.14, abs=0.03)

    def test_a_never_hedged_book_is_a_different_and_noisier_trade(self, hot_path):
        from dataclasses import replace
        never = engine.run_backtest(hot_path, replace(
            strategies.long_straddle(tenor_days=30), hedge=HedgeRule(mode="none")))
        hedged = engine.run_backtest(hot_path, strategies.long_straddle(tenor_days=30))
        assert float(never.stats["n_hedges"]) == 0
        assert float(hedged.stats["n_hedges"]) > 0
        assert float(never.stats["total_cost"]) < float(hedged.stats["total_cost"])


# --------------------------------------------------------------------------- #
# 3. costs
# --------------------------------------------------------------------------- #
class TestCostsAreCharged:
    def test_gross_minus_net_is_exactly_the_charged_cost(self, hot_path):
        r = engine.run_backtest(hot_path, strategies.long_straddle(tenor_days=30))
        eq = r.equity
        assert np.allclose(eq["pnl_gross"] - eq["pnl"], eq["cost"], atol=1e-9)
        assert float(eq["equity_gross"].iloc[-1] - eq["equity"].iloc[-1]) == \
            pytest.approx(float(eq["cost"].sum()), rel=1e-9)

    def test_the_cost_series_is_never_negative(self, hot_path):
        r = engine.run_backtest(hot_path, strategies.long_straddle(tenor_days=30))
        assert (r.equity["cost"].to_numpy(float) >= -1e-12).all()
        assert (r.trades["cost"].to_numpy(float) >= -1e-12).all()

    def test_hedge_cost_plus_option_cost_is_the_total(self, hot_path):
        s = engine.run_backtest(hot_path, strategies.long_straddle(tenor_days=30)).stats
        assert s["hedge_cost"] + s["option_cost"] == pytest.approx(s["total_cost"],
                                                                  rel=1e-9)
        assert s["hedge_cost"] > 0 and s["option_cost"] > 0

    def test_a_tighter_band_hedges_more_often_and_costs_more(self, hot_path):
        wide = engine.run_backtest(hot_path, strategies.long_straddle(
            tenor_days=30, band_pct=40.0)).stats
        tight = engine.run_backtest(hot_path, strategies.long_straddle(
            tenor_days=30, band_pct=2.0)).stats
        assert tight["n_hedges"] > wide["n_hedges"]
        assert tight["hedge_cost"] > wide["hedge_cost"]
        assert tight["turnover_base"] > wide["turnover_base"]

    def test_doubling_the_spot_cost_doubles_the_hedge_cost(self, hot_path):
        from dataclasses import replace
        base = strategies.long_straddle(tenor_days=30)
        a = engine.run_backtest(hot_path, replace(base, cost_bp=0.4)).stats
        b = engine.run_backtest(hot_path, replace(base, cost_bp=0.8)).stats
        assert a["turnover_base"] == pytest.approx(b["turnover_base"], rel=0.35)
        assert b["hedge_cost"] > 1.5 * a["hedge_cost"]

    def test_zero_costs_make_gross_and_net_coincide(self, hot_path):
        from dataclasses import replace
        cfg = replace(strategies.long_straddle(tenor_days=30),
                      vega_spread_pts=0.0, cost_bp=0.0)
        r = engine.run_backtest(hot_path, cfg)
        assert float(r.equity["cost"].sum()) == pytest.approx(0.0, abs=1e-9)
        assert float(r.equity["equity"].iloc[-1]) == \
            pytest.approx(float(r.equity["equity_gross"].iloc[-1]), rel=1e-9)

    def test_the_option_bid_offer_is_charged_on_both_sides(self, hot_path):
        """A short straddle sells at the bid and buys back at the offer; charging the
        spread only on entry makes every short-vol strategy look better than it is."""
        from dataclasses import replace
        cfg = strategies.short_straddle(tenor_days=30)
        wide = replace(cfg, vega_spread_pts=1.0)
        narrow = replace(cfg, vega_spread_pts=0.1)
        assert engine.run_backtest(hot_path, wide).stats["option_cost"] > \
            5 * engine.run_backtest(hot_path, narrow).stats["option_cost"]


# --------------------------------------------------------------------------- #
# accounting invariants
# --------------------------------------------------------------------------- #
class TestAccounting:
    def test_equity_is_cash_plus_option_pv_plus_the_spot_position(self, hot_path):
        eq = engine.run_backtest(hot_path,
                                 strategies.long_straddle(tenor_days=30)).equity
        assert np.allclose(eq["equity"],
                           eq["cash"] + eq["pv"] + eq["spot_pos"] * eq["spot"],
                           rtol=1e-12, atol=1e-6)

    def test_step_pnl_cumulates_to_equity(self, hot_path):
        eq = engine.run_backtest(hot_path,
                                 strategies.long_straddle(tenor_days=30)).equity
        assert np.allclose(eq["pnl"].cumsum(), eq["equity"], rtol=1e-10, atol=1e-6)

    @pytest.mark.parametrize("band_pct", [5.0, 10.0, 25.0])
    def test_the_recorded_delta_never_leaves_the_band(self, hot_path, band_pct):
        """``delta_total`` is recorded *after* the step's hedge, so the band is a hard
        bound on it.  A band that is not binding means the hedger is not running; one
        that is exceeded means the trade size is wrong."""
        eq = engine.run_backtest(hot_path, strategies.long_straddle(
            tenor_days=30, band_pct=band_pct)).equity
        band = band_pct / 100.0 * 2 * 10e6          # a straddle is two 10mm legs
        d = eq["delta_total"].abs().max()
        assert d <= band * (1 + 1e-9)
        assert d > 0.5 * band, "the band is not the binding constraint"

    def test_a_position_is_open_at_essentially_every_step(self, hot_path):
        eq = engine.run_backtest(hot_path,
                                 strategies.long_straddle(tenor_days=30)).equity
        assert (eq["n_legs"] > 0).mean() > 0.95

    def test_expiries_settle_to_intrinsic(self, hot_path):
        tr = engine.run_backtest(hot_path,
                                 strategies.long_straddle(tenor_days=30)).trades
        exp = tr[tr["kind"] == "expiry"]
        assert len(exp) > 0
        for _, r in exp.iterrows():
            want = max(r["cp"] * (r["spot"] - r["strike"]), 0.0) * r["notional"] * \
                r["direction"]
            assert float(r["cash"]) == pytest.approx(want, rel=1e-12)

    def test_the_run_is_bit_for_bit_reproducible(self, hot_path):
        cfg = strategies.long_straddle(tenor_days=30)
        a = engine.run_backtest(hot_path, cfg).equity["equity"].to_numpy(float)
        b = engine.run_backtest(hot_path, cfg).equity["equity"].to_numpy(float)
        assert np.array_equal(a, b)

    def test_the_same_seed_gives_the_same_path_and_a_different_one_does_not(self):
        a = engine.synthetic_path(n_days=60, seed=5).df["spot"].to_numpy(float)
        b = engine.synthetic_path(n_days=60, seed=5).df["spot"].to_numpy(float)
        c = engine.synthetic_path(n_days=60, seed=6).df["spot"].to_numpy(float)
        assert np.array_equal(a, b) and not np.array_equal(a, c)

    def test_the_path_is_badged_as_simulated(self):
        p = engine.synthetic_path(n_days=30)
        assert "SIMULATED" in p.meta["note"]
        assert p.meta["kind"] == "synthetic"


class TestSyntheticPath:
    def test_the_paths_realized_vol_is_the_one_that_was_asked_for(self):
        p = engine.synthetic_path(n_days=2000, sigma_r=0.12, sigma_i=0.08, seed=2)
        r = np.diff(np.log(p.df["spot"].to_numpy(float)))
        rv = float(np.std(r, ddof=1) * math.sqrt(252.0))
        assert rv == pytest.approx(0.12, rel=0.05)

    def test_the_implied_mark_is_the_one_that_was_asked_for(self):
        p = engine.synthetic_path(n_days=100, sigma_i=0.09)
        assert np.allclose(p.df["vol"], 0.09)

    def test_vol_of_vol_makes_the_mark_wander_without_going_negative(self):
        p = engine.synthetic_path(n_days=250, sigma_i=0.09, vol_of_vol=0.4, seed=4)
        v = p.df["vol"].to_numpy(float)
        assert v.std() > 0 and (v > 0).all()

    def test_path_from_history_keeps_only_the_close(self, provider):
        import datetime as dt
        h = provider.spot_history("EURUSD", dt.date(2025, 1, 1), dt.date(2026, 1, 1))
        p = engine.path_from_history(h, "EURUSD", implied=0.08)
        assert np.allclose(p.df["spot"].to_numpy(float),
                           h["close"].to_numpy(float)[: len(p.df)])
        assert p.meta["implied"] == "constant"

    def test_path_from_history_refuses_a_frame_without_a_datetime_index(self):
        bad = pd.DataFrame({"close": [1.0, 1.1]})
        with pytest.raises(TypeError):
            engine.path_from_history(bad, "EURUSD", implied=0.08)


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
class TestMetrics:
    def test_the_decomposition_sums_back_to_the_simulated_pnl(self, hot_path):
        """REQ-064: the split must reconstruct gross exactly, with everything the
        four terms miss named ``residual`` rather than quietly dropped."""
        r = engine.run_backtest(hot_path, strategies.long_straddle(tenor_days=30))
        d = metrics.decompose(r).set_index("component")["pnl"]
        four = d[["delta", "gamma", "theta", "vega"]].sum()
        assert four + d["residual"] == pytest.approx(d["gross"], rel=1e-9)
        assert d["net"] == pytest.approx(d["gross"] + d["costs"], rel=1e-9)
        assert d["costs"] <= 0.0

    def test_the_discretisation_line_is_gamma_plus_theta_minus_the_carry_identity(
            self, hot_path):
        r = engine.run_backtest(hot_path, strategies.long_straddle(tenor_days=30))
        d = metrics.decompose(r).set_index("component")["pnl"]
        assert d["discretisation"] == pytest.approx(
            d["gamma"] + d["theta"] - d["carry_identity"], rel=1e-9)

    def test_sharpe_annualises_on_the_stated_basis(self, hot_path):
        r = engine.run_backtest(hot_path, strategies.long_straddle(tenor_days=30))
        pnl = r.equity["pnl"]
        want = float(pnl.mean() / pnl.std(ddof=1) * math.sqrt(252.0))
        assert r.stats["sharpe_net"] == pytest.approx(want, rel=1e-9)
        assert "252" in r.stats["sharpe_basis"]

    def test_net_sharpe_is_below_gross_sharpe(self, hot_path):
        s = engine.run_backtest(hot_path,
                                strategies.long_straddle(tenor_days=30)).stats
        assert s["sharpe_net"] < s["sharpe_gross"]

    def test_drawdown_is_non_positive_and_starts_flat(self, hot_path):
        eq = engine.run_backtest(hot_path,
                                 strategies.long_straddle(tenor_days=30)).equity
        dd = metrics.drawdown(eq["equity"])
        assert (dd["drawdown"].to_numpy(float) <= 1e-12).all()
        assert dd["underwater"].iloc[0] == 0

    def test_the_stats_carry_their_provenance(self, hot_path):
        s = engine.run_backtest(hot_path,
                                strategies.long_straddle(tenor_days=30)).stats
        assert s["provenance"] == "synthetic"
        assert "252" in s["annualisation"]

    def test_small_buckets_are_flagged_as_uninterpretable(self, hot_path):
        r = engine.run_backtest(hot_path, strategies.long_straddle(tenor_days=30))
        tags = pd.Series(["big"] * (len(r.equity) - 5) + ["tiny"] * 5,
                         index=r.equity.index)
        b = metrics.bucket_stats(r, tags, min_n=20).set_index("bucket")
        assert bool(b.loc["big", "interpretable"])
        assert not bool(b.loc["tiny", "interpretable"])


# --------------------------------------------------------------------------- #
# strategies and the sweep
# --------------------------------------------------------------------------- #
class TestStrategies:
    @pytest.mark.parametrize("structure", ["straddle", "strangle", "risk_reversal"])
    def test_every_structure_runs_and_opens_the_right_legs(self, hot_path, structure):
        from dataclasses import replace
        cfg = replace(strategies.long_straddle(tenor_days=30), structure=structure)
        r = engine.run_backtest(hot_path, cfg)
        opens = r.trades[r.trades["kind"] == "open"]
        assert len(opens) > 0
        assert set(opens["cp"].unique()) == {+1, -1}
        if structure == "risk_reversal":
            first = opens.head(2)
            assert set(first["direction"].unique()) == {+1, -1}

    def test_an_unknown_structure_raises(self, hot_path):
        from dataclasses import replace
        cfg = replace(strategies.long_straddle(tenor_days=30), structure="condor")
        with pytest.raises(ValueError, match="structure"):
            engine.run_backtest(hot_path, cfg)

    def test_the_entry_rule_can_take_the_book_flat(self, hot_path):
        cfg = strategies.long_straddle(tenor_days=10, entry=lambda v: 0)
        r = engine.run_backtest(hot_path, cfg)
        assert (r.equity["n_legs"] == 0).all()
        assert float(r.equity["equity"].iloc[-1]) == pytest.approx(0.0, abs=1e-9)

    def test_the_frequency_sweep_returns_both_families_and_flags_the_argmax(self):
        path = engine.synthetic_path(n_days=90, sigma_r=0.12, sigma_i=0.08, seed=9)
        sw = strategies.hedge_frequency_sweep(path,
                                              strategies.long_straddle(tenor_days=30),
                                              bands=(5, 25), intervals=(1, 5))
        assert set(sw["rule"]) == {"band", "time"}
        assert int(sw["argmax"].sum()) == 1
        assert sw["in_sample"].all()
        assert "IN-SAMPLE" in sw["caution"].iloc[0]

    def test_a_tighter_band_in_the_sweep_hedges_more(self):
        path = engine.synthetic_path(n_days=90, sigma_r=0.12, sigma_i=0.08, seed=9)
        sw = strategies.hedge_frequency_sweep(path,
                                              strategies.long_straddle(tenor_days=30),
                                              bands=(2, 40), intervals=None)
        by = sw.set_index("param")
        assert by.loc[2.0, "n_hedges"] > by.loc[40.0, "n_hedges"]

    def test_run_grid_stacks_one_row_per_config(self, hot_path):
        df = strategies.run_grid(hot_path, [strategies.long_straddle(tenor_days=30),
                                            strategies.short_straddle(tenor_days=30)])
        assert len(df) == 2 and "total_pnl_net" in df.columns

    def test_the_config_serialises_to_something_a_user_can_copy(self):
        d = strategies.long_straddle(tenor_days=21, band_pct=10.0).as_dict()
        assert d["structure"] == "straddle" and d["tenor_days"] == 21
        assert d["hedge"]["band_pct"] == 10.0
        assert isinstance(d["entry"], str)


# --------------------------------------------------------------------------- #
# hedge-rule semantics, amendment v1.6 CR-1
# --------------------------------------------------------------------------- #
def test_the_backtest_and_the_risk_engine_read_band_pct_identically(hot_path, snapshot):
    """**FINDING (docs/05_test_report.md F-8), the half that is still open.**

    Amendment v1.6 CR-1 ruled ``HedgeRule.band_pct`` a **FRACTION** of gross option
    notional (``0.25`` = 25%), not a percent.  ``zones._band_width`` now honours that.
    ``engine.run_backtest`` still computes ``band = rule.band_pct / 100 * gross``
    (``engine.py:414``), and ``strategies.DEFAULT_BAND_PCT`` is ``15.0``.

    So the two halves of the app now disagree by 100x about what a hedge band is: a
    band the user tunes in the Lab means something else on the Risk page, and the
    presets ship at 15% in one reading and 1500% -- never hedge -- in the other.  A
    split reading is worse than either reading being wrong, because the Lab is exactly
    where a trader goes to *choose* the band they will run.

    Fix ``engine.py`` and ``strategies.DEFAULT_BAND_PCT`` together.  Owner: quant.
    """
    from fxgamma.portfolio import zones
    from fxgamma.types import Book, OptionPosition

    from tests.conftest import in_days

    gross, band_pct = 10e6, 0.25
    book = Book(options=[OptionPosition(id="s", pair="EURUSD", cp=+1, strike=1.165,
                                        expiry=in_days(30), notional_base=gross,
                                        direction=+1)], spots=[])
    risk_band = float(zones.hedge_bands(book, snapshot, "EURUSD",
                                        rule=HedgeRule(mode="band",
                                                       band_pct=band_pct)
                                        )["band_base"].iloc[0])

    # the band the engine actually enforces, read off the widest delta it tolerated
    cfg = strategies.long_straddle(tenor_days=30, band_pct=band_pct,
                                   notional_base=gross)
    eq = engine.run_backtest(hot_path, cfg).equity
    engine_band = float(eq["delta_total"].abs().max())

    # a straddle is two legs, so the engine's gross is 2x the single-leg book above
    assert engine_band == pytest.approx(2 * risk_band, rel=0.05), (
        f"the Lab enforces a {engine_band:,.0f} band where the Risk page computes "
        f"{2 * risk_band:,.0f} for the same band_pct={band_pct}")
