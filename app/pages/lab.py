"""Page 7 - Signals & Backtest (`/lab`).

Two questions, in this order: **is this vol cheap or dear** (signals), and **would
owning it have paid** (backtest).

The one thing this page refuses to let you forget
-------------------------------------------------
A delta-hedged straddle's P&L is dominated by **how often you hedge**, not by the
strategy label on top of it.  On the shipped synthetic path a long 1M straddle makes
roughly 4x more net at a 60% band than at a 2% band, purely from hedge frequency, and
the band that wins is the one fitted on the same path.  So the hedge-frequency sweep is
laid out as the **result**, not as a sensitivity annex, the argmax row travels with the
words "IN-SAMPLE", and the stats table is deliberately richer than a Sharpe ratio:
gross and net, cost as a share of gross, turnover, hedge count per day, max drawdown and
the underwater duration, the hit rate, and the realized vol the run actually captured.

Provenance (architecture s7)
----------------------------
The synthetic path is SIMULATED and says so on every figure it feeds.  The history path
uses the provider's own spot history with a **constant** implied assumption, badged as a
stress assumption rather than a mark, because no vol history is stored yet (amendment
v1.3 C-6: the chain snapshotter is what will eventually make this honest, and until it
has run for a while there is no smile history to walk).
"""
from __future__ import annotations

import json
import logging

import dash
import numpy as np
from dash import Input, Output, State, callback, dcc, html

from fxgamma.backtest import (BacktestConfig, decompose, hedge_frequency_sweep,
                              lookahead_report, path_from_history, run_backtest,
                              synthetic_path)
from fxgamma.backtest.metrics import drawdown
from fxgamma.conventions import pair_spec
from fxgamma.portfolio.zones import COST_BP
from fxgamma.signals import cones as CN
from fxgamma.signals import realized as RL
from fxgamma.signals.richness import gamma_carry_expectancy
from fxgamma.types import HedgeRule

from ..components.badges import surface_status, synthetic_banner
from ..components.cards import empty_state, grid, kv, metric, metric_row, note, panel
from ..components.charts import figure, zero_line
from ..components.fmt import EM_DASH, fmt_money, fmt_pct, fmt_vol
from ..components.tables import col, data_table, sign_rules
from ..pricing import DISTANCE_BASIS, ECONOMICS_BASIS
from ..state import ALL_PAIRS, get_session
from ..theme import ACCENT, NEG, POS, SERIES, TEXT_DIM, WARN, empty_figure

log = logging.getLogger(__name__)

dash.register_page(__name__, path="/lab", name="Signals & Backtest",
                   title="FX Gamma · Lab")

STRUCTURES = [{"label": "ATM straddle", "value": "straddle"},
              {"label": "strangle (25d by default)", "value": "strangle"},
              {"label": "risk reversal", "value": "risk_reversal"}]
ENTRY_RULES = [
    {"label": "always on", "value": "always_on"},
    {"label": "gamma carry: buy when RV21 − implied > threshold", "value": "gamma_carry"},
    {"label": "cone rule: buy below the 25th percentile, sell above the 75th",
     "value": "cone_rule"},
]
BANDS = (2, 5, 10, 15, 25, 40, 60, 100)
INTERVALS = (1, 2, 5, 10, 21)


