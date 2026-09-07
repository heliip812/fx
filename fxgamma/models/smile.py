"""Shared FX smile utilities: ATM conventions, RR/BF algebra, moneyness grids,
and the Breeden-Litzenberger risk-neutral density.

References
----------
* Clark, I. (2011), *FX Option Pricing*, ch. 3 ("Volatility Surface Construction")
  -- ATM conventions, market vs smile strangle.
* Reiswich, D. and Wystup, U. (2010), "A Guide to FX Options Quoting Conventions".
* Castagna, A. and Mercurio, F. (2007), "The Vanna-Volga Method for Implied
  Volatilities", *Risk*, January.
* Breeden, D. and Litzenberger, R. (1978), "Prices of State-Contingent Claims
  Implicit in Option Prices", *Journal of Business* 51(4).

Units
-----
Vols and rates are decimals (0.085 = 8.5%).  Risk reversals and butterflies are
also decimals (0.0025 = 0.25 vol points).  ``T`` in years, ACT/365F.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Final, Literal

import numpy as np

from . import gk

__all__ = [
    "ATM_CONVENTIONS", "atm_strike", "log_moneyness", "strike_grid",
    "rr_bf_to_vols", "vols_to_rr_bf", "delta_pillar_strikes",
    "risk_neutral_density", "DensityReport", "StrangleConvention",
    "total_variance", "vol_from_total_variance", "atm_convention_for",
    "pchip_slopes", "pchip_eval", "SmileSurfaceMixin",
    "LEE_CAP", "fit_wing", "eval_wing", "wing_slope",
]

#: Lee's (2004) moment-formula bound on the asymptotic slope of **total variance**
#: in log-moneyness: ``limsup_{k->+inf} w(k)/k <= 2`` and
#: ``limsup_{k->-inf} w(k)/|k| <= 2``.  A wing steeper than this in the limit has
#: an implied density with no finite moments and is guaranteed to admit butterfly
#: arbitrage far enough out.  Every extrapolated wing in this package is capped
#: here.  Reference: Lee, R. (2004), "The Moment Formula for Implied Volatility at
#: Extreme Strikes", *Mathematical Finance* 14(3), 469-480.
LEE_CAP: Final[float] = 2.0

#: ATM strike conventions.  FX **defaults to ``"dns"``** (delta-neutral straddle) for
#: G10 out to ~2Y; ``"fwd"`` (ATM-forward) is the equity/rates habit and is offered
#: for cross-checking, ``"spot"`` only for very short dates / some EM desks.
ATM_CONVENTIONS: Final[tuple[str, ...]] = ("dns", "dns_pa", "fwd", "spot")

#: Which butterfly the input quote represents.  See :func:`~fxgamma.models.vanna_volga.
#: market_to_smile_bf`.  **This library treats ``SmileQuotes.bf25`` as a SMILE
#: strangle by default** and provides an explicit converter for market strangles.
StrangleConvention = Literal["smile", "market"]


# --------------------------------------------------------------------------- #
# moneyness helpers
# --------------------------------------------------------------------------- #
def log_moneyness(K: Any, F: Any) -> Any:
    """``k = ln(K / F)`` -- forward log-moneyness, the natural smile abscissa.

    Forward (not spot) moneyness is used everywhere in this package so that a
    parallel shift in the rate differential translates the smile rigidly rather
    than reshaping it.
    """
    return np.log(np.asarray(K, float) / np.asarray(F, float))


def strike_grid(F: float, sigma: float, T: float, *, n: int = 101,
                n_std: float = 4.0) -> np.ndarray:
    """Log-uniform strike grid spanning ``+/- n_std`` standard deviations around ``F``.

    Returns ``n`` strictly increasing strikes.  Used for density integration, the
    butterfly-arbitrage scan and surface plotting.
    """
    w = max(sigma, 1e-8) * math.sqrt(max(T, gk.T_MIN))
    return F * np.exp(np.linspace(-n_std * w, n_std * w, int(n)))


def total_variance(sigma: Any, T: Any) -> Any:
    """``w = sigma^2 T`` -- total implied variance, the quantity that must be
    non-decreasing in ``T`` at fixed log-moneyness for calendar-arbitrage freedom."""
    return np.square(np.asarray(sigma, float)) * np.asarray(T, float)


def vol_from_total_variance(w: Any, T: Any) -> Any:
    """Inverse of :func:`total_variance`; ``T`` is floored at :data:`gk.T_MIN`."""
    return np.sqrt(np.maximum(np.asarray(w, float), 0.0) / np.maximum(np.asarray(T, float), gk.T_MIN))


# --------------------------------------------------------------------------- #
# ATM conventions
# --------------------------------------------------------------------------- #
def atm_strike(S: float, T: float, rd: float, rf: float, sigma: float,
               convention: str = "dns") -> float:
    """ATM strike for the requested convention.  **FX market standard is ``"dns"``.**

    =========== ============================ ==================================
    convention  strike                        rationale
    =========== ============================ ==================================
    ``dns``     ``F exp(+sigma^2 T / 2)``     delta-neutral straddle: the strike
                                              where call delta + put delta = 0
                                              in the *spot / forward* convention
                                              (``d1 = 0``).
    ``dns_pa``  ``F exp(-sigma^2 T / 2)``     delta-neutral straddle under the
                                              *premium-adjusted* convention
                                              (``d2 = 0``) -- USDJPY, USDCHF, ...
    ``fwd``     ``F``                         ATM-forward (zero dual-delta-ish);
                                              not the FX broker convention.
    ``spot``    ``S``                         ATM-spot; only for very short dates.
    =========== ============================ ==================================

    Note the *sign flip* between ``dns`` and ``dns_pa``: getting it backwards moves
    the ATM pillar by ``sigma^2 T`` in log-strike, which at 1Y / 10 vol is ~1% of
    spot and silently reprices the whole smile.  Reference: Clark (2011) s3.2.1;
    Reiswich-Wystup (2010) s3.3.

    ``atm_strike`` is convention-consistent with :func:`delta_pillar_strikes`: pass
    ``"dns_pa"`` exactly when the pair's ``delta_convention`` ends in ``_pa``.
    """
    conv = str(convention).lower()
    if conv not in ATM_CONVENTIONS:
        raise ValueError(f"convention must be one of {ATM_CONVENTIONS}, got {convention!r}")
    if conv == "spot":
        return float(S)
    F = float(S) * math.exp((rd - rf) * T)
    if conv == "fwd":
        return F
    w = sigma * sigma * max(T, 0.0)
    return float(F * math.exp(0.5 * w if conv == "dns" else -0.5 * w))


def atm_convention_for(delta_convention: str) -> str:
    """Map a pair's delta convention onto the matching ATM (DNS) convention."""
    return "dns_pa" if str(delta_convention).lower().endswith("_pa") else "dns"


