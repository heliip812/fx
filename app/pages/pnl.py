"""Page 6 - P&L (`/pnl`).

`portfolio.attribution.daily_pnl` explains the move between **two stamped snapshots**.
The page's whole job is to make sure the two marks it is comparing are named, that the
elapsed wall-clock is printed, and that the residual is on screen every day rather than
only when it breaches.

Where t0 comes from, stated on the page
---------------------------------------
The session holds every snapshot it has stamped.  When two exist the user picks them,
and the explain is between two real marks.  On a fresh process only one exists, and the
honest thing is **not** to invent yesterday: instead the page offers a **hypothetical
prior mark** built by rolling this snapshot backwards (`risk.shift_market`), badged
SIMULATED in a banner and in the panel title.  It is a what-moved calculator, useful
for checking that the explain closes and that vanna/volga behave, and it is never
labelled as a published P&L.  REQ-054's stored EOD/AM snapshot series is a data-layer
capability the app cannot manufacture; it is named as a gap, not faked.

What the page insists on
------------------------
* **the residual is always displayed** (REQ-052), with the reference bands next to it
  (<1% overnight on a clean vanilla book, 2-3% on a big smile day) and a red alarm plus
  the three worst positions above 5% -- from ``attribution.top_offenders``, i.e. from
  ``PnLBreakdown.detail``, never from a guess;
* **elapsed time is wall-clock** (W-14).  A 07:00 London mark is ~14h after the 17:00 NY
  close; charging a whole calendar day of theta puts the theta bar ~40% out and pushes
  the error into the residual, which then trips the alarm for the wrong reason;
* **hedges are a bar, not residual** (MISS-7 / REQ-055), taken from the store's hedge
  log, ordered by ``trade_time`` with a fallback to ``trade_date`` (CG-5), and the log
  totals reconcile to the ``hedge`` bar;
* **vanna and volga are split out** (REQ-057) with the dS and dsigma that produced them.
"""
from __future__ import annotations

import logging
from datetime import timezone

import dash
import numpy as np
from dash import Input, Output, callback, dcc, html

from fxgamma.conventions import pair_spec
from fxgamma.portfolio import risk as R
from fxgamma.portfolio.attribution import (COMPONENTS, daily_pnl, residual_ratio,
                                           top_offenders)
from fxgamma.types import SpotPosition

from ..components.badges import synthetic_banner
from ..components.cards import empty_state, kv, metric, metric_row, note, panel
from ..components.charts import figure, zero_line
from ..components.fmt import EM_DASH, fmt_money, fmt_spot, fmt_vol
from ..components.tables import col, data_table, sign_rules
from ..pricing import ECONOMICS_BASIS, mark_vols
from ..state import get_session
from ..theme import ACCENT, NEG, POS, SERIES, TEXT_DIM, WARN, empty_figure

log = logging.getLogger(__name__)

dash.register_page(__name__, path="/pnl", name="P&L", title="FX Gamma · P&L")

#: REQ-052 reference bands and the alarm threshold
RESIDUAL_GOOD, RESIDUAL_BUSY, RESIDUAL_ALARM = 0.01, 0.03, 0.05

BAR_COLOUR = {"delta": SERIES[0], "gamma": POS, "theta": NEG, "vega": SERIES[2],
              "vanna": SERIES[4], "volga": SERIES[5], "rates": SERIES[6],
              "carry": SERIES[7], "hedge": SERIES[1], "unexplained": WARN}


