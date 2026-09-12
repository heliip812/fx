"""The overnight delta cap: the most delta you are willing to wake up holding.

``recommend_cap(book, mkt, pair) -> DeltaCap`` derives that number **from the book**
so the user never types one, prints the rule and the arithmetic in words, and accepts
an override that reports what the override changes.

Why this module exists
----------------------
Three independent workstreams landed on the same conclusion from three directions:
the band optimiser found its objective so flat that being 30% off costs 0.1-0.9% of
the gamma P&L (``docs/09`` s5); the trader priced the whole band decision at ~USD 60 a
night against "thousands" for the cap (``docs/11`` s4.3); and the trend-conditional
study found that a rule which widens the band binds the cap on 32-46% of nights and is
a bet on persistence rather than a hedging improvement (``docs/12`` s7).  All three
*asserted* that the cap dominates.  ``docs/13_delta_cap.md`` measures it, and the
measurement is what the defaults here are set from.

The four things worth knowing before using it
---------------------------------------------
**1. The cap's effect on the expected overnight P&L is exactly minus its transaction
cost, and the dial is loud.**  Measured through a full option repricing on 40,000
nights (``docs/13`` s4.1): capping the reference EURUSD book at EUR 0.30mm costs
**USD 932 a night**, the paired difference against no cap reproduces minus that cost
cell by cell to under half a percent, and across the defensible cap range the expected
night moves by **USD 696**.  In ``docs/09`` s5's own units -- the units the band was
priced in -- **being 30% off the cap moves the night by 1.2-16.3% of its gamma P&L
against 0.1-0.9% for being 30% off the band**, and it does so at *both* cost tiers
(at the trader's 1.03bp it lands in the tail, at the repo's 5bp in the mean).
**The objective is not flat in the cap.**  That is the first-order result and it is
why this module exists.  It also reconciles the trader's independent USD 81: their
15/30/45-pip band sweep *was* a 0.5/1.0/1.5mm cap sweep, and this simulator reproduces
it at USD 90 (``docs/13`` s4.14).

**2. On a LONG-gamma book the cap does not buy much tail.**  The 5% tail of a
long-gamma night is the *quiet* night -- you pay the theta and the spot does not move
-- and the delta you wake up holding is small on exactly those nights.  Worse, the
delta you do wake up holding is insured by the gamma that produced it: a 150-pip gap
against EUR 2.4mm of accumulated delta costs USD 36k of delta and hands back USD 34k
of gamma.  Measured: capping at the tail-optimal EUR 0.30mm improves CVaR-95 by
USD 451 and costs USD 932 of mean -- an exchange rate of about **2 USD of mean per
1 USD of tail**.  Adding an unhedgeable morning (1-3 hours, or a 30-60 pip gap at the
open) moves that by under USD 100.  So for a long-gamma book the cap is a **stated
preference about the shape of the night, not an optimisation**, and this module says
so rather than dressing it as an optimum.

**3. On a SHORT-gamma book it is the whole decision.**  Same book, sign flipped, stops
slipped at twice the spread: CVaR-95 runs from USD -8,775 uncapped to USD -2,558 at
EUR 0.30mm, and the exchange rate over most of that range is **0.03-0.34 USD of mean
per USD of tail** -- an order of magnitude cheaper insurance than the long-gamma case,
on a tail an order of magnitude deeper.  :func:`recommend_cap` therefore uses a
different rule for short gamma, prices the stops with slippage, and applies the
trader's refusal conditions (``docs/11`` s5.2) rather than quietly returning a number.

**4. The shipped ``zones.hedge_bands`` default of 15% of gross notional is wrong for
overnight use.**  On the reference book it is EUR 3.0mm -- 2.8-5.6x every rule derived
here (3.4-6.7x the PM's uncorrected candidates), and above the EUR 2.4mm of delta the *unhedged* book accumulates at the 95th
percentile of a whole night.  It therefore never binds and is not a band at all
overnight.  It is a daytime intraday default and :func:`recommend_cap` says so in
``warnings`` whenever it is what the caller would otherwise have used.

Two clocks, and a correction to the PM's candidate table
--------------------------------------------------------
Spot variance is delivered on **252 trading days**; theta is paid on **365 calendar
days** (``docs/09`` s1).  The PM's candidate caps were computed with an overnight sigma
of 0.256%, which is ``sigma * sqrt(vf / 365)``.  On the repo's own convention it is
``sigma * sqrt(vf / 252) = 0.309%``, 20.8% larger.  Restated, the PM's EUR 0.89 /
0.45 / 0.68mm become EUR 1.08 / 0.54 / 0.57mm -- and note that R2 and R3 move in
*opposite* directions and converge, because they are the same rule under two anchors
(see :func:`cap_candidates`).  :data:`TRADING_DAYS` is imported from ``zones`` rather
than restated, because a duplicated constant is how ``band_pct`` drifted before.

What this module does NOT do
----------------------------
It does not ask for a risk-aversion coefficient (nobody can introspect one;
``bandopt.risk_aversion_for_band`` goes the other way).  It does not forecast anything:
the window sigma is session-scaled implied unless a ``range_forecast`` is supplied, and
path roughness stays at the Brownian baseline ``kappa = 1`` per ``docs/10``.  It does
not re-derive the repriced delta profile -- ``risk.spot_ladder`` owns that, and the
up-side/down-side asymmetry of a skewed book is read off it rather than assumed away
by a single ``Gamma_1pct``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ..conventions import pair_spec
from ..types import Book, MarketSnapshot
from .bandopt import (BookGamma, RETAIL_COST_BP, book_gamma, cost_bp_for,
                      risk_aversion_for_band)
from .overnight import PassiveWindow, RETAIL_LOT_BASE, passive_window
from .risk import fx_rate, spot_ladder
from .zones import COST_BP, DEFAULT_BAND_PCT, TRADING_DAYS

__all__ = [
    "CapCandidate", "DeltaCap", "CAP_RULES",
    "EFFICIENT_LO_MULT", "EFFICIENT_HI_MULT", "TAIL_MULT_95", "MORNING_HOURS",
    "SHORT_GAMMA_SLIP_MULT", "GAP_QUANTILE_SIGMAS",
    "overnight_sigma", "accumulated_delta", "breakeven_clip",
    "gap_loss", "short_gamma_cap", "cap_candidates", "recommend_cap", "recommend_caps", "cap_frame",
    "override_effect", "format_cap", "cap_scaling_note",
]

#: CVaR multiplier for a normal loss at the 95% level: ``phi(z) / (1 - alpha)``.
#: 2.0627 at alpha = 0.95.  Used by the tail-budget rule so the "one sigma" in the
#: PM's R2/R3 is replaced by an explicit tail statistic rather than left implicit.
TAIL_MULT_95 = 2.062713

#: How long after the London open the user is assumed to be unable to deal -- the
#: window over which the delta they woke up holding is genuinely unhedgeable.  One
#: hour is the London open bar of the shipped profile.  MEASURED to matter very little
#: on a long-gamma book (docs/13 s4.5), which is itself the finding.
MORNING_HOURS = 1.0

#: The efficient zone as a multiple of the RECOMMENDED cap: the range over which one
#: USD of CVaR-95 costs less than about 1.5 USD of expected P&L.  Outside it the
#: exchange rate deteriorates sharply in both directions.  MEASURED (``docs/13`` s4.3
#: and s6) at 0.74-1.39x across a 13x range of tenor, a 4x range of notional and a 2.2x
#: range of vol -- 7 cells, and the recommended rule lands inside the zone in all 7.
#: Numbers off synthetic paths, not theory.
EFFICIENT_LO_MULT = 0.75
EFFICIENT_HI_MULT = 1.4

#: A short-gamma stop does not fill at your level.  Default: twice the all-in spread
#: on an ordinary night (docs/11 s5.1.2).  The gap row is priced separately.
SHORT_GAMMA_SLIP_MULT = 2.0

#: The move a short-gamma cap is sized against, in window sigmas.  3.0 is the
#: ``ladder_summary`` gap convention; the 99th percentile of a normal is 2.33, so this
#: is deliberately beyond it and is still only a diffusion number.
GAP_QUANTILE_SIGMAS = 3.0

CAP_RULES: dict[str, str] = {
    "sigma_accumulation":
        "R1 -- the delta the book actually accumulates over one overnight sigma, read "
        "off the repriced delta profile. The cap then binds only on nights that move "
        "more than one sigma. A reference SCALE, not an optimum.",
    "delta_equals_gamma":
        "R2 -- THE DEFAULT for long gamma. The cap at which the directional P&L on the delta you wake up holding, "
        "over a further one-sigma move, equals the whole night's gamma P&L. 'The bet "
        "you did not choose is no bigger than the one you did.'",
    "theta_anchored":
        "R3 -- the cap at which that same directional P&L equals the theta you paid "
        "for the window. Identical to R2 up to the night's carry ratio.",
    "tail_budget":
        "R4 -- the cap at which the CVaR-95 of the unchosen directional loss, over the "
        "hours you genuinely cannot deal, equals a stated loss budget (default: the "
        "window's theta bill). R3 with an explicit tail statistic and an explicit "
        "unhedgeable horizon instead of two implicit ones. Use it when the user has "
        "an overnight loss limit; otherwise R2 says the same thing with one fewer "
        "assumption.",
    "premium_at_risk":
        "R5 -- a share of the option premium already at risk. Reported for scale; it "
        "scales as sqrt(T) where every other rule scales as 1/sqrt(T), so it is the "
        "wrong shape for a tenor change and is not used as a default.",
    "short_gamma_loss_limit":
        "R6 -- short gamma only: the cap at which the loss on a 3-sigma gap, with the "
        "stops slipped, stays inside the user's overnight loss limit. The only rule "
        "here whose tail actually responds to the cap at a cheap exchange rate.",
    "cost_floor":
        "F  -- not a cap but a FLOOR: 2*lambda*S*Gamma, the clip whose round trip "
        "costs exactly what its gamma capture is worth. Below it every fill loses "
        "money outright, whatever your risk aversion.",
}


# --------------------------------------------------------------------------- #
# result types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CapCandidate:
    """One rule's answer, with the arithmetic and the assumption it rests on."""
    rule: str
    cap_base: float                 # base ccy, the scalar you would type
    cap_up: float                   # base ccy, delta accumulated / tolerated ABOVE spot
    cap_dn: float                   # base ccy, below spot
    basis: str                      # the formula with this book's numbers substituted
    assumption: str                 # what has to be true for it to be the right rule
    scaling: str                    # how it moves with notional / tenor / vol / window
    band_pips: float = float("nan")  # the spot distance that cap implies, this book

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "cap_base": self.cap_base, "cap_mm": self.cap_base / 1e6,
                "cap_up": self.cap_up, "cap_dn": self.cap_dn, "band_pips": self.band_pips,
                "basis": self.basis, "assumption": self.assumption, "scaling": self.scaling}


