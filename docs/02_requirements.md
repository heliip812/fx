# Requirements — FX Gamma Desk (G3 + G10)

**Owner:** Business Analyst (`ba`) · **Status:** M1 draft for trader review · **Reads against:**
`docs/00_charter.md`, `docs/01_architecture.md` (both FROZEN), `fxgamma/types.py`,
`fxgamma/conventions.py`.

This document is the build contract for behaviour. Where it names a function it means the one in
architecture §4–§5. Where it needs something the frozen contract does not provide, it is marked
**[CONTRACT-GAP-n]** and listed in §8.3 for PM arbitration. Nothing here changes the contract.

**Notation used throughout**

| Symbol | Meaning | Unit |
|---|---|---|
| `S` | spot, quote ccy per 1 base ccy | e.g. 1.0850, 147.20 |
| `Γ₁` | `Greeks.gamma_1pct` — change in `delta_base` for a **+1% spot move** | base ccy |
| `Γ$` | `Γ₁ × S` — quote-ccy value of that delta change | quote ccy per 1% |
| `θ` | `Greeks.theta` — quote ccy per **calendar** day | quote ccy/day |
| `ν` | `Greeks.vega` — quote ccy per **1.00 vol point** (0.01 of σ) | quote ccy/vol pt |
| `σ` | implied vol, decimal (0.085 = 8.5%) | — |
| `x` | spot move in **percent** | % |
| BE | daily breakeven move (§4.1) | % and pips |

**Derived identity used by several screens (developers: implement once, in one helper).**
Gamma P&L over a move of `x` percent is `0.5·Γ·dS²` with `Γ = Γ₁/(0.01·S)` and `dS = 0.01·x·S`:

```
gamma_pnl(x%)  = 0.005 · Γ₁ · S · x²        [quote ccy]
BE_daily (%)   = sqrt( |θ| / (0.005 · Γ₁ · S) )
```

For a pure ATM position this reduces exactly to `σ_ATM / sqrt(365)`. QA uses that as a check.

---

## 1. Personas and jobs-to-be-done

### 1.1 Primary persona — "the gamma trader" (the user)

| Attribute | Value |
|---|---|
| Book | G3 core (EURUSD, GBPUSD, USDJPY), opportunistic G10 + EUR crosses |
| Style | Buys/sells short-dated vol (ON–3M), delta-hedges discretionarily around a band, runs the residual smile risk |
| Typical position | 10–100mm base per leg, 5–40 live option lines, 1–10 spot hedge lines |
| Screen budget | one laptop, one monitor, no Bloomberg on this app; this app is the *second* opinion, not the OMS |
| Numeracy | expert. Wants raw numbers and units, not gauges. Distrusts anything without provenance |
| Tolerance | will abandon a screen that takes >2s to answer "am I long or short gamma right now" |

**Secondary persona — "the reviewer"** (`trader` agent, §M6): opens the app cold with the demo book
and looks for desk-realism errors. Every screen must be legible with zero prior context: units on
every number, convention stated, asof timestamp visible.

**Anti-persona:** this is not a retail P&L tracker and not an execution tool. No order entry, no
broker connectivity, no "signals to follow blindly".

### 1.2 The day. Each moment → the decision → the screen that must answer it

| # | Moment (user local time) | Decision to make | Screen | Must answer in |
|---|---|---|---|---|
| J1 | **Pre-London mark**, 06:30–07:15 | Is my book marked right? Did anything gap overnight? What is today's theta bill and today's breakeven move? | 4 Book → 5 Risk (Greek cards + BE card) | ≤ 60 s total |
| J2 | **Morning richness check**, 07:15–08:00 | Is gamma cheap or expensive *today*, per pair and per tenor? Buy or sell vol? Which tenor is the cheapest way to own the move I want? | 1 Market Monitor → 2 Vol Surface | ≤ 3 min |
| J3 | **Positioning check**, 08:00 | Where is the *market's* gamma? Am I fighting the strike magnets or riding them? | 3 Gamma Map | ≤ 2 min |
| J4 | **Intraday hedge decision**, any time | Spot has moved x%. Do I hedge now, how much, and what does it cost vs. leaving the band? Where is my next hedge trigger? | 5 Risk (ladder + hedge bands) | ≤ 5 s from spot input |
| J5 | **New trade / structure sizing** | If I add this straddle/RR, what happens to Γ₁, θ, ν, vanna, and to my breakeven? Does it fill or worsen a gamma hole? | 4 Book (what-if ticket) → 5 Risk (with/without overlay) | ≤ 20 s |
| J6 | **Expiry / cut management**, T-3d to cut | Which strikes expire at which cut? What is my pin risk? What delta do I inherit at the cut under each spot outcome? When exactly (my clock) does each option die? | 5 Risk (expiry ladder, pin panel, cut clock) | ≤ 2 min |
| J7 | **End-of-day P&L explain**, 17:00 NY | I made/lost X. How much was gamma vs. theta vs. vega vs. my hedges? Is the unexplained residual small enough to trust the mark? | 6 P&L | ≤ 3 min |
| J8 | **Weekend / rolldown**, Friday 15:00–17:00 | What do I pay to hold this over the weekend? What does the book look like Monday morning if nothing happens? Should I sell the weekend? | 5 Risk (decay path, weekend weighting) + 1 Monitor (event calendar) | ≤ 5 min |
| J9 | **Weekly rule review**, Friday late / weekend | Would a mechanical hedge rule have beaten what I did? What hedge frequency / band was optimal in this regime? | 7 Lab | no latency target |
| J10 | **Trust check**, whenever a number looks wrong | Where did this number come from, how old is it, is it synthetic? | 8 Data & Settings + provenance badges everywhere | ≤ 10 s |

### 1.3 Jobs-to-be-done statements

- **JTBD-1** When I mark my book in the morning, I want every position repriced off a stamped
  snapshot with visible provenance, so I can trust the P&L I am about to explain.
- **JTBD-2** When I decide whether to own gamma, I want implied vs. realized on the *same* horizon
  and estimator basis with a historical cone, so I am not fooled by an estimator artefact.
- **JTBD-3** When spot moves intraday, I want to know my new delta, my hedge trigger, and the
  cost/benefit of hedging now, so I capture the gamma instead of donating the spread.
- **JTBD-4** When I'm short gamma into a cut, I want strike-by-strike pin risk and a countdown to
  each cut in my own clock, so I am never surprised by an inherited delta.
- **JTBD-5** When I close the day, I want P&L split into delta/gamma/theta/vega/vanna/volga/hedge
  with a small residual, so I know *why* I made money and whether the model still fits.
- **JTBD-6** When I consider a rule, I want a no-look-ahead backtest with real costs and a hedge
  frequency sweep, so I can size the rule rather than admire the curve.

---

## 2. User stories and acceptance criteria

MoSCoW: **M** = v1 blocker, **S** = v1 target, **C** = build if cheap, **W** = explicitly deferred
(listed in §7). Format: `G:` given, `W:` when, `T:` then. Every "Then" must be machine-checkable by
QA. `ε` denotes the numerical tolerance stated in §6.4.

### 2.0 Cross-cutting / application shell

**REQ-001 (M) — Single stamped snapshot per session.**
G: the app is running with any provider. W: the user loads any page or presses **Refresh**.
T: all eight pages read the *same* `MarketSnapshot` from `dcc.Store`; the header shows
`asof` in the user's timezone plus the snapshot id; no page issues its own market fetch; pressing
Refresh restamps once and every open figure updates from the new snapshot.

**REQ-002 (M) — Provenance badge on every market number.**
G: a rendered number derives from `MarketSnapshot`. W: the user hovers/inspects it.
T: a badge shows `source`, `kind` ∈ {live, cached, synthetic, user_override} and the field `asof`,
from `MarketSnapshot.meta`; `synthetic` renders purple, `user_override` blue, `cached` amber,
`live` green; a page that contains **any** synthetic field shows a persistent page-level banner.
Live data is never silently replaced by synthetic (charter §3).

**REQ-003 (M) — Empty-book grace.**
G: the book is empty. W: the user opens pages 4, 5, 6.
T: every figure renders an empty axis with the message "no positions — import a CSV or add a
trade", no traceback, no blank white panel, and the aggregate Greek cards read exact zeros.

**REQ-004 (M) — Global pair and reporting-currency selector.**
G: any page. W: the user changes the pair selector or the reporting currency (default USD).
T: the selection persists across pages and sessions (SQLite settings); all cross-pair aggregates
are converted to the reporting currency using the snapshot spot, and the conversion rate used is
disclosed on hover. **[CONTRACT-GAP-1]** `Greeks` has no currency tag and `book_greeks` returns one
untagged `Greeks`, so the conversion must live in the app layer.

