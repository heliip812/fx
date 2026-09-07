"""Manual vol marks -- the desk's own ATM / 25d RR / 25d BF grid.  **PRIMARY input.**

Contract amendment v1.2, T-1 (``docs/01_architecture.md``), which accepted the trader's
blocking objection in ``docs/06_trader_review.md`` sections 1.4 and 2/Q-3:

    "ETF-implied vol is never a mark.  ... it has to be the *primary* morning input: a
     30-second ATM / 25d RR / 25d BF grid per pair per tenor that I paste in, after which the
     app is marked to my curve and the ETF data becomes what it should always have been -- a
     z-score input."

So this module owns three things:

1. :class:`ManualMark` -- one pair/tenor row of broker-style quotes, plus who entered it and
   when.  Converts to the frozen :class:`~fxgamma.models.surface.SmileQuotes` (contract s8).
2. :class:`ManualQuoteStore` -- JSON persistence under ``data/manual/marks.json`` (override
   with ``$FXGAMMA_MANUAL_MARKS``), atomic writes, CSV import/export, plus optional manual
   **spot** and **rate** overrides (contract s6 page 8, "manual vol/rate overrides").
3. :class:`ManualQuoteProvider` -- a normal :class:`~fxgamma.data.base.MarketDataProvider`
   whose ``smile_quotes()`` serves the stored grid, badged
   ``Provenance(kind="user_override")``.  It sits **first** in ``ChainProvider``
   (manual -> live -> cache -> synthetic), so a manual mark always wins and is never
   overwritten by a live pull -- the marks live in their own file, nothing else writes it.

Partial grids on purpose
------------------------
If the user marks only 1M and 3M, we return **only** those tenors and let
``build_surface`` interpolate/extrapolate the desk's own curve.  We deliberately do *not*
splice ETF tenors into the gaps: a surface that is half broker mark and half listed-ETF vol
has a term structure nobody quoted, and the single ``surface.<PAIR>`` badge could not tell
the truth about it.  One pair is either marked or it is not.

The paste parser
----------------
:func:`parse_grid` is deliberately forgiving because the input is whatever the trader has in
the clipboard at 07:05.  It accepts tab / comma / multi-space separation, ``%`` signs,
bid/ask pairs (``7.05/7.25`` -> mid), accounting negatives (``(0.15)`` -> ``-0.15``), a
leading pair column, header rows in any of the usual spellings (``ATM``, ``25d RR``,
``RR25``, ``BF``, ``fly``...), and both row-per-tenor and column-per-tenor (transposed)
layouts.  It infers the vol unit -- ``8.5`` vs ``0.085`` -- from the ATM column and applies
that one scale to every column, then **reports what it inferred** in
:attr:`ParsedGrid.inferred` and anything suspicious in :attr:`ParsedGrid.warnings`.  Nothing
is silently coerced: call :meth:`ParsedGrid.summary` and show it to the user before saving.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

from ..conventions import PAIRS, TENORS, tenor_years
from ..types import Provenance
from .base import (MarketDataProvider, SmileQuotes, SourceStatus, empty_event_frame,
                   empty_oi_frame, empty_spot_frame, utcnow)

log = logging.getLogger(__name__)

__all__ = [
    "ManualMark", "ManualQuoteStore", "ManualQuoteProvider", "ParsedGrid", "parse_grid",
    "default_marks_path", "GRID_TENORS", "MARK_COLUMNS", "SCHEMA",
]

#: on-disk schema tag; bumped if the JSON layout ever changes
SCHEMA = "fxgamma.manual/1"

#: the tenor row set the UI offers by default (a broker run is usually exactly this)
GRID_TENORS: tuple[str, ...] = ("1W", "1M", "2M", "3M", "6M", "1Y")

#: column order for :meth:`ManualQuoteStore.as_frame` / CSV round-trip
MARK_COLUMNS = ["pair", "tenor", "T", "atm", "rr25", "bf25", "rr10", "bf10",
                "asof", "source", "note"]

#: plausibility band for a decimal FX vol.  Outside this we warn (never silently rescale).
_VOL_MIN, _VOL_MAX = 0.005, 1.50


def default_marks_path() -> Path:
    env = os.environ.get("FXGAMMA_MANUAL_MARKS")
    if env:
        return Path(env).expanduser()
    # fxgamma/data/manual.py -> parents[2] == repo root
    return Path(__file__).resolve().parents[2] / "data" / "manual" / "marks.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(x: Any) -> datetime:
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(x))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return _now()


def _norm_tenor(tok: str) -> str | None:
    """``'o/n'`` -> ``'ON'``, ``'1mo'`` -> ``'1M'``.  ``None`` if it is not a tenor."""
    t = str(tok).strip().upper().replace(" ", "")
    if not t:
        return None
    t = {"O/N": "ON", "ON": "ON", "1D": "ON", "12M": "1Y", "1MO": "1M", "3MO": "3M",
         "6MO": "6M", "1YR": "1Y", "2YR": "2Y"}.get(t, t)
    if t in TENORS:
        return t
    if re.fullmatch(r"\d+[DWMY]", t):
        return t
    return None


# ======================================================================== one mark
@dataclass(frozen=True)
class ManualMark:
    """One pair/tenor of the desk's own smile.  Vols are **decimals** (0.0705 = 7.05%)."""

    pair: str
    tenor: str
    atm: float
    rr25: float = 0.0
    bf25: float = 0.0
    rr10: float | None = None
    bf10: float | None = None
    asof: datetime = field(default_factory=_now)     # when the user entered/pasted it
    source: str = "manual"                           # "typed" | "paste" | "csv" | free text
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair", str(self.pair).strip().upper())
        t = _norm_tenor(self.tenor)
        if t is None:
            raise ValueError(f"bad tenor {self.tenor!r} for {self.pair}")
        object.__setattr__(self, "tenor", t)
        object.__setattr__(self, "asof", _parse_dt(self.asof))
        for f in ("atm", "rr25", "bf25"):
            object.__setattr__(self, f, float(getattr(self, f)))
        for f in ("rr10", "bf10"):
            v = getattr(self, f)
            object.__setattr__(self, f, None if v is None or (isinstance(v, float)
                                                              and math.isnan(v))
                               else float(v))
        if not math.isfinite(self.atm) or self.atm <= 0.0:
            raise ValueError(f"{self.pair} {self.tenor}: ATM must be a positive decimal vol, "
                             f"got {self.atm!r}")

    @property
    def T(self) -> float:
        return tenor_years(self.tenor)

    def age_hours(self, now: datetime | None = None) -> float:
        return ((now or _now()) - self.asof).total_seconds() / 3600.0

    def to_smile_quotes(self) -> SmileQuotes:
        """The frozen contract s8 object.  This is the only thing that crosses to models."""
        return SmileQuotes(T=self.T, atm=self.atm, rr25=self.rr25, bf25=self.bf25,
                           rr10=self.rr10, bf10=self.bf10, tenor=self.tenor)

    # ------------------------------------------------------------------ (de)serialise
    def to_dict(self) -> dict[str, Any]:
        return {"pair": self.pair, "tenor": self.tenor, "atm": self.atm, "rr25": self.rr25,
                "bf25": self.bf25, "rr10": self.rr10, "bf10": self.bf10,
                "asof": self.asof.isoformat(), "source": self.source, "note": self.note}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ManualMark":
        return cls(pair=d["pair"], tenor=d["tenor"], atm=float(d["atm"]),
                   rr25=float(d.get("rr25", 0.0) or 0.0),
                   bf25=float(d.get("bf25", 0.0) or 0.0),
                   rr10=d.get("rr10"), bf10=d.get("bf10"),
                   asof=d.get("asof"), source=d.get("source", "manual"),
                   note=d.get("note", ""))

    def as_row(self) -> dict[str, Any]:
        r = self.to_dict()
        r["T"] = self.T
        return {c: r.get(c) for c in MARK_COLUMNS}

    def __str__(self) -> str:                                        # pragma: no cover
        return (f"{self.pair} {self.tenor:>3}  ATM {self.atm*100:6.3f}  "
                f"RR25 {self.rr25*100:+6.3f}  BF25 {self.bf25*100:6.3f}")


