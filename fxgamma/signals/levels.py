"""Candidate price anchors, and the **measurement** that says whether any of them work.

The PM steer (``docs/08_overnight_gamma.md`` §2) is the whole design of this module:
prior-session high/low/close, round numbers, pivots, swing points, moving averages and
open-interest clusters enter as *candidate anchors*.  Whether snapping a resting rung
to one of them improves anything is an **empirical question**, and this file contains
the experiment as well as the levels.

Nothing here asserts that a level "works".  :func:`technical_levels` returns a
``strength`` column, and that column is a **display prior** -- a hand-set ordering used
to break ties and to decide what to draw first.  It is not a probability, it is not
fitted, and it must never be presented as evidence.  The evidence is
:func:`measure_reversal_stats`, which measures each kind against a **distance-matched
random control** and reports the effect size, the dependence-corrected interval, and
the multiple-testing adjustment.

Why a control is not optional
-----------------------------
Every level in this file sits *near spot* -- that is what makes it a candidate.  Spot
is therefore likely to touch it, likely to trade through it, and likely to come back,
because spot does all of those things around any nearby price.  Measuring "how often
does spot reverse at yesterday's high" without a control measures the behaviour of
*spot*, not of *yesterday's high*.  The control here is a price drawn at the same
distance-from-spot distribution and evaluated by the identical code path, so the only
thing that differs between the two columns is whether the price is special.

And the multiple-comparisons point, stated once and repeated in the output frame:
this module tests **eight or more** level kinds.  At the 5% level, one of eight will
look significant by chance roughly a third of the time.  ``p_reversal_bh`` and
``p_reversal_holm`` are the numbers to read; ``p_reversal`` on its own is not.

Look-ahead
----------
A level stamped ``asof=d`` uses only bars with index ``<= d``, and a swing point
additionally requires its ``k`` confirming bars to have already printed.  The panel
builder and the measurement never see a bar they would not have had.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from ..conventions import pair_spec

__all__ = [
    "LEVEL_KINDS", "KIND_GROUP", "STRENGTH_PRIOR", "LEVEL_COLUMNS",
    "technical_levels", "oi_levels", "level_panel", "measure_reversal_stats",
    "nearest_level", "pivot_levels", "swing_points", "round_levels",
]

#: canonical output columns.  The first five are frozen by the contract; the rest are
#: additive and carry the provenance a rung needs to explain itself.
LEVEL_COLUMNS = ["level", "kind", "strength", "age_days", "source",
                 "group", "side", "dist_pips", "asof"]

#: level kinds this module can produce.  Every one of them is a *candidate*.
LEVEL_KINDS: tuple[str, ...] = (
    "prior_high", "prior_low", "prior_close",
    "round_big", "round_half", "round_quarter",
    "pivot", "pivot_r1", "pivot_s1", "pivot_r2", "pivot_s2",
    "swing_high", "swing_low",
    "sma_20", "sma_50", "sma_100", "sma_200",
    "oi_cluster",
)

#: coarse family, for reporting the measurement at both granularities.  Testing 18
#: kinds and testing 6 families are different multiple-comparison problems and the
#: honest report shows both.
KIND_GROUP: dict[str, str] = {
    "prior_high": "prior_session", "prior_low": "prior_session",
    "prior_close": "prior_session",
    "round_big": "round", "round_half": "round", "round_quarter": "round",
    "pivot": "pivot", "pivot_r1": "pivot", "pivot_s1": "pivot",
    "pivot_r2": "pivot", "pivot_s2": "pivot",
    "swing_high": "swing", "swing_low": "swing",
    "sma_20": "ma", "sma_50": "ma", "sma_100": "ma", "sma_200": "ma",
    "oi_cluster": "oi",
}

#: **DISPLAY PRIOR ONLY -- not a measurement, not a probability.**  A trader's ordering
#: of which anchors get drawn first and which wins a tie when two sit within a pip.
#: ``measure_reversal_stats`` is the only thing in this module entitled to an opinion
#: about efficacy, and if it disagrees with this table then this table is wrong.
STRENGTH_PRIOR: dict[str, float] = {
    "prior_high": 0.85, "prior_low": 0.85, "prior_close": 0.55,
    "round_big": 1.00, "round_half": 0.60, "round_quarter": 0.35,
    "pivot": 0.50, "pivot_r1": 0.45, "pivot_s1": 0.45,
    "pivot_r2": 0.30, "pivot_s2": 0.30,
    "swing_high": 0.70, "swing_low": 0.70,
    "sma_20": 0.30, "sma_50": 0.35, "sma_100": 0.30, "sma_200": 0.45,
    "oi_cluster": 0.65,
}


# --------------------------------------------------------------------------------------
# level constructors
# --------------------------------------------------------------------------------------


def round_levels(spot: float, pip: float, *, span_pips: float = 200.0,
                 quarter_span_pips: float = 100.0) -> list[tuple[float, str]]:
    """The big-figure / half-figure / quarter-figure hierarchy around ``spot``.

    A "big figure" is 100 pips (1.1700 on EURUSD, 148.00 on USDJPY), the half figure
    is 50 (1.1750), the quarter 25 (1.1725).  Each price is emitted **once**, at its
    highest rank: 1.1700 is a big figure and is not also reported as a half figure.
    That matters for the measurement -- double-counting a big figure inside the
    ``round_half`` sample would drag the half-figure statistic towards the big-figure
    one and manufacture an effect.
    """
    big, half, quarter = 100.0 * pip, 50.0 * pip, 25.0 * pip
    out: dict[float, str] = {}
    def _emit(step: float, span: float, kind: str) -> None:
        lo = math.floor((spot - span * pip) / step) * step
        hi = math.ceil((spot + span * pip) / step) * step
        n = int(round((hi - lo) / step))
        for i in range(n + 1):
            lvl = round(lo + i * step, 10)
            if abs(lvl - spot) <= span * pip + 1e-12 and lvl > 0:
                out.setdefault(lvl, kind)
    _emit(big, span_pips, "round_big")
    _emit(half, span_pips, "round_half")
    _emit(quarter, quarter_span_pips, "round_quarter")
    return sorted(out.items())


def pivot_levels(high: float, low: float, close: float) -> dict[str, float]:
    """Classic (floor-trader) pivots from the prior session's H/L/C."""
    p = (high + low + close) / 3.0
    r1, s1 = 2 * p - low, 2 * p - high
    r2, s2 = p + (high - low), p - (high - low)
    return {"pivot": p, "pivot_r1": r1, "pivot_s1": s1, "pivot_r2": r2, "pivot_s2": s2}


