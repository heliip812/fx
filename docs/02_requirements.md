# Requirements — FX Gamma Desk (G3 + G10)

**Owner:** Business Analyst (`ba`) · **Status:** M6a — corrected after trader review (rev 2,
2026-09-07; see §11 changelog) · **Reads against:**
`docs/00_charter.md`, `docs/01_architecture.md` (FROZEN) **+ AMENDMENT v1.1 (CG rulings) + AMENDMENT
v1.2 (trader-review rulings, incl. T-5 assigned to the BA)**, `docs/06_trader_review.md`,
`fxgamma/types.py`, `fxgamma/conventions.py`.

This document is the build contract for behaviour. Where it names a function it means the one in
architecture §4–§5. Where it needs something the frozen contract does not provide, it is marked
**[CONTRACT-GAP-n]** and listed in §8.3 for PM arbitration. Nothing here changes the contract.
All seven gaps CG-1…CG-7 were ruled on in arch AMENDMENT v1.1; §8.3 now records the ruling per gap
and is **closed**. §10 records the trader's answers to §9 and which of them are still open.

**Notation used throughout**

| Symbol | Meaning | Unit |
|---|---|---|
| `S` | spot, quote ccy per 1 base ccy | e.g. 1.0850, 147.20 |
| `Γ₁` | `Greeks.gamma_1pct` — change in `delta_base` for a **+1% spot move** | base ccy |
| `Γ$` | `Γ₁ × S` — quote-ccy value of that delta change, **per 1% move** | quote ccy per 1% |
| `Γ_S²` | `Γ·S² = 100·Γ$` — **cash gamma**; the coefficient in every delta-hedged P&L identity | quote ccy |
| `θ` | `Greeks.theta` — quote ccy per **calendar** day | quote ccy/day |
| `ν` | `Greeks.vega` — quote ccy per **1.00 vol point** (0.01 of σ) | quote ccy/vol pt |
| `σ` | implied vol, decimal (0.085 = 8.5%) | — |
| `x` | spot move in **percent** | % |
| BE | daily breakeven move (§4.1) | % and pips |

**Derived identities used by several screens (developers: implement once, in one helper module —
`fxgamma/portfolio/risk.py`; every screen calls it, no screen re-derives it).**
Gamma P&L over a move of `x` percent is `0.5·Γ·dS²` with `Γ = Γ₁/(0.01·S)` and `dS = 0.01·x·S`:

```
gamma_pnl(x%)      = 0.005 · Γ₁ · S · x²                       [quote ccy]
BE_daily (%)       = sqrt( |θ| / (0.005 · Γ₁ · S) )            [%]
sigma_day_move (%) = 100 · σ_ATM / sqrt(252)                   [%]
dhedge_pnl         = 50 · Γ₁ · S · (σ_r² − σ_i²) · Δt_years    [quote ccy]
                   = 0.5 · Γ_S² · (σ_r² − σ_i²) · Δt_years
```

For a pure ATM position `BE_daily` reduces exactly to `σ_ATM / sqrt(365)`. QA uses that as a check.

**The 1% scaling trap (this document got it wrong once — see §11).** `Γ₁` and therefore `Γ$` are
defined **per 1% spot move**, not per unit of spot. Any identity written in terms of `Γ·S²` must
therefore carry a factor of **100** when re-expressed in `Γ$`: `Γ·S² = 100·Γ$ = 100·Γ₁·S`. The
delta-hedged carry constant is consequently **50**, never 0.5. Derivation and a worked, unit-testable
example are in **§2.5 → "Normative derivation for REQ-046 / REQ-064"**. Nothing in the document may
restate this identity locally; REQ-046, REQ-056 and REQ-064 all call the same helper.

