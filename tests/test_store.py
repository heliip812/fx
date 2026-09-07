"""``fxgamma/store.py`` -- the book's persistence and its import gate.

The store is the only place in the app where a user's own data can be *lost* rather
than merely mis-displayed, so the priorities are:

* **Soft delete.** REQ-031: a deleted position is hidden, not destroyed, and
  ``load_book(asof=)`` must reconstruct the book as it stood.  A hard delete that
  fires by default is unrecoverable.
* **The V-1..V-21 validation rules.** The CSV importer and the on-screen ticket go
  through the same gate, so a rule that is only enforced on one path is a hole.  The
  rules that matter most are the *unit* ones -- a USDJPY strike of 1.47, a vol typed
  as 8.5 instead of 0.085, a notional of 10 instead of 10mm.
* **Round trip.** Export then re-import must reproduce the same book, or the CSV is
  a one-way door.

Most tests construct ``Store(path)`` directly so they never touch the process-wide
cache; the ``get_store`` block at the bottom fences QA finding F-4, which the dev has
now fixed (a second book path used to return the first store, silently).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pytest

from fxgamma import store as st
from fxgamma.types import Book, OptionPosition, SpotPosition

pytestmark = pytest.mark.contract

UTC = timezone.utc


@pytest.fixture()
def store(tmp_path):
    s = st.Store(tmp_path / "book.db")
    yield s
    s.close()


def _opt(pid="o1", pair="EURUSD", cp=+1, K=1.1800, days=90, N=10e6, d=+1, **kw):
    return OptionPosition(id=pid, pair=pair, cp=cp, strike=K,
                          expiry=date.today() + timedelta(days=days),
                          notional_base=N, direction=d, **kw)


# --------------------------------------------------------------------------- #
# round trip and soft delete
# --------------------------------------------------------------------------- #
class TestPersistence:
    def test_an_option_round_trips_through_sqlite_field_for_field(self, store):
        o = _opt(premium_paid=123_456.78, premium_ccy="USD",
                 trade_date=date(2026, 9, 1), trade_spot=1.1650, trade_vol=0.0812,
                 cut="TKY15", tag="gamma",
                 trade_time=datetime(2026, 9, 1, 7, 5, tzinfo=UTC))
        store.save_position(o)
        back = store.load_book().options[0]
        for f in ("id", "pair", "cp", "strike", "expiry", "notional_base", "direction",
                  "premium_paid", "premium_ccy", "trade_date", "trade_spot",
                  "trade_vol", "cut", "tag", "trade_time"):
            assert getattr(back, f) == getattr(o, f), f

    def test_a_spot_leg_round_trips_including_its_sign(self, store):
        s = SpotPosition(id="s1", pair="USDJPY", notional_base=-7.5e6,
                         entry_rate=147.25, trade_date=date(2026, 9, 2), tag="hedge")
        store.save_position(s)
        back = store.load_book().spots[0]
        assert back.notional_base == -7.5e6 and back.entry_rate == 147.25

    def test_saving_the_same_id_twice_updates_rather_than_duplicating(self, store):
        store.save_position(_opt(K=1.18))
        store.save_position(_opt(K=1.20))
        book = store.load_book()
        assert len(book.options) == 1 and book.options[0].strike == 1.20

    def test_a_multi_row_save_is_all_or_nothing(self, store):
        """**FINDING (docs/05_test_report.md F-12).**

        ``save_positions`` documents itself as "Atomic multi-write: all rows or none
        (requirements section 3.6 step 4)" and ``commit_import`` as "Write a parsed
        report in **one** transaction: all rows or none".  Neither is.  ``_tx`` is a
        re-entrant context manager that **commits on exit at every level**, so the
        inner ``save_position`` commits row 1 before row 2 raises, and the outer
        rollback has nothing left to undo.

        The result is a half-imported book with no audit row -- the user cannot tell
        which half landed, and the preview gate they were shown described neither
        state.  The fix is a nesting depth counter (commit only when the outermost
        block exits) or a SAVEPOINT.  Owner: dev.
        """
        with pytest.raises((TypeError, AttributeError)):
            store.save_positions([_opt("o1"), "not a position"])
        assert store.load_book().options == [], (
            "row 1 was committed before row 2 failed: the multi-write is not atomic")

    def test_books_are_isolated_from_each_other(self, store):
        store.save_position(_opt("o1"), book="alpha")
        store.save_position(_opt("o2"), book="beta")
        assert [o.id for o in store.load_book("alpha").options] == ["o1"]
        assert [o.id for o in store.load_book("beta").options] == ["o2"]

    def test_the_store_survives_being_reopened(self, tmp_path):
        a = st.Store(tmp_path / "b.db")
        a.save_position(_opt("o1"))
        a.close()
        b = st.Store(tmp_path / "b.db")
        assert [o.id for o in b.load_book().options] == ["o1"]
        b.close()


class TestSoftDelete:
    def test_delete_hides_the_position_but_keeps_the_row(self, store):
        store.save_position(_opt("o1"))
        assert store.delete_position("o1") is True
        assert store.load_book().options == []
        assert [o.id for o in store.load_book(include_deleted=True).options] == ["o1"]

    def test_a_deleted_position_is_listed_with_its_timestamp(self, store):
        store.save_position(_opt("o1"))
        store.delete_position("o1")
        rows = store.deleted_positions()
        assert len(rows) == 1 and rows[0]["id"] == "o1" and rows[0]["deleted_at"]

    def test_undelete_brings_it_back(self, store):
        store.save_position(_opt("o1"))
        store.delete_position("o1")
        assert store.undelete_position("o1") is True
        assert [o.id for o in store.load_book().options] == ["o1"]

    def test_a_hard_delete_is_opt_in_and_unrecoverable(self, store):
        store.save_position(_opt("o1"))
        assert store.delete_position("o1", hard=True) is True
        assert store.load_book(include_deleted=True).options == []
        assert store.undelete_position("o1") is False

    def test_deleting_an_unknown_id_is_false_not_an_exception(self, store):
        assert store.delete_position("nope") is False

    def test_asof_reconstructs_the_book_as_it_stood(self, store):
        """REQ-031: "what did the book look like on Tuesday" is the point of the soft
        delete.  If ``asof`` ignores ``deleted_at`` the P&L page silently re-prices
        yesterday against today's positions."""
        store.save_position(_opt("o1"))
        t_mid = st.utcnow()
        store.delete_position("o1")
        assert store.load_book().options == []
        assert [o.id for o in store.load_book(asof=t_mid).options] == ["o1"]
        assert store.load_book(asof=t_mid - timedelta(days=1)).options == []

    def test_clear_book_soft_deletes_by_default(self, store):
        store.save_positions([_opt("o1"), _opt("o2")])
        assert store.clear_book() == 2
        assert store.load_book().options == []
        assert len(store.load_book(include_deleted=True).options) == 2

    def test_delete_by_tag_only_touches_that_tag(self, store):
        store.save_positions([_opt("o1", tag="hedge"), _opt("o2", tag="core")])
        assert store.delete_tag("hedge") == 1
        assert [o.id for o in store.load_book().options] == ["o2"]


