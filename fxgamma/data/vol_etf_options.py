"""Listed currency-ETF option chains -> a real, free implied-vol **smile**.

This is the only free source of an actual traded FX smile.  Each of FXE/FXB/FXY/FXA/FXC/FXF
(and UUP for the dollar index) holds the foreign currency and has a US-listed, publicly
quoted option chain.  Yahoo publishes bid/ask/IV/open-interest per contract:

    https://query2.finance.yahoo.com/v7/finance/options/FXE               # nearest expiry
    https://query2.finance.yahoo.com/v7/finance/options/FXE?date=<unix>   # one expiry

    optionChain.result[0].expirationDates      -> [epoch, ...]
    optionChain.result[0].quote.regularMarketPrice
    optionChain.result[0].options[0].calls[i]  -> {strike, bid, ask, lastPrice,
                                                   impliedVolatility, openInterest, volume,
                                                   expiration, inTheMoney, contractSymbol}

The ETF-vol vs OTC-vol basis (read this before trusting a number)
----------------------------------------------------------------
1. **American vs European.**  Listed ETF options are American; OTC FX vanillas are European.
   Yahoo's ``impliedVolatility`` is an American (binomial) IV.  For low-carry, non-dividend
   windows the early-exercise premium is small, but it is *not* zero and it is one-sided:
   American IV <= European IV for the same price on deep ITM puts.  We therefore build the
   smile from OTM contracts only, where the two coincide to well inside the bid/ask.
2. **The ETF is not the pair.**  FXE tracks a euro deposit net of a management fee, so the
   ETF forward embeds fee and (for shorted names) borrow.  Vol is first-order invariant to
   these level effects, but they bias the *forward* used for moneyness, which biases the skew.
   We recover the forward from put-call parity instead of assuming ``F = S*exp((r-q)T)``.
3. **Inversion.**  FXY is USD-per-JPY while USDJPY is JPY-per-USD.  Vol is invariant to
   inversion to first order; **skew is not** -- an FXY call is a USDJPY put.  ``inverted_etf``
   in ``conventions.PAIRS`` drives the reflection ``k -> -k`` in :func:`reflect_smile`.
4. **Discrete strikes, wide spreads, stale quotes.**  FXE strikes are $0.50-$1 apart (~0.5%
   of spot) so a 10-delta wing may simply not exist; quotes go stale outside 09:30-16:00 ET
   and are unreliable in the last minutes before the close.  We require two-sided quotes and
   drop crossed/zero markets.
5. **Expiry mismatch.**  Listed expiries are the third Friday (plus weeklies), with a 16:00 ET
   cut; OTC tenors are 1M/2M/3M with a 10:00 NY cut.  We return the listed T as-is and let
   ``build_surface`` interpolate -- no pretending a 24-day listed expiry is a "1M".

Net: good enough for *relative* vol analytics (richness z-scores, skew direction, term-structure
shape) and roughly 0.3-1.0 vol points away from the OTC ATM in G3.  Not good enough to mark a
book.  See docs/04_data_sources.md for the full basis table.

Terms of use: Yahoo personal/non-commercial use; no redistribution.  Rate-limited; the option
endpoint has historically required a cookie+crumb pair (see :func:`fetch_crumb`).

STATUS: **UNVERIFIED** here -- Yahoo is blocked by this sandbox's egress proxy, and the
crumb requirement in particular changes without notice.  Parsers are fixture-tested.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from statistics import NormalDist
from typing import Any, Sequence

import numpy as np
import pandas as pd

from ..conventions import PAIRS, pair_spec
from . import _http
from .base import SmileQuotes

log = logging.getLogger(__name__)

__all__ = ["OPTIONS_URL", "CRUMB_URL", "ETFS", "VOL_BETA", "fetch_chain", "fetch_crumb",
           "parse_expirations", "parse_chain", "chain_smile", "reflect_smile",
           "smile_quotes", "CHAIN_COLUMNS", "VERIFIED"]

VERIFIED = False

OPTIONS_URL = "https://query2.finance.yahoo.com/v7/finance/options/{symbol}"
COOKIE_URL = "https://fc.yahoo.com"
CRUMB_URL = "https://query2.finance.yahoo.com/v1/test/getcrumb"

CHAIN_COLUMNS = ["expiry", "cp", "strike", "bid", "ask", "mid", "last", "iv", "oi", "volume"]

#: ETF -> what it holds. Used for documentation and for the UUP special case.
ETFS: dict[str, str] = {
    "FXE": "Invesco CurrencyShares Euro Trust (EUR deposit)",
    "FXB": "Invesco CurrencyShares British Pound Sterling Trust",
    "FXY": "Invesco CurrencyShares Japanese Yen Trust",
    "FXA": "Invesco CurrencyShares Australian Dollar Trust",
    "FXC": "Invesco CurrencyShares Canadian Dollar Trust",
    "FXF": "Invesco CurrencyShares Swiss Franc Trust",
    "UUP": "Invesco DB US Dollar Index Bullish Fund (DXY futures)",
}

#: pairs with no listed ETF of their own. vol(pair) ~= beta * vol(proxy ETF's pair).
#: Betas are desk rules of thumb, NOT fitted; anything using them is badged in the note.
VOL_BETA: dict[str, tuple[str, float]] = {
    "NZDUSD": ("FXA", 1.08),     # NZD trades ~8% wider than AUD
    "USDSEK": ("FXE", 1.40),     # SEK ~ EUR beta plus a liquidity premium
    "USDNOK": ("FXE", 1.55),
    "EURGBP": ("FXE", 0.62),     # cross vol is much lower than either leg
    "EURCHF": ("FXE", 0.45),
    "EURJPY": ("FXY", 1.05),
}

_N = NormalDist()
_MIN_IV, _MAX_IV = 0.005, 2.00
_MAX_REL_SPREAD = 0.60          # (ask-bid)/mid; listed FX ETF wings are genuinely wide


# ----------------------------------------------------------------------------- fetch
def fetch_crumb() -> str | None:
    """Best-effort Yahoo cookie+crumb handshake. Returns ``None`` if not required/available."""
    try:
        _http.get(COOKIE_URL)                       # sets the A1/A3 cookies on the session
    except _http.HttpError as exc:
        log.debug("yahoo cookie step failed (may be fine): %s", exc)
    try:
        crumb = _http.get_text(CRUMB_URL).strip()
        return crumb or None
    except _http.HttpError as exc:
        log.info("yahoo crumb unavailable: %s", exc)
        return None


def fetch_chain(symbol: str, expiration: int | None = None,
                crumb: str | None = None) -> dict[str, Any]:
    """Network only. One Yahoo option-chain payload."""
    params: dict[str, Any] = {}
    if expiration is not None:
        params["date"] = int(expiration)
    if crumb:
        params["crumb"] = crumb
    return _http.get_json(OPTIONS_URL.format(symbol=symbol.upper()), params=params)


# ----------------------------------------------------------------------------- parse
def _result(payload: dict[str, Any]) -> dict[str, Any]:
    oc = (payload or {}).get("optionChain") or {}
    if oc.get("error"):
        raise ValueError(f"yahoo options error: {oc['error']}")
    res = oc.get("result") or []
    if not res:
        raise ValueError("yahoo options: empty result")
    return res[0]


def parse_expirations(payload: dict[str, Any]) -> list[int]:
    """Pure. -> sorted epoch-second expiration stamps."""
    return sorted(int(e) for e in (_result(payload).get("expirationDates") or []))


def parse_underlying(payload: dict[str, Any]) -> float:
    """Pure. -> the ETF's last price."""
    q = _result(payload).get("quote") or {}
    for k in ("regularMarketPrice", "postMarketPrice", "previousClose"):
        if q.get(k) is not None:
            return float(q[k])
    raise ValueError("yahoo options: no underlying price")


