"""``fxgamma.data.intraday_yahoo`` (offline) and the repo-wide ``_pct`` units sweep.

Two jobs.

**1. The intraday adapter.**  It is the only source that could ever replace the
*modelled* hour-of-day variance profile with a *measured* one, and every sigma in the
overnight feature scales with that profile.  It is UNVERIFIED in this sandbox, so what
is tested is the contract around that fact: it must never silently fall back to
synthetic data, ``synthetic_intraday`` must be badged, the interval lookback limits
must be enforced rather than silently truncated, and the session machinery must
reproduce the variance-share measurement the whole feature rests on.

**2. Priority 3 -- the units hazard.**  ``DeltaCap.binds_pct`` holds **percent
(0-100)**; ``HedgeRule.band_pct`` holds a **fraction**.  Two identically-suffixed
fields with different units in one codebase is exactly how the 60x band error
happened.  Rather than assert the two in isolation, this file sweeps **every** ``_pct``
identifier in the package and pins the units of each, so the next one to drift fails
here.  The sweep found three more fraction-valued ``_pct`` names against twenty-odd
percent-valued ones; they are enumerated below and the list is asserted closed.
"""
from __future__ import annotations

import inspect
import math
import pathlib
import re

import numpy as np
import pandas as pd
import pytest

iy = pytest.importorskip("fxgamma.data.intraday_yahoo")

ROOT = pathlib.Path(__file__).resolve().parents[1] / "fxgamma"


@pytest.fixture(scope="module")
def bars():
    """730 days of hourly synthetic bars -- the shape the real adapter returns."""
    return iy.synthetic_intraday("EURUSD", days=400, interval="1h", seed=3)


# =========================================================================== #
# 1.  Offline contract -- never a silent fallback
# =========================================================================== #
class TestOfflineContract:
    def test_the_adapter_is_badged_unverified(self):
        """It has never been confirmed against the live host from this environment,
        and the module says so rather than implying it works."""
        assert iy.VERIFIED is False
        assert iy.status("EURUSD").verified is False

    def test_status_names_the_host_and_the_intervals(self):
        s = iy.status("EURUSD")
        assert "query1.finance.yahoo.com" in s.detail
        assert "1h" in s.detail

    def test_synthetic_intraday_is_explicitly_requested_not_defaulted(self):
        """Architecture s7: synthetic must never be a silent fallback.  It is a
        separate, named call."""
        src = inspect.getsource(iy.intraday_history)
        assert "synthetic_intraday" not in src

    def test_get_intraday_badges_its_source(self):
        out = iy.get_intraday("EURUSD", source="synthetic", days=60, interval="1h")
        df = out[0] if isinstance(out, tuple) else out
        assert len(df)
        if isinstance(out, tuple):
            prov = out[1]
            blob = str(getattr(prov, "source", "")) + str(getattr(prov, "kind", "")) \
                + str(prov)
            assert "synthetic" in blob.lower()

    def test_an_unknown_interval_is_rejected(self):
        with pytest.raises(ValueError):
            iy._check_interval("3h")

    @pytest.mark.parametrize("interval", list(iy.INTERVALS))
    def test_every_interval_has_a_lookback_limit_and_a_bar_rate(self, interval):
        assert interval in iy.MAX_LOOKBACK_DAYS
        assert interval in iy.BARS_PER_HOUR
        assert iy.MAX_LOOKBACK_DAYS[interval] > 0
        assert iy.BARS_PER_HOUR[interval] > 0

    def test_the_lookback_limits_are_ordered_by_granularity(self):
        """Finer bars, shorter history -- and the module encodes the limits rather
        than letting a too-long request come back silently truncated."""
        d = iy.MAX_LOOKBACK_DAYS
        assert d["1h"] > d["30m"] >= d["15m"] >= d["5m"] > d["1m"]

    def test_bars_per_hour_is_consistent_with_the_interval(self):
        assert iy.BARS_PER_HOUR["1h"] == 1.0
        assert iy.BARS_PER_HOUR["30m"] == 2.0
        assert iy.BARS_PER_HOUR["5m"] == 12.0

    def test_empty_frame_has_the_standard_columns(self):
        e = iy.empty_intraday_frame()
        assert list(e.columns) == list(iy.INTRADAY_COLUMNS)
        assert len(e) == 0

    def test_sample_payload_parses_back_to_bars(self):
        df = iy.parse_intraday(iy.sample_payload(rows=40, seed=2))
        assert len(df) > 0
        for c in ("open", "high", "low", "close"):
            assert c in df.columns

    def test_sample_payload_json_round_trips(self):
        import json
        assert isinstance(json.loads(iy.sample_payload_json(rows=30)), dict)

    @pytest.mark.regression
    def test_sample_payload_works_for_a_small_row_count(self):
        """FINDING (QA-8, cosmetic).  ``sample_payload(rows=n)`` raises
        ``IndexError: list assignment index out of range`` for every ``n < 8``.  It is
        the fixture generator QA is pointed at, so it should not have a lower bound
        nobody documented."""
        for n in (2, 3, 6):
            p = iy.sample_payload(rows=n, seed=2)
            assert len(iy.parse_intraday(p)) >= 1, n

    def test_parse_of_a_junk_payload_raises_rather_than_inventing_bars(self):
        """Architecture s7: an empty or malformed response must NOT come back as a
        plausible-looking empty frame that a caller then treats as 'no move'."""
        with pytest.raises(Exception):
            iy.parse_intraday({"chart": {"result": None}})
        with pytest.raises(Exception):
            iy.parse_intraday({})

    def test_invert_flips_the_quote(self):
        p = iy.sample_payload(rows=20, seed=4)
        a = iy.parse_intraday(p)
        b = iy.parse_intraday(p, invert=True)
        assert len(a) == len(b)
        assert float(b["close"].iloc[0]) == pytest.approx(1.0 / float(a["close"].iloc[0]),
                                                          rel=1e-9)

    def test_inverting_swaps_high_and_low(self):
        p = iy.sample_payload(rows=20, seed=5)
        b = iy.parse_intraday(p, invert=True)
        assert (b["high"] >= b["low"]).all()


