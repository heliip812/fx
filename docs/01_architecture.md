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
