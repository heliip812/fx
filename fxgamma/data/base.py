"""`MarketDataProvider` ABC -- the only surface the rest of the app sees.

Contract references: docs/01_architecture.md sections 3 (types), 7 (provenance) and 8
(``SmileQuotes`` / ``build_surface`` is the *only* data<->model boundary).

Every provider method returns data **plus** provenance.  ``snapshot()`` must populate
``MarketSnapshot.meta`` with a :class:`~fxgamma.types.Provenance` for every field it fills,
keyed ``"<kind>.<name>"`` -- e.g. ``spot.EURUSD``, ``rates.USD``, ``surface.USDJPY``.
Never badge synthetic data as live (contract section 7).
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Sequence

import pandas as pd

from ..types import MarketSnapshot, Provenance

log = logging.getLogger(__name__)

__all__ = [
    "MarketDataProvider", "SmileQuotes", "build_surface", "SPOT_COLUMNS", "OI_COLUMNS",
    "EVENT_COLUMNS", "empty_spot_frame", "empty_oi_frame", "empty_event_frame",
    "utcnow", "prov", "SourceStatus",
]

# --------------------------------------------------------------------------------------
# Section 8 boundary.
#
# `SmileQuotes` and `build_surface` are owned by [quant] in fxgamma/models/surface.py.
# We import them; if that module has not landed yet we fall back to a *field-identical*
# local mirror of the frozen definition so the data layer is independently runnable.
# The mirror is only ever used when models/surface.py is absent.
# --------------------------------------------------------------------------------------
try:                                                                # pragma: no cover
    from ..models.surface import SmileQuotes, build_surface         # type: ignore
    _SURFACE_BACKEND = "fxgamma.models.surface"
except Exception:                                                   # pragma: no cover
    _SURFACE_BACKEND = "stub (fxgamma.models.surface not available)"

    @dataclass(frozen=True)
    class SmileQuotes:                                              # type: ignore[no-redef]
        """Mirror of the frozen contract section 8 dataclass. Quant's version wins on import."""
        T: float
        atm: float
        rr25: float
        bf25: float
        rr10: float | None = None
        bf10: float | None = None
        tenor: str = ""

    def build_surface(pair: str, asof: datetime, quotes: "list[SmileQuotes]",
                      spot: float, rd: float, rf: float,
                      method: str = "vanna_volga"):                 # type: ignore[no-redef]
        raise NotImplementedError(
            "fxgamma.models.surface.build_surface is not available yet ([quant] owns it). "
            "The data layer produces list[SmileQuotes]; the surface is built there."
        )


def surface_backend() -> str:
    """Which build_surface implementation is in play (diagnostics for the /data page)."""
    return _SURFACE_BACKEND


# --------------------------------------------------------------------------------------
# Canonical frame schemas -- every provider returns exactly these columns, in this order.
# --------------------------------------------------------------------------------------
SPOT_COLUMNS = ["open", "high", "low", "close"]              # index: DatetimeIndex (UTC), name="date"
OI_COLUMNS = ["strike", "expiry", "cp", "oi", "settle"]
EVENT_COLUMNS = ["datetime", "ccy", "event", "importance", "source"]


def empty_spot_frame() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], tz="UTC", name="date")
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in SPOT_COLUMNS}, index=idx)


def empty_oi_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "strike": pd.Series(dtype="float64"),
        "expiry": pd.Series(dtype="object"),
        "cp": pd.Series(dtype="int64"),
        "oi": pd.Series(dtype="float64"),
        "settle": pd.Series(dtype="float64"),
    })


def empty_event_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "datetime": pd.Series(dtype="datetime64[ns, UTC]"),
        "ccy": pd.Series(dtype="object"),
        "event": pd.Series(dtype="object"),
        "importance": pd.Series(dtype="object"),
        "source": pd.Series(dtype="object"),
    })


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def prov(source: str, kind: str = "live", asof: datetime | None = None,
         note: str = "") -> Provenance:
    return Provenance(source=source, kind=kind, asof=asof or utcnow(), note=note)


@dataclass(frozen=True)
class SourceStatus:
    """One row of the `scripts/verify_live_sources.py` table and the /data page."""
    name: str
    ok: bool
    detail: str = ""
    latency_ms: float | None = None
    verified: bool = False        # False => endpoint never confirmed against the live host
    rows: int | None = None


