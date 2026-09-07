"""Book pricing, aggregation, ladders, scenarios and decay.

Implements architecture s5 (`price_book`, `book_greeks`, `spot_ladder`,
`scenario_grid`, `time_decay`) plus the AMENDMENT v1.1 reporting-currency layer
(CG-1: `ccy` / `fx_to_report` columns, `*_rep` copies, `book_greeks(..., report_ccy=)`
and `fx_rate` routing through USD and *raising* on a missing leg) and CG-4
(`time_decay(..., weights=, calendar=)`, calendar time the default).

Units (frozen, see ``fxgamma/models/gk.py``)
--------------------------------------------
Per-position Greeks are in the position's **native quote ccy**, exactly as
``gk_greeks`` returns them.  Nothing in this module changes a per-position number;
the reporting-ccy layer only *adds* columns.

Which Greeks are quote-ccy money and which are base-ccy amounts matters for
aggregation and is the whole point of CG-1:

============================  =========================================
quote-ccy money               ``pv, vega, theta, rho_d, rho_f, volga``
base-ccy amounts              ``delta_base, gamma, gamma_1pct, vanna``
intensive (never additive)    ``delta_pct, dual_delta``  -> nan at book level
============================  =========================================

``vanna`` is ``d(vega)/dS`` in quote ccy per unit of spot, which is *identically*
``d(delta_base)/dsigma`` in base ccy per vol point (MISS-11's desk unit).  It
therefore converts like a base-ccy amount, not like vega.

Conversion
----------
* ``fx_to_report``      = ``fx_rate(quote_ccy, report_ccy)``  -> multiplies quote-ccy money.
* ``fx_base_to_report`` = ``fx_rate(base_ccy, report_ccy)``   -> multiplies base-ccy amounts
  into a report-ccy *value* (``delta_base_rep`` is "the USD value of the base position",
  which is the only representation that can be summed across pairs).

``book_greeks`` converts the quote-ccy money Greeks and sums them.  It sums the
base-ccy amounts **only when every contributing position shares one base ccy**;
otherwise they aggregate to ``nan``, following the T-2 precedent in ``types.Greeks``
(a card that reads "n/a" beats a card that prints EUR+USD+JPY added together).
Pass ``base_as_value=True`` to get them converted to report-ccy value instead, which
is what a cross-pair delta/gamma card needs.

Expiry (T-3)
------------
``conventions.year_fraction`` returns 0.0 past the cut.  ``price_book`` keeps expired
rows and flags them ``expired=True`` with their intrinsic PV and inherited delta;
``book_greeks`` excludes them by default.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from ..conventions import PAIRS, pair_spec, year_fraction
from ..models import gk
from ..models.smile import SmileSurfaceMixin
from ..types import Book, Greeks, MarketSnapshot, OptionPosition, SpotPosition

__all__ = [
    "price_book", "book_greeks", "spot_ladder", "scenario_grid", "time_decay",
    "shift_market", "fx_rate", "ShiftedSurface", "GREEK_COLS", "QUOTE_CCY_GREEKS",
    "BASE_CCY_GREEKS", "NON_ADDITIVE_GREEKS", "position_rows", "gamma_pnl_pct",
    "dhedge_pnl", "STICKY",
]

#: every Greek column ``price_book`` writes, in ``types.Greeks`` field order
GREEK_COLS: tuple[str, ...] = Greeks._FIELDS
#: quote-ccy monetary Greeks -- multiplied by ``fx_to_report``
QUOTE_CCY_GREEKS: tuple[str, ...] = ("pv", "vega", "theta", "rho_d", "rho_f", "volga")
#: base-ccy amounts -- multiplied by ``fx_base_to_report`` to become a report-ccy value
BASE_CCY_GREEKS: tuple[str, ...] = ("delta_base", "gamma", "gamma_1pct", "vanna")
#: intensive quantities that must never be summed (T-2)
NON_ADDITIVE_GREEKS: tuple[str, ...] = Greeks._NON_ADDITIVE

STICKY: tuple[str, ...] = ("strike", "delta", "none")

_DAY = 1.0 / 365.0


# --------------------------------------------------------------------------- #
# the two desk identities, implemented once (requirements s0, trader W-5)
# --------------------------------------------------------------------------- #
def gamma_pnl_pct(gamma_1pct: float, spot: float, move_pct: float) -> float:
    """Gamma P&L (quote ccy) for a spot move of ``move_pct`` **percent**.

    ``0.5 Gamma dS^2`` with ``Gamma = G1/(0.01 S)`` and ``dS = 0.01 x S`` gives
    ``0.005 * G1 * S * x^2`` -- requirements s0.
    """
    return 0.005 * float(gamma_1pct) * float(spot) * float(move_pct) ** 2


def dhedge_pnl(gamma_1pct: float, spot: float, sigma_r: float, sigma_i: float,
               dt_years: float) -> float:
    """Net delta-hedged P&L (quote ccy) over ``dt_years``: gamma income *net of* theta.

    ``0.5 Gamma S^2 (sigma_r^2 - sigma_i^2) dt`` with ``Gamma = G1/(0.01 S)``
    ``= 50 * G1 * S * (sigma_r^2 - sigma_i^2) * dt_years``.

    This is the trader's W-5 correction: REQ-046's ``0.5*Gamma$*(sr^2-si^2)*dt`` is
    out by 100x given the document's own ``Gamma$ = G1*S``.  At ``sigma_r == sigma_i``
    the path is flat (gamma pays exactly the theta bill); at ``sigma_r == 0`` it
    reduces to the gamma-theta path.
    """
    return 50.0 * float(gamma_1pct) * float(spot) * (float(sigma_r) ** 2 - float(sigma_i) ** 2) * float(dt_years)


# --------------------------------------------------------------------------- #
# FX conversion (CG-1)
# --------------------------------------------------------------------------- #
def _usd_per(ccy: str, mkt: MarketSnapshot, pairs: Mapping[str, Any] = PAIRS) -> float:
    """USD per 1 unit of ``ccy``, from the snapshot's spot dict. Raises if absent."""
    c = ccy.upper()
    if c == "USD":
        return 1.0
    for sym, spec in pairs.items():
        if sym not in mkt.spot:
            continue
        s = float(mkt.spot[sym])
        if not (np.isfinite(s) and s > 0.0):
            continue
        if spec.base == c and spec.quote == "USD":
            return s
        if spec.quote == c and spec.base == "USD":
            return 1.0 / s
    wanted = [sym for sym, sp in pairs.items() if "USD" in (sp.base, sp.quote)
              and c in (sp.base, sp.quote)]
    raise KeyError(
        f"fx_rate: no USD leg for {c}; snapshot has spot for {sorted(mkt.spot)}. "
        f"Supply one of {wanted or ['a USD pair for ' + c]} (or a user_override). "
        "Defaulting to 1.0 is forbidden by AMENDMENT v1.1 CG-1."
    )


