"""The one-line hedge instruction.

``hedge_suggestion(book, mkt, pair, rule) -> HedgeAction`` (architecture s5, REQ-043).

The desk output is a sentence, not a table::

    SELL EUR 4.2mm at 1.0871 -- delta +4.2mm vs band +/-2.5mm -- est. cost USD 217

so ``HedgeAction.reason`` is written to be printable verbatim.  The band maths lives
in :func:`fxgamma.portfolio.zones.hedge_bands` (CG-3: cost-aware bands belong in the
library, never in ``app/``); this module only decides *whether* and *how much*.

Modes (``types.HedgeRule.mode``)
--------------------------------
``band``          rebalance to ``target_delta`` when ``|delta - target| > band``.
``gamma_budget``  the band is stated as a delta budget directly.
``time``          rebalance on a clock; ``hedge_suggestion`` reports the full delta
                  and the caller decides when ``every_hours`` has elapsed.
``none``          never suggests a trade (used as the control arm in the backtest).

Rebalance style: **to the target**, not to the band edge.  Hedging back to the edge
halves turnover but leaves you systematically short of flat in the direction spot
just moved; desks that do it do it deliberately, so it is exposed as
``to_edge=True`` rather than assumed.
"""
from __future__ import annotations

import math
from typing import Mapping

import numpy as np

from ..conventions import pair_spec
from ..types import Book, HedgeAction, HedgeRule, MarketSnapshot
from .risk import price_book
from .zones import COST_BP, hedge_bands

__all__ = ["hedge_suggestion", "format_action"]


def hedge_suggestion(book: Book, mkt: MarketSnapshot, pair: str,
                     rule: HedgeRule | None = None, *, to_edge: bool = False,
                     report_ccy: str = "USD", min_clip: float = 0.0,
                     marks: Mapping[str, float] | None = None) -> HedgeAction:
    """What to trade in ``pair`` right now, given ``rule``.

    ``trade_base`` is signed in base ccy: **positive = buy base**.  It is zero when
    the book is inside the band (or when ``rule.mode == "none"``), and it is zero
    when the required clip is below ``min_clip`` -- a hedge you would not actually
    put on should not appear as an instruction.

    ``est_cost`` uses ``rule.cost_bp`` when supplied, otherwise the per-pair
    :data:`fxgamma.portfolio.zones.COST_BP` table (trader W-13: one global 0.2bp is
    10-25x too tight for the Scandies).
    """
    rule = rule or HedgeRule()
    spec = pair_spec(pair)
    S = float(mkt.spot[pair])
    df = price_book(book.filter(pair), mkt, report_ccy=report_ccy, marks=marks)
    live = df[~df["expired"].astype(bool)] if len(df) else df
    delta = float(live["delta_base"].sum()) if len(live) else 0.0
    target = float(rule.target_delta)
    bands = hedge_bands(book, mkt, pair, rule=rule, report_ccy=report_ccy, marks=marks)
    band = float(bands["band_base"].iloc[0]) if len(bands) else 0.0
    warn = str(bands["warning"].iloc[0]) if len(bands) else ""
    cost_bp = float(rule.cost_bp) if rule.cost_bp else COST_BP.get(pair, 0.5)

    excess = delta - target
    if rule.mode == "none":
        return HedgeAction(pair, 0.0, "rule.mode='none' -- no hedging", delta, delta, 0.0)
    if rule.mode == "time":
        trade = -excess
    elif abs(excess) <= band:
        return HedgeAction(
            pair, 0.0,
            f"FLAT ENOUGH -- delta {_mm(delta, spec.base)} vs band "
            f"+/-{_mm(band, spec.base)} ({_mm(band - abs(excess), spec.base)} of room)"
            + (f" | {warn}" if warn else ""),
            delta, delta, 0.0)
    else:
        edge = target + math.copysign(band, excess)
        trade = -(excess - (edge - target)) if to_edge else -excess

    if abs(trade) < float(min_clip):
        return HedgeAction(pair, 0.0,
                           f"trade {_mm(trade, spec.base)} below min clip "
                           f"{_mm(min_clip, spec.base)} -- skip", delta, delta, 0.0)
    cost = abs(trade) * S * (cost_bp / 1e4)
    post = delta + trade
    act = HedgeAction(pair=pair, trade_base=trade, current_delta=delta,
                      post_delta=post, est_cost=cost, reason="")
    return HedgeAction(pair, trade, format_action(act, mkt, rule, band, warn),
                       delta, post, cost)


def _mm(x: float, ccy: str = "") -> str:
    """Base-ccy amount in the units a trader speaks: 'EUR 4.2mm'."""
    if not np.isfinite(x):
        return "n/a"
    a = abs(x)
    unit, div = ("mm", 1e6) if a >= 1e5 else ("k", 1e3)
    return f"{ccy + ' ' if ccy else ''}{x / div:,.1f}{unit}".strip()


def format_action(act: HedgeAction, mkt: MarketSnapshot, rule: HedgeRule,
                  band: float, warning: str = "") -> str:
    """The REQ-043 one-liner, verbatim-printable."""
    spec = pair_spec(act.pair)
    S = float(mkt.spot[act.pair])
    side = "BUY " if act.trade_base > 0 else "SELL"
    quote_ccy = spec.quote
    s = (f"{side} {_mm(abs(act.trade_base), spec.base)} at {S:,.{4 if spec.pip < 1e-3 else 2}f}"
         f" -- delta {_mm(act.current_delta, spec.base)} vs band "
         f"+/-{_mm(band, spec.base)} -- est. cost {quote_ccy} {act.est_cost:,.0f}")
    return s + (f" | {warning}" if warning else "")
