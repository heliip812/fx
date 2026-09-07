"""The app's **single** pricing route.

Every number on every page that is a price, a Greek or an aggregate comes through
here, and everything here delegates to :mod:`fxgamma.portfolio.risk` -- the frozen
contract section 5 owner of ``price_book`` / ``book_greeks``.  The interim
direct-``models.gk`` path this module used to carry while the risk engine was being
written is **gone**: ``app/`` no longer calls ``gk_greeks`` anywhere, so there is
exactly one pricing route and one aggregation rule in the application.

What this module is allowed to do
---------------------------------
1. **Adapt shapes.**  ``price_book`` returns a DataFrame in the library's column
   names; the blotter and the tables were written against a list of dicts with the
   app's names.  :func:`price_positions` maps one to the other and adds nothing.
2. **Degrade.**  ``price_book`` raises (correctly) when a pair has no spot or no
   surface -- architecture section 7 forbids substituting one.  The app may not show a
   traceback, so :func:`price_positions` retries pair by pair and renders the pairs it
   *can* price, marking the rest ``UNPRICED`` with the reason.  It never invents a
   number to fill the hole.
3. **Re-export the identities** so no page restates one: ``gamma_pnl_pct`` and
   ``dhedge_pnl`` from ``portfolio.risk``, the breakeven from ``signals.richness``,
   the sigma-day from ``portfolio.zones``.

What this module must never do
------------------------------
Sum native-ccy Greeks across pairs (CG-1), or compute a Greek itself.  Aggregation is
:func:`aggregate`, which is a call to ``risk.book_greeks`` and nothing else.

W-7 bases, stated once and imported everywhere
----------------------------------------------
``DISTANCE_BASIS`` = sqrt(252): distance to a level, sigma-days, touch probability.
``ECONOMICS_BASIS`` = sqrt(365): the daily breakeven and theta.
Any panel showing either **prints the basis**; the two are never conflated.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Mapping

import pandas as pd

from fxgamma.conventions import PAIRS, pair_spec
from fxgamma.models import gk
from fxgamma.portfolio import risk
from fxgamma.portfolio.risk import (BASE_CCY_GREEKS, GREEK_COLS, QUOTE_CCY_GREEKS,
                                    dhedge_pnl, fx_rate, gamma_pnl_pct)
from fxgamma.portfolio.zones import CALENDAR_DAYS, TRADING_DAYS, sigma_day_pct
from fxgamma.signals.richness import breakeven_pct, daily_breakeven
from fxgamma.types import Book, Greeks, MarketSnapshot

log = logging.getLogger(__name__)

__all__ = ["price_positions", "unattainable_message", "price_frame", "aggregate", "pair_totals", "fx_rate",
           "gamma_pnl_pct", "dhedge_pnl", "breakeven_daily_pct", "sigma_day_move",
           "mark_vols", "headline", "days_to_next_mark", "attainable_delta",
           "DISTANCE_BASIS", "ECONOMICS_BASIS", "MONEY_FIELDS"]

#: W-7 / amendment v1.6: spot travels on trading days
DISTANCE_BASIS = f"sqrt({TRADING_DAYS:g}) — trading days (distance, sigma-days, touch prob.)"
#: W-7 / amendment v1.4 ruling 5: theta is paid on calendar days
ECONOMICS_BASIS = f"sqrt({CALENDAR_DAYS:g}) — calendar days (breakeven, theta)"

MONEY_FIELDS = QUOTE_CCY_GREEKS


# ------------------------------------------------------------------ identities
def breakeven_daily_pct(gamma_1pct: float, theta: float, spot: float) -> float | None:
    """Daily breakeven move in percent, or ``None`` when there is not one.

    Delegates to ``signals.richness.breakeven_pct`` (requirements section 0); the only thing
    added is the ``nan -> None`` conversion the UI needs so a short-gamma book renders
    an em dash rather than "nan%".  Economics, therefore ``ECONOMICS_BASIS``.
    """
    v = breakeven_pct(theta, gamma_1pct, spot)
    return None if (v is None or not math.isfinite(v)) else float(v)


def sigma_day_move(sigma: float | None) -> float | None:
    """One sigma-day of spot **distance**, in percent (``DISTANCE_BASIS``)."""
    if sigma is None or not math.isfinite(float(sigma)) or float(sigma) <= 0:
        return None
    return sigma_day_pct(float(sigma))


def days_to_next_mark(asof: datetime) -> float:
    """Calendar days of theta between now and the next mark (Friday -> Monday = 3).

    Trader Q-6: quoting "you pay X today" on a Friday when three days are owed is the
    recurring error, so the header card names the number of days it charged.
    """
    wd = asof.weekday()                     # Mon=0
    return 3.0 if wd == 4 else 2.0 if wd == 5 else 1.0


# ------------------------------------------------------------------ marks (CG-2)
def mark_vols(marks: Mapping[str, Any] | None) -> dict[str, float]:
    """``store.marks()`` (``{id: MarkVol}``) -> the ``{id: vol}`` the engine takes."""
    out: dict[str, float] = {}
    for pid, mk in (marks or {}).items():
        v = getattr(mk, "mark_vol", mk)
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(fv) and fv > 0:
            out[str(pid)] = fv
    return out


# ------------------------------------------------------------------ pricing
def price_frame(book: Book, mkt: MarketSnapshot, *, marks: Mapping[str, Any] | None = None,
                report_ccy: str = "USD") -> pd.DataFrame:
    """``risk.price_book`` verbatim.  Raises exactly as the library does."""
    return risk.price_book(book, mkt, report_ccy=report_ccy, marks=mark_vols(marks))


def _unpriced_rows(book: Book, mkt: MarketSnapshot, reason: str,
                   report_ccy: str) -> list[dict[str, Any]]:
    """Rows for a pair the engine refused to price. Identity only, never a number."""
    out = []
    for o in book.options:
        spec = pair_spec(o.pair)
        out.append(_blank_row(
            o.id, "OPTION", o.pair, spec, reason, report_ccy,
            cp="C" if o.cp > 0 else "P", dir="B" if o.direction > 0 else "S",
            strike=float(o.strike), expiry=o.expiry.isoformat(), cut=o.cut,
            days=(o.expiry - mkt.asof.date()).days,
            notional_base=float(o.notional_base) * o.direction, tag=o.tag,
            premium_paid=float(o.premium_paid)))
    for sp in book.spots:
        spec = pair_spec(sp.pair)
        out.append(_blank_row(
            sp.id, "SPOT", sp.pair, spec, reason, report_ccy,
            dir="B" if sp.notional_base > 0 else "S",
            notional_base=float(sp.notional_base), tag=sp.tag))
    return out


def _blank_row(pid, instrument, pair, spec, reason, report_ccy, **kw) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": pid, "instrument": instrument, "kind": instrument.lower(), "pair": pair,
        "state": "UNPRICED", "structure": "", "tag": "", "cp": "", "dir": "",
        "strike": None, "expiry": "", "cut": "", "days": None, "T": None,
        "notional_base": 0.0, "vol": None, "vol_source": "unpriced",
        "vol_detail": reason, "spot": None, "premium_paid": 0.0,
        "pnl_since_trade": float("nan"), "ccy": spec.quote, "base_ccy": spec.base,
        "report_ccy": report_ccy, "fx_to_report": float("nan"),
        "fx_base_to_report": float("nan"), "expired": False,
    }
    row.update(kw)
    row.update({c: float("nan") for c in GREEK_COLS})
    row.update({f"{c}_rep": float("nan") for c in QUOTE_CCY_GREEKS + BASE_CCY_GREEKS})
    return row


_VOL_SOURCE = {"mark": "mark override", "surface": "surface", "expired": "expired",
               "n/a": "n/a (spot)"}


def _rows_from_frame(df: pd.DataFrame, mkt: MarketSnapshot) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in df.to_dict("records"):
        expired = bool(r.get("expired"))
        tag = str(r.get("tag") or "")
        expiry = r.get("expiry")
        row = dict(r)
        row.update({
            "instrument": "OPTION" if r.get("kind") == "option" else "SPOT",
            "state": "EXPIRED" if expired else "LIVE",
            "structure": tag.split(":")[0] if ":" in tag else "",
            "cp": ("C" if int(r.get("cp") or 0) > 0 else "P") if r.get("kind") == "option" else "",
            "dir": "B" if float(r.get("signed_notional") or 0.0) >= 0 else "S",
            "days": (None if expiry in (None, "") else
                     (expiry - mkt.asof.date()).days if hasattr(expiry, "year") else None),
            "notional_base": float(r.get("signed_notional") or 0.0),
            "vol_source": _VOL_SOURCE.get(str(r.get("vol_source")), str(r.get("vol_source"))),
            "vol_detail": "",
            "expiry": expiry.isoformat() if hasattr(expiry, "isoformat") else (expiry or ""),
        })
        if r.get("kind") != "option":
            row["vol"] = None
        out.append(row)
    return out


def price_positions(book: Book, mkt: MarketSnapshot, *,
                    marks: Mapping[str, Any] | None = None,
                    report_ccy: str = "USD") -> list[dict[str, Any]]:
    """One row per position, priced by ``risk.price_book``, in the app's row shape.

    Degrades pair by pair: a pair with no spot or no surface comes back ``UNPRICED``
    with the library's own message in ``vol_detail`` while every other pair prices
    normally.  Nothing is substituted for the missing input (architecture section 7).
    """
    if not book.options and not book.spots:
        return []
    try:
        return _rows_from_frame(price_frame(book, mkt, marks=marks,
                                            report_ccy=report_ccy), mkt)
    except Exception as exc:                               # noqa: BLE001
        log.warning("price_book on the whole book failed (%s); pricing pair by pair", exc)
    out: list[dict[str, Any]] = []
    for pair in book.pairs():
        sub = book.filter(pair)
        try:
            out += _rows_from_frame(price_frame(sub, mkt, marks=marks,
                                                report_ccy=report_ccy), mkt)
        except Exception as exc:                           # noqa: BLE001
            out += _unpriced_rows(sub, mkt, str(exc)[:300], report_ccy.upper())
    return out


# ------------------------------------------------------------------ aggregation (CG-1)
def aggregate(book: Book, mkt: MarketSnapshot, *, pair: str | None = None,
              report_ccy: str = "USD", marks: Mapping[str, Any] | None = None,
              base_as_value: bool = False, include_expired: bool = False,
              df: pd.DataFrame | None = None) -> Greeks:
    """The **only** aggregation entry point in ``app/``: ``risk.book_greeks``.

    ``pair`` restricts to one pair; pass ``report_ccy=pair_spec(pair).quote`` to read
    that pair's totals in its own quote ccy, which is the only currency in which a
    per-pair card may be shown.  Cross-pair cards must pass the reporting ccy and
    ``base_as_value=True`` so ``delta_base`` / ``gamma_1pct`` / ``vanna`` come back as
    report-ccy *values* rather than ``nan`` (CR-2).
    """
    sub = book.filter(pair) if pair else book
    return risk.book_greeks(sub, mkt, report_ccy, marks=mark_vols(marks),
                            base_as_value=base_as_value,
                            include_expired=include_expired, df=df)


def pair_totals(book: Book, mkt: MarketSnapshot, report_ccy: str = "USD", *,
                marks: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Per-pair totals in that pair's own quote ccy, plus the disclosed fx rate."""
    out = []
    for pair in book.pairs():
        spec = pair_spec(pair)
        try:
            g = aggregate(book, mkt, pair=pair, report_ccy=spec.quote, marks=marks)
            fx = fx_rate(spec.quote, report_ccy, mkt)
        except Exception as exc:                           # noqa: BLE001
            log.warning("pair total for %s failed: %s", pair, exc)
            continue
        out.append({"pair": pair, "ccy": spec.quote, "base_ccy": spec.base,
                    "fx_to_report": fx, "report_ccy": report_ccy.upper(),
                    **g.as_dict()})
    return out


