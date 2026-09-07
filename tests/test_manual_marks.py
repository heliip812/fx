"""``fxgamma/data/manual.py`` -- the desk's own marks.  **The primary mark path.**

Amendment v1.2 T-1 promoted the manual grid from a buried override to the *first*
input the book is priced off: ``manual -> live -> cache -> synthetic``.  Everything
downstream -- every Greek, every theta bill, every hedge -- is computed off whatever
this module decides the vol is.  A parser that reads ``7.05`` as ``0.25`` does not
produce an error; it produces a book marked at the wrong vol that looks completely
normal on screen.  So the parser is tested harder here than anything else in the suite.

The rule that organises these tests: **a wrong mark must be impossible, a refused
mark is acceptable, and a silent one is a defect.**  Every inference the parser makes
has to appear in ``ParsedGrid.inferred`` or ``ParsedGrid.warnings``, because the UI
contract (module docstring, arch section 7) is "show the user what I understood before
you save".
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from fxgamma import conventions as cv
from fxgamma.data.manual import (GRID_TENORS, MARK_COLUMNS, SCHEMA, ManualMark,
                                 ManualQuoteProvider, ManualQuoteStore, ParsedGrid,
                                 parse_grid)
from fxgamma.models.surface import SmileQuotes
from fxgamma.types import Provenance

pytestmark = pytest.mark.contract


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def store(tmp_path):
    """An empty store on a throwaway path -- never the repo's real marks file."""
    return ManualQuoteStore(tmp_path / "marks.json", strict=True)


@pytest.fixture()
def marked_store(store):
    store.set_mark("EURUSD", "1M", 0.0705, -0.0025, 0.0018, save=False)
    store.set_mark("EURUSD", "3M", 0.0740, -0.0035, 0.0022, save=True)
    return store


# --------------------------------------------------------------------------- #
# ManualMark -- the one row
# --------------------------------------------------------------------------- #
class TestManualMark:
    def test_vols_are_decimals_and_reach_models_as_the_frozen_contract_type(self):
        """``to_smile_quotes`` is the only thing that crosses to ``models`` (section 8)."""
        m = ManualMark("eurusd", "1m", 0.0705, -0.0025, 0.0018)
        assert m.pair == "EURUSD" and m.tenor == "1M"      # normalised on construction
        q = m.to_smile_quotes()
        assert isinstance(q, SmileQuotes)
        assert (q.atm, q.rr25, q.bf25) == (0.0705, -0.0025, 0.0018)
        assert q.T == pytest.approx(cv.tenor_years("1M"))
        assert q.tenor == "1M"

    @pytest.mark.parametrize("bad", [0.0, -0.05, float("nan"), float("inf")])
    def test_a_non_positive_or_non_finite_atm_is_refused_at_construction(self, bad):
        """There is no such thing as a zero-vol mark; storing one prices the book flat."""
        with pytest.raises(ValueError, match="ATM"):
            ManualMark("EURUSD", "1M", bad)

    @pytest.mark.parametrize("bad", ["", "1X", "banana", "25", "d"])
    def test_an_unreadable_tenor_is_refused_rather_than_defaulted(self, bad):
        """Same discipline as Q-3 on cuts: a silently defaulted tenor moves T."""
        with pytest.raises(ValueError, match="tenor"):
            ManualMark("EURUSD", bad, 0.07)

    @pytest.mark.parametrize("spelling,want", [("o/n", "ON"), ("ON", "ON"), ("1mo", "1M"),
                                               ("12M", "1Y"), ("1yr", "1Y"), (" 3m ", "3M")])
    def test_tenor_spellings_a_broker_actually_uses_normalise(self, spelling, want):
        assert ManualMark("EURUSD", spelling, 0.07).tenor == want

    def test_round_trips_through_its_own_dict_form(self):
        m = ManualMark("USDJPY", "6M", 0.0925, -0.0125, 0.0030, rr10=-0.024, bf10=0.009,
                       source="paste", note="broker run")
        back = ManualMark.from_dict(m.to_dict())
        assert back.to_dict() == m.to_dict()
        assert back.asof == m.asof                      # tz-aware, not re-stamped

    def test_naive_asof_is_read_as_utc(self):
        m = ManualMark("EURUSD", "1M", 0.07, asof=datetime(2026, 9, 7, 6, 30))
        assert m.asof.tzinfo is timezone.utc

    def test_age_hours_is_what_the_staleness_badge_reads(self):
        old = datetime.now(timezone.utc) - timedelta(hours=30)
        assert ManualMark("EURUSD", "1M", 0.07, asof=old).age_hours() == pytest.approx(30, abs=0.1)


