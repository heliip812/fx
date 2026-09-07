"""Page 4 - Position Book (`/book`).

This is one of the two things the user asked for by name: enter a spot trade or an
option and see it priced.  So the whole of requirements section 3 is here - the option and
spot tickets, the structure builder, the section 3.3 strike grammar with the resolved strike
echoed **before** the write, inline V-1...V-21 validation, the CSV import behind a preview
gate, and an export that round-trips.

Three defences the trader review demanded live on this page:

* **the one-line answer** (MISS-4) sits at the top: long or short gamma, by how much,
  what move pays for the day, and what the day costs - in words, with the sign spelled out;
* **the premium reconcile box** (MISS-5 / W-12): type a broker premium in any of the four
  units and the app backs out the implied vol and the difference to the surface, in vol
  points.  It checks the pricer, the day count, the premium convention and the surface in
  one keystroke;
* **soft delete** (REQ-031): deleting asks first, then stamps ``deleted_at``.  Last week's
  P&L cannot move because you tidied the blotter today.
"""
from __future__ import annotations

import base64
import io
import logging
import math
import uuid
from datetime import date, datetime, timedelta, timezone

import dash
from dash import Input, Output, State, callback, ctx, dcc, html, no_update

from fxgamma.conventions import CUTS, PAIRS, TENORS, pair_spec, tenor_years
from fxgamma.store import (CSV_COLUMNS, MarketContext, OptionPosition, SpotPosition,
                           parse_notional, parse_strike, validate_row)

from ..components.badges import provenance_badge, surface_status, synthetic_banner
from ..components.cards import empty_state, grid, kv, metric, metric_row, note, panel
from ..components.fmt import (EM_DASH, fmt_mm, fmt_money, fmt_pct, fmt_pips, fmt_spot,
                              fmt_vol, fmt_vol_pts, greek_unit, theta_sentence)
from ..components.tables import col, data_table, empty_table_note, status_rules
from ..pricing import (book_greeks, breakeven_daily_pct, gamma_pnl_pct, pair_totals,
                       price_positions, sigma_day_move)
from ..state import ALL_PAIRS, get_session
from ..theme import ACCENT, KIND_COLORS, NEG, POS, TEXT_DIM, WARN

log = logging.getLogger(__name__)

dash.register_page(__name__, path="/book", name="Position Book", title="FX Gamma · Book")

STRUCTURES = ["single", "straddle", "strangle", "risk reversal", "butterfly",
              "call spread", "put spread", "calendar"]
TENOR_BUTTONS = ["ON", "1W", "2W", "1M", "2M", "3M", "6M"]
PREMIUM_UNITS = ["total", "pips", "pct_base", "pct_quote"]


# ====================================================================== layout
def layout(**_kw):
    s = get_session()
    snap = s.snapshot()
    pair = s.setting("pair", "EURUSD")
    today = snap.asof.date()
    return html.Div([
        synthetic_banner(snap.meta, page="book"),
        html.Div(id="bk-headline"),
        grid([_option_ticket(pair, today), _spot_ticket(pair, today)], cols="3fr 2fr"),
        html.Div(id="bk-ticket-preview"),
        html.Div(id="bk-blotter"),
        grid([_reconcile_panel(), _mark_panel()], cols="1fr 1fr"),
        _csv_panel(),
        dcc.ConfirmDialog(id="bk-confirm-delete",
                          message="Delete the selected position?\n\n"
                                  "This is a SOFT delete: the row is stamped deleted_at and "
                                  "kept, so an already published P&L cannot move."),
        dcc.Download(id="bk-download"),
        dcc.Store(id="bk-import-store"),
    ])