**Two sigma-day bases, never conflated** (trader review §3a/W-7, accepted): `BE_daily` is an
*economics* quantity and annualises on **√365**, because theta is paid on calendar days;
`sigma_day_move` is a *distance/probability* quantity and annualises on **√252**, because spot only
moves on trading days. Every "distance in sigma-days" on any screen (REQ-025, REQ-042, REQ-045)
divides by `sigma_day_move`; every breakeven divides by `BE_daily`; the basis is printed on the
panel. Using √365 for distance overstates every distance-in-sigma-days by ~20%.

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
from `MarketSnapshot.meta` under the **frozen key grammar of [CG-7 RESOLVED — arch AMENDMENT v1.1]**
(`spot.<PAIR>`, `rate.<CCY>`, `fwd.<PAIR>.<TENOR>`, `surface.<PAIR>`, `surface.<PAIR>.<TENOR>`,
`oi.<PAIR>`, `events`), looked up **most-specific-first** so a badge for `surface.EURUSD.1M` falls
back to `surface.EURUSD`; `synthetic` renders purple, `user_override` blue, `cached` amber,
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
disclosed on hover. **[CG-1 RESOLVED — arch AMENDMENT v1.1]**: the conversion is a **library**
layer, not an app one, and `Greeks` gains no `ccy` field (so `+`/`*` stay safe). Per-position Greeks
stay in the position's native quote ccy; `price_book` carries `ccy`, `fx_to_report` and `*_rep`
columns; `book_greeks(book, mkt, report_ccy="USD")` returns already-converted Greeks; aggregating
across pairs in native ccy is forbidden; `risk.fx_rate(ccy, report_ccy, mkt)` routes through USD and
**raises** on a missing leg rather than defaulting to 1.0. Default `report_ccy` is **USD**
(§10/Q-5, market standard, settled), with per-pair sub-totals in the pair's quote ccy underneath.

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
convention is stated on the panel ("positive = implied over realized = gamma expensive"). This is a
**relative** screen, so an `INDICATIVE` (ETF/CBOE) surface is a legitimate input here per REQ-068(d)
— but the panel names the surface status it used, and where a `MARK` exists for the pair the
richness is computed off the mark and the ETF basis (REQ-068(e)) is shown next to it.

**REQ-010 (M) — Cross-pair richness table with ranking.**
G: all pairs priced. W: the page loads.
T: one row per pair with ATM 1M, RV21, spread, spread z, RR25 z, BF25 z, and a rank column; the
table can be sorted and the top/bottom 3 by spread-z are highlighted; pairs with stale or missing
inputs are greyed with the reason; a **surface-status column** shows `MARK` or `INDICATIVE` per pair
(REQ-068(c)), because a richness ranking that mixes marked and unmarked pairs is comparing two
different things and must say so.

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
G: a provider that emits `list[SmileQuotes]` per pair — **the manual grid of REQ-068 first, then
live, cache, synthetic** (arch AMENDMENT v1.2). W: the user selects pair and method
∈ {vanna_volga, sabr, interp}. T: `build_surface` is called and the 3-D surface (strike × tenor ×
vol), the smile-by-tenor slice and the ATM term structure render; changing the method re-renders
without a page reload and shows the method name **and the surface status (`MARK` / `INDICATIVE`,
REQ-068(c)) with the quote source per tenor** on the figure.

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
the pair's quote convention, with spot drawn as a vertical line. **Constraints settled in §10/Q-8
(market standard, not taste), all mandatory and all displayed:** OI is shown **unsigned** — it
carries no side tag, so no signed "dealer gamma" is printed; the **contract multiplier** is applied
and stated (6E = EUR 125,000, 6B = GBP 62,500, **6J = JPY 12,500,000**, 6A/6C/6N = 100,000,
6S = CHF 125,000) because OI in contracts is meaningless until multiplied; these are options **on
futures**, so the futures→spot conversion is stated rather than assumed away; and inverting a quote
is **not** a rescale — for a product quoted USD per JPY with `X = 1/F`,
`d²V/dX² = F⁴·V_FF + 2F³·V_F`, i.e. the gamma conversion carries a **delta term**, and any adapter
that scales gamma on inversion is wrong in a way nobody notices. The page is titled
**"Listed positioning (indicative)"**. *MoSCoW unchanged in this pass: the trader's demotion of
REQ-022/023/024 to Should is a priority ruling for the PM, recorded in §10/Q-8.*

**REQ-023 (M) — Dealer-gamma sign assumption is explicit.**
G: any market-gamma figure. W: the page loads.
T: **no signed dealer gamma is presented at all.** The previous acceptance criterion — printing the
assumption "customers buy calls/puts as tagged" — is withdrawn: that is not an assumption but a
placeholder, because open interest carries no side tag whatever (every contract has a buyer and a
seller), and a signed chart with a caption underneath is read as the chart, not the caption
(§10/Q-8, R-14). Instead the figure shows unsigned OI as a **strike-concentration and notional
map**, states the multiplier and the futures→spot conversion used, and carries the words
"open interest — not a dealer position" in the title, not in a footnote.

**REQ-024 (M) — Expiry ladder.**
G: CME/OI data. W: the page loads.
T: a ladder shows total OI notional by expiry date for the next 8 expiries, with each expiry's cut
time and the number of business days to it.

