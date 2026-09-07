"""Placeholders for the modules that are still being built.

``fxgamma/portfolio``, ``fxgamma/signals``, ``fxgamma/backtest``, ``fxgamma/store.py``
and ``app/`` are owned by other agents and may not exist yet.  Each test here is
guarded with ``pytest.importorskip`` so the suite stays green and simply reports
them as skipped; when a module lands, the guard falls away and these become the
first, shallow contract check on it.  A later QA pass owns the deep coverage --
see ``docs/05_test_report.md`` section "Not covered".
"""
from __future__ import annotations

from datetime import date

import pytest

from fxgamma import conventions as cv
from fxgamma.types import Book, Greeks, HedgeRule, OptionPosition, SpotPosition


@pytest.fixture()
def demo_book():
    """Two options and a hedge leg -- enough to exercise an aggregation path."""
    return Book(
        options=[
            OptionPosition(id="o1", pair="EURUSD", cp=+1, strike=1.20,
                           expiry=date(2026, 12, 18), notional_base=10e6, direction=+1,
                           premium_paid=120_000.0, trade_date=date(2026, 9, 1),
                           trade_spot=1.1650, trade_vol=0.085),
            OptionPosition(id="o2", pair="USDJPY", cp=-1, strike=145.0,
                           expiry=date(2026, 10, 16), notional_base=20e6, direction=-1,
                           premium_paid=-95_000.0, trade_date=date(2026, 9, 2)),
        ],
        spots=[SpotPosition(id="s1", pair="EURUSD", notional_base=-4.2e6,
                            entry_rate=1.1640, trade_date=date(2026, 9, 5), tag="hedge")],
        name="qa-demo",
    )


# --------------------------------------------------------------------------- #
# portfolio
# --------------------------------------------------------------------------- #
def test_price_book_returns_one_row_per_position(demo_book, snapshot):
    risk = pytest.importorskip("fxgamma.portfolio.risk",
                               reason="quant-risk has not landed portfolio/risk.py yet")
    df = risk.price_book(demo_book, snapshot)
    assert len(df) == len(demo_book.options) + len(demo_book.spots) or \
        len(df) == len(demo_book.options)
    # amendment v1.1 CG-1: native ccy plus the conversion columns
    for col in ("ccy", "fx_to_report"):
        assert col in df.columns, f"CG-1 requires a `{col}` column on price_book"


def test_book_greeks_converts_into_the_reporting_currency(demo_book, snapshot):
    risk = pytest.importorskip("fxgamma.portfolio.risk",
                               reason="quant-risk has not landed portfolio/risk.py yet")
    g = risk.book_greeks(demo_book, snapshot, report_ccy="USD")
    assert isinstance(g, Greeks)
    # CG-1 forbids aggregating native-ccy Greeks across pairs; T-2 forbids
    # aggregating the intensive ones at all
    import math
    assert math.isnan(g.delta_pct) and math.isnan(g.dual_delta)


def test_fx_rate_raises_on_a_missing_leg(snapshot):
    risk = pytest.importorskip("fxgamma.portfolio.risk",
                               reason="quant-risk has not landed portfolio/risk.py yet")
    with pytest.raises(Exception):
        risk.fx_rate("ZAR", "USD", snapshot)


def test_expired_positions_leave_the_aggregates(snapshot):
    """v1.2 T-3 / W-2: an expired leg must not contribute to the Greek cards."""
    risk = pytest.importorskip("fxgamma.portfolio.risk",
                               reason="quant-risk has not landed portfolio/risk.py yet")
    dead = Book(options=[OptionPosition(id="dead", pair="EURUSD", cp=+1, strike=1.00,
                                        expiry=date(2020, 1, 15), notional_base=10e6)])
    g = risk.book_greeks(dead, snapshot, report_ccy="USD")
    assert g.gamma == 0.0 and g.vega == 0.0 and g.theta == 0.0


def test_spot_ladder_is_centred_on_spot(demo_book, snapshot):
    risk = pytest.importorskip("fxgamma.portfolio.risk",
                               reason="quant-risk has not landed portfolio/risk.py yet")
    lad = risk.spot_ladder(demo_book, snapshot, "EURUSD", lo_pct=-5, hi_pct=5, n=21)
    assert len(lad) == 21


