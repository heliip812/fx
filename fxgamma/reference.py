"""The ONE canonical reference book and cost table, so documents cannot drift.

Four documents each invented their own "reference book" and one of them
(`docs/12` s7) ended up internally impossible: it paired ``Gamma_1pct = 3.91mm``
with ``theta = -3,978/day``, but those two are not independent -- 3.91mm implies
about 7.05 vol, at which theta is about -3,083, while -3,978 needs about 9 vol,
at which Gamma_1pct is about 3.05mm.

This is the fourth duplication-drift bug on this project (after ``band_pct``,
``COMPONENTS`` and the Whalley-Wilmott policy constant), so the fix is the same one
that worked before: a single importable source that documents cite and tests assert,
rather than a number retyped in each file.

Regenerate the canonical figures with::

    python -m fxgamma.reference
"""
from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["REFERENCE", "COST_TIERS", "reference_figures"]


@dataclass(frozen=True)
class ReferenceBook:
    """EURUSD 1M ATM straddle, EUR 10mm per leg. Quoted everywhere as *the* example."""
    pair: str = "EURUSD"
    spot: float = 1.1650
    tenor_years: float = 1.0 / 12.0
    vol: float = 0.0796           # the marked ATM on the synthetic provider
    rd: float = 0.042             # USD
    rf: float = 0.021             # EUR
    notional_per_leg: float = 10e6
    var_fraction: float = 0.382   # measured overnight share of a day's variance
    clock_fraction: float = 14.0 / 24.0

    def strike(self) -> float:
        """Delta-neutral-straddle ATM."""
        f = self.spot * math.exp((self.rd - self.rf) * self.tenor_years)
        return f * math.exp(0.5 * self.vol ** 2 * self.tenor_years)


REFERENCE = ReferenceBook()

#: All-in round-trip spot cost in basis points of notional.
#: The user has **no OTC access**, so `retail` is their tier. The exact number is
#: their broker's and is still unconfirmed -- `retail_default` is what ships until
#: they supply it. The trader put a no-OTC all-in cost at 15-40x interbank, which
#: brackets the 3-8bp range; 5.0 is the mid used by the backtests.
COST_TIERS: dict[str, float] = {
    "interbank_eurusd": 0.2,
    "interbank_usdjpy": 0.3,
    "retail_low": 3.0,
    "retail_default": 5.0,
    "retail_high": 8.0,
}


def reference_figures() -> dict[str, float]:
    """Derive every figure quoted in the docs, so none of them is retyped."""
    from .models import gk_greeks

    r = REFERENCE
    k = r.strike()
    g = (gk_greeks(r.spot, k, r.tenor_years, r.rd, r.rf, r.vol, 1, r.notional_per_leg)
         + gk_greeks(r.spot, k, r.tenor_years, r.rd, r.rf, r.vol, -1, r.notional_per_leg))
    # sqrt(365) governs ECONOMICS (theta, breakeven); sqrt(252) governs DISTANCE
    # and probability. Amendment W-7 -- the PM published this and then violated it.
    sigma_day_econ = r.vol / math.sqrt(365.0) * 100.0
    sigma_day_dist = r.vol / math.sqrt(252.0) * 100.0
    sigma_on = sigma_day_dist * math.sqrt(r.var_fraction)
    theta_window = g.theta * r.clock_fraction
    cap = 0.5 * g.gamma_1pct * sigma_on
    return {
        "strike": k,
        "gamma_1pct": g.gamma_1pct,
        "theta_day": g.theta,
        "theta_window": theta_window,
        "vega": g.vega,
        "breakeven_pct_econ": sigma_day_econ,
        "sigma_day_distance_pct": sigma_day_dist,
        "sigma_overnight_pct": sigma_on,
        "theta_variance_ratio": r.clock_fraction / r.var_fraction,
        "delta_cap_base": cap,
        # gamma_1pct is delta per +1 PERCENT, so cap/gamma_1pct is a move in percent
        # and must be divided by 100 before it multiplies spot. Getting this wrong
        # returns 1,805 pips instead of 18 -- the fifth percent-vs-fraction slip on
        # this project, which is the argument for this module existing.
        "ladder_band_pips": cap / abs(g.gamma_1pct) / 100.0 * r.spot / 1e-4,
    }


if __name__ == "__main__":                                   # pragma: no cover
    for key, val in reference_figures().items():
        print(f"{key:26s} {val:15,.4f}")
