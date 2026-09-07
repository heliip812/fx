"""Page 1 - Market Monitor (`/`).

The morning screen (trader review section 3b): in the first thirty seconds it must answer
*did anything gap overnight*, *is gamma cheap or expensive today*, and *where*.  So the
top strip is one sentence per G3 pair, and everything below it is the evidence.

Honesty rules applied here:
* implied vs realized is matched on the **actual calendar days to expiry** (W-8), and the
  matched window is printed next to the number;
* RV annualises on sqrt(252) and says so on the chart (REQ-008);
* the sign convention for the spread is written on the panel (REQ-009);
* a z-score that needs a stored implied history renders "—" with the reason, never a
  number computed off a proxy the user cannot see (REQ-010, review Q-10).
"""
from __future__ import annotations

import logging
import math

import dash
from dash import Input, Output, callback, dcc, html

from fxgamma.conventions import G3, PAIRS, pair_spec

from .. import analytics as A
from ..components.badges import kind_of, provenance_badge, surface_status, synthetic_banner
from ..components.cards import empty_state, grid, metric, metric_row, note, panel
from ..components.charts import figure, zero_line
from ..components.fmt import (EM_DASH, fmt_pct, fmt_spot, fmt_vol, fmt_vol_pts, move_both,
                              pips_between)
from ..components.tables import col, data_table
from ..state import ALL_PAIRS, get_session
from ..theme import ACCENT, KIND_COLORS, NEG, POS, SERIES, TEXT_DIM, WARN, empty_figure

log = logging.getLogger(__name__)

dash.register_page(__name__, path="/", name="Market Monitor", title="FX Gamma · Market")

RV_WINDOWS = [10, 21, 63]


def _iv_at(snap, pair: str, days: int) -> float | None:
    surf = snap.surfaces.get(pair)
    if surf is None or days <= 0:
        return None
    try:
        v = float(surf.atm(days / 365.0))
        return v if math.isfinite(v) else None
    except Exception:                                      # noqa: BLE001
        return None


def _row(session, snap, pair: str, window: int, estimator: str) -> dict:
    hist = session.history(pair)
    spec = pair_spec(pair)
    S = snap.spot.get(pair)
    prev = None
    if hist is not None and not hist.empty and len(hist) > 1:
        prev = float(hist["close"].iloc[-2])
    rv = A.realized_vol(hist, window, estimator)
    cal_days = int(round(window * 365 / A.BASIS))
    iv = _iv_at(snap, pair, cal_days)
    spread = (iv - rv) if (iv is not None and rv is not None) else None
    rvs = A.rv_series(hist, window, "close_to_close")
    z, n = A.zscore(rvs, rv)
    status, _ = surface_status(snap.meta, pair)
    return {
        "pair": pair, "spot": S, "prev": prev,
        "chg": move_both(S, prev, pair) if (S and prev) else EM_DASH,
        "chg_pips": pips_between(S, prev, pair) if (S and prev) else None,
        "r1": A.returns(hist, 1), "r5": A.returns(hist, 5), "r21": A.returns(hist, 21),
        "rv": rv, "iv": iv, "spread": spread, "rv_z": z, "rv_n": n,
        "window": window, "cal_days": cal_days, "estimator": estimator,
        "spot_kind": kind_of(snap.meta, f"spot.{pair}"),
        "surface_kind": kind_of(snap.meta, f"surface.{pair}"),
        "status": status, "quote": spec.quote,
    }


# ------------------------------------------------------------------ top strip
def _headline(r: dict) -> html.Div:
    pair = r["pair"]
    if r["spread"] is None:
        verdict, tone = "no comparable mark", ""
    elif r["spread"] > 0:
        verdict, tone = "implied OVER realized — gamma expensive", "neg"
    else:
        verdict, tone = "implied UNDER realized — gamma cheap", "pos"
    sub = (f"RV{r['window']}bd/{r['cal_days']}cd {fmt_vol(r['rv'])} vs ATM "
           f"{fmt_vol(r['iv'])} = {fmt_vol_pts(r['spread'])}")
    return metric(
        f"{pair}  ·  {r['status']}",
        f"{fmt_spot(r['spot'], pair)}   {r['chg']}",
        unit=verdict, tone=tone, sub=sub,
        hint=(f"spread = implied − realized, in vol points. Positive = implied over "
              f"realized = gamma expensive. RV window matched to {r['cal_days']} "
              f"calendar days (W-8); annualised on sqrt(252)."),
        badge=provenance_badge(None, "x", compact=True) if False else None)