@dataclass(frozen=True)
class DeltaCap:
    """The recommended overnight delta cap for one pair, and why.

    ``cap_base`` is the number to hand to
    :func:`fxgamma.portfolio.overnight.overnight_ladder` as ``max_overnight_delta``.
    ``cap_up`` / ``cap_dn`` are the per-side numbers -- they differ on a **skewed**
    book because the delta accumulated on the way up is not the delta accumulated on
    the way down, which is repricing and not a view (docs/11 s9.3).  ``reasoning`` is
    the human-readable string the UI prints; ``str(cap)`` returns it.
    """
    pair: str
    cap_base: float
    cap_up: float
    cap_dn: float
    rule: str
    reasoning: str
    # --- context ---
    spot: float = float("nan")
    base_ccy: str = ""
    ccy: str = ""
    gamma: float = 0.0
    gamma_1pct: float = 0.0
    gamma_side: str = "flat"                 # long | short | flat
    sigma_atm: float = float("nan")
    sigma_window: float = float("nan")
    var_fraction: float = float("nan")
    calendar_days: float = float("nan")
    window_label: str = ""
    theta_window: float = 0.0                # quote ccy, signed
    gamma_pnl_window: float = 0.0            # quote ccy, 0.5 * Gamma * V
    carry: float = 0.0
    cost_bp: float = float("nan")
    cost_tier: str = ""
    # --- the band the cap implies, and the constraints around it ---
    band_pips: float = float("nan")
    band_pips_up: float = float("nan")
    band_pips_dn: float = float("nan")
    floor_base: float = float("nan")         # dominance floor: below it, tighter is worse on BOTH
    ceiling_base: float = float("nan")       # above it the cap cannot bind on a tail night
    clip_floor_base: float = float("nan")    # smallest dealable clip
    breakeven_base: float = float("nan")     # 2*lam*S*Gamma -- every fill below this loses
    binds_pct: float = float("nan")          # PERCENT units 0-100, not a fraction:
                                             # 99.2 means "binds on 99.2% of nights".
                                             # Named _pct and holding a percent, unlike
                                             # HedgeRule.band_pct which is a fraction --
                                             # format with :.0f}% and never :.0%
    est_cost_night: float = float("nan")     # quote ccy, expected transaction cost
    # --- provenance and safety ---
    candidates: tuple[CapCandidate, ...] = ()
    warnings: tuple[str, ...] = ()
    refusals: tuple[str, ...] = ()
    unassessable: tuple[str, ...] = ()
    overridden: bool = False
    override_base: float = float("nan")
    default_rule_cap: float = float("nan")   # what 15% of gross notional would have said
    report_ccy: str = "USD"
    fx_to_report: float = 1.0
    asymmetry_note: str = ""
    profile_source: str = ""
    #: True when the smallest clip the user can deal is bigger than the delta the book
    #: accumulates in a whole one-sigma night: there is no ladder to leave, and a cap
    #: is not the thing that is wrong.
    no_ladder: bool = False

    @property
    def cap_mm(self) -> float:
        return self.cap_base / 1e6

    @property
    def is_symmetric(self) -> bool:
        hi = max(abs(self.cap_up), abs(self.cap_dn))
        return hi <= 0 or abs(self.cap_up - self.cap_dn) / hi < 0.02

    @property
    def refused(self) -> bool:
        return bool(self.refusals)

    def as_dict(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k != "candidates"}
        out["candidates"] = [c.as_dict() for c in self.candidates]
        out["cap_mm"] = self.cap_mm
        return out

    def __str__(self) -> str:                        # pragma: no cover - display
        return self.reasoning


# --------------------------------------------------------------------------- #
# the pieces every rule needs
# --------------------------------------------------------------------------- #
def overnight_sigma(bg: BookGamma, window: PassiveWindow, *,
                    range_forecast: Any | None = None) -> tuple[float, str]:
    """Standard deviation of the log spot move over the window, and its basis.

    **On the 252-day trading clock**, because that is the clock spot variance is
    delivered on -- ``sigma * sqrt(var_fraction / 252)``.  Using 365 here (as the PM's
    candidate table did) understates it by ``sqrt(365/252) = 1.2033``, i.e. 20.8%, and
    since two of the three candidate rules are linear in sigma and one is inverse in
    it, the error does not cancel.  ``docs/09`` s1 "two clocks, on purpose".

    A ``range_forecast`` (anything exposing ``sigma_window``, e.g. the frozen
    ``signals.rangeforecast.RangeForecast``) overrides it and is preferred, because it
    already contains the event term and the HAR-RV information.  Absent-safe.
    """
    if range_forecast is not None:
        s = float(getattr(range_forecast, "sigma_window", range_forecast))
        return s, f"range forecast sigma_window {s * 100:.3f}%"
    vf = max(float(window.var_fraction), 1e-12)
    s = float(bg.sigma) * math.sqrt(vf / TRADING_DAYS)
    return s, (f"ATM {bg.sigma * 100:.2f}% x sqrt({vf:.4f}/252) = {s * 100:.4f}% "
               f"-- session-scaled implied on the TRADING clock, not a forecast")


