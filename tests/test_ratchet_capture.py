"""``fxgamma.portfolio.ratchet`` -- trend-conditional hedging, and its null controls.

``docs/12_trend_conditional_hedging.md`` is the most control-heavy work on this
project: **three** control bugs were found and fixed in flight, each of which would
have manufactured a positive, and one of them -- ``vr = n*d^2/qv`` instead of
``d^2/qv`` -- *would have declared a trend every single night*.  The PM's standing
instruction is that any positive result on this project should be assumed to be a bug
until someone has tried to kill it.  So this file is roughly half nulls.

Pinned:

* **``capture = QV + 2 * SUM(within-leg cross-terms)``, exact per path, residual 0.0.**
  The best single invariant in the module.  Tested to machine precision on every rule,
  every regime, many paths.
* **``pm_table`` reproduces the AR(1) result**: with the open leg marked the
  random-walk column is **flat at 240** at every band (the PM's own table dropped the
  open leg and read 236 at band 4.0, which made a rule that hedges less often look
  ~1% worse for purely mechanical reasons); trending is **+45%** and choppy **-38%**
  at band 4.0.
* **The ratchet does not silently degenerate into a symmetric band** -- the first of
  the three control bugs.  Unarmed, it has no fill level at all.
* **Risk-matched comparison**: comparing a ratchet at ``h`` against a symmetric band
  at the same ``h`` measures band width, not persistence.  The matched comparison is
  the only column that is evidence, and at phi=0 it must measure **zero**.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

rt = pytest.importorskip("fxgamma.portfolio.ratchet")

#: d(delta_base)/dS of the reference book, so the money numbers are on its scale.
GAMMA = 298_050_214.0
STEP_SD = 0.0007


def _pers(*, vr: float, z: float, n_obs: int = 400, block: int = 20):
    """A hand-built :class:`Persistence`.  ``is_trending`` / ``is_choppy`` read ``z``,
    so significance is expressed by |z| > 2 rather than by a flag."""
    return rt.Persistence(vr=vr, kappa=1.0 / vr, phi1=rt.vr_to_phi(vr, block),
                          phi_implied=rt.vr_to_phi(vr, block), er=0.2, n_obs=n_obs,
                          block=block, se_vr=abs(vr - 1.0) / max(abs(z), 1e-9),
                          z=z, se_phi1=0.05, verdict="fixture")


@pytest.fixture(scope="module")
def rw_paths():
    """1,200 driftless random-walk price paths, one overnight session of hourly bars
    scaled up to 168 steps so the band sweeps have something to bite on."""
    return rt.ar1_price_paths(0.0, 1200, 168, seed=4242)


@pytest.fixture(scope="module")
def regimes():
    return {
        "trending": rt.ar1_price_paths(+0.3, 600, 168, seed=11),
        "random_walk": rt.ar1_price_paths(0.0, 600, 168, seed=12),
        "choppy": rt.ar1_price_paths(-0.3, 600, 168, seed=13),
    }


ALL_RULES = [
    ("symmetric_tight", lambda: rt.SymmetricBand(0.5)),
    ("symmetric_wide", lambda: rt.SymmetricBand(2.0)),
    ("asymmetric", lambda: rt.AsymmetricBand(1.0, 3.0)),
    ("ratchet", lambda: rt.Ratchet(h=2.0, give_back=0.5)),
    ("ratchet_tight_trail", lambda: rt.Ratchet(h=1.0, give_back=0.25)),
    ("time_and_state", lambda: rt.TimeAndState(h0=2.0, max_steps=40)),
]


# =========================================================================== #
# 1.  THE identity: capture = QV + 2 * cross, residual exactly 0
# =========================================================================== #
class TestCaptureDecomposition:
    """Quadratic variation is untouchable by any hedging rule.  Therefore every penny
    a trend rule can earn is the return autocovariance inside its own legs -- which is
    the variance ratio, which is the ``kappa`` that docs/10 already ruled
    unforecastable from daily bars.  Two negative results, one mechanism, and this
    identity is what makes them the same result."""

    @pytest.mark.parametrize("phi", [-0.3, -0.1, 0.0, 0.1, 0.3])
    @pytest.mark.parametrize("name,factory", ALL_RULES, ids=[n for n, _ in ALL_RULES])
    def test_residual_is_zero_to_machine_precision(self, phi, name, factory):
        x = rt.ar1_paths(phi, 6, 240, seed=hash((phi, name)) % 9999)
        for j in range(x.shape[0]):
            d = rt.capture_decomposition(x[j], factory())
            assert abs(d["residual"]) <= 1e-9 * max(abs(d["capture"]), 1.0), (
                f"{name} phi={phi}: residual {d['residual']:.3e}")

    def test_qv_is_the_same_for_every_rule_on_a_given_path(self):
        """QV is a property of the path, not of the rule.  If it moves with the rule,
        the decomposition is measuring the rule twice."""
        p = rt.ar1_paths(0.15, 1, 240, seed=5)[0]
        qvs = {rt.capture_decomposition(p, f())["qv"] for _, f in ALL_RULES}
        assert len(qvs) == 1

    def test_qv_is_the_sum_of_squared_increments(self):
        p = rt.ar1_paths(0.0, 1, 240, seed=6)[0]
        d = rt.capture_decomposition(p, rt.SymmetricBand(1.5))
        assert d["qv"] == pytest.approx(float(np.sum(np.diff(p) ** 2)), rel=1e-12)

    def test_continuous_hedging_has_no_cross_terms(self):
        """A band so tight that every bar fills leaves no within-leg pairs, so the
        capture IS the quadratic variation: ``ratio = 1`` exactly."""
        p = rt.ar1_paths(0.0, 1, 120, seed=7)[0]
        d = rt.capture_decomposition(p, rt.SymmetricBand(1e-9))
        assert d["cross"] == pytest.approx(0.0, abs=1e-12)
        assert d["ratio"] == pytest.approx(1.0, rel=1e-9)

    def test_never_hedging_gives_the_squared_net_move(self):
        p = rt.ar1_paths(0.0, 1, 120, seed=8)[0]
        d = rt.capture_decomposition(p, rt.SymmetricBand(1e12))
        assert d["n_hedges"] == 0
        assert d["capture"] == pytest.approx((p[-1] - p[0]) ** 2, rel=1e-12)

    @pytest.mark.parametrize("phi,direction", [(0.3, "above"), (-0.3, "below")])
    def test_the_ratio_is_the_variance_ratio_of_the_path(self, phi, direction):
        """``ratio = capture/qv`` at a wide band IS the variance ratio, so a trending
        path must read above 1 and a choppy path below it."""
        x = rt.ar1_paths(phi, 300, 240, seed=9)
        rs = [rt.capture_decomposition(x[j], rt.SymmetricBand(6.0))["ratio"]
              for j in range(x.shape[0])]
        m = float(np.mean(rs))
        assert (m > 1.05) if direction == "above" else (m < 0.95)

    def test_random_walk_ratio_is_one(self):
        x = rt.ar1_paths(0.0, 400, 240, seed=10)
        rs = [rt.capture_decomposition(x[j], rt.SymmetricBand(4.0))["ratio"]
              for j in range(x.shape[0])]
        assert float(np.mean(rs)) == pytest.approx(1.0, abs=0.08)

    def test_capture_ceiling_prize_is_zero_at_vr_one(self):
        c = rt.capture_ceiling(GAMMA, 1.165, 0.0031, 1.0)
        assert c["prize"] == pytest.approx(0.0, abs=1e-9)
        assert c["ceiling_pnl"] == pytest.approx(c["continuous_pnl"], rel=1e-12)

    @pytest.mark.parametrize("vr", [0.5, 1.5, 2.0])
    def test_capture_ceiling_prize_is_linear_in_vr_minus_one(self, vr):
        c = rt.capture_ceiling(GAMMA, 1.165, 0.0031, vr)
        assert c["prize"] == pytest.approx(c["continuous_pnl"] * (vr - 1.0), rel=1e-12)


# =========================================================================== #
# 2.  pm_table reproduces the AR(1) result -- with the open leg marked
# =========================================================================== #
@pytest.fixture(scope="module")
def table_marked():
    return rt.pm_table(n_paths=1500, n_steps=240, seed=5, include_open_leg=True)


@pytest.fixture(scope="module")
def table_dropped():
    return rt.pm_table(n_paths=1500, n_steps=240, seed=5, include_open_leg=False)


class TestPMTable:
    """The PM's own simulation dropped the leg still open at the end of the path,
    which biases against wide bands.  Marking it makes the random-walk column **flat
    at 240 at every band**, as theory requires."""

    def test_random_walk_column_is_flat_at_240_when_the_open_leg_is_marked(
            self, table_marked):
        col = table_marked["phi=+0.00"]
        se = table_marked["se(phi=+0.00)"]
        for band in col.index:
            assert col[band] == pytest.approx(240.0, abs=4.0 * se[band] + 1.0), band

    def test_random_walk_column_is_flat_ACROSS_bands(self, table_marked):
        col = table_marked["phi=+0.00"]
        assert (col.max() - col.min()) < 2.0

    def test_dropping_the_open_leg_biases_the_wide_band_down(self, table_dropped):
        """The mechanical ~1.6% artefact: 236 rather than 240 at band 4.0."""
        col = table_dropped["phi=+0.00"]
        assert col.loc[4.0] < col.loc[0.25]
        assert 233.0 < col.loc[4.0] < 239.0

    def test_the_artefact_is_small_next_to_the_real_effects(self, table_marked,
                                                            table_dropped):
        artefact = abs(table_marked["phi=+0.00"].loc[4.0]
                       - table_dropped["phi=+0.00"].loc[4.0]) / 240.0
        real = abs(table_marked["phi=+0.30"].loc[4.0] / 240.0 - 1.0)
        assert artefact < 0.03 and real > 0.30

    def test_trending_gains_about_45pct_at_band_4(self, table_marked):
        assert table_marked["phi=+0.30"].loc[4.0] / 240.0 - 1.0 == \
            pytest.approx(0.45, abs=0.06)

    def test_choppy_loses_about_38pct_at_band_4(self, table_marked):
        assert table_marked["phi=-0.30"].loc[4.0] / 240.0 - 1.0 == \
            pytest.approx(-0.38, abs=0.06)

    def test_trending_rises_with_the_band(self, table_marked):
        assert table_marked["phi=+0.30"].is_monotonic_increasing

    def test_choppy_falls_with_the_band(self, table_marked):
        assert table_marked["phi=-0.30"].is_monotonic_decreasing

    def test_the_three_columns_coincide_at_a_vanishing_band(self, table_marked):
        row = table_marked.loc[0.25]
        vals = [row["phi=+0.30"], row["phi=+0.00"], row["phi=-0.30"]]
        assert max(vals) - min(vals) < 6.0, \
            "at a band below one step's sd every rule captures the QV"

    def test_per_step_variance_is_equalised_across_phi(self):
        """The controlled experiment: same variance, different path shape.  If the
        variance is not equalised, the whole table is measuring vol, not persistence."""
        for phi in (-0.3, 0.0, 0.3):
            x = rt.ar1_paths(phi, 3000, 240, seed=21)
            qv = np.sum(np.diff(x, axis=1) ** 2, axis=1)
            assert float(qv.mean()) == pytest.approx(240.0, rel=0.03), phi

    def test_ar1_paths_start_from_the_stationary_distribution(self):
        """No burn-in artefact: the first increment has the same variance as the rest."""
        x = rt.ar1_paths(0.5, 4000, 60, seed=22)
        r = np.diff(x, axis=1)
        assert float(r[:, 0].std()) == pytest.approx(float(r[:, -1].std()), rel=0.06)

    def test_table_records_its_own_convention(self, table_marked, table_dropped):
        assert "included" in table_marked.attrs["note"]
        assert "dropped" in table_dropped.attrs["note"]


# =========================================================================== #
# 3.  NULL CONTROLS -- Priority 2.  Zero cost, zero edge, driftless = zero.
# =========================================================================== #
class TestNullControls:
    """Six manufactured effects have been caught on this project.  Every one of them
    was found by a control, not by inspection."""

    @pytest.mark.parametrize("h", [0.0004, 0.0008, 0.0016, 0.0032, 0.0064])
    def test_zero_cost_net_is_band_independent_on_a_random_walk(self, rw_paths, h):
        """THE null: with no cost and no drift, the expected P&L cannot depend on the
        band.  Any rule that appears to beat another here is measuring an artefact."""
        base = rt.score_paths(rw_paths, lambda: rt.SymmetricBand(0.0016),
                              gamma=GAMMA, cost_bp=0.0)
        got = rt.score_paths(rw_paths, lambda h=h: rt.SymmetricBand(h),
                             gamma=GAMMA, cost_bp=0.0)
        se = math.hypot(base["net_se"], got["net_se"])
        assert abs(got["net"] - base["net"]) < 3.5 * se, (
            f"band {h} moved the zero-cost mean by "
            f"{(got['net'] - base['net']) / se:.1f} standard errors")

    @pytest.mark.parametrize("h", [0.0004, 0.0016, 0.0064])
    def test_zero_cost_charges_exactly_zero_cost(self, rw_paths, h):
        s = rt.score_paths(rw_paths, lambda h=h: rt.SymmetricBand(h), gamma=GAMMA,
                           cost_bp=0.0)
        assert s["cost"] == 0.0
        assert s["net"] == pytest.approx(s["capture_pnl"], rel=1e-12)

    @pytest.mark.parametrize("cap", [0.2e6, 0.5e6, 1.0e6, 3.0e6])
    def test_zero_cost_cap_has_no_effect_on_the_mean(self, rw_paths, cap):
        """The docs/13 s3.2 shape, as a control: a cap that moves the zero-cost mean
        is a phantom.  The one that shipped read t = -51."""
        base = rt.score_paths(rw_paths, lambda: rt.SymmetricBand(0.0016), gamma=GAMMA,
                              cost_bp=0.0)
        got = rt.score_paths(rw_paths, lambda: rt.SymmetricBand(0.0016), gamma=GAMMA,
                             cost_bp=0.0, cap_delta_base=cap)
        se = math.hypot(base["net_se"], got["net_se"])
        assert abs(got["net"] - base["net"]) < 3.5 * se

    def test_a_cap_wider_than_the_band_never_binds(self, rw_paths):
        s = rt.score_paths(rw_paths, lambda: rt.SymmetricBand(0.0016), gamma=GAMMA,
                           cap_delta_base=10e6)
        assert s["cap_hit_rate"] == 0.0

    def test_a_cap_tighter_than_the_band_always_binds(self, rw_paths):
        s = rt.score_paths(rw_paths, lambda: rt.SymmetricBand(0.02), gamma=GAMMA,
                           cap_delta_base=0.2e6)
        assert s["cap_hit_rate"] > 0.9

    def test_matched_comparison_measures_zero_at_phi_zero(self, rw_paths):
        """The control that overturned a +27/night 'result' on a pure random walk: a
        trailing rule hedges less often, so it is a **wider band in disguise**.  Match
        the risk and the edge disappears -- as it must, since there is none."""
        rules = {"symmetric": lambda: rt.SymmetricBand(0.0016),
                 "ratchet": lambda: rt.Ratchet(0.0016, 0.5),
                 "time_and_state": lambda: rt.TimeAndState(0.0016, max_steps=40)}
        m = rt.matched_comparison(rw_paths, rules, gamma=GAMMA, cost_bp=0.0,
                                  lo=1e-5, hi=0.05)
        for name, row in m.iterrows():
            if np.isfinite(row["t"]):
                assert abs(row["t"]) < 3.0, (
                    f"{name} reads t={row['t']:.2f} against its own risk-matched "
                    "symmetric band on a driftless random walk")

    def test_matching_actually_matches_the_risk_statistic(self, rw_paths):
        rules = {"ratchet": lambda: rt.Ratchet(0.0016, 0.5)}
        m = rt.matched_comparison(rw_paths, rules, gamma=GAMMA, cost_bp=0.0,
                                  lo=1e-5, hi=0.05)
        row = m.loc["ratchet"]
        if np.isfinite(row["h_matched"]):
            assert row["match_achieved"] == pytest.approx(row["match_target"], rel=0.02)

    def test_the_unmatched_comparison_is_the_one_that_lies(self, rw_paths):
        """Documented explicitly, and worth pinning: at retail cost an UNMATCHED
        ratchet beats a same-``h`` symmetric band on a random walk purely because it
        trades less.  The matched column above shows the same rule measures zero."""
        rules = {"symmetric": lambda: rt.SymmetricBand(0.0016),
                 "ratchet": lambda: rt.Ratchet(0.0016, 0.5)}
        c = rt.compare_rules(rw_paths, rules, baseline="symmetric", gamma=GAMMA,
                             cost_bp=5.0)
        assert c.loc["ratchet", "n_hedges"] < c.loc["symmetric", "n_hedges"]
        assert c.loc["ratchet", "cost"] < c.loc["symmetric", "cost"]

    def test_a_rule_that_wins_on_net_while_carrying_more_delta_has_not_won(
            self, rw_paths):
        """docs/12: the ratchet binds the cap on 32-46% of nights against 9-18%, so it
        is worse P&L *and* worse risk.  The delta statistics must be reported next to
        the money or the comparison is not a comparison."""
        rules = {"symmetric": lambda: rt.SymmetricBand(0.0016),
                 "ratchet": lambda: rt.Ratchet(0.0016, 0.5)}
        c = rt.compare_rules(rw_paths, rules, baseline="symmetric", gamma=GAMMA,
                             cost_bp=5.0)
        for col in ("mean_abs_delta", "p95_abs_delta", "end_abs_delta",
                    "cap_hit_rate"):
            assert col in c.columns
        assert c.loc["ratchet", "p95_abs_delta"] > c.loc["symmetric", "p95_abs_delta"]

    def test_cost_is_exactly_linear_in_the_spread(self, rw_paths):
        a = rt.score_paths(rw_paths, lambda: rt.SymmetricBand(0.0016), gamma=GAMMA,
                           cost_bp=1.0)
        b = rt.score_paths(rw_paths, lambda: rt.SymmetricBand(0.0016), gamma=GAMMA,
                           cost_bp=5.0)
        assert b["cost"] == pytest.approx(5.0 * a["cost"], rel=1e-9)

    def test_giveback_zero_is_the_extreme_hedger_bound(self, regimes):
        g = rt.giveback_sensitivity({"random_walk": regimes["random_walk"]},
                                    h=0.0016, gamma=GAMMA, cost_bp=0.0)
        assert set(g.index.get_level_values("give_back")) >= {0.0, 0.5, 2.0}
        n = g.loc["random_walk"]["n_hedges"]
        assert n.loc[0.0] > n.loc[2.0], "a bigger give-back must hedge less often"

    def test_regime_matrix_covers_every_cell(self, regimes):
        rules = {"symmetric": lambda: rt.SymmetricBand(0.0016),
                 "ratchet": lambda: rt.Ratchet(0.0016, 0.5)}
        m = rt.regime_matrix(regimes, rules, baseline="symmetric", gamma=GAMMA,
                             cost_bp=5.0)
        assert len(m) == len(regimes) * len(rules)
        assert (m.xs("symmetric", level="rule")["vs_base"].abs() < 1e-9).all()

    def test_calling_the_regime_wrong_costs_money(self, regimes):
        """The cost of being wrong: -25 USD/night at phi=-0.10 in docs/12.  The sign
        is what matters -- a trend rule must LOSE on a choppy sample."""
        rules = {"symmetric": lambda: rt.SymmetricBand(0.0016),
                 "ratchet": lambda: rt.Ratchet(0.0016, 0.5)}
        m = rt.regime_matrix(regimes, rules, baseline="symmetric", gamma=GAMMA,
                             cost_bp=5.0)
        trend = m.loc[("trending", "ratchet"), "vs_base"]
        chop = m.loc[("choppy", "ratchet"), "vs_base"]
        assert trend > chop


# =========================================================================== #
# 4.  Look-ahead, fills, and the close-observed-bar hazard
# =========================================================================== #
class TestFillLogic:
    """Where look-ahead lives.  The sixth manufactured effect was **filling at the
    resting level on close-observed bars**: a clean phantom cost of -430/-990
    USD/night that *scaled with the cap*, exactly the shape of a real result."""

    def test_a_trigger_outside_the_path_never_fills(self):
        p = rt.ar1_paths(0.0, 1, 240, seed=31)[0]
        rec = rt.run_rule(p, rt.SymmetricBand(1e6))
        assert rec.n_hedges == 0

    def test_state_only_advances_on_a_bar_that_did_not_fill(self):
        """After a fill the rule restarts from the fill price and the rest of that bar
        is deliberately not replayed into it -- otherwise a bar is used twice."""
        r = rt.Ratchet(h=1.0, give_back=0.5)
        r.reset(0.0)
        r.observe(2.0, -0.1, 1.9)
        assert r.armed == +1 and r.extreme == 2.0
        r.on_fill(1.5)
        assert r.armed == 0 and r.ref == 1.5 and r.extreme == 1.5

    def test_close_fill_charges_the_overshoot(self):
        """``fill='close'`` is the conservative convention: you see the price and deal
        on it, so the rule is charged for the overshoot past its own trigger."""
        p = np.array([0.0, 0.0, 3.0, 3.0])
        rec = rt.run_rule(p, rt.SymmetricBand(1.0), fill="close")
        assert rec.hedge_price[0] == 3.0

    def test_touch_fill_needs_real_ranges(self):
        p = rt.ar1_paths(0.0, 1, 50, seed=32)[0]
        with pytest.raises(ValueError):
            rt.run_rule(p, rt.SymmetricBand(1.0), fill="touch")

    def test_touch_fill_fills_at_the_level(self):
        p = np.array([0.0, 0.0, 3.0])
        hi = np.array([0.0, 0.5, 3.0])
        lo = np.array([0.0, -0.5, 0.0])
        rec = rt.run_rule(p, rt.SymmetricBand(1.0), fill="touch", high=hi, low=lo)
        assert rec.hedge_price[0] == pytest.approx(1.0)

    def test_an_ambiguous_bar_is_counted_not_guessed_silently(self):
        """When a bar's range spans both triggers the intra-bar order is unknowable;
        pretending otherwise is how a backtest invents fills."""
        p = np.array([0.0, 0.1])
        hi = np.array([0.0, 5.0])
        lo = np.array([0.0, -5.0])
        rec = rt.run_rule(p, rt.SymmetricBand(1.0), fill="touch", high=hi, low=lo)
        assert rec.ambiguous_bars == 1

    def test_run_rule_rejects_a_degenerate_path(self):
        with pytest.raises(ValueError):
            rt.run_rule([1.0], rt.SymmetricBand(1.0))
        with pytest.raises(ValueError):
            rt.run_rule(np.zeros((2, 2)), rt.SymmetricBand(1.0))

    @pytest.mark.regression
    def test_touch_fill_on_close_only_bars_is_guarded_or_unbiased(self, rw_paths):
        """FINDING (QA-7).  ``fill='touch'`` validates that ``high``/``low`` are
        *present*, not that they are real ranges.  A close-only source -- which is
        what this repo's own daily and 1h adapters hand you when the range columns are
        the close -- therefore fills every order at its exact resting level while only
        the close was ever observed.

        Measured on 1,200 driftless zero-cost paths, against the honest close-fill:
        **-9,868 / -6,250 / -3,900 USD a night at h = 8 / 16 / 32 pips** on the
        reference book's gamma.  It is large, it is systematic, and it **scales with
        how tight the ladder is** -- the precise signature docs/13 s3.2 records for the
        sixth manufactured effect.  A zero-cost null control must read zero and this
        reads 37-59% of the night.

        The test passes if the call is refused (a guard was added) and fails while the
        biased number is returned silently."""
        try:
            got = rt.score_paths(rw_paths, lambda: rt.SymmetricBand(0.0008),
                                 gamma=GAMMA, cost_bp=0.0, fill="touch",
                                 highs=rw_paths, lows=rw_paths)
        except (ValueError, TypeError):
            return          # guarded: degenerate bars are refused, which is correct
        base = rt.score_paths(rw_paths, lambda: rt.SymmetricBand(0.0008),
                              gamma=GAMMA, cost_bp=0.0, fill="close")
        se = math.hypot(base["net_se"], got["net_se"])
        assert abs(got["net"] - base["net"]) < 4.0 * se, (
            f"close-only bars under fill='touch' shift the zero-cost mean by "
            f"{got['net'] - base['net']:,.0f} ({(got['net'] - base['net']) / se:.0f} se)")


# =========================================================================== #
# 5.  The ratchet must not degenerate into a symmetric band
# =========================================================================== #
class TestRatchetDoesNotDegenerate:
    """The first of the three control bugs: returning the band from ``triggers()``
    while unarmed fills on the arming bar and the ratchet silently becomes a symmetric
    band.  It did, and the give-back sweep read flat until it was found."""

    @pytest.mark.regression
    def test_unarmed_ratchet_has_no_fill_level(self):
        r = rt.Ratchet(h=2.0)
        r.reset(0.0)
        assert r.triggers() == (-math.inf, math.inf)

    @pytest.mark.regression
    def test_reaching_the_band_arms_rather_than_fills(self):
        p = np.array([0.0, 2.5, 2.6])
        rec = rt.run_rule(p, rt.Ratchet(h=2.0, give_back=0.5))
        assert rec.n_hedges == 0, "the arming bar must not fill"

    @pytest.mark.regression
    def test_the_ratchet_hedges_strictly_less_than_its_band(self, rw_paths):
        sym = rt.score_paths(rw_paths, lambda: rt.SymmetricBand(0.0016), gamma=GAMMA)
        rat = rt.score_paths(rw_paths, lambda: rt.Ratchet(0.0016, 0.5), gamma=GAMMA)
        assert rat["n_hedges"] < 0.85 * sym["n_hedges"]

    @pytest.mark.regression
    def test_a_huge_give_back_almost_never_fills(self, rw_paths):
        """If the give-back sweep reads flat, the rule has degenerated."""
        s = rt.score_paths(rw_paths, lambda: rt.Ratchet(0.0016, give_back=9.0),
                           gamma=GAMMA)
        assert s["n_hedges"] < 4.0

    def test_the_give_back_sweep_is_not_flat(self, regimes):
        g = rt.giveback_sensitivity({"trending": regimes["trending"]}, h=0.0016,
                                    gamma=GAMMA, cost_bp=5.0)
        n = g.loc["trending"]["n_hedges"]
        assert n.max() / max(n.min(), 1e-9) > 2.0

    def test_the_trail_follows_the_extreme(self):
        r = rt.Ratchet(h=1.0, give_back=0.5)
        r.reset(0.0)
        r.observe(1.5, 0.0, 1.4)
        assert r.armed == +1
        lo, hi = r.triggers()
        assert lo == pytest.approx(1.0) and hi == math.inf
        r.observe(3.0, 1.0, 2.9)
        lo2, _ = r.triggers()
        assert lo2 == pytest.approx(2.5)

    def test_the_opposite_side_keeps_its_ordinary_band(self):
        """A move that reverses all the way through the start is still hedged."""
        r = rt.Ratchet(h=1.0, give_back=5.0)
        r.reset(0.0)
        r.observe(1.2, 0.0, 1.1)
        lo, _ = r.triggers()
        assert lo == pytest.approx(-1.0)

    def test_the_ratchet_declares_it_is_not_a_resting_order(self):
        """It requires a server-side trailing stop or someone awake -- which is the
        thing the user is trying to avoid.  That has to be on the object."""
        assert "NOT leaveable" in rt.Ratchet(h=1.0).orderability

    def test_the_symmetric_band_declares_it_IS_leaveable(self):
        assert "leaveable" in rt.SymmetricBand(1.0).orderability
        assert "NOT leaveable" not in rt.SymmetricBand(1.0).orderability

    def test_time_and_state_widens_after_a_same_sign_leg(self):
        r = rt.TimeAndState(h0=1.0, widen=1.3, tighten=0.8)
        r.reset(0.0)
        r.on_fill(1.0)
        r.on_fill(2.0)
        assert r.h == pytest.approx(1.3, rel=1e-9)

    def test_time_and_state_tightens_after_a_reversal(self):
        r = rt.TimeAndState(h0=1.0, widen=1.3, tighten=0.8)
        r.reset(0.0)
        r.on_fill(1.0)
        r.on_fill(0.0)
        assert r.h == pytest.approx(0.8, rel=1e-9)

    def test_time_and_state_clips_into_its_bounds(self):
        r = rt.TimeAndState(h0=1.0, widen=3.0, h_max=2.0)
        r.reset(0.0)
        for k in range(6):
            r.on_fill(float(k + 1))
        assert r.h <= 2.0

    def test_the_time_leg_fires_unconditionally(self):
        """Without it an adaptive band can widen its way into the morning, which is
        the delta cap's job to prevent, not the band's."""
        p = np.array([0.0, 0.01, 0.02, 0.03, 0.04])
        rec = rt.run_rule(p, rt.TimeAndState(h0=1e6, max_steps=2))
        assert rec.n_hedges >= 1

    def test_make_rule_covers_every_shipped_rule(self):
        for name in rt.RULES:
            kw = {"h": 1.0} if name in ("symmetric", "ratchet") else (
                {"h_up": 1.0, "h_dn": 1.0} if name == "asymmetric" else {"h0": 1.0})
            assert isinstance(rt.make_rule(name, **kw), rt.TriggerRule)

    def test_make_rule_rejects_an_unknown_name(self):
        with pytest.raises(KeyError):
            rt.make_rule("wishful")


# =========================================================================== #
# 6.  Persistence: detectable, not forecastable
# =========================================================================== #
class TestPersistenceIsDetectableNotForecastable:
    """docs/12's verdict.  ``vr = n*d^2/qv`` instead of ``d^2/qv`` would have declared
    a trend **every single night**, so the variance-ratio arithmetic is pinned first."""

    @pytest.mark.regression
    def test_variance_ratio_of_a_random_walk_is_one_not_n(self):
        """The control bug, as an assertion.  A VR that scales with the block length
        declares a trend every night."""
        for n in (5, 20, 60):
            assert rt.ar1_vr(0.0, n) == pytest.approx(1.0, rel=1e-9)
            assert rt.phi_to_vr(0.0, n) == pytest.approx(1.0, rel=1e-9)

    @pytest.mark.parametrize("n", [5, 14, 20, 60])
    def test_vr_is_bounded_and_does_not_scale_with_n(self, n):
        assert 1.0 < rt.phi_to_vr(0.3, n) < 2.5
        assert 0.0 < rt.phi_to_vr(-0.3, n) < 1.0

    @pytest.mark.parametrize("phi", [-0.4, -0.2, 0.0, 0.2, 0.4])
    @pytest.mark.parametrize("n", [10, 20, 60])
    def test_vr_to_phi_inverts_phi_to_vr(self, phi, n):
        assert rt.vr_to_phi(rt.phi_to_vr(phi, n), n) == pytest.approx(phi, abs=1e-6)

    def test_vr_is_monotone_in_phi(self):
        vs = [rt.phi_to_vr(p, 20) for p in (-0.4, -0.2, 0.0, 0.2, 0.4)]
        assert all(a < b for a, b in zip(vs, vs[1:]))

    def test_kappa_and_vr_are_reciprocal_views(self):
        """``kappa = QV/D^2`` and ``vr = D^2/QV``.  docs/10 and docs/12 arrived at the
        same statistic independently, which is the strongest form the finding takes."""
        for kappa in (0.5, 1.0, 2.0):
            phi = rt.kappa_to_phi(kappa, 20)
            assert rt.phi_to_vr(phi, 20) == pytest.approx(1.0 / kappa, rel=1e-6)

    def test_estimated_persistence_recovers_the_truth_in_the_POOLED_limit(self):
        """'Persistence is a measurable property of the pair, not of tonight.'"""
        for phi in (-0.3, 0.0, 0.3):
            x = rt.ar1_paths(phi, 1, 20000, seed=41)[0]
            p = rt.estimate_persistence(np.exp(0.0007 * x) * 1.166, block=20)
            assert p.phi1 == pytest.approx(phi, abs=0.06), phi
            assert p.vr == pytest.approx(rt.ar1_vr(phi, 20), rel=0.10), phi
            assert p.kappa == pytest.approx(1.0 / p.vr, rel=1e-9)

    def test_estimated_persistence_carries_its_own_noise(self):
        x = rt.ar1_paths(0.0, 1, 4000, seed=43)[0]
        p = rt.estimate_persistence(np.exp(0.0007 * x) * 1.166, block=20)
        assert p.se_vr > 0 and np.isfinite(p.z)
        assert p.verdict

    def test_one_night_is_not_a_regime_call(self):
        """sd 1.25 around a mean of 1.0 on a single 14-hour session: one-night regime
        calls are 55% accurate.  The estimator must report that it cannot tell."""
        x = rt.ar1_paths(0.1, 400, 14, seed=42)
        flags = [rt.estimate_persistence(np.exp(0.0007 * x[j]) * 1.166,
                                         block=7).is_trending
                 for j in range(x.shape[0])]
        assert np.mean(flags) < 0.35, "a 14-bar session must not confidently trend"

    def test_significance_is_required_before_the_band_moves(self):
        """The default ``require_significance=True`` is the guard that stops a noisy
        one-night estimate from moving the orders."""
        weak = _pers(vr=1.4, z=0.3)
        m, note = rt.band_multiplier(weak)
        assert m == pytest.approx(1.0, rel=1e-9)
        assert note

    def test_a_significant_trend_widens_and_chop_tightens(self):
        up = _pers(vr=1.4, z=6.0)
        dn = _pers(vr=0.7, z=-6.0)
        assert rt.band_multiplier(up)[0] > 1.0 > rt.band_multiplier(dn)[0]

    def test_the_multiplier_is_clamped(self):
        wild = _pers(vr=9.0, z=90.0)
        m, _ = rt.band_multiplier(wild)
        assert m <= 2.0, "the x6.97 raw answer must not reach the orders"

    def test_trend_conditional_band_is_the_base_band_when_insignificant(self):
        weak = _pers(vr=1.4, z=0.3)
        h, note = rt.trend_conditional_band(0.0016, weak)
        assert float(h) == pytest.approx(0.0016, rel=1e-9)
        assert "noise" in note

    def test_trend_conditional_band_is_capped_by_the_delta_cap(self):
        strong = _pers(vr=2.5, z=9.0)
        h, note = rt.trend_conditional_band(0.0016, strong, cap_spot=0.0018)
        assert h == pytest.approx(0.0018, rel=1e-12)
        assert "CAPPED" in note

    def test_only_book_skew_asymmetry_ships_on(self):
        """The RULING: 'Only book-skew asymmetry ships on.'  With no skew tilt and no
        significant persistence the half-widths must be symmetric."""
        up, dn, note = rt.asymmetric_half_widths(0.0016)
        assert up == pytest.approx(dn, rel=1e-12)

    def test_book_skew_tilts_the_two_sides(self):
        up, dn, note = rt.asymmetric_half_widths(0.0016, skew_tilt=0.3)
        assert up != pytest.approx(dn, rel=1e-6)
        assert note

    def test_persistence_tilt_is_ignored_without_significance(self):
        weak = _pers(vr=1.6, z=0.4)
        a = rt.asymmetric_half_widths(0.0016, persistence=weak, last_move_sign=+1)
        b = rt.asymmetric_half_widths(0.0016)
        assert a[0] == pytest.approx(b[0], rel=1e-9)
        assert a[1] == pytest.approx(b[1], rel=1e-9)

    def test_the_tilt_is_capped(self):
        strong = _pers(vr=9.0, z=90.0)
        up, dn, _ = rt.asymmetric_half_widths(0.0016, persistence=strong,
                                              last_move_sign=+1, max_tilt=0.5)
        # max_tilt=0.5 means the widest legal split is 1.5 / 0.5 = 3.0x
        assert 0.33 <= up / dn <= 3.0 + 1e-9

    def test_lo_mackinlay_se_falls_with_sample_size(self):
        a = rt._lo_mackinlay_se(100, 5)
        b = rt._lo_mackinlay_se(10_000, 5)
        assert b < a


# =========================================================================== #
# 7.  delta_profile and the bridge to the shipped backtester
# =========================================================================== #
class TestDeltaProfileAndBridge:
    def test_delta_profile_quantiles_are_monotone(self, rw_paths):
        d = rt.delta_profile(rw_paths, lambda: rt.SymmetricBand(0.0016), gamma=GAMMA)
        assert d["abs_delta_any_time"].is_monotonic_increasing
        assert d["abs_delta_at_open"].is_monotonic_increasing

    def test_a_cap_truncates_the_delta_distribution(self, rw_paths):
        free = rt.delta_profile(rw_paths, lambda: rt.SymmetricBand(0.004),
                                gamma=GAMMA)
        capped = rt.delta_profile(rw_paths, lambda: rt.SymmetricBand(0.004),
                                  gamma=GAMMA, cap_spot=0.0008)
        assert capped["abs_delta_any_time"].iloc[-1] < \
            free["abs_delta_any_time"].iloc[-1]

    def test_a_wider_band_carries_more_delta(self, rw_paths):
        a = rt.score_paths(rw_paths, lambda: rt.SymmetricBand(0.0008), gamma=GAMMA)
        b = rt.score_paths(rw_paths, lambda: rt.SymmetricBand(0.0032), gamma=GAMMA)
        assert b["mean_abs_delta"] > a["mean_abs_delta"]
        assert b["p95_abs_delta"] > a["p95_abs_delta"]

    def test_score_paths_rejects_a_1d_input(self):
        with pytest.raises(ValueError):
            rt.score_paths(np.zeros(10), lambda: rt.SymmetricBand(1.0), gamma=1.0)

    def test_compare_rules_rejects_a_missing_baseline(self, rw_paths):
        with pytest.raises(KeyError):
            rt.compare_rules(rw_paths, {"a": lambda: rt.SymmetricBand(0.0016)},
                             baseline="nope", gamma=GAMMA)

    def test_rule_factories_are_not_shared_between_paths(self, rw_paths):
        """A shared rule object carries state across paths and silently correlates
        them.  Running twice must give the identical answer."""
        a = rt.score_paths(rw_paths[:50], lambda: rt.Ratchet(0.0016, 0.5), gamma=GAMMA)
        b = rt.score_paths(rw_paths[:50], lambda: rt.Ratchet(0.0016, 0.5), gamma=GAMMA)
        assert a["net"] == pytest.approx(b["net"], rel=1e-12)

    @pytest.mark.slow
    def test_verify_against_engine(self):
        """``backtest_with_rule(rule_factory=None)`` must reproduce the shipped
        engine bit-for-bit, or the cost accounting here is a second opinion rather
        than the same one."""
        eng = pytest.importorskip("fxgamma.backtest.engine")
        from fxgamma.types import HedgeRule
        path = eng.synthetic_path(n_days=10, sigma_r=0.08, sigma_i=0.08, S0=1.165,
                                  pair="EURUSD", steps_per_day=12, seed=3,
                                  rd=0.04, rf=0.02)
        cfg = eng.BacktestConfig(pair="EURUSD", structure="straddle", direction=1,
                                 notional_base=10e6, tenor_days=30, roll_days=None,
                                 hedge=HedgeRule(mode="band", band_delta=0.5e6,
                                                 cost_bp=5.0), cost_bp=5.0)
        out = rt.verify_against_engine(path, cfg)
        assert isinstance(out, dict)
        for k, v in out.items():
            if isinstance(v, float) and "diff" in k:
                assert abs(v) < 1e-6, f"{k} = {v}"