def layout(**_kw):                                          # noqa: D401
    s = get_session()
    snap = s.snapshot()
    rows = []
    for p in ALL_PAIRS:
        try:
            rows.append(_row(s, snap, p, 21, "close_to_close"))
        except Exception as exc:                            # noqa: BLE001
            log.warning("market row %s failed: %s", p, exc)
    g3 = [r for r in rows if r["pair"] in G3]

    spot_cols = [
        col("pair"), col("spot", unit="price"), col("chg", unit="vs prev close"),
        col("1d", "r1", unit="%", numeric=True), col("5d", "r5", unit="%", numeric=True),
        col("21d", "r21", unit="%", numeric=True),
        col("RV21", "rv_s", unit="ann. %, sqrt(252)"),
        col("ATM", "iv_s", unit="matched tenor, %"),
        col("spread", "spread_s", unit="vol pts, +=expensive"),
        col("RV z", "z_s", unit="1y, n shown"),
        col("spot src", "spot_kind"), col("surface", "status"),
    ]
    spot_data = [{
        "pair": r["pair"], "spot": fmt_spot(r["spot"], r["pair"]), "chg": r["chg"],
        "r1": None if r["r1"] is None else round(r["r1"], 2),
        "r5": None if r["r5"] is None else round(r["r5"], 2),
        "r21": None if r["r21"] is None else round(r["r21"], 2),
        "rv_s": fmt_vol(r["rv"]), "iv_s": fmt_vol(r["iv"]),
        "spread_s": fmt_vol_pts(r["spread"]),
        "z_s": (EM_DASH if r["rv_z"] is None else f"{r['rv_z']:+.2f} (n={r['rv_n']})"),
        "spot_kind": r["spot_kind"], "status": r["status"],
    } for r in rows]

    kind_rules = [
        {"if": {"filter_query": '{spot_kind} = "synthetic"', "column_id": "spot_kind"},
         "color": KIND_COLORS["synthetic"], "fontWeight": "700"},
        {"if": {"filter_query": '{spot_kind} = "live"', "column_id": "spot_kind"},
         "color": KIND_COLORS["live"]},
        {"if": {"filter_query": '{spot_kind} = "cached"', "column_id": "spot_kind"},
         "color": KIND_COLORS["cached"]},
        {"if": {"filter_query": '{spot_kind} = "user_override"', "column_id": "spot_kind"},
         "color": KIND_COLORS["user_override"], "fontWeight": "700"},
        {"if": {"filter_query": '{status} = "MARK"', "column_id": "status"},
         "color": KIND_COLORS["user_override"], "fontWeight": "700"},
        {"if": {"filter_query": '{status} = "INDICATIVE"', "column_id": "status"},
         "color": KIND_COLORS["cached"]},
        {"if": {"filter_query": "{r1} < 0", "column_id": "r1"}, "color": NEG},
        {"if": {"filter_query": "{r1} > 0", "column_id": "r1"}, "color": POS},
    ]

    return html.Div([
        synthetic_banner(snap.meta, page="market"),
        panel(metric_row([_headline(r) for r in g3],
                         cols="repeat(auto-fit, minmax(330px, 1fr))")
              if g3 else empty_state("no G3 pairs in this snapshot"),
              title="the first thirty seconds",
              sub="did anything gap · is gamma cheap or expensive · on what basis"),

        panel([
            data_table("mkt-spot-table", spot_cols, spot_data, page_size=14,
                       conditional=kind_rules,
                       tooltips={
                           "spread_s": "implied − realized, vol points. Positive = implied "
                                       "over realized = gamma expensive.",
                           "rv_s": "close-to-close, annualised sqrt(252), window matched "
                                   "to the option's calendar days (W-8)",
                           "status": "MARK = your own vol grid. INDICATIVE = listed/proxy "
                                     "or simulated vols: richness only, never a hedge mark.",
                       }),
            note("Moves are shown in pips and percent together (section 5.4). JPY pairs use "
                 "pip = 0.01 and 3 dp; everything else pip = 0.0001 and 5 dp. A blank "
                 "cell is '—' and never 0."),
        ], title="spot, returns and richness", sub="REQ-007 / REQ-009 / REQ-010"),

        grid([
            panel([
                html.Div([
                    html.Div([html.Label("pair"),
                              dcc.Dropdown(id="mkt-pair", options=ALL_PAIRS,
                                           value=s.setting("pair", "EURUSD"),
                                           clearable=False)],
                             style={"minWidth": "150px"}),
                    html.Div([html.Label("estimators"),
                              dcc.Checklist(
                                  id="mkt-estimators",
                                  options=[{"label": " " + e.replace("_", " "), "value": e}
                                           for e in A.ESTIMATORS],
                                  value=list(A.DEFAULT_ESTIMATORS),
                                  inline=True,
                                  labelStyle={"marginRight": "12px", "fontSize": "11px"})],
                             style={"flex": "1"}),
                    html.Div([html.Label("window (business days)"),
                              dcc.Dropdown(id="mkt-window", options=RV_WINDOWS, value=21,
                                           clearable=False)],
                             style={"minWidth": "120px"}),
                ], className="row"),
                dcc.Loading(dcc.Graph(id="mkt-rv-fig", config={"displaylogo": False}),
                            type="dot", color=ACCENT),
                html.Div(id="mkt-rv-note"),
            ], title="realized vs implied", sub="REQ-008 — multiple estimators, one basis"),

            panel(_events_panel(s), title="event calendar",
                  sub="REQ-012 — short gamma into an event is a hazard, not a preference"),
        ], cols="3fr 2fr"),
    ])


