"""Page 5 - Risk (`/risk`).

The page the trader opens first, so it is built around MISS-4's question -- *am I long
or short gamma, and what does today cost* -- and answers it in the first line, before
any grid.

Everything here is a call into `fxgamma.portfolio`; no risk arithmetic lives in this
file (CG-3: "cost-aware band maths lives in the library, never in ``app/``", and the
same discipline applies to zones, ladders and scenarios).  What the page owns is the
choice of what to show and the honesty of the labels:

* **the sticky toggle is not cosmetic** (REQ-040).  Sticky-strike holds sigma(K) fixed;
  sticky-delta rides the smile with spot and injects the skew delta ``nu dsigma/dS``
  into every delta on the curve.  The ladder therefore reports the *effective* gamma
  under the active convention (``gamma_1pct_fd``) alongside Black-Scholes
  ``gamma_1pct``, and **``skew_gamma_1pct`` sits next to the toggle** -- amendment v1.6
  records a measured 4.1% gap on the reference book, and a trader hedging off BS gamma
  under a sticky-delta assumption would silently mis-size by that much.
* **zones carry their sensitivity, not just their location** (REQ-042 / amendment v1.6
  CR-4).  ``gamma_zones`` returns ``GammaZoneDetail``, and the extra fields -- sigma-days
  on sqrt(252), touch probability, contributing strikes and expiries, P&L to the centre
  and the delta carried inside -- *are* the substance of "how sensitive is each gamma
  zone".  They are surfaced in the table and on the ladder, not left in the object.
* **the two bases are printed** (W-7 / amendment v1.6).  Distance, sigma-days and touch
  probability are sqrt(252); the breakeven and theta are sqrt(365).  Every panel that
  shows either says which, so they can never be read as the same number.
* **no hedge instruction off an INDICATIVE surface** (REQ-068(c) / REQ-043).  The
  instruction is greyed with the reason and names what would re-enable it.
"""
from __future__ import annotations

import logging

import dash
import numpy as np
from dash import Input, Output, callback, dcc, html

from fxgamma.conventions import expiry_datetime, pair_spec
from fxgamma.portfolio import risk as R
from fxgamma.portfolio import zones as Z
from fxgamma.portfolio.hedging import hedge_suggestion
from fxgamma.signals.richness import coverage_ratio
from fxgamma.types import HedgeRule

from ..components.badges import (provenance_badge, surface_status, surface_status_badge,
                                 synthetic_banner)
from ..components.cards import (divider, empty_state, kv, metric, metric_row, note,
                                panel)
from ..components.charts import figure, spot_line, zero_line
from ..components.fmt import (EM_DASH, fmt_mm, fmt_money, fmt_pct, fmt_spot, fmt_vol,
                              greek_unit)
from ..components.tables import col, data_table, sign_rules
from ..pricing import (DISTANCE_BASIS, ECONOMICS_BASIS, aggregate, headline,
                       mark_vols, price_frame, sigma_day_move)
from ..state import get_session
from ..theme import (ACCENT, BORDER, DIVERGING, NEG, POS, SERIES, TEXT_DIM, WARN,
                     empty_figure)

log = logging.getLogger(__name__)

dash.register_page(__name__, path="/risk", name="Risk", title="FX Gamma · Risk")

STICKY_OPTIONS = [
    {"label": "sticky strike — σ(K) pinned; delta is Black-Scholes", "value": "strike"},
    {"label": "sticky delta — smile rides spot; adds skew delta ν·∂σ/∂S", "value": "delta"},
    {"label": "none — vols pinned at base spot (fast path, = strike here)", "value": "none"},
]


# ====================================================================== layout
def layout(**_kw):
    s = get_session()
    snap = s.snapshot()
    pairs = s.store.load_book().pairs() or [s.setting("pair", "EURUSD")]
    pair = s.setting("pair", "EURUSD")
    if pair not in pairs:
        pair = pairs[0]
    return html.Div([
        synthetic_banner(snap.meta, page="risk"),
        html.Div(id="rk-headline"),
        panel(html.Div([
            html.Div([html.Label("pair"),
                      dcc.Dropdown(id="rk-pair", options=pairs, value=pair,
                                   clearable=False)], style={"flex": "1 1 150px"}),
            html.Div([html.Label("smile dynamic (sticky)"),
                      dcc.Dropdown(id="rk-sticky", options=STICKY_OPTIONS,
                                   value="strike", clearable=False)],
                     style={"flex": "2 1 380px"}),
            html.Div([html.Label("ladder range, ± % of spot"),
                      dcc.Slider(id="rk-range", min=1, max=10, step=1, value=5,
                                 marks={i: f"{i}%" for i in (1, 3, 5, 8, 10)})],
                     style={"flex": "2 1 260px"}),
            html.Div([html.Label("days forward"),
                      dcc.Dropdown(id="rk-days", options=[0, 1, 2, 3, 5, 7, 14],
                                   value=0, clearable=False)],
                     style={"flex": "0 0 120px"}),
        ], className="row"),
            title="risk controls",
            sub="the active convention is written on every figure it changes"),
        html.Div(id="rk-cards"),
        html.Div(id="rk-skew"),
        dcc.Loading(dcc.Graph(id="rk-ladder", config={"displaylogo": False}),
                    type="dot", color=ACCENT),
        html.Div(id="rk-zones"),
        dcc.Loading(dcc.Graph(id="rk-scenario", config={"displaylogo": False}),
                    type="dot", color=ACCENT),
        html.Div(id="rk-scenario-note"),
        panel(html.Div([
            html.Div([html.Label("band, % of the pair's gross option notional"),
                      dcc.Input(id="rk-band-pct", type="number", value=15, min=0.1,
                                max=200, step=0.5, debounce=True)],
                     style={"flex": "1 1 200px"}),
            html.Div([html.Label("target delta (base ccy)"),
                      dcc.Input(id="rk-target", type="number", value=0, step=100000,
                                debounce=True)], style={"flex": "1 1 180px"}),
            html.Div([html.Label("round-trip cost, bp (blank = per-pair table)"),
                      dcc.Input(id="rk-costbp", type="number", value=None, min=0,
                                step=0.1, debounce=True)], style={"flex": "1 1 200px"}),
        ], className="row"),
            title="hedge rule", sub="REQ-043 / CG-3 — the band maths is in the library"),
        html.Div(id="rk-hedge"),
        html.Div(id="rk-pin"),
        panel(html.Div([
            html.Div([html.Label("decay horizon, days"),
                      dcc.Dropdown(id="rk-decay-days", options=[7, 14, 30, 60], value=30,
                                   clearable=False)], style={"flex": "0 0 150px"}),
            html.Div([html.Label("time weighting (CG-4)"),
                      dcc.Dropdown(id="rk-calendar",
                                   options=[{"label": "calendar (default)", "value": "calendar"},
                                            {"label": "business (weekend 0.15)", "value": "business"},
                                            {"label": "event-weighted", "value": "event"}],
                                   value="calendar", clearable=False)],
                     style={"flex": "1 1 240px"}),
        ], className="row"), title="decay — “what if I do nothing”", sub="REQ-046 / CG-4"),
        dcc.Loading(dcc.Graph(id="rk-decay", config={"displaylogo": False}),
                    type="dot", color=ACCENT),
        html.Div(id="rk-expiry"),
    ])


