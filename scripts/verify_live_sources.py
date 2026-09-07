#!/usr/bin/env python3
"""Prove (or disprove) every live data source, from a machine that actually has network.

    python scripts/verify_live_sources.py                 # everything
    python scripts/verify_live_sources.py --spot --rates  # just those groups
    python scripts/verify_live_sources.py --vol-indices   # probe EVZ/JYVIX/BPVIX candidates
    python scripts/verify_live_sources.py --cme           # probe the CmeWS routes
    python scripts/verify_live_sources.py --json report.json --verbose

Why this exists
---------------
The build sandbox blocks every market-data host (the egress proxy answers ``403`` to
``CONNECT``), so **no adapter in ``fxgamma/data/`` has ever been run against its real
endpoint**.  Each one carries ``VERIFIED = False`` and ``docs/04_data_sources.md`` marks it
UNVERIFIED.  This script is the single command that turns that into a fact, on your machine:
one pass/fail row per source, with latency, the row count or value actually fetched, and a
diagnosis when it fails.

It distinguishes, in the ``reason`` column:

  * **BLOCKED**   - the network never let us out (proxy ``CONNECT`` refusal, DNS, TLS
                    interception).  Nothing is wrong with the adapter; you are behind
                    something.
  * **HTTP 4xx/5xx** - we reached the host and it said no.  404 means the *endpoint moved*;
                    401/403 means a UA/cookie/licence gate; 429 means back off.
  * **SCHEMA**    - the host answered 200 with a body we could not parse: the endpoint's
                    format changed, or it served an HTML error/captcha page.
  * **NO SERIES** - the endpoint works but the *series id / product / symbol* does not exist
                    (this is the usual FRED and CBOE-vol-index failure, and the reason the
                    JYVIX / BPVIX ids in ``vol_indices.CANDIDATES`` are marked as guesses).

Exit codes: ``0`` all required groups satisfied, ``1`` a required group failed, ``2`` bad
usage, ``3`` an internal error in this script (it should never crash - if it does, that is a
bug in this file, not in the adapters).

A group is *satisfied* when its policy is met: ``any`` (at least one row PASSed - spot has
three interchangeable sources) or ``all``.  Optional groups never affect the exit code; they
are reported so you know what the app will have to degrade without.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:                       # runnable without PYTHONPATH
    sys.path.insert(0, str(REPO))

# --------------------------------------------------------------------------- statuses
PASS, FAIL, BLOCKED, SKIP, WARN = "PASS", "FAIL", "BLOCKED", "SKIP", "WARN"
_BAD = {FAIL, BLOCKED}

_COLOR = {PASS: "\033[32m", FAIL: "\033[31m", BLOCKED: "\033[33m",
          SKIP: "\033[90m", WARN: "\033[33m"}
_RESET = "\033[0m"


@dataclass
class Result:
    name: str
    group: str
    status: str = SKIP
    latency_ms: float | None = None
    detail: str = ""          # rows / value actually fetched
    reason: str = ""          # why it failed, and what to do
    url: str = ""
    required: bool = False
    exc: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "group": self.group, "status": self.status,
                "latency_ms": None if self.latency_ms is None else round(self.latency_ms, 1),
                "detail": self.detail, "reason": self.reason, "url": self.url,
                "required": self.required, "exception": self.exc}


@dataclass
class Check:
    name: str
    group: str
    fn: Callable[["Ctx"], str]     # returns the "detail" string; raises on failure
    url: str = ""
    required: bool = False
    note: str = ""


@dataclass
class Ctx:
    pair: str = "EURUSD"
    days: int = 30
    verbose: bool = False
    scratch: dict[str, Any] = field(default_factory=dict)


#: group -> (policy, required, human title)
GROUPS: dict[str, tuple[str, bool, str]] = {
    "local":       ("all", True,  "offline path (must work with no network at all)"),
    "spot":        ("any", True,  "spot & OHLC history"),
    "rates":       ("any", True,  "r_d / r_f short rates (FRED)"),
    "vol":         ("any", True,  "listed ETF option chains -> indicative smile"),
    "vol-index":   ("any", False, "CBOE FX vol indices -> IV history for cones/z-scores"),
    "cme":         ("any", False, "CME open interest -> Gamma Map"),
}


# ===================================================================== diagnosis
def classify(exc: BaseException) -> tuple[str, str]:
    """Map an exception onto ``(status, human reason)``.  This is the useful part."""
    from fxgamma.data import _http

    msg = f"{type(exc).__name__}: {exc}"
    low = msg.lower()

    if isinstance(exc, _http.OfflineError):
        if "fxgamma_offline" in low:
            return SKIP, "$FXGAMMA_OFFLINE is set - unset it to test the live path"
        if "403" in low and ("connect" in low or "tunnel" in low or "proxy" in low):
            return BLOCKED, ("egress proxy refused CONNECT with 403 - this host is not on "
                             "the proxy allowlist (the build sandbox's failure mode)")
        if "proxyerror" in low or "cannot connect to proxy" in low or "407" in low:
            return BLOCKED, "HTTP proxy refused/failed - check $HTTPS_PROXY credentials"
        if ("nameresolution" in low or "getaddrinfo" in low
                or "name or service not known" in low or "temporary failure in name" in low):
            return BLOCKED, "DNS did not resolve - no egress, or a split-horizon resolver"
        if "sslerror" in low or "certificate" in low or "certificate_verify_failed" in low:
            return BLOCKED, ("TLS verification failed - a TLS-intercepting proxy is in the "
                             "path; point REQUESTS_CA_BUNDLE at its CA (never disable "
                             "verification)")
        if "timed out" in low or "timeout" in low or "readtimeout" in low:
            return FAIL, "timed out - host reachable but slow, or silently dropping packets"
        if "connection refused" in low or "connectionerror" in low:
            return BLOCKED, "connection refused/reset before any HTTP response"
        return BLOCKED, "host unreachable after retries"

    if isinstance(exc, _http.HttpError):
        m = re.search(r"http (\d{3})", low)
        code = m.group(1) if m else ""
        if code == "404":
            return FAIL, ("HTTP 404 - the ENDPOINT MOVED or the symbol/series/product does "
                          "not exist; fix the URL in the adapter")
        if code in ("401", "403"):
            return FAIL, ("HTTP " + code + " - host refused us: User-Agent block, missing "
                          "cookie/crumb, or a licence gate")
        if code == "429":
            return FAIL, "HTTP 429 - rate limited; wait and re-run (the cache exists for this)"
        if code.startswith("5"):
            return FAIL, f"HTTP {code} - upstream server error; transient, re-run"
        if "no fred series returned data" in low or "no vol index resolved" in low:
            return FAIL, ("NO SERIES - every candidate id was rejected or empty; the series "
                          "was renamed or discontinued (see the ids listed above)")
        if "no cme oi available" in low or "no cme productid" in low:
            return FAIL, "NO PRODUCT - CmeWS did not yield a usable productId for this pair"
        if "non-json" in low or "non-csv" in low:
            return FAIL, ("SCHEMA - 200 OK but the body is not the expected format (HTML "
                          "error/captcha page, or the endpoint changed)")
        return FAIL, msg[:160]

    if isinstance(exc, (ValueError, KeyError, IndexError, TypeError)):
        if "html body" in low or "non-csv" in low or "non-json" in low or "unexpected body" in low:
            return FAIL, ("SCHEMA - reachable, but the payload is not what the parser "
                          "expects (endpoint changed, or an error page was served)")
        if "exceeded the daily hits limit" in low:
            return FAIL, "rate limited by the source itself (Stooq daily hit limit)"
        if "no data" in low:
            return FAIL, "source answered 'no data' - symbol or date range wrong"
        return FAIL, f"SCHEMA - {msg[:160]}"

    if isinstance(exc, ImportError):
        return FAIL, f"missing dependency: {msg[:120]} (pip install -r requirements.txt)"

    return FAIL, msg[:180]


# ===================================================================== checks
def _dates(ctx: Ctx) -> tuple[date, date]:
    end = date.today()
    return end - timedelta(days=ctx.days), end


# ---- local / offline -------------------------------------------------------------
def _c_calendar(ctx: Ctx) -> str:
    from fxgamma.data import events as ev

    df = ev.load_calendar()
    missing = [c for c in ev.CSV_COLUMNS if c not in
               [c for c in df.columns] + ["datetime"]]
    if df.empty:
        raise ValueError(f"calendar {ev.CALENDAR_PATH} is empty or missing")
    if missing:
        raise ValueError(f"frozen CG-6 columns missing: {missing}")
    bad = df[~df["importance"].isin([1, 2, 3])]
    if len(bad):
        raise ValueError(f"{len(bad)} rows with importance outside 1..3")
    today = date.today()
    ahead = df[(df["date"] >= today) & (df["date"] <= today + timedelta(days=365))]
    top = ahead[ahead["importance"] == 3]
    return (f"{len(df)} rows, {len(ahead)} in the next 12m ({len(top)} top-tier), "
            f"{df['date'].min()}..{df['date'].max()}")


def _c_manual(ctx: Ctx) -> str:
    from fxgamma.data.manual import ManualQuoteStore

    st = ManualQuoteStore()                       # raises on a corrupt marks file
    if not st.pairs():
        return f"no marks yet ({st.path}) - the book will price off the next tier"
    ages = ", ".join(f"{p}:{len(st.get(p))}t/{st.age_hours(p):.1f}h" for p in st.pairs())
    return f"{st.count()} marks - {ages}"


def _c_synthetic(ctx: Ctx) -> str:
    from fxgamma.conventions import G3
    from fxgamma.data import get_provider

    p = get_provider("synthetic")
    snap = p.snapshot(G3)
    if len(snap.surfaces) != len(G3):
        raise ValueError(f"only {len(snap.surfaces)}/{len(G3)} surfaces built")
    atm = snap.surfaces["EURUSD"].atm(1 / 12)
    return (f"{len(snap.spot)} spots, {len(snap.rates)} rates, {len(snap.surfaces)} surfaces; "
            f"EURUSD 1M ATM {atm*100:.2f}%")


def _c_parsers(ctx: Ctx) -> str:
    """Every pure parser against its recorded fixture.  No network, ever."""
    import json as _json

    from fxgamma.data import (cme_options as cme, rates_fred as fred, spot_ecb as ecb,
                              spot_stooq as stooq, spot_yahoo as yah,
                              vol_etf_options as etf, vol_indices as vi)

    fx = REPO / "fxgamma" / "data" / "fixtures"
    ok, fails = [], []

    def run(label: str, fn: Callable[[], Any], expect: Callable[[Any], bool] = bool) -> None:
        try:
            got = fn()
            if not expect(got):
                raise ValueError("parsed, but the result was empty/unexpected")
            ok.append(label)
        except Exception as exc:                                    # noqa: BLE001
            fails.append(f"{label}: {type(exc).__name__}: {exc}")

    def txt(n: str) -> str:
        return (fx / n).read_text()

    def js(n: str) -> Any:
        return _json.loads(txt(n))

    run("yahoo.parse_chart", lambda: yah.parse_chart(js("yahoo_chart_eurusd.json")),
        lambda d: len(d) > 0)
    run("yahoo.parse_last", lambda: yah.parse_last(js("yahoo_chart_eurusd.json")),
        lambda v: v > 0)
    run("yahoo.parse_chart(error payload raises)",
        lambda: _expect_raises(lambda: yah.parse_chart(js("yahoo_chart_error.json"))))
    run("stooq.parse_csv", lambda: stooq.parse_csv(txt("stooq_eurusd.csv")), lambda d: len(d) > 0)
    run("stooq.parse_csv(volume)", lambda: stooq.parse_csv(txt("stooq_with_volume.csv")),
        lambda d: len(d) > 0)
    run("stooq.parse_csv(no-data raises)",
        lambda: _expect_raises(lambda: stooq.parse_csv(txt("stooq_nodata.txt"))))
    run("stooq.parse_csv(ratelimit raises)",
        lambda: _expect_raises(lambda: stooq.parse_csv(txt("stooq_ratelimit.txt"))))
    run("ecb.parse_daily_xml", lambda: ecb.parse_daily_xml(txt("ecb_daily.xml")),
        lambda d: len(d) > 0)
    run("ecb.parse_hist_csv", lambda: ecb.parse_hist_csv(txt("ecb_hist.csv")), lambda d: len(d) > 0)
    run("ecb.cross(EURUSD)",
        lambda: ecb.cross(ecb.parse_hist_csv(txt("ecb_hist.csv")), "EURUSD"), lambda s: len(s) > 0)
    run("fred.parse_fred_csv", lambda: fred.parse_fred_csv(txt("fred_sofr.csv")), lambda d: len(d) > 0)
    run("fred.parse_fred_csv(legacy header)",
        lambda: fred.parse_fred_csv(txt("fred_legacy_header.csv")), lambda d: len(d) > 0)
    run("fred.parse_fred_csv(multi)", lambda: fred.parse_fred_csv(txt("fred_multi.csv")),
        lambda d: d.shape[1] >= 2)
    run("vol_indices.parse_cboe_csv", lambda: vi.parse_cboe_csv(txt("cboe_evz_history.csv")),
        lambda s: len(s) > 0)
    run("etf.parse_chain", lambda: etf.parse_chain(js("yahoo_options_fxe.json")),
        lambda d: len(d) > 0)
    run("etf.chain_smile->SmileQuotes", lambda: _etf_smile(etf, js("yahoo_options_fxe.json")),
        lambda q: q.atm > 0)
    # The two CME fixtures are HAND-BUILT (the host is blocked, so nothing could be
    # recorded).  They pin the shape the adapter expects; they do not prove CME serves it.
    for label, fname, fn in (
            ("cme.parse_settlements_json", "cme_settlements_SYNTHETIC.json",
             lambda: cme.parse_settlements_json(js("cme_settlements_SYNTHETIC.json"))),
            ("cme.parse_stlcur", "cme_stlcur_SYNTHETIC.txt",
             lambda: cme.parse_stlcur(txt("cme_stlcur_SYNTHETIC.txt")))):
        if (fx / fname).exists():
            run(label + " [hand-built fixture]", fn, lambda d: len(d) > 0)
        else:
            fails.append(f"{label}: fixture {fname} missing")

    if fails:
        raise ValueError(f"{len(ok)} parsers ok, {len(fails)} FAILED -- " + "; ".join(fails))
    return f"{len(ok)} parsers ok against recorded fixtures"


def _short(exc: BaseException) -> str:
    """One-token summary of a failure, for the "which ids did we try" lists."""
    from fxgamma.data import _http

    if isinstance(exc, _http.OfflineError):
        return "unreachable"
    m = re.search(r"HTTP (\d{3})", str(exc))
    if m:
        return "HTTP" + m.group(1)
    return type(exc).__name__


def _reraise_transport(errs: Sequence[BaseException]) -> None:
    """If every attempt died in transport, re-raise that -- a blocked host is not a bad id."""
    from fxgamma.data import _http

    if errs and all(isinstance(e, _http.OfflineError) for e in errs):
        raise errs[-1]


def _expect_raises(fn: Callable[[], Any]) -> bool:
    try:
        fn()
    except Exception:                                               # noqa: BLE001
        return True
    raise ValueError("expected this bad payload to raise, but it parsed")


def _etf_smile(etf, payload):
    from datetime import datetime as _dt

    df = etf.parse_chain(payload)
    spot = etf.parse_underlying(payload)
    exp = sorted(set(df["expiry"]))[0]
    asof = _dt.combine(exp, _dt.min.time(), tzinfo=timezone.utc) - timedelta(days=30)
    s = etf.chain_smile(df, exp, spot=spot, asof=asof, r=0.04)
    return etf.to_smile_quotes(s, tenor="1M")


# ---- spot ------------------------------------------------------------------------
def _c_yahoo_spot(ctx: Ctx) -> str:
    from fxgamma.data import spot_yahoo as m

    start, end = _dates(ctx)
    df = m.spot_history(ctx.pair, start, end)
    if df.empty:
        raise ValueError("empty history")
    last = m.spot([ctx.pair]).get(ctx.pair)
    return (f"{len(df)} bars {df.index[0]:%Y-%m-%d}..{df.index[-1]:%Y-%m-%d}, "
            f"last close {df['close'].iloc[-1]:.5f}, spot {last}")


def _c_stooq_spot(ctx: Ctx) -> str:
    from fxgamma.data import spot_stooq as m

    start, end = _dates(ctx)
    df = m.spot_history(ctx.pair, start, end)
    if df.empty:
        raise ValueError("empty history")
    return f"{len(df)} bars, last close {df['close'].iloc[-1]:.5f}"


def _c_ecb_spot(ctx: Ctx) -> str:
    from fxgamma.data import spot_ecb as m

    got = m.spot([ctx.pair])
    if ctx.pair not in got:
        raise ValueError(f"daily fix carried no rate for {ctx.pair}")
    return f"{ctx.pair} fix {got[ctx.pair]:.5f} (one fix/day, no OHLC)"


def _c_ecb_hist(ctx: Ctx) -> str:
    from fxgamma.data import spot_ecb as m

    start, end = _dates(ctx)
    df = m.spot_history(ctx.pair, start, end)
    if df.empty:
        raise ValueError("empty history")
    return f"{len(df)} daily fixes from the 90d/zip history"


# ---- rates -----------------------------------------------------------------------
def _c_fred_ccy(ccy: str) -> Callable[[Ctx], str]:
    def run(ctx: Ctx) -> str:
        from fxgamma.data import rates_fred as m

        tried: list[str] = []
        errs: list[BaseException] = []
        for spec in m.SERIES.get(ccy, []):
            try:
                df = m.parse_fred_csv(m.fetch_series_csv(
                    [spec.series_id], date.today() - timedelta(days=400), date.today()))
                v = m.latest_value(df.iloc[:, 0], max_stale_days=400 if spec.freq == "M" else 45)
                if v is None:
                    tried.append(f"{spec.series_id}=stale/empty")
                    continue
                cc = m.to_continuous(v, spec.basis)
                return (f"{spec.series_id} = {v:.4f}% -> cc {cc*100:.4f}% "
                        f"[{spec.confidence} confidence, {spec.basis}]"
                        + (f"; earlier ids failed: {', '.join(tried)}" if tried else ""))
            except Exception as exc:                                # noqa: BLE001
                tried.append(f"{spec.series_id}={_short(exc)}")
                errs.append(exc)
        _reraise_transport(errs)          # a proxy/DNS block is not a missing series id
        from fxgamma.data import _http

        raise _http.HttpError(
            f"no FRED series returned data for {ccy}; tried {', '.join(tried) or 'nothing'}")
    return run


# ---- vol -------------------------------------------------------------------------
def _c_yahoo_crumb(ctx: Ctx) -> str:
    from fxgamma.data import _http
    from fxgamma.data import vol_etf_options as m

    # fetch_crumb() deliberately swallows failures (the chain works without a crumb), so hit
    # the cookie host directly first -- otherwise a blocked network would read as "PASS".
    _http.get(m.COOKIE_URL)
    crumb = m.fetch_crumb()
    ctx.scratch["crumb"] = crumb
    return f"cookie host reachable; crumb {'obtained' if crumb else 'not issued (may be optional)'}"


def _c_etf_chain(symbol: str, required: bool = False) -> Callable[[Ctx], str]:
    def run(ctx: Ctx) -> str:
        from fxgamma.data import vol_etf_options as m

        payload = m.fetch_chain(symbol, crumb=ctx.scratch.get("crumb"))
        exps = m.parse_expirations(payload)
        df = m.parse_chain(payload)
        under = m.parse_underlying(payload)
        two_sided = int(((df["bid"] > 0) & (df["ask"] > 0)).sum())
        return (f"{len(df)} contracts, {len(exps)} expiries, underlying {under:.3f}, "
                f"{two_sided} two-sided quotes")
    return run


def _c_etf_smile(ctx: Ctx) -> str:
    from fxgamma.data import vol_etf_options as m

    q = m.smile_quotes(ctx.pair, datetime.now(timezone.utc), crumb=ctx.scratch.get("crumb"))
    if not q:
        raise ValueError("chain fetched but no usable smile (all quotes filtered out)")
    head = q[0]
    return (f"{len(q)} tenors; shortest T={head.T:.3f}y ATM {head.atm*100:.2f}% "
            f"RR25 {head.rr25*100:+.2f} BF25 {head.bf25*100:.2f}")


# ---- vol indices -----------------------------------------------------------------
def _c_vol_index(key: str) -> Callable[[Ctx], str]:
    """Probe EVERY candidate id for `key` and say which ones actually resolve."""
    def run(ctx: Ctx) -> str:
        from fxgamma.data import _http
        from fxgamma.data import vol_indices as m

        start, end = date.today() - timedelta(days=365), date.today()
        tried: list[str] = []
        errs: list[BaseException] = []
        for spec in m.CANDIDATES.get(key.upper(), []):
            for label, fn in ((f"FRED:{spec.fred_id}",
                               lambda sp=spec: m.parse_fred_index(m.fetch_index(sp, start, end))),
                              (f"CBOE:{spec.cboe_ticker}",
                               lambda sp=spec: m.parse_cboe_csv(
                                   m.fetch_cboe_history(sp.cboe_ticker)))):
                try:
                    ser = fn()
                    if ser.empty:
                        tried.append(f"{label}=empty")
                        continue
                    return (f"{label} RESOLVES: {len(ser)} obs, last {ser.iloc[-1]*100:.2f}% "
                            f"on {ser.index[-1]:%Y-%m-%d} [declared {spec.confidence}]"
                            + (f"; failed: {', '.join(tried)}" if tried else ""))
                except Exception as exc:                            # noqa: BLE001
                    tried.append(f"{label}={_short(exc)}")
                    errs.append(exc)
        _reraise_transport(errs)
        raise _http.HttpError(f"no vol index resolved for {key}: {', '.join(tried) or 'no candidates'}")
    return run


# ---- cme -------------------------------------------------------------------------
def _c_cme_slate(ctx: Ctx) -> str:
    from fxgamma.data import cme_options as m

    ids = m.discover_product_ids("fx")
    ctx.scratch["cme_ids"] = ids
    if not ids:
        raise ValueError("product slate parsed but yielded no Globex->productId mapping")
    shown = ", ".join(f"{k}={v}" for k, v in sorted(ids.items())[:6])
    return f"{len(ids)} FX products discovered ({shown}...)"


def _c_cme_oi(ctx: Ctx) -> str:
    from fxgamma.data import cme_options as m

    df = m.open_interest(ctx.pair, datetime.now(timezone.utc), spot=1.0)
    if df.empty:
        raise ValueError("no OI rows returned")
    return (f"{len(df)} strike rows, {df['expiry'].nunique()} expiries, "
            f"total OI {df['oi'].sum():,.0f}")


def _c_cme_stlcur(ctx: Ctx) -> str:
    from fxgamma.data import cme_options as m

    df = m.parse_stlcur(m.fetch_stlcur())
    return f"stlcur bulletin parsed: {len(df)} option rows"


# ===================================================================== registry
def build_checks(ctx: Ctx) -> list[Check]:
    from fxgamma.data import (cme_options as cme, rates_fred as fred, spot_ecb as ecb,
                              spot_stooq as stooq, spot_yahoo as yah,
                              vol_etf_options as etf, vol_indices as vi)
    from fxgamma.data import events as ev

    checks: list[Check] = [
        Check("calendar-csv", "local", _c_calendar, str(ev.CALENDAR_PATH), True),
        Check("manual-marks", "local", _c_manual, "data/manual/marks.json", True),
        Check("synthetic-provider", "local", _c_synthetic, "(offline)", True),
        Check("parsers-vs-fixtures", "local", _c_parsers, "fxgamma/data/fixtures/", True),

        Check("yahoo-spot", "spot", _c_yahoo_spot, yah.CHART_URL.format(symbol="EURUSD=X"), True),
        Check("stooq-spot", "spot", _c_stooq_spot, stooq.CSV_URL + "?s=eurusd&i=d", True),
        Check("ecb-fix", "spot", _c_ecb_spot, ecb.DAILY_XML, True),
        Check("ecb-history", "spot", _c_ecb_hist, ecb.HIST_90D_XML, False),

        Check("yahoo-etf-crumb", "vol", _c_yahoo_crumb, etf.CRUMB_URL, False),
        Check("yahoo-etf-chain:FXE", "vol", _c_etf_chain("FXE"),
              etf.OPTIONS_URL.format(symbol="FXE"), True),
        Check("yahoo-etf-chain:FXB", "vol", _c_etf_chain("FXB"),
              etf.OPTIONS_URL.format(symbol="FXB"), False),
        Check("yahoo-etf-chain:FXY", "vol", _c_etf_chain("FXY"),
              etf.OPTIONS_URL.format(symbol="FXY"), False),
        Check("etf-smile:" + ctx.pair, "vol", _c_etf_smile, "(derived)", True),

        Check("cme-product-slate", "cme", _c_cme_slate, cme.PRODUCT_SLATE_URL, False),
        Check("cme-open-interest:" + ctx.pair, "cme", _c_cme_oi, cme.SETTLEMENTS_URL, False),
        Check("cme-stlcur-bulletin", "cme", _c_cme_stlcur, cme.STLCUR_URL, False),
    ]
    for ccy in ("USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK"):
        ids = ",".join(s.series_id for s in fred.SERIES.get(ccy, [])) or "(none mapped)"
        checks.append(Check(f"fred-rate:{ccy}", "rates", _c_fred_ccy(ccy),
                            f"{fred.GRAPH_CSV}?id={ids}", ccy == "USD"))
    for key in vi.CANDIDATES:
        ids = ",".join(s.fred_id for s in vi.CANDIDATES[key])
        checks.append(Check(f"vol-index:{key}", "vol-index", _c_vol_index(key),
                            f"{fred.GRAPH_CSV}?id={ids}", key == "EUR"))
    return checks


VERIFIED_FLAGS = {
    "yahoo-spot": ("fxgamma/data/spot_yahoo.py", "VERIFIED"),
    "stooq-spot": ("fxgamma/data/spot_stooq.py", "VERIFIED"),
    "ecb-fix": ("fxgamma/data/spot_ecb.py", "VERIFIED"),
    "fred-rate:USD": ("fxgamma/data/rates_fred.py", "VERIFIED"),
    "yahoo-etf-chain:FXE": ("fxgamma/data/vol_etf_options.py", "VERIFIED"),
    "vol-index:EUR": ("fxgamma/data/vol_indices.py", "VERIFIED"),
    "cme-product-slate": ("fxgamma/data/cme_options.py", "VERIFIED"),
}


# ===================================================================== runner
def run_check(chk: Check, ctx: Ctx) -> Result:
    res = Result(chk.name, chk.group, url=chk.url, required=chk.required)
    t0 = time.perf_counter()
    try:
        res.detail = chk.fn(ctx) or ""
        res.status = PASS
    except Exception as exc:                                        # noqa: BLE001
        res.status, res.reason = classify(exc)
        res.exc = f"{type(exc).__name__}: {exc}"[:400]
        if ctx.verbose:
            traceback.print_exc()
    except BaseException as exc:                                    # never crash the run
        res.status, res.reason = FAIL, f"unexpected {type(exc).__name__}: {exc}"[:200]
    res.latency_ms = (time.perf_counter() - t0) * 1000.0
    return res


def verdicts(results: Sequence[Result]) -> dict[str, tuple[bool, str]]:
    out: dict[str, tuple[bool, str]] = {}
    for g, (policy, required, _title) in GROUPS.items():
        rows = [r for r in results if r.group == g and r.status != SKIP]
        if not rows:
            out[g] = (not required, "not run")
            continue
        req = [r for r in rows if r.required] or rows
        if policy == "any":
            ok = any(r.status == PASS for r in req)
            why = ("; ".join(f"{r.name}: {r.reason or r.status}" for r in req
                             if r.status != PASS) if not ok else
                   ", ".join(r.name for r in rows if r.status == PASS))
        else:
            bad = [r for r in req if r.status != PASS]
            ok = not bad
            why = "; ".join(f"{r.name}: {r.reason or r.status}" for r in bad) or \
                  f"{len(req)} checks passed"
        out[g] = (ok, why)
    return out


# ===================================================================== output
def render(results: list[Result], ctx: Ctx, color: bool) -> str:
    import textwrap

    cols = ["", "source", "group", "ms", "rows / value fetched"]
    rows = [[r.status, r.name, r.group,
             "-" if r.latency_ms is None else f"{r.latency_ms:.0f}",
             (r.detail if r.status == PASS else (r.reason.split(" - ")[0] or r.status))]
            for r in results]
    w = [max(len(str(x[i])) for x in [cols] + rows) for i in range(len(cols))]
    w[4] = min(w[4], 76)
    pad = " " * (w[0] + w[1] + w[2] + w[3] + 8)
    line = "  ".join("-" * n for n in w)
    out = ["  ".join(str(c).ljust(n) for c, n in zip(cols, w)), line]
    seen_reasons: set[str] = set()
    for r, row in zip(results, rows):
        cell = str(row[4])
        cell = cell if len(cell) <= w[4] else cell[: w[4] - 1] + "\u2026"
        txt = "  ".join([str(row[0]).ljust(w[0]), str(row[1]).ljust(w[1]),
                         str(row[2]).ljust(w[2]), str(row[3]).rjust(w[3]), cell])
        out.append((_COLOR[r.status] + txt + _RESET) if color else txt)
        if r.status in _BAD:
            body = r.reason if r.reason else r.exc
            if body in seen_reasons:                 # identical diagnosis: say it once
                out.append(pad + "-> (same diagnosis as above)")
            else:
                seen_reasons.add(body)
                for i, seg in enumerate(textwrap.wrap(body, 96) or [""]):
                    out.append(pad + ("-> " if i == 0 else "   ") + seg)
            if ctx.verbose and r.exc and r.exc not in body:
                for seg in textwrap.wrap("raised: " + r.exc, 96):
                    out.append(pad + "   " + seg)
    return "\n".join(out)


def env_report() -> list[str]:
    keys = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY",
            "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "FXGAMMA_OFFLINE", "FRED_API_KEY",
            "FXGAMMA_USER_AGENT", "FXGAMMA_CACHE_DIR", "FXGAMMA_MANUAL_MARKS")
    seen = [f"{k}={'<set>' if k in ('FRED_API_KEY',) else os.environ[k]}"
            for k in keys if os.environ.get(k)]
    try:
        import requests
        req = f"requests {requests.__version__}"
    except Exception as exc:                                        # noqa: BLE001
        req = f"requests MISSING ({exc})"
    return [f"python {sys.version.split()[0]}, {req}, repo {REPO}",
            "env: " + ("; ".join(seen) if seen else "no proxy/override variables set")]


# ===================================================================== main
def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="verify_live_sources.py",
        description="Prove every free market-data source the dashboard depends on.",
        epilog="Exit 0 = every required group satisfied; 1 = a required group failed.")
    ap.add_argument("--pair", default="EURUSD", help="pair used for the per-pair probes")
    ap.add_argument("--days", type=int, default=30, help="history window for spot checks")
    ap.add_argument("--timeout", type=float, default=15.0, help="per-request timeout (s)")
    ap.add_argument("--retries", type=int, default=2, help="attempts per request")
    ap.add_argument("--only", default="", help="comma-separated check names (substring match)")
    ap.add_argument("--group", action="append", default=[], metavar="G",
                    help=f"limit to a group: {', '.join(GROUPS)}")
    for g in GROUPS:
        ap.add_argument(f"--{g}", dest="group", action="append_const", const=g,
                        help=f"shorthand for --group {g}")
    ap.add_argument("--all", action="store_true", help="run every group (the default)")
    ap.add_argument("--json", metavar="PATH", help="write the machine-readable report ('-' = stdout)")
    ap.add_argument("--verbose", "-v", action="store_true", help="print tracebacks")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--list", action="store_true", help="list checks and exit")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:                                       # argparse already printed
        return int(exc.code or 2)

    ctx = Ctx(pair=args.pair.upper(), days=max(5, args.days), verbose=args.verbose)

    try:
        from fxgamma.data import _http

        # HttpConfig is a frozen default on a keyword-only parameter, so override it there.
        _http.get.__kwdefaults__["cfg"] = _http.HttpConfig(
            timeout=args.timeout, retries=max(1, args.retries))
        checks = build_checks(ctx)
    except Exception as exc:                                        # noqa: BLE001
        print(f"internal error building the check list: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        return 3

    wanted = set(args.group or []) if not args.all else set(GROUPS)
    if wanted - set(GROUPS):
        print(f"unknown group(s): {sorted(wanted - set(GROUPS))}", file=sys.stderr)
        return 2
    sel = [c for c in checks
           if (not wanted or c.group in wanted)
           and (not args.only or any(s.strip().lower() in c.name.lower()
                                     for s in args.only.split(",") if s.strip()))]
    if args.list:
        for c in sel:
            print(f"{c.group:10s} {c.name:28s} {'required' if c.required else 'optional':9s} {c.url}")
        return 0
    if not sel:
        print("no checks selected", file=sys.stderr)
        return 2

    color = sys.stdout.isatty() and not args.no_color
    started = datetime.now(timezone.utc)
    print(f"fxgamma live-source verification  {started:%Y-%m-%d %H:%M:%SZ}")
    for line in env_report():
        print("  " + line)
    print()

    results = [run_check(c, ctx) for c in sel]
    print(render(results, ctx, color))

    v = verdicts(results)
    print()
    print("group verdicts")
    failed_required = []
    for g, (policy, required, title) in GROUPS.items():
        if not any(r.group == g for r in results):
            continue
        ok, why = v[g]
        tag = "OK  " if ok else ("FAIL" if required else "DEGRADED")
        print(f"  {tag:9s} {g:10s} [{policy}-of, {'required' if required else 'optional'}]  "
              f"{title}\n             {why[:200]}")
        if required and not ok:
            failed_required.append(g)

    n_pass = sum(1 for r in results if r.status == PASS)
    n_block = sum(1 for r in results if r.status == BLOCKED)
    n_fail = sum(1 for r in results if r.status == FAIL)
    print(f"\n{n_pass} passed, {n_fail} failed, {n_block} blocked, "
          f"{len(results) - n_pass - n_fail - n_block} skipped "
          f"in {(datetime.now(timezone.utc) - started).total_seconds():.1f}s")

    flips = [f"  {VERIFIED_FLAGS[r.name][0]}: set {VERIFIED_FLAGS[r.name][1]} = True"
             for r in results if r.status == PASS and r.name in VERIFIED_FLAGS]
    if flips:
        print("\nthese adapters are now genuinely verified on this machine:")
        print("\n".join(flips))
        print("  (and update the verified/UNVERIFIED column in docs/04_data_sources.md)")
    if n_block:
        print("\nBLOCKED means the network never let us out - the adapters were not tested. "
              "\nRun this again from an unrestricted machine before trusting any live number.")

    if args.json:
        blob = {"started": started.isoformat(), "pair": ctx.pair,
                "results": [r.as_dict() for r in results],
                "groups": {g: {"ok": v[g][0], "why": v[g][1],
                               "required": GROUPS[g][1], "policy": GROUPS[g][0]}
                           for g in v if any(r.group == g for r in results)},
                "exit": 1 if failed_required else 0}
        text = json.dumps(blob, indent=2)
        if args.json == "-":
            print(text)
        else:
            Path(args.json).write_text(text)
            print(f"\nwrote {args.json}")

    return 1 if failed_required else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
    except SystemExit:
        raise
    except BaseException as exc:                                    # pragma: no cover
        print(f"internal error (this is a bug in verify_live_sources.py): "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        raise SystemExit(3)
