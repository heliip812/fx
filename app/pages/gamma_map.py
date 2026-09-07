"""Page 3 - Listed positioning, a.k.a. the Gamma Map (`/gamma-map`).

**Read the disclaimer, it is the feature.**  The trader review (Q-8, W-11) is blunt:
listed CME open interest is a small share of the FX option market, the *sign* of dealer
inventory is unknowable from it, and a screen that prints signed "dealer gamma" is
inventing a number.  So this page:

* shows **unsigned** open interest and an unsigned gamma-weight by strike;
* never uses the word "dealer positioning" as a fact, and carries the assumption text on
  the figure itself (REQ-023, rewritten per W-11);
* states the contract multiplier and the futures-vs-spot convention it used;
* takes expiry instants from the **listed product's** own dates, and says so rather than
  pretending they are the OTC ``PAIRS[pair].cut``;
* overlays *your* book's gamma - which is exact - as a separate, differently marked
  series, so the honest number and the indicative one are never mixed (REQ-026).
"""
from __future__ import annotations

import logging

import dash
import pandas as pd
from dash import Input, Output, callback, dcc, html

from fxgamma.conventions import pair_spec

from ..components.badges import provenance_badge, synthetic_banner
from ..components.cards import empty_state, grid, note, panel
from ..components.charts import figure, spot_line
from ..components.fmt import EM_DASH, fmt_spot
from ..components.tables import col, data_table
from ..pricing import DISTANCE_BASIS, price_positions, sigma_day_move
from ..state import ALL_PAIRS, get_session
from ..theme import ACCENT, NEG, POS, empty_figure

log = logging.getLogger(__name__)

dash.register_page(__name__, path="/gamma-map", name="Gamma Map",
                   title="FX Gamma · Listed positioning")

DISCLAIMER = ("OPEN INTEREST IS NOT DEALER POSITIONING. This is listed CME open interest: "
              "a small share of the FX option market, and the side each contract was "
              "opened on is not published. The sign of dealer inventory is unknowable "
              "from this data, so nothing here is signed. Use it as a map of where "
              "listed strikes cluster, not as a positioning read.")


def layout(**_kw):
    s = get_session()
    return html.Div([
        html.Div(id="gm-banner"),
        panel([
            html.Div([
                html.Div([html.Label("pair"),
                          dcc.Dropdown(id="gm-pair", options=ALL_PAIRS,
                                       value=s.setting("pair", "EURUSD"), clearable=False)],
                         style={"minWidth": "150px"}),
                html.Div([html.Label("expiries shown"),
                          dcc.Dropdown(id="gm-nexp", options=[1, 2, 3, 4], value=2,
                                       clearable=False)], style={"minWidth": "130px"}),
                html.Div([html.Label("overlay"),
                          dcc.Checklist(id="gm-overlay",
                                        options=[{"label": " my book's gamma (exact)",
                                                  "value": "book"}],
                                        value=["book"],
                                        labelStyle={"fontSize": "11px"})],
                         style={"minWidth": "200px"}),
                html.Div(id="gm-src", style={"alignSelf": "center", "paddingTop": "14px"}),
            ], className="row"),
            html.Div(DISCLAIMER, style={
                "marginTop": "8px", "padding": "8px 10px", "fontSize": "11px",
                "lineHeight": "1.5", "color": "#e6c07b",
                "backgroundColor": "#2a230f", "border": "1px solid #6b5a1f",
                "borderLeft": "4px solid #d29922", "borderRadius": "4px"}),
        ], title="listed positioning (indicative)",
            sub="REQ-022 / REQ-023 — rewritten per trader review W-11"),
        dcc.Loading(dcc.Graph(id="gm-strikes", config={"displaylogo": False}), type="dot",
                    color=ACCENT),
        grid([html.Div(dcc.Graph(id="gm-ladder", config={"displaylogo": False})),
              html.Div(id="gm-magnets")], cols="1fr 1fr"),
        html.Div(id="gm-mechanics"),
    ])


def _book_gamma_by_strike(s, pair, snap) -> pd.DataFrame:
    """The user's own ``gamma_1pct`` bucketed by strike — exact, not inferred.

    Priced through :func:`app.pricing.price_positions` -> ``portfolio.risk.price_book``
    like everything else on this app; the interim direct-``gk_greeks`` loop that lived
    here while the risk engine was being written is gone, so the diamonds on this
    figure and the blotter rows behind them are guaranteed to be the same numbers.
    """
    book = s.store.load_book().filter(pair)
    if not book.options:
        return pd.DataFrame(columns=["strike", "gamma_1pct"])
    rows = [r for r in price_positions(book, snap, marks=s.store.marks(),
                                       report_ccy=s.report_ccy)
            if r["instrument"] == "OPTION" and r["state"] == "LIVE"]
    if not rows:
        return pd.DataFrame(columns=["strike", "gamma_1pct"])
    df = pd.DataFrame([{"strike": float(r["strike"]), "gamma_1pct": float(r["gamma_1pct"]),
                        "expiry": r["expiry"]} for r in rows])
    return df.groupby("strike", as_index=False)["gamma_1pct"].sum()


