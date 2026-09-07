"""Smile / surface validation: repricing, calibration recovery and no-arbitrage.

The surface is the single input every downstream number is computed off (v1.2 T-1),
so the tests here are deliberately strict about the three things that make a surface
usable on a desk:

1. **It reprices its own inputs.**  A vanna-volga surface built from ATM / 25RR / 25BF
   must return those quotes back, on the pair's *own* delta convention.  The usual
   failure is placing the 25d strike with the ATM vol instead of solving the
   strike/vol fixed point -- it looks fine and it silently changes the risk reversal.
2. **A calibration recovers the parameters that generated the data.**
3. **It admits no arbitrage** -- non-negative Breeden-Litzenberger density
   integrating to ~1, and non-decreasing total variance in time.
"""
from __future__ import annotations

import math
import pickle

import numpy as np
import pytest

from fxgamma import conventions as cv
from fxgamma.models import gk, sabr, smile
from fxgamma.models.surface import (FlatSurface, SABRSurface, SmileQuotes,
                                    VannaVolgaSurface, build_surface)

from conftest import ASOF, G3_G10

REPRICE_PAIRS = ("EURUSD", "USDJPY", "EURGBP", "USDCHF")   # spot and spot_pa, 1 and 100 handle


def _spot(pair: str) -> float:
    return {"EURUSD": 1.1650, "USDJPY": 147.50, "EURGBP": 0.8600,
            "USDCHF": 0.8000, "EURJPY": 172.0}.get(pair, 1.10)


def _rates(pair: str) -> tuple[float, float]:
    """(rd, rf) with a deliberately asymmetric differential."""
    return {"EURUSD": (0.0400, 0.0200), "USDJPY": (0.0050, 0.0450),
            "EURGBP": (0.0425, 0.0200), "USDCHF": (0.0010, 0.0450)}.get(pair, (0.04, 0.02))


# =========================================================================== #
# vanna-volga reprices its own quotes
# =========================================================================== #
@pytest.mark.parametrize("pair", REPRICE_PAIRS)
@pytest.mark.parametrize("method", ["approx", "exact"])
def test_vanna_volga_reprices_its_input_quotes(pair, method, quotes_3pt):
    """ATM / 25RR / 25BF come back out of the surface to 1e-8 vol (1e-6 vol points)."""
    rd, rf = _rates(pair)
    surf = VannaVolgaSurface.from_smile_quotes(pair, ASOF, quotes_3pt, _spot(pair), rd, rf,
                                               method=method)
    for q in quotes_3pt:
        assert surf.atm(q.T) == pytest.approx(q.atm, abs=1e-8), f"{pair} ATM @ T={q.T}"
        assert surf.rr(q.T, 0.25) == pytest.approx(q.rr25, abs=1e-8), f"{pair} RR @ T={q.T}"
        assert surf.bf(q.T, 0.25) == pytest.approx(q.bf25, abs=1e-8), f"{pair} BF @ T={q.T}"


@pytest.mark.parametrize("pair", REPRICE_PAIRS)
def test_vanna_volga_reprices_the_10_delta_wings_when_quoted(pair, quotes_3pt):
    """With 10d quotes supplied all five pillars must reprice, not just the three."""
    rd, rf = _rates(pair)
    surf = VannaVolgaSurface.from_smile_quotes(pair, ASOF, quotes_3pt, _spot(pair), rd, rf)
    for q in quotes_3pt:
        if q.rr10 is None:
            continue
        assert surf.rr(q.T, 0.10) == pytest.approx(q.rr10, abs=1e-7)
        assert surf.bf(q.T, 0.10) == pytest.approx(q.bf10, abs=1e-7)


@pytest.mark.parametrize("pair", REPRICE_PAIRS)
def test_surface_uses_the_pairs_delta_convention(pair, quotes_3pt):
    """A ``spot_pa`` pair must place its wings on the premium-adjusted convention.

    Reading the 25d strike off the plain-spot convention on USDJPY moves the wing
    by roughly the option premium -- a wrong vol at a wrong strike.
    """
    rd, rf = _rates(pair)
    surf = build_surface(pair, ASOF, list(quotes_3pt), _spot(pair), rd, rf)
    assert surf.delta_convention == cv.pair_spec(pair).delta_convention
    T = quotes_3pt[0].T
    K = surf.strike_by_delta(0.25, T, +1)
    d = gk.delta_from_strike(K, surf.spot, T, rd, rf, surf.vol(K, T), +1,
                             surf.delta_convention)
    assert abs(d) == pytest.approx(0.25, abs=1e-8)


