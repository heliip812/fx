"""The overnight resting-order ladder: what to leave in the market before going home.

``overnight_ladder(book, mkt, pair, ...) -> list[LadderRung]`` and
``ladder_summary(...) -> dict`` (docs/08_overnight_gamma.md s3).

READ THIS FIRST: overnight gamma is usually negative carry
----------------------------------------------------------
London close to London open is 14 of 24 hours, so you pay **58% of a day's theta**.
On the shipped EURUSD hour profile that window carries only **34% of a day's
variance** -- Asia is quiet and the two hours around the New York close are the
deadest of the twenty-four.  Correcting for the fact that theta runs on 365 calendar
days while variance is delivered over 252 trading days, the window pays for itself
only if realised vol comes in about **9% above implied**; over a weekend, where you
pay 2.6 calendar days of theta for ~0.35 days of tradeable variance, it needs realised
vol about **2.3x implied**.  :func:`crossover_vol` turns that into the one number the
decision actually needs: *the ATM implied at which tonight's forecast range exactly
pays tonight's theta*.  Above it, holding the gamma overnight loses money in
expectation.

So this module does **not** claim the ladder monetises anything.  Under driftless
spot, leaving the orders does not change the expected P&L at all: mark-to-market
already contains the gamma, every hedge is a fair bet, and the ladder's only effect on
the mean is **minus its transaction cost**.  What it does buy is

* **conversion** -- unrealised mark-to-market becomes realised cash you keep even if
  spot comes back, and
* **variance reduction** -- you do not wake up holding a delta you never chose.

Those are worth having.  They are not "capture", and the fields here are named
accordingly (``exp_realised``, ``exp_cost``, ``exp_marginal``, ``sd_reduction_pct``).

Three things it gets right that a naive ladder gets wrong
--------------------------------------------------------

**1. Session variance time, not clock time.**  See above; the ratio is 1.7x on
EURUSD, and every rung distance and every touch probability scales with the sigma it
produces.  Scheduled events are a real part of it -- roughly a third of the shipped
calendar falls inside this window -- so the profile is event-aware rather than a
single scalar: see :func:`session_variance_weight` and :data:`EVENT_VAR_UPLIFT`.

**2. The number of times a rung actually pays is a local-time question.**  For a grid
of spacing ``h``, the expected number of crossings of the level ``x`` away from spot
over a window whose spot standard deviation is ``s`` is ``E[L(x)] / h`` where
``E[L(x)] = 2 s (phi(u) - u (1 - Phi(u)))``, ``u = |x| / s``, is the expected Brownian
local time at that level (Tanaka's formula).  Summing over the grid reproduces
``E[total crossings] = V / h^2`` and hence ``sum of conversions = Gamma V / 2``, the
position's whole gamma P&L.  That identity is the proof that the ladder converts
rather than creates.  :func:`expected_crossings`.

**3. The delta cap leads; the analytic band is a refinement inside it.**  On the
reference book the entire band decision is worth of order 60 USD a night, while the
delta you are willing to wake up holding is worth thousands, and a realistic minimum
clip binds before the analytic optimum does.  ``overnight_ladder`` therefore takes
``max_overnight_delta`` as its primary risk input, clamps the
:func:`fxgamma.portfolio.bandopt.optimal_band` spacing inside it, and reports which
constraint actually set the spacing.  Nothing here asks the user for a risk-aversion
coefficient; :func:`fxgamma.portfolio.bandopt.risk_aversion_for_band` goes the other
way when a coefficient is needed downstream.

Cost is a retail cost.  ``zones.COST_BP`` is an interbank table and this user has no
OTC access, so the default here is :data:`fxgamma.portfolio.bandopt.RETAIL_COST_BP`
and :func:`ladder_cost_sensitivity` shows how much of the answer that assumption owns.

Short gamma
-----------
The ladder inverts and the orders become **stops**, not limits.  A short-gamma ladder
left unattended is a materially different and more dangerous object than a long-gamma
one -- the expected gamma term is negative, the theta is the entire edge, and the
stops that are supposed to protect it are exactly the orders that will not fill at
your price in the gap you are worried about.  :func:`ladder_summary` refuses to frame
it as an income strategy and returns ``gamma_side="short"`` with the warnings up
front.  Nothing here stops you leaving one; it will not pretend it is the same trade.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..conventions import pair_spec
from ..models import gk
from ..types import Book, HedgeRule, MarketSnapshot
from .bandopt import (BandResult, BookGamma, book_gamma, cost_bp_for, optimal_band)
from .risk import fx_rate, shift_market, spot_ladder
from .zones import TRADING_DAYS, touch_probability

__all__ = [
    "LadderRung", "PassiveWindow", "SessionProfile",
    "DEFAULT_HOUR_PROFILES", "EVENT_VAR_UPLIFT", "NIGHT_CALIBRATION", "MARKET_CLOSE_UTC_H",
    "MARKET_OPEN_UTC_H", "RETAIL_LOT_BASE",
    "passive_window", "session_variance_weight", "hour_profile",
    "estimate_hour_profile", "expected_crossings", "expected_local_time",
    "crossover_vol", "overnight_ladder", "ladder_summary",
    "ladder_cost_sensitivity", "ladder_frame", "format_ladder",
]

#: Smallest base-ccy clip a retail account can actually deal (one standard lot).
#: The trader review's point that the clip floor binds before the analytic optimum
#: does: a 30-pip ladder on a 3mm-gamma book asks for clips this size or smaller, and
#: a clip you cannot deal is not a band, it is a rounding error.
RETAIL_LOT_BASE = 100_000.0

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# contract types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LadderRung:
    """One resting order.  Frozen contract fields first, diagnostics appended.

    ``side``            ``+1`` buy base / ``-1`` sell base, the same sign convention as
                        :class:`fxgamma.types.HedgeAction.trade_base`.
    ``clip_base``       size of *this* order, base ccy, always positive.
    ``cum_delta_base``  the book's **net** delta once this rung and every rung inside
                        it has filled.  It should sit at the rule's target (0 by
                        default) at every rung: that is the "sensibly hedged at each
                        rung, not overhedged at the first" test, and it is why clips
                        are the delta *increments* between rungs rather than an equal
                        split of the total.
    ``p_touch``         first-passage probability of reaching this level inside the
                        window (``zones.touch_probability``, forward drift).
    ``exp_pnl``         **net cash this rung is expected to bank**:
                        ``exp_realised - exp_cost``.  Read the module docstring before
                        reading it as profit: under driftless spot the ladder does not
                        change the expected P&L of the position at all, it converts
                        mark-to-market into realised cash and caps the delta.  The
                        rung's effect on the *mean* is ``exp_marginal = -exp_cost``.
    """
    level: float
    side: int
    clip_base: float
    cum_delta_base: float
    pips_from_spot: float
    p_touch: float
    exp_pnl: float
    anchor: str = ""
    anchor_dist_pips: float = 0.0
    # ---- additive diagnostics (defaults preserve the frozen constructor) ----
    pair: str = ""
    k: int = 0                       # rung index, 1 = nearest spot
    order_type: str = "limit"        # limit (long gamma) | stop (short gamma)
    spacing_pips: float = 0.0        # distance to the next rung inward
    sigma_dist: float = 0.0          # distance in window sigmas
    exp_crossings: float = 0.0       # expected fills over the window (local time)
    exp_realised: float = 0.0        # mark-to-market CONVERTED to cash here, quote ccy
    exp_cost: float = 0.0            # expected transaction cost, quote ccy
    exp_marginal: float = 0.0        # effect on the expected P&L: exactly -exp_cost
    cost_bp: float = 0.0             # round-trip cost assumption used
    kappa: float = 1.0               # path-roughness multiplier on exp_crossings
    delta_at_level: float = 0.0      # book option delta at this level (pre-hedge)
    ccy: str = ""
    fx_to_report: float = 1.0
    note: str = ""

    @property
    def exp_pnl_rep(self) -> float:
        return self.exp_pnl * self.fx_to_report

    def as_dict(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in
                ("pair", "k", "level", "side", "order_type", "clip_base",
                 "cum_delta_base", "pips_from_spot", "spacing_pips", "sigma_dist",
                 "p_touch", "exp_crossings", "exp_realised", "exp_cost", "exp_pnl",
                 "exp_marginal", "anchor", "anchor_dist_pips", "delta_at_level",
                 "ccy", "cost_bp", "kappa", "note")}


@dataclass(frozen=True)
class PassiveWindow:
    """The hours the user is not watching.

    ``var_fraction`` is the share of a **full day's** variance that falls inside the
    window -- 1.0 would mean "a whole day's worth".  It is emphatically not the share
    of the clock: see the module docstring.  ``passive_window`` fills it from the
    hour-of-day profile; ``session_variance_weight`` computes it for a given pair.
    """
    start: datetime
    end: datetime
    label: str = "London close -> London open"
    var_fraction: float = 1.0
    # ---- additive ----
    clock_hours: float = 0.0
    calendar_days: float = 0.0       # for theta: end - start in calendar days
    open_hours: float = 0.0          # hours the market is actually open
    tz: str = "Europe/London"
    profile_source: str = ""
    spans_weekend: bool = False
    note: str = ""
    # ---- event-aware detail: var_fraction is a scalar, the night is not ----
    #: (UTC hour start, days-of-variance contributed) for every hour of the window,
    #: so a consumer can see *where* the variance sits rather than only how much.
    hour_var: tuple[tuple[datetime, float], ...] = ()
    #: days of variance attributable to scheduled events inside the window
    event_var: float = 0.0
    #: the events themselves, "HH:MM CCY label (importance)"
    events: tuple[str, ...] = ()

    @property
    def clock_fraction(self) -> float:
        """What a naive 14/24 calculation would have given.  Kept to shame it."""
        return self.clock_hours / 24.0


@dataclass(frozen=True)
class SessionProfile:
    """Hour-of-day variance weights, UTC, normalised so the 24 weights sum to 24.

    ``weights[h]`` is the variance of hour ``h`` relative to the average hour of a
    normal weekday.  ``source`` is one of ``"default"`` (the shipped profile),
    ``"estimated"`` (fitted from hourly bars) or ``"user"``.  Anything that is not
    ``"estimated"`` must be badged in the UI as a modelled default -- architecture s7.
    """
    pair: str
    weights: tuple[float, ...]
    source: str = "default"
    n_obs: int = 0
    span_days: float = 0.0
    note: str = ""

    def __post_init__(self) -> None:
        if len(self.weights) != 24:
            raise ValueError(f"need 24 hourly weights, got {len(self.weights)}")

    def array(self) -> np.ndarray:
        return np.asarray(self.weights, dtype=float)

    @property
    def peak_to_trough(self) -> float:
        a = self.array()
        return float(a.max() / a.min()) if a.min() > 0 else float("inf")

    def frame(self) -> pd.DataFrame:
        a = self.array()
        return pd.DataFrame({"hour_utc": np.arange(24), "var_weight": a,
                             "sigma_weight": np.sqrt(a),
                             "share_of_day_pct": 100.0 * a / a.sum(),
                             "pair": self.pair, "source": self.source})


# --------------------------------------------------------------------------- #
# hour-of-day variance profiles
# --------------------------------------------------------------------------- #
#: FX closes ~17:00 New York on Friday and reopens ~17:00 New York on Sunday.  Held in
#: UTC here (21:00) rather than tracking US DST, which moves it by an hour twice a
#: year; the error is one thin hour on two weekends and is noted, not hidden.
MARKET_CLOSE_UTC_H = 21.0   # Friday
MARKET_OPEN_UTC_H = 21.0    # Sunday
#: Sunday's reopening hours are thinner than the same hours midweek.
SUNDAY_THIN_FACTOR = 0.55

#: Extra variance a scheduled event puts into its hour, in **average-hour units**
#: (the same units as :data:`DEFAULT_HOUR_PROFILES`), by ``importance``.
#:
#: A ``PassiveWindow.var_fraction`` that is one scalar cannot describe a night with a
#: BoJ decision in it, and roughly a third of the shipped event calendar falls inside
#: the London-close-to-open window (BoJ, RBA, RBNZ and every Asian data print live
#: there).  So the window's variance is built hour by hour with these uplifts added to
#: the hour that contains the event.  An importance-3 hour ends up carrying roughly
#: 5-10x a normal hour of that time of day, which is the order of magnitude the
#: intraday event-study literature reports.
#:
#: MODELLED DEFAULTS.  When a ``signals.rangeforecast.RangeForecast`` is available its
#: ``components["event"]`` is the measured version and should be preferred -- pass the
#: forecast to :func:`overnight_ladder` and it overrides all of this.
EVENT_VAR_UPLIFT: dict[int, float] = {3: 4.0, 2: 1.5, 1: 0.4}

#: Relative **variance** per UTC hour, average weekday hour = 1.0 after normalising.
#:
#: PROVENANCE: these are MODELLED DEFAULTS, not measurements from this user's data.
#: Their shape follows the intraday seasonality that is standard in the literature
#: (Andersen & Bollerslev 1997/1998 on FX intraday periodicity; Bollerslev & Domowitz
#: on FX quote arrival): an Asian trough around 03:00-05:00 UTC, a step up at the
#: London open (07:00-08:00 UTC), the London/New York overlap peak (12:00-16:00 UTC,
#: which is where the US data releases at 12:30/13:30 UTC and the 4pm London WM/R fix
#: sit), and the day's genuine trough at 21:00-23:00 UTC around the New York close.
#: Pair-specific tilts: USDJPY and the JPY crosses carry a real Tokyo-fix bump at
#: 00:00 UTC (09:55 Tokyo); GBP is the most London-centric of the majors; AUD and NZD
#: keep more of their variance in Asia.
#:
#: The day/night **level** of all of them is then rescaled by :data:`NIGHT_CALIBRATION`
#: so that EURUSD reproduces the one measured session share available (0.382).
#:
#: **Replace them with measurement as soon as you can.**  :func:`estimate_hour_profile`
#: fits the same object from hourly bars, and Yahoo serves ~730 days of hourly FX bars,
#: which is ample.  Network hosts are blocked in this sandbox, so nothing here has been
#: fitted to data; ``SessionProfile.source`` says ``"default"`` and every consumer must
#: badge it.
_EURUSD = (0.55, 0.50, 0.45, 0.40, 0.40, 0.45, 0.65, 1.35, 1.75, 1.70, 1.45, 1.30,
           1.80, 2.20, 2.30, 2.05, 1.60, 1.10, 0.80, 0.65, 0.55, 0.40, 0.30, 0.35)
_USDJPY = (1.15, 0.95, 0.80, 0.60, 0.55, 0.55, 0.70, 1.20, 1.55, 1.45, 1.25, 1.15,
           1.70, 2.10, 2.15, 1.85, 1.45, 1.00, 0.75, 0.60, 0.50, 0.40, 0.35, 0.55)
_GBPUSD = (0.45, 0.40, 0.38, 0.35, 0.35, 0.40, 0.65, 1.55, 2.00, 1.90, 1.55, 1.35,
           1.80, 2.20, 2.25, 2.00, 1.55, 1.05, 0.75, 0.60, 0.50, 0.35, 0.28, 0.30)
_AUDUSD = (1.25, 1.20, 1.10, 0.85, 0.70, 0.65, 0.70, 1.15, 1.45, 1.40, 1.20, 1.10,
           1.60, 1.95, 2.00, 1.75, 1.35, 0.95, 0.70, 0.60, 0.55, 0.50, 0.50, 0.75)
_GENERIC = (0.70, 0.65, 0.58, 0.50, 0.48, 0.52, 0.70, 1.25, 1.60, 1.55, 1.35, 1.25,
            1.70, 2.05, 2.10, 1.90, 1.50, 1.10, 0.82, 0.68, 0.58, 0.48, 0.42, 0.54)


#: Hours (UTC) that fall in the London-close-to-London-open window under BST.
_NIGHT_HOURS_UTC = frozenset({16, 17, 18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5})

#: Single day/night rebalancing factor applied to every shipped profile.
#:
#: The *shape* of the profiles is modelled (s3 of docs/09).  Their day/night **level**
#: is anchored on the one measurement available: the forecasting quant's estimator puts
#: the London-close-to-open window at **0.382** of a day's variance on EURUSD, against
#: 0.583 of the clock -- a theta-per-variance-day ratio of **1.53** and a sigma
#: multiplier of sqrt(0.583/0.382) = 1.236.  The raw modelled shape gave 0.339 (ratio
#: 1.72), i.e. it made the night too quiet.  Scaling the night hours of every profile by
#: this factor and renormalising reproduces 0.382 exactly on EURUSD and shifts the other
#: pairs consistently, while leaving the *relative* pair tilts (the Tokyo fix on JPY, the
#: London concentration on GBP, the Asian weight on AUD) as modelled as they were.
#:
#: What is measured: the EURUSD level. What is not: every pair tilt, and the level on
#: every other pair. Replace the lot with `estimate_hour_profile` on real hourly bars.
NIGHT_CALIBRATION = 1.2033


def _norm24(w: Sequence[float], calibrate: bool = True) -> tuple[float, ...]:
    a = np.asarray(w, dtype=float).copy()
    if calibrate:
        for h in _NIGHT_HOURS_UTC:
            a[h] *= NIGHT_CALIBRATION
    return tuple(float(x) for x in a * 24.0 / a.sum())


DEFAULT_HOUR_PROFILES: dict[str, tuple[float, ...]] = {
    "EURUSD": _norm24(_EURUSD),
    "USDJPY": _norm24(_USDJPY),
    "GBPUSD": _norm24(_GBPUSD),
    "AUDUSD": _norm24(_AUDUSD),
    "NZDUSD": _norm24(_AUDUSD),
    "EURJPY": _norm24(tuple(0.5 * (a + b) for a, b in zip(_EURUSD, _USDJPY))),
    "EURGBP": _norm24(tuple(0.5 * (a + b) for a, b in zip(_EURUSD, _GBPUSD))),
    "": _norm24(_GENERIC),
}


def hour_profile(pair: str = "", profile: SessionProfile | Sequence[float] | None = None
                 ) -> SessionProfile:
    """Resolve ``profile`` to a :class:`SessionProfile`, falling back to the default.

    Accepts a ``SessionProfile``, a bare 24-vector (treated as ``source="user"``), or
    ``None``.  Never silently substitutes: the returned object always says where its
    numbers came from.
    """
    if isinstance(profile, SessionProfile):
        return profile
    if profile is not None:
        return SessionProfile(pair.upper(), _norm24(profile, calibrate=False), source="user",
                              note="caller-supplied 24-hour variance weights (UTC)")
    p = pair.upper()
    w = DEFAULT_HOUR_PROFILES.get(p)
    if w is None:
        w = DEFAULT_HOUR_PROFILES[""]
        note = (f"no pair-specific default for {p or '(unnamed)'}; using the generic G10 "
                "shape. MODELLED DEFAULT -- not estimated from data.")
    else:
        note = ("shipped default hour-of-day variance profile. MODELLED DEFAULT -- not "
                "estimated from this user's data; replace via estimate_hour_profile().")
    return SessionProfile(p, w, source="default", note=note)


def estimate_hour_profile(hourly: pd.DataFrame, pair: str = "", *,
                          price_col: str = "close", min_obs: int = 40,
                          max_gap_hours: float = 1.5, trim: float = 0.01,
                          smooth: bool = True, blend_default: bool = True
                          ) -> SessionProfile:
    """Fit the hour-of-day variance profile from hourly bars.

    ``hourly`` needs a ``DatetimeIndex`` (tz-aware, or assumed UTC) and a price
    column.  Yahoo serves roughly 730 days of hourly FX bars, which is about 12,000
    usable observations, i.e. ~500 per hour bucket -- enough for a stable profile.
    **Hosts are blocked in this sandbox, so this function has never been run against
    real data here**; the shipped :data:`DEFAULT_HOUR_PROFILES` are modelled, and the
    returned ``SessionProfile.source`` is the only thing that tells the two apart.

    Method
    ------
    Squared log returns bucketed by the UTC hour of the bar's *end*, averaged, then
    normalised to mean 1.  Returns whose bar spacing exceeds ``max_gap_hours`` are
    dropped (they span the weekend or a data hole and would dump two days of variance
    into one bucket).  ``trim`` winsorises the top and bottom tails of ``r^2`` in each
    bucket -- one NFP print in a thin bucket can otherwise double that hour's weight.
    ``smooth`` applies a circular 3-point [1,2,1] filter, because the profile is a
    smooth diurnal function and the estimator's noise is not.  ``blend_default``
    shrinks each bucket towards the shipped default with weight
    ``min_obs / (min_obs + n_h)``, so a short history degrades gracefully instead of
    producing a spiky profile that looks precise.
    """
    if price_col not in hourly.columns:
        raise KeyError(f"{price_col!r} not in hourly columns {list(hourly.columns)}")
    df = hourly.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("hourly must have a DatetimeIndex")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df.sort_index()
    px = df[price_col].astype(float)
    r = np.diff(np.log(px.to_numpy()))
    gap = np.diff(df.index.to_numpy()).astype("timedelta64[s]").astype(float) / 3600.0
    hr = df.index.hour.to_numpy()[1:]
    ok = np.isfinite(r) & (gap <= float(max_gap_hours)) & (gap > 0)
    r2, hr = r[ok] ** 2, hr[ok]
    if r2.size < 24 * 5:
        raise ValueError(f"only {r2.size} usable hourly returns -- too few to estimate a "
                         "24-hour profile; use the default and badge it")
    w = np.zeros(24)
    n = np.zeros(24, dtype=int)
    for h in range(24):
        v = r2[hr == h]
        n[h] = v.size
        if v.size == 0:
            continue
        if trim > 0 and v.size > 20:
            lo, hi = np.quantile(v, [trim, 1.0 - trim])
            v = v[(v >= lo) & (v <= hi)]
        w[h] = float(v.mean())
    if (n == 0).any():
        raise ValueError(f"hours {list(np.where(n == 0)[0])} have no observations")
    w = w / w.mean()
    if blend_default:
        d = np.asarray(hour_profile(pair).weights, dtype=float)
        k = float(min_obs) / (float(min_obs) + n.astype(float))
        w = k * d + (1.0 - k) * w
    if smooth:
        w = (np.roll(w, 1) + 2.0 * w + np.roll(w, -1)) / 4.0
    span = (df.index[-1] - df.index[0]).total_seconds() / 86400.0
    return SessionProfile(
        pair.upper(), _norm24(w, calibrate=False), source="estimated", n_obs=int(r2.size),
        span_days=float(span),
        note=(f"estimated from {int(r2.size):,} hourly returns over {span:,.0f} days "
              f"(min bucket {int(n.min())} obs); trim={trim:g}, "
              f"smooth={'3pt' if smooth else 'none'}, "
              f"shrink-to-default={'on' if blend_default else 'off'}"))


# --------------------------------------------------------------------------- #
# the window and its variance
# --------------------------------------------------------------------------- #
def _liquidity_factor(t: datetime) -> float:
    """0 while the FX market is shut, thinner on the Sunday reopen, else 1."""
    t = t.astimezone(UTC)
    dow, h = t.weekday(), t.hour + t.minute / 60.0
    if dow == 5:                                   # Saturday
        return 0.0
    if dow == 4 and h >= MARKET_CLOSE_UTC_H:       # Friday after the NY close
        return 0.0
    if dow == 6:                                   # Sunday
        return 0.0 if h < MARKET_OPEN_UTC_H else SUNDAY_THIN_FACTOR
    return 1.0


def session_variance_weight(start: datetime, end: datetime, pair: str,
                            profile: SessionProfile | Sequence[float] | None = None,
                            *, events: "pd.DataFrame | None" = None,
                            event_uplift: Mapping[int, float] | None = None,
                            detail: bool = False
                            ) -> float | tuple[float, list[tuple[datetime, float]], float, list[str]]:
    """Share of a **full day's** variance contained in ``[start, end)``.

    Walks the window in (partial) UTC hours, weighting each by the pair's hour-of-day
    variance profile and by a liquidity factor that is zero across the weekend
    closure and ``SUNDAY_THIN_FACTOR`` on the Sunday reopen.  Divides by 24 so that a
    full ordinary weekday returns 1.0.

    The weekend result is the useful one and it surprises people: Friday 17:00 London
    to Monday 07:00 London is 62 clock hours but only ~0.35 days of variance, because
    the market is shut for 48 of them.  You still pay **three** calendar days of
    theta.  :func:`ladder_summary` prints both.

    This does **not** model weekend *gap* risk -- the variance that arrives in one
    jump at the Sunday reopen and cannot be traded through.  Gap risk is a separate,
    fatter-tailed object; ``ladder_summary`` reports it as a scenario rather than
    folding it into a Gaussian sigma where it would be silently understated.

    ``events`` is the frozen calendar frame (``datetime, ccy, event, importance``)
    from ``MarketDataProvider.events``.  Events whose ``ccy`` is one of the pair's two
    currencies and whose timestamp falls inside the window add
    :data:`EVENT_VAR_UPLIFT` to their hour.  This is what stops a single scalar
    ``var_fraction`` from pricing a BoJ night the same as an ordinary Tuesday.

    ``detail=True`` returns ``(var_fraction, hour_var, event_var, event_labels)``.
    """
    prof = hour_profile(pair, profile)
    w = prof.array()
    spec = pair_spec(pair) if pair else None
    up = dict(EVENT_VAR_UPLIFT if event_uplift is None else event_uplift)
    s = start.astimezone(UTC) if start.tzinfo else start.replace(tzinfo=UTC)
    e = end.astimezone(UTC) if end.tzinfo else end.replace(tzinfo=UTC)
    if e <= s:
        return 0.0
    ev_rows = _events_in(events, s, e, spec)
    total = 0.0
    ev_total = 0.0
    hour_var: list[tuple[datetime, float]] = []
    t = s
    guard = 0
    while t < e and guard < 24 * 400:
        guard += 1
        nxt = (t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        step = min(nxt, e)
        frac = (step - t).total_seconds() / 3600.0
        liq = _liquidity_factor(t)
        base = w[t.hour] * frac * liq
        bump = 0.0
        for ts, imp, _lbl in ev_rows:
            if t <= ts < step:
                bump += float(up.get(int(imp), 0.0)) * (liq if liq > 0 else 1.0)
        total += base + bump
        ev_total += bump
        hour_var.append((t, (base + bump) / 24.0))
        t = step
    vf = float(total / 24.0)
    if detail:
        return vf, hour_var, float(ev_total / 24.0), [r[2] for r in ev_rows]
    return vf


def _events_in(events: "pd.DataFrame | None", s: datetime, e: datetime,
               spec: Any) -> list[tuple[datetime, int, str]]:
    """Rows of the frozen event calendar that land in the window and matter to the pair."""
    if events is None or not len(events):
        return []
    df = pd.DataFrame(events)
    if "datetime" not in df.columns:
        return []
    ts = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
    keep = ts.notna() & (ts >= pd.Timestamp(s)) & (ts < pd.Timestamp(e))
    if spec is not None and "ccy" in df.columns:
        keep &= df["ccy"].astype(str).str.upper().isin({spec.base, spec.quote})
    out: list[tuple[datetime, int, str]] = []
    for i in np.where(keep.to_numpy())[0]:
        r = df.iloc[int(i)]
        imp = int(r.get("importance", 1) or 1)
        out.append((ts.iloc[int(i)].to_pydatetime(), imp,
                    f"{ts.iloc[int(i)]:%a %H:%M}Z {r.get('ccy', '')} "
                    f"{r.get('event', '')} (imp {imp})"))
    return out


def passive_window(asof: datetime, tz: str = "Europe/London", close_h: int = 17,
                   open_h: int = 7, *, pair: str = "",
                   profile: SessionProfile | Sequence[float] | None = None,
                   events: "pd.DataFrame | None" = None,
                   label: str | None = None) -> PassiveWindow:
    """The next unattended window: today's ``close_h`` in ``tz`` to the next ``open_h``.

    If ``asof`` is already past ``close_h`` the window still starts at today's close
    (you are inside it), so the ladder you build at 18:00 covers the same night as the
    one you built at 16:45.  Friday's window runs to **Monday** morning; Saturday and
    Sunday roll forward to the Monday open too.

    ``tz`` is a real time zone, so the window follows British Summer Time rather than
    a fixed UTC offset -- London close is 16:00 UTC in summer and 17:00 UTC in winter,
    and those are different hours of the variance profile.
    """
    zone = ZoneInfo(tz)
    local = (asof if asof.tzinfo else asof.replace(tzinfo=UTC)).astimezone(zone)
    start_d = local.date()
    start = datetime.combine(start_d, time(int(close_h)), tzinfo=zone)
    if local < start and local.weekday() < 5:
        # before today's close: the window we are pricing is still tonight's
        pass
    end_d = start_d + timedelta(days=1)
    while end_d.weekday() >= 5:                    # Sat/Sun -> Monday
        end_d += timedelta(days=1)
    end = datetime.combine(end_d, time(int(open_h)), tzinfo=zone)
    if start.weekday() >= 5:                       # built on a weekend: start at Friday close
        back = start_d
        while back.weekday() >= 5:
            back -= timedelta(days=1)
        start = datetime.combine(back, time(int(close_h)), tzinfo=zone)
    vf, hour_var, ev_var, ev_lbl = session_variance_weight(
        start, end, pair, profile, events=events, detail=True)
    prof = hour_profile(pair, profile)
    clock = (end - start).total_seconds() / 3600.0
    openh = _open_hours(start, end)
    spans_we = (end.date() - start.date()).days > 1
    lbl = label or (f"{tz.split('/')[-1]} close {close_h:02d}:00 -> open {open_h:02d}:00"
                    + (" (over the weekend)" if spans_we else ""))
    return PassiveWindow(
        start=start.astimezone(UTC), end=end.astimezone(UTC), label=lbl,
        var_fraction=vf, clock_hours=clock,
        calendar_days=(end - start).total_seconds() / 86400.0,
        open_hours=openh, tz=tz, profile_source=prof.source, spans_weekend=spans_we,
        hour_var=tuple(hour_var), event_var=ev_var, events=tuple(ev_lbl),
        note=(f"{clock:.0f} clock hours ({clock / 24.0 * 100:.0f}% of the clock) but "
              f"{vf * 100:.0f}% of a day's variance on the {prof.source} "
              f"{prof.pair or 'generic'} hour profile"
              + (f"; {clock - openh:.0f} of those hours the market is shut"
                 if clock - openh > 0.5 else "")
              + (f"; {ev_var * 100:.0f} pts of that variance is scheduled events "
                 f"({len(ev_lbl)} in the window)" if ev_var > 0 else "")))


def _open_hours(start: datetime, end: datetime) -> float:
    t, tot, guard = start, 0.0, 0
    while t < end and guard < 24 * 400:
        guard += 1
        nxt = t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        step = min(nxt, end)
        if _liquidity_factor(t) > 0:
            tot += (step - t).total_seconds() / 3600.0
        t = step
    return tot


# --------------------------------------------------------------------------- #
# local time / crossings
# --------------------------------------------------------------------------- #
def expected_local_time(x: float | np.ndarray, sd: float) -> float | np.ndarray:
    """``E[L_T(x)]`` for a driftless Brownian motion started at 0 with ``sd(X_T) = sd``.

    By Tanaka's formula ``|X_T - x| = |x| + int sgn dX + L_T(x)``, so
    ``E[L_T(x)] = E|X_T - x| - |x|``.  With ``X_T ~ N(0, sd^2)``::

        E[L_T(x)] = 2 sd [ phi(u) - u (1 - Phi(u)) ],   u = |x| / sd

    Units are the units of ``x`` (spot).  At ``x = 0`` it is ``2 sd phi(0) = 0.798 sd``;
    it decays like a Gaussian tail in ``u``.  Sanity check that ties it to the rest of
    the module: ``int E[L_T(x)] dx = E[<X>_T] = sd^2``, the quadratic variation, which
    is why summing ``E[L]/h`` over a grid of spacing ``h`` gives ``sd^2 / h^2``.
    """
    sd = float(sd)
    u = np.abs(np.asarray(x, dtype=float)) / sd if sd > 0 else np.inf
    val = 2.0 * sd * (gk._norm_pdf(u) - u * (1.0 - gk._norm_cdf(u)))
    return float(val) if np.isscalar(x) else np.asarray(val)


def expected_crossings(x: float | np.ndarray, sd: float, h: float,
                       kappa: float = 1.0) -> float | np.ndarray:
    """Expected number of times a level ``x`` from spot is crossed in ``h``-sized steps.

    ``E[N(x)] = E[L_T(x)] / h``.  This is the number of times a resting order at that
    level actually **fills** over the window, given that after each fill the position
    is only re-established once spot has travelled ``h`` back the other way -- which is
    precisely what a ladder of spacing ``h`` does.

    It is not a probability and it is not bounded by 1: the nearest rung of a tight
    ladder on a choppy night fills several times, and that is where the money is.
    ``p_touch`` says whether you get filled once; this says how often.

    ``kappa`` is a **path-roughness multiplier**, and it is an input, not a forecast.
    A real path is not Brownian: it crosses a fine grid more or fewer times than
    ``E[L]/h`` for the same terminal variance, and that ratio is exactly what decides
    how many times a ladder pays.  The forecasting quant implemented and verified the
    crossings/efficiency machinery (identity to 1-5%) and found that **forecasting
    kappa from daily bars loses to the Brownian null on 5 of 5 pairs** -- daily
    sampling recovers only 46-63% of the true crossing count, so the estimate is not
    predictive.  Therefore ``kappa`` defaults to **1.0 (Brownian)** everywhere, and the
    panel must say that expected fills assume a Brownian path.  A user with a view
    ("this pair chops") can set it; the tool will not set it for them.
    """
    return float(kappa) * expected_local_time(x, sd) / float(h)


# --------------------------------------------------------------------------- #
# is tonight worth holding at all?  (the crossover vol)
# --------------------------------------------------------------------------- #
def crossover_vol(book: Book, mkt: MarketSnapshot, pair: str, *,
                  window: PassiveWindow | None = None,
                  range_forecast: Any | None = None,
                  profile: SessionProfile | Sequence[float] | None = None,
                  events: "pd.DataFrame | None" = None,
                  marks: Mapping[str, float] | None = None,
                  report_ccy: str = "USD",
                  lo: float = -0.90, hi: float = 4.0, tol: float = 1e-6) -> dict[str, Any]:
    """The go / no-go number: the ATM implied at which tonight exactly breaks even.

    The window's gamma P&L on a **fixed** forecast range ``sd`` is
    ``0.5 * Gamma(sigma) * sd^2``; the theta bill is ``|theta(sigma)| * calendar_days``.
    Gamma falls and theta rises with implied, so the two cross once.  Solve

        0.5 * Gamma(sigma*) * sd^2  =  |theta(sigma*)| * calendar_days

    by bisection on a parallel vol shift, repricing the whole book each time (rates,
    skew, multiple expiries and all -- not the textbook ATM identity).  Above
    ``sigma*`` you are paying more theta than the forecast range can pay back, and
    holding gamma over the window is negative carry **in expectation**; the ladder
    does not change that, it only decides how much of the outcome you bank and how
    much delta you wake up holding.

    The closed form behind it, for intuition: with the BS identity
    ``theta = -0.5 Gamma S^2 sigma^2 / 365`` the crossover is

        sigma* = sigma_window * sqrt(365 / calendar_days)

    and, if the range forecast is just the implied scaled by session variance
    (``sigma_window = sigma sqrt(var_fraction / 252)``), that collapses to
    ``sigma* / sigma = sqrt(365 * var_fraction / (252 * calendar_days))`` -- a pure
    calendar fact, independent of the book.  On the shipped EURUSD profile it is
    **0.92** for a weeknight (you need realised ~9% over implied) and **0.44** over a
    weekend (you need realised ~2.3x implied).  The 252-vs-365 split is amendment
    v1.4 W-7 and it is doing real work here: ignore it and the weeknight number comes
    out as 1.31x instead of 1.09x.
    """
    spec = pair_spec(pair)
    win = window or passive_window(mkt.asof, pair=pair, profile=profile, events=events)
    bg0 = book_gamma(book, mkt, pair, marks=marks)
    S = bg0.spot
    if range_forecast is not None:
        sw = float(getattr(range_forecast, "sigma_window", range_forecast))
        basis = "range forecast"
    else:
        sw = bg0.sigma * math.sqrt(max(win.var_fraction, 1e-12) / TRADING_DAYS)
        basis = (f"ATM {bg0.sigma * 100:.2f}% x sqrt({win.var_fraction:.3f}/252) "
                 "-- NOT a forecast, just today's implied put through the session clock")
    sd = S * sw
    D = win.calendar_days

    def f(x: float) -> float:
        # magnitudes: "does the forecast range pay the theta bill" is the same
        # question for a long and a short book, only the sign of the answer differs
        bg = book_gamma(book, shift_market(mkt, vol_add=x), pair, marks=marks)
        return 0.5 * abs(bg.gamma) * sd * sd - abs(bg.theta) * D

    a, b = float(lo) * bg0.sigma, float(hi) * bg0.sigma
    fa, fb = f(a), f(b)
    x = float("nan")
    if fa > 0 > fb:
        for _ in range(80):
            m = 0.5 * (a + b)
            fm = f(m)
            if fm > 0:
                a = m
            else:
                b = m
            if b - a < tol:
                break
        x = 0.5 * (a + b)
    sigma_star = bg0.sigma + x
    need_sd = (math.sqrt(max(2.0 * abs(bg0.theta) * D / abs(bg0.gamma), 0.0))
               if bg0.gamma else float("nan"))
    carry = 0.5 * bg0.gamma * sd * sd + bg0.theta * D
    return {
        "pair": pair, "crossover_vol": sigma_star, "atm_now": bg0.sigma,
        "ratio": sigma_star / bg0.sigma if bg0.sigma else float("nan"),
        "sigma_window": sw, "sigma_window_pips": sd / spec.pip, "range_basis": basis,
        "breakeven_move_pips": need_sd / spec.pip,
        "required_vs_forecast": need_sd / sd if sd else float("nan"),
        "expected_carry": carry, "calendar_days": D,
        "var_fraction": win.var_fraction, "window_label": win.label,
        "negative_carry": bool(carry < 0),
        "verdict": _carry_verdict(bg0, sigma_star, need_sd, sd, spec, D, win),
        "ccy": spec.quote,
    }


def _carry_verdict(bg: BookGamma, sigma_star: float, need_sd: float, sd: float,
                   spec: Any, D: float, win: PassiveWindow) -> str:
    if bg.gamma == 0 or not np.isfinite(sigma_star):
        return "no gamma in this pair -- nothing to decide"
    long_g = bg.gamma > 0
    need_p, fc_p = need_sd / spec.pip, sd / spec.pip
    head = (f"needs a {need_p:,.0f} pip move over the window to pay "
            f"{abs(bg.theta) * D:,.0f} {spec.quote} of theta; the forecast range is "
            f"{fc_p:,.0f} pips (1 sigma). Crossover ATM {sigma_star * 100:.2f}% vs "
            f"{bg.sigma * 100:.2f}% marked")
    if long_g and need_sd > sd:
        return ("NEGATIVE CARRY overnight: " + head + ". Holding this gamma through the "
                "window loses money in expectation. The ladder is RISK CONTROL, not "
                "monetisation -- leave it because you want the delta capped and the "
                "gamma banked, not because the night pays.")
    if long_g:
        return ("POSITIVE CARRY overnight: " + head + ". Unusual, and worth checking the "
                "vol mark before believing it.")
    return ("SHORT GAMMA: " + head + ". You are being paid the theta; the window's "
            "expected gamma bleed is smaller than it, which is the whole trade. The "
            "risk is the tail, not the mean.")


# --------------------------------------------------------------------------- #
# the ladder
# --------------------------------------------------------------------------- #
def overnight_ladder(book: Book, mkt: MarketSnapshot, pair: str, *,
                     window: PassiveWindow | None = None,
                     rule: HedgeRule | None = None,
                     levels: "pd.DataFrame | None" = None,
                     n_rungs: int = 4, cost_bp: float | None = None,
                     max_overnight_delta: float | None = None,
                     min_clip_base: float = RETAIL_LOT_BASE,
                     method: str = "zakamouline", risk_aversion: float = 1e-6,
                     range_forecast: Any | None = None,
                     profile: SessionProfile | Sequence[float] | None = None,
                     events: "pd.DataFrame | None" = None,
                     band_pips: float | None = None,
                     cost_tier: str = "retail",
                     snap: bool = False, snap_max_pips: float | None = None,
                     snap_inside_pips: float = 1.0,
                     assume_flat_at_close: bool = True,
                     kappa: float = 1.0,
                     persistence: float = 0.0, hurst: float | None = None,
                     up_mult: float = 1.0, down_mult: float = 1.0,
                     report_ccy: str = "USD",
                     marks: Mapping[str, float] | None = None,
                     band: BandResult | None = None) -> list[LadderRung]:
    """The orders to leave tonight.  Everything past ``cost_bp`` is a keyword-only
    addition with a default, so the frozen signature keeps working.

    Spacing, in priority order
    --------------------------
    1. ``band_pips``, if the caller states one.
    2. otherwise :func:`fxgamma.portfolio.bandopt.optimal_band` at ``method``, over
       *this window's* variance and at ``cost_bp`` (default: the **retail** table),
    3. **clamped above** by ``max_overnight_delta / |Gamma|`` -- the delta cap is the
       primary risk input and it wins over the optimiser, and
    4. **floored below** by ``min_clip_base / |Gamma|`` -- a clip you cannot deal is
       not a band.  Defaults to one standard lot (:data:`RETAIL_LOT_BASE`).

    Which constraint actually bound is recorded in ``LadderRung.note`` on the first
    rung and in ``ladder_summary()["spacing_source"]``.

    ``persistence`` / ``hurst`` / ``up_mult`` / ``down_mult``
                         passed straight to
                         :func:`fxgamma.portfolio.bandopt.optimal_band`, so the ladder
                         can be **asymmetric**: rungs above spot are spaced on
                         ``band_spot_up`` and rungs below on ``band_spot_down``, each
                         independently clamped by the delta cap and the clip floor.
                         Defaults are symmetric and Brownian. The state-dependent rule
                         that sets those multipliers from the shape of the move in
                         progress belongs in ``fxgamma/portfolio/ratchet.py``.
    ``kappa``            path-roughness multiplier on the expected number of fills.
                         Defaults to 1.0, the Brownian baseline, and stays there:
                         forecasting roughness from daily bars loses to that null on
                         5 of 5 pairs, so it is exposed as an override, never derived.
                         See :func:`expected_crossings`.
    ``max_overnight_delta``   the most base-ccy delta the user is willing to wake up
                         holding.  Ask for this, never for a risk-aversion
                         coefficient; ``bandopt.risk_aversion_for_band`` converts it
                         when a coefficient is needed downstream.
    ``range_forecast``   anything exposing ``sigma_window`` (the frozen
                         ``signals.rangeforecast.RangeForecast``), or a bare float,
                         interpreted as the **stdev of the log move over the window**.
                         When present it overrides the session-scaled implied *and*
                         the event uplift, since the forecast already contains them.
                         Absent-safe: nothing from ``signals`` is imported.
    ``levels``           optional frame from ``signals.levels.technical_levels`` with
                         columns ``level, kind, strength, age_days, source``.  Absent
                         and duck-typed on purpose -- that module is being built
                         concurrently.
    ``snap``             **off by default** (PM steer, docs/08 s2): snapping rungs to
                         technical levels is an empirical claim and belongs off until
                         ``measure_reversal_stats`` says it beats an unsnapped ladder
                         against its random-level control.  When on, a rung within
                         ``snap_max_pips`` of an anchor moves to ``snap_inside_pips``
                         *inside* it and **its clip is recomputed at the moved level**
                         -- keeping the original clip would leave the cumulative delta
                         wrong at that rung and at every rung beyond it.

    Clip sizing: ``clip_k = |delta(L_k) - delta(L_{k-1})|`` read off a **full
    repricing** of the book (``risk.spot_ladder``), never ``Gamma_1pct x spacing``.
    On a symmetric ATM straddle the linear approximation is only ~2% out at 1W, but a
    skewed book is exactly where a symmetric ladder built off one ``Gamma_1pct`` goes
    wrong, and that is the book this feature is for.
    """
    spec = pair_spec(pair)
    S = float(mkt.spot[pair])
    rule = rule or HedgeRule()
    target = float(rule.target_delta)
    win = window or passive_window(mkt.asof, pair=pair, profile=profile, events=events)
    vf = win.var_fraction
    bg = book_gamma(book, mkt, pair, marks=marks)
    if bg.gamma == 0.0 or bg.n_live == 0:
        return []

    cbp = float(cost_bp) if cost_bp is not None else (
        float(rule.cost_bp) if (rule.cost_bp and cost_bp is None and rule is not None
                                and rule.cost_bp != HedgeRule().cost_bp)
        else cost_bp_for(pair, cost_tier))
    lam = cbp / 2.0 / 1e4
    fq = fx_rate(spec.quote, report_ccy, mkt)

    # ---- window sigma ------------------------------------------------------ #
    sig_ann = bg.sigma
    if range_forecast is not None:
        sigma_window = float(getattr(range_forecast, "sigma_window", range_forecast))
        sig_basis = f"range forecast sigma_window {sigma_window * 100:.3f}%"
    else:
        sigma_window = sig_ann * math.sqrt(max(vf, 1e-12) / TRADING_DAYS)
        sig_basis = (f"ATM {sig_ann * 100:.2f}% x sqrt({vf:.3f}/252) -- session-scaled "
                     "implied, not a forecast")
    sd_spot = S * sigma_window

    # ---- spacing: cap first, optimum second, clip floor third -------------- #
    cap = float(max_overnight_delta) if (max_overnight_delta and max_overnight_delta > 0) \
        else (abs(rule.band_delta) if rule.band_delta else
              abs(rule.band_pct) * (bg.gross_notional or 0.0))
    cap_default = not (max_overnight_delta and max_overnight_delta > 0)
    h_cap = cap / abs(bg.gamma) if cap > 0 else math.inf
    h_floor = float(min_clip_base) / abs(bg.gamma) if min_clip_base > 0 else 0.0
    if band_pips is not None:
        h_opt = float(band_pips) * spec.pip
        mult_up, mult_dn = float(up_mult), float(down_mult)
        src = f"caller-supplied {band_pips:,.1f} pips"
    else:
        band = band or optimal_band(book, mkt, pair, cost_bp=cbp,
                                    risk_aversion=risk_aversion,
                                    horizon_days=max(vf, 1e-6), method=method,
                                    report_ccy=report_ccy, marks=marks,
                                    cost_tier=cost_tier, persistence=persistence,
                                    hurst=hurst, up_mult=up_mult, down_mult=down_mult)
        h_opt = band.band_spot
        mult_up = band.band_spot_up / band.band_spot if band.band_spot else 1.0
        mult_dn = band.band_spot_down / band.band_spot if band.band_spot else 1.0
        src = f"{band.method} optimum {band.band_pips:,.1f} pips"
        if not band.is_symmetric:
            src += (f" (asymmetric: up x{mult_up:.2f} / down x{mult_dn:.2f}"
                    + (f", persistence phi={band.persistence:+.2f}"
                       if band.persistence else "") + ")")
        elif band.persistence:
            src += f" (persistence phi={band.persistence:+.2f}, x{band.persistence_mult:.2f})"
    h = min(h_opt, h_cap)
    which = src if h == h_opt else (
        f"DELTA CAP {cap / 1e6:,.2f}mm ({h_cap / spec.pip:,.1f} pips) over {src}"
        + ("  [cap NOT supplied -- defaulted to HedgeRule.band_pct x gross notional; "
           "set max_overnight_delta to the delta you are actually willing to wake up "
           "holding]" if cap_default else ""))
    if h < h_floor:
        h = h_floor
        which = (f"MIN CLIP {min_clip_base / 1e6:,.2f}mm ({h_floor / spec.pip:,.1f} pips) "
                 f"over {src}" + (" and over the delta cap -- the smallest dealable clip "
                                  "already breaches your delta cap; deal smaller or widen "
                                  "the cap" if h > h_cap else ""))
    if not (h > 0) or not np.isfinite(h):
        return []

    # ---- the book's true delta profile ------------------------------------- #
    span_pct = max(1.6 * n_rungs * h / S * 100.0, 1.0)
    lad = spot_ladder(book, mkt, pair, lo_pct=-span_pct, hi_pct=span_pct,
                      n=int(max(201, 60 * n_rungs + 1)), sticky="strike",
                      report_ccy=report_ccy, marks=marks)
    Sg = lad["spot"].to_numpy(float)
    Dg = lad["delta_base"].to_numpy(float)

    def delta_at(x: float) -> float:
        return float(np.interp(x, Sg, Dg))

    d0 = delta_at(S)
    anchors = _anchor_frame(levels, spec)
    drift = bg.rd - bg.rf
    T_eff = max(vf, 1e-9) / TRADING_DAYS
    long_gamma = bg.gamma > 0

    # asymmetric half-widths: the cap and the clip floor apply to each side
    h_side = {+1: min(max(h * mult_up, h_floor), h_cap if h_cap < math.inf else math.inf),
              -1: min(max(h * mult_dn, h_floor), h_cap if h_cap < math.inf else math.inf)}

    rungs: list[LadderRung] = []
    for sgn in (+1, -1):                            # above spot, then below
        prev_level = S
        # what the book is hedged to at the close: flat at the target if the user
        # squares up before going home, otherwise still carrying today's residual,
        # which the first rung then has to absorb.
        prev_delta = d0 if assume_flat_at_close else target
        cum_hedge = -(d0 - target) if assume_flat_at_close else 0.0
        for k in range(1, int(n_rungs) + 1):
            level = S + sgn * k * h_side[sgn]
            anchor, adist = "", 0.0
            if snap and anchors is not None and len(anchors):
                level, anchor, adist = _snap(
                    level, sgn, anchors, spec,
                    snap_max_pips if snap_max_pips is not None
                    else 0.5 * h_side[sgn] / spec.pip,
                    snap_inside_pips)
            if level <= 0:
                break
            # NB: delta is read AFTER any snap, so the clip belongs to the level the
            # order actually rests at.  Reusing the unsnapped clip would leave the
            # cumulative delta wrong here and at every rung beyond.
            d_here = delta_at(level)
            trade = -(d_here - prev_delta)          # base ccy, + = buy base
            clip = abs(trade)
            cum_hedge += trade
            spacing = abs(level - prev_level)
            x = abs(level - S)
            n_cross = float(expected_crossings(x, sd_spot, max(spacing, 1e-12), kappa))
            # each crossing is half a round trip of size `spacing` on `clip`:
            # this is CONVERSION of mark-to-market into cash, not new P&L
            realised = 0.5 * n_cross * clip * spacing * (1.0 if long_gamma else -1.0)
            cost = n_cross * clip * level * lam
            p_t = touch_probability(S, level, T_eff, sig_ann, drift=drift)
            rungs.append(LadderRung(
                level=float(level), side=int(math.copysign(1, trade)),
                clip_base=float(clip),
                cum_delta_base=float(d_here + cum_hedge),
                pips_from_spot=float((level - S) / spec.pip),
                p_touch=float(p_t), exp_pnl=float(realised - cost),
                anchor=anchor, anchor_dist_pips=float(adist),
                pair=pair, k=int(k),
                order_type="limit" if long_gamma else "stop",
                spacing_pips=float(spacing / spec.pip),
                sigma_dist=float(x / sd_spot) if sd_spot > 0 else float("nan"),
                exp_crossings=n_cross, exp_realised=float(realised),
                exp_cost=float(cost), exp_marginal=float(-cost),
                delta_at_level=float(d_here), ccy=spec.quote, fx_to_report=fq,
                cost_bp=cbp, kappa=float(kappa)))
            prev_level, prev_delta = level, d_here
    rungs.sort(key=lambda r: -r.level)
    if rungs:
        rungs[0] = replace(rungs[0], note=(
            f"spacing: {which} | window sigma {sig_basis} = {sd_spot / spec.pip:,.1f} pips"
            f" | cost {cbp:g}bp ({cost_tier})"))
    return rungs


def _anchor_frame(levels: Any, spec: Any) -> "pd.DataFrame | None":
    """Normalise whatever ``signals.levels`` hands us into level/kind/strength."""
    if levels is None:
        return None
    try:
        df = pd.DataFrame(levels).copy()
    except Exception:                               # noqa: BLE001
        return None
    if "level" not in df.columns or not len(df):
        return None
    df["level"] = pd.to_numeric(df["level"], errors="coerce")
    df = df[np.isfinite(df["level"])]
    if "kind" not in df.columns:
        df["kind"] = "level"
    if "strength" not in df.columns:
        df["strength"] = 1.0
    return df[["level", "kind", "strength"]] if len(df) else None


def _snap(level: float, sgn: int, anchors: "pd.DataFrame", spec: Any,
          max_pips: float, inside_pips: float) -> tuple[float, str, float]:
    """Move a rung just inside a nearby anchor.  Returns (level, anchor, dist_pips)."""
    lv = anchors["level"].to_numpy(float)
    d = np.abs(lv - level) / spec.pip
    i = int(np.argmin(d))
    if d[i] > float(max_pips):
        return level, "", 0.0
    target = float(lv[i]) - sgn * float(inside_pips) * spec.pip
    kind = str(anchors["kind"].iloc[i])
    return target, kind, float((target - float(lv[i])) / spec.pip)


# --------------------------------------------------------------------------- #
# is tonight worth it?
# --------------------------------------------------------------------------- #
def ladder_summary(rungs: Sequence[LadderRung], book: Book, mkt: MarketSnapshot,
                   pair: str, *, window: PassiveWindow | None = None,
                   profile: SessionProfile | Sequence[float] | None = None,
                   events: "pd.DataFrame | None" = None,
                   range_forecast: Any | None = None,
                   report_ccy: str = "USD",
                   marks: Mapping[str, float] | None = None,
                   rule: HedgeRule | None = None,
                   assume_flat_at_close: bool = True,
                   gap_sigmas: float = 3.0) -> dict[str, Any]:
    """Is tonight worth it -- and what does the ladder actually do about it?

    Two questions, deliberately separated, because conflating them is how this feature
    would mislead:

    **A. Should the position be held at all overnight?**  ``carry`` = the window's
    expected gamma P&L minus the window's theta, and ``crossover_vol`` = the implied
    at which those two are equal.  This has **nothing to do with the ladder**: it is
    true whether you leave orders or go flat.  Theta is charged pro-rata on
    **calendar** days -- 14 hours is 0.583 of a day, so 0.583x the daily theta, not a
    whole day's (amendment v1.8's pro-rata ruling, and the same error the PM caught in
    the brief's own worked example) -- while variance arrives on the session clock.
    Those two clocks are why a night that looks free is usually not.

    **B. Given that you are holding it, what do the orders buy?**  Three numbers:
    ``realised_conversion`` (mark-to-market turned into cash you keep even if spot
    round-trips), ``exp_cost``, and ``sd_reduction_pct`` (how much smaller the
    overnight P&L standard deviation is with the ladder than without).  The ladder's
    effect on the **expected** P&L is ``marginal_vs_no_ladder = -exp_cost`` and
    nothing else: under driftless spot every hedge is a fair bet.  ``exp_realised`` is
    conversion, not creation, which is why ``conversion_vs_gamma_pnl`` -- the share of
    the position's whole gamma P&L that a *finite* ladder reaches -- tops out at 1.

    ``gap_scenario`` prices a jump straight to ``gap_sigmas`` window sigmas with the
    ladder filling on the way -- a full repricing through ``risk.spot_ladder``, not a
    quadratic.  For a short-gamma ladder it is the number that matters.
    """
    spec = pair_spec(pair)
    S = float(mkt.spot[pair])
    win = window or passive_window(mkt.asof, pair=pair, profile=profile, events=events)
    vf = win.var_fraction
    bg = book_gamma(book, mkt, pair, marks=marks)
    fq = fx_rate(spec.quote, report_ccy, mkt)
    long_gamma = bg.gamma > 0
    sig_ann = bg.sigma
    if range_forecast is not None:
        sigma_window = float(getattr(range_forecast, "sigma_window", range_forecast))
        range_basis = "range forecast"
    else:
        sigma_window = sig_ann * math.sqrt(max(vf, 1e-12) / TRADING_DAYS)
        range_basis = "session-scaled implied (NOT a forecast)"
    sd_spot = S * sigma_window
    V = sd_spot ** 2

    conv = float(sum(r.exp_realised for r in rungs))
    cost = float(sum(r.exp_cost for r in rungs))
    gamma_pnl = 0.5 * bg.gamma * V                 # the position's, ladder or not
    theta = bg.theta * win.calendar_days            # pro-rata calendar days (W-14)
    carry = gamma_pnl + theta                       # A: hold-or-not
    net = carry - cost                              # A + B, the whole night
    exp_fills = float(sum(r.exp_crossings for r in rungs))
    spacing = float(np.median([r.spacing_pips for r in rungs])) if rungs else float("nan")
    h = spacing * spec.pip if np.isfinite(spacing) else float("nan")

    # variance of the overnight P&L, with and without the ladder.
    # unhedged, delta-flat, long gamma:   P&L = 0.5 G dS^2  ->  var = 0.5 G^2 V^2
    # laddered on spacing h:              residual delta error var = G^2 h^2 V / 6
    sd_no = abs(bg.gamma) * V / math.sqrt(2.0)
    sd_yes = (abs(bg.gamma) * h * math.sqrt(V / 6.0)
              if np.isfinite(h) else float("nan"))
    sd_yes = min(sd_yes, sd_no) if np.isfinite(sd_yes) else sd_no
    sd_cut = 100.0 * (1.0 - sd_yes / sd_no) if sd_no > 0 else float("nan")

    cross = crossover_vol(book, mkt, pair, window=win, range_forecast=range_forecast,
                          profile=profile, events=events, marks=marks,
                          report_ccy=report_ccy)
    # the delta the user squares up at the close, so the gap scenario is the P&L of
    # the position they actually leave rather than of the un-hedged option legs
    target = float((rule or HedgeRule()).target_delta)
    close_hedge = (-(bg.delta_base - target)) if assume_flat_at_close else 0.0
    gap = _gap_scenario(rungs, book, mkt, pair, S, sd_spot, gap_sigmas,
                        report_ccy=report_ccy, marks=marks, close_hedge=close_hedge)

    delta_1sig = abs(bg.gamma) * sd_spot          # delta accumulated at 1 window sigma
    max_delta = max((r.clip_base for r in rungs), default=0.0)
    outer = max((abs(r.pips_from_spot) for r in rungs), default=0.0)
    beyond = 0.0
    if rungs:
        far = max(rungs, key=lambda r: abs(r.pips_from_spot))
        beyond = abs(far.delta_at_level + sum(
            r.side * r.clip_base for r in rungs
            if (far.level > S and S < r.level <= far.level)
            or (far.level < S and far.level <= r.level < S)))

    warnings: list[str] = []
    if not long_gamma:
        warnings.append(
            "SHORT GAMMA. This is not an income ladder. Every order is a STOP: it fills "
            "at or beyond your level, never at it, and in the gap you are actually "
            "worried about it will not fill anywhere near it. The expected gamma term "
            "is NEGATIVE, the theta is the entire edge, and the loss is unbounded "
            "beyond the last rung. Read gap_scenario before leaving these unattended.")
    if cross.get("negative_carry"):
        warnings.append(
            cross["verdict"] + " Leaving the ladder does not change that: its effect on "
            "the expected P&L is minus its cost. The alternative worth pricing is going "
            "flat into the close, or selling the front gamma.")
    if win.spans_weekend:
        warnings.append(
            f"WEEKEND: {win.calendar_days:.2f} calendar days of theta "
            f"({theta:,.0f} {spec.quote}) for {vf:.2f} days of tradeable variance -- "
            f"{win.clock_hours - win.open_hours:.0f} of the {win.clock_hours:.0f} hours "
            "the market is shut. Weekend gap risk is NOT in the sigma above: it arrives "
            "in one jump you cannot hedge through, and no resting order helps.")
    if win.events:
        warnings.append("scheduled events inside the window: " + "; ".join(win.events)
                        + f" -- they add {win.event_var:.2f} days of variance on the "
                        "modelled uplift table, which is a modelled default, not a "
                        "measurement. Prefer a RangeForecast if you have one.")
    thin = sorted({r.k for r in rungs if r.p_touch < 0.02})
    if thin:
        warnings.append(
            f"rung(s) {thin} have a touch probability under 2% on this window's sigma "
            "-- they contribute essentially nothing. The ladder is really "
            f"{len({r.k for r in rungs if r.p_touch >= 0.02})} rungs deep per side.")
    snapped_big = [r.k for r in rungs if r.anchor and rungs
                   and r.clip_base > 1.2 * float(np.median([q.clip_base for q in rungs]))]
    if snapped_big:
        warnings.append(
            f"rung(s) {sorted(set(snapped_big))} were snapped OUTWARD to an anchor and now "
            f"carry a clip more than 20% above the ladder median ({max_delta / 1e6:,.2f}mm "
            "at the largest). Snapping moves the level, and the clip is re-read at the "
            "moved level, so a snap away from spot widens the delta you carry between "
            "fills and can breach max_overnight_delta.")
    if rungs and max_delta > delta_1sig:
        warnings.append(
            f"the ladder barely binds: the clip is {max_delta / 1e6:,.2f}mm but an "
            f"unhedged book only accumulates {delta_1sig / 1e6:,.2f}mm of delta at one "
            f"window sigma ({2 * delta_1sig / 1e6:,.2f}mm at two). The first rung is "
            f"{min(r.sigma_dist for r in rungs):,.1f} sigmas out and the P&L standard "
            f"deviation falls "
            f"by {sd_cut:,.0f}%. If the point is risk control, set max_overnight_delta "
            "to the delta you actually mind waking up with -- that is the input worth "
            "thousands; the analytic band inside it is worth tens.")
    if win.profile_source != "estimated":
        warnings.append(
            f"session variance profile is a MODELLED DEFAULT ({win.profile_source}); it "
            "has not been fitted to hourly data. Every distance, probability and "
            "crossing count here scales with it.")
    if rungs:
        warnings.append(
            f"cost assumption {rungs[0].cost_bp:g}bp round trip "
            f"({rungs[0].cost_bp / 2e4 * S / spec.pip:.2f} pips one way). This is a "
            "RETAIL default, not the interbank zones.COST_BP table. Run "
            "ladder_cost_sensitivity() -- the band goes as cost^(1/3) and the cost line "
            "is linear in it.")

    out: dict[str, Any] = {
        "pair": pair, "spot": S, "asof": mkt.asof,
        "gamma_side": "long" if long_gamma else "short",
        "gamma_1pct": bg.gamma_1pct, "gamma": bg.gamma,
        "n_rungs": len(rungs), "spacing_pips": spacing,
        "spacing_source": rungs[0].note if rungs else "",
        "max_clip_base": max_delta, "outer_rung_pips": outer,
        "delta_beyond_last_rung": beyond,
        "delta_at_close": bg.delta_base,
        "close_hedge_base": close_hedge,
        "delta_1sigma_unhedged": delta_1sig,
        "delta_2sigma_unhedged": 2.0 * delta_1sig,
        # --- the window ---
        "window_label": win.label, "window_start": win.start, "window_end": win.end,
        "clock_hours": win.clock_hours, "open_hours": win.open_hours,
        "calendar_days": win.calendar_days,
        "var_fraction": vf, "clock_fraction": win.clock_fraction,
        "theta_per_var_day": (win.clock_fraction / vf) if vf else float("nan"),
        "event_var_fraction": win.event_var, "events": list(win.events),
        "profile_source": win.profile_source,
        "sigma_ann": sig_ann, "sigma_window": sigma_window,
        "sigma_window_pips": sd_spot / spec.pip, "range_basis": range_basis,
        # --- A: hold or not (nothing to do with the ladder) ---
        "gamma_pnl": gamma_pnl, "theta": theta, "carry": carry,
        "crossover_vol": cross["crossover_vol"], "atm_now": sig_ann,
        "crossover_ratio": cross["ratio"],
        "breakeven_move_pips": cross["breakeven_move_pips"],
        "negative_carry": cross["negative_carry"], "carry_verdict": cross["verdict"],
        # --- B: what the orders buy ---
        "realised_conversion": conv, "exp_cost": cost,
        "marginal_vs_no_ladder": -cost,
        "conversion_vs_gamma_pnl": conv / gamma_pnl if gamma_pnl else float("nan"),
        "sd_overnight_no_ladder": sd_no, "sd_overnight_with_ladder": sd_yes,
        "sd_reduction_pct": sd_cut,
        "kappa": float(rungs[0].kappa) if rungs else 1.0,
        "fills_basis": ("expected fills assume a BROWNIAN path (kappa = "
                        f"{(rungs[0].kappa if rungs else 1.0):g}); path roughness is an "
                        "override, not a forecast -- see expected_crossings()"),
        "exp_fills": exp_fills, "p_touch_first": rungs[0].p_touch if rungs else 0.0,
        "p_touch_any": max((r.p_touch for r in rungs), default=0.0),
        # --- the whole night ---
        "net": net, "gap_scenario": gap,
        "verdict": _verdict(long_gamma, conv, cost, theta, gamma_pnl, carry, net,
                            sd_cut, spec.quote, cross),
        "warnings": warnings,
        "ccy": spec.quote, "report_ccy": report_ccy.upper(), "fx_to_report": fq,
        "basis": ("variance on sqrt(252) trading days scaled by the session profile; "
                  "theta pro-rata on ACT/365 calendar days (amendment v1.4 W-7, v1.8 "
                  "pro-rata theta). The two clocks are different on purpose."),
    }
    for k in ("gamma_pnl", "theta", "carry", "realised_conversion", "exp_cost",
              "marginal_vs_no_ladder", "net", "sd_overnight_no_ladder",
              "sd_overnight_with_ladder"):
        out[f"{k}_rep"] = out[k] * fq
    return out


def _verdict(long_gamma: bool, conv: float, cost: float, theta: float,
             gamma_pnl: float, carry: float, net: float, sd_cut: float,
             ccy: str, cross: Mapping[str, Any]) -> str:
    hold = ("HOLDING IT IS NEGATIVE CARRY" if cross.get("negative_carry")
            else "holding it is positive carry")
    a = (f"{hold}: expected gamma {gamma_pnl:,.0f} {ccy} vs theta {theta:,.0f} "
         f"= {carry:+,.0f}; crossover ATM {cross['crossover_vol'] * 100:.2f}% vs "
         f"{cross['atm_now'] * 100:.2f}% marked.")
    if not long_gamma:
        return (a + f" SHORT GAMMA -- the ladder is a stop ladder that bounds the loss; "
                f"it costs {cost:,.0f} {ccy} and cuts the overnight P&L sd by "
                f"{sd_cut:,.0f}%. Size it off the tail, not the mean.")
    b = (f" THE LADDER banks {conv:,.0f} {ccy} of that as cash for {cost:,.0f} of cost "
         f"and cuts the overnight P&L standard deviation by {sd_cut:,.0f}%. Its effect "
         f"on the expected P&L is exactly -{cost:,.0f}; everything else it does is "
         "conversion and risk control.")
    return a + b


def _gap_scenario(rungs: Sequence[LadderRung], book: Book, mkt: MarketSnapshot,
                  pair: str, S: float, sd_spot: float, k: float, *,
                  report_ccy: str, marks: Mapping[str, float] | None,
                  close_hedge: float = 0.0) -> dict[str, Any]:
    """P&L if spot jumps ``k`` window sigmas, with the ladder filling on the way.

    Option P&L is a full repricing (``risk.spot_ladder``), not ``0.5 G dS^2``: at three
    sigmas the quadratic is meaningfully wrong, and for a short-gamma book it is wrong
    in the flattering direction.
    """
    if sd_spot <= 0:
        return {}
    out: dict[str, Any] = {"sigmas": float(k)}
    for name, sgn in (("up", +1.0), ("down", -1.0)):
        Sx = S + sgn * k * sd_spot
        if Sx <= 0:
            continue
        pct = (Sx / S - 1.0) * 100.0
        lad = spot_ladder(book, mkt, pair, lo_pct=min(pct, 0.0) - 0.1,
                          hi_pct=max(pct, 0.0) + 0.1, n=201, sticky="strike",
                          report_ccy=report_ccy, marks=marks)
        opt = float(np.interp(Sx, lad["spot"].to_numpy(float),
                              lad["pnl"].to_numpy(float)))
        # hedges that fill on a monotone move to Sx
        hedge = close_hedge * (Sx - S)
        for r in rungs:
            if (sgn > 0 and r.level <= Sx and r.level > S) or \
               (sgn < 0 and r.level >= Sx and r.level < S):
                hedge += r.side * r.clip_base * (Sx - r.level)
        out[name] = {"spot": Sx, "pct": pct,
                     "pips": (Sx - S) / pair_spec(pair).pip,
                     "option_pnl": opt, "hedge_pnl": hedge, "total": opt + hedge,
                     "rungs_filled": sum(1 for r in rungs
                                         if (sgn > 0 and S < r.level <= Sx)
                                         or (sgn < 0 and Sx <= r.level < S))}
    out["note"] = ("full repricing of the option legs at the shocked spot (sticky-strike) "
                   "plus the mark-to-market of every rung that fills on a monotone move "
                   "there. It does NOT include slippage through a gap, which is the whole "
                   "risk for a stop-style short-gamma ladder.")
    return out


# --------------------------------------------------------------------------- #
# tables and printable output
# --------------------------------------------------------------------------- #
def ladder_frame(rungs: Sequence[LadderRung]) -> pd.DataFrame:
    """The ladder as a table for the UI / a CSV of resting orders."""
    if not rungs:
        return pd.DataFrame(columns=list(LadderRung(0, 0, 0, 0, 0, 0, 0).as_dict())
                            + ["action"])
    df = pd.DataFrame([r.as_dict() for r in rungs])
    df["action"] = np.where(df["side"] > 0, "BUY", "SELL")
    return df


def ladder_cost_sensitivity(book: Book, mkt: MarketSnapshot, pair: str, *,
                            cost_bps: Sequence[float] = (0.2, 1.0, 2.5, 5.0, 10.0, 20.0),
                            **kw: Any) -> pd.DataFrame:
    """How much of the answer the cost assumption owns.

    ``zones.COST_BP`` is an interbank table and this user has no OTC access, so the
    honest range spans two orders of magnitude.  The band scales as ``cost^(1/3)`` --
    slowly -- but the cost line is **linear** in it, so what actually moves is whether
    the ladder's cost eats the conversion, not where the rungs go.  Run this before
    quoting any single net number.
    """
    rows: list[dict[str, Any]] = []
    for c in cost_bps:
        rungs = overnight_ladder(book, mkt, pair, cost_bp=float(c), **kw)
        s = ladder_summary(rungs, book, mkt, pair,
                           window=kw.get("window"), profile=kw.get("profile"),
                           events=kw.get("events"),
                           range_forecast=kw.get("range_forecast"),
                           report_ccy=kw.get("report_ccy", "USD"),
                           marks=kw.get("marks"))
        rows.append({"cost_bp": float(c), "cost_pips_round_trip":
                     float(c) / 1e4 * s["spot"] / pair_spec(pair).pip,
                     "spacing_pips": s["spacing_pips"], "n_rungs": s["n_rungs"],
                     "realised_conversion": s["realised_conversion"],
                     "exp_cost": s["exp_cost"], "net": s["net"],
                     "sd_reduction_pct": s["sd_reduction_pct"],
                     "spacing_source": s["spacing_source"][:60]})
    return pd.DataFrame(rows)


def format_ladder(rungs: Sequence[LadderRung], summary: Mapping[str, Any]) -> str:
    """The thing the user reads at 17:00, verbatim-printable.

    Leads with the carry decision, because that is the decision.  The orders come
    second, framed as what they are: conversion and a delta cap.
    """
    if not rungs:
        return ("no ladder: " + ("the book has no gamma in this pair"
                                 if summary.get("gamma", 0) == 0 else "no rungs produced"))
    spec = pair_spec(summary["pair"])
    dp = 4 if spec.pip < 1e-3 else 2
    side = {1: "BUY ", -1: "SELL"}
    ot = rungs[0].order_type.upper()
    ccy = summary["ccy"]
    L = [
        f"{summary['pair']}  spot {summary['spot']:,.{dp}f}  "
        f"{summary['gamma_side'].upper()} GAMMA {summary['gamma_1pct'] / 1e6:,.2f}mm per 1%",
        f"{summary['window_label']}: {summary['clock_hours']:.0f}h clock "
        f"({summary['clock_fraction']:.0%} of a day's THETA) but "
        f"{summary['var_fraction']:.2f} days of VARIANCE "
        f"-- you pay {summary['theta_per_var_day']:.2f}x the theta per day of "
        f"variance -- 1 sigma "
        f"{summary['sigma_window_pips']:,.0f} pips, breakeven "
        f"{summary['breakeven_move_pips']:,.0f} pips",
        f"CARRY  gamma {summary['gamma_pnl']:+,.0f} + theta {summary['theta']:+,.0f} "
        f"= {summary['carry']:+,.0f} {ccy}   |   crossover ATM "
        f"{summary['crossover_vol'] * 100:.2f}% vs {summary['atm_now'] * 100:.2f}% marked",
        f"E[fills] assume a BROWNIAN path (kappa {summary['kappa']:g}) -- roughness is "
        "an input here, not a forecast",
        f"{ot} orders, spacing {summary['spacing_pips']:,.0f} pips "
        f"[{summary['spacing_source'].split('|')[0].strip()}]:",
    ]
    for r in rungs:
        a = (f"  [{r.anchor} {r.anchor_dist_pips:+.0f}p]" if r.anchor else "")
        L.append(f"  {side[r.side]} {r.clip_base / 1e6:6.2f}mm {spec.base} at "
                 f"{r.level:,.{dp}f}  ({r.pips_from_spot:+6.0f}p, "
                 f"{r.sigma_dist:.2f}sig, touch {r.p_touch:4.0%}, "
                 f"E[fills] {r.exp_crossings:4.2f}, banks {r.exp_pnl:+7,.0f} {ccy})" + a)
    L.append(f"  LADDER banks {summary['realised_conversion']:,.0f} of the "
             f"{summary['gamma_pnl']:,.0f} gamma P&L as cash, costs "
             f"{summary['exp_cost']:,.0f}, cuts overnight P&L sd by "
             f"{summary['sd_reduction_pct']:,.0f}%. Effect on EXPECTED P&L: "
             f"{summary['marginal_vs_no_ladder']:+,.0f} {ccy} (= minus the cost).")
    L.append(f"  NIGHT  {summary['net']:+,.0f} {ccy} expected; "
             f"max delta between fills {summary['max_clip_base'] / 1e6:,.2f}mm; "
             f"delta beyond the last rung "
             f"{summary['delta_beyond_last_rung'] / 1e6:,.2f}mm")
    L.append(f"  {summary['verdict']}")
    for w in summary["warnings"]:
        L.append(f"  ! {w}")
    return "\n".join(L)
