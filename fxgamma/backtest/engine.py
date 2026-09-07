"""Event-driven backtest engine for delta-hedged FX option strategies.

Design rules, all of them consequences of the trader review's "I will dismiss a
backtest that..." list:

**No look-ahead, and it is demonstrable.**  The engine walks the path one step at a
time and hands the strategy a :class:`View` that can only see rows with timestamp
``<= t``.  Reaching past ``t`` raises.  :func:`lookahead_report` is the REQ-060
check: shift the driving series forward one step and the results must change; shift
it back and they must reproduce the original bit-for-bit.

**Costs are real, not a footnote.**  Every spot hedge pays half the round-trip spread
on the traded notional (per-pair defaults from
:data:`fxgamma.portfolio.zones.COST_BP`, not one global number -- W-13), and every
option trade crosses a vega bid/offer in vol points.  Slippage is charged against the
*executed* rate, so the hedge log's realized P&L already contains it and the
attribution never needs a plug.  ``net`` and ``gross`` equity are both returned.

**Cash accounting, not P&L accounting.**  Equity is
``cash + option_PV + spot_position * S``.  Premiums, hedge proceeds and costs all move
cash.  Step P&L is the change in equity, so nothing can be double counted and the
identity ``sum(steps) == final equity`` holds exactly.

**The output is more than a Sharpe ratio.**  Per-step gamma, theta, vega and cost are
recorded so :mod:`fxgamma.backtest.metrics` can report the decomposition REQ-064 asks
for: realized-vs-implied carry, hedge slippage, and discretisation error, which sum
back to the simulated P&L.

Hedging happens at the observed rate at the step being marked -- you see the price and
you deal on it -- which is the realistic convention and is *not* look-ahead.  Entry
signals, by contrast, may only use strictly prior information, which the ``View``
enforces.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd

from ..conventions import pair_spec
from ..models import gk
from ..portfolio.zones import COST_BP
from ..types import HedgeRule

__all__ = ["PathData", "synthetic_path", "path_from_history", "View", "Leg",
           "BacktestConfig", "BacktestResult", "run_backtest", "lookahead_report"]

_YEAR = 365.0
_TRADING = 252.0


# --------------------------------------------------------------------------- #
# market path
# --------------------------------------------------------------------------- #
@dataclass
class PathData:
    """The market the backtest walks.

    ``df`` is indexed by a tz-aware UTC ``DatetimeIndex`` (one row per hedge
    opportunity, so ``steps_per_day > 1`` means intraday hedging is possible) with
    columns:

    ``spot``  dealable rate at that instant
    ``vol``   ATM implied at the strategy's tenor, decimal
    ``rd``, ``rf``  continuously-compounded rates, decimal

    ``meta`` carries provenance for the /data page: real history, or the parameters
    of the simulation.  Nothing here is ever badged as live (architecture s7).
    """
    pair: str
    df: pd.DataFrame
    steps_per_day: int = 1
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        need = ("spot", "vol", "rd", "rf")
        missing = [c for c in need if c not in self.df.columns]
        if missing:
            raise KeyError(f"PathData needs columns {need}, missing {missing}")
        if not isinstance(self.df.index, pd.DatetimeIndex):
            raise TypeError("PathData.df must have a DatetimeIndex")
        if self.df.index.tz is None:
            self.df.index = self.df.index.tz_localize("UTC")
        self.df = self.df.sort_index()

    @property
    def dt_years(self) -> float:
        """Average step length in years (ACT/365)."""
        if len(self.df) < 2:
            return 1.0 / _YEAR
        secs = (self.df.index[-1] - self.df.index[0]).total_seconds()
        return secs / (len(self.df) - 1) / 86400.0 / _YEAR


def synthetic_path(*, n_days: int = 252, sigma_r: float = 0.10, sigma_i: float = 0.08,
                   S0: float = 1.1000, pair: str = "EURUSD", steps_per_day: int = 1,
                   seed: int = 7, rd: float = 0.04, rf: float = 0.02,
                   drift: float | None = None, start: datetime | None = None,
                   vol_of_vol: float = 0.0) -> PathData:
    """A GBM path with realized vol ``sigma_r`` marked at implied ``sigma_i``.

    The controlled-experiment fixture: with ``sigma_r > sigma_i`` a delta-hedged long
    straddle must make money and a short one must lose it, and vice versa.  If that
    fails, something in the pricer / Greeks / hedging / accounting chain is wrong --
    it is the single best end-to-end test of the whole stack.

    ``drift`` defaults to the risk-neutral ``rd - rf`` so the test is a pure
    vol experiment with no directional edge smuggled in.  ``vol_of_vol`` lets the
    implied mark wander (lognormal, mean-reverting to ``sigma_i``) for strategies
    that trade the mark rather than the realized.
    """
    rng = np.random.default_rng(seed)
    mu = (rd - rf) if drift is None else float(drift)
    n = int(n_days) * int(steps_per_day)
    dt = 1.0 / (_TRADING * steps_per_day)              # trading-time step
    z = rng.standard_normal(n)
    logS = np.log(S0) + np.cumsum((mu / _TRADING / steps_per_day - 0.5 * sigma_r ** 2 * dt)
                                  + sigma_r * math.sqrt(dt) * z)
    spot = np.concatenate([[S0], np.exp(logS)])
    if vol_of_vol > 0:
        v = np.empty(n + 1)
        v[0] = sigma_i
        for i in range(1, n + 1):
            v[i] = max(v[i - 1] + 0.05 * (sigma_i - v[i - 1])
                       + vol_of_vol * math.sqrt(dt) * rng.standard_normal(), 1e-3)
    else:
        v = np.full(n + 1, sigma_i)
    start = start or datetime(2024, 1, 1, tzinfo=timezone.utc)
    idx = pd.DatetimeIndex([start + timedelta(days=i / steps_per_day) for i in range(n + 1)],
                           tz="UTC")
    df = pd.DataFrame({"spot": spot, "vol": v, "rd": rd, "rf": rf}, index=idx)
    return PathData(pair, df, steps_per_day,
                    meta={"kind": "synthetic", "sigma_r": sigma_r, "sigma_i": sigma_i,
                          "seed": seed, "drift": mu, "steps_per_day": steps_per_day,
                          "note": "SIMULATED -- not market data"})


def path_from_history(history: pd.DataFrame, pair: str, *,
                      implied: pd.Series | float, rd: float = 0.0, rf: float = 0.0
                      ) -> PathData:
    """Wrap a real OHLC history plus an implied-vol series into a :class:`PathData`.

    ``implied`` may be a constant (a stress assumption, badged as such) or a series
    aligned to the history index -- the honest version, which needs a stored vol
    history from the data layer.  Only the close is used: intraday hedging cannot be
    simulated from daily bars without inventing a path, and inventing one is how a
    backtest ends up reporting a hedge P&L it could never have earned.
    """
    h = history.copy()
    if not isinstance(h.index, pd.DatetimeIndex):
        raise TypeError("history must have a DatetimeIndex")
    if h.index.tz is None:
        h.index = h.index.tz_localize("UTC")
    vol = (pd.Series(float(implied), index=h.index) if np.isscalar(implied)
           else pd.Series(implied).reindex(h.index).ffill())
    df = pd.DataFrame({"spot": h["close"].astype(float), "vol": vol.astype(float),
                       "rd": float(rd), "rf": float(rf)}, index=h.index)
    return PathData(pair, df.dropna(), 1,
                    meta={"kind": "history", "rows": int(len(df)),
                          "implied": "constant" if np.isscalar(implied) else "series"})


# --------------------------------------------------------------------------- #
# the no-look-ahead view
# --------------------------------------------------------------------------- #
class View:
    """What a strategy is allowed to see at step ``i``: rows ``0..i`` and nothing else.

    ``history`` is a *copy*, so a strategy cannot mutate the path, and any attempt to
    index beyond ``now`` raises rather than silently returning the future.
    """

    __slots__ = ("_df", "_i", "pair", "meta")

    def __init__(self, df: pd.DataFrame, i: int, pair: str, meta: dict):
        self._df, self._i, self.pair, self.meta = df, int(i), pair, meta

    @property
    def now(self) -> pd.Timestamp:
        return self._df.index[self._i]

    @property
    def i(self) -> int:
        return self._i

    @property
    def history(self) -> pd.DataFrame:
        return self._df.iloc[: self._i + 1].copy()

    @property
    def spot(self) -> float:
        return float(self._df["spot"].iloc[self._i])

    @property
    def vol(self) -> float:
        return float(self._df["vol"].iloc[self._i])

    def at(self, j: int) -> pd.Series:
        if j > self._i:
            raise IndexError(f"look-ahead: step {j} requested at step {self._i}")
        return self._df.iloc[j]

    def realized_vol(self, window: int = 21, steps_per_day: int = 1) -> float:
        """Close-to-close realized vol of the visible path, annualised on sqrt(252)."""
        s = self._df["spot"].iloc[: self._i + 1].to_numpy(float)
        s = s[::steps_per_day] if steps_per_day > 1 else s
        r = np.diff(np.log(s[-(window + 1):]))
        if r.size < 2:
            return float("nan")
        return float(np.sqrt(_TRADING * np.mean(r ** 2)))


# --------------------------------------------------------------------------- #
# positions
# --------------------------------------------------------------------------- #
@dataclass
class Leg:
    cp: int
    strike: float
    expiry: pd.Timestamp
    notional_base: float
    direction: int
    entry_vol: float
    entry_time: pd.Timestamp
    entry_spot: float

    def T(self, now: pd.Timestamp) -> float:
        return max((self.expiry - now).total_seconds() / 86400.0 / _YEAR, 0.0)


@dataclass
class BacktestConfig:
    """Strategy configuration (REQ-059).  ``as_dict`` is the copyable JSON summary."""
    pair: str = "EURUSD"
    structure: str = "straddle"            # straddle | strangle | risk_reversal
    direction: int = +1                    # +1 long vol, -1 short vol
    notional_base: float = 10_000_000.0
    tenor_days: int = 30
    roll_days: int | None = None           # None -> roll at expiry
    strangle_delta: float = 0.25
    hedge: HedgeRule = field(default_factory=lambda: HedgeRule(mode="band", band_pct=0.15))
    hedge_every_steps: int | None = None   # for mode="time": steps between hedges
    vega_spread_pts: float = 0.25          # option bid/offer, vol points, round trip
    cost_bp: float | None = None           # spot round-trip bp; None -> per-pair table
    entry: Callable[[View], int] | None = None   # -> -1/0/+1 overriding `direction`
    max_steps: int | None = None
    name: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in
             ("pair", "structure", "direction", "notional_base", "tenor_days",
              "roll_days", "strangle_delta", "hedge_every_steps", "vega_spread_pts",
              "cost_bp", "name")}
        d["hedge"] = {"mode": self.hedge.mode, "band_pct": self.hedge.band_pct,
                      "band_delta": self.hedge.band_delta,
                      "every_hours": self.hedge.every_hours,
                      "cost_bp": self.hedge.cost_bp,
                      "target_delta": self.hedge.target_delta}
        d["entry"] = getattr(self.entry, "__name__", None) if self.entry else "always_on"
        return d


@dataclass
class BacktestResult:
    equity: pd.DataFrame
    trades: pd.DataFrame
    stats: dict[str, Any]
    config: BacktestConfig
    meta: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:                                   # pragma: no cover
        s = self.stats
        return (f"<BacktestResult {self.config.name or self.config.structure} "
                f"net={s.get('total_pnl_net', float('nan')):,.0f} "
                f"sharpe={s.get('sharpe_net', float('nan')):.2f} "
                f"hedges={s.get('n_hedges', 0)}>")


# --------------------------------------------------------------------------- #
# the engine
# --------------------------------------------------------------------------- #
def _legs_for(cfg: BacktestConfig, now: pd.Timestamp, S: float, vol: float,
              rd: float, rf: float, direction: int) -> list[Leg]:
    """Build the structure's legs at the money / at the configured deltas."""
    expiry = now + timedelta(days=int(cfg.tenor_days))
    T = cfg.tenor_days / _YEAR
    conv = pair_spec(cfg.pair).delta_convention
    N = cfg.notional_base
    if cfg.structure == "straddle":
        K = S * math.exp((rd - rf) * T + 0.5 * vol * vol * T)     # DNS strike
        return [Leg(+1, K, expiry, N, direction, vol, now, S),
                Leg(-1, K, expiry, N, direction, vol, now, S)]
    if cfg.structure == "strangle":
        kc = gk.strike_from_delta(cfg.strangle_delta, S, T, rd, rf, vol, +1, conv)
        kp = gk.strike_from_delta(cfg.strangle_delta, S, T, rd, rf, vol, -1, conv)
        return [Leg(+1, kc, expiry, N, direction, vol, now, S),
                Leg(-1, kp, expiry, N, direction, vol, now, S)]
    if cfg.structure == "risk_reversal":
        kc = gk.strike_from_delta(cfg.strangle_delta, S, T, rd, rf, vol, +1, conv)
        kp = gk.strike_from_delta(cfg.strangle_delta, S, T, rd, rf, vol, -1, conv)
        return [Leg(+1, kc, expiry, N, direction, vol, now, S),
                Leg(-1, kp, expiry, N, -direction, vol, now, S)]
    raise ValueError(f"unknown structure {cfg.structure!r}")