def accumulated_delta(book: Book, mkt: MarketSnapshot, pair: str, *,
                      sigma_window: float, n_sigma: float = 1.0,
                      report_ccy: str = "USD",
                      marks: Mapping[str, float] | None = None,
                      sticky: str = "strike") -> tuple[float, float, float]:
    """Delta the book actually accumulates over +/- ``n_sigma`` window sigmas.

    Read off a **full repricing** (:func:`fxgamma.portfolio.risk.spot_ladder`), never
    ``Gamma_1pct x move``.  On a symmetric ATM straddle the two agree to ~2%; on a
    **skewed** book they do not, and the up-side and down-side numbers are genuinely
    different sizes.  That asymmetry is the book, not a view (docs/11 s9.3), and it is
    the whole reason this returns two numbers.

    Returns ``(up, dn, linear)`` in base ccy, all positive: the delta gained on the way
    up, the delta gained on the way down, and what ``|Gamma| * n_sigma * sigma * S``
    would have said, so a caller can see the size of the repricing correction.
    """
    S = float(mkt.spot[pair])
    m = float(n_sigma) * float(sigma_window)
    span = max(100.0 * (math.exp(2.5 * m) - 1.0), 0.25)
    lad = spot_ladder(book, mkt, pair, lo_pct=-span, hi_pct=span, n=401,
                      sticky=sticky, report_ccy=report_ccy, marks=marks)
    Sg = lad["spot"].to_numpy(float)
    Dg = lad["delta_base"].to_numpy(float)
    d0 = float(np.interp(S, Sg, Dg))
    up = abs(float(np.interp(S * math.exp(+m), Sg, Dg)) - d0)
    dn = abs(float(np.interp(S * math.exp(-m), Sg, Dg)) - d0)
    g = float(np.interp(S, lad["spot"].to_numpy(float), lad["gamma_fd"].to_numpy(float)))
    return up, dn, abs(g) * m * S


def breakeven_clip(bg: BookGamma, lam: float) -> float:
    """``2 * lambda * S * |Gamma|`` -- the clip whose round trip costs what it captures.

    A rehedge triggered by a move ``h`` converts ``0.5*|Gamma|*h^2`` and pays
    ``lambda*S*|Gamma|*h``, so it loses money outright for ``h < 2*lambda*S``
    **regardless of risk aversion** (``docs/09`` s4.3).  In delta units that is this
    number.  It is a hard floor under every rule below, and on this user's cost tier
    it is not small: EUR 0.175mm on the reference book at 5bp.
    """
    return 2.0 * float(lam) * float(bg.spot) * abs(float(bg.gamma))


def tail_optimal_cap(bg: BookGamma, lam: float, sigma_window: float, *,
                     z: float = TAIL_MULT_95) -> float:
    """The cap that MINIMISES the 5% tail of the night -- the dominance floor.

    Derived (``docs/13`` s4.4), and it is a genuinely different object from every band
    formula in ``bandopt``.  The 5% tail of a long-gamma night is the *quiet* night, so
    what the tail wants is not less variance but **more of the window's quadratic
    variation actually converted**.  Splitting the night into ``n = V/h^2`` legs, the
    captured ``sum(leg^2)`` concentrates as ``chi^2_n / n``, so

    ``tail(h) ~ 0.5*G*V - (z/sqrt2)*G*sqrt(V)*h - lambda*S*G*V/h - theta_w``

    whose stationary point is ``h* = S * sqrt(sqrt(2)/z * lambda * sigma_window)`` --
    the **geometric mean of the spread and the window sigma**, where the mean-variance
    band is a cube root of cost over risk aversion.  ``D* = |Gamma| * h*``.

    Measured against the backtester (``docs/13`` s4.4): 0.051 / 0.081 / 0.114 / 0.180 /
    0.255mm predicted against 0.030 / 0.070 / 0.100 / 0.200 / 0.280mm measured at
    0.2 / 0.5 / 1 / 2.5 / **5**bp.  It is good where it matters and **runs 30-45% light
    above 10bp**, where the chi-square approximation for a handful of legs gives out;
    above that tier trust the sweep, not the formula.

    Below ``D*`` a tighter cap makes the expected P&L **and** the tail worse, so the
    region under it is dominated and no preference can justify it.
    """
    if not (lam > 0 and sigma_window > 0) or bg.gamma == 0:
        return float("nan")
    h = bg.spot * math.sqrt(math.sqrt(2.0) / float(z) * float(lam) * float(sigma_window))
    return abs(bg.gamma) * h


def _two_sided_first_passage(a: float, sd: float, terms: int = 12) -> float:
    """P(sup |W_t| >= a) over a window whose terminal sd is ``sd``, driftless.

    ``1 - (4/pi) sum_k (-1)^k/(2k+1) exp(-(2k+1)^2 pi^2 sd^2 / (8 a^2))``.  Exact for
    a driftless Brownian motion; the window's risk-neutral drift is under half a pip
    on a EURUSD night, so it is not approximated away, it is negligible.  Used only to
    print *how often the cap binds*, and validated against the sweep in ``docs/13`` s4.6.
    """
    if not (a > 0 and sd > 0):
        return 0.0 if a > 0 else 1.0
    q = math.pi * math.pi * sd * sd / (8.0 * a * a)
    s = 0.0
    for k in range(int(terms)):
        s += (-1.0) ** k / (2 * k + 1) * math.exp(-((2 * k + 1) ** 2) * q)
    return float(min(max(1.0 - 4.0 / math.pi * s, 0.0), 1.0))


def _analytic_cost(bg: BookGamma, lam: float, sigma_window: float, cap: float) -> float:
    """``lambda * S * Gamma^2 * V / D`` -- expected transaction cost of a cap-``D`` ladder.

    Continuous monitoring.  Measured against the backtester it runs **15-25% above**
    the discretely-monitored simulation (``docs/13`` s4.2, and the same gap
    ``docs/09`` s4.7 records for the band sweep), because a bar grid misses crossings.
    Reported unfudged, as the upper bound it is.
    """
    if not (cap > 0) or not np.isfinite(cap):
        return 0.0
    V = (bg.spot * float(sigma_window)) ** 2
    return float(lam) * bg.spot * bg.gamma * bg.gamma * V / float(cap)


