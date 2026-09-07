"""Regression net for the four defects that already shipped once.

Each test here fails against the *old* behaviour and passes against the fix, so a
future refactor cannot quietly reintroduce the bug.  Sources: AMENDMENT v1.2
(T-2, T-3, T-4) in ``docs/01_architecture.md`` and the premium-adjusted call-delta
underflow found in ``fxgamma/models/gk.py``.

Bug 1  premium-adjusted call delta underflow  -- ``strike_from_delta`` -> nan for
       every ``spot_pa`` pair, killing the USDJPY/USDCHF/USDCAD/USDSEK/USDNOK
       surfaces outright.
Bug 2  ``Greeks.__add__`` summed intensive quantities (T-2 / trader W-3).
Bug 3  expired options never died (T-3 / trader W-2).
Bug 4  ``MarketSnapshot.rd_rf`` silently defaulted a missing rate to 0.0 (T-4 / W-4).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from fxgamma import conventions as cv
from fxgamma.models import gk
from fxgamma.types import Greeks, MarketSnapshot

from conftest import PA_PAIRS, G3_G10, ASOF

pytestmark = pytest.mark.regression


# =========================================================================== #
# Bug 1 -- premium-adjusted call delta underflow
# =========================================================================== #
#: The exact configuration that broke: USDJPY-ish spot, where the old `>=` guard
#: in the peak search returned the right-hand bracket edge (K = 553 for S = 147.5)
#: and poisoned the Brent bracket, so every pa call strike came back nan.
_HISTORIC_FAILURE = dict(S=147.50, T=0.25, rd=0.005, rf=0.045, sigma=0.09)


def _spot_for(pair: str) -> float:
    """A realistic spot level for `pair`, independent of the data layer."""
    spec = cv.pair_spec(pair)
    if "JPY" in (spec.base, spec.quote):
        return 147.50 if spec.base == "USD" else 172.0
    return {"USDCHF": 0.80, "USDCAD": 1.37, "USDSEK": 9.60, "USDNOK": 10.40}.get(pair, 1.10)


def test_pa_call_strike_is_finite_at_the_historic_failure_point():
    """S=147.5 / 3M / 9 vol returned nan before the fix; assert a sensible strike."""
    K = gk.strike_from_delta(0.25, cp=+1, convention="spot_pa", **_HISTORIC_FAILURE)
    assert np.isfinite(K), "premium-adjusted 25d call strike regressed to nan"
    F = _HISTORIC_FAILURE["S"] * math.exp(
        (_HISTORIC_FAILURE["rd"] - _HISTORIC_FAILURE["rf"]) * _HISTORIC_FAILURE["T"])
    # the 25d call sits above the forward and nowhere near the search bracket edge
    # (the old bug reported K = 553 for spot 147.5)
    assert F < K < 1.25 * F, f"25d pa call strike {K} is not near the forward {F}"
    back = abs(gk.delta_from_strike(K, _HISTORIC_FAILURE["S"], _HISTORIC_FAILURE["T"],
                                    _HISTORIC_FAILURE["rd"], _HISTORIC_FAILURE["rf"],
                                    _HISTORIC_FAILURE["sigma"], +1, "spot_pa"))
    assert abs(back - 0.25) < 1e-10


@pytest.mark.parametrize("pair", PA_PAIRS)
def test_pa_strike_from_delta_is_finite_across_the_grid(pair):
    """Every attainable premium-adjusted delta resolves to a finite, exact strike.

    Sweeps spot (x0.5 .. x2), vol (3% .. 35%), tenor (ON .. 2Y), delta and cp for
    every ``spot_pa`` pair in ``conventions.PAIRS``.  A delta above the pa call's
    attainable maximum is allowed to be nan -- that is the documented behaviour --
    but nothing else may be.
    """
    S0 = _spot_for(pair)
    rd, rf = 0.045, 0.005
    failures: list[str] = []
    max_att = getattr(gk, "max_attainable_delta", None)
    for S in (0.5 * S0, S0, 2.0 * S0):
        for T in (1 / 365, 7 / 365, 1 / 12, 0.25, 1.0, 2.0):
            for sigma in (0.03, 0.08, 0.15, 0.25, 0.35):
                for d in (0.10, 0.25, 0.35, 0.50):
                    for cp in (+1, -1):
                        K = gk.strike_from_delta(d, S, T, rd, rf, sigma, cp, "spot_pa")
                        if np.isfinite(K):
                            back = abs(gk.delta_from_strike(K, S, T, rd, rf, sigma,
                                                            cp, "spot_pa"))
                            if abs(back - d) > 1e-8:
                                failures.append(
                                    f"{pair} S={S} T={T} sig={sigma} d={d} cp={cp}: "
                                    f"round-trip {back}")
                            continue
                        attainable = 1.0 if max_att is None else max_att(
                            S, T, rd, rf, sigma, cp, "spot_pa")
                        if not (cp > 0 and d > attainable - 1e-12):
                            failures.append(
                                f"{pair} S={S} T={T} sig={sigma} d={d} cp={cp}: nan "
                                f"(max attainable {attainable})")
    assert not failures, "\n".join(failures[:20])


def test_pa_call_delta_keeps_its_sign_in_the_deep_wing():
    """``N(d2)`` must not underflow to exactly 0.0 -- that tie caused the bug.

    At ``d2 ~ -20`` the true ``N(d2)`` is 2.8e-89.  Computed as
    ``0.5 (1 + erf(d2/sqrt 2))`` it is exactly 0.0 from ``d2 ~ -8.3`` down, which
    turns the peak search's strict inequality into a tie at the bracket edge.
    """
    S, T, rd, rf, sigma = 147.5, 0.25, 0.005, 0.045, 0.09
    sqT = sigma * math.sqrt(T)
    F = S * math.exp((rd - rf) * T)
    K = F * math.exp(20.0 * sqT)            # d2 ~ -20
    d = float(gk.delta_from_strike(K, S, T, rd, rf, sigma, +1, "spot_pa"))
    assert d > 0.0, "far-OTM premium-adjusted call delta underflowed to exactly zero"
    assert d < 1e-70


@pytest.mark.parametrize("pair", G3_G10)
def test_every_g3_g10_surface_builds_and_prices_its_wings(pair, provider):
    """G3 + G10 surfaces build from the synthetic provider and yield finite wings.

    This is the *downstream* half of bug 1: a nan pa call strike does not raise, it
    propagates into ``vol_by_delta`` and empties the surface for the whole pair.
    """
    from fxgamma.models.surface import build_surface

    quotes = provider.smile_quotes(pair, ASOF)
    assert quotes, f"no smile quotes for {pair}"
    spot = provider.spot([pair])[pair]
    spec = cv.pair_spec(pair)
    rates = provider.rates([spec.base, spec.quote])
    surf = build_surface(pair, ASOF, list(quotes), spot,
                         rates[spec.quote], rates[spec.base])
    for q in quotes:
        assert np.isfinite(surf.atm(q.T)), f"{pair} @ {q.tenor or q.T}: ATM not finite"
        for cp in (+1, -1):
            v = surf.vol_by_delta(0.25, q.T, cp)
            assert np.isfinite(v), f"{pair} @ {q.tenor or q.T} cp={cp}: 25d vol is nan"
            assert 0.0 < v < 3.0


# =========================================================================== #
# Bug 2 -- Greeks.__add__ aggregating intensive quantities (v1.2 T-2)
# =========================================================================== #
_EXTENSIVE = ("pv", "delta_base", "gamma", "gamma_1pct", "vega", "theta",
              "rho_d", "rho_f", "vanna", "volga")
_INTENSIVE = ("delta_pct", "dual_delta")


def _sample_greeks(k: float = 1.0) -> Greeks:
    return Greeks(pv=1.0 * k, delta_base=2.0 * k, delta_pct=0.5, gamma=3.0 * k,
                  gamma_1pct=4.0 * k, vega=5.0 * k, theta=-6.0 * k, rho_d=7.0 * k,
                  rho_f=-8.0 * k, vanna=9.0 * k, volga=10.0 * k, dual_delta=-0.3)


@pytest.mark.contract
@pytest.mark.parametrize("field", _INTENSIVE)
def test_intensive_greeks_aggregate_to_nan(field):
    """`delta_pct` is per unit of notional, `dual_delta` is per *that* strike.

    Summing either across a book prints a confident, meaningless number on the
    aggregate card (trader review W-3).  v1.2 T-2 requires nan so the card reads n/a.
    """
    total = _sample_greeks() + _sample_greeks(2.0)
    assert math.isnan(getattr(total, field)), f"{field} must not aggregate"


@pytest.mark.contract
@pytest.mark.parametrize("field", _EXTENSIVE)
def test_extensive_greeks_still_add(field):
    a, b = _sample_greeks(), _sample_greeks(2.0)
    got = getattr(a + b, field)
    assert got == pytest.approx(getattr(a, field) + getattr(b, field), rel=1e-15)


def test_adding_none_is_the_identity():
    """`price_book` folds over an optional accumulator; None must not poison it."""
    a = _sample_greeks()
    assert (a + None).pv == a.pv


def test_greeks_sum_builtin_aggregates_a_book():
    """``sum(per_position_greeks)`` is the obvious book aggregation.

    ``__radd__`` is defined precisely so this works, but the builtin ``sum`` seeds
    with the integer ``0`` and only ``None`` is special-cased, so the fold raises
    ``AttributeError: 'int' object has no attribute 'pv'``.
    """
    legs = [_sample_greeks(), _sample_greeks(2.0), _sample_greeks(3.0)]
    total = sum(legs)
    assert total.pv == pytest.approx(6.0)
    assert math.isnan(total.delta_pct)


# =========================================================================== #
# Bug 3 -- expired options never died (v1.2 T-3 / trader W-2)
# =========================================================================== #
@pytest.mark.contract
@pytest.mark.parametrize("cut", sorted(cv.CUTS))
def test_year_fraction_is_exactly_zero_past_the_cut(cut):
    """One second past the cut, and a week past it, T must be exactly 0.0.

    The old ``max(dt/(365*86400), 1/(365*24))`` returned one hour of time value
    *forever*: an option that expired last Tuesday still showed gamma, vega and
    theta and still moved the hedge.
    """
    expiry = date(2026, 6, 17)
    cut_utc = cv.expiry_datetime(expiry, cut)
    for delta in (timedelta(seconds=0), timedelta(seconds=1), timedelta(days=7)):
        asof = cut_utc + delta
        assert cv.year_fraction(asof, expiry, cut) == 0.0
        assert cv.is_expired(asof, expiry, cut) is True


def test_year_fraction_floor_still_applies_before_the_cut():
    """The 1-hour numerical floor is legitimate on the *live* side of the cut."""
    expiry = date(2026, 6, 17)
    asof = cv.expiry_datetime(expiry, "NY10") - timedelta(seconds=1)
    T = cv.year_fraction(asof, expiry, "NY10")
    assert T == pytest.approx(1.0 / (365 * 24))
    assert not cv.is_expired(asof, expiry, "NY10")


@pytest.mark.parametrize("cp,S,K,intrinsic", [
    (+1, 1.2000, 1.1000, 0.1000),      # ITM call
    (-1, 1.1000, 1.2000, 0.1000),      # ITM put
    (+1, 1.1000, 1.2000, 0.0),         # OTM call
    (-1, 1.2000, 1.1000, 0.0),         # OTM put
])
def test_expired_option_is_intrinsic_with_no_greeks(cp, S, K, intrinsic):
    """T = 0 prices to intrinsic, carries the exercise delta, and nothing else."""
    notional = 10_000_000.0
    g = gk.gk_greeks(S, K, 0.0, 0.04, 0.02, 0.10, cp, notional, +1)
    assert g.pv == pytest.approx(intrinsic * notional, abs=1e-6)
    assert g.gamma == 0.0
    assert g.gamma_1pct == 0.0
    assert g.vega == 0.0
    assert g.theta == 0.0
    expected_delta = notional * (cp if intrinsic > 0 else 0.0)
    assert g.delta_base == pytest.approx(expected_delta)


def test_expired_option_has_no_risk_through_the_conventions_clock():
    """End to end: an option whose cut passed contributes zero gamma/vega/theta."""
    expiry = date(2026, 6, 17)
    asof = cv.expiry_datetime(expiry, "NY10") + timedelta(hours=3)
    T = cv.year_fraction(asof, expiry, "NY10")
    g = gk.gk_greeks(1.20, 1.10, T, 0.04, 0.02, 0.10, +1, 1e6, +1)
    assert (g.gamma, g.vega, g.theta) == (0.0, 0.0, 0.0)
    assert g.pv == pytest.approx(1e5)


# =========================================================================== #
# Bug 4 -- rd_rf silent zero-rate default (v1.2 T-4 / trader W-4)
# =========================================================================== #
@pytest.mark.contract
def test_rd_rf_raises_and_names_the_missing_currency():
    """A missing rate must raise, not collapse the forward onto spot.

    Assuming rd = 0 on USDJPY 1Y puts the forward about four big figures out, and
    every delta-placed strike with it (arch section 7: never silently substitute).
    """
    snap = MarketSnapshot(asof=ASOF, spot={"USDJPY": 147.5}, rates={"USD": 0.045})
    with pytest.raises(KeyError) as exc:
        snap.rd_rf("USDJPY", cv.PAIRS)
    assert "JPY" in str(exc.value), "the exception must name the missing currency"


def test_rd_rf_returns_domestic_then_foreign_when_both_present():
    snap = MarketSnapshot(asof=ASOF, spot={"USDJPY": 147.5},
                          rates={"USD": 0.045, "JPY": 0.005})
    rd, rf = snap.rd_rf("USDJPY", cv.PAIRS)
    assert (rd, rf) == (0.005, 0.045), "USDJPY: domestic is JPY, foreign is USD"


def test_rd_rf_is_not_fooled_by_a_zero_rate_that_is_really_present():
    """A genuine 0.0 rate is data; only an *absent* one is an error."""
    snap = MarketSnapshot(asof=ASOF, rates={"USD": 0.045, "JPY": 0.0})
    assert snap.rd_rf("USDJPY", cv.PAIRS) == (0.0, 0.045)
