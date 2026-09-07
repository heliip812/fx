"""Volatility cones with percentile bands (REQ-018).

A cone is the distribution of *realized* vol by horizon, drawn as percentile bands,
with today's realized and today's implied overlaid.  It answers "is 7.9% a high or a
low number for a one-month EURUSD vol" without needing an opinion.

Two honesty requirements, both from the trader review, are built in rather than
bolted on:

* **Overlapping windows are not independent samples (W-8).**  A 63-day rolling vol
  sampled daily over 500 days has ~8 independent observations, not 438.  Every row
  carries ``n_obs`` *and* ``n_eff = n_obs / horizon``, and ``interpretable`` is
  ``False`` below ``min_obs`` (default 100 raw observations, REQ-018's hatching
  rule).  Percentiles from an overlapping sample are still the right *description*
  of the sample; what they are not is a confidence statement, and the frame says so.
* **The basis is printed.**  ``sqrt(252)``, matching ``signals.realized``.
"""
from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from .realized import ANNUAL, ESTIMATORS, realized_vol, rolling_vol

__all__ = ["vol_cone", "effective_n", "cone_percentile", "DEFAULT_HORIZONS",
           "DEFAULT_PCTILES"]

DEFAULT_HORIZONS: tuple[int, ...] = (5, 10, 21, 42, 63, 126, 252)
DEFAULT_PCTILES: tuple[int, ...] = (5, 25, 50, 75, 95)


def effective_n(n_obs: int, horizon: int) -> float:
    """Independent-observation count for an overlapping rolling window."""
    return float(max(n_obs, 0)) / float(max(horizon, 1))


def vol_cone(df: pd.DataFrame, horizons: Sequence[int] = DEFAULT_HORIZONS,
             percentiles: Sequence[int] = DEFAULT_PCTILES, *,
             method: str = "close_to_close", annual: float = ANNUAL,
             implied: Mapping[int, float] | None = None,
             min_obs: int = 100) -> pd.DataFrame:
    """Realized-vol percentiles by horizon, with the current values overlaid.

    Parameters
    ----------
    implied : optional ``{horizon_days: implied_vol}`` to overlay.  Match it on
        **calendar days to the actual expiry**, not on a nominal tenor label:
        21 business days is 30 calendar days, and comparing a 21bd RV to a "1M"
        implied is trader W-8's sign-flipping error.

    Columns
    -------
    ``horizon, p5..p95, current, implied, spread (implied - current), n_obs, n_eff,
    interpretable, method, annualisation``
    """
    if method not in ESTIMATORS:
        raise KeyError(f"unknown estimator {method!r}; have {sorted(ESTIMATORS)}")
    rows = []
    for h in horizons:
        ser = rolling_vol(df, window=int(h), method=method, annual=annual).dropna()
        n = int(ser.size)
        row: dict[str, object] = {"horizon": int(h), "n_obs": n,
                                  "n_eff": effective_n(n, int(h)),
                                  "interpretable": n >= int(min_obs),
                                  "method": method,
                                  "annualisation": f"sqrt({annual:g})"}
        for p in percentiles:
            row[f"p{p}"] = float(np.percentile(ser.to_numpy(), p)) if n else float("nan")
        cur = float(ser.iloc[-1]) if n else float("nan")
        row["current"] = cur
        row["current_pctile"] = (float((ser.to_numpy() <= cur).mean() * 100.0)
                                 if n else float("nan"))
        iv = float(implied[int(h)]) if implied and int(h) in implied else float("nan")
        row["implied"] = iv
        row["spread"] = iv - cur
        rows.append(row)
    cols = (["horizon"] + [f"p{p}" for p in percentiles]
            + ["current", "current_pctile", "implied", "spread",
               "n_obs", "n_eff", "interpretable", "method", "annualisation"])
    return pd.DataFrame(rows)[cols]


def cone_percentile(df: pd.DataFrame, horizon: int, value: float, *,
                    method: str = "close_to_close", annual: float = ANNUAL) -> float:
    """Where ``value`` sits in the cone for ``horizon``, in percent (0-100).

    Used as a backtest entry rule ("sell vol above the 75th percentile") -- see
    ``fxgamma.backtest.strategies``.
    """
    ser = rolling_vol(df, window=int(horizon), method=method, annual=annual).dropna()
    if not ser.size or not np.isfinite(value):
        return float("nan")
    return float((ser.to_numpy() <= float(value)).mean() * 100.0)
