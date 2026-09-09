"""Trend-conditional and asymmetric hedging rules -- and the test that decides whether
any of them beats a symmetric band.

``docs/12_trend_conditional_hedging.md`` is the write-up; this module is the machinery
and every number in that document comes from a function named here.

The user's question, and the maths that answers it
--------------------------------------------------
    "Depends if you predict the trend will continue, ie if it keeps going one
    direction you want to optimise the level dynamically so that we don't hedge
    too early."

It is a good question and it is **not** a request for a directional forecast.  The
gamma P&L of a delta-hedged book over a window is, to second order,

    P&L = 0.5 * G * sum_over_legs (S_hedge_i - S_hedge_{i-1})^2  -  theta * tau

where a *leg* is the spot move between two consecutive hedges and ``G = d(delta)/dS``.
The kernel is a **sum of squares of the moves between hedges**, so for two consecutive
same-sign legs ``(a+b)^2 > a^2 + b^2`` and for opposite-sign legs ``(a+b)^2 < a^2+b^2``.
A trending path therefore pays you for hedging **late** and a choppy path pays you for
hedging **often**, at identical total variance.  :func:`pm_table` reproduces that.

The decomposition that kills the free-lunch story
-------------------------------------------------
Write the leg sum out in terms of the underlying per-step returns ``r_i``.  Within one
leg, ``(sum r_i)^2 = sum r_i^2 + 2 * sum_{i<j} r_i r_j``, so summing over legs

    capture  =  QV  +  2 * sum_{legs} sum_{i<j in the same leg} r_i r_j       (exact)

:func:`capture_decomposition` verifies this to machine precision on any path.  ``QV``
is the quadratic variation -- fixed by the volatility, and *nothing a hedging rule does
can change it*.  So **every penny a trend-conditional rule can earn is the sum of the
return autocovariances inside its own hedge legs**.  In expectation

    E[capture] - QV  =  2 * sum_{legs} sum_{i<j} gamma(j - i)

with ``gamma(k) = Cov(r_t, r_{t+k})``.  Three consequences, and they frame everything:

1. If returns are serially uncorrelated the expected gain of *any* such rule is
   **exactly zero** -- the band-independence result.  What is left is the cost you
   spend and the delta you carry.  A ratchet that "lets winners run" on a random walk
   is a momentum position in disguise with an expected P&L of zero and a fatter tail.
2. The quantity to forecast is therefore the **return autocovariance at intraday lags**,
   equivalently the variance ratio ``VR(n) = Var(sum of n returns) / (n Var(r))``,
   equivalently the path-roughness ``kappa = QV / D^2 = 1 / VR`` that
   :mod:`fxgamma.signals.rangeforecast` already computes.  It is the *same statistic*
   ``docs/10`` ruled unforecastable from daily bars, which is why this work needed
   intraday data (:mod:`fxgamma.data.intraday_yahoo`).
3. The ceiling is knowable in advance: at an infinitely wide band the capture ratio is
   ``VR`` over the window, so a night with ``VR = 1.10`` has at most **10% more gamma
   P&L** available to a perfect late-hedger than to a continuous one -- and only if the
   delta cap lets you hold the position.  :func:`capture_ceiling` prints it in money.

What is in here
---------------
``SymmetricBand``    the baseline: hedge whenever spot is ``h`` from the last hedge.
                     This is ``bandopt.optimal_band`` expressed as a trigger rule and
                     it is what every other rule is scored against.
``AsymmetricBand``   separate up/down half-widths.  Two legitimate sources, and the
                     rule records which: **book skew** (repricing, not a view -- trader
                     s9.3) and a **persistence tilt** (a view, and labelled as one).
``Ratchet``          trailing hedge trigger with an explicit ``give_back``: once spot
                     has run, keep the delta and hedge only after it retraces
                     ``give_back * h`` from the extreme.
``TimeAndState``     widens the band after a same-sign leg, tightens after a reversal,
                     and hedges unconditionally after ``max_steps`` bars.

Every rule takes the **delta cap** as a hard override (``cap`` in :func:`run_rule`):
the most delta the user is willing to be carrying.  Per the trader's s4.3 that cap is
worth thousands a night while the band choice is worth tens, so it binds first by
design and the scorers report how often it does.

Orderability -- the practical objection, stated once
----------------------------------------------------
This user is **asleep** during the window.  A symmetric or asymmetric band is two
resting limit orders and needs nothing from the platform.  A ratchet is a *trailing*
order, and a time-and-state rule is a conditional order chain; neither can be left as
static limits.  Every rule carries ``.orderability`` saying what the platform must
support, because a rule that cannot be typed in at 17:00 is not a rule.

Units, stated once
------------------
``h``, ``give_back * h``, ``cap_spot``   spot units (quote ccy per base ccy).
``G`` (gamma)                            ``d(delta_base)/dS``: base^2 / quote.
``capture``                              spot^2; ``0.5 * G * capture`` is quote ccy.
``cost_bp``                              round-trip spot cost in bp of traded value;
                                         one-way ``lam = cost_bp / 2 / 1e4``, the same
                                         convention as the backtest engine and
                                         :mod:`fxgamma.portfolio.bandopt`.  The default
                                         is the **retail** table -- this user has no OTC
                                         access and the interbank table is 15-40x wrong
                                         for him.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from ..conventions import pair_spec
from .bandopt import cost_bp_for

__all__ = [
    # rules
    "TriggerRule", "SymmetricBand", "AsymmetricBand", "Ratchet", "TimeAndState",
    "RULES", "make_rule",
    # persistence
    "Persistence", "estimate_persistence", "ar1_vr", "vr_to_phi", "phi_to_vr",
    "kappa_to_phi", "band_multiplier", "trend_conditional_band",
    "asymmetric_half_widths",
    # kernels and paths
    "ar1_paths", "ar1_price_paths", "pm_table", "capture_decomposition",
    "capture_ceiling", "expected_capture_ar1",
    # running and scoring
    "RunRecord", "run_rule", "score_paths", "compare_rules", "regime_matrix",
    "giveback_sensitivity", "delta_profile", "match_symmetric",
    "matched_comparison",
    # backtester bridge
    "backtest_with_rule", "verify_against_engine",
]


# --------------------------------------------------------------------------- #
# 1.  Persistence: the only thing a trend-conditional rule can be paid for
# --------------------------------------------------------------------------- #
def phi_to_vr(phi: float, n: int) -> float:
    """Variance ratio at horizon ``n`` of an AR(1)-in-returns process.

    ``VR(n) = 1 + 2 * sum_{k=1..n-1} (1 - k/n) phi^k``.  ``VR > 1`` is trending
    (momentum), ``VR < 1`` is choppy (mean reverting), ``VR = 1`` is a random walk.
    """
    n = int(n)
    if n <= 1:
        return 1.0
    p = float(phi)
    return float(1.0 + 2.0 * sum((1.0 - k / n) * p ** k for k in range(1, n)))


def ar1_vr(phi: float, n: int) -> float:
    """Alias of :func:`phi_to_vr` kept for readability at call sites."""
    return phi_to_vr(phi, n)


def vr_to_phi(vr: float, n: int) -> float:
    """Invert :func:`phi_to_vr` by bisection.  Returns ``nan`` outside ``|phi| < 1``."""
    v = float(vr)
    if not np.isfinite(v) or v <= 0.0 or int(n) <= 1:
        return float("nan")
    lo, hi = -0.98, 0.98
    if v <= phi_to_vr(lo, n):
        return lo
    if v >= phi_to_vr(hi, n):
        return hi
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if phi_to_vr(mid, n) < v:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def kappa_to_phi(kappa: float, n: int) -> float:
    """``kappa`` (rangeforecast's roughness, ``QV/D^2``) is ``1/VR``.  Invert to phi."""
    k = float(kappa)
    if not np.isfinite(k) or k <= 0:
        return float("nan")
    return vr_to_phi(1.0 / k, n)


@dataclass(frozen=True)
class Persistence:
    """A persistence measurement **with its own noise attached**.

    ``vr``/``kappa`` are the same number two ways (``kappa = 1/vr``).  ``phi1`` is the
    lag-1 autocorrelation of the returns; ``phi_implied`` is the AR(1) coefficient that
    would produce the observed ``vr`` at horizon ``block``.

    ``se_vr`` is the standard error of the variance ratio **under the random-walk
    null** (Lo-MacKinlay homoskedastic form), and ``z`` is the resulting test statistic.
    Read ``z`` before reading ``vr``: with 14 hourly bars in an overnight window a
    single session's ``vr`` has a standard error of order 0.4, so a night that "looks
    trending" is almost always noise.  :func:`estimate_persistence` refuses to call a
    regime without it.
    """
    vr: float
    kappa: float
    phi1: float
    phi_implied: float
    er: float
    n_obs: int
    block: int
    se_vr: float
    z: float
    se_phi1: float
    verdict: str = ""
    note: str = ""

    @property
    def is_trending(self) -> bool:
        """``True`` only when the variance ratio is above 1 by more than 2 se."""
        return bool(np.isfinite(self.z) and self.z > 2.0)

    @property
    def is_choppy(self) -> bool:
        return bool(np.isfinite(self.z) and self.z < -2.0)


def _lo_mackinlay_se(n_obs: int, q: int) -> float:
    """Asymptotic se of ``VR(q)`` under the iid null: ``sqrt(2(2q-1)(q-1) / (3 q n))``."""
    n, q = int(n_obs), int(q)
    if n < 2 or q < 2:
        return float("nan")
    return float(math.sqrt(2.0 * (2.0 * q - 1.0) * (q - 1.0) / (3.0 * q * n)))


def estimate_persistence(prices: Sequence[float] | pd.Series, *, block: int = 14,
                         returns: Sequence[float] | None = None) -> Persistence:
    """Measure path persistence from a price series (or ready-made returns).

    ``block`` is the horizon that matters -- for the overnight question it is the
    number of bars in the window (14 hourly bars for London close -> open).

    The variance ratio is computed with **overlapping** blocks and the Lo-MacKinlay
    unbiasing constants, because non-overlapping blocks throw away a factor of ``q`` of
    the data and this estimator is noise-limited.
    """
    if returns is not None:
        r = np.asarray(returns, float)
    else:
        p = np.asarray(pd.Series(prices).astype(float).dropna(), float)
        r = np.diff(np.log(p)) if p.size > 1 else np.array([])
    r = r[np.isfinite(r)]
    n, q = r.size, int(block)
    if n < q + 2:
        return Persistence(float("nan"), float("nan"), float("nan"), float("nan"),
                           float("nan"), int(n), q, float("nan"), float("nan"),
                           float("nan"), "insufficient data",
                           f"{n} returns for a {q}-bar block; need at least {q + 2}")
    mu = float(r.mean())
    d = r - mu
    var1 = float(np.sum(d * d) / (n - 1))
    # overlapping q-sums, Lo-MacKinlay (1988) unbiased denominators
    csum = np.concatenate([[0.0], np.cumsum(r)])
    qsum = csum[q:] - csum[:-q]                      # n - q + 1 overlapping sums
    m = q * (n - q + 1) * (1.0 - q / n)
    # `m` is the Lo-MacKinlay unbiasing constant; with it, `varq` estimates the
    # variance of a q-sum DIVIDED BY q, so VR is simply varq / var1.
    varq = float(np.sum((qsum - q * mu) ** 2) / m) if m > 0 else float("nan")
    vr = float(varq / var1) if var1 > 0 else float("nan")
    se = _lo_mackinlay_se(n, q)
    z = (vr - 1.0) / se if np.isfinite(se) and se > 0 else float("nan")
    phi1 = float(np.corrcoef(r[:-1], r[1:])[0, 1]) if n > 3 else float("nan")
    tv = float(np.sum(np.abs(r)))
    er = float(abs(np.sum(r)) / tv) if tv > 0 else float("nan")
    if not np.isfinite(z):
        verdict = "undetermined"
    elif z > 2.0:
        verdict = "trending (VR > 1 by more than 2 se)"
    elif z < -2.0:
        verdict = "choppy (VR < 1 by more than 2 se)"
    else:
        verdict = "indistinguishable from a random walk"
    return Persistence(
        vr=float(vr), kappa=float(1.0 / vr) if vr > 0 else float("nan"),
        phi1=phi1, phi_implied=vr_to_phi(vr, q), er=er, n_obs=int(n), block=q,
        se_vr=float(se), z=float(z), se_phi1=float(1.0 / math.sqrt(n)),
        verdict=verdict,
        note=(f"VR({q}) = {vr:.3f} +/- {se:.3f} (iid null) from {n} returns; "
              f"kappa = {1.0 / vr if vr > 0 else float('nan'):.3f}; "
              f"lag-1 rho = {phi1:+.3f} +/- {1.0 / math.sqrt(n):.3f}"))


def band_multiplier(p: Persistence | float, *, gain: float = 0.5,
                    lo: float = 0.6, hi: float = 2.0,
                    require_significance: bool = True) -> tuple[float, str]:
    """Band multiplier implied by a persistence measurement, and the words for it.

    **This is a bounded heuristic, not an optimum, and the default is 1.0.**  There is
    no interior optimum to find: under positive autocorrelation the capture rises
    monotonically in the band, so the width is set by the delta cap and the cost, not
    by the persistence.  All the persistence estimate can honestly do is say whether to
    sit nearer the cap (trending) or nearer the cost-optimal band (choppy), so the
    multiplier is ``VR^gain`` clipped into ``[lo, hi]``.

    With ``require_significance`` (the default) the multiplier is **1.0 unless the
    variance ratio is more than 2 se from 1**.  Turning that off is how you reproduce
    the failure mode in ``docs/12``: acting on an insignificant estimate is what turns
    a hedging rule into a noisy momentum bet.
    """
    if isinstance(p, Persistence):
        vr, sig, z = p.vr, (p.is_trending or p.is_choppy), p.z
        why = p.verdict
    else:
        vr, sig, z, why = float(p), True, float("nan"), "variance ratio supplied directly"
    if not np.isfinite(vr) or vr <= 0:
        return 1.0, "no usable persistence estimate -- symmetric band"
    if require_significance and not sig:
        return 1.0, (f"VR = {vr:.2f} (z = {z:+.1f}) is inside the noise -- "
                     "symmetric band, no tilt")
    m = float(np.clip(vr ** float(gain), lo, hi))
    return m, (f"band x{m:.2f} from VR = {vr:.2f} (z = {z:+.1f}); {why}")


def trend_conditional_band(base_band_spot: float, p: Persistence, *,
                           cap_spot: float | None = None, gain: float = 0.5,
                           lo: float = 0.6, hi: float = 2.0,
                           require_significance: bool = True) -> tuple[float, str]:
    """``optimal_band`` half-width scaled by persistence, then capped by the delta cap.

    Returns ``(half_width_spot, source_words)``.  The words are written to be printed
    on the screen verbatim, because an asymmetric or widened band that does not say
    where it came from reads as a directional call (trader s9.3).
    """
    m, why = band_multiplier(p, gain=gain, lo=lo, hi=hi,
                             require_significance=require_significance)
    h = float(base_band_spot) * m
    if cap_spot is not None and h > float(cap_spot):
        return float(cap_spot), (f"{why}; CAPPED by the delta cap at "
                                 f"{float(cap_spot):.5f} spot")
    return h, why


def asymmetric_half_widths(base_band_spot: float, *, skew_tilt: float = 0.0,
                           persistence: Persistence | None = None,
                           last_move_sign: int = 0, trend_gain: float = 0.5,
                           max_tilt: float = 0.5,
                           require_significance: bool = True
                           ) -> tuple[float, float, str]:
    """Up/down half-widths and the sentence that explains them.

    Two additive sources, kept separate on purpose:

    ``skew_tilt``   from the **book**: a skewed book's delta profile is genuinely
                    asymmetric, so the spot distance that accrues a given delta differs
                    up-side and down-side.  This is repricing, not a view, and it is the
                    only asymmetry the trader's s9.3 endorses unconditionally.  Positive
                    ``skew_tilt`` widens the up-side trigger.
    persistence     from the **path**: after an up-move, a positively autocorrelated
                    market makes the next move more likely to be up too, so the up-side
                    trigger widens (do not sell into the rally) and the down-side
                    tightens.  **This is a view**, it is gated on significance, and the
                    returned words say so.

    Returns ``(h_up, h_dn, source)``.  ``h_up`` is the distance above the last hedge at
    which the up-side order rests.
    """
    h = float(base_band_spot)
    parts: list[str] = []
    tilt = float(skew_tilt)
    if tilt:
        parts.append(f"book skew {tilt:+.0%} (your strikes, not a view)")
    if persistence is not None and last_move_sign:
        sig = persistence.is_trending or persistence.is_choppy
        if sig or not require_significance:
            t = float(np.clip(float(trend_gain) * (persistence.vr - 1.0),
                              -float(max_tilt), float(max_tilt)))
            tilt += t * float(np.sign(last_move_sign))
            parts.append(f"persistence tilt {t * np.sign(last_move_sign):+.0%} "
                         f"(VR = {persistence.vr:.2f}, z = {persistence.z:+.1f}) "
                         "-- THIS IS A VIEW ON THE PATH")
        else:
            parts.append(f"persistence tilt suppressed: VR = {persistence.vr:.2f} "
                         f"(z = {persistence.z:+.1f}) is inside the noise")
    tilt = float(np.clip(tilt, -0.9, 0.9))
    return (h * (1.0 + tilt), h * (1.0 - tilt),
            "; ".join(parts) or "symmetric -- no asymmetry source")


# --------------------------------------------------------------------------- #
# 2.  The rules
# --------------------------------------------------------------------------- #
class TriggerRule:
    """Base class: a stateful hedge trigger expressed in **spot levels**.

    The driver (:func:`run_rule`) walks a price path and asks the rule, at each bar,
    for the two levels at which it would hedge (``lo``, ``hi``).  A rule that only ever
    hedges back to flat needs nothing else; that matches
    :func:`fxgamma.portfolio.hedging.hedge_suggestion`'s default and the backtest
    engine's, so the comparison across rules is like-for-like.

    Subclasses implement :meth:`reset`, :meth:`triggers`, :meth:`observe` and
    :meth:`on_fill`.
    """

    name: str = "rule"
    #: what the platform must support for this rule to be leaveable overnight
    orderability: str = "two resting limit orders"

    def reset(self, s0: float) -> None:                      # pragma: no cover - trivial
        self.ref = float(s0)

    def triggers(self) -> tuple[float, float]:               # pragma: no cover - abstract
        raise NotImplementedError

    def observe(self, high: float, low: float, close: float) -> None:
        """Update trailing state from a bar on which no hedge fired."""

    def on_fill(self, price: float) -> None:
        """Called after a hedge at ``price``; must reset the reference."""
        self.ref = float(price)

    def describe(self) -> str:                               # pragma: no cover - display
        return self.name


@dataclass
class SymmetricBand(TriggerRule):
    """Hedge whenever spot is ``h`` away from the last hedge.  **The baseline.**

    This is ``bandopt.optimal_band`` (or the delta cap, whichever binds) expressed as a
    pair of resting orders.  Every other rule in this module is scored against it.
    """
    h: float
    name: str = "symmetric"
    orderability: str = "two resting limit orders -- leaveable, no platform features"
    ref: float = field(default=0.0, init=False)

    def reset(self, s0: float) -> None:
        self.ref = float(s0)

    def triggers(self) -> tuple[float, float]:
        return self.ref - self.h, self.ref + self.h


@dataclass
class AsymmetricBand(TriggerRule):
    """Separate up/down half-widths.  ``source`` records *why*, for the screen."""
    h_up: float
    h_dn: float
    source: str = ""
    name: str = "asymmetric"
    orderability: str = "two resting limit orders at different distances -- leaveable"
    ref: float = field(default=0.0, init=False)

    def reset(self, s0: float) -> None:
        self.ref = float(s0)

    def triggers(self) -> tuple[float, float]:
        return self.ref - self.h_dn, self.ref + self.h_up

    def describe(self) -> str:                               # pragma: no cover - display
        return f"asymmetric up {self.h_up:.5f} / dn {self.h_dn:.5f} -- {self.source}"


@dataclass
class Ratchet(TriggerRule):
    """Trailing hedge trigger: keep the delta while spot runs, hedge on the give-back.

    Mechanics
    ---------
    * Unarmed, the rule is a symmetric band of half-width ``h``: nothing happens until
      spot has moved ``h`` from the last hedge.
    * Once armed in a direction it stops being a limit order and becomes a **trailing
      stop**: the trigger follows the running extreme at a distance
      ``give_back * h`` and fires on the retracement.
    * The opposite side keeps its ordinary band at ``ref -/+ h``, so a move that
      reverses all the way through the starting point is still hedged.
    * ``give_back = 0`` degenerates to hedging at the extreme (unattainable in practice
      and reported only as a bound); ``give_back`` large means the trail never binds and
      only the delta cap stops you.

    The give-back is the fee.  Every reversal hands back ``give_back * h`` of spot move
    that had already been earned, whether or not the trend was real, and
    :func:`giveback_sensitivity` is the sweep that prices it.

    **It is not a resting order.**  Unless the platform supports server-side trailing
    stops this rule requires someone awake, which is the thing the user is trying to
    avoid.
    """
    h: float
    give_back: float = 0.5
    name: str = "ratchet"
    orderability: str = ("SERVER-SIDE TRAILING STOP (or an OCO chain, or someone awake) "
                         "-- NOT leaveable as static limits")
    ref: float = field(default=0.0, init=False)
    armed: int = field(default=0, init=False)          # 0 / +1 / -1
    extreme: float = field(default=0.0, init=False)

    def reset(self, s0: float) -> None:
        self.ref = float(s0)
        self.armed = 0
        self.extreme = float(s0)

    def triggers(self) -> tuple[float, float]:
        if self.armed > 0:
            trail = self.extreme - self.give_back * self.h
            return max(trail, self.ref - self.h), math.inf
        if self.armed < 0:
            trail = self.extreme + self.give_back * self.h
            return -math.inf, min(trail, self.ref + self.h)
        # Unarmed the rule has NO fill level: reaching +/- h *arms* the trail, it does
        # not hedge.  (Returning the band here instead is the obvious implementation and
        # it is wrong -- it fills on the arming bar and the ratchet degenerates silently
        # into a symmetric band.  It did, and the give-back sweep read flat until this
        # was found.)  The delta cap in `run_rule` is the only hard trigger while
        # unarmed.
        return -math.inf, math.inf

    def observe(self, high: float, low: float, close: float) -> None:
        if self.armed == 0:
            if high - self.ref >= self.h:
                self.armed, self.extreme = +1, float(high)
            elif self.ref - low >= self.h:
                self.armed, self.extreme = -1, float(low)
            return
        if self.armed > 0:
            self.extreme = max(self.extreme, float(high))
        else:
            self.extreme = min(self.extreme, float(low))

    def on_fill(self, price: float) -> None:
        self.ref = float(price)
        self.armed = 0
        self.extreme = float(price)

    def describe(self) -> str:                               # pragma: no cover - display
        return f"ratchet h={self.h:.5f} give_back={self.give_back:.2f}h"


@dataclass
class TimeAndState(TriggerRule):
    """Widen after a same-sign leg, tighten after a reversal, hedge on the clock.

    The state variable is the sign of the last completed leg.  Two consecutive legs in
    the same direction is the only *observable* evidence of persistence a rule this
    simple can act on, so the band is multiplied by ``widen`` after one and by
    ``tighten`` after a reversal, clipped into ``[h_min, h_max]``.

    ``max_steps`` is the time leg: hedge unconditionally after that many bars whatever
    spot has done.  Without it an adaptive band can widen its way into carrying the
    position to the morning, which is the delta cap's job to prevent, not the band's.
    """
    h0: float
    widen: float = 1.3
    tighten: float = 0.8
    h_min: float = 0.0
    h_max: float = math.inf
    max_steps: int | None = None
    name: str = "time_and_state"
    orderability: str = ("conditional / replace-on-fill order chain -- a platform that "
                         "can amend resting orders, or someone awake")
    ref: float = field(default=0.0, init=False)
    h: float = field(default=0.0, init=False)
    last_sign: int = field(default=0, init=False)
    steps: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.h_min <= 0:
            self.h_min = 0.25 * float(self.h0)
        if not np.isfinite(self.h_max):
            self.h_max = 4.0 * float(self.h0)

    def reset(self, s0: float) -> None:
        self.ref = float(s0)
        self.h = float(self.h0)
        self.last_sign = 0
        self.steps = 0

    def triggers(self) -> tuple[float, float]:
        if self.max_steps is not None and self.steps >= int(self.max_steps):
            return self.ref, self.ref                # fires on any price -> time hedge
        return self.ref - self.h, self.ref + self.h

    def observe(self, high: float, low: float, close: float) -> None:
        self.steps += 1

    def on_fill(self, price: float) -> None:
        sign = int(np.sign(price - self.ref)) or self.last_sign
        if self.last_sign and sign == self.last_sign:
            self.h = float(np.clip(self.h * self.widen, self.h_min, self.h_max))
        elif self.last_sign:
            self.h = float(np.clip(self.h * self.tighten, self.h_min, self.h_max))
        self.last_sign = sign
        self.ref = float(price)
        self.steps = 0

    def describe(self) -> str:                               # pragma: no cover - display
        return (f"time-and-state h0={self.h0:.5f} widen={self.widen:.2f} "
                f"tighten={self.tighten:.2f} max_steps={self.max_steps}")


#: name -> constructor.  ``make_rule("ratchet", h=..., give_back=...)``.
RULES: dict[str, Callable[..., TriggerRule]] = {
    "symmetric": SymmetricBand,
    "asymmetric": AsymmetricBand,
    "ratchet": Ratchet,
    "time_and_state": TimeAndState,
}


def make_rule(name: str, **kw: Any) -> TriggerRule:
    try:
        return RULES[str(name).strip().lower()](**kw)
    except KeyError as exc:
        raise KeyError(f"unknown rule {name!r}; have {sorted(RULES)}") from exc


# --------------------------------------------------------------------------- #
# 3.  The driver
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RunRecord:
    """One path under one rule.

    ``legs`` are the signed spot moves between consecutive hedges (the thing that gets
    squared).  ``open_leg`` is the unhedged move still running at the end of the path --
    the delta the user wakes up holding, and the PM's table drops it (see
    :func:`pm_table`).
    """
    legs: np.ndarray
    hedge_idx: np.ndarray
    hedge_price: np.ndarray
    open_leg: float
    x_path: np.ndarray                 # displacement from the live reference, per bar
    cap_hits: int = 0
    ambiguous_bars: int = 0

    @property
    def n_hedges(self) -> int:
        return int(self.legs.size)

    @property
    def capture_closed(self) -> float:
        return float(np.sum(self.legs ** 2))

    @property
    def capture_total(self) -> float:
        return float(np.sum(self.legs ** 2) + self.open_leg ** 2)


def run_rule(prices: Sequence[float], rule: TriggerRule, *,
             high: Sequence[float] | None = None, low: Sequence[float] | None = None,
             fill: str = "close", cap: float | None = None) -> RunRecord:
    """Walk one path under one rule.

    ``fill="close"``   a hedge happens at the **bar close** on the bar whose close is
                       through the trigger.  This is the backtest engine's convention
                       ("you see the price and you deal on it") and it is the
                       conservative one: it charges the rule for the overshoot.
    ``fill="touch"``   a hedge happens **at the trigger level** when the bar's range
                       reaches it, which is what a resting limit order actually does.
                       Needs ``high``/``low``.  When a bar's range spans both triggers
                       the one nearer the previous close is taken and the bar is counted
                       in ``ambiguous_bars`` -- intra-bar order is unknowable and
                       pretending otherwise is how a backtest invents fills.

    ``cap`` is the **delta cap in spot units**: an unconditional hedge as soon as the
    displacement from the last hedge reaches it, whatever the rule wanted.  This is the
    user's "largest delta I am willing to wake up holding" and it overrides everything.
    """
    p = np.asarray(prices, float)
    if p.ndim != 1 or p.size < 2:
        raise ValueError("prices must be a 1-D path with at least two points")
    touch = str(fill).lower() == "touch"
    if touch and (high is None or low is None):
        raise ValueError("fill='touch' needs high and low arrays")
    hi_arr = np.asarray(high, float) if high is not None else p
    lo_arr = np.asarray(low, float) if low is not None else p
    capv = float(cap) if cap is not None else math.inf

    rule.reset(float(p[0]))
    legs: list[float] = []
    hidx: list[int] = []
    hpx: list[float] = []
    xs = np.empty(p.size, float)
    xs[0] = 0.0
    cap_hits = 0
    ambiguous = 0

    for i in range(1, p.size):
        c, h_i, l_i = float(p[i]), float(hi_arr[i]), float(lo_arr[i])
        ref = rule.ref
        lo_t, hi_t = rule.triggers()
        # the delta cap is a hard trigger and it is checked first
        cap_lo, cap_hi = ref - capv, ref + capv
        lo_t, hi_t = max(lo_t, cap_lo), min(hi_t, cap_hi)
        fill_px: float | None = None
        if touch:
            up_hit, dn_hit = h_i >= hi_t, l_i <= lo_t
            if up_hit and dn_hit:
                ambiguous += 1
                prev = float(p[i - 1])
                fill_px = hi_t if abs(hi_t - prev) <= abs(prev - lo_t) else lo_t
            elif up_hit:
                fill_px = hi_t
            elif dn_hit:
                fill_px = lo_t
        else:
            if c >= hi_t or c <= lo_t:
                fill_px = c
        if fill_px is not None:
            if abs(fill_px - ref) >= capv - 1e-15:
                cap_hits += 1
            legs.append(fill_px - ref)
            hidx.append(i)
            hpx.append(fill_px)
            rule.on_fill(fill_px)
        else:
            # state (the trailing extreme, the clock) only advances on a bar that did
            # not fill: after a fill the rule restarts from the fill price and the rest
            # of that bar is deliberately not replayed into it.
            rule.observe(h_i, l_i, c)
        xs[i] = c - rule.ref
    return RunRecord(np.asarray(legs, float), np.asarray(hidx, int),
                     np.asarray(hpx, float), float(p[-1] - rule.ref), xs,
                     cap_hits, ambiguous)


# --------------------------------------------------------------------------- #
# 4.  Kernels: the PM's table, the exact decomposition, the ceiling
# --------------------------------------------------------------------------- #
def ar1_paths(phi: float, n_paths: int, n_steps: int, *, seed: int = 0,
              step_sd: float = 1.0, s0: float = 0.0) -> np.ndarray:
    """AR(1)-in-returns paths with the **per-step variance equalised across phi**.

    ``r_t = phi r_{t-1} + e_t`` with ``Var(r_t) = step_sd^2`` for every ``phi``, so the
    quadratic variation -- the total gamma P&L available to a continuous hedger -- is
    identical in every cell of the comparison and only the *ordering* of the moves
    differs.  That is the controlled experiment: same variance, different path shape.

    Returns levels (arithmetic, ``n_paths x (n_steps+1)``), started from the stationary
    distribution of the return process so there is no burn-in artefact.
    """
    rng = np.random.default_rng(int(seed))
    phi = float(phi)
    sd_e = float(step_sd) * math.sqrt(max(1.0 - phi * phi, 0.0))
    e = rng.standard_normal((int(n_paths), int(n_steps))) * sd_e
    r = np.empty_like(e)
    prev = rng.standard_normal(int(n_paths)) * float(step_sd)      # stationary start
    for t in range(int(n_steps)):
        prev = phi * prev + e[:, t]
        r[:, t] = prev
    return float(s0) + np.concatenate([np.zeros((int(n_paths), 1)), np.cumsum(r, axis=1)],
                                      axis=1)


def ar1_price_paths(phi: float, n_paths: int, n_steps: int, *, seed: int = 0,
                    s0: float = 1.1660, step_sd_pct: float = 0.0007) -> np.ndarray:
    """:func:`ar1_paths` mapped into prices: ``S_t = s0 * exp(sigma_step * x_t)``.

    ``step_sd_pct`` is the per-bar log-return standard deviation; 0.07% is about one
    hourly EURUSD bar at a 7% annualised vol (``0.07 / sqrt(252*24) ~ 0.00090`` on a
    24h basis, less in the thin overnight hours).
    """
    x = ar1_paths(phi, n_paths, n_steps, seed=seed, step_sd=1.0, s0=0.0)
    return float(s0) * np.exp(float(step_sd_pct) * x)


def pm_table(*, n_paths: int = 4000, n_steps: int = 240,
             bands: Sequence[float] = (0.25, 1.0, 2.0, 4.0),
             phis: Sequence[float] = (0.3, 0.0, -0.3), seed: int = 20260909,
             include_open_leg: bool = False) -> pd.DataFrame:
    """Reproduce the PM's captured-sum-of-squared-moves table.

    Rows are bands (in units of one step's standard deviation), columns are ``phi``.
    Each cell is the mean over ``n_paths`` of the sum of squared moves **between
    hedges** under a symmetric band, on paths whose per-step variance is equalised
    across ``phi`` (:func:`ar1_paths`).

    ``include_open_leg=False`` matches the PM's numbers: only *completed* legs count,
    so the leftover unhedged move at the end of the path is dropped.  That convention
    is worth knowing, because it is what makes the random-walk column read 237 rather
    than 240 at a 4-sigma band -- the missing 3 is the open leg, not a band effect.
    Set it ``True`` and the random-walk column is flat at 240 to within Monte-Carlo
    error at every band, which is the exact band-independence result.
    """
    rows = []
    for b in bands:
        row: dict[str, Any] = {"band": float(b)}
        for k, phi in enumerate(phis):
            x = ar1_paths(phi, n_paths, n_steps, seed=int(seed) + 101 * k)
            caps = np.empty(x.shape[0])
            for j in range(x.shape[0]):
                rec = run_rule(x[j], SymmetricBand(float(b)))
                caps[j] = rec.capture_total if include_open_leg else rec.capture_closed
            row[f"phi={phi:+.2f}"] = float(caps.mean())
            row[f"se(phi={phi:+.2f})"] = float(caps.std(ddof=1) / math.sqrt(caps.size))
        rows.append(row)
    out = pd.DataFrame(rows).set_index("band")
    out.attrs["note"] = (f"n_paths={n_paths}, n_steps={n_steps}, per-step variance "
                         f"equalised at 1.0, open leg "
                         f"{'included' if include_open_leg else 'dropped'}")
    return out


def capture_decomposition(prices: Sequence[float], rule: TriggerRule, **kw: Any) -> dict:
    """The exact identity ``capture = QV + 2 * (within-leg cross terms)``.

    Returned keys: ``capture``, ``qv``, ``cross``, ``residual`` (must be ~0 to machine
    precision), ``ratio = capture/qv``.  ``ratio`` is the realised variance ratio of the
    path *as the rule sampled it*, and it is the only thing that separates one rule's
    gamma P&L from another's on the same path.

    Works in the same units as the path: pass a level path for the kernel identity, a
    price path for money.
    """
    p = np.asarray(prices, float)
    rec = run_rule(p, rule, **kw)
    r = np.diff(p)
    qv = float(np.sum(r * r))
    bounds = [0, *rec.hedge_idx.tolist(), p.size - 1]
    cross = 0.0
    for a, b in zip(bounds[:-1], bounds[1:]):
        seg = r[a:b]
        if seg.size > 1:
            s = float(np.sum(seg))
            cross += 0.5 * (s * s - float(np.sum(seg * seg)))
    cap = rec.capture_total
    return {"capture": cap, "qv": qv, "cross": 2.0 * cross,
            "residual": cap - qv - 2.0 * cross,
            "ratio": cap / qv if qv > 0 else float("nan"),
            "n_hedges": rec.n_hedges}


def expected_capture_ar1(phi: float, n_steps: int, band: float, *, n_paths: int = 4000,
                         seed: int = 7, include_open_leg: bool = False) -> tuple[float, float]:
    """Monte-Carlo ``E[capture]`` and its standard error for one (phi, band) cell."""
    x = ar1_paths(phi, n_paths, n_steps, seed=seed)
    recs = [run_rule(x[j], SymmetricBand(float(band))) for j in range(x.shape[0])]
    caps = np.array([(r.capture_total if include_open_leg else r.capture_closed)
                     for r in recs])
    return float(caps.mean()), float(caps.std(ddof=1) / math.sqrt(caps.size))


def capture_ceiling(gamma: float, spot: float, sigma_window: float, vr: float) -> dict:
    """The most a perfect late-hedger can win, in money, on a given night.

    At an infinitely wide band the captured sum of squares tends to the squared *net*
    move, whose expectation is ``VR`` times the quadratic variation.  So the whole prize
    for calling the regime right is ``0.5 * G * (VR - 1) * (S sigma)^2`` -- before the
    cost of the delta you had to carry to collect it, and before being wrong.

    ``gamma`` is ``d(delta_base)/dS``; ``sigma_window`` is the log-return sd over the
    window (not annualised); the result is in quote ccy.
    """
    qv = (float(spot) * float(sigma_window)) ** 2
    base = 0.5 * float(gamma) * qv
    return {"qv_spot2": qv, "continuous_pnl": base,
            "ceiling_pnl": base * float(vr), "prize": base * (float(vr) - 1.0),
            "vr": float(vr),
            "note": ("prize = the entire gamma P&L difference between hedging "
                     "continuously and hedging once at the end, at this VR; a rule "
                     "captures a fraction of it and pays cost and delta risk for the try")}


# --------------------------------------------------------------------------- #
# 5.  Scoring: money, and the delta you are left holding
# --------------------------------------------------------------------------- #
def _lam(pair: str | None, cost_bp: float | None, cost_tier: str = "retail") -> float:
    if cost_bp is None:
        cost_bp = cost_bp_for(pair or "EURUSD", cost_tier)
    return float(cost_bp) / 2.0 / 1e4


def score_paths(paths: np.ndarray, rule_factory: Callable[[], TriggerRule], *,
                gamma: float, cost_bp: float | None = None, pair: str = "EURUSD",
                cost_tier: str = "retail", cap_spot: float | None = None,
                fill: str = "close", highs: np.ndarray | None = None,
                lows: np.ndarray | None = None,
                cap_delta_base: float | None = None) -> dict:
    """Run one rule over many price paths and return the money **and the delta**.

    ``rule_factory`` is a zero-argument callable so every path gets a fresh, unshared
    rule state.  ``gamma`` is ``d(delta_base)/dS``, assumed constant over the window --
    fine for one overnight session on a book that is not pinned, and the reason
    :func:`backtest_with_rule` exists for when it is not.

    Reported per night, in quote ccy: ``capture_pnl = 0.5 G sum(leg^2)``,
    ``cost = lam S sum|G x_hedge|``, ``net``.  Reported in base ccy: the mean and 95th
    percentile of ``|delta|`` carried, the delta left open at the end, and how often the
    cap bound.  Holding delta is exactly what the cap exists to limit, so a rule that
    wins on ``net`` while doubling ``p95_abs_delta`` has not won.
    """
    P = np.asarray(paths, float)
    if P.ndim != 2:
        raise ValueError("paths must be 2-D (n_paths x n_bars)")
    lam = _lam(pair, cost_bp, cost_tier)
    G = float(gamma)
    n = P.shape[0]
    cap = cap_spot
    if cap is None and cap_delta_base is not None and G != 0:
        cap = abs(float(cap_delta_base) / G)
    net = np.empty(n)
    capt = np.empty(n)
    cost = np.empty(n)
    nh = np.empty(n)
    mean_abs = np.empty(n)
    max_abs = np.empty(n)
    end_abs = np.empty(n)
    caphits = np.empty(n)
    for j in range(n):
        rec = run_rule(P[j], rule_factory(),
                       high=None if highs is None else highs[j],
                       low=None if lows is None else lows[j],
                       fill=fill, cap=cap)
        cap_j = 0.5 * G * rec.capture_total
        # hedging back to flat trades G*|x| of base ccy at the fill price
        clip = np.abs(G * rec.legs)
        cst = float(np.sum(clip * rec.hedge_price)) * lam if clip.size else 0.0
        capt[j] = cap_j
        cost[j] = cst
        net[j] = cap_j - cst
        nh[j] = rec.n_hedges
        d = np.abs(G * rec.x_path)
        mean_abs[j] = float(d.mean())
        max_abs[j] = float(d.max())
        end_abs[j] = abs(G * rec.open_leg)
        caphits[j] = rec.cap_hits
    out = {
        "n_paths": int(n),
        "capture_pnl": float(capt.mean()), "cost": float(cost.mean()),
        "net": float(net.mean()), "net_se": float(net.std(ddof=1) / math.sqrt(n)),
        "net_p05": float(np.percentile(net, 5)), "net_p95": float(np.percentile(net, 95)),
        "n_hedges": float(nh.mean()),
        "mean_abs_delta": float(mean_abs.mean()),
        "p95_abs_delta": float(np.percentile(max_abs, 95)),
        "max_abs_delta": float(max_abs.mean()),
        "end_abs_delta": float(end_abs.mean()),
        "cap_hit_rate": float((caphits > 0).mean()),
        "cost_bp": float(cost_bp if cost_bp is not None else cost_bp_for(pair, cost_tier)),
    }
    out["_net_paths"] = net
    return out


def compare_rules(paths: np.ndarray, rules: Mapping[str, Callable[[], TriggerRule]],
                  *, baseline: str = "symmetric", **kw: Any) -> pd.DataFrame:
    """Score several rules on the **same paths** and difference them against a baseline.

    Common random numbers throughout: the difference ``net - net(baseline)`` is computed
    path by path, so its standard error is the se of the *paired* difference and not the
    much larger se of either level.  Without that pairing nothing in this study would be
    significant either way and the comparison would be uninformative rather than
    honest.
    """
    scores = {k: score_paths(paths, f, **kw) for k, f in rules.items()}
    if baseline not in scores:
        raise KeyError(f"baseline {baseline!r} is not among the rules {sorted(scores)}")
    base = scores[baseline]["_net_paths"]
    rows = []
    for k, s in scores.items():
        d = s["_net_paths"] - base
        rows.append({
            "rule": k, "net": s["net"], "net_se": s["net_se"],
            "vs_base": float(d.mean()),
            "vs_base_se": float(d.std(ddof=1) / math.sqrt(d.size)) if d.size > 1 else 0.0,
            "t": (float(d.mean() / (d.std(ddof=1) / math.sqrt(d.size)))
                  if d.size > 1 and d.std(ddof=1) > 0 else 0.0),
            "capture_pnl": s["capture_pnl"], "cost": s["cost"],
            "n_hedges": s["n_hedges"], "mean_abs_delta": s["mean_abs_delta"],
            "p95_abs_delta": s["p95_abs_delta"], "end_abs_delta": s["end_abs_delta"],
            "cap_hit_rate": s["cap_hit_rate"],
            "net_p05": s["net_p05"],
        })
    return pd.DataFrame(rows).set_index("rule")


def match_symmetric(paths: np.ndarray, rule_factory: Callable[[], TriggerRule], *,
                    gamma: float, stat: str = "mean_abs_delta",
                    lo: float = 1e-6, hi: float = 1.0, tol: float = 1e-3,
                    max_iter: int = 40, **kw: Any) -> tuple[float, float, float]:
    """Half-width of the symmetric band that carries the **same risk** as ``rule``.

    This is the control that decides whether any of this is real, and it was added
    after the first run of :func:`compare_rules` "showed" the ratchet beating the
    symmetric band on a *random walk* -- where the expected gain is provably zero.  The
    explanation was mundane: a trailing rule hedges less often, so it is a **wider band
    in disguise**, and at retail costs a wider band saves money for reasons that have
    nothing to do with trend.  Comparing a ratchet at ``h`` against a symmetric band at
    the same ``h`` therefore measures band width, not path persistence.

    So: solve for the symmetric ``h*`` that matches the rule on ``stat``
    (``mean_abs_delta`` by default -- the delta actually carried, which is what the
    user's cap is written in; ``n_hedges`` and ``p95_abs_delta`` are the other sensible
    choices) and score against *that*.  Returns ``(h_star, target, achieved)``.
    """
    target = float(score_paths(paths, rule_factory, gamma=gamma, **kw)[stat])
    a, b = float(lo), float(hi)
    fa = float(score_paths(paths, lambda: SymmetricBand(a), gamma=gamma, **kw)[stat])
    fb = float(score_paths(paths, lambda: SymmetricBand(b), gamma=gamma, **kw)[stat])
    if not (min(fa, fb) <= target <= max(fa, fb)):
        return float("nan"), target, float("nan")
    for _ in range(int(max_iter)):
        m = 0.5 * (a + b)
        fm = float(score_paths(paths, lambda: SymmetricBand(m), gamma=gamma, **kw)[stat])
        if abs(fm - target) <= tol * max(abs(target), 1e-12):
            return m, target, fm
        if (fm < target) == (fa < target):
            a, fa = m, fm
        else:
            b, fb = m, fm
    m = 0.5 * (a + b)
    return m, target, float(score_paths(paths, lambda: SymmetricBand(m), gamma=gamma,
                                        **kw)[stat])


def matched_comparison(paths: np.ndarray, rules: Mapping[str, Callable[[], TriggerRule]],
                       *, gamma: float, stat: str = "mean_abs_delta",
                       **kw: Any) -> pd.DataFrame:
    """Each rule against **its own risk-matched symmetric band**, paired path by path.

    ``vs_matched`` is the only column in this module that is evidence about persistence.
    Everything else in a rule comparison can be reproduced by moving the band.
    """
    rows = []
    for name, f in rules.items():
        h_star, target, got = match_symmetric(paths, f, gamma=gamma, stat=stat, **kw)
        s = score_paths(paths, f, gamma=gamma, **kw)
        if not np.isfinite(h_star):
            rows.append({"rule": name, "h_matched": float("nan"), "net": s["net"],
                         "matched_net": float("nan"), "vs_matched": float("nan"),
                         "se": float("nan"), "t": float("nan"),
                         "match_stat": stat, "match_target": target,
                         "match_achieved": float("nan"),
                         "n_hedges": s["n_hedges"],
                         "mean_abs_delta": s["mean_abs_delta"],
                         "p95_abs_delta": s["p95_abs_delta"]})
            continue
        b = score_paths(paths, lambda h=h_star: SymmetricBand(h), gamma=gamma, **kw)
        d = s["_net_paths"] - b["_net_paths"]
        se = float(d.std(ddof=1) / math.sqrt(d.size)) if d.size > 1 else float("nan")
        rows.append({"rule": name, "h_matched": float(h_star), "net": s["net"],
                     "matched_net": b["net"], "vs_matched": float(d.mean()),
                     "se": se, "t": float(d.mean() / se) if se else float("nan"),
                     "match_stat": stat, "match_target": target, "match_achieved": got,
                     "n_hedges": s["n_hedges"], "matched_n_hedges": b["n_hedges"],
                     "mean_abs_delta": s["mean_abs_delta"],
                     "p95_abs_delta": s["p95_abs_delta"],
                     "matched_p95_abs_delta": b["p95_abs_delta"],
                     "end_abs_delta": s["end_abs_delta"],
                     "matched_end_abs_delta": b["end_abs_delta"],
                     "cost": s["cost"], "matched_cost": b["cost"],
                     "capture_pnl": s["capture_pnl"],
                     "matched_capture_pnl": b["capture_pnl"]})
    return pd.DataFrame(rows).set_index("rule")


def regime_matrix(regimes: Mapping[str, np.ndarray],
                  rules: Mapping[str, Callable[[], TriggerRule]], *,
                  baseline: str = "symmetric", **kw: Any) -> pd.DataFrame:
    """Every rule against every regime -- the conditional table.

    This is the table that decides the question, because a rule tuned for trend will
    look wonderful on a trending sample by construction.  What matters is the row for
    the regime it was **not** built for: the cost of calling it wrong.  Returns a long
    frame (regime, rule) with ``vs_base`` and the delta statistics.
    """
    frames = []
    for rname, paths in regimes.items():
        f = compare_rules(paths, rules, baseline=baseline, **kw)
        f.insert(0, "regime", rname)
        frames.append(f.reset_index())
    return pd.concat(frames, ignore_index=True).set_index(["regime", "rule"])


def giveback_sensitivity(regimes: Mapping[str, np.ndarray], h: float, *,
                         give_backs: Sequence[float] = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0),
                         **kw: Any) -> pd.DataFrame:
    """Sweep the ratchet's give-back against a symmetric band of the same ``h``.

    The give-back is the parameter the whole rule hinges on and it has a sharp
    trade-off: small give-back hedges near the extreme (good) but is triggered by any
    wiggle (so it degenerates towards the symmetric band and pays more cost); large
    give-back holds through the noise but hands back the move on every reversal.
    """
    rows = []
    for rname, paths in regimes.items():
        base = score_paths(paths, lambda: SymmetricBand(h), **kw)
        for g in give_backs:
            s = score_paths(paths, lambda g=g: Ratchet(h, give_back=float(g)), **kw)
            d = s["_net_paths"] - base["_net_paths"]
            rows.append({"regime": rname, "give_back": float(g), "net": s["net"],
                         "vs_symmetric": float(d.mean()),
                         "se": float(d.std(ddof=1) / math.sqrt(d.size)),
                         "n_hedges": s["n_hedges"],
                         "p95_abs_delta": s["p95_abs_delta"],
                         "end_abs_delta": s["end_abs_delta"],
                         "cost": s["cost"]})
    return pd.DataFrame(rows).set_index(["regime", "give_back"])