**REQ-025 (S) — Strike magnets / pin candidates.**
G: OI by strike. W: the page loads.
T: strikes holding > 5% of front-expiry OI within ±2% of spot are labelled as magnets, with
distance in pips and %, and in **sigma-days** on the distance basis of §0
(`distance% / sigma_day_move` with `sigma_day_move = 100·σ_ATM/√252` — **not** √365, which is the
economics basis and overstates every distance by ~20%); the basis is printed on the panel.

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
(`PV − premium_paid`); the **vol used carries its surface status** (`MARK` / `INDICATIVE` /
`user_override`, REQ-068(c)) in the row, so no PV is ever read without knowing whose vol produced
it; the whole table recomputes in < 500 ms (§6.1).

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
G: a position already priced off the REQ-068 curve. W: the user sets a manual mark vol on that
single position.
T: the position prices off that vol, the row is badged `user_override`, and the P&L page shows
"mark-to-this-position's-vol vs mark-to-the-curve" as a separate line.
**[CG-2 RESOLVED — arch AMENDMENT v1.1]**: `OptionPosition` stays frozen; the override lives in a
`store.py` `position_marks` side-table keyed on position `id` (`mark_vol`, `mark_source`, `asof`).
*Priority note:* the trader review §3b asks to promote this to **M** on the grounds that "the
trader's own mark is the book". That argument is now discharged by REQ-068 being a Must — the book
is marked to the user's curve by default, and REQ-036 is the **per-position exception** on top of
it, which is genuinely a Should. Recorded in §10 for the PM.

**REQ-037 (S) — Book grouping and filters.**
G: a book with tags. W: the user filters by pair / tag / expiry bucket / structure.
T: the blotter, the Greek cards and page 5 all respect the filter, and the active filter is stated
in the page header so no screen ever silently shows a subset.

### 2.5 Page 5 — Risk (`/risk`)

**REQ-038 (M) — Aggregate Greek cards.**
G: a non-empty book. W: the page loads.
T: cards show PV, `delta_base` (per pair, in base ccy mm), `Γ₁` (base ccy mm per 1%), `ν` (per vol
pt), `θ` (per calendar day), vanna, volga, and dual delta, each with its unit written on the card;
per-pair values are in that pair's quote ccy, and the totals row is in the reporting currency
(default USD) with the conversion disclosed. The totals row comes from
`book_greeks(book, mkt, report_ccy=...)` per **[CG-1 RESOLVED]** — the page never sums native-ccy
Greeks itself. **Acceptance (settled, §10/Q-5):** QA asserts that the per-pair sub-totals converted
at the disclosed rate sum to the aggregate to §6.4 ε, on every monetary Greek — this is R-15
promoted from a risk-register line to a test.
*Open, not actioned in this pass:* trader review W-3 (aggregate `dual_delta` is meaningless and
`types.py` now returns `nan` for it, arch v1.2/T-2 — so the card must render "n/a" and dual delta
should be dropped from the aggregate card set), W-10 (single-number vega should be tenor-bucketed;
the delta card must name its sticky convention) and MISS-10 (delta shown in base mm, USD equivalent
and P&L-per-pip). All three are W-item corrections outside the T-5 assignment; recorded in §10.

**REQ-039 (M) — Daily breakeven card.**
G: `Γ₁`, `θ`, `S`. W: the page loads.
T: a card shows the daily breakeven move per pair in % and in pips using the §0 identity, the
number of realized-vol-implied sigma this represents (`BE% ÷ (100σ_r/√365)`, economics basis), and
a **dimensionless gamma/theta coverage ratio**
`coverage = gamma_pnl(x_realized%) / |θ_to_next_mark|`, where `x_realized` is the last completed
mark-to-mark spot move; `coverage = 1.0` exactly at breakeven and `= (σ_r/σ_i)²` for an ATM book.
The superseded ratio `Γ$/|θ|` is **withdrawn**: `Γ$` is quote ccy per 1% and `θ` is quote ccy per
day, so the quotient has units of days-per-percent and cannot be compared across pairs, which was
its stated purpose (§4.1). For an ATM-only test book `BE_daily` equals `σ_ATM/√365` to within ε.

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
**[CG-3 APPROVED — arch AMENDMENT v1.1]**:
`hedge_bands(book, mkt, pair, *, gamma_budget=None, rule: HedgeRule | None = None)`; the cost-aware
band maths lives in the library, never in `app/`.
**Defaults (settled, §10/Q-2):** `mode="band"`, band expressed as **% of the pair's gross option
notional, default 15%**, with a floor at the pair's minimum sensible clip (EUR 1mm / USD 1mm or
equivalent), and **per-pair** transaction costs rather than one global `cost_bp` (0.2bp is right for
EURUSD, ~2× too tight for AUD/NZD/CAD and 10–25× too tight for USDSEK/USDNOK). The trader's band in
mm and pips per pair remains **[AWAITING USER]** (§10/Q-2). The `types.HedgeRule` defaults
themselves are a contract matter (trader CR-1) and are not changed by this document.
**Gate (REQ-068(c)):** no hedge instruction is issued off an `INDICATIVE` surface; the panel greys
with the reason and names the acknowledgement that would re-enable it.

**REQ-044 (M) — Expiry ladder and cut clock.**
G: options with mixed expiries and cuts. W: the page loads.
T: a table lists each expiry with cut, the exact cut instant in the user's timezone and in the cut's
own timezone, a live countdown, notional and `Γ₁` expiring, and θ released after that expiry;
`conventions.expiry_datetime` is the single source of the instant.

