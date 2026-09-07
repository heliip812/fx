"""CME FX options -> daily settlement vols and **open interest by strike/expiry**.

Open interest by strike is what the Gamma Map page (contract section 6, page 3) is built on:
it is the only free, exchange-official picture of where listed FX gamma actually sits.  CME
publishes it daily, one business day in arrears, with no key.

Candidate public routes (all free; **none verified from this sandbox**)
----------------------------------------------------------------------
1. **Product discovery** -- resolve a Globex code (``6E``) to CME's numeric ``productId``::

       https://www.cmegroup.com/CmeWS/mvc/ProductSlate/V2/List?group=fx&pageSize=200

   We *discover* ids at runtime rather than hard-coding guesses; :data:`PRODUCT_ID_HINTS` is
   empty on purpose (see the doc: inventing ids would be worse than failing loudly).

2. **Option settlements (per product / contract month)**::

       https://www.cmegroup.com/CmeWS/mvc/Settlements/Options/Settlements/{productId}/OOF
           ?monthYear={MONTHYEAR}&strategy=DEFAULT&tradeDate={MM/DD/YYYY}&pageSize=500

   JSON with one row per strike/side: month, strike, optionType, settle, volume, openInterest.

3. **Volume & open interest report** (the daily bulletin export)::

       https://www.cmegroup.com/CmeWS/exp/voiProductDetailsViewExport.ctl
           ?media=csv&tradeDate=YYYYMMDD&reportType=F&productId={productId}

4. **Daily bulletin flat files** -- the most format-stable but least documented route::

       https://www.cmegroup.com/ftp/pub/settle/stlcur          (currency settlements)

Strike-convention reconciliation
--------------------------------
CME FX futures are all quoted **USD per unit of the foreign currency**, which is the market
convention for 6E/6B/6A/6N but the *reciprocal* of ours for 6J/6C/6S.  ``CME_INVERTED`` marks
those, and a strike ``K_cme`` maps to ``K_pair = 1/K_cme``; ``cp`` flips at the same time (a
call on JPY futures is a USDJPY put).  6J strikes are additionally published in points
(``0.006700`` vs ``670``), so :func:`normalise_strikes` sanity-checks the result against spot
and rescales by powers of ten rather than trusting a hard-coded multiplier.

Also note the **futures vs spot basis**: CME options are options *on futures*, so their
settlement vols are futures vols and their strikes are futures strikes.  For 1M-3M G10 the
gap is the CIP forward points (a few tenths of a percent of spot), which matters for placing
OI on a spot axis -- :func:`to_spot_strikes` converts using the snapshot's forwards.

Terms of use: CME's public settlement and volume/OI files are free for personal reference;
redistribution and commercial use require a licence.  Be polite -- one request per product
per day, cached.

STATUS: **UNVERIFIED**.  cmegroup.com is blocked by this sandbox's egress proxy, and unlike
Yahoo/Stooq/ECB/FRED these CmeWS paths are undocumented and have changed in the past.  Treat
this module as a best-effort implementation to be confirmed by
``python scripts/verify_live_sources.py --cme`` on a networked machine.
"""
from __future__ import annotations

import io
import logging
import math
import re
from datetime import date, datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from ..conventions import PAIRS, pair_spec
from . import _http
from .base import OI_COLUMNS, empty_oi_frame

log = logging.getLogger(__name__)

__all__ = ["PRODUCT_SLATE_URL", "SETTLEMENTS_URL", "VOI_URL", "STLCUR_URL", "CME_INVERTED",
           "discover_product_ids", "fetch_settlements", "parse_settlements_json",
           "parse_voi_csv", "parse_stlcur", "normalise_strikes", "to_pair_frame",
           "open_interest", "MONTH_CODES", "VERIFIED"]

VERIFIED = False

PRODUCT_SLATE_URL = "https://www.cmegroup.com/CmeWS/mvc/ProductSlate/V2/List"
SETTLEMENTS_URL = ("https://www.cmegroup.com/CmeWS/mvc/Settlements/Options/Settlements/"
                   "{product_id}/OOF")
VOI_URL = "https://www.cmegroup.com/CmeWS/exp/voiProductDetailsViewExport.ctl"
STLCUR_URL = "https://www.cmegroup.com/ftp/pub/settle/stlcur"