@pytest.mark.parametrize("pair", REPRICE_PAIRS)
def test_atm_strike_is_the_delta_neutral_straddle(pair, quotes_3pt):
    """Call delta + put delta = 0 at the ATM strike, in the pair's own convention."""
    rd, rf = _rates(pair)
    surf = build_surface(pair, ASOF, list(quotes_3pt), _spot(pair), rd, rf)
    T = quotes_3pt[0].T
    K = surf.atm_strike(T)
    v = surf.vol(K, T)
    conv = surf.delta_convention
    dc = gk.delta_from_strike(K, surf.spot, T, rd, rf, v, +1, conv)
    dp = gk.delta_from_strike(K, surf.spot, T, rd, rf, v, -1, conv)
    assert dc + dp == pytest.approx(0.0, abs=1e-9)


# =========================================================================== #
# term structure
# =========================================================================== #
def test_atm_interpolates_in_total_variance_and_extrapolates_flat(quotes_3pt):
    """Between pillars ATM is linear in ``sigma^2 T``; outside it is flat in vol.

    Linear-in-vol interpolation, or linear extrapolation of the wings, is how a
    surface manufactures calendar arbitrage out of two clean quotes.
    """
    surf = VannaVolgaSurface.from_smile_quotes("EURUSD", ASOF, quotes_3pt, 1.165, 0.04, 0.02)
    q0, q1 = quotes_3pt[0], quotes_3pt[1]
    T = 0.5 * (q0.T + q1.T)
    w = np.interp(T, [q0.T, q1.T], [q0.atm ** 2 * q0.T, q1.atm ** 2 * q1.T])
    assert surf.atm(T) == pytest.approx(math.sqrt(w / T), abs=1e-9)
    assert surf.atm(q1.T * 5) == pytest.approx(q1.atm, abs=1e-9)
    assert surf.atm(q0.T / 5) == pytest.approx(q0.atm, abs=1e-9)


@pytest.mark.parametrize("pair", REPRICE_PAIRS)
def test_total_variance_is_non_decreasing_in_time(pair, quotes_3pt):
    """Calendar arbitrage check: ``w = sigma(k)^2 T`` must not fall as T rises."""
    rd, rf = _rates(pair)
    surf = build_surface(pair, ASOF, list(quotes_3pt), _spot(pair), rd, rf)
    ks = np.linspace(-0.5, 0.5, 41)
    Ts = np.array([0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0])
    prev = None
    for T in Ts:
        K = surf.forward(T) * np.exp(ks)
        w = np.asarray(surf.slice(T, K), float) ** 2 * T
        if prev is not None:
            assert np.all(w - prev > -1e-10), f"{pair}: calendar arbitrage at T={T}"
        prev = w


# =========================================================================== #
# no-arbitrage diagnostics
# =========================================================================== #
@pytest.mark.parametrize("pair", REPRICE_PAIRS)
@pytest.mark.parametrize("T", [0.25, 1.0])
def test_breeden_litzenberger_density_is_a_density(pair, T, quotes_3pt):
    """Non-negative everywhere and integrating to ~1: the definition of no butterfly
    arbitrage, and the one smile check a desk actually runs."""
    rd, rf = _rates(pair)
    surf = build_surface(pair, ASOF, list(quotes_3pt), _spot(pair), rd, rf)
    rep = surf.density(T, n=601, n_std=5.0)
    assert rep.n_negative == 0, f"{pair} T={T}: {rep.n_negative} negative density points"
    assert rep.min_density > -1e-8
    assert np.all(np.isfinite(rep.density))
    assert rep.integral == pytest.approx(1.0, abs=5e-3), rep.note


@pytest.mark.parametrize("pair", REPRICE_PAIRS)
def test_surface_diagnostics_report_no_violations(pair, quotes_3pt):
    """The surface's own arbitrage scan is what the /surface page badges off."""
    rd, rf = _rates(pair)
    surf = build_surface(pair, ASOF, list(quotes_3pt), _spot(pair), rd, rf)
    d = surf.diagnostics()
    assert d.ok, d.summary()