@callback(Output("gm-banner", "children"), Output("gm-src", "children"),
          Output("gm-strikes", "figure"), Output("gm-ladder", "figure"),
          Output("gm-magnets", "children"), Output("gm-mechanics", "children"),
          Input("gm-pair", "value"), Input("gm-nexp", "value"),
          Input("gm-overlay", "value"), Input("snapshot-token", "data"),
          Input("book-version", "data"))
def _render(pair, nexp, overlay, _token, _bv):
    s = get_session()
    pair = (pair or "EURUSD").upper()
    try:
        s.set_setting("pair", pair)
        snap = s.snapshot()
        banner = synthetic_banner(snap.meta, page="gamma-map")
        spec = pair_spec(pair)
        src = html.Div([provenance_badge(snap.meta, f"oi.{pair}", label="OI"),
                        html.Span(f"CME product {spec.cme_code or '—'}",
                                  className="tiny dim",
                                  style={"marginLeft": "8px"})])
        oi = s.open_interest(pair)
        if oi is None or oi.empty:
            msg = (f"no listed open interest for {pair}"
                   + ("" if spec.cme_code else " — this pair has no CME product code"))
            return (banner, src, empty_figure(msg), empty_figure(msg),
                    panel(empty_state(msg), title="strike magnets"),
                    _mechanics(pair, None, snap))
        oi = oi.copy()
        oi["expiry"] = pd.to_datetime(oi["expiry"]).dt.date
        expiries = sorted(oi["expiry"].unique())[: int(nexp or 2)]
        front = oi[oi["expiry"].isin(expiries)]
        S = snap.spot.get(pair)

        by_strike = front.groupby("strike", as_index=False)["oi"].sum()
        by_strike = by_strike.sort_values("strike")
        traces = [dict(type="bar", name="listed open interest (unsigned)",
                       x=list(by_strike["strike"]), y=list(by_strike["oi"]),
                       marker=dict(color="#3b5c7a"),
                       hovertemplate="K %{x:.4f}<br>OI %{y:,.0f} contracts"
                                     "<extra>listed, unsigned</extra>")]
        if "book" in (overlay or []):
            bg = _book_gamma_by_strike(s, pair, snap)
            if not bg.empty:
                traces.append(dict(
                    type="scatter", mode="markers", name="MY book gamma_1pct (exact)",
                    x=list(bg["strike"]), y=[abs(v) / 1e6 for v in bg["gamma_1pct"]],
                    yaxis="y2",
                    marker=dict(size=[min(26, 8 + abs(v) / 4e5) for v in bg["gamma_1pct"]],
                                color=[POS if v > 0 else NEG for v in bg["gamma_1pct"]],
                                symbol="diamond",
                                line=dict(width=1, color="#e6edf3")),
                    customdata=[[v / 1e6] for v in bg["gamma_1pct"]],
                    hovertemplate="K %{x:.4f}<br>my gamma_1pct %{customdata[0]:+,.2f}mm "
                                  + spec.base + " per +1%<extra>mine — exact, signed"
                                  "</extra>"))
        fig = figure(traces,
                     title=f"{pair} — listed open interest by strike, front {len(expiries)} "
                           f"expiries (UNSIGNED)",
                     xtitle=f"strike ({spec.quote} per {spec.base})",
                     ytitle="open interest, contracts (left)", height=340, barmode="stack")
        fig["layout"]["yaxis2"] = dict(
            title=dict(text=f"|my gamma_1pct|, {spec.base} mm per +1% (right)",
                       font=dict(color="#9aa7b4", size=11)),
            overlaying="y", side="right", gridcolor="#222b36",
            tickfont=dict(color="#9aa7b4", size=11))
        fig["layout"]["annotations"] = [dict(
            x=0, y=1.13, xref="paper", yref="paper", showarrow=False,
            text="green diamond = you are long gamma at that strike, red = short. "
                 "Bars are unsigned listed OI.",
            font=dict(color="#6e7d8d", size=10))]
        if S:
            fig = spot_line(fig, S)

        ladder = _ladder(oi, snap, pair)
        magnets = _magnets(front, S, snap, pair)
        return banner, src, fig, ladder, magnets, _mechanics(pair, oi, snap)
    except Exception as exc:                               # noqa: BLE001
        log.exception("gamma map failed")
        e = empty_figure("listed positioning failed")
        return (note(f"gamma map failed: {exc}", tone="warn"), "", e, e,
                panel(empty_state("unavailable"), title="strike magnets"), "")