# --------------------------------------------------------------------------- #
# the rules
# --------------------------------------------------------------------------- #
def cap_candidates(book: Book, mkt: MarketSnapshot, pair: str, *,
                   bg: BookGamma | None = None,
                   window: PassiveWindow | None = None,
                   sigma_window: float | None = None,
                   cost_bp: float | None = None, cost_tier: str = "retail",
                   loss_limit: float | None = None,
                   morning_hours: float = MORNING_HOURS,
                   premium_share: float = 0.10,
                   report_ccy: str = "USD",
                   marks: Mapping[str, float] | None = None,
                   range_forecast: Any | None = None,
                   sticky: str = "strike") -> list[CapCandidate]:
    """Every rule's answer for this book, with its arithmetic and its assumption.

    The three PM candidates (R1, R2, R3), the two better-founded ones (R4 tail budget,
    R5 premium at risk), the short-gamma loss-limit rule (R6), and the cost floor (F).
    All of them read the **repriced** delta profile for their up-side and down-side
    numbers, so a skewed book gets two different answers and the reason is printed.

    Scaling, which is the part that has to stay right as the book changes -- at the
    money, with ``Gamma_1pct = 0.3989 * N_gross / (sigma sqrt(T))``:

    ``R1 = 0.3989 * N_gross * sqrt(vf / (252 T))`` and
    ``R3 = 0.1995 * N_gross * D_cal / (365 * sqrt(T) * sqrt(vf/252))``.

    **Both are exactly independent of the vol level**, because ``Gamma`` falls as
    ``1/sigma`` and the window sigma rises as ``sigma``, and the same cancellation
    happens in theta.  Verified numerically over a +/-1 vol point mark error
    (``docs/13`` s4.7): R1/R2/R3 move by less than 0.1%.  For a user who cannot mark to
    a broker curve that is the most useful property in this module -- the one input
    they cannot get right is the one input the cap does not need.

    ``R3 / R2 = 252 * D_cal / (365 * vf)`` is the night's theta-per-day-of-variance
    ratio, 1.05 on a EURUSD weeknight.  R2 and R3 are the same rule under two anchors
    and they coincide when the night is carry-neutral; when they diverge, the night is
    expensive and the divergence is telling you so.
    """
    spec = pair_spec(pair)
    S = float(mkt.spot[pair])
    bg = bg if bg is not None else book_gamma(book, mkt, pair, marks=marks)
    win = window or passive_window(mkt.asof, pair=pair)
    if sigma_window is None:
        sigma_window, _ = overnight_sigma(bg, win, range_forecast=range_forecast)
    cbp = float(cost_bp) if cost_bp is not None else cost_bp_for(pair, cost_tier)
    lam = cbp / 2.0 / 1e4
    G = abs(bg.gamma)
    if G <= 0:
        return []
    pip = spec.pip
    sd_spot = S * sigma_window
    theta_w = bg.theta * win.calendar_days                  # signed, quote ccy
    gpnl_w = 0.5 * bg.gamma * sd_spot ** 2                  # signed, quote ccy
    up, dn, lin = accumulated_delta(book, mkt, pair, sigma_window=sigma_window,
                                    report_ccy=report_ccy, marks=marks, sticky=sticky)

    def band(c: float) -> float:
        return c / G / pip

    out: list[CapCandidate] = []
    out.append(CapCandidate(
        "sigma_accumulation", min(up, dn), up, dn,
        basis=(f"repriced delta over +/-1 window sigma ({sigma_window * 100:.4f}% = "
               f"{sd_spot / pip:,.1f} pips): up {up / 1e6:,.3f}mm, down {dn / 1e6:,.3f}mm "
               f"(linear |Gamma|*sigma*S would say {lin / 1e6:,.3f}mm)"),
        assumption=("that you are content to be hedged only once per one-sigma night. "
                    "It is a SCALE, not an optimum -- nothing in it trades cost against "
                    "risk, and the measured efficient zone sits below it."),
        scaling="0.3989 * gross_notional * sqrt(vf / (252 T)).  Linear in notional, "
                "1/sqrt(tenor), INDEPENDENT OF VOL, sqrt of the window's variance share.",
        band_pips=band(min(up, dn))))
    out.append(CapCandidate(
        "delta_equals_gamma", 0.5 * min(up, dn), 0.5 * up, 0.5 * dn,
        basis=(f"half of R1 = {0.5 * min(up, dn) / 1e6:,.3f}mm.  At that delta a further "
               f"one-sigma move is worth {0.5 * min(up, dn) * sd_spot:,.0f} "
               f"{spec.quote} against the night's whole gamma P&L of {abs(gpnl_w):,.0f}"),
        assumption=("that the directional bet you did not choose should not be bigger "
                    "than the convexity bet you did. Needs no loss limit, no risk "
                    "aversion and no view on how long you cannot deal."),
        scaling="exactly R1/2, so identical scaling.",
        band_pips=band(0.5 * min(up, dn))))
    r3 = abs(theta_w) / max(sd_spot, 1e-12)
    ratio = (TRADING_DAYS * win.calendar_days
             / (365.0 * max(win.var_fraction, 1e-12)))
    out.append(CapCandidate(
        "theta_anchored", r3, r3 * (up / max(min(up, dn), 1e-9)),
        r3 * (dn / max(min(up, dn), 1e-9)),
        basis=(f"window theta {abs(theta_w):,.0f} {spec.quote} / (sigma_on * S = "
               f"{sd_spot:.6f}) = {r3 / 1e6:,.3f}mm.  R3/R2 = 252*D_cal/(365*vf) = "
               f"{ratio:.3f}, the night's theta per day of variance"),
        assumption=("that the unchosen bet should be no bigger than the bill you have "
                    "already agreed to pay tonight. Same rule as R2 with the anchor "
                    "moved from the gamma to the theta; they coincide at the crossover "
                    "vol and diverge exactly as much as the night's carry does."),
        scaling="0.1995 * gross_notional * D_cal / (365 * sqrt(T) * sqrt(vf/252)). "
                "Also INDEPENDENT OF VOL. Inverse in the variance share where R1/R2 "
                "are direct in it -- a quieter night wants a LOOSER theta-anchored cap.",
        band_pips=band(r3)))
    # R4 -- tail budget
    L = float(loss_limit) if loss_limit else abs(theta_w)
    sd_m = bg.sigma * math.sqrt(max(morning_hours, 1e-9) / 24.0 / TRADING_DAYS)
    r4 = L / max(TAIL_MULT_95 * sd_m * S, 1e-12)
    out.append(CapCandidate(
        "tail_budget", r4, r4 * (up / max(min(up, dn), 1e-9)),
        r4 * (dn / max(min(up, dn), 1e-9)),
        basis=(f"budget {L:,.0f} {spec.quote} "
               f"({'user loss limit' if loss_limit else 'default: the window theta'}) "
               f"/ (CVaR mult {TAIL_MULT_95:.3f} x {morning_hours:g}h sigma "
               f"{sd_m * 100:.4f}% x S) = {r4 / 1e6:,.3f}mm"),
        assumption=(f"that you genuinely cannot deal for {morning_hours:g}h after the "
                    "open, that the move over that hour is normal, and that a 95% CVaR "
                    "is the tail you care about. MEASURED to barely matter on a "
                    "long-gamma book (docs/13 s4.5) -- the gamma that created the delta "
                    "also insures it -- and to matter a great deal on a short one."),
        scaling="linear in the loss budget, inverse in the unhedgeable-horizon sigma. "
                "NOT vol-invariant: it falls as 1/sigma_morning.",
        band_pips=band(r4)))
    return out


def _premium_at_risk(book: Book, mkt: MarketSnapshot, pair: str, *,
                     marks: Mapping[str, float] | None = None) -> float:
    """Absolute option PV of the pair's live legs, quote ccy.  A stock, not a flow."""
    from .risk import price_book                       # local: avoids an import cycle
    df = price_book(book.filter(pair), mkt, marks=marks)
    if not len(df):
        return 0.0
    live = df[~df["expired"].astype(bool)]
    live = live[live["kind"] == "option"] if "kind" in live.columns else live
    return float(live["pv"].abs().sum()) if len(live) else 0.0


def _gamma_sign_flips(book: Book, mkt: MarketSnapshot, pair: str, *, sigma_window: float,
                      marks: Mapping[str, float] | None = None) -> bool:
    """True when the book is long gamma on one side of spot and short on the other.

    The trader's point (docs/11 s3.1): deriving the order type -- and therefore the
    whole meaning of the cap -- from a single book-level gamma sign puts a limit where
    a stop belongs.  A one-sided strike ladder does this routinely.
    """
    S = float(mkt.spot[pair])
    span = max(100.0 * (math.exp(3.0 * sigma_window) - 1.0), 0.3)
    lad = spot_ladder(book, mkt, pair, lo_pct=-span, hi_pct=span, n=201,
                      sticky="strike", marks=marks)
    g = lad["gamma_fd"].to_numpy(float)
    g = g[np.isfinite(g)]
    return bool(len(g) and g.max() > 0 and g.min() < 0
                and min(abs(g.max()), abs(g.min())) > 0.02 * max(abs(g.max()), abs(g.min())))


def gap_loss(bg: BookGamma, *, cap: float, gap_spot: float, lam: float,
             slip_pips: float, pip: float) -> dict[str, float]:
    """Loss on a short-gamma book through a jump of ``gap_spot``, with stops at ``cap``.

    Derived, because it is the number the short-gamma cap has to be solved against and
    it is not the same shape as anything in ``bandopt``.  Stopping out in clips of
    ``D`` through a move ``g`` cuts the move into ``n = g / h`` legs with
    ``h = D / |Gamma|``, and the un-hedged quadratic loss left inside the legs is

    ``n * 0.5 |Gamma| h^2  =  0.5 * g * D``          <- LINEAR in the cap

    while the turnover is ``|Gamma| * g`` whatever the cap, so the spread and the
    slippage cost ``|Gamma| * g * (S*lambda + slip)`` and are **cap-independent**.
    Hence

    ``loss(D) = 0.5 * g * D  +  |Gamma| * g * (S*lambda + slip)``

    against ``0.5 |Gamma| g^2`` with no stops at all.  Two things fall out.  The cap is
    the *only* term you control, and it is linear, so halving the cap halves the
    convex part of the gap loss exactly.  And the slippage floor does not move, so
    below a certain position size no cap can bring the loss inside a limit -- which is
    the trader's point that the answer is then to reduce the position, not to cut the
    cap (docs/11 s5.2).

    **Optimistic by construction**: it assumes every stop fills.  In the gap you are
    actually worried about they do not, and ``docs/09`` s8 declines to model that
    rather than produce a comforting number.  Read it as a lower bound on the loss.
    """
    g = abs(float(gap_spot))
    G = abs(float(bg.gamma))
    slip = float(slip_pips) * pip
    friction = G * g * (bg.spot * float(lam) + slip)
    residual = 0.5 * g * float(cap) if np.isfinite(cap) else 0.5 * G * g * g
    naked = 0.5 * G * g * g
    return {"gap_spot": g, "gap_pips": g / pip, "residual_gamma_loss": residual,
            "friction": friction, "total": residual + friction, "naked": naked,
            "cap_free_floor": friction}


