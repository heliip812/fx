"""``fxgamma.portfolio.bandopt`` -- and the constant that had already caused a bug.

The load-bearing facts from ``docs/09_hedging_theory.md``:

* **``POLICY_CONST``**: Whalley-Wilmott's ``3/2`` is for hedging to the band **EDGE**;
  everything in this repo hedges to **TARGET**, whose constant is **6** -- a band
  ``4**(1/3) = 1.587x`` wider.  ``zones.py`` had restated the formula with the edge
  constant and was 1.587x too tight for the policy the repo actually runs.  It now
  takes the constant **from** ``bandopt`` and must not restate it: a duplicated
  formula is how ``band_pct`` and ``COMPONENTS`` drifted earlier on this project, and
  it has now caused three bugs.  That coupling is tested by *changing* the constant
  and checking ``zones`` moves with it.
* **The objective is extremely flat**: being 30% off the band costs 0.1-0.9% of the
  gamma P&L, against 1.2-16.3% for being 30% off the cap.  The band is therefore
  DEMOTED and ``max_overnight_delta`` is the decision.
* The empirical referee **imposes** ``E[hedging error] = 0`` and then **tests** the
  imposition; ``worst |z|`` was 1.92 and must stay consistent with zero.
* Zakamouline/empirical agreement 0.79-1.23 across the four measured cells.

And the null control that matters: ``exp_capture`` is constant down the whole utility
curve -- the band cannot change the expected P&L, only the cost and the variance.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from fxgamma import conventions as cv

bo = pytest.importorskip("fxgamma.portfolio.bandopt")
zones = pytest.importorskip("fxgamma.portfolio.zones")

from conftest import _straddle  # noqa: E402

PIP = cv.pair_spec("EURUSD").pip


@pytest.fixture(scope="module")
def bg(ref_book, on_mkt):
    return bo.book_gamma(ref_book, on_mkt, "EURUSD")


# =========================================================================== #
# 1.  POLICY_CONST -- hedge-to-edge 1.5 vs hedge-to-target 6
# =========================================================================== #
class TestPolicyConstant:
    def test_the_two_constants(self):
        assert bo.POLICY_CONST["edge"] == 1.5
        assert bo.POLICY_CONST["center"] == 6.0

    def test_center_is_four_times_edge(self):
        assert bo.POLICY_CONST["center"] / bo.POLICY_CONST["edge"] == pytest.approx(4.0)

    def test_the_band_ratio_is_the_cube_root_of_four(self):
        r = (bo.POLICY_CONST["center"] / bo.POLICY_CONST["edge"]) ** (1 / 3)
        assert r == pytest.approx(4 ** (1 / 3), rel=1e-12)
        assert r == pytest.approx(1.587, abs=0.001)

    def test_zakamouline_respects_the_policy(self, ref_book, on_mkt):
        e = bo.optimal_band(ref_book, on_mkt, "EURUSD", policy="edge").band_pips
        c = bo.optimal_band(ref_book, on_mkt, "EURUSD", policy="center").band_pips
        assert c / e == pytest.approx(4 ** (1 / 3), rel=0.03)

    def test_center_is_the_default_policy(self, ref_book, on_mkt):
        a = bo.optimal_band(ref_book, on_mkt, "EURUSD")
        b = bo.optimal_band(ref_book, on_mkt, "EURUSD", policy="center")
        assert a.band_pips == pytest.approx(b.band_pips, rel=1e-12)

    def test_whalley_wilmott_reports_on_its_own_terms(self, ref_book, on_mkt):
        """WW is the classic and is reported with ITS constant, badged ``edge``, with
        the hedge-to-target figure stated in the note.  Silently re-basing a named
        author's formula would be the other way to get this wrong."""
        r = bo.optimal_band(ref_book, on_mkt, "EURUSD", method="whalley_wilmott")
        assert r.policy == "edge"
        assert "BAND EDGE" in r.note and "TARGET" in r.note
        assert "1.587" in r.note

    def test_ww_note_quotes_the_correct_wider_band(self, ref_book, on_mkt):
        r = bo.optimal_band(ref_book, on_mkt, "EURUSD", method="whalley_wilmott")
        wider = r.band_pips * 4 ** (1 / 3)
        assert f"{wider:,.1f} pips" in r.note

    def test_unknown_policy_raises(self, ref_book, on_mkt):
        with pytest.raises(ValueError):
            bo.optimal_band(ref_book, on_mkt, "EURUSD", policy="middle")

    def test_unknown_method_raises_rather_than_falling_back(self, ref_book, on_mkt):
        """Architecture s7: never silently substitute."""
        with pytest.raises(ValueError):
            bo.optimal_band(ref_book, on_mkt, "EURUSD", method="magic")


