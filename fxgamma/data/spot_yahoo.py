"""Yahoo Finance chart API -> daily FX OHLC and last spot.

Endpoint (undocumented but long-lived, free, no key)::

    https://query1.finance.yahoo.com/v8/finance/chart/EURUSD%3DX
        ?period1=<unix>&period2=<unix>&interval=1d&events=div%2Csplit

Response shape used here::

    chart.result[0].meta.{symbol, regularMarketPrice, currency, exchangeTimezoneName}
    chart.result[0].timestamp                       -> list[int]   (epoch seconds, UTC)
    chart.result[0].indicators.quote[0].{open,high,low,close}

Convention: Yahoo's six-letter FX tickers already follow the market/FORDOM convention
(``EURUSD=X`` = USD per 1 EUR; ``USDJPY=X`` = JPY per 1 USD), so no inversion is needed for
the pairs in ``conventions.PAIRS``.  The *shorthand* tickers
(``JPY=X``, ``CHF=X``) are USD-base and would need care -- we never use them.  The inversion
machinery lives in :func:`normalise_series` and is exercised by ``INVERT_IF_NEEDED`` so that
a future source quoting e.g. JPYUSD still lands in FORDOM.

Terms of use: Yahoo's ToS permit personal, non-commercial use of quotes; there is no public
redistribution licence.  Treat as best-effort, rate-limited, and never redistribute the raw
data.  Yahoo also throttles aggressively and may require a cookie+crumb (see
:mod:`fxgamma.data.vol_etf_options`); the chart endpoint historically does not.

STATUS: UNVERIFIED in this build sandbox -- every market-data host is blocked by the egress
proxy (``CONNECT`` -> 403).  Parsers are fixture-tested; the fetch path is not.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Sequence

import pandas as pd

from ..conventions import PAIRS
from . import _http
from .base import SPOT_COLUMNS, empty_spot_frame

log = logging.getLogger(__name__)

__all__ = ["CHART_URL", "yahoo_symbol", "fetch_chart", "parse_chart", "spot_history",
           "spot", "normalise_series", "VERIFIED"]

VERIFIED = False        # never confirmed against the live host from this environment

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

#: pair -> Yahoo ticker. Explicit six-letter form only (shorthand tickers are USD-based).
SYMBOLS: dict[str, str] = {p: f"{p}=X" for p in PAIRS}

#: pairs whose Yahoo series is quoted the *other* way round. Empty for the 6-letter tickers,
#: but the plumbing is kept because Stooq/other sources are not always so tidy.
INVERT_IF_NEEDED: dict[str, bool] = {p: False for p in PAIRS}


def yahoo_symbol(pair: str) -> str:
    try:
        return SYMBOLS[pair.upper()]
    except KeyError as exc:
        raise KeyError(f"no Yahoo symbol mapped for {pair!r}") from exc


# ----------------------------------------------------------------------------- fetch
def fetch_chart(pair: str, start: date, end: date, interval: str = "1d") -> dict[str, Any]:
    """Network only. Returns the decoded JSON body."""
    p1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    p2 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
    return _http.get_json(
        CHART_URL.format(symbol=yahoo_symbol(pair)),
        params={"period1": p1, "period2": p2, "interval": interval,
                "includePrePost": "false", "events": "div,split"},
    )


# ----------------------------------------------------------------------------- parse
def parse_chart(payload: dict[str, Any], *, invert: bool = False) -> pd.DataFrame:
    """Pure. Yahoo chart JSON -> OHLC frame (UTC DatetimeIndex named ``date``).

    Raises ``ValueError`` on Yahoo's in-band error envelope or an empty result.
    """
    chart = (payload or {}).get("chart") or {}
    if chart.get("error"):
        err = chart["error"]
        raise ValueError(f"yahoo error {err.get('code')}: {err.get('description')}")
    results = chart.get("result") or []
    if not results:
        raise ValueError("yahoo chart: empty result")
    res = results[0]
    ts = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    if not ts:
        return empty_spot_frame()

    idx = pd.to_datetime(pd.Series(ts, dtype="int64"), unit="s", utc=True)
    df = pd.DataFrame({c: pd.Series(quote.get(c) or [None] * len(ts), dtype="float64")
                       for c in SPOT_COLUMNS})
    df.index = pd.DatetimeIndex(idx, name="date")
    # Yahoo emits a trailing partial bar and nulls on holidays.
    df = df.dropna(how="all").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return normalise_series(df, invert=invert)


def normalise_series(df: pd.DataFrame, *, invert: bool) -> pd.DataFrame:
    """Reconcile a source's quoting convention with our FORDOM convention.

    Inverting an OHLC bar swaps high and low (1/low is the high of the reciprocal).
    """
    if not invert or df.empty:
        return df[SPOT_COLUMNS]
    out = pd.DataFrame(index=df.index)
    out["open"] = 1.0 / df["open"]
    out["high"] = 1.0 / df["low"]
    out["low"] = 1.0 / df["high"]
    out["close"] = 1.0 / df["close"]
    return out[SPOT_COLUMNS]


def parse_last(payload: dict[str, Any], *, invert: bool = False) -> float:
    """Pure. `meta.regularMarketPrice`, falling back to the last close."""
    res = ((payload or {}).get("chart") or {}).get("result") or []
    if not res:
        raise ValueError("yahoo chart: empty result")
    meta = res[0].get("meta") or {}
    px = meta.get("regularMarketPrice")
    if px is None:
        closes = [c for c in
                  (((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
                  if c is not None]
        if not closes:
            raise ValueError("yahoo chart: no price")
        px = closes[-1]
    px = float(px)
    return 1.0 / px if invert else px


# ----------------------------------------------------------------------------- public
def spot_history(pair: str, start: date, end: date) -> pd.DataFrame:
    return parse_chart(fetch_chart(pair, start, end),
                       invert=INVERT_IF_NEEDED.get(pair.upper(), False))


def spot(pairs: Sequence[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    today = datetime.now(timezone.utc).date()
    for p in pairs:
        try:
            payload = fetch_chart(p, _days_ago(today, 7), today)
            out[p] = parse_last(payload, invert=INVERT_IF_NEEDED.get(p.upper(), False))
        except Exception as exc:                       # noqa: BLE001
            log.warning("yahoo spot %s failed: %s", p, exc)
    return out


def _days_ago(d: date, n: int) -> date:
    from datetime import timedelta
    return d - timedelta(days=n)
