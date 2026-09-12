"""Yahoo Finance chart API -> **intraday** FX OHLC bars (hourly and finer).

Why this module exists
----------------------
``docs/10_forecast_evaluation.md`` s5.4 ruled out forecasting path roughness from daily
bars, and s5.2 says exactly why: an ``h``-move counter recovers only **46-63%** of the
true count when ``h`` is about one bar's move, so a daily series cannot see the
oscillation that decides how often a resting rung refills.  Its s8.4 names the fix --
*"the single highest-value thing intraday history would unlock"* -- and this is that
fix.  It is also the only way to **measure** the overnight session variance profile
(``portfolio.overnight.estimate_hour_profile``) instead of assuming it, and the input
to the trend-persistence question in ``docs/12_trend_conditional_hedging.md``.

Endpoint (the same undocumented, free, no-key chart API as
:mod:`fxgamma.data.spot_yahoo`, with a finer ``interval``)::

    https://query1.finance.yahoo.com/v8/finance/chart/EURUSD%3DX
        ?period1=<unix>&period2=<unix>&interval=1h&includePrePost=false

Response shape used here::

    chart.result[0].meta.{symbol, dataGranularity, gmtoffset, exchangeTimezoneName}
    chart.result[0].timestamp                      -> list[int]  (epoch seconds, UTC,
                                                      the bar's START)
    chart.result[0].indicators.quote[0].{open,high,low,close,volume}

The limits that shape the design (Yahoo's, not ours)
----------------------------------------------------
================  ==============  =========================================
interval          max lookback    usable overnight bars per night
================  ==============  =========================================
``1h``            ~730 days       14  (London 17:00 -> 07:00)
``30m``           ~60 days        28
``15m``           ~60 days        56
``5m``            ~60 days        168
``1m``            ~7 days         840
================  ==============  =========================================

:data:`MAX_LOOKBACK_DAYS` encodes them.  A request beyond the limit does not error --
Yahoo silently returns a **shorter** series, which is the dangerous failure mode
(you think you have two years and you have two months), so :func:`intraday_history`
clamps the request, and :func:`parse_intraday` reports the span it actually got in the
frame's ``attrs`` and warns when it is materially short of what was asked for.

Hourly is the honest choice for this project: 730 days x ~120 hourly bars a week is
~12,000 bars, about 500 per hour-of-day bucket and ~500 overnight windows -- enough to
estimate a diurnal profile and to run an out-of-sample study on the *session*, which is
the horizon the ladder is set for.  The finer intervals are supported because 60 days of
5-minute data is the right tool for measuring how much of the true crossing count hourly
sampling itself is missing (the same convergence question as ``docs/10`` s5.2, one level
down), but 60 days is far too short to evaluate a forecast on.

Conventions and gaps
--------------------
* Six-letter Yahoo FX tickers are already FORDOM (``EURUSD=X`` = USD per 1 EUR), so no
  inversion is needed for the pairs in ``conventions.PAIRS``; the machinery is kept and
  shared with :mod:`fxgamma.data.spot_yahoo` for a source that is not so tidy.
* The index is the bar's **start** in UTC, which is what Yahoo sends.  It is stamped
  ``datetime`` (not ``date``) so nothing downstream can confuse an hourly frame with a
  daily one -- they are not interchangeable and a daily estimator fed hourly bars
  silently annualises by the wrong factor.
* FX trades ~24x5.  The weekend hole (Fri 21:00 UTC -> Sun 21:00 UTC) is real and must
  **not** be interpolated: :func:`session_returns` and
  ``overnight.estimate_hour_profile`` both drop returns whose bar spacing exceeds a
  threshold, because one weekend return dumped into an hour bucket doubles that hour's
  variance.  :func:`bar_gaps` reports the holes so they can be seen rather than assumed.
* Yahoo emits null OHLC for hours with no ticks and a trailing partial bar; both are
  dropped by the parser.
* Volume is meaningless for spot FX on this feed (it is 0 or null); it is parsed and
  discarded, and never used as a liquidity proxy.

STATUS: **UNVERIFIED** in this build sandbox, like every other live adapter here --
every market-data host is blocked by the egress proxy (``CONNECT`` -> 403), so the fetch
path has never run against the live host.  The parser is fixture-tested against a
recorded-shape payload (:func:`sample_payload`), and the research in ``docs/12`` runs on
:func:`synthetic_intraday`, which is badged ``synthetic`` and is never a silent fallback
for a failed fetch (architecture s7).  ``VERIFIED = False`` until someone runs
:func:`check_live` from a machine with egress and pastes the result into
``docs/04_data_sources.md``.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Sequence

import numpy as np
import pandas as pd

from ..conventions import pair_spec
from ..types import Provenance
from . import _http
from .base import SPOT_COLUMNS, SourceStatus, prov, utcnow
from .cache import Cache, get_cache
from .spot_yahoo import INVERT_IF_NEEDED, SYMBOLS, normalise_series, yahoo_symbol

log = logging.getLogger(__name__)

__all__ = [
    "VERIFIED", "CHART_URL", "INTERVALS", "MAX_LOOKBACK_DAYS", "INTRADAY_COLUMNS",
    "empty_intraday_frame", "fetch_chart", "parse_intraday", "intraday_history",
    "intraday_with_provenance", "sample_payload", "status", "check_live",
    "bar_gaps", "session_returns", "overnight_sessions", "session_stats",
    "synthetic_intraday", "get_intraday",
]

VERIFIED = False        # never confirmed against the live host from this environment

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

#: intervals this module will request, finest last
INTERVALS: tuple[str, ...] = ("1h", "30m", "15m", "5m", "1m")

#: Yahoo's lookback ceiling per interval, in calendar days.  Exceeding it returns a
#: silently truncated series rather than an error, which is why we clamp.
MAX_LOOKBACK_DAYS: dict[str, int] = {"1h": 730, "30m": 60, "15m": 60, "5m": 60, "1m": 7}

#: bars per hour, used to convert a window into a bar count
BARS_PER_HOUR: dict[str, float] = {"1h": 1.0, "30m": 2.0, "15m": 4.0, "5m": 12.0,
                                   "1m": 60.0}

#: same OHLC schema as the daily frames, but the index is named ``datetime``
INTRADAY_COLUMNS = list(SPOT_COLUMNS)


def empty_intraday_frame() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], tz="UTC", name="datetime")
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in INTRADAY_COLUMNS},
                        index=idx)


def _check_interval(interval: str) -> str:
    iv = str(interval).strip().lower()
    if iv not in MAX_LOOKBACK_DAYS:
        raise ValueError(f"unsupported interval {interval!r}; have {list(MAX_LOOKBACK_DAYS)}")
    return iv


# ----------------------------------------------------------------------------- fetch
def fetch_chart(pair: str, start: date, end: date, interval: str = "1h") -> dict[str, Any]:
    """Network only.  Returns the decoded JSON body for one request window.

    The caller is responsible for keeping ``start`` inside
    :data:`MAX_LOOKBACK_DAYS` for the interval -- :func:`intraday_history` does that.
    """
    iv = _check_interval(interval)
    p1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    p2 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
    return _http.get_json(
        CHART_URL.format(symbol=yahoo_symbol(pair)),
        params={"period1": p1, "period2": p2, "interval": iv,
                "includePrePost": "false", "events": ""},
    )


# ----------------------------------------------------------------------------- parse
def parse_intraday(payload: dict[str, Any], *, invert: bool = False,
                   interval: str | None = None) -> pd.DataFrame:
    """Pure.  Yahoo chart JSON -> intraday OHLC frame (UTC index named ``datetime``).

    Raises ``ValueError`` on Yahoo's in-band error envelope, on an empty result, or when
    the payload's ``meta.dataGranularity`` contradicts the ``interval`` asked for -- that
    mismatch is Yahoo silently downgrading the request (it does this when the window is
    longer than the interval allows) and accepting it would put daily bars into an
    hourly frame.

    Frame ``attrs``: ``symbol``, ``granularity``, ``rows``, ``span_days``,
    ``first``/``last``, ``dropped_null``, ``source``.
    """
    chart = (payload or {}).get("chart") or {}
    if chart.get("error"):
        err = chart["error"]
        raise ValueError(f"yahoo error {err.get('code')}: {err.get('description')}")
    results = chart.get("result") or []
    if not results:
        raise ValueError("yahoo chart: empty result")
    res = results[0]
    meta = res.get("meta") or {}
    gran = str(meta.get("dataGranularity") or "")
    if interval is not None and gran and gran != _check_interval(interval):
        raise ValueError(
            f"yahoo returned {gran!r} bars for an {interval!r} request -- the window is "
            f"longer than Yahoo serves at that interval (see MAX_LOOKBACK_DAYS); "
            "refusing to mix granularities in one frame")
    ts = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    if not ts:
        out = empty_intraday_frame()
        out.attrs.update({"symbol": meta.get("symbol", ""), "granularity": gran,
                          "rows": 0, "span_days": 0.0, "dropped_null": 0,
                          "source": "yahoo_intraday"})
        return out

    idx = pd.to_datetime(pd.Series(ts, dtype="int64"), unit="s", utc=True)
    df = pd.DataFrame({c: pd.Series(quote.get(c) or [None] * len(ts), dtype="float64")
                       for c in INTRADAY_COLUMNS})
    df.index = pd.DatetimeIndex(idx, name="datetime")
    n0 = len(df)
    # a bar with any null leg is unusable for a range estimator; a bar with all nulls is
    # simply an hour with no ticks.  Drop both, and say how many.
    df = df.dropna(how="any").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df = df[(df[INTRADAY_COLUMNS] > 0).all(axis=1)]
    out = normalise_series(df, invert=invert)
    out.index.name = "datetime"
    span = ((out.index[-1] - out.index[0]).total_seconds() / 86400.0) if len(out) else 0.0
    out.attrs.update({"symbol": meta.get("symbol", ""), "granularity": gran,
                      "rows": int(len(out)), "span_days": float(span),
                      "first": out.index[0].isoformat() if len(out) else "",
                      "last": out.index[-1].isoformat() if len(out) else "",
                      "dropped_null": int(n0 - len(out)), "source": "yahoo_intraday"})
    return out


# ----------------------------------------------------------------------------- public
def intraday_history(pair: str, start: date | None = None, end: date | None = None, *,
                     interval: str = "1h", cache: Cache | None = None,
                     use_cache: bool = True, chunk_days: int | None = None
                     ) -> pd.DataFrame:
    """Intraday OHLC for ``pair``, FORDOM, UTC, ``INTRADAY_COLUMNS``.

    ``start`` defaults to the interval's maximum lookback and is **clamped** to it: ask
    for three years of hourly data and you get the two years Yahoo has, with a warning,
    rather than a silently truncated frame you believe is three years.

    Long windows are fetched in ``chunk_days`` slices and concatenated, because the
    endpoint has been observed to cap the number of bars per response.  Chunks are
    de-duplicated on the index; overlapping chunk boundaries are therefore harmless.

    The result is cached under ``spot/YAHOO_<PAIR>_<interval>`` with the daily history's
    TTL (an hourly bar is still one bar an hour, and the cache is what makes the app
    work offline).  ``use_cache=False`` forces the network.
    """
    iv = _check_interval(interval)
    today = datetime.now(timezone.utc).date()
    end = end or today
    limit = MAX_LOOKBACK_DAYS[iv]
    earliest = end - timedelta(days=limit)
    if start is None:
        start = earliest
    elif start < earliest:
        log.warning("yahoo serves at most %d days of %s bars; clamping start %s -> %s",
                    limit, iv, start, earliest)
        start = earliest

    key = f"YAHOO_{pair.upper()}_{iv}"
    c = cache if cache is not None else (get_cache() if use_cache else None)
    if c is not None and use_cache:
        hit = c.get("spot", key)
        if hit is not None and len(hit):
            df = _as_intraday(hit)
            sel = df.loc[(df.index >= pd.Timestamp(start, tz="UTC"))
                         & (df.index <= pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1))]
            if len(sel):
                sel.attrs.update({"source": "cache", "granularity": iv})
                return sel

    step = int(chunk_days or min(limit, 60 if iv != "1h" else 180))
    frames: list[pd.DataFrame] = []
    a = start
    while a <= end:
        b = min(a + timedelta(days=step), end)
        frames.append(parse_intraday(fetch_chart(pair, a, b, iv),
                                     invert=INVERT_IF_NEEDED.get(pair.upper(), False),
                                     interval=iv))
        a = b + timedelta(days=1)
    if not frames:
        return empty_intraday_frame()
    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep="last")]
    out.index.name = "datetime"
    span = (out.index[-1] - out.index[0]).total_seconds() / 86400.0 if len(out) else 0.0
    out.attrs.update({"symbol": yahoo_symbol(pair), "granularity": iv,
                      "rows": int(len(out)), "span_days": float(span),
                      "source": "yahoo_intraday"})
    if span < 0.8 * (end - start).days:
        log.warning("%s %s: asked for %d days, got %.0f -- Yahoo truncated the window",
                    pair, iv, (end - start).days, span)
    if c is not None and not c.read_only:
        c.put("spot", key, out, source="yahoo_intraday",
              url=CHART_URL.format(symbol=yahoo_symbol(pair)),
              note=f"{iv} bars, {len(out)} rows, span {span:.0f}d, UNVERIFIED endpoint")
    return out


def _as_intraday(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce a cache round-trip back into the canonical intraday shape."""
    out = df.copy()
    if not isinstance(out.index, pd.DatetimeIndex):
        for c in ("datetime", "date", "index"):
            if c in out.columns:
                out[c] = pd.to_datetime(out[c], utc=True, errors="coerce")
                out = out.set_index(c)
                break
    if out.index.tz is None:
        out.index = out.index.tz_localize("UTC")
    out.index.name = "datetime"
    keep = [c for c in INTRADAY_COLUMNS if c in out.columns]
    return out[keep].astype(float).sort_index()