def layout(**_kw):
    s = get_session()
    snap = s.snapshot()
    pair = s.setting("pair", "EURUSD")
    return html.Div([
        synthetic_banner(snap.meta, page="lab"),
        panel([
            html.Div([
                html.Div([html.Label("pair"),
                          dcc.Dropdown(id="lb-pair", options=ALL_PAIRS, value=pair,
                                       clearable=False)], style={"flex": "1 1 140px"}),
                html.Div([html.Label("structure"),
                          dcc.Dropdown(id="lb-structure", options=STRUCTURES,
                                       value="straddle", clearable=False)],
                         style={"flex": "1 1 200px"}),
                html.Div([html.Label("direction"),
                          dcc.Dropdown(id="lb-direction",
                                       options=[{"label": "long vol", "value": 1},
                                                {"label": "short vol", "value": -1}],
                                       value=1, clearable=False)],
                         style={"flex": "1 1 140px"}),
                html.Div([html.Label("tenor, days"),
                          dcc.Dropdown(id="lb-tenor", options=[7, 14, 30, 60, 90],
                                       value=30, clearable=False)],
                         style={"flex": "0 0 130px"}),
                html.Div([html.Label("notional, base mm"),
                          dcc.Input(id="lb-notional", type="number", value=10, min=0.1,
                                    step=1, debounce=True)], style={"flex": "0 0 150px"}),
            ], className="row"),
            html.Div([
                html.Div([html.Label("entry rule"),
                          dcc.Dropdown(id="lb-entry", options=ENTRY_RULES,
                                       value="always_on", clearable=False)],
                         style={"flex": "2 1 320px"}),
                html.Div([html.Label("hedge band, % of gross option notional"),
                          dcc.Input(id="lb-band", type="number", value=15, min=0.5,
                                    max=200, step=0.5, debounce=True)],
                         style={"flex": "1 1 220px"}),
                html.Div([html.Label("spot cost, bp round trip"),
                          dcc.Input(id="lb-costbp", type="number", value=None, min=0,
                                    step=0.1, debounce=True,
                                    placeholder="per-pair table")],
                         style={"flex": "1 1 180px"}),
                html.Div([html.Label("option spread, vol pts round trip"),
                          dcc.Input(id="lb-vegabp", type="number", value=0.25, min=0,
                                    step=0.05, debounce=True)],
                         style={"flex": "1 1 200px"}),
            ], className="row"),
            html.Div([
                html.Div([html.Label("market path"),
                          dcc.Dropdown(id="lb-path",
                                       options=[{"label": "simulated (controlled "
                                                          "experiment)", "value": "synthetic"},
                                                {"label": "provider spot history + "
                                                          "constant implied",
                                                 "value": "history"}],
                                       value="synthetic", clearable=False)],
                         style={"flex": "2 1 300px"}),
                html.Div([html.Label("days"),
                          dcc.Dropdown(id="lb-days", options=[126, 252, 504], value=252,
                                       clearable=False)], style={"flex": "0 0 120px"}),
                html.Div([html.Label("realized σ (simulated path)"),
                          dcc.Input(id="lb-sigr", type="number", value=10.0, min=0.5,
                                    step=0.5, debounce=True)],
                         style={"flex": "1 1 190px"}),
                html.Div([html.Label("implied σ (the mark you pay)"),
                          dcc.Input(id="lb-sigi", type="number", value=8.0, min=0.5,
                                    step=0.5, debounce=True)],
                         style={"flex": "1 1 190px"}),
                html.Div([html.Label(" "),
                          html.Button("Run backtest", id="lb-run", n_clicks=0,
                                      style={"padding": "6px 14px"})],
                         style={"flex": "0 0 auto"}),
                html.Div([html.Label(" "),
                          html.Button("Run hedge-frequency sweep", id="lb-sweep",
                                      n_clicks=0, style={"padding": "6px 14px"})],
                         style={"flex": "0 0 auto"}),
            ], className="row"),
        ], title="strategy configuration", sub="REQ-059 — validated, and copyable as JSON"),
        html.Div(id="lb-signals"),
        html.Div(id="lb-config"),
        dcc.Loading(dcc.Graph(id="lb-equity", config={"displaylogo": False}),
                    type="dot", color=ACCENT),
        html.Div(id="lb-stats"),
        dcc.Loading(dcc.Graph(id="lb-sweep-fig", config={"displaylogo": False}),
                    type="dot", color=ACCENT),
        html.Div(id="lb-sweep-tbl"),
        html.Div(id="lb-decompose"),
    ])


# ====================================================================== config
def _entry_fn(name, sigma_i):
    from fxgamma.backtest import strategies as ST
    if name == "gamma_carry":
        return ST.gamma_carry(threshold_pts=0.5, window=21)
    if name == "cone_rule":
        return ST.cone_rule(buy_pctile=25.0, sell_pctile=75.0)
    return ST.always_on(+1)


def _config(pair, structure, direction, tenor, notional, entry, band, costbp, vegabp,
            sigma_i) -> BacktestConfig:
    return BacktestConfig(
        pair=(pair or "EURUSD").upper(),
        structure=structure or "straddle",
        direction=int(direction or 1),
        notional_base=float(notional or 10) * 1e6,
        tenor_days=int(tenor or 30),
        hedge=HedgeRule(mode="band", band_pct=float(band or 15),
                        cost_bp=float(costbp) if costbp else 0.0),
        vega_spread_pts=float(vegabp if vegabp is not None else 0.25),
        cost_bp=float(costbp) if costbp else None,
        entry=(None if (entry or "always_on") == "always_on"
               else _entry_fn(entry, sigma_i)),
        name=f"{'long' if int(direction or 1) > 0 else 'short'} "
             f"{structure or 'straddle'} {int(tenor or 30)}d band {float(band or 15):g}%",
    )


