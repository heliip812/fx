"""Numerical validation of the Garman-Kohlhagen pricer (docs/01_architecture.md s4).

Everything here is a *reusable harness* rather than a hand-checked constant:

* every analytic Greek against central finite differences of the price, to better
  than 1e-4 relative, parameterised over pair / moneyness / tenor / vol / cp;
* put-call parity;
* the price -> implied vol -> price round trip;
* all four delta conventions round-tripping through
  ``strike_from_delta`` / ``delta_from_strike``.

A failure here is a pricing failure: every P&L, hedge and card in the app is
downstream of these five functions.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from fxgamma.models import gk

from conftest import (CPS, MARKETS, TENORS, VOLS, ZS, fd_greeks, greek_mismatch,
                      rel_err, strike_at)

GREEKS = ("delta_base", "gamma", "vega", "theta", "rho_d", "rho_f",
          "vanna", "volga", "dual_delta")

#: the charter's target.  The finite-difference steps in ``conftest`` are scaled to
#: the option's own volatility scale, which holds the worst case near 5e-5.
FD_RTOL = 1e-4
#: per 1 unit of base notional, so 1e-8 quote ccy is 0.01 on a 1mm ticket -- below
#: any number the app displays, and far below anything that moves a hedge.
FD_ATOL = 1e-8


def _cases():
    for m in MARKETS:
        for T in TENORS:
            for sigma in VOLS:
                for z in ZS:
                    for cp in CPS:
                        yield pytest.param(
                            m, T, sigma, z, cp,
                            id=f"{m.pair}-T{T:.4f}-v{sigma:g}-z{z:+g}-{'C' if cp > 0 else 'P'}")


ALL_CASES = list(_cases())


# --------------------------------------------------------------------------- #
# analytic Greeks vs central finite differences
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("m,T,sigma,z,cp", ALL_CASES)
def test_analytic_greeks_match_central_differences(m, T, sigma, z, cp):
    """All nine Greeks, in desk units, against central differences of ``gk_price``."""
    K = strike_at(m, T, sigma, z)
    a = gk.gk_greeks(m.S, K, T, m.rd, m.rf, sigma, cp).as_dict()
    f = fd_greeks(m.S, K, T, m.rd, m.rf, sigma, cp)
    bad = greek_mismatch(a, f, GREEKS, rtol=FD_RTOL, atol=FD_ATOL * max(1.0, m.S))
    assert not bad, f"analytic vs FD mismatch (analytic, fd, rel): {bad}"


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_gamma_1pct_is_gamma_times_one_percent_of_spot(m):
    """`gamma_1pct` is the desk unit the whole app reads: delta gained per +1% spot."""
    K = strike_at(m, 0.25, 0.10, 0.0)
    g = gk.gk_greeks(m.S, K, 0.25, m.rd, m.rf, 0.10, +1, 10e6, +1)
    assert g.gamma_1pct == pytest.approx(g.gamma * m.S * 0.01, rel=1e-13)
    # and it is what a real +1% spot move does to delta, to second order
    up = gk.gk_greeks(m.S * 1.01, K, 0.25, m.rd, m.rf, 0.10, +1, 10e6, +1)
    assert (up.delta_base - g.delta_base) == pytest.approx(g.gamma_1pct, rel=2e-2)


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_monetary_greeks_scale_with_notional_and_direction(m):
    """Extensive Greeks carry notional x direction; `delta_pct` carries direction only."""
    args = (m.S, strike_at(m, 0.25, 0.10, 0.5), 0.25, m.rd, m.rf, 0.10, +1)
    one = gk.gk_greeks(*args, 1.0, +1)
    big = gk.gk_greeks(*args, 25e6, +1)
    short = gk.gk_greeks(*args, 25e6, -1)
    for f in ("pv", "delta_base", "gamma", "vega", "theta", "vanna", "volga"):
        assert getattr(big, f) == pytest.approx(getattr(one, f) * 25e6, rel=1e-12)
        assert getattr(short, f) == pytest.approx(-getattr(big, f), rel=1e-12)
    assert big.delta_pct == pytest.approx(one.delta_pct, rel=1e-12)
    assert short.delta_pct == pytest.approx(-one.delta_pct, rel=1e-12)


# --------------------------------------------------------------------------- #
# put-call parity
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("m,T,sigma,z,cp", [c for c in ALL_CASES if c.values[4] == +1])
def test_put_call_parity(m, T, sigma, z, cp):
    """``C - P = S e^{-rf T} - K e^{-rd T}`` -- the identity that catches a swapped
    ``rd``/``rf``, which is the single most common FX pricing bug."""
    K = strike_at(m, T, sigma, z)
    c = gk.gk_price(m.S, K, T, m.rd, m.rf, sigma, +1)
    p = gk.gk_price(m.S, K, T, m.rd, m.rf, sigma, -1)
    rhs = m.S * math.exp(-m.rf * T) - K * math.exp(-m.rd * T)
    assert (c - p) == pytest.approx(rhs, rel=1e-12, abs=1e-14 * m.S)


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_delta_parity_across_conventions(m):
    """Call delta - put delta is ``e^{-rf T}`` (spot) / ``1`` (fwd) at every strike."""
    T, sigma = 0.5, 0.11
    for z in ZS:
        K = strike_at(m, T, sigma, z)
        dc = gk.delta_from_strike(K, m.S, T, m.rd, m.rf, sigma, +1, "spot")
        dp = gk.delta_from_strike(K, m.S, T, m.rd, m.rf, sigma, -1, "spot")
        assert dc - dp == pytest.approx(math.exp(-m.rf * T), rel=1e-12)
        fc = gk.delta_from_strike(K, m.S, T, m.rd, m.rf, sigma, +1, "fwd")
        fp = gk.delta_from_strike(K, m.S, T, m.rd, m.rf, sigma, -1, "fwd")
        assert fc - fp == pytest.approx(1.0, rel=1e-12)


# --------------------------------------------------------------------------- #
# implied vol round trip
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("m,T,sigma,z,cp", ALL_CASES)
def test_implied_vol_round_trips_price_to_vol_to_price(m, T, sigma, z, cp):
    """price -> vol -> price to the solver's own tolerance, and vol to 1e-6.

    Deep wings carry almost no vega, so the *vol* recovered there is only as good
    as ``tol / vega``; the price round trip must still be exact.
    """
    K = strike_at(m, T, sigma, z)
    px = gk.gk_price(m.S, K, T, m.rd, m.rf, sigma, cp)
    iv = gk.implied_vol(px, m.S, K, T, m.rd, m.rf, cp)
    assert np.isfinite(iv), f"implied_vol returned nan for a price it produced ({px})"
    px2 = gk.gk_price(m.S, K, T, m.rd, m.rf, iv, cp)
    assert px2 == pytest.approx(px, abs=1e-9 * max(1.0, m.S), rel=1e-9)
    assert iv == pytest.approx(sigma, abs=1e-6)


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_implied_vol_returns_nan_outside_the_no_arbitrage_band(m):
    """A price outside the bounds is bad data; nan is the contract, never an exception."""
    K, T = m.S, 0.25
    lo, hi = gk.no_arb_bounds(m.S, K, T, m.rd, m.rf, +1)
    assert math.isnan(gk.implied_vol(lo - 0.05 * m.S, m.S, K, T, m.rd, m.rf, +1))
    assert math.isnan(gk.implied_vol(hi * 1.05, m.S, K, T, m.rd, m.rf, +1))
    assert math.isnan(gk.implied_vol(float("nan"), m.S, K, T, m.rd, m.rf, +1))


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_implied_vol_at_intrinsic_is_zero_vol(m):
    """Zero time value must return 0.0 vol, not nan and not an exception."""
    T = 0.25
    K = 0.8 * m.F(T)
    lo, _ = gk.no_arb_bounds(m.S, K, T, m.rd, m.rf, +1)
    assert gk.implied_vol(lo, m.S, K, T, m.rd, m.rf, +1) == 0.0


# --------------------------------------------------------------------------- #
# delta conventions
# --------------------------------------------------------------------------- #
def _delta_cases():
    for m in MARKETS:
        for conv in gk.DELTA_CONVENTIONS:
            for T in (1 / 52, 0.25, 1.0, 2.0):
                for sigma in (0.05, 0.10, 0.25):
                    for d in (0.05, 0.10, 0.25, 0.40):
                        for cp in CPS:
                            yield pytest.param(
                                m, conv, T, sigma, d, cp,
                                id=f"{m.pair}-{conv}-T{T:.3f}-v{sigma:g}-"
                                   f"d{d:g}-{'C' if cp > 0 else 'P'}")


@pytest.mark.parametrize("m,conv,T,sigma,d,cp", list(_delta_cases()))
def test_strike_delta_round_trip_in_every_convention(m, conv, T, sigma, d, cp):
    """``delta_from_strike(strike_from_delta(d)) == d`` for all four conventions.

    This is the join between the smile and the pricer: if it drifts, every wing
    quote is applied at the wrong strike.
    """
    K = gk.strike_from_delta(d, m.S, T, m.rd, m.rf, sigma, cp, conv)
    if not np.isfinite(K):
        # only legitimate for a premium-adjusted call above its attainable maximum
        max_att = getattr(gk, "max_attainable_delta", None)
        assert conv.endswith("_pa") and cp > 0, f"{conv} {cp}: unexpected nan strike"
        if max_att is not None:
            assert d > max_att(m.S, T, m.rd, m.rf, sigma, cp, conv) - 1e-9
        pytest.skip(f"{d} delta unattainable for a {conv} call at T={T}, vol={sigma}")
    assert K > 0
    back = gk.delta_from_strike(K, m.S, T, m.rd, m.rf, sigma, cp, conv)
    assert abs(back) == pytest.approx(d, abs=1e-9)
    assert math.copysign(1.0, back) == math.copysign(1.0, cp)


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_signed_and_unsigned_delta_spellings_agree(m):
    """`-0.25` and `0.25` must give the same strike; the sign comes from ``cp``."""
    for conv in gk.DELTA_CONVENTIONS:
        for cp in CPS:
            a = gk.strike_from_delta(0.25, m.S, 0.25, m.rd, m.rf, 0.10, cp, conv)
            b = gk.strike_from_delta(-0.25, m.S, 0.25, m.rd, m.rf, 0.10, cp, conv)
            assert (a == b) or (np.isnan(a) and np.isnan(b))


@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_premium_adjusted_delta_is_below_plain_delta_for_a_call(m):
    """``Delta_pa = Delta_spot - V/S``: the premium is paid in base ccy, so the
    pa hedge is always smaller than the plain one for a call."""
    T, sigma = 0.25, 0.10
    for z in ZS:
        K = strike_at(m, T, sigma, z)
        plain = gk.delta_from_strike(K, m.S, T, m.rd, m.rf, sigma, +1, "spot")
        pa = gk.delta_from_strike(K, m.S, T, m.rd, m.rf, sigma, +1, "spot_pa")
        prem = gk.gk_price(m.S, K, T, m.rd, m.rf, sigma, +1)
        assert pa == pytest.approx(plain - prem / m.S, rel=1e-12, abs=1e-14)
        assert pa <= plain + 1e-15


@pytest.mark.parametrize("conv", ["", "SPOT ", "premium_adjusted", "spot-pa", None])
def test_unknown_delta_convention_raises(conv):
    """A typo'd convention must never be silently treated as ``spot``."""
    with pytest.raises((ValueError, TypeError, AttributeError)):
        gk.delta_from_strike(1.1, 1.1, 0.25, 0.04, 0.02, 0.10, +1, conv)
    with pytest.raises((ValueError, TypeError, AttributeError)):
        gk.strike_from_delta(0.25, 1.1, 0.25, 0.04, 0.02, 0.10, +1, conv)


# --------------------------------------------------------------------------- #
# vectorisation -- the ladder calls these ~1e5 times per refresh
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("m", MARKETS, ids=str)
def test_vector_and_scalar_paths_agree(m):
    """``gk_price`` broadcasts; the array path must equal the scalar path exactly."""
    T, sigma = 0.25, 0.12
    K = np.array([strike_at(m, T, sigma, z) for z in ZS])
    vec = gk.gk_price(m.S, K, T, m.rd, m.rf, sigma, +1)
    sca = np.array([gk.gk_price(m.S, float(k), T, m.rd, m.rf, sigma, +1) for k in K])
    assert isinstance(vec, np.ndarray)
    np.testing.assert_allclose(vec, sca, rtol=0, atol=0)
    assert isinstance(gk.gk_price(m.S, float(K[0]), T, m.rd, m.rf, sigma, +1), float)
