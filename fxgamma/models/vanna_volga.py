"""Vanna-volga smile construction -- the FX market standard for turning three
broker quotes (ATM, 25d risk reversal, 25d butterfly) into a full smile.

Why vanna-volga is the FX standard
----------------------------------
FX brokers quote *three* liquid instruments per tenor: the ATM (delta-neutral)
straddle, the 25-delta risk reversal and the 25-delta butterfly.  Vanna-volga
(VV) answers exactly the question a market maker faces: *given that I can hedge
vega, vanna and volga with those three instruments, what does a fourth option
cost?*  The construction is (i) exact at the three pillars by design, (ii)
model-free -- no calibration, no optimiser, no parameters to fit, (iii) fast
enough to rebuild a whole surface on every tick, and (iv) it reproduces the
market's own hedging cost logic rather than an assumed dynamic.  That is why it
survives on desks even though it is not an arbitrage-free model.

Method
------
Let ``K1 < K2 < K3`` be the 25d put, ATM(DNS) and 25d call strikes with market
vols ``s1, s2, s3``.  Build a portfolio of the three market options that matches
the vega, vanna and volga of the target option at the *ATM* vol ``s2``.  The
weights reduce to Lagrange quadratic weights in log-strike times a vega ratio:

    y1(K) = ln(K2/K) ln(K3/K) / [ln(K2/K1) ln(K3/K1)]
    y2(K) = ln(K/K1) ln(K3/K) / [ln(K2/K1) ln(K3/K2)]
    y3(K) = ln(K/K1) ln(K/K2) / [ln(K3/K1) ln(K3/K2)]
    x_i(K) = y_i(K) * vega(K, s2) / vega(K_i, s2)

*Exact VV price* (``method="exact"``):

    C_VV(K) = C_BS(K, s2) + sum_i x_i(K) [ C_BS(K_i, s_i) - C_BS(K_i, s2) ]

then invert Black-Scholes for the implied vol.

*Second-order VV vol* (``method="approx"``, the default -- Castagna & Mercurio
2007 eq. 14):

    D1(K) = y1 s1 + y2 s2 + y3 s3 - s2                       (first order)
    D2(K) = y1 d1(K1)d2(K1)(s1-s2)^2 + y3 d1(K3)d2(K3)(s3-s2)^2
    sigma(K) = s2 + [ -s2 + sqrt( s2^2 + d1(K)d2(K)(2 s2 D1 + D2) ) ] / (d1(K) d2(K))

with ``d1, d2`` evaluated at ``s2``.  Both are **exact at K1, K2, K3** (at ``K_i``
the radicand collapses to ``(s2 + d1 d2 (s_i - s2))^2``), agree to ~1e-5 vol over
the 10d-10d range, and the approximation is ~40x faster because it needs no
implied-vol inversion.

Known failure modes (do not skip this)
--------------------------------------
1. **Far wings.**  The quadratic-in-log-strike term ``D1`` keeps growing outside
   ``[K1, K3]``, so beyond roughly 5-delta the raw VV vol explodes and the implied
   density goes negative.  We therefore do **not** evaluate VV outside its pillars
   at all: past the 25d strikes the smile switches to the C1, Lee-bounded
   total-variance wing described in :class:`VannaVolgaSmile` (10d-anchored
   quadratic, then an exponentially relaxing tail).  That removes the explosion
   and the ``max(w, floor)`` kink, but it is an *extrapolation rule*, not an
   arbitrage-free model: always run
   :func:`~fxgamma.models.smile.risk_neutral_density` before trusting a wing price.
2. **Very short tenors (< ~1W).**  ``d1 d2`` is large and the second-order term
   dominates; the radicand can go negative.  We fall back to first order there.
3. **Long tenors (> ~2Y).**  VV systematically over-prices convexity because the
   vega/vanna/volga hedge is assumed to be held to maturity at constant cost.
4. **No arbitrage guarantee at all.**  VV is an interpolation-by-hedging-cost
   rule, not a model.  Butterfly arbitrage is possible for large ``|RR|`` combined
   with small ``BF``; calendar arbitrage is possible between tenors.  Both are
   *measured*, not prevented -- see ``VannaVolgaSurface.diagnostics()``.
5. **Premium-adjusted pairs.**  The pillar strikes depend on the delta convention;
   using spot delta for USDJPY shifts ``K1``/``K3`` by ~0.5-1% of spot, which is a
   larger error than the whole butterfly.

References
----------
* Castagna, A. and Mercurio, F. (2007), "The Vanna-Volga Method for Implied
  Volatilities", *Risk* 20(1), 106-111.
* Castagna, A. and Mercurio, F. (2006), "Consistent Pricing of FX Options",
  Banca IMI working paper.
* Clark, I. (2011), *FX Option Pricing*, s3.5.
* Wystup, U. (2006), *FX Options and Structured Products*, s1.5.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np

from . import gk, smile

__all__ = ["VannaVolgaSmile", "vv_weights", "vv_vol", "vv_price",
           "market_to_smile_bf", "VV_METHODS"]

VV_METHODS: Final[tuple[str, ...]] = ("approx", "exact")

_VOL_FLOOR: Final[float] = 1e-4      # 0.01 vol point
_VOL_CAP: Final[float] = 5.0         # 500 vol


# --------------------------------------------------------------------------- #
# core VV algebra (free functions -- vectorised, no state)
# --------------------------------------------------------------------------- #
def vv_weights(K: Any, K1: float, K2: float, K3: float) -> tuple[Any, Any, Any]:
    """Lagrange quadratic weights ``(y1, y2, y3)`` in log-strike.

    ``y_i(K_j) = delta_ij``, ``sum_i y_i(K) = 1`` for every ``K``.  These are the
    vega-normalised vanna-volga replication weights (Castagna-Mercurio 2007 eq. 6).
    """
    lk = np.log(np.asarray(K, float))
    l1, l2, l3 = math.log(K1), math.log(K2), math.log(K3)
    y1 = (l2 - lk) * (l3 - lk) / ((l2 - l1) * (l3 - l1))
    y2 = (lk - l1) * (l3 - lk) / ((l2 - l1) * (l3 - l2))
    y3 = (lk - l1) * (lk - l2) / ((l3 - l1) * (l3 - l2))
    return y1, y2, y3


def vv_vol(K: Any, S: float, T: float, rd: float, rf: float,
           K1: float, K2: float, K3: float, s1: float, s2: float, s3: float) -> Any:
    """Second-order vanna-volga implied vol (Castagna-Mercurio 2007 eq. 14).

    Exact at ``K1, K2, K3``.  Falls back to the first-order value ``s2 + D1`` when
    ``|d1 d2|`` underflows or the radicand turns negative (very short tenors, far
    wings).  Vectorised in ``K``; all other arguments are scalars.

    Returns vol as a decimal, clipped to ``[1e-4, 5.0]``.
    """
    Karr = np.asarray(K, float)
    y1, y2, y3 = vv_weights(Karr, K1, K2, K3)
    D1 = y1 * s1 + y2 * s2 + y3 * s3 - s2

    d1K, d2K, _ = gk.d1_d2(S, Karr, T, rd, rf, s2)
    a1, b1, _ = gk.d1_d2(S, K1, T, rd, rf, s2)
    a3, b3, _ = gk.d1_d2(S, K3, T, rd, rf, s2)
    D2 = y1 * (a1 * b1) * (s1 - s2) ** 2 + y3 * (a3 * b3) * (s3 - s2) ** 2

    p = d1K * d2K
    first = s2 + D1
    # Stable rearrangement of Castagna-Mercurio eq. (14).  Written literally as
    #     s2 + (-s2 + sqrt(s2^2 + p (2 s2 D1 + D2))) / p
    # the numerator is a difference of two nearly equal positive numbers whenever
    # |p| is small -- i.e. right next to the two strikes where d1 = 0 or d2 = 0,
    # which in FX sit within a few tenths of a percent of the ATM.  There it loses
    # most of its significant digits, and the old guard `|p| > 1e-12` papered over
    # that by *switching branch* to the first-order value, putting a small but real
    # discontinuity in the smile a hair away from the money.
    #
    # Multiplying through by the conjugate removes the cancellation and the
    # division by p together:
    #     (-s2 + sqrt(rad)) / p = (rad - s2^2) / (p (s2 + sqrt(rad)))
    #                           = (2 s2 D1 + D2) / (s2 + sqrt(rad))
    # which is exact and continuous at p = 0 (where it collapses to the correct
    # limit D1 + D2/(2 s2)).  No branch on p is needed at all now.
    with np.errstate(invalid="ignore", divide="ignore"):
        rad = s2 * s2 + p * (2.0 * s2 * D1 + D2)
        second = s2 + (2.0 * s2 * D1 + D2) / (s2 + np.sqrt(np.maximum(rad, 0.0)))
    out = np.where(np.isfinite(second) & (rad > 0.0), second, first)
    out = np.clip(out, _VOL_FLOOR, _VOL_CAP)
    return out if np.ndim(K) else float(out)


def vv_price(K: Any, cp: Any, S: float, T: float, rd: float, rf: float,
             K1: float, K2: float, K3: float, s1: float, s2: float, s3: float) -> Any:
    """Exact vanna-volga *price* (quote ccy per 1 base notional).

    ``C_VV(K) = C_BS(K, s2) + sum_i x_i(K) [C_BS(K_i, s_i) - C_BS(K_i, s2)]``.
    Put-call parity is preserved because the correction term is strike-and-
    convention symmetric (the same replication applies to both), so the exact-VV
    implied vol is automatically the same for a call and a put at one strike.
    """
    Karr = np.asarray(K, float)
    y1, y2, y3 = vv_weights(Karr, K1, K2, K3)
    vK = _bs_vega(S, Karr, T, rd, rf, s2)
    v1 = _bs_vega(S, K1, T, rd, rf, s2)
    v2 = _bs_vega(S, K2, T, rd, rf, s2)
    v3 = _bs_vega(S, K3, T, rd, rf, s2)
    with np.errstate(divide="ignore", invalid="ignore"):
        x1 = y1 * vK / v1
        x2 = y2 * vK / v2
        x3 = y3 * vK / v3
    base = gk.gk_price(S, Karr, T, rd, rf, s2, cp)
    corr = (x1 * (gk.gk_price(S, K1, T, rd, rf, s1, cp) - gk.gk_price(S, K1, T, rd, rf, s2, cp))
            + x2 * (gk.gk_price(S, K2, T, rd, rf, s2, cp) - gk.gk_price(S, K2, T, rd, rf, s2, cp))
            + x3 * (gk.gk_price(S, K3, T, rd, rf, s3, cp) - gk.gk_price(S, K3, T, rd, rf, s2, cp)))
    return base + corr


def _bs_vega(S: Any, K: Any, T: float, rd: float, rf: float, sigma: float) -> Any:
    """Raw Black-Scholes vega ``dV/dsigma`` (per 1.00 of sigma, per 1 base notional)."""
    d1, _, _ = gk.d1_d2(S, K, T, rd, rf, sigma)
    return np.asarray(S, float) * math.exp(-rf * max(T, 0.0)) * gk._norm_pdf(d1) * math.sqrt(max(T, gk.T_MIN))


# --------------------------------------------------------------------------- #
# market strangle <-> smile strangle
# --------------------------------------------------------------------------- #
def market_to_smile_bf(atm: float, rr: float, bf_market: float, S: float, T: float,
                       rd: float, rf: float, *, delta: float = 0.25,
                       delta_convention: str = "spot",
                       atm_convention: str | None = None,
                       tol: float = 1e-12) -> float:
    """Convert a **market strangle** butterfly quote into the **smile strangle**
    butterfly this library consumes.

    The two are different objects and confusing them is the classic FX vol-surface
    bug (it typically moves 25d wings by 0.05-0.30 vol, more for high-skew pairs):

    * **Smile strangle (SS)** -- a pure vol definition:
      ``bf_ss = (sigma(K_25c) + sigma(K_25p)) / 2 - sigma_atm``.  Algebraic, what
      :func:`~fxgamma.models.smile.rr_bf_to_vols` uses.
    * **Market strangle (MS)** -- a *price* definition, and what brokers actually
      trade: the strangle whose two legs are both struck at 25 delta computed with
      the single vol ``sigma_atm + bf_ms`` and both priced at that same vol.

    The smile must reprice the market strangle:

        V(K_ms_p, sigma_smile(K_ms_p)) + V(K_ms_c, sigma_smile(K_ms_c))
            = V(K_ms_p, sigma_atm + bf_ms) + V(K_ms_c, sigma_atm + bf_ms)

    We solve that one-dimensional equation for ``bf_ss`` by Brent, rebuilding the
    VV smile at each iteration (so the pillar strikes stay self-consistent).

    Returns
    -------
    float
        ``bf_ss``, a decimal.  Returns ``bf_market`` unchanged if the solve fails
        (with the caller free to check via a reprice).

    Reference: Clark (2011) s3.4.3; Reiswich-Wystup (2010) s4.
    """
    from scipy.optimize import brentq

    ac = atm_convention or smile.atm_convention_for(delta_convention)
    s_ms = atm + bf_market
    k_ms_p = gk.strike_from_delta(delta, S, T, rd, rf, s_ms, -1, delta_convention)
    k_ms_c = gk.strike_from_delta(delta, S, T, rd, rf, s_ms, +1, delta_convention)
    if not (np.isfinite(k_ms_p) and np.isfinite(k_ms_c)):
        return float(bf_market)
    target = (gk.gk_price(S, k_ms_p, T, rd, rf, s_ms, -1)
              + gk.gk_price(S, k_ms_c, T, rd, rf, s_ms, +1))

    def resid(bf_ss: float) -> float:
        sm = VannaVolgaSmile.from_quotes(S, T, rd, rf, atm, rr, bf_ss,
                                         delta=delta, delta_convention=delta_convention,
                                         atm_convention=ac)
        vp = float(sm.vol(k_ms_p))
        vc = float(sm.vol(k_ms_c))
        return float(gk.gk_price(S, k_ms_p, T, rd, rf, vp, -1)
                     + gk.gk_price(S, k_ms_c, T, rd, rf, vc, +1) - target)

    lo, hi = bf_market - 0.05, bf_market + 0.05
    try:
        flo, fhi = resid(lo), resid(hi)
        if flo * fhi > 0.0:
            return float(bf_market)
        return float(brentq(resid, lo, hi, xtol=tol, rtol=1e-14, maxiter=200))
    except (ValueError, RuntimeError):
        return float(bf_market)


# --------------------------------------------------------------------------- #
# the smile object
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VannaVolgaSmile:
    """A single-tenor FX smile.  Immutable, picklable, cheap to evaluate.

    Built by :meth:`from_quotes`.

    Shape (three regions per side, all joined ``C^1`` in total variance)
    --------------------------------------------------------------------
    Everything below is done on ``w(k) = sigma^2 T`` against forward log-moneyness
    ``k = ln(K/F)``, because that is the variable in which "no arbitrage" is
    expressible (Lee's bound, Gatheral's ``g(k)``) and in which the density is a
    smooth functional.

    1. **Core**, ``k1 <= k <= k3`` (25d put to 25d call): the vanna-volga vol --
       the second-order formula, or the exact replication price inverted.
    2. **Anchor region**, ``k3 < k <= kR`` (and mirrored on the left): the unique
       quadratic in ``k`` matching *value and slope* at ``k3`` and *passing through
       the 10d quote* at ``kR``.  All five broker quotes therefore reprice exactly.
       Absent 10d quotes this region is empty and ``kR = k3``.
    3. **Tail**, ``k > kR``: with ``u = k - kR`` and ``q`` the anchor region's
       slope arriving at ``kR``,

           w(u) = w(kR) + beta u + (q - beta) lam (1 - e^{-u/lam}),
           beta = clip(q, 0, lee_cap).

       This matches value *and slope* at ``kR`` with **no clipping at the join**,
       relaxes monotonically to an asymptotic slope inside **Lee's moment bound**,
       keeps ``w'' `` bounded and continuous, and is bounded below by
       ``w(kR) + min(0,q) lam > 0``.  See
       :func:`~fxgamma.models.smile.fit_wing` for the derivation and for why the
       obvious alternatives (raw linear continuation; clipping the slope at the
       join) both put a Dirac in ``w''`` and hence a spike in the
       Breeden-Litzenberger density.

    What this buys, concretely: the previous "damped wing" clipped ``dw/dk`` to
    ``+/- lee_cap`` at the join and let a *negative* right-wing slope run to
    ``w = 0``, where a ``max(w, 1e-12)`` floor took over.  On a 3M USDJPY smile
    with atm 12 / rr25 -5 / bf25 0.4 / rr10 -9 / bf10 1.2 that floor engaged at
    ``K = 172`` and every strike above it priced at a 0.01% vol.  It now flattens
    to a constant total variance instead, and the density is smooth across both
    joins.

    ``lam`` (the relaxation length) is a pure shape parameter -- it does not affect
    C1-ness, the Lee bound or positivity -- and is set to
    ``max(2 |k_join|, 0.10)``, i.e. the tail flattens over roughly the distance
    from the money to the join again, floored at 10% log-moneyness.

    Attributes
    ----------
    S, T, rd, rf : market state for this tenor.
    strikes, vols : pillar strikes and their market vols (3, or 5 with 10d quotes),
        strictly increasing in strike.  These are the points the smile reprices exactly.
    atm_vol : the ATM (DNS) vol -- the ``s2`` of the VV algebra.
    method : ``"approx"`` (second-order formula) or ``"exact"`` (price replication).
    """

    S: float
    T: float
    rd: float
    rf: float
    strikes: np.ndarray
    vols: np.ndarray
    atm_vol: float
    atm_strike_: float
    method: str = "approx"
    delta: float = 0.25
    delta_convention: str = "spot"
    lee_cap: float = 2.0
    #: Flat wing coefficients, laid out as
    #: ``(k1, k3, kL, kR, w1, w3, p1, p3, cL, cR,
    #:    wLj, qL, betaL, lamL, wRj, qR, betaR, lamR)``.
    #: ``p1``/``p3`` are ``dw/dk`` at the 25d joins (central-differenced off the
    #: core), ``cL``/``cR`` the anchor-region quadratic coefficients, and the last
    #: eight the two :func:`~fxgamma.models.smile.fit_wing` tails (``q`` is the
    #: *outward* slope, so ``qL = -dw/dk`` at ``kL``).  Plain floats -> picklable.
    _wing: tuple[float, ...] = field(default=(), repr=False)
    #: (l1, l2, l3, dn1, dn2, dn3, s1, s2, s3, A1, A3, c0, sqT) -- VV scalar fast path
    _fast: tuple[float, ...] = field(default=(), repr=False)

    # -- construction ---------------------------------------------------- #
    @classmethod
    def from_quotes(cls, S: float, T: float, rd: float, rf: float,
                    atm: float, rr25: float, bf25: float,
                    rr10: float | None = None, bf10: float | None = None,
                    *, delta: float = 0.25, delta_convention: str = "spot",
                    atm_convention: str | None = None, method: str = "approx",
                    bf_convention: str = "smile", lee_cap: float = 2.0
                    ) -> "VannaVolgaSmile":
        """Build a smile from broker quotes for one tenor.

        Parameters
        ----------
        atm, rr25, bf25 : decimals (0.085, -0.0035, 0.0022).
        rr10, bf10 : optional 10-delta quotes.  When both are given the wings are
            anchored to them (see the class docstring); the core stays 3-point VV.
        delta_convention : the pair's convention, ``{"spot","spot_pa","fwd","fwd_pa"}``.
        atm_convention : defaults to the DNS variant matching ``delta_convention``.
        bf_convention : ``"smile"`` (default, algebraic) or ``"market"``, in which
            case the butterflies are first converted via :func:`market_to_smile_bf`.
        method : ``"approx"`` | ``"exact"`` -- see the module docstring.
        lee_cap : cap on ``|dw/dk|`` in the wings (Lee's moment bound is 2.0).
        """
        if method not in VV_METHODS:
            raise ValueError(f"method must be one of {VV_METHODS}, got {method!r}")
        ac = atm_convention or smile.atm_convention_for(delta_convention)

        if bf_convention == "market":
            bf25 = market_to_smile_bf(atm, rr25, bf25, S, T, rd, rf, delta=delta,
                                      delta_convention=delta_convention, atm_convention=ac)
            if bf10 is not None and rr10 is not None:
                bf10 = market_to_smile_bf(atm, rr10, bf10, S, T, rd, rf, delta=0.10,
                                          delta_convention=delta_convention, atm_convention=ac)
        elif bf_convention != "smile":
            raise ValueError("bf_convention must be 'smile' or 'market'")

        p25, c25 = smile.rr_bf_to_vols(atm, rr25, bf25)
        k_p, k_atm, k_c = smile.delta_pillar_strikes(
            S, T, rd, rf, atm, p25, c25, delta=delta,
            delta_convention=delta_convention, atm_convention=ac)
        core_K = np.array([k_p, k_atm, k_c], float)
        core_V = np.array([p25, atm, c25], float)
        if not np.all(np.diff(core_K) > 0.0) or not np.all(np.isfinite(core_K)):
            raise ValueError(f"non-monotone or non-finite pillar strikes: {core_K}")

        l1, l2, l3 = (math.log(float(k)) for k in core_K)
        s1, s2, s3 = (float(v) for v in core_V)
        a1, b1, _ = gk.d1_d2(S, float(core_K[0]), T, rd, rf, s2)
        a3, b3, _ = gk.d1_d2(S, float(core_K[2]), T, rd, rf, s2)
        fast = (l1, l2, l3,
                (l2 - l1) * (l3 - l1), (l2 - l1) * (l3 - l2), (l3 - l1) * (l3 - l2),
                s1, s2, s3,
                float(a1 * b1) * (s1 - s2) ** 2, float(a3 * b3) * (s3 - s2) ** 2,
                math.log(S) + (rd - rf) * T, s2 * math.sqrt(max(T, gk.T_MIN)))

        obj = cls(float(S), float(T), float(rd), float(rf), core_K, core_V, float(atm),
                  float(k_atm), method, float(delta), str(delta_convention),
                  float(lee_cap), (), fast)

        # ---- wing anchors -------------------------------------------- #
        F = obj.forward
        k1, k3 = math.log(core_K[0] / F), math.log(core_K[2] / F)
        kL = kR = float("nan")
        wL = wR = 0.0
        pill_K, pill_V = list(core_K), list(core_V)
        if rr10 is not None and bf10 is not None:
            p10, c10 = smile.rr_bf_to_vols(atm, rr10, bf10)
            k_p10 = gk.strike_from_delta(0.10, S, T, rd, rf, p10, -1, delta_convention)
            k_c10 = gk.strike_from_delta(0.10, S, T, rd, rf, c10, +1, delta_convention)
            if np.isfinite(k_p10) and np.isfinite(k_c10) and k_p10 < core_K[0] < core_K[2] < k_c10:
                kL, wL = math.log(k_p10 / F), p10 * p10 * T
                kR, wR = math.log(k_c10 / F), c10 * c10 * T
                pill_K = [float(k_p10)] + pill_K + [float(k_c10)]
                pill_V = [float(p10)] + pill_V + [float(c10)]

        wing = obj._build_wing(k1, k3, kL, wL, kR, wR)
        return cls(float(S), float(T), float(rd), float(rf),
                   np.asarray(pill_K, float), np.asarray(pill_V, float), float(atm),
                   float(k_atm), method, float(delta), str(delta_convention),
                   float(lee_cap), wing, fast)

    def _build_wing(self, k1: float, k3: float, kL: float, wL: float,
                    kR: float, wR: float) -> tuple[float, ...]:
        """Precompute the C1, Lee-bounded wing coefficients (see the class docstring).

        ``kL``/``kR`` are the 10-delta anchors in log-moneyness (``nan`` when no 10d
        quotes were supplied) and ``wL``/``wR`` their total variances.
        """
        T = self.T
        cap = float(self.lee_cap)
        F = self.forward

        def core_w(k: float) -> float:
            v = float(self._core_vol(np.array([F * math.exp(k)]))[0])
            return v * v * T

        w1, w3 = core_w(k1), core_w(k3)
        # Central differences: a one-sided stencil is only O(h) accurate and an
        # O(1e-5) error in the join slope is a visible kink in the density.  The VV
        # core is an analytic formula on both sides of its own pillars, so the
        # central stencil is legitimate here.
        p1 = smile.wing_slope(core_w, k1)
        p3 = smile.wing_slope(core_w, k3)

        cR, kR_, wR_, qR = self._anchor(k3, w3, p3, kR, wR, +1)
        cL, kL_, wL_, qL = self._anchor(k1, w1, p1, kL, wL, -1)

        lamR = max(2.0 * abs(kR_), 0.10)
        lamL = max(2.0 * abs(kL_), 0.10)
        wRj, qRo, betaR, lamR = smile.fit_wing(kR_, wR_, qR, lee_cap=cap, lam=lamR)
        wLj, qLo, betaL, lamL = smile.fit_wing(kL_, wL_, qL, lee_cap=cap, lam=lamL)
        return (k1, k3, kL_, kR_, w1, w3, p1, p3, cL, cR,
                wLj, qLo, betaL, lamL, wRj, qRo, betaR, lamR)

    @staticmethod
    def _anchor(kj: float, wj: float, pj: float, ka: float, wa: float,
                side: int) -> tuple[float, float, float, float]:
        """Fit the 10d anchor quadratic on one side.

        Returns ``(c, k_anchor, w_anchor, q_out)`` where ``c`` is the quadratic's
        curvature, ``k_anchor``/``w_anchor`` the outer end of the anchor region and
        ``q_out`` the slope there measured **outward** (``+dw/dk`` on the right,
        ``-dw/dk`` on the left).  With no usable 10d quote the anchor region is
        empty: ``c = 0`` and the tail starts straight at the 25d join.

        A quadratic pinned by value+slope at one end and value at the other can dip
        between them when the arriving slope points inward hard enough.  We refuse
        to let it fall below 5% of the smaller endpoint (which would make the smile
        non-sensical and the density lumpy); if it would, the curvature is raised to
        exactly touch that floor and the 10d quote is then *not* repriced exactly.
        That trade is deliberate and rare -- it only fires on a 10d quote that is
        inconsistent with the 25d smile it is attached to.
        """
        if not (np.isfinite(ka) and np.isfinite(wa)) or abs(ka - kj) < 1e-12:
            return 0.0, kj, wj, float(side) * pj
        dk = ka - kj
        c = (wa - wj - pj * dk) / (dk * dk)
        floor = 0.05 * min(wj, wa)
        # vertex of w(k) = wj + pj (k-kj) + c (k-kj)^2 lies inside the interval only
        # when c > 0 and -pj/(2c) is between 0 and dk (in the interval's direction)
        if c > 0.0:
            k_star = -pj / (2.0 * c)
            if 0.0 < k_star / dk < 1.0:
                w_min = wj - pj * pj / (4.0 * c)
                if w_min < floor and wj > floor:
                    c = pj * pj / (4.0 * (wj - floor))
        q_out = float(side) * (pj + 2.0 * c * dk)
        w_out = wj + pj * dk + c * dk * dk
        return float(c), float(ka), float(max(w_out, 1e-14)), float(q_out)

    # -- evaluation ------------------------------------------------------ #
    @property
    def forward(self) -> float:
        """Outright forward for this tenor."""
        return float(self.S * math.exp((self.rd - self.rf) * self.T))

    def _core_vol(self, K: np.ndarray) -> np.ndarray:
        """Vanna-volga vol on the core region (no wing treatment)."""
        K1, K2, K3 = self.strikes[0], self.strikes[len(self.strikes) // 2], self.strikes[-1]
        if self.strikes.size == 5:
            K1, K2, K3 = self.strikes[1], self.strikes[2], self.strikes[3]
        s1, s2, s3 = (self.vols[1], self.vols[2], self.vols[3]) if self.strikes.size == 5 \
            else (self.vols[0], self.vols[1], self.vols[2])
        if self.method == "exact":
            return self._exact_vol(np.asarray(K, float))
        return np.asarray(vv_vol(np.asarray(K, float), self.S, self.T, self.rd, self.rf,
                                 K1, K2, K3, s1, s2, s3), float)

    def vol(self, K: Any) -> Any:
        """Implied vol (decimal) at strike(s) ``K``.

        Vectorised, with a pure-python scalar fast path (~2 us) because the spot
        ladder hits this ~1e5 times per refresh.
        """
        if np.ndim(K) == 0 and self.method == "approx":
            return self._vol_scalar(float(K))
        Karr = np.asarray(K, float)
        k = np.log(np.maximum(Karr, 1e-300) / self.forward)
        (k1, k3, kL, kR, w1, w3, p1, p3, cL, cR,
         wLj, qL, betaL, lamL, wRj, qR, betaR, lamR) = self._wing
        out = np.empty(np.shape(k), float)
        mid = (k >= k1) & (k <= k3)
        if np.any(mid):
            out[mid] = self._core_vol(self.forward * np.exp(k[mid]))
        left = k < k1
        if np.any(left):
            kk = k[left]
            w = np.where(kk >= kL,
                         w1 + p1 * (kk - k1) + cL * (kk - k1) ** 2,
                         smile.eval_wing(kL - kk, (wLj, qL, betaL, lamL)))
            out[left] = np.sqrt(np.maximum(w, 1e-14) / max(self.T, gk.T_MIN))
        right = k > k3
        if np.any(right):
            kk = k[right]
            w = np.where(kk <= kR,
                         w3 + p3 * (kk - k3) + cR * (kk - k3) ** 2,
                         smile.eval_wing(kk - kR, (wRj, qR, betaR, lamR)))
            out[right] = np.sqrt(np.maximum(w, 1e-14) / max(self.T, gk.T_MIN))
        out = np.clip(out, _VOL_FLOOR, _VOL_CAP)
        return out if np.ndim(K) else float(out)

    def _vol_scalar(self, K: float) -> float:
        """Scalar three-pillar VV vol plus wings; no numpy, no allocation.

        Numerically identical to the vectorised path (asserted in validation).
        """
        (l1, l2, l3, dn1, dn2, dn3, s1, s2, s3, A1, A3, c0, sqT) = self._fast
        (k1, k3, kL, kR, w1, w3, p1, p3, cL, cR,
         wLj, qL, betaL, lamL, wRj, qR, betaR, lamR) = self._wing
        lnF = math.log(self.forward)
        k = math.log(K) - lnF
        if k < k1:
            w = (w1 + p1 * (k - k1) + cL * (k - k1) ** 2) if k >= kL else \
                smile._eval_wing_scalar(kL - k, (wLj, qL, betaL, lamL))
            return min(max(math.sqrt(max(w, 1e-14) / max(self.T, gk.T_MIN)), _VOL_FLOOR), _VOL_CAP)
        if k > k3:
            w = (w3 + p3 * (k - k3) + cR * (k - k3) ** 2) if k <= kR else \
                smile._eval_wing_scalar(k - kR, (wRj, qR, betaR, lamR))
            return min(max(math.sqrt(max(w, 1e-14) / max(self.T, gk.T_MIN)), _VOL_FLOOR), _VOL_CAP)
        lk = math.log(K)
        y1 = (l2 - lk) * (l3 - lk) / dn1
        y2 = (lk - l1) * (l3 - lk) / dn2
        y3 = (lk - l1) * (lk - l2) / dn3
        D1 = y1 * s1 + y2 * s2 + y3 * s3 - s2
        D2 = y1 * A1 + y3 * A3
        d1 = (c0 - lk) / sqT + 0.5 * sqT
        d2 = d1 - sqT
        pp = d1 * d2
        num = 2.0 * s2 * D1 + D2
        rad = s2 * s2 + pp * num
        # same conjugate rearrangement as vv_vol -- see the comment there
        sig = s2 + num / (s2 + math.sqrt(rad)) if rad > 0.0 else s2 + D1
        return min(max(sig, _VOL_FLOOR), _VOL_CAP)

    def _exact_vol(self, K: np.ndarray) -> np.ndarray:
        """Implied vol from the exact VV replication price (slow path)."""
        idx = (1, 2, 3) if self.strikes.size == 5 else (0, 1, 2)
        K1, K2, K3 = (float(self.strikes[i]) for i in idx)
        s1, s2, s3 = (float(self.vols[i]) for i in idx)
        Kf = np.atleast_1d(np.asarray(K, float)).ravel()
        cp = np.where(Kf >= self.forward, 1.0, -1.0)
        px = np.asarray(vv_price(Kf, cp, self.S, self.T, self.rd, self.rf,
                                 K1, K2, K3, s1, s2, s3), float)
        out = np.empty_like(Kf)
        for i, (kk, pp, c) in enumerate(zip(Kf, px, cp)):
            iv = gk.implied_vol(float(pp), self.S, float(kk), self.T, self.rd, self.rf, int(c))
            out[i] = iv if np.isfinite(iv) else float(vv_vol(kk, self.S, self.T, self.rd,
                                                             self.rf, K1, K2, K3, s1, s2, s3))
        return out.reshape(np.shape(K))

    def vol_by_delta(self, delta: float, cp: int) -> float:
        """Implied vol at a given delta, solving the fixed point
        ``sigma = smile.vol(strike_from_delta(delta, sigma))``.

        The fixed point is required because in FX the strike itself depends on the
        vol.  Converges in 3-6 iterations for ``|delta|`` in [0.02, 0.5]; damped
        after 20 iterations and never raises.
        """
        s = float(self.atm_vol)
        for i in range(60):
            K = gk.strike_from_delta(delta, self.S, self.T, self.rd, self.rf, s, cp,
                                     self.delta_convention)
            if not np.isfinite(K):
                return float("nan")
            s_new = float(self.vol(float(K)))
            if abs(s_new - s) < 1e-13:
                return s_new
            s = 0.5 * (s + s_new) if i > 20 else s_new
        return s

    def strike_by_delta(self, delta: float, cp: int) -> float:
        """Smile-consistent strike at a given delta (uses :meth:`vol_by_delta`)."""
        s = self.vol_by_delta(delta, cp)
        return gk.strike_from_delta(delta, self.S, self.T, self.rd, self.rf, s, cp,
                                    self.delta_convention)

    def rr(self, d: float = 0.25) -> float:
        """Risk reversal ``sigma(d-call) - sigma(d-put)`` implied by this smile."""
        return self.vol_by_delta(d, +1) - self.vol_by_delta(d, -1)

    def bf(self, d: float = 0.25) -> float:
        """Smile butterfly ``(sigma(d-call) + sigma(d-put))/2 - sigma_ATM``."""
        return 0.5 * (self.vol_by_delta(d, +1) + self.vol_by_delta(d, -1)) - self.atm_vol