# --------------------------------------------------------------------------- #
# risk reversal / butterfly algebra
# --------------------------------------------------------------------------- #
def rr_bf_to_vols(atm: float, rr: float, bf: float) -> tuple[float, float]:
    """(ATM, RR, **smile** BF) -> (put vol, call vol) at the quoted delta.

    ``sigma_call = atm + bf + rr/2``, ``sigma_put = atm + bf - rr/2``.

    This is the *smile-strangle* identity: it is an exact algebraic definition of
    the butterfly as ``(sigma_call + sigma_put)/2 - sigma_atm``.  Brokers often
    quote the **market strangle** instead, which is a price-based definition -- see
    :func:`~fxgamma.models.vanna_volga.market_to_smile_bf` for the conversion.
    """
    return float(atm + bf - 0.5 * rr), float(atm + bf + 0.5 * rr)


def vols_to_rr_bf(put_vol: float, call_vol: float, atm: float) -> tuple[float, float]:
    """Inverse of :func:`rr_bf_to_vols`: returns ``(rr, bf_smile)``."""
    return float(call_vol - put_vol), float(0.5 * (call_vol + put_vol) - atm)


def delta_pillar_strikes(S: float, T: float, rd: float, rf: float,
                         atm_vol: float, put_vol: float, call_vol: float,
                         *, delta: float = 0.25, delta_convention: str = "spot",
                         atm_convention: str | None = None) -> tuple[float, float, float]:
    """Return ``(K_put, K_atm, K_call)`` for one smile pillar set.

    Each wing strike is solved with **its own** vol (the smile vol at that delta),
    which is what makes the delta pillars self-consistent -- using the ATM vol for
    all three is a common and material bug for skewed pairs.

    ``delta_convention`` must be the pair's convention from ``conventions.PAIRS``;
    ``atm_convention`` defaults to the DNS variant matching it.
    """
    ac = atm_convention or atm_convention_for(delta_convention)
    k_atm = atm_strike(S, T, rd, rf, atm_vol, ac)
    k_put = gk.strike_from_delta(delta, S, T, rd, rf, put_vol, -1, delta_convention)
    k_call = gk.strike_from_delta(delta, S, T, rd, rf, call_vol, +1, delta_convention)
    return float(k_put), float(k_atm), float(k_call)