def _path(s, kind, pair, days, sigr, sigi, tenor):
    """(PathData, provenance sentence)."""
    pair = (pair or "EURUSD").upper()
    if kind == "history":
        hist = s.history(pair, years=max(float(days or 252) / 252.0, 1.0))
        if hist is None or hist.empty or "close" not in hist:
            raise ValueError(f"no spot history for {pair} from this provider")
        hist = hist.tail(int(days or 252))
        snap = s.snapshot()
        surf = snap.surfaces.get(pair)
        iv = float(surf.atm(int(tenor or 30) / 365.0)) if surf is not None else float(sigi or 8) / 100.0
        spec = pair_spec(pair)
        rd = float(snap.rates.get(spec.quote, 0.0))
        rf = float(snap.rates.get(spec.base, 0.0))
        p = path_from_history(hist, pair, implied=iv, rd=rd, rf=rf)
        return p, (f"REAL spot history from the {s.provider_name} provider "
                   f"({len(p.df)} bars) marked at a CONSTANT implied of "
                   f"{iv * 100:.2f}% — a stress assumption, not a mark. No stored vol "
                   f"history exists yet (amendment v1.3 C-6: the chain snapshotter has "
                   f"to run for a while before a smile history exists to walk), so the "
                   f"vega leg of this run is an assumption and is badged as one.")
    p = synthetic_path(n_days=int(days or 252), sigma_r=float(sigr or 10) / 100.0,
                       sigma_i=float(sigi or 8) / 100.0, pair=pair, seed=7)
    return p, (f"SIMULATED GBM path: realized {float(sigr or 10):.2f}% against an "
               f"implied mark of {float(sigi or 8):.2f}%, seed 7, drift = rd − rf so no "
               f"directional edge is smuggled in. This is the controlled experiment: "
               f"with σ_r > σ_i a delta-hedged long straddle MUST make money and a short "
               f"one MUST lose it. Nothing here is market data.")


# ====================================================================== signals
@callback(Output("lb-signals", "children"),
          Input("lb-pair", "value"), Input("lb-tenor", "value"),
          Input("snapshot-token", "data"))
def _signals(pair, tenor, _token):
    s = get_session()
    pair = (pair or "EURUSD").upper()
    try:
        snap = s.snapshot()
        hist = s.history(pair)
        surf = snap.surfaces.get(pair)
        T = int(tenor or 30) / 365.0
        iv = float(surf.atm(T)) if surf is not None else None
        rv = None
        if hist is not None and not hist.empty:
            try:
                rv = float(RL.realized_vol(hist, "close_to_close", 21))
            except Exception as exc:                       # noqa: BLE001
                log.debug("realized vol failed: %s", exc)
        if rv is not None and not np.isfinite(rv):
            rv = None
        spread = (rv - iv) if (rv is not None and iv is not None) else None
        pct = None
        if iv is not None and hist is not None and not hist.empty:
            try:
                p = float(CN.cone_percentile(hist, 21, iv))
                pct = p if np.isfinite(p) else None
            except Exception as exc:                       # noqa: BLE001
                log.debug("cone percentile failed: %s", exc)
        status, _why = surface_status(snap.meta, pair)
        exp = None
        if rv is not None and iv is not None:
            try:
                S = float(snap.spot[pair])
                exp = gamma_carry_expectancy(1e6, S, rv, iv, days=1.0)
            except Exception:                              # noqa: BLE001
                exp = None
        cards = [
            metric("implied ATM", fmt_vol(iv),
                   unit=f"{status} surface at {int(tenor or 30)}d",
                   sub="the mark you would pay"),
            metric("realized (21d, close-to-close)", fmt_vol(rv),
                   unit=f"annualised on {DISTANCE_BASIS}"),
            metric("RV − IV", EM_DASH if spread is None else f"{spread * 100:+.2f} vol pts",
                   tone="pos" if (spread or 0) > 0 else "neg" if spread is not None else "",
                   unit="positive = the market is realizing more than it charges"),
            metric("IV percentile in the 21d cone",
                   EM_DASH if pct is None else f"{float(pct):.0f}%",
                   unit="where today's implied sits in this pair's own RV history"),
            metric("carry expectancy per EUR 1mm of Γ₁",
                   EM_DASH if exp is None else fmt_money(exp, pair_spec(pair).quote),
                   tone="pos" if (exp or 0) > 0 else "neg" if exp is not None else "",
                   unit=f"50·Γ₁·S·(σ_r²−σ_i²)·Δt for one day — {ECONOMICS_BASIS}",
                   sub="the W-5-corrected identity, computed by the library helper"),
        ]
        return panel([metric_row(cards, cols="repeat(auto-fit, minmax(230px, 1fr))"),
                      note("These are the inputs the entry rules read. A z-score or a "
                           "cone percentile is only as good as the history behind it — "
                           "on ETF-chain data there is no back-history at all on day "
                           "one (amendment v1.3 C-6), so a percentile computed on a "
                           "short self-collected series says so on the Market page "
                           "rather than pretending to be a long-run rank.")],
                     title=f"signals — {pair}", sub="REQ-011 / REQ-018 inputs to the Lab")
    except Exception as exc:                               # noqa: BLE001
        log.exception("signals failed")
        return note(f"signals unavailable: {exc}", tone="warn")