# ------------------------------------------------------------------ MISS-4 headline
def headline(book: Book, mkt: MarketSnapshot, pair: str, *,
             marks: Mapping[str, Any] | None = None,
             days: float | None = None) -> dict[str, Any]:
    """The one-line answer for one pair (MISS-4), computed, never constant.

    **Amendment v1.4 ruling 2 is binding here:** theta comes from
    ``risk.book_greeks`` on this pair's sub-book, in the pair's own quote ccy.  The
    trader's table said USD 5,800/day for the reference straddle; pricing it gives
    USD 2,868, and this is the most-read number in the app, so it is priced on every
    render and never written down.

    Returns a dict; the sentence is ``["text"]``.  ``be_pips`` uses the
    ``ECONOMICS_BASIS`` (calendar days) because it is a theta question.
    """
    spec = pair_spec(pair)
    S = (mkt.spot or {}).get(pair)
    d = float(days if days is not None else days_to_next_mark(mkt.asof))
    g = aggregate(book, mkt, pair=pair, report_ccy=spec.quote, marks=marks)
    out: dict[str, Any] = {
        "pair": pair, "ccy": spec.quote, "base_ccy": spec.base, "spot": S,
        "gamma_1pct": g.gamma_1pct, "theta": g.theta, "pv": g.pv, "vega": g.vega,
        "delta_base": g.delta_base, "days": d, "greeks": g,
    }
    side = ("LONG GAMMA" if g.gamma_1pct > 0 else
            "SHORT GAMMA" if g.gamma_1pct < 0 else "FLAT GAMMA")
    out["side"] = side
    be = be_pips = None
    try:
        card = daily_breakeven(book.filter(pair), mkt, pair, days=d,
                               report_ccy=spec.quote, marks=mark_vols(marks))
        be = card.get("be_pct")
        be_pips = card.get("be_pips")
        out["theta_gamma"] = card.get("theta_gamma_per_day")
        out["sigma_used"] = card.get("sigma_used")
    except Exception as exc:                               # noqa: BLE001
        log.debug("daily_breakeven(%s) unavailable: %s", pair, exc)
    be = None if (be is None or not math.isfinite(be)) else float(be)
    be_pips = None if (be_pips is None or not math.isfinite(be_pips)) else float(be_pips)
    out["be_pct"], out["be_pips"] = be, be_pips
    pays = (f"pays above {be_pips:,.0f} pips today" if be_pips is not None else
            "no breakeven (you are short gamma — the move costs you)"
            if g.gamma_1pct < 0 else "breakeven unavailable")
    verb = "costs" if g.theta < 0 else "earns"
    day_word = "today" if d <= 1 else f"over {d:g} calendar days"
    theta_txt = (f"{verb} {spec.quote} {abs(g.theta) * d:,.0f} {day_word}"
                 if math.isfinite(g.theta) else "theta unavailable")
    out["text"] = (f"{side} · {_mm(g.gamma_1pct, spec.base)} per 1% · {pays} · {theta_txt}")
    return out


