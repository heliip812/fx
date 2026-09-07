# Architecture & Interface Contract (FROZEN — PM sign-off required to change)

Every agent codes against these signatures. If you need a change, say so in your final report;
do not silently diverge.

## 1. Layout

```
fx/
├── run.py                     # entrypoint: python run.py [--port 8050] [--provider synthetic|live]
├── requirements.txt
├── fxgamma/
│   ├── conventions.py         # PAIRS, day-count, cuts, pip sizes, delta conventions   [quant]
│   ├── types.py               # shared dataclasses (below)                             [quant]
│   ├── store.py               # SQLite persistence for the book                        [dev]
│   ├── data/                  # market data adapters + cache + synthetic               [data]
│   │   ├── base.py  provider.py  synthetic.py  cache.py
│   │   ├── spot_yahoo.py  spot_stooq.py  spot_ecb.py  rates_fred.py
│   │   ├── vol_etf_options.py     # FXE/FXB/FXY/... listed chains -> implied vols
│   │   ├── vol_indices.py         # CBOE EVZ/JYVIX/BPVIX via FRED
│   │   └── cme_options.py         # CME public settlements + open interest by strike
│   ├── models/                # pricing & smiles                                       [quant]
│   │   ├── gk.py  smile.py  vanna_volga.py  sabr.py  surface.py  interp.py
│   ├── portfolio/             # book aggregation, scenarios, attribution               [quant]
│   │   ├── book.py  risk.py  zones.py  attribution.py  hedging.py
│   ├── signals/               # RV estimators, cones, richness, regimes                 [quant]
│   │   └── realized.py  richness.py  cones.py  gex.py
│   └── backtest/              # delta-hedged gamma strategies                          [quant]
│       └── engine.py  strategies.py  metrics.py
├── app/                       # Dash                                                   [dev]
│   ├── main.py  theme.py  layout.py
│   ├── components/  (charts.py, tables.py, inputs.py, cards.py)
│   └── pages/  (1..8, see §6)
├── tests/                                                                              [qa]
├── scripts/verify_live_sources.py                                                      [data]
└── docs/
```

## 2. Conventions (non-negotiable)

- A pair is `FORDOM`, e.g. `EURUSD`: **base/foreign = EUR**, **quote/domestic = USD**.
  Spot `S` = units of DOM per 1 unit of FOR.
- `r_d` = domestic (quote ccy) continuously-compounded rate; `r_f` = foreign (base ccy).
- Notional is always quoted **in base ccy** (`notional_base`). Long call on EURUSD 10mm =
  right to buy EUR 10mm.
- `cp = +1` call, `-1` put. `direction = +1` long, `-1` short.
- Time in **years, ACT/365F**, from `asof` to the expiry **cut** (NY 10:00 / TKY 15:00 → stored as
  a UTC datetime by `conventions.expiry_datetime`).
- Delta convention per pair from `conventions.PAIRS[pair].delta_convention` ∈
  `{"spot", "spot_pa", "fwd", "fwd_pa"}` (premium-adjusted for e.g. USDJPY).
- Vols are decimals (0.085 = 8.5%), **not** percent. Rates are decimals. Money is float.

## 3. Shared types — `fxgamma/types.py` (quant writes; everyone imports)

```python
@dataclass(frozen=True)
class PairSpec:
    symbol: str; base: str; quote: str; pip: float; spot_lag: int
    delta_convention: str; cut: str; etf_proxy: str | None; cme_code: str | None

@dataclass(frozen=True)
class OptionPosition:
    id: str; pair: str; cp: int; strike: float; expiry: date
    notional_base: float; direction: int
    premium_paid: float = 0.0          # in premium_ccy, total, signed by direction
    premium_ccy: str = ""              # default = quote ccy
    trade_date: date | None = None; trade_spot: float | None = None
    trade_vol: float | None = None; cut: str = "NY10"; tag: str = ""

@dataclass(frozen=True)
class SpotPosition:
    id: str; pair: str; notional_base: float   # signed, + = long base
    entry_rate: float; trade_date: date | None = None; tag: str = ""

@dataclass(frozen=True)
class Greeks:
    pv: float          # in quote ccy
    delta_base: float  # base-ccy amount to sell to be flat
    delta_pct: float   # delta per 1 unit notional
    gamma: float       # d(delta_base)/dS
    gamma_1pct: float  # delta_base change for a +1% spot move  <- desk unit
    vega: float        # quote ccy per 1 vol point (0.01)
    theta: float       # quote ccy per calendar day
    rho_d: float; rho_f: float
    vanna: float       # d(vega)/dS  ... quote ccy per 1 vol pt per 1 unit spot
    volga: float       # d(vega)/dvol
    def __add__(self, other) -> "Greeks": ...

@dataclass
class MarketSnapshot:
    asof: datetime
    spot: dict[str, float]
    rates: dict[str, float]                 # ccy -> cc zero rate (flat curve v1)
    surfaces: dict[str, "VolSurface"]
    forwards: dict[str, dict[float, float]] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)   # source provenance per field

@dataclass
class Book:
    options: list[OptionPosition]; spots: list[SpotPosition]; name: str = "default"
```