# =========================================================================== #
# 2.  Bar hygiene -- OHLC coherence and gap detection
# =========================================================================== #
class TestBarHygiene:
    def test_ohlc_is_coherent(self, bars):
        assert (bars["high"] >= bars["low"]).all()
        assert (bars["high"] >= bars[["open", "close"]].max(axis=1) - 1e-12).all()
        assert (bars["low"] <= bars[["open", "close"]].min(axis=1) + 1e-12).all()

    def test_prices_are_positive_and_finite(self, bars):
        assert np.isfinite(bars.to_numpy(float)).all()
        assert (bars.to_numpy(float) > 0).all()

    def test_the_index_is_utc_and_strictly_increasing(self, bars):
        assert bars.index.tz is not None
        assert bars.index.is_monotonic_increasing
        assert not bars.index.duplicated().any()

    def test_gaps_are_reported_not_hidden(self, bars):
        g = iy.bar_gaps(bars, interval="1h")
        assert isinstance(g, pd.DataFrame)
        assert len(g) > 0, "a 24/5 series must show weekend gaps"

    def test_a_continuous_series_has_no_gaps(self):
        idx = pd.date_range("2026-01-05", periods=48, freq="h", tz="UTC")
        c = np.linspace(1.16, 1.17, 48)
        df = pd.DataFrame({"open": c, "high": c, "low": c, "close": c}, index=idx)
        assert len(iy.bar_gaps(df, interval="1h")) == 0

    def test_no_bars_inside_the_weekend_close(self, bars):
        """FX closes ~21:00 UTC Friday and reopens ~21:00 UTC Sunday.  A bar in that
        window would corrupt the hour-of-day profile."""
        dow = bars.index.dayofweek
        hour = bars.index.hour
        inside = ((dow == 5) | ((dow == 4) & (hour > 21)) | ((dow == 6) & (hour < 21)))
        assert inside.mean() < 0.02

    def test_session_returns_are_log_returns(self, bars):
        r = iy.session_returns(bars, interval="1h")
        assert len(r) > 0
        assert abs(float(np.asarray(r["ret"] if "ret" in getattr(r, "columns", [])
                                    else r).mean())) < 0.001


