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
│   │   ├── base.py  provider.py  synthetic.py  cache.py  manual.py
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

---

# AMENDMENT v1.4 — PM arbitration: BA vs trader on the reference trade (binding)

The BA challenged the trader's reference table (`docs/06_trader_review.md` §2, lines 108-116) as
internally inconsistent. **The BA is right.** The PM settled it by pricing the trader's own
reference trade rather than taking either side's word.

Reference trade: EURUSD 1M ATM straddle, EUR 10mm per leg, spot 1.084, sigma 7.05% (all the
trader's own figures). Priced through `gk_greeks` on the DNS strike:

| Quantity | Trader's table | Priced | Verdict |
|---|---|---|---|
| Gamma_1pct, both legs | EUR 3.95mm per 1% | EUR **3.914mm** | ✅ agrees |
| Theta, calendar | USD **5,800**/day | USD **2,868**/day | ❌ trader ~2x too high |
| Daily breakeven | 0.369% = 40 pips | 0.3677% = **39.9 pips** | ✅ agrees |
| Cross-check sigma/sqrt(365) | — | 0.3690% = **40.0 pips** | ✅ identity holds to 0.3% |

The three numbers are not independent: given Gamma_1pct and the breakeven, theta is determined.
The trader's own Gamma_1pct of 3.95mm and breakeven of 0.369% imply **USD 2,915/day**, which
matches the priced 2,868 and not 5,800. The likely slip is double-counting the two straddle legs in
theta while the quoted gamma was already both-legs.

**Rulings:**
1. The correct reference theta is **~USD 2,870/day**, ~USD 8,600 Friday to Monday (3 calendar days).
   The 5,800 / 17,400 figures are withdrawn wherever they appear.
2. This matters beyond a table: MISS-4 asks for an always-visible header card reading
   "costs USD 5,800". Shipped as-is it would overstate the daily cost of carry by 2x in the single
   most-read number in the app. **Binding on `dev`: the header card computes theta from
   `book_greeks`, never from a constant.**
3. QA: adopt this trade as a **golden fixture** — Gamma_1pct 3.914mm, theta -2,868, breakeven
   39.9 pips, and assert the `BE = sigma/sqrt(365)` identity to <1%. It cross-checks gamma, theta
   and the breakeven helper in one test.
4. The BA's REQ-046 correction is **confirmed independently**: the delta-hedged carry is
   `50·Γ₁·S·(σ_r² − σ_i²)·Δt_years`, and its worked example (one day of 9% realised against 7.05%
   implied = +USD 1,836) reproduces to USD 1,834 on the PM's own derivation.
5. The trader's **W-7** stands and is accepted: `√365` for economics (breakeven, theta), `√252` for
   distance and touch probability. Never conflate them; print the basis on the panel.

---

# AMENDMENT v1.5 — manual marks registered; QA findings ruled (binding)

**M-1 — the `manual` provider is now part of the frozen layout** (`fxgamma/data/manual.py`,
added to §1). API: `ManualMark`, `ManualQuoteStore`, `ManualQuoteProvider`,
`get_provider(..., manual=...)`, `ChainProvider.manual`, `ChainProvider.marked_pairs()`.
Resolution order is **manual → live → cache → synthetic**, with manual hoisted to the front
regardless of construction order. Marks persist to `data/manual/marks.json`, which is
**gitignored** — it holds the user's own marks and is not source.

**QA findings (`docs/05_test_report.md`), PM rulings:**

**Q-1 — synthetic market was not reproducible across processes. FIXED (PM).** `synthetic.py`
seeded three generators off `hash(pair)`. Since PEP 456 Python salts str hashing per process, so
every run produced a *different* synthetic market: RR/BF, OHLC ranges and the entire open-interest
table changed each time the module was loaded. That silently invalidates every backtest and breaks
"does yesterday's screen still say what it said yesterday" — the charter promises determinism under
a fixed seed and it was not being delivered. Replaced with `zlib.crc32`, which is stable across
processes and versions. Verified: three separate interpreters now return an identical EURUSD 1M
risk-reversal.

**Q-2 — `sum(Greeks)` raised AttributeError. FIXED (PM)** in `types.py`: `sum()` seeds with the int
`0`, and folding over an empty book is legitimate, so `__add__` now accepts a falsy numeric seed as
well as `None`. It was latent only because `risk.py` happened to aggregate via pandas.

**Q-3 — an unknown cut string silently became NY10. RESOLVED: it now raises.** A typo'd cut would
shift every affected expiry by hours, quietly changing `T`, theta and the pin clock on those legs —
precisely the silent substitution architecture §7 forbids. QA's strict-xfail is now a passing test.

**Q-4 — `get_store(path)` ignores `path` after the first call. ASSIGNED to `dev`** (still in
flight). A second book path silently returns the first store: a user opening a second book would
write into the first.

**Q-5 — SABR misses the 25d butterfly by ~0.09 vol points (a quarter of the BF). ASSIGNED to
`quant`** — acceptable for interpolation, never acceptable as a mark. Must be stated in
`docs/03_model_spec.md` and the surface method badged in the UI so SABR is never mistaken for a
repricing surface. Vanna-volga (which reprices its inputs to ~1e-8) remains the default.

**Standing test gap, acknowledged not closed:** every live adapter is unverified (hosts blocked
here) and `manual.py` — now the *primary* mark path — has no test coverage. Both are first calls on
the next QA pass; `scripts/verify_live_sources.py` covers the live half on the user's own machine.

---

# AMENDMENT v1.6 — PM rulings on the risk engine's contract-change requests (binding)

The PM verified the engine's headline claims independently before ruling. Pin risk: a **long put**
now reports `delta_if_above 0 / delta_if_below -10mm / jump +10mm`, where REQ-045's old formula
gave -10mm — a 20mm error, and the jump is correctly independent of call/put. Cross-ccy theta on a
EURUSD+USDJPY book: naive sum **-209,974** (USD added to JPY) vs USD-converted **-3,424**, i.e.
the un-converted number is **61x wrong**. CG-1 was worth the trouble.

**CR-1 — `HedgeRule.band_pct` semantics. RESOLVED: it is a FRACTION, not a percent.**
The frozen comment said "this % of notional" with a default of `0.25`, which reads as 0.25% and is
~60x tighter than any desk would run: on a 10mm book it rehedges on a 25k delta drift. The intent
was 25%. `types.py` is amended to say so unambiguously; the default value is unchanged and is now
correct rather than dangerous. The engine's `warning` column stays — it costs nothing and catches a
user who read the old comment. **The band remains open question Q-2 for the real user**; 25% of
gross option notional, floored at a 1mm clip, is the shipping default until they say otherwise.
`cost_bp` moves to the per-pair table `zones.COST_BP` (**approved**), with `HedgeRule.cost_bp`
retained as a per-rule override.

**CR-2 — CG-1 scope for base-ccy Greeks. RATIFIED as the engine read it.**
`delta_base`, `gamma`, `gamma_1pct` and `vanna` are base-ccy *amounts*, not quote-ccy money, so
amendment v1.1's "monetary Greeks" clause does not reach them. Aggregate natively when the book has
a single base ccy; return `nan` across mixed bases (the T-2 precedent — a wrong number is worse
than "n/a"); `book_greeks(..., base_as_value=True)` converts to report-ccy value when a single
figure is wanted. This is the correct reading and the aggregate delta card depends on it.

**CR-3 — `sticky="none"`. ACCEPTED as implemented.** For a strike-parameterised surface it is
numerically identical to `"strike"`; shipping it as the documented pinned-vol fast path is right.

**CR-4 — `GammaZoneDetail(GammaZone)` stays a subclass.** `list[GammaZoneDetail]` satisfies the
frozen signature, so no contract change. **Binding on `dev`:** the extra fields (sigma-days on
√252, touch probability, contributing strikes/expiries, P&L-to-centre, delta carried inside) are
available and should be surfaced — they are the substance of "how sensitive each gamma zone is".

**Recorded finding, no action needed — `skew_gamma_1pct`.** Under sticky-delta the vol moves with
spot, so true gamma is *not* Black-Scholes gamma: the engine measures a **4.1%** gap on the
reference book and now reports the difference explicitly. This is a real effect a trader hedging
off BS gamma under a sticky-delta assumption would silently mis-size. Good work; surface it in the
UI next to the sticky toggle.
