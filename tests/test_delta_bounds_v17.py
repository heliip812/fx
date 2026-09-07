"""AMENDMENT v1.7 -- ``gk.max_attainable_delta`` and the unattainable wing.

Premium-adjusted call delta is **bounded**, and at long tenors and high vols the bound
sits below the deltas a broker routinely quotes.  On USDJPY, USDCHF, USDCAD, USDSEK and
USDNOK a "30-delta call" can simply not exist.  The amendment made two things binding:

* ``strike_from_delta`` returns ``nan`` above the bound -- **not a wrong root**.  A
  wrong root is the dangerous outcome: it is a finite, plausible strike that is not
  the one asked for, and nothing downstream can tell.
* the app must render "unattainable at this tenor/vol" rather than a blank, a zero or
  a ``nan`` strike (binding on dev; the library half is fenced here).

The PM's two verified numbers, on USDJPY with rd 2% / rf 4%: **0.8745** at 3M/10% vol
and **0.2764** at 5Y/40% vol.  Both are pinned below.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from fxgamma import conventions as cv
from fxgamma.models import gk

from tests.conftest import PA_PAIRS

pytestmark = pytest.mark.contract

S_JPY = 147.50
RD, RF = 0.02, 0.04            # the rate pair the amendment's figures were priced on


# --------------------------------------------------------------------------- #
# the two pinned numbers
# --------------------------------------------------------------------------- #
class TestThePinnedBounds:
    def test_the_bound_at_five_years_and_forty_vol(self):
        """~0.276.  Below the 25 delta a broker quotes on the *other* side and far
        below any 30-delta call, which is what makes it a UI problem and not a curio."""
        b = gk.max_attainable_delta(S_JPY, 5.0, RD, RF, 0.40, +1, "spot_pa")
        assert b == pytest.approx(0.2764, abs=5e-4), b
        assert b < 0.30, "a 30-delta pa call does not exist at 5Y/40 vol"

    def test_the_bound_at_three_months_and_ten_vol(self):
        """~0.8745 -- comfortably above every quoted delta, which is why the bug hid."""
        b = gk.max_attainable_delta(S_JPY, 0.25, RD, RF, 0.10, +1, "spot_pa")
        assert b == pytest.approx(0.8745, abs=5e-4), b
        assert b > 0.50

    def test_the_bound_does_not_depend_on_the_spot_level(self):
        """It is a function of ``sigma sqrt(T)`` and the rate differential only; a
        bound that moved with spot would mean a scaling bug in the peak search."""
        a = gk.max_attainable_delta(100.0, 5.0, RD, RF, 0.40, +1, "spot_pa")
        b = gk.max_attainable_delta(200.0, 5.0, RD, RF, 0.40, +1, "spot_pa")
        assert a == pytest.approx(b, rel=1e-9)


class TestTheBoundIsReal:
    @pytest.mark.parametrize("T", [0.25, 1.0, 2.0, 5.0])
    @pytest.mark.parametrize("sigma", [0.08, 0.20, 0.40])
    def test_no_strike_anywhere_beats_the_bound(self, T, sigma):
        """The claim, tested by brute force: scan five decades of strike space and
        assert nothing exceeds the reported maximum."""
        b = gk.max_attainable_delta(S_JPY, T, RD, RF, sigma, +1, "spot_pa")
        ks = S_JPY * np.exp(np.linspace(-5.0, 5.0, 4001))
        d = np.array([gk.delta_from_strike(float(k), S_JPY, T, RD, RF, sigma, +1,
                                           "spot_pa") for k in ks])
        assert np.nanmax(np.abs(d)) <= b * (1 + 1e-9), (np.nanmax(np.abs(d)), b)
        assert np.nanmax(np.abs(d)) > 0.98 * b, "the bound is not attained -- too loose"

    @pytest.mark.parametrize("T,sigma", [(5.0, 0.40), (2.0, 0.35), (1.0, 0.30)])
    def test_the_delta_map_is_unimodal_so_two_strikes_share_each_delta(self, T, sigma):
        """Why a solver must be told about the peak: below the bound there are *two*
        roots, and the wrong one is a strike nobody asked for."""
        b = gk.max_attainable_delta(S_JPY, T, RD, RF, sigma, +1, "spot_pa")
        ks = S_JPY * np.exp(np.linspace(-4.0, 4.0, 3001))
        d = np.array([gk.delta_from_strike(float(k), S_JPY, T, RD, RF, sigma, +1,
                                           "spot_pa") for k in ks])
        target = 0.5 * b
        crossings = int(np.sum(np.diff(np.sign(d - target)) != 0))
        assert crossings == 2, crossings

    def test_the_bound_falls_as_vol_and_tenor_rise(self):
        by_vol = [gk.max_attainable_delta(S_JPY, 1.0, RD, RF, s, +1, "spot_pa")
                  for s in (0.05, 0.10, 0.20, 0.40)]
        by_T = [gk.max_attainable_delta(S_JPY, T, RD, RF, 0.20, +1, "spot_pa")
                for T in (0.25, 1.0, 3.0, 5.0)]
        assert all(a > b for a, b in zip(by_vol, by_vol[1:])), by_vol
        assert all(a > b for a, b in zip(by_T, by_T[1:])), by_T


# --------------------------------------------------------------------------- #
# strike_from_delta above the bound
# --------------------------------------------------------------------------- #
class TestStrikeFromDeltaAboveTheBound:
    @pytest.mark.parametrize("T,sigma", [(5.0, 0.40), (3.0, 0.45), (2.0, 0.50)])
    def test_it_returns_nan_and_not_a_wrong_root(self, T, sigma):
        b = gk.max_attainable_delta(S_JPY, T, RD, RF, sigma, +1, "spot_pa")
        for mult in (1.001, 1.05, 1.5, 3.0):
            k = gk.strike_from_delta(b * mult, S_JPY, T, RD, RF, sigma, +1, "spot_pa")
            assert math.isnan(k), (
                f"delta {b * mult:.4f} is above the {b:.4f} bound but solved to K={k}")

    @pytest.mark.parametrize("T,sigma", [(5.0, 0.40), (3.0, 0.45), (1.0, 0.30)])
    def test_just_below_the_bound_it_solves_and_round_trips(self, T, sigma):
        """The other half: the bound must not be so conservative that it refuses
        strikes that do exist."""
        b = gk.max_attainable_delta(S_JPY, T, RD, RF, sigma, +1, "spot_pa")
        for frac in (0.25, 0.5, 0.9, 0.99):
            d = b * frac
            k = gk.strike_from_delta(d, S_JPY, T, RD, RF, sigma, +1, "spot_pa")
            assert math.isfinite(k) and k > 0, (d, k)
            back = gk.delta_from_strike(k, S_JPY, T, RD, RF, sigma, +1, "spot_pa")
            assert abs(back) == pytest.approx(d, rel=1e-7), (d, k, back)

    def test_a_thirty_delta_call_does_not_exist_at_5y_40_vol_but_a_25_does(self):
        """The concrete case the amendment is about: the bound is 0.2764, so the 25d
        wing solves and the 30d one cannot, on the same USDJPY smile."""
        assert math.isfinite(
            gk.strike_from_delta(0.25, S_JPY, 5.0, RD, RF, 0.40, +1, "spot_pa"))
        assert math.isnan(
            gk.strike_from_delta(0.30, S_JPY, 5.0, RD, RF, 0.40, +1, "spot_pa"))

    def test_the_put_side_is_unbounded_so_the_wing_always_exists(self):
        """Only the pa *call* is capped; a test that fenced both would be wrong."""
        assert math.isinf(gk.max_attainable_delta(S_JPY, 5.0, RD, RF, 0.40, -1,
                                                  "spot_pa"))
        for d in (0.10, 0.25, 0.50, 0.90):
            k = gk.strike_from_delta(d, S_JPY, 5.0, RD, RF, 0.40, -1, "spot_pa")
            assert math.isfinite(k) and k > 0


class TestTheNonPremiumAdjustedConventions:
    def test_the_spot_bound_is_the_foreign_discount_factor(self):
        for T, rf in ((1.0, 0.04), (5.0, 0.02), (0.25, 0.0)):
            assert gk.max_attainable_delta(1.16, T, 0.03, rf, 0.10, +1, "spot") == \
                pytest.approx(math.exp(-rf * T), rel=1e-12)

    def test_a_delta_above_the_spot_bound_is_refused(self):
        b = gk.max_attainable_delta(1.16, 1.0, 0.04, 0.02, 0.10, +1, "spot")
        assert math.isnan(gk.strike_from_delta(b * 1.01, 1.16, 1.0, 0.04, 0.02, 0.10,
                                               +1, "spot"))
        assert math.isfinite(gk.strike_from_delta(b * 0.99, 1.16, 1.0, 0.04, 0.02, 0.10,
                                                  +1, "spot"))

    # PM ruling (amendment v1.9): fixed. Forward delta is cp*N(cp*d1), so the
    # supremum is 1; returning inf told callers every delta was reachable.
    def test_the_forward_bound_is_one_as_the_docstring_says(self):
        """The docstring reads: "the supremum is the trivial one (``e^{-rf T}`` for
        ``spot``, ``1`` for ``fwd``, ``+inf`` in strike for a ``_pa`` put)".  The code
        returns ``inf`` for every non-``spot`` convention, so ``fwd`` reports a bound
        of infinity when the true supremum is 1.

        Cosmetic today -- ``strike_from_delta`` still returns ``nan`` for a forward
        delta above 1, so nothing prices wrong.  It matters because the amendment
        makes this function the thing the strike ticket asks *before* solving: a UI
        that trusts ``inf`` will show "attainable" and then render a ``nan`` strike,
        which is precisely the outcome v1.7 forbids.
        """
        assert gk.max_attainable_delta(1.16, 1.0, 0.04, 0.02, 0.10, +1, "fwd") == \
            pytest.approx(1.0)

    def test_a_forward_delta_above_one_is_still_refused_by_the_solver(self):
        """The safety net that makes F-13 cosmetic rather than dangerous."""
        for d in (1.0001, 1.5, 3.0):
            assert math.isnan(gk.strike_from_delta(d, 1.16, 1.0, 0.04, 0.02, 0.10,
                                                   +1, "fwd"))

    def test_an_unknown_convention_raises(self):
        with pytest.raises(ValueError, match="convention"):
            gk.max_attainable_delta(1.16, 1.0, 0.04, 0.02, 0.10, +1, "spot_premium")


# --------------------------------------------------------------------------- #
# every premium-adjusted pair, on the real conventions table
# --------------------------------------------------------------------------- #
class TestEveryPremiumAdjustedPair:
    @pytest.mark.parametrize("pair", PA_PAIRS)
    def test_the_bound_is_finite_positive_and_binding_at_long_tenors(self, pair,
                                                                     snapshot):
        spec = cv.pair_spec(pair)
        S = snapshot.spot.get(pair, 1.0)
        b = gk.max_attainable_delta(S, 5.0, 0.02, 0.04, 0.40, +1, spec.delta_convention)
        assert math.isfinite(b) and 0.0 < b < 1.0
        assert b < 0.35, f"{pair}: expected a binding bound at 5Y/40 vol, got {b:.4f}"

    @pytest.mark.parametrize("pair", PA_PAIRS)
    def test_the_quoted_25_delta_wing_exists_at_the_tenors_v1_actually_quotes(
            self, pair, snapshot):
        """The reassuring half: on the shipped tenor grid at realistic vols the 25d
        call is always attainable, so the ``nan`` path is an edge case and not the
        normal case."""
        spec = cv.pair_spec(pair)
        S = snapshot.spot.get(pair, 1.0)
        for tenor in ("1M", "3M", "6M", "1Y"):
            T = cv.tenor_years(tenor)
            for sigma in (0.06, 0.10, 0.15):
                k = gk.strike_from_delta(0.25, S, T, 0.02, 0.04, sigma, +1,
                                         spec.delta_convention)
                assert math.isfinite(k) and k > 0, (pair, tenor, sigma)

    def test_a_surface_propagates_the_nan_rather_than_inventing_a_wing(self):
        """Test-report section 5, item 1 -- the path nothing downstream was tested on.

        A 5Y USDJPY smile marked at 40 vol has a pa call bound of 0.2764, so a 30d
        call wing does not exist.  ``vol_by_delta`` must hand back ``nan``: not 0.0
        (a free option), not the vol at the nearest attainable delta (a wrong vol on
        a real strike), not the far root.
        """
        from datetime import datetime, timezone

        from fxgamma.models.surface import SmileQuotes, build_surface

        asof = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
        surf = build_surface("USDJPY", asof,
                             [SmileQuotes(T=5.0, atm=0.40, rr25=-0.02, bf25=0.008,
                                          tenor="5Y")], S_JPY, RD, RF)
        bound = gk.max_attainable_delta(S_JPY, 5.0, RD, RF, 0.40, +1, "spot_pa")
        assert bound < 0.30
        for d in (0.30, 0.40, 0.50):
            v = surf.vol_by_delta(d, 5.0, +1)
            assert math.isnan(v), f"delta {d} is above the {bound:.4f} bound but got {v}"
        for d in (0.10, 0.25):
            v = surf.vol_by_delta(d, 5.0, +1)
            assert math.isfinite(v) and 0.0 < v < 2.0, (d, v)

    def test_the_attainable_side_of_the_same_smile_still_prices(self):
        """The failure mode to avoid while fixing the one above: refusing the whole
        wing because part of it is unattainable."""
        from datetime import datetime, timezone

        from fxgamma.models.surface import SmileQuotes, build_surface

        asof = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
        surf = build_surface("USDJPY", asof,
                             [SmileQuotes(T=5.0, atm=0.40, rr25=-0.02, bf25=0.008,
                                          tenor="5Y")], S_JPY, RD, RF)
        for d in (0.10, 0.25, 0.50, 0.75):
            assert math.isfinite(surf.vol_by_delta(d, 5.0, -1)), d   # puts are unbounded


# --------------------------------------------------------------------------- #
# property-based: the bound is never violated, whatever the inputs
# --------------------------------------------------------------------------- #
def test_hypothesis_never_finds_a_delta_above_the_bound_that_solves():
    """Let Hypothesis shrink against the one invariant that matters: a delta above
    the reported bound must never come back as a finite strike."""
    pytest.importorskip("hypothesis", reason="hypothesis is optional here")
    from hypothesis import given, settings
    from hypothesis import strategies as stg

    @given(stg.floats(50.0, 300.0), stg.floats(0.02, 10.0), stg.floats(0.03, 1.0),
           stg.floats(-0.01, 0.10), stg.floats(-0.01, 0.10), stg.floats(1.0001, 4.0))
    @settings(max_examples=300, deadline=None)
    def prop(S, T, sigma, rd, rf, over):
        b = gk.max_attainable_delta(S, T, rd, rf, sigma, +1, "spot_pa")
        if not math.isfinite(b) or b <= 0:
            return
        k = gk.strike_from_delta(min(b * over, 0.9999), S, T, rd, rf, sigma, +1,
                                 "spot_pa")
        if b * over >= 1.0:
            return
        assert math.isnan(k), (S, T, sigma, rd, rf, b, b * over, k)

    prop()