def swing_points(hist: pd.DataFrame, *, k: int = 3, lookback: int = 120
                 ) -> list[tuple[int, float, str]]:
    """Fractal swing highs/lows: a bar whose high (low) exceeds ``k`` bars either side.

    A swing needs ``k`` bars **after** it to be confirmed, so the newest point this can
    return is ``k`` bars old.  That lag is real and is why ``age_days`` is never zero
    for a swing; pretending otherwise would be look-ahead.
    """
    h = hist["high"].to_numpy(float)
    l = hist["low"].to_numpy(float)
    n = len(h)
    out: list[tuple[int, float, str]] = []
    lo_i = max(k, n - int(lookback))
    for i in range(lo_i, n - k):
        w_h, w_l = h[i - k:i + k + 1], l[i - k:i + k + 1]
        if h[i] >= w_h.max():
            out.append((i, float(h[i]), "swing_high"))
        if l[i] <= w_l.min():
            out.append((i, float(l[i]), "swing_low"))
    return out


def technical_levels(hist: pd.DataFrame, pair: str, asof, *,
                     kinds: Sequence[str] | None = None,
                     swing_k: int = 3, swing_lookback: int = 120,
                     round_span_pips: float = 200.0,
                     max_dist_pips: float | None = None,
                     min_dist_pips: float = 2.0) -> pd.DataFrame:
    """Candidate anchors as of ``asof``, using only bars at or before it.

    Columns (contract order first): ``level, kind, strength, age_days, source`` plus
    ``group, side, dist_pips, asof``.  ``side`` is ``+1`` for a level above the
    reference close and ``-1`` below -- the resting order that would sit there is a
    sell above and a buy below, and the measurement conditions on approaching from
    the correct side.

    ``strength`` is :data:`STRENGTH_PRIOR`, a display ordering.  Read
    :func:`measure_reversal_stats` before believing any of it.
    """
    spec = pair_spec(pair)
    pip = spec.pip
    ts = pd.Timestamp(asof)
    if ts.tzinfo is None and getattr(hist.index, "tz", None) is not None:
        ts = ts.tz_localize("UTC")
    ref = hist.loc[hist.index <= ts]
    if len(ref) < 2:
        return pd.DataFrame(columns=LEVEL_COLUMNS)
    want = set(kinds) if kinds else set(LEVEL_KINDS)
    last = ref.iloc[-1]
    last_dt = ref.index[-1]
    c = float(last["close"])
    rows: list[dict] = []

    def _add(level, kind, source, age_days):
        if kind not in want or not np.isfinite(level) or level <= 0:
            return
        rows.append({"level": float(level), "kind": kind,
                     "strength": float(STRENGTH_PRIOR.get(kind, 0.3)),
                     "age_days": float(age_days), "source": source,
                     "group": KIND_GROUP.get(kind, "other"),
                     "side": 1 if level > c else -1,
                     "dist_pips": abs(level - c) / pip,
                     "asof": last_dt})

    _add(float(last["high"]), "prior_high", "prior session", 0.0)
    _add(float(last["low"]), "prior_low", "prior session", 0.0)
    _add(float(last["close"]), "prior_close", "prior session", 0.0)

    for name, lvl in pivot_levels(float(last["high"]), float(last["low"]), c).items():
        _add(lvl, name, "classic pivot on prior H/L/C", 0.0)

    for lvl, kind in round_levels(c, pip, span_pips=round_span_pips):
        _add(lvl, kind, "round-number hierarchy", float("nan"))

    if want & {"swing_high", "swing_low"}:
        for i, lvl, kind in swing_points(ref, k=swing_k, lookback=swing_lookback):
            age = float((last_dt - ref.index[i]).days)
            _add(lvl, kind, f"fractal swing k={swing_k}", age)

    for w in (20, 50, 100, 200):
        kind = f"sma_{w}"
        if kind in want and len(ref) >= w:
            _add(float(ref["close"].iloc[-w:].mean()), kind, f"SMA({w}) of close", 0.0)

    df = pd.DataFrame(rows, columns=LEVEL_COLUMNS)
    if df.empty:
        return df
    df = df[df["dist_pips"] >= float(min_dist_pips)]
    if max_dist_pips is not None:
        df = df[df["dist_pips"] <= float(max_dist_pips)]
    return df.sort_values("dist_pips").reset_index(drop=True)