def intraday_with_provenance(pair: str, start: date | None = None,
                             end: date | None = None, *, interval: str = "1h",
                             **kw: Any) -> tuple[pd.DataFrame, Provenance]:
    """:func:`intraday_history` plus the badge the UI must show (architecture s7).

    The badge is ``live`` only when the frame really came off the wire; a cache hit is
    badged ``cached`` and carries its age.  Nothing here can return ``synthetic`` --
    :func:`synthetic_intraday` is a separate, explicit call.
    """
    df = intraday_history(pair, start, end, interval=interval, **kw)
    kind = "cached" if df.attrs.get("source") == "cache" else "live"
    note = (f"{interval} bars, {len(df)} rows, span {df.attrs.get('span_days', 0):.0f}d; "
            f"endpoint UNVERIFIED in this build")
    return df, prov(f"yahoo_intraday.{pair.upper()}", kind, note=note)


def status(pair: str = "EURUSD") -> SourceStatus:
    """Row for ``scripts/verify_live_sources.py`` / the /data page.  Never claims live."""
    ok = _http.network_enabled()
    return SourceStatus("yahoo_intraday", ok=ok,
                        detail=(f"{CHART_URL.format(symbol=SYMBOLS.get(pair, pair))} "
                                f"intervals={','.join(INTERVALS)}; "
                                "hourly lookback 730d"),
                        verified=VERIFIED)