class MarketDataProvider(ABC):
    """Abstract market-data provider.

    Implementations: :class:`~fxgamma.data.synthetic.SyntheticProvider` (deterministic, offline),
    :class:`~fxgamma.data.provider.LiveProvider` (real public endpoints),
    :class:`~fxgamma.data.provider.CacheProvider` (on-disk replay),
    :class:`~fxgamma.data.provider.ChainProvider` (live -> cache -> [synthetic if allowed]).
    """

    #: short stable id used in `Provenance.source`
    name: str = "abstract"
    #: "live" | "cached" | "synthetic" | "user_override" -- the default badge for this provider
    kind: str = "live"

    # ---------------- required ----------------
    @abstractmethod
    def spot_history(self, pair: str, start: date, end: date) -> pd.DataFrame:
        """Daily OHLC for `pair` in **FORDOM** convention, index tz-aware UTC, ``SPOT_COLUMNS``.

        Inclusive of both endpoints where the source has data. Must be free of look-ahead:
        the bar stamped D is the bar that closed on D.
        """

    @abstractmethod
    def spot(self, pairs: Sequence[str]) -> dict[str, float]:
        """Latest spot per pair, FORDOM convention."""

    @abstractmethod
    def rates(self, ccys: Sequence[str]) -> dict[str, float]:
        """Continuously-compounded zero rate per currency (decimal, flat curve in v1)."""

    @abstractmethod
    def smile_quotes(self, pair: str, asof: datetime) -> list[SmileQuotes]:
        """Broker-style smile per tenor. The ONLY thing that crosses into the model layer."""

    @abstractmethod
    def open_interest(self, pair: str, asof: datetime) -> pd.DataFrame:
        """Listed option OI by strike/expiry: columns ``OI_COLUMNS``. Drives the Gamma Map."""

    @abstractmethod
    def events(self, start: date, end: date) -> pd.DataFrame:
        """Event calendar, columns ``EVENT_COLUMNS``, `datetime` tz-aware UTC."""

    # ---------------- provided ----------------
    def provenance(self, field: str, kind: str | None = None, note: str = "",
                   asof: datetime | None = None) -> Provenance:
        return Provenance(source=self.name, kind=kind or self.kind,
                          asof=asof or utcnow(), note=note)

    def forwards(self, pair: str, spot: float, rd: float, rf: float,
                 tenors: Iterable[float] = (1 / 12, 0.25, 0.5, 1.0)) -> dict[float, float]:
        """Covered-interest-parity forwards from the flat zero curves (v1)."""
        import math
        return {float(T): spot * math.exp((rd - rf) * T) for T in tenors}

    def snapshot(self, pairs: Sequence[str], asof: datetime | None = None,
                 *, method: str = "vanna_volga",
                 with_surfaces: bool = True) -> MarketSnapshot:
        """Assemble a fully-badged :class:`MarketSnapshot`.

        Contract section 7: every field written here gets a ``Provenance`` in ``meta``.
        A surface that fails to build is *omitted* and the failure recorded in meta -- we
        never quietly swap in another source's number.
        """
        from ..conventions import pair_spec

        asof = asof or utcnow()
        pairs = list(pairs)
        snap = MarketSnapshot(asof=asof)

        spots = self.spot(pairs)
        for p in pairs:
            if p in spots and spots[p] == spots[p]:
                snap.spot[p] = float(spots[p])
                snap.meta[f"spot.{p}"] = self.provenance(f"spot.{p}", asof=asof)

        ccys = sorted({c for p in pairs for c in (pair_spec(p).base, pair_spec(p).quote)})
        rr = self.rates(ccys)
        for c in ccys:
            if c in rr and rr[c] == rr[c]:
                snap.rates[c] = float(rr[c])
                snap.meta[f"rates.{c}"] = self.provenance(f"rates.{c}", asof=asof)

        for p in pairs:
            if p not in snap.spot:
                continue
            spec = pair_spec(p)
            rd = snap.rates.get(spec.quote, 0.0)
            rf = snap.rates.get(spec.base, 0.0)
            snap.forwards[p] = self.forwards(p, snap.spot[p], rd, rf)
            snap.meta[f"forwards.{p}"] = self.provenance(
                f"forwards.{p}", note="CIP from flat zero curves", asof=asof)
            if not with_surfaces:
                continue
            try:
                quotes = self.smile_quotes(p, asof)
                if not quotes:
                    raise ValueError("no smile quotes")
                snap.surfaces[p] = build_surface(p, asof, quotes, snap.spot[p], rd, rf,
                                                 method=method)
                snap.meta[f"surface.{p}"] = self.provenance(
                    f"surface.{p}", note=f"{len(quotes)} tenors via {method}", asof=asof)
            except Exception as exc:                      # noqa: BLE001 - degrade, never fake
                log.warning("surface for %s unavailable: %s", p, exc)
                snap.meta[f"surface.{p}"] = Provenance(
                    source=self.name, kind="unavailable", asof=asof, note=str(exc)[:200])
        return snap

    # ---------------- diagnostics ----------------
    def status(self) -> list[SourceStatus]:
        """Per-source health, surfaced on the /data page. Overridden by real providers."""
        return [SourceStatus(self.name, True, f"kind={self.kind}", verified=False)]

    def __repr__(self) -> str:              # pragma: no cover
        return f"<{type(self).__name__} name={self.name!r} kind={self.kind!r}>"
