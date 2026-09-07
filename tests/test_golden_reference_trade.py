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