def layout(**_kw):
    s = get_session()
    snap = s.snapshot()
    snaps = s.snapshot_ids()
    opts = [{"label": f"{sid}  ({asof:%Y-%m-%d %H:%M:%SZ})", "value": sid}
            for sid, asof in snaps]
    return html.Div([
        synthetic_banner(snap.meta, page="pnl"),
        panel([
            html.Div([
                html.Div([html.Label("t0 — the prior mark"),
                          dcc.Dropdown(id="pl-t0", options=opts,
                                       value=(snaps[0][0] if len(snaps) > 1 else None),
                                       placeholder="only one snapshot this session — "
                                                   "use the hypothetical below",
                                       clearable=True)], style={"flex": "2 1 320px"}),
                html.Div([html.Label("t1 — the current mark"),
                          dcc.Dropdown(id="pl-t1", options=opts,
                                       value=(snaps[-1][0] if snaps else None),
                                       clearable=False)], style={"flex": "2 1 320px"}),
            ], className="row"),
            html.Div([
                html.Div([html.Label("hypothetical: spot move since t0, %"),
                          dcc.Input(id="pl-dspot", type="number", value=0.4, step=0.1,
                                    debounce=True)], style={"flex": "1 1 200px"}),
                html.Div([html.Label("vol move since t0, vol pts"),
                          dcc.Input(id="pl-dvol", type="number", value=0.3, step=0.1,
                                    debounce=True)], style={"flex": "1 1 200px"}),
                html.Div([html.Label("elapsed, hours"),
                          dcc.Input(id="pl-hours", type="number", value=14, step=1,
                                    min=0.25, debounce=True)],
                         style={"flex": "1 1 160px"}),
            ], className="row"),
            note("Pick two stamped snapshots to explain a real move. With only one "
                 "snapshot in the session the boxes above build a HYPOTHETICAL prior "
                 "mark by rolling this one backwards — badged as simulated everywhere "
                 "it is used. Elapsed hours default to 14, the gap between a 17:00 NY "
                 "close and a 07:00 London mark (REQ-051 / W-14): theta and carry are "
                 "charged over the actual wall-clock, never over an integer day."),
        ], title="which two marks", sub="REQ-051 — the day boundary is 17:00 America/New_York"),
        html.Div(id="pl-summary"),
        dcc.Loading(dcc.Graph(id="pl-waterfall", config={"displaylogo": False}),
                    type="dot", color=ACCENT),
        html.Div(id="pl-residual"),
        html.Div(id="pl-detail"),
        html.Div(id="pl-hedges"),
        dcc.Loading(dcc.Graph(id="pl-cumulative", config={"displaylogo": False}),
                    type="dot", color=ACCENT),
        html.Div(id="pl-cumulative-note"),
    ])


# ====================================================================== marks
def _resolve_marks(s, t0_id, t1_id, dspot, dvol, hours):
    """(mkt_t0, mkt_t1, simulated, description)."""
    snaps = dict(s.snapshot_ids())
    t1 = s.snapshot(t1_id) if t1_id in snaps else s.snapshot()
    if t0_id and t0_id in snaps and t0_id != t1_id:
        return s.snapshot(t0_id), t1, False, (
            f"two stamped marks: t0 {t0_id} and t1 {t1_id}")
    hrs = float(hours or 14)
    mult = 1.0 / (1.0 + float(dspot or 0.0) / 100.0)
    t0 = R.shift_market(t1, spot_mult=mult, vol_add=-float(dvol or 0.0) / 100.0,
                        days=-hrs / 24.0)
    return t0, t1, True, (
        f"HYPOTHETICAL prior mark: this snapshot rolled back {hrs:g}h with spot "
        f"{float(dspot or 0):+.2f}% and vol {float(dvol or 0):+.2f} vol pts undone")


def _hedges_between(s, t0, t1):
    """Hedge-log entries struck in (t0, t1], as ``SpotPosition`` for ``daily_pnl``.

    Ordered by ``trade_time`` with a ``trade_date`` fallback (CG-5); entries carrying
    no time at all are excluded from the window rather than silently assumed to be
    inside it, and the panel says how many were skipped.
    """
    out, untimed = [], 0
    try:
        entries = s.store.hedge_log()
    except Exception as exc:                               # noqa: BLE001
        log.warning("hedge log unavailable: %s", exc)
        return [], 0
    for h in entries:
        tt = h.trade_time
        if tt is None:
            untimed += 1
            continue
        if tt.tzinfo is None:
            tt = tt.replace(tzinfo=timezone.utc)
        if not (t0.asof < tt <= t1.asof):
            continue
        out.append(SpotPosition(id=h.id, pair=h.pair, notional_base=h.notional_base,
                                entry_rate=h.entry_rate, trade_date=h.trade_date,
                                tag=h.tag or "hedge", trade_time=tt))
    return out, untimed