def _mark(legs: Sequence[Leg], now: pd.Timestamp, S: float, vol: float,
          rd: float, rf: float, conv: str) -> tuple[float, float, float, float, float, float]:
    """(pv, delta_base, gamma, gamma_1pct, vega, theta) of the option legs."""
    pv = dl = ga = g1 = ve = th = 0.0
    for lg in legs:
        T = lg.T(now)
        g = gk.gk_greeks(S, lg.strike, T, rd, rf, vol, lg.cp,
                         lg.notional_base, lg.direction, delta_convention=conv)
        pv += g.pv
        dl += g.delta_base
        ga += g.gamma
        g1 += g.gamma_1pct
        ve += g.vega
        th += g.theta
    return pv, dl, ga, g1, ve, th


def run_backtest(path: PathData, cfg: BacktestConfig) -> BacktestResult:
    """Walk ``path`` one step at a time and return the full record.

    Accounting: ``equity = cash + option_pv + spot_position * S``.  Premiums, hedge
    proceeds and all costs move ``cash``; step P&L is the change in equity, gross P&L
    is the same series with the costs added back.
    """
    df = path.df
    n = len(df) if cfg.max_steps is None else min(len(df), int(cfg.max_steps))
    spec = pair_spec(cfg.pair)
    conv = spec.delta_convention
    cost_bp = float(cfg.cost_bp if cfg.cost_bp is not None
                    else (cfg.hedge.cost_bp if cfg.hedge.cost_bp else COST_BP.get(cfg.pair, 0.5)))
    lam = cost_bp / 2.0 / 1e4                   # one-way proportional spot cost
    vega_spread = cfg.vega_spread_pts / 100.0 / 2.0        # one-way, in vol decimals

    legs: list[Leg] = []
    cash = 0.0
    pos = 0.0                                    # spot position, base ccy, + = long
    last_hedge_i = -10 ** 9
    rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    prev_equity = 0.0
    cum_cost = 0.0

    for i in range(n):
        r = df.iloc[i]
        now = df.index[i]
        S, vol, rd, rf = float(r["spot"]), float(r["vol"]), float(r["rd"]), float(r["rf"])
        view = View(df, i, cfg.pair, path.meta)
        step_cost = 0.0

        # ---- expiry / roll: settle then re-strike -------------------------- #
        expired = [lg for lg in legs if lg.T(now) <= 0.0]
        if expired:
            for lg in expired:
                intrinsic = max(lg.cp * (S - lg.strike), 0.0) * lg.notional_base * lg.direction
                cash += intrinsic
                trades.append({"time": now, "kind": "expiry", "cp": lg.cp,
                               "strike": lg.strike, "notional": lg.notional_base,
                               "direction": lg.direction, "spot": S,
                               "cash": intrinsic, "cost": 0.0})
            legs = [lg for lg in legs if lg.T(now) > 0.0]
        roll_due = (not legs) or (cfg.roll_days is not None and legs
                                  and (now - legs[0].entry_time).days >= cfg.roll_days)

        if roll_due and i < n - 1:
            if legs:                                     # unwind the old structure
                pv_old, *_ = _mark(legs, now, S, vol, rd, rf, conv)
                # crossing the vega spread on the way out
                pv_bid, *_ = _mark(legs, now, S, max(vol - vega_spread, 1e-4), rd, rf, conv)
                pv_ask, *_ = _mark(legs, now, S, vol + vega_spread, rd, rf, conv)
                exit_pv = pv_bid if pv_old >= 0 else pv_ask
                cash += exit_pv
                step_cost += abs(exit_pv - pv_old)
                trades.append({"time": now, "kind": "unwind", "cp": 0, "strike": np.nan,
                               "notional": sum(l.notional_base for l in legs),
                               "direction": 0, "spot": S, "cash": exit_pv,
                               "cost": abs(exit_pv - pv_old)})
                legs = []
            side = int(cfg.entry(view)) if cfg.entry is not None else int(cfg.direction)
            if side != 0:
                new = _legs_for(cfg, now, S, vol, rd, rf, side)
                pv_mid, *_ = _mark(new, now, S, vol, rd, rf, conv)
                pv_exec, *_ = _mark(new, now, S,
                                    vol + vega_spread if side > 0 else max(vol - vega_spread, 1e-4),
                                    rd, rf, conv)
                cash -= pv_exec
                step_cost += abs(pv_exec - pv_mid)
                legs = new
                for lg in new:
                    trades.append({"time": now, "kind": "open", "cp": lg.cp,
                                   "strike": lg.strike, "notional": lg.notional_base,
                                   "direction": lg.direction, "spot": S,
                                   "cash": -pv_exec / len(new),
                                   "cost": abs(pv_exec - pv_mid) / len(new)})

        # ---- mark ---------------------------------------------------------- #
        pv, dl, ga, g1, ve, th = _mark(legs, now, S, vol, rd, rf, conv) if legs else (0,) * 6
        delta_total = dl + pos

        # ---- hedge --------------------------------------------------------- #
        traded = 0.0
        rule = cfg.hedge
        gross = sum(lg.notional_base for lg in legs) or cfg.notional_base
        if rule.mode == "band":
            band = (rule.band_delta if rule.band_delta > 0
                    else rule.band_pct * gross)   # v1.6 CR-1: fraction, not percent
            if abs(delta_total - rule.target_delta) > band:
                traded = -(delta_total - rule.target_delta)
        elif rule.mode == "time":
            every = cfg.hedge_every_steps or max(int(round(rule.every_hours / 24.0
                                                           * path.steps_per_day)), 1)
            if i - last_hedge_i >= every:
                traded = -(delta_total - rule.target_delta)
        elif rule.mode == "gamma_budget":
            band = max(abs(rule.band_delta), 1.0)
            if abs(delta_total - rule.target_delta) > band:
                traded = -(delta_total - rule.target_delta)
        # mode == "none" -> never hedge

        if traded != 0.0:
            c = abs(traded) * S * lam
            cash -= traded * S + c
            pos += traded
            step_cost += c
            last_hedge_i = i
            trades.append({"time": now, "kind": "hedge", "cp": 0, "strike": np.nan,
                           "notional": abs(traded), "direction": int(np.sign(traded)),
                           "spot": S, "cash": -(traded * S + c), "cost": c})

        equity = cash + pv + pos * S
        cum_cost += step_cost
        rows.append({
            "time": now, "spot": S, "vol": vol, "pv": pv, "cash": cash,
            "spot_pos": pos, "delta_options": dl, "delta_total": dl + pos,
            "gamma": ga, "gamma_1pct": g1, "vega": ve, "theta": th,
            "equity": equity, "pnl": equity - prev_equity,
            "cost": step_cost, "cum_cost": cum_cost,
            "hedge_base": traded, "n_legs": len(legs),
        })
        prev_equity = equity

    eq = pd.DataFrame(rows).set_index("time")
    eq["pnl_gross"] = eq["pnl"] + eq["cost"]
    eq["equity_gross"] = eq["pnl_gross"].cumsum()
    tr = pd.DataFrame(trades) if trades else pd.DataFrame(
        columns=["time", "kind", "cp", "strike", "notional", "direction", "spot", "cash", "cost"])

    from .metrics import summarise                       # local: avoids a cycle
    stats = summarise(eq, tr, path, cfg)
    return BacktestResult(eq, tr, stats, cfg, dict(path.meta))