def delta_profile(paths: np.ndarray, rule_factory: Callable[[], TriggerRule], *,
                  gamma: float, cap_spot: float | None = None,
                  quantiles: Sequence[float] = (0.5, 0.9, 0.95, 0.99)) -> pd.DataFrame:
    """Distribution of the delta carried under a rule -- the delta-cap view.

    Columns: quantiles of ``|delta|`` over all bars of all paths, and of the delta held
    at the end of the window (what the user actually wakes up with).
    """
    P = np.asarray(paths, float)
    allx: list[np.ndarray] = []
    endx: list[float] = []
    for j in range(P.shape[0]):
        rec = run_rule(P[j], rule_factory(), cap=cap_spot)
        allx.append(np.abs(float(gamma) * rec.x_path))
        endx.append(abs(float(gamma) * rec.open_leg))
    a = np.concatenate(allx)
    e = np.asarray(endx, float)
    return pd.DataFrame({
        "quantile": list(quantiles),
        "abs_delta_any_time": [float(np.quantile(a, q)) for q in quantiles],
        "abs_delta_at_open": [float(np.quantile(e, q)) for q in quantiles],
    }).set_index("quantile")


# --------------------------------------------------------------------------- #
# 6.  Bridge to the real backtester, so costs are charged by the shipped engine
# --------------------------------------------------------------------------- #
def backtest_with_rule(path: Any, cfg: Any, rule_factory: Callable[[], TriggerRule] | None,
                       *, cap_delta_base: float | None = None) -> Any:
    """:func:`fxgamma.backtest.engine.run_backtest` with a pluggable spot-trigger rule.

    The engine's hedge decision is hard-coded to ``band`` / ``time`` / ``gamma_budget``
    on **delta**, and this module must not edit it, so the loop is mirrored here with
    exactly the same accounting (cash accounting, one-way ``lam = cost_bp/2/1e4`` on the
    traded notional at the executed rate, the same option marks through
    :mod:`fxgamma.models.gk`).  :func:`verify_against_engine` is the proof that the
    mirror is faithful: with ``rule_factory=None`` this function reproduces
    ``run_backtest`` **bit for bit**, and only the trigger differs when a rule is passed.

    The rule triggers on **spot distance since the last hedge**, which is what a resting
    order does, rather than on delta.  On a book whose gamma moves a lot over the window
    the two differ, and that difference is part of what is being measured.
    """
    from ..backtest.engine import BacktestResult, Leg, _legs_for, _mark   # noqa: PLC0415
    from ..backtest.metrics import summarise                              # noqa: PLC0415
    from .zones import COST_BP                                            # noqa: PLC0415
    from datetime import timedelta                                        # noqa: PLC0415

    df = path.df
    n = len(df) if cfg.max_steps is None else min(len(df), int(cfg.max_steps))
    spec = pair_spec(cfg.pair)
    conv = spec.delta_convention
    cost_bp = float(cfg.cost_bp if cfg.cost_bp is not None
                    else (cfg.hedge.cost_bp if cfg.hedge.cost_bp else COST_BP.get(cfg.pair, 0.5)))
    lam = cost_bp / 2.0 / 1e4
    vega_spread = cfg.vega_spread_pts / 100.0 / 2.0

    legs: list[Leg] = []
    cash = 0.0
    pos = 0.0
    last_hedge_i = -10 ** 9
    rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    prev_equity = 0.0
    cum_cost = 0.0
    rule: TriggerRule | None = None

    for i in range(n):
        r = df.iloc[i]
        now = df.index[i]
        S, vol, rd, rf = float(r["spot"]), float(r["vol"]), float(r["rd"]), float(r["rf"])
        step_cost = 0.0

        expired = [lg for lg in legs if lg.T(now) <= 0.0]
        if expired:
            for lg in expired:
                intrinsic = max(lg.cp * (S - lg.strike), 0.0) * lg.notional_base * lg.direction
                cash += intrinsic
                trades.append({"time": now, "kind": "expiry", "cp": lg.cp,
                               "strike": lg.strike, "notional": lg.notional_base,
                               "direction": lg.direction, "spot": S,
                               "cash": intrinsic, "cost": 0.0})
            legs = [lg for lg in legs if lg.T(now) > 0.0]
        roll_due = (not legs) or (cfg.roll_days is not None and legs
                                  and (now - legs[0].entry_time).days >= cfg.roll_days)

        if roll_due and i < n - 1:
            if legs:
                pv_old, *_ = _mark(legs, now, S, vol, rd, rf, conv)
                pv_bid, *_ = _mark(legs, now, S, max(vol - vega_spread, 1e-4), rd, rf, conv)
                pv_ask, *_ = _mark(legs, now, S, vol + vega_spread, rd, rf, conv)
                exit_pv = pv_bid if pv_old >= 0 else pv_ask
                cash += exit_pv
                step_cost += abs(exit_pv - pv_old)
                trades.append({"time": now, "kind": "unwind", "cp": 0, "strike": np.nan,
                               "notional": sum(l.notional_base for l in legs),
                               "direction": 0, "spot": S, "cash": exit_pv,
                               "cost": abs(exit_pv - pv_old)})
                legs = []
            side = int(cfg.direction)
            if cfg.entry is not None:
                from ..backtest.engine import View                        # noqa: PLC0415
                side = int(cfg.entry(View(df, i, cfg.pair, path.meta)))
            if side != 0:
                new = _legs_for(cfg, now, S, vol, rd, rf, side)
                pv_mid, *_ = _mark(new, now, S, vol, rd, rf, conv)
                pv_exec, *_ = _mark(new, now, S,
                                    vol + vega_spread if side > 0 else max(vol - vega_spread, 1e-4),
                                    rd, rf, conv)
                cash -= pv_exec
                step_cost += abs(pv_exec - pv_mid)
                legs = new
                for lg in new:
                    trades.append({"time": now, "kind": "open", "cp": lg.cp,
                                   "strike": lg.strike, "notional": lg.notional_base,
                                   "direction": lg.direction, "spot": S,
                                   "cash": -pv_exec / len(new),
                                   "cost": abs(pv_exec - pv_mid) / len(new)})
                if rule_factory is not None:            # a fresh rule per structure
                    rule = rule_factory()
                    rule.reset(S)

        pv, dl, ga, g1, ve, th = _mark(legs, now, S, vol, rd, rf, conv) if legs else (0,) * 6
        delta_total = dl + pos

        traded = 0.0
        hrule = cfg.hedge
        gross = sum(lg.notional_base for lg in legs) or cfg.notional_base
        if rule_factory is None:
            if hrule.mode == "band":
                band = (hrule.band_delta if hrule.band_delta > 0 else hrule.band_pct * gross)
                if abs(delta_total - hrule.target_delta) > band:
                    traded = -(delta_total - hrule.target_delta)
            elif hrule.mode == "time":
                every = cfg.hedge_every_steps or max(int(round(hrule.every_hours / 24.0
                                                               * path.steps_per_day)), 1)
                if i - last_hedge_i >= every:
                    traded = -(delta_total - hrule.target_delta)
            elif hrule.mode == "gamma_budget":
                band = max(abs(hrule.band_delta), 1.0)
                if abs(delta_total - hrule.target_delta) > band:
                    traded = -(delta_total - hrule.target_delta)
        elif rule is not None and legs:
            lo_t, hi_t = rule.triggers()
            if cap_delta_base is not None and ga != 0:
                capx = abs(float(cap_delta_base) / ga)
                lo_t, hi_t = max(lo_t, rule.ref - capx), min(hi_t, rule.ref + capx)
            if S >= hi_t or S <= lo_t:
                traded = -(delta_total - hrule.target_delta)
                rule.on_fill(S)
            else:
                rule.observe(S, S, S)

        if traded != 0.0:
            c = abs(traded) * S * lam
            cash -= traded * S + c
            pos += traded
            step_cost += c
            last_hedge_i = i
            trades.append({"time": now, "kind": "hedge", "cp": 0, "strike": np.nan,
                           "notional": abs(traded), "direction": int(np.sign(traded)),
                           "spot": S, "cash": -(traded * S + c), "cost": c})

        equity = cash + pv + pos * S
        cum_cost += step_cost
        rows.append({
            "time": now, "spot": S, "vol": vol, "pv": pv, "cash": cash,
            "spot_pos": pos, "delta_options": dl, "delta_total": dl + pos,
            "gamma": ga, "gamma_1pct": g1, "vega": ve, "theta": th,
            "equity": equity, "pnl": equity - prev_equity,
            "cost": step_cost, "cum_cost": cum_cost,
            "hedge_base": traded, "n_legs": len(legs),
        })
        prev_equity = equity

    eq = pd.DataFrame(rows).set_index("time")
    eq["pnl_gross"] = eq["pnl"] + eq["cost"]
    eq["equity_gross"] = eq["pnl_gross"].cumsum()
    tr = pd.DataFrame(trades) if trades else pd.DataFrame(
        columns=["time", "kind", "cp", "strike", "notional", "direction", "spot", "cash", "cost"])
    stats = summarise(eq, tr, path, cfg)
    meta = dict(path.meta)
    meta["hedge_rule"] = ("engine band (mirror)" if rule_factory is None
                          else rule_factory().describe())
    return BacktestResult(eq, tr, stats, cfg, meta)