# --------------------------------------------------------------------------- #
# Breeden-Litzenberger
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DensityReport:
    """Result of :func:`risk_neutral_density`.

    Attributes
    ----------
    strikes, density : aligned arrays; ``density`` is the risk-neutral pdf of ``S_T``
        in units of 1/(quote ccy per base ccy).
    integral : trapezoidal integral of the density over ``strikes``.  Should be ~1
        (it is < 1 by exactly the mass outside the grid).
    min_density : most negative density value; < 0 means **butterfly arbitrage**.
    n_negative : count of negative grid points.
    ok : True when the density is non-negative to tolerance and integrates near 1.
    """
    strikes: np.ndarray
    density: np.ndarray
    integral: float
    min_density: float
    n_negative: int
    ok: bool
    note: str = ""
    peak_density: float = 0.0
    rel_min_density: float = 0.0    # min_density / peak_density -- the scale-free number


def risk_neutral_density(vol_fn: Callable[[np.ndarray], np.ndarray],
                         S: float, T: float, rd: float, rf: float,
                         strikes: np.ndarray | None = None, *,
                         n: int = 801, n_std: float = 6.0,
                         rtol: float = -1e-6,
                         atol: float | None = None) -> DensityReport:
    """Breeden-Litzenberger risk-neutral density implied by a smile.

    ``q(K) = e^{rd T} d^2 C / dK^2`` where ``C(K)`` is the undiscounted-notional call
    price at strike ``K`` computed at the *smile* vol ``vol_fn(K)``.  The second
    derivative is taken by central differences on a log-uniform strike grid, which
    is far better conditioned than a uniform one for FX (log-normal-ish underlyings).

    Parameters
    ----------
    vol_fn : callable
        Vectorised ``K -> sigma``.  Pass ``surface.slice`` partial or a lambda-free
        bound method; anything numpy-broadcastable works.
    strikes : optional explicit grid.  If ``None`` a ``n``-point log grid spanning
        ``n_std`` ATM standard deviations is built.
    rtol : negativity tolerance **relative to the peak density**, which is the only
        scale-free way to state it.  The density of ``S_T`` has units of 1/spot, so
        an absolute threshold means completely different things across pairs: a
        1-vol 1M EURUSD density peaks near 8.6 per USD while the same smile on
        USDJPY peaks near 0.027 per JPY, a factor of ~320.  A fixed ``atol =
        -1e-8`` therefore called USDJPY clean at 320x the relative violation it
        rejected on EURUSD.  ``rtol`` is applied as ``q < rtol * peak``.
    atol : optional absolute override, for callers that really do want a fixed
        threshold (or a stricter one than the relative default).  When given it is
        used instead of ``rtol * peak``.

    Returns
    -------
    DensityReport

    Caveats
    -------
    * A *smooth* smile is required.  Piecewise-linear-in-strike vols produce
      delta-function artefacts in ``q`` at the knots.
    * The integral is always slightly < 1: the tails outside the grid carry the
      rest.  At ``n_std = 6`` the deficit is ~1e-6 for typical G10 vols.
    * Negative density is the *definition* of butterfly (call-spread convexity)
      arbitrage; it is the single most useful smile sanity check on a desk.
    """
    F = float(S) * math.exp((rd - rf) * T)
    if strikes is None:
        s0 = float(np.asarray(vol_fn(np.array([F])), float).reshape(-1)[0])
        strikes = strike_grid(F, s0, T, n=n, n_std=n_std)
    K = np.asarray(strikes, float)
    if K.ndim != 1 or K.size < 5:
        raise ValueError("need at least 5 strictly increasing strikes")
    sig = np.asarray(vol_fn(K), float)
    C = np.asarray(gk.gk_price(S, K, T, rd, rf, sig, +1), float)

    # non-uniform central second difference
    h_l = K[1:-1] - K[:-2]
    h_r = K[2:] - K[1:-1]
    d2C = 2.0 * (h_l * C[2:] - (h_l + h_r) * C[1:-1] + h_r * C[:-2]) / (h_l * h_r * (h_l + h_r))
    q = math.exp(rd * T) * d2C
    Kc = K[1:-1]

    integral = float(np.trapezoid(q, Kc)) if hasattr(np, "trapezoid") else float(np.trapz(q, Kc))
    mn = float(np.nanmin(q)) if q.size else 0.0
    peak = float(np.nanmax(q)) if q.size else 0.0
    thr = float(atol) if atol is not None else float(rtol) * max(peak, 1e-300)
    nneg = int(np.sum(q < thr))
    ok = bool(nneg == 0 and 0.95 <= integral <= 1.02 and np.all(np.isfinite(q)))
    rel = mn / peak if peak > 0.0 else 0.0
    note = "" if ok else (
        f"{nneg} density point(s) below {thr:.3e}; min/peak={rel:.3e}; "
        f"integral={integral:.6f}"
    )
    return DensityReport(Kc, q, integral, mn, nneg, ok, note, peak, float(rel))