# --------------------------------------------------------------------------- #
# the paste parser -- separators, units, layouts
# --------------------------------------------------------------------------- #
#: the same 3-tenor EURUSD grid written the way six different sources spell it
_EXPECT = {"1W": (0.0790, -0.0020, 0.0015),
           "1M": (0.0705, -0.0025, 0.0018),
           "3M": (0.0740, -0.0035, 0.0022)}

_SPELLINGS = {
    "tabs_percent": "1W\t7.90\t-0.20\t0.15\n1M\t7.05\t-0.25\t0.18\n3M\t7.40\t-0.35\t0.22",
    "commas_percent": "1W,7.90,-0.20,0.15\n1M,7.05,-0.25,0.18\n3M,7.40,-0.35,0.22",
    "spaces_percent": "1W   7.90  -0.20   0.15\n1M   7.05  -0.25   0.18\n3M  7.40 -0.35 0.22",
    "semicolons": "1W;7.90;-0.20;0.15\n1M;7.05;-0.25;0.18\n3M;7.40;-0.35;0.22",
    "pipes": "1W|7.90|-0.20|0.15\n1M|7.05|-0.25|0.18\n3M|7.40|-0.35|0.22",
    "percent_signs": ("1W\t7.90%\t-0.20%\t0.15%\n1M\t7.05%\t-0.25%\t0.18%\n"
                      "3M\t7.40%\t-0.35%\t0.22%"),
    "decimals": ("1W\t0.0790\t-0.0020\t0.0015\n1M\t0.0705\t-0.0025\t0.0018\n"
                 "3M\t0.0740\t-0.0035\t0.0022"),
    "header_row": ("Tenor\tATM\t25d RR\t25d BF\n1W\t7.90\t-0.20\t0.15\n"
                   "1M\t7.05\t-0.25\t0.18\n3M\t7.40\t-0.35\t0.22"),
    "header_aliases": ("Term,Vol,RR25,Fly\n1W,7.90,-0.20,0.15\n1M,7.05,-0.25,0.18\n"
                       "3M,7.40,-0.35,0.22"),
    "accounting_negatives": ("1W\t7.90\t(0.20)\t0.15\n1M\t7.05\t(0.25)\t0.18\n"
                             "3M\t7.40\t(0.35)\t0.22"),
    "unicode_minus": "1W\t7.90\t−0.20\t0.15\n1M\t7.05\t−0.25\t0.18\n3M\t7.40\t−0.35\t0.22",
    "transposed": ("Tenor\t1W\t1M\t3M\nATM\t7.90\t7.05\t7.40\n"
                   "25d RR\t-0.20\t-0.25\t-0.35\n25d BF\t0.15\t0.18\t0.22"),
    "transposed_no_corner": ("1W\t1M\t3M\nATM\t7.90\t7.05\t7.40\n"
                             "RR\t-0.20\t-0.25\t-0.35\nBF\t0.15\t0.18\t0.22"),
    "blank_and_comment_lines": ("# EURUSD broker run 07:05\n\n1W\t7.90\t-0.20\t0.15\n\n"
                                "1M\t7.05\t-0.25\t0.18\n3M\t7.40\t-0.35\t0.22\n\n"),
}


