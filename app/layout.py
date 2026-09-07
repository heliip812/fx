"""The application shell: header, navigation, global stores.

The header is where the trader's "spot age and spot source, always visible, never on
hover" (review Q-10) is honoured: the snapshot stamp, the provider, the count of
synthetic and overridden fields and the book state are on screen at all times, on every
page, and a page whose numbers are simulated says so twice - here and in its own banner.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import dash
from dash import dcc, html

from .components.badges import chip, provenance_badge
from .components.fmt import EM_DASH
from .pricing import DISTANCE_BASIS, ECONOMICS_BASIS, headline
from .theme import ACCENT, KIND_COLORS, NEG, POS, TEXT_DIM

NAV = [("/", "1 Market"), ("/surface", "2 Surface"), ("/gamma-map", "3 Gamma Map"),
       ("/book", "4 Book"), ("/risk", "5 Risk"), ("/pnl", "6 P&L"),
       ("/lab", "7 Lab"), ("/data", "8 Data")]
#: nothing is pending any more: all eight contract section 6 pages are wired.
PENDING: list[tuple[str, str]] = []


def local_time(dt: datetime, tz: str) -> str:
    try:
        z = ZoneInfo(tz)
    except Exception:                                      # noqa: BLE001
        z = ZoneInfo("UTC")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    loc = dt.astimezone(z)
    return f"{loc:%Y-%m-%d %H:%M:%S} {loc.tzname()}"


def gamma_strip(session) -> html.Div:
    """MISS-4 / AMENDMENT v1.4 — the always-visible one-line answer, per pair.

    "LONG GAMMA · EUR 4.0mm per 1% · pays above 41 pips today · costs USD 2,870".

    **Ruling 2 of amendment v1.4 is binding on this function: theta is computed from
    ``book_greeks`` on every render, never from a constant.**  The trader's reference
    table said USD 5,800/day for the EURUSD 1M ATM straddle; pricing that very trade
    gives USD 2,868, and the 5,800 figure is withdrawn.  Shipping a constant here
    would overstate the daily cost of carry by 2x in the single most-read number in
    the app, so there is no constant to ship: every figure below comes from
    ``app.pricing.headline`` -> ``fxgamma.portfolio.risk.book_greeks``.

    The theta charged is to the **next mark**, not always one day: on a Friday the
    strip says three calendar days and shows the three-day number (trader Q-6).
    """
    try:
        snap = session.snapshot()
        book = session.store.load_book()
        marks = session.store.marks()
    except Exception as exc:                               # noqa: BLE001
        return html.Div(f"header risk unavailable: {exc}", className="tiny",
                        style={"color": KIND_COLORS["unavailable"], "padding": "4px 0"})
    if not book.options and not book.spots:
        return html.Div(
            ["EMPTY BOOK — no gamma, no theta. ",
             dcc.Link("add a trade on the Book page", href="/book",
                      style={"color": ACCENT})],
            className="gamma-strip",
            style={"color": TEXT_DIM, "fontSize": "12px", "padding": "5px 0"})
    items = []
    for pair in book.pairs():
        try:
            h = headline(book, snap, pair, marks=marks)
        except Exception as exc:                           # noqa: BLE001
            items.append(html.Span(f"{pair}: not priced ({str(exc)[:70]})",
                                   style={"color": KIND_COLORS["unavailable"]}))
            continue
        colour = (POS if h["gamma_1pct"] > 0 else NEG if h["gamma_1pct"] < 0 else TEXT_DIM)
        items.append(html.Span(
            [html.Span(f"{pair} ", style={"color": TEXT_DIM}),
             html.Span(h["text"] or EM_DASH, style={"color": colour, "fontWeight": 600})],
            title=(f"theta from book_greeks on this render (amendment v1.4 ruling 2), "
                   f"charged over {h['days']:g} calendar day(s) to the next mark. "
                   f"Breakeven basis {ECONOMICS_BASIS}; any distance/sigma-day figure "
                   f"on a page uses {DISTANCE_BASIS}."),
            style={"marginRight": "16px", "whiteSpace": "nowrap"}))
    return html.Div(items, className="gamma-strip",
                    style={"display": "flex", "flexWrap": "wrap", "gap": "2px",
                           "fontSize": "12px", "padding": "5px 0",
                           "borderTop": "1px solid #222b36",
                           "borderBottom": "1px solid #222b36", "marginTop": "6px"})


def header(session) -> html.Div:
    snap = session.snapshot()
    tz = str(session.setting("timezone", "Europe/London"))
    n_syn = sum(1 for p in snap.meta.values() if getattr(p, "kind", "") == "synthetic")
    n_ovr = sum(1 for p in snap.meta.values() if getattr(p, "kind", "") == "user_override")
    stats = session.store.stats()
    marked = session.marked_pairs()

    book_chip = (chip(f"BOOK {stats['options']} opt / {stats['spots']} spot", ACCENT)
                 if stats["options"] or stats["spots"]
                 else chip("EMPTY BOOK", TEXT_DIM, "add a trade on the Book page"))
    demo = chip("DEMO BOOK — synthetic", KIND_COLORS["synthetic"],
                "REQ-006: seeded on first run so every screen is populated") \
        if getattr(session, "demo_seeded", False) else None
    mark_chip = (chip(f"MARKED: {', '.join(marked)}", KIND_COLORS["user_override"],
                      "these pairs price off your own vol grid (amendment v1.2 T-1)")
                 if marked else
                 chip("NO MANUAL MARK", KIND_COLORS["cached"],
                      "the book is priced off an INDICATIVE surface. Paste your grid on "
                      "the Data page before hedging off this screen."))

    return html.Div([
        html.Div([
            html.Div([html.Span("FX GAMMA "), html.Span("DESK")], className="app-title"),
            html.Div([
                html.Span(f"snapshot {session.snapshot_id or '—'}", title="REQ-001: one "
                          "stamped snapshot per session; every page reads this one."),
                html.Span(f"asof {local_time(snap.asof, tz)}"),
                html.Span(f"provider {session.provider_name}"),
                provenance_badge(snap.meta, "snapshot", compact=True),
                html.Span(f"{n_syn} synthetic · {n_ovr} override fields",
                          style={"color": KIND_COLORS["synthetic"] if n_syn else TEXT_DIM}),
            ], className="header-meta"),
            html.Div([
                html.Button("Refresh ⟳", id="refresh-btn", n_clicks=0,
                            title="Restamp the snapshot (keyboard: r)"),
            ], style={"marginLeft": "auto", "display": "flex", "gap": "6px"}),
        ], className="header-row"),
        html.Div([book_chip, demo, mark_chip,
                  chip(f"report ccy {session.report_ccy}", TEXT_DIM,
                       "cross-pair totals are converted at the snapshot spot; the rate "
                       "used is shown on the row")],
                 style={"marginTop": "6px", "display": "flex", "flexWrap": "wrap",
                        "gap": "4px", "alignItems": "center"}),
        gamma_strip(session),
        html.Div(
            [dcc.Link(label, href=href, id={"type": "nav", "href": href},
                      className="nav-link") for href, label in NAV]
            + [html.Span(label, className="nav-pending", title="built in the next task "
                         "against the frozen contract section 5",
                         style={"padding": "6px 12px", "color": "#4a5663",
                                "fontSize": "12px"})
               for _, label in PENDING],
            className="nav", id="nav-bar"),
    ], className="app-header")


def shell(session) -> html.Div:
    """Full page skeleton: stores, header, routed page container."""
    return html.Div([
        dcc.Location(id="url"),
        dcc.Store(id="snapshot-token", data=session.token()),
        dcc.Store(id="book-version", data=0),
        html.Div(id="header-container", children=header(session)),
        html.Div(dash.page_container, className="page"),
        html.Div([
            html.Span("Numbers are badged by source: "),
            *[html.Span(f" {k} ", style={"color": v, "fontWeight": 700})
              for k, v in list(KIND_COLORS.items())[:4]],
            html.Span("  ·  vols are decimals internally and percent on screen  ·  "
                      "notional is always base ccy  ·  theta is signed, negative = you pay"),
        ], style={"color": "#4a5663", "fontSize": "11px", "padding": "6px 14px 20px"}),
    ])