# ====================================================================== helpers
def _pairs_or_none(s):
    book = s.store.load_book()
    return book, book.pairs()


def _basis_note():
    return note(f"Bases, never conflated (trader W-7, amendment v1.6): distance, "
                f"sigma-days and touch probability use {DISTANCE_BASIS}. "
                f"The daily breakeven and theta use {ECONOMICS_BASIS}. "
                "Using the calendar basis for distance would understate the achievable "
                "daily move by about 20% and make far strikes look safer than they are.")


# ====================================================================== cards
@callback(Output("rk-headline", "children"), Output("rk-cards", "children"),
          Input("rk-pair", "value"), Input("snapshot-token", "data"),
          Input("book-version", "data"))
def _cards(pair, _token, _bv):
    s = get_session()
    try:
        snap = s.snapshot()
        book, pairs = _pairs_or_none(s)
        if not pairs:
            e = panel(empty_state("empty book — no risk to show",
                                  "add a trade on the Book page (REQ-003)"),
                      title="the one-line answer", sub="MISS-4")
            return e, ""
        marks = s.store.marks()
        cards = []
        for p in pairs:
            try:
                h = headline(book, snap, p, marks=marks)
            except Exception as exc:                       # noqa: BLE001
                cards.append(metric(p, "not priced", unit=str(exc)[:140], tone="neg"))
                continue
            g = h["greeks"]
            status, _ = surface_status(snap.meta, p)
            cards.append(metric(
                p, h["text"],
                tone="pos" if g.gamma_1pct > 0 else "neg" if g.gamma_1pct < 0 else "",
                unit=f"{status} surface · theta charged over {h['days']:g} calendar "
                     f"day(s) to the next mark · {ECONOMICS_BASIS}",
                sub=f"PV {fmt_money(g.pv, h['ccy'])} · spot {fmt_spot(h['spot'], p)}",
                badge=provenance_badge(snap.meta, f"surface.{p}", compact=True)))
        head = panel([metric_row(cards, cols="repeat(auto-fit, minmax(430px, 1fr))"),
                      note("Every number in this strip is priced from "
                           "portfolio.risk.book_greeks on this render — amendment v1.4 "
                           "ruling 2 forbids a constant theta here, and the trader's "
                           "USD 5,800/day reference figure is withdrawn (that trade "
                           "prices to USD 2,868/day).")],
                     title="the one-line answer",
                     sub="MISS-4 — repeated in the app header, on every page")
        return head, _greek_cards(s, book, snap, pair or pairs[0], marks)
    except Exception as exc:                               # noqa: BLE001
        log.exception("risk cards failed")
        return note(f"risk cards failed: {exc}", tone="warn"), ""