def _option_ticket(pair, today):
    return panel([
        html.Div([
            html.Div([html.Label("pair"),
                      dcc.Dropdown(id="ot-pair", options=ALL_PAIRS, value=pair,
                                   clearable=False)], style={"flex": "1 1 130px"}),
            html.Div([html.Label("structure"),
                      dcc.Dropdown(id="ot-structure", options=STRUCTURES, value="single",
                                   clearable=False)], style={"flex": "1 1 150px"}),
            html.Div([html.Label("direction"),
                      dcc.Dropdown(id="ot-direction",
                                   options=[{"label": "Buy", "value": 1},
                                            {"label": "Sell", "value": -1}],
                                   value=1, clearable=False)], style={"flex": "1 1 100px"}),
            html.Div([html.Label("call / put (single)"),
                      dcc.Dropdown(id="ot-cp",
                                   options=[{"label": "Call", "value": 1},
                                            {"label": "Put", "value": -1}],
                                   value=1, clearable=False)], style={"flex": "1 1 110px"}),
        ], className="row"),
        html.Div(id="ot-cp-explain", className="tiny dim", style={"margin": "4px 0 8px"}),
        html.Div([
            html.Div([html.Label("expiry"),
                      dcc.DatePickerSingle(id="ot-expiry",
                                           date=(today + timedelta(days=30)).isoformat(),
                                           display_format="YYYY-MM-DD")],
                     style={"flex": "0 0 auto"}),
            html.Div([html.Label("tenor buttons"),
                      html.Div([html.Button(t, id={"type": "ot-tenor", "t": t}, n_clicks=0,
                                            style={"padding": "4px 8px", "fontSize": "11px"})
                                for t in TENOR_BUTTONS],
                               style={"display": "flex", "gap": "3px", "flexWrap": "wrap"})],
                     style={"flex": "1 1 auto"}),
            html.Div([html.Label("cut"),
                      dcc.Dropdown(id="ot-cut", options=list(CUTS), value="NY10",
                                   clearable=False)], style={"flex": "0 0 110px"}),
        ], className="row"),
        html.Div([
            html.Div([html.Label("strike / lower leg"),
                      dcc.Input(id="ot-strike", type="text", value="ATMF", debounce=True)],
                     style={"flex": "1 1 150px"}),
            html.Div([html.Label("second strike / upper leg"),
                      dcc.Input(id="ot-strike2", type="text", value="", debounce=True,
                                placeholder="25dc, +50p, 101% …")],
                     style={"flex": "1 1 150px"}),
            html.Div([html.Label("notional (base ccy)"),
                      dcc.Input(id="ot-notional", type="text", value="10mm",
                                debounce=True)], style={"flex": "1 1 130px"}),
        ], className="row"),
        html.Div([
            html.Div([html.Label("trade date"),
                      dcc.DatePickerSingle(id="ot-trade-date", date=today.isoformat(),
                                           display_format="YYYY-MM-DD")],
                     style={"flex": "0 0 auto"}),
            html.Div([html.Label("trade spot (blank = live)"),
                      dcc.Input(id="ot-trade-spot", type="text", value="", debounce=True)],
                     style={"flex": "1 1 120px"}),
            html.Div([html.Label("trade vol (7.05 or 0.0705)"),
                      dcc.Input(id="ot-trade-vol", type="text", value="", debounce=True)],
                     style={"flex": "1 1 130px"}),
        ], className="row"),
        html.Div([
            html.Div([html.Label("premium (blank = auto-price)"),
                      dcc.Input(id="ot-premium", type="text", value="", debounce=True)],
                     style={"flex": "1 1 130px"}),
            html.Div([html.Label("unit"),
                      dcc.Dropdown(id="ot-premium-unit", options=PREMIUM_UNITS,
                                   value="total", clearable=False)],
                     style={"flex": "0 0 130px"}),
            html.Div([html.Label("premium ccy"),
                      dcc.Dropdown(id="ot-premium-ccy", options=["USD"], value="USD",
                                   clearable=False)], style={"flex": "0 0 120px"}),
            html.Div([html.Label("tag"),
                      dcc.Input(id="ot-tag", type="text", value="", debounce=True,
                                placeholder="auto for structures")],
                     style={"flex": "1 1 130px"}),
        ], className="row"),
        note("premium_paid > 0 = DEBIT (you paid). A sold option carries a negative "
             "premium; the sign is taken from Direction and you are warned (V-15) if you "
             "override it. Notional is always the BASE currency (contract section 2)."),
        html.Div([
            html.Button("Preview legs", id="ot-preview", n_clicks=0, className="primary"),
            html.Button("Add to book", id="ot-add", n_clicks=0),
        ], style={"display": "flex", "gap": "8px", "marginTop": "8px"}),
        html.Div(id="ot-echo", style={"marginTop": "8px"}),
    ], title="option ticket", sub="requirements section 3.1 + REQ-030 structure builder")


def _spot_ticket(pair, today):
    return panel([
        html.Div([
            html.Div([html.Label("pair"),
                      dcc.Dropdown(id="st-pair", options=ALL_PAIRS, value=pair,
                                   clearable=False)], style={"flex": "1 1 130px"}),
            html.Div([html.Label("side"),
                      dcc.Dropdown(id="st-side",
                                   options=[{"label": "Buy base", "value": 1},
                                            {"label": "Sell base", "value": -1}],
                                   value=1, clearable=False)], style={"flex": "1 1 120px"}),
        ], className="row"),
        html.Div([
            html.Div([html.Label("notional (base)"),
                      dcc.Input(id="st-notional", type="text", value="1mm",
                                debounce=True)], style={"flex": "1 1 130px"}),
            html.Div([html.Label("rate (blank = live spot)"),
                      dcc.Input(id="st-rate", type="text", value="", debounce=True)],
                     style={"flex": "1 1 130px"}),
        ], className="row"),
        html.Div([
            html.Div([html.Label("trade date"),
                      dcc.DatePickerSingle(id="st-trade-date", date=today.isoformat(),
                                           display_format="YYYY-MM-DD")],
                     style={"flex": "0 0 auto"}),
            html.Div([html.Label("value date (blank = +spot lag)"),
                      dcc.DatePickerSingle(id="st-value-date",
                                           display_format="YYYY-MM-DD")],
                     style={"flex": "0 0 auto"}),
        ], className="row"),
        html.Div([dcc.Checklist(id="st-hedge",
                                options=[{"label": " this is a delta hedge (tag=hedge)",
                                          "value": "hedge"}],
                                value=["hedge"], labelStyle={"fontSize": "11px"})],
                 style={"marginTop": "6px"}),
        html.Div([html.Label("tag"),
                  dcc.Input(id="st-tag", type="text", value="hedge", debounce=True)],
                 className="field", style={"marginTop": "6px"}),
        html.Button("Add spot", id="st-add", n_clicks=0, className="primary",
                    style={"marginTop": "8px"}),
        html.Div(id="st-echo", style={"marginTop": "8px"}),
        note("A hedge line is tagged so P&L attribution can separate it (contract section 5) "
             "and it is written to the hedge log, ordered by trade_time (CG-5)."),
    ], title="spot / hedge ticket", sub="requirements section 3.2")


def _reconcile_panel():
    return panel([
        html.Div([
            html.Div([html.Label("position id"),
                      dcc.Input(id="rc-id", type="text", value="", debounce=True,
                                placeholder="opt-0001")], style={"flex": "1 1 120px"}),
            html.Div([html.Label("broker premium"),
                      dcc.Input(id="rc-prem", type="text", value="", debounce=True)],
                     style={"flex": "1 1 120px"}),
            html.Div([html.Label("unit"),
                      dcc.Dropdown(id="rc-unit", options=PREMIUM_UNITS, value="pips",
                                   clearable=False)], style={"flex": "0 0 120px"}),
        ], className="row"),
        html.Div(id="rc-out", style={"marginTop": "8px"}),
        note("Type a broker's premium for one of your lines. The app backs out the implied "
             "vol and shows the difference to the surface in vol points — one keystroke "
             "that checks the pricer, the day count, the premium convention, the delta "
             "convention and the surface (trader review MISS-5)."),
    ], title="reconcile box", sub="MISS-5")


