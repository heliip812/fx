"""``fxgamma/portfolio/attribution.py`` -- the daily P&L explain.

The first QA pass could only check that the components close on a *trivial* move
(``t0 == t1``), which they do by construction.  The real test needs two genuinely
different snapshots and an unexplained-residual budget (REQ-052; the trader wants
< 1% overnight), and that is what this file builds: a shifted market as ``t1`` so
the true P&L is known and every component can be attributed.

Two failure modes are worth more than the rest:

* **Elapsed-time theta (test report section 5, item 2).**  The morning mark is ~14
  hours after the close, not a day.  Charging a whole day of theta over 14 hours is a
  ~40% error on the theta bar that lands in ``unexplained`` and trains the trader to
  ignore the residual alarm -- which is the thing that catches the next bug.
* **The reporting-ccy chain.**  A cross-currency book's residual must be measured in
  one currency, or "1% unexplained" is meaningless.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from fxgamma.portfolio import attribution, risk
from fxgamma.types import Book, OptionPosition, SpotPosition

from tests.conftest import in_days

pytestmark = pytest.mark.contract

# Imported, never re-typed: a hardcoded copy silently drifted from the library when
# the `veta` bar was added, and the reconciliation test then passed over a component
# it was not summing.
COMPONENTS = attribution.COMPONENTS


def _t1(snapshot, *, spot_mult=1.0, vol_add=0.0, days=0.0, rate_add=0.0):
    """A second snapshot that differs from ``snapshot`` in a controlled way."""
    kw = {}
    if rate_add:
        kw["rate_add"] = rate_add
    return risk.shift_market(snapshot, spot_mult=spot_mult, vol_add=vol_add,
                             days=days, **kw)


# --------------------------------------------------------------------------- #
# closure over a real move
# --------------------------------------------------------------------------- #
class TestResidualBudget:
    @pytest.mark.parametrize("spot_mult,vol_add,days", [
        (1.000, 0.000, 1.0),          # pure decay
        (1.003, 0.000, 1.0),          # a third of a percent, one day
        (0.997, 0.000, 1.0),
        (1.010, 0.000, 1.0),          # a full percent
        (1.000, 0.000, 14 / 24),      # the morning mark, W-14
        (1.005, 0.000, 3.0),          # Friday to Monday
    ])
    def test_the_unexplained_residual_stays_inside_the_req_052_budget(
            self, eur_book, snapshot, spot_mult, vol_add, days):
        """REQ-052 / trader: < 1% of the gross explained P&L overnight.

        The residual is real (third-order terms), so this is a budget, not an
        identity.  A sign error or a missing component blows straight through it.
        """
        t1 = _t1(snapshot, spot_mult=spot_mult, vol_add=vol_add, days=days)
        pnl = attribution.daily_pnl(eur_book, snapshot, t1)
        ratio = attribution.residual_ratio(pnl)
        assert ratio < 0.01, (f"residual {ratio:.3%} on spot x{spot_mult} "
                              f"vol +{vol_add} {days}d: {pnl}")

    @pytest.mark.parametrize("vol_add", [0.0025, 0.005])
    def test_an_ordinary_overnight_vol_move_stays_inside_the_req_052_budget(
            self, eur_book, snapshot, vol_add):
        """**FINDING (docs/05_test_report.md F-10): the vol convexity is counted twice.**

        Vega is now evaluated at the midpoint of the two snapshots, which closed the
        vol-time cross term.  But a midpoint vega already *contains* half the second
        derivative -- ``vega(t1) = V_s + V_ss ds + ...``, so
        ``mid_vega x ds = V_s ds + 1/2 V_ss ds^2`` -- and the explain then adds the
        explicit ``volga`` bar ``1/2 V_ss ds^2`` on top.  The second-order vol
        convexity is charged twice, and the surplus lands in ``unexplained`` with the
        opposite sign.

        The signature is unmistakable and is asserted by the companion test below: the
        residual grows as ``ds^2``, not ``ds^3``.  A genuine truncation error would be
        one order higher than the terms retained.

        Measured on this two-leg book, spot +0.3%, one day:

        ==============================  =======  =======  =======
        explain variant                 0.25pt   0.50pt   1.00pt
        ==============================  =======  =======  =======
        mid vega + volga  (shipped)      0.71%   *1.32%*  *2.57%*
        t0 vega + volga   (before)       1.12%    1.56%    2.07%
        mid vega, no volga bar           0.44%    0.53%    0.57%
        **t0 vega + volga + veta**      **0.22%** **0.37%** **0.40%**
        ==============================  =======  =======  =======

        The last row is the recommendation: keep theta pro-rata (W-14, and QA's two
        elapsed-time tests depend on it), keep vega and volga at ``t0`` so the
        waterfall bars stay the Greeks the trader recognises, and add the vol-time
        cross as its own named ``veta`` bar,
        ``(vega(t0+dt, s0) - vega(t0, s0)) x ds``.  That is inside REQ-052's 1% at
        every move size tested and, unlike the current combination, stops growing.
        **Owner: quant-risk.**
        """
        t1 = _t1(snapshot, spot_mult=1.003, vol_add=vol_add, days=1.0)
        pnl = attribution.daily_pnl(eur_book, snapshot, t1)
        assert attribution.residual_ratio(pnl) < 0.01, (
            f"residual {attribution.residual_ratio(pnl):.3%} on a {vol_add * 100:g} "
            f"vol point overnight move: {pnl}")

    def test_the_residual_is_third_order_in_the_move_not_second(self, eur_book,
                                                                snapshot):
        """The diagnostic behind F-10, and the answer to "is it just third order?".

        An explain that keeps every term up to second order must leave a residual that
        is **third** order: halve the move and the residual falls ~8x.  Measured on an
        instantaneous vol move (no time, no spot, so nothing else can contribute):

        ===========  =========  ==================
        vol move     residual   ratio to previous
        ===========  =========  ==================
        0.125 pt        -9.80   --
        0.25 pt        -38.97   3.98
        0.50 pt       -154.26   3.96
        1.00 pt       -605.65   3.93
        2.00 pt      -2351.67   3.88
        ===========  =========  ==================

        A factor of ~4 per doubling is ``ds^2``.  For comparison, the *spot* leg -- which
        has no double count -- shows ~9-10x per doubling, which is the ``dS^3`` a
        correct second-order explain is supposed to leave behind.

        The magnitude closes the case: the residual is within a few percent of minus
        the volga bar itself.
        """
        ratios = []
        prev = None
        for va in (0.00125, 0.0025, 0.005, 0.010):
            pnl = attribution.daily_pnl(eur_book, snapshot,
                                        _t1(snapshot, vol_add=va, days=0.0))
            r = abs(pnl.unexplained)
            if prev is not None:
                ratios.append(r / prev)
            prev = r
        assert all(x > 5.0 for x in ratios), (
            "the residual doubles-and-quadruples with the vol move, i.e. it is second "
            f"order: a second-order term is being double-counted.  ratios={ratios}")

    def test_the_spot_leg_residual_really_is_third_order(self, eur_book, snapshot):
        """The control for the test above: the *spot* side of the same explain has no
        double count, and behaves the way a correct second-order explain should --
        ~8x per doubling of the move."""
        ratios = []
        prev = None
        for x in (0.0025, 0.005, 0.010, 0.020):
            pnl = attribution.daily_pnl(eur_book, snapshot,
                                        _t1(snapshot, spot_mult=1 + x, days=0.0))
            r = abs(pnl.unexplained)
            if prev is not None:
                ratios.append(r / prev)
            prev = r
        assert all(x > 5.0 for x in ratios), ratios

    def test_total_equals_the_repriced_pv_change(self, eur_book, snapshot):
        """The one thing that must hold exactly: the explain explains *this* book."""
        t1 = _t1(snapshot, spot_mult=1.004, vol_add=0.004, days=1.0)
        pnl = attribution.daily_pnl(eur_book, snapshot, t1)
        d0 = risk.price_book(eur_book, snapshot)
        d1 = risk.price_book(eur_book, t1)
        assert pnl.total == pytest.approx(float(d1["pv_rep"].sum() - d0["pv_rep"].sum()),
                                          rel=1e-9)

    def test_components_plus_unexplained_reconstruct_the_total_exactly(self, eur_book,
                                                                       snapshot):
        t1 = _t1(snapshot, spot_mult=1.004, vol_add=0.004, days=1.0)
        pnl = attribution.daily_pnl(eur_book, snapshot, t1)
        s = sum(getattr(pnl, c) for c in COMPONENTS) + pnl.unexplained
        assert s == pytest.approx(pnl.total, rel=1e-9, abs=1e-6)

    def test_a_zero_move_produces_a_zero_explain(self, eur_book, snapshot):
        pnl = attribution.daily_pnl(eur_book, snapshot, snapshot)
        assert pnl.total == pytest.approx(0.0, abs=1e-9)
        for c in COMPONENTS:
            assert getattr(pnl, c) == pytest.approx(0.0, abs=1e-9), c


class TestComponentsAreTheRightSize:
    def test_a_pure_decay_lands_in_theta_and_nowhere_else(self, eur_book, snapshot):
        t1 = _t1(snapshot, days=1.0)
        pnl = attribution.daily_pnl(eur_book, snapshot, t1)
        assert pnl.theta < 0
        assert abs(pnl.theta) > 0.95 * abs(pnl.total)
        for c in ("delta", "gamma", "vanna", "hedge"):
            assert getattr(pnl, c) == pytest.approx(0.0, abs=1e-9), c
        # vega is *not* zero: rolling the clock moves the option along the term
        # structure, so sigma(K, T) changes even with the surface frozen.  It must
        # stay small against theta, and it must be there rather than in unexplained.
        assert abs(pnl.vega) < 0.05 * abs(pnl.theta)

    def test_a_pure_spot_move_lands_in_delta_and_gamma(self, eur_book, snapshot):
        t1 = _t1(snapshot, spot_mult=1.005)
        pnl = attribution.daily_pnl(eur_book, snapshot, t1)
        assert abs(pnl.delta) > 0 and pnl.gamma > 0        # long gamma book
        assert pnl.theta == pytest.approx(0.0, abs=1e-9)
        assert pnl.vega == pytest.approx(0.0, abs=1e-9)

    def test_gamma_is_quadratic_in_the_move_and_delta_is_linear(self, eur_book, snapshot):
        small = attribution.daily_pnl(eur_book, snapshot, _t1(snapshot, spot_mult=1.002))
        big = attribution.daily_pnl(eur_book, snapshot, _t1(snapshot, spot_mult=1.004))
        assert big.delta == pytest.approx(2 * small.delta, rel=1e-6)
        assert big.gamma == pytest.approx(4 * small.gamma, rel=1e-6)

    def test_a_pure_vol_move_lands_in_vega(self, eur_book, snapshot):
        t1 = _t1(snapshot, vol_add=0.01)
        pnl = attribution.daily_pnl(eur_book, snapshot, t1)
        assert pnl.vega > 0
        assert abs(pnl.vega) > 0.9 * abs(pnl.total)
        assert pnl.delta == pytest.approx(0.0, abs=1e-9)

    def test_a_joint_spot_and_vol_move_puts_something_in_vanna(self, eur_book, snapshot):
        t1 = _t1(snapshot, spot_mult=1.01, vol_add=0.01)
        pnl = attribution.daily_pnl(eur_book, snapshot, t1)
        assert abs(pnl.vanna) > 0.0
        cross = attribution.daily_pnl(eur_book, snapshot, _t1(snapshot, spot_mult=1.02,
                                                              vol_add=0.01))
        assert cross.vanna == pytest.approx(2 * pnl.vanna, rel=1e-6)


class TestElapsedTimeTheta:
    """W-14: theta must be charged over wall-clock between the two stamps."""

    def test_half_a_day_charges_half_the_theta(self, eur_book, snapshot):
        full = attribution.daily_pnl(eur_book, snapshot, _t1(snapshot, days=1.0))
        half = attribution.daily_pnl(eur_book, snapshot, _t1(snapshot, days=0.5))
        assert half.theta == pytest.approx(0.5 * full.theta, rel=1e-9)

    def test_a_fourteen_hour_mark_is_not_charged_a_whole_day(self, eur_book, snapshot):
        """The error is ~40% of the theta bar and it lands in ``unexplained``."""
        morning = attribution.daily_pnl(eur_book, snapshot, _t1(snapshot, days=14 / 24))
        day = attribution.daily_pnl(eur_book, snapshot, _t1(snapshot, days=1.0))
        assert morning.theta == pytest.approx(day.theta * 14 / 24, rel=1e-9)
        assert abs(morning.theta) < 0.7 * abs(day.theta)

    def test_the_detail_frame_carries_the_elapsed_days_it_used(self, eur_book, snapshot):
        pnl = attribution.daily_pnl(eur_book, snapshot, _t1(snapshot, days=14 / 24))
        assert float(pnl.detail["dt_days"].iloc[0]) == pytest.approx(14 / 24, rel=1e-9)


class TestHedgesAndSpot:
    def test_a_hedge_struck_between_the_snapshots_lands_in_the_hedge_bar(self, eur_book,
                                                                        snapshot):
        """The hedge log (REQ-055) only reconciles if these are measured from the
        hedge's own entry rate, not from the t0 spot."""
        S0 = snapshot.spot["EURUSD"]
        t1 = _t1(snapshot, spot_mult=1.01, days=1.0)
        h = SpotPosition(id="h1", pair="EURUSD", notional_base=-5e6,
                         entry_rate=S0 * 1.004, tag="hedge")
        with_h = attribution.daily_pnl(eur_book, snapshot, t1, hedges=[h])
        without = attribution.daily_pnl(eur_book, snapshot, t1)
        want = -5e6 * (t1.spot["EURUSD"] - S0 * 1.004)
        assert with_h.hedge == pytest.approx(want, rel=1e-9)
        assert with_h.total == pytest.approx(without.total + with_h.hedge + with_h.carry
                                             - without.carry, rel=1e-6)

    def test_a_spot_leg_in_the_book_is_pure_delta_plus_carry(self, snapshot):
        S0 = snapshot.spot["EURUSD"]
        b = Book(options=[], spots=[SpotPosition(id="s", pair="EURUSD",
                                                 notional_base=10e6, entry_rate=S0)])
        t1 = _t1(snapshot, spot_mult=1.01, days=1.0)
        pnl = attribution.daily_pnl(b, snapshot, t1)
        assert pnl.delta == pytest.approx(10e6 * (t1.spot["EURUSD"] - S0), rel=1e-9)
        assert pnl.gamma == 0.0 and pnl.vega == 0.0 and pnl.theta == 0.0
        assert pnl.unexplained == pytest.approx(0.0, abs=1e-6)


