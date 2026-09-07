"""Chart helpers shared by the pages. Dark-legible, unit-labelled, zero-lined."""
from __future__ import annotations

from typing import Any

from ..theme import ACCENT, BORDER, TEXT_DIM, WARN, base_layout, empty_figure


def zero_line(fig: dict, *, axis: str = "y") -> dict:
    """Requirements section 5.7: draw the zero line wherever sign matters."""
    shapes = list(fig["layout"].get("shapes", []))
    if axis == "y":
        shapes.append(dict(type="line", xref="paper", x0=0, x1=1, yref="y", y0=0, y1=0,
                           line=dict(color=BORDER, width=1)))
    else:
        shapes.append(dict(type="line", yref="paper", y0=0, y1=1, xref="x", x0=0, x1=0,
                           line=dict(color=BORDER, width=1)))
    fig["layout"]["shapes"] = shapes
    return fig


def spot_line(fig: dict, spot: float, *, label: str = "spot", colour: str = ACCENT,
              dash: str = "solid") -> dict:
    """Vertical spot marker with its value written on it."""
    if spot is None:
        return fig
    shapes = list(fig["layout"].get("shapes", []))
    shapes.append(dict(type="line", x0=spot, x1=spot, yref="paper", y0=0, y1=1,
                       line=dict(color=colour, width=1.4, dash=dash)))
    fig["layout"]["shapes"] = shapes
    anns = list(fig["layout"].get("annotations", []))
    anns.append(dict(x=spot, y=1.0, yref="paper", text=f"{label} {spot:,.4f}".rstrip("0"),
                     showarrow=False, font=dict(color=colour, size=10),
                     xanchor="left", yanchor="bottom"))
    fig["layout"]["annotations"] = anns
    return fig


def vline(fig: dict, x: Any, label: str = "", colour: str = WARN) -> dict:
    shapes = list(fig["layout"].get("shapes", []))
    shapes.append(dict(type="line", x0=x, x1=x, yref="paper", y0=0, y1=1,
                       line=dict(color=colour, width=1, dash="dot")))
    fig["layout"]["shapes"] = shapes
    if label:
        anns = list(fig["layout"].get("annotations", []))
        anns.append(dict(x=x, y=1.0, yref="paper", text=label, showarrow=False,
                         font=dict(color=colour, size=9), textangle=-90,
                         xanchor="right", yanchor="top"))
        fig["layout"]["annotations"] = anns
    return fig


def figure(traces: list[dict], *, title: str = "", xtitle: str = "", ytitle: str = "",
           height: int = 300, **layout: Any) -> dict:
    """Axis titles always carry units - the caller passes them written out."""
    lay = base_layout(height=height, title=dict(text=title, x=0.01, xanchor="left"),
                      **layout)
    lay["xaxis"] = {**lay["xaxis"], "title": {"text": xtitle,
                                              "font": {"color": TEXT_DIM, "size": 11}}}
    lay["yaxis"] = {**lay["yaxis"], "title": {"text": ytitle,
                                              "font": {"color": TEXT_DIM, "size": 11}}}
    return {"data": traces, "layout": lay}


__all__ = ["zero_line", "spot_line", "vline", "figure", "empty_figure"]