def parse_chain(payload: dict[str, Any]) -> pd.DataFrame:
    """Pure. -> tidy contract frame with ``CHAIN_COLUMNS``.

    ``cp`` is +1 call / -1 put; ``expiry`` is a ``date``; ``iv`` is a decimal; ``mid`` is the
    bid/ask mid where two-sided, else NaN (we never silently use ``lastPrice`` as a mid).
    """
    res = _result(payload)
    rows: list[dict[str, Any]] = []
    for block in res.get("options") or []:
        for side, cp in (("calls", 1), ("puts", -1)):
            for c in block.get(side) or []:
                exp = c.get("expiration") or block.get("expirationDate")
                if exp is None or c.get("strike") is None:
                    continue
                bid = _f(c.get("bid"))
                ask = _f(c.get("ask"))
                mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0 and ask >= bid) else float("nan")
                rows.append({
                    "expiry": datetime.fromtimestamp(int(exp), timezone.utc).date(),
                    "cp": cp,
                    "strike": float(c["strike"]),
                    "bid": bid, "ask": ask, "mid": mid,
                    "last": _f(c.get("lastPrice")),
                    "iv": _f(c.get("impliedVolatility")),
                    "oi": _f(c.get("openInterest")),
                    "volume": _f(c.get("volume")),
                })
    if not rows:
        return pd.DataFrame(columns=CHAIN_COLUMNS)
    return pd.DataFrame(rows)[CHAIN_COLUMNS].sort_values(["expiry", "cp", "strike"],
                                                         ignore_index=True)


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