class TestCrossCurrencyExplain:
    def test_the_explain_is_reported_in_one_currency(self, mixed_book, snapshot):
        """CG-1 reaches the P&L page too: a EURUSD + USDJPY explain summed in native
        ccy is the same 100x error as the theta card."""
        t1 = _t1(snapshot, spot_mult=1.004, vol_add=0.003, days=1.0)
        usd = attribution.daily_pnl(mixed_book, snapshot, t1, report_ccy="USD")
        assert usd.ccy == "USD"
        native = float(usd.detail["total"].sum())
        assert abs(native / usd.total) > 20.0, "the fixture lost its JPY leg"
        assert usd.total == pytest.approx(float(usd.detail["total_rep"].sum()), rel=1e-9)

    @pytest.mark.parametrize("rep", ["USD", "EUR"])
    def test_changing_the_reporting_ccy_rescales_every_bar(self, mixed_book, snapshot,
                                                           rep):
        t1 = _t1(snapshot, spot_mult=1.004, vol_add=0.003, days=1.0)
        a = attribution.daily_pnl(mixed_book, snapshot, t1, report_ccy="USD")
        b = attribution.daily_pnl(mixed_book, snapshot, t1, report_ccy=rep)
        k = risk.fx_rate("USD", rep, t1)
        assert b.total == pytest.approx(a.total * k, rel=1e-9)
        for c in COMPONENTS:
            assert getattr(b, c) == pytest.approx(getattr(a, c) * k, rel=1e-9, abs=1e-9)

    def test_the_residual_budget_holds_on_the_cross_currency_book(self, mixed_book,
                                                                  snapshot):
        t1 = _t1(snapshot, spot_mult=1.003, vol_add=0.004, days=1.0)
        pnl = attribution.daily_pnl(mixed_book, snapshot, t1)
        assert attribution.residual_ratio(pnl) < 0.01, pnl


