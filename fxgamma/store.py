"""SQLite persistence for the book  [dev-owned, contract section 1].

Everything the app must not lose lives here: option and spot positions, the
per-position mark-vol side table mandated by AMENDMENT v1.1 **CG-2**, the hedge
log ordered by ``trade_time`` (**CG-5**), the manual OTC vol marks promoted to a
primary input by AMENDMENT v1.2 **T-1**, user levels, settings and an import
audit trail.

Two rules drive the design.

**Soft delete.**  ``delete_position`` stamps ``deleted_at`` and nothing else.
Requirements REQ-031 / section 5.6 are explicit: deleting a trade today must not
change what last week's P&L said, so a deleted row stays readable and every
historical reconstruction can ask for the book *as of* a moment.

**One validator, one grammar.**  The CSV importer (requirements section 3.5) and
the trade ticket in ``app/`` share :func:`parse_strike` and :func:`validate_row`,
so a strike typed into the ticket and the same strike in a file resolve
identically and are refused for identical reasons (rules V-1 ... V-21, section 3.4).

The public API is deliberately small::

    store = Store()                       # data/fxgamma.db, created on demand
    store.save_position(pos)
    book = store.load_book()
    store.delete_position("opt-0001")     # soft
    report = store.import_csv(text, ctx=MarketContext.from_snapshot(snap))
    csv_text = store.export_csv(book)
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .conventions import CUTS, PAIRS, TENORS, is_expired, pair_spec, tenor_years
from .types import Book, OptionPosition, Provenance, SpotPosition

log = logging.getLogger(__name__)

__all__ = [
    "Store", "get_store", "close_stores", "resolve_db_path", "MarketContext", "StrikeResolution", "parse_strike",
    "parse_notional", "ImportReport", "RowResult", "CSV_COLUMNS", "SCHEMA_VERSION",
    "MarkVol", "ManualQuote", "HedgeLogEntry", "ValidationMessage",
]

SCHEMA_VERSION = 3

#: Requirements section 3.5 columns, plus ``trade_time``: ``OptionPosition`` /
#: ``SpotPosition`` carry a UTC ``trade_time`` since AMENDMENT v1.1 CG-5 and
#: REQ-034 demands a field-by-field round trip, which the 19 documented columns
#: cannot deliver.  Files without it import unchanged.
CSV_COLUMNS = [
    "instrument_type", "id", "pair", "cp", "strike", "expiry", "cut",
    "notional_base", "direction", "premium_paid", "premium_ccy", "premium_unit",
    "trade_date", "trade_time", "trade_spot", "trade_vol", "entry_rate",
    "value_date", "tag", "notes",
]

PREMIUM_UNITS = ("total", "pips", "pct_base", "pct_quote")
_SUFFIX = {"k": 1e3, "m": 1e6, "mm": 1e6, "bn": 1e9, "b": 1e9}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ======================================================================== market context
@dataclass
class MarketContext:
    """The market inputs the ticket and the importer need to resolve and validate.

    Deliberately a plain snapshot-shaped bag rather than ``MarketSnapshot`` itself
    so validation can run with *partial* market data (or none at all): every rule
    that needs a number it does not have is skipped and says so, instead of
    inventing a spot and blocking a good row.
    """
    asof: datetime = field(default_factory=utcnow)
    spot: dict[str, float] = field(default_factory=dict)
    rates: dict[str, float] = field(default_factory=dict)
    surfaces: dict[str, Any] = field(default_factory=dict)
    forwards: dict[str, dict[float, float]] = field(default_factory=dict)
    meta: dict[str, Provenance] = field(default_factory=dict)

    @classmethod
    def from_snapshot(cls, snap: Any) -> "MarketContext":
        if snap is None:
            return cls()
        return cls(asof=snap.asof, spot=dict(snap.spot), rates=dict(snap.rates),
                   surfaces=dict(snap.surfaces), forwards=dict(snap.forwards),
                   meta=dict(snap.meta))

    # -- helpers ---------------------------------------------------------
    def rd_rf(self, pair: str) -> tuple[float, float] | None:
        spec = pair_spec(pair)
        if spec.quote not in self.rates or spec.base not in self.rates:
            return None
        return self.rates[spec.quote], self.rates[spec.base]

    def forward(self, pair: str, T: float) -> float | None:
        S = self.spot.get(pair.upper())
        rr = self.rd_rf(pair)
        if S is None or rr is None or T is None:
            return None
        rd, rf = rr
        return S * math.exp((rd - rf) * max(T, 0.0))

    def atm_vol(self, pair: str, T: float) -> float | None:
        surf = self.surfaces.get(pair.upper())
        if surf is None or T is None or T <= 0:
            return None
        try:
            return float(surf.atm(T))
        except Exception:                                  # noqa: BLE001
            return None

    def vol_at(self, pair: str, K: float, T: float) -> float | None:
        surf = self.surfaces.get(pair.upper())
        if surf is None or T is None or T <= 0:
            return None
        try:
            v = float(surf.vol(K, T))
            return v if math.isfinite(v) else None
        except Exception:                                  # noqa: BLE001
            return None


# ======================================================================== strike grammar
@dataclass(frozen=True)
class StrikeResolution:
    """The resolved strike **and** everything needed to echo it back (section 3.3).

    The requirement is that the ticket, the importer and the what-if pricer never
    resolve a strike without showing the trader the absolute number, the delta it
    corresponds to, and the *name* of the delta convention used.
    """
    strike: float
    source: str                 # "absolute" | "atm" | "atmf" | "atmdn" | "delta" | "pips" | "pct"
    text: str = ""
    delta: float | None = None
    convention: str = ""
    detail: str = ""


class _UnattainableDelta(Exception):
    """Internal: the requested delta has no strike (AMENDMENT v1.7). Re-raised as
    ``ValueError`` with a message written to be shown to the user verbatim."""


_RE_DELTA = re.compile(r"^([0-9]{1,2}(?:\.[0-9]+)?)\s*d\s*([cp])$", re.I)
_RE_PIPS = re.compile(r"^(f)?\s*([+-])\s*([0-9]+(?:\.[0-9]+)?)\s*p$", re.I)
_RE_PCT = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*%$")


def parse_strike(text: Any, pair: str, ctx: MarketContext | None = None, *,
                 T: float | None = None, expiry: date | None = None,
                 cut: str | None = None) -> StrikeResolution:
    """Resolve the section 3.3 strike grammar to an absolute strike.

    ``1.0850`` ``ATM`` ``ATMF`` ``ATMDN`` ``25dc`` ``25DP`` ``+50p`` ``-30p``
    ``F+100p`` ``101%``.

    Delta and forward inputs are **refused** when the surface or the rates they
    need are missing, rather than falling back to a flat vol or to spot (section 3.3,
    contract section 7).  Raises ``ValueError`` with a message fit to show the user.
    """
    pair = str(pair or "").upper()
    spec = pair_spec(pair)
    raw = str(text if text is not None else "").strip()
    if not raw:
        raise ValueError("strike is required")
    ctx = ctx or MarketContext()
    if T is None and expiry is not None:
        T = year_fraction_ctx(ctx, expiry, cut or spec.cut)
    S = ctx.spot.get(pair)

    def _need_spot() -> float:
        if S is None:
            raise ValueError(f"no spot for {pair} in this snapshot; type an absolute strike")
        return float(S)

    low = raw.lower().replace(" ", "")

    # absolute -----------------------------------------------------------
    try:
        val = float(raw.replace(",", ""))
        return StrikeResolution(val, "absolute", raw, detail="as typed",
                                delta=_delta_of(val, pair, ctx, T), convention=_conv(pair))
    except ValueError:
        pass

    if low in ("atm", "atms"):
        v = _need_spot()
        return StrikeResolution(v, "atm", raw, detail="at-the-money spot",
                                delta=_delta_of(v, pair, ctx, T), convention=_conv(pair))

    if low == "atmf":
        v = ctx.forward(pair, T) if T is not None else None
        if v is None:
            raise ValueError("ATMF needs spot, both rates and an expiry; none in this snapshot")
        return StrikeResolution(v, "atmf", raw, detail=f"forward to T={T:.4f}y",
                                delta=_delta_of(v, pair, ctx, T), convention=_conv(pair))

    if low in ("atmdn", "dns", "atmdns"):
        v = _atm_dn_strike(pair, ctx, T)
        if v is None:
            raise ValueError("ATMDN needs a surface for this pair/tenor")
        return StrikeResolution(v, "atmdn", raw, detail="delta-neutral straddle strike",
                                delta=_delta_of(v, pair, ctx, T), convention=_conv(pair))

    m = _RE_DELTA.match(low)
    if m:
        d = float(m.group(1)) / 100.0
        cp = +1 if m.group(2).lower() == "c" else -1
        if not 0.0 < d < 1.0:
            raise ValueError(f"delta {m.group(1)} out of range")
        surf = ctx.surfaces.get(pair)
        rr = ctx.rd_rf(pair)
        if surf is None or rr is None or S is None or T is None or T <= 0:
            raise ValueError(
                f"{raw}: no surface/rates for {pair} at this tenor - a delta strike cannot be "
                "resolved without one (section 3.3 forbids a flat-vol fallback)")
        from .models import gk
        conv = _conv(pair)
        try:
            sig = float(surf.vol_by_delta(d, T, cp))
            if not math.isfinite(sig) or sig <= 0.0:
                # A surface that inverts delta -> vol can itself fail on an
                # unattainable delta and hand back nan.  The bound below still has to
                # be computed and reported, so anchor it on the ATM vol for this tenor
                # rather than letting a nan quietly disable the check (which is how the
                # user ends up with "did not converge" instead of a straight answer).
                sig = float(surf.atm(T))
            # AMENDMENT v1.7 (binding): premium-adjusted CALL delta is bounded, and the
            # bound sits below common quoting deltas at long tenors / high vols (the PM
            # verified 0.8745 at 3M/10% but only 0.2764 at 5Y/40%).  On USDJPY, USDCHF,
            # USDCAD, USDSEK and USDNOK a "30-delta call" can simply not exist, and
            # `strike_from_delta` correctly returns nan there.  Ask for the maximum
            # BEFORE solving and refuse in words, so the ticket can never show a blank,
            # a zero or a nan strike for a delta that has no strike.
            mx = float(gk.max_attainable_delta(float(S), T, rr[0], rr[1], sig, cp, conv))
            if math.isfinite(mx) and d > mx:
                raise _UnattainableDelta(
                    f"{raw}: unattainable at this tenor/vol. The largest {conv} "
                    f"{'call' if cp > 0 else 'put'} delta any {pair} strike reaches at "
                    f"T={T:.4f}y and {sig * 100:.2f}% vol is {mx * 100:.2f}% "
                    f"(premium-adjusted delta is bounded). Enter a delta below "
                    f"{mx * 100:.1f} or an absolute strike.")
            K = float(gk.strike_from_delta(d, float(S), T, rr[0], rr[1], sig, cp, conv))
            for _ in range(3):                       # tighten: vol depends on K
                sig = float(surf.vol(K, T))
                mx = float(gk.max_attainable_delta(float(S), T, rr[0], rr[1], sig, cp, conv))
                if math.isfinite(mx) and d > mx:
                    raise _UnattainableDelta(
                        f"{raw}: unattainable at this tenor/vol. At the solved strike's "
                        f"own vol ({sig * 100:.2f}%) the largest attainable {conv} delta "
                        f"is {mx * 100:.2f}%. Enter a lower delta or an absolute strike.")
                K = float(gk.strike_from_delta(d, float(S), T, rr[0], rr[1], sig, cp, conv))
        except _UnattainableDelta as exc:
            raise ValueError(str(exc)) from None
        except Exception as exc:                     # noqa: BLE001
            raise ValueError(f"{raw}: delta solve failed ({exc})") from exc
        if not math.isfinite(K):
            # belt and braces: the bound above should have caught this, but a nan
            # strike must never reach the ticket unexplained.
            vtxt = f"{sig * 100:.2f}%" if math.isfinite(sig) else "unavailable"
            raise ValueError(
                f"{raw}: unattainable at this tenor/vol — the delta solve returned no "
                f"strike ({conv} convention, vol {vtxt}). Premium-adjusted call delta "
                f"is bounded; enter a lower delta or an absolute strike.")
        return StrikeResolution(K, "delta", raw, delta=d * cp, convention=conv,
                                detail=f"{m.group(1)}d {'call' if cp > 0 else 'put'} "
                                       f"@ {sig * 100:.2f}% vol, {conv} convention")

    m = _RE_PIPS.match(low)
    if m:
        base_is_fwd = bool(m.group(1))
        sign = 1.0 if m.group(2) == "+" else -1.0
        n = float(m.group(3))
        if base_is_fwd:
            anchor = ctx.forward(pair, T) if T is not None else None
            if anchor is None:
                raise ValueError("F+/-Np needs spot, both rates and an expiry")
            what = "forward"
        else:
            anchor = _need_spot()
            what = "spot"
        K = anchor + sign * n * spec.pip
        return StrikeResolution(K, "pips", raw, delta=_delta_of(K, pair, ctx, T),
                                convention=_conv(pair),
                                detail=f"{what} {anchor:.5f} {m.group(2)} {n:g} pips "
                                       f"(pip={spec.pip:g})")

    m = _RE_PCT.match(low)
    if m:
        pct = float(m.group(1))
        K = _need_spot() * pct / 100.0
        return StrikeResolution(K, "pct", raw, delta=_delta_of(K, pair, ctx, T),
                                convention=_conv(pair), detail=f"{pct:g}% of spot {S:.5f}")

    raise ValueError(
        f"cannot read strike {raw!r}. Use an absolute number, ATM, ATMF, ATMDN, "
        "25dc / 25dp, +50p / -30p, F+100p or 101%")


def _conv(pair: str) -> str:
    return pair_spec(pair).delta_convention


def _delta_of(K: float, pair: str, ctx: MarketContext, T: float | None,
              cp: int = 1) -> float | None:
    """Delta of a call at K, in the pair's own convention. ``None`` when unknowable."""
    rr = ctx.rd_rf(pair)
    S = ctx.spot.get(pair.upper())
    if rr is None or S is None or T is None or T <= 0:
        return None
    sig = ctx.vol_at(pair, K, T)
    if sig is None:
        return None
    try:
        from .models import gk
        d = float(gk.delta_from_strike(K, float(S), T, rr[0], rr[1], sig, cp, _conv(pair)))
        return d if math.isfinite(d) else None
    except Exception:                                      # noqa: BLE001
        return None