## 4. Model interfaces

```python
# fxgamma/models/gk.py
def gk_price(S, K, T, rd, rf, sigma, cp) -> float
def gk_greeks(S, K, T, rd, rf, sigma, cp, notional_base=1.0, direction=1) -> Greeks
def implied_vol(price, S, K, T, rd, rf, cp, *, tol=1e-10) -> float
def strike_from_delta(delta, S, T, rd, rf, sigma, cp, convention="spot") -> float
def delta_from_strike(K, S, T, rd, rf, sigma, cp, convention="spot") -> float

# fxgamma/models/surface.py
class VolSurface(Protocol):
    pair: str; asof: datetime
    def vol(self, K: float, T: float) -> float
    def vol_by_delta(self, delta: float, T: float, cp: int) -> float
    def atm(self, T: float) -> float
    def rr(self, T: float, d: float = 0.25) -> float
    def bf(self, T: float, d: float = 0.25) -> float
    def slice(self, T: float, strikes: np.ndarray) -> np.ndarray
```
Implementations: `VannaVolgaSurface.from_quotes(atm, rr25, bf25, tenors, ...)`,
`SABRSurface.calibrate(...)`, `InterpolatedSurface.from_chain(strikes, expiries, vols)`.
All must be picklable and cheap to re-evaluate (the ladder calls `vol()` ~1e5 times).

## 5. Portfolio interfaces

```python
# fxgamma/portfolio/risk.py
def price_book(book, mkt) -> pd.DataFrame        # one row per position + Greeks columns
def book_greeks(book, mkt) -> Greeks
def spot_ladder(book, mkt, pair, *, lo_pct=-5, hi_pct=5, n=101,
                sticky="strike"|"delta"|"none") -> pd.DataFrame   # spot -> Greeks
def scenario_grid(book, mkt, pair, spot_shocks, vol_shocks, days_fwd=0) -> pd.DataFrame
def time_decay(book, mkt, days=range(0,31)) -> pd.DataFrame

# fxgamma/portfolio/zones.py
def gamma_zones(book, mkt, pair, **kw) -> list[GammaZone]   # clustered strike regions
def hedge_bands(book, mkt, pair, *, gamma_budget) -> pd.DataFrame
def pin_risk(book, mkt, pair) -> pd.DataFrame

# fxgamma/portfolio/attribution.py
def daily_pnl(book, mkt_t0, mkt_t1, hedges: list[SpotPosition] | None = None) -> PnLBreakdown
#   -> total, delta, gamma, theta, vega, vanna, volga, rates, carry, hedge, unexplained

# fxgamma/portfolio/hedging.py
def hedge_suggestion(book, mkt, pair, rule: HedgeRule) -> HedgeAction
```

## 6. Dash pages (dev owns; each is `dash.register_page`)

| # | Path | Page | Key content |
|---|---|---|---|
| 1 | `/` | **Market Monitor** | spot + returns, RV(multi-estimator) vs IV, RV-IV spread heat, RR/BF z-scores, cross-pair table |
| 2 | `/surface` | **Vol Surface** | 3-D surface, smile by tenor, term structure, cone, calibration residuals |
| 3 | `/gamma-map` | **Gamma Map** | market gamma by strike (CME OI), expiry ladder, pin/strike magnets, spot overlay |
| 4 | `/book` | **Position Book** | add/edit/delete spot + option trades, CSV import/export, live re-pricing |
| 5 | `/risk` | **Risk** | aggregate Greeks cards, spot ladder, scenario matrix, gamma zones, hedge bands |
| 6 | `/pnl` | **P&L** | daily attribution waterfall, cumulative, per-position, hedge log |
| 7 | `/lab` | **Signals & Backtest** | strategy config, equity curve, hedge-frequency sweep, stats table |
| 8 | `/data` | **Data & Settings** | source status, cache, provider toggle, manual vol/rate overrides |

State: `dcc.Store` for the session snapshot; SQLite via `fxgamma.store` for the book.
Charts must work in dark theme and degrade gracefully to an empty book.

## 7. Error & provenance policy