def fx_rate(ccy: str, report_ccy: str, mkt: MarketSnapshot,
            pairs: Mapping[str, Any] = PAIRS) -> float:
    """Multiplier turning an amount in ``ccy`` into ``report_ccy``.

    Routes through USD (``ccy -> USD -> report_ccy``) and **raises** ``KeyError``
    naming the missing leg rather than defaulting to 1.0 (AMENDMENT v1.1 CG-1,
    same discipline as ``MarketSnapshot.rd_rf`` after T-4).

    >>> # JPY theta into USD when USDJPY = 147.5  ->  1/147.5
    """
    a, b = ccy.upper(), report_ccy.upper()
    if a == b:
        return 1.0
    return _usd_per(a, mkt, pairs) / _usd_per(b, mkt, pairs)


# --------------------------------------------------------------------------- #
# surface shifting
# --------------------------------------------------------------------------- #
@dataclass
class ShiftedSurface(SmileSurfaceMixin):
    """A ``VolSurface`` view of another surface under a spot/vol shift.

    ``sticky`` controls the *smile dynamic*, which is the entire difference between
    the two ladder curves the trader asked to overlay (REQ-040, W-10b):

    ``"strike"``
        ``sigma(K, T)`` is unchanged as spot moves.  The vol attached to each of your
        strikes stays put, so the reported delta is the plain Black-Scholes delta.
    ``"delta"``
        ``sigma`` is a function of moneyness: ``sigma_new(K) = sigma_old(K * S0/S1)``.
        The whole smile rides with spot, which injects the skew delta
        ``nu * dsigma/dS`` into every delta on the ladder.
    ``"none"``
        No re-evaluation of the surface at all: each position keeps the vol it had at
        the base spot.  For a surface parameterised in absolute strike (all of ours)
        this is numerically identical to ``"strike"``; it exists so a caller can pin
        vols even when a surface implementation carries its own spot dependence, and
        as the cheap path for large sweeps.
    """
    base: Any
    pair: str
    asof: datetime
    spot: float
    rd: float
    rf: float
    delta_convention: str = "spot"
    spot_mult: float = 1.0
    vol_add: float = 0.0
    sticky: str = "strike"
    method: str = "shifted"

    def _map_strike(self, K: Any) -> Any:
        if self.sticky == "delta" and self.spot_mult != 1.0:
            return np.asarray(K, float) / float(self.spot_mult)
        return K

    def _vol_impl(self, K: Any, T: float) -> Any:
        Ke = self._map_strike(K)
        if np.ndim(Ke) == 0:
            return float(self.base.vol(float(Ke), float(T))) + self.vol_add
        return np.asarray(self.base.slice(float(T), np.asarray(Ke, float)), float) + self.vol_add