# --------------------------------------------------------------------------- #
# C1, Lee-bounded wing extrapolation  (shared by vanna_volga and interp)
# --------------------------------------------------------------------------- #
#
# The problem this solves
# -----------------------
# Every smile in this package is quoted on a finite set of pillars (25d/ATM/25d,
# plus 10d when available; or a listed chain's strike ladder) and has to be
# continued outside them.  The obvious continuations all fail, and they fail in
# ways that show up as *density spikes*, not as obviously wrong vols:
#
# * **Straight linear-in-w extrapolation with the boundary slope.**  If that slope
#   points the wrong way -- and for a 10d-anchored right wing it often does, e.g.
#   whenever the 10d call vol prints below the C1 continuation of the 25d point --
#   total variance runs to zero and then hits whatever floor the evaluator uses.
#   The floor join is a slope discontinuity: ``w'`` jumps, ``w''`` contains a
#   Dirac, and Breeden-Litzenberger puts a delta function in the density there.
#   Downstream, every strike beyond the crossing prices at the floor vol, so a
#   3M 172-strike USDJPY call quietly becomes worthless.
# * **Clipping the slope at Lee's bound at the join.**  Capping ``w'`` *at* the
#   join to satisfy Lee breaks C1 exactly where the two pieces meet -- the same
#   Dirac, just moved inward to a strike people actually trade.
#
# The fix: relax the slope instead of clipping it
# -----------------------------------------------
# Let ``u >= 0`` be the outward distance in log-moneyness from the join and
# ``q = dw/du`` the *outward* slope the inner curve arrives with (so for a right
# wing ``q = +w'(k_join)``, for a left wing ``q = -w'(k_join)``).  Set the
# asymptotic slope to ``beta = clip(q, 0, lee_cap)`` and use
#
#     w(u) = w_join + beta u + (q - beta) lam (1 - e^{-u/lam})
#
# which gives, exactly and unconditionally:
#
#   * ``w(0) = w_join``                              -- continuous,
#   * ``w'(0) = beta + (q - beta) = q``              -- **C1 at the join**, with no
#     clipping applied there, so the market-quoted boundary slope is honoured,
#   * ``w'(u) = beta + (q - beta) e^{-u/lam} -> beta`` monotonically -- the
#     asymptotic slope obeys **Lee's bound** and never has the wrong sign,
#   * ``w''(u) = -((q - beta)/lam) e^{-u/lam}`` -- bounded and *continuous* on the
#     wing, so no Dirac and no density spike,
#   * ``w(u) >= w_join + min(0, q) lam > 0`` -- positivity, enforced by shrinking
#     ``lam`` (below), so the ``max(w, floor)`` guard never engages and the floor
#     kink cannot happen.
#
# ``lam`` is a pure shape parameter -- the distance over which the slope relaxes
# from the quoted one to the asymptote.  It changes none of the guarantees above.
#
# What this is *not*: it is not an arbitrage-free extrapolation.  Lee's bound is
# necessary, not sufficient.  Always run :func:`risk_neutral_density`.