def test_density_of_a_flat_surface_is_lognormal():
    """Control case: a constant vol must produce the exact Black-Scholes density."""
    S, T, rd, rf, v = 1.165, 0.5, 0.04, 0.02, 0.10
    surf = FlatSurface("EURUSD", ASOF, S, rd, rf, v)
    rep = surf.density(T, n=801, n_std=6.0)
    F = S * math.exp((rd - rf) * T)
    K = rep.strikes
    d2 = (np.log(F / K) - 0.5 * v * v * T) / (v * math.sqrt(T))
    exact = np.exp(-0.5 * d2 ** 2) / (K * v * math.sqrt(T) * math.sqrt(2 * math.pi))
    np.testing.assert_allclose(rep.density, exact, rtol=2e-5, atol=1e-8)
    assert rep.integral == pytest.approx(1.0, abs=1e-3)


# =========================================================================== #
# SABR
# =========================================================================== #
@pytest.mark.parametrize("alpha,rho,nu", [
    (0.10, -0.25, 0.45),
    (0.08, +0.15, 0.30),
    (0.20, -0.55, 0.90),
])
def test_sabr_recovers_the_parameters_that_generated_the_smile(alpha, rho, nu):
    """Round trip: params -> vols -> calibrate -> params, to 1e-6."""
    F, T, beta = 1.1650, 1.0, 1.0
    K = F * np.exp(np.linspace(-0.35, 0.35, 7))
    V = sabr.sabr_vol(K, F, T, alpha, beta, rho, nu)
    fit = sabr.calibrate_sabr(F, T, K, V, beta=beta)
    assert fit.success
    assert fit.rmse_vol < 1e-8
    assert fit.params.alpha == pytest.approx(alpha, rel=1e-5)
    assert fit.params.rho == pytest.approx(rho, abs=1e-5)
    assert fit.params.nu == pytest.approx(nu, rel=1e-5)


def test_sabr_pinned_atm_fits_the_atm_quote_exactly():
    """`atm_vol=` implies alpha, so the ATM pillar must be exact by construction."""
    F, T = 147.5, 0.5
    K = F * np.exp(np.linspace(-0.2, 0.2, 5))
    V = sabr.sabr_vol(K, F, T, 0.09, 1.0, -0.3, 0.5)
    atm = float(sabr.sabr_vol(F, F, T, 0.09, 1.0, -0.3, 0.5))
    fit = sabr.calibrate_sabr(F, T, K, V, beta=1.0, atm_vol=atm)
    assert fit.params.atm() == pytest.approx(atm, abs=1e-10)


def test_sabr_calibration_is_deterministic():
    """No random restarts: the same quotes must always give the same parameters."""
    F, T = 1.165, 0.75
    K = F * np.exp(np.linspace(-0.3, 0.3, 5))
    V = sabr.sabr_vol(K, F, T, 0.11, 1.0, -0.2, 0.6)
    a = sabr.calibrate_sabr(F, T, K, V, beta=1.0).params
    b = sabr.calibrate_sabr(F, T, K, V, beta=1.0).params
    assert (a.alpha, a.rho, a.nu) == (b.alpha, b.rho, b.nu)


def test_sabr_calibration_needs_at_least_three_points():
    with pytest.raises(ValueError):
        sabr.calibrate_sabr(1.1, 1.0, np.array([1.0, 1.1]), np.array([0.1, 0.1]))


@pytest.mark.parametrize("pair", ["EURUSD", "USDJPY"])
def test_sabr_surface_is_close_to_its_broker_quotes(pair, quotes_3pt):
    """A 3-parameter fit cannot be exact on 3 pillars *and* pinned at the money;
    it must still land inside half a basis point of vol."""
    rd, rf = _rates(pair)
    surf = SABRSurface.calibrate(pair, ASOF, quotes_3pt, _spot(pair), rd, rf)
    for q in quotes_3pt:
        assert surf.atm(q.T) == pytest.approx(q.atm, abs=5e-4)
        assert surf.rr(q.T) == pytest.approx(q.rr25, abs=5e-4)
        assert surf.bf(q.T) == pytest.approx(q.bf25, abs=5e-4)


