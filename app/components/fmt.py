"""Unit-safe number formatting  (requirements section 5.4; trader review section 3a).

The review lists the exact ways a number lies on a screen.  Each defence below is
one of them:

1. **Silent unit change.**  Vols are stored as decimals and rendered as percent;
   the ``x100`` happens *here* and nowhere else, and every vol string carries ``%``.
2. **JPY rounding.**  A JPY pair prices to 3 dp and its pip is ``0.01``; everything
   else prices to 5 dp with a ``0.0001`` pip.  ``pip`` always comes from ``PAIRS``.
3. **Distance shown one way only.**  ``move_both`` prints pips *and* percent, always
   together, because either alone is unreadable across pairs.
4. **Zero standing in for missing.**  ``dash_if_none`` renders ``None``/``NaN`` as an
   em dash; an exact zero renders ``0``.  They never look the same.
5. **A Greek without its unit.**  ``GREEK_UNITS`` carries the unit for every field of
   ``Greeks``; cards and table headers pull the label from here, so ``gamma_1pct`` is
   never shown without "delta change per +1% spot".
6. **A confident number that is not additive.**  ``delta_pct`` and ``dual_delta``
   aggregate to ``nan`` by contract (v1.2 T-2); ``fmt`` renders that as "n/a".
7. **Notional read as premium.**  Notionals render in mm with the *base* ccy, money
   renders with thousands separators and the *quote* ccy - different shapes on purpose.
8. **Scientific notation.**  Never emitted, at any magnitude.
"""
from __future__ import annotations

import math
from typing import Any

from fxgamma.conventions import PAIRS, pair_spec

EM_DASH = "—"


def _isnan(x: Any) -> bool:
    try:
        return x is None or (isinstance(x, float) and math.isnan(x))
    except TypeError:
        return True


def dash_if_none(x: Any, unavailable: str = EM_DASH) -> str | None:
    """``None``/NaN -> em dash. Returns ``None`` when the value is present."""
    if _isnan(x):
        return unavailable
    if isinstance(x, float) and math.isinf(x):
        return unavailable
    return None


def _fixed(x: float, dp: int) -> str:
    """Half-up fixed-point, never scientific, with thousands separators above 1e4."""
    if abs(x) >= 1e4 or dp == 0:
        return f"{x:,.{dp}f}"
    return f"{x:.{dp}f}"


# ------------------------------------------------------------------ prices
def price_dp(pair: str) -> int:
    """5 dp for a 1e-4 pip pair, 3 dp for a JPY (1e-2 pip) pair - i.e. pip/10."""
    try:
        pip = pair_spec(pair).pip
    except KeyError:
        pip = 1e-4
    return 3 if pip >= 1e-2 else 5


def fmt_spot(x: Any, pair: str = "EURUSD") -> str:
    d = dash_if_none(x)
    return d if d else _fixed(float(x), price_dp(pair))


fmt_strike = fmt_spot


def pips_between(a: float, b: float, pair: str) -> float:
    """``a - b`` expressed in the pair's own pips."""
    return (float(a) - float(b)) / pair_spec(pair).pip


def fmt_pips(x: Any, dp: int = 1, *, signed: bool = True) -> str:
    d = dash_if_none(x)
    if d:
        return d
    s = f"{float(x):+,.{dp}f}" if signed else f"{float(x):,.{dp}f}"
    return f"{s} pips"


def fmt_pct(x: Any, dp: int = 2, *, signed: bool = False) -> str:
    """``x`` is already a percentage number (2.31 -> '2.31%')."""
    d = dash_if_none(x)
    if d:
        return d
    return (f"{float(x):+,.{dp}f}%" if signed else f"{float(x):,.{dp}f}%")


def move_both(new: Any, old: Any, pair: str) -> str:
    """Requirement section 5.4: a move is shown in pips **and** percent, never one alone."""
    if _isnan(new) or _isnan(old) or not old:
        return EM_DASH
    pips = pips_between(new, old, pair)
    pct = (float(new) / float(old) - 1.0) * 100.0
    return f"{pips:+,.1f}p / {pct:+.2f}%"


# ------------------------------------------------------------------ vols
def fmt_vol(x: Any, dp: int = 2) -> str:
    """Decimal vol in, percent out. 0.0705 -> '7.05%'. The only place x100 happens."""
    d = dash_if_none(x)
    if d:
        return d
    return f"{float(x) * 100:,.{dp}f}%"


def fmt_vol_pts(x: Any, dp: int = 2, *, decimal_in: bool = True) -> str:
    """A vol *difference*. Always labelled 'vol pts' so it is never read as a level."""
    d = dash_if_none(x)
    if d:
        return d
    v = float(x) * 100.0 if decimal_in else float(x)
    return f"{v:+,.{dp}f} vol pts"