class TestPasteParser:
    @pytest.mark.parametrize("name", sorted(_SPELLINGS))
    def test_every_spelling_of_the_same_grid_yields_the_same_marks(self, name):
        """One grid, twelve clipboards.  All must land on identical decimal vols.

        This is the test that matters: the trader has 30 seconds and pastes whatever
        the broker sent.  Tab, comma, ``%``, parenthesised negatives, a transposed
        grid -- none of them may change the number the book is priced off.
        """
        res = parse_grid(_SPELLINGS[name], "EURUSD")
        assert res.ok, res.summary()
        got = {m.tenor: (m.atm, m.rr25, m.bf25) for m in res.marks}
        assert set(got) == set(_EXPECT), res.summary()
        for t, (atm, rr, bf) in _EXPECT.items():
            assert got[t][0] == pytest.approx(atm, abs=1e-12), f"{name} {t} ATM"
            assert got[t][1] == pytest.approx(rr, abs=1e-12), f"{name} {t} RR"
            assert got[t][2] == pytest.approx(bf, abs=1e-12), f"{name} {t} BF"

    def test_marks_come_back_sorted_by_expiry(self):
        res = parse_grid("3M\t7.40\t-0.35\t0.22\n1W\t7.90\t-0.20\t0.15\n"
                         "1M\t7.05\t-0.25\t0.18", "EURUSD")
        assert [m.tenor for m in res.marks] == ["1W", "1M", "3M"]
        assert [m.T for m in res.marks] == sorted(m.T for m in res.marks)

    def test_bid_ask_becomes_the_mid(self):
        """A broker quotes ``7.05/7.25``; marking the book on the offer is a real cost."""
        res = parse_grid("1M\t7.05/7.25\t-0.25\t0.18", "EURUSD")
        assert res.marks[0].atm == pytest.approx(0.0715, abs=1e-12)

    def test_a_leading_pair_column_marks_several_pairs_in_one_paste(self):
        """The G3 book in one paste is the whole point of T-1's 30-second target."""
        res = parse_grid("EURUSD 1M 7.05 -0.25 0.18\nUSDJPY 1M 9.25 -1.25 0.30\n"
                         "GBPUSD 1M 8.10 -0.40 0.20")
        assert res.pairs == ["EURUSD", "GBPUSD", "USDJPY"]
        by = {(m.pair, m.tenor): m for m in res.marks}
        assert by[("USDJPY", "1M")].atm == pytest.approx(0.0925)
        assert by[("USDJPY", "1M")].rr25 == pytest.approx(-0.0125)

    def test_a_slashed_pair_token_is_understood(self):
        assert parse_grid("EUR/USD 1M 7.05 -0.25 0.18").marks[0].pair == "EURUSD"

    @pytest.mark.parametrize("text,pair", [("1M\t7.05\t-0.25\t0.18", None)])
    def test_a_grid_with_no_pair_anywhere_fails_loudly_instead_of_guessing(self, text, pair):
        res = parse_grid(text, pair)
        assert not res.ok
        assert any("no pair" in s for s in res.skipped), res.summary()

    def test_unparseable_lines_are_skipped_and_named_not_swallowed(self):
        res = parse_grid("EURUSD\n1M\t7.05\t-0.25\t0.18\nplease call me\n"
                         "3M\t7.40\t-0.35\t0.22")
        assert [m.tenor for m in res.marks] == ["1M", "3M"]
        assert any("please call me" in s for s in res.skipped), res.summary()

    @pytest.mark.parametrize("text", ["", "   \n\n  ", "# only a comment"])
    def test_empty_input_is_not_an_exception(self, text):
        """The parser is called from a Dash callback on every keystroke-ish paste."""
        res = parse_grid(text, "EURUSD")
        assert isinstance(res, ParsedGrid) and not res.ok and res.warnings


class TestUnitInference:
    """``8.5`` vs ``0.085`` -- a 100x error in the single most load-bearing number."""

    @pytest.mark.parametrize("text,scale,atm", [
        ("1M\t8.50\t-0.25\t0.18", 0.01, 0.0850),          # percent, biggest ATM > 1
        ("1M\t0.085\t-0.0025\t0.0018", 1.0, 0.0850),      # decimals
        ("1M\t8.50%\t-0.25%\t0.18%", 0.01, 0.0850),       # explicit %
        ("1M\t0.85%\t-0.02%", 0.01, 0.0085),              # '%' wins over magnitude
    ])
    def test_the_scale_is_inferred_from_the_atm_column(self, text, scale, atm):
        res = parse_grid(text, "EURUSD")
        assert res.scale == scale
        assert res.marks[0].atm == pytest.approx(atm, rel=1e-12)

    def test_the_inference_is_reported_never_silent(self):
        """Arch section 7: an assumption the user cannot see is a silent substitution."""
        for text in ("1M\t8.50\t-0.25\t0.18", "1M\t0.085\t-0.0025\t0.0018"):
            res = parse_grid(text, "EURUSD")
            assert any("read as" in s for s in res.inferred), res.summary()
            assert "vols read as" in res.summary()

    def test_one_scale_is_applied_to_the_whole_grid_not_per_column(self):
        """A per-column guess would read a 0.18 BF as decimal and a 7.05 ATM as percent."""
        res = parse_grid("1M\t7.05\t-0.25\t0.18", "EURUSD")
        m = res.marks[0]
        assert (m.atm, m.rr25, m.bf25) == pytest.approx((0.0705, -0.0025, 0.0018), abs=1e-12)

    def test_a_percent_grid_pasted_as_decimals_is_still_one_consistent_scale(self):
        """Mixed magnitudes inside one paste must not produce mixed scales."""
        res = parse_grid("1W\t0.0790\t-0.0020\t0.0015\n1Y\t0.0995\t-0.0055\t0.0040",
                         "EURUSD")
        assert res.scale == 1.0
        assert all(0.005 <= m.atm <= 0.20 for m in res.marks)