#: deliberately empty: CME numeric product ids are NOT public knowledge we can assert.
#: `discover_product_ids()` resolves them from the product slate at runtime and the result
#: is cached. Populate this from a verified run if you want to skip discovery.
PRODUCT_ID_HINTS: dict[str, int] = {}

#: Globex codes whose futures are the reciprocal of our FORDOM pair.
CME_INVERTED = {"6J", "6C", "6S"}

MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
               "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}
_MONTH_NAMES = {m.upper(): i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}

_NUM = re.compile(r"-?[\d,]*\.?\d+")


def _num(x: Any) -> float:
    """CME writes '-' for nothing, '1,234' for 1234, and 'A'/'B' price suffixes."""
    if x is None:
        return float("nan")
    s = str(x).strip().replace(",", "")
    if s in ("", "-", "--", "N/A", "UNCH"):
        return float("nan")
    m = _NUM.search(s)
    return float(m.group()) if m else float("nan")


# ----------------------------------------------------------------------------- fetch
def discover_product_ids(group: str = "fx") -> dict[str, int]:
    """Globex code -> numeric productId, straight from CME's own product slate."""
    payload = _http.get_json(PRODUCT_SLATE_URL,
                             params={"group": group, "pageSize": 200, "pageNumber": 1})
    out: dict[str, int] = {}
    for p in (payload or {}).get("products", []) or []:
        code = (p.get("globex") or p.get("globexSymbol") or p.get("symbol") or "").strip()
        pid = p.get("id") or p.get("productId")
        if code and pid is not None:
            try:
                out[code.upper()] = int(pid)
            except (TypeError, ValueError):
                continue
    if not out:
        raise ValueError("cme: product slate returned no id mapping")
    return out


def fetch_settlements(product_id: int, month_year: str, trade_date: date | None = None
                      ) -> dict[str, Any]:
    params: dict[str, Any] = {"monthYear": month_year, "strategy": "DEFAULT", "pageSize": 500}
    if trade_date:
        params["tradeDate"] = trade_date.strftime("%m/%d/%Y")
    return _http.get_json(SETTLEMENTS_URL.format(product_id=product_id), params=params)


def fetch_voi_csv(product_id: int, trade_date: date) -> str:
    return _http.get_text(VOI_URL, params={"media": "csv", "reportType": "F",
                                           "tradeDate": trade_date.strftime("%Y%m%d"),
                                           "productId": product_id})


def fetch_stlcur() -> str:
    return _http.get_text(STLCUR_URL, cfg=_http.HttpConfig(timeout=60.0))


# ----------------------------------------------------------------------------- parse
def _month_to_expiry(token: str) -> date | None:
    """'SEP 26' / 'SEP26' / 'U6' / '202609' -> a date (3rd Friday, the CME FX convention).

    CME FX options expire the Friday before the 3rd Wednesday of the contract month; quarterly
    serials and weeklies differ.  We return the **3rd Friday** as the standard listed date and
    let the caller override where the real expiry calendar matters.
    """
    t = (token or "").strip().upper().replace(" ", "")
    yy = mm = None
    m = re.fullmatch(r"([A-Z]{3})(\d{2,4})", t)
    if m and m.group(1) in _MONTH_NAMES:
        mm, yy = _MONTH_NAMES[m.group(1)], int(m.group(2))
    elif re.fullmatch(r"\d{6}", t):
        yy, mm = int(t[:4]), int(t[4:])
    else:
        m = re.fullmatch(r"([FGHJKMNQUVXZ])(\d{1,2})", t)
        if m:
            mm, yy = MONTH_CODES[m.group(1)], int(m.group(2))
    if mm is None or yy is None:
        return None
    if yy < 100:
        yy += 2000 if yy < 80 else 1900
    d = date(yy, mm, 1)
    fridays = [x for x in range(1, 29)
               if date(yy, mm, x).weekday() == 4]
    return date(yy, mm, fridays[2]) if len(fridays) >= 3 else d


