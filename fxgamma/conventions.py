"""Market conventions for G3 + G10. FROZEN CONTRACT - see docs/01_architecture.md.

ETF proxies are the free/public route to *listed* FX implied vols: each of these US-listed
currency ETFs holds the foreign currency (or a swap on it) and has a liquid, publicly quoted
option chain. Their implied vols track the corresponding OTC pair vol closely (small basis from
fees, borrow and the ETF's own creation mechanics), which is good enough for richness analytics.
`inverted_etf=True` means the ETF is quoted as USD per foreign ccy while the pair is quoted the
other way round (e.g. FXY vs USDJPY) - vol is invariant to inversion at first order but delta and
skew signs flip, so the loader must handle it.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .types import PairSpec

__all__ = ["PAIRS", "G3", "G10_PAIRS", "CCYS", "CUTS", "TENORS", "tenor_years",
           "year_fraction", "expiry_datetime", "pair_spec", "is_jpy_pair", "pip_value", "is_expired"]

ACT = 365.0

PAIRS: dict[str, PairSpec] = {
    "EURUSD": PairSpec("EURUSD", "EUR", "USD", 1e-4, 2, "spot",    "NY10", "FXE", "6E"),
    "GBPUSD": PairSpec("GBPUSD", "GBP", "USD", 1e-4, 2, "spot",    "NY10", "FXB", "6B"),
    "USDJPY": PairSpec("USDJPY", "USD", "JPY", 1e-2, 2, "spot_pa", "NY10", "FXY", "6J", True),
    "AUDUSD": PairSpec("AUDUSD", "AUD", "USD", 1e-4, 2, "spot",    "NY10", "FXA", "6A"),
    "NZDUSD": PairSpec("NZDUSD", "NZD", "USD", 1e-4, 2, "spot",    "NY10", None, "6N"),
    "USDCAD": PairSpec("USDCAD", "USD", "CAD", 1e-4, 1, "spot_pa", "NY10", "FXC", "6C", True),
    "USDCHF": PairSpec("USDCHF", "USD", "CHF", 1e-4, 2, "spot_pa", "NY10", "FXF", "6S", True),
    "USDSEK": PairSpec("USDSEK", "USD", "SEK", 1e-4, 2, "spot_pa", "NY10", None, None, True),
    "USDNOK": PairSpec("USDNOK", "USD", "NOK", 1e-4, 2, "spot_pa", "NY10", None, None, True),
    # useful crosses for relative-value gamma
    "EURJPY": PairSpec("EURJPY", "EUR", "JPY", 1e-2, 2, "spot_pa", "TKY15", None, None),
    "EURGBP": PairSpec("EURGBP", "EUR", "GBP", 1e-4, 2, "spot",    "LDN16", None, None),
    "EURCHF": PairSpec("EURCHF", "EUR", "CHF", 1e-4, 2, "spot",    "NY10", None, None),
}

G3 = ["EURUSD", "GBPUSD", "USDJPY"]
G10_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD",
             "USDCAD", "USDCHF", "USDSEK", "USDNOK"]
CROSSES = ["EURJPY", "EURGBP", "EURCHF"]
CCYS = ["USD", "EUR", "JPY", "GBP", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK"]

CUTS: dict[str, tuple[time, str]] = {
    "NY10":  (time(10, 0), "America/New_York"),
    "TKY15": (time(15, 0), "Asia/Tokyo"),
    "LDN16": (time(16, 0), "Europe/London"),
}

# standard broker tenor grid
TENORS: dict[str, float] = {
    "ON": 1 / 365, "1W": 7 / 365, "2W": 14 / 365, "1M": 1 / 12, "2M": 2 / 12,
    "3M": 0.25, "6M": 0.5, "9M": 0.75, "1Y": 1.0, "2Y": 2.0,
}


def pair_spec(pair: str) -> PairSpec:
    try:
        return PAIRS[pair.upper()]
    except KeyError as exc:
        raise KeyError(f"unknown pair {pair!r}; known: {sorted(PAIRS)}") from exc


def is_jpy_pair(pair: str) -> bool:
    s = pair_spec(pair)
    return "JPY" in (s.base, s.quote)


def tenor_years(tenor: str) -> float:
    """'3M' -> 0.25. Accepts the TENORS keys or NxD/W/M/Y."""
    t = tenor.strip().upper()
    if t in TENORS:
        return TENORS[t]
    unit, n = t[-1], t[:-1]
    mult = {"D": 1 / 365, "W": 7 / 365, "M": 1 / 12, "Y": 1.0}
    if unit not in mult or not n:
        raise ValueError(f"bad tenor {tenor!r}")
    return float(n) * mult[unit]


def expiry_datetime(expiry: date, cut: str = "NY10") -> datetime:
    """Expiry date + cut -> a UTC timestamp, so T is unambiguous across pairs."""
    try:
        t, tz = CUTS[cut]
    except KeyError:
        # Never silently fall back to NY10: a typo'd cut would shift every expiry by
        # hours, quietly changing T, theta and the pin clock on the affected legs.
        raise ValueError(
            f"unknown cut {cut!r}; known cuts: {sorted(CUTS)}"
        ) from None
    return datetime.combine(expiry, t, tzinfo=ZoneInfo(tz)).astimezone(ZoneInfo("UTC"))


def year_fraction(asof: datetime, expiry: date, cut: str = "NY10",
                  floor: float = 1 / (365 * 24)) -> float:
    """ACT/365F to the cut.

    Returns exactly ``0.0`` once the cut has passed, so an expired option prices to
    intrinsic and stops contributing gamma, vega and theta. The `floor` applies only
    on the *live* side of the cut, keeping options priceable in their final minutes
    without letting a dead option linger in the risk forever.
    """
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=ZoneInfo("UTC"))
    secs = (expiry_datetime(expiry, cut) - asof.astimezone(ZoneInfo("UTC"))).total_seconds()
    if secs <= 0.0:
        return 0.0
    return max(secs / (ACT * 86400.0), floor)


def is_expired(asof: datetime, expiry: date, cut: str = "NY10") -> bool:
    """True once `expiry`'s cut has passed. The book view uses this to retire legs."""
    return year_fraction(asof, expiry, cut) == 0.0


def pip_value(pair: str, notional_base: float, spot: float) -> float:
    """Quote-ccy value of one pip on `notional_base`."""
    return pair_spec(pair).pip * notional_base
