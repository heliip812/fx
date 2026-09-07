"""Panels, cards and metric tiles. Every metric carries its unit, by construction."""
from __future__ import annotations

from typing import Any

from dash import html

from ..theme import BORDER, NEG, PANEL, POS, TEXT, TEXT_DIM, TEXT_FAINT


def section_title(text: str, sub: str = "", right: Any = None) -> html.Div:
    return html.Div(
        [html.Div([html.Span(text, className="sec-title"),
                   html.Span(sub, className="sec-sub")]),
         html.Div(right or "", style={"marginLeft": "auto"})],
        style={"display": "flex", "alignItems": "baseline", "gap": "10px",
               "marginBottom": "8px"})


def panel(children: Any, *, title: str = "", sub: str = "", right: Any = None,
          style: dict | None = None, id: str | None = None) -> html.Div:
    body = []
    if title:
        body.append(section_title(title, sub, right))
    body.append(html.Div(children))
    kw = {"id": id} if id else {}
    return html.Div(body, className="panel", style=style or {}, **kw)


card = panel


def metric(label: str, value: Any, *, unit: str = "", hint: str = "",
           tone: str = "", badge: Any = None, sub: str = "") -> html.Div:
    """One number. ``unit`` is printed under it - never omitted for a Greek."""
    colour = {"pos": POS, "neg": NEG, "": TEXT}.get(tone, TEXT)
    return html.Div(
        [html.Div([html.Span(label, className="metric-label"), badge or ""],
                  style={"display": "flex", "alignItems": "center"}),
         html.Div(value, className="metric-value", style={"color": colour},
                  title=hint or unit),
         html.Div(sub, className="metric-sub") if sub else None,
         html.Div(unit, className="metric-unit", title=hint or unit) if unit else None],
        className="metric")


def metric_row(items: list[Any], *, cols: str = "repeat(auto-fit, minmax(190px, 1fr))"
               ) -> html.Div:
    return html.Div(items, style={"display": "grid", "gridTemplateColumns": cols,
                                  "gap": "8px"})


def grid(items: list[Any], cols: str = "1fr 1fr", gap: str = "10px") -> html.Div:
    return html.Div(items, style={"display": "grid", "gridTemplateColumns": cols,
                                  "gap": gap, "alignItems": "start"})


def note(text: str, *, tone: str = "dim") -> html.Div:
    colour = {"dim": TEXT_DIM, "faint": TEXT_FAINT, "warn": "#d29922"}.get(tone, TEXT_DIM)
    return html.Div(text, style={"color": colour, "fontSize": "11px", "lineHeight": "1.5",
                                 "marginTop": "6px"})


def kv(rows: list[tuple[str, Any]]) -> html.Table:
    return html.Table([html.Tbody([
        html.Tr([html.Td(k, style={"color": TEXT_DIM, "padding": "2px 12px 2px 0",
                                   "whiteSpace": "nowrap", "verticalAlign": "top"}),
                 html.Td(v, style={"color": TEXT, "padding": "2px 0"})])
        for k, v in rows])], style={"borderCollapse": "collapse", "fontSize": "12px"})


def divider() -> html.Div:
    return html.Div(style={"borderTop": f"1px solid {BORDER}", "margin": "10px 0"})


def empty_state(message: str, hint: str = "") -> html.Div:
    return html.Div([html.Div(message, style={"color": TEXT_DIM, "fontSize": "13px"}),
                     html.Div(hint, style={"color": TEXT_FAINT, "fontSize": "11px",
                                           "marginTop": "4px"}) if hint else None],
                    style={"backgroundColor": PANEL, "border": f"1px dashed {BORDER}",
                           "borderRadius": "5px", "padding": "18px", "textAlign": "center"})