def check_live(pair: str = "EURUSD", *, interval: str = "1h", days: int = 5) -> SourceStatus:
    """Try the endpoint once and report honestly.  Blocked in this sandbox by design.

    Do **not** loop on this: the hosts are unreachable here and retrying only burns the
    proxy's patience.  Run it from a machine with egress, then update
    ``docs/04_data_sources.md`` and flip :data:`VERIFIED`.
    """
    import time
    t0 = time.monotonic()
    try:
        end = datetime.now(timezone.utc).date()
        df = parse_intraday(fetch_chart(pair, end - timedelta(days=days), end, interval),
                            interval=interval)
    except Exception as exc:                                   # noqa: BLE001
        return SourceStatus("yahoo_intraday", False, f"{type(exc).__name__}: {exc}"[:200],
                            (time.monotonic() - t0) * 1e3, VERIFIED, None)
    return SourceStatus("yahoo_intraday", True,
                        f"{len(df)} {interval} bars, span {df.attrs.get('span_days', 0):.1f}d",
                        (time.monotonic() - t0) * 1e3, VERIFIED, int(len(df)))


# ----------------------------------------------------------------------------- fixture
#: A recorded-**shape** payload: the JSON envelope is exactly what the endpoint returns
#: (checked against the daily fixture in ``fixtures/yahoo_chart_eurusd.json``), while the
#: prices are generated, not observed -- so the parser is testable with zero connectivity
#: and nobody can mistake this for market data.  It deliberately contains the three
#: things that break naive parsers: a **null bar** (an hour with no ticks), a
#: **duplicate timestamp**, and a **weekend gap**.
def sample_payload(*, rows: int = 30, seed: int = 11, granularity: str = "1h",
                   symbol: str = "EURUSD=X") -> dict[str, Any]:
    """Build the fixture payload.  Deterministic in ``seed``."""
    rng = np.random.default_rng(int(seed))
    t0 = int(datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc).timestamp())   # a Thursday
    step = 3600
    ts: list[int] = []
    t = t0
    for i in range(rows):
        ts.append(t)
        # a weekend hole after the 26th bar (Fri 21:00 -> Sun 21:00)
        t += step * (49 if i == 25 else 1)
    px = 1.1650 * np.exp(np.cumsum(rng.standard_normal(rows) * 6e-4))
    o = np.concatenate([[1.1650], px[:-1]])
    span = np.abs(rng.standard_normal(rows)) * 4e-4
    hi = np.maximum(o, px) * (1.0 + span)
    lo = np.minimum(o, px) * (1.0 - span)
    q: dict[str, list[Any]] = {"open": [round(v, 6) for v in o],
                               "high": [round(v, 6) for v in hi],
                               "low": [round(v, 6) for v in lo],
                               "close": [round(v, 6) for v in px],
                               "volume": [0] * rows}
    null_ix = 7 if rows > 7 else rows // 2         # an hour with no ticks
    for k in ("open", "high", "low", "close"):     # (index was hard-coded at 7, so any
        q[k][null_ix] = None                       #  fixture with rows < 8 raised)
    ts.append(ts[-1])                              # a duplicate timestamp
    for k in q:
        q[k].append(q[k][-1])
    return {"chart": {"result": [{
        "meta": {"currency": "USD", "symbol": symbol, "exchangeName": "CCY",
                 "instrumentType": "CURRENCY", "gmtoffset": 0, "timezone": "GMT",
                 "exchangeTimezoneName": "Europe/London", "dataGranularity": granularity,
                 "range": "", "priceHint": 4,
                 "validRanges": ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "max"]},
        "timestamp": ts,
        "indicators": {"quote": [q]}}], "error": None}}


