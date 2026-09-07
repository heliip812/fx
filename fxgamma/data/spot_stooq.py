"""Stooq CSV -> daily FX OHLC.  Free, no key, no cookie, very stable format.

Endpoint::

    https://stooq.com/q/d/l/?s=eurusd&i=d           # full history, daily
    https://stooq.com/q/d/l/?s=eurusd&i=d&d1=20200101&d2=20261231

Body is plain CSV.  FX symbols return five columns::

    Date,Open,High,Low,Close
    2026-09-04,1.16512,1.16840,1.16401,1.16733

(equity/index symbols add a ``Volume`` column -- the parser accepts both).  Failure modes are
returned as *200 OK with a text body*, which is why :func:`parse_csv` checks for them:

    ``No data``                      -- unknown symbol / empty range
    ``Exceeded the daily hits limit``-- soft rate limit; back off and use the cache

Convention: Stooq uses the market convention for FX (``eurusd`` = USD per EUR, ``usdjpy`` =
JPY per USD), which is our FORDOM convention for every pair in ``conventions.PAIRS``.  Stooq
*also* publishes the reciprocals (``jpyusd``, ``usdeur``); ``SYMBOLS`` deliberately maps only
the market-convention tickers and ``INVERT`` documents the one knob you would flip if a pair
had to be sourced from a reciprocal ticker.

Terms of use: Stooq permits free personal use of its data downloads and applies a daily hit
limit per IP; there is no redistribution licence.  Be polite (``_http`` throttles to one
request / 2s per host) and cache aggressively.

STATUS: UNVERIFIED here -- stooq.com is blocked by this sandbox's egress proxy.
"""
from __future__ import annotations

import io
import logging
from datetime import date
from typing import Sequence

import pandas as pd

from ..conventions import PAIRS
from . import _http
from .base import SPOT_COLUMNS, empty_spot_frame
from .spot_yahoo import normalise_series

log = logging.getLogger(__name__)

__all__ = ["CSV_URL", "SYMBOLS", "fetch_csv", "parse_csv", "spot_history", "spot", "VERIFIED"]

VERIFIED = False

CSV_URL = "https://stooq.com/q/d/l/"

SYMBOLS: dict[str, str] = {p: p.lower() for p in PAIRS}
INVERT: dict[str, bool] = {p: False for p in PAIRS}

_ERRORS = ("no data", "exceeded the daily hits limit")


def stooq_symbol(pair: str) -> str:
    try:
        return SYMBOLS[pair.upper()]
    except KeyError as exc:
        raise KeyError(f"no Stooq symbol mapped for {pair!r}") from exc


# ----------------------------------------------------------------------------- fetch
def fetch_csv(pair: str, start: date | None = None, end: date | None = None) -> str:
    params: dict[str, str] = {"s": stooq_symbol(pair), "i": "d"}
    if start:
        params["d1"] = start.strftime("%Y%m%d")
    if end:
        params["d2"] = end.strftime("%Y%m%d")
    return _http.get_text(CSV_URL, params=params)


# ----------------------------------------------------------------------------- parse
def parse_csv(text: str, *, invert: bool = False) -> pd.DataFrame:
    """Pure. Stooq CSV -> OHLC frame, UTC DatetimeIndex named ``date``."""
    head = (text or "").strip()
    if not head:
        raise ValueError("stooq: empty body")
    low = head[:200].lower()
    for e in _ERRORS:
        if low.startswith(e):
            raise ValueError(f"stooq: {head.splitlines()[0][:120]}")
    if not low.startswith("date,"):
        raise ValueError(f"stooq: unexpected body {head[:120]!r}")

    df = pd.read_csv(io.StringIO(head))
    df.columns = [c.strip().lower() for c in df.columns]
    missing = [c for c in ("date", *SPOT_COLUMNS) if c not in df.columns]
    if missing:
        raise ValueError(f"stooq: missing columns {missing}")
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    df.index.name = "date"
    for c in SPOT_COLUMNS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[SPOT_COLUMNS].dropna(how="all")
    if df.empty:
        return empty_spot_frame()
    return normalise_series(df, invert=invert)


# ----------------------------------------------------------------------------- public
def spot_history(pair: str, start: date, end: date) -> pd.DataFrame:
    df = parse_csv(fetch_csv(pair, start, end), invert=INVERT.get(pair.upper(), False))
    return df.loc[str(start):str(end)]


def spot(pairs: Sequence[str]) -> dict[str, float]:
    """Stooq has no realtime tick for free -- the last daily close is the best it offers."""
    out: dict[str, float] = {}
    for p in pairs:
        try:
            df = parse_csv(fetch_csv(p), invert=INVERT.get(p.upper(), False))
            if not df.empty:
                out[p] = float(df["close"].iloc[-1])
        except Exception as exc:                       # noqa: BLE001
            log.warning("stooq spot %s failed: %s", p, exc)
    return out