def oi_levels(oi: pd.DataFrame, spot: float, *, top: int = 8,
              pair: str | None = None, tol_pips: float | None = None,
              max_expiries: int | None = None) -> pd.DataFrame:
    """Strike / open-interest clusters -- **where listed gamma sits**, nothing more.

    This is the same discipline as :mod:`fxgamma.signals.gex`: open interest says how
    many contracts are outstanding at a strike, and it says **nothing** about who is
    long them.  These clusters are candidate *anchors* because listed hedging activity
    concentrates there, not because "dealers are short gamma" at them -- that claim is
    not in the data and is never made here.

    Strikes are expected in **pair (FORDOM) strike space** (what
    ``data.cme_options.to_pair_frame`` and the synthetic provider emit).  Adjacent
    strikes within ``tol_pips`` are merged into one OI-weighted cluster, because the
    exchange's strike grid is finer than any level a resting order should use.
    """
    cols = LEVEL_COLUMNS + ["oi", "n_strikes", "oi_share"]
    if oi is None or len(oi) == 0 or "strike" not in oi.columns:
        return pd.DataFrame(columns=cols)
    pip = pair_spec(pair).pip if pair else max(abs(float(spot)) * 1e-4, 1e-6)
    tol = float(tol_pips) * pip if tol_pips is not None else max(abs(float(spot)) * 2.5e-4, pip)
    d = oi[["strike", "oi"]].copy()
    d["strike"] = pd.to_numeric(d["strike"], errors="coerce")
    d["oi"] = pd.to_numeric(d["oi"], errors="coerce").fillna(0.0)
    if max_expiries is not None and "expiry" in oi.columns:
        keep = sorted(pd.Series(oi["expiry"]).dropna().unique())[:int(max_expiries)]
        d = d[oi["expiry"].isin(keep)]
    d = d[np.isfinite(d["strike"]) & (d["strike"] > 0) & (d["oi"] > 0)]
    if d.empty:
        return pd.DataFrame(columns=cols)
    agg = d.groupby("strike", as_index=False)["oi"].sum().sort_values("strike")
    ks = agg["strike"].to_numpy(float)
    ws = agg["oi"].to_numpy(float)
    clusters: list[tuple[float, float, int]] = []
    cur_k, cur_w, cur_n = [ks[0]], [ws[0]], 1
    for i in range(1, len(ks)):
        if ks[i] - cur_k[-1] <= tol:
            cur_k.append(ks[i]); cur_w.append(ws[i]); cur_n += 1
        else:
            w = float(np.sum(cur_w))
            clusters.append((float(np.dot(cur_k, cur_w) / w), w, cur_n))
            cur_k, cur_w, cur_n = [ks[i]], [ws[i]], 1
    w = float(np.sum(cur_w))
    clusters.append((float(np.dot(cur_k, cur_w) / w), w, cur_n))

    tot = sum(c[1] for c in clusters) or 1.0
    mx = max(c[1] for c in clusters) or 1.0
    rows = []
    for lvl, wt, n in clusters:
        rows.append({"level": lvl, "kind": "oi_cluster", "strength": float(wt / mx),
                     "age_days": 0.0, "source": "listed OI by strike (unsigned)",
                     "group": "oi", "side": 1 if lvl > spot else -1,
                     "dist_pips": abs(lvl - float(spot)) / pip, "asof": pd.NaT,
                     "oi": wt, "n_strikes": int(n), "oi_share": float(wt / tot)})
    out = pd.DataFrame(rows, columns=cols)
    return out.sort_values("oi", ascending=False).head(int(top)).reset_index(drop=True)


