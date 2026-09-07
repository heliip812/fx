"""Strategy presets and the hedge-frequency sweep.

Delta-hedged straddle P&L is *dominated* by how often you hedge -- more than by the
strike, more than by the tenor, and often more than by whether implied was rich.  A
backtest that reports one equity curve at one band is therefore not a result, it is a
sample of one, which is why :func:`hedge_frequency_sweep` (REQ-062) is part of the
deliverable rather than an extra.

Presets
-------
``long_straddle`` / ``short_straddle``
    Roll an ATM (delta-neutral-straddle strike) straddle at the chosen tenor, delta
    hedged on a band.  The band defaults to the desk standard: 15% of the pair's
    gross option notional (trader Q-2), **not** the frozen ``HedgeRule.band_pct=0.25``
    which is ~60x too tight (W-13 / CR-1).
``gamma_carry``
    Long vol when trailing realized is above implied by ``threshold`` vol points,
    short when it is below, flat in between.  The signal reads only the visible
    history through the engine's ``View``, so the no-look-ahead check applies to it.
``cone_rule``
    Sell vol when implied sits above the ``sell_pctile`` of the realized cone, buy
    below ``buy_pctile``.  A positioning rule rather than a spread rule.

Every entry rule returns ``-1 / 0 / +1`` and is evaluated **only at a roll**, which is
when a real desk makes the decision.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd

from ..types import HedgeRule
from .engine import BacktestConfig, PathData, View, run_backtest

__all__ = ["long_straddle", "short_straddle", "gamma_carry", "cone_rule",
           "always_on", "hedge_frequency_sweep", "run_grid", "DEFAULT_BAND_PCT"]

#: trader Q-2: 15% of the pair's gross option notional
DEFAULT_BAND_PCT = 15.0


def always_on(direction: int = +1) -> Callable[[View], int]:
    def rule(view: View) -> int:
        return direction
    rule.__name__ = f"always_on({direction:+d})"
    return rule


def gamma_carry(threshold_pts: float = 0.5, window: int = 21,
                allow_short: bool = True) -> Callable[[View], int]:
    """Long vol when RV - IV > threshold; short when below -threshold; else flat.

    ``threshold_pts`` is in **vol points** (0.5 = half a vol).  A typical EURUSD
    IV-RV spread is 0.5-1.5 points on a 7% vol, so a threshold below ~0.3 is noise
    (trader W-8).
    """
    def rule(view: View) -> int:
        rv = view.realized_vol(window)
        iv = view.vol
        if not (np.isfinite(rv) and np.isfinite(iv)):
            return 0
        d = (rv - iv) * 100.0
        if d > threshold_pts:
            return +1
        if d < -threshold_pts and allow_short:
            return -1
        return 0
    rule.__name__ = f"gamma_carry({threshold_pts}pts,{window}d)"
    return rule


def cone_rule(buy_pctile: float = 25.0, sell_pctile: float = 75.0,
              window: int = 21, lookback: int = 252) -> Callable[[View], int]:
    """Buy vol when implied is cheap against the realized cone, sell when rich."""
    def rule(view: View) -> int:
        h = view.history["spot"].to_numpy(float)
        if h.size < lookback + window + 2:
            return 0
        r = np.diff(np.log(h[-(lookback + window + 1):]))
        rolls = np.array([np.sqrt(252.0 * np.mean(r[i:i + window] ** 2))
                          for i in range(r.size - window + 1)])
        pct = float((rolls <= view.vol).mean() * 100.0)
        if pct <= buy_pctile:
            return +1
        if pct >= sell_pctile:
            return -1
        return 0
    rule.__name__ = f"cone_rule({buy_pctile},{sell_pctile},{window}d)"
    return rule


def _base(pair: str, direction: int, **kw: Any) -> BacktestConfig:
    cfg = BacktestConfig(pair=pair, direction=direction,
                         hedge=HedgeRule(mode="band", band_pct=DEFAULT_BAND_PCT))
    return replace(cfg, **kw) if kw else cfg


def long_straddle(pair: str = "EURUSD", *, tenor_days: int = 30,
                  band_pct: float = DEFAULT_BAND_PCT, notional_base: float = 10e6,
                  **kw: Any) -> BacktestConfig:
    """Buy the ATM straddle, delta hedge on a band, roll at expiry."""
    return _base(pair, +1, tenor_days=tenor_days, notional_base=notional_base,
                 hedge=HedgeRule(mode="band", band_pct=band_pct),
                 name=kw.pop("name", f"long straddle {tenor_days}d band {band_pct:g}%"),
                 **kw)


def short_straddle(pair: str = "EURUSD", *, tenor_days: int = 30,
                   band_pct: float = DEFAULT_BAND_PCT, notional_base: float = 10e6,
                   **kw: Any) -> BacktestConfig:
    """Sell the ATM straddle, delta hedge on a band, roll at expiry."""
    return _base(pair, -1, tenor_days=tenor_days, notional_base=notional_base,
                 hedge=HedgeRule(mode="band", band_pct=band_pct),
                 name=kw.pop("name", f"short straddle {tenor_days}d band {band_pct:g}%"),
                 **kw)


def hedge_frequency_sweep(path: PathData, cfg: BacktestConfig, *,
                          bands: Sequence[float] = (2, 5, 10, 15, 25, 40, 60, 100),
                          intervals: Sequence[int] | None = (1, 2, 5, 10, 21),
                          ) -> pd.DataFrame:
    """Net P&L, Sharpe, cost and turnover against band width **and** hedge interval.

    Delta-hedged straddle P&L is dominated by hedge frequency, so this sweep is the
    result, not a sensitivity annex.  Two families are run:

    * ``rule="band"``   -- band width as a % of gross option notional;
    * ``rule="time"``   -- rehedge every N steps regardless of delta.

    The argmax row is returned flagged ``argmax=True`` **and** ``in_sample=True``:
    picking the best band on the same path you fitted it to is not a result and
    REQ-062 requires the caution to travel with the number.
    """
    rows: list[dict[str, Any]] = []
    for b in bands:
        c = replace(cfg, hedge=HedgeRule(mode="band", band_pct=float(b),
                                         cost_bp=cfg.hedge.cost_bp,
                                         target_delta=cfg.hedge.target_delta),
                    name=f"band {b:g}%")
        r = run_backtest(path, c)
        rows.append({"rule": "band", "param": float(b), "label": f"band {b:g}%",
                     **{k: r.stats[k] for k in
                        ("total_pnl_net", "total_pnl_gross", "total_cost",
                         "cost_pct_of_gross", "sharpe_net", "n_hedges",
                         "hedges_per_day", "turnover_base", "max_drawdown",
                         "realized_vol_captured")}})
    for iv in (intervals or ()):
        c = replace(cfg, hedge=HedgeRule(mode="time", every_hours=24.0 * float(iv),
                                         cost_bp=cfg.hedge.cost_bp,
                                         target_delta=cfg.hedge.target_delta),
                    hedge_every_steps=int(iv) * int(path.steps_per_day),
                    name=f"every {iv} step(s)")
        r = run_backtest(path, c)
        rows.append({"rule": "time", "param": float(iv), "label": f"every {iv}d",
                     **{k: r.stats[k] for k in
                        ("total_pnl_net", "total_pnl_gross", "total_cost",
                         "cost_pct_of_gross", "sharpe_net", "n_hedges",
                         "hedges_per_day", "turnover_base", "max_drawdown",
                         "realized_vol_captured")}})
    df = pd.DataFrame(rows)
    df["argmax"] = df["total_pnl_net"] == df["total_pnl_net"].max()
    df["in_sample"] = True
    df["caution"] = "argmax band is IN-SAMPLE; do not read it as a recommendation"
    return df


def run_grid(path: PathData, configs: Sequence[BacktestConfig]) -> pd.DataFrame:
    """Run several configurations over one path and stack their stats."""
    return pd.DataFrame([run_backtest(path, c).stats for c in configs])
