"""The golden reference trade -- AMENDMENT v1.4, ruling 3 (QA is named as the owner).

    EURUSD 1M ATM straddle, EUR 10mm per leg, spot 1.084, sigma 7.05%,
    struck on the delta-neutral-straddle strike.

    Gamma_1pct  EUR 3.914mm per 1%
    theta       USD -2,868 per calendar day     (the withdrawn figure was -5,800)
    breakeven   0.3678% = 39.9 pips, and BE = sigma / sqrt(365) to < 1%

One fixture that cross-checks gamma, theta and the breakeven identity together: the
three are not independent, and it was exactly their inconsistency that exposed the
2x theta error in the trader's reference table.  The header card MISS-4 puts on
every screen is computed from these numbers, so a 2x error here is a 2x error in
the single most-read number in the application.
"""
from __future__ import annotations

import math

import pytest

from fxgamma.models import gk, smile

# ---- the trade, exactly as amendment v1.4 states it ----------------------- #
SPOT = 1.084
SIGMA = 0.0705
T = 1.0 / 12.0            # "1M" on the frozen conventions.TENORS grid
RD, RF = 0.04, 0.02       # USD domestic, EUR foreign
LEG = 10_000_000.0        # EUR per leg
PIP = 1e-4

# ---- the arbitrated values ------------------------------------------------ #
GAMMA_1PCT = 3.914e6      # EUR per 1% spot move, both legs
THETA_DAY = -2868.0       # USD per calendar day, both legs (signed: you pay it)
BREAKEVEN_PIPS = 39.9


@pytest.fixture(scope="module")
def straddle():
    """(call, put) Greeks for the reference trade on its DNS strike."""
    K = smile.atm_strike(SPOT, T, RD, RF, SIGMA, "dns")
    c = gk.gk_greeks(SPOT, K, T, RD, RF, SIGMA, +1, LEG, +1)
    p = gk.gk_greeks(SPOT, K, T, RD, RF, SIGMA, -1, LEG, +1)
    return c, p


def test_reference_gamma_1pct(straddle):
    """EUR 3.914mm of delta gained per +1% spot -- the desk unit the app is built on."""
    c, p = straddle
    assert (c.gamma_1pct + p.gamma_1pct) == pytest.approx(GAMMA_1PCT, rel=1e-3)


def test_reference_theta_is_not_the_withdrawn_figure(straddle):
    """USD -2,868/day, not the withdrawn -5,800.  Signed: negative = you pay."""
    c, p = straddle
    theta = c.theta + p.theta
    assert theta == pytest.approx(THETA_DAY, rel=1e-3)
    assert theta < 0.0
    assert abs(theta) < 4000.0, "theta regressed towards the withdrawn 2x figure"


def test_reference_theta_friday_to_monday(straddle):
    """Three calendar days: ~USD 8,600, not the withdrawn 17,400."""
    c, p = straddle
    assert (c.theta + p.theta) * 3 == pytest.approx(-8600.0, rel=5e-3)


def test_reference_breakeven_in_pips(straddle):
    """``BE% = sqrt(|theta| / (0.005 Gamma_1pct S))`` = 39.9 pips."""
    c, p = straddle
    theta, g1 = c.theta + p.theta, c.gamma_1pct + p.gamma_1pct
    be_pct = math.sqrt(abs(theta) / (0.005 * g1 * SPOT))
    assert be_pct * 0.01 * SPOT / PIP == pytest.approx(BREAKEVEN_PIPS, abs=0.15)


def test_breakeven_reduces_to_sigma_over_sqrt_365(straddle):
    """Amendment v1.4 ruling 3: the identity must hold to better than 1%.

    It is the closure test between gamma, theta and the breakeven helper: get any
    one of the three wrong and this fails.
    """
    c, p = straddle
    theta, g1 = c.theta + p.theta, c.gamma_1pct + p.gamma_1pct
    be_pct = math.sqrt(abs(theta) / (0.005 * g1 * SPOT))
    assert be_pct == pytest.approx(100.0 * SIGMA / math.sqrt(365.0), rel=0.01)


def test_the_three_numbers_are_mutually_consistent(straddle):
    """Given Gamma_1pct and the breakeven, theta is determined -- the cross-check
    that showed the reference table's 5,800 could not be right."""
    c, p = straddle
    g1 = c.gamma_1pct + p.gamma_1pct
    implied_theta = 0.005 * g1 * SPOT * (BREAKEVEN_PIPS * PIP / SPOT * 100.0) ** 2
    assert implied_theta == pytest.approx(abs(c.theta + p.theta), rel=0.01)