**REQ-005 (S) — Keyboard-first navigation.**
G: any page. W: the user presses `1`–`8`. T: the corresponding page loads; `r` refreshes the
snapshot; `/` focuses the pair selector; `?` shows the shortcut sheet.

**REQ-006 (M) — Deterministic demo book.**
G: a fresh install with no SQLite file. W: `python run.py`. T: the app starts on the synthetic
provider with a seeded demo book (≥1 EURUSD straddle, 1 USDJPY risk reversal, 1 GBPUSD strangle,
1 spot hedge line) so every screen is populated, and the header states "DEMO BOOK — synthetic".

### 2.1 Page 1 — Market Monitor (`/`)

**REQ-007 (M) — Spot and return dashboard.**
G: a snapshot with the 9 G10 pairs + 3 crosses. W: the page loads.
T: a table shows for each pair spot (per-pair precision, §6.4), change vs. previous close in pips
and %, and 1d/5d/21d returns; JPY pairs use pip = 0.01; the table sorts by any column.

**REQ-008 (M) — Realized vol, multiple estimators, matched horizon.**
G: a spot history of ≥ 2y. W: the user selects a horizon (10/21/63d) and estimator set.
T: close-to-close, Parkinson, Garman–Klass, Rogers–Satchell and Yang–Zhang RV are shown on the same
annualisation basis (√252 on business days, stated on the chart), each labelled, and the estimator
formula is available on hover; missing OHLC degrades to close-to-close with a badge rather than a
crash.

**REQ-009 (M) — Implied vs. realized spread and richness.**
G: a surface and an RV series per pair. W: the page loads.
T: for each pair a panel shows ATM implied at the tenor matching the RV horizon, the RV, the spread
`σ_i − σ_r` in vol points, and its z-score over a user-set lookback (default 1y); the sign
convention is stated on the panel ("positive = implied over realized = gamma expensive").

**REQ-010 (M) — Cross-pair richness table with ranking.**
G: all pairs priced. W: the page loads.
T: one row per pair with ATM 1M, RV21, spread, spread z, RR25 z, BF25 z, and a rank column; the
table can be sorted and the top/bottom 3 by spread-z are highlighted; pairs with stale or missing
inputs are greyed with the reason.

**REQ-011 (S) — RV–IV heat strip through time.**
G: ≥1y of paired IV/RV history. W: the user picks a pair.
T: a heat strip plots the spread by date × tenor, diverging palette centred at zero, with a legend
in vol points, and a hover readout of the exact pair of numbers behind each cell.

**REQ-012 (S) — Event calendar overlay.**
G: the bundled event calendar (§4.6). W: the user views any time-series chart on the page.
T: FOMC/ECB/BoE/BoJ decisions, US CPI, US NFP, and each pair's top-two domestic releases are drawn
as vertical markers with a tooltip (name, local time, affected ccys); the next 10 events are also
listed as a table with days-to-event and the number of calendar days each adds to the nearest
option expiry.

**REQ-013 (S) — Regime tag.**
G: RV, spread-z and a trend measure. W: the page loads.
T: each pair is tagged one of {quiet-range, trending, breakout, event-pending} with the numeric
rule that fired shown on hover; the rule set is documented in `03_model_spec.md` and the tag never
appears without its inputs.

**REQ-014 (C) — Cross-pair implied correlation.**
G: EURUSD, USDJPY and EURJPY surfaces. W: the user opens the correlation panel.
T: implied correlation is shown as `ρ = (σ_c² − σ_1² − σ_2²)/(2σ_1σ_2)` per tenor for each USD
triangle, alongside realized correlation over the matched window, with the triangle identity stated
on the panel.

### 2.2 Page 2 — Vol Surface (`/surface`)

**REQ-015 (M) — Surface build from broker-style quotes.**
G: a provider that emits `list[SmileQuotes]` per pair. W: the user selects pair and method
∈ {vanna_volga, sabr, interp}. T: `build_surface` is called and the 3-D surface (strike × tenor ×
vol), the smile-by-tenor slice and the ATM term structure render; changing the method re-renders
without a page reload and shows the method name on the figure.

**REQ-016 (M) — Calibration residuals are visible.**
G: a built surface. W: the page loads.
T: a residual panel shows, per tenor, the reproduction error on the input ATM/RR25/BF25 (and 10d if
supplied) in vol points; any tenor with |residual| > 0.10 vol pt is flagged amber and > 0.25 red;
the model is never shown as valid without its residuals.

**REQ-017 (M) — Smile readout in the trader's units.**
G: a smile slice. W: the user hovers a point.
T: the tooltip gives strike, delta (in the pair's `delta_convention`, named explicitly, e.g.
"25d spot premium-adjusted"), vol, and the strike expressed as % of spot and as pips from spot.

**REQ-018 (M) — Vol cone.**
G: ≥ 2y RV history. W: the user selects a pair.
T: a cone plots RV percentiles (5/25/50/75/95) by horizon with current RV and current implied
overlaid; the sample size per horizon is printed; horizons with < 100 observations are hatched.

**REQ-019 (S) — Skew and convexity richness.**
G: RR25/BF25 history. W: the page loads.
T: RR25 and BF25 are plotted with their 1y z-score and percentile; the sign convention is stated
("RR > 0 = base-ccy calls over"); the current value and z are shown as cards.

**REQ-020 (S) — Term-structure kink / event pricing.**
G: an ATM term structure and the event calendar. W: the user enables "event view".
T: the ATM curve is replotted against **event-weighted time** (§4.6) and the implied event-day move
is reported as `sqrt( (σ_a²T_a − σ_b²T_b) / w_event )` in % for the tenor pair that straddles the
event, with the two tenors used named on the panel.

**REQ-021 (C) — Strike/tenor what-if pricer.**
G: a surface. W: the user types a strike (any format of §3.3) and a tenor.
T: the panel returns vol, price in quote ccy and in pips and in % of base notional, all Greeks per
1mm base notional, and the breakeven per §4.1 — without touching the book.

### 2.3 Page 3 — Gamma Map (`/gamma-map`)

**REQ-022 (M) — Market gamma by strike.**
G: CME settlement + open interest by strike for the pair's `cme_code`. W: the page loads.
T: a bar chart shows open interest and estimated dealer gamma by strike for the front expiries, in
the pair's quote convention (CME quotes USD per foreign ccy; inverted products must be converted
and the conversion stated), with spot drawn as a vertical line.

**REQ-023 (M) — Dealer-gamma sign assumption is explicit.**
G: any market-gamma figure. W: the page loads.
T: the sign convention and the assumption used to infer dealer positioning are printed on the
figure ("assumes customers buy calls/puts as tagged; OI is not net dealer position"); no screen
presents inferred dealer gamma as fact.

**REQ-024 (M) — Expiry ladder.**
G: CME/OI data. W: the page loads.
T: a ladder shows total OI notional by expiry date for the next 8 expiries, with each expiry's cut
time and the number of business days to it.

**REQ-025 (S) — Strike magnets / pin candidates.**
G: OI by strike. W: the page loads.
T: strikes holding > 5% of front-expiry OI within ±2% of spot are labelled as magnets, with
distance in pips and %, and in **sigma-days** (`distance% / (100·σ_ATM/√365)`).

**REQ-026 (S) — My book overlaid on the market's gamma.**
G: a non-empty book. W: the user toggles "overlay my book".
T: the user's `Γ₁` by strike bucket is drawn on the same axis (secondary y, different mark), so
alignment/conflict with market gamma is visible; the legend distinguishes "market (OI-implied)"
from "mine (exact)".

**REQ-027 (C) — Barrier / option-expiry news lines.**
G: user-entered levels. W: the user adds a level with a label.
T: the level persists in SQLite and is drawn on the gamma map and on the Risk ladder; levels are
user data and badged `user_override`.

### 2.4 Page 4 — Position Book (`/book`)

**REQ-028 (M) — Option ticket.**
G: the Book page. W: the user completes the option ticket of §3.1 and presses Add.
T: an `OptionPosition` is written to SQLite with a generated `id`, appears in the blotter within
500 ms, is priced on the current snapshot, and all Greek cards on page 5 change accordingly; any
validation failure (§3.4) blocks the write and names the offending field.

**REQ-029 (M) — Spot / hedge ticket.**
G: the Book page. W: the user completes the spot ticket of §3.2 and presses Add.
T: a `SpotPosition` is written with signed `notional_base` (+ = long base), `entry_rate`, optional
`value_date`, and `tag`; selecting "this is a delta hedge" sets `tag="hedge"` so P&L attribution
separates it (contract §5).