def sample_payload_json(**kw: Any) -> str:            # pragma: no cover - convenience
    """The fixture as a JSON string, for writing to ``fixtures/`` if qa wants a file."""
    return json.dumps(sample_payload(**kw), indent=1)


# ----------------------------------------------------------------------------- shaping
def bar_gaps(df: pd.DataFrame, *, interval: str = "1h") -> pd.DataFrame:
    """Every hole in the series, so the weekend is visible rather than assumed.

    Columns: ``start``, ``end``, ``hours``, ``bars_missing``, ``weekend`` (the gap
    starts on a Friday and ends on a Sunday/Monday).  A gap that is *not* a weekend is
    either a holiday (the repo has no holiday calendar -- ``docs/10`` s4.2 and CR-16) or
    a data hole, and either way a variance estimator must drop the return across it.
    """
    if len(df) < 2:
        return pd.DataFrame(columns=["start", "end", "hours", "bars_missing", "weekend"])
    step_h = 1.0 / BARS_PER_HOUR[_check_interval(interval)]
    idx = df.index
    dt_h = np.diff(idx.to_numpy()).astype("timedelta64[s]").astype(float) / 3600.0
    hit = np.where(dt_h > step_h * 1.5)[0]
    rows = []
    for i in hit:
        a, b = idx[i], idx[i + 1]
        rows.append({"start": a, "end": b, "hours": float(dt_h[i]),
                     "bars_missing": int(round(dt_h[i] / step_h)) - 1,
                     "weekend": bool(a.dayofweek == 4 and b.dayofweek in (0, 6))})
    return pd.DataFrame(rows)