class TestImplausibleMarksAreRefused:
    @pytest.mark.parametrize("atm_token,why", [
        ("705", "a percent grid pasted twice / a stray factor of 100"),
        ("0.0000705", "a decimal grid pasted as if it were percent"),
        ("1e6", "clipboard garbage"),
    ])
    def test_an_implausible_atm_is_not_stored(self, atm_token, why):
        """Refuse, do not rescale.  A 705% ATM prices the whole book insanely; a
        parser that "helpfully" divides by 100 is guessing at the one number the
        trader is being asked to confirm."""
        res = parse_grid(f"1M\t{atm_token}\t-0.25\t0.18", "EURUSD")
        assert not res.marks, f"{why}: stored {res.summary()}"
        assert res.warnings and any("implausible" in w for w in res.warnings), res.summary()
        assert any("not stored" in s for s in res.skipped), res.summary()

    def test_a_rejected_row_does_not_take_the_good_rows_with_it(self):
        res = parse_grid("1M\t7.05\t-0.25\t0.18\n3M\t740\t-0.35\t0.22", "EURUSD")
        assert [m.tenor for m in res.marks] == ["1M"]
        assert any("3M" in w and "implausible" in w for w in res.warnings)

    def test_the_plausible_band_admits_a_real_jpy_crisis_vol(self):
        res = parse_grid("1M\t35.00\t-8.50\t2.50", "USDJPY")
        assert res.marks and res.marks[0].atm == pytest.approx(0.35)

    def test_a_negative_butterfly_is_flagged_but_kept(self):
        """A market strangle can quote negative; it is odd, not impossible."""
        res = parse_grid("1M\t7.05\t-0.25\t-0.18", "EURUSD")
        assert res.marks[0].bf25 == pytest.approx(-0.0018)
        assert any("negative" in w for w in res.warnings), res.summary()

    def test_a_risk_reversal_bigger_than_the_atm_is_flagged(self):
        res = parse_grid("1M\t7.05\t-9.00\t0.18", "EURUSD")
        assert any("exceeds ATM" in w for w in res.warnings), res.summary()

    def test_a_missing_rr_or_bf_defaults_to_symmetric_and_says_so(self):
        res = parse_grid("1M\t7.05", "EURUSD")
        assert (res.marks[0].rr25, res.marks[0].bf25) == (0.0, 0.0)
        assert any("defaulted to 0.0" in w for w in res.warnings), res.summary()


class TestParserAlignmentHazards:
    """The parser reads column *positions*.  Anything that shifts them is a wrong mark."""

    def test_a_header_whose_columns_disagree_with_the_rows_must_not_produce_a_mark(self):
        """**FINDING (see docs/05_test_report.md F-7).**

        Header says ``Pair, Tenor, ATM, RR, BF``; the pasted rows omit the pair
        column (the user picked the pair from the dropdown instead).  Everything
        shifts one place left, so the header maps the *RR* value onto ``atm``.  The
        parser's own row scan finds the tenor at column 0 while the header insists
        column 1 is the tenor -- a contradiction it never checks.  A 7.05 vol book
        is then marked and saved at 25 vol with no warning about the ATM at all.
        """
        text = "Pair\tTenor\tATM\t25d RR\t25d BF\n1M\t7.05\t0.25\t0.18\n3M\t7.40\t0.35\t0.22"
        res = parse_grid(text, "EURUSD")
        for m in res.marks:
            assert m.atm == pytest.approx(_EXPECT[m.tenor][0], rel=1e-9), (
                f"header/row column misalignment silently marked {m.pair} {m.tenor} at "
                f"{m.atm * 100:.2f} vol instead of {_EXPECT[m.tenor][0] * 100:.2f}: "
                + res.summary())

    def test_the_mirror_case_refuses_rather_than_mismarks(self):
        """Rows carry a pair column the header does not declare -> nothing is stored.

        This direction is safe today (no ATM is found, so the grid is rejected) and
        is pinned so a future "be more forgiving" change cannot turn it into the
        silent-mismark direction above.
        """
        text = ("Tenor\tATM\t25d RR\t25d BF\nEURUSD\t1M\t7.05\t-0.25\t0.18\n"
                "USDJPY\t1M\t9.25\t-1.25\t0.30")
        res = parse_grid(text)
        assert not res.marks, res.summary()
        assert res.warnings, res.summary()


