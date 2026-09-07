"""Garman-Kohlhagen FX vanilla pricing, Greeks, implied vol and delta conventions.

References
----------
* Garman, M. and Kohlhagen, S. (1983), "Foreign Currency Option Values", JIF 2, 231-237.
* Clark, I. (2011), *Foreign Exchange Option Pricing: A Practitioner's Guide*, Wiley.
  Ch. 2 (pricing), Ch. 3 (delta conventions), Ch. 4 (Greeks).
* Reiswich, D. and Wystup, U. (2010), "A Guide to FX Options Quoting Conventions",
  Journal of Derivatives 18(2).  Definitive on premium-adjusted delta and the
  non-monotonicity of the premium-adjusted *call* delta.

Conventions (frozen, see ``docs/01_architecture.md`` s2)
-------------------------------------------------------
Pair is FORDOM, e.g. EURUSD -> base/foreign = EUR, quote/domestic = USD.
``S`` and ``K`` are in DOM per 1 FOR.  ``rd`` is the DOM continuously-compounded
zero, ``rf`` the FOR one.  ``T`` is in years (ACT/365F).  ``sigma`` is a decimal
(0.085 = 8.5%).  ``cp`` = +1 call on base, -1 put on base.  Notional is in base ccy.

All monetary outputs are in QUOTE (domestic) currency.

Greek units (the "desk units" the whole application depends on)
---------------------------------------------------------------
=================  =========================================================
``pv``             quote ccy, total (already x notional x direction)
``delta_pct``      spot delta per 1 unit of base notional, signed by direction
``delta_base``     base-ccy amount you are long; hedge = sell this much base
``gamma``          d(delta_base)/dS          [base ccy per 1.0 of spot]
``gamma_1pct``     change in ``delta_base`` for a +1% spot move  = gamma * S/100
``vega``           quote ccy per **1 vol point** (i.e. per 0.01 of sigma)
``theta``          quote ccy per **calendar day** (dV/dt, t = calendar time)
``rho_d``          quote ccy per **1 percentage point** (0.01) of rd
``rho_f``          quote ccy per **1 percentage point** (0.01) of rf
``vanna``          d(vega)/dS   -> quote ccy per vol point per 1.0 of spot
``volga``          d(vega)/dsigma per vol point -> quote ccy per vol point squared
``dual_delta``     d(pv)/dK, quote ccy per 1.0 of strike
=================  =========================================================

Everything except :func:`gk_greeks` (which fills the scalar ``Greeks`` dataclass)
is vectorised over numpy arrays; :func:`gk_greeks_array` is the vector version
used by the spot ladder, which calls it ~1e5 times per screen refresh.
"""
from __future__ import annotations

import math
from typing import Any, Final

import numpy as np
from scipy.special import erf, erfinv

from ..types import Greeks

__all__ = [
    "gk_price", "gk_greeks", "gk_greeks_array", "implied_vol",
    "strike_from_delta", "delta_from_strike", "forward", "d1_d2",
    "no_arb_bounds", "DELTA_CONVENTIONS", "SQRT_2PI",
]

SQRT_2PI: Final[float] = math.sqrt(2.0 * math.pi)
_INV_SQRT_2PI: Final[float] = 1.0 / SQRT_2PI
_INV_SQRT2: Final[float] = 1.0 / math.sqrt(2.0)

#: Minimum time to expiry we treat as "alive".  One second in ACT/365F terms.
T_MIN: Final[float] = 1.0 / (365.0 * 86400.0)
#: Minimum vol we treat as "alive".
VOL_MIN: Final[float] = 1e-12

DELTA_CONVENTIONS: Final[tuple[str, ...]] = ("spot", "spot_pa", "fwd", "fwd_pa")

