"""Data-layer contract tests: schemas, provenance grammar, determinism, boundary.

Nothing here touches the network -- market-data hosts are blocked in this
environment, and a test that needs them is a test that will be skipped forever.
Everything runs off ``get_provider("synthetic")`` and the shipped fixtures.

The three things worth protecting here:

* **the section 8 boundary** -- a provider emits ``list[SmileQuotes]`` and calls
  ``build_surface``; nothing else crosses;
* **provenance** (arch s7 + CG-7) -- every number badged, most-specific-first
  lookup, synthetic never badged live;
* **determinism** -- "deterministic, offline" is the synthetic provider's entire
  reason to exist, and a backtest or an overnight P&L reconciliation is worthless
  without it.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from fxgamma import conventions as cv
from fxgamma.data import (EVENT_COLUMNS, OI_COLUMNS, SPOT_COLUMNS, MarketDataProvider,
                          SmileQuotes, get_provider, meta_lookup)
from fxgamma.types import Provenance

from conftest import ASOF, G3_G10

REPO = "/home/user/fx"


# --------------------------------------------------------------------------- #
# the section 8 boundary
# --------------------------------------------------------------------------- #
@pytest.mark.contract
def test_smile_quotes_is_the_models_class_not_a_stub():
    """``fxgamma.data.SmileQuotes`` must *be* the models class, not the local mirror.

    The data layer ships a field-identical fallback for when models/surface.py is
    absent.  If that fallback is ever in play in a built repo, two dataclasses with
    the same name flow around the app and `isinstance` checks start lying.
    """
    from fxgamma.data.base import surface_backend
    from fxgamma.models.surface import SmileQuotes as ModelQuotes
    assert SmileQuotes is ModelQuotes
    assert surface_backend() == "fxgamma.models.surface"


@pytest.mark.contract
@pytest.mark.parametrize("pair", G3_G10)
def test_smile_quotes_are_well_formed(pair, provider):
    """Vols are decimals, not percent; tenors ascend; BF is quoted as a smile BF."""
    quotes = provider.smile_quotes(pair, ASOF)
    assert quotes, f"no quotes for {pair}"
    assert all(isinstance(q, SmileQuotes) for q in quotes)
    Ts = [q.T for q in quotes]
    assert Ts == sorted(Ts) and len(set(Ts)) == len(Ts)
    for q in quotes:
        assert 0.0 < q.T <= 5.0
        assert 0.005 < q.atm < 1.0, f"{pair} {q.tenor}: ATM {q.atm} looks like percent"
        assert abs(q.rr25) < 0.10
        assert -0.02 < q.bf25 < 0.10
        if q.rr10 is not None:
            assert abs(q.rr10) >= abs(q.rr25) - 1e-9, "10d RR should not be inside 25d"
        if q.bf10 is not None:
            assert q.bf10 >= q.bf25 - 1e-9, "10d BF should not be below 25d"


@pytest.mark.contract
def test_provider_implements_the_abstract_interface(provider):
    assert isinstance(provider, MarketDataProvider)
    for method in ("spot", "spot_history", "rates", "smile_quotes",
                   "open_interest", "events", "snapshot", "status"):
        assert callable(getattr(provider, method))


# --------------------------------------------------------------------------- #
# frame schemas
# --------------------------------------------------------------------------- #
def test_spot_history_schema_and_ordering(provider):
    df = provider.spot_history("EURUSD", date(2026, 1, 1), date(2026, 6, 30))
    assert list(df.columns) == SPOT_COLUMNS
    assert isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None
    assert df.index.is_monotonic_increasing
    assert not df.isna().any().any()
    assert (df["high"] >= df[["open", "close"]].max(axis=1) - 1e-12).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1) + 1e-12).all()
    assert (df["high"] >= df["low"]).all()


def test_spot_history_has_no_look_ahead(provider):
    """The bar stamped D closed on D: a window request must not return later bars."""
    end = date(2026, 6, 30)
    df = provider.spot_history("EURUSD", date(2026, 1, 1), end)
    assert df.index.max().date() <= end


def test_open_interest_schema(provider):
    oi = provider.open_interest("USDJPY", ASOF)
    assert list(oi.columns) == OI_COLUMNS
    assert len(oi) > 0
    assert set(np.unique(oi["cp"])) <= {-1, 1}
    assert (oi["oi"] >= 0).all()
    assert (oi["strike"] > 0).all()
    assert all(isinstance(e, date) for e in oi["expiry"])


def test_events_schema_and_importance_grammar(provider):
    """CG-6 froze the columns and ``importance in {1,2,3}``."""
    ev = provider.events(date(2026, 1, 1), date(2026, 12, 31))
    assert list(ev.columns) == EVENT_COLUMNS
    if len(ev):
        assert set(ev["importance"].unique()) <= {1, 2, 3}
        assert set(ev["ccy"].unique()) <= set(cv.CCYS)
        assert str(ev["datetime"].dtype).endswith("UTC]")
        assert ev["datetime"].is_monotonic_increasing


def test_events_window_is_respected(provider):
    lo, hi = date(2026, 3, 1), date(2026, 3, 31)
    ev = provider.events(lo, hi)
    if len(ev):
        assert ev["datetime"].min().date() >= lo
        assert ev["datetime"].max().date() <= hi


def test_rates_are_decimals_not_percent(provider):
    r = provider.rates(cv.CCYS)
    assert set(r) <= set(cv.CCYS)
    for ccy, v in r.items():
        assert -0.05 < v < 0.25, f"{ccy}: {v} does not look like a decimal rate"


def test_spot_is_fordom_and_plausible(provider):
    s = provider.spot(list(cv.PAIRS))
    assert s["USDJPY"] > 50, "USDJPY must be quoted JPY per USD"
    assert 0.5 < s["EURUSD"] < 2.0
    assert 0.5 < s["EURGBP"] < 1.5
    # triangulation: EURJPY = EURUSD * USDJPY to within a few basis points
    assert s["EURJPY"] == pytest.approx(s["EURUSD"] * s["USDJPY"], rel=1e-6)


# --------------------------------------------------------------------------- #
# snapshot + provenance (arch s7, CG-7)
# --------------------------------------------------------------------------- #
@pytest.mark.contract
def test_snapshot_badges_every_field_it_fills(snapshot):
    for pair in snapshot.spot:
        assert f"spot.{pair}" in snapshot.meta
    for ccy in snapshot.rates:
        assert f"rate.{ccy}" in snapshot.meta
    for pair in snapshot.surfaces:
        assert meta_lookup(snapshot.meta, f"surface.{pair}") is not None
    assert all(isinstance(p, Provenance) for p in snapshot.meta.values())


@pytest.mark.contract
def test_synthetic_data_is_never_badged_live(snapshot):
    """Arch s7: never silently substitute synthetic data for live data."""
    kinds = {p.kind for p in snapshot.meta.values()}
    assert "live" not in kinds, f"synthetic provider badged something live: {kinds}"
    assert kinds <= {"synthetic", "unavailable", "cached", "user_override"}
    assert all("SIMULATED" in p.note or p.kind != "synthetic"
               for p in snapshot.meta.values())


@pytest.mark.contract
def test_meta_keys_follow_the_cg7_grammar(snapshot):
    """``spot.<PAIR>`` / ``rate.<CCY>`` / ``fwd.<PAIR>.<TENOR>`` / ``surface.<PAIR>``
    / ``oi.<PAIR>`` / ``events`` -- and nothing else."""
    for key in snapshot.meta:
        parts = key.split(".")
        head = parts[0]
        assert head in ("spot", "rate", "fwd", "surface", "oi", "events"), key
        if head == "events":
            assert len(parts) == 1
        elif head == "rate":
            assert len(parts) == 2 and parts[1] in cv.CCYS
        else:
            assert parts[1] in cv.PAIRS and parts[1].isupper() and len(parts[1]) == 6
            if head == "fwd":
                assert len(parts) == 3 and parts[2] in cv.TENORS
            elif head == "surface":
                assert len(parts) in (2, 3)
                if len(parts) == 3:
                    assert parts[2] in cv.TENORS
            else:
                assert len(parts) == 2


@pytest.mark.contract
def test_meta_lookup_resolves_most_specific_first():
    """CG-7: ``surface.EURUSD.1M`` falls back to ``surface.EURUSD``, and a more
    specific badge always wins over the generic one."""
    generic = Provenance("synthetic", "synthetic", note="pair level")
    specific = Provenance("user", "user_override", note="tenor level")
    meta = {"surface.EURUSD": generic}
    assert meta_lookup(meta, "surface.EURUSD.1M") is generic
    assert meta_lookup(meta, "surface.EURUSD") is generic
    meta["surface.EURUSD.1M"] = specific
    assert meta_lookup(meta, "surface.EURUSD.1M") is specific
    assert meta_lookup(meta, "surface.EURUSD.3M") is generic
    assert meta_lookup(meta, "surface.USDJPY") is None
    assert meta_lookup({}, "spot.EURUSD") is None


def test_snapshot_carries_forwards_consistent_with_covered_interest_parity(snapshot):
    import math
    for pair, curve in snapshot.forwards.items():
        spec = cv.pair_spec(pair)
        rd, rf = snapshot.rd_rf(pair, cv.PAIRS)
        for T, F in curve.items():
            assert F == pytest.approx(snapshot.spot[pair] * math.exp((rd - rf) * T),
                                      rel=1e-12)


def test_snapshot_surfaces_are_keyed_by_pair_and_priceable(snapshot):
    for pair, surf in snapshot.surfaces.items():
        assert surf.pair == pair
        v = surf.atm(0.25)
        assert 0.0 < v < 1.0


def test_a_missing_surface_is_omitted_and_recorded_never_faked(provider):
    """Arch s7 + base.snapshot: a surface that fails to build must be *absent* from
    ``surfaces`` with the failure in ``meta``, not silently replaced."""
    class Broken(type(provider)):                      # noqa: N801 - test double
        def smile_quotes(self, pair, asof=None, **kw):
            if pair == "GBPUSD":
                raise RuntimeError("simulated feed failure")
            return super().smile_quotes(pair, asof, **kw)

    snap = Broken(seed=provider.seed, asof=ASOF).snapshot(["EURUSD", "GBPUSD"], asof=ASOF)
    assert "GBPUSD" not in snap.surfaces
    assert "EURUSD" in snap.surfaces
    badge = snap.meta.get("surface.GBPUSD")
    assert badge is not None and badge.kind != "live"


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #
def test_same_seed_gives_the_same_market_within_a_process():
    from fxgamma.data.synthetic import SyntheticProvider
    a = SyntheticProvider(seed=4242, asof=ASOF)
    b = SyntheticProvider(seed=4242, asof=ASOF)
    assert a.spot(list(cv.PAIRS)) == b.spot(list(cv.PAIRS))
    assert a.smile_quotes("EURUSD", ASOF) == b.smile_quotes("EURUSD", ASOF)
    pd.testing.assert_frame_equal(a.spot_history("EURUSD", date(2026, 1, 1), date(2026, 6, 1)),
                                  b.spot_history("EURUSD", date(2026, 1, 1), date(2026, 6, 1)))


def test_different_seeds_give_a_different_history():
    """Spot *today* is deliberately anchored, so determinism is checked on the path."""
    from fxgamma.data.synthetic import SyntheticProvider
    a = SyntheticProvider(seed=1, asof=ASOF).spot_history("EURUSD", date(2025, 1, 1),
                                                          date(2026, 1, 1))
    b = SyntheticProvider(seed=2, asof=ASOF).spot_history("EURUSD", date(2025, 1, 1),
                                                          date(2026, 1, 1))
    assert not np.allclose(a["close"].to_numpy(), b["close"].to_numpy())


_PROBE = textwrap.dedent("""
    from datetime import date, datetime, timezone
    from fxgamma.data.synthetic import SyntheticProvider
    asof = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    p = SyntheticProvider(seed=20260101, asof=asof)
    q = p.smile_quotes("EURUSD", asof)[3]
    h = p.spot_history("EURUSD", date(2026, 8, 1), date(2026, 9, 1))
    oi = p.open_interest("USDJPY", asof)
    print(repr((q.atm, q.rr25, q.bf25,
                round(float(h["high"].iloc[-1]), 12),
                float(oi["oi"].iloc[0]), float(oi["strike"].iloc[0]))))
