"""Vol-surface implementations and the frozen data <-> model factory.

This module owns the **one boundary** between the data layer and the model layer
(``docs/01_architecture.md`` s8): a provider produces ``list[SmileQuotes]`` per
pair and calls :func:`build_surface`.  Nothing else crosses.

Three surfaces are offered, all implementing the frozen ``VolSurface`` protocol
(``vol``, ``vol_by_delta``, ``atm``, ``rr``, ``bf``, ``slice``) and all picklable
(plain dataclasses of floats and numpy arrays; evaluation caches are dropped on
pickling):

======================  =================================================================
:class:`VannaVolgaSurface`  FX market standard.  Exact at ATM/25RR/25BF (and 10d when
                        quoted).  No calibration -> instant rebuild.  **Default.**
:class:`SABRSurface`    Hagan lognormal SABR calibrated per tenor.  Use for smile
                        *dynamics* (backbone) and for wings beyond 10 delta.
:class:`InterpolatedSurface`  SVI-per-slice fit to a listed option chain (ETF options).
                        Use when the input is real listed quotes rather than broker
                        ATM/RR/BF strings.
======================  =================================================================

Term structure
--------------
Both quote-driven surfaces interpolate **ATM in total variance** (``w = sigma^2 T``
linear in ``T``), which is the only interpolation that cannot manufacture calendar
arbitrage on the ATM line, and **RR / BF linearly in ``T``** with flat
extrapolation beyond the quoted grid.  A smile is then constructed at the target
``T`` from the interpolated quotes -- interpolating *quotes* rather than
*parameters* keeps the wing behaviour anchored to something a trader can read.

References
----------
* Clark, I. (2011), *FX Option Pricing*, ch. 3.
* Castagna, A. and Mercurio, F. (2007), "The Vanna-Volga Method for Implied
  Volatilities", *Risk*.
* Hagan et al. (2002), "Managing Smile Risk", *Wilmott*.
* Reiswich, D. and Wystup, U. (2010), "A Guide to FX Options Quoting Conventions".
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Sequence

import numpy as np

from ..types import VolSurface
from . import gk, sabr, smile
from .interp import InterpolatedSurface, SurfaceDiagnostics
from .vanna_volga import VannaVolgaSmile, market_to_smile_bf

__all__ = ["SmileQuotes", "build_surface", "VannaVolgaSurface", "SABRSurface",
           "InterpolatedSurface", "FlatSurface", "METHODS", "VolSurface"]

METHODS: Final[tuple[str, ...]] = ("vanna_volga", "sabr", "interp", "flat")


# --------------------------------------------------------------------------- #
# the frozen quote container (contract s8)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SmileQuotes:
    """Broker-style quotes for one tenor.  All vols/RR/BF are **decimals**.

    ``atm`` is on the **delta-neutral-straddle** convention (FX standard), and
    ``bf25`` / ``bf10`` are **smile** butterflies:
    ``bf = (sigma_call + sigma_put)/2 - sigma_atm``.  If your source quotes
    *market* strangles, convert first with
    :func:`~fxgamma.models.vanna_volga.market_to_smile_bf`, or pass
    ``bf_convention="market"`` to the surface constructors, which does it for you.
    """
    T: float
    atm: float
    rr25: float
    bf25: float
    rr10: float | None = None
    bf10: float | None = None
    tenor: str = ""


def _delta_convention(pair: str) -> str:
    """Look up the pair's delta convention, defaulting to ``"spot"`` for unknowns."""
    try:
        from ..conventions import pair_spec
        return pair_spec(pair).delta_convention
    except Exception:
        return "spot"


def _sorted_quotes(quotes: Sequence[SmileQuotes]) -> list[SmileQuotes]:
    q = [x for x in quotes if np.isfinite(x.T) and x.T > gk.T_MIN and np.isfinite(x.atm)]
    if not q:
        raise ValueError("build_surface needs at least one finite SmileQuotes")
    return sorted(q, key=lambda x: x.T)