# ====================================================================== backtest
@callback(Output("lb-config", "children"), Output("lb-equity", "figure"),
          Output("lb-stats", "children"), Output("lb-decompose", "children"),
          Input("lb-run", "n_clicks"), Input("snapshot-token", "data"),
          State("lb-pair", "value"), State("lb-structure", "value"),
          State("lb-direction", "value"), State("lb-tenor", "value"),
          State("lb-notional", "value"), State("lb-entry", "value"),
          State("lb-band", "value"), State("lb-costbp", "value"),
          State("lb-vegabp", "value"), State("lb-path", "value"),
          State("lb-days", "value"), State("lb-sigr", "value"), State("lb-sigi", "value"))
def _run(_n, _token, pair, structure, direction, tenor, notional, entry, band, costbp,
         vegabp, path_kind, days, sigr, sigi):
    s = get_session()
    try:
        cfg = _config(pair, structure, direction, tenor, notional, entry, band, costbp,
                      vegabp, float(sigi or 8) / 100.0)
        path, prov = _path(s, path_kind, pair, days, sigr, sigi, tenor)
        res = run_backtest(path, cfg)
        try:
            look = lookahead_report(path, cfg)
        except Exception as exc:                           # noqa: BLE001
            look = {"passed": None, "note": str(exc)}
        return (_config_panel(s, cfg, path, prov, look),
                _equity_figure(res, cfg, path, s),
                _stats_panel(res, cfg, path, s),
                _decompose_panel(res, s))
    except Exception as exc:                               # noqa: BLE001
        log.exception("backtest failed")
        e = note(f"backtest failed: {exc}", tone="warn")
        return e, empty_figure("backtest unavailable"), "", ""