def short_gamma_cap(bg: BookGamma, *, loss_limit: float, gap_spot: float, lam: float,
                    slip_pips: float, pip: float) -> tuple[float, float]:
    """R6: the cap that keeps :func:`gap_loss` inside ``loss_limit``.

    ``D_max = 2 * (L - friction) / g``.  Returns ``(cap, position_scale)`` -- and when
    the friction alone already breaches the limit the cap is ``0.0`` and
    ``position_scale`` is the fraction of the short gamma that would have to go for the
    limit to be reachable at all.  That second number is the honest output in that
    case, and it is the one the trader asked the screen to print.
    """
    L = abs(float(loss_limit))
    gl = gap_loss(bg, cap=float("inf"), gap_spot=gap_spot, lam=lam,
                  slip_pips=slip_pips, pip=pip)
    if gl["friction"] >= L:
        return 0.0, float(L / max(gl["friction"], 1e-12))
    return 2.0 * (L - gl["friction"]) / max(abs(gap_spot), 1e-12), 1.0


def _refusals(book: Book, mkt: MarketSnapshot, pair: str, *, bg: BookGamma,
              win: PassiveWindow, sigma_window: float, cap: float, lam: float,
              slip_pips: float, loss_limit: float | None,
              marks: Mapping[str, float] | None) -> tuple[list[str], list[str]]:
    """The trader's short-gamma refusal conditions (docs/11 s5.2), as far as they are
    evaluable from the book and the market -- and an explicit list of the ones that are
    NOT, because a refusal list that silently drops the conditions it cannot check is
    worse than no refusal list.
    """
    spec = pair_spec(pair)
    S = float(mkt.spot[pair])
    ref: list[str] = []
    unk: list[str] = []
    if bg.gamma >= 0:
        return ref, unk

    prov = mkt.meta.get(f"surface.{pair}") or mkt.meta.get(f"vol.{pair}")
    src = getattr(prov, "source", "") if prov is not None else ""
    kind = getattr(prov, "kind", "") if prov is not None else ""
    if kind in ("synthetic", "user_override") or src in ("synthetic", "etf", "cboe"):
        ref.append(f"REFUSE: the vol surface for {pair} is '{src or kind or 'unverified'}' "
                   "-- you cannot size a stop off an indicative mark (docs/11 s5.2).")
    if win.spans_weekend:
        ref.append("REFUSE: the window spans a weekend. A stop resting through a "
                   "Sunday-evening gap is the most dangerous order type in FX.")
    tier3 = [e for e in win.events if "imp 3" in e]
    if tier3:
        ref.append(f"REFUSE: tier-3 event inside the window ({'; '.join(tier3)}). "
                   "A decision is a jump and a stop is the wrong instrument for a jump.")
    gap = GAP_QUANTILE_SIGMAS * sigma_window * S
    gl = gap_loss(bg, cap=cap, gap_spot=gap, lam=lam, slip_pips=slip_pips, pip=spec.pip)
    if loss_limit:
        if gl["total"] > abs(loss_limit):
            _, scale = short_gamma_cap(bg, loss_limit=loss_limit, gap_spot=gap, lam=lam,
                                       slip_pips=slip_pips, pip=spec.pip)
            fix = ("no cap can bring it inside the limit -- the slippage on the turnover "
                   f"alone is {gl['friction']:,.0f} {spec.quote}. Cut the short gamma to "
                   f"{scale * 100:.0f}% of its size, or buy the wing."
                   if scale < 1.0 else
                   "tighten the cap, cut the position, or buy the wing.")
            ref.append(f"REFUSE: the {GAP_QUANTILE_SIGMAS:g}-sigma gap ({gap / spec.pip:,.0f} "
                       f"pips) costs {gl['total']:,.0f} {spec.quote} with the stops filling "
                       f"and slipped {slip_pips:.1f} pips, against your stated limit "
                       f"{abs(loss_limit):,.0f}. {fix}")
    else:
        unk.append(f"NOT CHECKED: no overnight loss limit supplied. The "
                   f"{GAP_QUANTILE_SIGMAS:g}-sigma gap ({gap / spec.pip:,.0f} pips) costs "
                   f"{gl['total']:,.0f} {spec.quote} with the stops filling "
                   f"({gl['naked']:,.0f} with no stops at all); supply `loss_limit=` and "
                   "this becomes a refusal condition rather than a number to read.")
    unk.append("NOT CHECKABLE HERE: pin risk at the next cut (use zones.pin_risk), the "
               "outermost clip against what the pair trades at 03:00, and whether your "
               "platform offers stop-market or only stop-limit. All three are refusal "
               "conditions in docs/11 s5.2 and none is derivable from the book.")
    unk.append("AND the gap number above assumes every stop FILLS. In the gap you are "
               "actually worried about it does not, so read it as a lower bound.")
    return ref, unk


