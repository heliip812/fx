"""Dash DataTable wrapper: dark, sortable, CSV-exportable, empty-safe."""
from __future__ import annotations

from typing import Any

from dash import dash_table, html

from ..theme import BORDER, NEG, POS, TABLE_STYLE, TEXT_DIM, TEXT_FAINT


def empty_table_note(message: str = "no positions — import a CSV or add a trade"
                     ) -> html.Div:
    """REQ-003: an empty table says what to do, and never shows a blank panel."""
    return html.Div(message, style={"color": TEXT_FAINT, "fontSize": "12px",
                                    "padding": "16px", "textAlign": "center",
                                    "border": f"1px dashed {BORDER}", "borderRadius": "5px"})


def data_table(id: str, columns: list[dict], data: list[dict], *,
               page_size: int = 25, sort: bool = True, height: str | None = None,
               conditional: list[dict] | None = None, editable: bool = False,
               row_selectable: str | None = None, tooltips: dict | None = None,
               **kw: Any):
    """Every table is sortable and exports CSV (requirements section 5.7)."""
    style = dict(TABLE_STYLE)
    cond = list(style["style_data_conditional"]) + list(conditional or [])
    tbl_style = dict(style["style_table"])
    if height:
        tbl_style.update({"maxHeight": height, "overflowY": "auto"})
    return dash_table.DataTable(
        id=id,
        columns=columns,
        data=data or [],
        sort_action="native" if sort else "none",
        filter_action="none",
        page_size=page_size,
        page_action="native" if len(data or []) > page_size else "none",
        export_format="csv",
        export_headers="display",
        editable=editable,
        row_selectable=row_selectable,
        style_table=tbl_style,
        style_header=style["style_header"],
        style_cell=style["style_cell"],
        style_data_conditional=cond,
        tooltip_header=tooltips or {},
        tooltip_delay=200,
        tooltip_duration=None,
        css=[{"selector": ".dash-spreadsheet-menu", "rule": "color: #9aa7b4;"},
             {"selector": ".export", "rule":
              "color:#9aa7b4;background:#1c232c;border:1px solid #2b3440;"
              "border-radius:3px;padding:2px 8px;font-size:11px;cursor:pointer;"}],
        **kw,
    )


def sign_rules(columns: list[str]) -> list[dict]:
    """Colour negatives red and positives green - alongside the printed sign, never instead."""
    out = []
    for c in columns:
        out.append({"if": {"filter_query": f"{{{c}}} < 0", "column_id": c}, "color": NEG})
        out.append({"if": {"filter_query": f"{{{c}}} > 0", "column_id": c}, "color": POS})
    return out


def status_rules(col: str = "status") -> list[dict]:
    return [
        {"if": {"filter_query": f'{{{col}}} = "ERROR"', "column_id": col},
         "color": NEG, "fontWeight": "700"},
        {"if": {"filter_query": f'{{{col}}} = "WARN"', "column_id": col},
         "color": "#d29922", "fontWeight": "700"},
        {"if": {"filter_query": f'{{{col}}} = "OK"', "column_id": col}, "color": POS},
        {"if": {"filter_query": f'{{{col}}} = "EXPIRED"', "column_id": col},
         "color": TEXT_DIM, "fontStyle": "italic"},
    ]


def col(name: str, id: str | None = None, *, unit: str = "", numeric: bool = False,
        **kw: Any) -> dict:
    """A column whose header carries its unit (requirements section 5.4)."""
    head = f"{name}\n{unit}" if unit else name
    d: dict[str, Any] = {"name": head.split("\n"), "id": id or name}
    if numeric:
        d["type"] = "numeric"
    d.update(kw)
    return d