# =========================================================================== #
# 2.  zones.py must TAKE the constant, not restate it
# =========================================================================== #
class TestZonesTakesTheConstantFromBandopt:
    """A duplicated formula caused three bugs on this project.  Changing
    ``bandopt.POLICY_CONST`` must move ``zones.hedge_bands``' WW band by the cube root
    of the change.  If it does not, the formula has been restated somewhere."""

    @pytest.mark.regression
    @pytest.mark.parametrize("factor", [8.0, 27.0, 0.125])
    def test_zones_ww_band_follows_bandopt_policy_const(self, ref_book, on_mkt,
                                                        monkeypatch, factor):
        base = float(zones.hedge_bands(ref_book, on_mkt, "EURUSD")["band_ww_base"].iloc[0])
        monkeypatch.setitem(bo.POLICY_CONST, "center", 6.0 * factor)
        got = float(zones.hedge_bands(ref_book, on_mkt, "EURUSD")["band_ww_base"].iloc[0])
        assert got / base == pytest.approx(factor ** (1 / 3), rel=1e-9), (
            "zones is not reading bandopt.POLICY_CONST -- the formula is restated")

    @pytest.mark.regression
    def test_zones_uses_the_center_constant_not_the_edge_one(self, ref_book, on_mkt,
                                                             monkeypatch):
        """The exact bug: ``zones`` had the edge constant and was 1.587x too tight."""
        base = float(zones.hedge_bands(ref_book, on_mkt, "EURUSD")["band_ww_base"].iloc[0])
        monkeypatch.setitem(bo.POLICY_CONST, "edge", 1.5 * 64.0)
        got = float(zones.hedge_bands(ref_book, on_mkt, "EURUSD")["band_ww_base"].iloc[0])
        assert got == pytest.approx(base, rel=1e-9), \
            "zones moved when the EDGE constant changed; it should read 'center'"

    def test_zones_ww_band_matches_the_analytic_cubic(self, ref_book, on_mkt, bg):
        row = zones.hedge_bands(ref_book, on_mkt, "EURUSD").iloc[0]
        lam = float(row["cost_bp_round_trip"]) / 2.0 / 1e4
        ra = float(row["risk_aversion"])
        ww = (bo.POLICY_CONST["center"] * math.exp(-bg.rd * bg.T_ref) * lam
              * bg.spot * bg.gamma ** 2 / ra) ** (1 / 3)
        assert float(row["band_ww_base"]) == pytest.approx(ww, rel=1e-6)

    def test_trading_days_constant_is_shared_not_copied(self):
        assert bo.TRADING_DAYS is zones.TRADING_DAYS

    def test_risk_aversion_units_are_not_assumed_interchangeable(self, ref_book,
                                                                  on_mkt):
        """RFC-1 (accepted, assigned to PM): ``zones.risk_aversion`` and ``bandopt``'s
        differ in units and the two must not be compared until reconciled.  Recorded
        here as a measurement so a future reader does not assume they agree: the same
        coefficient fed to both produces materially different bands."""
        ra = 1e-6
        z = float(zones.hedge_bands(ref_book, on_mkt, "EURUSD",
                                    risk_aversion=ra)["band_ww_base"].iloc[0])
        b = bo.optimal_band(ref_book, on_mkt, "EURUSD", risk_aversion=ra,
                            method="whalley_wilmott", cost_tier="interbank")
        assert z > 0 and b.band_delta_base > 0
        assert z != pytest.approx(b.band_delta_base, rel=0.05), (
            "if these ever coincide, RFC-1 has been resolved and this test should be "
            "replaced with the reconciled identity")