class TestTradeTimeOrdering:
    def test_spot_legs_come_back_in_trade_time_order(self, store):
        """CG-5: "The hedge log orders by ``trade_time`` and falls back to
        ``trade_date``."  Out-of-order hedges make the log unreconcilable."""
        base = datetime(2026, 9, 7, tzinfo=UTC)
        for i, h in enumerate([15, 7, 11]):
            store.save_position(SpotPosition(id=f"s{i}", pair="EURUSD",
                                             notional_base=1e6, entry_rate=1.16,
                                             trade_date=base.date(), tag="hedge",
                                             trade_time=base + timedelta(hours=h)))
        got = [s.trade_time.hour for s in store.load_book().spots]
        assert got == sorted(got) == [7, 11, 15]

    def test_a_leg_without_a_trade_time_falls_back_to_its_trade_date(self, store):
        store.save_position(SpotPosition(id="old", pair="EURUSD", notional_base=1e6,
                                         entry_rate=1.16, trade_date=date(2026, 9, 1)))
        store.save_position(SpotPosition(
            id="new", pair="EURUSD", notional_base=1e6, entry_rate=1.17,
            trade_date=date(2026, 9, 5),
            trade_time=datetime(2026, 9, 5, 9, tzinfo=UTC)))
        assert [s.id for s in store.load_book().spots] == ["old", "new"]

    def test_the_hedge_log_is_written_automatically_and_ordered(self, store):
        base = datetime(2026, 9, 7, tzinfo=UTC)
        for i, h in enumerate([16, 8]):
            store.save_position(SpotPosition(id=f"h{i}", pair="EURUSD",
                                             notional_base=-1e6, entry_rate=1.16,
                                             tag="hedge",
                                             trade_time=base + timedelta(hours=h)))
        log = store.hedge_log()
        assert len(log) == 2
        times = [e.trade_time for e in log]
        assert times == sorted(times)
        assert [t.hour for t in times] == [8, 16]

    def test_a_non_hedge_spot_leg_is_not_logged_as_a_hedge(self, store):
        store.save_position(SpotPosition(id="p", pair="EURUSD", notional_base=1e6,
                                         entry_rate=1.16, tag="position"))
        assert len(store.hedge_log()) == 0

    def test_naive_trade_times_are_stored_as_utc(self, store):
        store.save_position(SpotPosition(id="s", pair="EURUSD", notional_base=1e6,
                                         entry_rate=1.16,
                                         trade_time=datetime(2026, 9, 7, 7, 5)))
        assert store.load_book().spots[0].trade_time.tzinfo is not None