**REQ-030 (M) — Structure builder expands to legs.**
G: the option ticket. W: the user selects a structure ∈ {single, straddle, strangle, risk
reversal, butterfly, call spread, put spread, calendar} and enters the structure-level inputs.
T: the ticket expands into the correct legs with correct signs before submission, the legs are
shown for confirmation, each leg is stored as its own `OptionPosition`, and all legs share a
`tag` of the form `<structure>:<ticket-uuid>` so they can be grouped, rolled and deleted together.

**REQ-031 (M) — Edit and delete.**
G: an existing position. W: the user edits a field or deletes the row.
T: the change is persisted and re-priced immediately; deletion asks for confirmation and is
soft (row kept with `deleted_at`) so historical P&L snapshots remain reconstructible.

**REQ-032 (M) — Live re-pricing blotter.**
G: a book of ≤ 200 positions. W: the snapshot refreshes.
T: `price_book` output renders one row per position with PV (quote ccy and reporting ccy), current
vol used, `delta_base`, `Γ₁`, `ν`, `θ`, vanna, volga, days to expiry, and P&L since trade
(`PV − premium_paid`); the whole table recomputes in < 500 ms (§6.1).

**REQ-033 (M) — CSV import with a preview gate.**
G: a CSV per §3.5. W: the user uploads it.
T: a preview table shows every row with status OK / WARN / ERROR and a human reason; the Commit
button is disabled while any ERROR exists; committing is atomic (all rows or none); WARN rows
require an explicit "accept warnings" tick; a downloadable rejects file is offered.

**REQ-034 (M) — CSV export round-trips.**
G: any book. W: the user exports.
T: the file uses exactly the §3.5 schema and re-importing it reproduces the identical book
(field-by-field equality on all `OptionPosition`/`SpotPosition` fields, `id` preserved).

**REQ-035 (S) — What-if / pre-trade ticket.**
G: a book. W: the user fills a ticket and presses **Preview** instead of Add.
T: the app shows the book Greeks before, after, and the delta of each Greek, plus the change in
daily breakeven and in θ; nothing is persisted; a Commit button on the same panel converts the
preview into a real trade.

**REQ-036 (S) — Mark-vol override per position.**
G: a position. W: the user sets a manual mark vol on it.
T: the position prices off that vol, the row is badged `user_override`, and the P&L page shows
"mark-to-my-vol vs mark-to-surface" as a separate line. **[CONTRACT-GAP-2]** `OptionPosition` is
frozen and has no `mark_vol`; the override must be stored in a `store.py` side-table keyed by
position `id`.

**REQ-037 (S) — Book grouping and filters.**
G: a book with tags. W: the user filters by pair / tag / expiry bucket / structure.
T: the blotter, the Greek cards and page 5 all respect the filter, and the active filter is stated
in the page header so no screen ever silently shows a subset.

### 2.5 Page 5 — Risk (`/risk`)

**REQ-038 (M) — Aggregate Greek cards.**
G: a non-empty book. W: the page loads.
T: cards show PV, `delta_base` (per pair, in base ccy mm), `Γ₁` (base ccy mm per 1%), `ν` (per vol
pt), `θ` (per calendar day), vanna, volga, and dual delta, each with its unit written on the card;
per-pair values are in that pair's quote ccy, and the totals row is in the reporting currency with
the conversion disclosed.

**REQ-039 (M) — Daily breakeven card.**
G: `Γ₁`, `θ`, `S`. W: the page loads.
T: a card shows the daily breakeven move per pair in % and in pips using the §0 identity, the
number of realized-vol-implied sigma this represents (`BE% ÷ (100σ_r/√365)`), and the gamma/theta
ratio `Γ$/|θ|`; for an ATM-only test book the value equals `σ_ATM/√365` to within ε.

**REQ-040 (M) — Spot ladder with sticky toggle.**
G: a book and a pair. W: the user sets range and `sticky` ∈ {strike, delta, none}.
T: `spot_ladder` renders PV, `delta_base`, `Γ₁`, `ν`, `θ` against spot over the range; sticky-delta
adds the skew-induced delta `ν·∂σ/∂S` and the two curves can be shown together for comparison; the
active convention is written on the figure; 101 points recompute in < 800 ms (§6.1).

**REQ-041 (M) — Scenario grid.**
G: a book. W: the user sets spot shocks (default −5%…+5%), vol shocks (default −3…+3 vol pts) and
`days_fwd` (default 0). T: `scenario_grid` renders a P&L matrix, diverging palette centred at zero,
cells labelled in reporting ccy; the worst and best cells are called out with their coordinates;
hovering a cell gives the full Greek set at that node.

**REQ-042 (M) — Gamma zones with sensitivity.**
G: a book with clustered strikes. W: the page loads.
T: each `GammaZone` is listed with lo/hi/center, `gamma_1pct`, `peak_gamma_1pct`,
`share_of_total`, `distance_pct`, the constituent expiries, and the label; and, added by this
spec: distance in **sigma-days**, the **probability spot touches the zone** before the zone's
nearest expiry (first-passage under GBM in the forward measure — formula in `03_model_spec.md`),
the **P&L if spot goes to the zone centre** under the active sticky convention, and the **delta the
book would carry inside the zone**. Zones render as shaded bands on the spot ladder.

**REQ-043 (M) — Hedge bands and the next trigger.**
G: a book, a pair, a `HedgeRule`. W: the user sets the rule.
T: `hedge_bands` renders the upper/lower spot triggers, current delta vs. band, distance to the
next trigger in pips and %, the size that would be traded, and the estimated cost from
`HedgeRule.cost_bp`; `hedge_suggestion` returns a `HedgeAction` shown as a one-line instruction
("SELL EUR 4.2mm at 1.0871 — delta 4.2mm vs band 2.5mm — est. cost USD 217").
**[CONTRACT-GAP-3]** `hedge_bands(book, mkt, pair, *, gamma_budget)` takes no cost/rule argument;
requesting it accept an optional `rule: HedgeRule`.

**REQ-044 (M) — Expiry ladder and cut clock.**
G: options with mixed expiries and cuts. W: the page loads.
T: a table lists each expiry with cut, the exact cut instant in the user's timezone and in the cut's
own timezone, a live countdown, notional and `Γ₁` expiring, and θ released after that expiry;
`conventions.expiry_datetime` is the single source of the instant.

**REQ-045 (M) — Pin risk panel.**
G: options with ≤ 3 business days to expiry. W: the page loads.
T: `pin_risk` renders per strike: distance in pips and %, notional at that strike, `Γ₁` at the cut,
the **delta discontinuity inherited at expiry** (`Σ direction·cp·notional_base` for strikes crossed),
and P(finish within ±k pips of the strike) for k ∈ {10, 25, 50} (JPY-scaled); strikes within 0.5
sigma-days of spot are highlighted red.

**REQ-046 (M) — Decay path ("what if I do nothing").**
G: a book. W: the user opens the decay panel.
T: `time_decay` renders PV and cumulative θ day by day to the last expiry with spot and surface
frozen; the path is drawn under three realized-vol assumptions (0, implied, current RV21) using the
delta-hedged identity `0.5·Γ$·(σ_r² − σ_i²)·Δt`; weekends and holidays use the decay weights of
§4.6 and the weighting scheme in force is named on the figure. **[CONTRACT-GAP-4]** `time_decay`
exposes no calendar/weighting argument.

**REQ-047 (S) — Worst case / stop-out.**
G: a book and a user-set stop loss. W: the page loads.
T: the panel reports the worst 1-day P&L across (a) the scenario grid, (b) a bundled historical
shock library (§4.9), and (c) the empirical 1-day joint spot/vol move distribution over the
available history; and the spot level in each direction at which cumulative loss breaches the stop.

**REQ-048 (S) — Concentration.**
G: a book. W: the page loads.
T: `Γ₁` and vega concentration by strike bucket and by expiry are shown as a heat table with a
Herfindahl index and the "% of gamma in the single largest expiry" figure; anything above a
user-set threshold (default 40%) is flagged.

**REQ-049 (S) — Cross-pair netting through USD.**
G: a multi-pair book. W: the user opens the USD panel.
T: each position's delta is decomposed into its two currency legs; net exposure per currency is
shown; and a beta-weighted "USD basket gamma" reports the change in total delta for a 1% move in a
trade-weighted USD factor with per-pair betas estimated over a stated window.

**REQ-050 (C) — Correlation exposure of cross positions.**
G: positions in EURJPY/EURGBP/EURCHF. W: the user opens the correlation panel.
T: each cross position is shown with its implied-correlation sensitivity
`corr_vega = ν_cross · σ₁σ₂/σ_cross · 0.01` per 0.01 of ρ, and the equivalent USD-leg gamma
decomposition, with the triangle identity printed.