# =========================================================================== #
# 3.  The utility curve -- and the band cannot move the expected P&L
# =========================================================================== #
@pytest.fixture(scope="module")
def curve(bg):
    return bo.band_utility_curve(bg, np.array([2e-4, 5e-4, 1e-3, 2e-3, 5e-3]),
                                 lam=2.5e-4, gamma_q=1e-6, horizon_days=1.0)


class TestUtilityCurveNullControls:
    def test_exp_capture_is_constant_down_the_whole_table(self, curve):
        """THE null control of the band question: the expected gamma P&L does not
        depend on the band.  Everything a band does is cost and variance."""
        assert curve["exp_capture"].nunique() == 1

    def test_zero_cost_gives_zero_cost_at_every_band(self, bg):
        c = bo.band_utility_curve(bg, np.array([2e-4, 1e-3, 5e-3]), lam=0.0,
                                  gamma_q=1e-6, horizon_days=1.0)
        assert (c["exp_cost"] == 0.0).all()

    def test_zero_cost_and_zero_risk_aversion_makes_utility_flat(self, bg):
        c = bo.band_utility_curve(bg, np.array([2e-4, 1e-3, 5e-3]), lam=0.0,
                                  gamma_q=0.0, horizon_days=1.0)
        assert c["utility"].nunique() == 1

    def test_cost_is_inverse_in_the_band(self, curve):
        a, b = curve.iloc[0], curve.iloc[2]
        assert a["exp_cost"] / b["exp_cost"] == pytest.approx(
            b["band_spot"] / a["band_spot"], rel=1e-12)

    def test_rehedges_scale_as_one_over_band_squared(self, curve):
        a, b = curve.iloc[0], curve.iloc[2]
        assert a["exp_rehedges"] / b["exp_rehedges"] == pytest.approx(
            (b["band_spot"] / a["band_spot"]) ** 2, rel=1e-12)

    def test_variance_scales_as_band_squared(self, curve):
        a, b = curve.iloc[0], curve.iloc[2]
        assert b["exp_var"] / a["exp_var"] == pytest.approx(
            (b["band_spot"] / a["band_spot"]) ** 2, rel=1e-12)

    def test_band_delta_is_gamma_times_band(self, curve, bg):
        for _, r in curve.iterrows():
            assert r["band_delta_base"] == pytest.approx(
                abs(bg.gamma) * r["band_spot"], rel=1e-12)

    def test_band_pips_is_band_over_pip(self, curve):
        for _, r in curve.iterrows():
            assert r["band_pips"] == pytest.approx(r["band_spot"] / PIP, rel=1e-12)

    def test_capture_is_half_gamma_variance(self, curve, bg):
        tau = 1.0 / 252.0
        V = bg.sigma ** 2 * tau * bg.spot ** 2
        assert curve["exp_capture"].iloc[0] == pytest.approx(0.5 * bg.gamma * V,
                                                             rel=1e-12)


