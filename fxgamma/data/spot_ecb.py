"""ECB euro foreign-exchange reference rates -> official daily fixes.

Endpoints (all free, no key)::

    https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml      # today only
    https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml   # last 90 days
    https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip       # 1999-> , CSV in a zip
    https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A?format=csvdata  # portal

What it is and is not
---------------------
* **Is**: the official ECB reference rate, published on TARGET business days at ~16:00 CET,
  based on the 14:15 CET concertation between central banks.  Authoritative, revision-free,
  redistributable with attribution.
* **Is not**: a tradable rate, a close, or intraday.  **One fix per day, no OHLC.**  We
  therefore populate ``open=high=low=close`` and say so in the provenance note -- any
  realised-vol estimator that needs a range (Parkinson, Garman-Klass, Rogers-Satchell) must
  *not* be run on ECB bars.  Close-to-close only.

Convention
----------
ECB quotes **EUR-base**: the value for USD is "1 EUR = 1.1673 USD".  Our pairs are FORDOM, so

    S(FOR/DOM) = (EUR->DOM) / (EUR->FOR),      with EUR->EUR := 1

which gives EURUSD directly, USDJPY = JPY/USD, AUDUSD = USD/AUD, and so on.  Cross rates
built this way are exact triangulations of the fix, not independent observations -- fine for
history, useless for cross-basis analysis.

Terms of use: the ECB permits free reuse of the reference rates with attribution
("Source: European Central Bank"); the Data Portal is published under CC BY 4.0.

STATUS: UNVERIFIED here -- www.ecb.europa.eu is blocked by this sandbox's egress proxy.
"""
from __future__ import annotations

import csv
import io
import logging
import re
import zipfile
from datetime import date
from typing import Sequence

import pandas as pd

from ..conventions import PAIRS, pair_spec
from . import _http
from .base import SPOT_COLUMNS, empty_spot_frame

log = logging.getLogger(__name__)

__all__ = ["DAILY_XML", "HIST_90D_XML", "HIST_ZIP", "PORTAL_CSV", "fetch_daily_xml",
           "fetch_hist_zip", "parse_daily_xml", "parse_hist_csv", "parse_hist_zip",
           "cross", "to_pair_frame", "spot_history", "spot", "VERIFIED"]

VERIFIED = False

DAILY_XML = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
HIST_90D_XML = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
HIST_ZIP = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
PORTAL_CSV = ("https://data-api.ecb.europa.eu/service/data/EXR/D.{ccy}.EUR.SP00.A"
              "?format=csvdata&detail=dataonly")

#: currencies the reference-rate file covers that we care about
ECB_CCYS = ("USD", "JPY", "GBP", "CHF", "SEK", "NOK", "DKK", "AUD", "NZD", "CAD")

_MISSING = {"", "N/A", "NA", "-", "."}


# ----------------------------------------------------------------------------- fetch
def fetch_daily_xml() -> str:
    return _http.get_text(DAILY_XML)


def fetch_hist_90d_xml() -> str:
    return _http.get_text(HIST_90D_XML)


def fetch_hist_zip() -> bytes:
    return _http.get_bytes(HIST_ZIP, cfg=_http.HttpConfig(timeout=60.0))


# ----------------------------------------------------------------------------- parse
_TAG = re.compile(r"\{[^}]*\}")