def _greek_cards(s, book, snap, pair, marks):
    """REQ-038 aggregate cards + REQ-039 breakeven, per pair and in the reporting ccy."""
    spec = pair_spec(pair)
    S = snap.spot.get(pair)
    rep = s.report_ccy
    g = aggregate(book, snap, pair=pair, report_ccy=spec.quote, marks=marks)
    tot = aggregate(book, snap, report_ccy=rep, marks=marks, base_as_value=True)
    sigma = None
    try:
        df = price_frame(book.filter(pair), snap, marks=marks, report_ccy=spec.quote)
        live = df[~df["expired"].astype(bool)]
        if len(live) and np.isfinite(live["vega"]).any():
            w = np.abs(live["vega"].to_numpy(float)) + 1e-12
            sigma = float(np.average(live["vol"].to_numpy(float), weights=w))
    except Exception as exc:                               # noqa: BLE001
        log.debug("vega-weighted vol unavailable: %s", exc)
    h = headline(book, snap, pair, marks=marks)
    be, be_pips = h.get("be_pct"), h.get("be_pips")
    sd = sigma_day_move(sigma)
    cov = None
    if sigma is not None and S:
        try:
            cov = coverage_ratio(g.gamma_1pct, float(S), h.get("theta_gamma") or g.theta,
                                 float(sigma))
        except Exception:                                  # noqa: BLE001
            cov = None

    per_pip = (g.gamma_1pct * 0.01 * float(S) / spec.pip) if (S and np.isfinite(g.gamma_1pct)) else None
    cards = [
        metric("PV", fmt_money(g.pv, spec.quote), unit=greek_unit("pv")),
        # MISS-10: three representations of one delta, always together
        metric("delta", fmt_mm(g.delta_base, spec.base), unit=greek_unit("delta_base"),
               sub=(f"{fmt_mm(g.delta_base * float(S), spec.quote)} equivalent · "
                    f"{fmt_money(g.delta_base * spec.pip, spec.quote)} per pip"
                    if S else "spot unavailable"),
               hint="MISS-10: base mm, quote-ccy equivalent and P&L per pip"),
        metric("Γ₁ (gamma per 1%)", fmt_mm(g.gamma_1pct, spec.base),
               unit=greek_unit("gamma_1pct"),
               tone="pos" if g.gamma_1pct > 0 else "neg",
               sub=(f"{fmt_mm(per_pip, spec.base)} of delta per pip" if per_pip else "")),
        metric("vega", fmt_money(g.vega, spec.quote), unit=greek_unit("vega")),
        metric("theta", fmt_money(g.theta, spec.quote), unit=greek_unit("theta"),
               tone="neg" if g.theta < 0 else "pos",
               sub="from book_greeks, not a constant (amendment v1.4)"),
        # MISS-11: vanna and volga in desk units, stated on the card
        metric("vanna", fmt_money(g.vanna * 0.01, spec.quote),
               unit="quote ccy per +1 vol pt per +1% spot (desk unit, MISS-11)",
               sub=f"raw d(vega)/dS = {fmt_money(g.vanna, spec.quote)} per unit of spot"),
        metric("volga", fmt_money(g.volga, spec.quote), unit=greek_unit("volga")),
        metric("dual delta", "n/a", unit="intensive — not additive at book level (T-2)",
               hint="types.Greeks returns nan for delta_pct and dual_delta once summed; "
                    "the card says n/a rather than printing a confident wrong number"),
    ]
    be_card = [
        metric("daily breakeven", EM_DASH if be is None else fmt_pct(be, 3),
               unit=f"sqrt(|θ| / (0.005·Γ₁·S)) — {ECONOMICS_BASIS}",
               sub=(EM_DASH if be_pips is None else f"{be_pips:,.1f} pips today")),
        metric("one sigma-day of spot", EM_DASH if sd is None else fmt_pct(sd, 3),
               unit=f"σ_ATM/√252 — {DISTANCE_BASIS}",
               sub=(f"{sd / 100 * float(S) / spec.pip:,.1f} pips" if (sd and S) else ""),
               hint="distance, not economics: the two bases differ by 20%"),
        metric("coverage ratio", EM_DASH if cov is None else f"{cov:,.2f}",
               unit="gamma P&L at the realized move ÷ |θ| — 1.0 at breakeven",
               tone="pos" if (cov or 0) > 1 else "neg" if cov is not None else "",
               sub="dimensionless, so it compares across pairs (REQ-039; Γ$/|θ| withdrawn)"),
        metric("vega-weighted σ", fmt_vol(sigma), unit="the implied this book is marked at",
               sub=f"surface status: {surface_status(snap.meta, pair)[0]}"),
    ]
    tot_rows = [
        (f"totals in {rep} (CG-1)",
         f"PV {fmt_money(tot.pv, rep)} · vega {fmt_money(tot.vega, rep)} · theta "
         f"{fmt_money(tot.theta, rep)} · delta value {fmt_money(tot.delta_base, rep)} · "
         f"Γ₁ value {fmt_money(tot.gamma_1pct, rep)}"),
        ("conversion",
         " · ".join(f"{p}: {pair_spec(p).quote}→{rep} "
                    f"{R.fx_rate(pair_spec(p).quote, rep, snap):.6g}"
                    for p in book.pairs())),
    ]
    return html.Div([
        panel([metric_row(cards, cols="repeat(auto-fit, minmax(210px, 1fr))"),
               divider(),
               kv(tot_rows),
               note("Per-pair cards are in that pair's own quote ccy. The totals row "
                    "comes from book_greeks(book, mkt, report_ccy=…) with "
                    "base_as_value=True, so delta/Γ₁/vanna are report-ccy VALUES — the "
                    "only form that may be summed across pairs (CG-1, amendment v1.6 "
                    "CR-2). Summing native-ccy Greeks across pairs is forbidden: on a "
                    "EURUSD+USDJPY book the naive theta sum is 61x wrong.")],
              title=f"aggregate Greeks — {pair} ({spec.quote})", sub="REQ-038",
              right=surface_status_badge(snap.meta, pair)),
        panel([metric_row(be_card, cols="repeat(auto-fit, minmax(230px, 1fr))"),
               _basis_note()],
              title="daily breakeven and the theta bill", sub="REQ-039"),
    ])


# ====================================================================== ladder + zones
@callback(Output("rk-ladder", "figure"), Output("rk-skew", "children"),
          Output("rk-zones", "children"),
          Input("rk-pair", "value"), Input("rk-sticky", "value"),
          Input("rk-range", "value"), Input("rk-days", "value"),
          Input("snapshot-token", "data"), Input("book-version", "data"))
def _ladder(pair, sticky, rng, days, _token, _bv):
    s = get_session()
    pair = (pair or "EURUSD").upper()
    sticky = sticky or "strike"
    try:
        snap = s.snapshot()
        book = s.store.load_book()
        if pair not in book.pairs():
            msg = f"no {pair} positions in the book"
            return empty_figure(msg), "", panel(empty_state(msg), title="gamma zones")
        s.set_setting("pair", pair)
        marks = mark_vols(s.store.marks())
        rng = float(rng or 5)
        lad = R.spot_ladder(book, snap, pair, lo_pct=-rng, hi_pct=rng, n=101,
                            sticky=sticky, days_fwd=float(days or 0),
                            report_ccy=s.report_ccy, marks=marks)
        try:
            zs = Z.gamma_zones(book, snap, pair, sticky=sticky, report_ccy=s.report_ccy,
                               marks=marks)
        except Exception as exc:                           # noqa: BLE001
            log.warning("gamma_zones failed: %s", exc)
            zs = []
        fig = _ladder_figure(lad, snap, pair, sticky, float(days or 0), zs)
        return fig, _skew_panel(lad, snap, pair, sticky), _zones_panel(zs, snap, pair, sticky)
    except Exception as exc:                               # noqa: BLE001
        log.exception("ladder failed")
        return (empty_figure("ladder unavailable"),
                note(f"ladder failed: {exc}", tone="warn"),
                panel(empty_state("gamma zones unavailable"), title="gamma zones"))