# --------------------------------------------------------------------------- #
# CG-2: the mark-vol side table
# --------------------------------------------------------------------------- #
class TestMarkVols:
    def test_a_mark_is_stored_against_the_position_id(self, store):
        store.save_position(_opt("o1"))
        store.set_mark_vol("o1", 0.0925, mark_source="trader")
        m = store.marks()["o1"]
        assert m.mark_vol == pytest.approx(0.0925) and m.mark_source == "trader"

    def test_clearing_a_mark_removes_it(self, store):
        store.save_position(_opt("o1"))
        store.set_mark_vol("o1", 0.09)
        assert store.clear_mark_vol("o1") is True
        assert "o1" not in store.marks()

    def test_marks_survive_a_reopen(self, tmp_path):
        a = st.Store(tmp_path / "m.db")
        a.save_position(_opt("o1"))
        a.set_mark_vol("o1", 0.0812)
        a.close()
        b = st.Store(tmp_path / "m.db")
        assert b.marks()["o1"].mark_vol == pytest.approx(0.0812)
        b.close()


# --------------------------------------------------------------------------- #
# CSV round trip
# --------------------------------------------------------------------------- #
class TestCsvRoundTrip:
    def test_export_then_import_reproduces_the_book(self, tmp_path):
        a = st.Store(tmp_path / "a.db")
        a.save_positions([
            _opt("o1", premium_paid=95_000.0, trade_date=date(2026, 9, 1),
                 trade_spot=1.1650, trade_vol=0.0812, tag="core"),
            _opt("o2", pair="USDJPY", cp=-1, K=145.0, N=20e6, d=-1),
        ])
        a.save_position(SpotPosition(id="s1", pair="EURUSD", notional_base=-4.2e6,
                                     entry_rate=1.1640, tag="hedge"))
        text = a.export_csv()

        b = st.Store(tmp_path / "b.db")
        rep = b.parse_csv(text)
        assert not rep.fatal and rep.n_error == 0, rep.rejects_csv()
        b.commit_import(rep, accept_warnings=True)

        before = a.load_book()
        after = b.load_book()
        assert {o.id for o in after.options} == {o.id for o in before.options}
        for x, y in zip(sorted(before.options, key=lambda p: p.id),
                        sorted(after.options, key=lambda p: p.id)):
            for f in ("pair", "cp", "strike", "expiry", "notional_base", "direction",
                      "premium_paid", "cut", "tag"):
                assert getattr(x, f) == getattr(y, f), f
        assert after.spots[0].notional_base == pytest.approx(-4.2e6)
        a.close()
        b.close()

    def test_the_export_header_is_the_documented_schema(self, store):
        store.save_position(_opt("o1"))
        header = store.export_csv().splitlines()[0].split(",")
        assert header[:3] == ["instrument_type", "id", "pair"]
        assert "notional_base" in header and "direction" in header

    def test_parsing_writes_nothing_until_commit(self, store):
        store.save_position(_opt("o1"))
        text = store.export_csv()
        fresh = st.Store(":memory:")
        rep = fresh.parse_csv(text)
        assert len(rep.rows) == 1
        assert fresh.load_book().options == []          # the REQ-033 preview gate
        fresh.close()

    def test_a_file_with_no_pair_column_is_rejected_with_the_schema_named(self, store):
        rep = store.parse_csv("foo,bar\n1,2\n")
        assert rep.fatal and "pair" in rep.fatal

    def test_an_empty_file_is_rejected(self, store):
        assert store.parse_csv("").fatal
        assert store.parse_csv("# just a comment\n").fatal

    def test_commit_refuses_a_report_that_carries_errors(self, store):
        rep = store.parse_csv("instrument_type,pair,cp,strike,expiry,notional_base,"
                              "direction\nOPTION,XXXYYY,C,1.10,2027-01-15,10mm,B\n")
        assert rep.n_error >= 1
        with pytest.raises(ValueError, match="refusing to commit"):
            store.commit_import(rep)

    def test_commit_refuses_warnings_unless_they_are_accepted(self, store):
        rep = store.parse_csv("instrument_type,pair,cp,strike,expiry,notional_base,"
                              "direction\nOPTION,EURUSD,C,1.10,2027-01-15,10,B\n")
        assert rep.n_warn >= 1
        with pytest.raises(ValueError, match="warning"):
            store.commit_import(rep)
        store.commit_import(rep, accept_warnings=True)
        assert len(store.load_book().options) == 1

    def test_replace_mode_soft_deletes_the_old_book(self, store):
        store.save_position(_opt("o1"))
        text = ("instrument_type,id,pair,cp,strike,expiry,notional_base,direction\n"
                "OPTION,o9,EURUSD,C,1.10,2027-01-15,10mm,B\n")
        rep = store.parse_csv(text, mode="replace")
        store.commit_import(rep, accept_warnings=True)
        assert [o.id for o in store.load_book().options] == ["o9"]
        assert len(store.load_book(include_deleted=True).options) == 2

    def test_unknown_columns_are_kept_in_notes_not_dropped(self, store):
        text = ("instrument_type,pair,cp,strike,expiry,notional_base,direction,"
                "broker\nOPTION,EURUSD,C,1.10,2027-01-15,10mm,B,SomeBank\n")
        rep = store.parse_csv(text)
        assert any("unknown column" in m.text for r in rep.rows for m in r.messages)
        store.commit_import(rep, accept_warnings=True)
        assert "SomeBank" in "".join(store.notes().values())

    def test_the_import_is_audited(self, store):
        text = ("instrument_type,pair,cp,strike,expiry,notional_base,direction\n"
                "OPTION,EURUSD,C,1.10,2027-01-15,10mm,B\n")
        store.commit_import(store.parse_csv(text), accept_warnings=True)
        audit = store.import_audit()
        assert len(audit) == 1 and audit[0]["committed"] == 1

    def test_rejects_csv_is_a_file_the_user_can_fix_and_resubmit(self, store):
        text = ("instrument_type,pair,cp,strike,expiry,notional_base,direction\n"
                "OPTION,EURUSD,C,1.10,2027-01-15,10mm,B\n"
                "OPTION,USDJPY,C,1.47,2027-01-15,10mm,B\n")
        rep = store.parse_csv(text)
        out = rep.rejects_csv()
        assert "USDJPY" in out and "V-6" in out


