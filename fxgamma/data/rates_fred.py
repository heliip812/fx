"""FRED (St. Louis Fed) -> per-currency short rates for r_d / r_f.

Two access paths, both free:

* **No key (default)** -- the graph CSV service::

      https://fred.stlouisfed.org/graph/fredgraph.csv?id=SOFR
      https://fred.stlouisfed.org/graph/fredgraph.csv?id=SOFR,EFFR&cosd=2024-01-01&coed=2026-09-01

  Body is CSV whose *first* column is the date (FRED renamed it from ``DATE`` to
  ``observation_date`` in 2025, so the parser reads column 0 by position, not by name) and
  whose missing observations are a literal ``.``.

* **With key (optional)** -- ``FRED_API_KEY`` in the environment switches to
  ``https://api.stlouisfed.org/fred/series/observations?series_id=...&file_type=json``,
  which is faster, versioned and rate-limit friendly.  Never hard-coded; read from env only.

Series coverage is the honest weak point
----------------------------------------
FRED is a US-centric mirror.  USD is excellent; the rest of G10 ranges from good to absent.
Every entry in :data:`SERIES` carries a ``confidence`` field:

  ``high``   -- ID used routinely, near-certain to exist and update daily.
  ``medium`` -- ID believed correct but NOT verified from this sandbox; may be renamed.
  ``low``    -- best guess, or a monthly/OECD series that FRED has been discontinuing.

Where FRED has no usable series (CHF/SARON, NOK, SEK, NZD, and arguably CAD/JPY overnight),
:data:`NATIVE_FALLBACK` records the central bank's own free endpoint -- documented in
docs/04_data_sources.md, not implemented in v1 -- and :data:`STATIC_FALLBACK` provides a
clearly-badged last resort so the pricer never divides by a missing rate.  Static values are
badged ``user_override`` with note ``static fallback``; contract section 7 forbids passing
them off as live.

STATUS: UNVERIFIED here -- fred.stlouisfed.org is blocked by this sandbox's egress proxy.
"""
from __future__ import annotations

import io
import logging
import math
import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

import pandas as pd

from . import _http

log = logging.getLogger(__name__)

__all__ = ["GRAPH_CSV", "API_URL", "SERIES", "STATIC_FALLBACK", "NATIVE_FALLBACK",
           "SeriesSpec", "fetch_series_csv", "parse_fred_csv", "series_frame",
           "to_continuous", "rates", "latest_value", "VERIFIED"]

VERIFIED = False

GRAPH_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
API_URL = "https://api.stlouisfed.org/fred/series/observations"


@dataclass(frozen=True)
class SeriesSpec:
    ccy: str
    series_id: str
    label: str
    basis: str = "ACT/360"       # money-market basis the rate is quoted on
    freq: str = "D"              # D | B | M
    confidence: str = "medium"   # high | medium | low
    note: str = ""


#: preferred first, fallbacks after. `rates()` walks the list until one returns a value.
SERIES: dict[str, list[SeriesSpec]] = {
    "USD": [
        SeriesSpec("USD", "SOFR", "Secured Overnight Financing Rate", "ACT/360", "B", "high"),
        SeriesSpec("USD", "EFFR", "Effective Federal Funds Rate", "ACT/360", "B", "high"),
        SeriesSpec("USD", "DGS3MO", "3-Month Treasury CMT", "ACT/365", "B", "high",
                   "bond-equivalent yield; close enough for a flat v1 curve"),
    ],
    "EUR": [
        SeriesSpec("EUR", "ECBESTRVOLWGTTRMDMNRT", "euro short-term rate (€STR)",
                   "ACT/360", "B", "medium",
                   "long FRED ID; verify with scripts/verify_live_sources.py"),
        SeriesSpec("EUR", "ECBMRRFR", "ECB main refinancing operations, fixed rate",
                   "ACT/360", "D", "medium", "policy rate, not an overnight fixing"),
        SeriesSpec("EUR", "IR3TIB01EZM156N", "3M interbank rate, euro area (OECD)",
                   "ACT/360", "M", "low", "OECD MEI; several MEI series discontinued 2022-23"),
    ],
    "GBP": [
        SeriesSpec("GBP", "IUDSOIA", "SONIA (Bank of England)", "ACT/365", "B", "medium",
                   "BoE IADB code mirrored on FRED; verify"),
        SeriesSpec("GBP", "IR3TIB01GBM156N", "3M interbank rate, UK (OECD)",
                   "ACT/365", "M", "low"),
    ],
    "JPY": [
        SeriesSpec("JPY", "IRSTCI01JPM156N", "Call money / interbank rate, Japan (OECD)",
                   "ACT/365", "M", "low", "monthly; no free daily TONA on FRED"),
        SeriesSpec("JPY", "IR3TIB01JPM156N", "3M interbank rate, Japan (OECD)",
                   "ACT/360", "M", "low"),
    ],
    "CHF": [
        SeriesSpec("CHF", "IR3TIB01CHM156N", "3M interbank rate, Switzerland (OECD)",
                   "ACT/360", "M", "low", "SARON itself is not on FRED - see NATIVE_FALLBACK"),
    ],
    "CAD": [
        SeriesSpec("CAD", "IRSTCI01CAM156N", "Call money / overnight rate, Canada (OECD)",
                   "ACT/365", "M", "low", "CORRA is not on FRED - see NATIVE_FALLBACK"),
        SeriesSpec("CAD", "IR3TIB01CAM156N", "3M interbank rate, Canada (OECD)",
                   "ACT/365", "M", "low"),
    ],
    "AUD": [
        SeriesSpec("AUD", "IR3TIB01AUM156N", "3M interbank rate, Australia (OECD)",
                   "ACT/365", "M", "low", "RBA cash rate: see NATIVE_FALLBACK"),
    ],
    "NZD": [
        SeriesSpec("NZD", "IR3TIB01NZM156N", "3M interbank rate, New Zealand (OECD)",
                   "ACT/365", "M", "low"),
    ],
    "SEK": [
        SeriesSpec("SEK", "IR3TIB01SEM156N", "3M interbank rate, Sweden (OECD)",
                   "ACT/360", "M", "low", "Riksbank policy rate: see NATIVE_FALLBACK"),
    ],
    "NOK": [
        SeriesSpec("NOK", "IR3TIB01NOM156N", "3M interbank rate, Norway (OECD)",
                   "ACT/360", "M", "low", "Norges Bank: see NATIVE_FALLBACK"),
    ],
}