# --------------------------------------------------------------------------- #
# the public entry point
# --------------------------------------------------------------------------- #
def recommend_cap(book: Book, mkt: MarketSnapshot, pair: str, *,
                  override: float | None = None,
                  window: PassiveWindow | None = None,
                  cost_bp: float | None = None, cost_tier: str = "retail",
                  loss_limit: float | None = None,
                  morning_hours: float = MORNING_HOURS,
                  min_clip_base: float = RETAIL_LOT_BASE,
                  range_forecast: Any | None = None,
                  profile: Any | None = None,
                  events: "pd.DataFrame | None" = None,
                  report_ccy: str = "USD",
                  marks: Mapping[str, float] | None = None,
                  sticky: str = "strike",
                  rule: str | None = None,
                  slip_pips: float | None = None) -> DeltaCap:
    """The overnight delta cap for one pair, derived from the book.  Never typed.

    Hand ``DeltaCap.cap_base`` to ``overnight_ladder(..., max_overnight_delta=)`` and
    print ``DeltaCap.reasoning``.  ``override=`` substitutes the user's own number and
    keeps every derived figure alongside it so :func:`override_effect` can say what
    changed.

    Which rule
    ----------
    * **Long gamma** -> ``delta_equals_gamma`` (R2), clamped below by the dominance
      floor (:func:`tail_optimal_cap`) and the smallest dealable clip, and above by the
      one-sigma accumulation (R1).  R2 is chosen over R3 and R4 because it needs the
      fewest assumptions -- no loss limit, no unhedgeable horizon, no risk aversion --
      and because it is exactly invariant to the vol mark, which this user cannot get
      right.  On the reference book it lands at EUR 0.54mm, inside the measured
      efficient zone of EUR 0.40-0.75mm (``docs/13`` s4.3).
    * **Short gamma** -> ``tail_budget`` (R4) against the user's ``loss_limit`` if they
      gave one, else R2, and the refusal conditions run first.  The cap means something
      different here: not being filled is the loss, the orders are stops, they slip, and
      the tail responds to the cap by thousands rather than hundreds.
    * **Gamma that changes sign across the window** -> a single scalar cap is the wrong
      object and the warning says so.

    What the answer is NOT
    ----------------------
    An optimum.  Measured on 40,000 nights through the shipped pricer and the shipped
    cost accounting, the cap's effect on the **expected** overnight P&L is exactly minus
    its transaction cost, and on a long-gamma book at retail cost it buys CVaR-95 at
    about **2 USD of mean per 1 USD of tail** -- an exchange rate that is roughly flat
    across the whole improving region, so there is no kink to call an optimum.  The
    honest statement, which ``reasoning`` makes, is: *this is the cheapest place on the
    frontier to buy tail, here is the price, and if you do not want to pay it the answer
    is a much wider cap.*  The one preference-free statement available is the floor:
    below :func:`tail_optimal_cap` a tighter cap makes the mean **and** the tail worse.
    """
    spec = pair_spec(pair)
    S = float(mkt.spot[pair])
    bg = book_gamma(book, mkt, pair, marks=marks)
    win = window or passive_window(mkt.asof, pair=pair, profile=profile, events=events)
    fq = fx_rate(spec.quote, report_ccy, mkt)
    cbp = float(cost_bp) if cost_bp is not None else cost_bp_for(pair, cost_tier)
    lam = cbp / 2.0 / 1e4
    slip = float(slip_pips) if slip_pips is not None else \
        SHORT_GAMMA_SLIP_MULT * cbp / 1e4 * S / spec.pip

    if bg.gamma == 0.0 or bg.n_live == 0:
        return DeltaCap(pair=pair, cap_base=0.0, cap_up=0.0, cap_dn=0.0, rule="none",
                        spot=S, base_ccy=spec.base, ccy=spec.quote, report_ccy=report_ccy,
                        window_label=win.label, cost_bp=cbp, cost_tier=cost_tier,
                        reasoning=(f"{pair}: no live gamma in this pair tonight, so there "
                                   "is nothing to cap and no ladder to leave."))

    sigma_window, sig_basis = overnight_sigma(bg, win, range_forecast=range_forecast)
    sd_spot = S * sigma_window
    G = abs(bg.gamma)
    theta_w = bg.theta * win.calendar_days
    gpnl_w = 0.5 * bg.gamma * sd_spot ** 2
    carry = gpnl_w + theta_w
    long_gamma = bg.gamma > 0
    side = "long" if long_gamma else "short"

    cands = cap_candidates(book, mkt, pair, bg=bg, window=win, sigma_window=sigma_window,
                           cost_bp=cbp, cost_tier=cost_tier, loss_limit=loss_limit,
                           morning_hours=morning_hours, report_ccy=report_ccy,
                           marks=marks, range_forecast=range_forecast, sticky=sticky)
    by = {c.rule: c for c in cands}
    prem = _premium_at_risk(book, mkt, pair, marks=marks)
    sd_m = bg.sigma * math.sqrt(max(morning_hours, 1e-9) / 24.0 / TRADING_DAYS)
    r5 = 0.10 * prem / max(TAIL_MULT_95 * sd_m * S, 1e-12)
    r1 = by["sigma_accumulation"]
    cands.append(CapCandidate(
        "premium_at_risk", r5, r5 * (r1.cap_up / max(min(r1.cap_up, r1.cap_dn), 1e-9)),
        r5 * (r1.cap_dn / max(min(r1.cap_up, r1.cap_dn), 1e-9)),
        basis=(f"10% of premium at risk {prem:,.0f} {spec.quote} / (CVaR mult x "
               f"{morning_hours:g}h sigma x S) = {r5 / 1e6:,.3f}mm"),
        assumption="that the overnight bet should be a fixed share of what the position "
                   "already has at risk.",
        scaling="premium goes as sqrt(T) while every other rule here goes as 1/sqrt(T), "
                "so this rule moves the WRONG WAY on a tenor change -- it would give a "
                "3M book a bigger cap than a 1W one. Reported for scale; not a default.",
        band_pips=r5 / G / spec.pip))
    by = {c.rule: c for c in cands}

    gap_spot = GAP_QUANTILE_SIGMAS * sigma_window * S
    if not long_gamma:
        L6 = abs(float(loss_limit)) if loss_limit else abs(theta_w) * 5.0
        r6, pos_scale = short_gamma_cap(bg, loss_limit=L6, gap_spot=gap_spot, lam=lam,
                                        slip_pips=slip, pip=spec.pip)
        gl = gap_loss(bg, cap=r6 if r6 > 0 else float("inf"), gap_spot=gap_spot,
                      lam=lam, slip_pips=slip, pip=spec.pip)
        cands.append(CapCandidate(
            "short_gamma_loss_limit", r6,
            r6 * (r1.cap_up / max(min(r1.cap_up, r1.cap_dn), 1e-9)),
            r6 * (r1.cap_dn / max(min(r1.cap_up, r1.cap_dn), 1e-9)),
            basis=(f"loss(D) = 0.5*g*D + |Gamma|*g*(S*lam+slip) with g = "
                   f"{GAP_QUANTILE_SIGMAS:g} sigma = {gap_spot / spec.pip:,.0f} pips: "
                   f"cap-free slippage floor {gl['friction']:,.0f} {spec.quote}, budget "
                   f"{L6:,.0f} " + ("(user loss limit)" if loss_limit else
                                    "(default: 5x the window theta -- SUPPLY YOUR OWN)")
                   + f" -> D = {r6 / 1e6:,.3f}mm"
                   + ("" if pos_scale >= 1.0 else
                      f".  NO CAP WORKS: cut the short gamma to {pos_scale * 100:.0f}%")),
            assumption=("that every stop FILLS, which in a real gap they do not. The "
                        "residual gamma loss is exactly LINEAR in the cap, so this is "
                        "the one rule here whose tail responds to the cap one-for-one; "
                        "the slippage term does not respond to it at all."),
            scaling="linear in the loss budget, inverse in the gap size, and it does NOT "
                    "scale with gamma except through the slippage floor -- which is why "
                    "a bigger short-gamma position is fixed by selling it, not by capping.",
            band_pips=r6 / G / spec.pip if r6 > 0 else float("nan")))
        by = {c.rule: c for c in cands}

    floor_tail = tail_optimal_cap(bg, lam, sigma_window)
    be = breakeven_clip(bg, lam)
    clip_floor = float(min_clip_base)
    floor = float(np.nanmax([floor_tail, be, clip_floor]))
    ceiling_up, ceiling_dn = r1.cap_up, r1.cap_dn
    ceiling = min(ceiling_up, ceiling_dn)
    default_15 = max(DEFAULT_BAND_PCT / 100.0 * (bg.gross_notional or 0.0), 1_000_000.0)
    no_ladder = clip_floor > max(ceiling_up, ceiling_dn)

    chosen = rule or ("short_gamma_loss_limit" if not long_gamma else "delta_equals_gamma")
    base = by[chosen]
    if no_ladder:
        cap_up, cap_dn = ceiling_up, ceiling_dn
    else:
        cap_up = float(np.clip(base.cap_up, min(floor, ceiling_up), max(ceiling_up, floor)))
        cap_dn = float(np.clip(base.cap_dn, min(floor, ceiling_dn), max(ceiling_dn, floor)))
    cap = min(cap_up, cap_dn)
    clamped = ""
    if no_ladder:
        clamped = ""
    elif base.cap_base < floor - 1e-9:
        which = ("tail-optimal" if floor_tail >= max(be, clip_floor)
                 else "round-trip breakeven" if be >= clip_floor
                 else "smallest dealable clip")
        clamped = (f"clamped UP to the dominance floor {floor / 1e6:,.3f}mm ({which})")
    elif base.cap_base > ceiling + 1e-9:
        clamped = f"clamped DOWN to the one-sigma accumulation {ceiling / 1e6:,.3f}mm"

    ref, unk = _refusals(book, mkt, pair, bg=bg, win=win, sigma_window=sigma_window,
                         cap=cap, lam=lam, slip_pips=slip, loss_limit=loss_limit,
                         marks=marks)

    overridden = override is not None and float(override) > 0
    if overridden:
        cap_used = float(override)
        r = cap_used / max(cap, 1e-9)
        cap_up, cap_dn = cap_up * r, cap_dn * r
    else:
        cap_used = cap

    warn: list[str] = []
    if default_15 > 0:
        warn.append(
            f"zones.hedge_bands' desk default of {DEFAULT_BAND_PCT:g}% of gross notional "
            f"is {default_15 / 1e6:,.2f}mm here -- {default_15 / max(cap_used, 1e-9):.1f}x this "
            f"cap and {default_15 / max(ceiling, 1e-9):.1f}x the delta the UNHEDGED book "
            "accumulates in a whole one-sigma night. Overnight it can never bind. "
            "Measured (docs/13 s4.8): it costs USD 10/night and improves CVaR-95 by "
            "zero, i.e. it is strictly dominated by having no cap at all. It is an "
            "intraday default; do not use it for the night.")
    if win.profile_source != "estimated":
        warn.append(f"the hour-of-day variance profile is '{win.profile_source}' -- a "
                    "MODELLED default, not measured. Every sigma here scales with it; "
                    "run overnight.estimate_hour_profile on your own hourly bars.")
    if win.spans_weekend:
        warn.append(f"this window spans a weekend: {win.calendar_days:.2f} calendar days "
                    f"of theta against {win.var_fraction:.3f} of a day's variance. Size "
                    "it as a different trade from a Tuesday night.")
    if cost_bp is None:
        warn.append(f"cost is the shipped {cost_tier} default {cbp:g}bp "
                    f"({cbp / 1e4 * S / spec.pip:.2f} pips round trip). It is an "
                    "ASSUMPTION and it owns this answer: at 1bp the cap costs "
                    "~USD 107/night and buys tail at 0.31 USD of mean per USD of tail; "
                    "at 5bp it costs ~USD 533 and the rate is 2.03. Put your broker's "
                    "own number in `cost_bp=`.")
    # QA-5: the cap's vol-mark invariance is an AT-THE-MONEY property. Away from the
    # money it is not invariant at all -- moving the mark 6% -> 12% moves the cap by
    # 1.57x at 2% out and 3.20x at 4% out. That matters because this user has no OTC
    # access, and docs/13 previously sold mark-invariance as the reason they could
    # trust the cap without a broker curve. Say so when the book is not ATM.
    try:
        _spot = float(mkt.spot[pair])
        _mny = [abs(o.strike / _spot - 1.0) for o in book.options
                if o.pair == pair and _spot > 0]
        _far = max(_mny) if _mny else 0.0
        if _far > 0.01:
            warn.append(
                f"this book's furthest strike is {_far:.1%} from spot. The cap is only "
                f"vol-mark invariant AT THE MONEY: at {_far:.0%} out, a 6%->12% mark "
                f"error moves the cap by roughly "
                f"{1.57 if _far < 0.03 else 3.2:.2f}x. Without an OTC curve to mark "
                f"against, treat this cap as mark-dependent and check it against your "
                f"own vol view (QA-5, docs/13 s4.7).")
    except Exception:                                 # never let a warning break sizing
        pass
    if _gamma_sign_flips(book, mkt, pair, sigma_window=sigma_window, marks=marks):
        warn.append("this book is LONG gamma on one side of spot and SHORT on the other. "
                    "A single scalar cap is the wrong object: the order type is derived "
                    "per rung from the local gamma sign (docs/11 s3.1), and the two "
                    "sides mean different things. Use cap_up/cap_dn and read the "
                    "per-rung order types off overnight_ladder.")
    if not long_gamma:
        warn.append("SHORT GAMMA: these are STOPS, they will be slipped, and an unfilled "
                    "order is the loss rather than the safe outcome. The cap is doing "
                    "real work here -- measured CVaR-95 runs from USD -8,775 uncapped to "
                    "USD -2,558 capped on the reference book (docs/13 s4.9) -- but with "
                    f"realistic slippage ({slip:.1f} pips) most of the tightening is "
                    "whipsaw and the honest answers are to reduce the position or buy "
                    "the wing, not to cut the cap.")

    asym = ""
    if max(cap_up, cap_dn) > 0 and abs(cap_up - cap_dn) / max(cap_up, cap_dn) >= 0.02:
        bigger, smaller = ("up", "down") if cap_up > cap_dn else ("down", "up")
        asym = (f"{smaller}-side cap {min(cap_up, cap_dn) / 1e6:,.3f}mm is "
                f"{100 * (1 - min(cap_up, cap_dn) / max(cap_up, cap_dn)):.0f}% smaller than "
                f"the {bigger}-side {max(cap_up, cap_dn) / 1e6:,.3f}mm because your book "
                f"accumulates less delta on the way {smaller}. THAT IS YOUR STRIKES, NOT "
                "A VIEW -- it is read off a full repricing, and the linear "
                f"|Gamma|*sigma*S proxy would have said {r1.cap_base / 1e6:,.3f}mm on "
                "both sides.")

    binds = _two_sided_first_passage(cap_used / G, sd_spot)
    est_cost = _analytic_cost(bg, lam, sigma_window, cap_used)

    dc = DeltaCap(
        pair=pair, cap_base=cap_used, cap_up=cap_up, cap_dn=cap_dn,
        rule=("user override" if overridden else chosen), reasoning="",
        spot=S, base_ccy=spec.base, ccy=spec.quote, gamma=bg.gamma,
        gamma_1pct=bg.gamma_1pct, gamma_side=side, sigma_atm=bg.sigma,
        sigma_window=sigma_window, var_fraction=win.var_fraction,
        calendar_days=win.calendar_days, window_label=win.label,
        theta_window=theta_w, gamma_pnl_window=gpnl_w, carry=carry,
        cost_bp=cbp, cost_tier=cost_tier,
        band_pips=cap_used / G / spec.pip,
        band_pips_up=cap_up / G / spec.pip, band_pips_dn=cap_dn / G / spec.pip,
        floor_base=floor, ceiling_base=ceiling, clip_floor_base=clip_floor,
        breakeven_base=be, binds_pct=100.0 * binds, est_cost_night=est_cost,
        candidates=tuple(cands), warnings=tuple(warn), refusals=tuple(ref),
        unassessable=tuple(unk), overridden=overridden,
        override_base=float(override) if overridden else float("nan"),
        default_rule_cap=default_15, report_ccy=report_ccy, fx_to_report=fq,
        asymmetry_note=asym, profile_source=win.profile_source,
        no_ladder=bool(no_ladder))
    return _with_reasoning(dc, sig_basis, clamped, floor_tail, be, clip_floor)