def verify_against_engine(path: Any, cfg: Any) -> dict:
    """Prove the mirrored loop is the engine's loop.

    Runs :func:`fxgamma.backtest.engine.run_backtest` and
    :func:`backtest_with_rule` (``rule_factory=None``) on the same path and config and
    returns the maximum absolute difference in equity, cost and hedge count.  All three
    must be zero; anything else means this module's accounting has drifted from the
    shipped engine and no number produced here can be trusted.
    """
    from ..backtest.engine import run_backtest                            # noqa: PLC0415
    a = run_backtest(path, cfg)
    b = backtest_with_rule(path, cfg, None)
    d_eq = float(np.max(np.abs(a.equity["equity"].to_numpy() - b.equity["equity"].to_numpy())))
    d_c = float(np.max(np.abs(a.equity["cum_cost"].to_numpy() - b.equity["cum_cost"].to_numpy())))
    n_a = int((a.trades["kind"] == "hedge").sum()) if len(a.trades) else 0
    n_b = int((b.trades["kind"] == "hedge").sum()) if len(b.trades) else 0
    return {"max_abs_equity_diff": d_eq, "max_abs_cost_diff": d_c,
            "n_hedges_engine": n_a, "n_hedges_mirror": n_b,
            "identical": bool(d_eq == 0.0 and d_c == 0.0 and n_a == n_b)}