""")


def _probe(hashseed: str) -> str:
    import os
    env = dict(os.environ, PYTHONPATH=REPO, PYTHONHASHSEED=hashseed)
    out = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True,
                         text=True, env=env, cwd=REPO, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip()


@pytest.mark.slow
def test_synthetic_market_is_reproducible_across_processes():
    """The same seed must give the same market in a *different process*.

    ``SyntheticProvider`` seeds several of its generators with
    ``self.seed ^ abs(hash(pair))``.  ``hash`` on a ``str`` is salted per process
    (PEP 456), so the smile wiggle, the intraday range and the whole open-interest
    table change on every run unless ``PYTHONHASHSEED`` happens to be pinned.
    That breaks reproducible backtests, cache/live comparison, and the trader's
    first trust check ("does yesterday still say what it said yesterday?").
    """
    assert _probe("0") == _probe("12345") == _probe("98765")


# --------------------------------------------------------------------------- #
# provider factory
# --------------------------------------------------------------------------- #
def test_get_provider_returns_the_synthetic_provider_by_name():
    from fxgamma.data.synthetic import SyntheticProvider
    p = get_provider("synthetic")
    assert isinstance(p, SyntheticProvider)
    assert p.name == "synthetic" and p.kind == "synthetic"


def test_get_provider_rejects_an_unknown_name():
    with pytest.raises(ValueError):
        get_provider("bloomberg")


def test_provider_status_is_reportable(provider):
    st = provider.status()
    assert st and all(hasattr(s, "ok") and hasattr(s, "name") for s in st)


# --------------------------------------------------------------------------- #
# fixtures on disk
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["yahoo_chart_eurusd.json", "yahoo_options_fxe.json",
                                  "yahoo_chart_error.json"])
def test_shipped_fixtures_are_valid_json(name):
    """The offline fixtures are what the adapter parsers are tested against."""
    import json
    import pathlib
    p = pathlib.Path(REPO) / "fxgamma" / "data" / "fixtures" / name
    assert p.exists(), f"missing fixture {name}"
    assert isinstance(json.loads(p.read_text()), (dict, list))
