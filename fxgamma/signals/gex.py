"""Listed positioning: a market gamma profile from CME open interest by strike.

**This module does not compute "dealer gamma", and nothing it returns may be
labelled that way.**  The trader review (Q-8, W-11) is unambiguous and correct:

1. **The sign is unknowable from open interest.**  OI counts contracts outstanding,
   not who is long them.  Every one of those contracts has a long and a short; which
   side is the dealer is not in the data and cannot be inferred from the strike, the
   call/put tag, or the volume.  A screen that prints "dealers are short gamma below
   1.15" is inventing the most important input.
2. **The sample is small.**  CME FX options are a minority of G10 vanilla activity;
   the OTC market that actually pins spot is not in this dataset (which is what
   MISS-6's manual OTC expiry table is for).
3. **The mechanics are unforgiving.**  Contract multipliers, futures-vs-spot strikes,
   American-vs-European exercise, the reciprocal quoting of 6J/6C/6S and the CME's own
   expiry calendar all have to be right before the number means anything.

So this module returns an **unsigned** gamma profile by default -- "here is where
listed gamma sits", which is a real and useful fact -- and exposes the sign
assumption as an explicit parameter you have to choose, whose name is printed in the
output frame (``sign_assumption`` column) so it can never travel without its caveat.

``SIGN_ASSUMPTIONS``
--------------------
``"unsigned"`` (default)
    ``|gamma|`` per strike.  No positioning claim of any kind.
``"long_all"`` / ``"short_all"``
    The book of open interest is entirely long / entirely short.  Both are
    counterfactual; they bracket the possible profiles, which is the honest way to
    show a range.
``"dealer_short_calls_long_puts"`` and ``"dealer_long_calls_short_puts"``
    The two textbook customer-flow stories.  Provided because people will ask for
    them, labelled as assumptions, and *never* the default.

Strike space
------------
Frames are expected in **pair (FORDOM) strike space** -- which is what
``fxgamma.data.cme_options.to_pair_frame`` and the synthetic provider already emit
(they handle the 6J/6C/6S reciprocal and the ``cp`` flip that comes with it).
:func:`market_gamma_profile` therefore assumes strikes and ``cp`` are already in the
pair's convention, states that in the output, and does not attempt an inversion of
its own.  If you hand it raw CME-space strikes the profile is wrong, silently -- so
the frame carries a ``strike_space`` column and the loader is the single place that
converts.

Futures vs spot: CME settles options on *futures*, so the strikes are forward
strikes.  ``spot_strikes=True`` divides out the CIP forward factor
(``K_spot = K_fut * exp(-(rd - rf) T)``) so the profile lines up with the spot axis
the rest of the app draws on.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Sequence

import numpy as np
import pandas as pd

from ..conventions import PAIRS, pair_spec, year_fraction
from ..models import gk
from ..types import MarketSnapshot

__all__ = ["CONTRACT_SIZE", "QUOTE_DENOMINATED", "SIGN_ASSUMPTIONS",
           "market_gamma_profile", "gamma_profile_curve", "strike_magnets",
           "oi_expiry_ladder", "gamma_flip_level"]

#: CME FX option contract sizes, in **base ccy of the FORDOM pair** after the
#: 6J/6C/6S reciprocal has been undone by the loader.  Source: CME product specs.
#: Displayed on the figure (W-11 makes the multiplier table mandatory and visible).
CONTRACT_SIZE: dict[str, float] = {
    "EURUSD": 125_000.0,     # 6E  EUR 125,000
    "GBPUSD": 62_500.0,      # 6B  GBP 62,500
    "USDJPY": 12_500_000.0,  # 6J  JPY 12,500,000 -> USD-base after inversion, see note
    "AUDUSD": 100_000.0,     # 6A
    "NZDUSD": 100_000.0,     # 6N
    "USDCAD": 100_000.0,     # 6C  CAD 100,000
    "USDCHF": 125_000.0,     # 6S  CHF 125,000
}
#: For the inverted products (6J/6C/6S) the contract is denominated in the pair's
#: *quote* ccy, so the base-ccy notional per contract is ``size / K``.  This flag
#: keeps that conversion in one place instead of in a chart callback.
QUOTE_DENOMINATED = {"USDJPY", "USDCAD", "USDCHF"}

SIGN_ASSUMPTIONS: tuple[str, ...] = (
    "unsigned", "long_all", "short_all",
    "dealer_short_calls_long_puts", "dealer_long_calls_short_puts",
)


def _sign(assumption: str, cp: np.ndarray) -> np.ndarray:
    a = str(assumption)
    if a == "unsigned":
        return np.ones_like(cp, dtype=float)          # magnitude only
    if a == "long_all":
        return np.ones_like(cp, dtype=float)
    if a == "short_all":
        return -np.ones_like(cp, dtype=float)
    if a == "dealer_short_calls_long_puts":
        return np.where(cp > 0, -1.0, +1.0)
    if a == "dealer_long_calls_short_puts":
        return np.where(cp > 0, +1.0, -1.0)
    raise ValueError(f"sign_assumption must be one of {SIGN_ASSUMPTIONS}, got {assumption!r}")


def market_gamma_profile(oi: pd.DataFrame, mkt: MarketSnapshot, pair: str, *,
                         sign_assumption: str = "unsigned",
                         spot_strikes: bool = True,
                         contract_size: float | None = None,
                         max_expiries: int = 4) -> pd.DataFrame:
    """Listed gamma by strike, and the aggregate profile against spot.

    Parameters
    ----------
    oi : ``strike, expiry, cp, oi, settle`` in **pair strike space** (see module doc).
    sign_assumption : one of :data:`SIGN_ASSUMPTIONS`.  The default ``"unsigned"``
        makes no claim about who is long.  Whatever you choose is carried in the
        output as a column so the caption cannot lose it.
    spot_strikes : divide out the CIP forward factor so strikes sit on the spot axis.

    Returns
    -------
    DataFrame, one row per (strike, expiry, call/put), with ``oi``,
    ``notional_base``, ``T``, ``vol``, ``gamma_1pct_abs``, ``gamma_1pct_raw`` and the
    ``gamma_1pct`` actually implied by ``sign_assumption``, plus the assumption, the
    strike space and the contract size used.  :func:`gamma_profile_curve` turns it
    into the aggregate profile against spot.

    Nothing in the output is called dealer gamma, and nothing may be relabelled as
    such downstream (REQ-023 as rewritten by W-11: unsigned only, "Listed positioning
    (indicative)").
    """
    spec = pair_spec(pair)
    if not len(oi):
        return pd.DataFrame(columns=["pair", "strike", "expiry", "cp", "oi",
                                     "notional_base", "gamma_1pct", "sign_assumption"])
    S = float(mkt.spot[pair])
    rd, rf = mkt.rd_rf(pair, PAIRS)
    size = float(contract_size if contract_size is not None
                 else CONTRACT_SIZE.get(pair, float("nan")))
    if not np.isfinite(size):
        raise KeyError(f"no CME contract size known for {pair}; supply contract_size=. "
                       f"Known: {sorted(CONTRACT_SIZE)}")
    surf = mkt.surfaces.get(pair)
    df = oi.copy()
    expiries = sorted(pd.unique(df["expiry"]))[:max_expiries]
    df = df[df["expiry"].isin(expiries)].copy()

    rows = []
    for _, r in df.iterrows():
        K = float(r["strike"])
        exp = r["expiry"]
        exp_d = exp if isinstance(exp, date) else pd.Timestamp(exp).date()
        T = year_fraction(mkt.asof, exp_d, spec.cut)
        if T <= 0 or K <= 0:
            continue
        if spot_strikes:
            K = K * math.exp(-(rd - rf) * T)
        # base-ccy notional per contract: quote-denominated products divide by K
        n_base = size / K if pair in QUOTE_DENOMINATED else size
        notional = n_base * float(r["oi"])
        sig = float(surf.vol(K, T)) if surf is not None else float("nan")
        if not np.isfinite(sig) or sig <= 0:
            continue
        g = gk.gk_greeks(S, K, T, rd, rf, sig, int(r["cp"]), notional, 1)
        rows.append({"pair": pair, "strike": K, "expiry": exp_d, "cp": int(r["cp"]),
                     "oi": float(r["oi"]), "notional_base": notional,
                     "T": T, "vol": sig,
                     "gamma_1pct_abs": abs(g.gamma_1pct),
                     "gamma_1pct_raw": g.gamma_1pct})
    if not rows:
        return pd.DataFrame(columns=["pair", "strike", "expiry", "cp", "oi",
                                     "notional_base", "gamma_1pct", "sign_assumption"])
    out = pd.DataFrame(rows)
    sgn = _sign(sign_assumption, out["cp"].to_numpy(float))
    out["gamma_1pct"] = (out["gamma_1pct_abs"].to_numpy(float) if sign_assumption == "unsigned"
                         else out["gamma_1pct_raw"].to_numpy(float) * sgn)
    out["sign_assumption"] = sign_assumption
    out["signed"] = sign_assumption != "unsigned"
    out["strike_space"] = "spot" if spot_strikes else "futures"
    out["contract_size"] = size
    out["label"] = "Listed positioning (indicative) -- OI is not net dealer position"
    return out.sort_values(["expiry", "strike"], ignore_index=True)


def gamma_profile_curve(profile: pd.DataFrame, mkt: MarketSnapshot, pair: str, *,
                        span_pct: float = 6.0, n: int = 121) -> pd.DataFrame:
    """Aggregate listed gamma as a function of spot, from :func:`market_gamma_profile`.

    Re-prices every strike at each node of a spot grid, so the curve is the real
    profile rather than a bar chart summed at today's spot.
    """
    if not len(profile):
        return pd.DataFrame(columns=["spot", "gamma_1pct"])
    S0 = float(mkt.spot[pair])
    rd, rf = mkt.rd_rf(pair, PAIRS)
    grid = S0 * (1.0 + np.linspace(-span_pct, span_pct, int(n)) / 100.0)
    sgn = (np.ones(len(profile)) if profile["sign_assumption"].iloc[0] == "unsigned"
           else _sign(profile["sign_assumption"].iloc[0], profile["cp"].to_numpy(float)))
    unsigned = profile["sign_assumption"].iloc[0] == "unsigned"
    tot = np.zeros(grid.size)
    for i, r in profile.reset_index(drop=True).iterrows():
        g = gk.gk_greeks_array(grid, float(r["strike"]), float(r["T"]), rd, rf,
                               float(r["vol"]), int(r["cp"]), float(r["notional_base"]), 1)
        v = np.asarray(g["gamma_1pct"], float)
        tot += np.abs(v) if unsigned else v * sgn[i]
    return pd.DataFrame({"spot": grid, "spot_pct": (grid / S0 - 1.0) * 100.0,
                         "gamma_1pct": tot, "pair": pair,
                         "sign_assumption": profile["sign_assumption"].iloc[0]})


def gamma_flip_level(curve: pd.DataFrame) -> float:
    """Spot level where the (signed) listed gamma profile changes sign.

    Returns ``nan`` for an unsigned profile -- an unsigned curve has no flip, and
    manufacturing one is exactly the fiction this module refuses to sell.
    """
    if not len(curve) or curve["sign_assumption"].iloc[0] == "unsigned":
        return float("nan")
    g = curve["gamma_1pct"].to_numpy(float)
    s = curve["spot"].to_numpy(float)
    sgn = np.sign(g)
    idx = np.where(np.diff(sgn) != 0)[0]
    if not idx.size:
        return float("nan")
    i = int(idx[0])
    if g[i + 1] == g[i]:
        return float(s[i])
    return float(s[i] - g[i] * (s[i + 1] - s[i]) / (g[i + 1] - g[i]))


def strike_magnets(profile: pd.DataFrame, mkt: MarketSnapshot, pair: str, *,
                   top: int = 10) -> pd.DataFrame:
    """The biggest listed-gamma strikes near spot (REQ-025), with distance in pips.

    Ranked on unsigned gamma: a big strike is a big strike whoever owns it.
    """
    if not len(profile):
        return pd.DataFrame(columns=["strike", "gamma_1pct_abs", "dist_pips"])
    spec = pair_spec(pair)
    S = float(mkt.spot[pair])
    g = (profile.groupby("strike", as_index=False)
         .agg(gamma_1pct_abs=("gamma_1pct_abs", "sum"),
              notional_base=("notional_base", "sum"),
              oi=("oi", "sum")))
    g["dist_pips"] = (g["strike"] - S) / spec.pip
    g["dist_pct"] = (g["strike"] - S) / S * 100.0
    g["pair"] = pair
    g["note"] = "unsigned; OI is not net dealer position"
    return g.sort_values("gamma_1pct_abs", ascending=False, ignore_index=True).head(top)


def oi_expiry_ladder(oi: pd.DataFrame, mkt: MarketSnapshot, pair: str, *,
                     n_expiries: int = 8,
                     contract_size: float | None = None) -> pd.DataFrame:
    """Total listed OI notional by expiry (REQ-024), with days to each expiry.

    The expiry *instant* here is the pair's OTC cut, which is **not** the CME product
    calendar (W-11).  The column is named ``otc_cut_used`` to keep that visible: for
    anything time-critical the CME calendar is the source of truth and this ladder is
    a day-count convenience.
    """
    spec = pair_spec(pair)
    size = float(contract_size if contract_size is not None
                 else CONTRACT_SIZE.get(pair, float("nan")))
    if not len(oi):
        return pd.DataFrame(columns=["expiry", "oi", "notional_base", "days"])
    df = oi.copy()
    df["expiry_d"] = [e if isinstance(e, date) else pd.Timestamp(e).date() for e in df["expiry"]]
    g = df.groupby("expiry_d", as_index=False).agg(oi=("oi", "sum"),
                                                   strikes=("strike", "nunique"))
    g["notional_base"] = g["oi"] * size
    g["days"] = [(e - mkt.asof.date()).days for e in g["expiry_d"]]
    g["otc_cut_used"] = spec.cut
    g["caveat"] = "expiry instants from PAIRS[pair].cut, not the CME product calendar"
    g = g.rename(columns={"expiry_d": "expiry"})
    return g.sort_values("expiry", ignore_index=True).head(n_expiries)