def parse_settlements_json(payload: dict[str, Any]) -> pd.DataFrame:
    """Pure. CmeWS option settlements JSON -> ``strike, expiry, cp, oi, settle`` (CME space).

    Tolerant to key casing/renames: CME has shipped ``openInterest``/``openinterest``/``oi``.
    """
    if not isinstance(payload, dict):
        raise ValueError("cme: settlements payload is not an object")
    rows = payload.get("settlements") or payload.get("Settlements") or []
    if not rows:
        raise ValueError("cme: settlements payload has no rows")
    out: list[dict[str, Any]] = []
    for r in rows:
        low = {str(k).lower(): v for k, v in r.items()}
        strike = _num(low.get("strike"))
        if not math.isfinite(strike) or strike <= 0:
            continue                                  # summary/total rows
        typ = str(low.get("optiontype") or low.get("type") or "").strip().upper()
        cp = 1 if typ.startswith("C") else (-1 if typ.startswith("P") else 0)
        if cp == 0:
            continue
        exp = _month_to_expiry(str(low.get("month") or low.get("monthyear") or ""))
        if exp is None:
            continue
        out.append({"strike": strike, "expiry": exp, "cp": cp,
                    "oi": _num(low.get("openinterest") or low.get("oi")),
                    "settle": _num(low.get("settle") or low.get("settlement"))})
    if not out:
        return empty_oi_frame()
    df = pd.DataFrame(out)[OI_COLUMNS]
    df["oi"] = df["oi"].fillna(0.0)
    return df.sort_values(["expiry", "cp", "strike"], ignore_index=True)


def parse_voi_csv(text: str) -> pd.DataFrame:
    """Pure. Volume & Open Interest CSV export -> the same frame.

    Column names in this report have changed more than once; we match case-insensitively on
    substrings (``strike``, ``put/call`` or ``type``, ``open interest``, ``settle``,
    ``month``/``contract``).
    """
    body = (text or "").strip()
    if not body or body.lstrip().startswith("<"):
        raise ValueError("cme: non-CSV body from VOI export")
    df = pd.read_csv(io.StringIO(body))
    cols = {str(c).strip().lower(): c for c in df.columns}

    def find(*subs: str) -> str | None:
        for lc, orig in cols.items():
            if any(s in lc for s in subs):
                return orig
        return None

    c_strike, c_type = find("strike"), find("put/call", "putcall", "type", "cpflag")
    c_oi, c_settle = find("open interest", "openinterest", "at close"), find("settle")
    c_month = find("contract month", "month", "contract")
    if not (c_strike and c_type and c_oi):
        raise ValueError(f"cme voi: missing columns in {list(df.columns)[:10]}")
    cp = df[c_type].astype(str).str.strip().str.upper().str[0].map({"C": 1, "P": -1})
    out = pd.DataFrame({
        "strike": df[c_strike].map(_num),
        "expiry": (df[c_month].map(lambda v: _month_to_expiry(str(v)))
                   if c_month else pd.Series([None] * len(df))),
        "cp": cp,
        "oi": df[c_oi].map(_num).fillna(0.0),
        "settle": df[c_settle].map(_num) if c_settle else np.nan,
    })
    out = out.dropna(subset=["strike", "cp", "expiry"])
    out["cp"] = out["cp"].astype(int)
    return out[OI_COLUMNS].sort_values(["expiry", "cp", "strike"], ignore_index=True)


def parse_stlcur(text: str) -> pd.DataFrame:
    """Pure. The `stlcur` daily bulletin -> the same frame. **Most speculative parser here.**

    The bulletin is whitespace-delimited with product/section headers like
    ``EURO FX OPTIONS`` followed by ``<month> <strike> <c/p> ... <settle> ... <oi>``.  We scan
    for option sections and take the first numeric as the strike and the last as OI.  If the
    layout does not match, we raise rather than return junk.
    """
    rows: list[dict[str, Any]] = []
    month: date | None = None
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        head = re.match(r"^([A-Z]{3}\s?\d{2})\b", s.upper())
        if head and "OPTION" not in s.upper():
            m = _month_to_expiry(head.group(1))
            if m:
                month = m
        toks = s.split()
        if month is None or len(toks) < 4:
            continue
        cp = None
        for t in toks[:3]:
            u = t.upper()
            if u in ("C", "CALL", "CALLS"):
                cp = 1
            elif u in ("P", "PUT", "PUTS"):
                cp = -1
        if cp is None:
            continue
        nums = [_num(t) for t in toks]
        nums = [n for n in nums if math.isfinite(n)]
        if len(nums) < 2:
            continue
        rows.append({"strike": nums[0], "expiry": month, "cp": cp,
                     "oi": nums[-1], "settle": nums[-2] if len(nums) > 2 else float("nan")})
    if not rows:
        raise ValueError("cme stlcur: no option rows recognised (layout changed?)")
    return pd.DataFrame(rows)[OI_COLUMNS].sort_values(["expiry", "cp", "strike"],
                                                      ignore_index=True)