def _interp_quotes(qs: list[SmileQuotes], T: float) -> tuple[float, float, float,
                                                             float | None, float | None]:
    """Interpolate ``(atm, rr25, bf25, rr10, bf10)`` to expiry ``T``.

    ATM in total variance (linear in ``T``); RR/BF linearly in ``T``.  Flat-vol /
    flat-quote extrapolation outside the quoted grid -- never linear extrapolation,
    which would send the wings to nonsense at 5Y from a 1Y-max quote set.
    """
    Ts = np.array([q.T for q in qs], float)
    T = max(float(T), gk.T_MIN)
    if Ts.size == 1:
        q = qs[0]
        return q.atm, q.rr25, q.bf25, q.rr10, q.bf10

    w = np.array([q.atm ** 2 * q.T for q in qs], float)
    if T <= Ts[0]:
        atm = float(np.sqrt(w[0] / Ts[0]))
    elif T >= Ts[-1]:
        atm = float(np.sqrt(w[-1] / Ts[-1]))
    else:
        atm = float(np.sqrt(np.interp(T, Ts, w) / T))

    def lin(vals: list[float | None]) -> float | None:
        if any(v is None for v in vals):
            return None
        arr = np.array([float(v) for v in vals], float)  # type: ignore[arg-type]
        return float(np.interp(T, Ts, arr))              # np.interp clamps -> flat wings

    return (atm, float(lin([q.rr25 for q in qs]) or 0.0),
            float(lin([q.bf25 for q in qs]) or 0.0),
            lin([q.rr10 for q in qs]), lin([q.bf10 for q in qs]))