def _mark_panel():
    return panel([
        html.Div([
            html.Div([html.Label("position id"),
                      dcc.Input(id="mk-id", type="text", value="", debounce=True,
                                placeholder="opt-0001")], style={"flex": "1 1 120px"}),
            html.Div([html.Label("mark vol (7.05 or 0.0705)"),
                      dcc.Input(id="mk-vol", type="text", value="", debounce=True)],
                     style={"flex": "1 1 130px"}),
            html.Div([html.Label(" "),
                      html.Button("Set mark", id="mk-set", n_clicks=0)],
                     style={"flex": "0 0 auto"}),
            html.Div([html.Label(" "),
                      html.Button("Clear", id="mk-clear", n_clicks=0)],
                     style={"flex": "0 0 auto"}),
        ], className="row"),
        html.Div(id="mk-out", style={"marginTop": "8px"}),
        note("A per-position mark vol is stored in the position_marks side table "
             "(amendment v1.1 CG-2 — OptionPosition stays frozen). The row prices off your "
             "vol and is badged OVERRIDE."),
    ], title="mark-vol override", sub="REQ-036 / CG-2")


def _csv_panel():
    return panel([
        html.Div([
            dcc.Upload(id="bk-upload",
                       children=html.Div(["drop a CSV here, or ", html.A("browse")]),
                       style={"border": "1px dashed #2b3440", "borderRadius": "5px",
                              "padding": "14px", "textAlign": "center", "flex": "1 1 300px",
                              "cursor": "pointer", "color": TEXT_DIM},
                       multiple=False),
            html.Div([
                html.Label("mode"),
                dcc.Dropdown(id="bk-import-mode",
                             options=[{"label": "append", "value": "append"},
                                      {"label": "upsert by id", "value": "upsert"},
                                      {"label": "replace book", "value": "replace"}],
                             value="append", clearable=False),
                dcc.Checklist(id="bk-allow-expired",
                              options=[{"label": " allow expired rows (history)",
                                        "value": "yes"}], value=[],
                              labelStyle={"fontSize": "11px"}),
                dcc.Checklist(id="bk-accept-warn",
                              options=[{"label": " accept warnings", "value": "yes"}],
                              value=[], labelStyle={"fontSize": "11px"}),
            ], style={"flex": "0 0 220px"}),
            html.Div([
                html.Label("export"),
                html.Button("Export CSV", id="bk-export", n_clicks=0),
                html.Div("full section 3.5 schema — import(export(book)) == book",
                         className="tiny faint", style={"marginTop": "4px"}),
            ], style={"flex": "0 0 200px"}),
        ], className="row"),
        html.Div(id="bk-import-preview", style={"marginTop": "10px"}),
    ], title="CSV import / export", sub="REQ-033 preview gate · REQ-034 round trip")


# ====================================================================== headline + blotter
def _headline_children(s):
    snap = s.snapshot()
    rows = price_positions(s.store.load_book(), snap, marks=s.store.marks(),
                           report_ccy=s.report_ccy)
    if not rows:
        return panel(empty_state("no positions — import a CSV or add a trade",
                                 "every screen renders with an empty book (REQ-003)"),
                     title="the one-line answer", sub="MISS-4")
    cards = []
    for pair in sorted({r["pair"] for r in rows}):
        g = book_greeks(rows, pair=pair)
        spec = pair_spec(pair)
        S = snap.spot.get(pair)
        be = breakeven_daily_pct(g.gamma_1pct, g.theta, S) if S else None
        long_short = ("LONG GAMMA" if g.gamma_1pct > 0 else
                      "SHORT GAMMA" if g.gamma_1pct < 0 else "FLAT GAMMA")
        be_pips = (be * S / spec.pip / 100) if (be and S) else None
        sentence = (f"{long_short} · {fmt_mm(g.gamma_1pct, spec.base)} per +1% · "
                    + (f"pays above {be_pips:,.0f} pips today" if be_pips else
                       "breakeven unavailable")
                    + f" · {theta_sentence(g.theta, spec.quote)}")
        status, _ = surface_status(snap.meta, pair)
        cards.append(metric(
            pair, sentence,
            tone="pos" if g.gamma_1pct > 0 else "neg" if g.gamma_1pct < 0 else "",
            unit=f"priced off the {status} surface · gamma_1pct = "
                 + greek_unit("gamma_1pct"),
            sub=(f"PV {fmt_money(g.pv, spec.quote)} · vega {fmt_money(g.vega, spec.quote)} "
                 f"per vol pt · delta {fmt_mm(g.delta_base, spec.base)}"),
            badge=provenance_badge(snap.meta, f"surface.{pair}", compact=True)))
    return panel(metric_row(cards, cols="repeat(auto-fit, minmax(420px, 1fr))"),
                 title="the one-line answer",
                 sub="MISS-4 — long or short, by how much, what pays, what it costs")