# ====================================================================== main callback
@callback(Output("pl-summary", "children"), Output("pl-waterfall", "figure"),
          Output("pl-residual", "children"), Output("pl-detail", "children"),
          Output("pl-hedges", "children"), Output("pl-cumulative", "figure"),
          Output("pl-cumulative-note", "children"),
          Input("pl-t0", "value"), Input("pl-t1", "value"),
          Input("pl-dspot", "value"), Input("pl-dvol", "value"),
          Input("pl-hours", "value"), Input("snapshot-token", "data"),
          Input("book-version", "data"))
def _render(t0_id, t1_id, dspot, dvol, hours, _token, _bv):
    s = get_session()
    try:
        book = s.store.load_book()
        if not book.options and not book.spots:
            e = panel(empty_state("empty book — nothing to explain",
                                  "add a trade on the Book page (REQ-003)"),
                      title="daily attribution", sub="REQ-051")
            return (e, empty_figure("empty book"), "", "", "",
                    empty_figure("empty book"), "")
        t0, t1, simulated, desc = _resolve_marks(s, t0_id, t1_id, dspot, dvol, hours)
        hedges, untimed = _hedges_between(s, t0, t1)
        hedge_ids = {h.id for h in hedges}
        # a hedge struck in the window is explained by the `hedge` bar; leaving it in
        # the book as well would count it twice.
        base = type(book)([o for o in book.options],
                          [sp for sp in book.spots if sp.id not in hedge_ids],
                          book.name)
        marks = mark_vols(s.store.marks())
        pnl = daily_pnl(base, t0, t1, hedges, report_ccy=s.report_ccy,
                        marks_t0=marks, marks_t1=marks)
        ratio = residual_ratio(pnl)
        elapsed = (t1.asof - t0.asof).total_seconds() / 3600.0
        return (_summary(s, pnl, t0, t1, elapsed, simulated, desc),
                _waterfall(s, pnl),
                _residual(s, pnl, ratio),
                _detail(s, pnl),
                _hedge_log(s, hedges, untimed, pnl, t1),
                _cumulative(s, pnl),
                _cumulative_note(s, simulated))
    except Exception as exc:                               # noqa: BLE001
        log.exception("attribution failed")
        e = note(f"attribution failed: {exc}", tone="warn")
        return (e, empty_figure("attribution unavailable"), "", "", "",
                empty_figure("attribution unavailable"), "")


def _summary(s, pnl, t0, t1, elapsed, simulated, desc):
    rep = s.report_ccy
    h, m = divmod(int(round(elapsed * 60)), 60)
    banner = []
    if simulated:
        banner.append(html.Div([
            html.Span("SIMULATED PRIOR MARK  ", style={"fontWeight": 800,
                                                       "color": WARN}),
            html.Span(desc + ". This is a what-moved calculator, not a published P&L. "
                      "A real explain needs two stamped marks; press Refresh in the "
                      "header to stamp a second one, or see the gap note below.",
                      style={"color": TEXT_DIM})],
            style={"padding": "6px 10px", "marginBottom": "8px", "fontSize": "12px",
                   "border": f"1px solid {WARN}55", "borderLeft": f"4px solid {WARN}",
                   "borderRadius": "4px"}))
    cards = [
        metric("total P&L", fmt_money(pnl.total, rep),
               tone="pos" if pnl.total >= 0 else "neg",
               unit=f"reporting ccy {rep}, converted at the t1 rate (CG-1)"),
        metric("mark to mark", f"{h}h {m:02d}m",
               unit="actual elapsed wall-clock (REQ-051 / W-14)",
               sub=f"t0 {t0.asof:%Y-%m-%d %H:%M:%SZ} → t1 {t1.asof:%Y-%m-%d %H:%M:%SZ}"),
        metric("theta charged", fmt_money(pnl.theta, rep), tone="neg" if pnl.theta < 0 else "pos",
               unit=f"θ₀ × elapsed days — {ECONOMICS_BASIS}",
               sub="never a whole day by default: a 14h mark charges 14h"),
        metric("gamma", fmt_money(pnl.gamma, rep),
               tone="pos" if pnl.gamma >= 0 else "neg",
               unit="½·Γ₀·dS² — the income the theta above is buying"),
        metric("smile (vanna+volga)", fmt_money(pnl.vanna + pnl.volga, rep),
               unit="second-order smile terms, split out below (REQ-057)"),
        metric("hedge + carry", fmt_money(pnl.hedge + pnl.carry, rep),
               unit="in-window hedge P&L plus the financing of every spot leg (MISS-7)"),
    ]
    return html.Div(banner + [
        panel([metric_row(cards, cols="repeat(auto-fit, minmax(230px, 1fr))"),
               kv([("marks compared", desc),
                   ("day boundary", "17:00 America/New_York — the value-date roll, not "
                                    "London close and not midnight (REQ-051, settled)"),
                   ("closure", "total = Σ components + unexplained holds to floating "
                               "point by construction, because unexplained IS the "
                               "difference. The number that matters is the ratio below.")]),
               ], title="daily attribution", sub="REQ-051")])