# ------------------------------------------------------------------- convention bridge
def normalise_strikes(df: pd.DataFrame, ref: float, *, invert: bool) -> pd.DataFrame:
    """CME strike space -> pair (FORDOM) strike space.

    ``ref`` is current spot in the CME quoting direction (i.e. ``1/spot`` for 6J/6C/6S).
    CME publishes some strikes in points, so we first rescale by the power of ten that puts
    the median strike within a factor of two of ``ref``, then invert if required (which also
    flips ``cp``: a call on JPY futures is a USDJPY put).
    """
    if df.empty:
        return df
    out = df.copy()
    med = float(np.nanmedian(out["strike"].to_numpy(float)))
    if med > 0 and ref > 0:
        power = round(math.log10(ref / med))
        if power != 0 and abs(power) <= 6:
            out["strike"] = out["strike"] * (10.0 ** power)
    if invert:
        out["strike"] = 1.0 / out["strike"]
        out["cp"] = -out["cp"]
    return out.sort_values(["expiry", "cp", "strike"], ignore_index=True)


def to_spot_strikes(df: pd.DataFrame, spot: float, rd: float, rf: float) -> pd.DataFrame:
    """Futures strikes -> spot-equivalent strikes (divide out the CIP forward points).

    ``K_spot = K_fut * S / F(T)`` with ``F(T) = S*exp((rd-rf)T)``, i.e. ``K*exp(-(rd-rf)T)``.
    Only meaningful when `expiry` is populated.
    """
    if df.empty:
        return df
    out = df.copy()
    today = pd.Timestamp.now(tz="UTC").date()
    T = out["expiry"].map(lambda d: max((d - today).days, 0) / 365.0).astype(float)
    out["strike"] = out["strike"] * np.exp(-(rd - rf) * T)
    return out


def to_pair_frame(df: pd.DataFrame, pair: str, spot: float) -> pd.DataFrame:
    spec = pair_spec(pair)
    code = (spec.cme_code or "").upper()
    invert = code in CME_INVERTED
    ref = (1.0 / spot) if invert else spot
    return normalise_strikes(df, ref, invert=invert)


# ----------------------------------------------------------------------------- public
def open_interest(pair: str, asof: datetime, *, spot: float,
                  months: int = 4, product_ids: dict[str, int] | None = None) -> pd.DataFrame:
    """Live path: CME OI by strike/expiry for `pair`, in FORDOM strike space."""
    spec = pair_spec(pair)
    if not spec.cme_code:
        raise KeyError(f"{pair} has no CME FX option product")
    ids = product_ids or PRODUCT_ID_HINTS or discover_product_ids()
    pid = ids.get(spec.cme_code.upper())
    if pid is None:
        raise KeyError(f"no CME productId discovered for {spec.cme_code} "
                       f"(known: {sorted(ids)[:12]})")

    trade_date = asof.astimezone(timezone.utc).date()
    frames: list[pd.DataFrame] = []
    y, m = trade_date.year, trade_date.month
    for i in range(months):
        mm = (m - 1 + i) % 12 + 1
        yy = y + (m - 1 + i) // 12
        try:
            frames.append(parse_settlements_json(
                fetch_settlements(pid, f"{yy}{mm:02d}", trade_date)))
        except Exception as exc:                    # noqa: BLE001
            log.info("cme settlements %s %d%02d failed: %s", spec.cme_code, yy, mm, exc)
    if not frames:
        try:
            frames.append(parse_voi_csv(fetch_voi_csv(pid, trade_date)))
        except Exception as exc:                    # noqa: BLE001
            log.info("cme voi fallback failed: %s", exc)
    if not frames:
        raise _http.HttpError(f"no CME OI available for {pair}")
    df = pd.concat(frames, ignore_index=True)
    return to_pair_frame(df, pair, spot)


def products() -> dict[str, str]:
    """pair -> Globex option code, for the docs and the /data page."""
    return {p: s.cme_code for p, s in PAIRS.items() if s.cme_code}
