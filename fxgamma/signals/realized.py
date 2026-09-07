"""Realized-volatility estimators, with each one's bias and drift assumption stated.

Every estimator here takes an OHLC frame (``open, high, low, close``, DatetimeIndex,
one row per trading day -- the canonical shape from ``fxgamma.data``) and returns an
**annualised decimal** vol (0.085 = 8.5%).

Annualisation basis
-------------------
``sqrt(252)`` -- spot moves on trading days.  This is deliberately *not* the
``sqrt(365)`` used by the daily-breakeven identity, and conflating the two is trader
review W-7: the ``365`` basis is the economics of the theta bill, the ``252`` basis is
how far spot can actually travel.  An RV annualised on 252 is directly comparable to
an ACT/365 implied vol, because the implied is a *rate* per unit of calendar time and
the realized is a *rate* per unit of trading time, both scaled to a year.

Bias and drift assumptions (this is the part that matters)
----------------------------------------------------------
============  ==========  =====================================================
estimator     efficiency  assumption / bias
============  ==========  =====================================================
close_close   1.0 (ref)   Unbiased under zero-drift GBM. Uses 2 points a day, so
                          it is the noisiest. ``demean=False`` (the default)
                          assumes **zero drift**; with ``demean=True`` it is the
                          sample variance, which removes drift at the cost of
                          1 degree of freedom and a small downward bias in
                          short windows. It is the only estimator here that
                          captures overnight gaps *and* nothing else.
parkinson     ~5x         Range-based, zero drift assumed. **Biased low**: a
                          discretely-observed high/low understates the true
                          continuous extremes (~-3% for hourly ticks, worse for
                          less liquid records), and it ignores the overnight gap
                          entirely, so it under-reads on gappy markets.
garman_klass  ~7.4x       Uses O, H, L, C. Zero drift assumed. Most efficient of
                          the single-day estimators under its assumptions, and
                          the most fragile: it assumes a continuous, gap-free
                          session, so **jumps and overnight gaps bias it low**
                          while a "drifty" day biases it high.
rogers_satch  ~6x         **Drift-independent by construction** -- the only one
                          here that is unbiased when the price has a trend, which
                          is exactly when Parkinson and Garman-Klass break. Still
                          ignores overnight gaps, and it is noisier than GK when
                          the drift really is zero.
yang_zhang    ~7-14x      Overnight variance + k * open-to-close + (1-k) *
                          Rogers-Satchell, ``k = 0.34/(1.34 + (n+1)/(n-1))``.
                          **Handles both drift and overnight gaps**; minimum
                          variance in this set. Needs ``n > 2`` and is sensitive
                          to bad opens -- a source that stamps ``open == previous
                          close`` (many free feeds do) silently collapses its
                          overnight term to zero and it degenerates towards
                          Rogers-Satchell.
============  ==========  =====================================================

FX-specific caveat: "overnight" is a fiction in a 24-hour market.  The gap is an
artefact of where the data source cuts the day (Stooq/Yahoo cut at 00:00 UTC, the
market rolls at 17:00 New York).  Yang-Zhang's overnight term is therefore measuring
the source's cut, not a real close-to-open gap -- which is a reason to quote
close-to-close and Yang-Zhang side by side (trader 3b) rather than to trust one.
"""
from __future__ import annotations

import math
from typing import Callable

import numpy as np
import pandas as pd

__all__ = ["ANNUAL", "ESTIMATORS", "realized_vol", "rolling_vol", "all_estimators",
           "close_to_close", "parkinson", "garman_klass", "rogers_satchell",
           "yang_zhang", "log_returns"]

#: trading days per year -- the distance/probability basis (W-7)
ANNUAL = 252.0
_LN2 = math.log(2.0)


def _ohlc(df: pd.DataFrame) -> pd.DataFrame:
    need = ("open", "high", "low", "close")
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise KeyError(f"realized vol needs OHLC columns, missing {missing}; "
                       f"have {list(df.columns)}")
    out = df[list(need)].astype(float)
    return out[np.isfinite(out).all(axis=1) & (out > 0).all(axis=1)]


def log_returns(close: pd.Series | np.ndarray) -> np.ndarray:
    c = np.asarray(close, float)
    return np.diff(np.log(c))


def close_to_close(df: pd.DataFrame, *, annual: float = ANNUAL,
                   demean: bool = False) -> float:
    """Close-to-close. Zero drift unless ``demean=True``. Efficiency 1 (the yardstick)."""
    d = _ohlc(df)
    r = log_returns(d["close"])
    if r.size < 2:
        return float("nan")
    if demean:
        return float(np.sqrt(annual * np.sum((r - r.mean()) ** 2) / (r.size - 1)))
    return float(np.sqrt(annual * np.sum(r ** 2) / r.size))


def parkinson(df: pd.DataFrame, *, annual: float = ANNUAL) -> float:
    """Parkinson (1980) high-low range. Zero drift; biased low; ignores gaps."""
    d = _ohlc(df)
    if len(d) < 1:
        return float("nan")
    hl = np.log(d["high"].to_numpy() / d["low"].to_numpy())
    return float(np.sqrt(annual * np.mean(hl ** 2) / (4.0 * _LN2)))