def _ladder_figure(lad, snap, pair, sticky, days, zs):
    spec = pair_spec(pair)
    S0 = float(snap.spot[pair])
    x = lad["spot"].to_numpy(float)
    traces = [
        dict(type="scatter", mode="lines", name="P&L (quote ccy)", x=x,
             y=list(lad["pnl"]), line=dict(color=SERIES[0], width=2),
             hovertemplate="spot %{x:.5f}<br>P&L %{y:,.0f} " + spec.quote + "<extra></extra>"),
        dict(type="scatter", mode="lines", name="delta_base", x=x, yaxis="y2",
             y=list(lad["delta_base"]), line=dict(color=SERIES[1], width=1.6),
             hovertemplate="spot %{x:.5f}<br>delta %{y:,.0f} " + spec.base + "<extra></extra>"),
        dict(type="scatter", mode="lines", name="Γ₁ (Black-Scholes)", x=x, yaxis="y3",
             y=list(lad["gamma_1pct"]), line=dict(color=SERIES[2], width=1.4, dash="dot"),
             hovertemplate="spot %{x:.5f}<br>Γ₁(BS) %{y:,.0f}<extra></extra>"),
        dict(type="scatter", mode="lines", name=f"Γ₁ effective (sticky {sticky})", x=x,
             yaxis="y3", y=list(lad["gamma_1pct_fd"]),
             line=dict(color=SERIES[3], width=1.8),
             hovertemplate="spot %{x:.5f}<br>Γ₁(eff) %{y:,.0f}<extra></extra>"),
    ]
    fig = figure(traces,
                 title=f"{pair} spot ladder — sticky {sticky.upper()}"
                       + (f", {days:g} day(s) forward" if days else "")
                       + f"  ·  P&L in {spec.quote}, delta and Γ₁ in {spec.base}",
                 xtitle=f"spot ({spec.quote} per {spec.base})",
                 ytitle=f"P&L, {spec.quote}", height=420)
    fig["layout"]["yaxis2"] = dict(title=dict(text=f"delta, {spec.base}",
                                              font=dict(color=TEXT_DIM, size=11)),
                                   overlaying="y", side="right", showgrid=False,
                                   tickfont=dict(color=TEXT_DIM, size=11))
    fig["layout"]["yaxis3"] = dict(title=dict(text=f"Γ₁, {spec.base} per +1%",
                                              font=dict(color=TEXT_DIM, size=11)),
                                   overlaying="y", side="right", position=1.0,
                                   anchor="free", showgrid=False,
                                   tickfont=dict(color=TEXT_DIM, size=11))
    fig["layout"]["margin"] = dict(l=70, r=110, t=52, b=48)
    fig = zero_line(fig)
    fig = spot_line(fig, S0)
    # REQ-042: zones render as shaded bands on the ladder
    shapes = list(fig["layout"].get("shapes", []))
    anns = list(fig["layout"].get("annotations", []))
    for z in zs:
        colour = POS if z.side == "long" else NEG
        shapes.append(dict(type="rect", x0=z.lo, x1=z.hi, yref="paper", y0=0, y1=1,
                           fillcolor=colour + "1a", line=dict(width=0), layer="below"))
        anns.append(dict(x=z.center, y=0.02, yref="paper", showarrow=False,
                         text=z.label, font=dict(color=colour, size=9),
                         xanchor="center"))
    fig["layout"]["shapes"] = shapes
    fig["layout"]["annotations"] = anns + [dict(
        x=0, y=1.11, xref="paper", yref="paper", showarrow=False,
        text=f"convention in force: sticky {sticky} — "
             + ("σ(K) is pinned, so the delta drawn is Black-Scholes"
                if sticky != "delta" else
                "the smile rides spot, so the delta drawn includes the skew delta ν·∂σ/∂S"),
        font=dict(color=TEXT_DIM, size=10))]
    return fig


def _skew_panel(lad, snap, pair, sticky):
    """AMENDMENT v1.6 recorded finding — ``skew_gamma_1pct``, next to the sticky toggle.

    Under sticky-delta the vol moves with spot, so true gamma is *not* Black-Scholes
    gamma.  The engine measures the difference; on the reference book it is 4.1%.  A
    trader hedging off BS gamma under a sticky-delta assumption mis-sizes by exactly
    that, silently, so it is printed here rather than left in a column.
    """
    spec = pair_spec(pair)
    at = lad.iloc[(lad["spot_pct"].abs()).idxmin()]
    bs = float(at["gamma_1pct"])
    eff = float(at["gamma_1pct_fd"])
    skew = float(at["skew_gamma_1pct"])
    gap = (abs(skew) / abs(bs) * 100.0) if bs else float("nan")
    peak = float(lad["skew_gamma_1pct"].abs().max())
    tone = "neg" if (np.isfinite(gap) and gap > 2.0) else ""
    if sticky == "delta":
        verdict = (f"Under sticky-delta the true gamma at spot is {fmt_mm(eff, spec.base)}, "
                   f"not the Black-Scholes {fmt_mm(bs, spec.base)}. Hedging off the BS "
                   f"number would mis-size by {gap:.1f}%.")
    else:
        verdict = ("Under sticky-strike the effective and Black-Scholes gammas agree by "
                   "construction (to grid accuracy). Switch the toggle to sticky-delta "
                   "to see the skew gamma your hedge would be missing if the smile "
                   "actually rides spot.")
    return panel([
        metric_row([
            metric("Γ₁ Black-Scholes", fmt_mm(bs, spec.base), unit=greek_unit("gamma_1pct")),
            metric(f"Γ₁ effective (sticky {sticky})", fmt_mm(eff, spec.base),
                   unit="numerical slope of the ladder's own delta"),
            metric("skew gamma", fmt_mm(skew, spec.base), tone=tone,
                   unit="ν·∂σ/∂S — the part a sticky-strike gamma leaves out",
                   sub=(f"{gap:.1f}% of BS gamma at spot · peak across the ladder "
                        f"{fmt_mm(peak, spec.base)}" if np.isfinite(gap) else "")),
        ], cols="repeat(auto-fit, minmax(240px, 1fr))"),
        note(verdict + "  Amendment v1.6 records a measured 4.1% gap on the reference "
             "book and rules that this must be surfaced next to the toggle; this is it. "
             "It is a real effect, not a numerical artefact: under sticky-delta the vol "
             "attached to your strike changes as spot moves, so d(delta)/dS picks up "
             "vanna × ∂σ/∂S."),
    ], title="skew gamma — what the sticky toggle actually changes",
        sub="amendment v1.6, recorded finding")


