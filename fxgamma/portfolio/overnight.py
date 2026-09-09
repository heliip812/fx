"""The overnight resting-order ladder: what to leave in the market before going home.

``overnight_ladder(book, mkt, pair, ...) -> list[LadderRung]`` and
``ladder_summary(...) -> dict`` (docs/08_overnight_gamma.md s3).

The user trades London by hand and is asleep the rest of the time.  This module turns
the book's gamma into a set of resting orders that keep monetising it overnight, and
into an honest arithmetic of whether tonight is worth doing at all.

Three things it gets right that a naive ladder gets wrong
--------------------------------------------------------

**1. Session variance time, not clock time.**  London close 17:00 to London open 07:00
is 14 of 24 hours, but it is *not* 14/24 = 58% of a day's variance.  Asia is quiet,
the Tokyo fix and the London open are not, and the two hours around the New York
close are the deadest of the twenty-four.  On the default EURUSD profile the overnight
window carries **~34%** of a day's variance -- a factor of 1.7 in variance and 1.31 in
sigma against the clock-time answer.  Every rung distance and every touch probability
is proportional to that sigma, so getting it wrong misprices the whole ladder.
See :data:`DEFAULT_HOUR_PROFILES` and :func:`estimate_hour_profile`.

**2. The number of times a rung actually pays is a local-time question.**  For a grid
of spacing ``h``, the expected number of crossings of the level ``x`` away from spot
over a window whose spot standard deviation is ``s`` is ``E[L(x)] / h`` where
``E[L(x)] = 2 s (phi(u) - u (1 - Phi(u)))``, ``u = |x| / s``, is the expected Brownian
local time at that level (Tanaka's formula).  Summing over the grid reproduces
``E[total crossings] = V / h^2`` and hence ``E[capture] = Gamma V / 2``, the
continuous-hedging gamma P&L.  This is not a hand-wave and it is not a "probability of
touch": a rung that is touched once pays half a round trip; the same rung in a choppy
night can pay six times.  :func:`expected_crossings`.

**3. The band comes from the optimiser, not a rule of thumb.**  Spacing is
:func:`fxgamma.portfolio.bandopt.optimal_band` evaluated over *this window's*
variance.  Read ``docs/09_hedging_theory.md`` s2 before reading the P&L numbers: the
*expected* gamma capture does not depend on the spacing.  Spacing buys you lower cost
at the price of higher variance, and it decides how much of the theoretical
``Gamma V / 2`` a *finite* ladder actually reaches.

Short gamma
-----------
The ladder inverts and the orders become **stops**, not limits.  A short-gamma ladder
left unattended is a materially different and more dangerous object than a long-gamma
one -- it has unbounded loss, it sells lows and buys highs by construction, and the
stops that are supposed to protect it are exactly the orders that will not fill at
your price in the gap you are worried about.  :func:`ladder_summary` refuses to frame
it as an income strategy and returns ``gamma_side="short"`` with the warnings up
front.  Nothing here stops you leaving one; it will not pretend it is the same trade.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..conventions import PAIRS, pair_spec
from ..models import gk
from ..types import Book, HedgeRule, MarketSnapshot
from .bandopt import BandResult, BookGamma, book_gamma, optimal_band
from .risk import fx_rate, price_book, spot_ladder
from .zones import COST_BP, TRADING_DAYS, touch_probability

__all__ = [
    "LadderRung", "PassiveWindow", "SessionProfile",
    "DEFAULT_HOUR_PROFILES", "MARKET_CLOSE_UTC_H", "MARKET_OPEN_UTC_H",
    "passive_window", "session_variance_weight", "hour_profile",
    "estimate_hour_profile", "expected_crossings", "expected_local_time",
    "overnight_ladder", "ladder_summary", "format_ladder",
]

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
    ``exp_pnl``         **net** expected contribution: ``exp_capture - exp_cost``.
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
    exp_capture: float = 0.0         # gross gamma capture from this rung, quote ccy
    exp_cost: float = 0.0            # expected transaction cost, quote ccy
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
                 "p_touch", "exp_crossings", "exp_capture", "exp_cost", "exp_pnl",
                 "anchor", "anchor_dist_pips", "delta_at_level", "ccy", "note")}


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


def _norm24(w: Sequence[float]) -> tuple[float, ...]:
    a = np.asarray(w, dtype=float)
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
        return SessionProfile(pair.upper(), _norm24(profile), source="user",
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
        pair.upper(), _norm24(w), source="estimated", n_obs=int(r2.size),
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
                            profile: SessionProfile | Sequence[float] | None = None
                            ) -> float:
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
    """
    prof = hour_profile(pair, profile)
    w = prof.array()
    s = start.astimezone(UTC) if start.tzinfo else start.replace(tzinfo=UTC)
    e = end.astimezone(UTC) if end.tzinfo else end.replace(tzinfo=UTC)
    if e <= s:
        return 0.0
    total = 0.0
    t = s
    guard = 0
    while t < e and guard < 24 * 400:
        guard += 1
        nxt = (t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        step = min(nxt, e)
        frac = (step - t).total_seconds() / 3600.0
        total += w[t.hour] * frac * _liquidity_factor(t)
        t = step
    return float(total / 24.0)


def passive_window(asof: datetime, tz: str = "Europe/London", close_h: int = 17,
                   open_h: int = 7, *, pair: str = "",
                   profile: SessionProfile | Sequence[float] | None = None,
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
    vf = session_variance_weight(start, end, pair, profile)
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
        note=(f"{clock:.0f} clock hours ({clock / 24.0 * 100:.0f}% of the clock) but "
              f"{vf * 100:.0f}% of a day's variance on the {prof.source} "
              f"{prof.pair or 'generic'} hour profile"
              + (f"; {clock - openh:.0f} of those hours the market is shut"
                 if clock - openh > 0.5 else "")))


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


def expected_crossings(x: float | np.ndarray, sd: float, h: float) -> float | np.ndarray:
    """Expected number of times a level ``x`` from spot is crossed in ``h``-sized steps.

    ``E[N(x)] = E[L_T(x)] / h``.  This is the number of times a resting order at that
    level actually **fills** over the window, given that after each fill the position
    is only re-established once spot has travelled ``h`` back the other way -- which is
    precisely what a ladder of spacing ``h`` does.

    It is not a probability and it is not bounded by 1: the nearest rung of a tight
    ladder on a choppy night fills several times, and that is where the money is.
    ``p_touch`` says whether you get filled once; this says how often.
    """
    return expected_local_time(x, sd) / float(h)


# --------------------------------------------------------------------------- #
# the ladder
# --------------------------------------------------------------------------- #
def overnight_ladder(book: Book, mkt: MarketSnapshot, pair: str, *,
                     window: PassiveWindow | None = None,
                     rule: HedgeRule | None = None,
                     levels: "pd.DataFrame | None" = None,
                     n_rungs: int = 4, cost_bp: float | None = None,
                     method: str = "zakamouline", risk_aversion: float = 1e-6,
                     range_forecast: Any | None = None,
                     profile: SessionProfile | Sequence[float] | None = None,
                     band_pips: float | None = None,
                     snap: bool = False, snap_max_pips: float | None = None,
                     snap_inside_pips: float = 1.0,
                     min_clip_base: float = 0.0,
                     assume_flat_at_close: bool = True,
                     report_ccy: str = "USD",
                     marks: Mapping[str, float] | None = None,
                     band: BandResult | None = None) -> list[LadderRung]:
    """The orders to leave tonight.

    Parameters that are not in the frozen signature are keyword-only additions with
    defaults, so the contract signature keeps working.

    ``range_forecast``   anything exposing ``sigma_window`` (the frozen
                         ``signals.rangeforecast.RangeForecast``), or a bare float,
                         interpreted as the **stdev of the log move over the window**.
                         When absent the window sigma is the book's gamma-weighted ATM
                         implied scaled by the session variance weight -- which is the
                         honest fallback, not a forecast.  Absent-safe: nothing from
                         ``signals`` is imported.
    ``levels``           optional frame from ``signals.levels.technical_levels`` with
                         columns ``level, kind, strength, age_days, source``.  Absent
                         and duck-typed on purpose -- that module is being built
                         concurrently.
    ``snap``             **off by default** (PM steer, docs/08 s2): snapping rungs to
                         technical levels is an empirical claim and belongs off until
                         ``measure_reversal_stats`` says it beats an unsnapped ladder
                         against its random-level control.  When on, a rung within
                         ``snap_max_pips`` of an anchor moves to ``snap_inside_pips``
                         *inside* it (you want to be filled before the level, not at
                         it), and records ``anchor`` / ``anchor_dist_pips``.
    ``assume_flat_at_close``  the user hedges to the rule's target before going home,
                         so the first clip is the delta *increment* from spot to the
                         first rung.  Set False to fold today's residual delta into
                         the first rung instead; ``ladder_summary`` reports it either
                         way.

    Clip sizing: ``clip_k = |delta(L_k) - delta(L_{k-1})|`` read off a full
    repricing of the book (``risk.spot_ladder``), not from a constant gamma.  On a
    book with strikes inside the ladder that matters: the linear-gamma answer
    over-hedges the first rung and under-hedges the outer ones.
    """
    spec = pair_spec(pair)
    S = float(mkt.spot[pair])
    rule = rule or HedgeRule()
    target = float(rule.target_delta)
    win = window or passive_window(mkt.asof, pair=pair, profile=profile)
    vf = win.var_fraction if window is None else session_variance_weight(
        win.start, win.end, pair, profile)
    bg = book_gamma(book, mkt, pair, marks=marks)
    if bg.gamma == 0.0 or bg.n_live == 0:
        return []

    cbp = float(cost_bp) if cost_bp is not None else (
        float(rule.cost_bp) if rule.cost_bp else COST_BP.get(pair, 0.5))
    lam = cbp / 2.0 / 1e4
    fq = fx_rate(spec.quote, report_ccy, mkt)

    # ---- window sigma ------------------------------------------------------ #
    sig_ann = bg.sigma
    if range_forecast is not None:
        sw = getattr(range_forecast, "sigma_window", range_forecast)
        sigma_window = float(sw)
        sig_basis = "range forecast"
    else:
        sigma_window = sig_ann * math.sqrt(max(vf, 1e-12) / TRADING_DAYS)
        sig_basis = f"ATM {sig_ann * 100:.2f}% x sqrt({vf:.3f}/252)"
    sd_spot = S * sigma_window

    # ---- spacing ----------------------------------------------------------- #
    if band_pips is not None:
        h = float(band_pips) * spec.pip
        band_note = f"caller-supplied spacing {band_pips:,.1f} pips"
    else:
        band = band or optimal_band(book, mkt, pair, cost_bp=cbp,
                                    risk_aversion=risk_aversion,
                                    horizon_days=max(vf, 1e-6), method=method,
                                    report_ccy=report_ccy, marks=marks)
        h = band.band_spot
        band_note = f"{band.method} band {band.band_pips:,.1f} pips"
    if not (h > 0) or not np.isfinite(h):
        return []

    # ---- the book's true delta profile ------------------------------------- #
    span_pct = max(1.5 * n_rungs * h / S * 100.0, 1.0)
    lad = spot_ladder(book, mkt, pair, lo_pct=-span_pct, hi_pct=span_pct,
                      n=int(max(201, 40 * n_rungs + 1)), sticky="strike",
                      report_ccy=report_ccy, marks=marks)
    Sg = lad["spot"].to_numpy(float)
    Dg = lad["delta_base"].to_numpy(float)

    def delta_at(x: float) -> float:
        return float(np.interp(x, Sg, Dg))

    d0 = delta_at(S)
    residual = d0 - target if not assume_flat_at_close else 0.0
    anchors = _anchor_frame(levels, spec)
    drift = bg.rd - bg.rf
    T_eff = max(vf, 1e-9) / TRADING_DAYS
    long_gamma = bg.gamma > 0

    rungs: list[LadderRung] = []
    for sgn in (+1, -1):                            # above spot, then below
        prev_level = S
        prev_delta = d0 - residual                  # what we are hedged to at the close
        cum_hedge = -residual if not assume_flat_at_close else 0.0
        for k in range(1, int(n_rungs) + 1):
            level = S + sgn * k * h
            anchor, adist = "", 0.0
            if snap and anchors is not None and len(anchors):
                level, anchor, adist = _snap(level, sgn, anchors, spec,
                                             snap_max_pips if snap_max_pips is not None
                                             else 0.5 * h / spec.pip,
                                             snap_inside_pips)
            if level <= 0:
                break
            d_here = delta_at(level)
            trade = -(d_here - prev_delta)          # base ccy, + = buy base
            clip = abs(trade)
            if clip < float(min_clip_base):
                prev_level, prev_delta = level, d_here
                continue
            cum_hedge += trade
            spacing = abs(level - prev_level)
            x = abs(level - S)
            n_cross = float(expected_crossings(x, sd_spot, max(spacing, 1e-12)))
            # each crossing is half a round trip of size `spacing` on `clip`
            cap = 0.5 * n_cross * clip * spacing * (1.0 if long_gamma else -1.0)
            cost = n_cross * clip * level * lam
            p_t = touch_probability(S, level, T_eff, sig_ann, drift=drift)
            rungs.append(LadderRung(
                level=float(level), side=int(math.copysign(1, trade)),
                clip_base=float(clip),
                cum_delta_base=float(d_here + cum_hedge),
                pips_from_spot=float((level - S) / spec.pip),
                p_touch=float(p_t), exp_pnl=float(cap - cost),
                anchor=anchor, anchor_dist_pips=float(adist),
                pair=pair, k=int(k),
                order_type="limit" if long_gamma else "stop",
                spacing_pips=float(spacing / spec.pip),
                sigma_dist=float(x / sd_spot) if sd_spot > 0 else float("nan"),
                exp_crossings=n_cross, exp_capture=float(cap), exp_cost=float(cost),
                delta_at_level=float(d_here), ccy=spec.quote, fx_to_report=fq,
                note=band_note if k == 1 and sgn > 0 else ""))
            prev_level, prev_delta = level, d_here
    rungs.sort(key=lambda r: -r.level)
    if rungs and sig_basis:
        rungs[0] = replace(rungs[0], note=(rungs[0].note + f" | window sigma {sig_basis} "
                                           f"= {sd_spot / spec.pip:,.1f} pips").strip(" |"))
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
                   report_ccy: str = "USD",
                   marks: Mapping[str, float] | None = None,
                   gap_sigmas: float = 3.0) -> dict[str, Any]:
    """Capture, cost, theta and net for the window -- the go / no-go number.

    Theta is charged on **calendar** days and capture is earned on **variance** days.
    Those are different clocks and over a weekend they diverge violently: Friday to
    Monday is 3.0 calendar days of theta against ~0.35 days of variance, so the same
    ladder that is worth leaving on a Tuesday night is a 9:1 loser on a Friday.  The
    summary prints both so the trader can see it rather than being told a single
    ``net``.

    ``capture_vs_theory`` is the fraction of the continuous-hedging maximum
    ``Gamma V / 2`` that a *finite* ladder of this many rungs actually reaches.  It is
    the honest answer to "should I add rungs": when it is already 0.9, more rungs buy
    nothing.

    ``gap_scenario`` prices a jump straight to ``gap_sigmas`` window sigmas with the
    ladder filling on the way -- a full repricing through ``risk.spot_ladder``, not a
    quadratic.  For a short-gamma ladder that is the number that matters and it is
    reported first.
    """
    spec = pair_spec(pair)
    S = float(mkt.spot[pair])
    win = window or passive_window(mkt.asof, pair=pair, profile=profile)
    vf = win.var_fraction
    bg = book_gamma(book, mkt, pair, marks=marks)
    fq = fx_rate(spec.quote, report_ccy, mkt)
    long_gamma = bg.gamma > 0
    sig_ann = bg.sigma
    sigma_window = sig_ann * math.sqrt(max(vf, 1e-12) / TRADING_DAYS)
    sd_spot = S * sigma_window
    V = sd_spot ** 2

    cap = float(sum(r.exp_capture for r in rungs))
    cost = float(sum(r.exp_cost for r in rungs))
    theory = 0.5 * bg.gamma * V
    theta = bg.theta * win.calendar_days
    net = cap - cost + theta
    exp_fills = float(sum(r.exp_crossings for r in rungs))
    p_any = max((r.p_touch for r in rungs), default=0.0)
    spacing = float(np.median([r.spacing_pips for r in rungs])) if rungs else float("nan")

    gap = _gap_scenario(rungs, book, mkt, pair, S, sd_spot, gap_sigmas,
                        report_ccy=report_ccy, marks=marks)

    warnings: list[str] = []
    if not long_gamma:
        warnings.append(
            "SHORT GAMMA. This is not an income ladder. Every order is a STOP, so it "
            "fills at or beyond your level, not at it; the expected gamma contribution "
            "is NEGATIVE and the ladder's job is to bound a loss, not to earn. The "
            "theta you are collecting is the whole of the edge and the tail is "
            "unbounded. Leaving stops unattended in a gap is how the loss becomes much "
            "larger than the ladder implies -- see gap_scenario.")
    if win.spans_weekend:
        warnings.append(
            f"WEEKEND: {win.calendar_days:.1f} calendar days of theta "
            f"({theta:,.0f} {spec.quote}) against {vf:.2f} days of tradeable variance. "
            "Weekend gap risk is NOT in the sigma above -- it arrives in one jump you "
            "cannot hedge through.")
    if rungs:
        thin = [r.k for r in rungs if r.p_touch < 0.02]
        if thin:
            warnings.append(
                f"rung(s) {sorted(set(thin))} have a touch probability under 2% on this "
                "window's sigma -- they contribute essentially nothing. Either drop them "
                "or accept that the ladder is really 1-2 rungs deep.")
    if long_gamma and net < 0:
        warnings.append("Expected capture does not cover tonight's theta. The ladder is "
                        "still the right ladder; the position is simply paying to be long "
                        "gamma over a quiet window.")
    if win.profile_source != "estimated":
        warnings.append(f"session variance profile is a MODELLED DEFAULT "
                        f"({win.profile_source}); it has not been fitted to data. "
                        "Every distance and probability here scales with it.")

    verdict = _verdict(long_gamma, cap, cost, theta, net, spec.quote)
    out = {
        "pair": pair, "spot": S, "asof": mkt.asof,
        "gamma_side": "long" if long_gamma else "short",
        "gamma_1pct": bg.gamma_1pct, "gamma": bg.gamma,
        "n_rungs": len(rungs), "spacing_pips": spacing,
        "window_label": win.label, "window_start": win.start, "window_end": win.end,
        "clock_hours": win.clock_hours, "open_hours": win.open_hours,
        "calendar_days": win.calendar_days,
        "var_fraction": vf, "clock_fraction": win.clock_fraction,
        "var_vs_clock": vf / win.clock_fraction if win.clock_fraction else float("nan"),
        "profile_source": win.profile_source,
        "sigma_ann": sig_ann, "sigma_window": sigma_window,
        "sigma_window_pips": sd_spot / spec.pip,
        "exp_capture": cap, "exp_cost": cost, "exp_capture_net": cap - cost,
        "theta": theta, "net": net,
        "theory_max_capture": theory,
        "capture_vs_theory": cap / theory if theory else float("nan"),
        "exp_fills": exp_fills, "p_touch_first": rungs[0].p_touch if rungs else 0.0,
        "p_touch_any": p_any,
        "breakeven_move_pips": _breakeven_move(bg, win, spec),
        "gap_scenario": gap,
        "residual_delta_hedged_at_close": True,
        "verdict": verdict, "warnings": warnings,
        "ccy": spec.quote, "report_ccy": report_ccy.upper(), "fx_to_report": fq,
        "basis": ("variance on sqrt(252) trading days scaled by the session profile; "
                  "theta on ACT/365 calendar days (amendment v1.4 W-7)"),
    }
    for k in ("exp_capture", "exp_cost", "exp_capture_net", "theta", "net",
              "theory_max_capture"):
        out[f"{k}_rep"] = out[k] * fq
    return out