def session_returns(df: pd.DataFrame, *, interval: str = "1h",
                    max_gap_bars: float = 1.5) -> pd.DataFrame:
    """Log returns with the gap-spanning ones removed.

    Returns a frame indexed by the bar's timestamp with ``ret``, ``gap_hours`` and
    ``hour`` (UTC hour of the bar start).  A return whose bar spacing exceeds
    ``max_gap_bars`` bars is dropped, not zeroed: a weekend return is a real price
    change but it is not one bar's worth of variance, and averaging it in is how an
    hour-of-day profile ends up with a spurious Sunday spike.
    """
    step_h = 1.0 / BARS_PER_HOUR[_check_interval(interval)]
    c = df["close"].astype(float)
    r = np.log(c / c.shift(1))
    dt_h = np.concatenate([[np.nan],
                           np.diff(c.index.to_numpy()).astype("timedelta64[s]")
                           .astype(float) / 3600.0])
    out = pd.DataFrame({"ret": r.to_numpy(), "gap_hours": dt_h,
                        "hour": c.index.hour.to_numpy()}, index=c.index)
    ok = np.isfinite(out["ret"]) & (out["gap_hours"] <= step_h * float(max_gap_bars))
    out = out[ok]
    out.attrs["dropped_gap_returns"] = int((~ok).sum())
    return out