# --------------------------------------------------------------------------- #
# V-1 .. V-21
# --------------------------------------------------------------------------- #
def _row(**kw):
    base = {"instrument_type": "OPTION", "pair": "EURUSD", "cp": "C", "strike": "1.1000",
            "expiry": "2027-01-15", "notional_base": "10mm", "direction": "B"}
    base.update(kw)
    return base


def _rules(res, severity=None):
    return {m.rule for m in res.messages if severity is None or m.severity == severity}


class TestValidationRules:
    def test_a_clean_option_row_passes_with_no_messages(self):
        r = st.validate_row(_row())
        assert r.position is not None and not _rules(r, "E")

    def test_v1_an_unknown_pair_is_refused_with_the_known_list(self):
        r = st.validate_row(_row(pair="XXXYYY"))
        assert "V-1" in _rules(r, "E")
        assert r.position is None

    def test_v2_an_expired_option_is_refused_unless_history_is_asked_for(self):
        past = (date.today() - timedelta(days=30)).isoformat()
        assert "V-2" in _rules(st.validate_row(_row(expiry=past)), "E")
        ok = st.validate_row(_row(expiry=past), allow_expired=True)
        assert "V-2" in _rules(ok, "W") and ok.position is not None

    def test_v3_an_expiry_past_the_two_year_grid_warns(self):
        far = (date.today() + timedelta(days=900)).isoformat()
        assert "V-3" in _rules(st.validate_row(_row(expiry=far)), "W")

    @pytest.mark.parametrize("pair,strike,rule", [
        ("USDJPY", "1.4725", "V-6"),        # JPY strike typed in EURUSD units
        ("EURUSD", "147.25", "V-7"),        # the mirror image
    ])
    def test_v6_v7_a_strike_in_the_wrong_units_is_refused(self, pair, strike, rule):
        """The most common ticket error there is, and the most expensive: a USDJPY
        option struck at 1.47 is a 100x notional error dressed as a typo."""
        r = st.validate_row(_row(pair=pair, strike=strike))
        assert rule in _rules(r, "E") and r.position is None

    def test_v8_an_option_notional_must_be_positive(self):
        assert "V-8" in _rules(st.validate_row(_row(notional_base="-10mm")), "E")

    def test_v8_a_spot_notional_must_be_non_zero(self):
        r = st.validate_row(_row(instrument_type="SPOT", notional_base="0",
                                 entry_rate="1.16"))
        assert "V-8" in _rules(r, "E")

    def test_v9_a_bare_notional_of_ten_is_queried(self):
        """"did you mean 10mm?" -- a 1,000,000x error the user cannot see on screen."""
        assert "V-9" in _rules(st.validate_row(_row(notional_base="10")), "W")

    def test_v10_a_vol_typed_as_a_percent_is_rescaled_and_reported(self):
        r = st.validate_row(_row(trade_vol="8.5"))
        assert "V-10" in _rules(r, "W")
        assert r.position.trade_vol == pytest.approx(0.085)

    def test_v10_a_vol_outside_the_plausible_band_is_refused(self):
        assert "V-10" in _rules(st.validate_row(_row(trade_vol="450")), "E")

    @pytest.mark.parametrize("bad", ["X", "", "long"])
    def test_v11_an_unreadable_call_put_flag_is_refused(self, bad):
        assert "V-11" in _rules(st.validate_row(_row(cp=bad)), "E")

    @pytest.mark.parametrize("good,want", [("C", +1), ("call", +1), ("P", -1),
                                           ("put", -1), ("+1", +1), ("-1", -1)])
    def test_v11_the_spellings_a_human_types_are_accepted(self, good, want):
        assert st.validate_row(_row(cp=good)).position.cp == want

    def test_v12_an_unreadable_direction_is_refused(self):
        assert "V-12" in _rules(st.validate_row(_row(direction="maybe")), "E")

    @pytest.mark.parametrize("good,want", [("B", +1), ("buy", +1), ("long", +1),
                                           ("S", -1), ("sell", -1), ("short", -1)])
    def test_v12_the_spellings_a_human_types_are_accepted(self, good, want):
        assert st.validate_row(_row(direction=good)).position.direction == want

    def test_v13_an_unknown_cut_is_refused_rather_than_defaulted_to_ny10(self):
        """The store's half of QA finding F-3 / amendment v1.5 Q-3: a typo'd cut must
        never silently become New York."""
        assert "V-13" in _rules(st.validate_row(_row(cut="NY1O")), "E")
        assert st.validate_row(_row(cut="TKY15")).position.cut == "TKY15"

    def test_v14_a_premium_ccy_that_is_not_a_leg_of_the_pair_is_refused(self):
        assert "V-14" in _rules(st.validate_row(_row(premium_ccy="CHF")), "E")
        assert "V-14" not in _rules(st.validate_row(_row(premium_ccy="EUR")), "E")

    def test_v15_a_premium_whose_sign_contradicts_the_direction_is_queried(self):
        r = st.validate_row(_row(direction="B", premium_paid="-95000"))
        assert "V-15" in _rules(r, "W")

    def test_v16_a_premium_that_is_a_large_share_of_notional_is_queried(self, snapshot):
        """Needs a market context: the check is premium against *quote* notional, so
        without a spot it cannot fire.  A 9mm premium on a 10mm EURUSD call is 77% of
        notional -- a premium-unit error, not a trade."""
        ctx = st.MarketContext.from_snapshot(snapshot)
        assert "V-16" in _rules(st.validate_row(_row(premium_paid="9000000"), ctx), "W")
        assert "V-16" not in _rules(st.validate_row(_row(premium_paid="95000"), ctx), "W")

    def test_v18_a_duplicate_id_is_refused_on_append_and_allowed_on_upsert(self, store):
        store.save_position(_opt("o1"))
        text = ("instrument_type,id,pair,cp,strike,expiry,notional_base,direction\n"
                "OPTION,o1,EURUSD,C,1.10,2027-01-15,10mm,B\n")
        assert store.parse_csv(text, mode="append").n_error >= 1
        assert store.parse_csv(text, mode="upsert").n_error == 0

    def test_v18_a_duplicate_id_inside_one_file_is_refused(self, store):
        text = ("instrument_type,id,pair,cp,strike,expiry,notional_base,direction\n"
                "OPTION,x1,EURUSD,C,1.10,2027-01-15,10mm,B\n"
                "OPTION,x1,EURUSD,P,1.05,2027-01-15,10mm,B\n")
        rep = store.parse_csv(text, mode="upsert")
        assert any(m.rule == "V-18" for r in rep.rows for m in r.messages)

    def test_v20_legs_sharing_a_structure_tag_must_be_consistent(self, store):
        """A "straddle" whose legs have different expiries is a data-entry slip that
        prices fine and hedges wrong."""
        bad = ("instrument_type,pair,cp,strike,expiry,notional_base,direction,tag\n"
               "OPTION,EURUSD,C,1.12,2027-01-15,10mm,B,straddle:s1\n"
               "OPTION,EURUSD,P,1.10,2027-01-15,10mm,B,straddle:s1\n")
        rep = store.parse_csv(bad)
        assert any(m.rule == "V-20" for r in rep.rows for m in r.messages), \
            rep.rejects_csv()
        good = bad.replace("1.12", "1.10")
        assert not any(m.rule == "V-20" for r in store.parse_csv(good).rows
                       for m in r.messages)

    def test_v20_a_risk_reversal_with_split_expiries_is_refused(self, store):
        text = ("instrument_type,pair,cp,strike,expiry,notional_base,direction,tag\n"
                "OPTION,EURUSD,C,1.22,2027-01-15,10mm,B,rr25:x\n"
                "OPTION,EURUSD,P,1.10,2027-03-15,10mm,S,rr25:x\n")
        msgs = [m for r in store.parse_csv(text).rows for m in r.messages]
        assert any(m.rule == "V-20" and "expiry" in m.text for m in msgs)

    def test_v21_an_oversized_import_warns_about_the_performance_envelope(self, store):
        rows = "\n".join(
            f"OPTION,EURUSD,C,1.10,2027-01-15,10mm,B" for _ in range(2001))
        rep = store.parse_csv("instrument_type,pair,cp,strike,expiry,notional_base,"
                              "direction\n" + rows)
        assert any(m.rule == "V-21" for r in rep.rows for m in r.messages)

    def test_a_spot_row_needs_an_entry_rate(self):
        r = st.validate_row(_row(instrument_type="SPOT", notional_base="5mm",
                                 entry_rate=""))
        assert r.position is None and _rules(r, "E")

    def test_an_unknown_instrument_type_is_refused(self):
        assert _rules(st.validate_row(_row(instrument_type="FUTURE")), "E")