def _atm_dn_strike(pair: str, ctx: MarketContext, T: float | None) -> float | None:
    surf = ctx.surfaces.get(pair.upper())
    if surf is None or T is None or T <= 0:
        return None
    fn = getattr(surf, "atm_strike", None)
    if callable(fn):
        try:
            v = float(fn(T))
            if math.isfinite(v):
                return v
        except Exception:                                  # noqa: BLE001
            pass
    F = ctx.forward(pair, T)
    sig = ctx.atm_vol(pair, T)
    if F is None or sig is None:
        return None
    pa = _conv(pair).endswith("_pa")
    return F * math.exp((-0.5 if pa else 0.5) * sig * sig * T)


def year_fraction_ctx(ctx: MarketContext, expiry: date, cut: str) -> float | None:
    from .conventions import year_fraction
    if expiry is None:
        return None
    try:
        return year_fraction(ctx.asof, expiry, cut)
    except Exception:                                      # noqa: BLE001
        return None


def parse_notional(text: Any) -> float:
    """``"10mm"`` -> 10_000_000.  Accepts k / m / mm / bn / b, commas, spaces, sign."""
    if text is None or (isinstance(text, float) and math.isnan(text)):
        raise ValueError("notional is required")
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().lower().replace(",", "").replace(" ", "").replace("_", "")
    if not s:
        raise ValueError("notional is required")
    for suf in ("mm", "bn", "m", "k", "b"):
        if s.endswith(suf):
            body = s[: -len(suf)]
            try:
                return float(body) * _SUFFIX[suf]
            except ValueError as exc:
                raise ValueError(f"cannot read notional {text!r}") from exc
    try:
        return float(s)
    except ValueError as exc:
        raise ValueError(f"cannot read notional {text!r}") from exc


def parse_cp(text: Any) -> int:
    s = str(text).strip().upper()
    if s in ("C", "CALL", "1", "+1"):
        return 1
    if s in ("P", "PUT", "-1"):
        return -1
    raise ValueError(f"cannot read call/put {text!r}")


def parse_direction(text: Any) -> int:
    s = str(text).strip().upper()
    if s in ("B", "BUY", "1", "+1", "LONG", "L"):
        return 1
    if s in ("S", "SELL", "-1", "SHORT"):
        return -1
    raise ValueError(f"cannot read direction {text!r}")


def parse_date(text: Any) -> date | None:
    if text is None or str(text).strip() == "":
        return None
    s = str(text).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"cannot read date {text!r} (use YYYY-MM-DD)")


def parse_dt(text: Any) -> datetime | None:
    if text is None or str(text).strip() == "":
        return None
    s = str(text).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"cannot read timestamp {text!r} (ISO-8601)") from exc
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ======================================================================== validation
@dataclass(frozen=True)
class ValidationMessage:
    rule: str            # "V-4"
    severity: str        # "E" | "W"
    text: str
    field: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "severity": self.severity, "text": self.text,
                "field": self.field}


@dataclass
class RowResult:
    row_no: int
    raw: dict[str, str] = field(default_factory=dict)
    messages: list[ValidationMessage] = field(default_factory=list)
    position: Any = None
    notes: str = ""
    unknown_columns: dict[str, str] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if any(m.severity == "E" for m in self.messages):
            return "ERROR"
        return "WARN" if self.messages else "OK"

    @property
    def reason(self) -> str:
        return " | ".join(f"[{m.rule}] {m.text}" for m in self.messages)


