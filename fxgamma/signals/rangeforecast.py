"""Overnight **range** forecasting: HAR-RV, an implied blend, session variance time.

What this module does and does not do
-------------------------------------
It forecasts **how far** spot travels over a window.  It does **not** forecast
**which way**, and nothing here may be used to build a directional signal
(PM steer, ``docs/08_overnight_gamma.md`` §2).  That is not modesty, it is the
empirical position: daily FX returns are close to unforecastable, while daily
realized *variance* has an autocorrelation of ~0.5-0.7 at lag 1 and a slowly
decaying, near-hyperbolic ACF.  The forecastable object is the range, and the
range is also the object that pays a gamma book.

Three ingredients, each measured rather than asserted
-----------------------------------------------------
1. **HAR-RV** (Corsi 2009).  ``log v_{t+1} = c + b_d log v_t^{(1)} + b_w log v_t^{(5)}
   + b_m log v_t^{(22)}``.  Three regressors, no latent state, no optimiser -- it is a
   linear regression, which is precisely why it is trustworthy.  The daily variance
   proxy comes from :mod:`fxgamma.signals.realized`; a one-bar **range** estimator
   (Rogers-Satchell by default) is ~6x more efficient than that day's squared return,
   and the whole reason HAR works on daily OHLC is that you can see the range.
2. **Implied vol**, which is a genuinely forward-looking input: it prices the events
   the calendar knows about and the ones only the market knows about.  It is blended,
   not trusted -- see :func:`fit_blend_weights` and the honest note on
   :data:`DEFAULT_BLEND` about what our data can and cannot tell us here.
3. **Session variance time.**  The London-close-to-open window is 14/24 of the clock
   and roughly **a third** of the day's variance.  Using clock time overprices every
   rung by ~30% in sigma.  :data:`SESSION_VAR_PROFILE` carries the hourly weights and
   :func:`window_var_fraction` integrates them.

Bases (trader W-7, and it is easy to get wrong)
------------------------------------------------
Everything in this module that is a *distance* or a *probability* uses
``sqrt(252)``, matching :mod:`fxgamma.signals.realized`.  The ``sqrt(365)`` basis
belongs to the theta bill in :mod:`fxgamma.signals.richness` and never appears here.
A forecast for "one overnight window" is ``sigma_annual * sqrt(var_fraction / 252)``:
the window is a fraction of one **trading** day's variance, not of a calendar day's.

Loss functions
--------------
Vol forecasts are **not** evaluated with RMSE alone.  RMSE on variance is dominated
by the largest few observations and is not robust to the fact that the "actual" is a
noisy proxy for the latent variance.  :func:`qlike` (Patton 2011) is the headline
loss; it is robust to proxy noise in the sense that its ranking of forecasts is
unchanged in expectation when the proxy is unbiased.  Out-of-sample R^2 is reported
against a **named benchmark** (yesterday's RV; implied alone), never against zero.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from ..conventions import pair_spec
from .realized import ANNUAL, ESTIMATORS, log_returns

__all__ = [
    "RangeForecast", "har_rv", "overnight_range_forecast",
    "rv_daily", "har_design", "har_walk_forward", "har_fit",
    "qlike", "mse_var", "r2_oos", "mincer_zarnowitz", "diebold_mariano",
    "evaluate_forecasts", "fit_blend_weights", "blend_sigma",
    "SESSION_VAR_PROFILE", "window_var_fraction", "session_hours",
    "EVENT_SIGMA", "event_variance_add", "calibrate_event_uplift",
    "touch_probability", "expected_max_excursion", "DEFAULT_BLEND",
    "ONE_BAR_ESTIMATORS", "DEFAULT_RV_METHOD", "proxy_scale", "simulate_rv",
    "coverage_report",
]

# --------------------------------------------------------------------------------------
# Frozen output type (docs/08_overnight_gamma.md §3)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RangeForecast:
    """The forecast the ladder builder consumes.

    ``sigma_window``
        stdev of the **log** move over the window (decimal, not annualised).
    ``exp_abs_move_pips``
        ``E|S_T - S_0|`` in pips: ``S * sigma * sqrt(2/pi)`` for a driftless
        normal log move.  This is the *terminal* move, and it is deliberately not
        the same thing as the range -- see ``components['exp_max_excursion_pips']``,
        which is ``E[max_t |S_t - S_0|] = S * sigma * sqrt(8/pi)``, twice as big, and
        is the number that matters for a resting order.
    ``quantiles``
        ``{p: signed pips}`` of the terminal log move, so ``quantiles[0.05]`` is
        negative and ``quantiles[0.95]`` positive.  **Symmetric by construction**:
        there is no drift term and there is no direction in this model.
    ``components``
        every input that moved the number, so a rung can explain itself:
        ``har``/``implied`` (annualised vol), ``event`` (the variance multiplier),
        ``session`` (the window's share of a trading day's variance), plus
        ``w_har``/``w_implied``, ``sigma_daily``, ``n_events``,
        ``exp_max_excursion_pips``, ``spot``, ``pip``.
    ``basis``
        a one-line provenance string, printed on the panel.
    """

    sigma_window: float
    exp_abs_move_pips: float
    quantiles: dict[float, float]
    components: dict[str, float]
    basis: str


# --------------------------------------------------------------------------------------
# Daily RV proxy -- reuse fxgamma.signals.realized, never re-implement it
# --------------------------------------------------------------------------------------

#: estimators from :mod:`.realized` that are defined on a **single** OHLC bar.
#: Yang-Zhang needs n>2 bars (it estimates an overnight variance), so it cannot be a
#: one-day proxy; it is still the right choice for the multi-day windows elsewhere.
ONE_BAR_ESTIMATORS: tuple[str, ...] = ("parkinson", "garman_klass", "rogers_satchell")

#: Rogers-Satchell: drift-independent by construction, which matters here because a
#: single day *does* trend and Parkinson/Garman-Klass are biased high when it does.
DEFAULT_RV_METHOD = "rogers_satchell"


def rv_daily(hist: pd.DataFrame, method: str = DEFAULT_RV_METHOD, *,
             annual: float = ANNUAL, floor: float = 1e-4) -> pd.Series:
    """Per-bar annualised RV (decimal vol), one observation per day -- the HAR input.

    ``method='close_to_close'`` degenerates to ``|r_t| * sqrt(annual)``, which is an
    unbiased but extremely noisy variance proxy (efficiency 1).  The range estimators
    are 5-7x more efficient and are the reason HAR is usable on daily OHLC at all.
    ``floor`` keeps ``log v`` finite on a bar with a zero range (a holiday stamp, a
    stale quote); floored bars are rare and are reported by the caller as such.
    """
    need = ("open", "high", "low", "close")
    missing = [c for c in need if c not in hist.columns]
    if missing:
        raise KeyError(f"rv_daily needs OHLC columns, missing {missing}")
    d = hist[list(need)].astype(float)
    d = d[np.isfinite(d).all(axis=1) & (d > 0).all(axis=1)]
    if d.empty:
        return pd.Series(dtype=float, name=f"rv1_{method}")

    if method == "close_to_close":
        r = log_returns(d["close"])
        vals = np.abs(r) * math.sqrt(annual)
        idx = d.index[1:]
    elif method in ONE_BAR_ESTIMATORS:
        fn = ESTIMATORS[method]
        vals = np.array([fn(d.iloc[[i]], annual=annual) for i in range(len(d))], float)
        idx = d.index
    else:
        raise KeyError(
            f"{method!r} is not a one-bar estimator; use one of "
            f"{('close_to_close',) + ONE_BAR_ESTIMATORS}. Yang-Zhang needs n>2 bars.")

    vals = np.where(np.isfinite(vals), vals, np.nan)
    vals = np.maximum(vals, float(floor))
    return pd.Series(vals, index=idx, name=f"rv1_{method}").dropna()


def proxy_scale(hist: pd.DataFrame, method: str = DEFAULT_RV_METHOD, *,
                annual: float = ANNUAL) -> float:
    """Multiplicative correction from a range proxy's vol level to close-to-close.

    A one-bar range estimator is efficient but its *level* is not the level of the
    close-to-close move you actually have to price a rung against.  On real data the
    discrete-sampling bias makes it ~3-8% low; on the synthetic provider the bar
    generator is not internally consistent with its own close path and the factor is
    materially below 1.  Either way the fix is one measured number, applied openly:
    ``sqrt(mean var_c2c / mean var_proxy)`` over the history supplied.

    Fit it on **training data only** in any walk-forward -- it uses the whole frame it
    is handed, so hand it the training slice.
    """
    if method == "close_to_close":
        return 1.0
    a = rv_daily(hist, "close_to_close", annual=annual)
    b = rv_daily(hist, method, annual=annual)
    j = a.index.intersection(b.index)
    if len(j) < 30:
        return float("nan")
    va, vb = float((a[j] ** 2).mean()), float((b[j] ** 2).mean())
    if not np.isfinite(va) or not np.isfinite(vb) or vb <= 0:
        return float("nan")
    return float(math.sqrt(va / vb))


def simulate_rv(n: int = 1500, *, seed: int = 7, kappa: float = 4.0, xi: float = 0.45,
                mean_vol: float = 0.08, proxy_noise_sd: float = 0.0,
                annual: float = ANNUAL) -> pd.DataFrame:
    """A **machinery self-test** generator: RV with a known, tunable persistence.

    Log-variance follows a discretely-sampled OU with stationary sd ``xi/sqrt(2*kappa)``
    on the *log-vol* scale; ``proxy_noise_sd`` adds IID measurement noise to the
    observed log-variance so the estimator's signal-to-noise can be dialled.  Returns
    ``latent`` (the true vol) and ``proxy`` (what an estimator would see).  Used in
    ``docs/10_forecast_evaluation.md`` to show that the HAR code recovers a known
    process, so that a *small* R^2 on real or synthetic data can be attributed to the
    data rather than to a broken estimator.
    """
    rng = np.random.default_rng(int(seed))
    dt = 1.0 / annual
    lv = np.empty(int(n))
    lv0 = math.log(float(mean_vol))
    sd_stat = xi / math.sqrt(2.0 * kappa)
    lv[0] = lv0 + sd_stat * rng.standard_normal()
    for t in range(1, int(n)):
        lv[t] = lv[t - 1] + kappa * (lv0 - lv[t - 1]) * dt + xi * math.sqrt(dt) * rng.standard_normal()
    latent = np.exp(lv)
    noise = rng.standard_normal(int(n)) * float(proxy_noise_sd)
    proxy = np.sqrt(np.exp(np.log(latent ** 2) + noise))
    idx = pd.bdate_range(end=pd.Timestamp("2026-09-09", tz="UTC"), periods=int(n), tz="UTC",
                         name="date")
    return pd.DataFrame({"latent": latent, "proxy": proxy}, index=idx)


# --------------------------------------------------------------------------------------
# HAR-RV
# --------------------------------------------------------------------------------------

HAR_LAGS: tuple[int, int, int] = (1, 5, 22)          # Corsi's daily / weekly / monthly


def _to_var(rv: pd.Series) -> pd.Series:
    return pd.Series(np.asarray(rv, float) ** 2, index=rv.index, name="var")


def har_design(rv_series: pd.Series, *, horizon: int = 1, space: str = "log",
               lags: Sequence[int] = HAR_LAGS) -> tuple[pd.DataFrame, pd.Series]:
    """Corsi design matrix ``(X, y)``.

    ``y`` is the **average** variance over the next ``horizon`` days (Corsi's
    multi-step target), transformed by ``space``; ``X`` carries the daily, weekly and
    monthly averages of past variance, transformed the same way.  Rows are aligned so
    that every regressor in row ``t`` is known at the close of ``t`` -- there is no
    look-ahead, and ``har_walk_forward`` re-checks that property numerically.
    """
    if space not in ("log", "vol", "var"):
        raise ValueError(f"space must be log|vol|var, got {space!r}")
    v = _to_var(pd.Series(rv_series).dropna().astype(float))
    h = int(horizon)
    cols = {}
    for L in lags:
        cols[f"lag{L}"] = v.rolling(int(L)).mean()
    X = pd.DataFrame(cols)
    # forward average of v_{t+1..t+h}; h=1 is just v_{t+1}
    y = v.shift(-1) if h == 1 else v[::-1].rolling(h).mean()[::-1].shift(-1)

    def _tx(a):
        if space == "log":
            return np.log(np.maximum(a, 1e-12))
        if space == "vol":
            return np.sqrt(np.maximum(a, 0.0))
        return a

    X = X.apply(_tx)
    y = _tx(y)
    both = X.join(y.rename("y")).dropna()
    return both[list(X.columns)], both["y"]


def har_fit(rv_series: pd.Series, *, horizon: int = 1, space: str = "log",
            lags: Sequence[int] = HAR_LAGS) -> dict:
    """OLS fit of the HAR design.  Returns coefficients and in-sample diagnostics."""
    X, y = har_design(rv_series, horizon=horizon, space=space, lags=lags)
    n = len(y)
    if n < 30:
        return {"ok": False, "n": n, "reason": "fewer than 30 usable rows"}
    A = np.column_stack([np.ones(n), X.to_numpy(float)])
    beta, *_ = np.linalg.lstsq(A, y.to_numpy(float), rcond=None)
    fit = A @ beta
    resid = y.to_numpy(float) - fit
    dof = max(n - A.shape[1], 1)
    s2 = float(resid @ resid / dof)
    sst = float(((y - y.mean()) ** 2).sum())
    names = ["const"] + [f"b_{L}" for L in lags]
    # HAC (Newey-West) standard errors: HAR residuals are heteroskedastic and, at
    # horizon>1, overlapping.  Plain OLS t-stats would be optimistic.
    L_hac = max(int(math.floor(4 * (n / 100.0) ** (2.0 / 9.0))), 1)
    Xe = A * resid[:, None]
    S = Xe.T @ Xe
    for lag in range(1, L_hac + 1):
        G = Xe[lag:].T @ Xe[:-lag]
        w = 1.0 - lag / (L_hac + 1.0)
        S += w * (G + G.T)
    XtX_inv = np.linalg.pinv(A.T @ A)
    cov = XtX_inv @ S @ XtX_inv
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    return {
        "ok": True, "n": int(n), "space": space, "horizon": int(horizon),
        "coef": {k: float(b) for k, b in zip(names, beta)},
        "se_hac": {k: float(s) for k, s in zip(names, se)},
        "t_hac": {k: float(b / s) if s > 0 else float("nan")
                  for k, b, s in zip(names, beta, se)},
        "hac_lag": int(L_hac),
        "r2_in": float(1.0 - (resid @ resid) / sst) if sst > 0 else float("nan"),
        "resid_var": s2,
        "persistence": float(sum(beta[1:])),
        "lags": tuple(int(L) for L in lags),
    }


def _har_predict(info: dict, rv_series: pd.Series, *, jensen: bool = True) -> float:
    """Annualised vol forecast from a fitted HAR at the end of ``rv_series``."""
    v = _to_var(pd.Series(rv_series).dropna().astype(float))
    lags = info["lags"]
    if len(v) < max(lags):
        return float("nan")
    space = info["space"]

    def _tx(a):
        if space == "log":
            return math.log(max(a, 1e-12))
        if space == "vol":
            return math.sqrt(max(a, 0.0))
        return a

    x = [1.0] + [_tx(float(v.iloc[-int(L):].mean())) for L in lags]
    yhat = float(np.dot(x, [info["coef"]["const"]]
                        + [info["coef"][f"b_{L}"] for L in lags]))
    if space == "log":
        # E[v] = exp(mu + s^2/2): without this the level of the variance forecast is
        # biased LOW by exp(-s^2/2), which on these residuals is ~20-30%.
        var = math.exp(yhat + (0.5 * info["resid_var"] if jensen else 0.0))
    elif space == "vol":
        var = max(yhat, 0.0) ** 2
    else:
        var = max(yhat, 0.0)
    return float(math.sqrt(max(var, 0.0)))


def har_rv(rv_series, *, horizon: int = 1, space: str = "log",
           lags: Sequence[int] = HAR_LAGS, jensen: bool = True) -> tuple[float, dict]:
    """Corsi HAR-RV.  Returns ``(annualised vol forecast, diagnostics)``.

    ``rv_series`` is a series of **annualised daily RV** (decimal vol, one value per
    bar) -- exactly what :func:`rv_daily` produces from OHLC.  The fit is in
    ``space='log'`` variance by default: variance is right-skewed and strictly
    positive, log-space keeps the forecast positive without a constraint, and it is
    what wins on QLIKE (see ``docs/10_forecast_evaluation.md``).

    The returned diagnostics carry the coefficients with **HAC** standard errors, the
    in-sample R^2, the persistence ``b_d+b_w+b_m``, and the residual variance used
    for the Jensen correction.  In-sample R^2 is reported because it is asked for; it
    is **not** evidence.  Use :func:`har_walk_forward` for that.
    """
    ser = pd.Series(rv_series).dropna().astype(float)
    info = har_fit(ser, horizon=horizon, space=space, lags=lags)
    if not info.get("ok"):
        return float("nan"), info
    f = _har_predict(info, ser, jensen=jensen)
    info["forecast_vol_annual"] = f
    info["jensen"] = bool(jensen and space == "log")
    return f, info


def har_walk_forward(rv_series, *, horizon: int = 1, space: str = "log",
                     lags: Sequence[int] = HAR_LAGS, min_train: int = 250,
                     refit_every: int = 21, expanding: bool = True,
                     jensen: bool = True,
                     implied: pd.Series | None = None) -> pd.DataFrame:
    """Honest out-of-sample HAR: fit on the past only, predict the next bar, roll on.

    Every forecast for target day ``t+1`` uses data up to and including the close of
    ``t``.  Refitting every ``refit_every`` bars (rather than every bar) is a cost /
    realism trade-off, not a shortcut: it is *more* conservative, since the model is
    on average ~10 bars stale.

    Returns one row per forecastable target with columns ``actual`` (the realised RV
    proxy, annualised vol), ``har``, and the two naive benchmarks the model has to
    beat: ``rw`` (yesterday's RV -- the random walk) and ``mean`` (the trailing
    expanding mean).  If ``implied`` is supplied and aligned, an ``implied`` column
    and a fitted ``blend`` column are added.
    """
    ser = pd.Series(rv_series).dropna().astype(float)
    n = len(ser)
    h = int(horizon)
    if n < min_train + max(lags) + h + 5:
        return pd.DataFrame(columns=["actual", "har", "rw", "mean"])
    v = _to_var(ser)
    rows = []
    info: dict = {}
    for i in range(int(min_train), n - h):
        if not info or (i - int(min_train)) % int(refit_every) == 0:
            train = ser.iloc[:i + 1] if expanding else ser.iloc[max(0, i + 1 - min_train):i + 1]
            info = har_fit(train, horizon=h, space=space, lags=lags)
            if not info.get("ok"):
                continue
        f = _har_predict(info, ser.iloc[:i + 1], jensen=jensen)
        actual = float(np.sqrt(v.iloc[i + 1:i + 1 + h].mean()))
        rows.append({
            "date": ser.index[i + h],
            "actual": actual,
            "har": f,
            "rw": float(ser.iloc[i]),
            "mean": float(ser.iloc[:i + 1].mean()),
            "asof": ser.index[i],
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out = out.set_index("date")
    if implied is not None:
        imp = pd.Series(implied).dropna().astype(float)
        out["implied"] = imp.reindex(out["asof"]).to_numpy()   # implied known at asof
    return out


# --------------------------------------------------------------------------------------
# Loss functions and evaluation
# --------------------------------------------------------------------------------------


def qlike(actual_vol, pred_vol) -> float:
    """QLIKE on **variance** (Patton 2011): ``mean(a/p - log(a/p) - 1)``, lower better.

    Zero only when the forecast equals the proxy exactly.  Unlike MSE it is robust to
    the "actual" being a noisy but unbiased proxy for latent variance: the *ranking*
    of two forecasts under QLIKE is preserved in expectation, whereas under MSE on
    vol (as opposed to variance) it is not.  It also penalises under-forecasting far
    more than over-forecasting, which is the correct asymmetry for a book that leaves
    resting orders: a range forecast that is too small leaves the rungs unfilled or,
    worse, invites a stop.
    """
    a = np.asarray(actual_vol, float) ** 2
    p = np.asarray(pred_vol, float) ** 2
    m = np.isfinite(a) & np.isfinite(p) & (a > 0) & (p > 0)
    if not m.any():
        return float("nan")
    r = a[m] / p[m]
    return float(np.mean(r - np.log(r) - 1.0))


def mse_var(actual_vol, pred_vol) -> float:
    """MSE on variance.  Reported for completeness; QLIKE is the headline."""
    a = np.asarray(actual_vol, float) ** 2
    p = np.asarray(pred_vol, float) ** 2
    m = np.isfinite(a) & np.isfinite(p)
    return float(np.mean((a[m] - p[m]) ** 2)) if m.any() else float("nan")


def r2_oos(actual_vol, pred_vol, bench_vol, *, loss: str = "qlike") -> float:
    """Out-of-sample R^2 **against a named benchmark**: ``1 - L(model)/L(bench)``.

    ``loss='qlike'`` uses the QLIKE loss; ``loss='mse'`` uses MSE on variance.  A
    negative value means the model is worse than the benchmark, which is a result and
    must be reported as one.  R^2 against a zero forecast is meaningless for variance
    and is never computed here.
    """
    f = qlike if loss == "qlike" else mse_var
    lb = f(actual_vol, bench_vol)
    lm = f(actual_vol, pred_vol)
    if not np.isfinite(lb) or lb <= 0:
        return float("nan")
    return float(1.0 - lm / lb)


def mincer_zarnowitz(actual_vol, pred_vol) -> dict:
    """Regress actual variance on predicted variance: ``a = alpha + beta p + e``.

    An unbiased forecast has ``alpha=0, beta=1``.  ``beta<1`` is the usual finding and
    means the forecast is too *variable*: it should be shrunk toward its mean.
    """
    a = np.asarray(actual_vol, float) ** 2
    p = np.asarray(pred_vol, float) ** 2
    m = np.isfinite(a) & np.isfinite(p)
    a, p = a[m], p[m]
    if a.size < 10:
        return {"alpha": float("nan"), "beta": float("nan"), "r2": float("nan"), "n": int(a.size)}
    A = np.column_stack([np.ones(a.size), p])
    coef, *_ = np.linalg.lstsq(A, a, rcond=None)
    res = a - A @ coef
    sst = float(((a - a.mean()) ** 2).sum())
    return {"alpha": float(coef[0]), "beta": float(coef[1]),
            "r2": float(1 - (res @ res) / sst) if sst > 0 else float("nan"),
            "n": int(a.size)}


def diebold_mariano(actual_vol, pred_a, pred_b, *, loss: str = "qlike",
                    hac_lag: int | None = None) -> dict:
    """Diebold-Mariano test that ``pred_a`` and ``pred_b`` have equal expected loss.

    ``stat`` is ``mean(d)/se(d)`` with a Newey-West HAC standard error (the loss
    differential is autocorrelated because volatility is).  Negative ``stat`` favours
    ``pred_a``.  The small-sample Harvey-Leybourne-Newbold correction is applied.
    """
    a = np.asarray(actual_vol, float) ** 2
    pa = np.asarray(pred_a, float) ** 2
    pb = np.asarray(pred_b, float) ** 2
    m = np.isfinite(a) & np.isfinite(pa) & np.isfinite(pb) & (a > 0) & (pa > 0) & (pb > 0)
    a, pa, pb = a[m], pa[m], pb[m]
    n = a.size
    if n < 20:
        return {"stat": float("nan"), "p_value": float("nan"), "n": int(n)}
    if loss == "qlike":
        la = a / pa - np.log(a / pa) - 1.0
        lb = a / pb - np.log(a / pb) - 1.0
    else:
        la, lb = (a - pa) ** 2, (a - pb) ** 2
    d = la - lb
    L = hac_lag if hac_lag is not None else max(int(math.floor(4 * (n / 100.0) ** (2.0 / 9.0))), 1)
    dm = d - d.mean()
    g0 = float(dm @ dm / n)
    var = g0
    for lag in range(1, L + 1):
        g = float(dm[lag:] @ dm[:-lag] / n)
        var += 2.0 * (1.0 - lag / (L + 1.0)) * g
    var = max(var, 1e-24)
    stat = float(d.mean() / math.sqrt(var / n))
    stat *= math.sqrt(max((n + 1 - 2 * L + L * (L - 1) / n) / n, 1e-9))   # HLN correction
    from math import erfc
    p = float(erfc(abs(stat) / math.sqrt(2.0)))
    return {"stat": stat, "p_value": p, "n": int(n), "hac_lag": int(L),
            "mean_loss_a": float(la.mean()), "mean_loss_b": float(lb.mean())}


def evaluate_forecasts(actual_vol, preds: Mapping[str, Sequence[float]], *,
                       benchmark: str = "rw") -> pd.DataFrame:
    """One row per model: QLIKE, MSE(var), RMSE(vol), OOS R^2 vs ``benchmark``, MZ, DM.

    This is the table ``docs/10_forecast_evaluation.md`` is built from.
    """
    a = np.asarray(actual_vol, float)
    bench = np.asarray(preds[benchmark], float)
    rows = []
    for name, p in preds.items():
        p = np.asarray(p, float)
        mz = mincer_zarnowitz(a, p)
        dm = diebold_mariano(a, p, bench) if name != benchmark else {"stat": 0.0, "p_value": 1.0}
        m = np.isfinite(a) & np.isfinite(p)
        rows.append({
            "model": name,
            "n": int(m.sum()),
            "qlike": qlike(a, p),
            "r2_oos_qlike": r2_oos(a, p, bench, loss="qlike"),
            "r2_oos_mse": r2_oos(a, p, bench, loss="mse"),
            "mse_var": mse_var(a, p),
            "rmse_vol": float(np.sqrt(np.mean((a[m] - p[m]) ** 2))) if m.any() else float("nan"),
            "bias_vol": float(np.mean(p[m] - a[m])) if m.any() else float("nan"),
            "r2_logvar": _r2_logvar(a, p),
            "mz_alpha": mz["alpha"], "mz_beta": mz["beta"], "mz_r2": mz["r2"],
            "dm_vs_bench": dm.get("stat", float("nan")),
            "dm_p": dm.get("p_value", float("nan")),
        })
    return pd.DataFrame(rows).set_index("model")


def _r2_logvar(actual_vol, pred_vol) -> float:
    """R^2 of ``log`` actual variance on ``log`` predicted variance, against the
    sample mean.  This is the scale the HAR literature quotes and the scale on which
    a signal-to-noise ceiling can be computed, so it is the one number here that is
    comparable to a published figure."""
    a = np.asarray(actual_vol, float) ** 2
    p = np.asarray(pred_vol, float) ** 2
    m = np.isfinite(a) & np.isfinite(p) & (a > 0) & (p > 0)
    if m.sum() < 10:
        return float("nan")
    la, lp = np.log(a[m]), np.log(p[m])
    sse = float(((la - lp - (la - lp).mean()) ** 2).sum())   # demeaned: level bias is
    sst = float(((la - la.mean()) ** 2).sum())               # a separate diagnostic
    return float(1.0 - sse / sst) if sst > 0 else float("nan")


def coverage_report(hist: pd.DataFrame, sigma_daily: Sequence[float], *,
                    quantiles: Sequence[float] = (0.05, 0.25, 0.75, 0.95)) -> pd.DataFrame:
    """Calibration: does the predicted band actually contain the realised move?

    ``sigma_daily`` is the per-day predicted log-move stdev, aligned to the *target*
    day of ``hist``.  The realised move is close-to-close.  A forecast can have a fine
    R^2 and still be badly calibrated -- and it is calibration, not R^2, that decides
    whether a resting rung at the 90th percentile gets filled once a fortnight or once
    a week.  Under-coverage means the ladder is too tight.
    """
    c = pd.Series(hist["close"]).astype(float)
    r = np.log(c / c.shift(1)).dropna()
    s = pd.Series(np.asarray(sigma_daily, float), index=pd.Index(getattr(sigma_daily, "index", r.index[-len(sigma_daily):])))
    j = r.index.intersection(s.index)
    r, s = r[j].to_numpy(float), s[j].to_numpy(float)
    m = np.isfinite(r) & np.isfinite(s) & (s > 0)
    r, s = r[m], s[m]
    rows = []
    for q in quantiles:
        z = _norm_ppf(float(q))
        hit = float(np.mean(r <= z * s))
        rows.append({"quantile": float(q), "expected": float(q), "realised": hit,
                     "n": int(r.size),
                     "se": float(math.sqrt(max(q * (1 - q) / max(r.size, 1), 0.0)))})
    rows.append({"quantile": float("nan"), "expected": float("nan"),
                 "realised": float(np.mean(np.abs(r)) / np.mean(s * math.sqrt(2 / math.pi)))
                 if r.size else float("nan"), "n": int(r.size), "se": float("nan")})
    out = pd.DataFrame(rows)
    out.loc[out.index[-1], "quantile"] = -1.0        # marker row: E|r| ratio realised/predicted
    return out


# --------------------------------------------------------------------------------------
# Blending HAR with implied
# --------------------------------------------------------------------------------------

#: Default blend when nothing has been measured for this pair.  It is a **prior**, not
#: a measurement, and it is 50/50 because that is roughly where the published
#: encompassing regressions land for liquid G10 pairs (implied carries information HAR
#: does not, and vice versa; neither subsumes the other).  On the synthetic provider
#: the measured weight on implied is ~0 and that number is *not* evidence about real
#: markets -- the simulator builds its ATM as a fixed linear function of trailing
#: realized vol, so implied there is a noiseless copy of HAR's own regressors.  Replace
#: this with :func:`fit_blend_weights` on the user's own recorded implied history.
DEFAULT_BLEND: dict[str, float] = {"har": 0.5, "implied": 0.5}


def blend_sigma(sigma_har: float, sigma_implied: float | None,
                weights: Mapping[str, float] | None = None) -> tuple[float, dict]:
    """Convex blend in **vol** space.  Returns ``(sigma, weights_used)``.

    Vol space rather than variance space because the loss we care about is closer to
    linear in vol at the point of use (a rung is placed at a distance), and because
    the two candidates are of similar magnitude so the choice moves the answer by
    well under a tenth of a vol point.
    """
    w = dict(weights or DEFAULT_BLEND)
    wh, wi = float(w.get("har", 0.0)), float(w.get("implied", 0.0))
    if sigma_implied is None or not np.isfinite(sigma_implied) or sigma_implied <= 0:
        return float(sigma_har), {"har": 1.0, "implied": 0.0}
    if not np.isfinite(sigma_har) or sigma_har <= 0:
        return float(sigma_implied), {"har": 0.0, "implied": 1.0}
    tot = wh + wi
    if tot <= 0:
        return float(sigma_har), {"har": 1.0, "implied": 0.0}
    wh, wi = wh / tot, wi / tot
    return float(wh * sigma_har + wi * sigma_implied), {"har": wh, "implied": wi}


def fit_blend_weights(actual_vol, har_vol, implied_vol, *, loss: str = "qlike",
                      grid: int = 101) -> dict:
    """Choose the convex weight on implied by minimising ``loss`` on the sample given.

    **Fit this on a training sample and score it on a later one.**  The grid search is
    over a single parameter on ``[0, 1]``, so the in-sample optimism is small, but it
    is not zero and it is not reported here -- ``docs/10`` scores the fitted weight out
    of sample.
    """
    a = np.asarray(actual_vol, float)
    h = np.asarray(har_vol, float)
    i = np.asarray(implied_vol, float)
    m = np.isfinite(a) & np.isfinite(h) & np.isfinite(i) & (a > 0) & (h > 0) & (i > 0)
    a, h, i = a[m], h[m], i[m]
    if a.size < 30:
        return {"ok": False, "n": int(a.size), "weights": dict(DEFAULT_BLEND),
                "reason": "fewer than 30 paired observations"}
    f = qlike if loss == "qlike" else mse_var
    ws = np.linspace(0.0, 1.0, int(grid))
    losses = np.array([f(a, (1 - w) * h + w * i) for w in ws])
    k = int(np.argmin(losses))
    return {"ok": True, "n": int(a.size), "loss": loss,
            "weights": {"har": float(1 - ws[k]), "implied": float(ws[k])},
            "loss_at_opt": float(losses[k]),
            "loss_har_only": float(losses[0]), "loss_implied_only": float(losses[-1]),
            "curve": pd.DataFrame({"w_implied": ws, "loss": losses})}


# --------------------------------------------------------------------------------------
# Session variance time
# --------------------------------------------------------------------------------------

#: Relative variance intensity by **UTC hour**, normalised so a full day sums to 1.0.
#: Shape (not the exact numbers) is the well-documented FX intraday seasonality: a step
#: up at the London open (07:00 UTC), the peak through the London/New York overlap
#: (12:00-16:00 UTC), the trough in the Asia-Pacific handover (20:00-05:00 UTC).
#: These are **priors**, printed wherever they are used, and
#: :func:`fxgamma.signals.rangeforecast.window_var_fraction` takes a ``profile``
#: argument so the user can substitute weights measured from their own intraday data.
#: The single number that matters: London close to London open is 14/24 = 58% of the
#: clock and about a third of the variance.  Using clock time overstates the overnight
#: sigma by ~30%, which mis-spaces every rung.
_RAW_HOURLY = {
    0: 0.55, 1: 0.60, 2: 0.50, 3: 0.45, 4: 0.40, 5: 0.40,
    6: 0.55, 7: 1.00, 8: 1.25, 9: 1.20, 10: 1.05, 11: 1.00,
    12: 1.55, 13: 1.95, 14: 2.05, 15: 1.75, 16: 1.35, 17: 0.95,
    18: 0.70, 19: 0.55, 20: 0.50, 21: 0.45, 22: 0.40, 23: 0.45,
}
_TOT = sum(_RAW_HOURLY.values())
SESSION_VAR_PROFILE: dict[int, float] = {h: w / _TOT for h, w in _RAW_HOURLY.items()}


def session_hours(start: datetime, end: datetime) -> list[tuple[int, float]]:
    """Decompose ``[start, end)`` into ``(utc_hour, hours_spent)`` pieces."""
    if end <= start:
        return []
    s = start.astimezone(timezone.utc)
    e = end.astimezone(timezone.utc)
    out: list[tuple[int, float]] = []
    cur = s
    while cur < e:
        nxt = min((cur + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0), e)
        if nxt <= cur:
            nxt = min(cur + timedelta(hours=1), e)
        out.append((cur.hour, (nxt - cur).total_seconds() / 3600.0))
        cur = nxt
    return out


def window_var_fraction(start: datetime, end: datetime, *,
                        profile: Mapping[int, float] | None = None) -> float:
    """Share of **one trading day's** variance falling in ``[start, end)``.

    A weekend or a holiday is *not* handled here: this is a pure intraday seasonality
    integral.  A Friday-close-to-Monday-open window should be passed as the trading
    hours it actually contains, which is what ``portfolio.overnight.passive_window``
    is for -- if that gives us a ``var_fraction`` we use it and never second-guess it.
    """
    prof = dict(profile or SESSION_VAR_PROFILE)
    return float(sum(prof.get(h, 1.0 / 24.0) * frac for h, frac in session_hours(start, end)))


def _window_bounds(window) -> tuple[datetime | None, datetime | None, float | None, str]:
    """Duck-type a window: ``PassiveWindow``, a ``(start, end)`` pair, or a fraction."""
    if window is None:
        return None, None, None, "window=None"
    vf = getattr(window, "var_fraction", None)
    s = getattr(window, "start", None)
    e = getattr(window, "end", None)
    if s is not None and e is not None:
        label = getattr(window, "label", "") or "window"
        return s, e, (float(vf) if vf is not None else None), label
    if isinstance(window, (tuple, list)) and len(window) == 2:
        return window[0], window[1], None, "explicit (start, end)"
    if isinstance(window, (int, float)):
        return None, None, float(window), f"var_fraction={float(window):.4f} (given)"
    raise TypeError(f"cannot interpret window={window!r}")


# --------------------------------------------------------------------------------------
# Scheduled events
# --------------------------------------------------------------------------------------

#: Extra **instantaneous** log-move stdev contributed by one scheduled event, by
#: ``importance``.  A top-tier print (FOMC/ECB/NFP/CPI) moving G10 spot by 0.35%
#: one-sigma on the announcement is the desk rule of thumb this encodes.  These are
#: PRIORS.  :func:`calibrate_event_uplift` measures the uplift from the user's own
#: history and returns the multiplier to use instead; the synthetic provider's own
#: event bump is ~0.9 vol points of ATM on a top-tier event, which is the same order.
EVENT_SIGMA: dict[int, float] = {3: 0.0035, 2: 0.0015, 1: 0.0}


def event_variance_add(events: pd.DataFrame | None, pair: str,
                       start: datetime | None, end: datetime | None, *,
                       sigma_by_importance: Mapping[int, float] | None = None
                       ) -> tuple[float, int]:
    """Added log-move variance from calendar events inside ``[start, end)``.

    Only events in the pair's **own two currencies** count.  A Riksbank decision does
    not widen the EURUSD overnight range, and pretending it does is how an event
    adjustment becomes noise.  Returns ``(added_variance, n_events)``.
    """
    if events is None or len(events) == 0 or start is None or end is None:
        return 0.0, 0
    ev = events
    if "datetime" not in ev.columns or "ccy" not in ev.columns:
        return 0.0, 0
    spec = pair_spec(pair)
    ts = pd.to_datetime(ev["datetime"], utc=True)
    lo = pd.Timestamp(start).tz_convert("UTC") if pd.Timestamp(start).tzinfo else \
        pd.Timestamp(start, tz="UTC")
    hi = pd.Timestamp(end).tz_convert("UTC") if pd.Timestamp(end).tzinfo else \
        pd.Timestamp(end, tz="UTC")
    sel = ev[(ts >= lo) & (ts < hi) & ev["ccy"].isin([spec.base, spec.quote])]
    if sel.empty:
        return 0.0, 0
    tbl = dict(sigma_by_importance or EVENT_SIGMA)
    add = 0.0
    for imp in sel["importance"].astype(int):
        s = float(tbl.get(int(imp), 0.0))
        add += s * s
    return float(add), int(len(sel))


def calibrate_event_uplift(hist: pd.DataFrame, events: pd.DataFrame, pair: str, *,
                           importance: int = 3, method: str = DEFAULT_RV_METHOD
                           ) -> dict:
    """MEASURED: ratio of mean daily variance on event days to non-event days.

    This is the honest replacement for :data:`EVENT_SIGMA`.  It is a *daily* uplift,
    so mapping it onto an overnight window assumes the event's variance lands inside
    the window -- true for an Asia-session BoJ or a US 08:30 print seen from a London
    book, false for a 14:15 ECB.  Returns the ratio, both sample sizes, a Welch t on
    log variance, and the implied ``EVENT_SIGMA`` entry, so the caller can see whether
    the sample is anywhere near large enough to move off the prior.
    """
    rv = rv_daily(hist, method=method)
    if rv.empty or events is None or len(events) == 0:
        return {"ok": False, "reason": "no data"}
    spec = pair_spec(pair)
    ev = events[(events["importance"].astype(int) >= int(importance))
                & (events["ccy"].isin([spec.base, spec.quote]))]
    if ev.empty:
        return {"ok": False, "reason": "no relevant events in the calendar window"}
    ev_days = set(pd.to_datetime(ev["datetime"], utc=True).dt.date)
    idx_days = pd.Index([d.date() for d in rv.index])
    mask = idx_days.isin(ev_days)
    v = rv.to_numpy(float) ** 2
    ve, vn = v[mask], v[~mask]
    if ve.size < 5 or vn.size < 20:
        return {"ok": False, "n_event": int(ve.size), "n_other": int(vn.size),
                "reason": "sample too small to measure an event uplift"}
    le, ln = np.log(ve), np.log(vn)
    se = math.sqrt(le.var(ddof=1) / le.size + ln.var(ddof=1) / ln.size)
    t = float((le.mean() - ln.mean()) / se) if se > 0 else float("nan")
    ratio = float(ve.mean() / vn.mean())
    # implied per-event sigma: sqrt of the extra variance over one day
    extra = max(ve.mean() - vn.mean(), 0.0) / ANNUAL
    return {"ok": True, "var_ratio": ratio, "n_event": int(ve.size), "n_other": int(vn.size),
            "t_log": t, "implied_event_sigma": float(math.sqrt(extra)),
            "mean_var_event": float(ve.mean()), "mean_var_other": float(vn.mean()),
            "note": ("daily uplift; overnight applicability depends on whether the print "
                     "falls inside the window")}


# --------------------------------------------------------------------------------------
# Distance / probability helpers (the ladder builder's actual questions)
# --------------------------------------------------------------------------------------


def touch_probability(pips: float, sigma_window: float, spot: float, pip: float) -> float:
    """P(spot **touches** a level ``pips`` away at some point in the window).

    Reflection principle for driftless Brownian motion: ``2 * N(-d/sigma)``, which is
    roughly **twice** the probability of finishing beyond it.  A ladder priced off the
    terminal distribution rather than the running maximum will systematically
    under-estimate fills, which is the single most common way to mis-size a resting
    order book.  Drift is deliberately set to zero -- see the module docstring.
    """
    if not np.isfinite(sigma_window) or sigma_window <= 0 or spot <= 0:
        return float("nan")
    d = abs(float(pips)) * float(pip) / float(spot)
    from math import erfc
    return float(min(erfc(d / (sigma_window * math.sqrt(2.0))), 1.0))


def expected_max_excursion(sigma_window: float, spot: float, pip: float) -> float:
    """``E[max_t |S_t - S_0|]`` in pips: ``S * sigma * sqrt(8/pi)``.

    For driftless BM the expected maximum *absolute* excursion is ``sqrt(8/pi)*sigma``
    against ``sqrt(2/pi)*sigma`` for the terminal absolute move -- a factor of 2.  This
    is the number that says how far a resting ladder should reach.
    """
    if not np.isfinite(sigma_window) or sigma_window <= 0:
        return float("nan")
    return float(spot * sigma_window * math.sqrt(8.0 / math.pi) / pip)


def _norm_ppf(p: float) -> float:
    """Acklam's inverse normal CDF (the models layer's version is not importable here
    without pulling the pricer in; this is a 1e-9-accurate standalone)."""
    if not 0.0 < p < 1.0:
        return float("nan")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        x = (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    elif p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        x = -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    else:
        q, r = p - 0.5, (p - 0.5) ** 2
        x = (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
            (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    return float(x)


DEFAULT_QUANTILES: tuple[float, ...] = (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)


# --------------------------------------------------------------------------------------
# The public forecast
# --------------------------------------------------------------------------------------


def overnight_range_forecast(pair: str, mkt, hist: pd.DataFrame, window,
                             events: pd.DataFrame | None = None,
                             blend: str = "har+implied", *,
                             weights: Mapping[str, float] | None = None,
                             rv_method: str = DEFAULT_RV_METHOD,
                             space: str = "log",
                             quantiles: Sequence[float] = DEFAULT_QUANTILES,
                             profile: Mapping[int, float] | None = None,
                             event_sigma: Mapping[int, float] | None = None,
                             rescale: bool = True,
                             spot: float | None = None) -> RangeForecast:
    """Forecast the **magnitude** of the overnight move.  No direction, ever.

    ``pair``    FORDOM symbol.
    ``mkt``     a :class:`~fxgamma.types.MarketSnapshot`; the ATM implied for the
                window's own tenor is read from ``mkt.surfaces[pair]`` if present.  A
                missing surface is not an error: the blend falls back to HAR alone and
                says so in ``basis``.
    ``hist``    daily OHLC, the canonical ``fxgamma.data`` shape.
    ``window``  a ``PassiveWindow`` (its ``var_fraction`` wins if it has one), a
                ``(start, end)`` datetime pair, or a bare variance fraction.
    ``events``  the calendar frame from ``provider.events(...)``; only rows in the
                pair's own currencies inside the window are used.
    ``blend``   ``"har+implied"`` | ``"har"`` | ``"implied"``.

    The chain, in order, with every step visible in ``components``:
    HAR annualised vol -> blended with implied -> per-trading-day sigma -> scaled by
    the window's variance fraction -> scheduled-event variance added -> pips.
    """
    spec = pair_spec(pair)
    S = float(spot if spot is not None else
              (getattr(mkt, "spot", {}) or {}).get(pair, float("nan")))
    if not np.isfinite(S) or S <= 0:
        if hist is not None and len(hist):
            S = float(hist["close"].iloc[-1])
        else:
            raise ValueError(f"no spot for {pair}: pass spot= or a populated MarketSnapshot")

    start, end, vf_given, wlabel = _window_bounds(window)
    if vf_given is not None:
        var_fraction = float(vf_given)
        vf_src = "window.var_fraction"
    elif start is not None and end is not None:
        var_fraction = window_var_fraction(start, end, profile=profile)
        vf_src = "SESSION_VAR_PROFILE"
    else:
        var_fraction = 1.0
        vf_src = "full trading day (no window given)"

    # --- HAR ---------------------------------------------------------------------
    rv = rv_daily(hist, method=rv_method) if hist is not None and len(hist) else pd.Series(dtype=float)
    sigma_har, har_info = har_rv(rv, horizon=1, space=space) if len(rv) else (float("nan"), {"ok": False})
    # A range proxy forecasts its own level, not the level of the close-to-close move a
    # rung is actually filled by.  One measured constant, reported, never hidden.
    pscale = 1.0
    if rescale and rv_method != "close_to_close" and hist is not None and len(hist) > 60:
        ps = proxy_scale(hist, rv_method)
        if np.isfinite(ps) and ps > 0:
            pscale = float(ps)
            sigma_har = float(sigma_har * pscale)

    # --- implied -----------------------------------------------------------------
    sigma_imp = float("nan")
    surf = (getattr(mkt, "surfaces", {}) or {}).get(pair)
    if surf is not None:
        # match the implied tenor to the window, floored at overnight
        if start is not None and end is not None:
            T = max((pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() / 86400.0, 1.0) / 365.0
        else:
            T = 1.0 / 365.0
        try:
            sigma_imp = float(surf.atm(T))
        except Exception:                                       # noqa: BLE001
            sigma_imp = float("nan")

    mode = str(blend).lower()
    if mode == "har":
        sigma_ann, w_used = (sigma_har, {"har": 1.0, "implied": 0.0})
    elif mode == "implied":
        sigma_ann, w_used = ((sigma_imp, {"har": 0.0, "implied": 1.0})
                             if np.isfinite(sigma_imp)
                             else (sigma_har, {"har": 1.0, "implied": 0.0}))
    else:
        sigma_ann, w_used = blend_sigma(sigma_har, sigma_imp, weights)

    if not np.isfinite(sigma_ann) or sigma_ann <= 0:
        raise ValueError(f"no usable vol for {pair}: HAR={sigma_har}, implied={sigma_imp}. "
                         "Supply more history or a surface.")

    # --- session variance time ----------------------------------------------------
    sigma_day = sigma_ann / math.sqrt(ANNUAL)          # one trading day, W-7 basis
    var_window = (sigma_day ** 2) * var_fraction

    # --- scheduled events ---------------------------------------------------------
    add_var, n_ev = event_variance_add(events, pair, start, end,
                                       sigma_by_importance=event_sigma)
    var_total = var_window + add_var
    event_mult = float(var_total / var_window) if var_window > 0 else float("nan")
    sigma_window = math.sqrt(max(var_total, 0.0))

    # --- to pips ------------------------------------------------------------------
    pip = spec.pip
    exp_abs = S * sigma_window * math.sqrt(2.0 / math.pi) / pip
    qs = {float(p): float(S * (math.exp(_norm_ppf(float(p)) * sigma_window) - 1.0) / pip)
          for p in quantiles}

    components = {
        "har": float(sigma_har),
        "implied": float(sigma_imp),
        "event": float(event_mult),
        "session": float(var_fraction),
        "w_har": float(w_used["har"]),
        "w_implied": float(w_used["implied"]),
        "sigma_annual_blend": float(sigma_ann),
        "sigma_daily": float(sigma_day),
        "n_events": float(n_ev),
        "event_add_var": float(add_var),
        "exp_max_excursion_pips": float(expected_max_excursion(sigma_window, S, pip)),
        "spot": float(S),
        "pip": float(pip),
        "har_r2_in": float(har_info.get("r2_in", float("nan"))),
        "har_persistence": float(har_info.get("persistence", float("nan"))),
        "har_n": float(har_info.get("n", 0)),
        "proxy_scale": float(pscale),
    }
    imp_txt = f"{sigma_imp * 100:.2f}%" if np.isfinite(sigma_imp) else "unavailable"
    basis = (f"sqrt(252) distance basis | HAR({'/'.join(str(x) for x in HAR_LAGS)}) on "
             f"{rv_method} 1-bar RV, n={int(har_info.get('n', 0))}, "
             f"in-sample R2={har_info.get('r2_in', float('nan')):.3f}, "
             f"proxy_scale={pscale:.3f} "
             f"| HAR {sigma_har * 100:.2f}% x {w_used['har']:.2f} + implied {imp_txt} "
             f"x {w_used['implied']:.2f} = {sigma_ann * 100:.2f}% ann "
             f"| var_fraction {var_fraction:.3f} from {vf_src} ({wlabel}) "
             f"| {n_ev} in-window event(s) x{event_mult:.3f} variance")
    return RangeForecast(sigma_window=float(sigma_window),
                         exp_abs_move_pips=float(exp_abs),
                         quantiles=qs, components=components, basis=basis)
