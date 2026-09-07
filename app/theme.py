"""Dark theme tokens and the shared Plotly template.

Dark is the default (contract section 6 / requirements section 5.7) and every chart must be
legible in it.  Two rules from the review are baked in here rather than left to each page:

* **No information by colour alone.**  The provenance palette below is paired with a
  text label everywhere it is used, and the diverging scale is colour-blind safe.
* **A zero line wherever sign matters**, drawn by :func:`base_layout`.
"""
from __future__ import annotations

from typing import Any

# ------------------------------------------------------------------ palette
BG = "#0e1116"           # page
PANEL = "#161b22"        # card
PANEL_2 = "#1c232c"      # nested / table header
BORDER = "#2b3440"
GRID = "#222b36"
TEXT = "#e6edf3"
TEXT_DIM = "#9aa7b4"
TEXT_FAINT = "#6e7d8d"
ACCENT = "#4da3ff"

POS = "#3fb950"          # long / gain
NEG = "#f85149"          # short / loss
WARN = "#d29922"
INFO = "#58a6ff"

#: REQ-002 fixes these four colours: live green, cached amber, synthetic purple,
#: user_override blue.  Anything else is treated as unknown and rendered grey.
KIND_COLORS: dict[str, str] = {
    "live": "#2ea043",
    "cached": "#d29922",
    "synthetic": "#a371f7",
    "user_override": "#3b82f6",
    "unavailable": "#f85149",
    "unknown": "#6e7d8d",
}
KIND_LABEL: dict[str, str] = {
    "live": "LIVE",
    "cached": "CACHED",
    "synthetic": "SYNTHETIC",
    "user_override": "OVERRIDE",
    "unavailable": "MISSING",
    "unknown": "UNKNOWN",
}

#: series colours - qualitative, distinguishable in dark, not red/green coded
SERIES = ["#4da3ff", "#f0883e", "#a371f7", "#3fb950", "#e3b341", "#39c5cf",
          "#ff7b72", "#d2a8ff", "#7ee787", "#ffa657"]

#: diverging (colour-blind safe, centred at zero) for RV-IV spreads and scenario grids
DIVERGING = [[0.0, "#2166ac"], [0.25, "#67a9cf"], [0.5, "#1c232c"],
             [0.75, "#ef8a62"], [1.0, "#b2182b"]]
SEQUENTIAL = [[0.0, "#0e1116"], [0.4, "#1f4e79"], [0.7, "#4da3ff"], [1.0, "#c9e5ff"]]

FONT = ('ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, '
        '"Liberation Mono", monospace')
FONT_UI = ('-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, '
           'Helvetica, Arial, sans-serif')


def base_layout(**over: Any) -> dict[str, Any]:
    """Plotly layout defaults: dark, gridded, unit-bearing axes, generous hover."""
    lay: dict[str, Any] = dict(
        paper_bgcolor=BG,
        plot_bgcolor=PANEL,
        font=dict(color=TEXT, family=FONT, size=12),
        margin=dict(l=62, r=18, t=44, b=46),
        hoverlabel=dict(bgcolor=PANEL_2, font=dict(color=TEXT, family=FONT, size=12),
                        bordercolor=BORDER),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(color=TEXT_DIM, size=11),
                    orientation="h", yanchor="bottom", y=1.02, x=0),
        xaxis=dict(gridcolor=GRID, zerolinecolor=BORDER, linecolor=BORDER,
                   tickfont=dict(color=TEXT_DIM, size=11),
                   title=dict(font=dict(color=TEXT_DIM, size=11))),
        yaxis=dict(gridcolor=GRID, zerolinecolor=BORDER, linecolor=BORDER,
                   tickfont=dict(color=TEXT_DIM, size=11),
                   title=dict(font=dict(color=TEXT_DIM, size=11))),
        title=dict(font=dict(color=TEXT, size=13), x=0.01, xanchor="left"),
        colorway=SERIES,
        uirevision="keep",
    )
    lay.update(over)
    return lay


def empty_figure(message: str = "no data", *, height: int = 260) -> dict[str, Any]:
    """REQ-003: an empty *axis* with a message - never a blank white panel."""
    return {
        "data": [],
        "layout": base_layout(
            height=height,
            xaxis=dict(gridcolor=GRID, zerolinecolor=BORDER, linecolor=BORDER,
                       showticklabels=False, zeroline=False),
            yaxis=dict(gridcolor=GRID, zerolinecolor=BORDER, linecolor=BORDER,
                       showticklabels=False, zeroline=False),
            annotations=[dict(text=message, x=0.5, y=0.5, xref="paper", yref="paper",
                              showarrow=False,
                              font=dict(color=TEXT_FAINT, size=13, family=FONT))],
        ),
    }


#: dash_table style bundle, reused by every table on every page
TABLE_STYLE = dict(
    style_table={"overflowX": "auto"},
    style_header={"backgroundColor": PANEL_2, "color": TEXT_DIM, "fontWeight": "600",
                  "border": f"1px solid {BORDER}", "whiteSpace": "normal",
                  "height": "auto", "fontSize": "12px", "textAlign": "right"},
    style_cell={"backgroundColor": PANEL, "color": TEXT, "border": f"1px solid {BORDER}",
                "fontFamily": FONT, "fontSize": "12px", "padding": "5px 8px",
                "textAlign": "right", "minWidth": "62px"},
    style_data_conditional=[
        {"if": {"row_index": "odd"}, "backgroundColor": "#12171e"},
    ],
)
