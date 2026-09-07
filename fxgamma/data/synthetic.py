"""Deterministic synthetic market data. **Everything here is fake and badged as such.**

Purpose: the build sandbox and CI have no access to any market-data host, and the app must
still render every screen and run every backtest.  ``SyntheticProvider(seed=...)`` produces a
fully self-consistent market -- spot paths, term structure, smile, open interest, calendar --
that is *bit-for-bit reproducible* from the seed and plausible enough that a trader looking at
the Gamma Map recognises the shapes.

Contract section 7 is absolute: every value this provider returns is badged
``Provenance(source="synthetic", kind="synthetic")``.  It is never used as a live fallback
unless the caller explicitly opts in (``ChainProvider(allow_synthetic=True)``).

How it is built
---------------
1. **One factor space, so crosses are consistent.**  We simulate the log of ``USD per 1 unit``
   for each of the nine non-USD G10 currencies.  Every pair is then a ratio of two legs, so
   EURJPY is *exactly* EURUSD x USDJPY and no triangular arbitrage exists in the data.
2. **Correlations that a trader would recognise** (:data:`LEG_CORR`): EUR/GBP ~ +0.78,
   EUR/CHF ~ +0.85, AUD/NZD ~ +0.88, and the JPY leg only ~ +0.30 to EUR -- which makes
   ``corr(EURUSD, USDJPY) ~ -0.30``, i.e. the negative sign the desk expects.
3. **Stochastic vol** -- each leg's log-vol is an Ornstein-Uhlenbeck process with a shared
   global vol factor, so realized vol has regimes, vol cones are non-degenerate, and the
   RV-IV spread on page 1 actually moves.  Long-run leg vols (:data:`LEG_VOL`) sit at 5.5-10.5%,
   which reproduces published G10 levels (EURUSD ~7, USDJPY ~9.5, USDCAD ~5.5, EURCHF ~4).
4. **A smile with the right signs** -- ATM from blended realized vol plus a volatility risk
   premium; risk reversals from :data:`RR25_BASE`, where **JPY pairs skew for yen calls**
   (USDJPY RR is negative because a yen call is a USDJPY put), commodity/EM-ish dollar-block
   pairs skew for dollar calls, and EURUSD/EURGBP are near flat.  Butterflies grow with tenor.
5. **A strike ladder that clusters where real OI clusters** -- Gaussian in log-moneyness,
   boosted on big figures and half figures, monthly (3rd-Friday) expiries carrying far more
   OI than the weeklies, with a call/put split tilted by the skew.
6. **The real curated event calendar** (``data/calendar/events.csv``, CG-6) -- not synthetic,
   and badged ``user_override`` rather than ``synthetic`` so the distinction survives.
   Top-tier events inside a tenor's window add an event bump to the short-dated ATM.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ..conventions import PAIRS, TENORS, pair_spec, tenor_years
from ..types import Provenance
from .base import (OI_COLUMNS, SPOT_COLUMNS, MarketDataProvider, SmileQuotes, SourceStatus,
                   empty_oi_frame)
from . import events as events_mod

log = logging.getLogger(__name__)

__all__ = ["SyntheticProvider", "LEGS", "LEG_VOL", "LEG_CORR", "ANCHOR_SPOT", "RATES",
           "RR25_BASE", "BF25_BASE"]

#: the nine non-USD legs; each path is USD per 1 unit of the currency
LEGS = ["EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "SEK", "NOK"]

#: long-run annualised vol of each USD leg (decimals). Pair vols fall out of these.
LEG_VOL: dict[str, float] = {
    "EUR": 0.068, "GBP": 0.078, "JPY": 0.095, "CHF": 0.075, "AUD": 0.100,
    "NZD": 0.105, "CAD": 0.055, "SEK": 0.090, "NOK": 0.105,
}

#: pairwise correlation of the USD legs. Sign convention: all legs are XXXUSD, so a positive
#: EUR/JPY entry here produces a NEGATIVE corr(EURUSD, USDJPY).
_CORR_PAIRS: dict[tuple[str, str], float] = {
    ("EUR", "GBP"): 0.78, ("EUR", "JPY"): 0.30, ("EUR", "CHF"): 0.85, ("EUR", "AUD"): 0.55,
    ("EUR", "NZD"): 0.52, ("EUR", "CAD"): 0.45, ("EUR", "SEK"): 0.80, ("EUR", "NOK"): 0.75,
    ("GBP", "JPY"): 0.20, ("GBP", "CHF"): 0.65, ("GBP", "AUD"): 0.58, ("GBP", "NZD"): 0.55,
    ("GBP", "CAD"): 0.50, ("GBP", "SEK"): 0.68, ("GBP", "NOK"): 0.65,
    ("JPY", "CHF"): 0.55, ("JPY", "AUD"): -0.05, ("JPY", "NZD"): -0.03, ("JPY", "CAD"): 0.05,
    ("JPY", "SEK"): 0.18, ("JPY", "NOK"): 0.10,
    ("CHF", "AUD"): 0.40, ("CHF", "NZD"): 0.38, ("CHF", "CAD"): 0.32, ("CHF", "SEK"): 0.62,
    ("CHF", "NOK"): 0.55,
    ("AUD", "NZD"): 0.88, ("AUD", "CAD"): 0.62, ("AUD", "SEK"): 0.58, ("AUD", "NOK"): 0.60,
    ("NZD", "CAD"): 0.55, ("NZD", "SEK"): 0.55, ("NZD", "NOK"): 0.55,
    ("CAD", "SEK"): 0.48, ("CAD", "NOK"): 0.55,
    ("SEK", "NOK"): 0.82,
}


def _corr_matrix() -> np.ndarray:
    n = len(LEGS)
    c = np.eye(n)
    for i, a in enumerate(LEGS):
        for j, b in enumerate(LEGS):
            if i < j:
                v = _CORR_PAIRS.get((a, b), _CORR_PAIRS.get((b, a), 0.0))
                c[i, j] = c[j, i] = v
    # nearest PSD by eigenvalue clipping, then renormalise to unit diagonal
    w, V = np.linalg.eigh(c)
    c = V @ np.diag(np.clip(w, 1e-8, None)) @ V.T
    d = np.sqrt(np.diag(c))
    return c / np.outer(d, d)


LEG_CORR = _corr_matrix()

#: spot levels the simulation is anchored to at `asof` (plausible, not observed)
ANCHOR_SPOT: dict[str, float] = {
    "EURUSD": 1.1650, "GBPUSD": 1.3450, "USDJPY": 147.50, "AUDUSD": 0.6580,
    "NZDUSD": 0.5950, "USDCAD": 1.3720, "USDCHF": 0.7950, "USDSEK": 9.3500,
    "USDNOK": 9.9500,
}

#: continuously-compounded flat zero rates (decimals). Plausible policy levels, not observed.
RATES: dict[str, float] = {
    "USD": 0.0400, "EUR": 0.0200, "JPY": 0.0075, "GBP": 0.0375, "CHF": 0.0025,
    "CAD": 0.0250, "AUD": 0.0350, "NZD": 0.0300, "SEK": 0.0200, "NOK": 0.0425,
}

#: 1M 25-delta risk reversal per pair (decimals; +ve = base-ccy calls over).
#: USDJPY / EURJPY are negative: the market pays up for YEN calls, which are JPY-pair puts.
RR25_BASE: dict[str, float] = {
    "EURUSD": -0.0025, "GBPUSD": -0.0045, "USDJPY": -0.0180, "AUDUSD": -0.0110,
    "NZDUSD": -0.0120, "USDCAD": 0.0060, "USDCHF": -0.0055, "USDSEK": 0.0090,
    "USDNOK": 0.0110, "EURJPY": -0.0155, "EURGBP": -0.0015, "EURCHF": -0.0035,
}

#: 1M 25-delta butterfly per pair (decimals). Wider for JPY and the Scandies.
BF25_BASE: dict[str, float] = {
    "EURUSD": 0.0018, "GBPUSD": 0.0022, "USDJPY": 0.0035, "AUDUSD": 0.0028,
    "NZDUSD": 0.0030, "USDCAD": 0.0020, "USDCHF": 0.0022, "USDSEK": 0.0032,
    "USDNOK": 0.0038, "EURJPY": 0.0034, "EURGBP": 0.0018, "EURCHF": 0.0025,
}

_VRP = 0.0065          # implied-minus-realized volatility risk premium, ~0.65 vol pts
_KAPPA = 4.0           # log-vol mean reversion, per year
_XI = 0.45             # log-vol vol -> stationary sd(ln vol) ~ 0.16, i.e. regimes of x0.85..x1.17
_GLOBAL_W = 0.55       # share of the log-vol shock that is a common global factor
_TS_LAMBDA = 2.6       # ATM term-structure mean-reversion speed


def _bs_price(F: float, K: float, T: float, sigma: float, df: float, cp: int) -> float:
    """Black-76 on the forward. Local fallback only -- quant's `models.gk` is authoritative."""
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return df * max(cp * (F - K), 0.0)
    from statistics import NormalDist
    n = NormalDist()
    v = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * v * v) / v
    d2 = d1 - v
    return df * cp * (F * n.cdf(cp * d1) - K * n.cdf(cp * d2))