def _with_reasoning(dc: DeltaCap, sig_basis: str, clamped: str,
                    floor_tail: float, be: float, clip_floor: float) -> DeltaCap:
    """Attach the human-readable string.  This is the whole point of the module."""
    from dataclasses import replace
    q, b = dc.ccy, dc.base_ccy
    by = {c.rule: c for c in dc.candidates}
    lines: list[str] = []
    if dc.refusals:
        lines += ["!" * 78] + [f"  {r}" for r in dc.refusals] + ["!" * 78, ""]
    if dc.no_ladder:
        lines += [
            f"NO OVERNIGHT LADDER for {dc.pair}. The smallest clip you can deal "
            f"({dc.clip_floor_base / 1e6:,.2f}mm) is larger than the delta this book",
            f"accumulates over a WHOLE one-sigma night ({max(dc.cap_up, dc.cap_dn) / 1e6:,.3f}mm "
            f"up / {min(dc.cap_up, dc.cap_dn) / 1e6:,.3f}mm down). There is nothing to hedge",
            "overnight and no cap will change that -- this is not a gamma position tonight.",
            ""]
    lines += [
        f"{dc.pair} overnight delta cap: {b} {dc.cap_mm:,.2f}mm "
        f"({dc.band_pips:,.1f} pips of spot on this book)"
        + ("  [YOUR OVERRIDE]" if dc.overridden else f"  [rule: {dc.rule}]"),
        "",
        f"  window     {dc.window_label}, {dc.calendar_days:.3f} calendar days of theta "
        f"for {dc.var_fraction:.3f} of a day's variance "
        f"({TRADING_DAYS * dc.calendar_days / (365.0 * max(dc.var_fraction, 1e-9)):.2f}x "
        f"theta per day of variance)",
        f"  sigma      {sig_basis} = {dc.sigma_window * dc.spot / pair_spec(dc.pair).pip:,.1f} pips",
        f"  book       {'LONG' if dc.gamma_side == 'long' else 'SHORT'} gamma "
        f"{dc.gamma_1pct / 1e6:+,.2f}mm per 1%   window gamma P&L {dc.gamma_pnl_window:+,.0f} {q}"
        f"   theta {dc.theta_window:+,.0f} {q}   carry {dc.carry:+,.0f} {q}",
        "",
        "  why this number",
    ]
    if not dc.overridden:
        lines.append(f"    {CAP_RULES.get(dc.rule, dc.rule)}")
        c = by.get(dc.rule)
        if c is not None:
            lines.append(f"    arithmetic: {c.basis}")
            lines.append(f"    assumes:    {c.assumption}")
        if clamped:
            lines.append(f"    {clamped}")
    else:
        lines.append(f"    you set it. The derived rule would have said "
                     f"{by['delta_equals_gamma'].cap_base / 1e6:,.2f}mm.")
    lines += [
        "",
        "  the range that is actually defensible",
        f"    floor   {b} {dc.floor_base / 1e6:,.2f}mm  -- below this a tighter cap makes the "
        f"EXPECTED P&L and the TAIL both worse, so no preference justifies it",
        f"              (tail-optimal {floor_tail / 1e6:,.3f}mm; round-trip breakeven clip "
        f"{be / 1e6:,.3f}mm; smallest dealable clip {clip_floor / 1e6:,.3f}mm)",
        f"    ceiling {b} {dc.ceiling_base / 1e6:,.2f}mm  -- the delta the UNHEDGED book "
        f"accumulates over one whole overnight sigma; a cap above it cannot bind",
        f"    measured efficient zone {b} {dc.cap_base * EFFICIENT_LO_MULT / 1e6:,.2f}"
        f"-{dc.cap_base * EFFICIENT_HI_MULT / 1e6:,.2f}mm -- inside it, one USD of 5%-tail "
        f"costs under 1.5 USD of expected P&L; outside it, 5-6x that",
        "",
        "  what it costs and what it binds",
        f"    expected transaction cost   {dc.est_cost_night:,.0f} {q} a night "
        f"(continuous monitoring; a discrete night measures 15-25% below this)",
        f"    cap binds on about          {dc.binds_pct:.0f}% of nights "
        f"(two-sided first passage; within 4pp of the sweep and always a little\n                                 above it, because continuous monitoring catches crossings a\n                                 5-minute grid misses -- docs/13 s4.6)",
        f"    the cap's effect on the EXPECTED night is exactly minus that cost. It buys "
        f"tail, not P&L.",
    ]
    if dc.asymmetry_note:
        lines += ["", f"  asymmetry: {dc.asymmetry_note}"]
    if dc.candidates:
        lines += ["", "  every rule, for comparison"]
        for c in dc.candidates:
            lines.append(f"    {c.rule:22s} {c.cap_base / 1e6:7,.3f}mm "
                         f"({c.band_pips:6,.1f}p)   up {c.cap_up / 1e6:6,.3f} / "
                         f"dn {c.cap_dn / 1e6:6,.3f}")
    if dc.warnings:
        lines += ["", "  warnings"] + [f"    - {w}" for w in dc.warnings]
    if dc.unassessable:
        lines += ["", "  not checked here"] + [f"    - {u}" for u in dc.unassessable]
    return replace(dc, reasoning="\n".join(lines))