def shift_market(mkt: MarketSnapshot, *, spot_mult: Mapping[str, float] | float | None = None,
                 spot_abs: Mapping[str, float] | None = None,
                 vol_add: float = 0.0, days: float = 0.0,
                 rate_add: Mapping[str, float] | None = None,
                 sticky: str = "strike") -> MarketSnapshot:
    """Return a shifted copy of ``mkt`` for scenarios.  ``types.MarketSnapshot.bump``
    delegates here (the dataclass deliberately owns no logic).

    Parameters
    ----------
    spot_mult : multiplier per pair (or one multiplier for every pair).
    spot_abs : absolute spot per pair; overrides ``spot_mult`` for those pairs.
    vol_add : parallel vol shift in **decimals** (0.01 = 1 vol point).
    days : calendar days forward; advances ``asof`` only.  Spot and the surface
        (as a function of strike and *remaining* T) are frozen, which is what
        "what if I do nothing" means.
    rate_add : per-ccy parallel rate shift in decimals.
    sticky : smile dynamic for the spot shift -- see :class:`ShiftedSurface`.

    Provenance (arch s7) is carried through and annotated: every shifted field keeps
    its original ``Provenance`` with a ``scenario`` note appended, so the UI can badge
    a scenario number as derived rather than observed.
    """
    if sticky not in STICKY:
        raise ValueError(f"sticky must be one of {STICKY}, got {sticky!r}")
    out = MarketSnapshot(asof=mkt.asof + timedelta(days=float(days)),
                         spot=dict(mkt.spot), rates=dict(mkt.rates),
                         surfaces=dict(mkt.surfaces),
                         forwards={k: dict(v) for k, v in mkt.forwards.items()},
                         meta=dict(mkt.meta))
    mults: dict[str, float] = {}
    for p in list(out.spot):
        m = 1.0
        if isinstance(spot_mult, Mapping):
            m = float(spot_mult.get(p, 1.0))
        elif spot_mult is not None:
            m = float(spot_mult)
        if spot_abs and p in spot_abs:
            m = float(spot_abs[p]) / float(out.spot[p])
        mults[p] = m
        out.spot[p] = float(out.spot[p]) * m
    if rate_add:
        for c, dr in rate_add.items():
            if c in out.rates:
                out.rates[c] = float(out.rates[c]) + float(dr)

    for p, surf in list(out.surfaces.items()):
        m = mults.get(p, 1.0)
        if m == 1.0 and vol_add == 0.0:
            continue
        spec = PAIRS.get(p)
        rd = out.rates.get(spec.quote, getattr(surf, "rd", 0.0)) if spec else getattr(surf, "rd", 0.0)
        rf = out.rates.get(spec.base, getattr(surf, "rf", 0.0)) if spec else getattr(surf, "rf", 0.0)
        out.surfaces[p] = ShiftedSurface(
            base=surf, pair=p, asof=out.asof, spot=out.spot.get(p, getattr(surf, "spot", 1.0)),
            rd=float(rd), rf=float(rf),
            delta_convention=getattr(surf, "delta_convention", "spot"),
            spot_mult=m, vol_add=float(vol_add), sticky=sticky)
    note = f"scenario: spot x{mults}, vol {vol_add:+.4f}, {days:+g}d, sticky={sticky}"
    for k, pv in list(out.meta.items()):
        try:
            out.meta[k] = type(pv)(source=pv.source, kind=pv.kind, asof=out.asof,
                                   note=(pv.note + " | " + note)[:400])
        except Exception:                                    # pragma: no cover
            pass
    return out


# --------------------------------------------------------------------------- #
# per-position pricing
# --------------------------------------------------------------------------- #
def _surface(mkt: MarketSnapshot, pair: str):
    try:
        return mkt.surfaces[pair]
    except KeyError as exc:
        raise KeyError(
            f"no vol surface for {pair} in the snapshot (have {sorted(mkt.surfaces)}). "
            "Refusing to substitute another pair's or a flat vol -- architecture s7."
        ) from exc