def _ladder(oi, snap, pair):
    g = oi.groupby("expiry", as_index=False)["oi"].sum().sort_values("expiry").head(8)
    if g.empty:
        return empty_figure("no expiries")
    today = snap.asof.date()
    days = [(d - today).days for d in g["expiry"]]
    traces = [dict(type="bar", x=[str(d) for d in g["expiry"]], y=list(g["oi"]),
                   marker=dict(color="#3b5c7a"), name="OI",
                   customdata=[[d] for d in days],
                   hovertemplate="%{x}<br>OI %{y:,.0f} contracts<br>"
                                 "%{customdata[0]} calendar days<extra></extra>")]
    return figure(traces, title=f"{pair} listed expiry ladder — next {len(g)} expiries",
                  xtitle="listed expiry date (CME product calendar, NOT the OTC cut)",
                  ytitle="open interest, contracts", height=290)


def _magnets(front, S, snap, pair):
    if front.empty or not S:
        return panel(empty_state("no strikes near spot"), title="strike magnets")
    tot = float(front["oi"].sum()) or 1.0
    g = front.groupby("strike", as_index=False)["oi"].sum()
    g["share"] = g["oi"] / tot * 100
    g["dist_pct"] = (g["strike"] / S - 1) * 100
    spec = pair_spec(pair)
    g["dist_pips"] = (g["strike"] - S) / spec.pip
    near = g[(g["dist_pct"].abs() <= 2.0) & (g["share"] > 5.0)]
    surf = snap.surfaces.get(pair)
    sig = None
    if surf is not None:
        try:
            sig = float(surf.atm(30 / 365))
        except Exception:                                  # noqa: BLE001
            sig = None
    sd = sigma_day_move(sig) if sig else None
    data = [{"strike": fmt_spot(r["strike"], pair), "share": round(r["share"], 1),
             "dist": f"{r['dist_pips']:+,.0f}p / {r['dist_pct']:+.2f}%",
             "sigma_days": (EM_DASH if not sd else f"{abs(r['dist_pct']) / sd:.2f}")}
            for _, r in near.sort_values("share", ascending=False).iterrows()]
    body = data_table("gm-magnets-tbl",
                      [col("strike"), col("share", unit="% of front OI", numeric=True),
                       col("distance", "dist", unit="pips / %"),
                       col("sigma-days", "sigma_days", unit=DISTANCE_BASIS)],
                      data, page_size=8, sort=False) if data else \
        empty_state("no strike holds >5% of front open interest within ±2% of spot")
    return panel([body,
                  note("Sigma-days use the sqrt(252) trading-day basis, because this is a "
                       "*distance* question — how far spot can travel — not an economics "
                       "one. The calendar-day sqrt(365) basis used for the daily breakeven "
                       "would understate the achievable move by about 20% (trader review "
                       "W-7).")],
                 title="strike magnets", sub="REQ-025 — candidates, not predictions")


def _mechanics(pair, oi, snap):
    spec = pair_spec(pair)
    rows = [
        ("pair / listed product", f"{pair} / CME {spec.cme_code or '—'}"),
        ("quote convention", f"CME FX options are quoted USD per {spec.base if spec.quote == 'USD' else spec.quote}; "
                             f"{pair} is quoted {spec.quote} per {spec.base}"
                             + (" — the listed product is INVERTED versus this pair and the "
                                "strikes are converted for display"
                                if spec.inverted_etf else "")),
        ("contract multiplier", "not published by the free settlement feed used here — "
                                "OI is shown in CONTRACTS, never converted to notional, "
                                "because a wrong multiplier would silently scale every "
                                "number on this page (W-11)"),
        ("expiry instants", "taken from the listed product's own expiry dates. They are "
                            "NOT the OTC cut in PAIRS[pair].cut and must not be read as "
                            "your option's cut."),
        ("exercise style", "listed FX options are American-style on futures; your book is "
                           "European on spot. The two are not directly comparable."),
        ("sign", "unsigned, always. See the banner."),
        ("rows in this snapshot", f"{0 if oi is None else len(oi):,}"),
    ]
    return panel(kv_rows(rows), title="mechanics and caveats",
                 sub="stated on the page, not buried in a tooltip")


def kv_rows(rows):
    from ..components.cards import kv
    return kv(rows)


#: Dash 4 resolves the page layout from the registry at request time, so bind it
#: explicitly now that `layout` is defined.
dash.page_registry[__name__]["layout"] = layout