#: central banks' own free endpoints, for the currencies FRED covers badly.
#: Documented for the user; not fetched in v1 (each needs its own parser).
NATIVE_FALLBACK: dict[str, str] = {
    "GBP": "https://www.bankofengland.co.uk/boeapps/database/  (series IUDSOIA, CSV export)",
    "CHF": "https://data.snb.ch/api/cube/zimoma/data/csv/en    (SNB policy rate / SARON)",
    "CAD": "https://www.bankofcanada.ca/valet/observations/... (Valet API, CORRA group)",
    "AUD": "https://www.rba.gov.au/statistics/tables/csv/f1-data.csv  (cash rate target)",
    "NZD": "https://www.rbnz.govt.nz/statistics/series/         (OCR)",
    "SEK": "https://api.riksbank.se/swea/v1/                    (SWESTR / policy rate)",
    "NOK": "https://data.norges-bank.no/api/data/IR/            (SDMX, policy rate)",
    "JPY": "https://www.stat-search.boj.or.jp/                  (BOJ Time-Series, TONA)",
    "EUR": "https://data-api.ecb.europa.eu/service/data/EST/B.EU000A2X2A25.WT  (€STR)",
}

#: last-ditch flat levels, continuously-compounded decimals. Plausible mid-2026 policy
#: levels, NOT observations. Anything that uses these is badged `user_override`.
STATIC_FALLBACK: dict[str, float] = {
    "USD": 0.0400, "EUR": 0.0200, "JPY": 0.0075, "GBP": 0.0375, "CHF": 0.0025,
    "CAD": 0.0250, "AUD": 0.0350, "NZD": 0.0300, "SEK": 0.0200, "NOK": 0.0425,
}

_MISSING = {".", "", "NA", "N/A", "nan", "NaN"}


# ----------------------------------------------------------------------------- fetch
def fetch_series_csv(series_ids: Sequence[str], start: date | None = None,
                     end: date | None = None) -> str:
    params: dict[str, str] = {"id": ",".join(series_ids)}
    if start:
        params["cosd"] = start.isoformat()
    if end:
        params["coed"] = end.isoformat()
    return _http.get_text(GRAPH_CSV, params=params)


def fetch_series_api(series_id: str, start: date | None = None, end: date | None = None,
                     api_key: str | None = None) -> dict:
    key = api_key or os.environ.get("FRED_API_KEY", "")
    if not key:
        raise _http.HttpError("FRED_API_KEY not set")
    params = {"series_id": series_id, "api_key": key, "file_type": "json"}
    if start:
        params["observation_start"] = start.isoformat()
    if end:
        params["observation_end"] = end.isoformat()
    return _http.get_json(API_URL, params=params)