# ======================================================================== paste parser
_HEADER_WORDS = {"tenor", "term", "expiry", "exp", "pair", "ccy", "atm", "atmf", "vol",
                 "rr", "rr25", "rr10", "25rr", "10rr", "bf", "bf25", "bf10", "25bf", "10bf",
                 "fly", "butterfly", "strangle", "riskreversal", "rev"}
_METRIC_ALIASES = {
    "atm": "atm", "atmf": "atm", "vol": "atm", "atmvol": "atm", "sigma": "atm",
    "rr": "rr25", "rr25": "rr25", "25rr": "rr25", "25drr": "rr25", "rr25d": "rr25",
    "riskreversal": "rr25", "rev": "rr25", "rr_25": "rr25",
    "bf": "bf25", "bf25": "bf25", "25bf": "bf25", "25dbf": "bf25", "bf25d": "bf25",
    "fly": "bf25", "butterfly": "bf25", "strangle": "bf25", "bf_25": "bf25",
    "rr10": "rr10", "10rr": "rr10", "10drr": "rr10", "rr10d": "rr10",
    "bf10": "bf10", "10bf": "bf10", "10dbf": "bf10", "bf10d": "bf10",
    "tenor": "tenor", "term": "tenor", "expiry": "tenor", "exp": "tenor",
    "pair": "pair", "ccy": "pair", "ccypair": "pair",
}
_SPLIT = re.compile(r"[\t,;|]+|\s{1,}")
_NUMRE = re.compile(r"^[+\-−]?\d*\.?\d+$")