def _config_panel(s, cfg, path, prov, look):
    spec = pair_spec(cfg.pair)
    d = cfg.as_dict()
    cost_bp = cfg.cost_bp if cfg.cost_bp else COST_BP.get(cfg.pair, 0.5)
    passed = look.get("passed")
    look_txt = ("PASSED — shifting the input series forward one day changes the result, "
                "and reversing the shift reproduces the original exactly"
                if passed else
                "NOT DEMONSTRATED — " + str(look.get("note", "the check did not run"))
                if passed is None else
                "FAILED — the engine's own look-ahead check did not pass; treat every "
                "number on this page as invalid until it does")
    return panel([
        html.Div([
            html.Div([
                html.Div("SIMULATED" if path.meta.get("kind") == "synthetic"
                         else "HISTORY", style={"fontWeight": 800, "color": WARN}),
                html.Div(prov, style={"color": TEXT_DIM, "fontSize": "12px",
                                      "lineHeight": "1.5", "marginTop": "3px"}),
            ], style={"padding": "6px 10px", "border": f"1px solid {WARN}55",
                      "borderLeft": f"4px solid {WARN}", "borderRadius": "4px",
                      "marginBottom": "8px"}),
        ]),
        grid([
            kv([("pair / structure", f"{cfg.pair} {cfg.structure}, "
                                     f"{'long' if cfg.direction > 0 else 'short'} vol"),
                ("notional per leg", f"{spec.base} {cfg.notional_base / 1e6:,.1f}mm"),
                ("tenor / roll", f"{cfg.tenor_days} days, rolled at expiry"),
                ("hedge rule", f"band {cfg.hedge.band_pct:g}% of gross option notional "
                               f"(the desk default is 15%; the frozen HedgeRule default "
                               f"0.25 is a FRACTION per amendment v1.6 CR-1)"),
                ("costs", f"spot {cost_bp:.2f}bp round trip "
                          f"({'your override' if cfg.cost_bp else 'per-pair COST_BP table'}), "
                          f"option {cfg.vega_spread_pts:g} vol pts round trip"),
                ("entry rule", d["entry"]),
                ("no look-ahead (REQ-060)", look_txt),
                ("bases", f"P&L in {spec.quote}; Sharpe on √252 × steps/day; theta on "
                          f"ACT/365")]),
            html.Div([
                html.Div("copyable JSON summary (REQ-059)",
                         style={"color": TEXT_DIM, "fontSize": "11px",
                                "marginBottom": "4px"}),
                dcc.Textarea(value=json.dumps(d, indent=2, default=str),
                             style={"width": "100%", "height": "260px",
                                    "backgroundColor": "#12171e", "color": "#e6edf3",
                                    "border": "1px solid #2b3440", "fontSize": "11px",
                                    "fontFamily": "ui-monospace, monospace"},
                             readOnly=True),
            ]),
        ], cols="3fr 2fr"),
    ], title="run configuration", sub="REQ-059 / REQ-060")


def _equity_figure(res, cfg, path, s):
    spec = pair_spec(cfg.pair)
    eq = res.equity
    dd = drawdown(eq["equity"])
    x = list(eq.index)
    traces = [
        dict(type="scatter", mode="lines", name="equity, net of costs", x=x,
             y=list(eq["equity"]), line=dict(color=SERIES[0], width=2),
             hovertemplate="%{x|%Y-%m-%d}<br>net %{y:,.0f} " + spec.quote + "<extra></extra>"),
        dict(type="scatter", mode="lines", name="equity, gross", x=x,
             y=list(eq["equity_gross"]), line=dict(color=SERIES[3], width=1.2,
                                                   dash="dash"),
             hovertemplate="%{x|%Y-%m-%d}<br>gross %{y:,.0f} " + spec.quote + "<extra></extra>"),
        dict(type="scatter", mode="lines", name="drawdown (net)", x=x,
             y=list(dd["drawdown"]), yaxis="y2", fill="tozeroy",
             line=dict(color=NEG, width=0.8),
             hovertemplate="%{x|%Y-%m-%d}<br>drawdown %{y:,.0f}<extra></extra>"),
        dict(type="scatter", mode="lines", name="cumulative cost", x=x,
             y=[-c for c in eq["cum_cost"]], line=dict(color=WARN, width=1, dash="dot"),
             hovertemplate="%{x|%Y-%m-%d}<br>cost %{y:,.0f}<extra></extra>"),
    ]
    fig = figure(traces, title=f"{cfg.name} — equity, gross and net ({spec.quote})",
                 xtitle="date", ytitle=f"cumulative P&L, {spec.quote}", height=380)
    fig["layout"]["yaxis2"] = dict(title=dict(text="drawdown",
                                              font=dict(color=TEXT_DIM, size=11)),
                                   overlaying="y", side="right", showgrid=False,
                                   tickfont=dict(color=TEXT_DIM, size=11))
    fig["layout"]["annotations"] = [dict(
        x=0, y=1.10, xref="paper", yref="paper", showarrow=False,
        text=f"provenance: {path.meta.get('kind', 'unknown')} — "
             + ("SIMULATED, not market data" if path.meta.get("kind") == "synthetic"
                else "real spot, assumed constant implied"),
        font=dict(color=WARN, size=10))]
    return zero_line(fig)


