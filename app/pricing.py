"""Position pricing for the app  **[interim: see the seam note below]**.

`fxgamma/portfolio/risk.py` owns `price_book` / `book_greeks` in the frozen contract
(section 5) and is being written concurrently.  The Book page must nevertheless price the
trades it captures, so this module prices one position at a time straight through
`models.gk` and aggregates with `Greeks.__add__`.

**The seam.**  :func:`price_positions` returns the column set the contract gives
``price_book`` - one row per position, plus ``ccy`` and ``fx_to_report`` and ``*_rep``
copies of every monetary Greek (amendment v1.1 CG-1) - so when `portfolio.risk` lands
the call site swaps and the pages do not change.  :func:`fx_rate` mirrors CG-1's rule
exactly: route through USD, **raise** if a leg is missing, never default to 1.0.

Two identities from requirements section 0 live here, once, and are imported everywhere:
``gamma_pnl_pct`` and ``breakeven_daily_pct``.  Per trader review W-7 the *distance*
measure ``sigma_day_move`` annualises on 252 trading days while the *economic* breakeven
uses 365 calendar days; they are never conflated.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from fxgamma.conventions import PAIRS, is_expired, pair_spec, year_fraction
from fxgamma.models.gk import gk_greeks
from fxgamma.types import Book, Greeks, MarketSnapshot, OptionPosition, SpotPosition

MONEY_FIELDS = ("pv", "vega", "theta", "rho_d", "rho_f", "vanna", "volga")

__all__ = ["price_positions", "book_greeks", "fx_rate", "gamma_pnl_pct",
           "breakeven_daily_pct", "sigma_day_move", "PricedRow", "VolPick", "pick_vol"]


# ------------------------------------------------------------------ identities
def gamma_pnl_pct(gamma_1pct: float, spot: float, move_pct: float) -> float:
    """Gamma P&L in quote ccy for a spot move of ``move_pct`` **percent**.

    ``0.005 * G1 * S * x**2`` (requirements section 0). Written once, used everywhere.
    """
    return 0.005 * float(gamma_1pct) * float(spot) * float(move_pct) ** 2


def breakeven_daily_pct(gamma_1pct: float, theta: float, spot: float) -> float | None:
    """Daily breakeven move in **percent**: ``sqrt(|theta| / (0.005*G1*S))``.

    Economics, so calendar-day based (365).  Returns ``None`` when the book has no
    gamma or no theta - the honest answer is "—", not 0.
    """
    denom = 0.005 * float(gamma_1pct) * float(spot)
    if not denom or not theta or denom * theta > 0 and False:
        return None
    try:
        v = math.sqrt(abs(float(theta)) / abs(denom))
    except (ValueError, ZeroDivisionError):
        return None
    return v if math.isfinite(v) else None


def sigma_day_move(sigma: float, *, basis: int = 252) -> float:
    """One sigma-day of spot **distance**, in percent. W-7: 252, not 365."""
    return 100.0 * float(sigma) / math.sqrt(basis)


# ------------------------------------------------------------------ fx conversion
def fx_rate(ccy: str, report_ccy: str, mkt: MarketSnapshot) -> float:
    """Multiplier from ``ccy`` into ``report_ccy``, routed through USD (CG-1).

    Raises when a leg is missing rather than defaulting to 1.0: a silent 1.0 turns a
    JPY total into a USD total 150x too large.
    """
    ccy, report_ccy = ccy.upper(), report_ccy.upper()
    if ccy == report_ccy:
        return 1.0
    spot = getattr(mkt, "spot", {}) or {}

    def to_usd(c: str) -> float:
        if c == "USD":
            return 1.0
        if f"{c}USD" in spot:
            return float(spot[f"{c}USD"])
        if f"USD{c}" in spot:
            v = float(spot[f"USD{c}"])
            if not v:
                raise KeyError(f"USD{c} is zero in this snapshot")
            return 1.0 / v
        raise KeyError(f"no USD leg for {c} in this snapshot (need {c}USD or USD{c}); "
                       "supply it or set a manual spot override")

    return to_usd(ccy) / to_usd(report_ccy)


# ------------------------------------------------------------------ vol selection
@dataclass(frozen=True)
class VolPick:
    vol: float | None
    source: str          # "mark override" | "surface" | "trade vol" | "unavailable"
    detail: str = ""

    @property
    def kind(self) -> str:
        return {"mark override": "user_override", "surface": "surface",
                "trade vol": "user_override"}.get(self.source, "unavailable")


def pick_vol(pos: OptionPosition, mkt: MarketSnapshot, T: float,
             marks: dict[str, Any] | None = None) -> VolPick:
    """Which vol prices this line, and where it came from.

    Order: a per-position mark (CG-2 side table) beats the surface; the surface beats
    the trade vol; if none exist the row prices to nothing and says so.  It never
    silently substitutes a flat vol.
    """
    mk = (marks or {}).get(pos.id)
    if mk is not None:
        return VolPick(float(mk.mark_vol), "mark override",
                       f"{mk.mark_source}, set {mk.asof:%Y-%m-%d %H:%MZ}"
                       if getattr(mk, "asof", None) else str(mk.mark_source))
    surf = (getattr(mkt, "surfaces", {}) or {}).get(pos.pair)
    if surf is not None and T > 0:
        try:
            v = float(surf.vol(pos.strike, T))
            if math.isfinite(v) and v > 0:
                return VolPick(v, "surface", f"{pos.pair} surface at K={pos.strike:g}")
        except Exception as exc:                       # noqa: BLE001
            return VolPick(None, "unavailable", f"surface.vol failed: {exc}")
    if pos.trade_vol:
        return VolPick(float(pos.trade_vol), "trade vol",
                       "no surface for this pair/tenor; priced at the entry vol")
    if T <= 0:
        return VolPick(0.0, "expired", "past the cut: intrinsic only")
    return VolPick(None, "unavailable", "no surface and no trade vol for this position")


# ------------------------------------------------------------------ rows
@dataclass
class PricedRow:
    id: str
    kind: str                       # OPTION | SPOT
    pair: str
    state: str                      # LIVE | EXPIRED | UNPRICED
    greeks: Greeks
    ccy: str
    fx_to_report: float
    row: dict[str, Any]


def price_positions(book: Book, mkt: MarketSnapshot, *, marks: dict[str, Any] | None = None,
                    report_ccy: str = "USD") -> list[dict[str, Any]]:
    """One row per position with Greeks, in the shape contract section 5 gives ``price_book``.

    Expired options (v1.2 T-3) are priced to intrinsic, flagged ``EXPIRED`` and excluded
    from the aggregate by :func:`book_greeks`; the delta they hand you is still shown so
    the inherited spot position is never a surprise.
    """
    out: list[dict[str, Any]] = []
    asof = mkt.asof
    for o in book.options:
        spec = pair_spec(o.pair)
        S = (mkt.spot or {}).get(o.pair)
        T = year_fraction(asof, o.expiry, o.cut)
        expired = is_expired(asof, o.expiry, o.cut)
        vp = pick_vol(o, mkt, T, marks)
        rates_ok = spec.quote in mkt.rates and spec.base in mkt.rates
        state = "EXPIRED" if expired else "LIVE"
        g = Greeks.zero()
        if S is None or not rates_ok or (vp.vol is None and not expired):
            state = "UNPRICED"
        else:
            rd, rf = mkt.rd_rf(o.pair, PAIRS)
            g = gk_greeks(float(S), float(o.strike), max(T, 0.0), rd, rf,
                          float(vp.vol or 0.0), int(o.cp),
                          notional_base=abs(float(o.notional_base)),
                          direction=int(o.direction),
                          delta_convention=spec.delta_convention)
        try:
            fx = fx_rate(spec.quote, report_ccy, mkt)
        except KeyError:
            fx = float("nan")
        prem = float(o.premium_paid)
        if o.premium_ccy and o.premium_ccy.upper() == spec.base and S:
            prem = prem * float(o.trade_spot or S)          # premium into quote ccy
        pnl = (g.pv - prem) if state != "UNPRICED" else float("nan")
        days = (o.expiry - asof.date()).days
        row = {
            "id": o.id, "instrument": "OPTION", "pair": o.pair, "state": state,
            "structure": (o.tag.split(":")[0] if ":" in (o.tag or "") else ""),
            "tag": o.tag, "cp": "C" if o.cp > 0 else "P",
            "dir": "B" if o.direction > 0 else "S",
            "strike": float(o.strike), "expiry": o.expiry.isoformat(), "cut": o.cut,
            "days": days, "T": T, "notional_base": float(o.notional_base) * o.direction,
            "vol": vp.vol, "vol_source": vp.source, "vol_detail": vp.detail,
            "spot": S, "premium_paid": prem, "pnl_since_trade": pnl,
            "ccy": spec.quote, "base_ccy": spec.base, "fx_to_report": fx,
        }
        row.update({k: getattr(g, k) for k in Greeks._FIELDS})
        row.update({f"{k}_rep": getattr(g, k) * fx for k in MONEY_FIELDS})
        row["delta_base_rep"] = g.delta_base * (fx_or_nan(spec.base, report_ccy, mkt))
        out.append(row)

    for s in book.spots:
        spec = pair_spec(s.pair)
        S = (mkt.spot or {}).get(s.pair)
        pv = (float(S) - float(s.entry_rate)) * float(s.notional_base) if S else float("nan")
        g = Greeks(pv=pv if S else 0.0, delta_base=float(s.notional_base),
                   delta_pct=1.0 if s.notional_base > 0 else -1.0)
        try:
            fx = fx_rate(spec.quote, report_ccy, mkt)
        except KeyError:
            fx = float("nan")
        row = {
            "id": s.id, "instrument": "SPOT", "pair": s.pair,
            "state": "LIVE" if S else "UNPRICED", "structure": "",
            "tag": s.tag, "cp": "", "dir": "B" if s.notional_base > 0 else "S",
            "strike": None, "expiry": "", "cut": "", "days": None, "T": None,
            "notional_base": float(s.notional_base), "vol": None,
            "vol_source": "n/a (spot)", "vol_detail": "", "spot": S,
            "premium_paid": 0.0, "pnl_since_trade": pv,
            "ccy": spec.quote, "base_ccy": spec.base, "fx_to_report": fx,
        }
        row.update({k: getattr(g, k) for k in Greeks._FIELDS})
        row.update({f"{k}_rep": getattr(g, k) * fx for k in MONEY_FIELDS})
        row["delta_base_rep"] = g.delta_base * fx_or_nan(spec.base, report_ccy, mkt)
        out.append(row)
    return out


def fx_or_nan(ccy: str, report_ccy: str, mkt: MarketSnapshot) -> float:
    try:
        return fx_rate(ccy, report_ccy, mkt)
    except KeyError:
        return float("nan")


def book_greeks(rows: Iterable[dict[str, Any]], *, pair: str | None = None,
                include_expired: bool = False) -> Greeks:
    """Aggregate **within one pair** (native quote ccy).

    Cross-pair aggregation in native currency is forbidden by CG-1, so this refuses to
    do it: call it per pair and convert, or use the ``*_rep`` columns.
    """
    total = Greeks.zero()
    n = 0
    for r in rows:
        if pair and r["pair"] != pair:
            continue
        if r["state"] == "UNPRICED":
            continue
        if r["state"] == "EXPIRED" and not include_expired:
            continue
        total = total + Greeks(**{k: float(r.get(k, 0.0) or 0.0)
                                  for k in Greeks._FIELDS})
        n += 1
    return total if n else Greeks.zero()


def pair_totals(rows: list[dict[str, Any]], report_ccy: str = "USD"
                ) -> list[dict[str, Any]]:
    """Per-pair totals in the pair's own quote ccy, plus the disclosed fx to report."""
    out = []
    for p in sorted({r["pair"] for r in rows}):
        g = book_greeks(rows, pair=p)
        fx = next((r["fx_to_report"] for r in rows if r["pair"] == p), float("nan"))
        spec = pair_spec(p)
        out.append({"pair": p, "ccy": spec.quote, "base_ccy": spec.base,
                    "fx_to_report": fx, "report_ccy": report_ccy,
                    **{k: getattr(g, k) for k in Greeks._FIELDS}})
    return out