def _mm(x: float, ccy: str) -> str:
    if x is None or not math.isfinite(float(x)):
        return f"{ccy} n/a"
    v = float(x)
    return f"{ccy} {v / 1e6:+,.2f}mm" if abs(v) >= 1e5 else f"{ccy} {v:+,.0f}"


# ------------------------------------------------------------------ v1.7: delta bounds
def attainable_delta(pair: str, mkt: MarketSnapshot, T: float | None,
                     *, cp: int = 1, sigma: float | None = None) -> dict[str, Any]:
    """AMENDMENT v1.7 — the largest ``|delta|`` any strike can reach here.

    Premium-adjusted call delta is **bounded**: the PM verified 0.8745 at 3M/10% vol
    but only 0.2764 at 5Y/40% vol, so on USDJPY / USDCHF / USDCAD / USDSEK / USDNOK a
    "30-delta call" can simply not exist.  Every delta-based strike entry calls this
    and renders "unattainable at this tenor/vol" -- never a blank, a zero or a ``nan``
    strike.

    Returns ``{"max": float|None, "convention": str, "sigma": float|None,
    "bounded": bool, "note": str}``.  ``bounded`` is True only when the maximum can
    actually bite (a premium-adjusted call); everywhere else it is 1.0-ish and the
    caller says nothing.
    """
    spec = pair_spec(pair)
    conv = spec.delta_convention
    out: dict[str, Any] = {"max": None, "convention": conv, "sigma": None,
                           "bounded": False, "note": ""}
    if T is None or not math.isfinite(float(T)) or float(T) <= 0:
        out["note"] = "no tenor: a delta strike needs an expiry"
        return out
    S = (mkt.spot or {}).get(pair)
    if S is None or spec.quote not in mkt.rates or spec.base not in mkt.rates:
        out["note"] = f"no spot/rates for {pair} in this snapshot"
        return out
    sig = sigma
    if sig is None:
        surf = (getattr(mkt, "surfaces", {}) or {}).get(pair)
        if surf is None:
            out["note"] = f"no surface for {pair}: the bound needs a vol"
            return out
        try:
            sig = float(surf.atm(float(T)))
        except Exception as exc:                           # noqa: BLE001
            out["note"] = f"surface.atm failed: {exc}"
            return out
    rd, rf = mkt.rd_rf(pair, PAIRS)
    try:
        mx = float(gk.max_attainable_delta(float(S), float(T), rd, rf, float(sig),
                                           int(cp), conv))
    except Exception as exc:                               # noqa: BLE001
        out["note"] = f"max_attainable_delta failed: {exc}"
        return out
    out["sigma"] = float(sig)
    out["max"] = mx if math.isfinite(mx) else None
    out["bounded"] = bool(conv.endswith("_pa") and int(cp) > 0 and math.isfinite(mx)
                          and mx < 0.999)
    if out["bounded"]:
        out["note"] = (f"premium-adjusted call delta on {pair} is bounded at "
                       f"{mx * 100:.2f}% at T={float(T):.4f}y, vol {sig * 100:.2f}% "
                       f"({conv} convention). Deltas above that do not exist at this "
                       "tenor/vol — no strike produces them.")
    return out