STAT_ROWS = [
    ("total_pnl_net", "total P&L, net of costs", "quote ccy"),
    ("total_pnl_gross", "total P&L, gross", "quote ccy"),
    ("total_cost", "total cost", "quote ccy"),
    ("hedge_cost", "of which spot hedging", "quote ccy"),
    ("option_cost", "of which option spread", "quote ccy"),
    ("cost_pct_of_gross", "cost as a share of gross", "%"),
    ("sharpe_net", "Sharpe, net", "annualised — basis stated below"),
    ("sharpe_gross", "Sharpe, gross", "annualised"),
    ("hit_rate_daily", "hit rate, daily", "fraction of up days"),
    ("avg_daily_pnl", "average daily P&L", "quote ccy"),
    ("sd_daily_pnl", "daily standard deviation", "quote ccy"),
    ("avg_daily_gamma_pnl", "average daily gamma P&L", "quote ccy — ½Γ dS²"),
    ("avg_daily_theta", "average daily theta", "quote ccy — what the gamma has to beat"),
    ("max_drawdown", "max drawdown", "quote ccy"),
    ("max_underwater_steps", "longest time underwater", "steps"),
    ("turnover_base", "hedge turnover", "base ccy"),
    ("n_hedges", "hedges executed", "count"),
    ("hedges_per_day", "hedges per day", "count"),
    ("realized_vol_captured", "realized vol actually captured", "decimal — the σ_r that "
                                                               "made gamma pay theta"),
    ("avg_implied", "average implied paid", "decimal"),
]


def _stats_panel(res, cfg, path, s):
    st = res.stats
    rows = []
    for key, label, unit in STAT_ROWS:
        v = st.get(key)
        if isinstance(v, float) and key in ("realized_vol_captured", "avg_implied"):
            txt = fmt_vol(v)
        elif isinstance(v, float) and key == "hit_rate_daily":
            txt = fmt_pct(v * 100, 1)
        elif isinstance(v, float) and key == "cost_pct_of_gross":
            txt = fmt_pct(v, 2)
        elif isinstance(v, float) and abs(v) < 100 and key.startswith("sharpe"):
            txt = f"{v:,.2f}"
        elif isinstance(v, (int, float)):
            txt = f"{v:,.2f}" if isinstance(v, float) and abs(v) < 10 else f"{v:,.0f}"
        else:
            txt = str(v)
        rows.append({"stat": label, "value": txt, "unit": unit})
    captured = st.get("realized_vol_captured")
    implied = st.get("avg_implied")
    verdict = EM_DASH
    if captured and implied and np.isfinite(captured):
        edge = (captured - implied) * 100
        verdict = (f"you captured {captured * 100:.2f}% against {implied * 100:.2f}% "
                   f"paid — {edge:+.2f} vol points")
    return panel([
        metric_row([
            metric("net P&L", fmt_money(st.get("total_pnl_net"), pair_spec(cfg.pair).quote),
                   tone="pos" if st.get("total_pnl_net", 0) >= 0 else "neg",
                   unit="after spot hedging and option spread"),
            metric("cost drag", fmt_pct(st.get("cost_pct_of_gross"), 2),
                   unit="of gross P&L", tone="neg"),
            metric("Sharpe (net)", f"{st.get('sharpe_net', float('nan')):,.2f}",
                   unit=str(st.get("sharpe_basis", ""))),
            metric("max drawdown",
                   fmt_money(st.get("max_drawdown"), pair_spec(cfg.pair).quote),
                   tone="neg",
                   sub=f"{st.get('max_underwater_steps', 0):,} steps underwater"),
            metric("vol captured vs paid", verdict,
                   unit="the only vol number that matters ex-post (REQ-056)"),
        ], cols="repeat(auto-fit, minmax(230px, 1fr))"),
        data_table("lb-stats-tbl", [col("stat"), col("value"), col("unit")], rows,
                   page_size=25, sort=False),
        note(f"Annualisation: {st.get('sharpe_basis', '')}. {st.get('annualisation', '')}. "
             f"A Sharpe with an unstated basis is how a 12-step-a-day backtest reports "
             f"30. Provenance of the path: {st.get('provenance', 'unknown')}."),
    ], title="statistics", sub="REQ-061 — richer than a Sharpe ratio, by design")