def garman_klass(df: pd.DataFrame, *, annual: float = ANNUAL) -> float:
    """Garman-Klass (1980). Zero drift, gap-free session assumed; biased low on jumps."""
    d = _ohlc(df)
    if len(d) < 1:
        return float("nan")
    hl = np.log(d["high"].to_numpy() / d["low"].to_numpy())
    co = np.log(d["close"].to_numpy() / d["open"].to_numpy())
    v = 0.5 * hl ** 2 - (2.0 * _LN2 - 1.0) * co ** 2
    return float(np.sqrt(annual * max(np.mean(v), 0.0)))


def rogers_satchell(df: pd.DataFrame, *, annual: float = ANNUAL) -> float:
    """Rogers-Satchell (1991). **Drift-independent**; still ignores overnight gaps."""
    d = _ohlc(df)
    if len(d) < 1:
        return float("nan")
    h, l, c, o = (np.log(d[k].to_numpy()) for k in ("high", "low", "close", "open"))
    v = (h - c) * (h - o) + (l - c) * (l - o)
    return float(np.sqrt(annual * max(np.mean(v), 0.0)))


def yang_zhang(df: pd.DataFrame, *, annual: float = ANNUAL) -> float:
    """Yang-Zhang (2000). Handles drift **and** overnight gaps; minimum variance here.

    ``sigma^2 = sigma_o^2 + k sigma_c^2 + (1-k) sigma_rs^2`` with
    ``k = 0.34 / (1.34 + (n+1)/(n-1))``.  Degenerates towards Rogers-Satchell when
    the feed stamps ``open == previous close`` (see the module docstring).
    """
    d = _ohlc(df)
    n = len(d)
    if n < 3:
        return float("nan")
    o, h, l, c = (d[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    ro = np.log(o[1:] / c[:-1])                    # overnight
    rc = np.log(c[1:] / o[1:])                     # open-to-close
    hh, ll, cc, oo = (np.log(x[1:]) for x in (h, l, c, o))
    rs = (hh - cc) * (hh - oo) + (ll - cc) * (ll - oo)
    m = ro.size
    if m < 2:
        return float("nan")
    var_o = np.sum((ro - ro.mean()) ** 2) / (m - 1)
    var_c = np.sum((rc - rc.mean()) ** 2) / (m - 1)
    var_rs = np.mean(rs)
    k = 0.34 / (1.34 + (m + 1.0) / (m - 1.0))
    return float(np.sqrt(annual * max(var_o + k * var_c + (1.0 - k) * var_rs, 0.0)))


ESTIMATORS: dict[str, Callable[..., float]] = {
    "close_to_close": close_to_close,
    "parkinson": parkinson,
    "garman_klass": garman_klass,
    "rogers_satchell": rogers_satchell,
    "yang_zhang": yang_zhang,
}

#: what each estimator assumes, surfaced next to the number in the UI (REQ-008)
ASSUMPTIONS = {
    "close_to_close": "zero drift (unless demeaned); captures gaps; noisiest",
    "parkinson": "zero drift; ignores gaps; biased low (discrete extremes)",
    "garman_klass": "zero drift; gap-free session; biased low on jumps",
    "rogers_satchell": "drift-independent; ignores gaps",
    "yang_zhang": "handles drift and gaps; needs a real open (see docstring)",
}


def realized_vol(df: pd.DataFrame, method: str = "close_to_close",
                 window: int | None = None, *, annual: float = ANNUAL, **kw) -> float:
    """One estimator over the trailing ``window`` rows (all rows if ``None``)."""
    try:
        fn = ESTIMATORS[method]
    except KeyError as exc:
        raise KeyError(f"unknown estimator {method!r}; have {sorted(ESTIMATORS)}") from exc
    d = df if window is None else df.tail(int(window) + 1)
    return float(fn(d, annual=annual, **kw))


def rolling_vol(df: pd.DataFrame, window: int = 21, method: str = "close_to_close",
                *, annual: float = ANNUAL) -> pd.Series:
    """Rolling realized vol as a series (the input to cones and z-scores).

    Windows overlap, so successive observations are **not independent**: an
    ``n``-day rolling series of length ``N`` carries roughly ``N/n`` independent
    observations.  ``cones.effective_n`` and ``richness.zscore`` apply that
    correction; anything that does not will inflate a z-score by about ``sqrt(n)``
    (trader W-8).
    """
    d = _ohlc(df)
    vals = []
    idx = []
    for i in range(len(d)):
        if i + 1 < window + 1:
            continue
        vals.append(realized_vol(d.iloc[i - window: i + 1], method, annual=annual))
        idx.append(d.index[i])
    return pd.Series(vals, index=pd.Index(idx, name=d.index.name), name=f"rv_{method}_{window}")


def all_estimators(df: pd.DataFrame, window: int = 21, *,
                   annual: float = ANNUAL) -> pd.DataFrame:
    """Every estimator over the same window, with its assumption spelled out.

    The trader's 3b note: keep at least three available, but default the morning
    screen to close-to-close plus Yang-Zhang and put the rest one click away.
    """
    rows = []
    for name in ESTIMATORS:
        rows.append({"estimator": name, "window": window,
                     "vol": realized_vol(df, name, window, annual=annual),
                     "annualisation": f"sqrt({annual:g})",
                     "assumption": ASSUMPTIONS[name],
                     "front_screen": name in ("close_to_close", "yang_zhang")})
    return pd.DataFrame(rows)