class TestExpiryAndReporting:
    def test_a_leg_that_expires_between_the_snapshots_does_not_leave_a_residual(self,
                                                                               snapshot):
        """T-3: the option dies at the cut.  If the explain still charges it theta the
        residual jumps, which is how a trader learns to ignore the residual."""
        b = Book(options=[OptionPosition(id="dying", pair="EURUSD", cp=+1, strike=1.10,
                                         expiry=in_days(1), notional_base=10e6,
                                         direction=+1)], spots=[])
        t1 = _t1(snapshot, spot_mult=1.002, days=3.0)
        pnl = attribution.daily_pnl(b, snapshot, t1)
        assert bool(pnl.detail["expired_t1"].iloc[0])
        assert pnl.total == pytest.approx(
            sum(getattr(pnl, c) for c in COMPONENTS) + pnl.unexplained, rel=1e-9)

    def test_a_leg_already_dead_at_t0_contributes_nothing(self, snapshot):
        b = Book(options=[OptionPosition(id="dead", pair="EURUSD", cp=+1, strike=1.10,
                                         expiry=in_days(-3), notional_base=10e6,
                                         direction=+1)], spots=[])
        pnl = attribution.daily_pnl(b, snapshot, _t1(snapshot, spot_mult=1.01, days=1.0))
        for c in COMPONENTS:
            assert getattr(pnl, c) == 0.0, c

    def test_top_offenders_names_the_worst_positions(self, mixed_book, snapshot):
        t1 = _t1(snapshot, spot_mult=1.02, vol_add=0.02, days=2.0)
        pnl = attribution.daily_pnl(mixed_book, snapshot, t1)
        top = attribution.top_offenders(pnl, n=2)
        assert len(top) <= 2 and "id" in top.columns
        if len(top) > 1:
            a = np.abs(top["unexplained_rep"].to_numpy(float))
            assert a[0] >= a[1]

    def test_residual_ratio_is_zero_on_a_zero_move_and_bounded_otherwise(self, eur_book,
                                                                        snapshot):
        assert attribution.residual_ratio(
            attribution.daily_pnl(eur_book, snapshot, snapshot)) == pytest.approx(0.0,
                                                                                  abs=1e-9)
        r = attribution.residual_ratio(
            attribution.daily_pnl(eur_book, snapshot, _t1(snapshot, spot_mult=1.05,
                                                          vol_add=0.03, days=5)))
        assert 0.0 <= r < 1.0 and math.isfinite(r)

    def test_an_empty_book_returns_an_empty_but_well_formed_breakdown(self, snapshot):
        pnl = attribution.daily_pnl(Book(options=[], spots=[]), snapshot,
                                    _t1(snapshot, days=1.0))
        assert pnl.total == 0.0 and len(pnl.detail) == 0
        assert {"id", "pair", "kind"} <= set(pnl.detail.columns)