#: 1 vol point = 0.01 of sigma.  Vega/vanna/volga are reported per vol point.
VOL_POINT: Final[float] = 0.01
#: Calendar days per year used for theta (ACT/365F, matches ``conventions.ACT``).
DAYS_PER_YEAR: Final[float] = 365.0
#: 1 rate point = 0.01 (100bp).  rho_d / rho_f are reported per rate point.
RATE_POINT: Final[float] = 0.01


# --------------------------------------------------------------------------- #
# normal distribution helpers (scipy-free so the hot path has no import cost)
# --------------------------------------------------------------------------- #
def _norm_pdf(x: np.ndarray | float) -> np.ndarray | float:
    """Standard normal pdf, vectorised."""
    return _INV_SQRT_2PI * np.exp(-0.5 * np.square(x))


def _norm_cdf(x: np.ndarray | float) -> np.ndarray | float:
    """Standard normal cdf via ``erf``; ~1e-16 accurate, vectorised."""
    return 0.5 * (1.0 + erf(np.asarray(x, dtype=float) * _INV_SQRT2))


def _norm_ppf(p: np.ndarray | float) -> np.ndarray | float:
    """Inverse standard normal cdf, machine accurate, via ``erfinv``."""
    return math.sqrt(2.0) * erfinv(2.0 * np.asarray(p, dtype=float) - 1.0)


def _scalarise(x: Any, *inputs: Any) -> Any:
    """Return a python float when every input was scalar, else the array."""
    if all(np.ndim(i) == 0 for i in inputs):
        return float(np.asarray(x).reshape(-1)[0])
    return x


# --------------------------------------------------------------------------- #
# core quantities
# --------------------------------------------------------------------------- #
def forward(S: Any, T: Any, rd: Any, rf: Any) -> Any:
    """Outright forward ``F = S * exp((rd - rf) * T)`` (quote ccy per base ccy)."""
    S, T, rd, rf = np.asarray(S, float), np.asarray(T, float), np.asarray(rd, float), np.asarray(rf, float)
    return _scalarise(S * np.exp((rd - rf) * T), S, T, rd, rf)


