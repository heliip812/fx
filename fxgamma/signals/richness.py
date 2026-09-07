"""Is gamma cheap or expensive: RV-IV spread, quote z-scores, breakeven, carry.

Everything here is a *comparison*, and every comparison in this file is made on a
matched window with its basis printed, because the two ways to get this wrong both
flip the sign of "cheap or expensive":

* comparing a 21-**business**-day realized vol to a nominal "1M" implied when the
  option actually has 30 **calendar** days to run (trader W-8).  ``rv_iv_spread``
  matches on calendar days to the actual expiry and says so in the output.
* quoting a z-score off overlapping windows, which inflates every z by roughly
  ``sqrt(window)``.  ``zscore`` returns ``n_eff`` alongside and, by default, widens
  the standard error accordingly.

The two desk identities
-----------------------
``BE%``  daily breakeven move: ``sqrt(|theta| / (0.005 * G1 * S))`` (requirements s0).
         For a pure ATM position this reduces **exactly** to ``sigma_ATM / sqrt(365)``
         expressed in percent -- ``assert_breakeven_identity`` is the self-test, run
         on import of the test-suite and available to QA as REQ-039's acceptance.

         The identity only holds when ``theta`` is the **gamma-theta** term
         ``-0.5 * Gamma * S^2 * sigma^2`` (trader W-15).  Full Garman-Kohlhagen theta
         also carries ``-rd K e^{-rd T} N(d2) + rf S e^{-rf T} N(d1)``, which is carry,
         not the rent gamma pays back.  ``theta_mode="gamma"`` (the default) uses the
         gamma-theta; ``"total"`` uses the full theta and is reported alongside so the
         difference is visible rather than assumed away.

``BE`` is a **calendar** number (theta is paid on calendar days, ``sqrt(365)``), while
distance and probability are trading-day numbers (``sqrt(252)``, see
``portfolio.zones``).  Both appear in this module and they are never mixed.

Coverage ratio (trader W-6)
---------------------------
REQ-039's ``Gamma$/|theta|`` has units of days-per-percent and cannot be compared
across pairs.  :func:`coverage_ratio` replaces it with
``gamma_pnl(realized move) / |theta to next mark|``: dimensionless, exactly 1.0 at
breakeven, and equal to ``(sigma_r/sigma_i)^2`` for an ATM book -- a number that does
read across pairs.  Theta is kept **signed** everywhere it is displayed.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from ..conventions import pair_spec
from ..models import gk
from ..portfolio.risk import (dhedge_pnl, gamma_pnl_pct, fx_rate, price_book)
from ..types import Book, MarketSnapshot
from .realized import ANNUAL, realized_vol

__all__ = ["breakeven_pct", "gamma_theta", "daily_breakeven", "coverage_ratio",
           "gamma_carry_expectancy", "rv_iv_spread", "zscore", "quote_zscores",
           "richness_table", "assert_breakeven_identity", "CALENDAR_DAYS"]

CALENDAR_DAYS = 365.0          # theta is paid on calendar days (economics basis)


# --------------------------------------------------------------------------- #
# breakeven
# --------------------------------------------------------------------------- #
def breakeven_pct(theta: float, gamma_1pct: float, spot: float) -> float:
    """``BE% = sqrt(|theta| / (0.005 * G1 * S))`` -- requirements s0, in **percent**.

    Returns ``nan`` when the book is short gamma or flat: there is no move that pays
    a theta you are *collecting*, and printing a number there would be a lie.
    """
    denom = 0.005 * float(gamma_1pct) * float(spot)
    if not np.isfinite(denom) or denom <= 0.0:
        return float("nan")
    return float(math.sqrt(abs(float(theta)) / denom))


def gamma_theta(gamma: float, spot: float, sigma: float,
                days: float = 1.0) -> float:
    """The gamma-theta component of theta, in quote ccy per ``days`` calendar days.

    ``-0.5 * Gamma * S^2 * sigma^2 * dt`` with ``Gamma = d(delta_base)/dS``.  This is
    the only part of theta that gamma pays back, and therefore the only part that
    belongs in a breakeven (trader W-15).
    """
    return -0.5 * float(gamma) * float(spot) ** 2 * float(sigma) ** 2 * float(days) / CALENDAR_DAYS


def daily_breakeven(book: Book, mkt: MarketSnapshot, pair: str, *,
                    theta_mode: str = "gamma", days: float = 1.0,
                    realized_vol_now: float | None = None,
                    report_ccy: str = "USD",
                    marks: Mapping[str, float] | None = None) -> dict[str, float | str]:
    """The morning breakeven card for one pair (REQ-039).

    Returns ``be_pct``, ``be_pips``, the theta actually charged over ``days`` (signed,
    in words), ``G1``, and -- when ``realized_vol_now`` is supplied -- the
    :func:`coverage_ratio` and how many realized sigma-days the breakeven represents.

    ``days`` is "days to the next mark", not always 1: on a Friday it is 3, and
    showing "you pay USD 5,800" against USD 17,400 actually owed is the recurring
    error the trader flags in Q-6.
    """
    spec = pair_spec(pair)
    df = price_book(book.filter(pair), mkt, report_ccy=report_ccy, marks=marks)
    live = df[~df["expired"].astype(bool)] if len(df) else df
    if not len(live):
        return {"pair": pair, "be_pct": float("nan"), "be_pips": float("nan")}
    S = float(live["spot"].iloc[0])
    g1 = float(live["gamma_1pct"].sum())
    gam = float(live["gamma"].sum())
    th_total = float(live["theta"].sum())
    vw = np.abs(live["vega"].to_numpy(float)) + 1e-12
    sig = float(np.average(live["vol"].to_numpy(float), weights=vw))
    th_gamma = gamma_theta(gam, S, sig)
    theta_used = th_gamma if theta_mode == "gamma" else th_total
    be = breakeven_pct(theta_used, g1, S)
    fq = fx_rate(spec.quote, report_ccy, mkt)
    out: dict[str, float | str] = {
        "pair": pair, "spot": S, "gamma_1pct": g1,
        "theta_per_day": th_total, "theta_gamma_per_day": th_gamma,
        "theta_to_next_mark": th_total * days,
        "theta_to_next_mark_rep": th_total * days * fq,
        "days_to_next_mark": days,
        "theta_mode": theta_mode, "sigma_used": sig,
        "be_pct": be, "be_pips": be / 100.0 * S / spec.pip,
        "be_basis": "sqrt(365) -- theta is paid on calendar days",
        "words": (f"you {'pay' if th_total < 0 else 'collect'} "
                  f"{spec.quote} {abs(th_total * days):,.0f} over {days:g} day(s)"),
        "report_ccy": report_ccy.upper(), "ccy": spec.quote,
    }
    if realized_vol_now is not None and np.isfinite(realized_vol_now):
        out["coverage_ratio"] = coverage_ratio(g1, S, theta_used,
                                               float(realized_vol_now))
        out["realized_sigma_day_pct"] = 100.0 * float(realized_vol_now) / math.sqrt(CALENDAR_DAYS)
        out["be_in_realized_sigmas"] = (be / out["realized_sigma_day_pct"]
                                        if out["realized_sigma_day_pct"] else float("nan"))
    return out


def coverage_ratio(gamma_1pct: float, spot: float, theta: float,
                   realized_vol: float, days: float = 1.0) -> float:
    """``gamma_pnl(realized daily move) / |theta over the same period|`` (W-6).

    Dimensionless; 1.0 exactly at breakeven; ``(sigma_r/sigma_i)^2`` for an ATM book
    when ``theta`` is the gamma-theta.  Comparable across pairs, which
    ``Gamma$/|theta|`` (units: days per percent) is not.
    """
    move_pct = 100.0 * float(realized_vol) / math.sqrt(CALENDAR_DAYS) * math.sqrt(days)
    num = gamma_pnl_pct(gamma_1pct, spot, move_pct)
    den = abs(float(theta) * days)
    return float(num / den) if den > 0 else float("nan")


def gamma_carry_expectancy(gamma_1pct: float, spot: float, sigma_r: float,
                           sigma_i: float, days: float = 1.0) -> float:
    """Expected net delta-hedged P&L (quote ccy) over ``days``.

    ``50 * G1 * S * (sigma_r^2 - sigma_i^2) * dt_years`` -- the W-5-corrected
    identity, implemented once in :func:`fxgamma.portfolio.risk.dhedge_pnl`.  This is
    the number that says whether owning this gamma is expected to pay: positive when
    you expect to realize more than you paid.

    It is an *expectation under continuous hedging*.  What you actually collect
    depends on your hedge frequency, which is why the Lab's frequency sweep
    (``backtest.strategies.hedge_frequency_sweep``) exists.
    """
    return dhedge_pnl(gamma_1pct, spot, sigma_r, sigma_i, days / CALENDAR_DAYS)


def assert_breakeven_identity(sigma: float = 0.0705, S: float = 1.0850,
                              T: float = 1.0 / 12.0, tol: float = 1e-9) -> float:
    """Self-test: ``BE% == 100 * sigma / sqrt(365)`` for an ATM straddle.

    Built from a real straddle through ``gk_greeks`` (not from algebra), so it also
    checks that ``gamma_1pct`` really is ``gamma * S / 100`` and that the gamma-theta
    definition is consistent with the pricer.  Raises ``AssertionError`` on failure and
    returns the relative error.  This is REQ-039's / s6.3's acceptance test, run at
    zero rates *and* -- see the QA fixture -- at realistic rates, where it still holds
    because the definition uses the gamma-theta, not the full theta (trader W-15).
    """
    err_max = 0.0
    for rd, rf in ((0.0, 0.0), (0.04, 0.02)):
        K = S * math.exp((rd - rf) * T + 0.5 * sigma * sigma * T)     # DNS strike
        c = gk.gk_greeks(S, K, T, rd, rf, sigma, +1, 10e6, +1)
        p = gk.gk_greeks(S, K, T, rd, rf, sigma, -1, 10e6, +1)
        g = c + p
        th = gamma_theta(g.gamma, S, sigma)
        be = breakeven_pct(th, g.gamma_1pct, S)
        want = 100.0 * sigma / math.sqrt(CALENDAR_DAYS)
        err = abs(be - want) / want
        assert err < tol, (f"breakeven identity broken at rd={rd}, rf={rf}: "
                           f"BE={be:.10f}% vs sigma/sqrt(365)={want:.10f}%")
        err_max = max(err_max, err)
    return err_max


# --------------------------------------------------------------------------- #
# RV vs IV
# --------------------------------------------------------------------------- #
def rv_iv_spread(history: pd.DataFrame, mkt: MarketSnapshot, pair: str, *,
                 expiry: date | None = None, calendar_days: int = 30,
                 method: str = "close_to_close", strike: float | None = None
                 ) -> dict[str, float | str]:
    """Implied minus realized, matched on **calendar days to the actual expiry** (W-8).

    The realized window is the trailing ``calendar_days`` of *calendar* time, which
    on a daily FX record is about ``calendar_days * 252/365`` rows -- so a 30-calendar-day
    option is compared to ~21 business days of returns, annualised on ``sqrt(252)``,
    and the output states both counts.  ``spread > 0`` means implied is above realized:
    gamma looks expensive.
    """
    if expiry is not None:
        calendar_days = max((expiry - mkt.asof.date()).days, 1)
    rows = max(int(round(calendar_days * ANNUAL / CALENDAR_DAYS)), 2)
    rv = realized_vol(history, method, rows)
    T = calendar_days / CALENDAR_DAYS
    surf = mkt.surfaces.get(pair)
    if surf is None:
        iv = float("nan")
    elif strike is not None:
        iv = float(surf.vol(float(strike), T))
    else:
        iv = float(surf.atm(T))
    return {"pair": pair, "calendar_days": int(calendar_days), "business_rows": rows,
            "rv": float(rv), "iv": float(iv), "spread": float(iv - rv),
            "spread_pts": float((iv - rv) * 100.0),
            "method": method, "rv_annualisation": f"sqrt({ANNUAL:g})",
            "matched_to": str(expiry) if expiry is not None else f"{calendar_days}cd"}


def zscore(series: pd.Series | Sequence[float], window: int = 252, *,
           overlap: int = 1, current: float | None = None) -> dict[str, float]:
    """Z-score with an overlapping-window correction and the effective ``n`` returned.

    ``overlap`` is the length of the rolling window the series was built from (1 for
    a genuinely daily series).  The naive standard error is widened by
    ``sqrt(overlap)``, which is the leading-order correction for the autocorrelation
    an overlapping estimator induces -- without it every z is inflated by about
    ``sqrt(window_length)`` (trader W-8).
    """
    s = pd.Series(series).dropna().astype(float)
    s = s.tail(int(window))
    n = int(s.size)
    if n < 3:
        return {"z": float("nan"), "z_naive": float("nan"), "mean": float("nan"),
                "sd": float("nan"), "n": n, "n_eff": 0.0, "percentile": float("nan")}
    x = float(current) if current is not None else float(s.iloc[-1])
    mu, sd = float(s.mean()), float(s.std(ddof=1))
    n_eff = n / float(max(overlap, 1))
    z_naive = (x - mu) / sd if sd > 0 else float("nan")
    z = z_naive / math.sqrt(max(overlap, 1)) if np.isfinite(z_naive) else float("nan")
    return {"z": float(z), "z_naive": float(z_naive), "mean": mu, "sd": sd,
            "n": n, "n_eff": float(n_eff),
            "percentile": float((s.to_numpy() <= x).mean() * 100.0)}


def quote_zscores(quote_history: pd.DataFrame, pair: str, *, window: int = 252,
                  cols: Sequence[str] = ("atm", "rr25", "bf25")) -> dict[str, float]:
    """ATM / RR25 / BF25 z-scores from a stored quote history.

    ``quote_history`` is the dev layer's stored snapshot series with columns
    ``date, pair`` plus the quote columns.  These are genuinely daily observations
    (one mark a day), so ``overlap=1``; the sign convention printed on the UI must
    say "RR > 0 = base-ccy calls over" (REQ-019).
    """
    h = quote_history[quote_history["pair"] == pair] if "pair" in quote_history else quote_history
    out: dict[str, float] = {}
    for c in cols:
        if c not in h.columns:
            out[f"{c}_z"] = float("nan")
            out[f"{c}_n"] = 0.0
            continue
        z = zscore(h[c], window=window, overlap=1)
        out[f"{c}"] = float(pd.Series(h[c]).dropna().iloc[-1]) if len(h[c].dropna()) else float("nan")
        out[f"{c}_z"] = z["z"]
        out[f"{c}_pctile"] = z["percentile"]
        out[f"{c}_n"] = float(z["n"])
    return out


def richness_table(mkt: MarketSnapshot, histories: Mapping[str, pd.DataFrame], *,
                   calendar_days: int = 30, method: str = "close_to_close",
                   quote_history: pd.DataFrame | None = None,
                   window: int = 252) -> pd.DataFrame:
    """Cross-pair richness with a rank column (REQ-010).

    One row per pair with the matched RV, the ATM implied at the same calendar
    horizon, the spread, its z-score where a quote history exists, and the RR/BF
    z-scores.  Pairs with a missing input are kept with ``nan`` and a ``reason``
    rather than dropped, so a hole in the data is visible instead of silent
    (architecture s7).
    """
    rows = []
    for pair, hist in histories.items():
        reason = ""
        try:
            r = rv_iv_spread(hist, mkt, pair, calendar_days=calendar_days, method=method)
        except Exception as exc:                                  # noqa: BLE001
            r = {"pair": pair, "rv": float("nan"), "iv": float("nan"),
                 "spread": float("nan"), "spread_pts": float("nan")}
            reason = str(exc)[:120]
        if pair not in mkt.surfaces:
            reason = reason or "no surface for this pair in the snapshot"
        row = dict(r)
        row["reason"] = reason
        if quote_history is not None:
            row.update(quote_zscores(quote_history, pair, window=window))
            sp = quote_history[quote_history["pair"] == pair] if "pair" in quote_history else quote_history
            if "spread" in sp.columns:
                row["spread_z"] = zscore(sp["spread"], window=window, overlap=1)["z"]
        rows.append(row)
    df = pd.DataFrame(rows)
    if len(df) and "spread_pts" in df:
        df["rank"] = df["spread_pts"].rank(ascending=False, method="min")
        df = df.sort_values("spread_pts", ascending=False, ignore_index=True)
    return df