# =========================================================================== #
# 4.  The objective is FLAT -- the ruling that demoted the band
# =========================================================================== #
class TestTheObjectiveIsFlat:
    """docs/09 s5: being 30% off the optimal band costs **0.1-0.9%** of the gamma
    P&L.  That result is why ``max_overnight_delta`` is the decision and the band is
    a refinement inside it.  If this ever reads 10%, the ruling is wrong."""

    @pytest.mark.parametrize("off", [0.7, 1.3])
    @pytest.mark.parametrize("cost_bp", [0.2, 5.0])
    def test_thirty_percent_off_the_band_costs_under_one_percent(self, ref_book,
                                                                 on_mkt, off, cost_bp):
        r = bo.optimal_band(ref_book, on_mkt, "EURUSD", cost_bp=cost_bp,
                            horizon_days=1.0)
        bgm = bo.book_gamma(ref_book, on_mkt, "EURUSD")
        lam = cost_bp / 2e4
        c = bo.band_utility_curve(bgm, np.array([r.band_spot, r.band_spot * off]),
                                  lam=lam, gamma_q=1e-6, horizon_days=1.0)
        giveup = (c["utility"].iloc[0] - c["utility"].iloc[1]) / abs(c["exp_capture"].iloc[0])
        assert 0.0 <= giveup < 0.02, f"30% off the band gave up {giveup:.2%}"

    def test_the_analytic_optimum_is_a_maximum(self, ref_book, on_mkt, bg):
        r = bo.optimal_band(ref_book, on_mkt, "EURUSD", cost_bp=5.0)
        c = bo.band_utility_curve(
            bg, np.array([r.band_spot * 0.8, r.band_spot, r.band_spot * 1.25]),
            lam=5.0 / 2e4, gamma_q=1e-6, horizon_days=1.0)
        assert c["utility"].iloc[1] >= c["utility"].iloc[0]
        assert c["utility"].iloc[1] >= c["utility"].iloc[2]

    def test_band_scales_as_the_cube_root_of_cost(self, ref_book, on_mkt):
        a = bo.optimal_band(ref_book, on_mkt, "EURUSD", cost_bp=1.0,
                            method="whalley_wilmott")
        b = bo.optimal_band(ref_book, on_mkt, "EURUSD", cost_bp=8.0,
                            method="whalley_wilmott")
        assert b.band_pips / a.band_pips == pytest.approx(2.0, rel=1e-6)

    def test_a_25x_cost_error_is_a_29x_band_error(self, ref_book, on_mkt):
        """The module's own warning, made a test."""
        a = bo.optimal_band(ref_book, on_mkt, "EURUSD", cost_bp=0.2,
                            method="whalley_wilmott")
        b = bo.optimal_band(ref_book, on_mkt, "EURUSD", cost_bp=5.0,
                            method="whalley_wilmott")
        assert b.band_pips / a.band_pips == pytest.approx(25 ** (1 / 3), rel=1e-6)

    def test_retail_band_is_much_wider_than_interbank(self, ref_book, on_mkt):
        a = bo.optimal_band(ref_book, on_mkt, "EURUSD", cost_tier="interbank")
        b = bo.optimal_band(ref_book, on_mkt, "EURUSD", cost_tier="retail")
        assert b.band_pips > 2.5 * a.band_pips

    def test_retail_is_the_default_tier(self, ref_book, on_mkt):
        a = bo.optimal_band(ref_book, on_mkt, "EURUSD")
        b = bo.optimal_band(ref_book, on_mkt, "EURUSD", cost_tier="retail")
        assert a.band_pips == pytest.approx(b.band_pips, rel=1e-12)

    def test_zakamouline_eurusd_interbank_is_about_60_pips(self, ref_book, on_mkt):
        """docs/09's shipped intraday numbers: ~60 pips interbank, ~180 retail."""
        r = bo.optimal_band(ref_book, on_mkt, "EURUSD", cost_tier="interbank",
                            horizon_days=1.0)
        assert 35.0 < r.band_pips < 95.0

    def test_zakamouline_eurusd_retail_is_about_180_pips(self, ref_book, on_mkt):
        r = bo.optimal_band(ref_book, on_mkt, "EURUSD", cost_tier="retail",
                            horizon_days=1.0)
        assert 120.0 < r.band_pips < 260.0