def d1_d2(S: Any, K: Any, T: Any, rd: Any, rf: Any, sigma: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(d1, d2, sqrt_T_sigma)`` with degenerate cases made finite.

    ``d1 = [ln(S/K) + (rd - rf + sigma^2/2) T] / (sigma sqrt(T))``, ``d2 = d1 - sigma sqrt(T)``.
    When ``T <= T_MIN`` or ``sigma <= VOL_MIN`` the total vol is floored so the caller
    can mask with :func:`_alive` instead of dealing with inf/nan.
    """
    S = np.asarray(S, dtype=float)
    K = np.asarray(K, dtype=float)
    T = np.asarray(T, dtype=float)
    rd = np.asarray(rd, dtype=float)
    rf = np.asarray(rf, dtype=float)
    sigma = np.asarray(sigma, dtype=float)

    Ts = np.maximum(T, T_MIN)
    sg = np.maximum(sigma, VOL_MIN)
    sqT = sg * np.sqrt(Ts)
    with np.errstate(divide="ignore", invalid="ignore"):
        lnSK = np.log(np.maximum(S, 1e-300) / np.maximum(K, 1e-300))
        d1 = (lnSK + (rd - rf) * Ts) / sqT + 0.5 * sqT
    d2 = d1 - sqT
    return d1, d2, sqT


def _alive(T: Any, sigma: Any) -> np.ndarray:
    """True where the option still has time value (T and sigma both > floor)."""
    return (np.asarray(T, float) > T_MIN) & (np.asarray(sigma, float) > VOL_MIN)


def no_arb_bounds(S: Any, K: Any, T: Any, rd: Any, rf: Any, cp: Any) -> tuple[Any, Any]:
    """No-arbitrage price bounds (quote ccy, per 1 unit base notional).

    Lower = discounted forward intrinsic ``max(cp (F - K) DF_d, 0)``,
    upper = ``S exp(-rf T)`` for a call and ``K exp(-rd T)`` for a put.
    Used by :func:`implied_vol` to decide when to return ``nan``.
    """
    S = np.asarray(S, float); K = np.asarray(K, float); T = np.asarray(T, float)
    rd = np.asarray(rd, float); rf = np.asarray(rf, float); cp = np.asarray(cp, float)
    dfd, dff = np.exp(-rd * T), np.exp(-rf * T)
    lo = np.maximum(cp * (S * dff - K * dfd), 0.0)
    hi = np.where(cp > 0, S * dff, K * dfd)
    return _scalarise(lo, S, K, T, rd, rf, cp), _scalarise(hi, S, K, T, rd, rf, cp)


# --------------------------------------------------------------------------- #
# price
# --------------------------------------------------------------------------- #
def gk_price(S: Any, K: Any, T: Any, rd: Any, rf: Any, sigma: Any, cp: Any) -> Any:
    """Garman-Kohlhagen value of a European FX vanilla, **per 1 unit of base notional**.

    ``V = cp [ S e^{-rf T} N(cp d1) - K e^{-rd T} N(cp d2) ]``  (quote ccy).

    Vectorised: any argument may be a numpy array; the result broadcasts.  Returns a
    python ``float`` when every argument is scalar (the frozen contract's ``-> float``).

    ``T <= 0`` or ``sigma <= 0`` collapses to the undiscounted intrinsic
    ``max(cp (S - K), 0)`` at ``T = 0`` and to the discounted forward intrinsic
    ``max(cp (F - K), 0) e^{-rd T}`` for a zero-vol but still-alive option, so the
    expiry ladder never produces NaN.

    Reference: Clark (2011) eq. (2.30).
    """
    S_ = np.asarray(S, float); K_ = np.asarray(K, float); T_ = np.asarray(T, float)
    rd_ = np.asarray(rd, float); rf_ = np.asarray(rf, float)
    sg_ = np.asarray(sigma, float); cp_ = np.asarray(cp, float)

    d1, d2, _ = d1_d2(S_, K_, T_, rd_, rf_, sg_)
    dfd = np.exp(-np.maximum(T_, 0.0) * rd_)
    dff = np.exp(-np.maximum(T_, 0.0) * rf_)
    live = cp_ * (S_ * dff * _norm_cdf(cp_ * d1) - K_ * dfd * _norm_cdf(cp_ * d2))
    dead = np.maximum(cp_ * (S_ * dff - K_ * dfd), 0.0)
    out = np.where(_alive(T_, sg_), live, dead)
    return _scalarise(out, S_, K_, T_, rd_, rf_, sg_, cp_)


# --------------------------------------------------------------------------- #
# Greeks
# --------------------------------------------------------------------------- #
def gk_greeks_array(S: Any, K: Any, T: Any, rd: Any, rf: Any, sigma: Any, cp: Any,
                    notional_base: Any = 1.0, direction: Any = 1,
                    *, delta_convention: str = "spot") -> dict[str, np.ndarray]:
    """Vectorised full Greek set.  Returns a dict of broadcast numpy arrays.

    Same maths and the same desk units as :func:`gk_greeks` (see module docstring);
    this is the version the spot ladder / scenario grid use, where the scalar
    dataclass construction would dominate runtime.

    Parameters
    ----------
    delta_convention
        One of ``{"spot", "spot_pa", "fwd", "fwd_pa"}``.  Controls **only**
        ``delta_pct`` / ``delta_base`` (the hedge ratio); every other Greek is
        convention-independent.  Default ``"spot"`` reproduces the frozen contract's
        behaviour exactly.  Premium-adjusted is required for USDJPY, USDCHF, USDCAD,
        USDSEK, USDNOK and EURJPY (see ``conventions.PAIRS``).
    """
    S = np.asarray(S, float); K = np.asarray(K, float); T = np.asarray(T, float)
    rd = np.asarray(rd, float); rf = np.asarray(rf, float)
    sg = np.asarray(sigma, float); cp = np.asarray(cp, float)
    N = np.asarray(notional_base, float)
    dr = np.asarray(direction, float)

    live = _alive(T, sg)
    Tpos = np.maximum(T, 0.0)
    d1, d2, sqT = d1_d2(S, K, T, rd, rf, sg)
    dfd, dff = np.exp(-Tpos * rd), np.exp(-Tpos * rf)
    pdf1 = _norm_pdf(d1)
    Ncp1, Ncp2 = _norm_cdf(cp * d1), _norm_cdf(cp * d2)
    sqrtT = np.sqrt(np.maximum(T, T_MIN))
    scale = N * dr                                      # monetary scaling

    # -- value ------------------------------------------------------------- #
    pv_unit = np.where(live,
                       cp * (S * dff * Ncp1 - K * dfd * Ncp2),
                       np.maximum(cp * (S * dff - K * dfd), 0.0))

    # -- delta (convention dependent) -------------------------------------- #
    delta_spot = cp * dff * Ncp1
    delta_fwd = cp * Ncp1
    # premium-adjusted:  Delta_pa = Delta_spot - V/S = cp (K/S) e^{-rd T} N(cp d2)
    with np.errstate(divide="ignore", invalid="ignore"):
        delta_spot_pa = cp * (K / np.maximum(S, 1e-300)) * dfd * Ncp2
    delta_fwd_pa = delta_spot_pa / np.maximum(dff, 1e-300)

    conv = str(delta_convention).lower()
    if conv == "spot":
        delta_unit = delta_spot
    elif conv == "fwd":
        delta_unit = delta_fwd
    elif conv == "spot_pa":
        delta_unit = delta_spot_pa
    elif conv == "fwd_pa":
        delta_unit = delta_fwd_pa
    else:
        raise ValueError(f"delta_convention must be one of {DELTA_CONVENTIONS}, got {delta_convention!r}")

    # at expiry the hedge ratio is the exercise indicator; premium-adjusted keeps the
    # K/S factor because the premium is settled in base ccy.
    itm = np.asarray(cp * (S - K) > 0.0, dtype=float)
    dead_delta = cp * itm
    if conv in ("spot_pa", "fwd_pa"):
        dead_delta = dead_delta * (K / np.maximum(S, 1e-300))
    delta_unit = np.where(live, delta_unit, dead_delta)

    # -- second order ------------------------------------------------------ #
    with np.errstate(divide="ignore", invalid="ignore"):
        gamma_unit = np.where(live, dff * pdf1 / (S * sg * sqrtT), 0.0)
        vega_unit = np.where(live, S * dff * pdf1 * sqrtT, 0.0)           # per 1.00 of sigma
        vanna_unit = np.where(live, -dff * pdf1 * d2 / sg, 0.0)           # d(dV/dsigma)/dS
        volga_unit = np.where(live, vega_unit * d1 * d2 / sg, 0.0)        # d(dV/dsigma)/dsigma

    # -- theta: dV/dt (calendar time), i.e. -dV/dT ------------------------- #
    theta_yr = np.where(
        live,
        -S * dff * pdf1 * sg / (2.0 * sqrtT)
        + cp * rf * S * dff * Ncp1
        - cp * rd * K * dfd * Ncp2,
        0.0,
    )

    # -- rates ------------------------------------------------------------- #
    rho_d_unit = np.where(live, cp * K * Tpos * dfd * Ncp2, 0.0)
    rho_f_unit = np.where(live, -cp * S * Tpos * dff * Ncp1, 0.0)

    # -- dual delta dV/dK -------------------------------------------------- #
    dual_unit = np.where(live, -cp * dfd * Ncp2, -cp * dfd * (cp * (S - K) > 0.0))

    delta_base = scale * delta_unit
    gamma = scale * gamma_unit
    return {
        "pv": scale * pv_unit,
        "delta_pct": dr * delta_unit,
        "delta_base": delta_base,
        "gamma": gamma,
        "gamma_1pct": gamma * S * 0.01,
        "vega": scale * vega_unit * VOL_POINT,
        "theta": scale * theta_yr / DAYS_PER_YEAR,
        "rho_d": scale * rho_d_unit * RATE_POINT,
        "rho_f": scale * rho_f_unit * RATE_POINT,
        "vanna": scale * vanna_unit * VOL_POINT,
        "volga": scale * volga_unit * VOL_POINT * VOL_POINT,
        "dual_delta": scale * dual_unit,
    }


def gk_greeks(S: float, K: float, T: float, rd: float, rf: float, sigma: float, cp: int,
              notional_base: float = 1.0, direction: int = 1,
              *, delta_convention: str = "spot") -> Greeks:
    """Full Greek set for one European FX vanilla, in desk units (see module docstring).

    Frozen contract signature (``docs/01_architecture.md`` s4) plus one **keyword-only,
    default-preserving** extra, ``delta_convention``: with its default ``"spot"`` the
    behaviour is byte-identical to the frozen signature, so no existing call site changes.

    Returns
    -------
    Greeks
        ``pv`` and every monetary Greek already multiplied by
        ``notional_base * direction``; ``delta_pct`` carries ``direction`` only.

    Examples
    --------
    >>> g = gk_greeks(1.10, 1.12, 0.25, 0.04, 0.02, 0.09, +1, 10_000_000, +1)
    >>> round(g.gamma_1pct)          # base ccy of delta gained per +1% spot
    851273
    """
    a = gk_greeks_array(S, K, T, rd, rf, sigma, cp, notional_base, direction,
                        delta_convention=delta_convention)
    return Greeks(**{k: float(np.asarray(v).reshape(-1)[0]) for k, v in a.items()})


# --------------------------------------------------------------------------- #
# implied volatility
# --------------------------------------------------------------------------- #
def implied_vol(price: float, S: float, K: float, T: float, rd: float, rf: float, cp: int,
                *, tol: float = 1e-10, lo: float = 1e-9, hi: float = 5.0,
                max_iter: int = 100) -> float:
    """Invert :func:`gk_price` for sigma.  Returns ``nan``, never raises, on bad input.

    Algorithm
    ---------
    1. Reject prices outside the no-arbitrage band (:func:`no_arb_bounds`) -> ``nan``.
       A price within ``1e-12`` of a bound returns the corresponding limit
       (``lo`` -> 0.0 vol at the lower bound is *not* returned; we return ``nan`` only
       when strictly outside, and clamp on the boundary).
    2. Work on the *undiscounted forward* Black price ``c = V / DF_d`` with forward ``F``.
       This removes rd/rf from the iteration entirely.
    3. Seed with the Brenner-Subrahmanyam / Corrado-Miller estimate, refine with
       safeguarded Newton (vega is analytic and always positive for ``sigma > 0``).
    4. If Newton leaves the bracket, stalls, or vega underflows (deep ITM/OTM, tiny T),
       fall back to Brent on a bracket grown from ``[lo, hi]`` until the sign changes.

    Parameters
    ----------
    price : quote ccy **per 1 unit of base notional** (i.e. undo notional/direction first).
    tol : absolute tolerance on |model price - target| in quote ccy.

    Notes
    -----
    Deep-wing prices carry very little vega, so the *vol* returned there is only as
    accurate as ``tol / vega``; the price round-trip is still exact to ``tol``.
    Reference: Jaeckel (2015), "Let's Be Rational", for the seeding idea; Clark (2011) s2.7.
    """
    if not np.isfinite([price, S, K, T, rd, rf]).all() or S <= 0 or K <= 0:
        return float("nan")
    cp = int(np.sign(cp)) or 1
    if T <= T_MIN:
        return float("nan")

    lb, ub = no_arb_bounds(S, K, T, rd, rf, cp)
    eps = 1e-12 * max(1.0, abs(ub))
    if price < lb - eps or price > ub + eps:
        return float("nan")
    price = min(max(price, lb), ub)
    if price <= lb + eps:            # zero time value -> zero vol
        return 0.0
    if price >= ub - eps:            # maximum time value -> unbounded vol
        return float("nan")

    dfd = math.exp(-rd * T)
    F = S * math.exp((rd - rf) * T)
    c = price / dfd                                    # undiscounted Black price
    sqrtT = math.sqrt(T)

    def _black(sig: float) -> float:
        if sig <= 0.0:
            return max(cp * (F - K), 0.0)
        v = sig * sqrtT
        d1 = math.log(F / K) / v + 0.5 * v
        d2 = d1 - v
        return cp * (F * float(_norm_cdf(cp * d1)) - K * float(_norm_cdf(cp * d2)))

    def _black_vega(sig: float) -> float:
        v = max(sig, VOL_MIN) * sqrtT
        d1 = math.log(F / K) / v + 0.5 * v
        return F * float(_norm_pdf(d1)) * sqrtT

    # ---- seed --------------------------------------------------------- #
    x = math.log(F / K)
    atm_c = c - max(cp * (F - K), 0.0) * 0.0
    sig = SQRT_2PI / sqrtT * (atm_c / F)               # Brenner-Subrahmanyam (ATM)
    if not (1e-4 < sig < 5.0) or abs(x) > 0.05:
        # Corrado-Miller style widening for away-from-the-money
        cc = c - 0.5 * cp * (F - K)
        rad = max(cc * cc - (F - K) ** 2 / math.pi, 0.0)
        sig = SQRT_2PI / (sqrtT * (F + K)) * (cc + math.sqrt(rad)) * 2.0
    if not np.isfinite(sig) or sig <= 0.0:
        sig = 0.2
    sig = min(max(sig, 1e-3), 3.0)

    # ---- safeguarded Newton ------------------------------------------- #
    a, b = lo, hi
    for _ in range(max_iter):
        diff = _black(sig) * dfd - price
        if abs(diff) < tol:
            return float(sig)
        if diff > 0.0:
            b = min(b, sig)
        else:
            a = max(a, sig)
        v = _black_vega(sig) * dfd
        if v < 1e-14:
            break
        step = diff / v
        nxt = sig - step
        if not np.isfinite(nxt) or nxt <= a or nxt >= b:
            break
        if abs(step) < 1e-14:
            return float(sig)
        sig = nxt

    # ---- Brent fallback with a grown bracket --------------------------- #
    from scipy.optimize import brentq   # deferred: only the fallback path needs it

    def _obj(s: float) -> float:
        return _black(s) * dfd - price

    a, b = lo, hi
    fa, fb = _obj(a), _obj(b)
    grow = 0
    while fa * fb > 0.0 and grow < 12:
        b *= 2.0
        fb = _obj(b)
        grow += 1
    if fa * fb > 0.0:
        return float("nan")
    try:
        return float(brentq(_obj, a, b, xtol=1e-14, rtol=1e-15, maxiter=300))
    except (ValueError, RuntimeError):
        return float("nan")


# --------------------------------------------------------------------------- #
# delta conventions
# --------------------------------------------------------------------------- #
def delta_from_strike(K: Any, S: Any, T: Any, rd: Any, rf: Any, sigma: Any, cp: Any,
                      convention: str = "spot") -> Any:
    """Signed delta of a vanilla in the requested FX convention (per 1 base notional).

    =========== ===================================================================
    ``spot``    ``cp e^{-rf T} N(cp d1)``          -- hedge in spot, premium in DOM
    ``fwd``     ``cp N(cp d1)``                    -- hedge in the forward
    ``spot_pa`` ``cp (K/S) e^{-rd T} N(cp d2)``    -- premium paid in FOR, spot hedge
    ``fwd_pa``  ``cp (K/F) N(cp d2)``              -- premium paid in FOR, fwd hedge
    =========== ===================================================================

    Premium-adjusted deltas subtract the FOR-denominated premium from the hedge:
    ``Delta_pa = Delta_spot - V_dom / S``.  Market convention (``conventions.PAIRS``)
    uses premium-adjusted for USD-base pairs quoted with USD premium: USDJPY, USDCHF,
    USDCAD, USDSEK, USDNOK, EURJPY.

    Vectorised over all arguments.  Reference: Reiswich-Wystup (2010) Table 1-2;
    Clark (2011) s3.3.
    """
    conv = str(convention).lower()
    if conv not in DELTA_CONVENTIONS:
        raise ValueError(f"convention must be one of {DELTA_CONVENTIONS}, got {convention!r}")
    S_ = np.asarray(S, float); K_ = np.asarray(K, float); T_ = np.asarray(T, float)
    rd_ = np.asarray(rd, float); rf_ = np.asarray(rf, float)
    sg_ = np.asarray(sigma, float); cp_ = np.asarray(cp, float)

    d1, d2, _ = d1_d2(S_, K_, T_, rd_, rf_, sg_)
    Tpos = np.maximum(T_, 0.0)
    dfd, dff = np.exp(-Tpos * rd_), np.exp(-Tpos * rf_)
    if conv == "spot":
        out = cp_ * dff * _norm_cdf(cp_ * d1)
    elif conv == "fwd":
        out = cp_ * _norm_cdf(cp_ * d1)
    elif conv == "spot_pa":
        out = cp_ * (K_ / S_) * dfd * _norm_cdf(cp_ * d2)
    else:  # fwd_pa
        out = cp_ * (K_ / S_) * (dfd / dff) * _norm_cdf(cp_ * d2)
    return _scalarise(out, S_, K_, T_, rd_, rf_, sg_, cp_)


def _pa_call_delta_peak(S: float, T: float, rd: float, rf: float, sigma: float) -> float:
    """Strike at which the premium-adjusted **call** delta is maximal.

    ``Delta_pa_call(K) = (K/S) e^{-rd T} N(d2(K))`` tends to 0 at *both* ends of the
    strike axis (``K -> 0``: the ``K`` factor wins; ``K -> inf``: ``N(d2)`` wins), so
    it has an interior maximum and is **two-to-one** in K.  Market convention
    (Reiswich-Wystup 2010 s3.2) selects the root on the **decreasing** branch, i.e.
    ``K >= K_peak``.

    ``d/dK [K N(d2)] = N(d2) - phi(d2) / (sigma sqrt(T))`` -- positive for small K,
    negative for large K (by the Mills-ratio asymptotic ``N(d2) ~ phi(d2)/|d2|``), so
    the peak is the unique sign change from ``+`` to ``-``, found by bisection in
    log-strike.  The ``K/S e^{-rd T}`` prefactor and the ``fwd_pa`` variant only
    rescale the delta, they do not move the peak.
    """
    sqT = sigma * math.sqrt(max(T, T_MIN))

    def g(lnK: float) -> float:
        _, d2, _ = d1_d2(S, math.exp(lnK), T, rd, rf, sigma)
        return float(_norm_cdf(d2)) - float(_norm_pdf(d2)) / sqT

    lo, hi = math.log(S) - 12.0 * sqT - 1.0, math.log(S) + 12.0 * sqT + 1.0
    if g(lo) <= 0.0:         # already on the decreasing branch at the left edge
        return math.exp(lo)
    if g(hi) > 0.0:          # genuinely still increasing at the right edge
        return math.exp(hi)
    # NB: strict `>`.  Far out of the money both N(d2) and phi(d2) underflow to
    # exactly 0.0, so g(hi) == 0.0 there; treating that tie as "still increasing"
    # would return the right-hand bracket edge as the peak and poison the Brent
    # bracket below, making every premium-adjusted call delta unsolvable (nan).
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if g(mid) > 0.0:     # still increasing -> peak is to the right
            lo = mid
        else:
            hi = mid
    return math.exp(0.5 * (lo + hi))


def strike_from_delta(delta: float, S: float, T: float, rd: float, rf: float, sigma: float,
                      cp: int, convention: str = "spot") -> float:
    """Strike with the given delta, inverting :func:`delta_from_strike`.

    ``delta`` may be supplied signed (``-0.25`` for a 25-delta put) or as a magnitude
    (``0.25``); the sign is always taken from ``cp``, so both spellings agree.

    Closed form for ``spot`` / ``fwd``
    ----------------------------------
    ``N(cp d1) = |delta| * e^{rf T}`` (spot) or ``|delta|`` (fwd), hence
    ``K = F exp(-cp Phi^{-1}(target) sigma sqrt(T) + sigma^2 T / 2)``.
    Returns ``nan`` when ``|delta| e^{rf T} >= 1`` (unreachable delta).

    Premium-adjusted (``spot_pa`` / ``fwd_pa``) -- solved numerically
    ----------------------------------------------------------------
    ``|delta| = (K/S) e^{-rd T} N(cp d2)`` is *implicit* in K.  For **puts** the map is
    strictly monotone and Brent on ``[K_lo, K_hi]`` is unique.  For **calls** it is
    unimodal with an interior maximum at ``K_peak`` (see :func:`_pa_call_delta_peak`);
    we take the market-standard root on ``[K_peak, K_hi]``.  If the requested delta
    exceeds the attainable maximum the function returns ``nan`` rather than a wrong root.

    Reference: Reiswich-Wystup (2010) s3.2 and eq. (23)-(28); Clark (2011) s3.4.
    """
    conv = str(convention).lower()
    if conv not in DELTA_CONVENTIONS:
        raise ValueError(f"convention must be one of {DELTA_CONVENTIONS}, got {convention!r}")
    cp = int(np.sign(cp)) or 1
    dl = abs(float(delta))
    if not (0.0 < dl < 1.0) or S <= 0 or sigma <= 0 or T <= T_MIN:
        return float("nan")

    F = S * math.exp((rd - rf) * T)
    sqT = sigma * math.sqrt(T)

    if conv in ("spot", "fwd"):
        target = dl * math.exp(rf * T) if conv == "spot" else dl
        if not (0.0 < target < 1.0):
            return float("nan")
        d1 = cp * float(_norm_ppf(target))
        return float(F * math.exp(-d1 * sqT + 0.5 * sqT * sqT))

    # ---- premium-adjusted: implicit, solve numerically ------------------ #
    from scipy.optimize import brentq

    def f(K: float) -> float:
        return abs(float(delta_from_strike(K, S, T, rd, rf, sigma, cp, conv))) - dl

    k_lo = F * math.exp(-12.0 * sqT - 0.5 * sqT * sqT)
    k_hi = F * math.exp(+12.0 * sqT + 0.5 * sqT * sqT)
    if cp > 0:
        k_peak = _pa_call_delta_peak(S, T, rd, rf, sigma)
        peak_val = abs(float(delta_from_strike(k_peak, S, T, rd, rf, sigma, cp, conv)))
        if peak_val < dl:
            return float("nan")          # delta unattainable on the pa call branch
        a, b = max(k_peak, 1e-12), k_hi
    else:
        a, b = k_lo, k_hi
    fa, fb = f(a), f(b)
    if fa * fb > 0.0:
        return float("nan")
    try:
        return float(brentq(f, a, b, xtol=1e-14, rtol=1e-15, maxiter=300))
    except (ValueError, RuntimeError):
        return float("nan")
