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
   ``[K1, K3]``, so beyond roughly 5-delta the vol explodes and the implied
   density goes negative.  We clamp: outside ``[K1, K3]`` the smile is continued
   with a damped linear-in-log-strike wing (see ``wing_damping``) and the vol is
   floored/capped.  Always run :func:`~fxgamma.models.smile.risk_neutral_density`
   before trusting a wing price.
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
    with np.errstate(invalid="ignore", divide="ignore"):
        rad = s2 * s2 + p * (2.0 * s2 * D1 + D2)
        second = s2 + (-s2 + np.sqrt(np.maximum(rad, 0.0))) / p
    out = np.where((np.abs(p) > 1e-12) & (rad > 0.0), second, first)
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

    Built by :meth:`from_quotes`.  With only 25d quotes it is a pure three-point
    vanna-volga smile; when 10d quotes are supplied it becomes a five-pillar
    monotone-cubic (PCHIP) smile in log-moneyness that reprices all five quotes
    exactly -- the standard desk treatment of a "5-point smile", since VV itself
    is defined on exactly three instruments.

    Attributes
    ----------
    S, T, rd, rf : market state for this tenor.
    strikes, vols : the pillar strikes and their market vols (3 or 5, increasing).
    atm_vol : the ATM (DNS) vol -- the ``s2`` of the VV algebra.
    method : ``"approx"`` (second-order formula) or ``"exact"`` (price replication).
    wing_lo, wing_hi : log-moneyness bounds outside which the wing is damped.
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
    wing_damping: float = 0.5
    _slopes: np.ndarray | None = field(default=None, repr=False)
    _wing_slopes: tuple[float, float] = field(default=(0.0, 0.0), repr=False)
    _fast: tuple[float, ...] = field(default=(), repr=False)
    _lkp: np.ndarray | None = field(default=None, repr=False)

    # -- construction ---------------------------------------------------- #
    @classmethod
    def from_quotes(cls, S: float, T: float, rd: float, rf: float,
                    atm: float, rr25: float, bf25: float,
                    rr10: float | None = None, bf10: float | None = None,
                    *, delta: float = 0.25, delta_convention: str = "spot",
                    atm_convention: str | None = None, method: str = "approx",
                    bf_convention: str = "smile", wing_damping: float = 0.5
                    ) -> "VannaVolgaSmile":
        """Build a smile from broker quotes for one tenor.

        Parameters
        ----------
        atm, rr25, bf25 : decimals (0.085, -0.0035, 0.0022).
        rr10, bf10 : optional 10-delta quotes; when both are given the smile
            switches to the five-pillar PCHIP form.
        delta_convention : the pair's convention, ``{"spot","spot_pa","fwd","fwd_pa"}``.
        atm_convention : defaults to the DNS variant matching ``delta_convention``.
        bf_convention : ``"smile"`` (default, algebraic) or ``"market"``, in which
            case ``bf25``/``bf10`` are first converted via :func:`market_to_smile_bf`.
        method : ``"approx"`` | ``"exact"`` -- see the module docstring.
        wing_damping : 0..1.  Fraction of the pillar-edge slope retained outside
            the outermost pillars.  ``0`` = flat wings, ``1`` = full linear
            continuation.  Default 0.5 tames the VV wing blow-up.
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
        strikes = [k_p, k_atm, k_c]
        vols = [p25, atm, c25]

        if rr10 is not None and bf10 is not None:
            p10, c10 = smile.rr_bf_to_vols(atm, rr10, bf10)
            k_p10 = gk.strike_from_delta(0.10, S, T, rd, rf, p10, -1, delta_convention)
            k_c10 = gk.strike_from_delta(0.10, S, T, rd, rf, c10, +1, delta_convention)
            if np.isfinite(k_p10) and np.isfinite(k_c10) and k_p10 < k_p and k_c10 > k_c:
                strikes = [k_p10] + strikes + [k_c10]
                vols = [p10] + vols + [c10]

        K = np.asarray(strikes, float)
        V = np.asarray(vols, float)
        if not np.all(np.diff(K) > 0.0) or not np.all(np.isfinite(K)):
            raise ValueError(f"non-monotone or non-finite pillar strikes: {K}")

        slopes = None
        wing = (0.0, 0.0)
        fwd = S * math.exp((rd - rf) * T)
        lkp = np.log(K / fwd)
        if K.size == 5:
            slopes = smile.pchip_slopes(lkp, V)
        else:
            # one-sided d(sigma)/d(ln K) just inside each edge pillar, precomputed
            # once so vol() stays allocation-light in the ladder's inner loop.
            eps = 1e-4
            k1, k3 = float(K[0]), float(K[-1])
            m_lo = (float(vv_vol(k1 * math.exp(eps), S, T, rd, rf, *K, *V)) - float(V[0])) / eps
            m_hi = (float(V[-1]) - float(vv_vol(k3 * math.exp(-eps), S, T, rd, rf, *K, *V))) / eps
            wing = (m_lo, m_hi)
        # constants for the scalar fast path (see _vol_scalar)
        fast: tuple[float, ...] = ()
        if K.size == 3:
            l1, l2, l3 = (math.log(float(k)) for k in K)
            s1, s2, s3 = (float(v) for v in V)
            a1, b1, _ = gk.d1_d2(S, float(K[0]), T, rd, rf, s2)
            a3, b3, _ = gk.d1_d2(S, float(K[2]), T, rd, rf, s2)
            fast = (l1, l2, l3,
                    (l2 - l1) * (l3 - l1), (l2 - l1) * (l3 - l2), (l3 - l1) * (l3 - l2),
                    s1, s2, s3,
                    float(a1 * b1) * (s1 - s2) ** 2, float(a3 * b3) * (s3 - s2) ** 2,
                    math.log(S) + (rd - rf) * T, s2 * math.sqrt(max(T, gk.T_MIN)))
        return cls(float(S), float(T), float(rd), float(rf), K, V, float(atm),
                   float(k_atm), method, float(delta), str(delta_convention),
                   float(wing_damping), slopes, wing, fast, lkp)

    # -- evaluation ------------------------------------------------------ #
    @property
    def forward(self) -> float:
        """Outright forward for this tenor."""
        return float(self.S * math.exp((self.rd - self.rf) * self.T))

    def vol(self, K: Any) -> Any:
        """Implied vol at strike(s) ``K`` (decimal).  Vectorised over arrays and with a
        pure-python scalar fast path (~2 us/call) because the spot ladder hits this
        ~1e5 times per refresh.  Wings outside the outer pillars are damped-linear in
        log-moneyness (see ``wing_damping``)."""
        if np.ndim(K) == 0 and self.method == "approx" and self._fast:
            return self._vol_scalar(float(K))
        Karr = np.asarray(K, float)
        if self.strikes.size == 5:
            lk = np.log(Karr / self.forward)
            out = smile.pchip_eval(lk, self._lkp, self.vols, self._slopes, extrap="linear")
            out = self._damp(out, lk, self._lkp)
        elif self.method == "approx":
            K1, K2, K3 = self.strikes
            s1, s2, s3 = self.vols
            core = vv_vol(Karr, self.S, self.T, self.rd, self.rf, K1, K2, K3, s1, s2, s3)
            out = self._damp_vv(np.asarray(core, float), Karr)
        else:
            out = self._exact_vol(Karr)
        out = np.clip(out, _VOL_FLOOR, _VOL_CAP)
        return out if np.ndim(K) else float(out)

    def _vol_scalar(self, K: float) -> float:
        """Scalar three-pillar second-order VV vol, no numpy, no allocation.

        Numerically identical to the vectorised path (asserted in the validation
        script); it exists purely so ``vol()`` costs ~2 us instead of ~80 us.
        """
        (l1, l2, l3, dn1, dn2, dn3, s1, s2, s3, A1, A3, c0, sqT) = self._fast
        lk = math.log(K)
        if self.strikes.size == 5:                       # not fast-pathed
            return float(self.vol(np.array([K]))[0])
        y1 = (l2 - lk) * (l3 - lk) / dn1
        y2 = (lk - l1) * (l3 - lk) / dn2
        y3 = (lk - l1) * (lk - l2) / dn3
        D1 = y1 * s1 + y2 * s2 + y3 * s3 - s2
        D2 = y1 * A1 + y3 * A3
        d1 = (c0 - lk) / sqT + 0.5 * sqT
        d2 = d1 - sqT
        p = d1 * d2
        rad = s2 * s2 + p * (2.0 * s2 * D1 + D2)
        sig = s2 + (-s2 + math.sqrt(rad)) / p if (abs(p) > 1e-12 and rad > 0.0) else s2 + D1
        if K < self.strikes[0]:
            sig = self.vols[0] + self.wing_damping * self._wing_slopes[0] * (lk - l1)
        elif K > self.strikes[2]:
            sig = self.vols[2] + self.wing_damping * self._wing_slopes[1] * (lk - l3)
        return min(max(sig, _VOL_FLOOR), _VOL_CAP)

    def _damp(self, v: np.ndarray, lk: np.ndarray, lkp: np.ndarray) -> np.ndarray:
        """Blend the extrapolated wing back towards the edge vol by ``wing_damping``."""
        d = self.wing_damping
        lo = lk < lkp[0]
        hi = lk > lkp[-1]
        v = np.where(lo, self.vols[0] + d * (v - self.vols[0]), v)
        v = np.where(hi, self.vols[-1] + d * (v - self.vols[-1]), v)
        return v

    def _damp_vv(self, v: np.ndarray, K: np.ndarray) -> np.ndarray:
        """VV wings: replace the quadratic continuation with a damped straight line.

        Outside ``[K1, K3]`` the second-order VV formula is extrapolating a parabola
        in log-strike, which blows up.  We continue with the *pillar-edge slope*
        scaled by ``wing_damping``, which keeps the wing monotone and the density
        positive far further out.
        """
        K1, K3 = float(self.strikes[0]), float(self.strikes[-1])
        s1, s3 = float(self.vols[0]), float(self.vols[-1])
        d = self.wing_damping
        m_lo, m_hi = self._wing_slopes
        lo = K < K1
        hi = K > K3
        with np.errstate(divide="ignore", invalid="ignore"):
            v = np.where(lo, s1 + d * m_lo * np.log(np.maximum(K, 1e-300) / K1), v)
            v = np.where(hi, s3 + d * m_hi * np.log(np.maximum(K, 1e-300) / K3), v)
        return v

    def _exact_vol(self, K: np.ndarray) -> np.ndarray:
        """Implied vol from the exact VV replication price (slow path)."""
        K1, K2, K3 = self.strikes
        s1, s2, s3 = self.vols
        Kf = np.atleast_1d(K).ravel()
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
        vol.  Converges in 3-6 iterations for |delta| in [0.02, 0.5]; falls back to
        the last iterate (and never raises) if it stalls.
        """
        s = float(self.atm_vol)
        for _ in range(60):
            K = gk.strike_from_delta(delta, self.S, self.T, self.rd, self.rf, s, cp,
                                     self.delta_convention)
            if not np.isfinite(K):
                return float("nan")
            s_new = float(self.vol(K))
            if abs(s_new - s) < 1e-12:
                return s_new
            s = 0.5 * (s + s_new) if _ > 20 else s_new     # damp if slow
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
