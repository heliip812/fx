"""Delta-hedged gamma strategy backtests.

``engine``      event-driven walk-forward with cash accounting and a no-look-ahead View
``strategies``  straddle / strangle / RR presets, entry rules, the hedge-frequency sweep
``metrics``     the stats table and the REQ-064 P&L decomposition
"""
from __future__ import annotations

from . import engine, metrics, strategies
from .engine import (BacktestConfig, BacktestResult, PathData, lookahead_report,
                     path_from_history, run_backtest, synthetic_path)
from .metrics import bucket_stats, decompose, drawdown, sharpe, summarise
from .strategies import (cone_rule, gamma_carry, hedge_frequency_sweep,
                         long_straddle, run_grid, short_straddle)

__all__ = [
    "engine", "strategies", "metrics",
    "PathData", "synthetic_path", "path_from_history", "BacktestConfig",
    "BacktestResult", "run_backtest", "lookahead_report",
    "long_straddle", "short_straddle", "gamma_carry", "cone_rule",
    "hedge_frequency_sweep", "run_grid",
    "summarise", "decompose", "drawdown", "sharpe", "bucket_stats",
]
