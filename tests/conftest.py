"""Shared fixtures and numerical helpers for the fxgamma regression suite.

Design rules for everything in ``tests/`` (QA owns this directory):

* **Public API only.**  Tests import from ``fxgamma.types``, ``fxgamma.conventions``,
  ``fxgamma.models`` and ``fxgamma.data`` -- the contract-frozen surface in
  ``docs/01_architecture.md``.  Nothing here touches a private helper whose name
  another agent is free to change.
* **Offline only.**  Market-data hosts are blocked in this environment; every test
  runs off ``fxgamma.data.get_provider("synthetic")`` or a hand-built quote set.
* **Fast by default.**  The whole suite is seconds.  Anything heavier is marked
  ``@pytest.mark.slow`` and can be excluded with ``-m "not slow"``.
* **Modules that do not exist yet** (``fxgamma.portfolio``, ``fxgamma.signals``,
  ``fxgamma.backtest``, ``fxgamma.store``, ``app``) are guarded with
  ``pytest.importorskip`` so the suite stays green and reports them as skipped.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pytest

from fxgamma import conventions as cv
from fxgamma.models import gk

# --------------------------------------------------------------------------- #
# pytest configuration
# --------------------------------------------------------------------------- #
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: heavier numerical sweeps; deselect with -m 'not slow'")
    config.addinivalue_line("markers", "regression: protects a bug that already shipped once")
    config.addinivalue_line("markers", "contract: asserts a frozen clause of docs/01_architecture.md")


# --------------------------------------------------------------------------- #
# market cases -- the parameterisation axis used by the numerical harness
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Market:
    """One (pair, spot, rates) triple to parameterise pricing tests over."""
    pair: str
    S: float
    rd: float
    rf: float

    @property
    def convention(self) -> str:
        return cv.pair_spec(self.pair).delta_convention

    def F(self, T: float) -> float:
        return self.S * math.exp((self.rd - self.rf) * T)

    def __str__(self) -> str:            # pragma: no cover - test ids only
        return self.pair


#: Deliberately spans a 1-handle pair, a 100-handle JPY pair (premium-adjusted),
#: a sub-1 pair and an inverted-quote pair, with realistic and *asymmetric* rate
#: differentials (rd == rf hides an entire class of forward bugs).
MARKETS: tuple[Market, ...] = (
    Market("EURUSD", 1.1650, 0.0400, 0.0200),     # spot convention
    Market("USDJPY", 147.50, 0.0050, 0.0450),     # spot_pa, JPY pips, rd < rf
    Market("AUDUSD", 0.6500, 0.0400, 0.0380),     # sub-1 handle
    Market("USDCHF", 0.8000, 0.0010, 0.0450),     # spot_pa, near-zero domestic rate
)

TENORS: tuple[float, ...] = (1 / 365, 1 / 52, 1 / 12, 0.25, 1.0, 2.0)
VOLS: tuple[float, ...] = (0.04, 0.08, 0.15, 0.30)
#: strike offsets in ATM standard deviations (``K = F exp(z sigma sqrt(T))``)
ZS: tuple[float, ...] = (-2.5, -1.0, 0.0, 1.0, 2.5)
CPS: tuple[int, ...] = (+1, -1)

PA_PAIRS: tuple[str, ...] = tuple(
    p for p, s in cv.PAIRS.items() if s.delta_convention.endswith("_pa"))
G3_G10: tuple[str, ...] = tuple(dict.fromkeys(list(cv.G3) + list(cv.G10_PAIRS)))


def strike_at(m: Market, T: float, sigma: float, z: float) -> float:
    """Strike ``z`` ATM standard deviations from the forward."""
    return m.F(T) * math.exp(z * sigma * math.sqrt(T))


# --------------------------------------------------------------------------- #
# central finite differences
# --------------------------------------------------------------------------- #
#: Step sizes are scaled to the option's own natural scale (one hundredth of an
#: ATM standard deviation in S and K, a thousandth of T).  Fixed absolute steps
#: fail either on a 147-handle JPY pair or on a 1-day option; these hold every
#: Greek to better than 5e-5 relative across the whole grid.
_H_S = 0.005          # x S x sigma sqrt(T)
_H_K = 0.005          # x K x sigma sqrt(T)
_H_VOL = 1e-4         # x sigma
_H_T = 1e-3           # x T
_H_R = 1e-6           # absolute


def fd_greeks(S: float, K: float, T: float, rd: float, rf: float,
              sigma: float, cp: int) -> dict[str, float]:
    """Central-difference Greeks in the *same desk units* as :func:`gk.gk_greeks`.

    ``vanna`` and ``volga`` differentiate the analytic vega (a first derivative
    each); every other Greek differentiates :func:`gk.gk_price` directly, so the
    analytic formulas are checked against prices, not against themselves.
    """
    p = gk.gk_price
    hS = _H_S * S * sigma * math.sqrt(T)
    hK = _H_K * K * sigma * math.sqrt(T)
    hv = _H_VOL * sigma
    hT = _H_T * T
    hr = _H_R

    def vega_raw(S_: float = S, sg_: float = sigma) -> float:
        """Analytic vega per 1.00 of sigma (undo the per-vol-point scaling)."""
        return gk.gk_greeks(S_, K, T, rd, rf, sg_, cp).vega / 0.01

    return {
        "delta_base": (p(S + hS, K, T, rd, rf, sigma, cp)
                       - p(S - hS, K, T, rd, rf, sigma, cp)) / (2 * hS),
        "gamma": (p(S + hS, K, T, rd, rf, sigma, cp) - 2 * p(S, K, T, rd, rf, sigma, cp)
                  + p(S - hS, K, T, rd, rf, sigma, cp)) / (hS * hS),
        "vega": (p(S, K, T, rd, rf, sigma + hv, cp)
                 - p(S, K, T, rd, rf, sigma - hv, cp)) / (2 * hv) * 0.01,
        "theta": -(p(S, K, T + hT, rd, rf, sigma, cp)
                   - p(S, K, T - hT, rd, rf, sigma, cp)) / (2 * hT) / 365.0,
        "rho_d": (p(S, K, T, rd + hr, rf, sigma, cp)
                  - p(S, K, T, rd - hr, rf, sigma, cp)) / (2 * hr) * 0.01,
        "rho_f": (p(S, K, T, rd, rf + hr, sigma, cp)
                  - p(S, K, T, rd, rf - hr, sigma, cp)) / (2 * hr) * 0.01,
        "vanna": (vega_raw(S_=S + hS) - vega_raw(S_=S - hS)) / (2 * hS) * 0.01,
        "volga": (vega_raw(sg_=sigma + hv) - vega_raw(sg_=sigma - hv)) / (2 * hv) * 1e-4,
        "dual_delta": (p(S, K + hK, T, rd, rf, sigma, cp)
                       - p(S, K - hK, T, rd, rf, sigma, cp)) / (2 * hK),
    }


def rel_err(a: float, b: float, floor: float = 1e-14) -> float:
    """Relative error between analytic ``a`` and finite-difference ``b``."""
    return abs(a - b) / max(abs(b), abs(a), floor)


def greek_mismatch(analytic: dict[str, float], fd: dict[str, float],
                   names: tuple[str, ...], *, rtol: float, atol: float
                   ) -> dict[str, tuple[float, float, float]]:
    """Greeks where ``|analytic - fd| > rtol |fd| + atol``.

    ``atol`` exists only to stop a Greek that is *numerically zero* (a 5-day 4-vol
    2.5-sigma-OTM theta of 1.7e-9 quote ccy per unit notional -- 0.0017 on a 1mm
    ticket) from failing on finite-difference noise.  It is set several orders of
    magnitude below anything that could move a hedge.
    """
    out = {}
    for g in names:
        a, f = analytic[g], fd[g]
        if abs(a - f) > rtol * max(abs(a), abs(f)) + atol:
            out[g] = (a, f, rel_err(a, f))
    return out


# --------------------------------------------------------------------------- #
# data-layer fixtures -- one synthetic provider for the whole session
# --------------------------------------------------------------------------- #
ASOF = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def provider():
    """Deterministic offline provider (market-data hosts are blocked here)."""
    from fxgamma.data import get_provider
    return get_provider("synthetic", asof=ASOF)


@pytest.fixture(scope="session")
def snapshot(provider):
    """A fully badged :class:`MarketSnapshot` over every pair in ``conventions.PAIRS``."""
    return provider.snapshot(list(cv.PAIRS), asof=ASOF)


@pytest.fixture(scope="session")
def quotes_3pt():
    """A plausible hand-built broker quote set: two tenors, skewed, positive BF."""
    from fxgamma.models.surface import SmileQuotes
    return [
        SmileQuotes(T=0.25, atm=0.0900, rr25=-0.0100, bf25=0.0025,
                    rr10=-0.0190, bf10=0.0085, tenor="3M"),
        SmileQuotes(T=1.00, atm=0.0975, rr25=-0.0125, bf25=0.0032,
                    rr10=-0.0235, bf10=0.0105, tenor="1Y"),
    ]


@pytest.fixture(scope="session")
def np_seed():
    """A fixed numpy Generator for any test that needs randomness."""
    return np.random.default_rng(20260907)
