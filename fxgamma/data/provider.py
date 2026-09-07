"""Provider factory and the live -> cache -> (synthetic) fallback chain.

Contract section 7 is the design driver: **never silently substitute synthetic data for live
data.**  Amendment v1.2 (T-1) adds the second driver: **the desk's own mark always wins.**  So:

* :class:`~fxgamma.data.manual.ManualQuoteProvider` is the *first* link of every chain.  A
  pair the user has marked is priced off that grid and a live pull can never overwrite it
  (the marks live in their own file; nothing else writes it).

* :class:`LiveProvider` fans out over the real adapters with a per-source fallback chain
  (Yahoo -> Stooq -> ECB for spot; CME -> ETF chain for open interest) and records, per field,
  exactly which upstream served the number.
* :class:`CacheProvider` replays the on-disk cache and badges everything ``cached``.
* :class:`ChainProvider` walks a list of providers in order and remembers which one answered
  for each field, so ``MarketSnapshot.meta`` says ``yahoo/live`` for one pair and
  ``stooq/cached`` for the next.  It only reaches :class:`SyntheticProvider` when the caller
  passed ``allow_synthetic=True``; the resulting badges say ``synthetic`` and the UI shows
  them as such.

Provider names accepted by :func:`get_provider`:

    ``synthetic``  deterministic offline market (default in this sandbox and in CI)
    ``manual``     the user's own vol marks only (``data/manual/marks.json``); nothing else
    ``live``       real endpoints only; raises rather than inventing anything
    ``cache``      on-disk replay only, no network
    ``chain``      manual -> live -> cache             (production default; v1.2 T-1 order)
    ``auto``       manual -> live -> cache -> synthetic (explicitly badged; for demos)
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Sequence

import pandas as pd

from ..conventions import PAIRS, pair_spec
from ..types import Provenance
from . import cme_options, events as events_mod, rates_fred, spot_ecb, spot_stooq
from . import spot_yahoo, vol_etf_options
from .manual import ManualQuoteProvider, ManualQuoteStore
from .base import (MarketDataProvider, SmileQuotes, SourceStatus, empty_oi_frame,
                   empty_spot_frame, utcnow)
from .cache import Cache, get_cache
from .synthetic import SyntheticProvider

log = logging.getLogger(__name__)

__all__ = ["LiveProvider", "CacheProvider", "ChainProvider", "ManualQuoteProvider",
           "ManualQuoteStore", "get_provider", "PROVIDERS"]


class _Badged(MarketDataProvider):
    """Mixin: remember which upstream served each field, and badge from that."""

    def __init__(self):
        self._prov: dict[str, Provenance] = {}

    def _record(self, field: str, source: str, kind: str, note: str = "") -> None:
        self._prov[field] = Provenance(source=source, kind=kind, asof=utcnow(), note=note)

    def provenance(self, field: str, kind: str | None = None, note: str = "",
                   asof: datetime | None = None) -> Provenance:
        hit = self._prov.get(field)
        if hit is not None:
            return hit if not note else Provenance(hit.source, hit.kind, hit.asof,
                                                   (hit.note + "; " + note).strip("; "))
        return Provenance(self.name, kind or self.kind, asof or utcnow(), note)


# ======================================================================== live
class LiveProvider(_Badged):
    """Real public endpoints, with a documented fallback chain per data type.

    Every adapter it calls is marked UNVERIFIED (see each module's docstring): none of them
    could be exercised against the live hosts from the build sandbox.  Run
    ``python scripts/verify_live_sources.py`` on a networked machine before relying on this.
    """

    name = "live"
    kind = "live"

    #: spot fallback order. Yahoo has OHLC + intraday; Stooq has OHLC daily; ECB is the
    #: official single daily fix (open=high=low=close) and is the last resort.
    SPOT_CHAIN = (("yahoo", spot_yahoo), ("stooq", spot_stooq), ("ecb", spot_ecb))

    def __init__(self, cache: Cache | None = None, *, allow_static_rates: bool = False,
                 calendar_path: str | None = None):
        super().__init__()
        self.cache = cache if cache is not None else get_cache()
        self.allow_static_rates = allow_static_rates
        self.calendar_path = calendar_path
        self._crumb: str | None = None

    # ------------------------------------------------------------------ spot
    def spot_history(self, pair: str, start: date, end: date) -> pd.DataFrame:
        key = f"{pair}_{start:%Y%m%d}_{end:%Y%m%d}"
        cached = self.cache.get("spot", key)
        if cached is not None and not cached.empty:
            self._record(f"spot_history.{pair}", "cache", "cached")
            return cached
        errors = []
        for src, mod in self.SPOT_CHAIN:
            try:
                df = mod.spot_history(pair, start, end)
            except Exception as exc:                    # noqa: BLE001
                errors.append(f"{src}: {exc}")
                continue
            if df is None or df.empty:
                errors.append(f"{src}: empty")
                continue
            note = ("ECB daily fix: open=high=low=close, no intraday range"
                    if src == "ecb" else "")
            self.cache.put("spot", key, df, source=src, note=note)
            self._record(f"spot_history.{pair}", src, "live", note)
            return df
        stale = self.cache.get_stale("spot", key)
        if stale is not None:
            df, entry = stale
            self._record(f"spot_history.{pair}", entry.source or "cache", "cached",
                         f"stale {entry.age()/3600:.1f}h; live failed: {'; '.join(errors)}")
            return df
        raise RuntimeError(f"spot_history({pair}) failed on every source: {'; '.join(errors)}")

    def spot(self, pairs: Sequence[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        remaining = [p for p in pairs]
        for src, mod in self.SPOT_CHAIN:
            if not remaining:
                break
            try:
                got = mod.spot(remaining)
            except Exception as exc:                    # noqa: BLE001
                log.info("spot via %s failed: %s", src, exc)
                continue
            for p, v in (got or {}).items():
                if v == v:
                    out[p] = float(v)
                    self._record(f"spot.{p}", src, "live",
                                 "ECB 14:15 CET fix, not tradable" if src == "ecb" else "")
            remaining = [p for p in remaining if p not in out]
        for p in remaining:                              # last resort: newest cached bar
            stale = self.cache.get_stale("spot", f"last_{p}")
            if stale is not None and not stale[0].empty:
                out[p] = float(stale[0]["close"].iloc[-1])
                self._record(f"spot.{p}", stale[1].source or "cache", "cached",
                             f"stale {stale[1].age()/3600:.1f}h")
        for p, v in out.items():
            self.cache.put("spot", f"last_{p}",
                           pd.DataFrame({"close": [v]},
                                        index=pd.DatetimeIndex([pd.Timestamp.now(tz="UTC")],
                                                               name="date")),
                           source=self._prov.get(f"spot.{p}", Provenance("?")).source)
        return out

    # ------------------------------------------------------------------ rates
    def rates(self, ccys: Sequence[str]) -> dict[str, float]:
        try:
            vals, srcs = rates_fred.rates(ccys, allow_static=self.allow_static_rates)
        except Exception as exc:                        # noqa: BLE001
            log.warning("FRED rates failed wholesale: %s", exc)
            vals, srcs = {}, {}
        for c, v in vals.items():
            if srcs.get(c) == "static-fallback":
                self._record(f"rate.{c}", "static", "user_override",
                             "no live series resolved; STATIC fallback level")
            else:
                self._record(f"rate.{c}", "fred", "live", f"FRED {srcs.get(c, '?')}")
        missing = [c for c in ccys if c not in vals]
        for c in missing:
            stale = self.cache.get_stale("rates", c)
            if stale is not None and not stale[0].empty:
                vals[c] = float(stale[0].iloc[-1, 0])
                self._record(f"rate.{c}", stale[1].source or "cache", "cached",
                             f"stale {stale[1].age()/3600:.1f}h")
        for c, v in vals.items():
            self.cache.put("rates", c, pd.DataFrame({"rate": [v]}), source=srcs.get(c, "fred"))
        return vals

    # ------------------------------------------------------------------ vol
    def smile_quotes(self, pair: str, asof: datetime | None = None) -> list[SmileQuotes]:
        asof = asof or utcnow()
        if self._crumb is None:
            self._crumb = vol_etf_options.fetch_crumb() or ""
        try:
            etf, _, beta = vol_etf_options.etf_for(pair)
        except KeyError as exc:
            self._record(f"surface.{pair}", "none", "unavailable", str(exc))
            return []
        try:
            q = vol_etf_options.smile_quotes(pair, asof, crumb=self._crumb or None)
        except Exception as exc:                        # noqa: BLE001
            self._record(f"surface.{pair}", "yahoo-etf", "unavailable", str(exc)[:150])
            return []
        note = f"listed {etf} option chain (American IV, ETF basis)"
        if beta != 1.0:
            note += f"; scaled by vol beta {beta} -- MODELLED, not observed"
        self._record(f"surface.{pair}", "yahoo-etf", "live", note)
        return q

    # ------------------------------------------------------------------ open interest
    def open_interest(self, pair: str, asof: datetime | None = None) -> pd.DataFrame:
        asof = asof or utcnow()
        spot = self.spot([pair]).get(pair)
        if spot is None:
            self._record(f"oi.{pair}", "none", "unavailable", "no spot to normalise strikes")
            return empty_oi_frame()
        try:
            df = cme_options.open_interest(pair, asof, spot=spot)
            self._record(f"oi.{pair}", "cme", "live",
                         "CME settlements: options on futures, T+1, futures strikes")
            self.cache.put("oi", f"{pair}_{asof:%Y%m%d}", df, source="cme")
            return df
        except Exception as exc:                        # noqa: BLE001
            log.info("CME OI for %s failed: %s", pair, exc)
        try:
            df = vol_etf_options.open_interest(pair, asof, crumb=self._crumb or None)
            self._record(f"oi.{pair}", "yahoo-etf", "live",
                         "ETF-chain OI (strikes in ETF space) -- CME unavailable")
            return df
        except Exception as exc:                        # noqa: BLE001
            stale = self.cache.get_stale("oi", f"{pair}_{asof:%Y%m%d}")
            if stale is not None:
                self._record(f"oi.{pair}", stale[1].source or "cache", "cached",
                             f"stale {stale[1].age()/3600:.1f}h")
                return stale[0]
            self._record(f"oi.{pair}", "none", "unavailable", str(exc)[:150])
            return empty_oi_frame()

    # ------------------------------------------------------------------ events
    def events(self, start: date, end: date) -> pd.DataFrame:
        self._record("events", "curated-csv", "user_override",
                     "data/calendar/events.csv (CG-6); user-editable")
        return events_mod.events(start, end, self.calendar_path)

    # ------------------------------------------------------------------ status
    def status(self) -> list[SourceStatus]:
        rows = [
            SourceStatus("yahoo-spot", True, spot_yahoo.CHART_URL.format(symbol="EURUSD=X"),
                         verified=spot_yahoo.VERIFIED),
            SourceStatus("stooq-spot", True, spot_stooq.CSV_URL, verified=spot_stooq.VERIFIED),
            SourceStatus("ecb-fix", True, spot_ecb.DAILY_XML, verified=spot_ecb.VERIFIED),
            SourceStatus("fred-rates", True, rates_fred.GRAPH_CSV,
                         verified=rates_fred.VERIFIED),
            SourceStatus("yahoo-etf-options", True, vol_etf_options.OPTIONS_URL.format(
                symbol="FXE"), verified=vol_etf_options.VERIFIED),
            SourceStatus("cme-oi", True, cme_options.SETTLEMENTS_URL,
                         verified=cme_options.VERIFIED),
            SourceStatus("calendar-csv", events_mod.CALENDAR_PATH.exists(),
                         str(events_mod.CALENDAR_PATH), verified=True),
        ]
        return rows


# ======================================================================== cache
class CacheProvider(_Badged):
    """Replay whatever is on disk. Never touches the network. Everything badged ``cached``."""

    name = "cache"
    kind = "cached"

    def __init__(self, cache: Cache | None = None, calendar_path: str | None = None):
        super().__init__()
        self.cache = cache if cache is not None else get_cache()
        self.calendar_path = calendar_path

    def _stale(self, kind: str, key: str):
        return self.cache.get_stale(kind, key)

    def spot_history(self, pair: str, start: date, end: date) -> pd.DataFrame:
        hit = self._stale("spot", f"{pair}_{start:%Y%m%d}_{end:%Y%m%d}")
        if hit is None:
            return empty_spot_frame()
        df, e = hit
        self._record(f"spot_history.{pair}", e.source or "cache", "cached")
        return df

    def spot(self, pairs: Sequence[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in pairs:
            hit = self._stale("spot", f"last_{p}")
            if hit is not None and not hit[0].empty:
                out[p] = float(hit[0]["close"].iloc[-1])
                self._record(f"spot.{p}", hit[1].source or "cache", "cached",
                             f"stale {hit[1].age()/3600:.1f}h")
        return out

    def rates(self, ccys: Sequence[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for c in ccys:
            hit = self._stale("rates", c)
            if hit is not None and not hit[0].empty:
                out[c] = float(hit[0].iloc[-1, 0])
                self._record(f"rate.{c}", hit[1].source or "cache", "cached")
        return out

    def smile_quotes(self, pair: str, asof: datetime | None = None) -> list[SmileQuotes]:
        hit = self._stale("vol_chain", f"quotes_{pair}")
        if hit is None:
            return []
        df, e = hit
        self._record(f"surface.{pair}", e.source or "cache", "cached")
        return [SmileQuotes(T=float(r.T), atm=float(r.atm), rr25=float(r.rr25),
                            bf25=float(r.bf25),
                            rr10=None if pd.isna(getattr(r, "rr10", None)) else float(r.rr10),
                            bf10=None if pd.isna(getattr(r, "bf10", None)) else float(r.bf10),
                            tenor=str(getattr(r, "tenor", "")))
                for r in df.itertuples()]

    def open_interest(self, pair: str, asof: datetime | None = None) -> pd.DataFrame:
        asof = asof or utcnow()
        for key in (f"{pair}_{asof:%Y%m%d}", f"{pair}_latest"):
            hit = self._stale("oi", key)
            if hit is not None:
                self._record(f"oi.{pair}", hit[1].source or "cache", "cached")
                return hit[0]
        return empty_oi_frame()

    def events(self, start: date, end: date) -> pd.DataFrame:
        self._record("events", "curated-csv", "user_override", "data/calendar/events.csv")
        return events_mod.events(start, end, self.calendar_path)

    def status(self) -> list[SourceStatus]:
        entries = self.cache.list_entries()
        return [SourceStatus("cache", bool(entries), str(self.cache.root), verified=True,
                             rows=len(entries))]


# ======================================================================== chain
class ChainProvider(_Badged):
    """Try each provider in order; badge with whoever actually answered.

    **Resolution order (amendment v1.2, T-1): manual -> live -> cache -> synthetic.**
    Any :class:`~fxgamma.data.manual.ManualQuoteProvider` in ``providers`` is hoisted to the
    front regardless of the order it was passed in, so the desk's own mark always wins and a
    live pull can never overwrite it.  A pair with no manual mark falls straight through to
    live, so marking is per pair and never all-or-nothing.

    ``allow_synthetic`` must be set explicitly for a :class:`SyntheticProvider` to be reached
    (contract section 7).  A synthetic answer is always badged ``kind='synthetic'`` and never
    inherits a live source name.
    """

    name = "chain"
    kind = "live"

    def __init__(self, providers: Sequence[MarketDataProvider], *,
                 allow_synthetic: bool = False):
        super().__init__()
        real = []
        for p in providers:
            if isinstance(p, SyntheticProvider) and not allow_synthetic:
                log.info("ChainProvider: dropping %r (allow_synthetic=False)", p)
                continue
            real.append(p)
        if not real:
            raise ValueError("ChainProvider needs at least one provider")
        # T-1: manual first, always. Order within each tier is preserved.
        manual = [p for p in real if isinstance(p, ManualQuoteProvider)]
        rest = [p for p in real if not isinstance(p, ManualQuoteProvider)]
        if manual and rest and not isinstance(real[0], ManualQuoteProvider):
            log.info("ChainProvider: hoisting %r to the front (v1.2 T-1)", manual[0])
        self.providers = manual + rest
        self.allow_synthetic = allow_synthetic

    # ------------------------------------------------------------------ marks
    @property
    def manual(self) -> ManualQuoteProvider | None:
        """The manual provider in this chain, if any (the Data page edits it directly)."""
        for p in self.providers:
            if isinstance(p, ManualQuoteProvider):
                return p
        return None

    def marked_pairs(self) -> list[str]:
        """Pairs currently priced off the desk's own curve rather than off a live source."""
        m = self.manual
        return m.store.pairs() if m is not None else []

    def _adopt(self, prov: MarketDataProvider, field: str) -> None:
        self._prov[field] = prov.provenance(field)

    def _try(self, method: str, field: str, *args, **kw):
        errors = []
        for prov in self.providers:
            try:
                res = getattr(prov, method)(*args, **kw)
            except Exception as exc:                    # noqa: BLE001
                errors.append(f"{prov.name}: {exc}")
                continue
            empty = (res is None
                     or (isinstance(res, (pd.DataFrame, pd.Series, list, dict)) and len(res) == 0))
            if empty:
                errors.append(f"{prov.name}: empty")
                continue
            self._adopt(prov, field)
            return res
        log.info("chain %s(%s) exhausted: %s", method, field, "; ".join(errors))
        return None

    def spot_history(self, pair: str, start: date, end: date) -> pd.DataFrame:
        r = self._try("spot_history", f"spot_history.{pair}", pair, start, end)
        return empty_spot_frame() if r is None else r

    def spot(self, pairs: Sequence[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        todo = list(pairs)
        for prov in self.providers:
            if not todo:
                break
            try:
                got = prov.spot(todo) or {}
            except Exception as exc:                    # noqa: BLE001
                log.info("chain spot via %s: %s", prov.name, exc)
                continue
            for p, v in got.items():
                out[p] = float(v)
                self._adopt(prov, f"spot.{p}")
            todo = [p for p in todo if p not in out]
        return out

    def rates(self, ccys: Sequence[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        todo = list(ccys)
        for prov in self.providers:
            if not todo:
                break
            try:
                got = prov.rates(todo) or {}
            except Exception as exc:                    # noqa: BLE001
                log.info("chain rates via %s: %s", prov.name, exc)
                continue
            for c, v in got.items():
                out[c] = float(v)
                self._adopt(prov, f"rate.{c}")
            todo = [c for c in todo if c not in out]
        return out

    def smile_quotes(self, pair: str, asof: datetime | None = None) -> list[SmileQuotes]:
        r = self._try("smile_quotes", f"surface.{pair}", pair, asof)
        return [] if r is None else r

    def open_interest(self, pair: str, asof: datetime | None = None) -> pd.DataFrame:
        r = self._try("open_interest", f"oi.{pair}", pair, asof)
        return empty_oi_frame() if r is None else r

    def events(self, start: date, end: date) -> pd.DataFrame:
        r = self._try("events", "events", start, end)
        return events_mod.empty_event_frame() if r is None else r

    def status(self) -> list[SourceStatus]:
        return [s for p in self.providers for s in p.status()]


# ======================================================================== factory
PROVIDERS = ("synthetic", "manual", "live", "cache", "chain", "auto")


def get_provider(name: str | None = None, *, seed: int | None = None,
                 cache: Cache | None = None, refresh: bool = False,
                 allow_synthetic: bool | None = None,
                 asof: datetime | None = None,
                 manual: ManualQuoteProvider | ManualQuoteStore | bool | None = None,
                 marks_path: str | None = None) -> MarketDataProvider:
    """Build a provider by name. ``None`` -> ``$FXGAMMA_PROVIDER`` -> ``"synthetic"``.

    ``manual`` controls the T-1 manual tier of ``chain``/``auto``: ``None`` (default) attaches
    a :class:`~fxgamma.data.manual.ManualQuoteProvider` on the repo's marks file, ``False``
    disables it, and an explicit provider/store instance is used as given (the Dash app passes
    the one it is editing so the UI and the pricer share a single object).
    """
    name = (name or os.environ.get("FXGAMMA_PROVIDER") or "synthetic").strip().lower()
    if cache is None:
        cache = get_cache(refresh=refresh) if refresh else get_cache()
    if allow_synthetic is None:
        allow_synthetic = os.environ.get("FXGAMMA_ALLOW_SYNTHETIC", "").lower() in \
            ("1", "true", "yes")

    def _manual() -> ManualQuoteProvider | None:
        if manual is False:
            return None
        if isinstance(manual, ManualQuoteProvider):
            return manual
        if isinstance(manual, ManualQuoteStore):
            return ManualQuoteProvider(manual)
        return ManualQuoteProvider(path=marks_path)

    if name == "synthetic":
        kw = {"asof": asof} if asof else {}
        return SyntheticProvider(seed=seed if seed is not None
                                 else SyntheticProvider.DEFAULT_SEED, **kw)
    if name == "manual":
        mp = _manual()
        if mp is None:
            raise ValueError("provider 'manual' requested with manual=False")
        return mp
    if name == "live":
        return LiveProvider(cache)
    if name == "cache":
        return CacheProvider(cache)
    if name in ("chain", "auto"):
        chain: list[MarketDataProvider] = [LiveProvider(cache), CacheProvider(cache)]
        mp = _manual()
        if mp is not None:                        # v1.2 T-1: the desk's mark wins
            chain.insert(0, mp)
        if name == "auto" or allow_synthetic:
            chain.append(SyntheticProvider(seed=seed if seed is not None
                                           else SyntheticProvider.DEFAULT_SEED))
            allow_synthetic = True
        return ChainProvider(chain, allow_synthetic=bool(allow_synthetic))
    raise ValueError(f"unknown provider {name!r}; choose from {PROVIDERS}")