class TestNotionalAndStrikeParsing:
    @pytest.mark.parametrize("text,want", [
        ("10mm", 10e6), ("10MM", 10e6), ("10m", 10e6), ("1bn", 1e9),
        ("10,000,000", 10e6), ("1.5mm", 1.5e6), ("250k", 250e3),
    ])
    def test_the_notional_spellings_a_trader_types(self, text, want):
        assert st.parse_notional(text) == pytest.approx(want)

    @pytest.mark.parametrize("bad", ["", "ten million", "10zz", None])
    def test_an_unparseable_notional_raises(self, bad):
        with pytest.raises((ValueError, TypeError)):
            st.parse_notional(bad)

    def test_a_bare_number_is_taken_at_face_value(self):
        """No guessing: ``10`` is ten units, and V-9 asks whether that was meant."""
        assert st.parse_notional("10") == pytest.approx(10.0)


# --------------------------------------------------------------------------- #
# settings, levels, demo book
# --------------------------------------------------------------------------- #
class TestSettingsAndLevels:
    def test_settings_round_trip_json_values(self, store):
        store.set_setting("provider", "manual")
        store.set_setting("bands", {"EURUSD": 2.5e6})
        assert store.get_setting("provider") == "manual"
        assert store.get_setting("bands")["EURUSD"] == 2.5e6
        assert store.get_setting("missing", "fallback") == "fallback"
        assert set(store.settings()) == {"provider", "bands"}

    def test_levels_are_soft_deleted_and_sorted(self, store):
        a = store.add_level("EURUSD", 1.2000, "resistance")
        store.add_level("EURUSD", 1.1000, "support")
        assert [r["level"] for r in store.levels("EURUSD")] == [1.10, 1.20]
        assert store.delete_level(a) is True
        assert [r["level"] for r in store.levels("EURUSD")] == [1.10]

    def test_is_empty_and_stats_track_the_book(self, store):
        assert store.is_empty()
        store.save_position(_opt("o1"))
        assert not store.is_empty()
        assert store.stats()["options"] == 1