def _decompose_panel(res, s):
    try:
        dec = decompose(res)
    except Exception as exc:                               # noqa: BLE001
        return note(f"decomposition unavailable: {exc}", tone="warn")
    if not len(dec):
        return ""
    ccy = pair_spec(res.config.pair).quote
    rows = [{"component": r["component"], "pnl": round(float(r["pnl"]), 0),
             "share": (EM_DASH if not np.isfinite(r["share_of_gross"])
                       else f"{float(r['share_of_gross']) * 100:,.1f}%")}
            for _, r in dec.iterrows()]
    return panel([
        data_table("lb-decompose-tbl",
                   [col("component"), col("P&L", "pnl", unit=ccy, numeric=True),
                    col("share of gross", "share")],
                   rows, page_size=12, sort=False, conditional=sign_rules(["pnl"])),
        note("delta + gamma + theta + vega + residual = gross by construction; residual "
             "is every higher-order term (charm, veta, speed, and the vol/spot cross "
             "terms). carry_identity is the continuous-hedging expectation at the path's "
             "own step-by-step realized vol — 50·Γ₁·S·(σ_r²−σ_i²)·Δt, the same helper "
             "the Risk page's decay panel uses — and discretisation (gamma + theta − "
             "carry_identity) is the P&L attributable purely to hedging at a finite "
             "frequency. That last line is the whole argument for the sweep below."),
    ], title="realized-vs-implied decomposition", sub="REQ-064")


# ====================================================================== the sweep
@callback(Output("lb-sweep-fig", "figure"), Output("lb-sweep-tbl", "children"),
          Input("lb-sweep", "n_clicks"),
          State("lb-pair", "value"), State("lb-structure", "value"),
          State("lb-direction", "value"), State("lb-tenor", "value"),
          State("lb-notional", "value"), State("lb-entry", "value"),
          State("lb-band", "value"), State("lb-costbp", "value"),
          State("lb-vegabp", "value"), State("lb-path", "value"),
          State("lb-days", "value"), State("lb-sigr", "value"), State("lb-sigi", "value"),
          prevent_initial_call=True)
def _sweep(_n, pair, structure, direction, tenor, notional, entry, band, costbp, vegabp,
           path_kind, days, sigr, sigi):
    s = get_session()
    try:
        cfg = _config(pair, structure, direction, tenor, notional, entry, band, costbp,
                      vegabp, float(sigi or 8) / 100.0)
        path, _prov = _path(s, path_kind, pair, days, sigr, sigi, tenor)
        sw = hedge_frequency_sweep(path, cfg, bands=BANDS, intervals=INTERVALS)
        return _sweep_figure(sw, cfg), _sweep_table(sw, cfg)
    except Exception as exc:                               # noqa: BLE001
        log.exception("sweep failed")
        return empty_figure("sweep unavailable"), note(f"sweep failed: {exc}", tone="warn")


def _sweep_figure(sw, cfg):
    ccy = pair_spec(cfg.pair).quote
    bandrows = sw[sw["rule"] == "band"]
    timerows = sw[sw["rule"] == "time"]
    traces = [
        dict(type="scatter", mode="lines+markers", name="net P&L vs band width",
             x=list(bandrows["param"]), y=list(bandrows["total_pnl_net"]),
             line=dict(color=SERIES[0], width=2),
             hovertemplate="band %{x:g}%<br>net %{y:,.0f} " + ccy + "<extra></extra>"),
        dict(type="scatter", mode="lines+markers", name="gross P&L vs band width",
             x=list(bandrows["param"]), y=list(bandrows["total_pnl_gross"]),
             line=dict(color=SERIES[3], width=1.2, dash="dash"),
             hovertemplate="band %{x:g}%<br>gross %{y:,.0f}<extra></extra>"),
        dict(type="bar", name="total cost", x=list(bandrows["param"]),
             y=list(bandrows["total_cost"]), yaxis="y2", opacity=0.45,
             marker=dict(color=WARN),
             hovertemplate="band %{x:g}%<br>cost %{y:,.0f}<extra></extra>"),
        dict(type="scatter", mode="lines+markers", name="Sharpe, net (right)",
             x=list(bandrows["param"]), y=list(bandrows["sharpe_net"]), yaxis="y3",
             line=dict(color=SERIES[2], width=1.4),
             hovertemplate="band %{x:g}%<br>Sharpe %{y:.2f}<extra></extra>"),
    ]
    fig = figure(traces,
                 title=f"hedge-frequency sweep — {cfg.name}: P&L is dominated by how "
                       f"often you hedge",
                 xtitle="hedge band, % of gross option notional "
                        "(time-interval family is in the table below)",
                 ytitle=f"P&L, {ccy}", height=380)
    fig["layout"]["yaxis2"] = dict(title=dict(text=f"cost, {ccy}",
                                              font=dict(color=TEXT_DIM, size=11)),
                                   overlaying="y", side="right", showgrid=False,
                                   tickfont=dict(color=TEXT_DIM, size=11))
    fig["layout"]["yaxis3"] = dict(title=dict(text="Sharpe",
                                              font=dict(color=TEXT_DIM, size=11)),
                                   overlaying="y", side="right", position=1.0,
                                   anchor="free", showgrid=False,
                                   tickfont=dict(color=TEXT_DIM, size=11))
    fig["layout"]["margin"] = dict(l=70, r=110, t=52, b=56)
    best = sw.loc[sw["total_pnl_net"].idxmax()]
    fig["layout"]["annotations"] = [dict(
        x=0, y=1.11, xref="paper", yref="paper", showarrow=False,
        text=f"argmax on this path: {best['label']} at "
             f"{best['total_pnl_net']:,.0f} {ccy} — IN-SAMPLE, not a recommendation. "
             f"Time-interval family spans {timerows['total_pnl_net'].min():,.0f} to "
             f"{timerows['total_pnl_net'].max():,.0f} {ccy}.",
        font=dict(color=WARN, size=10))]
    return zero_line(fig)