def wing_slope(w_fn: Callable[[float], float], k0: float, *, h: float = 1e-5) -> float:
    """Central difference ``dw/dk`` at ``k0``.

    Central rather than one-sided: the one-sided version is ``O(h)`` accurate, and
    an ``O(1e-5)`` error in the join slope is a *visible* kink in the density.
    Every smile core in this package is defined (as a formula) on both sides of
    its own pillars, so the central stencil is always legitimate.
    """
    return float((w_fn(k0 + h) - w_fn(k0 - h)) / (2.0 * h))


def fit_wing(k_join: float, w_join: float, q_out: float, *,
             lee_cap: float = LEE_CAP, lam: float = 0.25,
             keep: float = 0.75) -> tuple[float, float, float, float]:
    """Coefficients ``(w_join, q_out, beta, lam)`` of a C1 Lee-bounded wing.

    Parameters
    ----------
    k_join : log-moneyness of the join (kept by the caller; not used here beyond
        documentation of intent).
    w_join : total variance at the join.  Must be > 0.
    q_out : the inner curve's slope **measured outward** -- ``+dw/dk`` for a right
        wing, ``-dw/dk`` for a left wing.  Passed through untouched so the join
        stays C1.
    lee_cap : asymptotic-slope cap (see :data:`LEE_CAP`).
    lam : nominal relaxation length in log-moneyness.  Shrunk if positivity needs
        it; never grown.
    keep : the wing is not allowed to give up more than ``keep`` of ``w_join`` to
        a falling slope, which is what bounds ``lam`` from above when ``q_out < 0``.

    Returns
    -------
    (w_join, q_out, beta, lam) -- feed straight to :func:`eval_wing`.
    """
    w0 = max(float(w_join), 1e-14)
    q = float(q_out)
    if not math.isfinite(q):
        q = 0.0
    beta = float(min(max(q, 0.0), float(lee_cap)))
    lam_ = max(float(lam), 1e-6)
    if q < 0.0:
        # w(inf) = w0 + q*lam ; keep at least (1 - keep) * w0
        lam_ = min(lam_, float(keep) * w0 / (-q))
    return (w0, q, beta, max(lam_, 1e-9))


def eval_wing(u: Any, coef: tuple[float, float, float, float]) -> Any:
    """Total variance on the wing at outward distance ``u >= 0``.  Vectorised.

    ``w(u) = w_join + beta u + (q - beta) lam (1 - exp(-u/lam))``.
    """
    w0, q, beta, lam = coef
    uu = np.maximum(np.asarray(u, float), 0.0)
    return w0 + beta * uu + (q - beta) * lam * (-np.expm1(-uu / lam))


def _eval_wing_scalar(u: float, coef: tuple[float, float, float, float]) -> float:
    """Pure-python :func:`eval_wing` for the scalar fast paths."""
    w0, q, beta, lam = coef
    uu = u if u > 0.0 else 0.0
    return w0 + beta * uu - (q - beta) * lam * math.expm1(-uu / lam)