def _breakeven_move(bg: BookGamma, win: PassiveWindow, spec: Any) -> float:
    """The one-way move that makes gamma pay the window's theta, in pips."""
    if bg.gamma == 0:
        return float("nan")
    th = abs(bg.theta * win.calendar_days)
    x2 = 2.0 * th / abs(bg.gamma)
    return math.sqrt(max(x2, 0.0)) / spec.pip


def _gap_scenario(rungs: Sequence[LadderRung], book: Book, mkt: MarketSnapshot,
                  pair: str, S: float, sd_spot: float, k: float, *,
                  report_ccy: str, marks: Mapping[str, float] | None) -> dict[str, Any]:
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
        hedge = 0.0
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


def _verdict(long_gamma: bool, cap: float, cost: float, theta: float, net: float,
             ccy: str) -> str:
    if long_gamma:
        if net > 0:
            return (f"WORTH LEAVING: expected capture {cap:,.0f} {ccy} less {cost:,.0f} "
                    f"of cost covers {abs(theta):,.0f} of theta with {net:,.0f} to spare.")
        if cap - cost > 0.5 * abs(theta):
            return (f"MARGINAL: capture net of cost {cap - cost:,.0f} {ccy} recovers "
                    f"{100 * (cap - cost) / max(abs(theta), 1e-9):.0f}% of the "
                    f"{abs(theta):,.0f} theta. Leave the orders -- they are free money "
                    "against a bill you are paying anyway -- but do not expect a profit.")
        return (f"THIN: capture net of cost {cap - cost:,.0f} {ccy} against "
                f"{abs(theta):,.0f} of theta. The orders still cost nothing to leave; "
                "the position, not the ladder, is the problem.")
    return (f"SHORT GAMMA: theta of {theta:,.0f} {ccy} against an expected gamma bleed "
            f"of {abs(cap):,.0f} and {cost:,.0f} of cost -- net {net:,.0f}. Read "
            "gap_scenario before you leave stops unattended; the expectation is not the "
            "risk.")