@dataclass
class ImportReport:
    rows: list[RowResult] = field(default_factory=list)
    fatal: str = ""
    mode: str = "append"
    committed: int = 0
    header: list[str] = field(default_factory=list)

    @property
    def n_ok(self) -> int:
        return sum(1 for r in self.rows if r.status == "OK")

    @property
    def n_warn(self) -> int:
        return sum(1 for r in self.rows if r.status == "WARN")

    @property
    def n_error(self) -> int:
        return sum(1 for r in self.rows if r.status == "ERROR")

    @property
    def can_commit(self) -> bool:
        return not self.fatal and bool(self.rows) and self.n_error == 0

    def rejects_csv(self) -> str:
        """The section 3.5 schema plus a ``reason`` column - fix and re-upload."""
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS + ["status", "reason"],
                           extrasaction="ignore")
        w.writeheader()
        for r in self.rows:
            if r.status == "OK":
                continue
            w.writerow({**{c: r.raw.get(c, "") for c in CSV_COLUMNS},
                        "status": r.status, "reason": r.reason})
        return buf.getvalue()

    def preview_records(self) -> list[dict[str, Any]]:
        out = []
        for r in self.rows:
            rec = {"row": r.row_no, "status": r.status,
                   "reason": r.reason or "-"}
            rec.update({c: r.raw.get(c, "") for c in CSV_COLUMNS})
            out.append(rec)
        return out


def _norm_header(name: str) -> str:
    return re.sub(r"[\s_]+", "", str(name or "")).strip().lower()


_HEADER_ALIASES = {_norm_header(c): c for c in CSV_COLUMNS}
_HEADER_ALIASES.update({
    "type": "instrument_type", "instrument": "instrument_type",
    "callput": "cp", "putcall": "cp", "k": "strike", "expirydate": "expiry",
    "notional": "notional_base", "side": "direction", "buysell": "direction",
    "premium": "premium_paid", "premiumccy": "premium_ccy",
    "premiumunit": "premium_unit", "rate": "entry_rate", "tradetime": "trade_time",
})


def validate_row(raw: dict[str, Any], ctx: MarketContext | None = None, *,
                 row_no: int = 0, allow_expired: bool = False,
                 existing_ids: set[str] | None = None, upsert: bool = False,
                 next_id=None) -> RowResult:
    """Apply rules **V-1 ... V-21** (requirements section 3.4) to one CSV/ticket row.

    Returns a :class:`RowResult`; ``E`` messages block the write, ``W`` messages
    require an explicit acknowledgement.  Market-dependent rules are skipped, with
    a note, when the snapshot does not carry the input they need - a missing spot
    must never turn a good row into a rejected one, nor silently pass a bad one.
    """
    ctx = ctx or MarketContext()
    res = RowResult(row_no=row_no, raw={k: ("" if v is None else str(v)) for k, v in raw.items()})
    msg = res.messages.append

    def E(rule: str, text: str, fld: str = "") -> None:
        msg(ValidationMessage(rule, "E", text, fld))

    def W(rule: str, text: str, fld: str = "") -> None:
        msg(ValidationMessage(rule, "W", text, fld))

    g = lambda k: str(raw.get(k, "") or "").strip()           # noqa: E731

    itype = g("instrument_type").upper() or ("SPOT" if g("entry_rate") else "OPTION")
    if itype not in ("OPTION", "SPOT"):
        E("V-0", f"instrument_type must be OPTION or SPOT, got {itype!r}", "instrument_type")
        return res

    # V-1 pair --------------------------------------------------------------
    pair = g("pair").upper()
    if pair not in PAIRS:
        E("V-1", f"unknown pair {pair or '(blank)'!r}; known: {', '.join(sorted(PAIRS))}", "pair")
        return res
    spec = pair_spec(pair)
    S = ctx.spot.get(pair)

    # notional --------------------------------------------------------------
    try:
        notional = parse_notional(g("notional_base"))
    except ValueError as exc:
        E("V-8", str(exc), "notional_base")
        return res

    pid = g("id")
    if pid and existing_ids and pid in existing_ids and not upsert:
        E("V-18", f"id {pid} already exists; import with 'upsert by id' or clear the id", "id")

    tag = g("tag")
    notes = g("notes")
    res.notes = notes
    trade_date = trade_time = None
    try:
        trade_date = parse_date(g("trade_date"))
    except ValueError as exc:
        E("V-0", str(exc), "trade_date")
    try:
        trade_time = parse_dt(g("trade_time"))
    except ValueError as exc:
        E("V-0", str(exc), "trade_time")
    trade_spot = None
    if g("trade_spot"):
        try:
            trade_spot = float(g("trade_spot").replace(",", ""))
        except ValueError:
            E("V-0", f"cannot read trade_spot {g('trade_spot')!r}", "trade_spot")

    # V-17 trade spot sanity -------------------------------------------------
    if trade_spot is not None and S:
        if abs(trade_spot / S - 1.0) > 0.20:
            W("V-17", f"trade spot {trade_spot:g} is far from today's {S:g} "
                      f"({(trade_spot / S - 1) * 100:+.1f}%)", "trade_spot")

    # ------------------------------------------------------------------ SPOT
    if itype == "SPOT":
        if notional == 0:
            E("V-8", "spot notional must be non-zero (sign: + = long base)", "notional_base")
        if abs(notional) < 1000:
            W("V-9", f"notional {notional:g} - did you mean mm? use the mm/bn suffix",
              "notional_base")
        if not g("entry_rate"):
            E("V-0", "entry_rate is required for a SPOT row", "entry_rate")
            return res
        try:
            entry = float(g("entry_rate").replace(",", ""))
        except ValueError:
            E("V-0", f"cannot read entry_rate {g('entry_rate')!r}", "entry_rate")
            return res
        if entry <= 0:
            E("V-0", "entry_rate must be positive", "entry_rate")
        if S and abs(entry / S - 1.0) > 0.20:
            W("V-19", f"entry rate {entry:g} is {abs(entry / S - 1) * 100:.1f}% from live "
                      f"spot {S:g}", "entry_rate")
        value_date = None
        try:
            value_date = parse_date(g("value_date"))
        except ValueError as exc:
            E("V-0", str(exc), "value_date")
        if not any(m.severity == "E" for m in res.messages):
            res.position = SpotPosition(
                id=pid or (next_id("spt") if next_id else _rand_id("spt")),
                pair=pair, notional_base=notional, entry_rate=entry,
                trade_date=trade_date, value_date=value_date, tag=tag,
                trade_time=trade_time)
        return res

    # ---------------------------------------------------------------- OPTION
    if notional <= 0:
        E("V-8", "notional must be positive; the sign lives in Direction", "notional_base")
    if 0 < abs(notional) < 1000:
        W("V-9", f"notional {notional:g} - did you mean 10mm? use the mm/bn suffix",
          "notional_base")

    try:
        cp = parse_cp(g("cp"))
    except ValueError as exc:
        E("V-11", str(exc), "cp")
        cp = 0
    try:
        direction = parse_direction(g("direction"))
    except ValueError as exc:
        E("V-12", str(exc), "direction")
        direction = 0

    cut = g("cut").upper() or spec.cut
    if cut not in CUTS:
        E("V-13", f"cut {cut!r} is not one of {', '.join(CUTS)}", "cut")
        cut = spec.cut

    expiry = None
    try:
        expiry = parse_date(g("expiry"))
    except ValueError as exc:
        E("V-0", str(exc), "expiry")
    if expiry is None:
        E("V-0", "expiry is required for an OPTION row", "expiry")

    T = None
    if expiry is not None:
        T = year_fraction_ctx(ctx, expiry, cut)
        # V-2 expiry in the past
        if is_expired(ctx.asof, expiry, cut):
            if allow_expired:
                W("V-2", f"expiry {expiry} has passed the {cut} cut; loaded as history", "expiry")
            else:
                E("V-2", f"expiry {expiry} is in the past (the {cut} cut has passed)", "expiry")
        # V-3 beyond the 2Y grid
        elif (expiry - ctx.asof.date()).days > 730:
            W("V-3", f"expiry {expiry} is beyond the v1 tenor grid (2Y); the surface "
                     "will extrapolate", "expiry")

    # strike, through the shared grammar ------------------------------------
    strike = None
    strike_text = g("strike")
    if not strike_text:
        E("V-0", "strike is required for an OPTION row", "strike")
    else:
        try:
            r = parse_strike(strike_text, pair, ctx, T=T, expiry=expiry, cut=cut)
            strike = r.strike
            if r.source != "absolute":
                res.raw["strike_resolved"] = f"{strike:.6g} ({r.detail})"
        except ValueError as exc:
            E("V-0", str(exc), "strike")

    if strike is not None:
        jpy = spec.quote == "JPY"
        if jpy and strike < 10:                                    # V-6
            E("V-6", f"{pair} strike must be in JPY terms (e.g. 147.25), got {strike:g}",
              "strike")
        if not jpy and strike > 20:                                # V-7
            E("V-7", f"{pair} strike must be in the 0.2-5 range, got {strike:g}", "strike")
        if S:
            if not (0.2 * S <= strike <= 5.0 * S):                 # V-4
                E("V-4", f"strike {strike:g} is implausible vs spot {S:g} - unit error?",
                  "strike")
            sig = ctx.atm_vol(pair, T) if T else None
            if sig and T and T > 0:                                # V-5
                sd = abs(math.log(strike / S)) / (sig * math.sqrt(T))
                if sd > 4.0:
                    W("V-5", f"strike is {sd:.1f} standard deviations from spot; confirm",
                      "strike")

    # trade vol, V-10 --------------------------------------------------------
    trade_vol = None
    if g("trade_vol"):
        try:
            v = float(g("trade_vol").replace("%", "").replace(",", ""))
        except ValueError:
            E("V-10", f"cannot read trade_vol {g('trade_vol')!r}", "trade_vol")
            v = None
        if v is not None:
            if v > 1.0:
                W("V-10", f"read {v:g} as {v:g}% = {v / 100:.4f}", "trade_vol")
                v /= 100.0
            if not (0.005 <= v <= 2.0):
                E("V-10", f"vol {v:.4f} out of range (0.5%..200%)", "trade_vol")
            else:
                trade_vol = v

    # premium ---------------------------------------------------------------
    premium_ccy = g("premium_ccy").upper() or spec.quote
    if premium_ccy not in (spec.base, spec.quote):                 # V-14
        E("V-14", f"premium ccy {premium_ccy} is not a leg of {pair} "
                  f"({spec.base}/{spec.quote})", "premium_ccy")
    unit = (g("premium_unit") or "total").lower()
    if unit not in PREMIUM_UNITS:
        E("V-0", f"premium_unit must be one of {', '.join(PREMIUM_UNITS)}", "premium_unit")
        unit = "total"
    premium_paid = 0.0
    if g("premium_paid"):
        try:
            premium_raw = parse_notional(g("premium_paid"))
        except ValueError as exc:
            E("V-0", str(exc), "premium_paid")
            premium_raw = 0.0
        s_trade = trade_spot if trade_spot else S
        if unit == "total":
            premium_paid = premium_raw
        elif unit == "pips":
            premium_paid = premium_raw * abs(notional) * spec.pip
        elif unit == "pct_base":
            # % of the BASE notional -> an amount of base ccy -> convert at SPOT.
            if not s_trade:
                E("V-0", f"premium_unit={unit} needs trade_spot or a snapshot spot for {pair}",
                  "premium_unit")
            else:
                premium_paid = premium_raw / 100.0 * abs(notional) * s_trade
        else:                                    # pct_quote (section 3.5, PM rev-3 fix)
            # % of the QUOTE notional, which by FX convention is N_base x K, so this
            # one converts at the STRIKE, not at spot.  Rev 2 of the requirements gave
            # both units the same formula, which made them indistinguishable; the two
            # differ by S/K and agree only for a spot-struck option, which is exactly
            # why an ATM worked example never caught it.  Raised by dev, ruled by the
            # PM (docs/02_requirements.md section 3.5, "Correction (PM, rev 3)").
            if strike is None:
                E("V-0", "premium_unit=pct_quote needs a resolvable strike (the quote "
                         "notional is notional_base x strike)", "premium_unit")
            else:
                premium_paid = premium_raw / 100.0 * abs(notional) * float(strike)
        if direction and premium_paid and (premium_paid > 0) != (direction > 0):   # V-15
            W("V-15", "sign check: you "
                      f"{'sold' if direction < 0 else 'bought'} this option but recorded a "
                      f"{'debit' if premium_paid > 0 else 'credit'} premium "
                      "(debit > 0 = you paid)", "premium_paid")
        if S and premium_paid:                                                     # V-16
            quote_notional = abs(notional) * (s_trade or S)
            prem_q = premium_paid if premium_ccy == spec.quote else premium_paid * (s_trade or S)
            share = abs(prem_q) / quote_notional if quote_notional else 0.0
            if share > 0.25:
                W("V-16", f"premium is {share * 100:.0f}% of notional - unit error?",
                  "premium_paid")
        # W-12(b): the check that actually catches premium-unit errors - back out the
        # implied vol from the entered premium and compare it with the vol the row
        # itself claims.  It only runs on a *fresh* trade: for a trade booked in the
        # past, T today is shorter than T at execution, so the back-out would compare
        # two different things and cry wolf on every historical import.  Against a
        # stated `trade_vol` it is an internal-consistency check and blocks; against
        # today's surface it can only warn, because the market has moved since.
        fresh = (trade_date is None or trade_date == ctx.asof.date())
        if fresh and strike is not None and T and T > 0 and S and premium_paid:
            iv = _implied_from_premium(pair, ctx, strike, T, cp, notional, premium_paid,
                                       premium_ccy, s_trade or S)
            ref, ref_name, blocking = (trade_vol, "the trade vol on this row", True) \
                if trade_vol else (ctx.vol_at(pair, strike, T), "the surface", False)
            if iv is not None and ref is not None:
                diff = abs(iv - ref) * 100.0
                text = (f"premium implies {iv * 100:.2f}% vol vs {ref_name} "
                        f"{ref * 100:.2f}% ({diff:.2f} vol pts)")
                if diff > 2.0 and blocking:
                    E("V-16b", text + " - check the premium unit and ccy", "premium_paid")
                elif diff > 0.5:
                    W("V-16b", text, "premium_paid")

    if not any(m.severity == "E" for m in res.messages):
        res.position = OptionPosition(
            id=pid or (next_id("opt") if next_id else _rand_id("opt")),
            pair=pair, cp=cp, strike=float(strike), expiry=expiry,
            notional_base=abs(notional), direction=direction,
            premium_paid=premium_paid, premium_ccy=premium_ccy,
            trade_date=trade_date, trade_spot=trade_spot, trade_vol=trade_vol,
            cut=cut, tag=tag, trade_time=trade_time)
    return res