def _zones_panel(zs, snap, pair, sticky):
    """REQ-042 / amendment v1.6 CR-4 — the zone's *sensitivity*, not just its location."""
    spec = pair_spec(pair)
    if not zs:
        return panel(empty_state(f"no material gamma zone in {pair}",
                                 "a zone needs a node holding at least 20% of the book's "
                                 "own peak |Γ₁| and 5% of its gross gamma area"),
                     title="gamma zones", sub="REQ-042")
    rows = []
    for z in zs:
        rows.append({
            "zone": z.label,
            "side": z.side.upper(),
            "range": f"{fmt_spot(z.lo, pair)} – {fmt_spot(z.hi, pair)}",
            "center": fmt_spot(z.center, pair),
            "dist": f"{z.distance_pct:+.2f}% / {(z.center - z.spot) / spec.pip:+,.0f}p",
            "sigma_days": (EM_DASH if not np.isfinite(z.distance_sigma_days)
                           else f"{z.distance_sigma_days:+.2f}"),
            "edge_sd": (EM_DASH if not np.isfinite(z.edge_sigma_days)
                        else f"{z.edge_sigma_days:+.2f}"),
            "touch": (EM_DASH if not np.isfinite(z.touch_prob)
                      else f"{z.touch_prob * 100:.0f}%"),
            "horizon": (EM_DASH if not np.isfinite(z.horizon_days)
                        else f"{z.horizon_days:,.0f}d"),
            "g1": round(z.gamma_1pct / 1e6, 3),
            "peak": round(z.peak_gamma_1pct / 1e6, 3),
            "share": round(z.share_of_total * 100, 1),
            "pnl_center": round(z.pnl_to_center, 0),
            "delta_center": round(z.delta_at_center / 1e6, 3),
            "delta_edge": round(z.delta_at_near_edge / 1e6, 3),
            "strikes": ", ".join(fmt_spot(k, pair) for k in z.strikes[:6])
                       + (" …" if len(z.strikes) > 6 else ""),
            "expiries": ", ".join(str(e) for e in z.expiries[:4])
                        + (" …" if len(z.expiries) > 4 else ""),
        })
    cols = [
        col("zone"), col("side"), col("range", unit=spec.quote + " per " + spec.base),
        col("center"), col("distance", "dist", unit="% / pips"),
        col("σ-days to centre", "sigma_days", unit="√252"),
        col("σ-days to edge", "edge_sd", unit="√252"),
        col("P(touch edge)", "touch", unit="first passage, GBM"),
        col("horizon", unit="to the zone's own nearest expiry"),
        col("Γ₁ avg", "g1", unit=f"{spec.base} mm per +1%", numeric=True),
        col("Γ₁ peak", "peak", unit=f"{spec.base} mm", numeric=True),
        col("share", unit="% of gross gamma", numeric=True),
        col("P&L to centre", "pnl_center", unit=spec.quote + ", fully repriced",
            numeric=True),
        col("delta inside", "delta_center", unit=f"{spec.base} mm at the centre",
            numeric=True),
        col("delta at edge", "delta_edge", unit=f"{spec.base} mm", numeric=True),
        col("strikes"), col("expiries"),
    ]
    worst = max(zs, key=lambda z: abs(z.pnl_to_center))
    return panel([
        data_table("rk-zones-tbl", cols, rows, page_size=10,
                   conditional=sign_rules(["g1", "peak", "pnl_center", "delta_center",
                                           "delta_edge"]) + [
                       {"if": {"filter_query": '{side} = "SHORT"', "column_id": "side"},
                        "color": NEG, "fontWeight": "700"},
                       {"if": {"filter_query": '{side} = "LONG"', "column_id": "side"},
                        "color": POS, "fontWeight": "700"}]),
        note(f"Zones are defined on the book's own gamma profile swept under the active "
             f"convention (sticky {sticky}), not by eyeballing a chart: a node is kept "
             f"when |Γ₁| ≥ 20% of the book's peak, runs are sign-homogeneous, and the "
             f"centre is the |gamma|-weighted centroid. The largest move in the list is "
             f"{worst.label}: spot to {fmt_spot(worst.center, pair)} is "
             f"{fmt_money(worst.pnl_to_center, spec.quote)}, fully repriced rather than "
             f"a quadratic approximation (the 0.005·Γ₁·S·x² estimate is "
             f"{fmt_money(worst.gamma_pnl_to_center, spec.quote)})."),
        _basis_note(),
    ], title=f"gamma zones — {pair}",
        sub="REQ-042 / amendment v1.6 CR-4 — how sensitive each zone is")


# ====================================================================== scenario grid
@callback(Output("rk-scenario", "figure"), Output("rk-scenario-note", "children"),
          Input("rk-pair", "value"), Input("rk-sticky", "value"),
          Input("rk-days", "value"), Input("rk-range", "value"),
          Input("snapshot-token", "data"), Input("book-version", "data"))