# ------------------------------------------------------------------ money & size
def fmt_money(x: Any, ccy: str = "", dp: int = 0) -> str:
    """PV / premium / theta / vega: 0 dp, thousands separated, currency first."""
    d = dash_if_none(x)
    if d:
        return f"{ccy} {d}".strip()
    return f"{ccy} {float(x):,.{dp}f}".strip()


def fmt_signed(x: Any, ccy: str = "", dp: int = 0) -> str:
    d = dash_if_none(x)
    if d:
        return f"{ccy} {d}".strip()
    return f"{ccy} {float(x):+,.{dp}f}".strip()


def fmt_mm(x: Any, ccy: str = "", dp: int = 2, *, signed: bool = True) -> str:
    """Notional / delta_base / gamma_1pct: millions, 2 dp, currency named.

    Below 1mm the mm figure is meaningless, so small sizes print in units.
    """
    d = dash_if_none(x)
    if d:
        return f"{ccy} {d}".strip()
    v = float(x)
    if v == 0:
        return f"{ccy} 0".strip()
    if abs(v) < 1e5:
        return (f"{ccy} {v:+,.0f}" if signed else f"{ccy} {v:,.0f}").strip()
    mm = v / 1e6
    return (f"{ccy} {mm:+,.{dp}f}mm" if signed else f"{ccy} {mm:,.{dp}f}mm").strip()


def fmt_delta_base(x: Any, pair: str, *, spot: float | None = None) -> str:
    """MISS-10: a delta is read in base mm, in quote-ccy equivalent and per pip."""
    d = dash_if_none(x)
    if d:
        return d
    spec = pair_spec(pair)
    out = fmt_mm(x, spec.base)
    if spot:
        out += f"  ({fmt_mm(float(x) * float(spot), spec.quote)} eq)"
    return out


def fmt_ccy(pair: str, which: str = "quote") -> str:
    spec = pair_spec(pair)
    return spec.quote if which == "quote" else spec.base


# ------------------------------------------------------------------ Greek units
#: field -> (short label, unit sentence).  Nothing renders a Greek without this.
GREEK_UNITS: dict[str, tuple[str, str]] = {
    "pv": ("PV", "quote ccy, mark-to-market value"),
    "delta_base": ("delta", "base ccy to sell to be flat"),
    "delta_pct": ("delta/unit", "per 1 unit of notional - NOT additive across positions"),
    "gamma": ("gamma", "d(delta_base)/dS, per 1 unit of spot"),
    "gamma_1pct": ("gamma_1pct", "delta change per +1% spot, in base ccy"),
    "vega": ("vega", "quote ccy per +1.00 vol point (0.01 of sigma)"),
    "theta": ("theta", "quote ccy per calendar day; negative = you pay"),
    "rho_d": ("rho_d", "quote ccy per +1.00% domestic rate"),
    "rho_f": ("rho_f", "quote ccy per +1.00% foreign rate"),
    "vanna": ("vanna", "d(vega)/dS: quote ccy per vol pt per 1 unit of spot "
                       "(1 unit = a 100% spot move)"),
    "volga": ("volga", "d(vega)/d(vol): quote ccy per vol point per vol point"),
    "dual_delta": ("dual delta", "d(PV)/dK at this position's own strike - NOT additive"),
}
NON_ADDITIVE = ("delta_pct", "dual_delta")


def greek_label(field: str) -> str:
    return GREEK_UNITS.get(field, (field, ""))[0]


def greek_unit(field: str) -> str:
    return GREEK_UNITS.get(field, (field, ""))[1]


def fmt_greek(field: str, value: Any, pair: str, *, spot: float | None = None) -> str:
    """One entry point so a Greek can never reach the screen in the wrong unit."""
    spec = pair_spec(pair) if pair in PAIRS else None
    base = spec.base if spec else ""
    quote = spec.quote if spec else ""
    if field in NON_ADDITIVE and _isnan(value):
        return "n/a"                              # v1.2 T-2: intensive, not summable
    if field in ("pv", "vega", "theta", "rho_d", "rho_f", "volga"):
        return fmt_money(value, quote)
    if field in ("delta_base", "gamma_1pct"):
        return fmt_mm(value, base)
    if field == "gamma":
        return fmt_mm(value, base)
    if field == "vanna":
        return fmt_money(value, quote)
    if field == "delta_pct":
        return dash_if_none(value) or f"{float(value):+.4f}"
    if field == "dual_delta":
        return dash_if_none(value) or f"{float(value):+,.0f}"
    return dash_if_none(value) or f"{float(value):,.4f}"


def theta_sentence(theta: Any, ccy: str) -> str:
    """W-6(b): theta is signed and the card says it in words."""
    if _isnan(theta):
        return EM_DASH
    v = float(theta)
    if v == 0:
        return "flat theta"
    verb = "you pay" if v < 0 else "you collect"
    return f"{verb} {ccy} {abs(v):,.0f} per calendar day"


def compact_int(x: Any) -> str:
    d = dash_if_none(x)
    return d if d else f"{float(x):,.0f}"