def unattainable_message(pair: str, delta: float, cp: int,
                         info: Mapping[str, Any]) -> str:
    """The exact words amendment v1.7 requires when a requested delta cannot exist."""
    mx = info.get("max")
    what = f"{abs(float(delta)) * 100:g}-delta {'call' if cp > 0 else 'put'} on {pair}"
    if mx is None:
        return f"{what}: strike unavailable — {info.get('note') or 'no bound computable'}"
    return (f"{what}: UNATTAINABLE AT THIS TENOR/VOL. The premium-adjusted maximum is "
            f"{float(mx) * 100:.2f}% delta ({info.get('convention')} convention, vol "
            f"{float(info.get('sigma') or 0) * 100:.2f}%). No strike produces the delta "
            "you asked for — enter a lower delta or an absolute strike.")


# ------------------------------------------------------------------ removed API
def book_greeks(*_a: Any, **_kw: Any) -> Greeks:                # pragma: no cover
    """Removed.  ``app`` aggregates through :func:`aggregate` -> ``risk.book_greeks``."""
    raise NotImplementedError(
        "app.pricing.book_greeks was the interim aggregator and is gone. Call "
        "app.pricing.aggregate(book, mkt, pair=..., report_ccy=...), which delegates "
        "to fxgamma.portfolio.risk.book_greeks (amendment v1.1 CG-1).")