**REQ-045 (M) — Pin risk panel. Three deltas per strike, never one number.**
G: options with ≤ 3 business days to the cut (the panel arms at T-3bd; §10/Q-9). W: the page loads.
T: `pin_risk(book, mkt, pair)` returns one row per **(pair, strike, cut)** carrying, in this order:
distance in pips and %, distance in **sigma-days** (`sigma_day_move`, √252 basis, §0), notional at
that strike, `Γ₁` at the cut, then the **three separately labelled delta numbers**
`delta_if_above`, `delta_if_below` and `delta_now`, then the two derived numbers `jump_at_strike`
and the pair `hedge_if_above` / `hedge_if_below`, all defined normatively below; then
P(finish within ±k pips of the strike) for k ∈ {10, 25, 50} (JPY-scaled). Strikes within 0.5
sigma-days of spot are highlighted red. **No column, tooltip, label or return field may be called
"the delta discontinuity"**, and no single number may stand in for the three: the panel fails
review if it prints one delta at a strike. Escalation (§10/Q-9): the panel promotes itself to a
page-level banner inside 24h of the cut and to a header line inside 2h; the header carries a live
countdown to the next NY10 and the next TKY15 whenever the book holds a position at that cut.

**Normative derivation for REQ-045 — pin-risk deltas** (binding on `fxgamma/portfolio/zones.py`,
which is being implemented concurrently; if the code and this block disagree, this block wins and
the difference is a defect).

*Setup.* Fix one strike `K` at one cut. Let `O(K)` be the options at that strike and cut. Option
`i` has direction `dᵢ ∈ {+1,−1}` (long/short), `cpᵢ ∈ {+1,−1}` (call/put on the **base** ccy) and
base notional `Nᵢ > 0`. All deltas below are in **base ccy**, signed **`+` = long base** (the
`Greeks.delta_base` convention of arch §3: the base-ccy amount you would sell to be flat).

*Step 1 — what you inherit.* A European option is exercised iff it is ITM at the cut: a call iff
`S_T > K`, a put iff `S_T < K`. Exercising a **bought call** delivers `+N` of base (you buy the
base); exercising a **bought put** delivers `−N` (you sell it). So the spot position inherited from
option `i` is a step function of the fixing:

```
inherit_i(S_T) = dᵢ · cpᵢ · Nᵢ · 1{i is ITM at S_T}
```

Above `K` only the calls are ITM; below `K` only the puts are. Therefore

```
delta_if_above(K) =  Σ over calls at K   of  dᵢ·Nᵢ        (puts contribute exactly 0)
delta_if_below(K) = −Σ over puts  at K   of  dᵢ·Nᵢ        (calls contribute exactly 0)
```

*Step 2 — the discontinuity is the difference of those two, and the `cp` cancels.*

```
jump_at_strike(K) = delta_if_above(K) − delta_if_below(K)
                  = Σ over ALL options at K of dᵢ·Nᵢ      <- independent of cpᵢ
```

Crossing `K` upward, a long call goes `0 → +N` (gain `+N`) and a long put goes `−N → 0` (also gain
`+N`). Both are `+dᵢ·Nᵢ`. **This is why the correct discontinuity carries no `cp` factor.** The
superseded text used `Σ direction·cp·notional_base` — which is the *inherited-if-ITM* delta of
Step 1, a different quantity, and which returns the **wrong sign for every put** when read as a
discontinuity.

*Step 3 — the delta you have right now.* Let `δᵢ(S,T,σ)` be the **plain spot delta per unit of base
notional**, sign included: `e^{−r_f T}·N(d₁)` for a call, `−e^{−r_f T}·N(−d₁)` for a put. Then

```
delta_now(K) = Σ over options at K of dᵢ·Nᵢ·δᵢ
```

As `T → 0`, `δᵢ → cpᵢ·1{ITM}` away from the strike and `→ ±0.5` exactly at it, so `delta_now` is
always bracketed by `delta_if_below ≤ delta_now ≤ delta_if_above` (for a book long at that strike).
`delta_now` is what you are **hedged to**; it is not what you inherit.

*Step 4 — the two numbers that are actually actionable at 09:55 NY.*

```
hedge_if_above(K) = delta_if_above(K) − delta_now(K)      (+ ⇒ you must SELL that much base)
hedge_if_below(K) = delta_if_below(K) − delta_now(K)      (− ⇒ you must BUY  that much base)
hedge_if_above − hedge_if_below = jump_at_strike          (invariant; QA asserts it)
```