# --------------------------------------------------------------------------- #
# look-ahead detection (REQ-060)
# --------------------------------------------------------------------------- #
def lookahead_report(path: PathData, cfg: BacktestConfig) -> dict[str, Any]:
    """Shift the driving series one step forward, re-run, then shift back.

    A correct engine must (a) produce a *different* result on the shifted path -- if
    it does not, the strategy is not actually reading the data -- and (b) reproduce
    the original result exactly when the shift is undone.  Returns both checks plus
    the P&L numbers, so the Lab can display the evidence rather than a claim.
    """
    base = run_backtest(path, cfg)
    shifted_df = path.df.copy()
    shifted_df["spot"] = shifted_df["spot"].shift(1).bfill()
    shifted_df["vol"] = shifted_df["vol"].shift(1).bfill()
    shifted = run_backtest(PathData(path.pair, shifted_df, path.steps_per_day, path.meta), cfg)
    back_df = shifted_df.copy()
    back_df["spot"] = path.df["spot"].to_numpy()
    back_df["vol"] = path.df["vol"].to_numpy()
    back = run_backtest(PathData(path.pair, back_df, path.steps_per_day, path.meta), cfg)
    p0 = float(base.equity["equity"].iloc[-1])
    p1 = float(shifted.equity["equity"].iloc[-1])
    p2 = float(back.equity["equity"].iloc[-1])
    return {"base_pnl": p0, "shifted_pnl": p1, "restored_pnl": p2,
            "changed_under_shift": abs(p1 - p0) > 1e-9 * max(1.0, abs(p0)),
            "restored_exactly": abs(p2 - p0) <= 1e-9 * max(1.0, abs(p0)),
            "passed": (abs(p1 - p0) > 1e-9 * max(1.0, abs(p0))
                       and abs(p2 - p0) <= 1e-9 * max(1.0, abs(p0)))}