# --------------------------------------------------------------------------- #
# the delta-hedged carry identity (amendment v1.4 ruling 4 / trader W-5)
# --------------------------------------------------------------------------- #
def test_delta_hedged_carry_identity(straddle):
    """``50 Gamma_1pct S (sigma_r^2 - sigma_i^2) dt_years``.

    One day of 9% realized against 7.05% implied on the reference trade is
    ~USD +1,820 (the PM's +1,836 used the trader's rounded 3.95mm gamma).  The
    factor is 50, not 0.5: REQ-046's spelling is out by 100x (W-5).
    """
    risk = pytest.importorskip("fxgamma.portfolio.risk",
                               reason="portfolio/risk.py has not landed yet")
    c, p = straddle
    g1 = c.gamma_1pct + p.gamma_1pct
    pnl = risk.dhedge_pnl(g1, SPOT, 0.09, SIGMA, 1 / 365)
    assert pnl == pytest.approx(50.0 * g1 * SPOT * (0.09 ** 2 - SIGMA ** 2) / 365, rel=1e-12)
    assert pnl == pytest.approx(1836.0, rel=0.02)


def test_delta_hedged_carry_is_flat_when_realized_equals_implied(straddle):
    """At ``sigma_r == sigma_i`` gamma pays exactly the theta bill -- to epsilon."""
    risk = pytest.importorskip("fxgamma.portfolio.risk",
                               reason="portfolio/risk.py has not landed yet")
    c, p = straddle
    g1 = c.gamma_1pct + p.gamma_1pct
    assert risk.dhedge_pnl(g1, SPOT, SIGMA, SIGMA, 1 / 365) == pytest.approx(0.0, abs=1e-9)


def test_gamma_pnl_for_a_one_percent_move(straddle):
    """``0.005 Gamma_1pct S x^2`` -- and at the breakeven move it equals one day of
    theta, which is what "breakeven" means."""
    risk = pytest.importorskip("fxgamma.portfolio.risk",
                               reason="portfolio/risk.py has not landed yet")
    c, p = straddle
    g1, theta = c.gamma_1pct + p.gamma_1pct, c.theta + p.theta
    assert risk.gamma_pnl_pct(g1, SPOT, 1.0) == pytest.approx(0.005 * g1 * SPOT, rel=1e-12)
    be_pct = math.sqrt(abs(theta) / (0.005 * g1 * SPOT))
    assert risk.gamma_pnl_pct(g1, SPOT, be_pct) == pytest.approx(abs(theta), rel=1e-9)


# --------------------------------------------------------------------------- #
# W-7: the two annualisation bases must never be conflated
# --------------------------------------------------------------------------- #
def test_economics_and_distance_bases_differ_by_twenty_percent():
    """``sigma/sqrt(365)`` (what you pay) vs ``sigma/sqrt(252)`` (how far spot goes).

    39.9 pips vs 48.1 pips on this trade.  Using sqrt(365) for distance makes every
    far strike look ~20% safer than it is (W-7).
    """
    economics = SIGMA / math.sqrt(365.0) * SPOT / PIP
    distance = SIGMA / math.sqrt(252.0) * SPOT / PIP
    assert economics == pytest.approx(40.0, abs=0.2)
    assert distance == pytest.approx(48.1, abs=0.3)
    assert distance / economics == pytest.approx(math.sqrt(365 / 252), rel=1e-12)


# --------------------------------------------------------------------------- #
# AMENDMENT v1.4 ruling 3, extended: BE = sigma/sqrt(365) across the whole grid
# --------------------------------------------------------------------------- #
"""The single trade above pins the arbitrated numbers.  The identity behind it is
general, and a bug that only shows up on a 147-handle pair, at 2Y, or when rd != rf
would slip past a one-point fixture.  The sweep below re-derives the breakeven from a
freshly priced straddle at every combination and requires the same identity to hold.

Why it is worth the extra cases: ``BE% = sqrt(|theta_gamma| / (0.005 G1 S))`` closes
only if gamma, the gamma-theta and the 1%-move scaling are all mutually consistent.
The identity is *dimensionless* -- the handle, the pip size and the notional all
cancel -- so a JPY-specific scaling slip (the W-16 family) or a 100x in Gamma_1pct
(the W-5 family) breaks it, while a change in the pricer that is genuinely correct
leaves it alone.
"""

_IDENTITY_PAIRS = ("EURUSD", "USDJPY", "GBPUSD", "AUDUSD", "USDCHF")
_IDENTITY_SPOTS = {"EURUSD": 1.0840, "USDJPY": 147.50, "GBPUSD": 1.2650,
                   "AUDUSD": 0.6500, "USDCHF": 0.8000}
_IDENTITY_TENORS = (1 / 52, 1 / 12, 0.25, 0.5, 1.0, 2.0)
_IDENTITY_RATES = ((0.00, 0.00), (0.04, 0.02), (0.02, 0.04), (0.05, 0.001))
_IDENTITY_VOLS = (0.05, 0.0705, 0.12, 0.25)


