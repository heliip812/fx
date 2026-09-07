"""Reusable UI pieces. The two that matter: provenance badges and unit-safe numbers."""
from .fmt import (dash_if_none, fmt_ccy, fmt_delta_base, fmt_mm, fmt_money, fmt_pct,
                  fmt_pips, fmt_signed, fmt_spot, fmt_strike, fmt_vol, fmt_vol_pts,
                  GREEK_UNITS, greek_label, greek_unit, move_both, pips_between)
from .badges import (kind_of, provenance_badge, provenance_text, staleness, stale_badge,
                     synthetic_banner, surface_status, surface_status_badge)
from .cards import card, metric, metric_row, panel, section_title
from .tables import data_table, empty_table_note
from .charts import spot_line, zero_line

__all__ = [
    "fmt_spot", "fmt_strike", "fmt_pips", "fmt_pct", "fmt_vol", "fmt_vol_pts", "fmt_money",
    "fmt_mm", "fmt_ccy", "fmt_signed", "fmt_delta_base", "dash_if_none", "move_both",
    "pips_between", "GREEK_UNITS", "greek_label", "greek_unit",
    "provenance_badge", "provenance_text", "kind_of", "staleness", "stale_badge",
    "synthetic_banner", "surface_status", "surface_status_badge",
    "card", "panel", "metric", "metric_row", "section_title",
    "data_table", "empty_table_note", "spot_line", "zero_line",
]
