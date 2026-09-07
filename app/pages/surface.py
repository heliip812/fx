"""Page 2 - Vol Surface (`/surface`).

Built strictly through the frozen section 8 boundary: the provider (or the desk's own
manual grid) produces ``list[SmileQuotes]``, ``build_surface`` turns it into a
``VolSurface``, and nothing else crosses.

REQ-016 is the reason the residual table sits above the pretty 3-D chart: **a model is
never shown as valid without the reproduction error on its own inputs.**  Any tenor
missing the ATM / RR25 / BF25 it was built from by more than 0.10 vol points is amber
and more than 0.25 is red.

The what-if pricer (REQ-021, promoted to Must by the trader review) is here because it
needs no book and no state, and answers "what does this cost and how much gamma do I
get" twenty times a day.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import dash
import numpy as np
from dash import Input, Output, callback, dcc, html

from fxgamma.conventions import PAIRS, TENORS, pair_spec, tenor_years
from fxgamma.models.surface import build_surface
from fxgamma.store import parse_strike
from fxgamma.types import Book, OptionPosition

from .. import analytics as A
from ..components.badges import (provenance_badge, surface_status, surface_status_badge,
                                 synthetic_banner)
from ..components.cards import empty_state, grid, metric, metric_row, note, panel
from ..components.charts import figure, spot_line
from ..components.fmt import (EM_DASH, fmt_mm, fmt_money, fmt_spot, fmt_vol,
                              fmt_vol_pts, greek_unit)
from ..components.tables import col, data_table
from ..pricing import (DISTANCE_BASIS, aggregate, breakeven_daily_pct,
                       price_positions, sigma_day_move)
from ..state import ALL_PAIRS, get_session
from ..theme import ACCENT, DIVERGING, NEG, SERIES, WARN, empty_figure

log = logging.getLogger(__name__)

dash.register_page(__name__, path="/surface", name="Vol Surface", title="FX Gamma · Surface")

METHODS = ["vanna_volga", "sabr", "interp"]
SLICE_TENORS = ["1W", "1M", "3M", "6M", "1Y"]


def layout(**_kw):
    s = get_session()
    pair = s.setting("pair", "EURUSD")
    return html.Div([
        html.Div(id="surf-banner"),
        panel([html.Div([
            html.Div([html.Label("pair"),
                      dcc.Dropdown(id="surf-pair", options=ALL_PAIRS, value=pair,
                                   clearable=False)], style={"minWidth": "150px"}),
            html.Div([html.Label("method (contract section 8)"),
                      dcc.Dropdown(id="surf-method", options=METHODS,
                                   value=s.setting("surface_method", "vanna_volga"),
                                   clearable=False)], style={"minWidth": "170px"}),
            html.Div(id="surf-status", style={"alignSelf": "center", "paddingTop": "14px"}),
        ], className="row"),
            note("The method selector re-fits THIS page's surface only. The book prices "
                 "off the snapshot's method (Data & Settings) — refitting every pair on "
                 "each click would blow the 400 ms per-surface budget twelve times over.")],
              title="surface", sub="REQ-015 — build, switch method, re-render"),

        html.Div(id="surf-residuals"),
        grid([html.Div(dcc.Loading(dcc.Graph(id="surf-3d", config={"displaylogo": False}),
                                   type="dot", color=ACCENT)),
              html.Div(dcc.Loading(dcc.Graph(id="surf-smile", config={"displaylogo": False}),
                                   type="dot", color=ACCENT))], cols="1fr 1fr"),
        grid([html.Div(dcc.Graph(id="surf-term", config={"displaylogo": False})),
              html.Div(dcc.Graph(id="surf-cone", config={"displaylogo": False}))],
             cols="1fr 1fr"),
        panel([
            html.Div([
                html.Div([html.Label("strike (any format of section 3.3)"),
                          dcc.Input(id="wi-strike", type="text", value="ATMF",
                                    debounce=True)], style={"minWidth": "180px"}),
                html.Div([html.Label("tenor"),
                          dcc.Dropdown(id="wi-tenor", options=list(TENORS), value="1M",
                                       clearable=False)], style={"minWidth": "110px"}),
                html.Div([html.Label("call / put"),
                          dcc.Dropdown(id="wi-cp", options=[{"label": "Call", "value": 1},
                                                            {"label": "Put", "value": -1}],
                                       value=1, clearable=False)],
                         style={"minWidth": "110px"}),
                html.Div([html.Label("notional (base)"),
                          dcc.Input(id="wi-notional", type="text", value="1mm",
                                    debounce=True)], style={"minWidth": "130px"}),
            ], className="row"),
            html.Div(id="wi-out", style={"marginTop": "10px"}),
        ], title="what-if pricer", sub="REQ-021 — no book, no state, no persistence"),
    ])


# ------------------------------------------------------------------ residuals
def _residual_rows(surf, quotes) -> list[dict]:
    rows = []
    for q in quotes:
        r = {"tenor": q.tenor or f"{q.T:.4f}y", "T": round(float(q.T), 5),
             "atm_in": q.atm, "rr_in": q.rr25, "bf_in": q.bf25}
        try:
            r["atm_out"] = float(surf.atm(q.T))
            r["rr_out"] = float(surf.rr(q.T, 0.25))
            r["bf_out"] = float(surf.bf(q.T, 0.25))
        except Exception as exc:                           # noqa: BLE001
            r["error"] = str(exc)[:80]
            rows.append(r)
            continue
        r["d_atm"] = (r["atm_out"] - q.atm) * 100
        r["d_rr"] = (r["rr_out"] - q.rr25) * 100
        r["d_bf"] = (r["bf_out"] - q.bf25) * 100
        r["worst"] = max(abs(r["d_atm"]), abs(r["d_rr"]), abs(r["d_bf"]))
        rows.append(r)
    return rows


@callback(Output("surf-banner", "children"), Output("surf-status", "children"),
          Output("surf-residuals", "children"), Output("surf-3d", "figure"),
          Output("surf-smile", "figure"), Output("surf-term", "figure"),
          Output("surf-cone", "figure"),
          Input("surf-pair", "value"), Input("surf-method", "value"),
          Input("snapshot-token", "data"))
def _render(pair, method, _token):
    s = get_session()
    pair = (pair or "EURUSD").upper()
    method = method or "vanna_volga"
    try:
        s.set_setting("pair", pair)
        snap = s.snapshot()
        banner = synthetic_banner(snap.meta, page="surface")
        quotes = s.smile_quotes(pair)
        surf = snap.surfaces.get(pair)
        if method != s.setting("surface_method", "vanna_volga") and quotes:
            # Re-fit ONLY the displayed pair (REQ-015: change method, re-render, no page
            # reload).  Refitting the whole snapshot here would restamp every pair and
            # blow the 400 ms/surface budget many times over, so the *book* keeps pricing
            # on the snapshot's method - set that on the Data page - and this selector
            # is a display fit.  The panel says so.
            try:
                S = snap.spot.get(pair)
                rd, rf = snap.rd_rf(pair, PAIRS)
                surf = build_surface(pair, snap.asof, quotes, float(S), rd, rf,
                                     method=method)
            except Exception as exc:                       # noqa: BLE001
                return (banner, note(f"{method} fit failed for {pair}: {exc}", tone="warn"),
                        "", empty_figure("fit failed"), empty_figure("fit failed"),
                        empty_figure("fit failed"), empty_figure("fit failed"))
        status, why = surface_status(snap.meta, pair)
        status_bar = html.Div([
            surface_status_badge(snap.meta, pair),
            provenance_badge(snap.meta, f"surface.{pair}"),
            html.Span(why, className="tiny dim", style={"marginLeft": "8px"}),
        ])
        if surf is None:
            msg = f"no surface for {pair} in this snapshot"
            return (banner, status_bar,
                    panel(empty_state(msg, "check the Data page source board"),
                          title="calibration residuals"),
                    empty_figure(msg), empty_figure(msg), empty_figure(msg),
                    empty_figure(msg))

        rows = _residual_rows(surf, quotes)
        res_panel = _residual_panel(rows, method, pair, snap)
        S = snap.spot.get(pair)
        return (banner, status_bar, res_panel,
                _fig_3d(surf, pair, S, method), _fig_smile(surf, pair, S, snap),
                _fig_term(surf, quotes, pair), _fig_cone(s, pair, surf))
    except Exception as exc:                               # noqa: BLE001
        log.exception("surface page failed")
        e = empty_figure("surface page failed")
        return (note(f"surface page failed: {exc}", tone="warn"), "", "", e, e, e, e)


def _residual_panel(rows, method, pair, snap):
    if not rows:
        return panel(empty_state("no input quotes behind this surface",
                                 "the provider returned no SmileQuotes for this pair"),
                     title="calibration residuals", sub="REQ-016")
    data = [{
        "tenor": r["tenor"], "T": r["T"],
        "atm_in": fmt_vol(r["atm_in"]), "atm_out": fmt_vol(r.get("atm_out")),
        "d_atm": None if "d_atm" not in r else round(r["d_atm"], 3),
        "rr_in": fmt_vol_pts(r["rr_in"]), "d_rr": None if "d_rr" not in r
        else round(r["d_rr"], 3),
        "bf_in": fmt_vol_pts(r["bf_in"]), "d_bf": None if "d_bf" not in r
        else round(r["d_bf"], 3),
        "flag": ("ERROR" if "error" in r else
                 "RED" if r.get("worst", 0) > 0.25 else
                 "AMBER" if r.get("worst", 0) > 0.10 else "ok"),
    } for r in rows]
    worst = max((r.get("worst", 0.0) for r in rows), default=0.0)
    cond = [
        {"if": {"filter_query": '{flag} = "RED"'}, "color": NEG, "fontWeight": "700"},
        {"if": {"filter_query": '{flag} = "AMBER"'}, "color": WARN},
        {"if": {"filter_query": '{flag} = "ERROR"'}, "color": NEG, "fontWeight": "700"},
    ]
    return panel([
        data_table("surf-res", [
            col("tenor"), col("T", unit="years", numeric=True),
            col("ATM in", "atm_in"), col("ATM out", "atm_out"),
            col("ΔATM", "d_atm", unit="vol pts", numeric=True),
            col("RR25 in", "rr_in"), col("ΔRR", "d_rr", unit="vol pts", numeric=True),
            col("BF25 in", "bf_in"), col("ΔBF", "d_bf", unit="vol pts", numeric=True),
            col("flag")], data, page_size=12, sort=False, conditional=cond),
        note(f"Reproduction error of the {method} fit against its own input quotes, in vol "
             f"points. Amber above 0.10, red above 0.25 (REQ-016). Worst residual on this "
             f"surface: {worst:.3f} vol pts. On a EUR 100mm 1M straddle one vol point is "
             f"about USD 248,000 of PV — residuals are money, not housekeeping."),
    ], title="calibration residuals", sub="REQ-016 — never valid without them")


# ------------------------------------------------------------------ figures
def _strike_grid(surf, S, pair, n=61, width=0.22):
    return np.linspace(S * (1 - width), S * (1 + width), n)


def _fig_3d(surf, pair, S, method):
    if S is None:
        return empty_figure("no spot for this pair")
    Ts = [tenor_years(t) for t in SLICE_TENORS]
    Ks = _strike_grid(surf, S, pair, 41)
    Z = []
    for T in Ts:
        try:
            Z.append([float(v) * 100 for v in surf.slice(T, Ks)])
        except Exception:                                  # noqa: BLE001
            Z.append([float("nan")] * len(Ks))
    fig = {"data": [dict(type="surface", x=list(Ks), y=SLICE_TENORS, z=Z,
                         colorscale=DIVERGING, showscale=True,
                         colorbar=dict(title="vol %", tickfont=dict(size=9)),
                         hovertemplate="K %{x:.4f}<br>%{y}<br>vol %{z:.2f}%<extra></extra>")],
           "layout": {}}
    from ..theme import base_layout
    fig["layout"] = base_layout(
        height=340, title=dict(text=f"{pair} surface ({method}) — strike x tenor x vol %"),
        scene=dict(xaxis=dict(title="strike", color="#9aa7b4", gridcolor="#222b36",
                              backgroundcolor="#161b22"),
                   yaxis=dict(title="tenor", color="#9aa7b4", gridcolor="#222b36",
                              backgroundcolor="#161b22"),
                   zaxis=dict(title="vol, %", color="#9aa7b4", gridcolor="#222b36",
                              backgroundcolor="#161b22")))
    return fig


def _fig_smile(surf, pair, S, snap):
    if S is None:
        return empty_figure("no spot for this pair")
    spec = pair_spec(pair)
    traces = []
    for i, t in enumerate(SLICE_TENORS):
        T = tenor_years(t)
        Ks = _strike_grid(surf, S, pair)
        try:
            vols = [float(v) * 100 for v in surf.slice(T, Ks)]
        except Exception:                                  # noqa: BLE001
            continue
        deltas = []
        for K in Ks:
            try:
                from fxgamma.models.gk import delta_from_strike
                rd, rf = snap.rd_rf(pair, PAIRS)
                sig = float(surf.vol(K, T))
                deltas.append(float(delta_from_strike(K, S, T, rd, rf, sig, 1,
                                                      spec.delta_convention)) * 100)
            except Exception:                              # noqa: BLE001
                deltas.append(float("nan"))
        cust = [[d, (K / S - 1) * 100, (K - S) / spec.pip] for d, K in zip(deltas, Ks)]
        traces.append(dict(
            type="scatter", mode="lines", name=t, x=list(Ks), y=vols,
            customdata=cust, line=dict(width=1.6, color=SERIES[i % len(SERIES)]),
            hovertemplate=("K %{x:.5f}<br>vol %{y:.3f}%<br>"
                           "call delta %{customdata[0]:.1f}% ("
                           + spec.delta_convention + ")<br>"
                           "%{customdata[1]:+.2f}% of spot · %{customdata[2]:+,.0f} pips"
                           "<extra>" + t + "</extra>")))
    if not traces:
        return empty_figure("smile could not be evaluated")
    fig = figure(traces, title=f"{pair} smile by tenor — delta in {spec.delta_convention}",
                 xtitle=f"strike ({spec.quote} per {spec.base})", ytitle="vol, %",
                 height=340)
    return spot_line(fig, S)


def _fig_term(surf, quotes, pair):
    Ts = sorted({float(q.T) for q in quotes}) or [tenor_years(t) for t in TENORS]
    labels = [q.tenor or f"{q.T:.3f}" for q in sorted(quotes, key=lambda q: q.T)] or \
        [f"{t:.3f}" for t in Ts]
    atm, rr, bf = [], [], []
    for T in Ts:
        try:
            atm.append(float(surf.atm(T)) * 100)
            rr.append(float(surf.rr(T, 0.25)) * 100)
            bf.append(float(surf.bf(T, 0.25)) * 100)
        except Exception:                                  # noqa: BLE001
            atm.append(float("nan")); rr.append(float("nan")); bf.append(float("nan"))
    traces = [
        dict(type="scatter", mode="lines+markers", name="ATM", x=labels, y=atm,
             line=dict(color=SERIES[0], width=1.8),
             hovertemplate="%{x}: ATM %{y:.3f}%<extra></extra>"),
        dict(type="scatter", mode="lines+markers", name="RR25", x=labels, y=rr,
             yaxis="y2", line=dict(color=SERIES[1], width=1.4),
             hovertemplate="%{x}: RR25 %{y:+.3f} vol pts<extra></extra>"),
        dict(type="scatter", mode="lines+markers", name="BF25", x=labels, y=bf,
             yaxis="y2", line=dict(color=SERIES[2], width=1.4, dash="dot"),
             hovertemplate="%{x}: BF25 %{y:.3f} vol pts<extra></extra>"),
    ]
    fig = figure(traces, title=f"{pair} term structure — ATM level and 25d skew/convexity",
                 xtitle="tenor", ytitle="ATM vol, % (left axis)", height=300)
    fig["layout"]["yaxis2"] = dict(title=dict(text="RR25 / BF25, vol pts (right axis)",
                                              font=dict(color="#9aa7b4", size=11)),
                                   overlaying="y", side="right", gridcolor="#222b36",
                                   zerolinecolor="#2b3440",
                                   tickfont=dict(color="#9aa7b4", size=11))
    fig["layout"]["annotations"] = [dict(
        x=0, y=1.14, xref="paper", yref="paper", showarrow=False,
        text="RR > 0 = base-ccy calls over puts; BF > 0 = wings over the ATM",
        font=dict(color="#6e7d8d", size=10))]
    return fig


def _fig_cone(s, pair, surf):
    hist = s.history(pair)
    if hist is None or hist.empty:
        return empty_figure(f"no history for {pair} — cone unavailable")
    c = A.cone(hist)
    if c.empty or "p50" not in c:
        return empty_figure("not enough history for a cone")
    x = [f"{int(h)}d" for h in c["horizon"]]
    traces = []
    for band, colour, style in (("p95", "#3d4b5a", "dot"), ("p75", "#4f6377", "dash"),
                                ("p50", "#7d93a8", "solid"), ("p25", "#4f6377", "dash"),
                                ("p5", "#3d4b5a", "dot")):
        traces.append(dict(type="scatter", mode="lines", name=band, x=x,
                           y=[v * 100 for v in c[band]],
                           line=dict(color=colour, width=1.2, dash=style),
                           hovertemplate=band + " %{y:.2f}%<extra></extra>"))
    traces.append(dict(type="scatter", mode="lines+markers", name="current RV", x=x,
                       y=[v * 100 for v in c["current"]],
                       line=dict(color=SERIES[0], width=2),
                       hovertemplate="current RV %{y:.2f}%<extra></extra>"))
    ivs = []
    for h in c["horizon"]:
        try:
            ivs.append(float(surf.atm(float(h) * 365 / A.BASIS / 365.0)) * 100)
        except Exception:                                  # noqa: BLE001
            ivs.append(float("nan"))
    traces.append(dict(type="scatter", mode="lines+markers", name="implied ATM today", x=x,
                       y=ivs, line=dict(color="#f0883e", width=2, dash="dash"),
                       hovertemplate="implied %{y:.2f}%<extra></extra>"))
    fig = figure(traces, title=f"{pair} vol cone — RV percentiles by horizon vs today's implied",
                 xtitle="horizon, business days (n per horizon in hover)",
                 ytitle="annualised vol, % (sqrt(252))", height=300)
    ns = ", ".join(f"{int(h)}d n={int(n)}" for h, n in zip(c["horizon"], c["n"]))
    fig["layout"]["annotations"] = [dict(x=0, y=1.14, xref="paper", yref="paper",
                                         showarrow=False, text="sample: " + ns,
                                         font=dict(color="#6e7d8d", size=10))]
    return fig


# ------------------------------------------------------------------ what-if pricer
@callback(Output("wi-out", "children"),
          Input("wi-strike", "value"), Input("wi-tenor", "value"), Input("wi-cp", "value"),
          Input("wi-notional", "value"), Input("surf-pair", "value"),
          Input("snapshot-token", "data"))
def _whatif(strike_text, tenor, cp, notional_text, pair, _token):
    s = get_session()
    pair = (pair or "EURUSD").upper()
    try:
        from fxgamma.store import parse_notional
        snap = s.snapshot()
        ctx = s.ctx()
        spec = pair_spec(pair)
        T = tenor_years(tenor or "1M")
        S = snap.spot.get(pair)
        if S is None:
            return note(f"no spot for {pair} in this snapshot.", tone="warn")
        res = parse_strike(strike_text or "ATMF", pair, ctx, T=T)
        N = abs(parse_notional(notional_text or "1mm"))
        surf = snap.surfaces.get(pair)
        if surf is None:
            return note(f"no surface for {pair}; a price cannot be quoted.", tone="warn")
        # ONE pricing route (see app/pricing): the what-if ticket prices a one-line
        # throwaway Book through `portfolio.risk.price_book`, exactly as the blotter,
        # the Risk page and the P&L page do.  A pre-trade number and a booked number
        # can no longer be produced by two different code paths, and the vol, the day
        # count and the delta convention are whatever the *book* would have used.
        expiry = (snap.asof + timedelta(days=max(round(T * 365.0), 1))).date()
        probe = Book([OptionPosition(id="whatif", pair=pair, cp=int(cp or 1),
                                     strike=float(res.strike), expiry=expiry,
                                     notional_base=N, direction=1,
                                     cut=spec.cut)], [])
        rows = price_positions(probe, snap, report_ccy=spec.quote)
        if not rows or rows[0]["state"] == "UNPRICED":
            return note(f"{pair} could not be priced: "
                        f"{rows[0]['vol_detail'] if rows else 'no row'}", tone="warn")
        r0 = rows[0]
        g = aggregate(probe, snap, pair=pair, report_ccy=spec.quote)
        sig, T = float(r0["vol"]), float(r0["T"])
        pips = g.pv / (N * spec.pip)
        # premium as % of the BASE notional: an amount of base ccy, converted at spot
        pct_base = g.pv / (N * S) * 100
        # premium as % of the QUOTE notional, which is N_base x K -> at the STRIKE
        pct_quote = g.pv / (N * float(res.strike)) * 100
        be = breakeven_daily_pct(g.gamma_1pct, g.theta, S)
        status, _ = surface_status(snap.meta, pair)
        return html.Div([
            metric_row([
                metric("resolved strike", fmt_spot(res.strike, pair),
                       unit=res.detail or "as typed",
                       sub=(f"delta {res.delta * 100:+.1f}% ({res.convention})"
                            if res.delta is not None else "delta unavailable")),
                metric("vol used", fmt_vol(sig),
                       unit=f"{status} surface at K, T={T:.4f}y (expiry {expiry} "
                            f"{spec.cut}) — priced through portfolio.risk.price_book, "
                            "the same route as the blotter"),
                metric("premium", fmt_money(g.pv, spec.quote),
                       unit="all four units, always (W-12)",
                       sub=f"{pips:,.1f} pips · {pct_base:.3f}% of base notional "
                           f"(at spot) · {pct_quote:.3f}% of quote notional (at the "
                           f"strike — the two differ by S/K)"),
                metric("daily breakeven", EM_DASH if be is None else f"{be:.3f}%",
                       unit="sqrt(|theta| / (0.005·G1·S)), calendar-day basis",
                       sub=(EM_DASH if be is None else
                            f"{be * S / spec.pip / 100:,.1f} pips · "
                            f"{be / sigma_day_move(sig):.2f} sigma-days — {DISTANCE_BASIS}")),
            ], cols="repeat(auto-fit, minmax(230px, 1fr))"),
            metric_row([
                metric("delta", fmt_mm(g.delta_base, spec.base),
                       unit=greek_unit("delta_base"),
                       sub=f"{fmt_mm(g.delta_base * S, spec.quote)} equivalent"),
                metric("gamma_1pct", fmt_mm(g.gamma_1pct, spec.base),
                       unit=greek_unit("gamma_1pct")),
                metric("vega", fmt_money(g.vega, spec.quote), unit=greek_unit("vega")),
                metric("theta", fmt_money(g.theta, spec.quote), unit=greek_unit("theta"),
                       tone="neg" if g.theta < 0 else "pos"),
                metric("vanna", fmt_money(g.vanna, spec.quote), unit=greek_unit("vanna")),
                metric("volga", fmt_money(g.volga, spec.quote), unit=greek_unit("volga")),
            ], cols="repeat(auto-fit, minmax(190px, 1fr))"),
            note(f"Long 1 {int(cp or 1) > 0 and 'call' or 'put'} on {pair} for "
                 f"{fmt_mm(N, spec.base, signed=False)} — a {pair} call is the right to buy "
                 f"{spec.base}. Nothing here is persisted."),
        ])
    except ValueError as exc:
        return note(str(exc), tone="warn")
    except Exception as exc:                               # noqa: BLE001
        log.exception("what-if failed")
        return note(f"what-if pricer failed: {exc}", tone="warn")


#: Dash 4 resolves the page layout from the registry at request time, so bind it
#: explicitly now that `layout` is defined.
dash.page_registry[__name__]["layout"] = layout
