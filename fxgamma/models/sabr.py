"""Hagan lognormal (Black) SABR: implied-vol expansion, calibration and arbitrage checks.

Model
-----
    dF_t     = alpha_t F_t^beta dW_t
    dalpha_t = nu alpha_t dZ_t,      d<W, Z>_t = rho dt

Hagan et al.'s singular-perturbation expansion for the *Black* implied vol of a
European option struck at ``K`` on forward ``F`` with expiry ``T``:

    sigma_B(K, F) = alpha / ( (FK)^{(1-beta)/2} [1 + (1-beta)^2/24 ln^2(F/K)
                                                   + (1-beta)^4/1920 ln^4(F/K)] )
                    * (z / chi(z))
                    * [1 + ( (1-beta)^2/24 * alpha^2/(FK)^{1-beta}
                           + rho beta nu alpha / (4 (FK)^{(1-beta)/2})
                           + (2 - 3 rho^2) nu^2 / 24 ) T ]

    z      = (nu / alpha) (FK)^{(1-beta)/2} ln(F/K)
    chi(z) = ln( ( sqrt(1 - 2 rho z + z^2) + z - rho ) / (1 - rho) )

with the ATM limit ``K -> F`` taken analytically (``z/chi(z) -> 1``, and the
prefactor collapses to ``alpha / F^{1-beta}``).

Role in this application
------------------------
SABR is the *cross-check* on vanna-volga, not the primary FX surface: it has a
smile dynamic (the backbone), so it is the right tool for "how does my vega move
when spot moves" and for extrapolating wings beyond the 10-delta quotes, where VV
is unreliable.  With three quotes (ATM/RR/BF) and three free parameters
(alpha, rho, nu at fixed beta) the fit is exact-ish; with five it is a genuine
least-squares.

Where the Hagan expansion misbehaves (read before trusting a wing)
------------------------------------------------------------------
1. **Low strikes / long maturities.** The expansion is O(T) accurate; for
   ``nu^2 T`` greater than ~0.5 (long-dated, high vol-of-vol) the implied density
   goes negative in the low-strike wing.  This is the well-documented Hagan
   arbitrage; it is a property of the *approximation*, not of SABR itself.
   :meth:`SABRParams.arbitrage_check` measures it.
2. **beta near 0 with F near 0** -- not an FX concern (FX forwards are strictly
   positive) but the ``(FK)^{(1-beta)/2}`` prefactor is singular there.
3. **|rho| -> 1** makes ``chi(z)`` ill-conditioned; we bound ``|rho| <= 0.999``.
4. **Very short expiries (< 1W).** The T-order correction is negligible, so the
   fit becomes nearly degenerate in ``nu`` vs ``rho``; prefer VV there.
5. **beta and rho are not jointly identifiable** from a single smile: the ATM
   backbone and the skew trade off almost perfectly.  Always fix ``beta``
   (FX desks use 1.0 -- lognormal backbone; 0.5 is a rates habit) and fit the
   other three.

References
----------
* Hagan, P., Kumar, D., Lesniewski, A., Woodward, D. (2002), "Managing Smile
  Risk", *Wilmott Magazine*, September, 84-108.
* Obloj, J. (2008), "Fine-tune your smile: correction to Hagan et al." (the
  ``z/chi(z)`` normalisation used here).
* Clark, I. (2011), *FX Option Pricing*, s3.6.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from . import gk, smile

__all__ = ["SABRParams", "sabr_vol", "calibrate_sabr", "SABRCalibration"]

_RHO_MAX: Final[float] = 0.999
_EPS: Final[float] = 1e-12


def sabr_vol(K: Any, F: float, T: float, alpha: float, beta: float,
             rho: float, nu: float) -> Any:
    """Hagan (2002) lognormal SABR implied volatility, vectorised in ``K``.

    Parameters
    ----------
    K : strike(s), same units as ``F``.  Must be > 0.
    F : forward for the tenor (``S exp((rd - rf) T)``).
    T : years.
    alpha : instantaneous vol level (> 0).  For ``beta = 1`` it is ~ the ATM vol.
    beta : CEV exponent in ``[0, 1]``, **fixed**, not fitted (see module docstring).
    rho : spot/vol correlation in ``(-1, 1)`` -- controls the skew / risk reversal.
    nu : vol-of-vol (>= 0) -- controls the smile curvature / butterfly.

    Returns
    -------
    Black implied vol as a decimal.  The ATM branch (``|ln(F/K)| < 1e-10``) uses the
    analytic limit so there is no 0/0.
    """
    Karr = np.asarray(K, dtype=float)
    F = float(F); T = max(float(T), gk.T_MIN)
    alpha = max(float(alpha), _EPS)
    rho = float(np.clip(rho, -_RHO_MAX, _RHO_MAX))
    nu = max(float(nu), 0.0)
    one_b = 1.0 - beta

    logFK = np.log(F / np.maximum(Karr, _EPS))
    FK = np.maximum(F * Karr, _EPS)
    FK_pow = FK ** (0.5 * one_b)

    denom = FK_pow * (1.0 + (one_b ** 2 / 24.0) * logFK ** 2
                      + (one_b ** 4 / 1920.0) * logFK ** 4)

    z = (nu / alpha) * FK_pow * logFK
    # z/chi(z) has a removable singularity at z = 0.  Near it, `chi` is a difference
    # of nearly equal logs and `z/chi` loses digits, so switch to the series
    #     z/chi(z) = 1 + rho z / 2 + (2 - 3 rho^2) z^2 / 12 + O(z^3)
    # whose next neglected term is O(z^3): at the |z| < 1e-6 switch point that is
    # below 1e-18, i.e. the two branches agree to well inside double precision and
    # the join is invisible.  (The old threshold was 1e-9, which is *tighter* than
    # necessary and left `z/chi` evaluating in its badly-conditioned region.)
    #
    # The fallback when chi still misbehaves is the series, NOT the constant 1.0.
    # Returning 1.0 is the ATM ratio; using it at a non-zero z silently prices a
    # skewed strike as if it were at the money.
    ser = 1.0 + 0.5 * rho * z + (2.0 - 3.0 * rho ** 2) * z * z / 12.0
    with np.errstate(divide="ignore", invalid="ignore"):
        chi = np.log((np.sqrt(1.0 - 2.0 * rho * z + z * z) + z - rho) / (1.0 - rho))
        ratio = np.where(np.abs(z) < 1e-6, ser,
                         z / np.where(np.abs(chi) < _EPS, np.nan, chi))
    ratio = np.where(np.isfinite(ratio), ratio, ser)
    ratio = np.where(np.isfinite(ratio), ratio, 1.0)

    corr = 1.0 + ((one_b ** 2 / 24.0) * alpha ** 2 / np.maximum(FK ** one_b, _EPS)
                  + 0.25 * rho * beta * nu * alpha / np.maximum(FK_pow, _EPS)
                  + (2.0 - 3.0 * rho ** 2) * nu ** 2 / 24.0) * T

    out = (alpha / denom) * ratio * corr
    out = np.maximum(out, 1e-6)
    return out if np.ndim(K) else float(out)


def alpha_from_atm(atm_vol: float, F: float, T: float, beta: float,
                   rho: float, nu: float) -> float:
    """Solve the Hagan ATM relation for ``alpha`` given the quoted ATM vol.

    The ATM expansion is a cubic in ``alpha``::

        atm = alpha / F^{1-b} * [1 + ( (1-b)^2/24 alpha^2/F^{2-2b}
                                     + rho b nu alpha /(4 F^{1-b})
                                     + (2-3rho^2) nu^2/24 ) T ]

    Reducing the free parameter set from 3 to 2 this way is standard practice: it
    pins the fit to the single most liquid quote and makes the optimiser far better
    conditioned.  Returns the smallest positive real root; falls back to
    ``atm * F^{1-beta}`` if no root is found.
    """
    one_b = 1.0 - beta
    c3 = one_b ** 2 * T / (24.0 * F ** (1.0 + 2.0 * one_b))     # coeff of alpha^3
    c2 = 0.25 * rho * beta * nu * T / F ** (2.0 * one_b)        # coeff of alpha^2
    c1 = (1.0 + (2.0 - 3.0 * rho ** 2) * nu ** 2 * T / 24.0) / F ** one_b
    c0 = -atm_vol
    # Drop the cubic term when it is negligible *relative to the linear one*, not
    # against an absolute 1e-300.  For the FX default beta = 1 the cubic coefficient
    # is identically zero, but for beta = 0.999 it is ~1e-11 while c1 is ~1 -- a
    # leading coefficient that small makes np.roots return a spurious ~1e11 root and
    # loses precision on the real ones for no benefit.
    cubic = abs(c3) > 1e-12 * max(abs(c1), 1e-300)
    roots = np.roots([c3, c2, c1, c0]) if cubic else (
        np.roots([c2, c1, c0]) if abs(c2) > 1e-12 * max(abs(c1), 1e-300)
        else np.array([-c0 / c1]))
    real = [float(r.real) for r in np.atleast_1d(roots)
            if abs(r.imag) < 1e-9 and r.real > 0.0]
    return min(real) if real else float(atm_vol * F ** one_b)


@dataclass(frozen=True)
class SABRParams:
    """Calibrated SABR parameters for one tenor.  Immutable and picklable."""
    alpha: float
    beta: float
    rho: float
    nu: float
    F: float
    T: float

    def vol(self, K: Any) -> Any:
        """Black implied vol at strike(s) ``K``."""
        return sabr_vol(K, self.F, self.T, self.alpha, self.beta, self.rho, self.nu)

    def atm(self) -> float:
        """Implied vol at ``K = F`` (ATM-forward, *not* the DNS strike)."""
        return float(sabr_vol(self.F, self.F, self.T, self.alpha, self.beta, self.rho, self.nu))

    def arbitrage_check(self, S: float, rd: float, rf: float, *, n: int = 401,
                        n_std: float = 5.0) -> smile.DensityReport:
        """Butterfly-arbitrage sanity check via the Breeden-Litzenberger density.

        Hagan's expansion is known to admit negative densities in the low-strike
        wing for large ``nu^2 T``; this runs the density on a +/- ``n_std`` grid and
        reports the most negative value, the count of violations and the mass
        integral.  **Call this after every calibration** -- a fit that matches the
        quotes but implies a negative density must not be used for wing pricing.
        """
        return smile.risk_neutral_density(self.vol, S, self.T, rd, rf,
                                          n=n, n_std=n_std)


@dataclass(frozen=True)
class SABRCalibration:
    """Outcome of :func:`calibrate_sabr`."""
    params: SABRParams
    rmse_vol: float           # root-mean-square vol error across the input quotes
    max_abs_err: float        # worst single-quote vol error
    n_points: int
    success: bool
    message: str = ""


def calibrate_sabr(F: float, T: float, strikes: Any, vols: Any, *,
                   beta: float = 1.0, atm_vol: float | None = None,
                   weights: Any = None,
                   x0: tuple[float, float, float] | None = None) -> SABRCalibration:
    """Fit ``(alpha, rho, nu)`` to a smile at **fixed** ``beta``.

    Parameters
    ----------
    F, T : forward and expiry (years).
    strikes, vols : the market smile points (>= 3 for a determined fit).
    beta : held fixed -- see the module docstring on beta/rho non-identifiability.
        FX default 1.0 (lognormal backbone).
    atm_vol : if given, ``alpha`` is *implied* from it at every optimiser step via
        :func:`alpha_from_atm` and only ``(rho, nu)`` are searched.  This is the
        recommended mode: it guarantees an exact ATM fit and halves the search space.
    weights : optional per-quote weights (e.g. vega weights).  Defaults to 1.
    x0 : optional ``(alpha, rho, nu)`` start.  Default seeds ``alpha`` from the
        ATM vol, ``rho = -sign(skew) * 0.2``, ``nu = 0.4``.

    Returns
    -------
    SABRCalibration

    Method
    ------
    ``scipy.optimize.least_squares`` (trust-region reflective) on the vol residuals
    with box bounds ``alpha in (0, 5]``, ``rho in [-0.999, 0.999]``, ``nu in [0, 5]``.
    Deterministic: no random restarts, so the same inputs always give the same fit.
    """
    from scipy.optimize import least_squares

    K = np.atleast_1d(np.asarray(strikes, float))
    V = np.atleast_1d(np.asarray(vols, float))
    if K.shape != V.shape or K.size < 3:
        raise ValueError("need >= 3 aligned (strike, vol) points")
    w = np.ones_like(V) if weights is None else np.asarray(weights, float)

    v_atm = float(atm_vol) if atm_vol is not None else float(np.interp(F, K, V))
    if x0 is None:
        skew = float(V[-1] - V[0])
        x0 = (v_atm * F ** (1.0 - beta), -0.2 if skew < 0 else 0.2, 0.4)

    if atm_vol is not None:
        def resid(p: np.ndarray) -> np.ndarray:
            rho, nu = float(p[0]), float(p[1])
            a = alpha_from_atm(v_atm, F, T, beta, rho, nu)
            return w * (sabr_vol(K, F, T, a, beta, rho, nu) - V)

        lo, hi = [-_RHO_MAX, 0.0], [_RHO_MAX, 5.0]
        start = [float(np.clip(x0[1], -0.9, 0.9)), float(np.clip(x0[2], 1e-3, 4.0))]
    else:
        def resid(p: np.ndarray) -> np.ndarray:
            a, rho, nu = float(p[0]), float(p[1]), float(p[2])
            return w * (sabr_vol(K, F, T, a, beta, rho, nu) - V)

        lo, hi = [1e-6, -_RHO_MAX, 0.0], [5.0, _RHO_MAX, 5.0]
        start = [float(np.clip(x0[0], 1e-4, 4.0)),
                 float(np.clip(x0[1], -0.9, 0.9)), float(np.clip(x0[2], 1e-3, 4.0))]

    res = least_squares(resid, start, bounds=(lo, hi), xtol=1e-14, ftol=1e-14,
                        gtol=1e-14, max_nfev=20000)

    if atm_vol is not None:
        rho, nu = float(res.x[0]), float(res.x[1])
        alpha = alpha_from_atm(v_atm, F, T, beta, rho, nu)
    else:
        alpha, rho, nu = (float(v) for v in res.x)

    params = SABRParams(alpha, float(beta), rho, nu, float(F), float(T))
    err = np.asarray(sabr_vol(K, F, T, alpha, beta, rho, nu), float) - V
    return SABRCalibration(params,
                           float(np.sqrt(np.mean(err ** 2))),
                           float(np.max(np.abs(err))),
                           int(K.size), bool(res.success), str(res.message))