def position_rows(book: Book, mkt: MarketSnapshot, *,
                  marks: Mapping[str, float] | None = None) -> list[dict[str, Any]]:
    """Raw per-position dicts (no reporting-ccy columns).  Shared by everything here.

    ``marks`` is the CG-2 side-table: ``{position_id: mark_vol}``.  A mark always wins
    over the surface vol and is flagged in ``vol_source``.
    """
    marks = marks or {}
    rows: list[dict[str, Any]] = []
    asof = mkt.asof
    for o in book.options:
        spec = pair_spec(o.pair)
        S = mkt.spot.get(o.pair)
        if S is None:
            raise KeyError(f"no spot for {o.pair} (position {o.id}); have {sorted(mkt.spot)}")
        rd, rf = mkt.rd_rf(o.pair, PAIRS)
        T = year_fraction(asof, o.expiry, o.cut)
        expired = T <= 0.0
        if o.id in marks and np.isfinite(marks[o.id]):
            sigma, vsrc = float(marks[o.id]), "mark"
        elif expired:
            sigma, vsrc = 0.0, "expired"
        else:
            sigma, vsrc = float(_surface(mkt, o.pair).vol(o.strike, T)), "surface"
        g = gk.gk_greeks(S, o.strike, T, rd, rf, sigma, o.cp,
                         abs(o.notional_base), o.direction,
                         delta_convention=spec.delta_convention)
        row: dict[str, Any] = {
            "id": o.id, "pair": o.pair, "kind": "option", "tag": o.tag,
            "cp": int(o.cp), "strike": float(o.strike), "expiry": o.expiry,
            "cut": o.cut, "T": float(T), "days_to_expiry": float(T * 365.0),
            "expired": bool(expired), "notional_base": float(abs(o.notional_base)),
            "direction": int(o.direction),
            "signed_notional": float(o.direction * abs(o.notional_base)),
            "spot": float(S), "vol": float(sigma), "vol_source": vsrc,
            "rd": float(rd), "rf": float(rf),
            "ccy": spec.quote, "base_ccy": spec.base,
            "delta_convention": spec.delta_convention,
            "premium_paid": float(o.premium_paid),
            "premium_ccy": o.premium_ccy or spec.quote,
        }
        row.update(g.as_dict())
        row["pnl_since_trade"] = row["pv"] - float(o.premium_paid)
        rows.append(row)

    for s in book.spots:
        spec = pair_spec(s.pair)
        S = mkt.spot.get(s.pair)
        if S is None:
            raise KeyError(f"no spot for {s.pair} (position {s.id}); have {sorted(mkt.spot)}")
        rd, rf = mkt.rd_rf(s.pair, PAIRS)
        N = float(s.notional_base)
        row = {
            "id": s.id, "pair": s.pair, "kind": "spot", "tag": s.tag,
            "cp": 0, "strike": float(s.entry_rate), "expiry": s.value_date,
            "cut": "", "T": 0.0, "days_to_expiry": 0.0, "expired": False,
            "notional_base": abs(N), "direction": int(np.sign(N)) or 1,
            "signed_notional": N, "spot": float(S), "vol": float("nan"),
            "vol_source": "n/a", "rd": float(rd), "rf": float(rf),
            "ccy": spec.quote, "base_ccy": spec.base,
            "delta_convention": spec.delta_convention,
            "premium_paid": 0.0, "premium_ccy": spec.quote,
        }
        row.update({f: 0.0 for f in GREEK_COLS})
        row["pv"] = N * (float(S) - float(s.entry_rate))     # mark-to-market vs entry
        row["delta_base"] = N
        row["delta_pct"] = float("nan")
        row["dual_delta"] = float("nan")
        row["pnl_since_trade"] = row["pv"]
        rows.append(row)
    return rows