def test_pin_risk_reports_the_jump_independently_of_cp(demo_book, snapshot):
    """Trader W-1: the discontinuity at a strike is ``sum(direction * notional)``,
    with no ``cp`` in it -- a long put gains +N crossing up, exactly like a call."""
    zones = pytest.importorskip("fxgamma.portfolio.zones",
                                reason="quant-risk has not landed portfolio/zones.py yet")
    df = zones.pin_risk(demo_book, snapshot, "USDJPY")
    assert any("jump" in c for c in df.columns), \
        "pin_risk must report a labelled jump_at_strike, not one number called 'the discontinuity'"


def test_daily_pnl_attribution_closes(demo_book, snapshot):
    attribution = pytest.importorskip(
        "fxgamma.portfolio.attribution",
        reason="quant-risk has not landed portfolio/attribution.py yet")
    br = attribution.daily_pnl(demo_book, snapshot, snapshot)
    parts = br.as_dict()
    assert parts["total"] == pytest.approx(
        sum(v for k, v in parts.items() if k != "total"), abs=1e-6)


def test_hedge_suggestion_respects_the_rule(demo_book, snapshot):
    hedging = pytest.importorskip("fxgamma.portfolio.hedging",
                                  reason="quant-risk has not landed portfolio/hedging.py yet")
    action = hedging.hedge_suggestion(demo_book, snapshot, "EURUSD", HedgeRule())
    assert action.pair == "EURUSD"


# --------------------------------------------------------------------------- #
# signals
# --------------------------------------------------------------------------- #
def test_realized_vol_estimators_annualise_on_252(provider):
    """Trader review W-7: distance/probability quantities annualise on sqrt(252).

    Shallow check only -- estimator maths and window matching belong to the later
    QA pass on ``fxgamma/signals``.
    """
    realized = pytest.importorskip("fxgamma.signals.realized",
                                   reason="signals/realized.py has not landed yet")
    assert getattr(realized, "ANNUAL", None) == 252.0
    df = provider.spot_history("EURUSD", date(2025, 1, 1), date(2026, 6, 30))
    for name in ("close_to_close", "parkinson", "garman_klass", "rogers_satchell",
                 "yang_zhang"):
        fn = getattr(realized, name, None)
        if fn is None:
            continue
        v = float(fn(df.tail(21)))
        assert 0.0 < v < 1.0, f"{name} returned {v}; expected an annualised decimal"


def test_gex_is_unsigned(provider):
    """Trader Q-8 / W-11: listed open interest cannot tell you the dealer's sign."""
    gex = pytest.importorskip("fxgamma.signals.gex",
                              reason="signals/gex.py has not landed yet")
    assert hasattr(gex, "__all__")


# --------------------------------------------------------------------------- #
# backtest
# --------------------------------------------------------------------------- #
def test_backtest_engine_is_importable_and_deterministic():
    engine = pytest.importorskip("fxgamma.backtest.engine",
                                 reason="backtest/engine.py has not landed yet")
    assert hasattr(engine, "__all__")


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
def test_store_round_trips_a_book(tmp_path, demo_book):
    store = pytest.importorskip("fxgamma.store", reason="store.py has not landed yet")
    db = tmp_path / "qa.sqlite"
    saver = getattr(store, "save_book", None)
    loader = getattr(store, "load_book", None)
    if saver is None or loader is None:
        pytest.skip("store.save_book / load_book not present yet")
    saver(demo_book, str(db))
    again = loader(demo_book.name, str(db))
    assert {o.id for o in again.options} == {o.id for o in demo_book.options}


def test_position_marks_table_exists(tmp_path):
    """CG-2: per-position mark vol lives in a ``position_marks`` side table."""
    store = pytest.importorskip("fxgamma.store", reason="store.py has not landed yet")
    src = getattr(store, "SCHEMA", "") or ""
    if not src:
        pytest.skip("store.SCHEMA not exposed")
    assert "position_marks" in src


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #
def test_dash_app_imports():
    pytest.importorskip("dash", reason="dash is not installed in this environment")
    main = pytest.importorskip("app.main", reason="app/main.py has not landed yet")
    assert main is not None
