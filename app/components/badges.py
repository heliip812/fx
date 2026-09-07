"""Provenance badges  (contract section 7, REQ-002, amendment v1.1 CG-7).

The trader called this the best feature in the spec, so it is built to be impossible
to miss rather than to be tasteful:

* every badge shows the **kind word** as text as well as the colour (no meaning by
  colour alone), the **source**, and the **age** of the field;
* :func:`synthetic_banner` puts a page-wide bar on any page holding a synthetic or
  overridden number, so a screen full of simulated data can never be mistaken for a
  marked one;
* lookup follows the frozen CG-7 grammar - ``spot.<PAIR>``, ``rate.<CCY>``,
  ``surface.<PAIR>.<TENOR>``, ``oi.<PAIR>`` - resolved **most-specific-first**, so a
  badge asked for ``surface.EURUSD.1M`` falls back to ``surface.EURUSD``;
* a field with **no** provenance renders "UNKNOWN" in grey. It never renders as live.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from dash import html

from fxgamma.data.base import meta_lookup

from ..theme import BORDER, KIND_COLORS, KIND_LABEL, PANEL_2, TEXT_DIM

#: requirements section 5.2 staleness ladder
STALENESS = (
    (15 * 60, "live", "under 15 min old"),
    (60 * 60, "delayed", "15-60 min old"),
    (24 * 3600, "stale", "over an hour old - same session"),
)


def kind_of(meta: dict | None, key: str) -> str:
    """The badge kind for a CG-7 key, most-specific-first. Unknown is never 'live'."""
    p = meta_lookup(meta or {}, key)
    if p is None:
        return "unknown"
    k = getattr(p, "kind", "unknown")
    return k if k in KIND_COLORS else "unknown"


def age_seconds(p: Any, now: datetime | None = None) -> float | None:
    asof = getattr(p, "asof", None)
    if asof is None:
        return None
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - asof).total_seconds()


def staleness(p: Any, now: datetime | None = None) -> tuple[str, str]:
    """(word, explanation) from the section 5.2 ladder. Synthetic/override outrank age."""
    kind = getattr(p, "kind", "unknown")
    if kind in ("synthetic", "user_override", "unavailable"):
        return KIND_LABEL.get(kind, "UNKNOWN"), ""
    secs = age_seconds(p, now)
    if secs is None:
        return "UNKNOWN", "no timestamp on this field"
    for limit, word, why in STALENESS:
        if secs < limit:
            return word.upper(), f"{why} ({_age_text(secs)})"
    return "EOD", f"end-of-day mark, {_age_text(secs)} old"


def _age_text(secs: float) -> str:
    secs = max(secs, 0.0)
    if secs < 90:
        return f"{secs:.0f}s"
    if secs < 5400:
        return f"{secs / 60:.0f}m"
    if secs < 172800:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def provenance_text(meta: dict | None, key: str) -> str:
    """The full hover sentence: source, kind, age, note."""
    p = meta_lookup(meta or {}, key)
    if p is None:
        return (f"{key}: no provenance recorded. Treat as unknown - this build never "
                "labels an unbadged number as live.")
    word, why = staleness(p)
    asof = getattr(p, "asof", None)
    bits = [f"{key}", f"source: {p.source}", f"kind: {p.kind}", f"state: {word}"]
    if asof is not None:
        bits.append(f"asof: {asof:%Y-%m-%d %H:%M:%SZ}")
    if why:
        bits.append(why)
    if getattr(p, "note", ""):
        bits.append(str(p.note))
    return " | ".join(bits)


def provenance_badge(meta: dict | None, key: str, *, compact: bool = False,
                     label: str | None = None) -> html.Span:
    """The badge itself. ``key`` is a CG-7 key such as ``surface.EURUSD.1M``."""
    p = meta_lookup(meta or {}, key)
    kind = kind_of(meta, key)
    colour = KIND_COLORS[kind]
    word = KIND_LABEL[kind]
    if p is not None and kind == "live":
        state, _ = staleness(p)
        if state in ("DELAYED", "STALE", "EOD"):
            word = state
            colour = KIND_COLORS["cached"] if state == "DELAYED" else KIND_COLORS["unavailable"]
    text = word if compact else f"{word} · {getattr(p, 'source', '?')}"
    if label:
        text = f"{label} {text}"
    return html.Span(
        text,
        className="prov-badge",
        title=provenance_text(meta, key),
        style={"backgroundColor": colour + "22", "color": colour,
               "border": f"1px solid {colour}", "borderRadius": "3px",
               "padding": "1px 6px", "fontSize": "10px", "fontWeight": "700",
               "letterSpacing": "0.04em", "whiteSpace": "nowrap",
               "marginLeft": "6px", "cursor": "help"},
    )


def badge_row(meta: dict | None, keys: Iterable[str], **kw) -> html.Span:
    return html.Span([provenance_badge(meta, k, **kw) for k in keys],
                     style={"display": "inline-flex", "gap": "2px", "flexWrap": "wrap"})


def synthetic_banner(meta: dict | None, *, page: str = "") -> Any:
    """REQ-002: a persistent page-level banner whenever ANY field is not live.

    Split by kind so the sentence is specific: simulated numbers and the desk's own
    overrides are both "not live", but they are not the same warning.
    """
    meta = meta or {}
    synth = sorted(k for k, p in meta.items() if getattr(p, "kind", "") == "synthetic")
    over = sorted(k for k, p in meta.items() if getattr(p, "kind", "") == "user_override")
    miss = sorted(k for k, p in meta.items() if getattr(p, "kind", "") == "unavailable")
    bars = []
    if synth:
        bars.append(_bar(
            KIND_COLORS["synthetic"], "SYNTHETIC DATA",
            f"{len(synth)} field(s) on this page are SIMULATED, not market data. "
            "Nothing here is a mark and nothing here is tradable.",
            ", ".join(synth[:12]) + (" ..." if len(synth) > 12 else "")))
    if over:
        bars.append(_bar(
            KIND_COLORS["user_override"], "YOUR MARKS",
            f"{len(over)} field(s) come from your own manual marks and override every "
            "other source (amendment v1.2 T-1).",
            ", ".join(over[:12]) + (" ..." if len(over) > 12 else "")))
    if miss:
        bars.append(_bar(
            KIND_COLORS["unavailable"], "MISSING",
            f"{len(miss)} field(s) could not be built. They render as '—', never as 0.",
            ", ".join(miss[:12]) + (" ..." if len(miss) > 12 else "")))
    return html.Div(bars, style={"marginBottom": "10px"} if bars else {})


def _bar(colour: str, word: str, text: str, detail: str) -> html.Div:
    return html.Div(
        [html.Span(word, style={"fontWeight": "800", "color": colour,
                                "letterSpacing": "0.06em", "marginRight": "10px"}),
         html.Span(text, style={"color": TEXT_DIM}),
         html.Span(detail, title=detail,
                   style={"color": "#5c6b7a", "marginLeft": "10px", "fontSize": "11px",
                          "overflow": "hidden", "textOverflow": "ellipsis",
                          "whiteSpace": "nowrap", "maxWidth": "40%",
                          "display": "inline-block", "verticalAlign": "bottom"})],
        style={"backgroundColor": colour + "18", "border": f"1px solid {colour}55",
               "borderLeft": f"4px solid {colour}", "borderRadius": "4px",
               "padding": "6px 10px", "marginBottom": "6px", "fontSize": "12px"})


# ------------------------------------------------------------------ MARK / INDICATIVE
def surface_status(meta: dict | None, pair: str) -> tuple[str, str]:
    """Trader review Q-3: a surface is either the desk's **MARK** or **INDICATIVE**.

    Only a ``user_override`` surface is a MARK.  Everything else - ETF chains, CBOE
    indices, CME, the synthetic generator - is INDICATIVE: fine for z-scores, cones and
    richness, never the number you hedge off.
    """
    kind = kind_of(meta, f"surface.{pair.upper()}")
    if kind == "user_override":
        return "MARK", "built from your own ATM / 25d RR / 25d BF grid"
    if kind == "unavailable":
        return "NO SURFACE", "the surface could not be built for this pair"
    if kind == "synthetic":
        return "INDICATIVE", "SIMULATED surface - not a mark, not tradable"
    return "INDICATIVE", ("listed/proxy vols - usable for richness and z-scores, "
                          "not as a mark (trader review Q-3)")


def surface_status_badge(meta: dict | None, pair: str) -> html.Span:
    status, why = surface_status(meta, pair)
    colour = {"MARK": KIND_COLORS["user_override"],
              "NO SURFACE": KIND_COLORS["unavailable"]}.get(status, KIND_COLORS["synthetic"]
                                                            if "SIMULATED" in why
                                                            else KIND_COLORS["cached"])
    return html.Span(
        f"{pair} {status}", title=f"{why}. {provenance_text(meta, f'surface.{pair}')}",
        style={"backgroundColor": colour + "22", "color": colour,
               "border": f"1px solid {colour}", "borderRadius": "3px",
               "padding": "2px 7px", "fontSize": "11px", "fontWeight": "700",
               "marginRight": "6px", "cursor": "help"})


def stale_badge(word: str, detail: str = "") -> html.Span:
    colour = {"LIVE": KIND_COLORS["live"], "DELAYED": KIND_COLORS["cached"]}.get(
        word.upper(), KIND_COLORS["unavailable"])
    return html.Span(word.upper(), title=detail,
                     style={"color": colour, "border": f"1px solid {colour}",
                            "borderRadius": "3px", "padding": "1px 5px",
                            "fontSize": "10px", "fontWeight": "700"})


def chip(text: str, colour: str = TEXT_DIM, title: str = "") -> html.Span:
    return html.Span(text, title=title,
                     style={"backgroundColor": PANEL_2, "color": colour,
                            "border": f"1px solid {BORDER}", "borderRadius": "3px",
                            "padding": "1px 6px", "fontSize": "11px",
                            "marginRight": "5px", "whiteSpace": "nowrap"})