def price_book(book: Book, mkt: MarketSnapshot, *, report_ccy: str = "USD",
               marks: Mapping[str, float] | None = None) -> pd.DataFrame:
    """One row per position with its Greeks in native quote ccy, plus the CG-1
    reporting-ccy columns.

    Columns
    -------
    identity
        ``id, pair, kind, tag, cp, strike, expiry, cut, T, days_to_expiry, expired,
        notional_base, direction, signed_notional, spot, vol, vol_source, rd, rf,
        delta_convention, premium_paid, premium_ccy, pnl_since_trade``
    Greeks (native quote ccy / base ccy -- see the module docstring)
        ``pv, delta_base, delta_pct, gamma, gamma_1pct, vega, theta, rho_d, rho_f,
        vanna, volga, dual_delta``
    conversion (AMENDMENT v1.1 CG-1)
        ``ccy, base_ccy, report_ccy, fx_to_report, fx_base_to_report`` and a ``*_rep``
        copy of every monetary Greek.  ``delta_base_rep`` / ``gamma_1pct_rep`` /
        ``gamma_rep`` / ``vanna_rep`` are report-ccy **values** (base amount x spot x fx),
        the only form that may be summed across pairs.
    """
    rows = position_rows(book, mkt, marks=marks)
    if not rows:
        cols = (["id", "pair", "kind", "tag", "cp", "strike", "expiry", "cut", "T",
                 "days_to_expiry", "expired", "notional_base", "direction",
                 "signed_notional", "spot", "vol", "vol_source", "rd", "rf",
                 "delta_convention", "premium_paid", "premium_ccy", "pnl_since_trade",
                 "ccy", "base_ccy", "report_ccy", "fx_to_report", "fx_base_to_report"]
                + list(GREEK_COLS)
                + [f"{c}_rep" for c in QUOTE_CCY_GREEKS + BASE_CCY_GREEKS])
        return pd.DataFrame({c: pd.Series(dtype="object") for c in cols})

    df = pd.DataFrame(rows)
    rep = report_ccy.upper()
    fx_q = {c: fx_rate(c, rep, mkt) for c in df["ccy"].unique()}
    fx_b = {c: fx_rate(c, rep, mkt) for c in df["base_ccy"].unique()}
    df["report_ccy"] = rep
    df["fx_to_report"] = df["ccy"].map(fx_q).astype(float)
    df["fx_base_to_report"] = df["base_ccy"].map(fx_b).astype(float)
    for c in QUOTE_CCY_GREEKS:
        df[f"{c}_rep"] = df[c].astype(float) * df["fx_to_report"]
    for c in BASE_CCY_GREEKS:
        df[f"{c}_rep"] = df[c].astype(float) * df["fx_base_to_report"]
    df["pnl_since_trade_rep"] = df["pnl_since_trade"].astype(float) * df["fx_to_report"]
    return df


def book_greeks(book: Book, mkt: MarketSnapshot, report_ccy: str = "USD", *,
                include_expired: bool = False, base_as_value: bool = False,
                marks: Mapping[str, float] | None = None,
                df: pd.DataFrame | None = None) -> Greeks:
    """Aggregate Greeks **already converted** into ``report_ccy`` (CG-1).

    Quote-ccy money (pv, vega, theta, rho, volga) is converted at ``fx_to_report``
    and summed.  Base-ccy amounts (delta_base, gamma, gamma_1pct, vanna) are summed
    in their native base ccy only when every contributing position shares one base
    ccy; otherwise they come back ``nan`` (T-2 precedent -- "n/a" beats a confident
    wrong number).  ``base_as_value=True`` converts them to report-ccy value instead,
    which is what a cross-pair delta or gamma card needs.

    ``delta_pct`` and ``dual_delta`` are always ``nan`` at book level (T-2).
    Expired legs are excluded by default (T-3); their inherited spot delta is
    surfaced by :func:`fxgamma.portfolio.zones.pin_risk`, not silently added here.
    """
    d = price_book(book, mkt, report_ccy=report_ccy, marks=marks) if df is None else df
    if len(d) and not include_expired:
        d = d[~d["expired"].astype(bool)]
    if not len(d):
        return Greeks.zero()
    out: dict[str, float] = {}
    for c in QUOTE_CCY_GREEKS:
        out[c] = float(d[f"{c}_rep"].sum())
    one_base = d["base_ccy"].nunique() == 1
    for c in BASE_CCY_GREEKS:
        if base_as_value:
            out[c] = float(d[f"{c}_rep"].sum())
        elif one_base:
            out[c] = float(d[c].sum())
        else:
            out[c] = float("nan")
    for c in NON_ADDITIVE_GREEKS:
        out[c] = float("nan")
    return Greeks(**out)