Every number surfaced in the UI carries provenance (`MarketSnapshot.meta`): source name, timestamp,
and whether it is `live`, `cached`, `synthetic`, or `user_override`. Synthetic/override values are
badged in the UI. **Never silently substitute synthetic data for live data.**

## 8. The one factory that joins data <-> models (frozen)

```python
# fxgamma/models/surface.py                                     [quant-models owns]
@dataclass(frozen=True)
class SmileQuotes:
    """Broker-style quotes for one tenor."""
    T: float; atm: float; rr25: float; bf25: float
    rr10: float | None = None; bf10: float | None = None; tenor: str = ""

def build_surface(pair: str, asof: datetime, quotes: list[SmileQuotes],
                  spot: float, rd: float, rf: float,
                  method: str = "vanna_volga") -> VolSurface: ...
```
`method` in `{"vanna_volga", "sabr", "interp"}`. Every data provider produces
`list[SmileQuotes]` per pair and calls `build_surface`. Nothing else crosses that boundary.

---

# AMENDMENT v1.1 — PM ruling on BA contract gaps CG-1…CG-7 (binding)

Raised in `docs/02_requirements.md` §8.3. All other clauses of the contract stand unchanged.

**CG-1 — reporting currency. RESOLVED: a conversion layer, not a change to `Greeks`.**
`Greeks` stays purely numeric so `+` and `*` remain safe. Instead:
- Per-position Greeks remain in the position's **native quote ccy** (unchanged).
- `price_book(book, mkt)` gains two columns: `ccy` (native quote ccy) and `fx_to_report`
  (multiplier into the reporting ccy), plus `*_rep` copies of every monetary Greek.
- `book_greeks(book, mkt, report_ccy="USD")` returns Greeks **already converted** into
  `report_ccy`. Aggregating across pairs in native ccy is forbidden.
- `fxgamma/portfolio/risk.py` exposes `fx_rate(ccy, report_ccy, mkt) -> float`, which must
  route through USD and raise if a leg is missing rather than defaulting to 1.0.
  *Owner: quant-risk.*

**CG-2 — per-position mark vol. RESOLVED: side-table, no type change.**
`fxgamma/store.py` owns a `position_marks` table keyed on position `id`
(`mark_vol`, `mark_source`, `asof`). `OptionPosition` stays frozen. *Owner: dev.*

**CG-3 — APPROVED.** `hedge_bands(book, mkt, pair, *, gamma_budget=None, rule: HedgeRule | None = None)`.
Cost-aware band maths lives in the library, never in `app/`. *Owner: quant-risk.*

**CG-4 — APPROVED.** `time_decay(book, mkt, days=range(0,31), *, weights: Sequence[float] | None = None,
calendar: str = "calendar")` with `calendar in {"calendar", "business", "event"}`. Calendar time stays
the default; business/event weighting is opt-in and must be badged in the UI. *Owner: quant-risk.*

**CG-5 — RESOLVED in `types.py` (already applied).** `SpotPosition.trade_time` and
`OptionPosition.trade_time`, both `datetime | None = None`, UTC, appended last so construction
stays backward-compatible. The hedge log orders by `trade_time` and falls back to `trade_date`.

**CG-6 — ASSIGNED to `data`.** The event calendar is a versioned, user-editable
`data/calendar/events.csv` shipped in the repo, loaded by `fxgamma/data/events.py`, surfaced through
the existing `MarketDataProvider.events(start, end)`. Frozen columns:
`date, time_utc, ccy, event, importance, source` with `importance in {1,2,3}` (3 = top tier:
FOMC/ECB/BoJ/BoE decisions, US CPI, US NFP). Event weights for business time live in the library,
not the data layer.

**CG-7 — FROZEN key grammar for `MarketSnapshot.meta`.**
```
spot.<PAIR>        rate.<CCY>        fwd.<PAIR>.<TENOR>
surface.<PAIR>     surface.<PAIR>.<TENOR>
oi.<PAIR>          events
```
`PAIR` is the 6-letter uppercase symbol, `CCY` the 3-letter code, `TENOR` a `conventions.TENORS`
key. Lookup is most-specific-first: a badge for `surface.EURUSD.1M` falls back to `surface.EURUSD`.

---

# AMENDMENT v1.2 — PM ruling on the trader review (binding)

Source: `docs/06_trader_review.md` §1. Five defects were raised; three were in PM-owned files and
are **already fixed**, two are spec errors assigned back to the BA.

**T-1 — Manual vol marks are promoted to a PRIMARY input. ACCEPTED, and it changes the design.**
The trader's blocking objection is correct: everything downstream is computed off a vol mark, and
ETF-implied vol is not the desk's mark. Therefore:
- `fxgamma/data/manual.py` (**data**) adds `ManualQuoteProvider`: a per-pair, per-tenor
  ATM / 25d RR / 25d BF grid the user types or pastes, persisted, with `Provenance(kind="user_override")`.