# --------------------------------------------------------------------------- #
# the store
# --------------------------------------------------------------------------- #
class TestManualQuoteStore:
    def test_a_mark_survives_a_save_and_reload_bit_for_bit(self, marked_store, tmp_path):
        reloaded = ManualQuoteStore(tmp_path / "marks.json")
        assert reloaded.count() == 2 and reloaded.pairs() == ["EURUSD"]
        a = marked_store.mark("EURUSD", "1M")
        b = reloaded.mark("EURUSD", "1M")
        assert (b.atm, b.rr25, b.bf25) == (a.atm, a.rr25, a.bf25)
        assert b.asof == a.asof

    def test_the_on_disk_layout_carries_its_schema_tag(self, marked_store, tmp_path):
        blob = json.loads((tmp_path / "marks.json").read_text())
        assert blob["schema"] == SCHEMA
        assert set(blob) >= {"schema", "updated", "marks"}

    def test_a_corrupt_marks_file_raises_rather_than_starting_unmarked(self, tmp_path):
        """Silently emptying the grid sends the book back to ETF vols -- the exact
        failure T-1 exists to prevent.  ``strict=False`` is the opt-out."""
        p = tmp_path / "marks.json"
        p.write_text("{not json")
        with pytest.raises(ValueError, match="unreadable"):
            ManualQuoteStore(p, strict=True)
        assert ManualQuoteStore(p, strict=False).count() == 0

    def test_a_single_bad_row_is_dropped_without_losing_the_file(self, tmp_path):
        p = tmp_path / "marks.json"
        p.write_text(json.dumps({"schema": SCHEMA, "marks": {"EURUSD": [
            {"tenor": "1M", "atm": 0.0705},
            {"tenor": "NOPE", "atm": 0.07},
            {"tenor": "3M", "atm": -1.0},
        ]}}))
        s = ManualQuoteStore(p)
        assert [m.tenor for m in s.get("EURUSD")] == ["1M"]

    def test_re_marking_a_tenor_replaces_it(self, marked_store):
        marked_store.set_mark("EURUSD", "1M", 0.0810, -0.0030, 0.0020)
        assert marked_store.count() == 2
        assert marked_store.mark("EURUSD", "1M").atm == pytest.approx(0.0810)

    def test_paste_replaces_the_pair_by_default(self, marked_store):
        """A morning re-mark must not leave yesterday's 6M hiding under today's grid."""
        marked_store.set_mark("EURUSD", "6M", 0.0800)
        res = marked_store.paste("1M\t7.10\t-0.26\t0.19\n3M\t7.45\t-0.36\t0.23", "EURUSD")
        assert res.ok
        assert [m.tenor for m in marked_store.get("EURUSD")] == ["1M", "3M"]

    def test_paste_can_be_asked_to_merge_instead(self, marked_store):
        marked_store.paste("6M\t7.80\t-0.45\t0.26", "EURUSD", replace_pair=False)
        assert [m.tenor for m in marked_store.get("EURUSD")] == ["1M", "3M", "6M"]

    def test_a_paste_that_parses_to_nothing_does_not_wipe_the_existing_marks(self,
                                                                            marked_store):
        """Fat-fingering the clipboard must not silently unmark the book."""
        before = {m.tenor: m.atm for m in marked_store.get("EURUSD")}
        res = marked_store.paste("total garbage, no numbers", "EURUSD")
        assert not res.ok
        assert {m.tenor: m.atm for m in marked_store.get("EURUSD")} == before

    def test_clear_removes_a_tenor_a_pair_or_everything(self, marked_store):
        marked_store.set_mark("USDJPY", "1M", 0.0925)
        assert marked_store.clear("EURUSD", "1M") == 1
        assert [m.tenor for m in marked_store.get("EURUSD")] == ["3M"]
        assert marked_store.clear("EURUSD") == 1
        assert marked_store.get("EURUSD") == []
        assert marked_store.clear() == 1 and marked_store.count() == 0

    def test_csv_round_trip_preserves_the_vol_exactly(self, marked_store, tmp_path):
        """A 1-ULP drift here shows up as a phantom vega P&L on the next mark."""
        path = marked_store.to_csv(tmp_path / "marks.csv")
        before = {(m.pair, m.tenor): (m.atm, m.rr25, m.bf25)
                  for p in marked_store.pairs() for m in marked_store.get(p)}
        fresh = ManualQuoteStore(tmp_path / "other.json")
        assert fresh.from_csv(path) == len(before)
        after = {(m.pair, m.tenor): (m.atm, m.rr25, m.bf25)
                 for p in fresh.pairs() for m in fresh.get(p)}
        assert after == before

    def test_the_frame_has_the_frozen_columns_even_when_empty(self, store, marked_store):
        assert list(store.as_frame().columns) == MARK_COLUMNS
        df = marked_store.as_frame()
        assert list(df.columns) == MARK_COLUMNS and len(df) == 2
        assert (df["T"] > 0).all()

    def test_spot_and_rate_overrides_persist_and_are_upper_cased(self, store, tmp_path):
        store.set_spot("eurusd", 1.0850, "trader's fix")
        store.set_rate("usd", 0.0400)
        again = ManualQuoteStore(tmp_path / "marks.json")
        assert again.spot_overrides["EURUSD"].value == pytest.approx(1.0850)
        assert again.rate_overrides["USD"].value == pytest.approx(0.04)
        assert again.clear_overrides() == 2

    def test_the_grid_tenor_default_set_is_a_valid_tenor_list(self):
        assert set(GRID_TENORS) <= set(cv.TENORS)