class SyntheticProvider(MarketDataProvider):
    """Deterministic, offline, fully-badged fake market. See the module docstring."""

    name = "synthetic"
    kind = "synthetic"

    #: default seed. Chosen (not tuned) so the *terminal* stochastic-vol regime is close to
    #: neutral -- 21d realized vol within ~25% of each pair's long-run level -- otherwise a
    #: fresh screen can open in a 15%-vol AUDUSD regime, which is realistic but a poor default.
    DEFAULT_SEED = 20260101

    def __init__(self, seed: int = DEFAULT_SEED, asof: datetime | None = None,
                 history_days: int = 1500, calendar_path: str | None = None):
        self.seed = int(seed)
        self.asof = (asof or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.history_days = int(history_days)
        self.calendar_path = calendar_path
        self._rng = np.random.default_rng(self.seed)
        self._legs, self._legvol = self._simulate()
        self._closes = self._pair_closes()
        self._ohlc: dict[str, pd.DataFrame] = {}

    # ------------------------------------------------------------------ simulation
    def _simulate(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Correlated leg paths with OU stochastic vol. Deterministic in `seed`."""
        rng = np.random.default_rng(self.seed)
        n_days = self.history_days
        n = len(LEGS)
        dt = 1.0 / 252.0

        dates = pd.bdate_range(end=pd.Timestamp(self.asof.date(), tz="UTC"),
                               periods=n_days, tz="UTC", name="date")

        # --- stochastic log-vol: shared global factor + idiosyncratic, OU around ln(vol)
        lv0 = np.log(np.array([LEG_VOL[c] for c in LEGS]))
        lv = np.tile(lv0, (n_days, 1))
        zg = rng.standard_normal(n_days)
        zi = rng.standard_normal((n_days, n))
        for t in range(1, n_days):
            shock = (_GLOBAL_W * zg[t] + math.sqrt(max(1 - _GLOBAL_W ** 2, 0.0)) * zi[t])
            lv[t] = lv[t - 1] + _KAPPA * (lv0 - lv[t - 1]) * dt + _XI * math.sqrt(dt) * shock
        vol = np.exp(lv)

        # --- correlated return shocks
        L = np.linalg.cholesky(LEG_CORR)
        z = rng.standard_normal((n_days, n)) @ L.T
        drift = np.array([RATES["USD"] - RATES[c] for c in LEGS])       # carry in USD terms
        rets = (drift - 0.5 * vol ** 2) * dt + vol * math.sqrt(dt) * z
        rets[0] = 0.0

        logp = np.cumsum(rets, axis=0)
        # anchor the LAST observation to ANCHOR_SPOT so today's screen looks sane
        anchor = np.array([self._anchor_leg(c) for c in LEGS])
        prices = anchor * np.exp(logp - logp[-1])

        legs = pd.DataFrame(prices, index=dates, columns=LEGS)
        legvol = pd.DataFrame(vol, index=dates, columns=LEGS)
        return legs, legvol

    @staticmethod
    def _anchor_leg(ccy: str) -> float:
        """USD per 1 unit of `ccy`, derived from ANCHOR_SPOT."""
        for pair, s in ANCHOR_SPOT.items():
            spec = PAIRS[pair]
            if spec.base == ccy and spec.quote == "USD":
                return s
            if spec.quote == ccy and spec.base == "USD":
                return 1.0 / s
        raise KeyError(f"no anchor for {ccy}")

    def _pair_close(self, pair: str) -> pd.Series:
        spec = pair_spec(pair)
        one = pd.Series(1.0, index=self._legs.index)
        fo = one if spec.base == "USD" else self._legs[spec.base]
        do = one if spec.quote == "USD" else self._legs[spec.quote]
        return (fo / do).rename(pair)          # (USD per FOR) / (USD per DOM) = DOM per FOR

    def _pair_closes(self) -> pd.DataFrame:
        return pd.DataFrame({p: self._pair_close(p) for p in PAIRS})

    def _pair_ohlc(self, pair: str) -> pd.DataFrame:
        """Close path -> plausible OHLC. The intraday range is a seeded Brownian bridge."""
        if pair in self._ohlc:
            return self._ohlc[pair]
        c = self._closes[pair]
        rng = np.random.default_rng(self.seed ^ (abs(hash(pair)) % (2 ** 31)))
        o = c.shift(1).bfill()
        daily_sigma = np.abs(np.log(c / o).to_numpy())
        daily_sigma = np.where(daily_sigma > 0, daily_sigma, 1e-4)
        # a Brownian bridge's expected range is ~1.6x |net move|; add a floor for quiet days
        span = (0.9 + 0.8 * rng.random(len(c))) * np.maximum(daily_sigma, 2.5e-3)
        up = rng.random(len(c))
        hi = np.maximum(o, c) * np.exp(span * up)
        lo = np.minimum(o, c) * np.exp(-span * (1.0 - up))
        df = pd.DataFrame({"open": o.to_numpy(), "high": hi, "low": lo,
                           "close": c.to_numpy()}, index=c.index)[SPOT_COLUMNS]
        df.index.name = "date"
        self._ohlc[pair] = df
        return df

    # ------------------------------------------------------------------ realized vol
    def realized_vol(self, pair: str, window: int = 21) -> float:
        r = np.log(self._closes[pair]).diff().dropna().to_numpy()[-window:]
        return float(np.std(r, ddof=1) * math.sqrt(252.0)) if len(r) > 2 else LEG_VOL["EUR"]

    def _long_run_vol(self, pair: str) -> float:
        spec = pair_spec(pair)
        vb = LEG_VOL.get(spec.base, 0.0) if spec.base != "USD" else 0.0
        vq = LEG_VOL.get(spec.quote, 0.0) if spec.quote != "USD" else 0.0
        if spec.base == "USD" or spec.quote == "USD":
            return max(vb, vq)
        i, j = LEGS.index(spec.base), LEGS.index(spec.quote)
        rho = LEG_CORR[i, j]
        return math.sqrt(max(vb * vb + vq * vq - 2 * rho * vb * vq, 1e-6))

    # ------------------------------------------------------------------ MarketDataProvider
    def spot_history(self, pair: str, start: date, end: date) -> pd.DataFrame:
        df = self._pair_ohlc(pair)
        return df.loc[str(start):str(end)].copy()

    def spot(self, pairs) -> dict[str, float]:
        return {p: float(self._closes[p].iloc[-1]) for p in pairs if p in self._closes}

    def rates(self, ccys) -> dict[str, float]:
        return {c: RATES[c] for c in ccys if c in RATES}

    def _event_bump(self, pair: str, T: float) -> float:
        """Top-tier events inside the tenor add variance; short tenors feel it most."""
        spec = pair_spec(pair)
        hi = self.asof + timedelta(days=max(T * 365.0, 0.5))
        try:
            ev = events_mod.events(self.asof.date(), hi.date(), self.calendar_path)
        except Exception:                                # noqa: BLE001
            return 0.0
        if ev.empty:
            return 0.0
        rel = ev[(ev["importance"] >= 3) & (ev["ccy"].isin([spec.base, spec.quote]))]
        if rel.empty:
            return 0.0
        # each top-tier event contributes ~ (0.9 vol pt)^2 * 1 day of variance
        add_var = len(rel) * (0.009 ** 2) * (1.0 / 365.0)
        return add_var / max(T, 1.0 / 365.0)

    def smile_quotes(self, pair: str, asof: datetime | None = None,
                     tenors=("ON", "1W", "2W", "1M", "2M", "3M", "6M", "9M", "1Y")
                     ) -> list[SmileQuotes]:
        """Contract section 8 output: broker-style quotes per tenor. Deterministic."""
        if pair not in PAIRS:
            raise KeyError(f"unknown pair {pair}")
        rv21 = self.realized_vol(pair, 21)
        rv63 = self.realized_vol(pair, 63)
        lr = self._long_run_vol(pair)
        short = 0.45 * rv21 + 0.30 * rv63 + 0.25 * lr + _VRP
        long_ = lr + _VRP + 0.0035                      # term structure rises to the long end
        rr0, bf0 = RR25_BASE.get(pair, 0.0), BF25_BASE.get(pair, 0.002)

        # deterministic slow wiggle so richness z-scores are not constant across runs
        wig = np.random.default_rng(self.seed ^ (abs(hash(pair)) % 9973))
        wr, wb = float(wig.normal(0, 0.18)), float(wig.normal(0, 0.14))

        out: list[SmileQuotes] = []
        for tn in tenors:
            T = tenor_years(tn)
            atm = long_ + (short - long_) * math.exp(-_TS_LAMBDA * T)
            atm = math.sqrt(max(atm * atm + self._event_bump(pair, T), 1e-6))
            scale_rr = (T / TENORS["1M"]) ** 0.25
            scale_bf = (T / TENORS["1M"]) ** 0.35
            rr25 = rr0 * scale_rr * (1.0 + wr)
            bf25 = bf0 * scale_bf * (1.0 + wb)
            out.append(SmileQuotes(T=T, atm=round(atm, 6), rr25=round(rr25, 6),
                                   bf25=round(bf25, 6), rr10=round(1.85 * rr25, 6),
                                   bf10=round(3.10 * bf25, 6), tenor=tn))
        return out

    # ------------------------------------------------------------------ open interest
    @staticmethod
    def _third_friday(y: int, m: int) -> date:
        return events_mod.nth_weekday(y, m, 4, 3)

    def _expiries(self, asof: date, n_month: int = 6, n_week: int = 4
                  ) -> list[tuple[date, float]]:
        """(expiry, OI weight). Monthlies dominate; weeklies are thin."""
        out: list[tuple[date, float]] = []
        y, m = asof.year, asof.month
        for i in range(n_month + 1):
            mm, yy = (m - 1 + i) % 12 + 1, y + (m - 1 + i) // 12
            d = self._third_friday(yy, mm)
            if d > asof:
                out.append((d, 1.0 / (1.0 + 0.55 * len(out))))
            if len(out) >= n_month:
                break
        d = asof + timedelta(days=(4 - asof.weekday()) % 7 or 7)
        for _ in range(n_week):
            if d not in [e for e, _ in out]:
                out.append((d, 0.16 * math.exp(-0.35 * ((d - asof).days / 7.0))))
            d += timedelta(days=7)
        return sorted(out)

    def open_interest(self, pair: str, asof: datetime | None = None) -> pd.DataFrame:
        """Strike-ladder OI clustered on round numbers and monthly expiries."""
        if pair not in PAIRS:
            return empty_oi_frame()
        asof = (asof or self.asof).astimezone(timezone.utc)
        spec = pair_spec(pair)
        S = float(self._closes[pair].iloc[-1])
        rd, rf = RATES.get(spec.quote, 0.0), RATES.get(spec.base, 0.0)
        rng = np.random.default_rng(self.seed ^ 0x0117 ^ (abs(hash(pair)) % 9973))

        step = 50.0 * spec.pip                       # 50 pips: the listed strike granularity
        fig = 100.0 * spec.pip                       # a "big figure": 0.0100 EURUSD, 1.00 JPY
        round5 = 500.0 * spec.pip                    # the real magnets: 1.1500, 150.00
        lo, hi = S * 0.88, S * 1.12
        strikes = np.round(np.arange(math.floor(lo / step) * step,
                                     math.ceil(hi / step) * step + step / 2, step), 10)
        quotes = {q.tenor: q for q in self.smile_quotes(pair)}
        rows: list[dict] = []

        for expiry, w_exp in self._expiries(asof.date()):
            T = max((expiry - asof.date()).days, 1) / 365.0
            F = S * math.exp((rd - rf) * T)
            q = min(quotes.values(), key=lambda x: abs(x.T - T))
            width = 0.033 * math.sqrt(T / TENORS["1M"])          # OI is wider for longer T
            norm = 0.033 / width                                 # keep total OI ~ w_exp
            skew_tilt = np.sign(q.rr25) * min(abs(q.rr25) / 0.02, 1.0) * 0.35
            for K in strikes:
                k = math.log(K / F)
                base = 1.0e4 * w_exp * norm * math.exp(-0.5 * (k / width) ** 2)
                if abs(K / round5 - round(K / round5)) < 1e-9:
                    base *= 1.90                                  # 1.1500 / 150.00 magnets
                elif abs(K / fig - round(K / fig)) < 1e-9:
                    base *= 1.35                                  # big figures
                base *= 0.75 + 0.5 * rng.random()
                for cp in (1, -1):
                    side = 1.0 + cp * skew_tilt + (0.25 if cp * k > 0 else -0.10)
                    oi = base * max(side, 0.05) * (0.85 + 0.3 * rng.random())
                    if oi < 25:
                        continue
                    sig = max(q.atm + (q.rr25 / 2.0) * np.sign(k) * min(abs(k) / 0.03, 1.0)
                              + q.bf25 * min((k / 0.03) ** 2, 4.0), 0.005)
                    rows.append({
                        "strike": float(K), "expiry": expiry, "cp": int(cp),
                        "oi": float(round(oi)),
                        "settle": float(_bs_price(F, K, T, sig, math.exp(-rd * T), cp)),
                    })
        if not rows:
            return empty_oi_frame()
        df = pd.DataFrame(rows)[OI_COLUMNS]
        return df.sort_values(["expiry", "cp", "strike"], ignore_index=True)

    # ------------------------------------------------------------------ calendar
    def events(self, start: date, end: date) -> pd.DataFrame:
        """The real curated calendar (CG-6) -- NOT synthetic; badged ``user_override``."""
        return events_mod.events(start, end, self.calendar_path)

    # ------------------------------------------------------------------ provenance
    def snapshot(self, pairs, asof: datetime | None = None, **kw):
        snap = super().snapshot(pairs, asof or self.asof, **kw)
        note = f"SyntheticProvider(seed={self.seed}) -- SIMULATED, not market data"
        for k in list(snap.meta):
            p = snap.meta[k]
            snap.meta[k] = Provenance(source="synthetic", kind=p.kind if p.kind == "unavailable"
                                      else "synthetic", asof=p.asof, note=note)
        for p in pairs:
            snap.meta[f"oi.{p}"] = Provenance("synthetic", "synthetic", snap.asof, note)
        snap.meta["events"] = Provenance("curated-csv", "user_override", snap.asof,
                                         "data/calendar/events.csv (CG-6); user-editable")
        return snap

    def status(self) -> list[SourceStatus]:
        return [SourceStatus("synthetic", True,
                             f"seed={self.seed}, {self.history_days}d history, "
                             f"{len(PAIRS)} pairs", verified=True, rows=self.history_days)]