- `ChainProvider` resolution order becomes **manual → live → cache → synthetic**. A manual mark
  always wins; it is never overwritten by a live pull.
- The Data page (**dev**) gets a paste-and-go grid as its top panel, targeting <30 seconds to mark
  the whole G3 book, not a buried override dialog.
- ETF-implied and CBOE-index vols are demoted to what they are good at: **z-scores, cones and
  richness**, never the mark. The Market Monitor may show them; the book is priced off the manual
  curve when one exists, and the badge says which.

**T-2 — `Greeks.__add__` summed intensive quantities. FIXED** in `types.py`: `delta_pct` and
`dual_delta` are per-unit / per-strike and now aggregate to `nan`, so book-level cards read "n/a"
rather than printing a confident wrong number. All extensive Greeks still add normally.

**T-3 — expired options never died. FIXED** in `conventions.py`: `year_fraction` returns exactly
`0.0` once the cut has passed (the one-hour floor now applies only on the live side), and
`is_expired()` is added. Verified: an expired ITM call prices to intrinsic with full delta and zero
gamma, vega and theta.

**T-4 — `rd_rf` silently defaulted a missing rate to 0.0. FIXED** in `types.py`: it now raises with
the missing ccy named. Assuming a zero rate put the USDJPY 1Y forward about four big figures out.

**T-5 — spec errors, ASSIGNED TO BA.** REQ-045's pin-risk formula is the *inherited-if-ITM* delta,
not the discontinuity, and is sign-wrong for puts; REQ-046's decay identity is out by 100x given the
document's own definition of `Γ$`. Both to be corrected in `docs/02_requirements.md` with a
worked numeric example each.

**Scope note:** T-1 does not widen v1. It re-prioritises an existing requirement (REQ-068) from a
buried override to the primary path, and adds one small provider plus one UI panel.

---

# AMENDMENT v1.3 — PM ruling on the credit extension (binding for v2)

Source: `docs/07_credit_gamma.md`. The PM independently verified the two load-bearing claims
before accepting: Garman-Kohlhagen with `rd=rf=0` reproduces undiscounted Black-76 on a forward
**exactly** (0.00e+00 difference), so the existing pricer is reusable for credit index options once
multiplied by the risky annuity; and the spread-inversion amplification `1/(OASD·s)` is ~9.5x for
HYG, ~9.2x for JNK and ~13.4x for LQD.

**C-1 — "CDX gamma analytics from free data" is REJECTED.** A 1-point error in HYG implied vol
becomes a ~9.5-point error in implied *spread* vol, before rates contamination (LQD is largely a
rates instrument by variance), the unobservable spread-rate correlation, duration mismatch and the
cash-CDS basis. Free data cannot mark a CDX book. We will not ship an analytic that implies it can.

**C-2 — "Credit ETF Gamma" is ACCEPTED as the v2 product.** HYG/LQD/JNK options are themselves
listed, liquid and free-data-complete. A gamma book *in those options* is the instrument, not a
proxy, and gets full FX-grade fidelity: surface, skew, chain-OI gamma map, IV-RV, attribution.
This is an honest product; the CDX proxy was not.

**C-3 — a manual-mark CDX pricer is ACCEPTED** (annuity numeraire, front-end protection, full
Greeks), on the same precedent as amendment v1.2 T-1: the user supplies the mark, the tool does the
maths. ~1 day.

**C-4 — iTraxx / Europe is CUT.** No free EU credit-ETF option market exists to support it.

**C-5 — CG-7 key grammar is extended (additive):** `spread.<INDEX>` and `oasd.<ETF>` join the
frozen provenance keys. No existing key changes.

**C-6 — CROSS-CUTTING, AND IT BITES FX v1 TOO. Assigned to `data`, priority raised.**
The credit analyst's underrated finding: Yahoo serves only *today's* option chain, so nothing built
on it is backtestable until you have been recording it. This is not a credit-only problem — the FX
side has exactly the same hole. FXE/FXB/FXY chains give us today's smile and no history, so
**skew/RR/BF z-scores and vol cones built on ETF chains have no back-history on day one**. CBOE
EVZ via FRED partly covers EURUSD ATM history; it covers no skew and no other pair.
Required: a daily chain **snapshotter** that appends each day's pulled chain to the parquet cache,
shipped in v1 so history starts accumulating from the user's first run, plus honest UI copy stating
how many days of history exist before a z-score is meaningful. A z-score computed on 11 days of
self-collected history must say so.