# =========================================================================== #
# factory contract (arch s8) and pickling
# =========================================================================== #
@pytest.mark.parametrize("method", ["vanna_volga", "sabr", "interp", "flat"])
def test_build_surface_returns_a_working_volsurface(method, quotes_3pt):
    """Every advertised method satisfies the frozen ``VolSurface`` protocol."""
    surf = build_surface("EURUSD", ASOF, list(quotes_3pt), 1.165, 0.04, 0.02, method=method)
    T = quotes_3pt[0].T
    assert 0.0 < surf.vol(1.20, T) < 3.0
    assert 0.0 < surf.atm(T) < 3.0
    assert np.isfinite(surf.rr(T)) and np.isfinite(surf.bf(T))
    sl = surf.slice(T, np.array([1.05, 1.10, 1.165, 1.25]))
    assert sl.shape == (4,) and np.all(np.isfinite(sl))
    assert np.isfinite(surf.vol_by_delta(0.25, T, +1))


def test_build_surface_rejects_an_unknown_method(quotes_3pt):
    with pytest.raises(ValueError):
        build_surface("EURUSD", ASOF, list(quotes_3pt), 1.165, 0.04, 0.02, method="magic")


def test_build_surface_rejects_an_empty_quote_list():
    with pytest.raises(ValueError):
        build_surface("EURUSD", ASOF, [], 1.165, 0.04, 0.02)


@pytest.mark.parametrize("method", ["vanna_volga", "sabr", "flat"])
def test_surfaces_are_picklable_and_survive_the_round_trip(method, quotes_3pt):
    """The snapshot goes into a ``dcc.Store`` and the backtest into a worker: a
    surface that cannot pickle, or that changes value after unpickling, breaks both."""
    surf = build_surface("USDJPY", ASOF, list(quotes_3pt), 147.5, 0.005, 0.045, method=method)
    again = pickle.loads(pickle.dumps(surf))
    T = quotes_3pt[0].T
    Ks = np.array([140.0, 147.5, 155.0])
    np.testing.assert_allclose(again.slice(T, Ks), surf.slice(T, Ks), rtol=0, atol=0)
    assert again.atm(T) == surf.atm(T)


def test_surface_is_cheap_to_re_evaluate(quotes_3pt):
    """Arch s4: "the ladder calls vol() ~1e5 times".  10k scalar calls must be quick."""
    import time
    surf = build_surface("EURUSD", ASOF, list(quotes_3pt), 1.165, 0.04, 0.02)
    Ks = np.linspace(1.0, 1.35, 10_000)
    t0 = time.perf_counter()
    out = surf.slice(0.25, Ks)
    dt = time.perf_counter() - t0
    assert np.all(np.isfinite(out))
    assert dt < 2.0, f"10k-point slice took {dt:.2f}s"


# =========================================================================== #
# smile algebra
# =========================================================================== #
def test_rr_bf_round_trip():
    """``(atm, rr, bf) -> (put, call) -> (rr, bf)`` is an exact algebraic identity."""
    atm, rr, bf = 0.09, -0.012, 0.0031
    p, c = smile.rr_bf_to_vols(atm, rr, bf)
    assert c - p == pytest.approx(rr, abs=1e-15)
    assert 0.5 * (c + p) - atm == pytest.approx(bf, abs=1e-15)
    assert smile.vols_to_rr_bf(p, c, atm) == pytest.approx((rr, bf), abs=1e-15)


@pytest.mark.parametrize("conv,expected", [
    ("spot", "dns"), ("fwd", "dns"), ("spot_pa", "dns_pa"), ("fwd_pa", "dns_pa"),
])
def test_atm_convention_follows_the_delta_convention(conv, expected):
    """The DNS sign flip: ``F exp(+w/2)`` vs ``F exp(-w/2)``.  Getting it backwards
    moves the ATM pillar by ``sigma^2 T`` and silently reprices the whole smile."""
    assert smile.atm_convention_for(conv) == expected
    S, T, rd, rf, v = 1.165, 1.0, 0.04, 0.02, 0.10
    K = smile.atm_strike(S, T, rd, rf, v, expected)
    F = S * math.exp((rd - rf) * T)
    sign = +1.0 if expected == "dns" else -1.0
    assert K == pytest.approx(F * math.exp(sign * 0.5 * v * v * T), rel=1e-14)