# --------------------------------------------------------------------------- #
# monotone cubic (Fritsch-Carlson / PCHIP) -- pure numpy so surfaces stay
# picklable, dependency-light and cheap in the ladder's inner loop.
# --------------------------------------------------------------------------- #
def pchip_slopes(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Fritsch-Carlson monotone-preserving slopes for a PCHIP through ``(x, y)``.

    Monotone interpolation matters here because an overshooting cubic through five
    smile pillars invents local convexity, which shows up immediately as negative
    Breeden-Litzenberger density in the wings.

    Reference: Fritsch, F. and Carlson, R. (1980), SIAM J. Numer. Anal. 17(2).
    """
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    n = x.size
    if n < 2:
        return np.zeros_like(y)
    h = np.diff(x)
    delta = np.diff(y) / h
    m = np.empty(n, dtype=float)
    if n == 2:
        m[:] = delta[0]
        return m
    # interior: weighted harmonic mean, zeroed at extrema
    w1 = 2.0 * h[1:] + h[:-1]
    w2 = h[1:] + 2.0 * h[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        m_int = (w1 + w2) / (w1 / delta[:-1] + w2 / delta[1:])
    m_int = np.where(delta[:-1] * delta[1:] > 0.0, m_int, 0.0)
    m[1:-1] = m_int
    # one-sided three-point ends, clipped to preserve monotonicity
    m[0] = ((2.0 * h[0] + h[1]) * delta[0] - h[0] * delta[1]) / (h[0] + h[1])
    if m[0] * delta[0] <= 0.0:
        m[0] = 0.0
    elif delta[0] * delta[1] <= 0.0 and abs(m[0]) > abs(3.0 * delta[0]):
        m[0] = 3.0 * delta[0]
    m[-1] = ((2.0 * h[-1] + h[-2]) * delta[-1] - h[-1] * delta[-2]) / (h[-1] + h[-2])
    if m[-1] * delta[-1] <= 0.0:
        m[-1] = 0.0
    elif delta[-1] * delta[-2] <= 0.0 and abs(m[-1]) > abs(3.0 * delta[-1]):
        m[-1] = 3.0 * delta[-1]
    return m


def pchip_eval(xq: Any, x: np.ndarray, y: np.ndarray, m: np.ndarray,
               *, extrap: str = "linear") -> np.ndarray:
    """Evaluate the PCHIP defined by ``(x, y, m)`` (slopes from :func:`pchip_slopes`).

    ``extrap`` is ``"linear"`` (continue the end slope -- the desk-sane choice for
    vols, giving a straight wing) or ``"flat"`` (hold the end vol).  Vectorised.
    """
    xq_arr = np.asarray(xq, float)
    x = np.asarray(x, float); y = np.asarray(y, float); m = np.asarray(m, float)
    idx = np.clip(np.searchsorted(x, xq_arr, side="right") - 1, 0, x.size - 2)
    h = x[idx + 1] - x[idx]
    t = (xq_arr - x[idx]) / h
    t2, t3 = t * t, t * t * t
    h00 = 2.0 * t3 - 3.0 * t2 + 1.0
    h10 = t3 - 2.0 * t2 + t
    h01 = -2.0 * t3 + 3.0 * t2
    h11 = t3 - t2
    out = h00 * y[idx] + h10 * h * m[idx] + h01 * y[idx + 1] + h11 * h * m[idx + 1]
    lo, hi = xq_arr < x[0], xq_arr > x[-1]
    if extrap == "flat":
        out = np.where(lo, y[0], out)
        out = np.where(hi, y[-1], out)
    else:
        out = np.where(lo, y[0] + m[0] * (xq_arr - x[0]), out)
        out = np.where(hi, y[-1] + m[-1] * (xq_arr - x[-1]), out)
    return out


# --------------------------------------------------------------------------- #
# shared VolSurface protocol plumbing
# --------------------------------------------------------------------------- #
class SmileSurfaceMixin:
    """Implements the ``VolSurface`` protocol on top of a single primitive.

    A concrete surface only has to provide

    * attributes ``spot``, ``rd``, ``rf``, ``delta_convention`` (and ``pair``,
      ``asof`` for the protocol), and
    * ``_vol_impl(K, T)`` -- vectorised in ``K``, scalar ``T``, returning decimals.

    The mixin then supplies ``vol``, ``slice``, ``vol_by_delta``, ``atm``, ``rr``,
    ``bf`` with the correct FX conventions: DNS ATM (premium-adjusted variant where
    the pair requires it) and *smile-consistent* delta strikes, i.e. the fixed point
    ``sigma = surface_vol(strike_from_delta(delta, sigma))``.  Getting that fixed
    point wrong (using the ATM vol to place the 25d strike) is the single most
    common way a surface fails to reprice its own risk reversal.

    No mutable state is stored, so subclasses stay ``frozen`` and picklable.
    """

    spot: float
    rd: float
    rf: float
    delta_convention: str

    # -- helpers ---------------------------------------------------------- #
    def forward(self, T: float) -> float:
        """Outright forward at ``T`` from the flat-curve rate differential."""
        return float(self.spot * math.exp((self.rd - self.rf) * max(T, 0.0)))

    def _vol_impl(self, K: Any, T: float) -> Any:      # pragma: no cover - abstract
        raise NotImplementedError

    # -- VolSurface protocol ---------------------------------------------- #
    def vol(self, K: float, T: float) -> float:
        """Implied vol (decimal) at strike ``K`` and expiry ``T`` years."""
        out = self._vol_impl(K, float(T))
        return float(out) if np.ndim(K) == 0 else out

    def slice(self, T: float, strikes: np.ndarray) -> np.ndarray:
        """Vectorised smile slice at expiry ``T`` -- the fast path for plotting and
        for the spot ladder."""
        return np.asarray(self._vol_impl(np.asarray(strikes, float), float(T)), float)

    def vol_by_delta(self, delta: float, T: float, cp: int) -> float:
        """Vol at a given delta, solving the strike/vol fixed point (see class doc).

        ``delta`` may be signed or a magnitude; ``cp`` decides the sign.  Returns
        ``nan`` if the delta is unattainable in the pair's convention (possible for
        premium-adjusted calls -- see :func:`gk.strike_from_delta`).
        """
        s = float(self.atm(T))
        for i in range(60):
            K = gk.strike_from_delta(delta, self.spot, T, self.rd, self.rf, s, cp,
                                     self.delta_convention)
            if not np.isfinite(K):
                return float("nan")
            s_new = float(self._vol_impl(float(K), float(T)))
            if abs(s_new - s) < 1e-13:
                return s_new
            s = 0.5 * (s + s_new) if i > 20 else s_new
        return s

    def strike_by_delta(self, delta: float, T: float, cp: int) -> float:
        """Smile-consistent strike at a given delta."""
        s = self.vol_by_delta(delta, T, cp)
        if not np.isfinite(s):
            return float("nan")
        return float(gk.strike_from_delta(delta, self.spot, T, self.rd, self.rf, s, cp,
                                          self.delta_convention))

    def atm_strike(self, T: float) -> float:
        """Delta-neutral-straddle strike, solving ``K = F exp(+/- sigma(K)^2 T / 2)``."""
        conv = atm_convention_for(self.delta_convention)
        s = float(self._vol_impl(self.forward(T), float(T)))
        K = self.forward(T)
        for _ in range(50):
            K_new = atm_strike(self.spot, T, self.rd, self.rf, s, conv)
            s_new = float(self._vol_impl(K_new, float(T)))
            if abs(K_new - K) < 1e-14 * max(1.0, K) and abs(s_new - s) < 1e-14:
                return float(K_new)
            K, s = K_new, s_new
        return float(K)

    def atm(self, T: float) -> float:
        """ATM vol on the FX (delta-neutral straddle) convention."""
        return float(self._vol_impl(self.atm_strike(T), float(T)))

    def rr(self, T: float, d: float = 0.25) -> float:
        """Risk reversal at delta ``d``: ``sigma(d call) - sigma(d put)``, decimal."""
        return float(self.vol_by_delta(d, T, +1) - self.vol_by_delta(d, T, -1))

    def bf(self, T: float, d: float = 0.25) -> float:
        """Smile butterfly at delta ``d``: ``mean(wing vols) - ATM``, decimal."""
        return float(0.5 * (self.vol_by_delta(d, T, +1) + self.vol_by_delta(d, T, -1))
                     - self.atm(T))

    # -- risk checks ------------------------------------------------------ #
    def density(self, T: float, *, n: int = 601, n_std: float = 5.0) -> DensityReport:
        """Breeden-Litzenberger density of this surface at expiry ``T``."""
        return risk_neutral_density(lambda K: self._vol_impl(K, float(T)),
                                    self.spot, float(T), self.rd, self.rf,
                                    n=n, n_std=n_std)