# --------------------------------------------------------------------------- #
# printable
# --------------------------------------------------------------------------- #
def format_ladder(rungs: Sequence[LadderRung], summary: Mapping[str, Any]) -> str:
    """The thing the user reads at 17:00, verbatim-printable."""
    if not rungs:
        return "no ladder: the book has no gamma in this pair"
    spec = pair_spec(summary["pair"])
    dp = 4 if spec.pip < 1e-3 else 2
    side = {1: "BUY ", -1: "SELL"}
    ot = rungs[0].order_type.upper()
    L = [f"{summary['pair']}  spot {summary['spot']:,.{dp}f}  "
         f"{summary['gamma_side'].upper()} GAMMA {summary['gamma_1pct'] / 1e6:,.2f}mm per 1%",
         f"{summary['window_label']}: {summary['clock_hours']:.0f}h clock, "
         f"{summary['var_fraction']:.2f} days of variance "
         f"({summary['var_vs_clock']:.2f}x the clock-time answer), "
         f"1 sigma = {summary['sigma_window_pips']:,.0f} pips",
         f"{ot} orders, spacing {summary['spacing_pips']:,.0f} pips:"]
    for r in rungs:
        a = (f"  [{r.anchor} {r.anchor_dist_pips:+.0f}p]" if r.anchor else "")
        L.append(f"  {side[r.side]} {r.clip_base / 1e6:6.2f}mm {spec.base} at "
                 f"{r.level:,.{dp}f}  ({r.pips_from_spot:+6.0f}p, "
                 f"{r.sigma_dist:.2f}sig, p_touch {r.p_touch:4.0%}, "
                 f"E[fills] {r.exp_crossings:4.2f}, E[P&L] {r.exp_pnl:+7,.0f} "
                 f"{summary['ccy']}){a}")
    L.append(f"  capture {summary['exp_capture']:,.0f} - cost {summary['exp_cost']:,.0f} "
             f"+ theta {summary['theta']:,.0f} = NET {summary['net']:,.0f} "
             f"{summary['ccy']}")
    L.append(f"  {summary['verdict']}")
    for w in summary["warnings"]:
        L.append(f"  ! {w}")
    return "\n".join(L)