def _clean_num_token(tok: str) -> str:
    t = str(tok).strip().replace("−", "-").replace("%", "")
    if t.startswith("(") and t.endswith(")"):        # accounting negative
        t = "-" + t[1:-1]
    return t.strip()


def _as_float(tok: str) -> float | None:
    """``'7.05'``/``'-.15'``/``'(0.20)'``/``'7.05/7.25'`` (bid/ask -> mid) -> float."""
    t = _clean_num_token(tok)
    if not t or t in {"-", "--", "n/a", "na", "nan", "."}:
        return None
    if "/" in t:                                     # broker bid/ask -> mid
        parts = [p for p in t.split("/") if p]
        vals = [p for p in parts if _NUMRE.match(p)]
        if len(vals) == 2:
            return (float(vals[0]) + float(vals[1])) / 2.0
        return None
    if not _NUMRE.match(t):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _norm_head(tok: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(tok).strip().lower())


def _merge_header_tokens(toks: Sequence[str]) -> list[str]:
    """``['25d', 'RR']`` -> ``['25rr']`` so "25d RR" survives whitespace splitting."""
    out: list[str] = []
    i = 0
    while i < len(toks):
        a = _norm_head(toks[i])
        b = _norm_head(toks[i + 1]) if i + 1 < len(toks) else ""
        if re.fullmatch(r"\d+d?", a) and b in {"rr", "bf", "fly", "rev", "butterfly",
                                               "strangle"}:
            out.append(re.sub(r"d$", "", a) + ("bf" if b in {"fly", "butterfly", "strangle"}
                                               else "rr" if b == "rev" else b))
            i += 2
            continue
        out.append(toks[i])
        i += 1
    return out


@dataclass(frozen=True)
class ParsedGrid:
    """Result of :func:`parse_grid`.  Show ``summary()`` to the user before saving."""

    marks: list[ManualMark] = field(default_factory=list)
    scale: float = 1.0                      # 1.0 = input already decimal, 0.01 = percent
    layout: str = "rows"                    # "rows" | "columns"
    inferred: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.marks)

    @property
    def pairs(self) -> list[str]:
        return sorted({m.pair for m in self.marks})

    def as_frame(self) -> pd.DataFrame:
        if not self.marks:
            return pd.DataFrame(columns=MARK_COLUMNS)
        return pd.DataFrame([m.as_row() for m in self.marks], columns=MARK_COLUMNS)

    def summary(self) -> str:
        """Human-readable "here is what I understood" block for the UI / CLI."""
        lines = [f"parsed {len(self.marks)} mark(s) for {', '.join(self.pairs) or '-'} "
                 f"({self.layout} layout)"]
        lines += [f"  inferred: {s}" for s in self.inferred]
        lines += [f"  WARNING:  {s}" for s in self.warnings]
        lines += [f"  skipped:  {s}" for s in self.skipped]
        lines += ["  " + str(m) for m in self.marks]
        return "\n".join(lines)


def _pair_token(tok: str) -> str | None:
    t = re.sub(r"[^A-Za-z]", "", str(tok)).upper()
    if len(t) == 6 and t in PAIRS:
        return t
    if len(t) == 7 and t[:3] + t[3:] in PAIRS:       # "EUR/USD" already stripped
        return t
    return None


