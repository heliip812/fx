"""Realized-vol estimators and richness maths used by the Market Monitor and Surface pages.

`fxgamma/signals/` (quant-owned, contract section 1) will supersede this; until it lands the
Monitor needs numbers, so the five estimators of REQ-008 are implemented here on the
provider's OHLC.  Two things the review insists on are built in:

* **one annualisation basis, stated** - all estimators use sqrt(252) on business-day bars,
  and the basis is printed on every chart that shows them (REQ-008);
* **matched windows** - W-8: implied is compared with the realized vol over the *actual
  calendar days to expiry*, not a nominal tenor label, and the panel says which.

Missing OHLC degrades to close-to-close with a reason, never a crash.
"""
from __future__ import annotations

import math
import numpy as np
import pandas as pd

BASIS = 252
ESTIMATORS = ("close_to_close", "parkinson", "garman_klass", "rogers_satchell",
              "yang_zhang")
#: the two the trader wants on the morning screen (review section 3b); the rest one click away
DEFAULT_ESTIMATORS = ("close_to_close", "yang_zhang")

ESTIMATOR_FORMULA = {
    "close_to_close": "sqrt(252/n * sum(ln(C_t/C_{t-1})^2))",
    "parkinson": "sqrt(252/(4 ln2 n) * sum(ln(H/L)^2))",
    "garman_klass": "sqrt(252/n * sum(0.5 ln(H/L)^2 - (2ln2-1) ln(C/O)^2))",
    "rogers_satchell": "sqrt(252/n * sum(ln(H/C)ln(H/O) + ln(L/C)ln(L/O)))",
    "yang_zhang": "sigma_o^2 + k sigma_c^2 + (1-k) sigma_rs^2, k = 0.34/(1.34+(n+1)/(n-1))",
}


def _ok(df: pd.DataFrame) -> bool:
    return isinstance(df, pd.DataFrame) and not df.empty and "close" in df


def has_ohlc(df: pd.DataFrame) -> bool:
    if not _ok(df) or not {"open", "high", "low"} <= set(df.columns):
        return False
    h, l = df["high"].to_numpy(float), df["low"].to_numpy(float)
    return bool(np.nanmax(h - l) > 0)


def realized_vol(df: pd.DataFrame, window: int = 21,
                 estimator: str = "close_to_close") -> float | None:
    """Annualised realized vol as a **decimal**, or ``None`` when the data cannot support it."""
    if not _ok(df) or len(df) < window + 1:
        return None
    d = df.tail(window + 1)
    c = d["close"].to_numpy(float)
    if estimator != "close_to_close" and not has_ohlc(d):
        estimator = "close_to_close"
    with np.errstate(divide="ignore", invalid="ignore"):
        if estimator == "close_to_close":
            r = np.diff(np.log(c))
            v = float(np.nanmean(r ** 2) * BASIS)
        else:
            o = d["open"].to_numpy(float)
            h = d["high"].to_numpy(float)
            lo = d["low"].to_numpy(float)
            if estimator == "parkinson":
                v = float(np.nanmean(np.log(h / lo) ** 2) / (4 * math.log(2)) * BASIS)
            elif estimator == "garman_klass":
                v = float(np.nanmean(0.5 * np.log(h / lo) ** 2
                                     - (2 * math.log(2) - 1) * np.log(c / o) ** 2) * BASIS)
            elif estimator == "rogers_satchell":
                v = float(np.nanmean(np.log(h / c) * np.log(h / o)
                                     + np.log(lo / c) * np.log(lo / o)) * BASIS)
            elif estimator == "yang_zhang":
                n = len(d) - 1
                o_, c_, h_, l_ = o[1:], c[1:], h[1:], lo[1:]
                cprev = c[:-1]
                ro = np.log(o_ / cprev)
                rc = np.log(c_ / o_)
                rs = np.log(h_ / c_) * np.log(h_ / o_) + np.log(l_ / c_) * np.log(l_ / o_)
                k = 0.34 / (1.34 + (n + 1) / max(n - 1, 1))
                v = float((np.nanvar(ro, ddof=1) + k * np.nanvar(rc, ddof=1)
                           + (1 - k) * np.nanmean(rs)) * BASIS)
            else:
                raise ValueError(f"unknown estimator {estimator!r}")
    if not math.isfinite(v) or v < 0:
        return None
    return math.sqrt(v)


def rv_series(df: pd.DataFrame, window: int = 21,
              estimator: str = "close_to_close") -> pd.Series:
    """Rolling RV through time (decimal), for cones and z-scores."""
    if not _ok(df) or len(df) < window + 2:
        return pd.Series(dtype=float)
    if estimator == "close_to_close" or not has_ohlc(df):
        r = np.log(df["close"].astype(float)).diff()
        return (r.rolling(window).std(ddof=1) * math.sqrt(BASIS)).dropna()
    out = {}
    idx = df.index
    for i in range(window, len(df)):
        out[idx[i]] = realized_vol(df.iloc[i - window: i + 1], window, estimator)
    return pd.Series(out, dtype=float).dropna()


def zscore(series: pd.Series, value: float | None, lookback: int = 252
           ) -> tuple[float | None, int]:
    """(z, effective n).  W-8: overlapping windows inflate z, so n is always reported."""
    if value is None or series is None or len(series) < 30:
        return None, 0
    s = series.tail(lookback).dropna()
    if len(s) < 30 or not float(s.std(ddof=1)):
        return None, len(s)
    return float((value - s.mean()) / s.std(ddof=1)), int(len(s))


def percentile(series: pd.Series, value: float | None) -> float | None:
    if value is None or series is None or series.empty:
        return None
    return float((series.dropna() < value).mean() * 100.0)


def returns(df: pd.DataFrame, days: int) -> float | None:
    """Simple return over ``days`` bars, in percent."""
    if not _ok(df) or len(df) <= days:
        return None
    c = df["close"].astype(float)
    prev = float(c.iloc[-1 - days])
    if not prev:
        return None
    return (float(c.iloc[-1]) / prev - 1.0) * 100.0


def cone(df: pd.DataFrame, horizons=(5, 10, 21, 42, 63, 126),
         estimator: str = "close_to_close") -> pd.DataFrame:
    """RV percentile cone by horizon, with the sample size per horizon (REQ-018)."""
    rows = []
    for h in horizons:
        s = rv_series(df, h, estimator)
        if s.empty:
            rows.append({"horizon": h, "n": 0})
            continue
        rows.append({
            "horizon": h, "n": int(len(s)),
            "p5": float(s.quantile(0.05)), "p25": float(s.quantile(0.25)),
            "p50": float(s.quantile(0.50)), "p75": float(s.quantile(0.75)),
            "p95": float(s.quantile(0.95)), "current": float(s.iloc[-1]),
        })
    return pd.DataFrame(rows)


def matched_window_days(expiry_days: int) -> int:
    """W-8: an option with N **calendar** days to expiry is compared with the trailing
    N calendar days of returns (about N*252/365 business bars)."""
    return max(int(round(expiry_days * BASIS / 365.0)), 5)