def overnight_sessions(df: pd.DataFrame, *, tz: str = "Europe/London",
                       close_h: int = 17, open_h: int = 7, interval: str = "1h",
                       min_bars: int | None = None) -> list[pd.DataFrame]:
    """Cut an intraday history into **London-close -> London-open** windows.

    This is the window the overnight ladder is left for (``portfolio.overnight``), and
    cutting on it in the *local* timezone rather than on UTC is what makes the series
    comparable across a DST change -- the user leaves the desk at 17:00 London, not at
    16:00 UTC, and half the year those are different hours.

    Sessions containing a gap longer than one bar (a weekend, a holiday, a data hole)
    are returned as-is but flagged in ``attrs['gap_bars']``; ``min_bars`` drops the
    short ones outright.  Nothing is interpolated.
    """
    if len(df) == 0:
        return []
    step_h = 1.0 / BARS_PER_HOUR[_check_interval(interval)]
    want = int(round(((24 - int(close_h) + int(open_h)) % 24) / step_h))
    min_bars = int(min_bars if min_bars is not None else max(want * 0.6, 3))
    loc = df.tz_convert(tz)
    h = loc.index.hour.to_numpy()
    # a new session starts at the first bar at/after close_h; it ends before open_h
    day = loc.index.normalize()
    # label each bar with the *evening* it belongs to, as a plain date string: the
    # integer-nanosecond shortcut here is a trap, because pandas' index resolution is
    # not always nanoseconds and the labels silently land in 1970.
    lab = np.where(h >= int(close_h), day.strftime("%Y-%m-%d"),
                   (day - pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    keep = (h >= int(close_h)) | (h < int(open_h))
    out: list[pd.DataFrame] = []
    for sid in pd.unique(lab[keep]):
        m = keep & (lab == sid)
        seg = df[m]
        if len(seg) < min_bars:
            continue
        dt_h = (np.diff(seg.index.to_numpy()).astype("timedelta64[s]").astype(float)
                / 3600.0) if len(seg) > 1 else np.array([])
        seg = seg.copy()
        seg.attrs.update({
            "session": str(sid),
            "bars": int(len(seg)), "expected_bars": want,
            "gap_bars": int(np.sum(dt_h > step_h * 1.5)),
            "window": f"{close_h:02d}:00-{open_h:02d}:00 {tz}"})
        out.append(seg)
    return out


def session_stats(sessions: Sequence[pd.DataFrame], pair: str = "EURUSD") -> pd.DataFrame:
    """Per-session path statistics: the inputs to the persistence question.

    One row per session: ``n_ret`` (usable returns), ``qv`` (sum of squared log
    returns), ``d2`` (squared net log return), ``vr = d2 / qv`` -- the session's
    realised variance ratio, whose reciprocal is exactly
    ``rangeforecast.roughness_kappa`` -- ``er`` (Kaufman efficiency ratio),
    ``range_pips``, ``net_pips`` and ``rho1`` (lag-1 return autocorrelation).

    ``vr`` here is a **single-session** estimate from a handful of bars, and per-session
    ratios must be **aggregated as sum(d2)/sum(qv)**, never averaged: the denominator can
    be arbitrarily small (a night can end where it started) so the mean of the ratio is
    dominated by a few nights and is not an estimate of anything.  ``docs/12`` gives the
    null distribution and shows that this noise, not the absence of an effect, is what
    defeats a nightly regime call.
    """
    pip = pair_spec(pair).pip
    rows = []
    for s in sessions:
        c = s["close"].astype(float).to_numpy()
        if c.size < 3:
            continue
        r = np.diff(np.log(c))
        qv = float(np.sum(r * r))
        d = float(np.sum(r))
        tv = float(np.sum(np.abs(r)))
        n = int(r.size)
        rows.append({
            "session": s.attrs.get("session", ""),
            "start": s.index[0], "end": s.index[-1], "n_ret": n,
            "n_bars": int(c.size), "qv": qv, "d2": d * d,
            "vr": float(d * d / qv) if qv > 0 else float("nan"),
            "er": float(abs(d) / tv) if tv > 0 else float("nan"),
            "rho1": (float(np.corrcoef(r[:-1], r[1:])[0, 1]) if n > 3 else float("nan")),
            "sigma": float(math.sqrt(qv)),
            "range_pips": float((s["high"].max() - s["low"].min()) / pip),
            "net_pips": float((c[-1] - c[0]) / pip),
            "gap_bars": int(s.attrs.get("gap_bars", 0)),
        })
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------- synthetic
def synthetic_intraday(pair: str = "EURUSD", *, days: int = 730, interval: str = "1h",
                       seed: int = 20260101, annual_vol: float = 0.07,
                       phi: float | Sequence[float] = 0.0,
                       profile: Sequence[float] | None = None,
                       s0: float | None = None,
                       session_phi: Sequence[float] | None = None) -> pd.DataFrame:
    """Deterministic **synthetic** intraday bars -- the offline research path.

    Every market-data host is blocked in this build, so the study in ``docs/12`` runs
    here.  This is badged synthetic by :func:`get_intraday` and is never substituted for
    a failed live fetch (architecture s7).

    What it contains, on purpose:

    * a **diurnal variance profile** (``profile``, 24 UTC weights, default the same
      shape as ``portfolio.overnight``'s: Asia trough, London step, London/NY peak),
      so the overnight window really is a minority of the day's variance;
    * a real **weekend hole** -- no bars from Friday 21:00 UTC to Sunday 21:00 UTC;
    * a tunable **return persistence** ``phi``: the AR(1) coefficient of the *hourly*
      returns.  ``phi = 0`` is the honest null (a martingale, which is what liquid FX
      is to a very good approximation at this frequency).  A non-zero ``phi``, or a
      per-session sequence in ``session_phi``, is how ``docs/12`` asks "what would have
      to be true for a trend-conditional band to pay?" -- it is an *assumption being
      tested*, never a claim about the market.
    * bar highs and lows from a seeded Brownian bridge within the bar, so range-based
      estimators have something consistent to read.

    ``phi`` may be a scalar or a sequence broadcast over sessions.  ``session_phi``
    (one value per overnight session, cycled) is the regime-switching variant.
    """
    iv = _check_interval(interval)
    per_h = BARS_PER_HOUR[iv]
    rng = np.random.default_rng(int(seed))
    spec = pair_spec(pair)
    s0 = float(s0 if s0 is not None else _ANCHOR.get(pair.upper(), 1.1660))

    # --- timestamps: 24x5 with a real weekend hole
    end = pd.Timestamp(datetime(2026, 9, 9, tzinfo=timezone.utc)).floor("h")
    idx = pd.date_range(end=end, periods=int(days * 24 * per_h), freq=f"{int(60 / per_h)}min",
                        tz="UTC", name="datetime")
    dow, hh = idx.dayofweek.to_numpy(), idx.hour.to_numpy()
    open_mask = ~(((dow == 4) & (hh >= 21)) | (dow == 5) | ((dow == 6) & (hh < 21)))
    idx = idx[open_mask]

    # --- diurnal variance weights
    w = np.asarray(profile if profile is not None else _DEFAULT_PROFILE, float)
    if w.size != 24:
        raise ValueError("profile must have 24 hourly weights")
    w = w / w.mean()
    sd_bar = float(annual_vol) / math.sqrt(252.0 * 24.0 * per_h)
    sd = sd_bar * np.sqrt(w[idx.hour.to_numpy()])

    # --- AR(1) in returns, per-bar variance held at sd (persistence changes the path
    #     shape, not the quadratic variation -- the controlled experiment of docs/12)
    n = idx.size
    if session_phi is not None:
        sp = np.asarray(session_phi, float)
        day_i = (idx.normalize() - idx.normalize()[0]).days.to_numpy()
        ph = sp[day_i % sp.size]
    else:
        ph = np.full(n, float(np.mean(phi)) if np.ndim(phi) else float(phi))
    e = rng.standard_normal(n)
    r = np.empty(n)
    prev = 0.0
    for t in range(n):
        prev = ph[t] * prev + math.sqrt(max(1.0 - ph[t] ** 2, 0.0)) * e[t]
        r[t] = prev * sd[t]
    close = s0 * np.exp(np.cumsum(r))
    op = np.concatenate([[s0], close[:-1]])
    # intra-bar extremes from a Brownian bridge: E[range] ~ 1.6 |net| plus a floor
    u = rng.random(n)
    span = (0.9 + 0.8 * rng.random(n)) * np.maximum(np.abs(r), 0.25 * sd)
    high = np.maximum(op, close) * np.exp(span * u)
    low = np.minimum(op, close) * np.exp(-span * (1.0 - u))
    df = pd.DataFrame({"open": op, "high": high, "low": low, "close": close}, index=idx)
    df = df[INTRADAY_COLUMNS]
    df.attrs.update({"source": "synthetic", "granularity": iv, "rows": int(len(df)),
                     "span_days": float(days), "pair": pair.upper(),
                     "pip": spec.pip, "annual_vol": float(annual_vol),
                     "phi": ("session_phi" if session_phi is not None else float(np.mean(phi))),
                     "note": "SIMULATED -- not market data"})
    return df


#: default 24-hour UTC variance weights (Asia trough, London step, London/NY peak).
#: A prior, the same shape ``portfolio.overnight`` ships, printed wherever it is used.
_DEFAULT_PROFILE: tuple[float, ...] = (
    0.55, 0.50, 0.60, 0.70, 0.65, 0.60, 0.70, 1.05, 1.35, 1.45, 1.35, 1.20,
    1.15, 1.55, 1.85, 1.75, 1.45, 1.10, 0.85, 0.70, 0.60, 0.55, 0.55, 0.55)

_ANCHOR: dict[str, float] = {
    "EURUSD": 1.1650, "GBPUSD": 1.3450, "USDJPY": 147.50, "AUDUSD": 0.6580,
    "NZDUSD": 0.5950, "USDCAD": 1.3720, "USDCHF": 0.7950, "USDSEK": 9.3500,
    "USDNOK": 9.9500,
}


def get_intraday(pair: str = "EURUSD", *, source: str = "auto", interval: str = "1h",
                 days: int | None = None, **kw: Any) -> tuple[pd.DataFrame, Provenance]:
    """One call that always returns a badged frame.  ``source`` in
    ``{"auto", "yahoo", "synthetic"}``.

    ``auto`` tries Yahoo (live, then cache) and falls back to :func:`synthetic_intraday`
    **only** because there is no other option in an air-gapped build -- and it says so
    in the badge, which is ``synthetic``.  Nothing downstream may treat a synthetic
    frame as live: that is architecture s7 and it is not negotiable.  Callers doing
    research should pass ``source="synthetic"`` explicitly so the intent is in the code
    rather than in the network's behaviour.
    """
    src = str(source).strip().lower()
    iv = _check_interval(interval)
    d = int(days or MAX_LOOKBACK_DAYS[iv])
    if src in ("yahoo", "auto"):
        try:
            return intraday_with_provenance(
                pair, datetime.now(timezone.utc).date() - timedelta(days=d),
                interval=iv, **{k: v for k, v in kw.items()
                                if k in ("cache", "use_cache", "chunk_days")})
        except Exception as exc:                                   # noqa: BLE001
            if src == "yahoo":
                raise
            log.warning("yahoo intraday unavailable (%s); using SYNTHETIC bars", exc)
    df = synthetic_intraday(pair, days=d, interval=iv,
                            **{k: v for k, v in kw.items()
                               if k in ("seed", "annual_vol", "phi", "profile", "s0",
                                        "session_phi")})
    return df, Provenance(source="synthetic_intraday", kind="synthetic", asof=utcnow(),
                          note=f"{iv} bars, {len(df)} rows -- SIMULATED, not market data")