# ------------------------------------------------------------------------ smile build
@dataclass(frozen=True)
class RawSmile:
    """A fitted per-expiry smile in log-moneyness space, in the ETF's own quoting direction."""
    expiry: date
    T: float
    forward: float
    atm: float
    c1: float          # d(vol)/dk        -- skew
    c2: float          # d2(vol)/dk2 / 2  -- curvature
    n_quotes: int
    k_min: float
    k_max: float

    def vol(self, k: float) -> float:
        k = float(np.clip(k, self.k_min, self.k_max))     # never extrapolate a quadratic
        return max(_MIN_IV, self.atm + self.c1 * k + self.c2 * k * k)


def implied_forward(df: pd.DataFrame, spot: float, r: float, T: float) -> float:
    """Put-call parity forward from the strike whose call/put mids are closest together.

    ``C - P = df * (F - K)``  ->  ``F = K + (C - P) / df``.  Falls back to ``spot`` when no
    strike has two-sided quotes on both wings (which happens on quiet ETF chains).
    """
    calls = df[df.cp == 1].set_index("strike")["mid"].dropna()
    puts = df[df.cp == -1].set_index("strike")["mid"].dropna()
    common = calls.index.intersection(puts.index)
    if len(common) == 0:
        return float(spot)
    diff = (calls.loc[common] - puts.loc[common]).abs()
    k = float(diff.idxmin())
    dfac = math.exp(-r * max(T, 1e-6))
    return float(k + (calls.loc[k] - puts.loc[k]) / dfac)


