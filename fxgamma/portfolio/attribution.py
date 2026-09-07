"""Daily P&L attribution -- a full second-order explain with an honest residual.

``daily_pnl(book, mkt_t0, mkt_t1, hedges=None) -> PnLBreakdown`` (architecture s5,
REQ-051..REQ-057).

The expansion
-------------
Each position's value is ``V(S, sigma, r_d, r_f, t)``.  Between the two stamped
snapshots we Taylor-expand around the **t0** state -- t0 Greeks, t0 spot, t0 vols --
and attribute in this order (the order matters: each term is the derivative that the
previous terms have not already used)::

    dV  =  V_S dS                         -> delta
         + 1/2 V_SS dS^2                  -> gamma
         + V_t dt                         -> theta
         + V_sigma dsigma                 -> vega
         + V_S,sigma dS dsigma            -> vanna   (cross term, coefficient 1)
         + 1/2 V_sigma,sigma dsigma^2     -> volga
         + V_rd drd + V_rf drf            -> rates
         + carry (financing of spot legs) -> carry
         + hedge trades struck in-window  -> hedge
         + everything else                -> unexplained

In desk units (``fxgamma/models/gk.py``) that is, per position, in the position's
native quote ccy::

    delta  = delta_base_0 * dS
    gamma  = 0.5 * gamma_0 * dS^2                 (== 0.005 * G1 * S * x^2, x in %)
    theta  = theta_0 * dt_days                    (dt_days = ACTUAL elapsed, see below)
    vega   = vega_0   * dsigma / 0.01             (vega is per vol point)
    vanna  = vanna_0  * dS * dsigma / 0.01
    volga  = 0.5 * volga_0 * (dsigma / 0.01)^2
    rates  = rho_d_0 * drd / 0.01 + rho_f_0 * drf / 0.01

What is deliberately *not* separated, and therefore lands in ``unexplained``: charm
(``V_St``), veta (``V_sigma t``), speed (``V_SSS``), ultima, and every term of third
order or higher.  On a clean overnight move on a vanilla book they are worth well
under 1% of the total; when they are not, that is exactly the signal REQ-052 wants
you to see rather than a bar quietly absorbing model error.

Choices that the trader review makes non-negotiable
---------------------------------------------------
* **Elapsed time is wall-clock, not "a day" (W-14).**  ``dt_days`` is the actual
  interval between ``mkt_t0.asof`` and ``mkt_t1.asof``.  A 07:00 London mark is ~14
  hours after the 17:00 NY close; charging a full calendar day of theta puts the
  theta bar ~40% out and pushes the error into the residual, which then trips the
  residual alarm for the wrong reason and teaches the trader to ignore it.
* **``dsigma`` is per position, at its own strike, and includes rolldown.**
  ``dsigma_i = sigma_1(K_i, T_1) - sigma_0(K_i, T_0)``: the vol you are marked at
  today minus the vol you were marked at yesterday, at *your* strike.  Because
  ``T`` shrinks, this contains the term-structure rolldown, which is the market
  convention for a daily explain and keeps ``vega`` reconciling to the mark change
  a trader can see on the surface page.
* **Hedge carry is a line, not residual (MISS-7).**  A spot hedge is a T+2 position
  that must be rolled; on USDJPY at a ~3.5% differential, USD 100mm of hedge is
  ~USD 10k a day.  ``carry = N * S0 * (rf - rd) * dt_years`` for every spot leg in
  the book and for every in-window hedge, from its trade time where one is stamped.
  Because a spot leg's PV here is a pure mark-to-market against its entry rate,
  carry is *added* to ``total`` rather than being netted out of it -- so ``total``
  is the economic P&L, and closure still holds exactly.
* **Reporting ccy (CG-1).**  Components are computed per position in native quote
  ccy and converted at the **t1** rate, so ``total`` is "today's P&L at today's fx"
  and every component reconciles to it exactly.  No fx-translation P&L on the prior
  mark is invented; if you want that, diff two report-ccy PV series.

Closure
-------
``total = delta + gamma + theta + vega + vanna + volga + rates + carry + hedge +
unexplained`` holds to floating point **by construction**, because ``unexplained``
is defined as the difference.  The number that matters is therefore not the closure
but ``|unexplained| / sum|components|`` -- returned by :func:`residual_ratio` and
displayed always, not only when it breaches (trader Q-10.2).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ..conventions import PAIRS, pair_spec, year_fraction
from ..types import Book, MarketSnapshot, PnLBreakdown, SpotPosition
from .risk import fx_rate, price_book

__all__ = ["daily_pnl", "residual_ratio", "top_offenders", "COMPONENTS"]

COMPONENTS = ("delta", "gamma", "theta", "vega", "vanna", "volga",
              "rates", "carry", "hedge")

_VOL_PT = 0.01          # vega/vanna/volga are quoted per 1 vol point
_RATE_PT = 0.01         # rho_d / rho_f are quoted per 1 rate point (100bp)
_YEAR = 365.0


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def daily_pnl(book: Book, mkt_t0: MarketSnapshot, mkt_t1: MarketSnapshot,
              hedges: Sequence[SpotPosition] | None = None, *,
              report_ccy: str = "USD",
              marks_t0: Mapping[str, float] | None = None,
              marks_t1: Mapping[str, float] | None = None) -> PnLBreakdown:
    """Second-order P&L explain between two stamped snapshots.

    Parameters
    ----------
    book : the book as it stood at ``t0`` (options plus any pre-existing spot legs).
    hedges : spot trades struck **between** the snapshots.  Their P&L is measured
        from their own ``entry_rate`` to the t1 spot and reported in the ``hedge``
        bar, which is what makes the hedge log (REQ-055) reconcile.
    marks_t0 / marks_t1 : the CG-2 per-position mark-vol side-table at each date.

    Returns
    -------
    PnLBreakdown
        ``.detail`` is a per-position DataFrame with the same component split plus a
        per-position ``unexplained``, so REQ-052 can name the top three offenders
        when the residual is large (see :func:`top_offenders`).
    """
    t0, t1 = _utc(mkt_t0.asof), _utc(mkt_t1.asof)
    dt_days = (t1 - t0).total_seconds() / 86400.0
    dt_years = dt_days / _YEAR

    d0 = price_book(book, mkt_t0, report_ccy=report_ccy, marks=marks_t0)
    d1 = price_book(book, mkt_t1, report_ccy=report_ccy, marks=marks_t1)
    if len(d0):
        d1 = d1.set_index("id").reindex(d0["id"]).reset_index()

    rows: list[dict[str, Any]] = []
    for i in range(len(d0)):
        a, b = d0.iloc[i], d1.iloc[i]
        pair = str(a["pair"])
        spec = pair_spec(pair)
        fx = fx_rate(spec.quote, report_ccy, mkt_t1)
        S0, S1 = float(a["spot"]), float(b["spot"])
        dS = S1 - S0
        rd0, rf0 = mkt_t0.rd_rf(pair, PAIRS)
        rd1, rf1 = mkt_t1.rd_rf(pair, PAIRS)
        total = float(b["pv"]) - float(a["pv"])
        rec: dict[str, Any] = {
            "id": a["id"], "pair": pair, "kind": a["kind"], "tag": a["tag"],
            "expiry": a["expiry"], "strike": a["strike"], "ccy": spec.quote,
            "fx_to_report": fx, "spot_t0": S0, "spot_t1": S1, "dS": dS,
            "dt_days": dt_days, "pv_t0": float(a["pv"]), "pv_t1": float(b["pv"]),
            "expired_t1": bool(b["expired"]),
        }
        if a["kind"] == "spot":
            N = float(a["signed_notional"])
            carry = N * S0 * (rf0 - rd0) * dt_years
            rec.update({c: 0.0 for c in COMPONENTS})
            rec["delta"] = N * dS
            rec["carry"] = carry
            rec["dvol"] = 0.0
            rec["total"] = total + carry
        elif bool(a["expired"]):
            # already dead at t0 -- it contributes nothing and must not be explained
            rec.update({c: 0.0 for c in COMPONENTS})
            rec["dvol"] = 0.0
            rec["total"] = total
        else:
            sig0, sig1 = float(a["vol"]), float(b["vol"])
            dvol = 0.0 if (bool(b["expired"]) or not np.isfinite(sig1)) else sig1 - sig0
            dvp = dvol / _VOL_PT
            rec.update({
                "delta": float(a["delta_base"]) * dS,
                "gamma": 0.5 * float(a["gamma"]) * dS * dS,
                "theta": float(a["theta"]) * dt_days,
                "vega": float(a["vega"]) * dvp,
                "vanna": float(a["vanna"]) * dS * dvp,
                "volga": 0.5 * float(a["volga"]) * dvp * dvp,
                "rates": (float(a["rho_d"]) * (rd1 - rd0) / _RATE_PT
                          + float(a["rho_f"]) * (rf1 - rf0) / _RATE_PT),
                "carry": 0.0, "hedge": 0.0,
                "dvol": dvol, "vol_t0": sig0, "vol_t1": sig1,
                "total": total,
            })
        rec["unexplained"] = rec["total"] - sum(rec[c] for c in COMPONENTS)
        rows.append(rec)

    for h in (hedges or []):
        spec = pair_spec(h.pair)
        fx = fx_rate(spec.quote, report_ccy, mkt_t1)
        S1 = float(mkt_t1.spot[h.pair])
        S0 = float(mkt_t0.spot.get(h.pair, S1))
        rd0, rf0 = mkt_t0.rd_rf(h.pair, PAIRS)
        N = float(h.notional_base)
        held = dt_years
        if h.trade_time is not None:
            held = max((t1 - _utc(h.trade_time)).total_seconds() / 86400.0, 0.0) / _YEAR
        pnl = N * (S1 - float(h.entry_rate))
        carry = N * float(h.entry_rate) * (rf0 - rd0) * held
        rec = {"id": h.id, "pair": h.pair, "kind": "hedge", "tag": h.tag or "hedge",
               "expiry": None, "strike": float(h.entry_rate), "ccy": spec.quote,
               "fx_to_report": fx, "spot_t0": S0, "spot_t1": S1, "dS": S1 - S0,
               "dt_days": held * _YEAR, "pv_t0": 0.0, "pv_t1": pnl,
               "expired_t1": False, "dvol": 0.0}
        rec.update({c: 0.0 for c in COMPONENTS})
        rec["hedge"] = pnl
        rec["carry"] = carry
        rec["total"] = pnl + carry
        rec["unexplained"] = 0.0
        rows.append(rec)

    cols = list(COMPONENTS) + ["total", "unexplained"]
    if not rows:
        detail = pd.DataFrame(columns=["id", "pair", "kind"] + cols)
        return PnLBreakdown(ccy=report_ccy.upper(), detail=detail)

    det = pd.DataFrame(rows)
    for c in cols:
        det[f"{c}_rep"] = det[c].astype(float) * det["fx_to_report"].astype(float)
    det["abs_unexplained_rep"] = det["unexplained_rep"].abs()

    out = PnLBreakdown(ccy=report_ccy.upper(), detail=det)
    for c in COMPONENTS:
        setattr(out, c, float(det[f"{c}_rep"].sum()))
    out.total = float(det["total_rep"].sum())
    out.unexplained = out.total - sum(getattr(out, c) for c in COMPONENTS)
    return out


def residual_ratio(pnl: PnLBreakdown) -> float:
    """``|unexplained| / sum|components|`` -- REQ-052's policing number.

    Display it every day, not only when it breaches: a threshold that only speaks
    once it is already broken teaches nothing (trader Q-10.2).  On a clean vanilla
    book with a good mark this should be under 1% overnight and under 2-3% on a day
    with a big smile move.
    """
    denom = sum(abs(getattr(pnl, c)) for c in COMPONENTS)
    return float(abs(pnl.unexplained) / denom) if denom > 0 else 0.0


def top_offenders(pnl: PnLBreakdown, n: int = 3) -> pd.DataFrame:
    """The ``n`` positions contributing most to the residual (REQ-052 diagnostic)."""
    det = pnl.detail
    if det is None or not len(det):
        return pd.DataFrame(columns=["id", "pair", "unexplained_rep"])
    cols = [c for c in ("id", "pair", "kind", "strike", "expiry", "dS", "dvol",
                        "unexplained_rep", "total_rep") if c in det.columns]
    return (det.reindex(det["abs_unexplained_rep"].sort_values(ascending=False).index)
            .head(int(n))[cols].reset_index(drop=True))