# ----------------------------------------------------------------------------- parse
def parse_fred_csv(text: str) -> pd.DataFrame:
    """Pure. fredgraph CSV -> float frame indexed by UTC date, one column per series.

    Column 0 is the date whatever it is called (``DATE`` pre-2025, ``observation_date``
    after).  ``.`` means "no observation" and becomes NaN.
    """
    body = (text or "").strip()
    if not body:
        raise ValueError("fred: empty body")
    if body.lstrip().startswith("<"):
        raise ValueError("fred: HTML body (bad series id or blocked)")
    df = pd.read_csv(io.StringIO(body))
    if df.shape[1] < 2:
        raise ValueError(f"fred: unexpected columns {list(df.columns)}")
    date_col = df.columns[0]
    idx = pd.to_datetime(df[date_col], utc=True, errors="coerce")
    out = df.drop(columns=[date_col]).replace(list(_MISSING), pd.NA)
    out = out.apply(pd.to_numeric, errors="coerce")
    out.index = pd.DatetimeIndex(idx, name="date")
    out.columns = [str(c).strip() for c in out.columns]
    return out[out.index.notna()].sort_index()


def parse_fred_api_json(payload: dict) -> pd.Series:
    """Pure. FRED API observations JSON -> float Series."""
    obs = (payload or {}).get("observations")
    if obs is None:
        raise ValueError(f"fred api: no observations ({str(payload)[:120]})")
    idx, val = [], []
    for o in obs:
        v = str(o.get("value", "")).strip()
        idx.append(pd.Timestamp(o.get("date"), tz="UTC"))
        val.append(float("nan") if v in _MISSING else float(v))
    return pd.Series(val, index=pd.DatetimeIndex(idx, name="date")).sort_index()


# ------------------------------------------------------------------------- transform
def to_continuous(rate_pct: float, basis: str = "ACT/360") -> float:
    """Quoted money-market rate (percent p.a.) -> continuously-compounded decimal.

    ACT/360: annualising the daily accrual gives ``(1 + r/360)**365 - 1`` effective, hence
    ``r_cc = 365 * ln(1 + r/360)``.  ACT/365 uses 365 in both places.  At G10 rate levels the
    difference from a naive ``r/100`` is 1-3 bp -- immaterial for gamma, but free to get right.
    """
    r = float(rate_pct) / 100.0
    days = 360.0 if basis.upper().endswith("360") else 365.0
    if 1.0 + r / days <= 0.0:                       # deeply negative rate guard
        return r
    return 365.0 * math.log(1.0 + r / days)


def latest_value(s: pd.Series, max_stale_days: int = 45) -> float | None:
    """Most recent non-NaN observation, rejected if older than `max_stale_days`."""
    s = s.dropna()
    if s.empty:
        return None
    ts = s.index[-1]
    if (pd.Timestamp.now(tz="UTC") - ts).days > max_stale_days:
        log.info("fred: last observation %s is stale (%s)", ts.date(), s.name)
        return None
    return float(s.iloc[-1])


# ----------------------------------------------------------------------------- public
def series_frame(ccy: str, start: date, end: date) -> pd.DataFrame:
    """History for one currency's preferred series (first that returns data)."""
    for spec in SERIES.get(ccy.upper(), []):
        try:
            df = parse_fred_csv(fetch_series_csv([spec.series_id], start, end))
            col = df.columns[0]
            if df[col].notna().any():
                out = df[[col]].rename(columns={col: ccy.upper()})
                out.attrs["series_id"] = spec.series_id
                out.attrs["basis"] = spec.basis
                return out
        except Exception as exc:                    # noqa: BLE001
            log.info("fred %s (%s) failed: %s", spec.series_id, ccy, exc)
    raise _http.HttpError(f"no FRED series returned data for {ccy}")


def rates(ccys: Sequence[str], *, allow_static: bool = False,
          lookback_days: int = 120) -> tuple[dict[str, float], dict[str, str]]:
    """Latest CC zero rate per ccy.

    Returns ``(values, sources)`` where ``sources[ccy]`` is the FRED series id actually used,
    or ``"static-fallback"``.  The caller badges provenance from that -- a static value is
    never allowed to look live.
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)
    out: dict[str, float] = {}
    src: dict[str, str] = {}
    wanted = [c.upper() for c in ccys]

    for ccy in wanted:
        for spec in SERIES.get(ccy, []):
            try:
                df = parse_fred_csv(fetch_series_csv([spec.series_id], start, end))
            except Exception as exc:                # noqa: BLE001
                log.info("fred %s failed: %s", spec.series_id, exc)
                continue
            col = df.columns[0]
            stale = 400 if spec.freq == "M" else 45
            v = latest_value(df[col].rename(spec.series_id), max_stale_days=stale)
            if v is not None:
                out[ccy] = to_continuous(v, spec.basis)
                src[ccy] = spec.series_id
                break
        else:
            if allow_static and ccy in STATIC_FALLBACK:
                out[ccy] = STATIC_FALLBACK[ccy]
                src[ccy] = "static-fallback"
                log.warning("no live rate for %s; using static fallback %.4f "
                            "(badged user_override)", ccy, STATIC_FALLBACK[ccy])
    return out, src