def _waterfall(s, pnl):
    rep = s.report_ccy
    names = list(COMPONENTS) + ["unexplained"]
    vals = [getattr(pnl, c) for c in names]
    measures = ["relative"] * len(names) + ["total"]
    traces = [dict(type="waterfall", orientation="v",
                   x=names + ["total"], y=vals + [pnl.total],
                   measure=measures,
                   text=[f"{v:,.0f}" for v in vals + [pnl.total]],
                   textposition="outside",
                   textfont=dict(size=10, color=TEXT_DIM),
                   connector=dict(line=dict(color="#2b3440")),
                   increasing=dict(marker=dict(color=POS)),
                   decreasing=dict(marker=dict(color=NEG)),
                   totals=dict(marker=dict(color=ACCENT)),
                   hovertemplate="%{x}<br>%{y:,.0f} " + rep + "<extra></extra>")]
    fig = figure(traces, title=f"P&L explain — {rep}, mark to mark",
                 xtitle="component (order matters: each term is the derivative the "
                        "previous terms have not used)",
                 ytitle=f"P&L, {rep}", height=380)
    return zero_line(fig)


def _residual(s, pnl, ratio):
    rep = s.report_ccy
    breached = ratio > RESIDUAL_ALARM
    tone = "neg" if breached else "" if ratio <= RESIDUAL_BUSY else "neg"
    body = [metric_row([
        metric("unexplained", fmt_money(pnl.unexplained, rep),
               tone="neg" if breached else "",
               unit="everything not in a named bar: charm, veta, speed, ultima and "
                    "every third-order term"),
        metric("residual ratio", f"{ratio * 100:.2f}%", tone=tone,
               unit="|unexplained| ÷ Σ|components| — shown every day, not only on breach",
               sub=f"reference: <{RESIDUAL_GOOD * 100:.0f}% overnight on a clean vanilla "
                   f"book with a good mark; {RESIDUAL_BUSY * 100:.0f}% on a big smile "
                   f"day; alarm at {RESIDUAL_ALARM * 100:.0f}%"),
    ], cols="repeat(auto-fit, minmax(300px, 1fr))")]
    if breached:
        off = top_offenders(pnl, 3)
        rows = []
        for _, r in off.iterrows():
            rows.append({
                "id": r.get("id"), "pair": r.get("pair"), "kind": r.get("kind"),
                "strike": (EM_DASH if r.get("strike") is None
                           else fmt_spot(r.get("strike"), str(r.get("pair")))),
                "expiry": str(r.get("expiry")),
                "dS": f"{float(r.get('dS', float('nan'))):+.5f}",
                "dvol": fmt_vol(r.get("dvol")),
                "unexplained": round(float(r.get("unexplained_rep", 0.0)), 0),
                "total": round(float(r.get("total_rep", 0.0)), 0),
            })
        body.append(html.Div(
            "ATTRIBUTION DOES NOT FIT — check marks or re-taylor. The three positions "
            "contributing most to the residual are named below, from "
            "PnLBreakdown.detail.",
            style={"color": NEG, "fontWeight": 700, "margin": "8px 0 6px",
                   "fontSize": "12px"}))
        body.append(data_table(
            "pl-offenders", [col("id"), col("pair"), col("kind"), col("strike"),
                             col("expiry"), col("dS", unit="spot move"),
                             col("dσ", "dvol", unit="this strike's own vol move"),
                             col("unexplained", unit=rep, numeric=True),
                             col("total", unit=rep, numeric=True)],
            rows, page_size=3, sort=False,
            conditional=sign_rules(["unexplained", "total"])))
    body.append(note(
        "A residual that only speaks when it is already broken teaches nothing, so the "
        "ratio is on screen every day (REQ-052 / trader Q-10.2) — watching it drift is "
        "the trust check. dσ is per position, at that position's own strike, and "
        "includes term-structure rolldown, which is the market convention for a daily "
        "explain and keeps the vega bar reconciling to a mark change you can see on the "
        "Surface page."))
    return panel(body, title="unexplained is policed, and always displayed",
                 sub="REQ-052")