# --------------------------------------------------------------------------- #
# vectorised spot sweep -- the engine behind the ladder and the scenario grid
# --------------------------------------------------------------------------- #
def _sweep(book: Book, mkt: MarketSnapshot, pair: str, mults: np.ndarray, *,
           sticky: str = "strike", vol_add: float = 0.0, days: float = 0.0,
           report_ccy: str = "USD", marks: Mapping[str, float] | None = None,
           ) -> dict[str, np.ndarray]:
    """Greeks of the ``pair`` sub-book at ``spot * mults``, vectorised over the grid.

    One ``gk_greeks_array`` call per position instead of one ``price_book`` per node:
    101 nodes on a 40-line book is ~40 vectorised calls, not 4040 scalar ones.
    """
    if sticky not in STICKY:
        raise ValueError(f"sticky must be one of {STICKY}, got {sticky!r}")
    sub = book.filter(pair)
    spec = pair_spec(pair)
    S0 = float(mkt.spot[pair])
    rd, rf = mkt.rd_rf(pair, PAIRS)
    S = S0 * np.asarray(mults, float)
    n = S.size
    asof = mkt.asof + timedelta(days=float(days))
    marks = marks or {}
    acc = {c: np.zeros(n) for c in GREEK_COLS}
    surf = mkt.surfaces.get(pair)

    for o in sub.options:
        T = year_fraction(asof, o.expiry, o.cut)
        if T <= 0.0:
            continue                                  # T-3: expired legs leave the risk
        if o.id in marks and np.isfinite(marks[o.id]):
            vols = np.full(n, float(marks[o.id]) + vol_add)
        elif surf is None:
            raise KeyError(f"no vol surface for {pair}; cannot build a ladder")
        elif sticky == "delta":
            vols = np.asarray(surf.slice(T, o.strike * S0 / S), float) + vol_add
        else:                                          # "strike" and "none"
            vols = np.full(n, float(surf.vol(o.strike, T)) + vol_add)
        g = gk.gk_greeks_array(S, o.strike, T, rd, rf, vols, o.cp,
                               abs(o.notional_base), o.direction,
                               delta_convention=spec.delta_convention)
        for c in GREEK_COLS:
            acc[c] = acc[c] + np.broadcast_to(np.asarray(g[c], float), (n,))
    for s in sub.spots:
        N = float(s.notional_base)
        acc["pv"] = acc["pv"] + N * (S - float(s.entry_rate))
        acc["delta_base"] = acc["delta_base"] + N
    acc["spot"] = S
    acc["spot_pct"] = (np.asarray(mults, float) - 1.0) * 100.0
    return acc


def spot_ladder(book: Book, mkt: MarketSnapshot, pair: str, *,
                lo_pct: float = -5.0, hi_pct: float = 5.0, n: int = 101,
                sticky: str = "strike", vol_add: float = 0.0, days_fwd: float = 0.0,
                report_ccy: str = "USD",
                marks: Mapping[str, float] | None = None) -> pd.DataFrame:
    """Book Greeks against spot for one pair (architecture s5, REQ-040).

    ``sticky`` is not cosmetic: sticky-strike holds ``sigma(K)`` fixed, sticky-delta
    rides the smile with spot and therefore adds the skew delta ``nu * dsigma/dS`` to
    every delta on the curve.  On a risk-reversal book that difference is a large
    fraction of the hedge, so the convention in force is returned in the frame
    (``sticky`` column) for the figure to name (W-10b).

    Only positions in ``pair`` are swept; other pairs are unaffected by this shock.
    The returned frame carries native-ccy Greeks and ``*_rep`` copies, plus
    ``pnl`` / ``pnl_rep``: the change in PV from the current spot, which is what
    the ladder is actually read for.
    """
    mults = 1.0 + np.linspace(float(lo_pct), float(hi_pct), int(n)) / 100.0
    acc = _sweep(book, mkt, pair, mults, sticky=sticky, vol_add=vol_add,
                 days=days_fwd, report_ccy=report_ccy, marks=marks)
    df = pd.DataFrame({"spot": acc["spot"], "spot_pct": acc["spot_pct"]})
    for c in GREEK_COLS:
        df[c] = acc[c]
    for c in NON_ADDITIVE_GREEKS:
        df[c] = float("nan")
    spec = pair_spec(pair)
    fq = fx_rate(spec.quote, report_ccy, mkt)
    fb = fx_rate(spec.base, report_ccy, mkt)
    for c in QUOTE_CCY_GREEKS:
        df[f"{c}_rep"] = df[c] * fq
    for c in BASE_CCY_GREEKS:
        df[f"{c}_rep"] = df[c] * fb
    base = _sweep(book, mkt, pair, np.array([1.0]), sticky=sticky, days=0.0,
                  report_ccy=report_ccy, marks=marks)
    df["pnl"] = df["pv"] - float(base["pv"][0])
    df["pnl_rep"] = df["pnl"] * fq
    df["sticky"] = sticky
    df["pair"] = pair
    df["report_ccy"] = report_ccy.upper()
    df["days_fwd"] = float(days_fwd)
    return df