*Convention, binding.* All five quantities use the **plain (unadjusted) spot delta**, never the
premium-adjusted one, even on `spot_pa` pairs. Premium adjustment nets the option's *current
premium* out of the quoted delta; it is a quoting convention, whereas the inherited spot position is
a delivery fact and carries no premium term (and at the cut the premium has collapsed to intrinsic
anyway). The pa delta may be displayed alongside, explicitly labelled per REQ-017, but must not
enter `delta_now`, the step or the hedge numbers.

**Worked example A — long call (EURUSD).** One long EURUSD call, `K = 1.0900`, NY10 cut,
`N = EUR 10mm`, `d = +1`, `cp = +1`. Spot `S = 1.0895` (5 pips **below** the strike), 1 hour to the
cut so `T = 1/8760 = 1.14155e-4` yr, `σ = 7.00%`, `r_d = r_f = 0` (over one hour the discount
factors differ from 1 by < 1e-5 and are immaterial to the illustration).

| Quantity | Working | Value |
|---|---|---|
| `σ√T` | `0.0700 × 0.0106843` | `7.479e-4` |
| `d₁` | `ln(1.0895/1.0900)/σ√T + 0.5·σ√T = −4.5882e-4/7.479e-4 + 3.74e-4` | `−0.6131` |
| `δ` (call) | `N(−0.6131)` | `0.2699` |
| `delta_now` | `+1 × 10mm × 0.2699` | **EUR +2.70mm** |
| `delta_if_above` | `Σ calls d·N = +1 × 10mm` | **EUR +10.00mm** |
| `delta_if_below` | no puts at this strike | **EUR 0.00mm** |
| `jump_at_strike` | `10.00 − 0.00` (= `Σ d·N` = `+1×10mm`) | **EUR +10.00mm** |
| `hedge_if_above` | `10.00 − 2.70` | **SELL EUR 7.30mm** |
| `hedge_if_below` | `0.00 − 2.70` | **BUY EUR 2.70mm** |

**Worked example B — long put (USDJPY). This is the case the superseded formula got backwards.**
One long **USD put** on USDJPY, `K = 147.00`, NY10 cut, `N = USD 20mm` (base = USD, arch §2),
`d = +1`, `cp = −1`. Spot `S = 147.05` (5 pips **above** the strike, pip = 0.01), 1 hour to the cut,
`σ = 9.00%`, `r_d = r_f = 0`.

| Quantity | Working | Value |
|---|---|---|
| `σ√T` | `0.0900 × 0.0106843` | `9.616e-4` |
| `d₁` | `ln(147.05/147.00)/σ√T + 0.5·σ√T = 3.4008e-4/9.616e-4 + 4.81e-4` | `+0.3541` |
| `δ` (put) | `−N(−0.3541)` | `−0.3616` |
| `delta_now` | `+1 × 20mm × (−0.3616)` | **USD −7.23mm** (short USD) |
| `delta_if_above` | no calls at this strike | **USD 0.00mm** |
| `delta_if_below` | `−Σ puts d·N = −(+1 × 20mm)` | **USD −20.00mm** |
| `jump_at_strike` | `0.00 − (−20.00)` (= `Σ d·N` = `+1×20mm`) | **USD +20.00mm** |
| `hedge_if_above` | `0.00 − (−7.23)` | **BUY USD 7.23mm** |
| `hedge_if_below` | `−20.00 − (−7.23)` | **SELL USD 12.77mm** |

Note the sign: the jump is **`+20mm`, positive**, exactly as for the long call in example A — spot
crossing a strike upward always *gains* `Σ d·N` of delta. The superseded formula
`Σ direction·cp·notional_base` returns **`−20mm`** here, i.e. the correct magnitude with the wrong
sign, so a trader hedging off it would trade **USD 40mm the wrong way** into the cut. That is the
single most expensive error in the previous revision.

**QA fixtures (REQ-045).** (i) Single long put, `N`, spot walked across `K`: assert
`jump_at_strike = +N`, `delta_if_above = 0`, `delta_if_below = −N`, and `delta_now → −N` as
`S → K⁻`, `→ 0` as `S → K⁺`. (ii) Single short call, `d = −1`: assert `jump_at_strike = −N`.
(iii) A long straddle at one strike (`+1` call and `+1` put, same `N`): assert
`delta_if_above = +N`, `delta_if_below = −N`, `jump_at_strike = +2N`. (iv) Invariant
`hedge_if_above − hedge_if_below = jump_at_strike` on the demo book, to < 1e-9 relative.
(v) Both worked examples above reproduce to the printed precision.