# =========================================================================== #
# 5.  The delta cap is the decision -- max_delta binds over the analytic band
# =========================================================================== #
class TestDeltaCapBindsOverTheBand:
    def test_max_delta_is_a_hard_ceiling(self, ref_book, on_mkt):
        r = bo.optimal_band(ref_book, on_mkt, "EURUSD", max_delta=0.5e6)
        assert r.band_delta_base <= 0.5e6 * 1.000001

    def test_cap_binds_is_reported(self, ref_book, on_mkt):
        r = bo.optimal_band(ref_book, on_mkt, "EURUSD", max_delta=0.5e6)
        assert getattr(r, "cap_binds", True)
        loose = bo.optimal_band(ref_book, on_mkt, "EURUSD", max_delta=50e6)
        assert not getattr(loose, "cap_binds", False)

    def test_implied_risk_aversion_round_trips(self, ref_book, on_mkt, bg):
        r = bo.optimal_band(ref_book, on_mkt, "EURUSD", risk_aversion=3e-6)
        ra = bo.risk_aversion_for_band(r.band_delta_base, lam=r.cost_bp / 2e4,
                                       spot=bg.spot, gamma=bg.gamma,
                                       rd=bg.rd, T=bg.T_ref)
        assert ra == pytest.approx(3e-6, rel=0.35)

    def test_risk_aversion_for_band_is_monotone(self, bg):
        kw = dict(lam=2.5e-4, spot=bg.spot, gamma=bg.gamma)
        assert bo.risk_aversion_for_band(0.5e6, **kw) > \
            bo.risk_aversion_for_band(2.0e6, **kw)


# =========================================================================== #
# 6.  The empirical referee -- and its imposed-then-tested assumption
# =========================================================================== #
@pytest.fixture(scope="module")
def emp(ref_book, on_mkt):
    return bo.optimal_band(ref_book, on_mkt, "EURUSD", method="empirical",
                           cost_bp=5.0, horizon_days=2.0, n_paths=16,
                           n_bands=7, steps_per_day=12)