### 2.6 Page 6 — P&L (`/pnl`)

**REQ-051 (M) — Daily attribution waterfall.**
G: two stamped snapshots t0, t1 and the hedges traded between them. W: the page loads.
T: `daily_pnl` renders a waterfall with delta, gamma, theta, vega, vanna, volga, rates, carry,
hedge and unexplained, summing exactly to `total` (to ε), in the reporting currency, with the two
snapshot timestamps and the trading-day boundary (default 17:00 New York) stated.

**REQ-052 (M) — Unexplained is policed.**
G: an attribution. W: `|unexplained| / (Σ|components|) > 5%`.
T: the residual is highlighted red with the message "attribution does not fit — check marks or
re-taylor", and a diagnostic panel lists the three positions contributing most to the residual.

**REQ-053 (M) — Per-position attribution.**
G: an attribution. W: the user expands the waterfall.
T: `PnLBreakdown.detail` renders one row per position with the same component split, sortable,
and totals reconciling to the aggregate waterfall.

**REQ-054 (M) — Cumulative P&L and drawdown.**
G: ≥ 2 stored daily marks. W: the page loads.
T: a cumulative line by component plus a drawdown sub-panel; the series is rebuilt from stored
snapshots (not recomputed against today's surface), so history is stable.

**REQ-055 (M) — Hedge log.**
G: spot positions tagged `hedge`. W: the page loads.
T: each hedge is listed with timestamp, pair, size, rate, the pre- and post-hedge delta, the spot at
the moment of the hedge, estimated cost, and its realized contribution; the log totals reconcile to
the `hedge` bar in the waterfall. **[CONTRACT-GAP-5]** `SpotPosition.trade_date` is a `date`, so
two hedges on the same day cannot be ordered or timestamped; intraday hedge logging needs a
datetime (side-table in `store.py` if the dataclass stays frozen).

**REQ-056 (S) — Gamma P&L vs. theta bill scoreboard.**
G: an attribution history. W: the user selects a window.
T: cumulative gamma P&L, cumulative theta, their ratio, and the implied "realized vol I actually
captured" (the σ_r that would make gamma pay theta) are shown per pair, against the average implied
paid over the window.

**REQ-057 (S) — Vanna/volga contribution split.**
G: an attribution with a moved smile. W: the page loads.
T: the second-order smile terms are shown separately as `vanna·dS·dσ` and `0.5·volga·dσ²`, with the
dS and dσ used printed, so smile P&L is never buried inside "vega".

**REQ-058 (C) — Mark-to-surface vs. mark-to-my-vol.**
G: positions with mark-vol overrides (REQ-036). W: the page loads.
T: a line shows the P&L difference between the two marks, so the trader sees exactly what their
override is worth.

### 2.7 Page 7 — Signals & Backtest (`/lab`)

**REQ-059 (M) — Strategy configuration.**
G: the Lab page. W: the user configures pair, tenor, structure (ATM straddle / strangle / RR),
entry rule (always-on, spread-z threshold, cone percentile), roll frequency, `HedgeRule`, and costs.
T: the configuration is validated, persisted as a named preset, and shown as a JSON summary that
can be copied.

**REQ-060 (M) — No look-ahead is enforced and demonstrated.**
G: any backtest. W: it runs.
T: every signal at date `t` uses only data with timestamp ≤ `t`; the engine exposes a check that
shifting the input series forward by one day changes results, and a QA fixture asserts that
reversing that shift reproduces the original — a look-ahead-detection test must exist and pass.

**REQ-061 (M) — Equity curve and stats.**
G: a completed run. W: results render.
T: the equity curve (gross and net of costs) plus a stats table with total P&L, Sharpe, hit rate,
average daily gamma P&L, average theta, max drawdown, turnover, total hedge cost, and cost as a %
of gross are shown; every stat states its annualisation basis.

**REQ-062 (M) — Hedge frequency / band sweep.**
G: a strategy. W: the user runs the sweep.
T: net P&L, Sharpe and cost are plotted against hedge band width and against hedge interval;
the argmax band is reported with a caution that it is in-sample; the sweep runs over a stated grid
and completes within the §6.1 budget or streams progress.

**REQ-063 (S) — Cost model is explicit and adjustable.**
G: any run. W: the user opens the cost panel.
T: per-pair spot bid/offer in pips and option vega bid/offer in vol points are user-editable, with
documented defaults per pair; changing them re-runs and the delta in net P&L is shown.

**REQ-064 (S) — Realized-vs-implied P&L decomposition of the backtest.**
G: a delta-hedged straddle run. W: results render.
T: P&L is decomposed into `Σ 0.5·Γ$·(σ_r² − σ_i²)·Δt` plus hedge slippage plus discretisation
error, and the three sum to the simulated P&L within ε.

**REQ-065 (S) — Regime and event conditioning.**
G: a completed run. W: the user groups by regime tag (REQ-013) or by event/non-event days.
T: the stats table is reproduced per bucket with sample sizes, and buckets with n < 20 are flagged
as not interpretable.

### 2.8 Page 8 — Data & Settings (`/data`)

**REQ-066 (M) — Source status board.**
G: the configured providers. W: the page loads.
T: each source is listed with endpoint, last successful pull, age, row count, and status ∈ {ok,
stale, failed, blocked, synthetic}; a blocked/403 source shows the exact reason, and the board never
shows "ok" for a source serving cached or synthetic values.

**REQ-067 (M) — Provider toggle.**
G: `--provider synthetic|live`. W: the user switches in the UI.
T: the snapshot is rebuilt from the chosen provider, every page restamps, and a confirmation is
required when switching *to* synthetic while a real book is loaded.

**REQ-068 (M) — Manual overrides.**
G: any pair/tenor/rate. W: the user enters an override value.
T: the value is used everywhere downstream, is badged `user_override` with the user's note, is
listed on a single "active overrides" panel, and can be cleared individually or all at once.

**REQ-069 (M) — Cache control.**
G: a populated cache. W: the user views the page.
T: cache size, per-source entry counts and oldest/newest timestamps are shown, with per-source and
global clear buttons; clearing prompts because live re-fetch may be blocked (charter §3).

**REQ-070 (S) — Live-source verification from the UI.**
G: `scripts/verify_live_sources.py` exists. W: the user presses "Verify live sources".
T: the script runs and its per-source pass/fail output is rendered in the page, with the caveat that
it will fail inside the sandbox by design.

**REQ-071 (S) — Preferences.**
G: the settings panel. W: the user sets timezone, reporting currency, trading-day boundary,
default hedge rule, notional display unit (absolute vs. mm), and theme.
T: all persist in SQLite and apply on next render without restart.

---

## 3. Trade entry — the data model from the trader's point of view

### 3.1 Option ticket

| # | Field | Required | Type / widget | Default | Notes |
|---|---|---|---|---|---|
| 1 | Pair | yes | dropdown, `conventions.PAIRS` | last used, else EURUSD | drives pip, cut, delta convention |
| 2 | Structure | yes | dropdown | Single | expands to legs (REQ-030) |
| 3 | Direction | yes | Buy / Sell toggle | Buy | → `direction` ±1; for structures, applies to the structure as quoted (buy a straddle = buy both legs; buy a RR = buy the base-ccy call, sell the put) |
| 4 | Call/Put | yes (single) | toggle | Call | `cp` = +1 call on base, −1 put on base. Label must spell it out: "EURUSD call = right to buy EUR" |
| 5 | Expiry | yes | date picker + tenor buttons ON/1W/2W/1M/2M/3M/6M | 1M | tenor button computes date from `asof` + `spot_lag` + tenor, rolled to the next good business day |
| 6 | Cut | no | dropdown NY10/TKY15/LDN16 | `PAIRS[pair].cut` | shown with the resulting UTC instant |
| 7 | Strike | yes | free text, §3.3 grammar | ATMF | resolved live; the resolved absolute strike is echoed before submit |
| 8 | Notional (base) | yes | free text with suffixes | 10mm | always **base ccy** (contract §2); label shows the ccy, e.g. "EUR notional" |
| 9 | Trade date | no | date | today | |
| 10 | Trade spot | no | number | live spot | used for entry-mark and P&L-since-trade |
| 11 | Trade vol | no | number | surface vol at (K, T) | accepts 7.85 or 0.0785 (§3.4 V-9) |
| 12 | Premium | no | number + unit selector | auto-priced from trade vol | unit ∈ {total, pips, % of base notional, % of quote notional}; normalised to a **total in `premium_ccy`** on save |
| 13 | Premium ccy | no | dropdown | quote ccy | e.g. USDJPY premium in JPY; if the user picks base ccy the app converts at `trade_spot` and shows both |
| 14 | Tag | no | free text | `""` | free grouping label; structures auto-tag `<structure>:<uuid>` |

Sign rules shown next to the premium box, always: **`premium_paid > 0` = debit (you paid).** A sold
option must carry a negative `premium_paid`; the UI sets the sign from Direction and warns if the
user overrides it.

### 3.2 Spot / hedge ticket

| # | Field | Required | Type | Default | Notes |
|---|---|---|---|---|---|
| 1 | Pair | yes | dropdown | last used | |
| 2 | Side | yes | Buy base / Sell base | Buy | writes the sign of `notional_base` |
| 3 | Notional (base) | yes | text with suffixes | current book delta, rounded (so "flatten me" is one click) | signed on save: + = long base |
| 4 | Rate | yes | number | live spot | → `entry_rate` |
| 5 | Trade date | no | date | today | |
| 6 | Value date | no | date | trade date + `spot_lag`, business-day rolled | |
| 7 | Is a delta hedge | no | checkbox | **on** when created from the Risk page hedge button | sets `tag="hedge"` (drives P&L split) |
| 8 | Tag | no | text | `"hedge"` or `""` | |

### 3.3 Strike-entry grammar (one parser, used by the ticket, the CSV importer and the what-if pricer)

| Input | Meaning | Resolves via |
|---|---|---|
| `1.0850`, `147.25` | absolute strike | as typed |
| `ATM` | at-the-money spot | `S` |
| `ATMF` | at-the-money forward | forward for the expiry |
| `ATMDN` | delta-neutral straddle strike | surface convention |
| `25dc`, `25DP`, `10dc` | 25-delta call / put | `strike_from_delta(...,convention=PAIRS[pair].delta_convention)` |
| `+50p`, `-30p` | ±50 pips from spot (pip per pair) | `S ± n·pip` |
| `101%`, `98.5%` | percent of spot | `S · pct/100` |
| `F+100p` | pips from the forward | forward ± n·pip |

The resolved absolute strike, the delta it corresponds to, and the convention name are always
echoed back before the trade is written. Delta inputs are rejected if the surface for that pair or
tenor is unavailable rather than silently falling back to a flat vol.

### 3.4 Validation rules

Severity: **E** blocks the write; **W** warns and requires acknowledgement.

| ID | Rule | Sev | Message shown |
|---|---|---|---|
| V-1 | `pair` ∈ `conventions.PAIRS` (case-insensitive) | E | "unknown pair 'EURSD'; known: …" |
| V-2 | `expiry_datetime(expiry, cut) > asof` | E | "expiry is in the past" (import may pass `allow_expired=true` to load history) |
| V-3 | expiry ≤ `asof` + 2 years | W | "beyond the v1 tenor grid (2Y); surface will extrapolate" |
| V-4 | `0.2·S ≤ K ≤ 5·S` | E | "strike 108.50 is implausible vs spot 1.0850 — unit error?" |
| V-5 | `|ln(K/S)| ≤ 4·σ_ATM·√T` | W | "strike is 6.1 std devs from spot; confirm" |
| V-6 | JPY-quoted pair and `K < 10` | E | "USDJPY strike must be in JPY (e.g. 147.25)" |
| V-7 | non-JPY pair and `K > 20` | E | "EURUSD strike must be in the 0.2–5 range" |
| V-8 | `notional_base > 0` for options; `≠ 0` for spot | E | "notional must be positive; sign lives in Direction" |
| V-9 | `|notional_base| ≥ 1000` | W | "notional 10 — did you mean 10mm? use the mm/bn suffix" |
| V-10 | `trade_vol` normalised: if value > 1.0 treat as percent (÷100) and warn; then `0.005 ≤ σ ≤ 2.0` | W then E | "read 7.85 as 7.85% = 0.0785" / "vol out of range" |
| V-11 | `cp` ∈ {+1, −1} after parsing C/P/CALL/PUT | E | |
| V-12 | `direction` ∈ {+1, −1} after parsing B/S/BUY/SELL | E | |
| V-13 | `cut` ∈ `conventions.CUTS` | E | |
| V-14 | `premium_ccy` ∈ {base, quote} of the pair | E | "premium ccy CHF is not a leg of EURUSD" |
| V-15 | sign(`premium_paid`) = sign(`direction`) when non-zero | W | "you sold this option but recorded a debit premium" |
| V-16 | `|premium_paid|` ≤ 25% of `notional_base·S` (quote ccy) | W | "premium is 31% of notional — unit error?" |
| V-17 | `|trade_spot / S_today − 1| ≤ 20%` | W | "trade spot 1.35 is far from today's 1.0850" |
| V-18 | duplicate `id` on import | E unless `--upsert` | "row 14: id opt-0007 already exists" |
| V-19 | spot ticket `entry_rate` within ±20% of live spot | W | |
| V-20 | structure legs internally consistent (RR = one call one put, same expiry, opposite directions; straddle = same strike) | E | |
| V-21 | total book size ≤ 2000 positions | W | "performance targets are specified up to 200 positions" |

### 3.5 CSV specification (import and export — one schema, both directions)

- UTF-8, comma separated, **header row required**, `.` decimal separator, no thousands separators
  (suffixes allowed, see below), lines beginning `#` and blank lines skipped, header order
  irrelevant, header names case-insensitive and matched after stripping spaces/underscores.
- Unknown columns are preserved in a `notes` blob and reported as a WARN, never silently dropped.
- One row per **leg**, not per structure. Structures are grouped by the `tag` column.
- Numeric suffixes accepted in `notional_base` and `premium_paid`: `k`, `m`/`mm`, `bn`
  (case-insensitive). `10mm` = 10 000 000.

| Column | Type | Required | Applies to | Default | Example |
|---|---|---|---|---|---|
| `instrument_type` | `OPTION` \| `SPOT` | yes | both | — | `OPTION` |
| `id` | string | no | both | generated `opt-####` / `spt-####` | `opt-0001` |
| `pair` | string | yes | both | — | `EURUSD` |
| `cp` | `C`\|`P`\|`CALL`\|`PUT`\|`1`\|`-1` | yes for OPTION | option | — | `C` |
| `strike` | number or §3.3 token | yes for OPTION | option | — | `1.0850`, `25dc` |
| `expiry` | ISO date `YYYY-MM-DD` | yes for OPTION | option | — | `2026-10-07` |
| `cut` | `NY10`\|`TKY15`\|`LDN16` | no | option | `PAIRS[pair].cut` | `NY10` |
| `notional_base` | number (+suffix) | yes | both | — | `10mm` |
| `direction` | `1`\|`-1`\|`B`\|`S`\|`BUY`\|`SELL` | yes for OPTION | option | — | `B` |
| `premium_paid` | number, signed, debit > 0 | no | option | `0` | `87500` |
| `premium_ccy` | ISO ccy | no | option | quote ccy of `pair` | `USD` |
| `premium_unit` | `total`\|`pips`\|`pct_base`\|`pct_quote` | no | option | `total` | `pips` |
| `trade_date` | ISO date | no | both | blank | `2026-09-05` |
| `trade_spot` | number | no | both | blank | `1.0848` |
| `trade_vol` | number (decimal or %) | no | option | blank | `7.05` |
| `entry_rate` | number | yes for SPOT | spot | — | `1.0862` |
| `value_date` | ISO date | no | spot | trade_date + spot_lag | `2026-09-09` |
| `tag` | string | no | both | `""` | `straddle:eu1m` |
| `notes` | string | no | both | `""` | `hedge from risk page` |

`premium_unit` is an **importer-side** convenience: it is normalised to a total in `premium_ccy`
before constructing the frozen `OptionPosition` (which stores only a total). Conversions used:
`total = pips · notional_base · pip`, `total = pct_base/100 · notional_base · S_trade`,
`total = pct_quote/100 · notional_base · S_trade`. `S_trade` = `trade_spot`, else the snapshot spot;
if neither exists the row is an ERROR.

**Example A — EURUSD 1M ATM straddle, long 10mm per leg** (spot 1.0850, ATM 7.05%, premium 87.5 pips
per leg; figures illustrative):

```csv
instrument_type,id,pair,cp,strike,expiry,cut,notional_base,direction,premium_paid,premium_ccy,premium_unit,trade_date,trade_spot,trade_vol,entry_rate,value_date,tag,notes
OPTION,opt-0001,EURUSD,C,1.0850,2026-10-07,NY10,10mm,B,87.5,USD,pips,2026-09-05,1.0848,7.05,,,straddle:eu1m,ATM straddle leg 1
OPTION,opt-0002,EURUSD,P,1.0850,2026-10-07,NY10,10mm,B,87.5,USD,pips,2026-09-05,1.0848,7.05,,,straddle:eu1m,ATM straddle leg 2
SPOT,spt-0001,EURUSD,,,,,-1.2mm,,,,,2026-09-05,1.0848,,1.0862,2026-09-09,hedge,delta hedge after leg 2
```

**Example B — USDJPY 3M 25d risk reversal, long USD call / short USD put, 20mm USD per leg**
(spot 147.20; note base = USD, premium in JPY, pip = 0.01, so 105 pips = 1.05 JPY per USD;
figures illustrative):

```csv
instrument_type,id,pair,cp,strike,expiry,cut,notional_base,direction,premium_paid,premium_ccy,premium_unit,trade_date,trade_spot,trade_vol,entry_rate,value_date,tag,notes
OPTION,opt-0010,USDJPY,C,150.50,2026-12-07,NY10,20mm,B,21000000,JPY,total,2026-09-05,147.20,8.60,,,rr25:uj3m,long USD call = long USD vs JPY
OPTION,opt-0011,USDJPY,P,141.50,2026-12-07,NY10,20mm,S,-23000000,JPY,total,2026-09-05,147.20,9.55,,,rr25:uj3m,short USD put; credit -> negative premium
```

Notes the importer must enforce on Example B: `notional_base` is **USD** because USD is the base of
USDJPY (contract §2) — a trader typing a JPY notional is caught by V-9/V-16; `premium_ccy=JPY` is
the quote ccy and is the default; the short leg's premium is negative (V-15); strikes must be in
JPY terms (V-6).

### 3.6 Import UX

1. Drop file → parse → **preview table** with a status column and a reason column per row.
2. Counts banner: `n OK · n WARN · n ERROR`. Commit disabled while ERROR > 0.
3. "Download rejects" produces the same schema plus a `reason` column, so the trader fixes and
   re-uploads only the bad rows.
4. Commit is a single SQLite transaction; on any failure nothing is written.
5. Modes: **append** (default), **upsert by id**, **replace book** (requires typing the book name).
6. Export always writes the full schema so `import(export(book)) == book` (REQ-034).

---

## 4. Features the user did not ask for but will want

Each is rated MoSCoW and mapped to the REQ that carries it. Rationale is one line, deliberately.

### 4.1 Gamma economics

| Feature | Rationale | MoSCoW | REQ |
|---|---|---|---|
| Daily breakeven move (%, pips, sigma-days) | The single number that says whether today's range pays the theta bill. | M | REQ-039 |
| Gamma/theta ratio `Γ$/|θ|` per pair and per expiry bucket | Compares "rent paid" across pairs and tenors on one scale. | M | REQ-039 |
| Breakeven ladder by tenor | Shows the cheapest tenor to own the move you actually expect. | S | REQ-021 |
| "Realized vol I captured" (σ_r that made gamma pay theta) | Converts P&L into the only vol number that matters ex-post. | S | REQ-056 |
| Gamma per unit premium and per unit vega | Ranks structures by gamma bought per dollar risked, not by notional. | S | REQ-032 |

### 4.2 Hedging

| Feature | Rationale | MoSCoW | REQ |
|---|---|---|---|
| Hedge bands with the next trigger in pips | Turns "am I too long delta" into a level to watch. | M | REQ-043 |
| Hedge P&L simulator (what a given band would have made on the last N days of spot) | Sizes the band with evidence instead of habit. | S | REQ-062 |
| Cost-aware band width (Whalley–Wilmott `(3/2·e^{−rT}·k·S·Γ²/γ)^{1/3}` with a risk-aversion slider) | Makes the cost/variance trade-off explicit rather than a rule of thumb. | C | REQ-062 |
| Empirical optimal band from the Lab sweep, fed back to the Risk page | Closes the loop between backtest and live band. | S | REQ-062 |
| Hedge slippage accounting separate from gamma P&L | Stops execution cost being mistaken for model error. | M | REQ-055, REQ-064 |

### 4.3 Expiry and pin management

| Feature | Rationale | MoSCoW | REQ |
|---|---|---|---|
| Cut clock — countdown to each cut in the trader's own timezone | Cut times are the most common operational error in an FX options book. | M | REQ-044 |
| Delta discontinuity inherited at expiry, per strike | Tells you the spot position you wake up with if it expires in/out. | M | REQ-045 |
| P(finish within ±k pips of strike) | Quantifies pin risk instead of eyeballing distance. | S | REQ-045 |
| Rolldown view: what the position is worth in 1W with spot unchanged | The Friday question, answered as a number. | S | REQ-046 |
| Auto-grouping of legs into structures with one-click roll | Structures are traded and rolled as units, not legs. | C | REQ-030 |

### 4.4 Zone sensitivity (the user asked "how sensitive is each gamma zone")

| Feature | Rationale | MoSCoW | REQ |
|---|---|---|---|
| Zone distance in **sigma-days** rather than % | 0.4% means nothing until you know it is 1.1 daily sigma away. | M | REQ-042 |
| Touch probability before the zone's nearest expiry | Weights each zone by how likely you are to trade in it. | S | REQ-042 |
| P&L if spot goes to zone centre, and the delta carried inside the zone | Pre-computes the decision you will otherwise make in a hurry. | M | REQ-042 |
| Zones shaded on the spot ladder rather than listed separately | One picture instead of a table plus mental arithmetic. | S | REQ-042 |

### 4.5 Ladder and scenario mechanics

| Feature | Rationale | MoSCoW | REQ |
|---|---|---|---|
| Sticky-strike vs. sticky-delta toggle, both curves overlayable | The difference is the entire skew-delta; hiding it misstates the hedge. | M | REQ-040 |
| Scenario grid with `days_fwd` | The realistic question is "spot there, two days from now", not "spot there, instantly". | M | REQ-041 |
| Notional ladder vs. premium ladder | Same risk, two mental models; traders switch between them constantly. | C | REQ-032 |
| Worst-case / stop-out spot levels | The pre-committed exit, computed rather than felt. | S | REQ-047 |
| Bundled historical shock library (SNB 2015, Brexit 2016, JPY intervention days, CHF/JPY 2022–24 moves) | Free, offline, and far more informative than a symmetric ±5% grid. | S | REQ-047 |

### 4.6 Time, events and the calendar

| Feature | Rationale | MoSCoW | REQ |
|---|---|---|---|
| Bundled event calendar (FOMC, ECB, BoE, BoJ, US CPI, US NFP, plus per-pair top-2 domestic releases), shipped as a versioned CSV in `data/` with a documented public refresh source and manual add/edit | The sandbox blocks live calendar fetches; a static, editable file is the only honest design. | S | REQ-012 |
| **Event-weighted (business) time**: day weights — normal weekday 1.0, weekend day 0.10, market holiday 0.25, NFP 1.6, CPI 1.7, central-bank decision 2.0 (all user-editable); `T_bt = Σw_i / Σw_normal_year` | Calendar-time theta over-charges weekends and under-charges event days; this is what the OTC market actually prices. | S | REQ-020, REQ-046 |
| Implied event-day move extracted from the term-structure kink | Says whether the event is already paid for before you buy it. | S | REQ-020 |
| Weekend decay panel: cost to carry Friday→Monday under both calendar and business time | The specific Friday decision, with the two conventions side by side. | S | REQ-046 |
| Event markers on every time series | Prevents attributing an event move to a model. | S | REQ-012 |

### 4.7 Smile / higher-order risk

| Feature | Rationale | MoSCoW | REQ |
|---|---|---|---|
| Vanna and volga as first-class cards and P&L lines | On a risk-reversal book these are the P&L, not a rounding term. | M | REQ-038, REQ-057 |
| Vol-of-vol proxy (realized vol of ATM implied) alongside volga | Prices whether the convexity you own is likely to be paid. | C | REQ-019 |
| Calibration residual gate on every surface | An uncalibrated smile silently corrupts every downstream Greek. | M | REQ-016 |
| Mark-to-my-vol override with an explicit P&L difference line | Every trader disagrees with the mark sometimes; make the disagreement measurable. | S | REQ-036, REQ-058 |

### 4.8 Cross-pair structure

| Feature | Rationale | MoSCoW | REQ |
|---|---|---|---|
| USD-leg netting across the whole book | A G10 book's real exposure is to USD, not to nine independent pairs. | S | REQ-049 |
| Beta-weighted USD-basket gamma | Answers "what happens to my delta if the dollar moves 1%" in one number. | S | REQ-049 |
| Implied correlation from the vol triangle + `corr_vega` for crosses | Cross positions carry a correlation risk that no per-pair Greek shows. | C | REQ-014, REQ-050 |
| Concentration by strike/expiry with an HHI | Catches the "all my gamma is one expiry" failure before the expiry does. | S | REQ-048 |

### 4.9 Trust and operational

| Feature | Rationale | MoSCoW | REQ |
|---|---|---|---|
| Unexplained-P&L policing with the top-3 offending positions | An attribution nobody checks is decoration. | M | REQ-052 |
| Snapshot history so P&L is rebuilt from stored marks, not re-derived | Otherwise yesterday's P&L changes overnight and the trader stops trusting it. | M | REQ-054 |
| Soft delete of positions | Deleting a position must not rewrite last week's P&L. | M | REQ-031 |
| What-if ticket showing the Greek delta before commit | The pre-trade question, answered on the same screen as the trade. | S | REQ-035 |
| Look-ahead detection test in the backtester | The most common and most expensive backtest bug. | M | REQ-060 |
| Export of any table to CSV | Every trader eventually wants it in Excel. | C | §5.7 |

---

## 5. Non-functional requirements

### 5.1 Latency budgets (95th percentile, on the reference machine of §6.2)

| Interaction | Budget | Notes |
|---|---|---|
| Page navigation → skeleton painted | 300 ms | figures may stream in after |
| Snapshot refresh (synthetic provider) | 1.0 s | live provider: 5 s, with a spinner and per-source timing |
| `price_book`, 200 positions | 500 ms | REQ-032 |
| `spot_ladder`, 101 points × 200 positions | 800 ms | vectorised; `VolSurface.vol` is called ~1e5 times (arch §4) |
| `scenario_grid`, 21 × 13 nodes | 2.5 s | show a progress indicator beyond 1 s |
| `time_decay` to 90 days | 1.5 s | |
| Surface build/calibrate, one pair, full tenor grid | 400 ms | |
| Greek card recompute after a ticket edit | 250 ms | this is the J4/J5 loop; it must feel instant |
| Backtest, 1 pair × 5y daily, one hedge rule | 15 s | progress bar past 3 s, cancellable |
| Hedge-frequency sweep, 8 bands × 5 intervals | 90 s | run in the background, results streamed |

Anything exceeding its budget must render a partial result plus a "slow" indicator; nothing may
block the browser thread.

### 5.2 Data staleness

| Age of the field's `asof` | Badge | Behaviour |
|---|---|---|
| ≤ 15 min | green "live" | normal |
| 15–60 min | amber "delayed" | tooltip states the age |
| > 60 min, same session date | red "stale" | risk numbers still render; the page header carries the warning |
| previous business day or older | red "EOD" | the mark is explicitly labelled as an end-of-day mark |
| any synthetic field | purple "synthetic" | page-level banner (REQ-002) |
| any override | blue "override" | listed on the overrides panel (REQ-068) |

Free public sources are typically 15-minute delayed; the badge must reflect the *source's* stated
latency, not the time of the HTTP call.

### 5.3 Offline / blocked-network behaviour

- The app must start, render all 8 pages and run a backtest with **no** network, using the cache or
  the synthetic provider (charter §3).
- A failed fetch never raises to the UI: it degrades to cache → synthetic, with the degradation
  announced in the source board and badged in place.
- Fetch timeouts: 5 s connect, 10 s read, 1 retry with backoff, then degrade.
- No feature may be *only* reachable with live data; every screen has a synthetic path.

### 5.4 Precision and rounding for display (never for computation)

| Quantity | Non-JPY pairs | JPY pairs | Notes |
|---|---|---|---|
| Spot / strike | 5 dp (1.08503) | 3 dp (147.253) | i.e. pip/10 resolution; `pip` from `PAIRS` |
| Move / distance | pips, 1 dp, and % to 2 dp | same, pip = 0.01 | both shown, never one alone |
| Implied vol, RR, BF | % with 2 dp (7.05%) | same | stored as decimals (arch §2); the `×100` happens only at render |
| Vol spread / z-score | 2 dp vol points / 2 dp | same | unit "vol pts" always printed |
| Notional, `delta_base`, `Γ₁` | mm with 2 dp + ccy ("EUR 12.35mm") | same | absolute figures available on hover |
| PV, θ, ν, premium | 0 dp with thousands separators + ccy | same | ν labelled "per 1.00 vol pt", θ "per calendar day" |
| Premium | total in `premium_ccy`, **and** pips, **and** % of base notional | same | all three, always |
| Probabilities / shares | % with 1 dp | same | |
| P&L | 0 dp in the reporting ccy | same | per-pair sub-totals in quote ccy |

Rules: half-up rounding at render only; no scientific notation in any cell; every number carries a
unit or a currency in the column header; a value that is exactly zero renders `0`, a value that is
unavailable renders `—` and never `0`.

### 5.5 Timezone

- Everything is computed in **UTC**. `conventions.expiry_datetime` is the only source of expiry
  instants.
- Display timezone is a user setting (default `Europe/London`); every timestamp in the UI is
  rendered in it, with the abbreviation shown.
- Cut instants are shown **twice**: user timezone and the cut's own timezone.
- The trading-day boundary for daily P&L defaults to **17:00 America/New_York** and is a setting;
  the boundary in force is printed on the P&L page.
- DST is handled by `zoneinfo` only; no fixed UTC offsets anywhere.

### 5.6 Persistence

- SQLite at `data/fxgamma.db` (`fxgamma.store`, dev-owned). Tables required by this spec: positions
  (with `deleted_at`), hedge log, settings/preferences, mark-vol overrides, user levels, saved
  backtest presets, daily snapshots (market + book marks) for P&L history, import audit log.
- Every write is a transaction; the book is never left half-imported.
- Schema version stamped in the file; a mismatched version triggers a documented migration or a
  clear refusal, never a silent partial read.
- Nothing sensitive is persisted; API keys read from env only (charter §6).

### 5.7 Error states and general UI

- Three error classes, three treatments: **data unavailable** → `—` plus a badge; **model failure**
  (calibration, root-find) → panel-level message naming the pair/tenor and the failing routine;
  **user error** → inline, field-level, with the rule id from §3.4.
- No traceback ever reaches the browser; every caught exception is logged with the snapshot id.
- Every table has a CSV export button.
- Dark theme is the default and all charts must be legible in it (arch §6); light theme must not
  break any colour scale.
- Accessibility floor: no information encoded by colour alone (sign is also stated in text or by a
  mark); minimum 12 px type; diverging scales are colour-blind safe.
- Charts: zero line always drawn where sign matters; axis titles always carry units; no dual y-axis
  without both axis titles labelled with their series.

---

## 6. Testability

**6.1** Every latency budget in §5.1 is asserted by a QA benchmark on the demo book (200 synthetic
positions), not merely observed.

**6.2** Reference machine: 4 vCPU, 8 GB RAM, no GPU, Python 3.11+, warm cache.

**6.3** Numerical acceptance (charter §6): all Greeks agree with finite-difference bumps to < 1e-4
relative. Additional checks this spec introduces:
- BE identity: for an ATM-only book, `BE_daily` = `σ_ATM/√365` to < 1e-6 relative (§0).
- Attribution closure: `Σ components = total` to < 1e-8 relative on a synthetic two-snapshot fixture.
- Backtest decomposition (REQ-064) closes to < 1e-6 relative.
- Put–call parity and the vol-triangle identity hold on the synthetic surfaces.

**6.4** ε for UI reconciliation checks = 1e-6 relative or 0.01 in the reporting currency, whichever
is larger.

**6.5** Each REQ maps to at least one test id in `docs/05_test_report.md`; a REQ with no test is not
Done.

---

## 7. Non-goals for v1 (explicit)

| # | Not building | Why / when |
|---|---|---|
| NG-1 | Order entry, broker or EMS connectivity | charter §2; this is an analysis tool, not an OMS |
| NG-2 | Exotics beyond a single barrier (touches, KIKOs, baskets, quantos) | v2 |
| NG-3 | Tick data, intraday bars below daily, or a live streaming spot feed | no free source that survives the charter's constraints |
| NG-4 | Multi-user auth, roles, or a hosted deployment | local single-user app |
| NG-5 | Credit gamma (CDX/iTraxx via HYG/LQD/JNK) | charter §7, deferred to v2 |
| NG-6 | Full term-structure interest-rate curves | flat cc rate per ccy in v1 (`MarketSnapshot.rates`) |
| NG-7 | Stochastic-vol P&L attribution beyond vanna/volga (no Heston/rough-vol) | model risk exceeds the benefit at this stage |
| NG-8 | Automatic hedge execution or any "auto-trade" mode | the app suggests; the human trades |
| NG-9 | Real-time push updates / websockets | manual and timed refresh only |
| NG-10 | Mobile layout | desktop-first; must merely not crash below 1280 px |
| NG-11 | Options on crosses priced off a true cross surface | v1 prices crosses off their own quoted surface where available, else flags as unsupported |
| NG-12 | Machine-learned signals | the Lab is rule-based only in v1 |

## 8. Risk register

### 8.1 Data risks

| ID | Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|---|
| R-1 | Sandbox blocks all market-data hosts (charter §3) | Nothing can be validated against live data here | Certain | Synthetic provider + recorded fixtures; `verify_live_sources.py` for the user's machine; every screen tested on both paths |
| R-2 | Free spot sources are 15-min delayed or EOD | Intraday hedge decisions run on stale spot | High | Staleness badges (§5.2); manual spot override (REQ-068); the hedge panel states the spot it used |
| R-3 | No free OTC FX vol surface exists | The whole surface layer rests on proxies | Certain | ETF chains + CBOE vol indices + CME settlements; basis disclosed (R-6); manual quote entry always available |
| R-4 | CME public settlement files change format or URL | Gamma map goes dark | Medium | Adapter isolated behind `SmileQuotes`/`build_surface` (arch §8); cache; graceful degradation to "market gamma unavailable" |
| R-5 | ETF option chains are thin at the wings | Wing vols and therefore BF/vanna/volga are unreliable | High | Minimum-liquidity filter (OI/volume/spread), wing points dropped rather than fitted, calibration residual gate (REQ-016) |

### 8.2 Model and interpretation risks

| ID | Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|---|
| R-6 | **ETF-implied vol ≠ OTC pair vol** (fees, borrow, dividends-in-kind, creation mechanics, US-listed hours, American exercise on the ETF options) | Richness signals biased; the level may be systematically off by tenths of a vol point or more | High | Show the ETF/OTC basis as its own series where any OTC reference exists; label every ETF-derived vol; use it for **relative** richness and z-scores, never as an absolute mark; never quote a hedge ratio off an ETF vol without the badge |
| R-7 | Vanna–Volga is an approximation, not a model; it misbehaves in the far wings and at very short tenors | Wing Greeks wrong exactly where pin risk lives | High | Restrict VV to the 10d–90d strike band, fall back to SABR or interpolation outside, show residuals, hard-flag extrapolation |
| R-8 | Premium-adjusted delta conventions (`spot_pa`, `fwd_pa` — USDJPY, USDCHF, USDCAD, USDSEK, USDNOK per `conventions.PAIRS`) are easy to get wrong | Delta hedges systematically wrong size on half the G10 book | Medium | Convention named on every delta readout; unit tests per convention against published examples; strike↔delta round-trip test per pair |
| R-9 | Flat interest-rate curves | Forwards and rho wrong for longer tenors; carry misattributed | Certain in v1 | Restrict headline analytics to ≤ 1Y; show the flat-rate assumption on the P&L rates/carry bars; NG-6 |
| R-10 | Realized-vol estimator choice materially changes the richness signal | Trader acts on an estimator artefact | Medium | Always show ≥ 3 estimators (REQ-008), never a single "the" RV; matched annualisation basis printed |
| R-11 | Attribution residual absorbs model error and looks like P&L | False confidence in the mark | Medium | Residual policing (REQ-052) with a hard 5% threshold and named offenders |
| R-12 | Backtest overfitting via the hedge-band sweep | Optimal band is in-sample noise | High | Label the argmax as in-sample; require an out-of-sample split before any band is recommended to the live Risk page; report sample sizes per bucket (REQ-065) |
| R-13 | Synthetic data looks realistic enough to be mistaken for real | Conclusions drawn from a random-number generator | Medium | Purple badge + page banner + confirmation on switching to synthetic (REQ-002, REQ-067) |
| R-14 | Users read "dealer gamma" from open interest as fact | Wrong positioning conclusions | High | Assumption printed on the figure (REQ-023); the word "estimated" is mandatory in the label |
| R-15 | Reporting-currency conversion of Greeks is done inconsistently between pages | Aggregate numbers that do not tie out | Medium | One conversion helper, one place; conversion rate disclosed on hover; a QA test that per-pair sub-totals sum to the aggregate |

### 8.3 Contract gaps for PM arbitration

| Gap | Where it bites | Requested resolution |
|---|---|---|
| **CG-1** `Greeks` carries no currency and `book_greeks(book, mkt) -> Greeks` returns a single untagged object; a EURUSD+USDJPY book mixes USD and JPY theta | REQ-004, REQ-038, REQ-051 | Either add a `ccy` field to `Greeks`, or define `book_greeks` as returning reporting-ccy-converted values with the rate exposed; otherwise the app layer must own conversion and the aggregate cards cannot be unit-tested against the library |
| **CG-2** `OptionPosition` is frozen with no `mark_vol` | REQ-036, REQ-058 | Accept a `store.py` side-table keyed on position `id` (no contract change), or add the field |
| **CG-3** `hedge_bands(book, mkt, pair, *, gamma_budget)` takes no cost or rule input | REQ-043 | Add optional `rule: HedgeRule | None = None`; cost-aware bands are otherwise impossible without duplicating the maths in `app/` |
| **CG-4** `time_decay(book, mkt, days=range(0,31))` has no calendar/weighting argument | REQ-046, §4.6 | Add optional `weights: Sequence[float] | None` or `calendar=` so weekend/event-weighted decay is computed once, in the library |
| **CG-5** `SpotPosition.trade_date` is a `date`, so intraday hedges cannot be ordered or timestamped | REQ-055, hedge log, J4/J7 | Add a `trade_time`/`datetime` field, or accept a hedge-log side-table in `store.py` carrying the timestamp and linking to the position `id` |
| **CG-6** No event-calendar type or module owner exists in the architecture | REQ-012, REQ-020, REQ-046 | PM to assign: a versioned CSV in `data/` owned by `data`, plus a small `conventions`-level loader; BA has specified the schema and weights in §4.6 |
| **CG-7** `MarketSnapshot.meta` keys are illustrated only as `"spot.EURUSD"` | REQ-002 | Freeze the key grammar (`spot.<PAIR>`, `rate.<CCY>`, `surface.<PAIR>`, `surface.<PAIR>.<TENOR>`, `oi.<PAIR>`) so the badge component can look provenance up generically |

None of CG-1…CG-7 blocks starting M2; CG-1 and CG-5 do block REQ-038/REQ-051/REQ-055 reaching Done.

---

## 9. Open questions for the trader reviewer

| # | Question | Why it changes the build |
|---|---|---|
| Q-1 | What is your **trading-day boundary** for daily P&L — 17:00 NY, London close, or your own mark time? And do you mark once a day or twice? | Sets the snapshot cadence, the P&L page, and how many snapshots we store per day |
| Q-2 | Do you hedge on a **fixed delta band**, a **fixed time**, a **gamma budget**, or discretionarily by level? Give a typical band in mm or in % of notional for a 10mm 1M straddle. | Chooses the default `HedgeRule` and what the Risk page shows first |
| Q-3 | Which **vol mark** do you consider truth when the ETF-implied and your own OTC mark disagree — do you want ETF vols used for the *level* at all, or only for relative richness and z-scores? | Determines whether R-6 is a badge or a hard restriction on the whole surface layer |
| Q-4 | For **USDJPY, USDCHF, USDCAD** the conventions file uses premium-adjusted spot delta. Is that your desk convention, and do you want the delta shown in the pair convention only, or also in plain spot delta alongside? | Affects every delta readout and hedge size on half the G10 book |
| Q-5 | In **which currency** do you want the book totals — USD always, or per-pair quote ccy with no aggregate? | Decides CG-1 and the whole reporting-ccy layer |
| Q-6 | Do you want **weekend/event-weighted time** to drive the *default* theta and decay numbers, or should calendar time stay the default with business time as a toggle? | Changes the headline theta number the trader sees every morning |
| Q-7 | How far out do you actually trade — is ≤ 3M enough for v1, or do you need 6M–1Y priced properly (which makes the flat-rate assumption R-9 material)? | Sets the tenor grid, and whether we must build real rate curves in v1 |
| Q-8 | Is **CME open interest** useful to you as a market-gamma proxy, or is it too small a share of the OTC market to inform your positioning read? | Decides whether page 3 is a headline page or a curiosity |
| Q-9 | For **pin risk**, what horizon do you care about — the last 3 business days, the last day, or only the morning of the cut? And which cut(s) do you actually trade besides NY10? | Sets the pin panel trigger window and which cuts get a clock |
| Q-10 | What would make you **stop trusting** a number on this screen — what is the one check you'd run first? | Tells us which reconciliation to put on the screen rather than in a test |