def _blotter_children(s, filter_text: str = "", pair_filter=None, tag_filter=None):
    snap = s.snapshot()
    book = s.store.load_book()
    marks = s.store.marks()
    rows = price_positions(book, snap, marks=marks, report_ccy=s.report_ccy)
    if pair_filter:
        rows = [r for r in rows if r["pair"] in pair_filter]
    if tag_filter:
        rows = [r for r in rows if r["tag"] in tag_filter]
    if not rows:
        return panel(empty_table_note(), title="blotter", sub="REQ-032")
    data = []
    for r in rows:
        spec = pair_spec(r["pair"])
        data.append({
            "id": r["id"], "state": r["state"], "pair": r["pair"],
            "type": r["instrument"], "cp": r["cp"], "dir": r["dir"],
            "strike": EM_DASH if r["strike"] is None else fmt_spot(r["strike"], r["pair"]),
            "expiry": r["expiry"] or EM_DASH,
            "days": EM_DASH if r["days"] is None else r["days"],
            "notional": fmt_mm(r["notional_base"], spec.base),
            "vol": fmt_vol(r["vol"]), "vol_src": r["vol_source"],
            "pv": round(r["pv"], 0), "delta": fmt_mm(r["delta_base"], spec.base),
            "g1": fmt_mm(r["gamma_1pct"], spec.base),
            "vega": round(r["vega"], 0), "theta": round(r["theta"], 0),
            "vanna": round(r["vanna"], 0), "volga": round(r["volga"], 0),
            "premium": round(r["premium_paid"], 0),
            "pnl": (None if r["pnl_since_trade"] != r["pnl_since_trade"]
                    else round(r["pnl_since_trade"], 0)),
            "ccy": r["ccy"], "tag": r["tag"],
        })
    cols = [
        col("id"), col("state"), col("pair"), col("type"), col("cp"), col("dir"),
        col("strike"), col("expiry"), col("days", unit="cal.", numeric=True),
        col("notional", unit="base ccy"), col("vol", unit="used"),
        col("vol src", "vol_src"), col("PV", "pv", unit="quote ccy", numeric=True),
        col("delta", unit="base ccy"), col("Γ₁", "g1", unit="base per +1%"),
        col("vega", unit="quote / vol pt", numeric=True),
        col("theta", unit="quote / cal. day", numeric=True),
        col("vanna", unit="quote / volpt / spot", numeric=True),
        col("volga", unit="quote / volpt²", numeric=True),
        col("premium", unit="quote, debit>0", numeric=True),
        col("P&L", "pnl", unit="PV − premium", numeric=True),
        col("ccy"), col("tag"),
    ]
    cond = status_rules("state") + [
        {"if": {"filter_query": "{pv} < 0", "column_id": "pv"}, "color": NEG},
        {"if": {"filter_query": "{pv} > 0", "column_id": "pv"}, "color": POS},
        {"if": {"filter_query": "{pnl} < 0", "column_id": "pnl"}, "color": NEG},
        {"if": {"filter_query": "{pnl} > 0", "column_id": "pnl"}, "color": POS},
        {"if": {"filter_query": "{theta} < 0", "column_id": "theta"}, "color": NEG},
        {"if": {"filter_query": "{theta} > 0", "column_id": "theta"}, "color": POS},
        {"if": {"filter_query": '{vol_src} = "mark override"', "column_id": "vol_src"},
         "color": KIND_COLORS["user_override"], "fontWeight": "700"},
        {"if": {"filter_query": '{state} = "UNPRICED"', "column_id": "state"},
         "color": WARN, "fontWeight": "700"},
    ]
    totals = pair_totals(rows, s.report_ccy)
    tot_rows = []
    for t in totals:
        tot_rows.append((f"{t['pair']} ({t['ccy']})",
                         f"PV {fmt_money(t['pv'], t['ccy'])} · Γ₁ "
                         f"{fmt_mm(t['gamma_1pct'], t['base_ccy'])} · vega "
                         f"{fmt_money(t['vega'], t['ccy'])} · theta "
                         f"{fmt_money(t['theta'], t['ccy'])} · fx to {s.report_ccy} "
                         f"{t['fx_to_report']:.6g}"))
    expired = [r for r in rows if r["state"] == "EXPIRED"]
    return panel([
        html.Div([
            html.Div([html.Label("filter: pair"),
                      dcc.Dropdown(id="bk-filter-pair", options=sorted({r["pair"]
                                                                        for r in rows}),
                                   multi=True, value=pair_filter or [])],
                     style={"flex": "1 1 200px"}),
            html.Div([html.Label("filter: tag / structure"),
                      dcc.Dropdown(id="bk-filter-tag",
                                   options=sorted({r["tag"] for r in rows if r["tag"]}),
                                   multi=True, value=tag_filter or [])],
                     style={"flex": "1 1 200px"}),
            html.Div([html.Label(" "),
                      html.Button("Delete selected", id="bk-delete", n_clicks=0,
                                  className="danger")], style={"flex": "0 0 auto"}),
        ], className="row"),
        html.Div((f"FILTER ACTIVE: {', '.join(pair_filter or [])} "
                  f"{', '.join(tag_filter or [])} — this blotter and its totals show a "
                  "SUBSET of the book")
                 if (pair_filter or tag_filter) else "",
                 style={"color": WARN, "fontSize": "11px", "margin": "6px 0"}),
        data_table("bk-blotter-table", cols, data, page_size=25,
                   conditional=cond, row_selectable="single",
                   tooltips={"g1": greek_unit("gamma_1pct"), "vega": greek_unit("vega"),
                             "theta": greek_unit("theta"), "vanna": greek_unit("vanna"),
                             "volga": greek_unit("volga"),
                             "pnl": "PV − premium paid, since trade (P&L since "
                                    "yesterday needs the stored EOD snapshot: page 6)"}),
        html.Div(id="bk-delete-msg", className="tiny", style={"marginTop": "6px"}),
        kv(tot_rows),
        note(f"{len(expired)} expired line(s) are shown with state EXPIRED, priced to "
             "intrinsic, and are excluded from the totals above (v1.2 T-3). Per-pair totals "
             "are in that pair's own quote ccy; cross-pair aggregation in native ccy is "
             "forbidden by CG-1, so the fx used into "
             f"{s.report_ccy} is printed on each row.")
        if expired else
        note("Per-pair totals are in that pair's quote ccy. Aggregating across pairs in "
             "native ccy is forbidden (CG-1); the conversion rate into "
             f"{s.report_ccy} is disclosed on each row."),
    ], title="blotter", sub="REQ-032 — live re-pricing on the stamped snapshot")


@callback(Output("bk-headline", "children"), Output("bk-blotter", "children"),
          Input("snapshot-token", "data"), Input("book-version", "data"),
          State("bk-filter-pair", "value"), State("bk-filter-tag", "value"))