def parse_grid(text: str, pair: str | None = None, *, source: str = "paste",
               asof: datetime | None = None, note: str = "") -> ParsedGrid:
    """Parse a pasted broker grid into :class:`ManualMark` objects.

    Parameters
    ----------
    text : the raw clipboard content.  See the module docstring for accepted shapes.
    pair : default pair for rows that do not carry one.  If ``None``, a 6-letter pair token
        anywhere in the text is used; if that is also absent the parse fails with a clear
        message rather than guessing.
    source, asof, note : provenance carried onto every mark.

    Returns
    -------
    ParsedGrid
        Never raises on messy input -- unparseable lines land in ``skipped`` and unit
        assumptions in ``inferred``.  Only a completely unusable input yields ``ok=False``.
    """
    asof = asof or _now()
    inferred: list[str] = []
    warnings: list[str] = []
    skipped: list[str] = []

    raw_lines = [ln for ln in str(text or "").splitlines()]
    lines = [ln for ln in raw_lines if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return ParsedGrid(warnings=["empty input"])

    saw_percent = "%" in text
    default_pair = (str(pair).upper() if pair else None)
    if default_pair and default_pair not in PAIRS:
        warnings.append(f"{default_pair} is not in conventions.PAIRS; storing it anyway")

    rows: list[list[str]] = [[t for t in _SPLIT.split(ln.strip()) if t] for ln in lines]

    # ---- a pair token on its own line, or the first token of the block, sets the default
    if default_pair is None:
        for r in rows:
            for tok in r:
                p = _pair_token(tok)
                if p:
                    default_pair = p
                    inferred.append(f"pair {p} taken from the pasted text")
                    break
            if default_pair:
                break

    # ---- header / layout detection ---------------------------------------------------
    head_map: dict[int, str] = {}
    body = rows
    layout = "rows"
    first = _merge_header_tokens(rows[0])         # "25d RR" -> "25rr" before anything else
    first_norm = [_norm_head(t) for t in first]
    numeric_in_first = sum(1 for t in rows[0] if _as_float(t) is not None)
    header_like = (numeric_in_first == 0
                   and any(h in _HEADER_WORDS or h in _METRIC_ALIASES for h in first_norm))
    metrics_in_first = any(_METRIC_ALIASES.get(h) in {"atm", "rr25", "bf25", "rr10", "bf10"}
                           for h in first_norm)
    # tenor detection runs on the *merged* tokens so "25d RR" is not read as a 25-day tenor
    tenors_in_first = [(_norm_tenor(t) or "") for t in first]

    if not metrics_in_first and sum(1 for t in tenors_in_first if t) >= 2 \
            and numeric_in_first == 0:
        # column-per-tenor (transposed) broker grid:  header = tenors, rows = ATM/RR/BF
        layout = "columns"
    elif header_like:
        for i, h in enumerate(first_norm):
            if h in _METRIC_ALIASES:
                head_map[i] = _METRIC_ALIASES[h]
        body = rows[1:]
        inferred.append("header row: " + ", ".join(
            f"col{i}={v}" for i, v in sorted(head_map.items())))

    # ---- collect (pair, tenor) -> {metric: raw value} ---------------------------------
    raw: dict[tuple[str, str], dict[str, float]] = {}
    order: list[tuple[str, str]] = []

    def _stash(p: str, tenor: str, metric: str, val: float) -> None:
        key = (p, tenor)
        if key not in raw:
            raw[key] = {}
            order.append(key)
        raw[key][metric] = val

    if layout == "columns":
        tenor_cols = {i: t for i, t in enumerate(tenors_in_first) if t}
        inferred.append("transposed grid: tenors across the top ("
                        + ", ".join(tenor_cols.values()) + ")")
        for r in rows[1:]:
            if not r:
                continue
            label = _METRIC_ALIASES.get(_norm_head(r[0]))
            used = 1
            if label not in {"atm", "rr25", "bf25", "rr10", "bf10"}:
                merged = _merge_header_tokens(r[:2])          # "25d RR" spread over 2 tokens
                label = _METRIC_ALIASES.get(_norm_head(merged[0]) if merged else "")
                used = 2 if len(merged) < len(r[:2]) else 1
            if label not in {"atm", "rr25", "bf25", "rr10", "bf10"}:
                skipped.append(f"row label not recognised: {' '.join(r[:2])!r}")
                continue
            vals = [v for v in (_as_float(t) for t in r[used:]) if v is not None]
            p = default_pair
            if p is None:
                skipped.append("no pair for transposed grid")
                continue
            for j, t in enumerate(tenor_cols.values()):
                if j < len(vals):
                    _stash(p, t, label, vals[j])
    else:
        for r in body:
            if not r:
                continue
            row_pair = default_pair
            toks = list(r)
            p0 = _pair_token(toks[0])
            if p0:
                row_pair = p0
                toks = toks[1:]
            if row_pair is None:
                skipped.append(f"no pair for row {' '.join(r)!r}")
                continue
            # tenor = first token that reads as one
            tenor = None
            for i, t in enumerate(toks):
                tn = _norm_tenor(t)
                if tn:
                    tenor, toks = tn, toks[:i] + toks[i + 1:]
                    break
            if tenor is None:
                skipped.append(f"no tenor in row {' '.join(r)!r}")
                continue
            nums = [v for v in (_as_float(t) for t in toks) if v is not None]
            if not nums:
                skipped.append(f"no numbers in row {' '.join(r)!r}")
                continue
            if head_map:
                # map by header position, counting only the columns we kept
                vals_by_col = {i: _as_float(t) for i, t in enumerate(r)}
                got = False
                for i, metric in head_map.items():
                    v = vals_by_col.get(i)
                    if metric in {"atm", "rr25", "bf25", "rr10", "bf10"} and v is not None:
                        _stash(row_pair, tenor, metric, v)
                        got = True
                if got:
                    continue
            for metric, v in zip(("atm", "rr25", "bf25", "rr10", "bf10"), nums):
                _stash(row_pair, tenor, metric, v)

    if not raw:
        return ParsedGrid(layout=layout, inferred=inferred,
                          warnings=warnings + ["nothing parseable found"], skipped=skipped)

    # ---- unit inference: one scale for the whole grid, taken from the ATM column -------
    atms = [d["atm"] for d in raw.values() if "atm" in d and math.isfinite(d["atm"])]
    if not atms:
        return ParsedGrid(layout=layout, inferred=inferred,
                          warnings=warnings + ["no ATM column found"], skipped=skipped)
    biggest = max(abs(a) for a in atms)
    if saw_percent:
        scale, why = 0.01, "'%' in the input"
    elif biggest > 1.0:
        scale, why = 0.01, f"largest ATM {biggest:g} > 1"
    else:
        scale, why = 1.0, f"largest ATM {biggest:g} <= 1"
    inferred.append(f"vols read as {'percent (8.5 = 8.5%)' if scale == 0.01 else 'decimals (0.085 = 8.5%)'}"
                    f" -- {why}; the same scale is applied to RR and BF")

    marks: list[ManualMark] = []
    for key in order:
        p, tenor = key
        d = raw[key]
        if "atm" not in d:
            skipped.append(f"{p} {tenor}: no ATM")
            continue
        atm = d["atm"] * scale
        if not (_VOL_MIN <= atm <= _VOL_MAX):
            # refuse rather than store: an ATM of 705% would price the whole book insanely
            skipped.append(f"{p} {tenor}: ATM reads as {atm*100:.2f}%, outside the plausible "
                           f"{_VOL_MIN*100:g}-{_VOL_MAX*100:g}% band -- not stored; "
                           "fix the units and paste again")
            warnings.append(f"{p} {tenor}: rejected, ATM {atm*100:.2f}% implausible")
            continue
        rr25 = d.get("rr25", 0.0) * scale
        bf25 = d.get("bf25", 0.0) * scale
        if "rr25" not in d or "bf25" not in d:
            warnings.append(f"{p} {tenor}: missing "
                            f"{'RR' if 'rr25' not in d else ''}"
                            f"{'/' if 'rr25' not in d and 'bf25' not in d else ''}"
                            f"{'BF' if 'bf25' not in d else ''} -- defaulted to 0.0 "
                            "(symmetric smile)")
        if abs(rr25) > atm:
            warnings.append(f"{p} {tenor}: |RR25| {rr25*100:.2f} exceeds ATM {atm*100:.2f} "
                            "-- sign/unit likely wrong")
        if bf25 < 0.0:
            warnings.append(f"{p} {tenor}: BF25 {bf25*100:.2f} is negative -- smile "
                            "butterflies are normally positive (market strangle?)")
        try:
            marks.append(ManualMark(
                pair=p, tenor=tenor, atm=atm, rr25=rr25, bf25=bf25,
                rr10=d["rr10"] * scale if "rr10" in d else None,
                bf10=d["bf10"] * scale if "bf10" in d else None,
                asof=asof, source=source, note=note))
        except ValueError as exc:
            skipped.append(str(exc))

    marks.sort(key=lambda m: (m.pair, m.T))
    return ParsedGrid(marks=marks, scale=scale, layout=layout, inferred=inferred,
                      warnings=warnings, skipped=skipped)


# ======================================================================== store
@dataclass(frozen=True)
class _Override:
    value: float
    asof: datetime
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "asof": self.asof.isoformat(), "note": self.note}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "_Override":
        return cls(float(d["value"]), _parse_dt(d.get("asof")), d.get("note", ""))


