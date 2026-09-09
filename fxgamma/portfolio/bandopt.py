"""Optimal delta-rebalance bands under proportional transaction costs.

``optimal_band(book, mkt, pair, ...) -> BandResult`` (docs/08_overnight_gamma.md s3).

The one thing to understand before reading anything else
-------------------------------------------------------
For a delta-hedged position the **expected gamma P&L over a window does not depend
on the rebalance band.**  Let ``V = sigma^2 tau S^2`` be the spot variance over the
window and ``G = d(delta_base)/dS`` the book's gamma.  Rebalancing on a grid of
spacing ``h`` (in spot), the expected number of grid crossings is ``V / h^2`` and each
crossing is worth ``G h^2 / 2``, so

    E[capture] = (V / h^2) * (G h^2 / 2) = G V / 2

which is the continuous-hedging gamma P&L, whatever ``h`` is.  The band is therefore
**not** a P&L maximiser.  What it actually trades off is

    cost(h)     = lambda S |G| V / h          (falls as the band widens)
    var(h)      = G^2 h^2 V / 6               (rises as the band widens)

so "maximise monetisation" is a *utility* choice -- how much hedging-error variance
you are willing to buy with the transaction costs you save -- not a maximisation of
expected P&L.  Every method here returns the same ``exp_capture`` and differs only in
where it puts the cost/variance trade-off.  See ``docs/09_hedging_theory.md`` s2.

Methods
-------
``whalley_wilmott``
    The classic asymptotic band, ``H = ((3/2) e^{-rd tau} lambda S G^2 / gamma)^{1/3}``
    in **base-ccy delta** units.  Derived for the *singular control* policy: hedge
    back to the **band edge**, trading infinitesimally at the boundary.
``zakamouline``
    Zakamouline-form correction: the same cubic evaluated at a Leland-adjusted
    volatility (so the band responds to sigma and to the option's own gamma, which
    the raw WW band does not), the rebalance-policy constant for hedging back to
    **target** rather than to the edge, and an additive floor at the round-trip
    breakeven move.  Default, and the one recommended for this tool.
``fixed_grid``
    The desk rule of thumb: rehedge on a fixed grid of one ``n``-day sigma, with the
    Leland number and vol drag reported so the cost of the rule is visible.  A
    comparator, not a recommendation.
``empirical``
    The referee.  Sweeps ``band_delta`` through :mod:`fxgamma.backtest` on common
    random numbers and takes the argmax of realised utility
    ``mean(P&L) - (risk_aversion/2) var(P&L)``.

Units, stated once
------------------
``lambda``           one-way proportional spot cost, a fraction of traded value
                     (``cost_bp / 2 / 1e4``; ``cost_bp`` is round trip).
``S``                spot, quote ccy per base ccy.
``G`` (gamma)        ``d(delta_base)/dS``: base^2 / quote.
``H`` (band_delta)   base ccy.  ``h`` (band in spot) ``= H / |G|``, quote/base.
``risk_aversion``    absolute risk aversion in **1 / report_ccy** (default USD), i.e.
                     the coefficient on the variance penalty of a quadratic utility
                     ``U = E[P&L] - (gamma/2) Var[P&L]``.  It is converted into the
                     pair's quote ccy internally, because a JPY-denominated variance
                     is ~1.5e4 times a USD one and using the same number for both
                     would make USDJPY bands wrong by a factor of ~24 in ``h``.
                     (Note: :func:`fxgamma.portfolio.zones.hedge_bands` reads its own
                     ``risk_aversion`` in 1/quote-ccy -- see docs/09 s7.)

Variance basis: spot travels on **trading days** (252), per amendment v1.4 W-7, and
that is also the basis the backtest engine steps on, so the analytic and empirical
numbers are directly comparable.  Theta is a calendar-day quantity and is not part of
the band problem at all -- it is the same whatever the band.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from ..conventions import PAIRS, pair_spec
from ..models import gk
from ..types import Book, HedgeRule, MarketSnapshot
from .risk import fx_rate, price_book
from .zones import COST_BP, TRADING_DAYS

__all__ = ["BandResult", "BookGamma", "book_gamma", "optimal_band", "compare_bands",
           "band_utility_curve", "leland_number", "POLICY_CONST", "METHODS"]

#: ``H^3 = POLICY_CONST[policy] * lambda * S * G^2 / gamma``.
#:
#: ``edge``   Whalley-Wilmott's own policy: on touching the boundary, trade back to
#:            the boundary.  Turnover per unit time is the boundary local time,
#:            ``v / (2H)``, and the stationary delta error is uniform on [-H, H]
#:            (``E[e^2] = H^2/3``).  Constant 3/2.
#: ``center`` Trade back to the *target* delta on touching the boundary -- what
#:            :mod:`fxgamma.portfolio.hedging`, the backtest engine and a resting
#:            order ladder all actually do.  Turnover ``v / H``, triangular
#:            occupation density, ``E[e^2] = H^2/6``.  Constant 6.
#:
#: The ratio is ``(6 / 1.5)^(1/3) = 4^(1/3) = 1.587``: **the correct band for a
#: hedge-to-flat desk is 59% wider than the textbook WW band.**  This single factor
#: is most of the gap between the WW number and the backtester's argmax.
POLICY_CONST: dict[str, float] = {"edge": 1.5, "center": 6.0}

METHODS = ("zakamouline", "whalley_wilmott", "empirical", "fixed_grid")

_SQRT_8_OVER_PI = math.sqrt(8.0 / math.pi)


# --------------------------------------------------------------------------- #
# result type
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BandResult:
    """Frozen contract type (docs/08 s3) plus additive diagnostic fields.

    ``band_pips``        half-width of the no-trade band measured in **spot pips**.
    ``band_delta_base``  the same band in base-ccy delta -- ``band_pips * pip * |G|``.
    ``exp_capture``      expected gamma P&L over ``horizon_days``, quote ccy.  Equal
                         for every method: ``0.5 * G * V``.  It is reported so the
                         reader can see that it does not move.
    ``exp_cost``         expected transaction cost over the horizon, quote ccy.
    ``exp_rehedges``     expected number of rebalances over the horizon (hedge-to-
                         target counting; see ``POLICY_CONST``).
    ``utility``          ``exp_capture - exp_cost - (gamma/2) * exp_var``, quote ccy.
    ``curve``            the full trade-off curve, one row per candidate band.
    """
    band_pips: float
    band_delta_base: float
    method: str
    exp_capture: float
    exp_cost: float
    exp_rehedges: float
    utility: float
    curve: "pd.DataFrame"
    note: str = ""
    # ---- additive (defaults keep the frozen positional signature valid) ----
    pair: str = ""
    band_spot: float = 0.0            # half-width in spot units (quote/base)
    band_pct: float = 0.0             # as % of spot
    band_sigma_days: float = 0.0      # in daily sigmas, sqrt(252) basis
    exp_var: float = 0.0              # variance of the hedging error, quote ccy^2
    exp_sd: float = 0.0               # its sqrt -- the number a trader can feel
    gamma: float = 0.0                # d(delta_base)/dS used
    gamma_1pct: float = 0.0
    sigma: float = float("nan")
    horizon_days: float = 1.0
    cost_bp: float = 0.0
    lam: float = 0.0                  # one-way proportional cost
    risk_aversion: float = 0.0        # as passed, in 1/report_ccy
    risk_aversion_quote: float = 0.0  # converted into quote ccy
    policy: str = "center"
    ccy: str = ""
    report_ccy: str = "USD"
    fx_to_report: float = 1.0
    leland: float = float("nan")      # Leland number at the implied rehedge interval
    vol_drag_pts: float = float("nan")  # cost expressed in vol points
    breakeven_pips: float = 0.0       # 2*lambda*S: below this a rehedge cannot pay
    asymptotic_ratio: float = float("nan")   # band / (S sigma sqrt(horizon))
    diagnostics: dict = field(default_factory=dict)

    # convenient report-ccy copies
    @property
    def exp_capture_rep(self) -> float:
        return self.exp_capture * self.fx_to_report

    @property
    def exp_cost_rep(self) -> float:
        return self.exp_cost * self.fx_to_report

    @property
    def utility_rep(self) -> float:
        return self.utility * self.fx_to_report

    def __repr__(self) -> str:                                   # pragma: no cover
        return (f"<BandResult {self.method} {self.pair} band={self.band_pips:,.1f}pips "
                f"({self.band_delta_base / 1e6:,.2f}mm delta) "
                f"cost={self.exp_cost:,.0f} util={self.utility:,.0f}>")


# --------------------------------------------------------------------------- #
# reading the book
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BookGamma:
    """What the band maths needs out of the book, in one place."""
    pair: str
    spot: float
    gamma: float          # d(delta_base)/dS, signed
    gamma_1pct: float
    delta_base: float
    theta: float          # quote ccy per calendar day
    vega: float
    sigma: float          # gamma-weighted ATM vol
    T_ref: float          # gamma-weighted years to expiry
    rd: float
    rf: float
    gross_notional: float
    ccy: str
    base_ccy: str
    n_live: int


def book_gamma(book: Book, mkt: MarketSnapshot, pair: str, *,
               marks: Mapping[str, float] | None = None,
               sigma: float | None = None) -> BookGamma:
    """Collapse the book's ``pair`` legs into the handful of numbers the band needs.

    ``sigma`` and ``T_ref`` are **gamma-weighted** across the live legs, not taken
    from the front expiry: a book that is long a 1W straddle and short a 1Y straddle
    has almost all its gamma in the 1W, and the band should be the 1W's band.
    """
    spec = pair_spec(pair)
    sub = book.filter(pair)
    S = float(mkt.spot[pair])
    rd, rf = mkt.rd_rf(pair, PAIRS)
    df = price_book(sub, mkt, marks=marks)
    live = df[~df["expired"].astype(bool)] if len(df) else df
    opts = live[live["kind"] == "option"] if len(live) else live
    if not len(live):
        return BookGamma(pair, S, 0.0, 0.0, 0.0, 0.0, 0.0,
                         float(sigma) if sigma is not None else _atm(mkt, pair, 1 / 12),
                         1 / 12, rd, rf, 0.0, spec.quote, spec.base, 0)
    g = float(live["gamma"].sum())
    w = np.abs(opts["gamma"].to_numpy(float)) if len(opts) else np.array([])
    if w.sum() > 0:
        T_ref = float(np.average(opts["T"].to_numpy(float), weights=w))
        sig = float(np.average(opts["vol"].to_numpy(float), weights=w))
    else:
        T_ref = float(opts["T"].min()) if len(opts) else 1 / 12
        sig = _atm(mkt, pair, T_ref)
    if sigma is not None:
        sig = float(sigma)
    return BookGamma(
        pair=pair, spot=S, gamma=g,
        gamma_1pct=float(live["gamma_1pct"].sum()),
        delta_base=float(live["delta_base"].sum()),
        theta=float(live["theta"].sum()), vega=float(live["vega"].sum()),
        sigma=sig, T_ref=max(T_ref, 1e-6), rd=rd, rf=rf,
        gross_notional=float(opts["notional_base"].abs().sum()) if len(opts) else 0.0,
        ccy=spec.quote, base_ccy=spec.base, n_live=int(len(live)))


def _atm(mkt: MarketSnapshot, pair: str, T: float) -> float:
    surf = mkt.surfaces.get(pair)
    return float(surf.atm(max(T, 1e-6))) if surf is not None else 0.08


# --------------------------------------------------------------------------- #
# the analytic pieces
# --------------------------------------------------------------------------- #
def leland_number(lam: float, sigma: float, dt_years: float) -> float:
    """Leland's (1985) ``Le = sqrt(8/pi) * lambda / (sigma sqrt(dt))``.

    ``lambda`` is the **one-way** proportional cost, so Leland's ``k`` (half the
    round-trip spread) is exactly our ``lam``.  ``Le`` is the fractional increase in
    *variance* needed to pay for rehedging every ``dt``; the modified Leland vol is
    ``sigma_hat^2 = sigma^2 (1 +/- Le)``, plus for a short-gamma book (you must
    over-price the vol you are selling) and minus for a long-gamma one.

    Where it breaks: ``Le >= 1`` means the cost of rehedging at that frequency
    exceeds the entire option variance, and the adjustment is meaningless.  We clamp
    the *vol* adjustment at ``Le = 0.9`` and say so in ``BandResult.note``.
    """
    if sigma <= 0 or dt_years <= 0:
        return float("nan")
    return _SQRT_8_OVER_PI * float(lam) / (float(sigma) * math.sqrt(float(dt_years)))


def _band_from_cubic(const: float, lam: float, S: float, G: float,
                     gamma_q: float, rd: float, T: float) -> float:
    """``H = (const * e^{-rd T} * lambda * S * G^2 / gamma)^(1/3)`` in base ccy."""
    if G == 0.0 or gamma_q <= 0.0 or lam <= 0.0:
        return float("nan")
    return (const * math.exp(-rd * max(T, 0.0)) * lam * S * G * G / gamma_q) ** (1.0 / 3.0)


def band_utility_curve(bg: BookGamma, bands_spot: np.ndarray, *, lam: float,
                       gamma_q: float, horizon_days: float,
                       sigma: float | None = None) -> pd.DataFrame:
    """The cost / variance / utility trade-off over a grid of spot bands.

    This is the function every analytic method is scored against, and it is also
    what makes the "the band does not change expected P&L" point visible: the
    ``exp_capture`` column is constant down the whole table.
    """
    S, G = bg.spot, bg.gamma
    sig = bg.sigma if sigma is None else float(sigma)
    tau = float(horizon_days) / TRADING_DAYS
    V = (sig * sig) * tau * S * S                     # spot variance over the horizon
    h = np.asarray(bands_spot, dtype=float)
    h = np.where(h > 0, h, np.nan)
    cap = 0.5 * G * V
    n = V / h ** 2
    cost = lam * S * abs(G) * V / h
    var = (G * h) ** 2 * V / 6.0
    util = cap - cost - 0.5 * gamma_q * var
    return pd.DataFrame({
        "band_spot": h,
        "band_pips": h / pair_spec(bg.pair).pip,
        "band_delta_base": abs(G) * h,
        "band_pct": 100.0 * h / S,
        "exp_rehedges": n,
        "exp_capture": np.full_like(h, cap),
        "exp_cost": cost,
        "exp_var": var,
        "exp_sd": np.sqrt(var),
        "utility": util,
    })


# --------------------------------------------------------------------------- #
# the public entry point
# --------------------------------------------------------------------------- #
def optimal_band(book: Book, mkt: MarketSnapshot, pair: str, *,
                 cost_bp: float | None = None, risk_aversion: float = 1e-6,
                 horizon_days: float = 1.0, method: str = "zakamouline",
                 report_ccy: str = "USD", policy: str = "center",
                 marks: Mapping[str, float] | None = None,
                 sigma: float | None = None,
                 rule: HedgeRule | None = None,
                 **empirical_kw: Any) -> BandResult:
    """The optimal no-trade half-band for ``pair``, by ``method``.

    ``horizon_days`` is in **trading days** (252 basis) and sets the variance the
    cost and risk terms are integrated over.  It does not move the analytic optimum
    -- the horizon cancels out of the first-order condition -- but it does scale
    ``exp_capture``, ``exp_cost`` and ``exp_var``, which is what the trader reads.
    The ``empirical`` method genuinely uses it: that is the length of path it walks.

    Raises ``ValueError`` on an unknown method rather than falling back (architecture
    s7: never silently substitute).
    """
    method = str(method).strip().lower()
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; known: {sorted(METHODS)}")
    if policy not in POLICY_CONST:
        raise ValueError(f"unknown policy {policy!r}; known: {sorted(POLICY_CONST)}")

    spec = pair_spec(pair)
    bg = book_gamma(book, mkt, pair, marks=marks, sigma=sigma)
    if rule is not None and rule.cost_bp and cost_bp is None:
        cost_bp = rule.cost_bp
    cbp = float(cost_bp) if cost_bp is not None else COST_BP.get(pair, 0.5)
    lam = cbp / 2.0 / 1e4
    fq = fx_rate(spec.quote, report_ccy, mkt)
    # risk aversion is quoted in 1/report_ccy; a quote-ccy variance is (1/fq)^2 the
    # report-ccy one, so the coefficient that reproduces the same certainty
    # equivalent in quote ccy is gamma_rep * fq.
    gamma_q = float(risk_aversion) * fq
    tau = float(horizon_days) / TRADING_DAYS

    if bg.gamma == 0.0:
        return _empty(bg, method, cbp, lam, risk_aversion, gamma_q, horizon_days,
                      report_ccy, fq, policy,
                      "book has no gamma in this pair -- there is nothing to band")

    if method == "empirical":
        return _empirical(book, mkt, pair, bg=bg, lam=lam, cbp=cbp,
                          risk_aversion=risk_aversion, gamma_q=gamma_q,
                          horizon_days=horizon_days, report_ccy=report_ccy, fq=fq,
                          policy=policy, **empirical_kw)

    const = POLICY_CONST[policy]
    sig = bg.sigma
    notes: list[str] = []

    if method == "whalley_wilmott":
        # The classic, on its own terms: WW's policy is hedge-to-edge, so it is
        # reported with the 3/2 constant regardless of `policy`, and the note says
        # what that means for a hedge-to-flat desk.
        H = _band_from_cubic(POLICY_CONST["edge"], lam, bg.spot, bg.gamma,
                             gamma_q, bg.rd, bg.T_ref)
        used_policy = "edge"
        notes.append(
            "Whalley-Wilmott (1997) asymptotic band, H = ((3/2) e^{-rd T} lam S G^2 / gamma)^(1/3), "
            "in base-ccy delta. Derived for hedging back to the BAND EDGE. This tool, "
            "portfolio.hedging and the backtester all hedge back to TARGET, for which the "
            f"constant is 6 and the band is 4^(1/3) = 1.587x wider ({H * 4 ** (1 / 3) / abs(bg.gamma) / spec.pip:,.1f} pips). "
            "Note sigma cancels out of this formula entirely -- that is a property of the "
            "asymptotic expansion, not of the market.")
    elif method == "fixed_grid":
        # Desk rule of thumb: rehedge on a one-day sigma grid (scaled to horizon).
        n_reh = float(empirical_kw.pop("rehedges_per_day", 1.0))
        dt = 1.0 / (TRADING_DAYS * max(n_reh, 1e-9))
        h = bg.spot * sig * math.sqrt(dt)
        H = abs(bg.gamma) * h
        used_policy = policy
        Le = leland_number(lam, sig, dt)
        notes.append(
            f"Fixed grid / Leland comparator: spacing = one {1 / n_reh:g}-trading-day sigma "
            f"= S*sigma*sqrt(dt) with dt = 1/{TRADING_DAYS * n_reh:g} yr. This is the rule of "
            "thumb, NOT an optimum: it ignores cost, gamma and risk aversion entirely. "
            f"Leland number at that frequency Le = {Le:.4f}, i.e. the cost of running this grid "
            f"is worth {50.0 * Le * sig * 100:.2f} vol points "
            "(sigma_hat^2 = sigma^2 (1 +/- Le)).")
    else:   # zakamouline
        H, zk_notes = _zakamouline(bg, lam=lam, gamma_q=gamma_q, const=const,
                                   horizon_days=horizon_days)
        used_policy = policy
        notes.extend(zk_notes)

    h_spot = H / abs(bg.gamma)
    curve = band_utility_curve(bg, _grid_around(h_spot), lam=lam, gamma_q=gamma_q,
                               horizon_days=horizon_days)
    return _finish(bg, method, H, h_spot, curve, lam, cbp, risk_aversion, gamma_q,
                   horizon_days, report_ccy, fq, used_policy, " ".join(notes),
                   sigma=sig)


# --------------------------------------------------------------------------- #
# Zakamouline
# --------------------------------------------------------------------------- #
def _zakamouline(bg: BookGamma, *, lam: float, gamma_q: float, const: float,
                 horizon_days: float) -> tuple[float, list[str]]:
    """Zakamouline-form band.

    Three corrections to Whalley-Wilmott, in the order they matter here:

    1. **Rebalance policy.**  ``const`` is 6 for hedge-to-target, not WW's 3/2.
       Worth a factor of 1.587 and it is the largest of the three.
    2. **Volatility.**  WW's sigma cancels exactly, which is an artefact of the
       lambda -> 0 expansion.  Zakamouline restores it by evaluating the hedge at a
       Leland-modified volatility ``sigma_hat^2 = sigma^2 (1 - sign(G) Le)``: a long
       gamma book cannot afford to hedge at the full implied vol once costs are
       charged, so it behaves like a smaller-gamma book, and the band widens; a short
       gamma book must hedge as if vol were higher, and its band tightens.  For an
       (approximately) at-the-money book ``G ~ 1/(S sigma sqrt(T))``, so
       ``G_hat = G * sigma / sigma_hat``.  ``Le`` is evaluated at the rehedge interval
       the band itself implies, which makes this a fixed point; two iterations are
       enough (the map is a contraction for ``Le < 1``).
    3. **A floor at the round-trip breakeven.**  A rehedge triggered by a move of
       ``h`` captures ``|G| h^2 / 2`` and pays ``lambda S |G| h``, so it is a losing
       trade for ``h < 2 lambda S`` **whatever the risk aversion**.  Zakamouline's
       additive term arises from fixed costs; our cost model has none, so the
       additive term used here is derived instead from that hard breakeven, blended
       as a smooth cube ``H = (H_util^3 + H_be^3)^(1/3)``.  It is negligible for
       EURUSD (0.2 pips) and binding for the Scandies (tens of pips).

    Returns ``(H_base_ccy, notes)``.
    """
    S, G, sig = bg.spot, bg.gamma, bg.sigma
    notes: list[str] = []
    sig_hat = sig
    G_hat = G
    Le = float("nan")
    clamped = False
    for _ in range(3):
        H = _band_from_cubic(const, lam, S, G_hat, gamma_q, bg.rd, bg.T_ref)
        if not np.isfinite(H) or H <= 0:
            break
        h = H / abs(G_hat)
        dt = (h / (S * sig)) ** 2                      # implied rehedge interval, yrs
        Le = leland_number(lam, sig, dt)
        if not np.isfinite(Le):
            break
        Le_used = min(Le, 0.9)
        clamped = clamped or (Le > 0.9)
        adj = 1.0 - math.copysign(1.0, G) * Le_used
        sig_hat = sig * math.sqrt(max(adj, 0.05))
        G_hat = G * sig / sig_hat
    H_util = _band_from_cubic(const, lam, S, G_hat, gamma_q, bg.rd, bg.T_ref)
    H_be = 2.0 * lam * S * abs(G)                       # breakeven floor, base ccy
    H = (H_util ** 3 + H_be ** 3) ** (1.0 / 3.0)
    notes.append(
        f"Zakamouline-form band. Policy constant {const:g} (hedge to target). "
        f"Leland-modified vol sigma_hat = {sig_hat * 100:.2f}% vs implied {sig * 100:.2f}% "
        f"(Le = {Le:.4f} at the implied rehedge interval), which moves the effective gamma "
        f"by {(G_hat / G - 1) * 100:+.1f}%. Breakeven floor 2*lam*S = "
        f"{2.0 * lam * S / pair_spec(bg.pair).pip:.2f} pips "
        f"({'BINDING' if H_be > H_util else 'not binding'}). "
        "The volatility and breakeven terms follow Zakamouline's structure (modified vol + "
        "a term that does not vanish with gamma); the breakeven constant is derived here "
        "from our own proportional-only cost model rather than taken from his fixed-cost "
        "calibration -- see docs/09_hedging_theory.md s4.")
    if clamped:
        notes.append("WARNING: Leland number exceeded 0.9 and was clamped -- at this cost "
                     "and rehedge frequency the transaction cost consumes the whole option "
                     "variance and NO band is economic. Treat the number as an upper bound.")
    return H, notes


# --------------------------------------------------------------------------- #
# empirical referee
# --------------------------------------------------------------------------- #
def _empirical(book: Book, mkt: MarketSnapshot, pair: str, *, bg: BookGamma,
               lam: float, cbp: float, risk_aversion: float, gamma_q: float,
               horizon_days: float, report_ccy: str, fq: float, policy: str,
               n_paths: int = 48, n_bands: int = 15, span: float = 5.0,
               steps_per_day: int = 24, seed0: int = 20260909,
               sigma_r: float | None = None, bands_spot: Sequence[float] | None = None,
               tenor_days: int | None = None) -> BandResult:
    """Sweep the backtester over bands on common random numbers and take the argmax.

    Design choices that make this a referee rather than a second opinion:

    * **Common random numbers.**  Every band walks the *same* set of paths, so the
      band-independent part of the P&L -- which dominates the variance by two orders
      of magnitude -- differences away between bands.
    * **The position is held, not rolled**, with an expiry at least 4x the horizon
      away, so gamma is roughly constant across the window.  Otherwise the sweep
      optimises a band for an average of several different gammas and cannot be
      compared with an analytic band evaluated at one.
    * **The straddle is sized to the book's own gamma**, so this is a band for *this*
      book rather than for a round 10mm.
    * **The objective imposes E[discretisation error] = 0 instead of estimating it.**
      This is the important one.  The realised P&L of a band ``h`` is
      ``capture - cost(h) + e(h)`` where ``e`` is the discretisation error: mean zero,
      standard deviation tens of thousands of dollars.  Resolving a 200-dollar cost
      difference through the sample mean of ``e`` would need order 1e5 paths.  So the
      objective scored here is

          U(h) = capture - E[cost(h)] - (risk_aversion / 2) * Var[e(h)]

      with ``E[cost]`` and ``Var[e]`` both measured from the runs (both are cheap to
      estimate: cost is nearly deterministic, and ``Var[e]`` is a variance rather than
      a mean).  The zero-mean assumption on ``e`` is then **tested rather than
      assumed**: the ``mean_resid`` / ``se_resid`` columns are
      ``mean(P&L(h) - P&L(h_ref)) + (cost(h) - cost(h_ref))`` and its standard error,
      and they must straddle zero.  If they do not, the engine has a bias and the
      empirical band is not trustworthy -- so read them.

    ``Var[e(h)]`` is estimated as ``Var(P&L(h) - P&L(h_tightest))`` under CRN, the
    tightest band on the grid standing in for continuous hedging.
    """
    from ..backtest.engine import BacktestConfig, PathData, run_backtest, synthetic_path

    spec = pair_spec(pair)
    S, sig = bg.spot, bg.sigma
    sigma_r = float(sigma_r) if sigma_r is not None else sig
    horizon_days = float(horizon_days)
    n_days = max(int(math.ceil(horizon_days)), 2)
    tenor = int(tenor_days) if tenor_days else int(max(round(bg.T_ref * 365.0),
                                                      math.ceil(4.0 * n_days), 7))

    # size an ATM straddle to the book's gamma
    T = tenor / 365.0
    K = S * math.exp((bg.rd - bg.rf) * T + 0.5 * sig * sig * T)
    g_unit = sum(gk.gk_greeks(S, K, T, bg.rd, bg.rf, sig, cp, 1.0, 1,
                              delta_convention=spec.delta_convention).gamma
                 for cp in (+1, -1))
    if g_unit == 0.0:
        raise ValueError("degenerate straddle gamma -- cannot build the empirical sweep")
    notional = abs(bg.gamma) / g_unit
    direction = 1 if bg.gamma > 0 else -1

    if bands_spot is None:
        anchor = _band_from_cubic(POLICY_CONST[policy], lam, S, bg.gamma, gamma_q,
                                  bg.rd, bg.T_ref) / abs(bg.gamma)
        bands_spot = np.exp(np.linspace(math.log(anchor / span), math.log(anchor * span),
                                        int(n_bands)))
    bands_spot = np.sort(np.asarray(list(bands_spot), dtype=float))

    spd = max(int(steps_per_day), 1)
    paths = [synthetic_path(n_days=n_days, sigma_r=sigma_r, sigma_i=sig, S0=S, pair=pair,
                            steps_per_day=spd, seed=int(seed0 + k), rd=bg.rd, rf=bg.rf)
             for k in range(int(n_paths))]

    rows: list[dict[str, Any]] = []
    pnl_matrix: list[np.ndarray] = []
    for h in bands_spot:
        H = abs(bg.gamma) * float(h)
        cfg = BacktestConfig(
            pair=pair, structure="straddle", direction=direction,
            notional_base=notional, tenor_days=tenor, roll_days=None,
            hedge=HedgeRule(mode="band", band_delta=H, cost_bp=cbp),
            cost_bp=cbp, vega_spread_pts=0.0, name=f"band {H / 1e6:,.3f}mm")
        pnls, costs, hedges = [], [], []
        for p in paths:
            r = run_backtest(p, cfg)
            pnls.append(float(r.equity["equity"].iloc[-1]))
            costs.append(float(r.stats["hedge_cost"]))
            hedges.append(float(r.stats["n_hedges"]))
        a = np.asarray(pnls, float)
        pnl_matrix.append(a)
        rows.append({"band_spot": float(h), "band_pips": float(h) / spec.pip,
                     "band_delta_base": H, "band_pct": 100.0 * float(h) / S,
                     "mean_pnl": float(a.mean()), "sd_pnl": float(a.std(ddof=1)),
                     "exp_cost": float(np.mean(costs)),
                     "sd_cost": float(np.std(costs, ddof=1)),
                     "exp_rehedges": float(np.mean(hedges))})
    df = pd.DataFrame(rows)
    M = np.vstack(pnl_matrix)
    ref = M[0]
    d = M - ref[None, :]
    df["var_hedge_error"] = [float(np.var(d[i], ddof=1)) for i in range(M.shape[0])]
    df["sd_hedge_error"] = np.sqrt(df["var_hedge_error"])
    df["mean_resid"] = d.mean(axis=1) + (df["exp_cost"] - float(df["exp_cost"].iloc[0]))
    df["se_resid"] = d.std(axis=1, ddof=1) / math.sqrt(M.shape[1])
    df["resid_z"] = df["mean_resid"] / df["se_resid"].replace(0.0, np.nan)

    tau = float(horizon_days) / TRADING_DAYS
    cap = 0.5 * bg.gamma * sig * sig * tau * S * S
    df["exp_capture"] = cap
    df["utility"] = cap - df["exp_cost"] - 0.5 * gamma_q * df["var_hedge_error"]
    # raw (unimposed) realised utility, kept for the reader to see how noisy it is
    df["utility_raw"] = df["mean_pnl"] - 0.5 * gamma_q * df["sd_pnl"] ** 2

    x = np.log(df["band_spot"].to_numpy(float))
    y = df["utility"].to_numpy(float)
    i_max = int(np.nanargmax(y))
    h_star = float(df["band_spot"].iloc[i_max])
    fit_note = ""
    if 0 < i_max < len(x) - 1:
        c = np.polyfit(x[i_max - 1: i_max + 2], y[i_max - 1: i_max + 2], 2)
        if c[0] < 0:
            xv = -c[1] / (2 * c[0])
            if x[i_max - 1] <= xv <= x[i_max + 1]:
                h_star = float(math.exp(xv))
                fit_note = ", refined by a local quadratic in log(band)"
    H_star = abs(bg.gamma) * h_star
    worst_z = float(np.nanmax(np.abs(df["resid_z"].to_numpy(float)[1:]))) if len(df) > 1 else 0.0
    note = (
        f"EMPIRICAL REFEREE. {len(paths)} common-random-number paths x {n_days}d x {spd} "
        f"steps/day; straddle sized to the book's gamma ({notional / 1e6:,.2f}mm per leg, "
        f"{tenor}d tenor, held not rolled); sigma_r {sigma_r * 100:.2f}% vs implied "
        f"{sig * 100:.2f}%; cost {cbp:g}bp round trip. Grid argmax "
        f"{float(df['band_pips'].iloc[i_max]):,.1f} pips{fit_note}. "
        f"Objective = capture - E[cost] - (ra/2) Var[hedging error], with E[cost] and "
        f"Var[.] measured and E[hedging error] imposed to be zero; that assumption is "
        f"tested by mean_resid/se_resid, worst |z| = {worst_z:.2f} "
        f"({'consistent with zero' if worst_z < 3 else 'NOT consistent with zero -- distrust this band'}). "
        f"Monitoring is discrete: one step is {S * sig * math.sqrt(1.0 / (TRADING_DAYS * spd)) / spec.pip:,.1f} pips, "
        f"so measured cost sits below the continuous-monitoring analytic by the overshoot. "
        "IN-SAMPLE on simulated paths (REQ-062): it validates the analytic formula, it is "
        "not itself a recommendation.")
    r_at = df.iloc[i_max]
    return _finish(bg, "empirical", H_star, h_star, df, lam, cbp, risk_aversion,
                   gamma_q, horizon_days, report_ccy, fq, policy, note, sigma=sig,
                   exp_cost=float(r_at["exp_cost"]),
                   exp_rehedges=float(r_at["exp_rehedges"]),
                   utility=float(r_at["utility"]),
                   diagnostics={"n_paths": len(paths), "steps_per_day": spd,
                                "tenor_days": tenor, "notional_base": notional,
                                "sigma_r": sigma_r, "grid_argmax_pips": float(df["band_pips"].iloc[i_max]),
                                "worst_resid_z": worst_z,
                                "step_pips": S * sig * math.sqrt(1.0 / (TRADING_DAYS * spd)) / spec.pip,
                                "measured_var_hedge_error": float(r_at["var_hedge_error"])})


# --------------------------------------------------------------------------- #
# assembly helpers
# --------------------------------------------------------------------------- #
def _grid_around(h: float, span: float = 8.0, n: int = 41) -> np.ndarray:
    if not np.isfinite(h) or h <= 0:
        return np.array([np.nan])
    return np.exp(np.linspace(math.log(h / span), math.log(h * span), n))


def _finish(bg: BookGamma, method: str, H: float, h_spot: float, curve: pd.DataFrame,
            lam: float, cbp: float, risk_aversion: float, gamma_q: float,
            horizon_days: float, report_ccy: str, fq: float, policy: str, note: str,
            *, sigma: float, exp_cost: float | None = None,
            exp_rehedges: float | None = None, utility: float | None = None,
            diagnostics: dict | None = None) -> BandResult:
    spec = pair_spec(bg.pair)
    tau = float(horizon_days) / TRADING_DAYS
    V = sigma * sigma * tau * bg.spot * bg.spot
    cap = 0.5 * bg.gamma * V
    n = V / h_spot ** 2 if h_spot > 0 else float("nan")
    cost = lam * bg.spot * abs(bg.gamma) * V / h_spot if h_spot > 0 else float("nan")
    var = (bg.gamma * h_spot) ** 2 * V / 6.0
    util = cap - cost - 0.5 * gamma_q * var
    dt_implied = (h_spot / (bg.spot * sigma)) ** 2 if sigma > 0 else float("nan")
    Le = leland_number(lam, sigma, dt_implied)
    sig_day = sigma / math.sqrt(TRADING_DAYS)
    return BandResult(
        band_pips=h_spot / spec.pip, band_delta_base=H, method=method,
        exp_capture=cap,
        exp_cost=cost if exp_cost is None else exp_cost,
        exp_rehedges=n if exp_rehedges is None else exp_rehedges,
        utility=util if utility is None else utility,
        curve=curve, note=note, pair=bg.pair, band_spot=h_spot,
        band_pct=100.0 * h_spot / bg.spot,
        band_sigma_days=(h_spot / bg.spot) / sig_day if sig_day > 0 else float("nan"),
        exp_var=var, exp_sd=math.sqrt(max(var, 0.0)),
        gamma=bg.gamma, gamma_1pct=bg.gamma_1pct, sigma=sigma,
        horizon_days=float(horizon_days), cost_bp=cbp, lam=lam,
        risk_aversion=float(risk_aversion), risk_aversion_quote=gamma_q,
        policy=policy, ccy=spec.quote, report_ccy=report_ccy.upper(), fx_to_report=fq,
        leland=Le, vol_drag_pts=50.0 * Le * sigma * 100.0 if np.isfinite(Le) else float("nan"),
        breakeven_pips=2.0 * lam * bg.spot / spec.pip,
        asymptotic_ratio=h_spot / (bg.spot * sigma * math.sqrt(max(tau, 1e-12))),
        diagnostics=diagnostics or {})


def _empty(bg: BookGamma, method: str, cbp: float, lam: float, ra: float,
           gamma_q: float, horizon_days: float, report_ccy: str, fq: float,
           policy: str, note: str) -> BandResult:
    return BandResult(float("nan"), 0.0, method, 0.0, 0.0, 0.0, 0.0,
                      pd.DataFrame(columns=["band_spot", "band_pips", "utility"]),
                      note, pair=bg.pair, sigma=bg.sigma, horizon_days=horizon_days,
                      cost_bp=cbp, lam=lam, risk_aversion=ra,
                      risk_aversion_quote=gamma_q, policy=policy, ccy=bg.ccy,
                      report_ccy=report_ccy.upper(), fx_to_report=fq)


def compare_bands(book: Book, mkt: MarketSnapshot, pair: str, *,
                  methods: Sequence[str] = METHODS, **kw: Any) -> pd.DataFrame:
    """One row per method -- the validation table in docs/09_hedging_theory.md s6.

    ``utility_at`` re-scores every method's band on the *same* analytic utility
    function, which is the only fair comparison: a method that lands 30% off the
    optimum in band width typically gives up well under 1% of the utility, because
    the objective is flat near its maximum.  That flatness is the real reason not to
    agonise over the third significant figure of a band.
    """
    out: list[dict[str, Any]] = []
    ref: BandResult | None = None
    for m in methods:
        try:
            r = optimal_band(book, mkt, pair, method=m, **kw)
        except Exception as exc:                      # noqa: BLE001
            out.append({"method": m, "error": str(exc)[:200]})
            continue
        if ref is None or m == "zakamouline":
            ref = r
        out.append({
            "method": m, "band_pips": r.band_pips,
            "band_delta_base": r.band_delta_base, "band_pct": r.band_pct,
            "band_sigma_days": r.band_sigma_days,
            "exp_capture": r.exp_capture, "exp_cost": r.exp_cost,
            "exp_rehedges": r.exp_rehedges, "exp_sd": r.exp_sd,
            "utility": r.utility, "policy": r.policy,
            "asymptotic_ratio": r.asymptotic_ratio, "note": r.note,
        })
    df = pd.DataFrame(out)
    if ref is not None and "band_spot" in ref.curve.columns and len(ref.curve):
        # score every band on one common analytic utility curve
        bg = book_gamma(book, mkt, pair)
        spec = pair_spec(pair)
        if "band_pips" in df.columns:
            hs = df["band_pips"].to_numpy(float) * spec.pip
            c = band_utility_curve(bg, hs, lam=ref.lam, gamma_q=ref.risk_aversion_quote,
                                   horizon_days=ref.horizon_days, sigma=ref.sigma)
            df["utility_common"] = c["utility"].to_numpy(float)
            best = float(np.nanmax(df["utility_common"]))
            worst_cap = float(np.nanmax(c["exp_capture"]))
            df["utility_giveup_pct"] = 100.0 * (best - df["utility_common"]) / abs(worst_cap or 1.0)
    return df