def _refresh_book(_token, _bv, pair_filter, tag_filter):
    s = get_session()
    try:
        return _headline_children(s), _blotter_children(s, "", pair_filter, tag_filter)
    except Exception as exc:                               # noqa: BLE001
        log.exception("book render failed")
        msg = note(f"book render failed: {exc}", tone="warn")
        return msg, msg


@callback(Output("book-version", "data", allow_duplicate=True),
          Input("bk-filter-pair", "value"), Input("bk-filter-tag", "value"),
          State("book-version", "data"), prevent_initial_call=True)
def _filters(_p, _t, v):
    return (v or 0) + 1


# ====================================================================== ticket helpers
def _pair_ccys(pair):
    spec = pair_spec(pair)
    return [spec.quote, spec.base]


@callback(Output("ot-premium-ccy", "options"), Output("ot-premium-ccy", "value"),
          Output("ot-cp-explain", "children"), Output("ot-cut", "value"),
          Input("ot-pair", "value"), Input("ot-cp", "value"))
def _pair_changed(pair, cp):
    pair = (pair or "EURUSD").upper()
    spec = pair_spec(pair)
    word = "call" if (cp or 1) > 0 else "put"
    verb = "buy" if (cp or 1) > 0 else "sell"
    txt = (f"{pair} {word} = the right to {verb} {spec.base} against {spec.quote}. "
           f"Notional is in {spec.base}. Delta convention: {spec.delta_convention}. "
           f"pip = {spec.pip:g}.")
    return _pair_ccys(pair), spec.quote, txt, spec.cut


@callback(Output("ot-expiry", "date"),
          Input({"type": "ot-tenor", "t": dash.ALL}, "n_clicks"),
          State("ot-pair", "value"), prevent_initial_call=True)
def _tenor_button(_clicks, pair):
    trig = ctx.triggered_id
    if not isinstance(trig, dict):
        return no_update
    s = get_session()
    spec = pair_spec((pair or "EURUSD").upper())
    asof = s.snapshot().asof.date()
    t = trig["t"]
    start = asof + timedelta(days=spec.spot_lag)
    days = {"ON": 1, "1W": 7, "2W": 14, "1M": 30, "2M": 61, "3M": 91, "6M": 182}[t]
    d = start + timedelta(days=days)
    while d.weekday() >= 5:                       # roll to the next good business day
        d += timedelta(days=1)
    return d.isoformat()


def _legs_from_ticket(s, values) -> tuple[list[dict], list[str]]:
    """Expand the ticket into raw CSV-shaped leg dicts (REQ-030). Signs are explicit."""
    (pair, structure, direction, cp, expiry, cut, k1, k2, notional, trade_date,
     trade_spot, trade_vol, premium, premium_unit, premium_ccy, tag) = values
    pair = (pair or "EURUSD").upper()
    direction = int(direction or 1)
    cp = int(cp or 1)
    tag = tag or f"{structure.replace(' ', '')}:{uuid.uuid4().hex[:6]}" \
        if structure != "single" else (tag or "")
    notes: list[str] = []
    base = {"instrument_type": "OPTION", "pair": pair, "expiry": expiry, "cut": cut,
            "notional_base": notional, "trade_date": trade_date,
            "trade_spot": trade_spot, "trade_vol": trade_vol,
            "premium_ccy": premium_ccy, "premium_unit": premium_unit, "tag": tag,
            "notes": ""}
    d = lambda x: "B" if x > 0 else "S"                     # noqa: E731

    def leg(cp_, dir_, strike, prem=""):
        return {**base, "cp": "C" if cp_ > 0 else "P", "direction": d(dir_),
                "strike": strike, "premium_paid": prem}

    k2 = (k2 or "").strip()
    if structure == "single":
        legs = [leg(cp, direction, k1, premium or "")]
    elif structure == "straddle":
        legs = [leg(1, direction, k1), leg(-1, direction, k1)]
        notes.append("straddle: call and put at the same strike, same direction")
    elif structure == "strangle":
        if not k2:
            raise ValueError("a strangle needs two strikes: put leg in 'strike', "
                             "call leg in 'second strike'")
        legs = [leg(-1, direction, k1), leg(1, direction, k2)]
        notes.append("strangle: put at the lower strike, call at the upper")
    elif structure == "risk reversal":
        if not k2:
            raise ValueError("a risk reversal needs two strikes: put leg in 'strike', "
                             "call leg in 'second strike'")
        legs = [leg(1, direction, k2), leg(-1, -direction, k1)]
        notes.append(f"risk reversal: {'buy' if direction > 0 else 'sell'} the "
                     f"{pair_spec(pair).base} call, opposite sign on the put "
                     "(section 3.1 field 3)")
    elif structure == "butterfly":
        if not k2:
            raise ValueError("a butterfly needs the wing strike in 'second strike' "
                             "(e.g. 25dc) and the body in 'strike' (e.g. ATMF)")
        legs = [leg(1, direction, k2), leg(-1, direction, f"-{_pips_of(k2)}"
                                           if False else k2)]
        legs = [leg(1, direction, k2), leg(-1, direction, k2),
                leg(1, -direction, k1), leg(-1, -direction, k1)]
        notes.append("smile butterfly: long the strangle, short the straddle, same "
                     "notional per leg (not vega-weighted — stated, not assumed)")
    elif structure == "call spread":
        if not k2:
            raise ValueError("a call spread needs both strikes")
        legs = [leg(1, direction, k1), leg(1, -direction, k2)]
    elif structure == "put spread":
        if not k2:
            raise ValueError("a put spread needs both strikes")
        legs = [leg(-1, direction, k1), leg(-1, -direction, k2)]
    elif structure == "calendar":
        raise ValueError("a calendar needs two expiries: book the two legs as two single "
                         "tickets sharing a tag (the ticket carries one expiry)")
    else:
        raise ValueError(f"unknown structure {structure!r}")
    return legs, notes


def _pips_of(x):                                            # pragma: no cover
    return x