def recommend_caps(book: Book, mkt: MarketSnapshot, *, pairs: Sequence[str] | None = None,
                   **kw: Any) -> dict[str, DeltaCap]:
    """:func:`recommend_cap` for every pair in the book (or the ones named)."""
    return {p: recommend_cap(book, mkt, p, **kw) for p in (pairs or book.pairs())}


def cap_frame(caps: Mapping[str, DeltaCap] | Sequence[DeltaCap]) -> pd.DataFrame:
    """One row per pair: the number, the rule, the range, what it costs and binds."""
    vals = list(caps.values()) if isinstance(caps, Mapping) else list(caps)
    return pd.DataFrame([{
        "pair": c.pair, "cap_mm": c.cap_mm, "cap_up_mm": c.cap_up / 1e6,
        "cap_dn_mm": c.cap_dn / 1e6, "rule": c.rule, "band_pips": c.band_pips,
        "gamma_side": c.gamma_side, "gamma_1pct_mm": c.gamma_1pct / 1e6,
        "sigma_window_pips": c.sigma_window * c.spot / pair_spec(c.pair).pip,
        "floor_mm": c.floor_base / 1e6, "ceiling_mm": c.ceiling_base / 1e6,
        "binds_pct": c.binds_pct, "est_cost_night": c.est_cost_night,
        "carry": c.carry, "cost_bp": c.cost_bp, "symmetric": c.is_symmetric,
        "refused": c.refused, "n_warnings": len(c.warnings),
    } for c in vals])


def override_effect(cap: DeltaCap, new_cap_base: float) -> dict[str, Any]:
    """What changes if the user types their own number instead.  Same units, no theory.

    Returns the two caps side by side with the band, the binding frequency, the cost and
    the delta they would wake up holding, plus a one-line verdict that names the region
    the new number falls in: dominated, defensible, or decorative.

    ``p95_wake_delta_mm`` is ``min(cap, 1.96 |Gamma| sd_window)`` -- an **upper bound**.
    It is exact where the cap does not bind (2.12mm against 2.1mm measured with no cap)
    and about 25% high where it does, because a fill leaves less than the cap behind.
    Bounding rather than estimating it is deliberate: this number is read to decide
    whether a cap is loose enough to be worth worrying about.
    """
    G = abs(cap.gamma)
    pip = pair_spec(cap.pair).pip
    sd = cap.sigma_window * cap.spot
    new = float(new_cap_base)
    old = cap.cap_base
    lam = cap.cost_bp / 2.0 / 1e4

    def row(c: float) -> dict[str, Any]:
        return {"cap_mm": c / 1e6, "band_pips": c / G / pip,
                "binds_pct": 100.0 * _two_sided_first_passage(c / G, sd),
                "est_cost_night": (lam * cap.spot * G * G * sd * sd / c) if c > 0 else 0.0,
                "p95_wake_delta_mm": min(c, 1.96 * abs(cap.gamma) * sd) / 1e6}

    a, b_ = row(old), row(new)
    if new < cap.floor_base:
        verdict = (f"DOMINATED: {new / 1e6:,.2f}mm is below the {cap.floor_base / 1e6:,.2f}mm "
                   "floor, where a tighter cap makes the expected P&L AND the tail worse. "
                   "There is no preference that justifies it -- it is paying spread to "
                   "make the night worse in both directions.")
    elif new > cap.ceiling_base:
        verdict = (f"DECORATIVE: {new / 1e6:,.2f}mm is above the {cap.ceiling_base / 1e6:,.2f}mm "
                   "of delta the unhedged book accumulates over a whole one-sigma night, "
                   f"so it binds on only {b_['binds_pct']:.0f}% of nights and the orders "
                   "are mostly not there. Measured: a cap this loose costs money and "
                   "improves CVaR-95 by zero.")
    else:
        verdict = (f"DEFENSIBLE: inside the floor-to-ceiling range. Against the derived "
                   f"{old / 1e6:,.2f}mm it changes the expected night by "
                   f"{a['est_cost_night'] - b_['est_cost_night']:+,.0f} {cap.ccy} and the "
                   f"delta you wake up holding by "
                   f"{b_['p95_wake_delta_mm'] - a['p95_wake_delta_mm']:+,.2f}mm.")
    return {"derived": a, "override": b_, "verdict": verdict,
            "floor_mm": cap.floor_base / 1e6, "ceiling_mm": cap.ceiling_base / 1e6,
            "delta_cost_night": b_["est_cost_night"] - a["est_cost_night"]}


def format_cap(cap: DeltaCap) -> str:
    """The panel, as text.  Identical to ``cap.reasoning``; kept for symmetry with
    ``overnight.format_ladder``."""
    return cap.reasoning


def cap_scaling_note(cap: DeltaCap) -> str:
    """One paragraph the user can keep: how the cap moves as the book moves."""
    return (
        f"Scaling rule. The cap is {cap.cap_mm:,.2f}mm for THIS book. It scales:\n"
        f"  * LINEARLY in gross notional -- double the position, double the cap.\n"
        f"  * as 1/sqrt(tenor) -- a 1W book of the same notional gets a cap "
        f"{math.sqrt(30.0 / 7.0):.2f}x LARGER than a 1M, because it accumulates that "
        f"much more delta per pip; a 3M gets {math.sqrt(30.0 / 91.0):.2f}x.\n"
        f"  * as sqrt(the window's variance share) -- a weekend or a quiet Asia night "
        f"lowers it.\n"
        f"  * NOT AT ALL with the vol level. Gamma falls as 1/sigma and the window sigma "
        f"rises as sigma, and they cancel exactly. A mark that is a full vol point wrong "
        f"moves this cap by less than 0.1% (measured, docs/13 s4.7). The money moves; "
        f"the cap does not.\n"
        f"  * per side, off the REPRICED delta profile, so a skewed book gets two "
        f"different numbers ({cap.cap_up / 1e6:,.2f}mm up / {cap.cap_dn / 1e6:,.2f}mm down "
        f"here). On a 25-delta risk reversal the two sides differ by 2.2x and the linear "
        f"Gamma_1pct proxy is out by 3.6x (measured, docs/13 s4.10).\n"
        f"  * the FLOOR moves as cost^(2/3) and as 1/sqrt(vol); the cap itself does not "
        f"move with cost at all, which is why a wrong cost estimate changes what the cap "
        f"COSTS without changing what it should BE.")