# --------------------------------------------------------------------------- #
# the provider
# --------------------------------------------------------------------------- #
class TestManualQuoteProvider:
    def test_it_serves_the_grid_badged_user_override(self, marked_store):
        p = ManualQuoteProvider(marked_store)
        q = p.smile_quotes("EURUSD")
        assert [x.tenor for x in q] == ["1M", "3M"]
        prov = p.provenance("surface.EURUSD")
        assert isinstance(prov, Provenance)
        assert prov.kind == "user_override" and prov.source == "manual"

    def test_the_per_tenor_badge_exists_for_the_cg_7_fallback(self, marked_store):
        """CG-7 lookup is most-specific-first: ``surface.EURUSD.1M`` then ``surface.EURUSD``."""
        p = ManualQuoteProvider(marked_store)
        p.smile_quotes("EURUSD")
        for key in ("surface.EURUSD", "surface.EURUSD.1M", "surface.EURUSD.3M"):
            assert p.provenance(key).kind == "user_override"
        assert "7.050" in p.provenance("surface.EURUSD.1M").note

    def test_an_unmarked_pair_returns_nothing_so_the_chain_falls_through(self, marked_store):
        assert ManualQuoteProvider(marked_store).smile_quotes("USDJPY") == []
        assert ManualQuoteProvider(marked_store).marked("USDJPY") is False

    def test_a_stale_mark_is_still_served_but_the_badge_says_so(self, store):
        old = datetime.now(timezone.utc) - timedelta(hours=20)
        store.set_mark("EURUSD", "1M", 0.0705, asof=old)
        p = ManualQuoteProvider(store)
        assert p.smile_quotes("EURUSD")
        assert "STALE" in p.provenance("surface.EURUSD").note
        assert p.age_hours("EURUSD") == pytest.approx(20, abs=0.1)

    def test_max_age_hours_declines_so_the_chain_can_fall_through(self, store):
        store.set_mark("EURUSD", "1M", 0.0705,
                       asof=datetime.now(timezone.utc) - timedelta(hours=48))
        assert ManualQuoteProvider(store, max_age_hours=24).smile_quotes("EURUSD") == []
        assert ManualQuoteProvider(store).smile_quotes("EURUSD")     # no cap: still served

    def test_it_declines_everything_that_is_not_a_mark(self, marked_store):
        """Spot history / OI / events must fall through, not be faked as empty truth."""
        from datetime import date
        p = ManualQuoteProvider(marked_store)
        assert len(p.spot_history("EURUSD", date(2026, 1, 1), date(2026, 2, 1))) == 0
        assert len(p.open_interest("EURUSD")) == 0
        assert len(p.events(date(2026, 1, 1), date(2026, 2, 1))) == 0

    def test_overrides_are_only_returned_for_the_keys_the_user_actually_set(self,
                                                                           marked_store):
        marked_store.set_spot("EURUSD", 1.0850)
        p = ManualQuoteProvider(marked_store)
        assert p.spot(["EURUSD", "USDJPY"]) == {"EURUSD": 1.0850}
        assert p.rates(["USD", "JPY"]) == {}
        assert p.provenance("spot.EURUSD").kind == "user_override"

    def test_status_names_the_marked_pairs_and_their_age(self, tmp_path, marked_store):
        empty = ManualQuoteStore(tmp_path / "empty.json")
        assert ManualQuoteProvider(empty).status()[0].ok is False
        rows = ManualQuoteProvider(marked_store).status()
        assert [r.name for r in rows] == ["manual-marks:EURUSD"]
        assert rows[0].ok and rows[0].rows == 2


# --------------------------------------------------------------------------- #
# ChainProvider resolution -- manual -> live -> cache -> synthetic (T-1 / M-1)
# --------------------------------------------------------------------------- #
class _FakeLive:
    """A stand-in for a live source: answers every pair with a distinctive vol."""
    name = "fake-live"
    kind = "live"

    def __init__(self, atm=0.1234):
        self.atm = atm
        self.calls: list[str] = []

    def smile_quotes(self, pair, asof=None):
        self.calls.append(pair)
        return [SmileQuotes(T=1 / 12, atm=self.atm, rr25=0.0, bf25=0.001, tenor="1M")]

    def spot(self, pairs):
        return {p: 1.0 for p in pairs}

    def rates(self, ccys):
        return {c: 0.01 for c in ccys}

    def spot_history(self, pair, start, end):
        import pandas as pd
        return pd.DataFrame()

    def open_interest(self, pair, asof=None):
        import pandas as pd
        return pd.DataFrame()

    def events(self, start, end):
        import pandas as pd
        return pd.DataFrame()

    def provenance(self, field, kind=None, note="", asof=None):
        return Provenance(self.name, self.kind, datetime.now(timezone.utc), note)

    def status(self):
        return []