def _validate_legs(s, legs, allow_expired=False):
    ctx_ = s.ctx()
    results = []
    for i, raw in enumerate(legs, start=1):
        results.append(validate_row(raw, ctx_, row_no=i, allow_expired=allow_expired,
                                    existing_ids=s.store.existing_ids(),
                                    next_id=s.store.next_id))
    return results


def _echo(results, s) -> html.Div:
    snap = s.snapshot()
    rows = []
    for r in results:
        p = r.position
        if p is None:
            rows.append(("—", r.reason or "row rejected"))
            continue
        spec = pair_spec(p.pair)
        T = None
        try:
            from fxgamma.conventions import year_fraction
            T = year_fraction(snap.asof, p.expiry, p.cut)
        except Exception:                                  # noqa: BLE001
            pass
        surf = snap.surfaces.get(p.pair)
        sig = None
        if surf is not None and T:
            try:
                sig = float(surf.vol(p.strike, T))
            except Exception:                              # noqa: BLE001
                sig = None
        delta = None
        try:
            from fxgamma.models.gk import delta_from_strike
            rd, rf = snap.rd_rf(p.pair, PAIRS)
            S = snap.spot[p.pair]
            if sig and T:
                delta = delta_from_strike(p.strike, S, T, rd, rf, sig, p.cp,
                                          spec.delta_convention)
        except Exception:                                  # noqa: BLE001
            delta = None
        S = snap.spot.get(p.pair)
        rows.append((
            f"{'BUY ' if p.direction > 0 else 'SELL'} {p.pair} "
            f"{'call' if p.cp > 0 else 'put'}",
            html.Span([
                f"K {fmt_spot(p.strike, p.pair)}",
                f" · {fmt_mm(p.notional_base, spec.base, signed=False)}",
                f" · exp {p.expiry} {p.cut}",
                f" · {(p.strike / S - 1) * 100:+.2f}% of spot, "
                f"{(p.strike - S) / spec.pip:+,.0f} pips" if S else "",
                f" · vol {fmt_vol(sig)}" if sig else " · vol unavailable",
                (f" · delta {delta * 100:+.1f}% ({spec.delta_convention})"
                 if delta is not None else " · delta n/a"),
                f" · premium {fmt_money(p.premium_paid, p.premium_ccy)}",
                html.Div(r.reason, className="tiny",
                         style={"color": NEG if r.status == "ERROR" else WARN})
                if r.messages else "",
            ])))
    ok = all(r.status != "ERROR" for r in results)
    return html.Div([
        html.Div(("legs resolve — press Add to book" if ok else
                  "BLOCKED: fix the errors below before adding"),
                 style={"color": POS if ok else NEG, "fontWeight": "700",
                        "marginBottom": "6px", "fontSize": "12px"}),
        kv(rows)])


@callback(Output("ot-echo", "children"), Output("book-version", "data"),
          Input("ot-preview", "n_clicks"), Input("ot-add", "n_clicks"),
          State("ot-pair", "value"), State("ot-structure", "value"),
          State("ot-direction", "value"), State("ot-cp", "value"),
          State("ot-expiry", "date"), State("ot-cut", "value"),
          State("ot-strike", "value"), State("ot-strike2", "value"),
          State("ot-notional", "value"), State("ot-trade-date", "date"),
          State("ot-trade-spot", "value"), State("ot-trade-vol", "value"),
          State("ot-premium", "value"), State("ot-premium-unit", "value"),
          State("ot-premium-ccy", "value"), State("ot-tag", "value"),
          State("book-version", "data"), prevent_initial_call=True)
def _ticket(_p, _a, *args):
    s = get_session()
    version = args[-1] or 0
    values = args[:-1]
    add = ctx.triggered_id == "ot-add"
    try:
        legs, notes = _legs_from_ticket(s, values)
        results = _validate_legs(s, legs)
        body = _echo(results, s)
        blocked = any(r.status == "ERROR" for r in results)
        extra = [note(n) for n in notes]
        if add and not blocked:
            positions = [r.position for r in results if r.position is not None]
            s.store.save_positions(positions)
            return (html.Div([
                html.Div(f"added {len(positions)} leg(s): "
                         + ", ".join(p.id for p in positions),
                         style={"color": POS, "fontWeight": 700}),
                body, *extra]), version + 1)
        if add and blocked:
            return html.Div([
                html.Div("NOT added — the ticket has errors (requirements section 3.4)",
                         style={"color": NEG, "fontWeight": 700}), body, *extra]), no_update
        return html.Div([body, *extra]), no_update
    except ValueError as exc:
        return note(str(exc), tone="warn"), no_update
    except Exception as exc:                               # noqa: BLE001
        log.exception("ticket failed")
        return note(f"ticket failed: {exc}", tone="warn"), no_update


@callback(Output("st-echo", "children"),
          Output("book-version", "data", allow_duplicate=True),
          Input("st-add", "n_clicks"),
          State("st-pair", "value"), State("st-side", "value"),
          State("st-notional", "value"), State("st-rate", "value"),
          State("st-trade-date", "date"), State("st-value-date", "date"),
          State("st-hedge", "value"), State("st-tag", "value"),
          State("book-version", "data"), prevent_initial_call=True)