def scenario_grid(book: Book, mkt: MarketSnapshot, pair: str,
                  spot_shocks: Sequence[float] | None = None,
                  vol_shocks: Sequence[float] | None = None,
                  days_fwd: float = 0, *, sticky: str = "strike",
                  report_ccy: str = "USD",
                  marks: Mapping[str, float] | None = None) -> pd.DataFrame:
    """P&L and Greeks on a spot x vol grid (REQ-041), long-form.

    ``spot_shocks`` in **percent** (default -5..+5 by 0.5), ``vol_shocks`` in **vol
    points** (default -3..+3 by 0.5).  ``days_fwd`` rolls the clock forward with the
    surface frozen, so the cell answers "spot there, two days from now" rather than
    "spot there, instantly".

    ``pnl`` is measured against the *unshocked, undecayed* PV, so the ``(0, 0)`` cell
    at ``days_fwd=0`` is exactly zero and the theta cost of ``days_fwd`` shows up in
    the whole grid rather than being netted away.
    """
    ss = np.asarray(spot_shocks if spot_shocks is not None else np.arange(-5, 5.01, 0.5), float)
    vs = np.asarray(vol_shocks if vol_shocks is not None else np.arange(-3, 3.01, 0.5), float)
    mults = 1.0 + ss / 100.0
    spec = pair_spec(pair)
    fq = fx_rate(spec.quote, report_ccy, mkt)
    fb = fx_rate(spec.base, report_ccy, mkt)
    base = _sweep(book, mkt, pair, np.array([1.0]), sticky=sticky, marks=marks)
    pv0 = float(base["pv"][0])

    frames = []
    for v in vs:
        acc = _sweep(book, mkt, pair, mults, sticky=sticky, vol_add=float(v) / 100.0,
                     days=float(days_fwd), marks=marks)
        d = pd.DataFrame({"spot_shock_pct": ss, "vol_shock_pts": float(v),
                          "spot": acc["spot"]})
        for c in GREEK_COLS:
            d[c] = acc[c]
        d["pnl"] = d["pv"] - pv0
        d["pnl_rep"] = d["pnl"] * fq
        for c in QUOTE_CCY_GREEKS:
            d[f"{c}_rep"] = d[c] * fq
        for c in BASE_CCY_GREEKS:
            d[f"{c}_rep"] = d[c] * fb
        frames.append(d)
    out = pd.concat(frames, ignore_index=True)
    out["pair"] = pair
    out["days_fwd"] = float(days_fwd)
    out["sticky"] = sticky
    out["report_ccy"] = report_ccy.upper()
    return out


# --------------------------------------------------------------------------- #
# decay (CG-4)
# --------------------------------------------------------------------------- #
#: default business-time day weights (requirements s4.6 with the trader's Q-6 numbers)
DAY_WEIGHTS = {"weekday": 1.0, "weekend": 0.15, "holiday": 0.25}
#: event multipliers by `events.importance` (3 = FOMC/ECB/BoJ/BoE, US CPI, US NFP)
EVENT_WEIGHTS = {3: 2.0, 2: 1.5, 1: 1.1}


def default_weights(asof: datetime, days: Sequence[int], calendar: str = "calendar",
                    events: pd.DataFrame | None = None,
                    holidays: Iterable[date] = ()) -> np.ndarray:
    """Per-day time weights for :func:`time_decay`.

    ``calendar="calendar"`` -> all 1.0 (the default the amendment mandates).
    ``"business"`` -> weekday 1.0, weekend 0.15, holiday 0.25.
    ``"event"`` -> business weights times the event multiplier for any day carrying an
    event from ``events`` (CG-6 frame: ``date, ccy, event, importance``).
    """
    cal = str(calendar).lower()
    if cal not in ("calendar", "business", "event"):
        raise ValueError("calendar must be one of {'calendar','business','event'}")
    ds = [asof.date() + timedelta(days=int(d)) for d in days]
    if cal == "calendar":
        return np.ones(len(ds))
    hol = set(holidays)
    w = np.array([DAY_WEIGHTS["holiday"] if d in hol else
                  (DAY_WEIGHTS["weekend"] if d.weekday() >= 5 else DAY_WEIGHTS["weekday"])
                  for d in ds], float)
    if cal == "event" and events is not None and len(events):
        ev = events.copy()
        col = "date" if "date" in ev.columns else "datetime"
        imp = ev["importance"].astype(int) if "importance" in ev.columns else 3
        by_day: dict[date, int] = {}
        for d, i in zip(pd.to_datetime(ev[col]).dt.date, np.atleast_1d(imp)):
            by_day[d] = max(by_day.get(d, 0), int(i))
        for j, d in enumerate(ds):
            if d in by_day:
                w[j] *= EVENT_WEIGHTS.get(by_day[d], 1.0)
    return w