# --------------------------------------------------------------------------- #
# vanna-volga surface
# --------------------------------------------------------------------------- #
@dataclass
class VannaVolgaSurface(smile.SmileSurfaceMixin):
    """Vanna-volga vol surface built from broker ATM / RR / BF quotes.

    Smiles at the *quoted* tenors are built once at construction; smiles at other
    tenors are built lazily from interpolated quotes and memoised, so the spot
    ladder (which hits a handful of distinct expiries ~1e5 times) pays the
    root-solve cost once per expiry.  The memo is dropped on pickling.
    """
    pair: str
    asof: datetime
    spot: float
    rd: float
    rf: float
    quotes: tuple[SmileQuotes, ...]
    delta_convention: str = "spot"
    method: str = "approx"
    bf_convention: str = "smile"
    lee_cap: float = 2.0
    _cache: dict[float, VannaVolgaSmile] = field(default_factory=dict, repr=False,
                                                 compare=False)

    # -- construction ----------------------------------------------------- #
    @classmethod
    def from_quotes(cls, atm: Any, rr25: Any, bf25: Any, tenors: Any, *,
                    spot: float, rd: float = 0.0, rf: float = 0.0,
                    pair: str = "", asof: datetime | None = None,
                    rr10: Any = None, bf10: Any = None,
                    delta_convention: str | None = None,
                    method: str = "approx", bf_convention: str = "smile",
                    lee_cap: float = 2.0) -> "VannaVolgaSurface":
        """Build from parallel arrays of quotes (contract s4 spelling).

        ``atm``, ``rr25``, ``bf25`` (and optionally ``rr10``, ``bf10``) are sequences
        aligned with ``tenors`` (years, or broker strings like ``"3M"`` which are
        resolved through ``conventions.tenor_years``).
        """
        Ts = [_years(t) for t in np.atleast_1d(np.asarray(tenors, dtype=object))]
        a = np.atleast_1d(np.asarray(atm, float))
        r = np.atleast_1d(np.asarray(rr25, float))
        b = np.atleast_1d(np.asarray(bf25, float))
        r10 = None if rr10 is None else np.atleast_1d(np.asarray(rr10, float))
        b10 = None if bf10 is None else np.atleast_1d(np.asarray(bf10, float))
        qs = [SmileQuotes(Ts[i], float(a[i]), float(r[i]), float(b[i]),
                          None if r10 is None else float(r10[i]),
                          None if b10 is None else float(b10[i]))
              for i in range(len(Ts))]
        return cls.from_smile_quotes(pair, asof or datetime.now(), qs, spot, rd, rf,
                                     delta_convention=delta_convention, method=method,
                                     bf_convention=bf_convention,
                                     lee_cap=lee_cap)

    @classmethod
    def from_smile_quotes(cls, pair: str, asof: datetime, quotes: Sequence[SmileQuotes],
                          spot: float, rd: float, rf: float, *,
                          delta_convention: str | None = None,
                          method: str = "approx", bf_convention: str = "smile",
                          lee_cap: float = 2.0) -> "VannaVolgaSurface":
        """Preferred constructor: takes the contract's :class:`SmileQuotes` list."""
        qs = _sorted_quotes(quotes)
        dc = delta_convention or _delta_convention(pair)
        surf = cls(pair, asof, float(spot), float(rd), float(rf), tuple(qs), dc,
                   method, bf_convention, float(lee_cap))
        for q in qs:                       # warm the quoted tenors
            surf._smile(q.T)
        return surf

    # -- smiles ----------------------------------------------------------- #
    def _smile(self, T: float) -> VannaVolgaSmile:
        """Memoised single-tenor smile at expiry ``T``."""
        key = round(float(T), 10)
        sm = self._cache.get(key)
        if sm is None:
            atm, rr25, bf25, rr10, bf10 = _interp_quotes(list(self.quotes), key)
            sm = VannaVolgaSmile.from_quotes(
                self.spot, key, self.rd, self.rf, atm, rr25, bf25, rr10, bf10,
                delta_convention=self.delta_convention, method=self.method,
                bf_convention=self.bf_convention, lee_cap=self.lee_cap)
            self._cache[key] = sm
        return sm

    def _vol_impl(self, K: Any, T: float) -> Any:
        return self._smile(float(T)).vol(K)

    def atm(self, T: float) -> float:
        """Quoted (interpolated) ATM vol -- exact at the quoted tenors by construction.

        Overrides the mixin's fixed-point search because for a VV smile the ATM
        pillar *is* an input, so returning the quote is both exact and free.
        """
        return float(self._smile(float(T)).atm_vol)

    def atm_strike(self, T: float) -> float:
        """The DNS strike used as the VV middle pillar."""
        return float(self._smile(float(T)).atm_strike_)

    @property
    def tenors(self) -> tuple[float, ...]:
        """Quoted expiries in years, ascending."""
        return tuple(q.T for q in self.quotes)

    def pillars(self, T: float) -> tuple[np.ndarray, np.ndarray]:
        """``(strikes, vols)`` of the smile pillars at ``T`` -- for calibration plots."""
        sm = self._smile(float(T))
        return sm.strikes.copy(), sm.vols.copy()

    def diagnostics(self, tenors: Sequence[float] | None = None, *,
                    k_lo: float = -0.5, k_hi: float = 0.5, n: int = 121
                    ) -> SurfaceDiagnostics:
        """Butterfly (density) and calendar (total variance) arbitrage scan.

        Vanna-volga carries **no** arbitrage guarantee, so this is not optional
        housekeeping -- it is the check that decides whether a VV wing price is
        usable.  Butterfly is tested through the Breeden-Litzenberger density
        rather than an analytic ``g(k)`` because a VV smile has no closed form.
        """
        Ts = list(tenors) if tenors is not None else list(self.tenors)
        kk = np.linspace(k_lo, k_hi, int(n))
        bfly: list[dict[str, Any]] = []
        cal: list[dict[str, Any]] = []
        fits: list[dict[str, Any]] = []
        prev_w = None
        for T in Ts:
            F = self.forward(T)
            K = F * np.exp(kk)
            rep = smile.risk_neutral_density(lambda x, _T=T: self._vol_impl(x, _T),
                                             self.spot, T, self.rd, self.rf, n=401)
            if rep.n_negative:
                bfly.append({"T": T, "n_bad": rep.n_negative, "min_density": rep.min_density,
                             "integral": rep.integral})
            fits.append({"T": T, "kind": f"vv-{self.method}",
                         "n_quotes": int(self._smile(T).strikes.size),
                         "rmse_vol": 0.0, "density_integral": rep.integral})
            w = np.asarray(self.slice(T, K), float) ** 2 * T
            if prev_w is not None and (w - prev_w < -1e-12).any():
                d = w - prev_w
                cal.append({"T_lo": prev_T, "T_hi": T, "n_bad": int((d < -1e-12).sum()),
                            "min_dw": float(np.min(d)), "k_at_min": float(kk[int(np.argmin(d))])})
            prev_w, prev_T = w, T
        return SurfaceDiagnostics(bfly, cal, fits, not bfly and not cal)

    # -- pickling: never carry the memo ----------------------------------- #
    def __getstate__(self) -> dict[str, Any]:
        st = self.__dict__.copy()
        st["_cache"] = {}
        return st

    def __setstate__(self, st: dict[str, Any]) -> None:
        self.__dict__.update(st)
        self._cache = {}