def _spot_add(_n, pair, side, notional, rate, trade_date, value_date, hedge, tag, version):
    s = get_session()
    try:
        pair = (pair or "EURUSD").upper()
        snap = s.snapshot()
        spec = pair_spec(pair)
        n = abs(parse_notional(notional or "0")) * int(side or 1)
        r = rate or snap.spot.get(pair)
        if not value_date and trade_date:
            d = date.fromisoformat(trade_date) + timedelta(days=spec.spot_lag)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            value_date = d.isoformat()
        is_hedge = "hedge" in (hedge or [])
        raw = {"instrument_type": "SPOT", "pair": pair, "notional_base": n,
               "entry_rate": r, "trade_date": trade_date, "value_date": value_date,
               "tag": (tag or ("hedge" if is_hedge else ""))}
        res = validate_row(raw, s.ctx(), row_no=1, existing_ids=s.store.existing_ids(),
                           next_id=s.store.next_id)
        if res.status == "ERROR" or res.position is None:
            return html.Div([html.Div("NOT added", style={"color": NEG,
                                                          "fontWeight": 700}),
                             note(res.reason, tone="warn")]), no_update
        p = res.position
        p = SpotPosition(p.id, p.pair, p.notional_base, p.entry_rate, p.trade_date,
                         p.value_date, p.tag, datetime.now(timezone.utc))
        s.store.save_position(p)
        pnl = (snap.spot.get(pair, p.entry_rate) - p.entry_rate) * p.notional_base
        return html.Div([
            html.Div(f"added {p.id}", style={"color": POS, "fontWeight": 700}),
            kv([("position", f"{fmt_mm(p.notional_base, spec.base)} at "
                             f"{fmt_spot(p.entry_rate, pair)}"),
                ("value date", str(p.value_date or "—")),
                ("tag", p.tag or "—"),
                ("mark-to-market now", fmt_money(pnl, spec.quote)),
                ("hedge log", "written (ordered by trade_time, CG-5)" if p.tag == "hedge"
                 else "not a hedge line")]),
            note(res.reason, tone="warn") if res.messages else "",
        ]), (version or 0) + 1
    except Exception as exc:                               # noqa: BLE001
        log.exception("spot ticket failed")
        return note(f"spot ticket failed: {exc}", tone="warn"), no_update


# ====================================================================== delete
@callback(Output("bk-confirm-delete", "displayed"),
          Input("bk-delete", "n_clicks"),
          State("bk-blotter-table", "selected_rows"), prevent_initial_call=True)
def _ask_delete(_n, selected):
    return bool(selected)


@callback(Output("bk-delete-msg", "children"),
          Output("book-version", "data", allow_duplicate=True),
          Input("bk-confirm-delete", "submit_n_clicks"),
          State("bk-blotter-table", "selected_rows"),
          State("bk-blotter-table", "data"), State("book-version", "data"),
          prevent_initial_call=True)
def _do_delete(_n, selected, data, version):
    s = get_session()
    try:
        if not selected:
            return "nothing selected", no_update
        pid = data[selected[0]]["id"]
        ok = s.store.delete_position(pid)
        return (f"{pid} soft-deleted (deleted_at stamped; the row is kept so historical "
                f"P&L is unchanged)" if ok else f"{pid} not found"), (version or 0) + 1
    except Exception as exc:                               # noqa: BLE001
        return f"delete failed: {exc}", no_update


# ====================================================================== marks
@callback(Output("mk-out", "children"),
          Output("book-version", "data", allow_duplicate=True),
          Input("mk-set", "n_clicks"), Input("mk-clear", "n_clicks"),
          State("mk-id", "value"), State("mk-vol", "value"),
          State("book-version", "data"), prevent_initial_call=True)
def _mark(_a, _b, pid, vol, version):
    s = get_session()
    try:
        pid = (pid or "").strip()
        if not pid:
            return note("enter a position id from the blotter", tone="warn"), no_update
        if ctx.triggered_id == "mk-clear":
            ok = s.store.clear_mark_vol(pid)
            return (note(f"mark cleared for {pid}; it prices off the surface again"
                         if ok else f"no mark on {pid}"), (version or 0) + 1)
        if s.store.get_position(pid) is None:
            return note(f"no position with id {pid} in this book — copy the id from the "
                        "blotter", tone="warn"), no_update
        v = float(str(vol).replace("%", "").strip())
        warn = ""
        if v > 1.0:
            warn = f"read {v:g} as {v:g}% = {v / 100:.4f} (V-10)"
            v /= 100.0
        s.store.set_mark_vol(pid, v, mark_source="user (Book page)")
        return html.Div([
            html.Div(f"{pid} now prices at {fmt_vol(v)} — badged OVERRIDE",
                     style={"color": KIND_COLORS['user_override'], "fontWeight": 700}),
            note(warn, tone="warn") if warn else "",
        ]), (version or 0) + 1
    except Exception as exc:                               # noqa: BLE001
        return note(f"mark failed: {exc}", tone="warn"), no_update


# ====================================================================== reconcile
@callback(Output("rc-out", "children"),
          Input("rc-id", "value"), Input("rc-prem", "value"), Input("rc-unit", "value"),
          Input("snapshot-token", "data"))
def _reconcile(pid, prem, unit, _token):
    s = get_session()
    pid = (pid or "").strip()
    if not pid or not prem:
        return note("enter a position id and a broker premium.")
    try:
        pos = s.store.get_position(pid)
        if pos is None or not isinstance(pos, OptionPosition):
            return note(f"{pid} is not an option in this book", tone="warn")
        snap = s.snapshot()
        spec = pair_spec(pos.pair)
        S = snap.spot.get(pos.pair)
        from fxgamma.conventions import year_fraction
        from fxgamma.models.gk import implied_vol
        T = year_fraction(snap.asof, pos.expiry, pos.cut)
        if S is None or T <= 0:
            return note("no spot, or the option has expired", tone="warn")
        raw = parse_notional(prem)
        N = abs(pos.notional_base)
        s_trade = pos.trade_spot or S
        if unit == "total":
            total = raw
        elif unit == "pips":
            total = raw * N * spec.pip
        else:
            total = raw / 100.0 * N * s_trade
        per_unit = abs(total) / N
        if (pos.premium_ccy or spec.quote).upper() == spec.base:
            per_unit *= s_trade
        rd, rf = snap.rd_rf(pos.pair, PAIRS)
        iv = float(implied_vol(per_unit, S, pos.strike, T, rd, rf, pos.cp))
        surf = snap.surfaces.get(pos.pair)
        sv = float(surf.vol(pos.strike, T)) if surf is not None else None
        diff = (iv - sv) * 100 if sv is not None else None
        tone = ("neg" if diff is not None and abs(diff) > 2.0 else
                "" if diff is None or abs(diff) <= 0.5 else "neg")
        verdict = (EM_DASH if diff is None else
                   "BLOCK-level disagreement (>2 vol pts): check the unit and ccy"
                   if abs(diff) > 2.0 else
                   "warn (>0.5 vol pts)" if abs(diff) > 0.5 else "agrees with the surface")
        return metric_row([
            metric("premium as entered", fmt_money(total, pos.premium_ccy or spec.quote),
                   unit=f"{raw:g} {unit} on {fmt_mm(N, spec.base, signed=False)}",
                   sub=f"{total / (N * spec.pip):,.1f} pips · "
                       f"{total / (N * s_trade) * 100:.3f}% of base notional"),
            metric("implied by that premium", fmt_vol(iv), unit="vol implied by the price"),
            metric("surface vol", fmt_vol(sv), unit=f"{surface_status(snap.meta, pos.pair)[0]}"
                                                    " surface at this strike/tenor"),
            metric("difference", EM_DASH if diff is None else f"{diff:+.2f} vol pts",
                   unit=verdict, tone=tone),
        ], cols="repeat(auto-fit, minmax(200px, 1fr))")
    except Exception as exc:                               # noqa: BLE001
        return note(f"reconcile failed: {exc}", tone="warn")


