"""Session state: one stamped snapshot, one store, one set of manual marks.

REQ-001 wants a **single stamped snapshot per session**: every page reads the same
``MarketSnapshot``, no page fetches on its own, and Refresh restamps once.  A
``VolSurface`` is not JSON-serialisable, so the snapshot itself lives here in a
server-side registry keyed by a snapshot id and the ``dcc.Store`` carries the id (plus
the JSON-safe summary the header needs).  Single-user desk app, single process: the
registry is a dict with a lock.

Amendment v1.2 T-1 is enforced in :meth:`Session.build`: after the provider has answered,
any pair the trader has marked has its surface **rebuilt from the manual grid** and
re-badged ``user_override``.  A live pull can never overwrite the desk's own mark, and a
manual mark is applied on the synthetic provider too - otherwise the marking workflow
could not be exercised offline, which is the only way this build can run.
"""
from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

from fxgamma.conventions import CROSSES, G10_PAIRS, PAIRS, TENORS
from fxgamma.data import get_cache, get_provider
from fxgamma.data.base import surface_backend
from fxgamma.models.surface import build_surface
from fxgamma.store import ManualQuote, MarketContext, Store, get_store
from fxgamma.types import MarketSnapshot, Provenance

log = logging.getLogger(__name__)

ALL_PAIRS = G10_PAIRS + CROSSES
DEFAULT_PAIRS = ALL_PAIRS
TENOR_ORDER = [t for t in TENORS]
G3_TENORS = ["ON", "1W", "2W", "1M", "2M", "3M", "6M", "1Y"]

DEFAULT_SETTINGS = {
    "report_ccy": "USD",
    "timezone": "Europe/London",
    "pair": "EURUSD",
    "surface_method": "vanna_volga",
    "notional_unit": "mm",
    "day_boundary": "17:00 America/New_York",
}