class ManualQuoteStore:
    """JSON-backed store of the desk's marks.  Safe to construct in the Dash callback path.

    Layout on disk::

        {"schema": "fxgamma.manual/1", "updated": "...",
         "marks": {"EURUSD": [{"tenor": "1M", "atm": 0.0705, ...}, ...]},
         "spot":  {"EURUSD": {"value": 1.0850, "asof": "...", "note": ""}},
         "rates": {"USD":    {"value": 0.0400, "asof": "...", "note": ""}}}

    A corrupt file raises on load by default (``strict=True``): silently starting from an
    empty grid would send the book straight back to ETF vols, which is exactly the failure
    the trader objected to.
    """

    def __init__(self, path: str | Path | None = None, *, strict: bool = True,
                 autoload: bool = True):
        self.path = Path(path) if path is not None else default_marks_path()
        self.strict = strict
        self.marks: dict[str, dict[str, ManualMark]] = {}
        self.spot_overrides: dict[str, _Override] = {}
        self.rate_overrides: dict[str, _Override] = {}
        self.updated: datetime | None = None
        if autoload:
            self.load()

    # ------------------------------------------------------------------ persistence
    def load(self) -> "ManualQuoteStore":
        self.marks, self.spot_overrides, self.rate_overrides = {}, {}, {}
        if not self.path.exists():
            log.debug("no manual marks file at %s (that is fine)", self.path)
            return self
        try:
            blob = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            msg = f"manual marks file {self.path} is unreadable: {exc}"
            if self.strict:
                raise ValueError(msg + " -- fix or delete it; refusing to run unmarked") from exc
            log.error("%s; continuing with an EMPTY manual grid", msg)
            return self
        if str(blob.get("schema", SCHEMA)) != SCHEMA:
            log.warning("manual marks schema %r != %r; reading best-effort",
                        blob.get("schema"), SCHEMA)
        for p, rows in (blob.get("marks") or {}).items():
            for row in rows:
                try:
                    m = ManualMark.from_dict({**row, "pair": row.get("pair", p)})
                except (KeyError, ValueError) as exc:
                    log.warning("dropping bad manual mark %s %s: %s", p, row, exc)
                    continue
                self.marks.setdefault(m.pair, {})[m.tenor] = m
        for k, v in (blob.get("spot") or {}).items():
            self.spot_overrides[k.upper()] = _Override.from_dict(v)
        for k, v in (blob.get("rates") or {}).items():
            self.rate_overrides[k.upper()] = _Override.from_dict(v)
        self.updated = _parse_dt(blob.get("updated")) if blob.get("updated") else None
        log.info("loaded %d manual marks for %s from %s", self.count(),
                 ", ".join(self.pairs()) or "-", self.path)
        return self

    def save(self) -> Path:
        """Atomic write.  Returns the path written."""
        blob = {
            "schema": SCHEMA,
            "updated": _now().isoformat(),
            "marks": {p: [m.to_dict() for m in self.get(p)] for p in self.pairs()},
            "spot": {k: v.to_dict() for k, v in sorted(self.spot_overrides.items())},
            "rates": {k: v.to_dict() for k, v in sorted(self.rate_overrides.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(blob, indent=2, sort_keys=False))
        os.replace(tmp, self.path)
        self.updated = _parse_dt(blob["updated"])
        return self.path

    # ------------------------------------------------------------------ read
    def pairs(self) -> list[str]:
        return sorted(p for p, d in self.marks.items() if d)

    def count(self) -> int:
        return sum(len(d) for d in self.marks.values())

    def get(self, pair: str) -> list[ManualMark]:
        """All marks for `pair`, sorted by expiry.  Empty list if the pair is unmarked."""
        return sorted(self.marks.get(str(pair).upper(), {}).values(), key=lambda m: m.T)

    def mark(self, pair: str, tenor: str) -> ManualMark | None:
        t = _norm_tenor(tenor)
        return self.marks.get(str(pair).upper(), {}).get(t or "")

    def smile_quotes(self, pair: str) -> list[SmileQuotes]:
        return [m.to_smile_quotes() for m in self.get(pair)]

    def newest(self, pair: str | None = None) -> datetime | None:
        ms = self.get(pair) if pair else [m for d in self.marks.values() for m in d.values()]
        return max((m.asof for m in ms), default=None)

    def age_hours(self, pair: str) -> float | None:
        n = self.newest(pair)
        return None if n is None else (_now() - n).total_seconds() / 3600.0

    # ------------------------------------------------------------------ write
    def put(self, marks: Iterable[ManualMark], *, replace_pair: bool = False,
            save: bool = True) -> int:
        """Insert/overwrite marks.  ``replace_pair`` wipes each touched pair first."""
        ms = list(marks)
        if replace_pair:
            for p in {m.pair for m in ms}:
                self.marks.pop(p, None)
        for m in ms:
            self.marks.setdefault(m.pair, {})[m.tenor] = m
        if save:
            self.save()
        return len(ms)

    def set_mark(self, pair: str, tenor: str, atm: float, rr25: float = 0.0,
                 bf25: float = 0.0, *, rr10: float | None = None, bf10: float | None = None,
                 source: str = "typed", note: str = "", asof: datetime | None = None,
                 save: bool = True) -> ManualMark:
        """Type one cell.  Vols are decimals; pass 0.0705 for 7.05%."""
        m = ManualMark(pair=pair, tenor=tenor, atm=atm, rr25=rr25, bf25=bf25,
                       rr10=rr10, bf10=bf10, asof=asof or _now(), source=source, note=note)
        self.put([m], save=save)
        return m

    def paste(self, text: str, pair: str | None = None, *, replace_pair: bool = True,
              save: bool = True, note: str = "") -> ParsedGrid:
        """Parse a pasted grid and store it.  Returns the report -- **show it to the user**."""
        res = parse_grid(text, pair, source="paste", note=note)
        if res.marks:
            self.put(res.marks, replace_pair=replace_pair, save=save)
        return res

    def clear(self, pair: str | None = None, tenor: str | None = None, *,
              save: bool = True) -> int:
        """Remove marks.  Returns how many were removed."""
        n = 0
        if pair is None:
            n, self.marks = self.count(), {}
        else:
            p = str(pair).upper()
            if tenor is None:
                n = len(self.marks.pop(p, {}))
            elif _norm_tenor(tenor) in self.marks.get(p, {}):
                self.marks[p].pop(_norm_tenor(tenor))
                n = 1
        if save:
            self.save()
        return n

    # ------------------------------------------------------------------ overrides
    def set_spot(self, pair: str, value: float, note: str = "", *, save: bool = True) -> None:
        self.spot_overrides[str(pair).upper()] = _Override(float(value), _now(), note)
        if save:
            self.save()

    def set_rate(self, ccy: str, value: float, note: str = "", *, save: bool = True) -> None:
        """Continuously-compounded zero rate, decimal (0.04 = 4%)."""
        self.rate_overrides[str(ccy).upper()] = _Override(float(value), _now(), note)
        if save:
            self.save()

    def clear_overrides(self, *, save: bool = True) -> int:
        n = len(self.spot_overrides) + len(self.rate_overrides)
        self.spot_overrides, self.rate_overrides = {}, {}
        if save:
            self.save()
        return n

    # ------------------------------------------------------------------ frames / CSV
    def as_frame(self, pair: str | None = None) -> pd.DataFrame:
        rows = [m.as_row() for m in (self.get(pair) if pair else
                                     [m for p in self.pairs() for m in self.get(p)])]
        return pd.DataFrame(rows, columns=MARK_COLUMNS) if rows else \
            pd.DataFrame(columns=MARK_COLUMNS)

    def to_csv(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.as_frame().to_csv(p, index=False)
        return p

    def from_csv(self, path: str | Path, *, replace_pair: bool = True,
                 save: bool = True) -> int:
        """Read back a file written by :meth:`to_csv` (already decimal vols)."""
        # round_trip: the default parser is 1 ULP lossy, so a save/load cycle would not be
        # bit-identical and any test asserting "the mark did not change" would flap.
        df = pd.read_csv(path, float_precision="round_trip")
        cols = {c.lower().strip(): c for c in df.columns}
        need = [c for c in ("pair", "tenor", "atm") if c not in cols]
        if need:
            raise ValueError(f"{path}: missing columns {need}")
        ms = []
        for r in df.to_dict("records"):
            g = lambda k, d=None: r.get(cols.get(k, ""), d)                  # noqa: E731
            ms.append(ManualMark(pair=g("pair"), tenor=g("tenor"), atm=float(g("atm")),
                                 rr25=float(g("rr25", 0.0) or 0.0),
                                 bf25=float(g("bf25", 0.0) or 0.0),
                                 rr10=g("rr10"), bf10=g("bf10"),
                                 asof=g("asof") or _now(), source=g("source", "csv") or "csv",
                                 note=g("note", "") or ""))
        return self.put(ms, replace_pair=replace_pair, save=save)

    def __repr__(self) -> str:                                       # pragma: no cover
        return (f"<ManualQuoteStore {self.path} pairs={self.pairs()} "
                f"marks={self.count()}>")


# ======================================================================== provider
class ManualQuoteProvider(MarketDataProvider):
    """The desk's own marks, served as a provider.  First link of :class:`ChainProvider`.

    Only ``smile_quotes`` (and the optional spot/rate overrides) return anything; every other
    method returns empty so the chain falls through to live/cache/synthetic for spot history,
    open interest and the calendar.  Everything it does return is badged
    ``Provenance(kind="user_override")`` per contract s7.

    Programmatic API for the Data page::

        p = ManualQuoteProvider()
        rep = p.paste(clipboard, pair="EURUSD")     # -> ParsedGrid, show rep.summary()
        p.set_mark("USDJPY", "1M", 0.0925, -0.0125, 0.0030)
        p.store.as_frame()                          # editable grid for a DataTable
        p.marked("EURUSD"), p.age_hours("EURUSD")   # badge + staleness
        p.clear("EURUSD")
    """

    name = "manual"
    kind = "user_override"

    #: marks older than this are still served but the badge says how stale they are
    STALE_WARN_HOURS = 12.0

    def __init__(self, store: ManualQuoteStore | None = None, *,
                 path: str | Path | None = None, strict: bool = True,
                 max_age_hours: float | None = None):
        self.store = store if store is not None else ManualQuoteStore(path, strict=strict)
        self.max_age_hours = max_age_hours
        self._prov: dict[str, Provenance] = {}

    # ------------------------------------------------------------------ provenance
    def _record(self, field_key: str, note: str, asof: datetime | None = None) -> None:
        self._prov[field_key] = Provenance(source=self.name, kind=self.kind,
                                           asof=asof or utcnow(), note=note)

    def provenance(self, field: str, kind: str | None = None, note: str = "",
                   asof: datetime | None = None) -> Provenance:
        hit = self._prov.get(field)
        if hit is None:
            return Provenance(self.name, kind or self.kind, asof or utcnow(), note)
        if not note:
            return hit
        return Provenance(hit.source, hit.kind, hit.asof,
                          "; ".join(x for x in (hit.note, note) if x))

    # ------------------------------------------------------------------ the mark
    def smile_quotes(self, pair: str, asof: datetime | None = None) -> list[SmileQuotes]:
        marks = self.store.get(pair)
        if not marks:
            return []
        age = max(m.age_hours() for m in marks)
        if self.max_age_hours is not None and age > self.max_age_hours:
            log.warning("manual marks for %s are %.1fh old (> max_age_hours=%.1f); "
                        "declining so the chain falls through to live",
                        pair, age, self.max_age_hours)
            return []
        newest = max(m.asof for m in marks)
        note = (f"manual mark: {len(marks)} tenors ({', '.join(m.tenor for m in marks)}), "
                f"entered {newest:%Y-%m-%d %H:%MZ} ({age:.1f}h ago)")
        if age > self.STALE_WARN_HOURS:
            note += " -- STALE, re-mark before hedging"
        self._record(f"surface.{pair}", note, asof=newest)
        for m in marks:
            self._record(f"surface.{pair}.{m.tenor}",
                         f"manual {m.tenor} ATM {m.atm*100:.3f} / RR25 {m.rr25*100:+.3f} / "
                         f"BF25 {m.bf25*100:.3f}", asof=m.asof)
        return [m.to_smile_quotes() for m in marks]

    # ------------------------------------------------------------------ overrides
    def spot(self, pairs: Sequence[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in pairs:
            ov = self.store.spot_overrides.get(str(p).upper())
            if ov is not None:
                out[str(p).upper()] = ov.value
                self._record(f"spot.{p}", f"manual spot override {ov.note}".strip(),
                             asof=ov.asof)
        return out

    def rates(self, ccys: Sequence[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for c in ccys:
            ov = self.store.rate_overrides.get(str(c).upper())
            if ov is not None:
                out[str(c).upper()] = ov.value
                self._record(f"rate.{c}", f"manual rate override {ov.note}".strip(),
                             asof=ov.asof)
        return out

    # ------------------------------------------------------------------ not ours
    def spot_history(self, pair: str, start: date, end: date) -> pd.DataFrame:
        return empty_spot_frame()

    def open_interest(self, pair: str, asof: datetime | None = None) -> pd.DataFrame:
        return empty_oi_frame()

    def events(self, start: date, end: date) -> pd.DataFrame:
        return empty_event_frame()

    # ------------------------------------------------------------------ convenience
    def paste(self, text: str, pair: str | None = None, *, replace_pair: bool = True,
              note: str = "") -> ParsedGrid:
        return self.store.paste(text, pair, replace_pair=replace_pair, note=note)

    def set_mark(self, *a, **kw) -> ManualMark:
        return self.store.set_mark(*a, **kw)

    def clear(self, pair: str | None = None, tenor: str | None = None) -> int:
        return self.store.clear(pair, tenor)

    def marked(self, pair: str) -> bool:
        """True when the book should price `pair` off the desk's own curve."""
        return bool(self.store.get(pair))

    def age_hours(self, pair: str) -> float | None:
        return self.store.age_hours(pair)

    def status(self) -> list[SourceStatus]:
        if not self.store.pairs():
            return [SourceStatus("manual-marks", False,
                                 f"no marks in {self.store.path} -- the book will price off "
                                 f"the next provider in the chain (ETF/indicative)",
                                 verified=True, rows=0)]
        rows = []
        for p in self.store.pairs():
            ms = self.store.get(p)
            age = self.store.age_hours(p) or 0.0
            rows.append(SourceStatus(
                f"manual-marks:{p}", True,
                f"{len(ms)} tenors ({', '.join(m.tenor for m in ms)}), {age:.1f}h old"
                + (" -- STALE" if age > self.STALE_WARN_HOURS else ""),
                verified=True, rows=len(ms)))
        return rows