# ====================================================================== CSV
@callback(Output("bk-import-preview", "children"), Output("bk-import-store", "data"),
          Input("bk-upload", "contents"), State("bk-upload", "filename"),
          State("bk-import-mode", "value"), State("bk-allow-expired", "value"),
          prevent_initial_call=True)
def _upload(contents, filename, mode, allow_expired):
    s = get_session()
    try:
        _, b64 = contents.split(",", 1)
        text = base64.b64decode(b64).decode("utf-8-sig", errors="replace")
        rep = s.store.parse_csv(text, ctx=s.ctx(), mode=mode or "append",
                               allow_expired=bool(allow_expired))
        if rep.fatal:
            return note(f"{filename}: {rep.fatal}", tone="warn"), None
        data = rep.preview_records()
        cols = ([col("row", numeric=True), col("status"), col("reason")]
                + [col(c) for c in CSV_COLUMNS])
        counts = html.Div([
            html.Span(f"{rep.n_ok} OK", style={"color": POS, "marginRight": "12px",
                                               "fontWeight": 700}),
            html.Span(f"{rep.n_warn} WARN", style={"color": WARN, "marginRight": "12px",
                                                   "fontWeight": 700}),
            html.Span(f"{rep.n_error} ERROR", style={"color": NEG, "marginRight": "18px",
                                                     "fontWeight": 700}),
            html.Span(f"file {filename} · mode {mode}", className="tiny dim"),
        ], style={"marginBottom": "8px", "fontSize": "13px"})
        buttons = html.Div([
            html.Button("Commit import", id="bk-commit", n_clicks=0, className="primary",
                        disabled=not rep.can_commit),
            html.Button("Download rejects", id="bk-rejects", n_clicks=0,
                        disabled=(rep.n_error + rep.n_warn) == 0),
        ], style={"display": "flex", "gap": "8px", "marginTop": "8px"})
        gate = note("Commit is disabled while any row is an ERROR. WARN rows need the "
                    "'accept warnings' tick. The commit is one SQLite transaction: all "
                    "rows or none (REQ-033).", tone="warn" if rep.n_error else "dim")
        return html.Div([
            counts,
            data_table("bk-import-table", cols, data, page_size=12,
                       conditional=status_rules("status")),
            buttons, gate, html.Div(id="bk-commit-msg", style={"marginTop": "6px"}),
        ]), {"text": text, "mode": mode or "append",
             "allow_expired": bool(allow_expired), "filename": filename}
    except Exception as exc:                               # noqa: BLE001
        log.exception("upload failed")
        return note(f"could not read {filename}: {exc}", tone="warn"), None


@callback(Output("bk-commit-msg", "children"),
          Output("book-version", "data", allow_duplicate=True),
          Input("bk-commit", "n_clicks"), State("bk-import-store", "data"),
          State("bk-accept-warn", "value"), State("book-version", "data"),
          prevent_initial_call=True)
def _commit(_n, store, accept_warn, version):
    s = get_session()
    if not store:
        return note("nothing to commit"), no_update
    try:
        rep = s.store.parse_csv(store["text"], ctx=s.ctx(), mode=store["mode"],
                                allow_expired=store["allow_expired"])
        s.store.commit_import(rep, accept_warnings=bool(accept_warn))
        return (html.Div(f"committed {rep.committed} row(s) from {store['filename']} "
                         f"in mode {store['mode']}",
                         style={"color": POS, "fontWeight": 700}), (version or 0) + 1)
    except ValueError as exc:
        return note(str(exc), tone="warn"), no_update
    except Exception as exc:                               # noqa: BLE001
        log.exception("commit failed")
        return note(f"commit failed: {exc}", tone="warn"), no_update


@callback(Output("bk-download", "data"),
          Input("bk-export", "n_clicks"), Input("bk-rejects", "n_clicks"),
          State("bk-import-store", "data"), prevent_initial_call=True)
def _download(_a, _b, store):
    s = get_session()
    try:
        if ctx.triggered_id == "bk-rejects" and store:
            rep = s.store.parse_csv(store["text"], ctx=s.ctx(), mode=store["mode"],
                                    allow_expired=store["allow_expired"])
            return dict(content=rep.rejects_csv(), filename="rejects.csv")
        return dict(content=s.store.export_csv(),
                    filename=f"fxgamma_book_{datetime.now():%Y%m%d_%H%M}.csv")
    except Exception as exc:                               # noqa: BLE001
        log.exception("export failed")
        return no_update


#: Dash 4 resolves the page layout from the registry at request time, so bind it
#: explicitly now that `layout` is defined.
dash.page_registry[__name__]["layout"] = layout
