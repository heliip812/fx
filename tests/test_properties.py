"""Property tests: limits, signs and monotonicity that theory requires.

These are the checks that catch the class of bug that does not show up at the money
in the middle of the grid -- deep wings, expiry morning, a zero rate, a 300% vol --
which is exactly where a gamma book spends its most dangerous hours.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from fxgamma.models import gk

from conftest import CPS, MARKETS

#: degenerate inputs the app can genuinely produce: expiry morning, a dead vol mark,
#: a strike five handles away because someone typed 1400 instead of 140.
EXTREME_T = (0.0, 1e-12, gk.T_MIN, 1.0 / (365 * 24), 1 / 365, 5.0, 30.0)
EXTREME_VOL = (0.0, 1e-12, 1e-4, 0.005, 1.0, 3.0)
EXTREME_MONEYNESS = (0.01, 0.5, 0.95, 1.0, 1.05, 2.0, 100.0)


@pytest.mark.parametrize("m", MARKETS, ids=str)
@pytest.mark.parametrize("T", EXTREME_T)
def test_no_nan_anywhere_in_the_greek_set(m, T):
    """Every Greek stays finite over the whole degenerate grid.

    A single nan does not stay local: it propagates through ``price_book`` into the
    aggregate cards and blanks the screen a trader is trying to read.
    """
    bad = []
    for sigma in EXTREME_VOL:
        for mn in EXTREME_MONEYNESS:
            for cp in CPS:
                K = m.S * mn
                px = gk.gk_price(m.S, K, T, m.rd, m.rf, sigma, cp)
                g = gk.gk_greeks(m.S, K, T, m.rd, m.rf, sigma, cp, 1e6, +1)
                if not np.isfinite(px):
                    bad.append((sigma, mn, cp, "price", px))
                bad += [(sigma, mn, cp, k, v) for k, v in g.as_dict().items()
                        if not np.isfinite(v)]
    assert not bad, f"{m.pair}: non-finite results at T={T}: {bad[:12]}"


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_price_is_between_the_no_arbitrage_bounds(m):
    """Intrinsic <= value <= the discounted underlying (or strike, for a put)."""
    for T in (1 / 365, 0.25, 2.0):
        for sigma in (0.01, 0.10, 1.5):
            for mn in (0.5, 0.9, 1.0, 1.1, 2.0):
                for cp in CPS:
                    K = m.S * mn
                    lo, hi = gk.no_arb_bounds(m.S, K, T, m.rd, m.rf, cp)
                    px = gk.gk_price(m.S, K, T, m.rd, m.rf, sigma, cp)
                    assert lo - 1e-12 <= px <= hi + 1e-12, (T, sigma, mn, cp, px, lo, hi)


@pytest.mark.parametrize("m", MARKETS, ids=str)
@pytest.mark.parametrize("cp", CPS)
def test_price_is_monotone_increasing_in_vol(m, cp):
    """Vega > 0 everywhere: the property ``implied_vol`` needs to be well posed."""
    for T in (1 / 365, 0.25, 2.0):
        for mn in (0.7, 1.0, 1.4):
            v = np.linspace(1e-4, 1.5, 60)
            px = np.array([gk.gk_price(m.S, m.S * mn, T, m.rd, m.rf, float(s), cp)
                           for s in v])
            # tolerance is pure float noise: prices are O(S)
            assert np.all(np.diff(px) > -1e-14 * m.S), (T, mn, np.min(np.diff(px)))


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_call_price_rises_and_put_price_falls_with_spot(m):
    """Monotonicity in spot -- the ladder is unreadable if this ever inverts."""
    T, sigma = 0.25, 0.11
    S = np.linspace(0.7 * m.S, 1.4 * m.S, 60)
    K = m.S
    c = np.array([gk.gk_price(float(s), K, T, m.rd, m.rf, sigma, +1) for s in S])
    p = np.array([gk.gk_price(float(s), K, T, m.rd, m.rf, sigma, -1) for s in S])
    assert np.all(np.diff(c) > 0)
    assert np.all(np.diff(p) < 0)


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_call_price_falls_and_put_price_rises_with_strike(m):
    T, sigma = 0.5, 0.11
    K = np.linspace(0.6 * m.S, 1.6 * m.S, 60)
    c = np.array([gk.gk_price(m.S, float(k), T, m.rd, m.rf, sigma, +1) for k in K])
    p = np.array([gk.gk_price(m.S, float(k), T, m.rd, m.rf, sigma, -1) for k in K])
    assert np.all(np.diff(c) < 0)
    assert np.all(np.diff(p) > 0)


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_gamma_and_vega_are_non_negative_for_a_long_option(m):
    for T in (1 / 365, 0.25, 2.0):
        for sigma in (0.02, 0.10, 0.60):
            for mn in (0.5, 0.8, 1.0, 1.2, 2.0):
                for cp in CPS:
                    g = gk.gk_greeks(m.S, m.S * mn, T, m.rd, m.rf, sigma, cp, 1e6, +1)
                    assert g.gamma >= 0.0
                    assert g.vega >= 0.0
                    assert g.gamma_1pct >= 0.0


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_gamma_and_vega_are_the_same_for_a_call_and_a_put(m):
    """Put-call parity differentiates twice: the second-order Greeks are identical."""
    T, sigma = 0.25, 0.12
    for mn in (0.8, 1.0, 1.25):
        c = gk.gk_greeks(m.S, m.S * mn, T, m.rd, m.rf, sigma, +1, 1e6, +1)
        p = gk.gk_greeks(m.S, m.S * mn, T, m.rd, m.rf, sigma, -1, 1e6, +1)
        assert c.gamma == pytest.approx(p.gamma, rel=1e-12)
        assert c.vega == pytest.approx(p.vega, rel=1e-12)
        assert c.volga == pytest.approx(p.volga, rel=1e-12)
        assert c.vanna == pytest.approx(p.vanna, rel=1e-12)


@pytest.mark.parametrize("m", MARKETS, ids=str)
@pytest.mark.parametrize("cp", CPS)
def test_deep_itm_converges_to_the_discounted_forward_intrinsic(cp, m):
    """As the option goes deep ITM its value tends to ``cp (F - K) DF_d`` and its
    spot delta to ``cp e^{-rf T}`` -- with no nan on the way."""
    T, sigma = 1.0, 0.08
    K = m.F(T) * math.exp(-cp * 8.0 * sigma * math.sqrt(T))
    px = gk.gk_price(m.S, K, T, m.rd, m.rf, sigma, cp)
    fwd_intrinsic = max(cp * (m.F(T) - K), 0.0) * math.exp(-m.rd * T)
    assert px == pytest.approx(fwd_intrinsic, rel=1e-6)
    g = gk.gk_greeks(m.S, K, T, m.rd, m.rf, sigma, cp)
    assert g.delta_pct == pytest.approx(cp * math.exp(-m.rf * T), rel=1e-5)
    assert abs(g.gamma) < 1e-4


@pytest.mark.parametrize("m", MARKETS, ids=str)
@pytest.mark.parametrize("cp", CPS)
def test_deep_otm_is_worthless_but_still_priced(cp, m):
    T, sigma = 1.0, 0.08
    K = m.F(T) * math.exp(cp * 10.0 * sigma * math.sqrt(T))
    px = gk.gk_price(m.S, K, T, m.rd, m.rf, sigma, cp)
    assert 0.0 <= px < 1e-6 * m.S
    g = gk.gk_greeks(m.S, K, T, m.rd, m.rf, sigma, cp, 1e6, +1)
    assert abs(g.delta_base) < 1.0            # base ccy, on a 1mm notional
    assert np.isfinite(g.theta)


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_time_value_decays_to_zero_as_expiry_approaches(m):
    """T -> 0 from above: price -> intrinsic, gamma_1pct does not blow up to inf."""
    for mn in (0.98, 1.0, 1.02):
        K = m.S * mn
        prev = None
        for T in (1.0, 0.25, 1 / 12, 1 / 52, 1 / 365, 1 / (365 * 24), gk.T_MIN, 0.0):
            g = gk.gk_greeks(m.S, K, T, m.rd, m.rf, 0.10, +1, 1e6, +1)
            assert np.isfinite(g.pv) and np.isfinite(g.gamma_1pct)
            prev = g
        assert prev.pv == pytest.approx(max(m.S - K, 0.0) * 1e6, abs=1e-6)
        assert prev.gamma == 0.0


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_theta_is_signed_and_negative_for_a_long_atm_option(m):
    """`theta` is signed (trader review W-6b): a long ATM option *pays* theta."""
    T = 1 / 12
    g = gk.gk_greeks(m.S, m.F(T), T, m.rd, m.rf, 0.10, +1, 10e6, +1)
    assert g.theta < 0.0
    short = gk.gk_greeks(m.S, m.F(T), T, m.rd, m.rf, 0.10, +1, 10e6, -1)
    assert short.theta == pytest.approx(-g.theta, rel=1e-12)


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_atm_breakeven_identity_at_zero_rates(m):
    """The section-0 breakeven identity, in the units it is actually true in.

    ``sqrt(|theta| / (0.005 gamma_1pct S))`` equals ``sigma / sqrt(365)`` **in
    percent**, i.e. 100x the decimal daily move, because ``gamma_1pct`` is already
    a per-1%-move quantity.  A screen that prints this number next to a decimal
    sigma-day is out by 100 (see trader review W-5 on the same class of error), so
    the test pins both spellings.

    Run at zero rates: full GK theta also carries the rate terms, which the gamma
    does not pay back, so the identity only holds on the gamma-theta component
    (trader review W-15).
    """
    sigma, T, N = 0.10, 0.25, 10e6
    K = m.S           # zero rates -> F = S, and the DNS strike is S exp(w/2) ~ S
    c = gk.gk_greeks(m.S, K, T, 0.0, 0.0, sigma, +1, N, +1)
    p = gk.gk_greeks(m.S, K, T, 0.0, 0.0, sigma, -1, N, +1)
    theta, gamma_1pct = c.theta + p.theta, c.gamma_1pct + p.gamma_1pct
    be_pct = math.sqrt(abs(theta) / (0.005 * gamma_1pct * m.S))
    assert be_pct == pytest.approx(100.0 * sigma / math.sqrt(365.0), rel=1e-3)
    # the same thing written in raw Greeks, where no factor of 100 hides
    gamma = c.gamma + p.gamma
    be_frac = math.sqrt(abs(theta) / (0.5 * gamma * m.S ** 2))
    assert be_frac == pytest.approx(sigma / math.sqrt(365.0), rel=1e-3)


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_a_call_is_worth_more_when_the_domestic_rate_rises(m):
    """rho_d > 0 for a call, rho_f < 0: the sign check that catches a swapped pair."""
    T, sigma = 1.0, 0.10
    K = m.F(T)
    c = gk.gk_greeks(m.S, K, T, m.rd, m.rf, sigma, +1)
    p = gk.gk_greeks(m.S, K, T, m.rd, m.rf, sigma, -1)
    assert c.rho_d > 0 and c.rho_f < 0
    assert p.rho_d < 0 and p.rho_f > 0
    up = gk.gk_price(m.S, K, T, m.rd + 0.01, m.rf, sigma, +1)
    assert up > gk.gk_price(m.S, K, T, m.rd, m.rf, sigma, +1)


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_zero_vol_prices_to_discounted_forward_intrinsic(m):
    """A dead vol mark must not produce nan; it produces the forward intrinsic."""
    T = 0.5
    for mn in (0.8, 1.0, 1.2):
        K = m.S * mn
        for cp in CPS:
            px = gk.gk_price(m.S, K, T, m.rd, m.rf, 0.0, cp)
            assert px == pytest.approx(
                max(cp * (m.S * math.exp(-m.rf * T) - K * math.exp(-m.rd * T)), 0.0),
                abs=1e-14)


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_strike_from_delta_is_monotone_in_delta(m):
    """Higher call delta -> lower strike, in every convention (on the branch the
    market uses).  A non-monotone map means the wing quotes are mis-ordered."""
    T, sigma = 0.25, 0.10
    for conv in gk.DELTA_CONVENTIONS:
        ds = [0.05, 0.10, 0.25, 0.40]
        Kc = [gk.strike_from_delta(d, m.S, T, m.rd, m.rf, sigma, +1, conv) for d in ds]
        Kp = [gk.strike_from_delta(d, m.S, T, m.rd, m.rf, sigma, -1, conv) for d in ds]
        Kc = [k for k in Kc if np.isfinite(k)]
        Kp = [k for k in Kp if np.isfinite(k)]
        assert all(b < a for a, b in zip(Kc, Kc[1:])), (conv, Kc)
        assert all(b > a for a, b in zip(Kp, Kp[1:])), (conv, Kp)


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_a_25_delta_call_strike_sits_above_the_forward(m):
    """Sanity anchor a trader would check by eye, in all four conventions."""
    T, sigma = 0.25, 0.10
    for conv in gk.DELTA_CONVENTIONS:
        Kc = gk.strike_from_delta(0.25, m.S, T, m.rd, m.rf, sigma, +1, conv)
        Kp = gk.strike_from_delta(0.25, m.S, T, m.rd, m.rf, sigma, -1, conv)
        assert Kp < m.F(T) < Kc, (conv, Kp, m.F(T), Kc)
        assert 0.5 * m.F(T) < Kp and Kc < 2.0 * m.F(T)