# --------------------------------------------------------------------------- #
# QA finding F-4, carried over from the first pass
# --------------------------------------------------------------------------- #
def test_get_store_returns_the_store_for_the_path_it_was_asked_for(tmp_path):
    """**QA finding F-4 is FIXED** (amendment v1.5 Q-4, assigned to dev).

    ``get_store`` used to be a process-wide singleton that ignored ``path`` after the
    first call, so a user switching book file on the Data page would edit, delete and
    re-import **into the wrong database**.  It is now cached per resolved path.  This
    test is the fence: a second path must give a second store.
    """
    a = st.get_store(tmp_path / "one.db")
    b = st.get_store(tmp_path / "two.db")
    assert a is not b
    assert str(a.path).endswith("one.db") and str(b.path).endswith("two.db")
    a.save_position(_opt("only-in-a"))
    assert b.load_book().options == []
    st.close_stores()


def test_the_same_book_asked_for_two_ways_is_one_store(tmp_path, monkeypatch):
    """The other half of the fix: one file must not become two connections.  WAL wants
    a single writer per process, and two handles on one file is how a stale read
    happens."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    a = st.get_store("data/x.db")
    b = st.get_store("./data/x.db")
    c = st.get_store(tmp_path / "data" / "x.db")
    assert a is b is c
    st.close_stores()


def test_an_in_memory_store_is_never_shared(tmp_path):
    """``:memory:`` is private by definition; caching it would silently join two
    unrelated books."""
    assert st.get_store(":memory:") is not st.get_store(":memory:")


def test_fresh_rebinds_only_the_book_it_names(tmp_path):
    a = st.get_store(tmp_path / "one.db")
    b = st.get_store(tmp_path / "two.db")
    a2 = st.get_store(tmp_path / "one.db", fresh=True)
    assert a2 is not a
    assert st.get_store(tmp_path / "two.db") is b, "fresh= closed an unrelated book"
    st.close_stores()