def _scenario(pair, sticky, days, rng, _token, _bv):
    s = get_session()
    pair = (pair or "EURUSD").upper()
    try:
        snap = s.snapshot()
        book = s.store.load_book()
        if pair not in book.pairs():
            return empty_figure(f"no {pair} positions"), ""
        rng = float(rng or 5)
        ss = np.round(np.linspace(-rng, rng, 11), 3)
        vs = np.arange(-3, 3.01, 1.0)
        grid_df = R.scenario_grid(book, snap, pair, ss, vs, days_fwd=float(days or 0),
                                  sticky=sticky or "strike", report_ccy=s.report_ccy,
                                  marks=mark_vols(s.store.marks()))
        piv = grid_df.pivot(index="vol_shock_pts", columns="spot_shock_pct",
                            values="pnl_rep")
        z = piv.to_numpy(float)
        lim = float(np.nanmax(np.abs(z))) or 1.0
        fig = {"data": [dict(type="heatmap", z=z.tolist(),
                             x=[float(c) for c in piv.columns],
                             y=[float(i) for i in piv.index],
                             colorscale=DIVERGING, zmid=0, zmin=-lim, zmax=lim,
                             colorbar=dict(title=dict(text=s.report_ccy,
                                                      font=dict(color=TEXT_DIM, size=11)),
                                           tickfont=dict(color=TEXT_DIM, size=10)),
                             hovertemplate="spot %{x:+.1f}%<br>vol %{y:+.1f} pts"
                                           "<br>P&L %{z:,.0f} " + s.report_ccy
                                           + "<extra></extra>")],
               "layout": figure([], title="", height=360)["layout"]}
        fig["layout"]["title"] = dict(
            text=f"{pair} scenario P&L — spot × vol, {days or 0:g} day(s) forward, "
                 f"sticky {sticky or 'strike'}, in {s.report_ccy}",
            x=0.01, xanchor="left", font=dict(size=13))
        fig["layout"]["xaxis"] = {**fig["layout"]["xaxis"],
                                  "title": {"text": "spot shock, %",
                                            "font": {"color": TEXT_DIM, "size": 11}}}
        fig["layout"]["yaxis"] = {**fig["layout"]["yaxis"],
                                  "title": {"text": "vol shock, vol points",
                                            "font": {"color": TEXT_DIM, "size": 11}}}
        best = grid_df.loc[grid_df["pnl_rep"].idxmax()]
        worst = grid_df.loc[grid_df["pnl_rep"].idxmin()]
        body = panel(kv([
            ("best cell", f"{fmt_money(best['pnl_rep'], s.report_ccy)} at spot "
                          f"{best['spot_shock_pct']:+.1f}% ({fmt_spot(best['spot'], pair)}), "
                          f"vol {best['vol_shock_pts']:+.1f} pts"),
            ("worst cell", f"{fmt_money(worst['pnl_rep'], s.report_ccy)} at spot "
                           f"{worst['spot_shock_pct']:+.1f}% ({fmt_spot(worst['spot'], pair)}), "
                           f"vol {worst['vol_shock_pts']:+.1f} pts"),
            ("the (0,0) cell", "is exactly zero at 0 days forward: P&L is measured "
                               "against the unshocked, undecayed PV, so the theta cost "
                               "of rolling the clock shows in the whole grid rather "
                               "than being netted away"),
        ]), title=f"scenario grid — {pair}", sub="REQ-041")
        return fig, body
    except Exception as exc:                               # noqa: BLE001
        log.exception("scenario failed")
        return empty_figure("scenario grid unavailable"), note(f"scenario failed: {exc}",
                                                              tone="warn")


# ====================================================================== hedge + pin
@callback(Output("rk-hedge", "children"), Output("rk-pin", "children"),
          Input("rk-pair", "value"), Input("rk-band-pct", "value"),
          Input("rk-target", "value"), Input("rk-costbp", "value"),
          Input("snapshot-token", "data"), Input("book-version", "data"))
def _hedge(pair, band_pct, target, costbp, _token, _bv):
    s = get_session()
    pair = (pair or "EURUSD").upper()
    try:
        snap = s.snapshot()
        book = s.store.load_book()
        if pair not in book.pairs():
            e = panel(empty_state(f"no {pair} positions"), title="hedge bands")
            return e, ""
        marks = mark_vols(s.store.marks())
        rule = HedgeRule(mode="band", band_pct=float(band_pct or 15),
                         target_delta=float(target or 0),
                         cost_bp=float(costbp) if costbp else 0.0)
        bands = Z.hedge_bands(book, snap, pair, rule=rule, report_ccy=s.report_ccy,
                              marks=marks)
        status, why = surface_status(snap.meta, pair)
        return (_hedge_panel(s, book, snap, pair, rule, bands, status, why, marks),
                _pin_panel(s, book, snap, pair, marks))
    except Exception as exc:                               # noqa: BLE001
        log.exception("hedge bands failed")
        return note(f"hedge bands failed: {exc}", tone="warn"), ""


def _hedge_panel(s, book, snap, pair, rule, bands, status, why, marks):
    spec = pair_spec(pair)
    rep = s.report_ccy
    rows = []
    for _, b in bands.iterrows():
        rows.append({
            "side": b["side"],
            "trigger": (EM_DASH if not b["trigger_found"]
                        else fmt_spot(b["trigger_spot"], pair)),
            "dist": (EM_DASH if not b["trigger_found"] else
                     f"{b['dist_pips']:+,.0f}p / {b['dist_pct']:+.2f}%"),
            "sigma_days": (EM_DASH if not np.isfinite(b["dist_sigma_days"])
                           else f"{b['dist_sigma_days']:+.2f}"),
            "band": round(b["band_base"] / 1e6, 3),
            "delta": round(b["delta_base"] / 1e6, 3),
            "trade": round(b["trade_base_at_trigger"] / 1e6, 3),
            "cost": round(b["est_cost_rep"], 0),
            "hedges_day": (EM_DASH if not np.isfinite(b["exp_hedges_day"])
                           else f"{b['exp_hedges_day']:.2f}"),
            "cost_day": (EM_DASH if not np.isfinite(b["exp_cost_day_rep"])
                         else round(b["exp_cost_day_rep"], 0)),
            "gamma_day": (EM_DASH if not np.isfinite(b["exp_gamma_pnl_day_rep"])
                          else round(b["exp_gamma_pnl_day_rep"], 0)),
            "note": b["trigger_note"],
        })
    cols = [col("side"), col("trigger spot", "trigger"),
            col("distance", "dist", unit="pips / %"),
            col("σ-days", "sigma_days", unit="√252"),
            col("band ±", "band", unit=f"{spec.base} mm", numeric=True),
            col("delta now", "delta", unit=f"{spec.base} mm", numeric=True),
            col("trade at trigger", "trade", unit=f"{spec.base} mm", numeric=True),
            col("est. cost", "cost", unit=rep, numeric=True),
            col("exp. hedges/day", "hedges_day", unit="σ²S²/h²"),
            col("exp. cost/day", "cost_day", unit=rep, numeric=True),
            col("gamma captured/day", "gamma_day", unit=rep, numeric=True),
            col("note")]
    warn = str(bands["warning"].iloc[0]) if len(bands) else ""
    src = str(bands["band_source"].iloc[0]) if len(bands) else ""
    ww = float(bands["band_ww_base"].iloc[0]) if len(bands) else float("nan")

    # REQ-043 / REQ-068(c): no instruction off an INDICATIVE surface
    if status == "MARK":
        try:
            act = hedge_suggestion(book, snap, pair, rule, report_ccy=rep, marks=marks)
            instruction = html.Div(act.reason, style={
                "fontSize": "14px", "fontWeight": 700,
                "color": POS if act.trade_base == 0 else WARN,
                "padding": "8px 10px", "border": f"1px solid {BORDER}",
                "borderRadius": "4px"})
        except Exception as exc:                           # noqa: BLE001
            instruction = note(f"hedge suggestion failed: {exc}", tone="warn")
    else:
        instruction = html.Div([
            html.Div("HEDGE INSTRUCTION DISABLED", style={"fontWeight": 800,
                                                          "color": WARN}),
            html.Div(f"{pair} is priced off an {status} surface — {why}. REQ-068(c) "
                     "issues no hedge instruction off an indicative surface: paste your "
                     "own ATM / 25d RR / 25d BF grid for this pair on the Data page and "
                     "the instruction re-enables itself. The bands below are still "
                     "shown, because a band you cannot act on is still a band you can "
                     "read.", style={"color": TEXT_DIM, "fontSize": "12px",
                                     "marginTop": "4px"}),
        ], style={"padding": "8px 10px", "border": f"1px solid {WARN}55",
                  "borderLeft": f"4px solid {WARN}", "borderRadius": "4px"})
    body = [instruction, html.Div(style={"height": "8px"}),
            data_table("rk-bands-tbl", cols, rows, page_size=4, sort=False,
                       conditional=sign_rules(["delta", "trade"]))]
    if warn:
        body.append(note(warn, tone="warn"))
    body.append(note(
        f"Band source: {src}. Cost basis: {bands['cost_bp_round_trip'].iloc[0]:.2f} bp "
        f"round trip, from the per-pair zones.COST_BP table unless you overrode it "
        f"(one global 0.2bp is right for EURUSD and 10-25x too tight for the Scandies, "
        f"trader W-13). Whalley-Wilmott indicative width "
        f"{fmt_mm(ww, spec.base) if np.isfinite(ww) else EM_DASH} — an indication that "
        f"needs a risk-aversion parameter no market quotes, never the default. "
        f"HedgeRule.band_pct is read here as a percent (15 = 15% of gross option "
        f"notional, the shipping default); the frozen 0.25 default is a FRACTION per "
        f"amendment v1.6 CR-1 and the engine warns if you pass it."))
    body.append(_basis_note())
    return panel(body, title=f"hedge bands and the next trigger — {pair}",
                 sub="REQ-043 / CG-3", right=surface_status_badge(snap.meta, pair))