# --------------------------------------------------------------------------------------
# the panel: levels through history, forward-clean
# --------------------------------------------------------------------------------------


def level_panel(hist: pd.DataFrame, pair: str, *, kinds: Sequence[str] | None = None,
                start: int = 250, step: int = 1, max_dist_pips: float = 250.0,
                **kw) -> pd.DataFrame:
    """Rebuild :func:`technical_levels` at every ``step``-th bar from ``start`` on.

    ``max_dist_pips`` trims levels too far away to be a plausible overnight anchor;
    it also keeps the control matched over a sensible distance range rather than one
    dominated by a 200-day moving average four big figures away.
    """
    idx = hist.index
    frames = []
    for i in range(int(start), len(idx) - 1, int(step)):
        lv = technical_levels(hist.iloc[:i + 1], pair, idx[i], kinds=kinds,
                              max_dist_pips=max_dist_pips, **kw)
        if lv.empty:
            continue
        lv = lv.copy()
        lv["bar"] = i
        frames.append(lv)
    if not frames:
        return pd.DataFrame(columns=LEVEL_COLUMNS + ["bar"])
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------------------
# THE measurement
# --------------------------------------------------------------------------------------


def _fwd_stats(hist: pd.DataFrame, n_bars: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Forward max-high, min-low over bars ``i+1..i+n``, and close at ``i+n``."""
    h = hist["high"].to_numpy(float)
    l = hist["low"].to_numpy(float)
    c = hist["close"].to_numpy(float)
    n = len(h)
    mx = np.full(n, np.nan)
    mn = np.full(n, np.nan)
    cl = np.full(n, np.nan)
    for i in range(n - 1):
        j = min(i + 1 + n_bars, n)
        if j <= i + 1:
            continue
        mx[i] = h[i + 1:j].max()
        mn[i] = l[i + 1:j].min()
        cl[i] = c[j - 1]
    return mx, mn, cl


def _atr(hist: pd.DataFrame, n: int = 14) -> np.ndarray:
    """Average true range at each bar, using **that bar and earlier only**.

    Used to match the control on distance measured *in units of the current
    volatility state*, not in raw pips -- see :func:`measure_reversal_stats`.
    """
    h = hist["high"].to_numpy(float); l = hist["low"].to_numpy(float)
    c = hist["close"].to_numpy(float)
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    s = pd.Series(tr).rolling(int(n), min_periods=2).mean().to_numpy(float)
    return np.where(np.isfinite(s) & (s > 0), s, np.nanmedian(tr))


def _agg(touch: np.ndarray, rev: np.ndarray, exc: np.ndarray) -> tuple[float, float, float]:
    t = float(np.mean(touch)) if touch.size else float("nan")
    m = touch.astype(bool)
    r = float(np.mean(rev[m])) if m.any() else float("nan")
    e = float(np.mean(exc[m])) if m.any() else float("nan")
    return t, r, e


def _cohens_h(p1: float, p2: float) -> float:
    if not (np.isfinite(p1) and np.isfinite(p2)):
        return float("nan")
    p1 = min(max(p1, 0.0), 1.0); p2 = min(max(p2, 0.0), 1.0)
    return float(2 * math.asin(math.sqrt(p1)) - 2 * math.asin(math.sqrt(p2)))


def _holm(p: np.ndarray) -> np.ndarray:
    m = p.size
    order = np.argsort(p)
    adj = np.empty(m)
    running = 0.0
    for rank, i in enumerate(order):
        val = (m - rank) * p[i]
        running = max(running, val)
        adj[i] = min(running, 1.0)
    return adj


def _bh(p: np.ndarray) -> np.ndarray:
    m = p.size
    order = np.argsort(p)
    adj = np.empty(m)
    running = 1.0
    for rank in range(m - 1, -1, -1):
        i = order[rank]
        val = m * p[i] / (rank + 1)
        running = min(running, val)
        adj[i] = min(running, 1.0)
    return adj


def measure_reversal_stats(hist: pd.DataFrame, levels: pd.DataFrame, *,
                           horizon_h: int = 12, pair: str | None = None,
                           control: str = "permuted", n_control: int = 20,
                           jitter: float = 0.25, seed: int = 20260909,
                           block: int = 21, n_boot: int = 400,
                           min_obs: int = 100) -> pd.DataFrame:
    """**The important function.** Per level kind, measured against a matched control.

    Parameters
    ----------
    hist : daily OHLC.  Daily bars are the honest resolution of this measurement and
        its main limitation -- see *Caveats*.
    levels : either a **panel** (the output of :func:`level_panel`, one row per
        level per ``asof`` bar) or a single-date frame, in which case ``pair`` must be
        given and the panel is rebuilt for you.
    horizon_h : forward horizon in hours, converted to ``ceil(horizon_h/24)`` daily
        bars.  The default 12h is one bar: the overnight window this feature is for.
    control : how the placebo level is placed, at a matched distance from spot.

        ``"permuted"`` (default, **scale-matched**) permutes the distance measured in
        ATRs within the kind, then maps it back through *today's* ATR.  Matching on
        raw pips is not enough and this is not a detail: several kinds
        (``pivot_r2``/``pivot_s2``, and anything proportional to the prior range) have
        a distance that is itself proportional to current volatility, so a raw-pip
        control lands too close on quiet days and too far on busy ones.  Conditional
        on a touch that makes the control overshoot more and revert less, and it
        manufactures a **positive** effect for the real level out of nothing.  We
        measured that artefact -- see ``docs/10_forecast_evaluation.md`` -- and
        ``"permuted_raw"`` is retained so it can be reproduced.

        ``"permuted_raw"`` is the naive distance-matched control described above,
        kept **only** to demonstrate the bias.  Do not report from it.

        ``"jitter"`` displaces the real level by a uniform ``+-jitter`` fraction of its
        own distance.  It is local, so it is automatically scale-matched, and it asks
        the sharper question: does *this exact price* matter, or just its
        neighbourhood?  It is the natural second opinion and the two should agree.
    n_control : control replicates, averaged.  Reduces the control's Monte-Carlo noise
        without touching the real sample.
    block, n_boot : moving-block bootstrap over **dates** for the confidence interval
        on ``d_reversal``.  Observations are not independent (the same prior high is a
        candidate on consecutive days; overlapping horizons share bars), so an
        ordinary two-proportion z-test would overstate significance by roughly
        ``sqrt(block)``.  The block bootstrap is the correction.

    Returns
    -------
    One row per kind, with the real and control statistics side by side:
    ``touch_rate`` / ``ctrl_touch_rate`` (a sanity check that the matching worked --
    they should be close), ``reversal_rate`` / ``ctrl_reversal_rate`` **conditional on
    a touch**, ``mean_excursion_pips`` beyond the level, the differences, Cohen's
    ``h`` and ``d`` effect sizes, a bootstrap CI, and raw / Holm / BH p-values.
    ``beats_control`` is ``True`` only when the BH-adjusted p is below 0.05 **and**
    the sample clears ``min_obs`` touches.

    Caveats, which belong next to the number
    ----------------------------------------
    * **Daily bars cannot see intraday sequencing.**  "Touched then reversed" is
      inferred from the bar's range and the close at the horizon.  A day that traded
      through the level, came back, and went through again reads as a reversal.  The
      real answer needs intraday data; this is the best a daily OHLC feed can do and
      it is stated rather than hidden.
    * **Reversal is defined against the approach side**: for a level above the
      reference close, "reversed" means the close at the horizon is back below it.
      That is a weak definition of a reversal, deliberately -- a stronger one (a close
      back beyond some buffer) is available via the returned ``reversal_rate_buf``.
    * **Multiple testing.**  See the module docstring.  Read the BH column.
    """
    if levels is None or len(levels) == 0:
        return pd.DataFrame()
    lv = levels
    if "bar" not in lv.columns:
        if pair is None:
            raise ValueError("levels is not a panel (no 'bar' column) and pair= was not "
                             "given, so the panel cannot be rebuilt")
        lv = level_panel(hist, pair, kinds=sorted(set(lv["kind"])))
        if lv.empty:
            return pd.DataFrame()
    if pair is not None:
        pip = pair_spec(pair).pip
    else:
        pip = float(lv["level"].iloc[0]) * 1e-4

    n_bars = max(1, int(math.ceil(float(horizon_h) / 24.0)))
    mx, mn, cl = _fwd_stats(hist, n_bars)
    close = hist["close"].to_numpy(float)

    bar = lv["bar"].to_numpy(int)
    lvl = lv["level"].to_numpy(float)
    kind = lv["kind"].to_numpy(object)
    ref = close[bar]
    side = np.where(lvl > ref, 1, -1)
    dist = np.abs(lvl - ref)
    ok = np.isfinite(mx[bar]) & np.isfinite(mn[bar]) & np.isfinite(cl[bar]) & (dist > 0)
    bar, lvl, kind, ref, side, dist = (a[ok] for a in (bar, lvl, kind, ref, side, dist))

    fmax, fmin, fcl = mx[bar], mn[bar], cl[bar]
    buf = 5.0 * pip

    def _eval(L: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        up = side > 0
        touch = np.where(up, fmax >= L, fmin <= L)
        exc = np.where(up, fmax - L, L - fmin) / pip
        exc = np.where(touch, np.maximum(exc, 0.0), np.nan)
        rev = np.where(up, fcl < L, fcl > L)
        revb = np.where(up, fcl < L - buf, fcl > L + buf)
        return touch.astype(float), rev.astype(float), exc, revb.astype(float)

    t_r, rev_r, exc_r, revb_r = _eval(lvl)

    rng = np.random.default_rng(int(seed))
    kinds = sorted(set(kind.tolist()))
    # Scale for the scale-matched control: ATR at the asof bar, known at that bar.
    scale = _atr(hist, 14)[bar]
    scale = np.where(np.isfinite(scale) & (scale > 0), scale, np.nanmedian(scale))
    dist_u = dist / scale
    ctrl_levels = np.empty((int(n_control), lvl.size))
    for c_i in range(int(n_control)):
        d_c = dist.copy()
        if control == "permuted":
            # permute the distance measured in ATRs, then map back through TODAY's ATR
            for k in kinds:
                m = kind == k
                d_c[m] = rng.permutation(dist_u[m]) * scale[m]
        elif control == "permuted_raw":
            for k in kinds:
                m = kind == k
                d_c[m] = rng.permutation(dist[m])
        elif control == "jitter":
            u = rng.uniform(-float(jitter), float(jitter), dist.size)
            u = np.where(np.abs(u) < 0.05 * float(jitter), np.sign(u + 1e-12) * 0.05 * float(jitter), u)
            d_c = dist * (1.0 + u)
        else:
            raise ValueError("control must be 'permuted', 'permuted_raw' or 'jitter'")
        ctrl_levels[c_i] = ref + side * d_c

    # Accumulate the control as **counts of touch events**, so that the control mean is
    # pooled over touches exactly the way the real mean is.  Averaging per observation
    # instead (each observation contributing its own across-replicate mean) silently
    # reweights far levels up, because a far level touches rarely but would still count
    # once -- and that alone manufactured a ~10pp spurious "effect" in an earlier cut of
    # this code.  Same estimator on both sides or the comparison means nothing.
    c_touch = np.zeros_like(t_r)          # number of touching replicates, per observation
    c_rev = np.zeros_like(rev_r)          # of which, reversed
    c_revb = np.zeros_like(revb_r)
    c_exc = np.zeros_like(exc_r)          # summed excursion over touching replicates
    dist_c_mean = np.zeros_like(dist)
    for c_i in range(int(n_control)):
        tt, rr, ee, rb = _eval(ctrl_levels[c_i])
        m = tt > 0
        c_touch += tt
        c_rev += np.where(m, rr, 0.0)
        c_revb += np.where(m, rb, 0.0)
        c_exc += np.where(m, np.nan_to_num(ee), 0.0)
        dist_c_mean += np.abs(ctrl_levels[c_i] - ref)
    dist_c_mean /= n_control

    # ---- aggregate per kind, with a block bootstrap over dates -----------------------
    ubars = np.unique(bar)
    n_blocks = max(int(math.ceil(ubars.size / float(block))), 1)
    rows = []
    for k in kinds:
        m = kind == k
        tr, rr, er = _agg(t_r[m], rev_r[m], exc_r[m])
        trb = float(np.nanmean(np.where(t_r[m] > 0, revb_r[m], np.nan))) if m.any() else float("nan")
        ct = float(np.sum(c_touch[m]))
        tc = ct / (m.sum() * float(n_control)) if m.any() else float("nan")
        rc = float(np.sum(c_rev[m]) / ct) if ct > 0 else float("nan")
        rcb = float(np.sum(c_revb[m]) / ct) if ct > 0 else float("nan")
        ec = float(np.sum(c_exc[m]) / ct) if ct > 0 else float("nan")
        n_touch = int(np.nansum(t_r[m]))

        # bootstrap the difference in reversal rate, resampling contiguous date blocks
        bars_k = bar[m]
        diffs = np.empty(int(n_boot))
        tr_k, rev_k = t_r[m], rev_r[m]
        ct_k, cr_k = c_touch[m], c_rev[m]
        starts_pool = ubars[: max(ubars.size - block + 1, 1)]
        bar_pos = {b: i for i, b in enumerate(ubars)}
        pos = np.array([bar_pos[b] for b in bars_k])
        for b_i in range(int(n_boot)):
            picks = rng.integers(0, starts_pool.size, n_blocks)
            sel = np.concatenate([np.arange(p, min(p + block, ubars.size)) for p in picks])
            take = np.isin(pos, sel)
            if not take.any():
                diffs[b_i] = np.nan; continue
            a_t = tr_k[take] > 0
            ra = float(np.mean(rev_k[take][a_t])) if a_t.any() else np.nan
            ctb = float(np.sum(ct_k[take]))
            rb_ = float(np.sum(cr_k[take]) / ctb) if ctb > 0 else np.nan
            diffs[b_i] = ra - rb_
        d_rev = rr - rc
        sd = float(np.nanstd(diffs, ddof=1))
        z = d_rev / sd if sd > 0 else float("nan")
        from math import erfc
        p = float(erfc(abs(z) / math.sqrt(2.0))) if np.isfinite(z) else float("nan")
        rows.append({
            "kind": k, "group": KIND_GROUP.get(k, "other"),
            "n_obs": int(m.sum()), "n_dates": int(np.unique(bars_k).size),
            "n_touch": n_touch,
            "mean_dist_pips": float(np.mean(dist[m]) / pip),
            "ctrl_mean_dist_pips": float(np.mean(dist_c_mean[m]) / pip),
            "touch_rate": tr, "ctrl_touch_rate": tc,
            "reversal_rate": rr, "ctrl_reversal_rate": rc, "d_reversal": d_rev,
            "d_reversal_lo": float(np.nanpercentile(diffs, 2.5)),
            "d_reversal_hi": float(np.nanpercentile(diffs, 97.5)),
            "cohens_h": _cohens_h(rr, rc),
            "reversal_rate_buf": trb, "ctrl_reversal_rate_buf": rcb,
            "mean_excursion_pips": er, "ctrl_mean_excursion_pips": ec,
            "d_excursion_pips": er - ec,
            "p_reversal": p,
            "boot_sd": sd,
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    pv = out["p_reversal"].to_numpy(float)
    fin = np.isfinite(pv)
    holm = np.full(pv.size, np.nan); bh = np.full(pv.size, np.nan)
    if fin.any():
        holm[fin] = _holm(pv[fin]); bh[fin] = _bh(pv[fin])
    out["p_reversal_holm"] = holm
    out["p_reversal_bh"] = bh
    out["n_kinds_tested"] = int(out.shape[0])
    out["beats_control"] = ((out["d_reversal"] > 0) & (out["p_reversal_bh"] < 0.05)
                            & (out["n_touch"] >= int(min_obs)))
    out["control"] = control
    out["horizon_bars"] = n_bars
    return out.sort_values("p_reversal_bh").reset_index(drop=True)


# --------------------------------------------------------------------------------------
# consumption helper for the ladder builder
# --------------------------------------------------------------------------------------


def nearest_level(price: float, levels: pd.DataFrame, *, pair: str,
                  max_pips: float = 10.0, kinds: Sequence[str] | None = None
                  ) -> dict | None:
    """Nearest candidate anchor to ``price`` within ``max_pips``, or ``None``.

    This is the *mechanism* a ladder would use to snap a rung.  It carries no opinion
    about whether snapping is a good idea: pass ``kinds=`` restricted to the kinds
    that ``measure_reversal_stats`` says beat their control on **your** data, and
    leave snapping off entirely if none of them do.  That is the PM's acceptance
    criterion, not a preference.
    """
    if levels is None or len(levels) == 0:
        return None
    pip = pair_spec(pair).pip
    d = levels if kinds is None else levels[levels["kind"].isin(list(kinds))]
    if d.empty:
        return None
    gap = (d["level"].to_numpy(float) - float(price)) / pip
    i = int(np.argmin(np.abs(gap)))
    if abs(gap[i]) > float(max_pips):
        return None
    row = d.iloc[i]
    return {"level": float(row["level"]), "kind": str(row["kind"]),
            "strength": float(row["strength"]),
            "dist_pips": float(gap[i]), "source": str(row.get("source", ""))}