def _implied_from_premium(pair: str, ctx: MarketContext, K: float, T: float, cp: int,
                          notional: float, premium_total: float, premium_ccy: str,
                          s_trade: float) -> float | None:
    """Vol implied by an entered premium (trader review W-12 / MISS-5 reconcile box)."""
    spec = pair_spec(pair)
    rr = ctx.rd_rf(pair)
    S = ctx.spot.get(pair)
    if rr is None or not S or not cp or T <= 0 or not notional:
        return None
    prem_q = abs(premium_total)
    if premium_ccy == spec.base:
        prem_q *= s_trade
    per_unit = prem_q / abs(notional)
    try:
        from .models import gk
        v = float(gk.implied_vol(per_unit, float(s_trade or S), K, T, rr[0], rr[1], cp))
        return v if math.isfinite(v) and 0.0 < v < 5.0 else None
    except Exception:                                      # noqa: BLE001
        return None


def _rand_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ======================================================================== side tables
@dataclass(frozen=True)
class MarkVol:
    """AMENDMENT v1.1 CG-2: per-position mark vol, side table, no type change."""
    id: str
    mark_vol: float
    mark_source: str = "user"
    asof: datetime | None = None
    note: str = ""


@dataclass(frozen=True)
class ManualQuote:
    """AMENDMENT v1.2 T-1: one tenor of the trader's own morning mark."""
    pair: str
    tenor: str
    atm: float
    rr25: float = 0.0
    bf25: float = 0.0
    rr10: float | None = None
    bf10: float | None = None
    asof: datetime | None = None
    source: str = "user"
    note: str = ""

    @property
    def T(self) -> float:
        return tenor_years(self.tenor)


@dataclass(frozen=True)
class HedgeLogEntry:
    id: str
    pair: str
    notional_base: float
    entry_rate: float
    trade_time: datetime | None
    trade_date: date | None
    tag: str = ""
    note: str = ""


# ======================================================================== the store
_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS option_positions (
    id            TEXT PRIMARY KEY,
    book          TEXT NOT NULL DEFAULT 'default',
    pair          TEXT NOT NULL,
    cp            INTEGER NOT NULL,
    strike        REAL NOT NULL,
    expiry        TEXT NOT NULL,
    notional_base REAL NOT NULL,
    direction     INTEGER NOT NULL,
    premium_paid  REAL NOT NULL DEFAULT 0.0,
    premium_ccy   TEXT NOT NULL DEFAULT '',
    trade_date    TEXT,
    trade_spot    REAL,
    trade_vol     REAL,
    cut           TEXT NOT NULL DEFAULT 'NY10',
    tag           TEXT NOT NULL DEFAULT '',
    trade_time    TEXT,
    notes         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    deleted_at    TEXT
);

CREATE TABLE IF NOT EXISTS spot_positions (
    id            TEXT PRIMARY KEY,
    book          TEXT NOT NULL DEFAULT 'default',
    pair          TEXT NOT NULL,
    notional_base REAL NOT NULL,
    entry_rate    REAL NOT NULL,
    trade_date    TEXT,
    value_date    TEXT,
    tag           TEXT NOT NULL DEFAULT '',
    trade_time    TEXT,
    notes         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    deleted_at    TEXT
);