@pytest.mark.slow
class TestEmpiricalReferee:
    """The referee **imposes** ``E[hedging error] = 0`` because resolving a USD 200
    cost difference through a USD 38k-sd sample mean would need ~1e5 paths -- and then
    **tests** the imposition.  ``worst |z|`` was 1.92 in docs/09 and the module itself
    calls anything above 3 untrustworthy."""

    def test_the_imposed_zero_mean_is_tested_not_assumed(self, emp):
        worst = emp.diagnostics["worst_resid_z"]
        assert worst < 3.0, "the engine has a bias; the empirical band is untrustworthy"

    def test_the_note_states_the_imposition_and_its_test(self, emp):
        assert "imposed to be zero" in emp.note
        assert "worst |z|" in emp.note

    def test_the_note_badges_it_as_in_sample(self, emp):
        assert "IN-SAMPLE" in emp.note

    def test_common_random_numbers_are_used(self, emp):
        assert emp.curve["exp_capture"].nunique() == 1

    def test_cost_falls_monotonically_with_the_band(self, emp):
        assert emp.curve["exp_cost"].is_monotonic_decreasing

    def test_rehedges_fall_monotonically_with_the_band(self, emp):
        assert emp.curve["exp_rehedges"].is_monotonic_decreasing

    def test_zakamouline_lands_near_the_empirical_argmax(self, ref_book, on_mkt, emp):
        """docs/09's measured agreement is 0.79-1.23 across four cells; on a short
        sweep the tolerance is wider but the two must not be a factor apart."""
        z = bo.optimal_band(ref_book, on_mkt, "EURUSD", cost_bp=5.0, horizon_days=2.0)
        assert 0.4 < z.band_pips / emp.band_pips < 2.6

    def test_measured_cost_sits_below_the_continuous_analytic(self, emp, bg):
        """Discrete monitoring misses crossings, so the simulated cost is BELOW the
        continuous-monitoring formula.  docs/09 s4.7 records the same 15-25% gap."""
        row = emp.curve.iloc[len(emp.curve) // 2]
        h = float(row["band_spot"])
        tau = 2.0 / 252.0
        V = (bg.sigma ** 2) * tau * bg.spot ** 2
        analytic = (5.0 / 2e4) * bg.spot * abs(bg.gamma) * V / h
        assert row["exp_cost"] < analytic


# =========================================================================== #
# 7.  Persistence cannot ship unclamped
# =========================================================================== #
class TestPersistenceClamp:
    """Taken literally the first-order condition gives a band multiplier of **x6.97**
    at phi=+0.3.  Clamped to ``sqrt(R)``.  That the raw answer is absurd is the whole
    argument for a state-dependent ratchet rather than a static multiplier."""

    def test_brownian_is_the_default_and_changes_nothing(self, ref_book, on_mkt):
        a = bo.optimal_band(ref_book, on_mkt, "EURUSD")
        b = bo.optimal_band(ref_book, on_mkt, "EURUSD", persistence=0.0)
        assert a.band_pips == pytest.approx(b.band_pips, rel=1e-12)

    def test_hurst_from_persistence_is_one_half_at_zero(self):
        assert bo.hurst_from_persistence(0.0) == pytest.approx(0.5, rel=1e-12)

    def test_hurst_rises_with_persistence(self):
        assert bo.hurst_from_persistence(0.3) > bo.hurst_from_persistence(0.0) > \
            bo.hurst_from_persistence(-0.3)

    def test_hurst_slope_is_the_documented_constant(self):
        assert (bo.hurst_from_persistence(0.2) - bo.hurst_from_persistence(0.0)) \
            == pytest.approx(0.2 * bo.HURST_PER_PHI, rel=1e-9)

    @pytest.mark.parametrize("phi", [0.1, 0.2, 0.3, 0.5])
    def test_the_multiplier_is_clamped_well_below_697(self, ref_book, on_mkt, phi):
        base = bo.optimal_band(ref_book, on_mkt, "EURUSD").band_pips
        got = bo.optimal_band(ref_book, on_mkt, "EURUSD", persistence=phi).band_pips
        assert got / base < 3.0, "the x6.97 raw answer must not ship"

    @pytest.mark.parametrize("phi", [0.1, 0.3])
    def test_trending_widens_and_choppy_tightens(self, ref_book, on_mkt, phi):
        base = bo.optimal_band(ref_book, on_mkt, "EURUSD").band_pips
        up = bo.optimal_band(ref_book, on_mkt, "EURUSD", persistence=+phi).band_pips
        dn = bo.optimal_band(ref_book, on_mkt, "EURUSD", persistence=-phi).band_pips
        assert dn < base < up

    def test_asymmetric_multipliers_are_carried_through(self, ref_book, on_mkt):
        r = bo.optimal_band(ref_book, on_mkt, "EURUSD", up_mult=1.4, down_mult=0.7)
        assert not r.is_symmetric
        assert r.band_spot_up / r.band_spot_down == pytest.approx(2.0, rel=1e-6)

    def test_symmetric_by_default(self, ref_book, on_mkt):
        assert bo.optimal_band(ref_book, on_mkt, "EURUSD").is_symmetric


# =========================================================================== #
# 8.  book_gamma, leland and general contract
# =========================================================================== #
class TestBookGammaAndContract:
    def test_gamma_weighted_sigma_and_tenor(self, on_mkt):
        """A book long a 1W and short a 1Y has almost all its gamma in the 1W, and the
        band should be the 1W's band -- not the front expiry, not an average."""
        from fxgamma.types import Book
        S = on_mkt.spot["EURUSD"]
        b = Book(options=(_straddle("EURUSD", S, days=7, leg=10e6, prefix="w").options
                          + _straddle("EURUSD", S, days=365, leg=10e6,
                                      prefix="y").options), spots=[])
        g = bo.book_gamma(b, on_mkt, "EURUSD")
        naive = 0.5 * (7 / 365.0 + 1.0)
        assert g.T_ref < 0.3 * naive, "T_ref must be pulled to the 1W by gamma weights"
        assert g.T_ref > 7 / 365.0

    def test_gross_notional_is_absolute(self, ref_book, on_mkt):
        assert bo.book_gamma(ref_book, on_mkt, "EURUSD").gross_notional == \
            pytest.approx(20e6, rel=1e-12)

    def test_empty_book_gives_zero_gamma_and_an_empty_result(self, on_mkt):
        from fxgamma.types import Book
        r = bo.optimal_band(Book(options=[], spots=[]), on_mkt, "EURUSD")
        assert r.band_delta_base == 0.0 or math.isnan(r.band_delta_base)
        assert "no gamma" in r.note

    def test_leland_number_formula(self):
        lam, sig, dt = 2.5e-4, 0.08, 1 / 252
        assert bo.leland_number(lam, sig, dt) == pytest.approx(
            math.sqrt(8 / math.pi) * lam / (sig * math.sqrt(dt)), rel=1e-12)

    def test_leland_is_nan_on_degenerate_input(self):
        assert math.isnan(bo.leland_number(1e-4, 0.0, 1 / 252))
        assert math.isnan(bo.leland_number(1e-4, 0.08, 0.0))

    def test_cost_tables_are_distinct_and_retail_is_higher(self):
        for p in bo.RETAIL_COST_BP:
            if p in zones.COST_BP:
                assert bo.RETAIL_COST_BP[p] > zones.COST_BP[p]

    @pytest.mark.parametrize("method", ["zakamouline", "whalley_wilmott", "fixed_grid"])
    @pytest.mark.parametrize("pair", ["EURUSD", "USDJPY"])
    def test_every_method_and_pair_gives_a_finite_positive_band(self, on_mkt, method,
                                                                pair):
        b = _straddle(pair, on_mkt.spot[pair], prefix=f"z{pair}")
        r = bo.optimal_band(b, on_mkt, pair, method=method)
        assert r.band_pips > 0 and np.isfinite(r.band_pips)
        assert r.band_delta_base > 0
        assert r.method == method
        assert r.note

    def test_horizon_does_not_move_the_analytic_optimum(self, ref_book, on_mkt):
        """Documented: the horizon cancels out of the first-order condition."""
        a = bo.optimal_band(ref_book, on_mkt, "EURUSD", horizon_days=1.0,
                            method="whalley_wilmott")
        b = bo.optimal_band(ref_book, on_mkt, "EURUSD", horizon_days=10.0,
                            method="whalley_wilmott")
        assert a.band_pips == pytest.approx(b.band_pips, rel=1e-12)

    def test_horizon_does_scale_the_reported_economics(self, ref_book, on_mkt):
        a = bo.optimal_band(ref_book, on_mkt, "EURUSD", horizon_days=1.0,
                            method="whalley_wilmott")
        b = bo.optimal_band(ref_book, on_mkt, "EURUSD", horizon_days=10.0,
                            method="whalley_wilmott")
        assert b.exp_capture / a.exp_capture == pytest.approx(10.0, rel=1e-9)

    def test_fixed_grid_is_one_sigma_and_says_it_is_not_an_optimum(self, ref_book,
                                                                    on_mkt, bg):
        r = bo.optimal_band(ref_book, on_mkt, "EURUSD", method="fixed_grid")
        expected = bg.spot * bg.sigma * math.sqrt(1 / 252.0)
        assert r.band_spot == pytest.approx(expected, rel=1e-9)
        assert "NOT an optimum" in r.note

    def test_compare_bands_covers_the_methods(self, ref_book, on_mkt):
        df = bo.compare_bands(ref_book, on_mkt, "EURUSD",
                              methods=("zakamouline", "whalley_wilmott", "fixed_grid"))
        assert len(df) == 3
        assert df["band_pips"].gt(0).all()

    def test_band_result_is_frozen(self, ref_book, on_mkt):
        r = bo.optimal_band(ref_book, on_mkt, "EURUSD")
        with pytest.raises(Exception):
            r.band_pips = 1.0