# --------------------------------------------------------------------------- #
# SABR surface
# --------------------------------------------------------------------------- #
@dataclass
class SABRSurface(smile.SmileSurfaceMixin):
    """Per-tenor Hagan SABR fits, joined in total variance across time.

    Parameters are **not** interpolated across tenors (parameter interpolation has
    no arbitrage meaning); instead total variance at fixed log-moneyness is
    interpolated linearly in ``T`` between the bracketing calibrated slices, and
    scaled proportionally to ``T`` outside them.
    """
    pair: str
    asof: datetime
    spot: float
    rd: float
    rf: float
    fits: tuple[sabr.SABRCalibration, ...]
    delta_convention: str = "spot"
    beta: float = 1.0
    method: str = "sabr"

    @classmethod
    def calibrate(cls, pair: str, asof: datetime, quotes: Sequence[SmileQuotes],
                  spot: float, rd: float, rf: float, *, beta: float = 1.0,
                  delta_convention: str | None = None,
                  bf_convention: str = "smile",
                  pin_atm: bool = True) -> "SABRSurface":
        """Calibrate one SABR slice per quoted tenor.

        The broker quotes are first turned into ``(strike, vol)`` pillars with the
        pair's delta convention (3 points, or 5 when 10d quotes are present), using
        :func:`~fxgamma.models.smile.delta_pillar_strikes` so every wing strike is
        placed with *its own* vol.  ``pin_atm=True`` implies ``alpha`` from the ATM
        quote so the ATM is fitted exactly and only ``(rho, nu)`` are searched.
        """
        qs = _sorted_quotes(quotes)
        dc = delta_convention or _delta_convention(pair)
        ac = smile.atm_convention_for(dc)
        fits: list[sabr.SABRCalibration] = []
        for q in qs:
            bf25, bf10 = q.bf25, q.bf10
            if bf_convention == "market":
                bf25 = market_to_smile_bf(q.atm, q.rr25, q.bf25, spot, q.T, rd, rf,
                                          delta=0.25, delta_convention=dc, atm_convention=ac)
                if q.bf10 is not None and q.rr10 is not None:
                    bf10 = market_to_smile_bf(q.atm, q.rr10, q.bf10, spot, q.T, rd, rf,
                                              delta=0.10, delta_convention=dc, atm_convention=ac)
            p25, c25 = smile.rr_bf_to_vols(q.atm, q.rr25, bf25)
            k_p, k_a, k_c = smile.delta_pillar_strikes(spot, q.T, rd, rf, q.atm, p25, c25,
                                                       delta=0.25, delta_convention=dc,
                                                       atm_convention=ac)
            Ks, Vs = [k_p, k_a, k_c], [p25, q.atm, c25]
            if q.rr10 is not None and bf10 is not None:
                p10, c10 = smile.rr_bf_to_vols(q.atm, q.rr10, bf10)
                k_p10 = gk.strike_from_delta(0.10, spot, q.T, rd, rf, p10, -1, dc)
                k_c10 = gk.strike_from_delta(0.10, spot, q.T, rd, rf, c10, +1, dc)
                if np.isfinite(k_p10) and np.isfinite(k_c10):
                    Ks, Vs = [k_p10] + Ks + [k_c10], [p10] + Vs + [c10]
            F = float(spot) * math.exp((rd - rf) * q.T)
            fits.append(sabr.calibrate_sabr(F, q.T, np.array(Ks), np.array(Vs), beta=beta,
                                            atm_vol=float(np.interp(F, Ks, Vs)) if pin_atm else None))
        return cls(pair, asof, float(spot), float(rd), float(rf), tuple(fits), dc,
                   float(beta), "sabr")

    @property
    def tenors(self) -> tuple[float, ...]:
        """Calibrated expiries in years, ascending."""
        return tuple(f.params.T for f in self.fits)

    def params(self, T: float) -> sabr.SABRParams:
        """Calibrated parameters of the nearest calibrated tenor (for display)."""
        Ts = np.array(self.tenors, float)
        return self.fits[int(np.argmin(np.abs(Ts - float(T))))].params

    def _vol_impl(self, K: Any, T: float) -> Any:
        Karr = np.asarray(K, float)
        T = max(float(T), gk.T_MIN)
        F = self.forward(T)
        k = np.log(np.maximum(Karr, 1e-300) / F)
        Ts = np.array(self.tenors, float)

        def w_of(i: int) -> np.ndarray:
            p = self.fits[i].params
            return np.asarray(p.vol(p.F * np.exp(k)), float) ** 2 * p.T

        if Ts.size == 1 or T <= Ts[0]:
            w = w_of(0) * (T / Ts[0])
        elif T >= Ts[-1]:
            w = w_of(len(Ts) - 1) * (T / Ts[-1])
        else:
            j = int(np.searchsorted(Ts, T, side="right"))
            a = (T - Ts[j - 1]) / (Ts[j] - Ts[j - 1])
            w = (1.0 - a) * w_of(j - 1) + a * w_of(j)
        out = np.sqrt(np.maximum(w, 1e-12) / T)
        return out if np.ndim(K) else float(out)

    def diagnostics(self, tenors: Sequence[float] | None = None) -> SurfaceDiagnostics:
        """Per-tenor calibration residuals plus the Hagan negative-density check."""
        Ts = list(tenors) if tenors is not None else list(self.tenors)
        bfly: list[dict[str, Any]] = []
        fits: list[dict[str, Any]] = []
        for f in self.fits:
            rep = f.params.arbitrage_check(self.spot, self.rd, self.rf)
            if rep.n_negative:
                bfly.append({"T": f.params.T, "n_bad": rep.n_negative,
                             "min_density": rep.min_density, "integral": rep.integral})
            fits.append({"T": f.params.T, "kind": "sabr", "n_quotes": f.n_points,
                         "rmse_vol": f.rmse_vol, "max_abs_err": f.max_abs_err,
                         "alpha": f.params.alpha, "rho": f.params.rho, "nu": f.params.nu,
                         "beta": f.params.beta, "success": f.success})
        cal: list[dict[str, Any]] = []
        kk = np.linspace(-0.5, 0.5, 121)
        prev = None
        for T in Ts:
            w = np.asarray(self.slice(T, self.forward(T) * np.exp(kk)), float) ** 2 * T
            if prev is not None and (w - prev[1] < -1e-12).any():
                d = w - prev[1]
                cal.append({"T_lo": prev[0], "T_hi": T, "n_bad": int((d < -1e-12).sum()),
                            "min_dw": float(np.min(d)), "k_at_min": float(kk[int(np.argmin(d))])})
            prev = (T, w)
        return SurfaceDiagnostics(bfly, cal, fits, not bfly and not cal)