-- AMENDMENT v1.1 CG-2
CREATE TABLE IF NOT EXISTS position_marks (
    id          TEXT PRIMARY KEY,
    mark_vol    REAL NOT NULL,
    mark_source TEXT NOT NULL DEFAULT 'user',
    asof        TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);

-- AMENDMENT v1.1 CG-5: ordered by trade_time, falling back to trade_date
CREATE TABLE IF NOT EXISTS hedge_log (
    id            TEXT PRIMARY KEY,
    book          TEXT NOT NULL DEFAULT 'default',
    pair          TEXT NOT NULL,
    notional_base REAL NOT NULL,
    entry_rate    REAL NOT NULL,
    trade_time    TEXT,
    trade_date    TEXT,
    tag           TEXT NOT NULL DEFAULT 'hedge',
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    deleted_at    TEXT
);

-- AMENDMENT v1.2 T-1: the trader's own vol marks. Versioned: a new paste inserts
-- new rows, the latest asof per (pair,tenor) is the live mark, history is kept so
-- a stored P&L can be re-marked on the curve that was in force at the time.
CREATE TABLE IF NOT EXISTS manual_vol_quotes (
    pair    TEXT NOT NULL,
    tenor   TEXT NOT NULL,
    asof    TEXT NOT NULL,
    atm     REAL NOT NULL,
    rr25    REAL NOT NULL DEFAULT 0.0,
    bf25    REAL NOT NULL DEFAULT 0.0,
    rr10    REAL,
    bf10    REAL,
    source  TEXT NOT NULL DEFAULT 'user',
    note    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (pair, tenor, asof)
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS levels (
    id      TEXT PRIMARY KEY,
    pair    TEXT NOT NULL,
    level   REAL NOT NULL,
    label   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS import_audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    mode      TEXT NOT NULL,
    n_ok      INTEGER NOT NULL,
    n_warn    INTEGER NOT NULL,
    n_error   INTEGER NOT NULL,
    committed INTEGER NOT NULL,
    detail    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_opt_book ON option_positions(book, deleted_at);
CREATE INDEX IF NOT EXISTS ix_spt_book ON spot_positions(book, deleted_at);
CREATE INDEX IF NOT EXISTS ix_hedge_time ON hedge_log(book, trade_time);
CREATE INDEX IF NOT EXISTS ix_mvq ON manual_vol_quotes(pair, tenor, asof);
"""


def _iso(x: Any) -> str | None:
    if x is None:
        return None
    if isinstance(x, datetime):
        return (x if x.tzinfo else x.replace(tzinfo=timezone.utc)).isoformat()
    if isinstance(x, date):
        return x.isoformat()
    return str(x)


def _as_date(x: Any) -> date | None:
    return None if x in (None, "") else date.fromisoformat(str(x)[:10])


def _as_dt(x: Any) -> datetime | None:
    if x in (None, ""):
        return None
    d = datetime.fromisoformat(str(x))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class Store:
    """SQLite persistence for the book. Thread-safe; every write is a transaction."""

    DEFAULT_PATH = "data/fxgamma.db"

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path or os.environ.get("FXGAMMA_DB") or self.DEFAULT_PATH)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # ------------------------------------------------------------- plumbing
    @contextmanager
    def _tx(self):
        """One transaction, however deeply nested (QA finding F-12).

        ``_tx`` is re-entrant: ``save_positions`` opens a block and calls
        ``save_position``, which opens another.  Committing on *every* exit meant row 1
        was already committed by the time row 2 raised, so the outer rollback had
        nothing to undo and the user was left with a half-imported book and no audit
        row -- neither of the two states the preview gate described.  The depth counter
        below makes the outermost block the only one that commits or rolls back, which
        is what both docstrings ("all rows or none") already promised.

        The lock is re-entrant (``RLock``), so nesting is safe; ``_failed`` marks a
        transaction poisoned so an inner ``except`` cannot accidentally commit a
        partially applied outer one.
        """
        with self._lock:
            self._depth = getattr(self, "_depth", 0) + 1
            outermost = self._depth == 1
            if outermost:
                self._failed = False
            try:
                yield self._conn
            except Exception:
                self._failed = True
                raise
            finally:
                self._depth -= 1
                if self._depth == 0:
                    if self._failed:
                        self._conn.rollback()
                    else:
                        self._conn.commit()

    def _migrate(self) -> None:
        with self._tx() as c:
            c.executescript(_DDL)
            row = c.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            if row is None:
                c.execute("INSERT INTO schema_meta(key, value) VALUES('version', ?)",
                          (str(SCHEMA_VERSION),))
            else:
                have = int(row["value"])
                if have > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"{self.path} was written by schema v{have}; this build speaks "
                        f"v{SCHEMA_VERSION}. Refusing to read it rather than risk a partial "
                        "read (requirements section 5.6).")
                if have < SCHEMA_VERSION:
                    c.execute("UPDATE schema_meta SET value=? WHERE key='version'",
                              (str(SCHEMA_VERSION),))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- ids
    def next_id(self, prefix: str = "opt") -> str:
        table = "option_positions" if prefix == "opt" else "spot_positions"
        with self._lock:
            rows = self._conn.execute(f"SELECT id FROM {table} WHERE id LIKE ?",
                                      (f"{prefix}-%",)).fetchall()
        n = 0
        for r in rows:
            m = re.match(rf"{prefix}-(\d+)$", r["id"])
            if m:
                n = max(n, int(m.group(1)))
        return f"{prefix}-{n + 1:04d}"

    def existing_ids(self) -> set[str]:
        with self._lock:
            a = self._conn.execute("SELECT id FROM option_positions").fetchall()
            b = self._conn.execute("SELECT id FROM spot_positions").fetchall()
        return {r["id"] for r in a} | {r["id"] for r in b}

    # ------------------------------------------------------------- positions
    def save_position(self, pos: OptionPosition | SpotPosition, *, book: str = "default",
                      notes: str = "") -> str:
        """Insert or update one position. Returns its id."""
        now = _iso(utcnow())
        with self._tx() as c:
            if isinstance(pos, OptionPosition):
                c.execute("""
                    INSERT INTO option_positions
                      (id, book, pair, cp, strike, expiry, notional_base, direction,
                       premium_paid, premium_ccy, trade_date, trade_spot, trade_vol,
                       cut, tag, trade_time, notes, created_at, updated_at, deleted_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                    ON CONFLICT(id) DO UPDATE SET
                      book=excluded.book, pair=excluded.pair, cp=excluded.cp,
                      strike=excluded.strike, expiry=excluded.expiry,
                      notional_base=excluded.notional_base, direction=excluded.direction,
                      premium_paid=excluded.premium_paid, premium_ccy=excluded.premium_ccy,
                      trade_date=excluded.trade_date, trade_spot=excluded.trade_spot,
                      trade_vol=excluded.trade_vol, cut=excluded.cut, tag=excluded.tag,
                      trade_time=excluded.trade_time, notes=excluded.notes,
                      updated_at=excluded.updated_at
                """, (pos.id, book, pos.pair, int(pos.cp), float(pos.strike),
                      _iso(pos.expiry), float(pos.notional_base), int(pos.direction),
                      float(pos.premium_paid), pos.premium_ccy, _iso(pos.trade_date),
                      pos.trade_spot, pos.trade_vol, pos.cut, pos.tag,
                      _iso(pos.trade_time), notes, now, now))
            elif isinstance(pos, SpotPosition):
                c.execute("""
                    INSERT INTO spot_positions
                      (id, book, pair, notional_base, entry_rate, trade_date, value_date,
                       tag, trade_time, notes, created_at, updated_at, deleted_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                    ON CONFLICT(id) DO UPDATE SET
                      book=excluded.book, pair=excluded.pair,
                      notional_base=excluded.notional_base, entry_rate=excluded.entry_rate,
                      trade_date=excluded.trade_date, value_date=excluded.value_date,
                      tag=excluded.tag, trade_time=excluded.trade_time,
                      notes=excluded.notes, updated_at=excluded.updated_at
                """, (pos.id, book, pos.pair, float(pos.notional_base),
                      float(pos.entry_rate), _iso(pos.trade_date), _iso(pos.value_date),
                      pos.tag, _iso(pos.trade_time), notes, now, now))
            else:
                raise TypeError(f"save_position expects OptionPosition|SpotPosition, "
                                f"got {type(pos).__name__}")
        if isinstance(pos, SpotPosition) and pos.tag == "hedge":
            self.log_hedge(pos, book=book, note=notes)
        return pos.id

    def save_positions(self, positions: Iterable[OptionPosition | SpotPosition], *,
                       book: str = "default", notes: dict[str, str] | None = None) -> int:
        """Atomic multi-write: all rows or none (requirements section 3.6 step 4)."""
        notes = notes or {}
        positions = list(positions)
        with self._tx():
            for p in positions:
                self.save_position(p, book=book, notes=notes.get(p.id, ""))
        return len(positions)

    def load_book(self, name: str = "default", *, include_deleted: bool = False,
                  asof: datetime | None = None, pair: str | None = None) -> Book:
        """The book as it stands, or as it stood at ``asof`` (soft delete, REQ-031)."""
        opt_sql = "SELECT * FROM option_positions WHERE book=?"
        spt_sql = "SELECT * FROM spot_positions WHERE book=?"
        args: list[Any] = [name]
        if not include_deleted:
            if asof is not None:
                clause = " AND created_at<=? AND (deleted_at IS NULL OR deleted_at>?)"
                opt_sql += clause
                spt_sql += clause
                args += [_iso(asof), _iso(asof)]
            else:
                opt_sql += " AND deleted_at IS NULL"
                spt_sql += " AND deleted_at IS NULL"
        if pair:
            opt_sql += " AND pair=?"
            spt_sql += " AND pair=?"
        with self._lock:
            o_args = args + ([pair.upper()] if pair else [])
            opts = self._conn.execute(opt_sql + " ORDER BY expiry, pair, strike",
                                      o_args).fetchall()
            spts = self._conn.execute(spt_sql + " ORDER BY COALESCE(trade_time, trade_date, "
                                                "created_at)", o_args).fetchall()
        return Book(options=[self._row_to_option(r) for r in opts],
                    spots=[self._row_to_spot(r) for r in spts], name=name)

    @staticmethod
    def _row_to_option(r: sqlite3.Row) -> OptionPosition:
        return OptionPosition(
            id=r["id"], pair=r["pair"], cp=int(r["cp"]), strike=float(r["strike"]),
            expiry=_as_date(r["expiry"]), notional_base=float(r["notional_base"]),
            direction=int(r["direction"]), premium_paid=float(r["premium_paid"]),
            premium_ccy=r["premium_ccy"] or "", trade_date=_as_date(r["trade_date"]),
            trade_spot=r["trade_spot"], trade_vol=r["trade_vol"], cut=r["cut"],
            tag=r["tag"] or "", trade_time=_as_dt(r["trade_time"]))

    @staticmethod
    def _row_to_spot(r: sqlite3.Row) -> SpotPosition:
        return SpotPosition(
            id=r["id"], pair=r["pair"], notional_base=float(r["notional_base"]),
            entry_rate=float(r["entry_rate"]), trade_date=_as_date(r["trade_date"]),
            value_date=_as_date(r["value_date"]), tag=r["tag"] or "",
            trade_time=_as_dt(r["trade_time"]))

    def get_position(self, pid: str) -> OptionPosition | SpotPosition | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM option_positions WHERE id=?", (pid,)).fetchone()
            if r is not None:
                return self._row_to_option(r)
            r = self._conn.execute("SELECT * FROM spot_positions WHERE id=?", (pid,)).fetchone()
            return self._row_to_spot(r) if r is not None else None

    def notes(self, book: str = "default") -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, notes FROM option_positions WHERE book=? "
                "UNION ALL SELECT id, notes FROM spot_positions WHERE book=?",
                (book, book)).fetchall()
        return {r["id"]: r["notes"] or "" for r in rows}

    def delete_position(self, pid: str, *, hard: bool = False) -> bool:
        """**Soft** delete by default: the row keeps its history so an already
        published P&L cannot move retroactively (REQ-031, section 5.6)."""
        now = _iso(utcnow())
        with self._tx() as c:
            if hard:
                a = c.execute("DELETE FROM option_positions WHERE id=?", (pid,)).rowcount
                b = c.execute("DELETE FROM spot_positions WHERE id=?", (pid,)).rowcount
            else:
                a = c.execute("UPDATE option_positions SET deleted_at=?, updated_at=? "
                              "WHERE id=? AND deleted_at IS NULL", (now, now, pid)).rowcount
                b = c.execute("UPDATE spot_positions SET deleted_at=?, updated_at=? "
                              "WHERE id=? AND deleted_at IS NULL", (now, now, pid)).rowcount
        return bool(a or b)

    def undelete_position(self, pid: str) -> bool:
        now = _iso(utcnow())
        with self._tx() as c:
            a = c.execute("UPDATE option_positions SET deleted_at=NULL, updated_at=? WHERE id=?",
                          (now, pid)).rowcount
            b = c.execute("UPDATE spot_positions SET deleted_at=NULL, updated_at=? WHERE id=?",
                          (now, pid)).rowcount
        return bool(a or b)

    def delete_tag(self, tag: str, *, book: str = "default") -> int:
        """Soft-delete a whole structure (legs share ``<structure>:<uuid>``, REQ-030)."""
        now = _iso(utcnow())
        with self._tx() as c:
            a = c.execute("UPDATE option_positions SET deleted_at=?, updated_at=? "
                          "WHERE book=? AND tag=? AND deleted_at IS NULL",
                          (now, now, book, tag)).rowcount
            b = c.execute("UPDATE spot_positions SET deleted_at=?, updated_at=? "
                          "WHERE book=? AND tag=? AND deleted_at IS NULL",
                          (now, now, book, tag)).rowcount
        return a + b

    def clear_book(self, name: str = "default", *, hard: bool = False) -> int:
        n = 0
        for p in self.load_book(name).options + self.load_book(name).spots:
            n += int(self.delete_position(p.id, hard=hard))
        return n

    def deleted_positions(self, book: str = "default") -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, pair, tag, deleted_at, 'OPTION' AS kind FROM option_positions "
                "WHERE book=? AND deleted_at IS NOT NULL UNION ALL "
                "SELECT id, pair, tag, deleted_at, 'SPOT' FROM spot_positions "
                "WHERE book=? AND deleted_at IS NOT NULL ORDER BY deleted_at DESC",
                (book, book)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- marks (CG-2)
    def set_mark_vol(self, pid: str, mark_vol: float, *, mark_source: str = "user",
                     asof: datetime | None = None, note: str = "") -> None:
        if not (0.0005 < float(mark_vol) < 5.0):
            raise ValueError(f"mark vol {mark_vol!r} out of range; vols are decimals "
                             "(0.085 = 8.5%)")
        with self._tx() as c:
            c.execute("INSERT INTO position_marks(id, mark_vol, mark_source, asof, note) "
                      "VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                      "mark_vol=excluded.mark_vol, mark_source=excluded.mark_source, "
                      "asof=excluded.asof, note=excluded.note",
                      (pid, float(mark_vol), mark_source, _iso(asof or utcnow()), note))

    def clear_mark_vol(self, pid: str) -> bool:
        with self._tx() as c:
            return bool(c.execute("DELETE FROM position_marks WHERE id=?", (pid,)).rowcount)

    def marks(self) -> dict[str, MarkVol]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM position_marks").fetchall()
        return {r["id"]: MarkVol(r["id"], float(r["mark_vol"]), r["mark_source"],
                                 _as_dt(r["asof"]), r["note"] or "") for r in rows}

    # ------------------------------------------------------------- hedge log (CG-5)
    def log_hedge(self, pos: SpotPosition, *, book: str = "default", note: str = "") -> None:
        with self._tx() as c:
            c.execute("INSERT INTO hedge_log(id, book, pair, notional_base, entry_rate, "
                      "trade_time, trade_date, tag, note, created_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                      "notional_base=excluded.notional_base, entry_rate=excluded.entry_rate, "
                      "trade_time=excluded.trade_time, trade_date=excluded.trade_date, "
                      "note=excluded.note",
                      (pos.id, book, pos.pair, float(pos.notional_base),
                       float(pos.entry_rate), _iso(pos.trade_time), _iso(pos.trade_date),
                       pos.tag or "hedge", note, _iso(utcnow())))

    def hedge_log(self, book: str = "default", pair: str | None = None
                  ) -> list[HedgeLogEntry]:
        """Ordered by ``trade_time``, falling back to ``trade_date`` (CG-5)."""
        sql = ("SELECT * FROM hedge_log WHERE book=? AND deleted_at IS NULL"
               + (" AND pair=?" if pair else "")
               + " ORDER BY COALESCE(trade_time, trade_date || 'T00:00:00+00:00', created_at)")
        args = [book] + ([pair.upper()] if pair else [])
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [HedgeLogEntry(r["id"], r["pair"], float(r["notional_base"]),
                              float(r["entry_rate"]), _as_dt(r["trade_time"]),
                              _as_date(r["trade_date"]), r["tag"] or "", r["note"] or "")
                for r in rows]

    # ------------------------------------------------------------- manual vols (T-1)
    def save_manual_quotes(self, quotes: Iterable[ManualQuote], *,
                           asof: datetime | None = None) -> int:
        """Persist a paste of the trader's own marks. One ``asof`` per paste."""
        stamp = _iso(asof or utcnow())
        quotes = list(quotes)
        with self._tx() as c:
            for q in quotes:
                if q.tenor not in TENORS:
                    raise ValueError(f"tenor {q.tenor!r} is not in the broker grid "
                                     f"({', '.join(TENORS)})")
                if q.pair.upper() not in PAIRS:
                    raise ValueError(f"unknown pair {q.pair!r}")
                if not (0.0005 < float(q.atm) < 5.0):
                    raise ValueError(f"{q.pair} {q.tenor}: ATM {q.atm!r} out of range; "
                                     "vols are decimals (0.085 = 8.5%)")
                c.execute("INSERT OR REPLACE INTO manual_vol_quotes"
                          "(pair, tenor, asof, atm, rr25, bf25, rr10, bf10, source, note) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (q.pair.upper(), q.tenor, stamp, float(q.atm), float(q.rr25),
                           float(q.bf25), q.rr10, q.bf10, q.source or "user", q.note))
        return len(quotes)

    def manual_quotes(self, pair: str | None = None, *, asof: datetime | None = None
                      ) -> list[ManualQuote]:
        """Latest mark per (pair, tenor), or the marks in force at ``asof``."""
        sql = ("SELECT m.* FROM manual_vol_quotes m JOIN (SELECT pair, tenor, MAX(asof) "
               "AS a FROM manual_vol_quotes {where} GROUP BY pair, tenor) x "
               "ON m.pair=x.pair AND m.tenor=x.tenor AND m.asof=x.a")
        args: list[Any] = []
        where = []
        if asof is not None:
            where.append("asof<=?")
            args.append(_iso(asof))
        sql = sql.format(where=("WHERE " + " AND ".join(where)) if where else "")
        if pair:
            sql += " AND m.pair=?"
            args.append(pair.upper())
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out = [ManualQuote(r["pair"], r["tenor"], float(r["atm"]), float(r["rr25"]),
                           float(r["bf25"]), r["rr10"], r["bf10"], _as_dt(r["asof"]),
                           r["source"], r["note"] or "") for r in rows]
        out.sort(key=lambda q: (q.pair, q.T))
        return out

    def manual_quote_pairs(self) -> list[str]:
        return sorted({q.pair for q in self.manual_quotes()})

    def clear_manual_quotes(self, pair: str | None = None, tenor: str | None = None) -> int:
        sql = "DELETE FROM manual_vol_quotes"
        args: list[Any] = []
        cond = []
        if pair:
            cond.append("pair=?")
            args.append(pair.upper())
        if tenor:
            cond.append("tenor=?")
            args.append(tenor)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        with self._tx() as c:
            return c.execute(sql, args).rowcount

    # ------------------------------------------------------------- settings / levels
    def set_setting(self, key: str, value: Any) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO settings(key, value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value)))

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            r = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if r is None:
            return default
        try:
            return json.loads(r["value"])
        except json.JSONDecodeError:
            return r["value"]

    def settings(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                out[r["key"]] = r["value"]
        return out

    def add_level(self, pair: str, level: float, label: str = "") -> str:
        lid = _rand_id("lvl")
        with self._tx() as c:
            c.execute("INSERT INTO levels(id, pair, level, label, created_at) VALUES(?,?,?,?,?)",
                      (lid, pair.upper(), float(level), label, _iso(utcnow())))
        return lid

    def levels(self, pair: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM levels WHERE deleted_at IS NULL"
        args: list[Any] = []
        if pair:
            sql += " AND pair=?"
            args.append(pair.upper())
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql + " ORDER BY level", args)]

    def delete_level(self, lid: str) -> bool:
        with self._tx() as c:
            return bool(c.execute("UPDATE levels SET deleted_at=? WHERE id=?",
                                  (_iso(utcnow()), lid)).rowcount)

    # ------------------------------------------------------------- CSV
    def parse_csv(self, text: str | bytes, *, ctx: MarketContext | None = None,
                  mode: str = "append", allow_expired: bool = False) -> ImportReport:
        """Parse and validate without writing anything - the REQ-033 preview gate."""
        if isinstance(text, bytes):
            text = text.decode("utf-8-sig", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        rep = ImportReport(mode=mode)
        lines = [ln for ln in text.split("\n") if ln.strip() and not ln.lstrip().startswith("#")]
        if not lines:
            rep.fatal = "the file has no data rows"
            return rep
        reader = csv.reader(io.StringIO("\n".join(lines)))
        try:
            header = next(reader)
        except StopIteration:
            rep.fatal = "the file has no header row"
            return rep
        cols: list[str] = []
        unknown: list[str] = []
        for h in header:
            key = _HEADER_ALIASES.get(_norm_header(h))
            if key is None:
                unknown.append(h)
                cols.append(f"?{h}")
            else:
                cols.append(key)
        rep.header = cols
        if "pair" not in cols:
            rep.fatal = (f"no 'pair' column found. Header was: {', '.join(header)}. "
                         f"The schema is: {', '.join(CSV_COLUMNS)}")
            return rep
        existing = self.existing_ids()
        seen_ids: set[str] = set()
        counters = {"opt": None, "spt": None}

        def _next_id(prefix: str) -> str:
            if counters[prefix] is None:
                counters[prefix] = int(self.next_id(prefix).split("-")[1])
            else:
                counters[prefix] += 1
            cand = f"{prefix}-{counters[prefix]:04d}"
            while cand in existing or cand in seen_ids:
                counters[prefix] += 1
                cand = f"{prefix}-{counters[prefix]:04d}"
            return cand

        for i, row in enumerate(reader, start=2):
            raw = {}
            extra = {}
            for c, v in zip(cols, row):
                if c.startswith("?"):
                    extra[c[1:]] = v
                else:
                    raw[c] = v
            r = validate_row(raw, ctx, row_no=i, allow_expired=allow_expired,
                             existing_ids=existing, upsert=(mode == "upsert"),
                             next_id=_next_id)
            r.unknown_columns = extra
            if extra:
                # section 3.5: unknown columns are preserved, never silently dropped
                r.notes = "; ".join(filter(None, [r.notes] + [f"{k}={v}" for k, v in
                                                              extra.items()]))
                r.messages.append(ValidationMessage(
                    "V-0", "W", f"unknown column(s) kept in notes: {', '.join(extra)}"))
            if r.position is not None:
                if r.position.id in seen_ids:
                    r.messages.append(ValidationMessage(
                        "V-18", "E", f"duplicate id {r.position.id} inside this file", "id"))
                seen_ids.add(r.position.id)
            rep.rows.append(r)

        n_pos = len(self.load_book().options) + len(self.load_book().spots)
        if mode != "replace" and n_pos + len(rep.rows) > 2000:                    # V-21
            for r in rep.rows[:1]:
                r.messages.append(ValidationMessage(
                    "V-21", "W", f"book would hold {n_pos + len(rep.rows)} positions; "
                                 "performance targets are specified up to 200"))
        _check_structures(rep)                                                    # V-20
        return rep

    def commit_import(self, rep: ImportReport, *, book: str = "default",
                      accept_warnings: bool = False) -> ImportReport:
        """Write a parsed report in **one** transaction: all rows or none."""
        if rep.fatal or rep.n_error:
            raise ValueError(f"refusing to commit: {rep.fatal or f'{rep.n_error} ERROR row(s)'}")
        if rep.n_warn and not accept_warnings:
            raise ValueError(f"{rep.n_warn} row(s) carry warnings; tick 'accept warnings' first")
        with self._tx() as c:
            if rep.mode == "replace":
                now = _iso(utcnow())
                c.execute("UPDATE option_positions SET deleted_at=?, updated_at=? "
                          "WHERE book=? AND deleted_at IS NULL", (now, now, book))
                c.execute("UPDATE spot_positions SET deleted_at=?, updated_at=? "
                          "WHERE book=? AND deleted_at IS NULL", (now, now, book))
            for r in rep.rows:
                if r.position is not None:
                    self.save_position(r.position, book=book, notes=r.notes)
            c.execute("INSERT INTO import_audit(at, mode, n_ok, n_warn, n_error, committed, "
                      "detail) VALUES(?,?,?,?,?,?,?)",
                      (_iso(utcnow()), rep.mode, rep.n_ok, rep.n_warn, rep.n_error,
                       len(rep.rows), f"header={','.join(rep.header)}"))
        rep.committed = sum(1 for r in rep.rows if r.position is not None)
        return rep

    def import_csv(self, text: str | bytes, *, ctx: MarketContext | None = None,
                   mode: str = "append", allow_expired: bool = False,
                   commit: bool = True, accept_warnings: bool = True,
                   book: str = "default") -> ImportReport:
        """Parse, validate and (by default) commit. ``commit=False`` = preview only."""
        rep = self.parse_csv(text, ctx=ctx, mode=mode, allow_expired=allow_expired)
        if commit and rep.can_commit:
            self.commit_import(rep, book=book, accept_warnings=accept_warnings)
        return rep

    def export_csv(self, book: Book | str = "default") -> str:
        """The full section 3.5 schema, so ``import(export(book)) == book`` (REQ-034)."""
        if isinstance(book, str):
            name = book
            book = self.load_book(name)
        else:
            name = book.name
        notes = self.notes(name)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
        w.writeheader()
        for o in book.options:
            w.writerow({
                "instrument_type": "OPTION", "id": o.id, "pair": o.pair,
                "cp": "C" if o.cp > 0 else "P", "strike": _num(o.strike),
                "expiry": _iso(o.expiry), "cut": o.cut,
                "notional_base": _num(o.notional_base),
                "direction": "B" if o.direction > 0 else "S",
                "premium_paid": _num(o.premium_paid),
                "premium_ccy": o.premium_ccy or pair_spec(o.pair).quote,
                "premium_unit": "total", "trade_date": _iso(o.trade_date) or "",
                "trade_time": _iso(o.trade_time) or "", "trade_spot": _num(o.trade_spot),
                "trade_vol": _num(o.trade_vol), "entry_rate": "", "value_date": "",
                "tag": o.tag, "notes": notes.get(o.id, ""),
            })
        for s in book.spots:
            w.writerow({
                "instrument_type": "SPOT", "id": s.id, "pair": s.pair, "cp": "",
                "strike": "", "expiry": "", "cut": "",
                "notional_base": _num(s.notional_base), "direction": "", "premium_paid": "",
                "premium_ccy": "", "premium_unit": "", "trade_date": _iso(s.trade_date) or "",
                "trade_time": _iso(s.trade_time) or "", "trade_spot": "", "trade_vol": "",
                "entry_rate": _num(s.entry_rate), "value_date": _iso(s.value_date) or "",
                "tag": s.tag, "notes": notes.get(s.id, ""),
            })
        return buf.getvalue()

    def import_audit(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM import_audit ORDER BY id DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------- demo book
    def seed_demo_book(self, ctx: MarketContext | None = None, *,
                       book: str = "default") -> int:
        """REQ-006: a deterministic demo book so every screen is populated.

        Struck off the passed snapshot when there is one, otherwise off anchor
        levels; every leg is tagged so it can be deleted in one click.
        """
        ctx = ctx or MarketContext()
        today = (ctx.asof or utcnow()).date()
        spot = {"EURUSD": 1.0850, "USDJPY": 147.20, "GBPUSD": 1.2650}
        spot.update({p: v for p, v in ctx.spot.items() if p in spot})
        exp1 = today.replace(day=min(today.day, 28))
        exp1 = date.fromordinal(today.toordinal() + 30)
        exp3 = date.fromordinal(today.toordinal() + 91)
        eu, uj, gb = spot["EURUSD"], spot["USDJPY"], spot["GBPUSD"]
        rounder = lambda x, s: round(x / s) * s                    # noqa: E731
        legs: list[OptionPosition] = [
            OptionPosition("opt-9001", "EURUSD", +1, rounder(eu, 0.0050), exp1, 10e6, +1,
                           87.5e-4 * 10e6, "USD", today, eu, 0.0705, "NY10", "straddle:demo-eu",
                           datetime.combine(today, datetime.min.time(), timezone.utc)),
            OptionPosition("opt-9002", "EURUSD", -1, rounder(eu, 0.0050), exp1, 10e6, +1,
                           87.5e-4 * 10e6, "USD", today, eu, 0.0705, "NY10", "straddle:demo-eu",
                           datetime.combine(today, datetime.min.time(), timezone.utc)),
            OptionPosition("opt-9003", "USDJPY", +1, rounder(uj * 1.022, 0.50), exp3, 20e6, +1,
                           21_000_000.0, "JPY", today, uj, 0.0860, "NY10", "rr25:demo-uj"),
            OptionPosition("opt-9004", "USDJPY", -1, rounder(uj * 0.961, 0.50), exp3, 20e6, -1,
                           -23_000_000.0, "JPY", today, uj, 0.0955, "NY10", "rr25:demo-uj"),
            OptionPosition("opt-9005", "GBPUSD", +1, rounder(gb * 1.018, 0.0050), exp1, 15e6, -1,
                           -62.0e-4 * 15e6, "USD", today, gb, 0.0790, "NY10",
                           "strangle:demo-gb"),
            OptionPosition("opt-9006", "GBPUSD", -1, rounder(gb * 0.982, 0.0050), exp1, 15e6, -1,
                           -58.0e-4 * 15e6, "USD", today, gb, 0.0815, "NY10",
                           "strangle:demo-gb"),
        ]
        hedge = SpotPosition("spt-9001", "EURUSD", -1.2e6, eu, today, None, "hedge",
                             datetime.combine(today, datetime.min.time(), timezone.utc))
        with self._tx():
            for leg in legs:
                self.save_position(leg, book=book, notes="demo book")
            self.save_position(hedge, book=book, notes="demo book: delta hedge")
        return len(legs) + 1

    def is_empty(self, book: str = "default") -> bool:
        b = self.load_book(book)
        return not b.options and not b.spots

    def stats(self, book: str = "default") -> dict[str, int]:
        b = self.load_book(book)
        return {"options": len(b.options), "spots": len(b.spots),
                "deleted": len(self.deleted_positions(book)),
                "marks": len(self.marks()), "manual_quotes": len(self.manual_quotes()),
                "hedges": len(self.hedge_log(book))}


def _num(x: Any) -> str:
    """Round-trip-exact float rendering; never scientific notation for normal sizes."""
    if x is None:
        return ""
    f = float(x)
    if not math.isfinite(f):
        return ""
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return repr(f)


def _check_structures(rep: ImportReport) -> None:
    """V-20: legs sharing a structure tag must be internally consistent."""
    groups: dict[str, list[RowResult]] = {}
    for r in rep.rows:
        p = r.position
        if isinstance(p, OptionPosition) and ":" in (p.tag or ""):
            groups.setdefault(p.tag, []).append(r)
    for tag, rows in groups.items():
        kind = tag.split(":")[0].lower()
        opts = [r.position for r in rows]
        def flag(text: str) -> None:
            for r in rows:
                r.messages.append(ValidationMessage("V-20", "E", text, "tag"))
        if kind.startswith("straddle") and len(opts) == 2:
            if len({o.strike for o in opts}) != 1:
                flag(f"straddle {tag}: legs must share a strike")
            if {o.cp for o in opts} != {1, -1}:
                flag(f"straddle {tag}: needs one call and one put")
        if kind.startswith("rr") and len(opts) == 2:
            if {o.cp for o in opts} != {1, -1}:
                flag(f"risk reversal {tag}: needs one call and one put")
            if len({o.expiry for o in opts}) != 1:
                flag(f"risk reversal {tag}: legs must share an expiry")
            if len({o.direction for o in opts}) != 2:
                flag(f"risk reversal {tag}: legs must have opposite directions")
        if kind.startswith("strangle") and len(opts) == 2:
            if {o.cp for o in opts} != {1, -1}:
                flag(f"strangle {tag}: needs one call and one put")
            if len({o.strike for o in opts}) != 2:
                flag(f"strangle {tag}: legs must have different strikes")


# ------------------------------------------------------------------ default store
#: one Store per **resolved** path, not one Store per process (QA finding Q-4).
_stores: "dict[str, Store]" = {}
_default_lock = threading.Lock()


def resolve_db_path(path: str | os.PathLike | None = None) -> str:
    """The canonical cache key for a book path: one book, one string.

    ``None`` -> ``$FXGAMMA_DB`` -> :attr:`Store.DEFAULT_PATH`, then made absolute and
    symlink-free so ``data/fxgamma.db``, ``./data/fxgamma.db`` and the absolute path
    are one store rather than three.  ``:memory:`` is passed through untouched: each
    in-memory database is genuinely private and is never cached.
    """
    raw = str(path if path is not None
              else (os.environ.get("FXGAMMA_DB") or Store.DEFAULT_PATH))
    if raw == ":memory:":
        return raw
    try:
        return str(Path(raw).expanduser().resolve())
    except OSError:                                        # pragma: no cover
        return str(Path(raw).expanduser().absolute())


def get_store(path: str | os.PathLike | None = None, *, fresh: bool = False) -> Store:
    """The store for ``path``, cached **per resolved path** (QA finding Q-4).

    The previous implementation cached one store per process and ignored ``path``
    after the first call, so a user opening a second book silently read and wrote the
    first one -- data loss, not cosmetics.  The cache is now keyed on
    :func:`resolve_db_path`: a second path gets a second store, and the same path
    always gets the same connection (WAL wants one writer per process, and two
    handles on one file is how you get a stale read).

    ``path=None`` always means the default book (``$FXGAMMA_DB`` or
    ``data/fxgamma.db``) -- it never means "whichever book was opened last", because
    that is the ambiguity the bug was made of.  ``fresh=True`` closes and rebuilds
    the store for *that path only* and leaves every other open book alone.
    """
    key = resolve_db_path(path)
    if key == ":memory:":
        return Store(":memory:")                 # private by definition; never cached
    with _default_lock:
        if fresh and key in _stores:
            try:
                _stores.pop(key).close()
            except Exception:                              # noqa: BLE001  pragma: no cover
                pass
        st = _stores.get(key)
        if st is None:
            st = _stores[key] = Store(key)
        return st


def close_stores() -> int:
    """Close every cached store (tests and ``--db`` switches). Returns the count."""
    with _default_lock:
        n = len(_stores)
        for st in list(_stores.values()):
            try:
                st.close()
            except Exception:                              # noqa: BLE001  pragma: no cover
                pass
        _stores.clear()
    return n
