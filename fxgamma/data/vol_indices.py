"""CBOE FX volatility indices -> a long ATM-ish 1M implied-vol *history*.

Why we want them: the ETF option chains give today's smile but no usable history (Yahoo does
not serve historical chains for free).  Vol **cones** and richness z-scores need years of
1M ATM implied vol.  The CBOE FX vol indices are the only free series that provide it.

Access paths
------------
* FRED mirror (preferred, one CSV per series)::

      https://fred.stlouisfed.org/graph/fredgraph.csv?id=EVZCLS

* CBOE's own history CSV (same shape as the VIX file; pattern, not a confirmed URL)::

      https://cdn.cboe.com/api/global/us_indices/daily_prices/EVZ_History.csv

Series IDs -- read this carefully, it is the honest part
-------------------------------------------------------
``EVZCLS`` (CBOE EuroCurrency ETF Volatility Index, daily close) is the one FX vol index that
is well established on FRED and I am confident exists.  It is 30-day implied vol **of FXE
options**, i.e. exactly the ETF-vol basis described in :mod:`fxgamma.data.vol_etf_options`.

For yen and sterling the picture is much weaker:

* CBOE historically published **JYVIX** (yen, from FXY options) and **BPVIX** (sterling, from
  FXB options).  Both were *discontinued* by CBOE some years ago, and I could **not** confirm
  a live FRED ID for either.  The candidate IDs in :data:`CANDIDATES` are marked
  ``confidence="low"`` and are **guesses to be probed**, not facts.  Run
  ``python scripts/verify_live_sources.py --vol-indices`` on a networked machine: it probes
  every candidate and prints which ones actually resolve.
* Until one resolves, the honest fallback for JPY/GBP 1M ATM history is
  :func:`proxy_from_evz` -- EVZ scaled by a fixed beta -- which is a *regime* proxy only and
  must be badged ``kind="synthetic"``/``note="EVZ beta proxy"``.  It carries none of the
  pair-specific event risk (BoJ, gilt stress) that makes JPY/GBP vol interesting.

Non-FX indices we also pull because they are cheap and genuinely useful regime overlays:
``VIXCLS`` (equity), ``OVXCLS`` (oil), ``GVZCLS`` (gold).  These three I am confident exist.

Terms of use: FRED redistributes with attribution; the underlying indices are CBOE
intellectual property, free for non-commercial reference use.

STATUS: **UNVERIFIED** for every ID except ``EVZCLS``/``VIXCLS``, and even those are unproven
from this sandbox (FRED is blocked).  Nothing here invents data: an unresolvable ID raises.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from . import _http
from .rates_fred import fetch_series_csv, parse_fred_csv

log = logging.getLogger(__name__)

__all__ = ["CBOE_HISTORY_URL", "CANDIDATES", "VolIndexSpec", "fetch_index", "parse_cboe_csv",
           "index_history", "proxy_from_evz", "EVZ_BETA", "VERIFIED"]

VERIFIED = False

CBOE_HISTORY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{ticker}_History.csv"


@dataclass(frozen=True)
class VolIndexSpec:
    key: str                 # our key: "EUR", "JPY", "GBP", "EQ", ...
    fred_id: str
    cboe_ticker: str
    label: str
    confidence: str          # high | medium | low
    note: str = ""


#: probed in order; the first that returns data wins.
CANDIDATES: dict[str, list[VolIndexSpec]] = {
    "EUR": [
        VolIndexSpec("EUR", "EVZCLS", "EVZ", "CBOE EuroCurrency ETF Volatility Index (FXE)",
                     "high", "the one FX vol index I am confident about"),
    ],
    "JPY": [
        VolIndexSpec("JPY", "JYVIX", "JYVIX", "CBOE/CME Yen Volatility Index (FXY)",
                     "low", "GUESS - CBOE discontinued JYVIX; FRED ID unconfirmed"),
        VolIndexSpec("JPY", "JYVIXCLS", "JYVIX", "CBOE Yen Volatility Index, close",
                     "low", "GUESS - by analogy with EVZCLS; unconfirmed"),
    ],
    "GBP": [
        VolIndexSpec("GBP", "BPVIX", "BPVIX", "CBOE/CME Pound Volatility Index (FXB)",
                     "low", "GUESS - CBOE discontinued BPVIX; FRED ID unconfirmed"),
        VolIndexSpec("GBP", "BPVIXCLS", "BPVIX", "CBOE Pound Volatility Index, close",
                     "low", "GUESS - by analogy with EVZCLS; unconfirmed"),
    ],
    "EQ":  [VolIndexSpec("EQ", "VIXCLS", "VIX", "CBOE Volatility Index", "high")],
    "OIL": [VolIndexSpec("OIL", "OVXCLS", "OVX", "CBOE Crude Oil ETF Vol Index", "high")],
    "GOLD": [VolIndexSpec("GOLD", "GVZCLS", "GVZ", "CBOE Gold ETF Vol Index", "high")],
}

#: crude cross-currency vol betas vs EVZ, for the JPY/GBP fallback. Desk rules of thumb.
EVZ_BETA: dict[str, float] = {"EUR": 1.00, "JPY": 1.30, "GBP": 1.15,
                              "AUD": 1.35, "CHF": 1.05, "CAD": 0.80,
                              "NZD": 1.45, "SEK": 1.40, "NOK": 1.55}


# ----------------------------------------------------------------------------- fetch
def fetch_index(spec: VolIndexSpec, start: date, end: date) -> str:
    return fetch_series_csv([spec.fred_id], start, end)


def fetch_cboe_history(ticker: str) -> str:
    return _http.get_text(CBOE_HISTORY_URL.format(ticker=ticker.upper()))


# ----------------------------------------------------------------------------- parse
def parse_cboe_csv(text: str) -> pd.Series:
    """Pure. CBOE ``*_History.csv`` (``DATE,OPEN,HIGH,LOW,CLOSE``) -> close Series, decimals.

    CBOE publishes index levels in vol *points* (14.2 = 14.2%); we return decimals.
    """
    import io

    body = (text or "").strip()
    if not body or body.lstrip().startswith("<"):
        raise ValueError("cboe: non-CSV body")
    df = pd.read_csv(io.StringIO(body))
    cols = {c.strip().lower(): c for c in df.columns}
    dcol = cols.get("date") or df.columns[0]
    ccol = cols.get("close") or df.columns[-1]
    idx = pd.to_datetime(df[dcol], utc=True, errors="coerce")
    s = pd.to_numeric(df[ccol], errors="coerce") / 100.0
    s.index = pd.DatetimeIndex(idx, name="date")
    return s[s.index.notna()].dropna().sort_index().rename("vol")


def parse_fred_index(text: str) -> pd.Series:
    """Pure. FRED CSV -> close Series in decimals (FRED also publishes vol points)."""
    df = parse_fred_csv(text)
    s = df.iloc[:, 0].dropna() / 100.0
    return s.rename("vol")


# ----------------------------------------------------------------------------- public
def index_history(key: str, start: date | None = None, end: date | None = None
                  ) -> tuple[pd.Series, VolIndexSpec]:
    """1M implied-vol history for `key` ("EUR"/"JPY"/"GBP"/"EQ"/...).

    Tries each candidate ID in turn. Raises when none resolve -- we do **not** silently swap
    in the EVZ proxy; the caller decides and badges (:func:`proxy_from_evz`).
    """
    end = end or date.today()
    start = start or (end - timedelta(days=365 * 10))
    errors: list[str] = []
    for spec in CANDIDATES.get(key.upper(), []):
        try:
            s = parse_fred_index(fetch_index(spec, start, end))
            if not s.empty:
                s.attrs.update(series_id=spec.fred_id, label=spec.label,
                               confidence=spec.confidence)
                return s, spec
            errors.append(f"{spec.fred_id}: empty")
        except Exception as exc:                    # noqa: BLE001
            errors.append(f"{spec.fred_id}: {exc}")
        try:
            s = parse_cboe_csv(fetch_cboe_history(spec.cboe_ticker))
            if not s.empty:
                s.attrs.update(series_id=f"CBOE:{spec.cboe_ticker}", label=spec.label,
                               confidence=spec.confidence)
                return s, spec
        except Exception as exc:                    # noqa: BLE001
            errors.append(f"CBOE:{spec.cboe_ticker}: {exc}")
    raise _http.HttpError(f"no vol index resolved for {key}: {'; '.join(errors) or 'no candidates'}")


def proxy_from_evz(evz: pd.Series, ccy: str) -> pd.Series:
    """EVZ -> a beta-scaled stand-in for another currency's 1M ATM history.

    Regime proxy ONLY. Caller must badge ``kind='synthetic'``, ``note='EVZ beta proxy'``.
    """
    beta = EVZ_BETA.get(ccy.upper())
    if beta is None:
        raise KeyError(f"no EVZ beta for {ccy}")
    out = (evz * beta).rename("vol")
    out.attrs.update(series_id=f"EVZ*{beta}", label=f"{ccy} 1M ATM (EVZ beta proxy)",
                     confidence="proxy")
    return out