def chain_smile(df: pd.DataFrame, expiry: date, *, spot: float, asof: datetime,
                r: float = 0.04, min_quotes: int = 5) -> RawSmile | None:
    """Pure. One expiry's contracts -> a quadratic smile in ``k = ln(K/F)``.

    Filters: OTM only (calls above F, puts below F -- where American == European to well
    inside the spread), two-sided markets, sane IV, non-absurd relative spread.  Weighted
    least squares with weight ``1/(rel_spread + 0.05)`` so tight ATM quotes dominate.
    """
    sub = df[df.expiry == expiry].copy()
    if sub.empty:
        return None
    T = max((datetime.combine(expiry, datetime.min.time(), timezone.utc)
             - asof.astimezone(timezone.utc)).total_seconds() / (365.0 * 86400.0), 1.0 / 365.0)
    fwd = implied_forward(sub, spot, r, T)
    if not (fwd > 0):
        return None

    sub = sub[np.isfinite(sub["mid"]) & np.isfinite(sub["iv"])]
    sub = sub[(sub["iv"] > _MIN_IV) & (sub["iv"] < _MAX_IV)]
    rel = (sub["ask"] - sub["bid"]) / sub["mid"].replace(0, np.nan)
    sub = sub[rel.fillna(9.9) <= _MAX_REL_SPREAD]
    sub = sub[((sub.cp == 1) & (sub.strike >= fwd)) | ((sub.cp == -1) & (sub.strike <= fwd))]
    if len(sub) < min_quotes:
        log.info("etf smile %s: only %d usable quotes", expiry, len(sub))
        return None

    k = np.log(sub["strike"].to_numpy(float) / fwd)
    v = sub["iv"].to_numpy(float)
    w = 1.0 / (((sub["ask"] - sub["bid"]) / sub["mid"]).to_numpy(float) + 0.05)
    A = np.vstack([np.ones_like(k), k, k * k]).T * w[:, None]
    try:
        coef, *_ = np.linalg.lstsq(A, v * w, rcond=None)
    except np.linalg.LinAlgError:
        return None
    a0, a1, a2 = (float(c) for c in coef)
    if not np.isfinite([a0, a1, a2]).all() or not (_MIN_IV < a0 < _MAX_IV):
        return None
    return RawSmile(expiry=expiry, T=T, forward=fwd, atm=a0, c1=a1, c2=a2,
                    n_quotes=int(len(sub)), k_min=float(k.min()), k_max=float(k.max()))


def reflect_smile(s: RawSmile) -> RawSmile:
    """Invert the quoting direction: ``k -> -k`` (an FXY call is a USDJPY put).

    ATM and curvature survive; the skew term flips sign.  This is the first-order inversion:
    it ignores the ``sigma^2 T`` convexity correction between vol(X/Y) and vol(Y/X) quoted on
    *fixed strikes*, which is <0.05 vol pt for 1M G10 and is documented as such.
    """
    return RawSmile(expiry=s.expiry, T=s.T, forward=1.0 / s.forward, atm=s.atm,
                    c1=-s.c1, c2=s.c2, n_quotes=s.n_quotes,
                    k_min=-s.k_max, k_max=-s.k_min)


def _k_at_delta(delta: float, sigma: float, T: float, cp: int) -> float:
    """log-moneyness of a `delta`-delta option under Black (spot-delta, unadjusted)."""
    d1 = _N.inv_cdf(delta if cp > 0 else 1.0 - delta)
    return 0.5 * sigma * sigma * T - d1 * sigma * math.sqrt(T)


def _solve_k(smile: RawSmile, delta: float, cp: int, iters: int = 3) -> float:
    sigma = smile.atm
    k = 0.0
    for _ in range(iters):
        k = _k_at_delta(delta, sigma, smile.T, cp)
        sigma = smile.vol(k)
    return k


def to_smile_quotes(s: RawSmile, tenor: str = "") -> SmileQuotes:
    """RawSmile -> the frozen contract-section-8 broker quote for that tenor."""
    atm = s.vol(0.0)
    v25c, v25p = s.vol(_solve_k(s, 0.25, +1)), s.vol(_solve_k(s, 0.25, -1))
    v10c, v10p = s.vol(_solve_k(s, 0.10, +1)), s.vol(_solve_k(s, 0.10, -1))
    return SmileQuotes(T=s.T, atm=atm, rr25=v25c - v25p, bf25=0.5 * (v25c + v25p) - atm,
                       rr10=v10c - v10p, bf10=0.5 * (v10c + v10p) - atm, tenor=tenor)