# =========================================================================== #
# 3.  The session machinery behind the 0.382 variance share
# =========================================================================== #
@pytest.fixture(scope="module")
def sessions(bars):
    return iy.overnight_sessions(bars)


class TestOvernightSessions:
    def test_sessions_are_found(self, sessions):
        assert len(sessions) > 150

    def test_each_session_is_about_fourteen_hours(self, sessions):
        lens = [len(s) for s in sessions[:50]]
        assert 10 <= float(np.median(lens)) <= 16

    def test_session_stats_columns(self, bars, sessions):
        st = iy.session_stats(sessions, "EURUSD")
        for c in ("qv", "d2", "vr", "er", "sigma", "range_pips", "net_pips"):
            assert c in st.columns

    def test_variance_ratio_of_a_synthetic_night_averages_about_one(self, bars,
                                                                    sessions):
        """The null: the synthetic generator is driftless, so the pooled variance
        ratio over many nights must sit near 1.  A VR that scaled with the number of
        bars -- the control bug docs/12 caught -- would read ~13 here."""
        st = iy.session_stats(sessions, "EURUSD")
        pooled = float(st["d2"].sum() / st["qv"].sum())
        assert 0.6 < pooled < 1.6, f"pooled VR {pooled:.2f}"

    def test_the_efficiency_ratio_is_between_zero_and_one(self, bars, sessions):
        st = iy.session_stats(sessions, "EURUSD")
        er = st["er"].dropna()
        assert (er >= 0).all() and (er <= 1.0 + 1e-9).all()

    def test_range_is_at_least_the_absolute_net_move(self, bars, sessions):
        st = iy.session_stats(sessions, "EURUSD")
        assert (st["range_pips"] >= st["net_pips"].abs() - 1e-6).all()

    def test_estimating_the_profile_reproduces_a_plausible_variance_share(self, bars):
        """The whole overnight feature scales with this number.  The shipped modelled
        default is 0.382; an estimate off synthetic bars must land in the same
        neighbourhood or the two halves of the feature disagree."""
        ov = pytest.importorskip("fxgamma.portfolio.overnight")
        prof = ov.estimate_hour_profile(bars, "EURUSD")
        assert prof.source == "estimated"
        assert prof.n_obs > 0
        import datetime as dtm
        w = ov.passive_window(dtm.datetime(2026, 9, 7, 12, 0, tzinfo=dtm.timezone.utc),
                              pair="EURUSD", profile=prof)
        assert 0.25 < w.var_fraction < 0.60
        assert w.var_fraction < w.clock_fraction, \
            "even measured, the night must be theta-expensive"

    def test_an_estimated_profile_still_sums_to_24(self, bars):
        ov = pytest.importorskip("fxgamma.portfolio.overnight")
        a = np.asarray(ov.estimate_hour_profile(bars, "EURUSD").array(), float)
        assert a.sum() == pytest.approx(24.0, rel=1e-9)
        assert (a > 0).all()

    def test_too_little_data_refuses_rather_than_guessing(self):
        """A 24-hour profile fitted off three days is noise wearing a label.  The
        module must refuse and say to badge the default -- not return a confident
        shape that every overnight sigma then scales with."""
        ov = pytest.importorskip("fxgamma.portfolio.overnight")
        tiny = iy.synthetic_intraday("EURUSD", days=3, interval="1h", seed=9)
        with pytest.raises(ValueError, match="too few"):
            ov.estimate_hour_profile(tiny, "EURUSD")

    def test_synthetic_intraday_is_deterministic(self):
        """The seventh finding of pass one was a 'deterministic' provider that was
        not.  Same seed, same bars -- and across processes, not just calls."""
        a = iy.synthetic_intraday("EURUSD", days=30, interval="1h", seed=7)
        b = iy.synthetic_intraday("EURUSD", days=30, interval="1h", seed=7)
        assert a.equals(b)

    def test_different_seeds_give_different_bars(self):
        a = iy.synthetic_intraday("EURUSD", days=30, interval="1h", seed=7)
        b = iy.synthetic_intraday("EURUSD", days=30, interval="1h", seed=8)
        assert not a.equals(b)

    @pytest.mark.parametrize("pair", ["EURUSD", "USDJPY", "GBPUSD"])
    def test_synthetic_anchors_are_plausible_for_the_pair(self, pair):
        df = iy.synthetic_intraday(pair, days=30, interval="1h", seed=3)
        lo, hi = (80.0, 300.0) if pair == "USDJPY" else (0.5, 3.0)
        assert lo < float(df["close"].mean()) < hi

    @pytest.mark.parametrize("interval", ["1h", "30m", "15m"])
    def test_synthetic_respects_the_requested_interval(self, interval):
        df = iy.synthetic_intraday("EURUSD", days=20, interval=interval, seed=3)
        assert len(df) > 0
        gaps = pd.Series(df.index).diff().dropna()
        expected = pd.Timedelta(hours=1.0 / iy.BARS_PER_HOUR[interval])
        assert gaps.mode().iloc[0] == expected