**REQ-046 (M) — Decay path ("what if I do nothing").**
G: a book. W: the user opens the decay panel.
T: `time_decay(book, mkt, days=range(0,31), *, weights=None, calendar="calendar")`
(**[CG-4 APPROVED — arch AMENDMENT v1.1]**, calendar time is the default and business/event
weighting is opt-in and badged) renders PV and cumulative θ day by day to the last expiry with spot
and surface frozen; the path is drawn under three realized-vol assumptions (`σ_r = 0`,
`σ_r = σ_i`, `σ_r = RV21`) using the **delta-hedged carry identity below** —
`dhedge_pnl = 50·Γ₁·S·(σ_r² − σ_i²)·Δt_years`, **not** `0.5·Γ$·(…)·Δt`, which is out by a factor of
100; weekends and holidays use the decay weights of §4.6 and the weighting scheme in force is named
on the figure. The identity is computed by the single §0 helper and never restated in `app/`.

**Normative derivation for REQ-046 / REQ-064 — the delta-hedged carry identity.**

*Definitions (§0).* `Γ = ∂delta_base/∂S`; `Γ₁ = Γ·0.01·S` (base ccy, **per 1% move**);
`Γ$ = Γ₁·S` (quote ccy, **per 1% move**); `Γ_S² = Γ·S²` (quote ccy, cash gamma).

*The scaling that was missed.* From `Γ₁ = Γ·0.01·S`:

```
Γ = Γ₁ / (0.01·S)        ⇒        Γ·S² = Γ₁·S / 0.01 = 100·Γ₁·S = 100·Γ$
```

*The identity.* The P&L of a continuously delta-hedged option book over `Δt`, spot and surface
otherwise frozen, with spot realizing `σ_r` against a book marked at `σ_i`, is the standard
`0.5·Γ·S²·(σ_r² − σ_i²)·Δt`. Substituting `Γ·S² = 100·Γ$`:

```
dhedge_pnl = 0.5 · Γ_S² · (σ_r² − σ_i²) · Δt_years
           = 50  · Γ$   · (σ_r² − σ_i²) · Δt_years          <- the form to implement
           = 50  · Γ₁·S · (σ_r² − σ_i²) · Δt_years
```

The published constant was `0.5` against `Γ$`; the correct constant against `Γ$` is **50**. The
document's definition of `Γ$` is the one that stands (it is used by §0, REQ-039 and §4.1); only the
constant moves. **`Δt` is in YEARS** (this was also unstated): one calendar day is `1/365`, and
where two marks are not a whole day apart the elapsed-wall-clock convention of REQ-051 applies —
`Δt_years = (t₁ − t₀)/365 days`.

*Consistency with §0 (one helper, not two).* Splitting the identity into its two halves:

```
gamma P&L over Δt at realized σ_r   =  50·Γ$·σ_r²·Δt_years
gamma-theta bill over Δt at σ_i     =  50·Γ$·σ_i²·Δt_years   =  |θ_gamma| · (365·Δt_years)
```

Setting them equal and solving for the daily move reproduces `BE_daily = σ_i/√365` exactly, i.e.
the §0 breakeven identity and this identity are the same equation and must share one implementation.

**Worked example (developers: unit-test against these numbers).** EURUSD 1M ATM straddle, **long
EUR 10mm per leg**, `S = K = 1.0850`, `T = 30/365 = 0.0821918` yr, `r_d = r_f = 0`, `σ_i = 7.05%`.

| Quantity | Working | Value |
|---|---|---|
| `σ√T` | `0.0705 × 0.2866911` | `0.0202117` |
| `d₁` (ATMF) | `0.5·σ√T` | `0.0101059` |
| `n(d₁)` | standard normal pdf | `0.3989219` |
| `Γ` (book, 2 legs) | `2 × 10mm × n(d₁)/(S·σ√T) = 2 × 10mm × 18.1909` | `EUR 363.82mm per 1.0 of spot` |
| `Γ₁` | `Γ · 0.01 · S` | **`EUR 3.94743mm` per 1%** |
| `Γ$` | `Γ₁ · S` | **`USD 4.28296mm` per 1%** |
| `Γ_S²` | `Γ·S² = 100·Γ$` | **`USD 428.296mm`** |
| `θ_gamma` | `−0.5·Γ_S²·σ_i² / 365` | **`−USD 2,916` per calendar day** |
| `BE_daily` | `σ_i/√365 = 0.3690%` | **`40.0 pips`** |
| **1 day at `σ_r` = 9.00%** | `50 × 4.28296e6 × (0.09² − 0.0705²) × (1/365)` | **`+USD 1,836`** |
| same, superseded formula | `0.5 × 4.28296e6 × 0.00312975 × (1/365)` | `+USD 18.36` ← the 100× tell |