def _straddle_identity(S: float, T: float, rd: float, rf: float, sigma: float):
    """(BE% from the priced straddle, sigma/sqrt(365) in percent)."""
    from fxgamma.signals.richness import breakeven_pct, gamma_theta

    K = smile.atm_strike(S, T, rd, rf, sigma, "dns")
    c = gk.gk_greeks(S, K, T, rd, rf, sigma, +1, LEG, +1)
    p = gk.gk_greeks(S, K, T, rd, rf, sigma, -1, LEG, +1)
    g = c + p
    be = breakeven_pct(gamma_theta(g.gamma, S, sigma), g.gamma_1pct, S)
    return be, 100.0 * sigma / math.sqrt(365.0)


@pytest.mark.parametrize("pair", _IDENTITY_PAIRS)
@pytest.mark.parametrize("T", _IDENTITY_TENORS)
def test_breakeven_identity_holds_across_pairs_and_tenors(pair, T):
    """``BE = sigma/sqrt(365)`` on every pair and tenor, at realistic rates.

    Amendment v1.4 ruling 3 asks for the identity to <1% on the reference trade; it
    holds to machine precision everywhere, so this asserts the tight bound and would
    catch a drift long before it reached 1%.
    """
    S = _IDENTITY_SPOTS[pair]
    be, want = _straddle_identity(S, T, 0.04, 0.02, SIGMA)
    assert be == pytest.approx(want, rel=1e-9), (pair, T, be, want)


@pytest.mark.parametrize("rd,rf", _IDENTITY_RATES)
@pytest.mark.parametrize("sigma", _IDENTITY_VOLS)
def test_breakeven_identity_is_independent_of_the_rate_setting(rd, rf, sigma):
    """Including ``rd < rf`` (the USDJPY case) and a near-zero domestic rate.

    The identity uses the **gamma-theta**, not the full theta, which is exactly why it
    survives a rate differential -- the carry part of theta is not a part gamma pays
    back (trader W-15).  A version built on the full theta fails here at 4%/2% and
    passes at 0/0, which is how a zero-rate-only test would have missed it.
    """
    be, want = _straddle_identity(147.50, 0.25, rd, rf, sigma)
    assert be == pytest.approx(want, rel=1e-9), (rd, rf, sigma, be, want)


def test_the_full_theta_version_of_the_identity_does_not_hold_at_non_zero_rates():
    """The negative control for the test above: if the identity were built on the
    *total* theta it would be rate-dependent, and the 39.9-pip breakeven card would
    quietly change with the rate differential.  This is why W-15 matters."""
    from fxgamma.signals.richness import breakeven_pct

    S, T, sigma, rd, rf = 147.50, 1.0, 0.0705, 0.05, 0.001
    K = smile.atm_strike(S, T, rd, rf, sigma, "dns")
    g = (gk.gk_greeks(S, K, T, rd, rf, sigma, +1, LEG, +1)
         + gk.gk_greeks(S, K, T, rd, rf, sigma, -1, LEG, +1))
    be_full = breakeven_pct(g.theta, g.gamma_1pct, S)
    want = 100.0 * sigma / math.sqrt(365.0)
    assert abs(be_full / want - 1.0) > 0.05, (
        "total-theta and gamma-theta breakevens agree here, so this control proves "
        "nothing -- pick a rate setting where the carry term actually bites")


@pytest.mark.parametrize("pair", _IDENTITY_PAIRS)
def test_the_identity_is_scale_free_in_notional_and_handle(pair):
    """Doubling the notional doubles gamma and theta and leaves the breakeven alone;
    so does quoting the same market on a different handle.  A pip-size or handle
    dependence (trader W-16) shows up here and nowhere else."""
    from fxgamma.signals.richness import breakeven_pct, gamma_theta

    S, T, sigma = _IDENTITY_SPOTS[pair], 0.25, 0.09
    K = smile.atm_strike(S, T, 0.04, 0.02, sigma, "dns")

    def be_for(notional: float, spot: float) -> float:
        k = smile.atm_strike(spot, T, 0.04, 0.02, sigma, "dns")
        g = (gk.gk_greeks(spot, k, T, 0.04, 0.02, sigma, +1, notional, +1)
             + gk.gk_greeks(spot, k, T, 0.04, 0.02, sigma, -1, notional, +1))
        return breakeven_pct(gamma_theta(g.gamma, spot, sigma), g.gamma_1pct, spot)

    base = be_for(LEG, S)
    assert be_for(4 * LEG, S) == pytest.approx(base, rel=1e-12)
    assert be_for(LEG, S * 100.0) == pytest.approx(base, rel=1e-12)
    assert base == pytest.approx(100.0 * sigma / math.sqrt(365.0), rel=1e-9)


def test_the_desk_helper_reproduces_the_identity_on_every_setting():
    """``richness.assert_breakeven_identity`` is the library's own acceptance test.
    Run it across the grid so a regression fails here as well as inside the library."""
    from fxgamma.signals.richness import assert_breakeven_identity

    for sigma in _IDENTITY_VOLS:
        for T in (1 / 52, 1 / 12, 0.25, 1.0):
            assert assert_breakeven_identity(sigma=sigma, S=1.0850, T=T) < 1e-9