# --------------------------------------------------------------------------- #
# flat surface (degenerate, for tests / empty-book screens)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FlatSurface(smile.SmileSurfaceMixin):
    """Constant-vol surface.  Useful as a control in tests and as the graceful
    degradation when a provider returns nothing but an ATM level."""
    pair: str
    asof: datetime
    spot: float
    rd: float
    rf: float
    level: float = 0.10
    delta_convention: str = "spot"
    method: str = "flat"

    def _vol_impl(self, K: Any, T: float) -> Any:
        if np.ndim(K) == 0:
            return float(self.level)
        return np.full(np.shape(np.asarray(K, float)), float(self.level))


# --------------------------------------------------------------------------- #
# the factory (contract s8 -- FROZEN SIGNATURE)
# --------------------------------------------------------------------------- #
def build_surface(pair: str, asof: datetime, quotes: list[SmileQuotes],
                  spot: float, rd: float, rf: float,
                  method: str = "vanna_volga") -> VolSurface:
    """Build a :class:`VolSurface` from broker quotes.  **The frozen contract s8 entry
    point** -- every data provider funnels through here.

    Parameters
    ----------
    pair : e.g. ``"EURUSD"``.  Drives the delta convention via ``conventions.PAIRS``
        (premium-adjusted for USDJPY, USDCHF, USDCAD, USDSEK, USDNOK, EURJPY).
    asof : snapshot timestamp, carried onto the surface for provenance.
    quotes : one :class:`SmileQuotes` per tenor.  ``bf`` is interpreted as a **smile**
        butterfly (see :class:`SmileQuotes`).
    spot, rd, rf : spot, domestic (quote) and foreign (base) continuously-compounded
        rates.  Flat curves in v1.
    method : ``"vanna_volga"`` (default), ``"sabr"``, ``"interp"`` or ``"flat"``.
        ``"interp"`` builds an :class:`InterpolatedSurface` from the quote-implied
        pillar strikes -- for a *listed chain* call
        :meth:`InterpolatedSurface.from_chain` directly, it is strictly better.

    Returns
    -------
    VolSurface
        Picklable, cheap to evaluate, exposing ``vol``, ``vol_by_delta``, ``atm``,
        ``rr``, ``bf``, ``slice``.

    Raises
    ------
    ValueError
        on an unknown ``method`` or an empty/degenerate quote list.
    """
    m = str(method).lower()
    if m not in METHODS:
        raise ValueError(f"method must be one of {METHODS}, got {method!r}")
    qs = _sorted_quotes(quotes)
    dc = _delta_convention(pair)

    if m == "vanna_volga":
        return VannaVolgaSurface.from_smile_quotes(pair, asof, qs, spot, rd, rf,
                                                   delta_convention=dc)
    if m == "sabr":
        return SABRSurface.calibrate(pair, asof, qs, spot, rd, rf, delta_convention=dc)
    if m == "flat":
        return FlatSurface(pair, asof, float(spot), float(rd), float(rf),
                           float(qs[0].atm), dc)

    # "interp": expand each quote into its delta pillars and fit slices to them
    ac = smile.atm_convention_for(dc)
    Ks: list[float] = []
    Ts: list[float] = []
    Vs: list[float] = []
    for q in qs:
        p25, c25 = smile.rr_bf_to_vols(q.atm, q.rr25, q.bf25)
        k_p, k_a, k_c = smile.delta_pillar_strikes(spot, q.T, rd, rf, q.atm, p25, c25,
                                                   delta=0.25, delta_convention=dc,
                                                   atm_convention=ac)
        pts = [(k_p, p25), (k_a, q.atm), (k_c, c25)]
        if q.rr10 is not None and q.bf10 is not None:
            p10, c10 = smile.rr_bf_to_vols(q.atm, q.rr10, q.bf10)
            k_p10 = gk.strike_from_delta(0.10, spot, q.T, rd, rf, p10, -1, dc)
            k_c10 = gk.strike_from_delta(0.10, spot, q.T, rd, rf, c10, +1, dc)
            if np.isfinite(k_p10) and np.isfinite(k_c10):
                pts = [(k_p10, p10)] + pts + [(k_c10, c10)]
        for kk, vv in pts:
            Ks.append(float(kk)); Ts.append(float(q.T)); Vs.append(float(vv))
    return InterpolatedSurface.from_chain(Ks, Ts, Vs, spot=spot, rd=rd, rf=rf,
                                          pair=pair, asof=asof, delta_convention=dc,
                                          min_svi_points=5)


def _years(t: Any) -> float:
    """Accept a year fraction or a broker tenor string (``"3M"``)."""
    if isinstance(t, str):
        from ..conventions import tenor_years
        return float(tenor_years(t))
    return float(t)
