"""Shared dataclasses. FROZEN CONTRACT - see docs/01_architecture.md.

Conventions recap: a pair is FORDOM (EURUSD -> base/foreign EUR, quote/domestic USD).
Spot is quote-ccy per 1 base-ccy. Notionals are in base ccy. Vols and rates are decimals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

import numpy as np

__all__ = [
    "PairSpec", "OptionPosition", "SpotPosition", "Greeks", "MarketSnapshot",
    "Book", "GammaZone", "PnLBreakdown", "HedgeRule", "HedgeAction", "VolSurface",
    "Provenance",
]


@dataclass(frozen=True)
class PairSpec:
    """Static market convention for one currency pair."""
    symbol: str                      # e.g. "EURUSD"
    base: str                        # foreign / left ccy
    quote: str                       # domestic / right ccy
    pip: float = 1e-4                # 1e-2 for JPY crosses
    spot_lag: int = 2                # business days
    delta_convention: str = "spot"   # spot | spot_pa | fwd | fwd_pa
    cut: str = "NY10"                # NY10 | TKY15 | LDN16
    etf_proxy: str | None = None     # listed ETF whose options proxy this vol
    cme_code: str | None = None      # CME FX option product code
    inverted_etf: bool = False       # True when the ETF tracks QUOTE/BASE (e.g. FXY vs USDJPY)


@dataclass(frozen=True)
class OptionPosition:
    id: str
    pair: str
    cp: int                          # +1 call on base, -1 put on base
    strike: float
    expiry: date
    notional_base: float             # always positive; sign lives in `direction`
    direction: int = 1               # +1 long, -1 short
    premium_paid: float = 0.0        # total, in premium_ccy, signed (paid > 0)
    premium_ccy: str = ""            # "" -> quote ccy
    trade_date: date | None = None
    trade_spot: float | None = None
    trade_vol: float | None = None
    cut: str = "NY10"
    tag: str = ""
    trade_time: datetime | None = None   # UTC execution timestamp (v1.1)

    def signed_notional(self) -> float:
        return self.direction * abs(self.notional_base)


@dataclass(frozen=True)
class SpotPosition:
    """A spot/forward/hedge leg. Signed: + = long base ccy."""
    id: str
    pair: str
    notional_base: float
    entry_rate: float
    trade_date: date | None = None
    value_date: date | None = None
    tag: str = ""                    # "hedge" marks delta hedges for the P&L log
    trade_time: datetime | None = None   # UTC; orders intraday hedges in the hedge log (v1.1)


@dataclass(frozen=True)
class Greeks:
    """All monetary Greeks are in QUOTE ccy unless the name says otherwise."""
    pv: float = 0.0
    delta_base: float = 0.0     # base-ccy equivalent position
    delta_pct: float = 0.0      # spot delta per 1 unit of notional
    gamma: float = 0.0          # d(delta_base)/dS
    gamma_1pct: float = 0.0     # change in delta_base for a +1% spot move  <- desk unit
    vega: float = 0.0           # quote ccy per 1.00 vol point (i.e. per 0.01 of sigma)
    theta: float = 0.0          # quote ccy per calendar day
    rho_d: float = 0.0
    rho_f: float = 0.0
    vanna: float = 0.0          # d(vega)/dS
    volga: float = 0.0          # d(vega)/dsigma, per vol point
    dual_delta: float = 0.0     # d(pv)/dK  ~ risk-neutral density proxy

    _FIELDS = ("pv", "delta_base", "delta_pct", "gamma", "gamma_1pct", "vega",
               "theta", "rho_d", "rho_f", "vanna", "volga", "dual_delta")
    #: Intensive (per-unit / per-strike) quantities that are NOT additive across
    #: positions.  `delta_pct` is per 1 unit of notional and `dual_delta` is d(pv)/dK
    #: at *that position's own strike*, so summing them across a book produces a
    #: number with no meaning.  Aggregation sets them to nan so any card built on
    #: them reads "n/a" instead of printing a confident wrong value.
    _NON_ADDITIVE = ("delta_pct", "dual_delta")

    def __add__(self, other: "Greeks") -> "Greeks":
        if other is None:
            return self
        return Greeks(*(float("nan") if f in Greeks._NON_ADDITIVE
                        else getattr(self, f) + getattr(other, f)
                        for f in Greeks._FIELDS))

    __radd__ = __add__

    def __mul__(self, k: float) -> "Greeks":
        return Greeks(*(getattr(self, f) * k for f in Greeks._FIELDS))

    __rmul__ = __mul__

    def as_dict(self) -> dict[str, float]:
        return {f: getattr(self, f) for f in Greeks._FIELDS}

    @staticmethod
    def zero() -> "Greeks":
        return Greeks()


@dataclass(frozen=True)
class Provenance:
    """Where a number came from. Surfaced in the UI as a badge."""
    source: str                      # "yahoo" | "stooq" | "fred" | "cme" | "synthetic" | "user"
    kind: str = "live"               # live | cached | synthetic | user_override
    asof: datetime | None = None
    note: str = ""


@runtime_checkable
class VolSurface(Protocol):
    pair: str
    asof: datetime

    def vol(self, K: float, T: float) -> float: ...
    def vol_by_delta(self, delta: float, T: float, cp: int) -> float: ...
    def atm(self, T: float) -> float: ...
    def rr(self, T: float, d: float = 0.25) -> float: ...
    def bf(self, T: float, d: float = 0.25) -> float: ...
    def slice(self, T: float, strikes: np.ndarray) -> np.ndarray: ...


@dataclass
class MarketSnapshot:
    asof: datetime
    spot: dict[str, float] = field(default_factory=dict)
    rates: dict[str, float] = field(default_factory=dict)          # ccy -> cc zero rate
    surfaces: dict[str, Any] = field(default_factory=dict)         # pair -> VolSurface
    forwards: dict[str, dict[float, float]] = field(default_factory=dict)
    meta: dict[str, Provenance] = field(default_factory=dict)      # "spot.EURUSD" -> Provenance

    def rd_rf(self, pair: str, pairs: dict[str, PairSpec]) -> tuple[float, float]:
        """(domestic, foreign) rate for `pair`.

        Raises rather than defaulting a missing rate to 0.0: silently assuming a
        zero rate misprices the forward (USDJPY 1Y lands about four big figures
        away) and violates the provenance rule in architecture section 7.
        """
        spec = pairs[pair]
        missing = [c for c in (spec.quote, spec.base) if c not in self.rates]
        if missing:
            raise KeyError(
                f"no rate for {', '.join(missing)} needed by {pair}; "
                f"have {sorted(self.rates)}. Supply it or set an explicit user_override."
            )
        return self.rates[spec.quote], self.rates[spec.base]

    def bump(self, *, spot_mult: dict[str, float] | None = None,
             vol_add: float = 0.0, days: float = 0.0) -> "MarketSnapshot":
        """Return a shifted copy for scenarios. Implemented in portfolio.risk."""
        raise NotImplementedError  # portfolio.risk.shift_market owns the logic


@dataclass
class Book:
    options: list[OptionPosition] = field(default_factory=list)
    spots: list[SpotPosition] = field(default_factory=list)
    name: str = "default"

    def pairs(self) -> list[str]:
        return sorted({p.pair for p in self.options} | {s.pair for s in self.spots})

    def filter(self, pair: str) -> "Book":
        return Book([o for o in self.options if o.pair == pair],
                    [s for s in self.spots if s.pair == pair], self.name)


@dataclass(frozen=True)
class GammaZone:
    """A contiguous spot region where the book's gamma is materially concentrated."""
    pair: str
    lo: float
    hi: float
    center: float
    gamma_1pct: float          # avg delta change per 1% inside the zone
    peak_gamma_1pct: float
    share_of_total: float      # 0..1
    expiries: tuple[date, ...] = ()
    label: str = ""            # "long gamma pocket" / "short gamma / pin risk"
    distance_pct: float = 0.0  # spot -> center, signed, in %


@dataclass
class PnLBreakdown:
    total: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    vanna: float = 0.0
    volga: float = 0.0
    rates: float = 0.0
    carry: float = 0.0
    hedge: float = 0.0
    unexplained: float = 0.0
    ccy: str = "USD"
    detail: Any = None          # pd.DataFrame, per-position

    def as_dict(self) -> dict[str, float]:
        return {k: getattr(self, k) for k in
                ("delta", "gamma", "theta", "vega", "vanna", "volga",
                 "rates", "carry", "hedge", "unexplained", "total")}


@dataclass(frozen=True)
class HedgeRule:
    mode: str = "band"           # band | time | gamma_budget | none
    band_pct: float = 0.25       # rehedge when |delta| drifts this % of notional
    band_delta: float = 0.0      # absolute base-ccy band (overrides band_pct if > 0)
    every_hours: float = 24.0    # for mode="time"
    cost_bp: float = 0.2         # round-trip spot cost in bp of notional
    target_delta: float = 0.0


@dataclass(frozen=True)
class HedgeAction:
    pair: str
    trade_base: float            # + = buy base
    reason: str = ""
    current_delta: float = 0.0
    post_delta: float = 0.0
    est_cost: float = 0.0