def _pin_panel(s, book, snap, pair, marks):
    spec = pair_spec(pair)
    try:
        pr = Z.pin_risk(book, snap, pair, report_ccy=s.report_ccy, marks=marks)
    except Exception as exc:                               # noqa: BLE001
        return note(f"pin risk failed: {exc}", tone="warn")
    if not len(pr):
        return panel(empty_state(
            f"no {pair} strike is inside the pin window",
            "the panel arms at 3 days to the cut (REQ-045 / Q-9); nothing in this book "
            "expires that soon"), title="pin risk", sub="REQ-045")
    rows = []
    for _, r in pr.iterrows():
        rows.append({
            "strike": fmt_spot(r["strike"], pair), "expiry": str(r["expiry"]),
            "cut": r["cut"],
            "hours": f"{r['hours_to_cut']:,.1f}h",
            "dist": f"{r['dist_pips']:+,.0f}p / {r['dist_pct']:+.2f}%",
            "sigma_days": (EM_DASH if not np.isfinite(r["dist_sigma_days"])
                           else f"{r['dist_sigma_days']:+.2f}"),
            "notional": round(r["notional_base"] / 1e6, 2),
            "g1": round(r["gamma_1pct"] / 1e6, 3),
            "d_above": round(r["delta_if_above"] / 1e6, 3),
            "d_below": round(r["delta_if_below"] / 1e6, 3),
            "jump": round(r["jump_at_strike"] / 1e6, 3),
            "p10": f"{r.get('p_within_10p', float('nan')) * 100:.1f}%",
            "p25": f"{r.get('p_within_25p', float('nan')) * 100:.1f}%",
            "p50": f"{r.get('p_within_50p', float('nan')) * 100:.1f}%",
            "alert": "PIN" if r["pin_alert"] else "",
        })
    cols = [col("strike"), col("expiry"), col("cut"), col("to cut", "hours"),
            col("distance", "dist", unit="pips / %"),
            col("σ-days", "sigma_days", unit="√252"),
            col("notional", unit=f"{spec.base} mm", numeric=True),
            col("Γ₁ at the cut", "g1", unit=f"{spec.base} mm", numeric=True),
            col("delta if above", "d_above", unit=f"{spec.base} mm", numeric=True),
            col("delta if below", "d_below", unit=f"{spec.base} mm", numeric=True),
            col("jump at the strike", "jump", unit=f"{spec.base} mm", numeric=True),
            col("P(±10p)", "p10"), col("P(±25p)", "p25"), col("P(±50p)", "p50"),
            col("alert")]
    return panel([
        data_table("rk-pin-tbl", cols, rows, page_size=10, sort=False,
                   conditional=sign_rules(["d_above", "d_below", "jump"]) + [
                       {"if": {"filter_query": '{alert} = "PIN"'},
                        "backgroundColor": "#3a1416"}]),
        note("Three separately labelled deltas, never one number: what you inherit if "
             "spot finishes above the strike, what you inherit if it finishes below, "
             "and the jump between them. REQ-045's original single formula was the "
             "inherited-if-ITM delta and was sign-wrong for puts — a long put now "
             "correctly reports 0 above / −N below / +N jump, where the old formula "
             "gave −N (amendment v1.6). Rows within 0.5 σ-days of spot are highlighted."),
        note("Library gap, reported not papered over: pin_risk does not yet return the "
             "REQ-045 columns delta_now, hedge_if_above or hedge_if_below, so they are "
             "not shown. They are not computed in app/ — the pin maths belongs in "
             "portfolio.zones.", tone="warn"),
        _basis_note(),
    ], title=f"pin risk — {pair}", sub="REQ-045 — three deltas per strike")


# ====================================================================== decay
@callback(Output("rk-decay", "figure"), Output("rk-expiry", "children"),
          Input("rk-decay-days", "value"), Input("rk-calendar", "value"),
          Input("snapshot-token", "data"), Input("book-version", "data"))