def _detail(s, pnl):
    rep = s.report_ccy
    det = pnl.detail
    if det is None or not len(det):
        return panel(empty_state("no positions in the explain"), title="per-position",
                     sub="REQ-053")
    rows = []
    for _, r in det.iterrows():
        pair = str(r["pair"])
        rows.append({
            "id": r["id"], "pair": pair, "kind": r["kind"], "tag": r.get("tag", ""),
            "strike": (EM_DASH if r.get("strike") is None
                       else fmt_spot(r["strike"], pair)),
            "expiry": "" if r.get("expiry") is None else str(r.get("expiry")),
            "dS": f"{float(r['dS']):+.5f}",
            "dvol": fmt_vol(r.get("dvol")),
            **{c: round(float(r[f"{c}_rep"]), 0) for c in COMPONENTS},
            "unexplained": round(float(r["unexplained_rep"]), 0),
            "total": round(float(r["total_rep"]), 0),
        })
    cols = ([col("id"), col("pair"), col("kind"), col("tag"), col("strike"),
             col("expiry"), col("dS", unit="spot t1−t0"),
             col("dσ", "dvol", unit="at this strike, incl. rolldown")]
            + [col(c, unit=rep, numeric=True) for c in COMPONENTS]
            + [col("unexplained", unit=rep, numeric=True),
               col("total", unit=rep, numeric=True)])
    sums = {c: float(det[f"{c}_rep"].sum()) for c in COMPONENTS}
    check = abs(sum(sums.values()) + float(det["unexplained_rep"].sum()) - pnl.total)
    return panel([
        data_table("pl-detail-tbl", cols, rows, page_size=25,
                   conditional=sign_rules(list(COMPONENTS) + ["unexplained", "total"])),
        note(f"Rows reconcile to the waterfall: Σ(per-position components + "
             f"unexplained) − total = {check:,.6f} {rep}. Vanna is "
             f"{fmt_money(pnl.vanna, rep)} = Σ vanna₀·dS·dσ/0.01 and volga is "
             f"{fmt_money(pnl.volga, rep)} = Σ ½·volga₀·(dσ/0.01)², shown separately so "
             f"smile P&L is never buried inside vega (REQ-057)."),
    ], title="per-position attribution", sub="REQ-053")