def parse_daily_xml(text: str) -> pd.DataFrame:
    """Pure. eurofxref XML (daily or 90-day) -> wide frame, index=date, columns=ccy, EUR-base.

    Namespace-agnostic: the ECB has changed the gesmes namespace URI before.
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring(text)
    rows: dict[pd.Timestamp, dict[str, float]] = {}
    for node in root.iter():
        if _TAG.sub("", node.tag) != "Cube" or "time" not in node.attrib:
            continue
        day = pd.Timestamp(node.attrib["time"], tz="UTC")
        row: dict[str, float] = {}
        for child in node:
            a = child.attrib
            ccy, rate = a.get("currency"), a.get("rate")
            if ccy and rate is not None and rate not in _MISSING:
                try:
                    row[ccy.upper()] = float(rate)
                except ValueError:
                    continue
        if row:
            rows[day] = row
    if not rows:
        raise ValueError("ecb: no <Cube time=...> nodes with rates")
    df = pd.DataFrame.from_dict(rows, orient="index").sort_index()
    df.index.name = "date"
    df["EUR"] = 1.0
    return df


def parse_hist_csv(text: str) -> pd.DataFrame:
    """Pure. eurofxref-hist.csv -> wide frame, index=date, columns=ccy, EUR-base.

    Real file quirks handled: header/rows have a trailing empty field, values may be ``N/A``,
    header cells carry leading spaces, and rows run newest-first.
    """
    reader = csv.reader(io.StringIO(text.strip()))
    try:
        header = [h.strip().upper() for h in next(reader)]
    except StopIteration as exc:
        raise ValueError("ecb: empty csv") from exc
    if not header or header[0] != "DATE":
        raise ValueError(f"ecb: unexpected header {header[:4]}")
    ccys = header[1:]
    dates: list[pd.Timestamp] = []
    recs: list[dict[str, float]] = []
    for raw in reader:
        if not raw or not raw[0].strip():
            continue
        try:
            d = pd.Timestamp(raw[0].strip(), tz="UTC")
        except ValueError:
            continue
        row: dict[str, float] = {}
        for ccy, val in zip(ccys, raw[1:]):
            v = (val or "").strip()
            if not ccy or v in _MISSING:
                continue
            try:
                row[ccy] = float(v)
            except ValueError:
                continue
        if row:
            dates.append(d)
            recs.append(row)
    if not recs:
        raise ValueError("ecb: no data rows")
    df = pd.DataFrame(recs, index=pd.DatetimeIndex(dates, name="date")).sort_index()
    df["EUR"] = 1.0
    return df


def parse_hist_zip(blob: bytes) -> pd.DataFrame:
    """Pure. The downloaded zip -> the same wide EUR-base frame."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError("ecb: zip contains no csv")
        return parse_hist_csv(z.read(names[0]).decode("utf-8", "replace"))


# ------------------------------------------------------------------- convention bridge
def cross(wide: pd.DataFrame, pair: str) -> pd.Series:
    """EUR-base table -> the FORDOM series for `pair`  ==  (EUR->DOM) / (EUR->FOR)."""
    spec = pair_spec(pair)
    for c in (spec.base, spec.quote):
        if c not in wide.columns:
            raise KeyError(f"ecb: no reference rate for {c} (pair {pair})")
    s = wide[spec.quote].astype(float) / wide[spec.base].astype(float)
    s.name = pair
    return s.dropna()


def to_pair_frame(wide: pd.DataFrame, pair: str) -> pd.DataFrame:
    """FORDOM OHLC frame from the single daily fix (open=high=low=close)."""
    s = cross(wide, pair)
    if s.empty:
        return empty_spot_frame()
    df = pd.DataFrame({c: s.astype(float) for c in SPOT_COLUMNS})
    df.index.name = "date"
    return df[SPOT_COLUMNS]


# ----------------------------------------------------------------------------- public
def spot_history(pair: str, start: date, end: date) -> pd.DataFrame:
    """Full history via the zip (cache it: it is ~1 MB and updates once a day)."""
    wide = parse_hist_zip(fetch_hist_zip())
    return to_pair_frame(wide, pair).loc[str(start):str(end)]


def spot(pairs: Sequence[str]) -> dict[str, float]:
    wide = parse_daily_xml(fetch_daily_xml())
    out: dict[str, float] = {}
    for p in pairs:
        if p.upper() not in PAIRS:
            continue
        try:
            s = cross(wide, p)
            if not s.empty:
                out[p] = float(s.iloc[-1])
        except KeyError as exc:
            log.info("ecb spot %s unavailable: %s", p, exc)
    return out