def _decay(horizon, calendar, _token, _bv):
    s = get_session()
    try:
        snap = s.snapshot()
        book = s.store.load_book()
        if not book.options:
            return (empty_figure("no options in the book — nothing decays"),
                    panel(empty_state("no options"), title="expiry ladder and cut clock"))
        n = int(horizon or 30)
        days = list(range(0, n + 1, max(1, n // 20)))
        marks = mark_vols(s.store.marks())
        rv = _rv_estimate(s, book)
        td = R.time_decay(book, snap, days=days, calendar=calendar or "calendar",
                          report_ccy=s.report_ccy, realized_vol=rv, marks=marks)
        rep = s.report_ccy
        traces = [
            dict(type="scatter", mode="lines", name="PV change (repriced)",
                 x=list(td["day"]), y=list(td["dpv_rep"]),
                 line=dict(color=SERIES[0], width=2),
                 hovertemplate="day %{x}<br>ΔPV %{y:,.0f} " + rep + "<extra></extra>"),
            dict(type="scatter", mode="lines", name="cumulative θ (predicted)",
                 x=list(td["day"]), y=list(td["cum_theta_rep"]),
                 line=dict(color=SERIES[1], width=1.4, dash="dash"),
                 hovertemplate="day %{x}<br>Σθ %{y:,.0f} " + rep + "<extra></extra>"),
            dict(type="scatter", mode="lines", name="delta-hedged path, σ_r = 0",
                 x=list(td["day"]), y=list(td["cum_dh_pnl_rv0_rep"]),
                 line=dict(color=SERIES[2], width=1.2, dash="dot"),
                 hovertemplate="day %{x}<br>%{y:,.0f} " + rep + "<extra></extra>"),
        ]
        if rv is not None:
            traces.append(dict(
                type="scatter", mode="lines",
                name=f"delta-hedged path, σ_r = RV21 ({rv * 100:.2f}%)",
                x=list(td["day"]), y=list(td["cum_dh_pnl_rep"]),
                line=dict(color=SERIES[3], width=1.6),
                hovertemplate="day %{x}<br>%{y:,.0f} " + rep + "<extra></extra>"))
        fig = figure(traces,
                     title=f"“what if I do nothing” — {n} days, {calendar} time, in {rep}",
                     xtitle="calendar days from the snapshot",
                     ytitle=f"P&L, {rep}", height=340)
        fig = zero_line(fig)
        fig["layout"]["annotations"] = [dict(
            x=0, y=1.13, xref="paper", yref="paper", showarrow=False,
            text=f"weighting scheme in force: {calendar} "
                 + ("(one day of time per calendar day — the CG-4 default)"
                    if calendar == "calendar" else
                    "(weekend 0.15, holiday 0.25, events ×1.1-2.0; total elapsed time "
                    "preserved, so the SHAPE changes and the endpoint does not)")
                 + f"  ·  delta-hedged identity 50·Γ₁·S·(σ_r²−σ_i²)·Δt — {ECONOMICS_BASIS}",
            font=dict(color=TEXT_DIM, size=10))]
        return fig, _expiry_panel(s, book, snap)
    except Exception as exc:                               # noqa: BLE001
        log.exception("decay failed")
        return empty_figure("decay unavailable"), note(f"decay failed: {exc}", tone="warn")


def _rv_estimate(s, book) -> float | None:
    """21-day realized vol for the first pair in the book, for the σ_r = RV21 path."""
    try:
        from .. import analytics as A
        pairs = book.pairs()
        if not pairs:
            return None
        return A.realized_vol(s.history(pairs[0]), 21, "close_to_close")
    except Exception as exc:                               # noqa: BLE001
        log.debug("RV21 unavailable: %s", exc)
        return None


def _expiry_panel(s, book, snap):
    """REQ-044 — the expiry ladder with the exact cut instant and a countdown."""
    rows = []
    tz = str(s.setting("timezone", "Europe/London"))
    marks = mark_vols(s.store.marks())
    try:
        df = price_frame(book, snap, marks=s.store.marks(), report_ccy=s.report_ccy)
    except Exception as exc:                               # noqa: BLE001
        return note(f"expiry ladder unavailable: {exc}", tone="warn")
    opts = df[df["kind"] == "option"] if len(df) else df
    if not len(opts):
        return panel(empty_state("no options"), title="expiry ladder and cut clock")
    for (pair, expiry, cut), grp in opts.groupby(["pair", "expiry", "cut"], sort=True):
        spec = pair_spec(str(pair))
        try:
            inst = expiry_datetime(expiry, str(cut))
        except Exception:                                  # noqa: BLE001
            continue
        hours = (inst - snap.asof).total_seconds() / 3600.0
        from ..layout import local_time
        rows.append({
            "pair": pair, "expiry": str(expiry), "cut": str(cut),
            "instant_local": local_time(inst, tz),
            "instant_cut_tz": f"{inst:%Y-%m-%d %H:%M} UTC",
            "countdown": (f"{hours / 24:,.1f} days" if hours > 48 else
                          f"{hours:,.1f} hours" if hours > 0 else "PAST THE CUT"),
            "notional": round(float(grp["notional_base"].sum()) / 1e6, 2),
            "g1": round(float(grp["gamma_1pct"].sum()) / 1e6, 3),
            "theta_rep": round(float(grp["theta_rep"].sum()), 0),
            "ccy": spec.base,
        })
    rows.sort(key=lambda r: r["expiry"])
    cols = [col("pair"), col("expiry"), col("cut"),
            col("cut instant", "instant_local", unit=f"your tz ({tz})"),
            col("cut instant (UTC)", "instant_cut_tz"),
            col("countdown"), col("notional", unit="base mm", numeric=True),
            col("Γ₁ expiring", "g1", unit="base mm per +1%", numeric=True),
            col("θ released", "theta_rep", unit=f"{s.report_ccy}/day after this expiry",
                numeric=True),
            col("base ccy", "ccy")]
    return panel([
        data_table("rk-expiry-tbl", cols, rows, page_size=12, sort=False,
                   conditional=sign_rules(["g1", "theta_rep"])),
        note("conventions.expiry_datetime is the single source of the cut instant "
             "(REQ-044). An unknown cut string now raises rather than silently becoming "
             "NY10 (amendment v1.5 Q-3): a typo'd cut would shift T, theta and the pin "
             "clock on every affected leg."),
    ], title="expiry ladder and cut clock", sub="REQ-044")


#: Dash 4 resolves the page layout from the registry at request time.
dash.page_registry[__name__]["layout"] = layout
