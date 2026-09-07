"""``InterpolatedSurface`` -- an arbitrage-aware vol surface built from a *listed*
option chain (strike, expiry, implied vol), e.g. the FXE / FXB / FXY ETF chains
that are our only free source of real, traded implied vols.

Why this module is not just "scipy.interp2d on vols"
----------------------------------------------------
Listed chains are ragged: each expiry has its own strike ladder, quotes are stale
in the wings, and the ETF's own dividend/borrow makes the raw mid vols slightly
inconsistent.  Interpolating vol directly in ``(K, T)`` produces two failures a
risk manager will spot immediately:

* **Calendar arbitrage** -- total variance ``w = sigma^2 T`` decreasing in ``T`` at
  a fixed log-moneyness, i.e. a free lunch from a calendar spread.
* **Butterfly arbitrage** -- a negative implied density, i.e. a butterfly with a
  negative price.

So we do what the literature prescribes:

1. **In strike:** fit each expiry slice with **raw SVI** in total variance,
   ``w(k) = a + b [ rho (k - m) + sqrt((k - m)^2 + s^2) ]`` with ``k = ln(K/F)``,
   under Gatheral's no-arbitrage box constraints plus an explicit penalty on the
   butterfly functional ``g(k)`` going negative.  SVI is smooth, has the right
   linear wings (Lee's moment formula), and 5 parameters is the right capacity for
   a listed chain.  Slices with fewer than 5 usable quotes fall back to a monotone
   PCHIP in ``k`` on total variance (no overshoot -> no invented convexity).
2. **In time:** linear interpolation of **total variance at fixed log-moneyness**,
   which is calendar-arbitrage-free by construction whenever the slices are
   ordered; ``enforce_calendar=True`` (the default) additionally clips each slice
   up to its predecessor so the ordering always holds.  Outside the quoted tenor
   range we extrapolate ``w`` proportionally to ``T`` (flat vol), never backwards.
3. **Diagnose, always.**  :meth:`InterpolatedSurface.diagnostics` reports every
   calendar and butterfly violation it can find; nothing is swept under the rug.

Formulae
--------
Gatheral's butterfly functional (non-negative <=> no butterfly arbitrage):

    g(k) = (1 - k w'(k) / (2 w(k)))^2 - (w'(k)^2 / 4)(1/w(k) + 1/4) + w''(k)/2

Raw SVI derivatives:

    w'(k)  = b [ rho + (k-m)/sqrt((k-m)^2 + s^2) ]
    w''(k) = b s^2 / ((k-m)^2 + s^2)^{3/2}

References
----------
* Gatheral, J. (2004), "A Parsimonious Arbitrage-free Implied Volatility
  Parameterization", Global Derivatives.
* Gatheral, J. and Jacquier, A. (2014), "Arbitrage-free SVI volatility surfaces",
  *Quantitative Finance* 14(1), 59-71.
* Clark, I. (2011), *FX Option Pricing*, s3.7 (surface construction and quality).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final, Sequence

import numpy as np

from . import gk, smile

__all__ = ["InterpolatedSurface", "SVIParams", "VolSlice", "SurfaceDiagnostics",
           "svi_w", "svi_g", "fit_svi_slice"]

_MIN_W: Final[float] = 1e-10


# --------------------------------------------------------------------------- #
# raw SVI
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SVIParams:
    """Raw SVI parameters for one expiry slice (in total-variance space).

    ``a`` level, ``b`` >= 0 angle/slope, ``rho`` in (-1, 1) rotation (skew),
    ``m`` horizontal shift, ``s`` > 0 smoothing (curvature at the vertex).
    """
    a: float
    b: float
    rho: float
    m: float
    s: float

    def as_array(self) -> np.ndarray:
        return np.array([self.a, self.b, self.rho, self.m, self.s], float)


def svi_w(k: Any, p: SVIParams) -> Any:
    """Total variance ``w(k)`` of a raw-SVI slice.  Vectorised in ``k``."""
    kk = np.asarray(k, float) - p.m
    return p.a + p.b * (p.rho * kk + np.sqrt(kk * kk + p.s * p.s))


def _svi_dw(k: Any, p: SVIParams) -> Any:
    kk = np.asarray(k, float) - p.m
    return p.b * (p.rho + kk / np.sqrt(kk * kk + p.s * p.s))


def _svi_d2w(k: Any, p: SVIParams) -> Any:
    kk = np.asarray(k, float) - p.m
    return p.b * p.s * p.s / np.power(kk * kk + p.s * p.s, 1.5)


def svi_g(k: Any, p: SVIParams) -> Any:
    """Gatheral's butterfly functional ``g(k)``.  ``g >= 0`` for all ``k`` is
    equivalent to a non-negative risk-neutral density for that slice."""
    kk = np.asarray(k, float)
    w = np.maximum(svi_w(kk, p), _MIN_W)
    d1 = _svi_dw(kk, p)
    d2 = _svi_d2w(kk, p)
    return (1.0 - kk * d1 / (2.0 * w)) ** 2 - (d1 * d1 / 4.0) * (1.0 / w + 0.25) + d2 / 2.0


def fit_svi_slice(k: np.ndarray, w: np.ndarray, T: float, *,
                  weights: np.ndarray | None = None,
                  arb_penalty: float = 1e4,
                  k_scan: np.ndarray | None = None,
                  w_floor: np.ndarray | None = None) -> tuple[SVIParams, float]:
    """Raw-SVI fit of total variance ``w`` against log-moneyness ``k``.

    Uses the **quasi-explicit** two-stage scheme of Zeliade Systems (2009), which is
    what makes the fit reliable at short tenors where naive 5-parameter
    least-squares is badly scaled (``w`` ~ 1e-4 while ``b`` ~ 1e-1):

    1. Substituting ``y = (k - m) / s`` makes ``w = a + d y + c sqrt(y^2 + 1)``
       **linear** in ``(a, d, c)`` for fixed ``(m, s)``, with ``c = b s`` and
       ``d = rho b s``.  That inner problem is a bounded linear least squares
       (``scipy.optimize.lsq_linear``) over the Zeliade domain
       ``0 <= c <= 4s``, ``|d| <= min(c, 4s - c)``, ``0 <= a <= max(w)``,
       which is exactly the region where SVI has no vertical or wing arbitrage.
    2. Only the two well-scaled parameters ``(m, log s)`` are searched, by
       Nelder-Mead from a small deterministic multi-start.
    3. A final full 5-parameter polish (``least_squares`` with ``x_scale="jac"``)
       adds the soft penalties: ``arb_penalty * min(0, g(k))`` on a dense scan grid
       (butterfly) and, when ``w_floor`` is supplied, ``arb_penalty * min(0, w - floor)``
       (calendar -- keeps this slice above the previous tenor's total variance).

    Returns ``(params, rmse_in_total_variance)``.  Deterministic.
    """
    from scipy.optimize import least_squares, lsq_linear, minimize

    k = np.asarray(k, float)
    w = np.asarray(w, float)
    wt = np.ones_like(w) if weights is None else np.asarray(weights, float)
    if k_scan is None:
        span = max(float(np.max(np.abs(k))) * 2.0, 0.10)
        k_scan = np.linspace(-1.5 * span, 1.5 * span, 121)
    w_max = float(np.max(w)) * 1.5 + 1e-12

    def inner(m: float, sig: float) -> tuple[np.ndarray, float]:
        """Bounded linear LS for (a, d, c) at fixed (m, sig).  Returns (x, sse)."""
        y = (k - m) / sig
        A = np.column_stack([np.ones_like(y), y, np.sqrt(y * y + 1.0)]) * wt[:, None]
        b_ = w * wt
        lo = np.array([0.0, -4.0 * sig, 0.0])
        hi = np.array([w_max, 4.0 * sig, 4.0 * sig])
        try:
            r = lsq_linear(A, b_, bounds=(lo, hi), method="bvls")
            x = r.x
        except (ValueError, RuntimeError):                      # pragma: no cover
            x = np.array([float(np.mean(w)), 0.0, 1e-8])
        # coupled Zeliade constraints |d| <= c and |d| <= 4s - c, applied as a
        # projection (the box solve above rarely violates them, but never trust it)
        cap = min(x[2], 4.0 * sig - x[2])
        x[1] = float(np.clip(x[1], -cap, cap)) if cap > 0 else 0.0
        res = A @ x - b_
        return x, float(res @ res)

    best = None
    k_lo, k_hi = float(np.min(k)), float(np.max(k))
    span = max(k_hi - k_lo, 1e-3)
    for m0 in (k_lo, 0.5 * (k_lo + k_hi), k_hi):
        for s0 in (0.05 * span, 0.25 * span, span, 3.0 * span):
            def obj(u: np.ndarray) -> float:
                return inner(float(u[0]), float(np.exp(u[1])))[1]

            try:
                r = minimize(obj, np.array([m0, math.log(max(s0, 1e-6))]),
                             method="Nelder-Mead",
                             options={"xatol": 1e-12, "fatol": 1e-18, "maxiter": 2000})
                u = r.x
                sse = float(r.fun)
            except (ValueError, RuntimeError):                  # pragma: no cover
                continue
            if best is None or sse < best[0]:
                best = (sse, float(u[0]), float(np.exp(u[1])))

    if best is None:                                            # pragma: no cover
        return SVIParams(float(np.mean(w)), 0.0, 0.0, 0.0, 0.1), float("nan")

    _, m, sig = best
    x, _ = inner(m, sig)
    a, d, c = float(x[0]), float(x[1]), float(x[2])
    b = c / sig if sig > 0 else 0.0
    rho = float(np.clip(d / c, -0.999, 0.999)) if c > 1e-14 else 0.0
    p = SVIParams(a, b, rho, m, sig)

    # ---- polish with the arbitrage penalties --------------------------- #
    def unpack(z: np.ndarray) -> SVIParams:
        return SVIParams(float(z[0]), float(z[1]), float(np.clip(z[2], -0.999, 0.999)),
                         float(z[3]), float(max(z[4], 1e-6)))

    def resid(z: np.ndarray) -> np.ndarray:
        q = unpack(z)
        r = wt * (svi_w(k, q) - w)
        pen = arb_penalty * np.minimum(svi_g(k_scan, q), 0.0)
        parts = [r, pen]
        if w_floor is not None:
            parts.append(arb_penalty * np.minimum(svi_w(k_scan, q) - w_floor, 0.0))
        parts.append(np.array([
            arb_penalty * min(0.0, q.a + q.b * q.s * math.sqrt(max(1.0 - q.rho ** 2, 0.0))),
            arb_penalty * min(0.0, 4.0 - q.b * (1.0 + abs(q.rho))),
        ]))
        return np.concatenate(parts)

    z0 = np.array([p.a, p.b, p.rho, p.m, p.s])
    try:
        res = least_squares(resid, z0,
                            bounds=(np.array([-np.inf, 0.0, -0.999, -5.0, 1e-6]),
                                    np.array([np.inf, np.inf, 0.999, 5.0, 10.0])),
                            x_scale="jac", xtol=1e-14, ftol=1e-14, gtol=1e-14,
                            max_nfev=5000)
        cand = unpack(res.x)
        if float(np.sum(resid(res.x) ** 2)) <= float(np.sum(resid(z0) ** 2)):
            p = cand
    except (ValueError, RuntimeError):                          # pragma: no cover
        pass

    rmse = float(np.sqrt(np.mean((svi_w(k, p) - w) ** 2)))
    return p, rmse


# --------------------------------------------------------------------------- #
# one expiry slice
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VolSlice:
    """One expiry's smile in total-variance space.  Picklable, evaluation is O(1).

    ``kind`` is ``"svi"`` (5-parameter fit) or ``"pchip"`` (monotone cubic through
    the quoted points, used when a slice has fewer than 5 usable quotes).
    """
    T: float
    F: float
    kind: str
    svi: SVIParams | None = None
    knots_k: np.ndarray | None = None
    knots_w: np.ndarray | None = None
    slopes: np.ndarray | None = None
    n_quotes: int = 0
    rmse_vol: float = 0.0

    def w(self, k: Any) -> Any:
        """Total variance at log-moneyness ``k = ln(K/F)``, floored at 0."""
        if self.kind == "svi" and self.svi is not None:
            out = svi_w(k, self.svi)
        else:
            out = smile.pchip_eval(k, self.knots_k, self.knots_w, self.slopes,
                                   extrap="linear")
        return np.maximum(out, _MIN_W)

    def vol(self, k: Any) -> Any:
        """Implied vol at log-moneyness ``k``."""
        return np.sqrt(self.w(k) / max(self.T, gk.T_MIN))

    def g(self, k: Any) -> Any:
        """Butterfly functional; SVI has it analytically, PCHIP by finite difference."""
        if self.kind == "svi" and self.svi is not None:
            return svi_g(k, self.svi)
        kk = np.atleast_1d(np.asarray(k, float))
        h = 1e-4
        w0 = self.w(kk)
        d1 = (self.w(kk + h) - self.w(kk - h)) / (2.0 * h)
        d2 = (self.w(kk + h) - 2.0 * w0 + self.w(kk - h)) / (h * h)
        return (1.0 - kk * d1 / (2.0 * w0)) ** 2 - (d1 * d1 / 4.0) * (1.0 / w0 + 0.25) + d2 / 2.0


@dataclass(frozen=True)
class SurfaceDiagnostics:
    """Arbitrage report for an :class:`InterpolatedSurface`.

    ``butterfly`` / ``calendar`` are lists of ``dict`` rows suitable for a
    ``pd.DataFrame(...)`` in the UI.  ``ok`` is True when both are empty.
    """
    butterfly: list[dict[str, Any]]
    calendar: list[dict[str, Any]]
    fit_rmse_vol: list[dict[str, Any]]
    ok: bool

    def summary(self) -> str:
        """One-line human summary for the surface page's status badge."""
        if self.ok:
            return f"OK - {len(self.fit_rmse_vol)} slices, no calendar or butterfly violations"
        return (f"{len(self.butterfly)} butterfly and {len(self.calendar)} calendar "
                f"violation(s) across {len(self.fit_rmse_vol)} slices")


# --------------------------------------------------------------------------- #
# the surface
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class InterpolatedSurface(smile.SmileSurfaceMixin):
    """Vol surface interpolated from a listed option chain.

    Implements the frozen ``VolSurface`` protocol (``vol``, ``vol_by_delta``,
    ``atm``, ``rr``, ``bf``, ``slice``) via
    :class:`~fxgamma.models.smile.SmileSurfaceMixin`.  Frozen dataclass of plain
    arrays -> picklable and thread-safe; evaluation is a handful of numpy ops.
    """
    pair: str
    asof: datetime
    spot: float
    rd: float
    rf: float
    slices: tuple[VolSlice, ...]
    delta_convention: str = "spot"
    method: str = "interp"

    # -- construction ----------------------------------------------------- #
    @classmethod
    def from_chain(cls, strikes: Sequence[float] | np.ndarray,
                   expiries: Sequence[float] | Sequence[date] | np.ndarray,
                   vols: Sequence[float] | np.ndarray,
                   *, spot: float, rd: float = 0.0, rf: float = 0.0,
                   pair: str = "", asof: datetime | None = None,
                   delta_convention: str = "spot",
                   weights: Sequence[float] | np.ndarray | None = None,
                   enforce_calendar: bool = True,
                   min_svi_points: int = 5) -> "InterpolatedSurface":
        """Build from *scattered* chain points -- one ``(strike, expiry, vol)`` per quote.

        Parameters
        ----------
        strikes, expiries, vols : equal-length sequences.  ``expiries`` may be
            year-fractions (floats) or ``date``/``datetime`` objects, in which case
            ``asof`` is required and ACT/365F is used.
        spot, rd, rf : underlying and flat continuously-compounded rates.  For an
            ETF proxy chain, ``rf`` is the dividend/borrow yield.
        weights : optional per-quote fit weights (vega or open-interest weights are
            both sensible; defaults to 1).
        enforce_calendar : clip each slice's total variance up to the previous
            slice's so ``w`` is non-decreasing in ``T`` at every log-moneyness.
        min_svi_points : slices with fewer usable quotes fall back to PCHIP.

        Bad quotes (non-finite, vol <= 0, strike <= 0) are dropped silently -- listed
        chains always contain some.
        """
        K = np.asarray(strikes, float).ravel()
        V = np.asarray(vols, float).ravel()
        Traw = np.asarray(expiries, dtype=object).ravel()
        if not (K.size == V.size == Traw.size):
            raise ValueError("strikes, expiries and vols must have equal length")

        T = np.empty(K.size, float)
        for i, e in enumerate(Traw):
            if isinstance(e, (datetime, date)):
                if asof is None:
                    raise ValueError("asof is required when expiries are dates")
                d0 = asof.date() if isinstance(asof, datetime) else asof
                d1 = e.date() if isinstance(e, datetime) else e
                T[i] = max((d1 - d0).days / 365.0, gk.T_MIN)
            else:
                T[i] = float(e)

        wt = np.ones_like(V) if weights is None else np.asarray(weights, float).ravel()
        good = np.isfinite(K) & np.isfinite(V) & np.isfinite(T) & (K > 0) & (V > 0) & (T > gk.T_MIN)
        K, V, T, wt = K[good], V[good], T[good], wt[good]
        if K.size == 0:
            raise ValueError("no usable quotes in the chain")

        built: list[VolSlice] = []
        prev: VolSlice | None = None
        for t in np.unique(np.round(T, 10)):
            sel = np.isclose(T, t, rtol=0, atol=1e-10)
            F = float(spot) * math.exp((rd - rf) * float(t))
            k = np.log(K[sel] / F)
            w = V[sel] ** 2 * float(t)
            order = np.argsort(k)
            k, w, ws = k[order], w[order], wt[sel][order]

            if enforce_calendar and prev is not None:
                w = np.maximum(w, np.asarray(prev.w(k), float) + 1e-14)

            if k.size >= min_svi_points:
                scan = np.linspace(-1.5 * max(float(np.max(np.abs(k))) * 2.0, 0.10),
                                   1.5 * max(float(np.max(np.abs(k))) * 2.0, 0.10), 121)
                floor = None
                if enforce_calendar and prev is not None:
                    floor = np.asarray(prev.w(scan), float)
                p, _ = fit_svi_slice(k, w, float(t), weights=ws,
                                     k_scan=scan, w_floor=floor)
                rmse_vol = float(np.sqrt(np.mean(
                    (np.sqrt(np.maximum(svi_w(k, p), _MIN_W) / t) - np.sqrt(w / t)) ** 2)))
                sl = VolSlice(float(t), F, "svi", p, None, None, None, int(k.size), rmse_vol)
            else:
                kk, ww = _dedupe(k, w)
                sl = VolSlice(float(t), F, "pchip", None, kk, ww,
                              smile.pchip_slopes(kk, ww), int(k.size), 0.0)
            built.append(sl)
            prev = sl

        built.sort(key=lambda s: s.T)
        return cls(pair, asof or datetime.now(), float(spot), float(rd), float(rf),
                   tuple(built), delta_convention, "interp")

    # -- evaluation ------------------------------------------------------- #
    def _vol_impl(self, K: Any, T: float) -> Any:
        """Vol at ``(K, T)``: linear-in-total-variance across slices at fixed ``k``."""
        Karr = np.asarray(K, float)
        F = self.forward(T)
        k = np.log(np.maximum(Karr, 1e-300) / F)
        w = self._total_variance(k, float(T))
        out = np.sqrt(np.maximum(w, _MIN_W) / max(float(T), gk.T_MIN))
        return out if np.ndim(K) else float(out)

    def _total_variance(self, k: Any, T: float) -> Any:
        """Total variance at log-moneyness ``k`` and expiry ``T``.

        Linear in ``T`` between bracketing slices (calendar-arbitrage-free given
        ordered slices); below the first slice ``w`` is scaled ``propto T`` (flat
        vol), above the last slice likewise, which never creates a free lunch.
        """
        Ts = np.array([s.T for s in self.slices], float)
        karr = np.asarray(k, float)
        if Ts.size == 1:
            s0 = self.slices[0]
            return np.asarray(s0.w(karr), float) * (T / s0.T)
        if T <= Ts[0]:
            return np.asarray(self.slices[0].w(karr), float) * (T / Ts[0])
        if T >= Ts[-1]:
            return np.asarray(self.slices[-1].w(karr), float) * (T / Ts[-1])
        j = int(np.searchsorted(Ts, T, side="right"))
        lo, hi = self.slices[j - 1], self.slices[j]
        a = (T - lo.T) / (hi.T - lo.T)
        return (1.0 - a) * np.asarray(lo.w(karr), float) + a * np.asarray(hi.w(karr), float)

    @property
    def tenors(self) -> tuple[float, ...]:
        """Quoted expiries in years, ascending."""
        return tuple(s.T for s in self.slices)

    # -- quality ---------------------------------------------------------- #
    def diagnostics(self, *, k_lo: float = -0.6, k_hi: float = 0.6,
                    n: int = 161, tol: float = -1e-10) -> SurfaceDiagnostics:
        """Scan for calendar and butterfly arbitrage across the whole surface.

        * **Butterfly**: ``g(k) < tol`` on each fitted slice (Gatheral's functional).
        * **Calendar**: ``w(k, T_{i+1}) < w(k, T_i)`` at any scanned ``k``.
        * Also returns the per-slice fit RMSE in *vol* points, which is what a risk
          manager actually asks for ("how well does the surface fit the market?").

        The scan grid is in log-moneyness, so it is tenor-independent; widen
        ``k_lo``/``k_hi`` to stress the wings.
        """
        kk = np.linspace(k_lo, k_hi, int(n))
        bfly: list[dict[str, Any]] = []
        cal: list[dict[str, Any]] = []
        fits: list[dict[str, Any]] = []
        for i, s in enumerate(self.slices):
            g = np.asarray(s.g(kk), float)
            bad = g < tol
            if bad.any():
                bfly.append({"T": s.T, "n_bad": int(bad.sum()), "min_g": float(np.min(g)),
                             "k_at_min": float(kk[int(np.argmin(g))]), "kind": s.kind})
            fits.append({"T": s.T, "kind": s.kind, "n_quotes": s.n_quotes,
                         "rmse_vol": s.rmse_vol})
            if i:
                prev = self.slices[i - 1]
                dw = np.asarray(s.w(kk), float) - np.asarray(prev.w(kk), float)
                if (dw < tol).any():
                    cal.append({"T_lo": prev.T, "T_hi": s.T,
                                "n_bad": int((dw < tol).sum()), "min_dw": float(np.min(dw)),
                                "k_at_min": float(kk[int(np.argmin(dw))])})
        return SurfaceDiagnostics(bfly, cal, fits, not bfly and not cal)


def _dedupe(k: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Collapse duplicate log-moneyness knots (listed chains repeat strikes)."""
    uk, inv = np.unique(np.round(k, 12), return_inverse=True)
    uw = np.zeros_like(uk)
    cnt = np.zeros_like(uk)
    np.add.at(uw, inv, w)
    np.add.at(cnt, inv, 1.0)
    return uk, uw / np.maximum(cnt, 1.0)