# =========================================================================== #
# 4.  PRIORITY 3 -- the repo-wide ``_pct`` units sweep
# =========================================================================== #
#: Every ``_pct`` identifier in ``fxgamma/``, classified.  "percent" means 0-100,
#: "fraction" means 0-1.  The hazard is that both conventions live under the same
#: suffix, so the classification is asserted rather than assumed, and the sweep below
#: fails on any NEW ``_pct`` name that is not in this table.
PCT_PERCENT = {
    "binds_pct",            # DeltaCap: 99.2 means 99.2% of nights
    "lo_pct", "hi_pct",     # risk.spot_ladder bounds, -8.0 = 8% below spot
    "span_pct",
    "edge_pct", "dist_pct", "distance_pct",
    "sd_reduction_pct", "gamma_pnl_pct", "breakeven_pct",
    "sigma_day_pct", "realized_sigma_day_pct",
    "move_pct", "spot_pct", "spot_shock_pct",
    "merge_pct", "be_pct", "rate_pct",
    "utility_giveup_pct", "share_of_day_pct",
    "sell_pct", "buy_pct", "cost_pct", "current_pct",
    "sigma_day_distance_pct", "sigma_overnight_pct",   # fxgamma/reference.py
    "breakeven_pct_econ",
}
#: The exceptions.  These are the ones that bite.
PCT_FRACTION = {
    "band_pct",             # HedgeRule: 0.25 = 25% of gross notional (v1.6 CR-1)
    "delta_pct",            # delta per 1 unit notional: 0.5 for an ATM call
    "step_sd_pct",          # ratchet.ar1_price_paths: 0.0007 = 0.07% per bar
}


def _all_pct_names() -> set[str]:
    names: set[str] = set()
    pat = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*_pct)\b")
    for f in ROOT.rglob("*.py"):
        names |= set(pat.findall(f.read_text(encoding="utf-8", errors="ignore")))
    return {n for n in names if n != "_pct"}