def time_decay(book: Book, mkt: MarketSnapshot, days: Sequence[int] | range = range(0, 31),
               *, weights: Sequence[float] | None = None, calendar: str = "calendar",
               normalize: bool = True, report_ccy: str = "USD",
               realized_vol: float | Mapping[str, float] | None = None,
               events: pd.DataFrame | None = None,
               holidays: Iterable[date] = (),
               marks: Mapping[str, float] | None = None) -> pd.DataFrame:
    """"What if I do nothing" -- PV and cumulative theta day by day (REQ-046, CG-4).

    Spot and the surface (as a function of strike and remaining T) are frozen; only
    the clock moves.

    Time weighting
    --------------
    ``calendar="calendar"`` (the default the amendment mandates) charges one day of
    time per calendar day.  ``"business"`` / ``"event"`` charge ``weights[i]``
    weekday-equivalents per day (weekend 0.15, holiday 0.25, event days x1.1/1.5/2.0),
    which is how the OTC market actually prices time.  ``weights`` overrides the
    scheme entirely.  With ``normalize=True`` the weights are rescaled to preserve
    total elapsed time over the requested horizon, so the *shape* changes (a cheap
    weekend and expensive weekdays -- the Friday question) while the endpoint does
    not.  The scheme in force is returned in the ``calendar`` column so the figure
    can name it, as REQ-046 requires.

    Realized-vol paths
    ------------------
    ``realized_vol`` (a decimal, or per-pair mapping) adds ``dh_pnl_rep``: the net
    delta-hedged P&L path ``50*G1*S*(sigma_r^2 - sigma_i^2)*dt`` (:func:`dhedge_pnl`,
    the W-5 correction).  ``dh_pnl_rv0_rep`` is the same at ``sigma_r = 0`` and
    reduces to the gamma-theta path; at ``sigma_r = sigma_i`` the path is flat.
    """
    days = [int(d) for d in days]
    w = (np.asarray(weights, float) if weights is not None
         else default_weights(mkt.asof, days, calendar, events, holidays))
    if w.size != len(days):
        raise ValueError(f"weights has {w.size} entries, days has {len(days)}")
    # steps[i] = calendar days between node i-1 and node i (node -1 == asof)
    steps = np.diff(np.asarray(days, float), prepend=0.0)
    if normalize and float(np.sum(w * steps)) > 0.0:
        w = w * (float(np.sum(steps)) / float(np.sum(w * steps)))
    eff = np.cumsum(w * steps)                       # weekday-equivalent days elapsed

    rows = []
    pv0 = None
    for i, d in enumerate(days):
        shifted = shift_market(mkt, days=float(eff[i]))
        df = price_book(book, shifted, report_ccy=report_ccy, marks=marks)
        live = df[~df["expired"].astype(bool)] if len(df) else df
        pv = float(df["pv_rep"].sum()) if len(df) else 0.0
        if pv0 is None:
            pv0 = pv
        row = {
            "day": d, "date": (mkt.asof + timedelta(days=float(d))).date(),
            "weight": float(w[i]), "eff_days": float(eff[i]),
            "calendar": str(calendar) if weights is None else "custom",
            "pv_rep": pv, "dpv_rep": pv - pv0,
            "theta_rep": float(live["theta_rep"].sum()) if len(live) else 0.0,
            "gamma_1pct_rep": float(live["gamma_1pct_rep"].sum()) if len(live) else 0.0,
            "vega_rep": float(live["vega_rep"].sum()) if len(live) else 0.0,
            "n_live": int(len(live)), "n_expired": int(len(df) - len(live)),
            "report_ccy": report_ccy.upper(),
        }
        # net delta-hedged path per pair, then converted
        dh = dh0 = 0.0
        if len(live):
            for pair, sub in live.groupby("pair"):
                S = float(sub["spot"].iloc[0])
                g1 = float(sub["gamma_1pct"].sum())
                sig_i = float(np.average(sub["vol"], weights=np.abs(sub["vega"]) + 1e-12))
                fq = float(sub["fx_to_report"].iloc[0])
                dt = float(w[i] * steps[i]) * _DAY
                if realized_vol is not None:
                    sr = (float(realized_vol[pair]) if isinstance(realized_vol, Mapping)
                          else float(realized_vol))
                    dh += dhedge_pnl(g1, S, sr, sig_i, dt) * fq
                dh0 += dhedge_pnl(g1, S, 0.0, sig_i, dt) * fq
        row["dh_pnl_rep"] = dh
        row["dh_pnl_rv0_rep"] = dh0
        rows.append(row)
    out = pd.DataFrame(rows)
    # cumulative theta over each step, trapezoidal, charged in weighted days.  This is
    # the *predicted* decay; `dpv_rep` is the repriced one.  They agree to O(step^2).
    th = out["theta_rep"].to_numpy(float)
    th_avg = np.concatenate([[th[0]], 0.5 * (th[1:] + th[:-1])])
    out["cum_theta_rep"] = np.cumsum(th_avg * steps * w)
    out["cum_dh_pnl_rep"] = out["dh_pnl_rep"].cumsum()
    out["cum_dh_pnl_rv0_rep"] = out["dh_pnl_rv0_rep"].cumsum()
    return out
