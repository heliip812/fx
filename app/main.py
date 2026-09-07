"""Dash application factory  (contract section 6).

Five pages are wired here: Market Monitor, Vol Surface, Gamma Map, Position Book and
Data & Settings.  Pages 6-8 (Risk, P&L, Lab) depend on ``fxgamma.portfolio``, which is
being written against the frozen contract section 5; they are shown greyed in the nav and
wired in a later task rather than stubbed with fake numbers.

Everything is defensive by policy (requirements section 5.7): no traceback ever reaches the
browser, every callback catches and renders a panel-level message naming what failed, and
every figure degrades to an empty axis with a sentence rather than a blank panel.
"""
from __future__ import annotations

import logging

import dash
from dash import Input, Output, dcc, html

from .layout import header, shell
from .state import Session, get_session, set_session

log = logging.getLogger(__name__)


def create_app(provider: str = "synthetic", *, db: str | None = None,
               seed_demo: bool = True, session: Session | None = None) -> dash.Dash:
    sess = session or Session(provider=provider, db=db, seed_demo=seed_demo)
    set_session(sess)

    app = dash.Dash(
        __name__,
        use_pages=True,
        pages_folder="",
        suppress_callback_exceptions=True,
        update_title=None,
        title="FX Gamma Desk",
        meta_tags=[{"name": "viewport",
                    "content": "width=device-width, initial-scale=1"}],
    )

    # importing a page registers it (dash.register_page at module scope)
    from .pages import book, data, gamma_map, market, surface     # noqa: F401

    app.layout = lambda: shell(get_session())

    @app.callback(Output("snapshot-token", "data"),
                  Output("header-container", "children"),
                  Input("refresh-btn", "n_clicks"),
                  prevent_initial_call=True)
    def _refresh(n):
        s = get_session()
        try:
            s.build()
        except Exception as exc:                           # noqa: BLE001
            log.exception("refresh failed")
            s.errors.append(f"refresh failed: {exc}")
        return s.token(), header(s)

    app.clientside_callback(
        """function(path){
            document.querySelectorAll('#nav-bar a').forEach(function(a){
                if (a.getAttribute('href') === path) { a.classList.add('active'); }
                else { a.classList.remove('active'); }
            });
            return window.dash_clientside.no_update;
        }""",
        Output("nav-bar", "data-active"),
        Input("url", "pathname"),
    )
    return app


def main(argv: list[str] | None = None) -> int:      # pragma: no cover - see run.py
    from run import main as _m
    return _m(argv)