def _events_panel(s):
    df = s.events(45)
    if df is None or df.empty:
        return empty_state("no event calendar available",
                           "data/calendar/events.csv feeds this (amendment v1.1 CG-6)")
    today = s.snapshot().asof.date()
    df = df.copy()
    try:
        df["days"] = [(d.date() - today).days for d in df["datetime"]]
    except Exception:                                      # noqa: BLE001
        df["days"] = None
    df = df[df["days"] >= 0].head(12)
    data = [{"when": f"{r['datetime']:%a %d %b %H:%MZ}", "days": r["days"],
             "ccy": r["ccy"], "event": r["event"], "imp": int(r["importance"])}
            for _, r in df.iterrows()]
    return data_table("mkt-events", [col("when"), col("days", unit="to go", numeric=True),
                                     col("ccy"), col("event"), col("imp", unit="1-3",
                                                                   numeric=True)],
                      data, page_size=12, sort=False,
                      conditional=[{"if": {"filter_query": "{imp} = 3"},
                                    "color": WARN, "fontWeight": "700"}])


@callback(Output("mkt-rv-fig", "figure"), Output("mkt-rv-note", "children"),
          Input("mkt-pair", "value"), Input("mkt-estimators", "value"),
          Input("mkt-window", "value"), Input("snapshot-token", "data"))
def _rv_figure(pair, estimators, window, _token):
    s = get_session()
    try:
        pair = pair or "EURUSD"
        window = int(window or 21)
        estimators = estimators or list(A.DEFAULT_ESTIMATORS)
        s.set_setting("pair", pair)
        snap = s.snapshot()
        hist = s.history(pair)
        if hist is None or hist.empty:
            return (empty_figure(f"no spot history for {pair}"),
                    note(f"the provider returned no history for {pair}.", tone="warn"))
        traces = []
        missing_ohlc = not A.has_ohlc(hist)
        for i, est in enumerate(estimators):
            ser = A.rv_series(hist, window, est)
            if ser.empty:
                continue
            traces.append(dict(type="scatter", mode="lines", name=est.replace("_", " "),
                               x=list(ser.index), y=[v * 100 for v in ser.values],
                               line=dict(width=1.4, color=SERIES[i % len(SERIES)]),
                               hovertemplate="%{x|%Y-%m-%d}<br>%{y:.2f}% ann<extra>"
                                             + est + "</extra>"))
        cal_days = int(round(window * 365 / A.BASIS))
        iv = _iv_at(snap, pair, cal_days)
        if iv is not None and traces:
            xs = list(traces[0]["x"])
            traces.append(dict(type="scatter", mode="lines", name=f"ATM implied ({cal_days}cd)",
                               x=[xs[0], xs[-1]], y=[iv * 100, iv * 100],
                               line=dict(width=1.6, color="#f0883e", dash="dash"),
                               hovertemplate="ATM %{y:.2f}%<extra>implied, today</extra>"))
        if not traces:
            return (empty_figure(f"not enough history for a {window}d window"),
                    note("pick a shorter window or another pair."))
        fig = figure(traces,
                     title=f"{pair} realized vol, {window} business-day window "
                           f"(annualised sqrt(252)) vs today's ATM",
                     xtitle="date", ytitle="annualised vol, %", height=320)
        status, why = surface_status(snap.meta, pair)
        msg = [f"RV window {window} business days ≈ {cal_days} calendar days, matched to the "
               f"option tenor (trader review W-8). Implied line is today's ATM at "
               f"{cal_days} calendar days — a level, not a history: there is no stored "
               "implied series yet, so a spread z-score would be a guess and is shown as "
               "'—' in the table above.",
               f" Surface status: {status} — {why}."]
        if missing_ohlc:
            msg.append(" This source has no intraday range, so range estimators degrade to "
                       "close-to-close (REQ-008).")
        formulas = " · ".join(f"{e}: {A.ESTIMATOR_FORMULA[e]}" for e in estimators
                              if e in A.ESTIMATOR_FORMULA)
        return fig, html.Div([note("".join(msg)), note(formulas, tone="faint")])
    except Exception as exc:                               # noqa: BLE001
        log.exception("rv figure failed")
        return (empty_figure("realized-vol panel failed"),
                note(f"realized-vol panel failed: {exc}", tone="warn"))