**QA (REQ-046).** (a) At `σ_r = σ_i` the delta-hedged path is identically zero and the PV path
equals the pure-theta path to ε — i.e. cumulative gamma P&L over `n` days equals `n·|θ_gamma|` to
< 1e-6 relative. (b) `gamma_pnl(BE_daily) = |θ| ` to < 1e-6 relative (this ties §0, REQ-039 and
REQ-046 to one helper). (c) The worked example above reproduces `Γ₁`, `Γ$`, `θ_gamma`, `BE_daily`
and the `+USD 1,836` to the printed precision. (d) A regression test asserts the constant is `50`
against `Γ$` and `0.5` against `Γ_S²`, so the factor-100 slip cannot return.

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
hedge and unexplained, summing exactly to `total` (to ε), in the reporting currency (converted per
**[CG-1 RESOLVED]**, default USD, per-pair sub-totals in quote ccy and QA-asserted to sum to the
aggregate), with the two snapshot timestamps and the trading-day boundary stated.
**Settled defaults (§10/Q-1, market standard):** the official FX day rolls at
**17:00 America/New_York** — the value-date roll, not London close and not midnight. The app stores
**two** snapshots a day: `EOD` (17:00 NY, official, immutable) and `AM` (a user-triggered morning
working mark). **Theta and carry are charged over the actual elapsed wall-clock between the two
snapshot timestamps, never over an integer number of days**, and the elapsed period is printed on
the waterfall ("mark to mark: 14h 12m"). A London morning mark is ~14 hours after the NY close, so
charging a full calendar day of theta into it puts the theta bar ~40% out and pushes the error into
`unexplained`, which then trips REQ-052 for the wrong reason and teaches the user to ignore the
residual alarm. `Δt_years = elapsed/365 days` feeds the §0 helper unchanged.
**[AWAITING USER]** whether the official cut is 17:00 NY or an internal firm cut, and whether the AM
mark is persisted or scratch (§10/Q-1).

**REQ-052 (M) — Unexplained is policed, and always displayed.**
G: an attribution. W: the page loads.
T: `|unexplained| / (Σ|components|)` is shown as a number **on every attribution, every day**, not
only when it breaches — a threshold that speaks only when it is already broken teaches nothing, and
the trader's first trust check is watching the residual drift **before** it breaches (§10/Q-10).
Reference bands printed next to it: **< 1% overnight on a clean vanilla book with a good mark**,
**< 2–3% on a day with a large smile move**. When it exceeds the alarm threshold of **5%** the
residual is highlighted red with the message "attribution does not fit — check marks or re-taylor",
and a diagnostic panel lists the three positions contributing most to the residual.

**REQ-053 (M) — Per-position attribution.**
G: an attribution. W: the user expands the waterfall.
T: `PnLBreakdown.detail` renders one row per position with the same component split, sortable,
and totals reconciling to the aggregate waterfall.

