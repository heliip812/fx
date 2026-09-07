"""Event calendar (contract amendment v1.1, CG-6).

The calendar is a **versioned, user-editable CSV shipped in the repo** at
``data/calendar/events.csv``, loaded here and surfaced through
``MarketDataProvider.events(start, end)``.  Frozen columns::

    date, time_utc, ccy, event, importance, source        importance in {1, 2, 3}

3 = top tier, 2 = second tier, 1 = background.  ``events()`` returns those six columns plus
a derived tz-aware ``datetime``.

**Importance policy** (CG-6 names the tier-3 examples; this is how we apply it consistently):

  * **3** -- every G10 central-bank *rate decision* (FOMC, ECB GC, BoJ, BoE, SNB, BoC, RBA,
    RBNZ, Riksbank, Norges Bank) plus the headline inflation print of each G3 economy
    (US CPI, US NFP, EZ flash HICP, UK CPI, Japan national CPI).
  * **2** -- everything else that moves a G10 spot on the day: FOMC minutes, ISM, retail
    sales, PCE, flash PMIs, UK labour market, Tankan, listed option expiry, the month-end fix.
  * **1** -- background; nothing in the shipped file uses it yet.

Note ``date`` is the **UTC** date, not the local one: Japan's 08:30 JST CPI is stored as
23:30 on the previous UTC day (see :func:`_utc_stamp`).

Why a curated CSV and not a feed
--------------------------------
There is no free, stable, machine-readable calendar that covers G10 central banks *and* US
data releases.  What exists, and why each is not enough:

===========================  ==================================================================
Federal Reserve              ``federalreserve.gov/newsevents/calendar.htm`` is HTML; the RSS at
                             ``/feeds/press_all.xml`` announces events after the fact.  FOMC
                             dates are published a year ahead but only as HTML/PDF.
BLS                          ``bls.gov/schedule/news_release/`` is HTML per year; there is an
                             ICS export but its URL has moved between years.
ECB                          ``ecb.europa.eu/press/calendars/`` HTML; the Data Portal carries
                             no calendar.
BoJ / BoE / RBA / ...        HTML pages only; no common format.
Aggregators (ForexFactory,   Scraping is against their ToS and/or behind Cloudflare -- out of
Investing.com, TradingEcon)  scope under the charter's "no scraping behind paywalls" rule.
===========================  ==================================================================

So: we ship a curated file, we mark every row with where it came from and how confident we
are (the ``source`` column carries ``rule:...`` for deterministically derived rows and
``approx:<host>`` for hand-entered ones that the user must verify), and we make it trivial to
edit.  ``regenerate()`` rebuilds the rule-derived rows for a new year range.

Rule-derived rows are exact:
  * **US NFP** -- first Friday of the month, 08:30 America/New_York (DST-aware).
  * **Listed option expiry** -- third Friday, 16:00 America/New_York (the CME/ETF cut).
  * **NY option cut** -- 10:00 America/New_York daily is *not* an event and is not emitted.

Everything else -- every central-bank decision, US CPI/PCE/ISM/retail sales, EZ HICP and
PMIs, UK CPI and labour market, Japan CPI and Tankan -- is hand-entered or derived from a
"usual day of the month" rule and is flagged ``approx:<host>``.  **These dates are curated,
not fetched: verify them against the official calendars in** :data:`LIVE_SOURCES` **before
trading around them.**  A wrong event date does not corrupt a price; it corrupts the
event-weighted business time in :func:`~fxgamma.portfolio.risk.time_decay`, which is opt-in
and badged for exactly this reason.
"""
from __future__ import annotations

import calendar as _cal
import logging
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .base import EVENT_COLUMNS, empty_event_frame

log = logging.getLogger(__name__)

__all__ = ["CALENDAR_PATH", "CSV_COLUMNS", "LIVE_SOURCES", "load_calendar", "events",
           "regenerate", "write_calendar", "nth_weekday"]

#: the frozen on-disk schema (CG-6)
CSV_COLUMNS = ["date", "time_utc", "ccy", "event", "importance", "source"]

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def default_calendar_path() -> Path:
    env = os.environ.get("FXGAMMA_CALENDAR")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[2] / "data" / "calendar" / "events.csv"


CALENDAR_PATH = default_calendar_path()

