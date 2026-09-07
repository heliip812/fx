"""Backtest statistics, and the decomposition that makes them auditable.

A Sharpe ratio on its own is a number about the sample, not about the strategy
(trader s7 / REQ-065), so :func:`summarise` returns the whole desk-relevant set --
gross and net P&L, cost as a share of gross, turnover, hedge count, average daily
gamma and theta, drawdown, hit rate, and the realized vol the run actually captured
(REQ-056) -- and **every statistic states its annualisation basis**.

:func:`decompose` is REQ-064: it splits the simulated P&L into the terms a trader can
argue with::

    gross P&L  =  delta  +  gamma  +  theta  +  vega  +  residual
    net P&L    =  gross  -  costs

with a second, independent cross-check against the continuous-hedging identity
``50 * G1 * S * (sigma_r^2 - sigma_i^2) * dt`` (:func:`fxgamma.portfolio.risk.dhedge_pnl`,
the W-5-corrected form).  The gap between ``gamma + theta`` and that identity **is**
the discretisation error -- the price of hedging at a finite frequency -- and it is
reported as its own line rather than being left inside "residual".  That is what tells
you whether the P&L came from vol or from hedge luck.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

__all__ = ["summarise", "decompose", "drawdown", "sharpe", "bucket_stats",
           "TRADING_DAYS"]

TRADING_DAYS = 252.0
_YEAR = 365.0


def drawdown(equity: pd.Series) -> pd.DataFrame:
    """Running peak, drawdown and the current underwater duration in steps."""
    e = pd.Series(equity).astype(float)
    peak = e.cummax()
    dd = e - peak
    under = (dd < 0).astype(int)
    dur = under * (under.groupby((under != under.shift()).cumsum()).cumcount() + 1)
    return pd.DataFrame({"equity": e, "peak": peak, "drawdown": dd, "underwater": dur})


def sharpe(pnl: pd.Series, steps_per_year: float) -> float:
    """Annualised Sharpe of a P&L series (zero risk-free; it is an excess series).

    ``steps_per_year`` is stated by the caller and echoed in the stats dict, because
    an unstated basis is how a 12-step-a-day backtest reports a Sharpe of 30.
    """
    p = pd.Series(pnl).astype(float).dropna()
    if p.size < 2 or p.std(ddof=1) == 0:
        return float("nan")
    return float(p.mean() / p.std(ddof=1) * math.sqrt(steps_per_year))


def summarise(equity: pd.DataFrame, trades: pd.DataFrame, path: Any,
              cfg: Any) -> dict[str, Any]:
    """The stats table for REQ-061, with bases stated."""
    steps_per_day = float(getattr(path, "steps_per_day", 1) or 1)
    steps_per_year = TRADING_DAYS * steps_per_day
    eq = equity
    pnl_net = eq["pnl"].astype(float)
    pnl_gross = eq["pnl_gross"].astype(float)
    total_net = float(eq["equity"].iloc[-1]) if len(eq) else 0.0
    total_gross = float(eq["equity_gross"].iloc[-1]) if len(eq) else 0.0
    cost = float(eq["cost"].sum())
    dd = drawdown(eq["equity"])
    hedges = trades[trades["kind"] == "hedge"] if len(trades) else trades
    turnover = float(hedges["notional"].sum()) if len(hedges) else 0.0
    hedge_cost = float(hedges["cost"].sum()) if len(hedges) else 0.0
    opt_cost = cost - hedge_cost

    # daily aggregation for the "per day" numbers, whatever the step frequency
    daily = pnl_net.groupby(eq.index.date).sum() if len(eq) else pnl_net
    dS = eq["spot"].diff()
    gamma_pnl = 0.5 * eq["gamma"].shift(1) * dS ** 2
    theta_pnl = eq["theta"].shift(1) * (eq.index.to_series().diff().dt.total_seconds() / 86400.0)

    sig_captured = float("nan")
    th = float(theta_pnl.sum())
    if len(eq) and th != 0:
        # the sigma_r that would have made gamma pay the theta bill (REQ-056)
        g_sum = float(gamma_pnl.sum())
        sig_i = float(np.nanmean(eq["vol"]))
        if g_sum > 0 and abs(th) > 0:
            sig_captured = float(sig_i * math.sqrt(g_sum / abs(th)))

    return {
        "name": getattr(cfg, "name", "") or getattr(cfg, "structure", ""),
        "pair": getattr(cfg, "pair", ""),
        "steps": int(len(eq)),
        "days": int(len(daily)) if len(eq) else 0,
        "total_pnl_net": total_net,
        "total_pnl_gross": total_gross,
        "total_cost": cost,
        "hedge_cost": hedge_cost,
        "option_cost": opt_cost,
        "cost_pct_of_gross": (abs(cost) / abs(total_gross) * 100.0
                              if total_gross else float("nan")),
        "sharpe_net": sharpe(pnl_net, steps_per_year),
        "sharpe_gross": sharpe(pnl_gross, steps_per_year),
        "sharpe_basis": f"sqrt({steps_per_year:g}) -- {TRADING_DAYS:g} trading days"
                        f" x {steps_per_day:g} steps/day",
        "hit_rate_daily": float((daily > 0).mean()) if len(daily) else float("nan"),
        "avg_daily_pnl": float(daily.mean()) if len(daily) else float("nan"),
        "sd_daily_pnl": float(daily.std(ddof=1)) if len(daily) > 1 else float("nan"),
        "avg_daily_gamma_pnl": float(gamma_pnl.sum() / max(len(daily), 1)),
        "avg_daily_theta": float(theta_pnl.sum() / max(len(daily), 1)),
        "max_drawdown": float(dd["drawdown"].min()) if len(dd) else 0.0,
        "max_underwater_steps": int(dd["underwater"].max()) if len(dd) else 0,
        "turnover_base": turnover,
        "n_hedges": int(len(hedges)),
        "hedges_per_day": float(len(hedges) / max(len(daily), 1)),
        "realized_vol_captured": sig_captured,
        "avg_implied": float(np.nanmean(eq["vol"])) if len(eq) else float("nan"),
        "annualisation": "P&L is in quote ccy; vols on sqrt(252); theta on ACT/365",
        "provenance": getattr(path, "meta", {}).get("kind", "unknown"),
    }


def decompose(result: Any) -> pd.DataFrame:
    """REQ-064: split the simulated P&L into terms that must sum back to it.

    Rows: ``delta``, ``gamma``, ``theta``, ``vega``, ``residual`` (everything of
    higher order -- charm, veta, speed, and the vol/spot cross terms), ``costs`` and
    ``net``; plus the two audit lines ``carry_identity`` (continuous-hedging
    expectation at the path's own realized vol) and ``discretisation`` (``gamma +
    theta - carry_identity``), which is the P&L attributable purely to hedging at a
    finite frequency.
    """
    eq = result.equity
    if len(eq) < 3:
        return pd.DataFrame(columns=["component", "pnl", "share_of_gross"])
    dt_days = eq.index.to_series().diff().dt.total_seconds() / 86400.0
    dS = eq["spot"].diff()
    dvol = eq["vol"].diff()
    delta = float((eq["delta_total"].shift(1) * dS).sum())
    gamma = float((0.5 * eq["gamma"].shift(1) * dS ** 2).sum())
    theta = float((eq["theta"].shift(1) * dt_days).sum())
    vega = float((eq["vega"].shift(1) * dvol / 0.01).sum())
    gross = float(eq["equity_gross"].iloc[-1])
    costs = -float(eq["cost"].sum())
    resid = gross - (delta + gamma + theta + vega)

    # continuous-hedging identity at the path's own step-by-step realized vol
    dt_yr = dt_days / _YEAR
    r = np.log(eq["spot"]).diff()
    var_r = (r ** 2) / dt_yr.replace(0.0, np.nan)
    carry = float((0.5 * eq["gamma"].shift(1) * eq["spot"].shift(1) ** 2
                   * (var_r - eq["vol"].shift(1) ** 2) * dt_yr).sum())
    rows = [
        ("delta", delta), ("gamma", gamma), ("theta", theta), ("vega", vega),
        ("residual", resid), ("gross", gross), ("costs", costs),
        ("net", gross + costs),
        ("carry_identity", carry), ("discretisation", gamma + theta - carry),
    ]
    df = pd.DataFrame(rows, columns=["component", "pnl"])
    df["share_of_gross"] = df["pnl"] / gross if gross else float("nan")
    return df


def bucket_stats(result: Any, tags: pd.Series, *, min_n: int = 20) -> pd.DataFrame:
    """Stats per regime / event bucket (REQ-065), with small buckets flagged.

    ``tags`` is a series aligned to ``result.equity.index``.  Buckets with fewer than
    ``min_n`` observations are marked ``interpretable=False``: an unconditional
    five-year Sharpe is a number about the sample, and so is a six-day bucket.
    """
    eq = result.equity
    t = pd.Series(tags).reindex(eq.index)
    steps_per_year = TRADING_DAYS * float((result.meta or {}).get("steps_per_day", 1) or 1)
    rows = []
    for name, idx in t.groupby(t).groups.items():
        sub = eq.loc[idx]
        rows.append({
            "bucket": name, "n": int(len(sub)),
            "total_pnl_net": float(sub["pnl"].sum()),
            "avg_pnl": float(sub["pnl"].mean()),
            "sharpe_net": sharpe(sub["pnl"], steps_per_year),
            "hit_rate": float((sub["pnl"] > 0).mean()),
            "cost": float(sub["cost"].sum()),
            "interpretable": len(sub) >= int(min_n),
        })
    return pd.DataFrame(rows).sort_values("bucket", ignore_index=True)