**REQ-054 (M) — Cumulative P&L and drawdown.**
G: ≥ 2 stored daily marks. W: the page loads.
T: a cumulative line by component plus a drawdown sub-panel; the series is rebuilt from stored
snapshots and the marks in force at the time (not recomputed against today's surface), so history is
stable. **Settled (§10/Q-10, check 1):** the stability is not an internal invariant but a
**displayed reconciliation line** — "yesterday's published PV vs yesterday's PV recomputed now:
0.00" — shown on the page every day. If yesterday's P&L can move overnight the user stops reading
the page permanently, so the proof belongs on the screen, not only in the test suite.

**REQ-055 (M) — Hedge log.**
G: spot positions tagged `hedge`. W: the page loads.
T: each hedge is listed with timestamp, pair, size, rate, the pre- and post-hedge delta, the spot at
the moment of the hedge, estimated cost, and its realized contribution; the log totals reconcile to
the `hedge` bar in the waterfall. **[CG-5 RESOLVED — arch AMENDMENT v1.1, already applied in
`types.py`]**: `trade_time: datetime | None = None` (UTC) exists on **both** `SpotPosition` **and**
`OptionPosition`, appended last so construction stays backward-compatible; no side-table is needed.
The hedge log **orders by `trade_time`, falling back to `trade_date`** where it is absent, and rows
lacking a time are marked as such rather than silently ordered by insertion. Because `trade_time` is
now a field on both position types, REQ-034's field-by-field round-trip requires it in the CSV
schema — see §3.5.

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
override is worth; where the pair also has an `INDICATIVE` surface, a second line shows
**mark-to-my-curve vs mark-to-ETF** (the basis of REQ-068(e) expressed in P&L), which is the number
that says what the ETF proxy would have cost had it been believed.

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
T: P&L is decomposed into `Σ 50·Γ$·(σ_r² − σ_i²)·Δt_years` (the REQ-046 identity, same helper,
same 50 — **not** `0.5·Γ$`, see §2.5) plus hedge slippage plus discretisation error, and the three
sum to the simulated P&L within ε.

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

**REQ-068 (M) — Manual mark grid: the primary pricing input.**
*(Re-prioritised by arch AMENDMENT v1.2/T-1, binding. This is no longer "manual overrides", a
buried fallback on page 8; it is the input the book is marked off. Trader review §1.4 and Q-3: with
no free OTC vol surface, ETF-implied vol is never a mark, and a tool that cannot be marked to the
desk's own curve will not be used to hedge. Build priority: this requirement precedes every other
analytic that consumes a surface.)*

G: the **top panel of page 8** (`/data`), mirrored on page 2 (`/surface`) so the trader can mark and
inspect the smile without changing page. W: the user types or pastes a per-pair, per-tenor grid of
**ATM / 25d RR / 25d BF** (10d RR/BF optional columns, blank = not supplied, never inferred) and
presses **Mark**. T, all clauses machine-checkable:

**(a) The grid and the 30-second budget.** Rows are `conventions.TENORS` (ON/1W/2W/1M/2M/3M, plus
6M/1Y where the user trades them), columns are the three (or five) quotes, one grid per pair.
Values accept `7.05` or `0.0705` per V-10 and are stored as decimals. Paste accepts a tab- or
comma-separated block from a broker run, a chat window or a spreadsheet; `Tab`/`Enter` traverse
cells; there is no modal dialog on the marking path. **Marking the whole G3 book (3 pairs × 6
tenors × 3 quotes) must be completable in under 30 seconds**, asserted as a QA task (§6.1) with a
scripted paste of three blocks, measured from first keystroke to `MARK` status on all three pairs.
Per-cell validation: V-10 vol range; `|RR25| ≤ 3·ATM` (W); `BF25 ≥ 0` (W); ATM monotonic-in-tenor
violations flagged (W, not blocked — kinks are real, §4.6). An invalid cell blocks only its own
tenor, never the whole grid.

**(b) Resolution order — manual wins, always.** The provider chain is
**manual → live → cache → synthetic** (`ManualQuoteProvider`, `fxgamma/data/manual.py`, arch
AMENDMENT v1.2). A manual mark is **never overwritten by a live pull**: not by Refresh (REQ-001),
not by a provider toggle (REQ-067), not by a cache warm, not by a session restart. It is removed
only by an explicit Clear (per cell / per pair / all), and Clear is confirmed. QA fixture: set a
manual mark, force a successful live pull that returns a different vol, assert the priced book is
unchanged and the badge still reads `user_override`.

**(c) Surface status, and what it gates.** Every surface carries a two-tier status, badged on every
number derived from it (REQ-002):
- **`MARK`** — built from this grid via `SmileQuotes` → `build_surface` (arch §8), unchanged.
- **`INDICATIVE`** — built from ETF chains, CBOE indices, CME settlements or the synthetic provider.
`MARK` is required for the two irreversible outputs: **REQ-043 issues no hedge instruction** and
**REQ-051 publishes no official mark** off an `INDICATIVE` surface — both are greyed with the reason
stated, and are re-enabled only by marking the pair or by a once-per-session explicit
acknowledgement that is recorded in the snapshot and printed on the output.

**(d) ETF and CBOE vols are demoted, permanently.** ETF-implied and CBOE index vols are **z-score,
cone, term-shape and richness inputs only** (REQ-009, REQ-010, REQ-011, REQ-018, REQ-019 and the
Lab) and are **never the mark** for PV, delta, hedge size, P&L or attribution. Any screen that
prices the book off an `INDICATIVE` surface says so in words, not only by colour (§5.7).

**(e) The basis is its own series.** Where a pair/tenor has both a manual mark and a live
(ETF/CBOE/CME) quote, both are retained and **`basis = indicative − mark`** is displayed in vol
points per pair and per tenor, with its own history. This is the honest measure of how far the
indicative path can be trusted, and it is what makes R-6 measurable rather than merely disclosed.

**(f) The other overrides, and where they live.** The same mechanism carries **spot**, **rate /
forward-point** and per-position mark-vol (REQ-036) overrides. The **spot override box lives on the
Risk page** (page 5) with a single-key shortcut as well as on page 8 — with 15-minute-delayed free
data (R-2) the first thing the user does is correct spot to a dealable price, and making that a
page-8 round trip means it does not happen (trader review Q-10). Overriding spot restamps every
downstream number in place and badges it.

**(g) Provenance, staleness and audit.** Every manual value is badged `user_override` (blue, §5.2)
with the user's optional note, the entry timestamp and the `mark_version`; all active overrides are
listed on one "active overrides" panel with per-item and global Clear. A manual mark **ages**: past
the trading-day boundary (§5.5), or more than 12 hours old, it is badged amber "stale mark" and the
Mark panel prompts for a re-mark; it is never silently dropped or replaced by live.

**(h) Marks are versioned with the snapshot.** Each `Mark` writes an immutable, timestamped grid
version to SQLite (§5.6) and is stamped into the snapshot, so REQ-054 rebuilds history from the mark
that was **in force at the time** and yesterday's P&L cannot move (trader review Q-10, check 1).

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