class TestChainResolutionOrder:
    def _chain(self, marked_store, **kw):
        from fxgamma.data.provider import ChainProvider
        return ChainProvider(**kw)

    def test_a_manual_mark_wins_over_live(self, marked_store):
        from fxgamma.data.provider import ChainProvider
        live = _FakeLive()
        ch = ChainProvider([live, ManualQuoteProvider(marked_store)])
        q = ch.smile_quotes("EURUSD")
        assert [x.atm for x in q] == [pytest.approx(0.0705), pytest.approx(0.0740)]
        assert live.calls == [], "the live source was consulted for a marked pair"
        assert ch.provenance("surface.EURUSD").kind == "user_override"

    def test_manual_is_hoisted_to_the_front_whatever_order_it_was_passed(self,
                                                                        marked_store):
        """M-1: "manual hoisted to the front regardless of construction order"."""
        from fxgamma.data.provider import ChainProvider
        for order in ("manual-first", "manual-last"):
            m = ManualQuoteProvider(marked_store)
            provs = [m, _FakeLive()] if order == "manual-first" else [_FakeLive(), m]
            ch = ChainProvider(provs)
            assert ch.providers[0] is m, order
            assert ch.manual is m and ch.marked_pairs() == ["EURUSD"]

    def test_an_unmarked_pair_falls_through_to_live_so_marking_is_per_pair(self,
                                                                          marked_store):
        from fxgamma.data.provider import ChainProvider
        live = _FakeLive()
        ch = ChainProvider([ManualQuoteProvider(marked_store), live])
        q = ch.smile_quotes("USDJPY")
        assert [x.atm for x in q] == [pytest.approx(0.1234)]
        assert live.calls == ["USDJPY"]
        assert ch.provenance("surface.USDJPY").kind == "live"

    def test_a_live_pull_never_overwrites_a_manual_mark(self, marked_store):
        """T-1's blocking clause.  Pull live repeatedly; the mark must not move."""
        from fxgamma.data.provider import ChainProvider
        ch = ChainProvider([ManualQuoteProvider(marked_store), _FakeLive()])
        for _ in range(3):
            ch.smile_quotes("USDJPY")                      # live traffic on another pair
            ch.spot(["EURUSD", "USDJPY"])
        assert [x.atm for x in ch.smile_quotes("EURUSD")] == [pytest.approx(0.0705),
                                                              pytest.approx(0.0740)]
        assert marked_store.mark("EURUSD", "1M").atm == pytest.approx(0.0705)

    def test_the_shipped_factory_builds_the_full_four_tier_order(self, marked_store):
        """manual -> live -> cache -> synthetic, as amendment v1.2 T-1 / M-1 states."""
        from fxgamma.data import get_provider
        from fxgamma.data.provider import CacheProvider, LiveProvider
        from fxgamma.data.synthetic import SyntheticProvider
        ch = get_provider("auto", manual=ManualQuoteProvider(marked_store))
        kinds = [type(p) for p in ch.providers]
        assert kinds[0] is ManualQuoteProvider
        assert kinds.index(LiveProvider) < kinds.index(CacheProvider) < \
            kinds.index(SyntheticProvider)
        assert kinds[-1] is SyntheticProvider, "synthetic must be the last resort"

    # PM ruling (amendment v1.9): the class defends itself -- synthetic is demoted to
    # the back however the chain was constructed.
    def test_synthetic_is_demoted_below_live_however_the_chain_was_constructed(self,
                                                                              marked_store):
        """Arch section 7: "Never silently substitute synthetic data for live data."

        ``ChainProvider`` guarantees exactly one re-ordering -- manual to the front.
        A caller who passes ``[synthetic, live]`` gets a chain that answers every
        unmarked pair from the simulator while a live source sits behind it, badged
        ``synthetic`` but never consulted.  The shipped ``get_provider`` orders it
        correctly, so this is a latent constructor hazard rather than a live defect;
        whether the class should defend itself is the PM's call.
        """
        from fxgamma.data.provider import ChainProvider
        from fxgamma.data.synthetic import SyntheticProvider
        live = _FakeLive()
        ch = ChainProvider([SyntheticProvider(), ManualQuoteProvider(marked_store), live],
                           allow_synthetic=True)
        assert isinstance(ch.providers[-1], SyntheticProvider)
        assert ch.provenance("surface.USDJPY").kind == "live"

    def test_synthetic_is_dropped_unless_explicitly_allowed(self, marked_store):
        from fxgamma.data.provider import ChainProvider
        from fxgamma.data.synthetic import SyntheticProvider
        ch = ChainProvider([ManualQuoteProvider(marked_store), SyntheticProvider()],
                           allow_synthetic=False)
        assert not any(isinstance(p, SyntheticProvider) for p in ch.providers)

    def test_the_snapshot_prices_a_marked_pair_off_the_desk_curve(self, marked_store):
        """End to end: the number the book is priced off *is* the typed number."""
        from fxgamma.data.provider import ChainProvider
        from fxgamma.data.synthetic import SyntheticProvider
        asof = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
        ch = ChainProvider([ManualQuoteProvider(marked_store),
                            SyntheticProvider(asof=asof)], allow_synthetic=True)
        snap = ch.snapshot(["EURUSD", "USDJPY"], asof=asof)
        assert snap.surfaces["EURUSD"].atm(cv.tenor_years("1M")) == pytest.approx(0.0705,
                                                                                 abs=1e-9)
        assert snap.meta["surface.EURUSD"].kind == "user_override"
        assert snap.meta["surface.USDJPY"].kind == "synthetic"

    def test_get_provider_attaches_and_can_disable_the_manual_tier(self, marked_store):
        from fxgamma.data import get_provider
        from fxgamma.data.provider import ChainProvider
        ch = get_provider("auto", manual=ManualQuoteProvider(marked_store))
        assert isinstance(ch, ChainProvider) and ch.marked_pairs() == ["EURUSD"]
        assert get_provider("auto", manual=False).manual is None
        assert get_provider("manual", manual=ManualQuoteProvider(marked_store)).marked("EURUSD")
        with pytest.raises(ValueError):
            get_provider("manual", manual=False)