#: free upstreams the user can check a row against (documented, not fetched)
LIVE_SOURCES = {
    "FOMC": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "US data": "https://www.bls.gov/schedule/news_release/",
    "ECB": "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html",
    "BoJ": "https://www.boj.or.jp/en/mopo/mpmsche_minu/",
    "BoE": "https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates",
    "RBA": "https://www.rba.gov.au/schedules-events/",
    "SNB": "https://www.snb.ch/en/the-snb/mandates-goals/monetary-policy-assessments",
    "BoC": "https://www.bankofcanada.ca/press/upcoming-events/",
    "RBNZ": "https://www.rbnz.govt.nz/news-and-events",
    "Riksbank": "https://www.riksbank.se/en-gb/press-and-published/meeting-calendar/",
    "Norges Bank": "https://www.norges-bank.no/en/news-events/calendar/",
    "Eurostat (HICP)": "https://ec.europa.eu/eurostat/news/release-calendar",
    "ONS (UK CPI)": "https://www.ons.gov.uk/releasecalendar",
    "Japan (CPI/Tankan)": "https://www.stat.go.jp/english/data/cpi/  |  https://www.boj.or.jp/en/statistics/tk/",
}


# ------------------------------------------------------------------------ date helpers
def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """`n`-th `weekday` (Mon=0) of the month. n=1 -> first."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _local_to_utc(d: date, hhmm: str, tzname: str) -> str:
    """Wall-clock local time in `tzname` on date `d` -> 'HH:MM' UTC, DST-aware.

    Note the returned time is paired with the *same* calendar date in the CSV, so only use
    local times whose UTC equivalent falls on that date (true for every release below:
    Tokyo/Sydney/Wellington announcements are morning-UTC on the meeting date).
    """
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(hh, mm), tzinfo=ZoneInfo(tzname)).astimezone(UTC).strftime("%H:%M")


def _ny_to_utc(d: date, hh: int, mm: int) -> str:
    return _local_to_utc(d, f"{hh:02d}:{mm:02d}", "America/New_York")


def _utc_stamp(d: date, hhmm: str, tzname: str) -> tuple[str, str]:
    """Local wall clock -> ``(UTC date, UTC HH:MM)``.

    Unlike :func:`_local_to_utc` this also returns the **UTC calendar date**, which differs
    from the local date for Asian morning releases (Japan CPI at 08:30 JST is 23:30 UTC on
    the *previous* day).  Every row emitted by :func:`regenerate` goes through here so the
    stored ``date + time_utc`` pair is a correct UTC instant.
    """
    hh, mm = (int(x) for x in hhmm.split(":"))
    stamp = datetime.combine(d, time(hh, mm), tzinfo=ZoneInfo(tzname)).astimezone(UTC)
    return stamp.date().isoformat(), stamp.strftime("%H:%M")


def _row(d: date, hhmm: str, tzname: str, ccy: str, event: str, importance: int,
         source: str) -> dict[str, object]:
    iso, utc_hhmm = _utc_stamp(d, hhmm, tzname)
    return {"date": iso, "time_utc": utc_hhmm, "ccy": ccy, "event": event,
            "importance": int(importance), "source": source}


def _nearest_weekday(d: date, forward: bool = True) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1 if forward else -1)
    return d


# ------------------------------------------------------------------------------ load
def load_calendar(path: str | Path | None = None) -> pd.DataFrame:
    """Read the shipped CSV, validate the frozen schema, return it unfiltered."""
    p = Path(path) if path else CALENDAR_PATH
    if not p.exists():
        log.warning("event calendar %s missing; returning empty calendar", p)
        return empty_event_frame()
    df = pd.read_csv(p, dtype=str, keep_default_na=False)
    df.columns = [c.strip().lower() for c in df.columns]
    missing = [c for c in CSV_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{p}: missing frozen columns {missing} (CG-6)")
    df = df[CSV_COLUMNS].copy()
    df = df[df["date"].str.strip() != ""]
    if df.empty:
        return empty_event_frame()

    d = pd.to_datetime(df["date"], errors="coerce", format="mixed")
    t = df["time_utc"].str.strip().replace("", "00:00")
    stamp = pd.to_datetime(d.dt.strftime("%Y-%m-%d") + " " + t,
                           errors="coerce", utc=True, format="mixed")
    bad = stamp.isna()
    if bad.any():
        log.warning("%s: dropping %d unparseable calendar rows", p, int(bad.sum()))
    out = pd.DataFrame({
        "datetime": stamp,
        "date": d.dt.date,
        "time_utc": t,
        "ccy": df["ccy"].str.strip().str.upper(),
        "event": df["event"].str.strip(),
        "importance": pd.to_numeric(df["importance"], errors="coerce").fillna(1).astype(int).clip(1, 3),
        "source": df["source"].str.strip(),
    })[EVENT_COLUMNS]
    return out[~bad].sort_values("datetime", ignore_index=True)


def events(start: date, end: date, path: str | Path | None = None) -> pd.DataFrame:
    """Calendar rows in ``[start, end]`` inclusive, columns ``EVENT_COLUMNS``."""
    df = load_calendar(path)
    if df.empty:
        return df
    lo = pd.Timestamp(start, tz="UTC")
    hi = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
    return df[(df["datetime"] >= lo) & (df["datetime"] < hi)].reset_index(drop=True)


# ------------------------------------------------------------------------- regenerate
#: hand-entered central-bank decision dates. **APPROXIMATE -- verify before use.**
#: Kept here (not only in the CSV) so `regenerate()` can rebuild the file from scratch.
CB_MEETINGS: dict[str, list[tuple[str, str, str, str]]] = {
    # ccy: [(YYYY-MM-DD, label, local announcement time, IANA tz)]
    "USD": [(d, "FOMC rate decision", "14:00", "America/New_York") for d in
            ["2026-09-16", "2026-10-28", "2026-12-09",
             "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-16",
             "2027-07-28", "2027-09-22", "2027-11-03", "2027-12-15"]],
    "EUR": [(d, "ECB Governing Council decision", "14:15", "Europe/Berlin") for d in
            ["2026-09-10", "2026-10-29", "2026-12-17",
             "2027-02-04", "2027-03-11", "2027-04-22", "2027-06-10",
             "2027-07-22", "2027-09-09", "2027-10-28", "2027-12-16"]],
    "JPY": [(d, "BoJ policy decision", "12:00", "Asia/Tokyo") for d in
            ["2026-09-18", "2026-10-30", "2026-12-18",
             "2027-01-22", "2027-03-18", "2027-05-01", "2027-06-17",
             "2027-07-30", "2027-09-21", "2027-10-29", "2027-12-17"]],
    "GBP": [(d, "BoE MPC decision", "12:00", "Europe/London") for d in
            ["2026-09-17", "2026-11-05", "2026-12-17",
             "2027-02-04", "2027-03-18", "2027-05-06", "2027-06-17",
             "2027-08-05", "2027-09-16", "2027-11-04", "2027-12-16"]],
    "CHF": [(d, "SNB monetary policy assessment", "09:30", "Europe/Zurich") for d in
            ["2026-09-24", "2026-12-10", "2027-03-25", "2027-06-17",
             "2027-09-23", "2027-12-16"]],
    "CAD": [(d, "BoC rate decision", "09:45", "America/Toronto") for d in
            ["2026-09-09", "2026-10-28", "2026-12-09",
             "2027-01-20", "2027-03-10", "2027-04-21", "2027-06-09",
             "2027-07-14", "2027-09-08", "2027-10-27", "2027-12-08"]],
    "AUD": [(d, "RBA rate decision", "14:30", "Australia/Sydney") for d in
            ["2026-09-29", "2026-11-03", "2026-12-08",
             "2027-02-02", "2027-03-16", "2027-05-04", "2027-06-15",
             "2027-08-03", "2027-09-21", "2027-11-02", "2027-12-07"]],
    "NZD": [(d, "RBNZ OCR decision", "14:00", "Pacific/Auckland") for d in
            ["2026-10-07", "2026-11-25", "2027-02-17", "2027-04-14",
             "2027-05-26", "2027-07-07", "2027-08-18", "2027-10-06", "2027-11-24"]],
    "SEK": [(d, "Riksbank policy decision", "09:30", "Europe/Stockholm") for d in
            ["2026-09-22", "2026-11-05", "2026-12-16",
             "2027-02-04", "2027-03-25", "2027-05-05", "2027-06-17",
             "2027-08-25", "2027-09-22", "2027-11-04", "2027-12-15"]],
    "NOK": [(d, "Norges Bank policy decision", "10:00", "Europe/Oslo") for d in
            ["2026-09-24", "2026-11-05", "2026-12-17",
             "2027-01-28", "2027-03-25", "2027-05-06", "2027-06-17",
             "2027-08-19", "2027-09-23", "2027-11-04", "2027-12-16"]],
}

#: US CPI is nominally "around the 10th-15th, 08:30 ET"; the exact day moves. APPROXIMATE.
_CPI_TARGET_DAY = 12


def regenerate(start_year: int, end_year: int) -> pd.DataFrame:
    """Rebuild the whole calendar: exact rule rows + the flagged approximate CB/CPI rows."""
    rows: list[dict[str, object]] = []

    for y in range(start_year, end_year + 1):
        for m in range(1, 13):
            nfp = nth_weekday(y, m, 4, 1)                       # first Friday
            rows.append(_row(nfp, "08:30", "America/New_York", "USD",
                             "US Non-Farm Payrolls", 3, "rule:first-friday-0830ET"))

            exp = nth_weekday(y, m, 4, 3)                       # third Friday
            rows.append(_row(exp, "16:00", "America/New_York", "USD",
                             "Listed option expiry (CME/ETF)", 2,
                             "rule:third-friday-1600ET"))

            # CPI: nearest weekday to the 12th, 08:30 ET. APPROXIMATE by construction.
            d = _nearest_weekday(date(y, m, min(_CPI_TARGET_DAY, _cal.monthrange(y, m)[1])))
            rows.append(_row(d, "08:30", "America/New_York", "USD", "US CPI", 3,
                             "approx:bls.gov"))

            eom = _nearest_weekday(date(y, m, _cal.monthrange(y, m)[1]), forward=False)
            rows.append(_row(eom, "16:00", "America/New_York", "USD",
                             "Month-end fix (WMR 16:00 LDN / NY close)", 2,
                             "rule:last-business-day"))

            # ---- other top-tier G3 data.  All APPROXIMATE: the exact day moves month to
            # ---- month and only the statistical office's own calendar is authoritative.
            rows.append(_row(_nearest_weekday(date(y, m, 1)), "10:00", "America/New_York",
                             "USD", "US ISM Manufacturing PMI", 2,
                             "approx:ismworld.org (1st business day)"))
            rows.append(_row(_nearest_weekday(date(y, m, 15)), "08:30", "America/New_York",
                             "USD", "US Retail Sales", 2, "approx:census.gov (~15th)"))
            rows.append(_row(eom, "08:30", "America/New_York", "USD",
                             "US PCE price index", 2, "approx:bea.gov (~last business day)"))

            # Euro area flash HICP -- last working day of the month, 11:00 CET (Eurostat)
            rows.append(_row(eom, "11:00", "Europe/Berlin", "EUR",
                             "EZ flash HICP", 3, "approx:ec.europa.eu/eurostat"))
            rows.append(_row(_nearest_weekday(date(y, m, 23)), "10:00", "Europe/Berlin",
                             "EUR", "EZ flash PMIs (HCOB)", 2, "approx:pmi.spglobal.com"))

            # UK CPI -- ONS, ~3rd Wednesday, 07:00 London
            rows.append(_row(nth_weekday(y, m, 2, 3), "07:00", "Europe/London", "GBP",
                             "UK CPI", 3, "approx:ons.gov.uk (~3rd Wednesday)"))
            rows.append(_row(nth_weekday(y, m, 1, 2), "07:00", "Europe/London", "GBP",
                             "UK labour market report", 2, "approx:ons.gov.uk"))

            # Japan national CPI -- ~3rd Friday, 08:30 JST == 23:30 UTC the PREVIOUS day
            rows.append(_row(nth_weekday(y, m, 4, 3), "08:30", "Asia/Tokyo", "JPY",
                             "Japan national CPI", 3, "approx:stat.go.jp (~3rd Friday)"))
            if m in (1, 4, 7, 10):                              # Tankan, quarterly
                rows.append(_row(_nearest_weekday(date(y, m, 1)), "08:50", "Asia/Tokyo",
                                 "JPY", "BoJ Tankan survey", 2, "approx:boj.or.jp"))

    host = {"USD": "federalreserve.gov", "EUR": "ecb.europa.eu", "JPY": "boj.or.jp",
            "GBP": "bankofengland.co.uk", "CHF": "snb.ch", "CAD": "bankofcanada.ca",
            "AUD": "rba.gov.au", "NZD": "rbnz.govt.nz", "SEK": "riksbank.se",
            "NOK": "norges-bank.no"}
    for ccy, meetings in CB_MEETINGS.items():
        for iso, label, hhmm, tzname in meetings:
            d = date.fromisoformat(iso)
            if not (start_year <= d.year <= end_year):
                continue
            rows.append(_row(d, hhmm, tzname, ccy, label, 3,
                             f"approx:{host.get(ccy, 'central bank')}"))
            if ccy == "USD":                                   # minutes, 3 weeks later
                mins = d + timedelta(days=21)
                if start_year <= mins.year <= end_year:
                    rows.append(_row(mins, "14:00", "America/New_York", "USD",
                                     "FOMC minutes", 2,
                                     "rule:fomc+3w (approx, verify federalreserve.gov)"))

    df = pd.DataFrame(rows)[CSV_COLUMNS]
    return df.sort_values(["date", "time_utc", "ccy"], ignore_index=True)


def write_calendar(df: pd.DataFrame, path: str | Path | None = None) -> Path:
    """Atomic write of the frozen-schema CSV."""
    p = Path(path) if path else CALENDAR_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp-{os.getpid()}")
    df[CSV_COLUMNS].to_csv(tmp, index=False)
    os.replace(tmp, p)
    return p


if __name__ == "__main__":                                      # pragma: no cover
    out = write_calendar(regenerate(2026, 2027))
    print(f"wrote {out} ({len(load_calendar(out))} rows)")