def _sweep_table(sw, cfg):
    ccy = pair_spec(cfg.pair).quote
    rows = []
    for _, r in sw.iterrows():
        rows.append({
            "rule": r["rule"], "setting": r["label"],
            "net": round(float(r["total_pnl_net"]), 0),
            "gross": round(float(r["total_pnl_gross"]), 0),
            "cost": round(float(r["total_cost"]), 0),
            "cost_pct": f"{float(r['cost_pct_of_gross']):,.2f}%",
            "sharpe": f"{float(r['sharpe_net']):,.2f}",
            "hedges": int(r["n_hedges"]),
            "per_day": f"{float(r['hedges_per_day']):.2f}",
            "turnover": round(float(r["turnover_base"]) / 1e6, 0),
            "dd": round(float(r["max_drawdown"]), 0),
            "captured": fmt_vol(r["realized_vol_captured"]),
            "argmax": "ARGMAX (in-sample)" if bool(r["argmax"]) else "",
        })
    spread = sw["total_pnl_net"].max() - sw["total_pnl_net"].min()
    base = abs(sw["total_pnl_net"].median()) or 1.0
    cols = [col("family", "rule"), col("setting"),
            col("net P&L", "net", unit=ccy, numeric=True),
            col("gross P&L", "gross", unit=ccy, numeric=True),
            col("cost", unit=ccy, numeric=True),
            col("cost / gross", "cost_pct"), col("Sharpe (net)", "sharpe"),
            col("hedges", numeric=True), col("per day", "per_day"),
            col("turnover", unit="base mm", numeric=True),
            col("max drawdown", "dd", unit=ccy, numeric=True),
            col("σ captured", "captured"), col("", "argmax")]
    return panel([
        data_table("lb-sweep-table", cols, rows, page_size=15, sort=False,
                   conditional=sign_rules(["net", "gross", "dd"]) + [
                       {"if": {"filter_query": '{argmax} != ""'},
                        "backgroundColor": "#1d2a1d"}]),
        html.Div(f"Hedge frequency moves net P&L by {spread:,.0f} {ccy} across this "
                 f"grid — {spread / base * 100:,.0f}% of the median run. That is larger "
                 f"than most strategy choices, which is why this sweep is the result "
                 f"and not an appendix.",
                 style={"color": WARN, "fontSize": "12px", "fontWeight": 600,
                        "margin": "8px 0 4px"}),
        note("The argmax row is flagged IN-SAMPLE because it is: picking the best band "
             "on the same path you fitted it to is not a result, and REQ-062 requires "
             "the caution to travel with the number. A tighter band captures more of "
             "the realized path and pays more cost; a wider one is cheaper and carries "
             "more path risk, which shows up in max drawdown rather than in the mean. "
             "Read the drawdown column next to the P&L column, never alone."),
    ], title="hedge-frequency / band sweep", sub="REQ-062")


#: Dash 4 resolves the page layout from the registry at request time.
dash.page_registry[__name__]["layout"] = layout