# --------------------------------------------------------------------------- #
# property-based: the parser must never invent a plausible-but-wrong vol
# --------------------------------------------------------------------------- #
def test_round_trip_over_random_grids_is_exact():
    """Hand-rolled property sweep (no external dependency): render a random grid in a
    random spelling, parse it, and require the decimals back exactly."""
    import random
    rng = random.Random(20260907)
    tenors = ["1W", "1M", "2M", "3M", "6M", "1Y"]
    for _ in range(200):
        n = rng.randint(1, 4)
        picked = rng.sample(tenors, n)
        rows = {t: (round(rng.uniform(3.0, 40.0), 3),
                    round(rng.uniform(-3.0, 3.0), 3),
                    round(rng.uniform(0.0, 1.5), 3)) for t in picked}
        sep = rng.choice(["\t", ",", "   ", ";", "|"])
        pct = rng.random() < 0.5
        def fmt(v):
            return f"{v:g}%" if pct else f"{v:g}"
        text = "\n".join(sep.join([t, fmt(a), fmt(r), fmt(b)])
                         for t, (a, r, b) in rows.items())
        res = parse_grid(text, "EURUSD")
        assert res.ok, text
        got = {m.tenor: (m.atm, m.rr25, m.bf25) for m in res.marks}
        assert set(got) == set(rows)
        for t, (a, r, b) in rows.items():
            assert got[t][0] == pytest.approx(a / 100.0, rel=1e-12), (text, t)
            assert got[t][1] == pytest.approx(r / 100.0, rel=1e-12, abs=1e-15), (text, t)
            assert got[t][2] == pytest.approx(b / 100.0, rel=1e-12, abs=1e-15), (text, t)


def test_hypothesis_never_finds_a_grid_that_parses_to_an_implausible_vol():
    """If Hypothesis is installed, let it shrink against the plausibility invariant.

    The invariant is the one the trader cares about: whatever the parser stores, it
    is a decimal FX vol in a band a human would recognise, and it never stores
    something it also warned was implausible.
    """
    hyp = pytest.importorskip("hypothesis", reason="hypothesis is optional here")
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as stg

    num = stg.floats(min_value=-999, max_value=999, allow_nan=False, allow_infinity=False)
    row = stg.tuples(stg.sampled_from(["1W", "1M", "3M", "6M", "1Y"]), num, num, num)

    @given(stg.lists(row, min_size=1, max_size=5),
           stg.sampled_from(["\t", ",", "  ", ";"]), stg.booleans())
    @settings(max_examples=250, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    def prop(rows, sep, pct):
        text = "\n".join(sep.join([t, f"{a:g}", f"{r:g}", f"{b:g}"]) + ("%" if pct else "")
                         for t, a, r, b in rows)
        res = parse_grid(text, "EURUSD")
        for m in res.marks:
            assert math.isfinite(m.atm) and 0.005 <= m.atm <= 1.50, res.summary()
            assert math.isfinite(m.rr25) and math.isfinite(m.bf25)
            assert not any(f"{m.tenor}: rejected" in w for w in res.warnings)

    prop()