class TestUnitsSweep:
    @pytest.mark.regression
    def test_no_new_pct_name_has_appeared_unclassified(self):
        """Two identically-suffixed fields with different units in one codebase is how
        the 60x band error happened.  A new ``_pct`` must be classified here before it
        can ship."""
        unknown = _all_pct_names() - PCT_PERCENT - PCT_FRACTION
        assert not unknown, (
            f"unclassified _pct identifiers: {sorted(unknown)} -- state the units in "
            "tests/test_intraday_and_units.py before shipping them")

    def test_the_two_tables_are_disjoint(self):
        assert not (PCT_PERCENT & PCT_FRACTION)

    def test_delta_cap_binds_pct_is_percent(self, ref_book, on_mkt):
        dc = pytest.importorskip("fxgamma.portfolio.deltacap")
        c = dc.recommend_cap(ref_book, on_mkt, "EURUSD")
        assert 0.0 <= c.binds_pct <= 100.0
        assert c.binds_pct > 1.0

    def test_hedge_rule_band_pct_is_a_fraction(self):
        from fxgamma.types import HedgeRule
        assert 0.0 < HedgeRule().band_pct < 1.0

    @pytest.mark.regression
    def test_the_two_are_not_interchangeable(self, ref_book, on_mkt):
        """If someone ever formats ``binds_pct`` with ``:.0%`` or multiplies
        ``band_pct`` by 100 a second time, this is the 60x error again."""
        dc = pytest.importorskip("fxgamma.portfolio.deltacap")
        from fxgamma.types import HedgeRule
        c = dc.recommend_cap(ref_book, on_mkt, "EURUSD")
        assert c.binds_pct / max(HedgeRule().band_pct, 1e-12) > 50.0

    def test_band_pct_is_applied_as_a_fraction_of_gross(self, ref_book, on_mkt):
        """v1.6 CR-1, as arithmetic rather than as a comment."""
        zones = pytest.importorskip("fxgamma.portfolio.zones")
        from fxgamma.types import HedgeRule
        bo = pytest.importorskip("fxgamma.portfolio.bandopt")
        gross = bo.book_gamma(ref_book, on_mkt, "EURUSD").gross_notional
        rule = HedgeRule(mode="band", band_pct=0.20)
        row = zones.hedge_bands(ref_book, on_mkt, "EURUSD", rule=rule).iloc[0]
        assert float(row["band_base"]) == pytest.approx(0.20 * gross, rel=1e-9)

    def test_default_band_pct_constant_is_a_percent(self):
        """``zones.DEFAULT_BAND_PCT = 15.0`` is a PERCENT and is divided by 100 at
        the point of use, while ``HedgeRule.band_pct`` is already a fraction.  Same
        suffix, same concept, 100x apart -- the hazard, pinned."""
        zones = pytest.importorskip("fxgamma.portfolio.zones")
        assert zones.DEFAULT_BAND_PCT > 1.0
        import inspect as _i
        assert "DEFAULT_BAND_PCT / 100.0" in _i.getsource(zones)

    def test_delta_pct_is_a_fraction_not_a_percent(self):
        """``Greeks.delta_pct`` is delta per 1 unit of notional: 0.5 for an ATM call,
        not 50.  Another ``_pct`` that is not a percent."""
        from fxgamma.models import gk
        g = gk.gk_greeks(1.165, 1.165, 1 / 12, 0.04, 0.02, 0.08, +1,
                         notional_base=1e6, direction=+1)
        assert 0.0 < g.delta_pct < 1.0

    def test_step_sd_pct_is_a_fraction(self):
        """``ratchet.ar1_price_paths(step_sd_pct=0.0007)`` is 0.07%, a fraction."""
        rt = pytest.importorskip("fxgamma.portfolio.ratchet")
        sig = inspect.signature(rt.ar1_price_paths)
        assert 0.0 < sig.parameters["step_sd_pct"].default < 0.01

    def test_sigma_day_pct_is_a_percent(self):
        zones = pytest.importorskip("fxgamma.portfolio.zones")
        got = zones.sigma_day_pct(0.08)
        assert got == pytest.approx(100.0 * 0.08 / math.sqrt(252.0), rel=1e-12)
        assert got > 0.1

    def test_lo_hi_pct_are_percent_moves(self, ref_book, on_mkt):
        risk = pytest.importorskip("fxgamma.portfolio.risk")
        lad = risk.spot_ladder(ref_book, on_mkt, "EURUSD", lo_pct=-5.0, hi_pct=5.0,
                               n=21, sticky="strike")
        S = on_mkt.spot["EURUSD"]
        assert float(lad["spot"].min()) == pytest.approx(S * 0.95, rel=1e-6)
        assert float(lad["spot"].max()) == pytest.approx(S * 1.05, rel=1e-6)

    def test_sd_reduction_pct_is_a_percent(self, ref_book, on_mkt):
        ov = pytest.importorskip("fxgamma.portfolio.overnight")
        r = ov.overnight_ladder(ref_book, on_mkt, "EURUSD")
        s = ov.ladder_summary(r, ref_book, on_mkt, "EURUSD")
        assert 1.0 < s["sd_reduction_pct"] <= 100.0

    def test_gamma_pnl_pct_takes_a_percent_move(self):
        risk = pytest.importorskip("fxgamma.portfolio.risk")
        a = risk.gamma_pnl_pct(1e6, 1.165, 1.0)
        b = risk.gamma_pnl_pct(1e6, 1.165, 2.0)
        assert b / a == pytest.approx(4.0, rel=1e-12)
        assert a == pytest.approx(0.005 * 1e6 * 1.165, rel=1e-12)

    def test_binds_pct_is_formatted_as_a_percent_not_a_fraction(self):
        """The field's own documentation says 'format with :.0f}% and never :.0%'.
        A ``:.0%`` on a 0-100 value prints 9,920%."""
        dc = pytest.importorskip("fxgamma.portfolio.deltacap")
        src = inspect.getsource(dc)
        assert "binds_pct:.0%" not in src
        assert "binds_pct:.1%" not in src

    def test_every_percent_named_field_the_ladder_emits_is_a_percent(self, ref_book,
                                                                      on_mkt):
        ov = pytest.importorskip("fxgamma.portfolio.overnight")
        r = ov.overnight_ladder(ref_book, on_mkt, "EURUSD")
        s = ov.ladder_summary(r, ref_book, on_mkt, "EURUSD")
        for k, v in s.items():
            if k.endswith("_pct") and isinstance(v, float) and np.isfinite(v):
                assert k in PCT_PERCENT or k in PCT_FRACTION, k
                if k in PCT_PERCENT and v != 0.0:
                    assert abs(v) > 0.01, f"{k}={v} looks like a fraction"