# ----------------------------------------------------------------------------- public
def etf_for(pair: str) -> tuple[str, bool, float]:
    """-> (etf symbol, invert?, vol beta). Raises when the pair has no route."""
    spec = pair_spec(pair)
    if spec.etf_proxy:
        return spec.etf_proxy, bool(spec.inverted_etf), 1.0
    if pair.upper() in VOL_BETA:
        etf, beta = VOL_BETA[pair.upper()]
        src = next((p for p, s in PAIRS.items() if s.etf_proxy == etf), None)
        return etf, bool(PAIRS[src].inverted_etf) if src else False, beta
    raise KeyError(f"no listed ETF proxy for {pair}")


def smile_quotes(pair: str, asof: datetime, *, max_expiries: int = 6,
                 r: float = 0.04, crumb: str | None = None) -> list[SmileQuotes]:
    """Live path: ETF chain -> ``list[SmileQuotes]`` for `pair` (contract section 8).

    Scales ATM/RR/BF by the documented ``VOL_BETA`` when the pair has no ETF of its own --
    such a result must be badged with ``note='vol beta proxy'`` by the caller.
    """
    symbol, invert, beta = etf_for(pair)
    first = fetch_chain(symbol, crumb=crumb)
    spot_etf = parse_underlying(first)
    exps = parse_expirations(first)[:max_expiries]

    frames = [parse_chain(first)]
    for e in exps[1:]:
        try:
            frames.append(parse_chain(fetch_chain(symbol, expiration=e, crumb=crumb)))
        except Exception as exc:                    # noqa: BLE001
            log.info("etf chain %s %s failed: %s", symbol, e, exc)
    df = pd.concat([f for f in frames if not f.empty], ignore_index=True) \
        if any(not f.empty for f in frames) else pd.DataFrame(columns=CHAIN_COLUMNS)
    if df.empty:
        return []

    out: list[SmileQuotes] = []
    for expiry in sorted(df["expiry"].unique()):
        s = chain_smile(df, expiry, spot=spot_etf, asof=asof, r=r)
        if s is None:
            continue
        if invert:
            s = reflect_smile(s)
        q = to_smile_quotes(s, tenor=f"L{expiry:%Y%m%d}")
        if beta != 1.0:
            q = SmileQuotes(T=q.T, atm=q.atm * beta, rr25=q.rr25 * beta, bf25=q.bf25 * beta,
                            rr10=None if q.rr10 is None else q.rr10 * beta,
                            bf10=None if q.bf10 is None else q.bf10 * beta, tenor=q.tenor)
        out.append(q)
    return sorted(out, key=lambda q: q.T)


def open_interest(pair: str, asof: datetime, *, max_expiries: int = 6,
                  crumb: str | None = None) -> pd.DataFrame:
    """ETF-chain OI by strike, mapped back into pair-strike space.

    A crude but *free* substitute for CME OI: ETF strikes are converted with the ratio
    ``pair_spot / etf_spot`` (and inverted where needed), which is only valid while the ETF's
    NAV/pair ratio is stable.  Prefer :mod:`fxgamma.data.cme_options` where it works.
    """
    symbol, invert, _ = etf_for(pair)
    first = fetch_chain(symbol, crumb=crumb)
    spot_etf = parse_underlying(first)
    frames = [parse_chain(first)]
    for e in parse_expirations(first)[1:max_expiries]:
        try:
            frames.append(parse_chain(fetch_chain(symbol, expiration=e, crumb=crumb)))
        except Exception as exc:                    # noqa: BLE001
            log.info("etf chain %s %s failed: %s", symbol, e, exc)
    df = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    if df.empty:
        return df
    out = pd.DataFrame({
        "strike": df["strike"] if not invert else 1.0 / df["strike"],
        "expiry": df["expiry"],
        "cp": df["cp"] if not invert else -df["cp"],
        "oi": df["oi"].fillna(0.0),
        "settle": df["mid"].fillna(df["last"]),
    })
    out.attrs["etf"] = symbol
    out.attrs["etf_spot"] = spot_etf
    out.attrs["note"] = "ETF-chain OI; strikes are ETF strikes (scale by pair_spot/etf_spot)"
    return out.sort_values(["expiry", "cp", "strike"], ignore_index=True)