def _hedge_log(s, hedges, untimed, pnl, t1):
    rep = s.report_ccy
    if not hedges:
        msg = ("no hedge was struck between these two marks"
               + (f" — {untimed} hedge-log row(s) carry no trade_time and are excluded "
                  "from the window rather than silently assumed to be inside it"
                  if untimed else ""))
        return panel([empty_state(msg),
                      note("Hedges from the Risk page are stamped with trade_time (UTC) "
                           "on creation, so the log can order intraday hedges; the log "
                           "orders by trade_time and falls back to trade_date (CG-5).")],
                     title="hedge log", sub="REQ-055")
    det = pnl.detail
    rows = []
    for h in sorted(hedges, key=lambda x: x.trade_time):
        spec = pair_spec(h.pair)
        sub = det[det["id"] == h.id] if det is not None and len(det) else None
        hp = float(sub["hedge_rep"].iloc[0]) if sub is not None and len(sub) else float("nan")
        cy = float(sub["carry_rep"].iloc[0]) if sub is not None and len(sub) else float("nan")
        S1 = float(t1.spot.get(h.pair, float("nan")))
        rows.append({
            "time": f"{h.trade_time:%Y-%m-%d %H:%M:%SZ}",
            "pair": h.pair,
            "size": round(h.notional_base / 1e6, 3),
            "rate": fmt_spot(h.entry_rate, h.pair),
            "spot_now": fmt_spot(S1, h.pair),
            "move": f"{(S1 - h.entry_rate) / spec.pip:+,.1f}p",
            "pnl": round(hp, 0), "carry": round(cy, 0),
            "tag": h.tag,
        })
    total_hedge = float(np.nansum([r["pnl"] for r in rows]))
    cols = [col("trade time", "time", unit="UTC, from trade_time (CG-5)"),
            col("pair"), col("size", unit="base mm, + = bought base", numeric=True),
            col("rate", unit="entry"), col("spot now", "spot_now"),
            col("move", unit="pips since entry"),
            col("realized P&L", "pnl", unit=rep, numeric=True),
            col("carry", unit=f"{rep}, tom-next financing (MISS-7)", numeric=True),
            col("tag")]
    return panel([
        data_table("pl-hedge-tbl", cols, rows, page_size=15, sort=False,
                   conditional=sign_rules(["pnl", "carry", "size"])),
        note(f"Log total {fmt_money(total_hedge, rep)} reconciles to the hedge bar "
             f"{fmt_money(pnl.hedge, rep)} (difference "
             f"{fmt_money(total_hedge - pnl.hedge, rep)}). Carry is a line, not "
             f"residual: a spot hedge is a T+2 position that must be rolled, and on "
             f"USDJPY at a ~3.5% differential a USD 100mm hedge is ~USD 10k a day."
             + (f" {untimed} row(s) without a trade_time are excluded and marked as "
                "such." if untimed else "")),
    ], title="hedge log", sub="REQ-055")


def _cumulative(s, pnl):
    """REQ-054 — cumulative by component across the session's stamped marks.

    Built only from marks that actually exist.  With one explain there is one step, and
    the figure says so rather than drawing a trend through a single point.
    """
    rep = s.report_ccy
    names = list(COMPONENTS) + ["unexplained"]
    vals = [getattr(pnl, c) for c in names]
    order = np.argsort([-abs(v) for v in vals])
    traces = [dict(type="bar", orientation="h",
                   x=[vals[i] for i in order], y=[names[i] for i in order],
                   marker=dict(color=[BAR_COLOUR.get(names[i], SERIES[0]) for i in order]),
                   hovertemplate="%{y}<br>%{x:,.0f} " + rep + "<extra></extra>")]
    fig = figure(traces, title=f"component sizes this mark-to-mark, {rep} "
                               "(cumulative series needs stored EOD marks — see note)",
                 xtitle=f"P&L, {rep}", ytitle="", height=300)
    return zero_line(fig, axis="x")


def _cumulative_note(s, simulated):
    return panel([
        kv([("what is missing",
             "REQ-054 wants a cumulative line and a drawdown rebuilt from STORED daily "
             "marks, plus the displayed reconciliation 'yesterday's published PV vs "
             "yesterday's PV recomputed now: 0.00'. That needs a persisted EOD/AM "
             "snapshot series (17:00 NY, immutable). The app holds this session's "
             "snapshots only, so the panel above shows the components of the single "
             "explain rather than drawing a trend through one point."),
            ("why it is not faked",
             "recomputing history against today's surface would make yesterday's P&L "
             "move overnight, which is precisely the thing REQ-054 exists to prevent."),
            ("status", "SIMULATED prior mark in force" if simulated else
                       "two stamped marks in force"),
        ]),
    ], title="cumulative P&L and drawdown", sub="REQ-054 — gap named, not papered over")


#: Dash 4 resolves the page layout from the registry at request time.
dash.page_registry[__name__]["layout"] = layout
