"""Page 8 - Data & Settings (`/data`).

**The manual vol-mark grid is the top panel, by ruling.**  Amendment v1.2 T-1 promoted the
desk's own ATM / 25d RR / 25d BF marks from a buried override to the primary input: the
book prices off the manual curve whenever one exists, a live pull can never overwrite it,
and the badge says which curve was used.  The target is under thirty seconds to mark the
whole G3 book, so there are two ways in and both are one action:

* **paste** a broker run straight from a chat or an email - the parser handles rows-per-
  tenor and columns-per-tenor layouts, percent or decimal, with or without a header, and
  shows you what it understood before anything is saved;
* **type** into the grid, which is pre-loaded with the G3 tenors and the current marks.

Below that: the source board (REQ-066), the provider toggle with a confirmation when
switching *to* synthetic while a real book is loaded (REQ-067), the active-overrides panel
(REQ-068), cache control (REQ-069) and preferences (REQ-071).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import dash
from dash import Input, Output, State, callback, ctx, dcc, html, no_update

from fxgamma.conventions import CCYS, G3, PAIRS, TENORS
from fxgamma.store import ManualQuote

from ..components.badges import (kind_of, provenance_badge, surface_status_badge,
                                 synthetic_banner)
from ..components.cards import empty_state, grid, kv, metric, metric_row, note, panel
from ..components.fmt import EM_DASH, fmt_vol, fmt_vol_pts
from ..components.tables import col, data_table
from ..state import ALL_PAIRS, G3_TENORS, get_session
from ..theme import ACCENT, KIND_COLORS, NEG, POS, TEXT_DIM, WARN

log = logging.getLogger(__name__)

dash.register_page(__name__, path="/data", name="Data & Settings", title="FX Gamma · Data")

TEMPLATE = """# paste your broker run here. Anything like this works:
#   tenor  atm    rr25    bf25
EURUSD
1W   7.35  -0.10  0.15
1M   7.05  -0.15  0.20
3M   7.30  -0.20  0.25
USDJPY
1M   9.25  -1.25  0.30
3M   9.60  -1.55  0.35
"""


# ====================================================================== layout
def layout(**_kw):
    s = get_session()
    snap = s.snapshot()
    return html.Div([
        synthetic_banner(snap.meta, page="data"),
        _mark_panel(s),
        html.Div(id="dt-marks-current"),
        grid([_source_panel(s), _provider_panel(s)], cols="3fr 2fr"),
        html.Div(id="dt-overrides"),
        grid([_cache_panel(s), _prefs_panel(s)], cols="1fr 1fr"),
        dcc.ConfirmDialog(
            id="dt-confirm-synthetic",
            message="Switch to the SYNTHETIC provider while a real book is loaded?\n\n"
                    "Every number on every page becomes SIMULATED. It will be badged "
                    "purple everywhere, but it is not a mark and it is not tradable."),
        dcc.Store(id="dt-pending-provider"),
    ])


def _mark_panel(s):
    marks = {(m.pair, m.tenor): m for p in (s.manual.store.pairs() if s.manual else [])
             for m in s.manual.store.get(p)}
    rows = []
    for pair in G3:
        for tenor in G3_TENORS:
            m = marks.get((pair, tenor))
            rows.append({"pair": pair, "tenor": tenor,
                         "atm": None if m is None else round(m.atm * 100, 4),
                         "rr25": None if m is None else round(m.rr25 * 100, 4),
                         "bf25": None if m is None else round(m.bf25 * 100, 4)})
    return panel([
        html.Div([
            html.Div([
                html.Label("paste a broker run (any layout)"),
                dcc.Textarea(id="dt-paste", value="", placeholder=TEMPLATE,
                             style={"height": "150px", "width": "100%"}),
                html.Div([
                    html.Div([html.Label("default pair (rows without one)"),
                              dcc.Dropdown(id="dt-paste-pair", options=ALL_PAIRS,
                                           value=None, clearable=True,
                                           placeholder="auto-detect")],
                             style={"flex": "1 1 160px"}),
                    html.Div([html.Label(" "),
                              html.Button("Parse", id="dt-parse", n_clicks=0)],
                             style={"flex": "0 0 auto"}),
                    html.Div([html.Label(" "),
                              html.Button("Save pasted marks", id="dt-save-paste",
                                          n_clicks=0, className="primary")],
                             style={"flex": "0 0 auto"}),
                    html.Div([html.Label(" "),
                              html.Button("Load template", id="dt-template", n_clicks=0)],
                             style={"flex": "0 0 auto"}),
                ], className="row", style={"marginTop": "6px"}),
            ], style={"flex": "1 1 420px"}),
            html.Div([
                html.Label("or type the G3 grid — vols in PERCENT (7.05), "
                           "RR/BF in vol points"),
                data_table("dt-grid", [
                    col("pair"), col("tenor"),
                    col("ATM", "atm", unit="%", numeric=True),
                    col("RR25", "rr25", unit="vol pts", numeric=True),
                    col("BF25", "bf25", unit="vol pts", numeric=True)],
                    rows, page_size=len(rows), sort=False, editable=True),
                html.Div([
                    html.Button("Save grid", id="dt-save-grid", n_clicks=0,
                                className="primary"),
                    html.Button("Clear all marks", id="dt-clear-marks", n_clicks=0,
                                className="danger"),
                ], style={"display": "flex", "gap": "8px", "marginTop": "8px"}),
            ], style={"flex": "1 1 420px"}),
        ], className="row", style={"alignItems": "flex-start"}),
        html.Div(id="dt-mark-out", style={"marginTop": "10px"}),
        note("Vols are typed in percent and stored as decimals. A saved mark flips that "
             "pair's surface status from INDICATIVE to MARK, is badged user_override "
             "everywhere it is used, and survives every live pull (amendment v1.2 T-1). "
             "It is written twice: to the SQLite book of record (versioned, so a stored "
             "P&L can be re-marked on the curve that was in force) and to the data "
             "layer's marks file, which is the first link of the provider chain."),
    ], title="mark the book — manual vol grid",
        sub="amendment v1.2 T-1 · target: under 30 seconds for G3")


def _source_panel(s):
    try:
        rows = s.source_status()
    except Exception as exc:                               # noqa: BLE001
        return panel(note(f"source board failed: {exc}", tone="warn"),
                     title="source status")
    data = [{"source": r.name, "status": ("ok" if r.ok else "failed"),
             "verified": "yes" if r.verified else "NEVER VERIFIED",
             "rows": EM_DASH if r.rows is None else r.rows,
             "latency": EM_DASH if r.latency_ms is None else f"{r.latency_ms:.0f} ms",
             "detail": r.detail} for r in rows]
    return panel([
        data_table("dt-sources", [col("source"), col("status"), col("verified"),
                                  col("rows", numeric=True), col("latency"),
                                  col("detail")], data, page_size=10, sort=False,
                   conditional=[
                       {"if": {"filter_query": '{status} = "failed"'}, "color": NEG},
                       {"if": {"filter_query": '{status} = "ok"', "column_id": "status"},
                        "color": POS},
                       {"if": {"filter_query": '{verified} = "NEVER VERIFIED"',
                               "column_id": "verified"}, "color": WARN}]),
        note("A source serving cached or synthetic values never shows 'ok' for live data — "
             "the kind is in the badge, not in this column. 'NEVER VERIFIED' means the "
             "endpoint could not be exercised from the build sandbox; run "
             "scripts/verify_live_sources.py on a networked machine (REQ-070)."),
    ], title="source status", sub="REQ-066")


def _provider_panel(s):
    return panel([
        html.Div([html.Label("provider"),
                  dcc.Dropdown(id="dt-provider",
                               options=["synthetic", "manual", "chain", "auto", "live",
                                        "cache"],
                               value=s.provider_name, clearable=False)]),
        html.Button("Apply provider", id="dt-apply-provider", n_clicks=0,
                    style={"marginTop": "8px"}),
        html.Div(id="dt-provider-out", style={"marginTop": "8px"}),
        note("chain = manual → live → cache. auto also falls through to synthetic and "
             "badges it. Market-data hosts are blocked in this sandbox, so live and chain "
             "will degrade — visibly, in the board above, never silently."),
    ], title="provider", sub="REQ-067")


def _cache_panel(s):
    try:
        df = s.cache().summary()
        n = len(df)
        oldest = EM_DASH if df.empty else str(df["fetched_at"].min())
        newest = EM_DASH if df.empty else str(df["fetched_at"].max())
        by_kind = ({} if df.empty else df.groupby("kind").size().to_dict())
    except Exception as exc:                               # noqa: BLE001
        return panel(note(f"cache unavailable: {exc}", tone="warn"), title="cache")
    return panel([
        kv([("entries", f"{n}"),
            ("by kind", ", ".join(f"{k}: {v}" for k, v in by_kind.items()) or EM_DASH),
            ("oldest", oldest), ("newest", newest),
            ("root", str(s.cache().root))]),
        html.Button("Clear cache", id="dt-clear-cache", n_clicks=0, className="danger",
                    style={"marginTop": "8px"}),
        html.Div(id="dt-cache-out", className="tiny", style={"marginTop": "6px"}),
        note("Clearing forces a live re-fetch, which is blocked in this sandbox and may "
             "leave you on synthetic data (charter section 3)."),
    ], title="cache", sub="REQ-069")


def _prefs_panel(s):
    return panel([
        html.Div([
            html.Div([html.Label("reporting currency"),
                      dcc.Dropdown(id="pf-ccy", options=CCYS, value=s.report_ccy,
                                   clearable=False)], style={"flex": "1 1 130px"}),
            html.Div([html.Label("display timezone"),
                      dcc.Dropdown(id="pf-tz",
                                   options=["Europe/London", "America/New_York",
                                            "Asia/Tokyo", "UTC", "Europe/Zurich"],
                                   value=s.setting("timezone", "Europe/London"),
                                   clearable=False)], style={"flex": "1 1 160px"}),
        ], className="row"),
        html.Div([
            html.Div([html.Label("surface method"),
                      dcc.Dropdown(id="pf-method",
                                   options=["vanna_volga", "sabr", "interp"],
                                   value=s.setting("surface_method", "vanna_volga"),
                                   clearable=False)], style={"flex": "1 1 150px"}),
            html.Div([html.Label("trading-day boundary"),
                      dcc.Dropdown(id="pf-boundary",
                                   options=["17:00 America/New_York",
                                            "16:00 Europe/London", "00:00 UTC"],
                                   value=s.setting("day_boundary",
                                                   "17:00 America/New_York"),
                                   clearable=False)], style={"flex": "1 1 190px"}),
        ], className="row"),
        html.Button("Save preferences", id="pf-save", n_clicks=0,
                    style={"marginTop": "8px"}),
        html.Div(id="pf-out", className="tiny", style={"marginTop": "6px"}),
        note("Reporting currency, timezone and the day boundary change the value of every "
             "number on every screen, so they are configuration, not taste (trader review "
             "note on REQ-071). They persist in SQLite."),
    ], title="preferences", sub="REQ-071")


# ====================================================================== marks
@callback(Output("dt-paste", "value"), Input("dt-template", "n_clicks"),
          prevent_initial_call=True)
def _load_template(_n):
    return TEMPLATE


def _parse(text, pair):
    from fxgamma.data.manual import parse_grid
    return parse_grid(text or "", pair or None, source="paste")


def _marks_table(grid_):
    data = [{"pair": m.pair, "tenor": m.tenor, "atm": round(m.atm * 100, 4),
             "rr25": round(m.rr25 * 100, 4), "bf25": round(m.bf25 * 100, 4),
             "T": round(m.T, 5)} for m in grid_.marks]
    return data_table("dt-parsed", [col("pair"), col("tenor"),
                                    col("ATM", "atm", unit="%", numeric=True),
                                    col("RR25", "rr25", unit="vol pts", numeric=True),
                                    col("BF25", "bf25", unit="vol pts", numeric=True),
                                    col("T", unit="years", numeric=True)],
                      data, page_size=20, sort=False)


@callback(Output("dt-mark-out", "children"),
          Output("snapshot-token", "data", allow_duplicate=True),
          Output("header-container", "children", allow_duplicate=True),
          Input("dt-parse", "n_clicks"), Input("dt-save-paste", "n_clicks"),
          Input("dt-save-grid", "n_clicks"), Input("dt-clear-marks", "n_clicks"),
          State("dt-paste", "value"), State("dt-paste-pair", "value"),
          State("dt-grid", "data"), prevent_initial_call=True)
def _marks(_a, _b, _c, _d, text, pair, grid_rows):
    from ..layout import header
    s = get_session()
    trig = ctx.triggered_id
    try:
        if trig == "dt-clear-marks":
            n = s.clear_marks()
            s.build()
            return (html.Div(f"cleared {n} stored mark(s); every pair is back to an "
                             "INDICATIVE surface", style={"color": WARN}),
                    s.token(), header(s))

        if trig in ("dt-parse", "dt-save-paste"):
            g = _parse(text, pair)
            if not g.ok:
                return (html.Div([
                    html.Div("nothing parsed", style={"color": NEG, "fontWeight": 700}),
                    html.Pre(g.summary(), style={"fontSize": "11px", "color": TEXT_DIM,
                                                 "whiteSpace": "pre-wrap"})]),
                    no_update, no_update)
            body = [html.Div(f"understood {len(g.marks)} mark(s) for "
                             f"{', '.join(g.pairs)} ({g.layout} layout)",
                             style={"color": POS, "fontWeight": 700}),
                    _marks_table(g)]
            if g.inferred or g.warnings or g.skipped:
                body.append(html.Pre("\n".join(
                    [f"inferred: {x}" for x in g.inferred]
                    + [f"WARNING: {x}" for x in g.warnings]
                    + [f"skipped: {x}" for x in g.skipped]),
                    style={"fontSize": "11px", "color": TEXT_DIM,
                           "whiteSpace": "pre-wrap"}))
            if trig == "dt-parse":
                body.insert(0, html.Div("nothing saved yet — press 'Save pasted marks'",
                                        className="tiny dim"))
                return html.Div(body), no_update, no_update
            quotes = [ManualQuote(m.pair, m.tenor, m.atm, m.rr25, m.bf25, m.rr10, m.bf10,
                                  source="paste") for m in g.marks]
            s.save_marks(quotes, note="pasted on the Data page")
            s.build()
            body.insert(0, html.Div(
                f"SAVED — {', '.join(g.pairs)} now price off YOUR curve",
                style={"color": KIND_COLORS["user_override"], "fontWeight": 800}))
            return html.Div(body), s.token(), header(s)

        # save the typed grid
        quotes = []
        bad = []
        for r in (grid_rows or []):
            if r.get("atm") in (None, "", "-"):
                continue
            try:
                atm = float(r["atm"]) / 100.0
                rr = float(r.get("rr25") or 0.0) / 100.0
                bf = float(r.get("bf25") or 0.0) / 100.0
                quotes.append(ManualQuote(r["pair"], r["tenor"], atm, rr, bf,
                                          source="typed"))
            except Exception as exc:                       # noqa: BLE001
                bad.append(f"{r.get('pair')} {r.get('tenor')}: {exc}")
        if not quotes:
            return (note("no ATM values typed — fill at least one row", tone="warn"),
                    no_update, no_update)
        s.save_marks(quotes, note="typed on the Data page")
        s.build()
        return (html.Div([
            html.Div(f"SAVED {len(quotes)} mark(s) — "
                     f"{', '.join(sorted({q.pair for q in quotes}))} now price off YOUR "
                     "curve", style={"color": KIND_COLORS["user_override"],
                                     "fontWeight": 800}),
            note("; ".join(bad), tone="warn") if bad else "",
        ]), s.token(), header(s))
    except Exception as exc:                               # noqa: BLE001
        log.exception("marks failed")
        return note(f"marks failed: {exc}", tone="warn"), no_update, no_update


@callback(Output("dt-marks-current", "children"), Output("dt-overrides", "children"),
          Input("snapshot-token", "data"))
def _current(_token):
    s = get_session()
    try:
        snap = s.snapshot()
        rows = s.store.manual_quotes()
        if rows:
            data = [{"pair": q.pair, "tenor": q.tenor, "atm": fmt_vol(q.atm),
                     "rr25": fmt_vol_pts(q.rr25), "bf25": fmt_vol_pts(q.bf25),
                     "asof": f"{q.asof:%Y-%m-%d %H:%MZ}" if q.asof else EM_DASH,
                     "age_h": (round((datetime.now(timezone.utc) - q.asof).total_seconds()
                                     / 3600, 1) if q.asof else None),
                     "source": q.source} for q in rows]
            body = [
                html.Div([surface_status_badge(snap.meta, p) for p in
                          sorted({q.pair for q in rows})],
                         style={"marginBottom": "8px"}),
                data_table("dt-marks-tbl",
                           [col("pair"), col("tenor"), col("ATM", "atm"),
                            col("RR25", "rr25"), col("BF25", "bf25"),
                            col("marked at", "asof"),
                            col("age", "age_h", unit="hours", numeric=True),
                            col("source")], data, page_size=15, sort=False,
                           conditional=[{"if": {"filter_query": "{age_h} > 12",
                                                "column_id": "age_h"},
                                         "color": WARN, "fontWeight": "700"}]),
                note("A mark over 12 hours old is amber: re-mark before hedging off it."),
            ]
        else:
            body = [empty_state("no manual marks — the book is priced off an INDICATIVE "
                                "surface",
                                "paste your morning run above; it takes about 20 seconds "
                                "for G3")]
        # active overrides (REQ-068)
        ov = [(k, p) for k, p in snap.meta.items()
              if getattr(p, "kind", "") == "user_override"]
        ov_body = ([data_table("dt-ov", [col("field"), col("source"), col("asof"),
                                         col("note")],
                               [{"field": k, "source": p.source,
                                 "asof": f"{p.asof:%Y-%m-%d %H:%MZ}" if p.asof else EM_DASH,
                                 "note": p.note} for k, p in sorted(ov)],
                               page_size=15, sort=False)]
                   if ov else [empty_state("no active overrides")])
        return (panel(body, title="current marks", sub="the book of record"),
                panel(ov_body + [note("Every override is listed here and badged blue "
                                      "wherever it is used. Clear them individually with "
                                      "the grid above (REQ-068).")],
                      title="active overrides", sub="REQ-068"))
    except Exception as exc:                               # noqa: BLE001
        log.exception("marks panel failed")
        m = note(f"marks panel failed: {exc}", tone="warn")
        return m, m


# ====================================================================== provider / cache / prefs
@callback(Output("dt-confirm-synthetic", "displayed"), Output("dt-pending-provider", "data"),
          Input("dt-apply-provider", "n_clicks"), State("dt-provider", "value"),
          prevent_initial_call=True)
def _ask_provider(_n, value):
    s = get_session()
    has_book = not s.store.is_empty()
    if value == "synthetic" and has_book and s.provider_name != "synthetic":
        return True, value
    return False, value


@callback(Output("dt-provider-out", "children"),
          Output("snapshot-token", "data", allow_duplicate=True),
          Output("header-container", "children", allow_duplicate=True),
          Input("dt-apply-provider", "n_clicks"),
          Input("dt-confirm-synthetic", "submit_n_clicks"),
          State("dt-pending-provider", "data"), prevent_initial_call=True)
def _apply_provider(_n, _c, value):
    from ..layout import header
    s = get_session()
    try:
        if value is None:
            return no_update, no_update, no_update
        if (ctx.triggered_id == "dt-apply-provider" and value == "synthetic"
                and not s.store.is_empty() and s.provider_name != "synthetic"):
            return no_update, no_update, no_update          # waiting on the confirmation
        s.provider_name = value
        s.build()
        msg = [html.Div(f"provider is now '{value}'; the snapshot was restamped",
                        style={"color": POS, "fontWeight": 700})]
        if s.errors:
            msg.append(note(" · ".join(s.errors[:4]), tone="warn"))
        return html.Div(msg), s.token(), header(s)
    except Exception as exc:                               # noqa: BLE001
        log.exception("provider switch failed")
        return note(f"provider switch failed: {exc}", tone="warn"), no_update, no_update


@callback(Output("dt-cache-out", "children"), Input("dt-clear-cache", "n_clicks"),
          prevent_initial_call=True)
def _clear_cache(_n):
    s = get_session()
    try:
        n = s.cache().clear()
        return f"cleared {n} cache file(s). A live re-fetch may be blocked here."
    except Exception as exc:                               # noqa: BLE001
        return f"cache clear failed: {exc}"


@callback(Output("pf-out", "children"),
          Output("header-container", "children", allow_duplicate=True),
          Input("pf-save", "n_clicks"), State("pf-ccy", "value"), State("pf-tz", "value"),
          State("pf-method", "value"), State("pf-boundary", "value"),
          prevent_initial_call=True)
def _save_prefs(_n, ccy, tz, method, boundary):
    from ..layout import header
    s = get_session()
    try:
        rebuild = method != s.setting("surface_method")
        s.set_setting("report_ccy", ccy)
        s.set_setting("timezone", tz)
        s.set_setting("surface_method", method)
        s.set_setting("day_boundary", boundary)
        if rebuild:
            s.build(method=method)
        return (f"saved — reporting {ccy}, timezone {tz}, surface {method}, "
                f"day boundary {boundary}"), header(s)
    except Exception as exc:                               # noqa: BLE001
        return f"save failed: {exc}", no_update


#: Dash 4 resolves the page layout from the registry at request time, so bind it
#: explicitly now that `layout` is defined.
dash.page_registry[__name__]["layout"] = layout
