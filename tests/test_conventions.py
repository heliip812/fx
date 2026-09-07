"""Market-convention tests: cuts, DST, day count, pips, tenors, pair statics.

The expiry clock is the quietest way to be wrong.  ``T`` feeds every Greek, so a
one-hour error in the cut is a real, if small, error in every number on the screen,
and a *day* error (which is what a DST slip becomes if a cut is stored as a fixed
UTC time) is a large one on a short-dated gamma book.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from fxgamma import conventions as cv
from fxgamma.types import PairSpec


# --------------------------------------------------------------------------- #
# pair statics
# --------------------------------------------------------------------------- #
@pytest.mark.contract
@pytest.mark.parametrize("pair", sorted(cv.PAIRS))
def test_pair_spec_is_internally_consistent(pair):
    spec = cv.PAIRS[pair]
    assert spec.symbol == pair
    assert pair == spec.base + spec.quote, "a pair symbol is FORDOM: base then quote"
    assert spec.base in cv.CCYS and spec.quote in cv.CCYS
    assert spec.delta_convention in ("spot", "spot_pa", "fwd", "fwd_pa")
    assert spec.cut in cv.CUTS
    assert spec.pip in (1e-2, 1e-4)
    assert spec.spot_lag in (0, 1, 2)


@pytest.mark.contract
@pytest.mark.parametrize("pair", sorted(cv.PAIRS))
def test_jpy_pairs_use_two_decimal_pips(pair):
    """JPY pairs pip at 0.01, everything else at 0.0001.  A JPY pip quoted at 1e-4
    is a factor-of-100 error on every premium and every pin distance."""
    spec = cv.PAIRS[pair]
    assert cv.is_jpy_pair(pair) == ("JPY" in (spec.base, spec.quote))
    assert spec.pip == (1e-2 if cv.is_jpy_pair(pair) else 1e-4)


@pytest.mark.parametrize("pair,notional,spot,expected", [
    ("USDJPY", 1_000_000.0, 147.50, 10_000.0),     # JPY 10k per pip on USD 1mm
    ("EURJPY", 1_000_000.0, 172.00, 10_000.0),
    ("EURUSD", 1_000_000.0, 1.1650, 100.0),        # USD 100 per pip on EUR 1mm
    ("GBPUSD", 25_000_000.0, 1.3400, 2_500.0),
])
def test_pip_value_is_quote_ccy_and_independent_of_spot(pair, notional, spot, expected):
    """``pip_value`` is ``pip * notional_base`` in QUOTE ccy.

    The ``spot`` argument is accepted and deliberately unused (trader review W-16);
    this test pins that, so a future "fix" that multiplies by spot -- a silent
    factor-of-S error on every JPY pip figure -- fails here first.
    """
    assert cv.pip_value(pair, notional, spot) == pytest.approx(expected)
    assert cv.pip_value(pair, notional, spot * 2.0) == pytest.approx(expected)


def test_unknown_pair_raises_with_a_useful_message():
    with pytest.raises(KeyError) as exc:
        cv.pair_spec("XXXYYY")
    assert "XXXYYY" in str(exc.value)


def test_pair_spec_is_case_insensitive():
    assert cv.pair_spec("eurusd") is cv.PAIRS["EURUSD"]


# --------------------------------------------------------------------------- #
# tenors
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tenor,years", sorted(cv.TENORS.items()))
def test_tenor_years_matches_the_frozen_grid(tenor, years):
    assert cv.tenor_years(tenor) == years
    assert cv.tenor_years(tenor.lower()) == years


@pytest.mark.parametrize("tenor,expected", [
    ("7D", 7 / 365), ("2W", 14 / 365), ("18M", 1.5), ("5Y", 5.0), (" 3m ", 0.25),
])
def test_tenor_years_parses_broker_strings(tenor, expected):
    assert cv.tenor_years(tenor) == pytest.approx(expected)


@pytest.mark.parametrize("bad", ["", "M", "3X", "three months", "1.5"])
def test_bad_tenor_raises(bad):
    with pytest.raises((ValueError, IndexError)):
        cv.tenor_years(bad)


def test_tenor_grid_is_strictly_increasing():
    ys = list(cv.TENORS.values())
    assert ys == sorted(ys) and len(set(ys)) == len(ys)


# --------------------------------------------------------------------------- #
# cuts, UTC and DST
# --------------------------------------------------------------------------- #
@pytest.mark.contract
@pytest.mark.parametrize("cut,local_time,tz", [
    ("NY10", "10:00", "America/New_York"),
    ("TKY15", "15:00", "Asia/Tokyo"),
    ("LDN16", "16:00", "Europe/London"),
])
def test_expiry_datetime_is_the_local_cut_expressed_in_utc(cut, local_time, tz):
    d = date(2026, 6, 17)
    got = cv.expiry_datetime(d, cut)
    assert got.tzinfo is not None and got.utcoffset() == timedelta(0)
    local = got.astimezone(ZoneInfo(tz))
    assert local.date() == d
    assert local.strftime("%H:%M") == local_time


@pytest.mark.parametrize("cut,winter_utc_hour,summer_utc_hour", [
    ("NY10", 15, 14),      # EST = UTC-5, EDT = UTC-4
    ("LDN16", 16, 15),     # GMT = UTC+0, BST = UTC+1
    ("TKY15", 6, 6),       # Japan has no DST -- and must not acquire one
])
def test_cuts_follow_dst_in_their_own_timezone(cut, winter_utc_hour, summer_utc_hour):
    """The cuts are NY/Tokyo/London *local* times.  Storing them as fixed UTC hours
    is the trap: it puts every expiry an hour out for half the year."""
    assert cv.expiry_datetime(date(2026, 1, 20), cut).hour == winter_utc_hour
    assert cv.expiry_datetime(date(2026, 7, 20), cut).hour == summer_utc_hour


def test_year_fraction_absorbs_the_dst_step_without_losing_a_day():
    """Across the US spring-forward, consecutive NY10 expiries are 23h apart in UTC.

    ``T`` must fall by 23/24 of a day there -- not by a whole day (which would mean
    the cut was pinned to UTC) and not by 25 hours (sign error).
    """
    asof = datetime(2026, 2, 1, 12, 0, tzinfo=timezone.utc)
    before = cv.year_fraction(asof, date(2026, 3, 7), "NY10")     # EST
    after = cv.year_fraction(asof, date(2026, 3, 8), "NY10")      # EDT (spring forward)
    step_hours = (after - before) * 365 * 24
    assert step_hours == pytest.approx(23.0, abs=1e-6)


def test_year_fraction_absorbs_the_autumn_dst_step():
    asof = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    before = cv.year_fraction(asof, date(2026, 10, 31), "NY10")
    after = cv.year_fraction(asof, date(2026, 11, 1), "NY10")     # fall back
    assert (after - before) * 365 * 24 == pytest.approx(25.0, abs=1e-6)


def test_london_and_tokyo_cuts_step_independently():
    """Europe and the US switch on different dates; a single global DST flag is wrong.

    On 2026-03-15 London is still on GMT while New York is already on EDT.
    """
    ldn = cv.expiry_datetime(date(2026, 3, 15), "LDN16")
    ny = cv.expiry_datetime(date(2026, 3, 15), "NY10")
    assert ldn.hour == 16 and ny.hour == 14


@pytest.mark.parametrize("cut", sorted(cv.CUTS))
def test_year_fraction_is_act_365f_to_the_cut(cut):
    """Exactly N days before the cut, T is exactly N/365 (no DST crossing)."""
    expiry = date(2026, 6, 17)
    cut_utc = cv.expiry_datetime(expiry, cut)
    for days in (1, 7, 30, 90):
        asof = cut_utc - timedelta(days=days)
        assert cv.year_fraction(asof, expiry, cut) == pytest.approx(days / 365.0, rel=1e-14)


def test_year_fraction_accepts_a_naive_asof_as_utc():
    """Naive datetimes are treated as UTC rather than raising or drifting locally."""
    expiry = date(2026, 6, 17)
    naive = datetime(2026, 6, 10, 14, 0)
    aware = naive.replace(tzinfo=timezone.utc)
    assert cv.year_fraction(naive, expiry) == cv.year_fraction(aware, expiry)


def test_year_fraction_accepts_a_non_utc_aware_asof():
    expiry = date(2026, 6, 17)
    tokyo = datetime(2026, 6, 10, 23, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    assert cv.year_fraction(tokyo, expiry) == pytest.approx(
        cv.year_fraction(tokyo.astimezone(timezone.utc), expiry), rel=1e-15)


def test_year_fraction_is_monotone_in_the_expiry_date():
    asof = datetime(2026, 6, 1, tzinfo=timezone.utc)
    ts = [cv.year_fraction(asof, date(2026, 6, 1) + timedelta(days=n)) for n in range(1, 400)]
    assert all(b > a for a, b in zip(ts, ts[1:]))


# PM ruling (QA finding 3): resolved in favour of raising. A typo'd cut would shift
# every affected expiry by hours, quietly changing T, theta and the pin clock.
def test_unknown_cut_does_not_silently_become_ny10():
    """A typo'd cut ("NY1O", "TKO15") must not price at a different cut in silence."""
    with pytest.raises((KeyError, ValueError)):
        cv.expiry_datetime(date(2026, 6, 17), "NY1O")


# --------------------------------------------------------------------------- #
# pair groupings used by the UI
# --------------------------------------------------------------------------- #
def test_g3_and_g10_are_subsets_of_pairs():
    assert set(cv.G3) <= set(cv.PAIRS)
    assert set(cv.G10_PAIRS) <= set(cv.PAIRS)
    assert set(cv.G3) <= set(cv.G10_PAIRS)


def test_premium_adjusted_pairs_are_the_usd_base_and_jpy_cross_set():
    """Market convention: USD-base G10 pairs and EURJPY quote premium in the base
    ccy, hence premium-adjusted delta (trader review Q-4)."""
    pa = {p for p, s in cv.PAIRS.items() if s.delta_convention == "spot_pa"}
    assert pa == {"USDJPY", "USDCHF", "USDCAD", "USDSEK", "USDNOK", "EURJPY"}