# =========================================================================== #
# 5.  fxgamma/reference.py -- the single source the documents cite
# =========================================================================== #
@pytest.fixture(scope="module")
def fig():
    ref = pytest.importorskip("fxgamma.reference")
    return ref.reference_figures()


class TestCanonicalReference:
    """The fourth duplication-drift bug on this project was four documents each
    inventing their own reference book, one of which was internally impossible
    (Gamma_1pct 3.91mm paired with theta -3,978/day: those two are not independent).
    The fix was a single importable source, so it gets pinned here -- including the
    percent-vs-fraction slip its own comment records as the FIFTH on this project."""

    def test_the_reference_book_is_eurusd_1m_atm_10mm_a_leg(self):
        ref = pytest.importorskip("fxgamma.reference")
        r = ref.REFERENCE
        assert r.pair == "EURUSD" and r.notional_per_leg == 10e6
        assert r.tenor_years == pytest.approx(1 / 12, rel=1e-12)

    def test_the_variance_share_is_the_measured_0382(self):
        ref = pytest.importorskip("fxgamma.reference")
        assert ref.REFERENCE.var_fraction == pytest.approx(0.382, abs=0.001)
        assert ref.REFERENCE.clock_fraction == pytest.approx(14 / 24, rel=1e-12)

    def test_the_theta_variance_ratio_is_153(self, fig):
        assert fig["theta_variance_ratio"] == pytest.approx(1.527, abs=0.01)

    def test_gamma_and_theta_are_mutually_consistent(self, fig):
        """The bug this module exists to prevent: docs/12 s7 paired
        ``Gamma_1pct = 3.91mm`` with ``theta = -3,978/day``, and those two are not
        independent.  For an ATM straddle ``theta = -0.5 Gamma S^2 sigma^2 / 365``
        with ``Gamma_1pct = Gamma S / 100``, so
        ``theta = -50 Gamma_1pct S sigma^2 / 365``.  Note the ``S`` -- dropping it is
        the same percent-vs-fraction family of slip this module records."""
        ref = pytest.importorskip("fxgamma.reference")
        r = ref.REFERENCE
        implied = -50.0 * fig["gamma_1pct"] * r.spot * r.vol ** 2 / 365.0
        assert fig["theta_day"] == pytest.approx(implied, rel=0.02)

    def test_the_impossible_pairing_from_docs12_is_rejected(self):
        """The actual bad cell: Gamma_1pct 3.91mm with theta -3,978/day implies two
        different vols (about 7.05 and about 9.0).  It must not reproduce."""
        ref = pytest.importorskip("fxgamma.reference")
        fig = ref.reference_figures()
        r = ref.REFERENCE
        # Gamma_1pct goes as 1/sigma, so the claimed 3.91mm pins the vol...
        vol_implied = r.vol * fig["gamma_1pct"] / 3.91e6
        assert vol_implied == pytest.approx(0.0705, abs=0.002)
        # ...and at THAT vol the theta is nowhere near the claimed -3,978.
        theta_at_that_vol = -50.0 * 3.91e6 * r.spot * vol_implied ** 2 / 365.0
        assert abs(theta_at_that_vol) == pytest.approx(3083.0, rel=0.06)
        assert abs(abs(theta_at_that_vol) - 3978.0) > 500.0

    def test_the_delta_cap_is_054mm(self, fig):
        assert fig["delta_cap_base"] / 1e6 == pytest.approx(0.54, abs=0.02)

    @pytest.mark.regression
    def test_the_ladder_band_is_18_pips_not_1805(self, fig):
        """The fifth percent-vs-fraction slip on this project, as an assertion:
        ``gamma_1pct`` is delta per +1 PERCENT, so ``cap / gamma_1pct`` is a move in
        percent and must be divided by 100 before it multiplies spot.  Getting it
        wrong returns 1,805 pips instead of 18."""
        assert fig["ladder_band_pips"] == pytest.approx(18.0, abs=0.6)
        assert fig["ladder_band_pips"] < 100.0

    def test_the_cap_is_half_gamma1pct_times_the_overnight_sigma(self, fig):
        assert fig["delta_cap_base"] == pytest.approx(
            0.5 * fig["gamma_1pct"] * fig["sigma_overnight_pct"], rel=1e-9)

    def test_the_two_clocks_are_both_present_and_differ_by_1203(self, fig):
        """W-7: sqrt(365) for ECONOMICS, sqrt(252) for DISTANCE."""
        assert fig["sigma_day_distance_pct"] / fig["breakeven_pct_econ"] == \
            pytest.approx(math.sqrt(365.0 / 252.0), rel=1e-9)
        assert fig["sigma_day_distance_pct"] / fig["breakeven_pct_econ"] == \
            pytest.approx(1.2033, abs=0.001)

    def test_the_overnight_sigma_is_the_distance_clock_scaled_by_variance_share(
            self, fig):
        ref = pytest.importorskip("fxgamma.reference")
        assert fig["sigma_overnight_pct"] == pytest.approx(
            fig["sigma_day_distance_pct"] * math.sqrt(ref.REFERENCE.var_fraction),
            rel=1e-9)

    def test_theta_window_is_pro_rata(self, fig):
        ref = pytest.importorskip("fxgamma.reference")
        assert fig["theta_window"] == pytest.approx(
            fig["theta_day"] * ref.REFERENCE.clock_fraction, rel=1e-9)

    def test_the_withdrawn_2870_theta_is_not_reproduced(self, fig):
        """The PM's withdrawn worked example charged USD 2,870 -- a full day's theta
        against a 14-hour window -- where the correct figure was USD 1,673."""
        assert abs(fig["theta_window"]) < abs(fig["theta_day"])

    def test_cost_tiers_bracket_the_no_otc_range(self):
        ref = pytest.importorskip("fxgamma.reference")
        t = ref.COST_TIERS
        assert t["retail_default"] == 5.0
        assert t["retail_low"] < t["retail_default"] < t["retail_high"]
        assert t["retail_default"] / t["interbank_eurusd"] > 15.0

    def test_the_canonical_cap_agrees_with_the_live_recommendation(self, fig, ref_book,
                                                                    on_mkt):
        """The whole point of a single source: the module and the document must not
        drift.  ``reference.py`` and ``deltacap.recommend_cap`` are independent
        derivations on the same book and must land within a couple of percent."""
        dc = pytest.importorskip("fxgamma.portfolio.deltacap")
        live = dc.recommend_cap(ref_book, on_mkt, "EURUSD")
        assert live.cap_base == pytest.approx(fig["delta_cap_base"], rel=0.05)
        assert live.band_pips == pytest.approx(fig["ladder_band_pips"], rel=0.05)

    def test_the_canonical_ratio_agrees_with_the_live_window(self, fig):
        ov = pytest.importorskip("fxgamma.portfolio.overnight")
        import datetime as dtm
        w = ov.passive_window(dtm.datetime(2026, 9, 7, 12, 0, tzinfo=dtm.timezone.utc),
                              pair="EURUSD")
        assert w.clock_fraction / w.var_fraction == pytest.approx(
            fig["theta_variance_ratio"], rel=0.01)