class Session:
    """Everything the callbacks share. One instance per process."""

    def __init__(self, provider: str = "synthetic", *, db: str | None = None,
                 seed_demo: bool = True, pairs: list[str] | None = None):
        self.store: Store = get_store(db, fresh=db is not None)
        self.provider_name = provider
        self.pairs = list(pairs or DEFAULT_PAIRS)
        self._snapshots: "OrderedDict[str, MarketSnapshot]" = OrderedDict()
        self._current: str | None = None
        self._lock = threading.RLock()
        self._history: dict[str, pd.DataFrame] = {}
        self._oi: dict[str, pd.DataFrame] = {}
        self._events: pd.DataFrame | None = None
        self.errors: list[str] = []
        self.manual = self._make_manual()
        self._sync_marks_from_store()
        if seed_demo and self.store.is_empty():
            try:
                self.store.seed_demo_book()
                self.demo_seeded = True
            except Exception as exc:                       # noqa: BLE001
                log.warning("demo book seed failed: %s", exc)
                self.demo_seeded = False
        else:
            self.demo_seeded = False
        for k, v in DEFAULT_SETTINGS.items():
            if self.store.get_setting(k) is None:
                self.store.set_setting(k, v)

    # ---------------------------------------------------------------- manual marks
    def _make_manual(self):
        try:
            from fxgamma.data import ManualQuoteProvider
            return ManualQuoteProvider()
        except Exception as exc:                           # noqa: BLE001
            log.warning("manual quote provider unavailable: %s", exc)
            return None

    def _sync_marks_from_store(self) -> int:
        """SQLite is the book of record for marks; the data layer's file feeds the chain."""
        if self.manual is None:
            return 0
        try:
            rows = self.store.manual_quotes()
            have = {(m.pair, m.tenor) for p in self.manual.store.pairs()
                    for m in self.manual.store.get(p)}
            n = 0
            for q in rows:
                if (q.pair, q.tenor) in have:
                    continue
                self.manual.set_mark(q.pair, q.tenor, q.atm, q.rr25, q.bf25,
                                     rr10=q.rr10, bf10=q.bf10, source=q.source or "store",
                                     note=q.note)
                n += 1
            return n
        except Exception as exc:                           # noqa: BLE001
            log.warning("mark sync failed: %s", exc)
            return 0

    def save_marks(self, marks: list[ManualQuote], *, note: str = "") -> int:
        """Persist a paste to **both** homes: SQLite (versioned) and the provider file."""
        asof = datetime.now(timezone.utc)
        self.store.save_manual_quotes(marks, asof=asof)
        if self.manual is not None:
            for q in marks:
                self.manual.set_mark(q.pair, q.tenor, q.atm, q.rr25, q.bf25,
                                     rr10=q.rr10, bf10=q.bf10,
                                     source=q.source or "paste", note=note or q.note)
        return len(marks)

    def clear_marks(self, pair: str | None = None, tenor: str | None = None) -> int:
        n = self.store.clear_manual_quotes(pair, tenor)
        if self.manual is not None:
            try:
                self.manual.clear(pair, tenor)
            except Exception as exc:                       # noqa: BLE001
                log.warning("clearing provider marks failed: %s", exc)
        return n

    def marked_pairs(self) -> list[str]:
        if self.manual is None:
            return self.store.manual_quote_pairs()
        try:
            return sorted(set(self.manual.store.pairs()) | set(self.store.manual_quote_pairs()))
        except Exception:                                  # noqa: BLE001
            return self.store.manual_quote_pairs()

    # ---------------------------------------------------------------- provider
    def make_provider(self):
        name = self.provider_name
        if name in ("chain", "auto", "live", "cache"):
            return get_provider(name, manual=self.manual if self.manual else False)
        return get_provider(name)

    # ---------------------------------------------------------------- snapshot
    def build(self, *, method: str | None = None, pairs: list[str] | None = None
              ) -> MarketSnapshot:
        """Stamp one snapshot. Every failure degrades and is recorded, never raised."""
        method = method or self.store.get_setting("surface_method", "vanna_volga")
        pairs = list(pairs or self.pairs)
        self.errors = []
        provider = self.make_provider()
        try:
            snap = provider.snapshot(pairs, method=method)
        except Exception as exc:                           # noqa: BLE001
            log.exception("snapshot failed")
            self.errors.append(f"{self.provider_name} provider failed: {exc}")
            snap = MarketSnapshot(asof=datetime.now(timezone.utc))
        self._apply_manual(snap, method)
        sid = f"snap-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:4]}"
        snap.meta.setdefault("snapshot", Provenance(
            source=self.provider_name, kind=getattr(provider, "kind", "unknown"),
            asof=snap.asof, note=f"id={sid}; surface backend {surface_backend()}"))
        with self._lock:
            self._snapshots[sid] = snap
            self._current = sid
            while len(self._snapshots) > 6:
                self._snapshots.popitem(last=False)
        self._history.clear()
        self._oi.clear()
        return snap

    def _apply_manual(self, snap: MarketSnapshot, method: str) -> None:
        """v1.2 T-1: the desk's own mark always wins, on every provider."""
        if self.manual is None:
            return
        try:
            pairs = self.manual.store.pairs()
        except Exception:                                  # noqa: BLE001
            return
        for pair in pairs:
            if pair not in PAIRS:
                continue
            marks = self.manual.store.get(pair)
            if not marks:
                continue
            S = snap.spot.get(pair)
            spec = PAIRS[pair]
            if S is None or spec.quote not in snap.rates or spec.base not in snap.rates:
                snap.meta[f"surface.{pair}"] = Provenance(
                    "manual", "unavailable", snap.asof,
                    "manual marks exist but the spot/rates to build the surface do not")
                continue
            rd, rf = snap.rates[spec.quote], snap.rates[spec.base]
            try:
                quotes = [m.to_smile_quotes() for m in marks]
                snap.surfaces[pair] = build_surface(pair, snap.asof, quotes, float(S),
                                                    rd, rf, method=method)
            except Exception as exc:                       # noqa: BLE001
                self.errors.append(f"manual surface for {pair} failed to build: {exc}")
                snap.meta[f"surface.{pair}"] = Provenance(
                    "manual", "unavailable", snap.asof, f"build_surface failed: {exc}")
                continue
            newest = max(m.asof for m in marks)
            age = (datetime.now(timezone.utc) - newest).total_seconds() / 3600.0
            note = (f"YOUR MARK: {len(marks)} tenors ({', '.join(m.tenor for m in marks)}), "
                    f"entered {newest:%Y-%m-%d %H:%MZ} ({age:.1f}h ago)"
                    + (" - STALE, re-mark before hedging" if age > 12 else ""))
            snap.meta[f"surface.{pair}"] = Provenance("manual", "user_override", newest, note)
            for m in marks:
                snap.meta[f"surface.{pair}.{m.tenor}"] = Provenance(
                    "manual", "user_override", m.asof,
                    f"ATM {m.atm * 100:.3f}% / RR25 {m.rr25 * 100:+.3f} / "
                    f"BF25 {m.bf25 * 100:.3f} vol pts")
        # spot / rate overrides travel with the marks file
        try:
            for p, ov in getattr(self.manual.store, "spot_overrides", {}).items():
                if p in snap.spot:
                    snap.spot[p] = float(ov.value)
                    snap.meta[f"spot.{p}"] = Provenance("manual", "user_override", ov.asof,
                                                        f"manual spot override {ov.note}")
            for c, ov in getattr(self.manual.store, "rate_overrides", {}).items():
                if c in snap.rates:
                    snap.rates[c] = float(ov.value)
                    snap.meta[f"rate.{c}"] = Provenance("manual", "user_override", ov.asof,
                                                        f"manual rate override {ov.note}")
        except Exception as exc:                           # noqa: BLE001
            log.warning("applying manual overrides failed: %s", exc)

    def snapshot(self, sid: str | None = None) -> MarketSnapshot:
        """The stamped snapshot. Builds one on first use so no page ever sees ``None``."""
        with self._lock:
            if sid and sid in self._snapshots:
                return self._snapshots[sid]
            if self._current and self._current in self._snapshots:
                return self._snapshots[self._current]
        return self.build()

    @property
    def snapshot_id(self) -> str:
        return self._current or ""

    def token(self) -> dict[str, Any]:
        """The JSON-safe half that travels in ``dcc.Store``."""
        snap = self.snapshot()
        return {"id": self.snapshot_id, "asof": snap.asof.isoformat(),
                "provider": self.provider_name, "pairs": list(snap.spot),
                "n_synthetic": sum(1 for p in snap.meta.values()
                                   if getattr(p, "kind", "") == "synthetic"),
                "n_override": sum(1 for p in snap.meta.values()
                                  if getattr(p, "kind", "") == "user_override"),
                "errors": list(self.errors)}

    def ctx(self) -> MarketContext:
        return MarketContext.from_snapshot(self.snapshot())

    # ---------------------------------------------------------------- market data
    def history(self, pair: str, years: float = 3.0) -> pd.DataFrame:
        pair = pair.upper()
        if pair in self._history:
            return self._history[pair]
        end = self.snapshot().asof.date()
        start = end - timedelta(days=int(365 * years))
        try:
            df = self.make_provider().spot_history(pair, start, end)
        except Exception as exc:                           # noqa: BLE001
            log.warning("spot_history(%s) failed: %s", pair, exc)
            self.errors.append(f"spot history for {pair}: {exc}")
            df = pd.DataFrame()
        self._history[pair] = df
        return df

    def open_interest(self, pair: str) -> pd.DataFrame:
        pair = pair.upper()
        if pair in self._oi:
            return self._oi[pair]
        try:
            df = self.make_provider().open_interest(pair, self.snapshot().asof)
        except Exception as exc:                           # noqa: BLE001
            log.warning("open_interest(%s) failed: %s", pair, exc)
            df = pd.DataFrame()
        self._oi[pair] = df
        return df

    def events(self, days: int = 60) -> pd.DataFrame:
        if self._events is not None:
            return self._events
        today = self.snapshot().asof.date()
        try:
            df = self.make_provider().events(today - timedelta(days=5),
                                             today + timedelta(days=days))
        except Exception as exc:                           # noqa: BLE001
            log.warning("events failed: %s", exc)
            df = pd.DataFrame()
        self._events = df
        return df

    def source_status(self) -> list[Any]:
        rows = []
        try:
            rows += list(self.make_provider().status())
        except Exception as exc:                           # noqa: BLE001
            from fxgamma.data.base import SourceStatus
            rows.append(SourceStatus(self.provider_name, False, str(exc)[:200]))
        if self.manual is not None:
            try:
                rows += list(self.manual.status())
            except Exception:                              # noqa: BLE001
                pass
        return rows

    def cache(self):
        return get_cache()

    # ---------------------------------------------------------------- settings
    def setting(self, key: str, default: Any = None) -> Any:
        v = self.store.get_setting(key, None)
        return DEFAULT_SETTINGS.get(key, default) if v is None else v

    def set_setting(self, key: str, value: Any) -> None:
        self.store.set_setting(key, value)

    @property
    def report_ccy(self) -> str:
        return str(self.setting("report_ccy", "USD"))


_session: Session | None = None
_session_lock = threading.Lock()


def get_session(**kw) -> Session:
    global _session
    with _session_lock:
        if _session is None:
            _session = Session(**kw)
        return _session


def set_session(s: Session) -> Session:
    global _session
    with _session_lock:
        _session = s
    return _session
