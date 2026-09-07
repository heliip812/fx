# Test Report — regression net and numerical validation

**Owner:** QA / Test Engineer (`qa`)

**Pass 2 scope (this document).** The modules the first pass listed as untested and
ranked as the highest-value gaps: `fxgamma/data/manual.py` (the primary mark path since
amendment v1.2 T-1), `fxgamma/portfolio/` (`risk`, `zones`, `attribution`, `hedging`),
`fxgamma/signals/`, `fxgamma/backtest/` and `fxgamma/store.py` — plus the testable
obligations created by amendments **v1.4** (the golden fixture and the `BE = σ/√365`
identity), **v1.6** (pin-risk signs, the reporting-ccy layer, the sticky modes, W-7's two
bases) and **v1.7** (`max_attainable_delta` and the unattainable wing).

**Suite:** `tests/` (16 files, **4,825 tests**). **Runtime:** 56 s.

```
PYTHONPATH=/home/user/fx python3 -m pytest tests -q            # everything, ~56s
PYTHONPATH=/home/user/fx python3 -m pytest tests -q -m "not slow"   # skips the subprocess probe
```

## 1. Result of the run

```
3 failed, 4820 passed, 2 xfailed in 55.95s

FAILED tests/test_zones_pin_hedging.py::TestHedgeBands::
       test_the_frozen_default_rule_gives_the_band_amendment_v1_6_ruled_on          <- F-8
FAILED tests/test_attribution.py::TestResidualBudget::
       test_an_ordinary_overnight_vol_move_stays_inside_the_req_052_budget[0.0025]  <- F-10
FAILED tests/test_attribution.py::TestResidualBudget::
       test_an_ordinary_overnight_vol_move_stays_inside_the_req_052_budget[0.005]   <- F-10
XFAIL  tests/test_manual_marks.py::TestChainResolutionOrder::
       test_synthetic_is_demoted_below_live_however_the_chain_was_constructed       <- F-9
XFAIL  tests/test_delta_bounds_v17.py::TestTheNonPremiumAdjustedConventions::
       test_the_forward_bound_is_one_as_the_docstring_says                          <- F-13
```

**The model-layer hardening is clean.** Every test from pass 1 — the 3,973 finite-
difference Greeks, the 63 surface tests, put-call parity, the implied-vol round trip, the
four delta conventions — is still green after the C1 wing rework, the erfc-based
`_norm_cdf` and the vanna-volga cancellation fix. Nothing regressed.

| File | Tests | New? | What it protects |
|---|---:|:--:|---|
| `test_gk_numerics.py` | 3,973 | | Greeks vs finite differences, parity, implied vol, delta conventions |
| `test_properties.py` | 100 | | limits, signs, monotonicity, no-NaN |
| **`test_manual_marks.py`** | **91** | ● | the paste parser, mark persistence, chain resolution order |
| **`test_store.py`** | **90** | ● | soft delete, `trade_time` order, CSV round trip, V-1…V-21 |
| `test_conventions.py` | 69 | | cuts, DST, day count, pips, tenors |
| `test_golden_reference_trade.py` | 63 | +53 | v1.4's fixture, and `BE = σ/√365` across pairs/tenors/rates |
| `test_surfaces.py` | 63 | | quote repricing, SABR recovery, no-arbitrage, pickling |
| **`test_signals.py`** | **58** | ● | RV estimators, cones, richness, listed gamma |
| **`test_backtest.py`** | **53** | ● | look-ahead, costs, the long/short sign flip, metrics |
| **`test_portfolio_risk.py`** | **53** | ● | CG-1 reporting ccy, sticky modes, ladder, decay, W-7 |
| **`test_delta_bounds_v17.py`** | **47** | ● | `max_attainable_delta` and the unattainable wing |
| **`test_zones_pin_hedging.py`** | **44** | ● | pin-risk signs, zones, hedge bands, the hedge instruction |
| `test_regressions.py` | 43 | | the bugs that already shipped once |
| `test_data_layer.py` | 34 | | schemas, provenance grammar, determinism, the §8 boundary |
| **`test_attribution.py`** | **30** | ● | the P&L explain against a real move, with a residual budget |
| `test_pending_modules.py` | 14 | | the original shallow contract checks, kept as a floor |

All three failures are **findings, not test bugs**. Each is reproduced below.

---

## 2. Findings from this pass, ranked by what the bug could cost

### F-8 — `HedgeRule.band_pct` is still read as a percent, ~100x too tight. *(fails)*

**The most expensive open bug in the repo, and it contradicts a binding ruling.**

Amendment v1.6 CR-1: *"`HedgeRule.band_pct` … is a FRACTION, not a percent … The intent
was 25%. `types.py` is amended to say so unambiguously; the default value is unchanged and
is now correct rather than dangerous."* `types.py` carries that amended comment. Two
consumers did not follow:

* `fxgamma/portfolio/zones.py:434` — `max(bp / 100.0 * gross, 0.0)`
* `fxgamma/backtest/engine.py:414` — `rule.band_pct / 100.0 * gross`

So the frozen default `HedgeRule()` produces a **25,000** delta band on a 10mm book:

```
>>> zones.hedge_bands(book_10mm, mkt, "EURUSD", rule=HedgeRule())["band_base"][0]
25000.0                      # amendment v1.6 says 2,500,000
```

`zones` even emits a `warning` column telling the user to *"set band_pct=15 for the desk
default"*, which is the reading the PM withdrew.

**Cost.** Pure churn, charged every day the book is live. On one 1Y synthetic path,
10mm straddle, EURUSD costs:

| band_pct | hedges | explicit cost | end equity |
|---|---:|---:|---:|
| `0.25` (as the frozen default is read today) | 190 | 29,310 | 430,741 |
| `25` (as amendment v1.6 defines it) | 23 | 27,735 | 606,827 |

176k of P&L on a 10mm book in one year — most of it discretisation, not the visible
cost line, which is why it would never be traced back to the band. Worse on the Scandies,
where `COST_BP` is 10-25x EURUSD.

Tests: `test_zones_pin_hedging.py::TestHedgeBands::test_the_frozen_default_rule_gives_the_band_amendment_v1_6_ruled_on`
(red) and `test_backtest.py::test_the_backtest_and_the_risk_engine_read_band_pct_identically`
(green — it pins the *consistency* of the two sites, so fixing one without the other fails
immediately). **Owner: quant-risk (`zones.py`) and quant (`backtest/engine.py`), together.**

### F-10 — the P&L explain breaks its own 1% residual budget on any ordinary vol move. *(fails)*

`daily_pnl` evaluates every Greek at `t0`. Vega itself decays, so whenever the vol *and*
the clock both move — which is every day — the vega bar is charged at the `t0` vega over
an interval whose true average is lower, and the difference lands in `unexplained`.

| overnight move | residual / Σ\|components\| | residual / total P&L |
|---|---:|---:|
| spot only, ±0.3% | 0.25% | 0.6% |
| decay only, 1 day | 0.45% | 0.5% |
| **+0.25 vol point** | **1.67%** | **5.4%** |
| **+0.5 vol point** | **1.95%** | **3.4%** |
| +1 vol pt and +1% spot | 3.02% | 3.6% |

REQ-052 and the trader ask for < 1% overnight. A quarter of a vol point is not a stress
scenario, it is Tuesday.

**Why it is second and not tenth.** The money is small. The cost is that the residual
alarm is the instrument that catches the *next* bug, and an alarm that fires on every
ordinary day is an alarm the trader turns off — the exact mechanism the test report's
own §5 item 2 warns about for elapsed-time theta.

**The fix is measured, not guessed:** evaluating vega and theta at the midpoint of the two
snapshots (`0.5 (g_t0 + g_t1)`) drops the residual on the 0.25-vol-point case from
**-136.25 to -0.95 USD** — a factor of 143 — with no other change. Owner: **quant-risk.**

Test: `test_attribution.py::TestResidualBudget::test_an_ordinary_overnight_vol_move_stays_inside_the_req_052_budget`.

### F-9 — `ChainProvider` hoists manual but never demotes synthetic. *(xfail, strict — PM ruling wanted)*

`ChainProvider` guarantees exactly one re-ordering: manual to the front (M-1). A caller
who constructs `ChainProvider([synthetic, live], allow_synthetic=True)` gets a chain that
answers every unmarked pair from the simulator while a live source sits behind it. It is
badged `synthetic`, so nothing is *mislabelled* — but architecture §7 says "never silently
substitute synthetic data for live data", and a source that is never consulted is a
substitution.

The shipped `get_provider("auto")` builds the order correctly (manual → live → cache →
synthetic, pinned green in `test_the_shipped_factory_builds_the_full_four_tier_order`), so
this is a latent constructor hazard, not a live defect. Whether the class should defend
itself is a judgement call, hence `xfail(strict=True)` rather than red.
**PM ruling wanted.**

### F-11 — `lookahead_report` certifies the engine, not the strategy. *(no failure; scope note)*

The REQ-060 check shifts the driving series, re-runs, and shifts back. A strategy that
closes over the *whole* `DataFrame` rather than reading its `View` shifts with it, so the
report returns `passed=True` on a rule that is reading 30 days of future prices. QA's
negative control demonstrates it: the same path, same tenor, gives **-1.1mm** honest and
**+2.1mm** peeking, and `lookahead_report` passes both.

This is a boundary, not a bug — only the `View` stops a strategy-level leak, and a rule
that ignores its `View` bypasses it. It matters because the Lab *displays* this evidence,
and "look-ahead check: passed" reads as a claim about the strategy. **The panel must say
which of the two it certifies.** Owner: PM to rule, dev to word the panel.
Tests: `test_backtest.py::TestNoLookAhead` (both the control and the boundary).

### F-13 — `max_attainable_delta` returns `inf` for the plain `fwd` convention. *(xfail, strict)*

Its own docstring says the supremum is *"`e^{-rf T}` for `spot`, **`1` for `fwd`**, `+inf`
in strike for a `_pa` put"*; the code returns `inf` for everything that is not `spot`.

Cosmetic today — `strike_from_delta` still returns `nan` for a forward delta above 1, so
nothing prices wrong (fenced green). It matters because amendment v1.7 makes this function
the thing the strike ticket calls **before** solving: a UI that trusts `inf` will render
"attainable" and then show a `nan` strike, which is exactly the outcome v1.7 forbids.
One line. **Owner: quant-models.**

---

## 2b. Findings raised and fixed inside this pass

Three of the first pass's items, and two raised by this one, closed while it was running.
The tests stay as fences.

* **F-4 — `get_store(path)` ignored `path`. FIXED** (amendment v1.5 Q-4, dev). Now cached
  per *resolved* path, with `:memory:` never cached and `fresh=` scoped to one book.
  Fenced by four tests, including "the same book asked for two ways is one store".
* **F-7 — the paste parser silently mis-marked a misaligned grid. FIXED** (data).
  A header declaring `Pair, Tenor, ATM, RR, BF` against rows that omit the pair column
  shifted every value one place left, so a 7.05-vol EURUSD book was marked and saved at
  **25 vol with RR +18** and no warning about the ATM at all — on the *primary* mark path.
  The parser now cross-checks the column the header assigns to the tenor against the column
  the row scan actually found it in, and refuses the paste naming both.
* **F-12 — `save_positions` / `commit_import` were not atomic. FIXED** (dev). `_tx` was
  re-entrant but committed at every level, so an inner `save_position` committed row 1
  before row 2 raised and the outer rollback had nothing to undo — a half-imported book
  with no audit row, and the user could not tell which half landed. Now depth-counted.
* Both `xfail`s from pass 1 are resolved: Q-2 (`sum(Greeks)`) and Q-3 (unknown cut raises)
  are green, and F-1 (synthetic reproducibility across processes) stays green under three
  `PYTHONHASHSEED` values.

---

## 3. What this pass verified

### 3.1 The manual mark path (`test_manual_marks.py`, 91 tests)

Since amendment v1.2 T-1 every Greek in the app is computed off whatever this module
decides the vol is, so the organising rule is: **a wrong mark must be impossible, a refused
mark is acceptable, a silent one is a defect.**

*The parser.* One 3-tenor EURUSD grid is written **twelve ways** — tab, comma, semicolon,
pipe, multi-space, `%` signs, decimals, a header row, header aliases (`Term/Vol/RR25/Fly`),
parenthesised negatives, a Unicode minus, a transposed (tenor-across-the-top) grid with and
without a corner label, and blank/comment lines — and every spelling must land on
identical decimals to 1e-12. Plus: bid/ask → mid (`7.05/7.25` → 7.15); a leading pair
column marking the G3 book in one paste; `EUR/USD` understood; unparseable lines named in
`skipped` rather than swallowed; a grid with no pair anywhere failing loudly instead of
guessing; empty input never raising (it runs in a Dash callback). A 200-case randomised
round-trip sweep renders random grids in random spellings and requires exact decimals back,
and an optional Hypothesis property (`importorskip`, so the suite is green without it)
shrinks against "whatever is stored is a plausible decimal vol".

*Unit inference.* `8.5` vs `0.085` is a 100x error in the most load-bearing number in the
app. The scale is taken from the ATM column, applied to the whole grid (never per column),
`%` beats magnitude, and **the inference is always reported** — `"vols read as percent…"`
appears in `inferred` and in `summary()`, per architecture §7.

*Refusal.* An ATM of 705%, 0.00705% or `1e6` is **not stored and not rescaled** — it is
skipped, warned about by pair and tenor, and the good rows in the same paste survive. A
35-vol JPY crisis mark is admitted. A negative butterfly and a `|RR| > ATM` are flagged and
kept. A missing RR/BF defaults to symmetric *and says so*.

*Store and provider.* Bit-for-bit save/reload including `asof`; the schema tag; a corrupt
file **raises** rather than starting unmarked (`strict=False` is the opt-out); one bad row
dropped without losing the file; `paste` replaces the pair by default and can merge; **a
paste that parses to nothing does not wipe the existing marks**; CSV round trip exact;
`user_override` badging on `surface.<PAIR>` *and* `surface.<PAIR>.<TENOR>` for the CG-7
fallback; a stale mark still served with `STALE` in the badge; `max_age_hours` declining so
the chain falls through; every non-mark method returning empty so it never fakes truth.

*Resolution order.* A manual mark wins over live and **the live source is never even
consulted** for a marked pair; manual is hoisted to the front from either construction
order; an unmarked pair falls straight through to live, so marking is per pair; repeated
live pulls never move the mark (T-1's blocking clause); synthetic is dropped unless
explicitly allowed; `get_provider("auto")` builds manual → live → cache → synthetic; and
end to end, `snapshot().surfaces["EURUSD"].atm(1M)` **is** the typed 7.05% while USDJPY is
badged `synthetic`.

### 3.2 Reporting currency, ladders and decay (`test_portfolio_risk.py`, 53 tests)

`fx_rate` is exactly 1 on the identity leg, is self-inverse, gives `1/USDJPY` for JPY→USD,
reproduces the triangle for a cross, and **raises naming the missing leg** rather than
defaulting to 1.0. `price_book` carries `ccy`, `base_ccy`, `report_ccy`, `fx_to_report`,
`fx_base_to_report` and a `*_rep` copy of every monetary Greek, and multiplies quote-ccy
money and base-ccy amounts by *different* rates — the CG-1 mistake that is invisible on
EURUSD and not on USDJPY.

**The v1.6 headline is a standing regression.** On a EURUSD+USDJPY book the naive theta sum
is dominated by the unconverted JPY leg; the test requires the naive/converted ratio to
exceed 20 and `book_greeks` to equal the `*_rep` sum, so it fails the moment anyone sums a
native column across pairs. Base-ccy amounts come back **`nan`** across mixed bases (CR-2 /
the T-2 precedent), aggregate natively within one base, and convert under
`base_as_value=True`; `delta_pct` and `dual_delta` are always `nan` at book level; changing
the reporting ccy rescales the whole book consistently for USD, EUR and JPY.

Ladder: 101 nodes bracketing spot, midpoint equal to `price_book` to 1e-9, only the named
pair shocked, all three sticky modes labelled, **`sticky="none"` numerically identical to
`"strike"`** (CR-3), sticky-delta genuinely different from sticky-strike on a
risk-reversal book but identical at `S₀`, `gamma_fd == gamma` under sticky-strike (so the
`skew_gamma_1pct` gap is real and not grid noise), and that gap non-trivial under
sticky-delta. Scenario grid: cartesian, zero at the origin, and a +2 vol-point cell equal
to `2·vega + ½·4·volga` to 2% — which pins the vol-*point* unit. Decay: calendar the
default with unit weights, monotone PV, predicted vs repriced within the charm term,
business weighting opt-in and **total-time-preserving**, unknown calendar and mismatched
weights raising, and the delta-hedged path flat at `σ_r = σ_i`.

### 3.3 Pin risk, zones and the hedge instruction (`test_zones_pin_hedging.py`, 44 tests)

The amendment v1.6 truth table, priced, one parametrised case per row:

| position | `delta_if_above` | `delta_if_below` | `jump_at_strike` | REQ-045's formula |
|---|---:|---:|---:|---:|
| long call | +10mm | 0 | **+10mm** | +10mm |
| **long put** | 0 | −10mm | **+10mm** | **−10mm ✗** |
| short call | −10mm | 0 | **−10mm** | −10mm |
| **short put** | 0 | +10mm | **−10mm** | **+10mm ✗** |

plus: `jump == above − below` by construction; the jump **independent of call/put**; the
old formula wrong by exactly `2N` on a put and kept as a visible column rather than as the
answer; a straddle inheriting ±N either side with a 2N jump; a risk reversal netting to
opposite jumps at two strikes; and a premium-adjusted pair reporting the same jump (the
`K/S` factor is identically 1 at the crossing).

Framing: only strikes inside the horizon by default, `dist_sigma_days` on the **√252**
basis, a strike at spot flagged and a far one not, finish probabilities bounded and
nested. Touch probability: 1 at the level, ~0 for the unreachable, monotone in distance,
rising in vol and time, and **≈ 2× the finish probability** for a driftless process — the
reflection-principle check that stops a finish probability being labelled "will I trade
this zone".

Zones: one long-gamma zone around a single strike, a short one for a short book, two
clusters reported as two zones, and the CR-4 detail fields (sigma-days, touch probability,
repriced *and* quadratic P&L-to-centre, contributing strikes, delta carried inside, the
`*_rep` copies) all populated. Hedge instruction: SELL on long delta and BUY on short, the
one-line reason printable, "FLAT ENOUGH" inside the band, `mode="none"` silent, `to_edge`
trading exactly one band less than `to_target`, a sub-min-clip trade suppressed, cost from
the per-pair `COST_BP` table, and an existing spot hedge reducing the suggestion.

### 3.4 W-7 — the two annualisation bases, asserted where both are exposed

Ratified by amendment v1.4 ruling 5. `zones.CALENDAR_DAYS == 365`, `zones.TRADING_DAYS ==
252`, `realized.ANNUAL == 252`; `sigma_day_pct` is √252 and explicitly **not** √365;
`daily_breakeven` is √365 and prints `be_basis`; the ratio of the two on one book is
`√(365/252)` to 0.5%; RV's annual factor is a pure scale on every one of the five
estimators; `all_estimators` prints `sqrt(252)` on every row; the golden fixture pins
39.9 vs 48.1 pips. They are never conflated anywhere the library exposes both.

### 3.5 The v1.4 golden fixture, extended (`test_golden_reference_trade.py`, 63 tests)

The arbitrated numbers stand (Γ₁ 3.914mm, θ −2,868/day, −8,608 Friday→Monday, 39.9 pips).
Ruling 3's identity is now swept: `BE = σ/√365` to **1e-9 relative** over 5 pairs × 6
tenors, and over 4 rate settings × 4 vol levels including `rd < rf` and a near-zero
domestic rate. Two additions earn their place:

* a **negative control** showing the same identity built on the *total* theta is
  rate-dependent (out by >5% at 5%/0.1% on USDJPY) — which is why W-15's gamma-theta
  matters and why a zero-rate-only test would have passed a wrong implementation;
* a **scale-freedom** check: quadrupling the notional and re-quoting the market on a
  100x handle leave the breakeven unchanged to 1e-12, which is where a pip-size or
  handle dependence (W-16) would surface and nowhere else.

### 3.6 Amendment v1.7 — the unattainable wing (`test_delta_bounds_v17.py`, 47 tests)

The PM's two figures are pinned on USDJPY at rd 2% / rf 4%: **0.8745** at 3M/10% and
**0.2764** at 5Y/40%, and the bound is shown independent of the spot level. The bound is
then verified by brute force — a 4,001-point scan across five decades of strike space finds
no delta above it and gets within 2% of it (so it is neither violated nor loose) — over 12
(T, σ) combinations. The delta map is confirmed **unimodal** (exactly two strikes share
each attainable delta), which is why a solver has to know about the peak.

`strike_from_delta` returns `nan` and never a wrong root for deltas 0.1%–200% above the
bound, and still solves and round-trips to 1e-7 at 25%, 50%, 90% and 99% of it. The
concrete case: at 5Y/40% the 25-delta call solves and the **30-delta call does not exist**.
The put side is unbounded and always solves. All five premium-adjusted pairs have a finite,
binding bound below 0.35 at 5Y/40%, while the 25d wing exists at every shipped tenor and
realistic vol — so the `nan` path is an edge case, not the normal case.

**The downstream path the report flagged as most likely to break next is now covered:** a
5Y USDJPY surface marked at 40 vol returns `nan` from `vol_by_delta` at 30d, 40d and 50d —
not 0.0, not the nearest attainable delta, not the far root — while the attainable side of
the same smile still prices. An optional Hypothesis property shrinks against the same
invariant over random `(S, T, σ, rd, rf)`.

### 3.7 Backtest (`test_backtest.py`, 53 tests)

*No look-ahead.* The `View` exposes rows `0..i` and no more, hands out a copy so a strategy
cannot mutate the path, **raises** on an index past `now`, and computes its realized vol
from visible data only. `lookahead_report` passes on all three shipped strategies
(`changed_under_shift` *and* `restored_exactly`). The strongest form: truncating the path
at step 60 leaves the first 60 equity values **bit-identical** (`array_equal`, not
`allclose`), so nothing that happened by step 60 depends on data arriving after it. The
negative control and the detector's boundary are F-11 above.

*The controlled vol experiment.* Realized 14% vs an 8% mark: long straddle makes money,
short loses. Realized 5% vs 10%: both flip. Gross of costs the two sides are mirrors to
10% and **both** pay their spread. `realized_vol_captured` recovers the path's own 0.14 to
±0.03. This one block exercises the pricer, the Greeks, the hedger and the cash accounting
end to end.

*Costs.* `pnl_gross − pnl_net` equals the charged cost exactly, per step and cumulatively;
the cost series is never negative; hedge cost + option cost = total; a tighter band hedges
more, turns over more and costs more; doubling `cost_bp` more than 1.5x's the hedge cost at
near-constant turnover; zeroing both costs makes gross and net coincide; and widening the
option spread 10x raises the option cost 5x on a short straddle — the check that the
bid/offer is charged on the way **out** as well as in.

*Accounting.* `equity == cash + pv + spot_pos·S` and `cumsum(pnl) == equity` to 1e-10; the
recorded delta never leaves its band at 5%, 10% and 25% and is within 2x of it (so the band
is binding); expiries settle to intrinsic exactly; a position is open >95% of steps; the
run is bit-for-bit reproducible and seed-controlled; the path is badged `SIMULATED`.

*Metrics.* `decompose` reconstructs gross exactly with everything the four terms miss named
`residual`; `discretisation == gamma + theta − carry_identity`; Sharpe annualises on the
stated √252 basis and net is below gross; drawdown is non-positive; the stats carry their
provenance; small buckets are flagged `interpretable=False`.

### 3.8 Signals (`test_signals.py`, 58 tests)

Five RV estimators, each finite and plausible, **scale-invariant** (a vol that changed with
the handle would read differently for USDJPY than for the same path on another quote), all
within a factor of 2 of each other (which catches a fumbled Parkinson or Garman-Klass
constant), zero on a flat path, and recovering the closed-form vol of a deterministic
zig-zag to 1e-3. `rolling_vol` starts **after** its warm-up rather than back-filling, and
its last point is reproducible from its own trailing window alone.

Cones: ordered percentiles, an effective-`n` column, bounded percentile lookups, and less
dispersion at longer horizons. Richness: the library's own breakeven identity across four
vol levels; `nan` (not a number) for a short-gamma book; `be_pips` consistent with
`be_pct`; **the Friday question** — `days=3` triples the theta bill and leaves the
breakeven alone; `coverage_ratio` exactly 1 at breakeven and `(σ_r/σ_i)²` scaling;
`gamma_carry_expectancy` identical to `risk.dhedge_pnl`; `rv_iv_spread` signed
implied-minus-realized and matching a 30-calendar-day option to **21 business rows** (W-8).

Z-scores: exact on a known series, `nan` on a constant one, and the **overlap correction**
verified — a z built on a 21-day rolling window is deflated by exactly √21 and reports
`n_eff = n/21`, without which every z is inflated ~4.6x. Amendment v1.3 C-6's obligation is
tested as the library's half of the contract: `n` and `n_eff` travel with every z (an
11-observation z reports `n=11`), and fewer than three observations refuses outright.

Listed gamma: the default assumption is `unsigned` and is carried in the output on every
row; an unsigned profile is `|gamma|` and **has no flip level** ("manufacturing one is
exactly the fiction this module refuses to sell"); `long_all` and `short_all` are exact
mirrors; each named assumption changes the signed profile and leaves `gamma_1pct_abs`
alone; magnets are ranked unsigned, carry their distance in pips and their "not net dealer
position" note; the expiry ladder never invents OI and names the `otc_cut_used` (W-11).

### 3.9 Attribution (`test_attribution.py`, 30 tests)

The residual budget now runs against genuinely different snapshots. Inside REQ-052's 1% on
six spot/decay moves including a 14-hour mark and a Friday→Monday. `total` equals the
repriced PV change exactly; components plus unexplained reconstruct the total; a zero move
gives a zero explain. A pure decay lands in theta (and a *small* vega, because rolling the
clock moves the option along the term structure — that belongs in vega, not in
unexplained); a pure spot move in delta and gamma, with delta linear and **gamma exactly
quadratic** in the move; a pure vol move in vega; a joint move putting a linear-in-`dS`
amount in vanna.

**Elapsed-time theta (W-14) is fenced:** half a day charges exactly half the theta, a
14-hour mark is not charged a whole day, and `dt_days` is reported. Hedges struck between
the snapshots land in the `hedge` bar measured from their own entry rate (which is what
makes REQ-055 reconcile); a spot leg is pure delta plus carry with a zero residual. The
cross-currency explain is reported in one ccy, rescales cleanly between USD and EUR, and
holds the residual budget. A leg expiring between the snapshots leaves no residual; a leg
already dead at `t0` contributes nothing; `top_offenders` ranks by absolute residual.

### 3.10 Store (`test_store.py`, 90 tests)

Field-for-field round trips for options (including `cut`, `trade_time`, `premium_ccy`) and
spot legs (including the sign); upsert on id; book isolation; survival of a reopen.

**Soft delete (REQ-031):** delete hides but keeps the row and lists it with a timestamp;
undelete restores; a hard delete is opt-in and unrecoverable; deleting an unknown id
returns `False`; `clear_book` and `delete_tag` are soft and scoped; and **`load_book(asof=)`
reconstructs the book as it stood** either side of the deletion — the property the P&L page
depends on.

**`trade_time` ordering (CG-5):** spot legs return in `trade_time` order, a leg without one
falls back to its `trade_date`, hedge-tagged legs are written to the hedge log
automatically and come back ordered, non-hedge legs are not logged, and naive timestamps
are stored as UTC. CG-2 mark vols round trip and survive a reopen.

**CSV:** export → parse → commit reproduces the book field for field, including the spot
leg's sign; the header is the documented schema; parsing writes **nothing** until commit
(the REQ-033 gate); a file with no `pair` column is rejected naming the schema; commit
refuses errors, and refuses warnings unless accepted; `replace` mode soft-deletes the old
book; unknown columns are kept in notes; the import is audited; `rejects_csv` is a fixable
file.

**V-1…V-21**, one test each where the rule has teeth: unknown pair; expired expiry refused
unless history is asked for; beyond-2Y warned; **a USDJPY strike typed as 1.4725 and a
EURUSD strike typed as 147.25 both refused** (V-6/V-7 — the most expensive ticket typo
there is); notional sign and the "did you mean mm?" query; a vol of `8.5` rescaled *and
reported*, one of `450` refused; every call/put and buy/sell spelling a human types, and
the unreadable ones refused; **a typo'd cut refused, not defaulted to NY10** (the store's
half of Q-3); a premium ccy that is not a leg of the pair; a premium whose sign contradicts
the direction; a premium that is 77% of notional; duplicate ids across the book and inside
one file; V-20 structure consistency (a "straddle" with split strikes, a risk reversal with
split expiries); V-21's size warning. Plus the notional grammar (`10mm`, `1bn`, `250k`,
`10,000,000`) and its refusals.

---

## 4. What is still **not** covered, and why

| Area | Status | Why |
|---|---|---|
| **Live data adapters** (`spot_yahoo`, `spot_stooq`, `spot_ecb`, `rates_fred`, `vol_etf_options`, `vol_indices`, `cme_options`, `_http`, `cache`) | **untested — now the largest hole** | Market-data hosts are blocked here. The parsers *can* be tested offline against fixtures; `fxgamma/data/fixtures/*.json` covers only part of the surface and there is still no stooq CSV, ECB XML, FRED series or CME settlement fixture. The FXY/FXC inversion (`inverted_etf`) and the ETF→pair vol basis are the two highest-value untested transforms. **Recommend `data` ships those four fixtures; a later pass writes the parser tests.** `scripts/verify_live_sources.py` covers the live half on the user's own machine. |
| Chain **snapshotter** / accumulating history (C-6) | **not covered** | No test yet that a day's pulled chain is appended to the parquet cache and read back. The z-score sample-size half of C-6 *is* covered (§3.8). |
| `app/` — Dash callbacks and rendering | **not covered** | Out of scope. Three v1.7/v1.6 obligations are binding on `dev` and testable only there: the header card computing theta from `book_greeks` (v1.4 ruling 2), `max_attainable_delta` rendering "unattainable" (v1.7), and the effective-sample-size caption on every z-score (C-6). |
| Options on crosses (MISS-14) | **not covered — policy undecided** | They price fine off the synthetic surface; whether v1 supports them is a PM decision. |
| Holiday / settlement calendar, real-date tenors (W-9, MISS-9) | **not covered — does not exist** | `TENORS` is a constant year-fraction grid. The tests pin the current frozen behaviour; the defect is a spec item awaiting a ruling. |
| Multi-day / path-dependent attribution | **partial** | Two-snapshot explain is now well covered; a *chained* week of explains summing to the week's P&L is not. |
| Performance against the real target | **one crude guard** | A 10k-point slice under 2 s. No benchmark for arch §4's ~1e5 `vol()` calls per refresh, and none for a 200-position book through `price_book` + ladder + zones, which is the actual UI budget. |
| Concurrency | **not covered** | `Store` takes an `RLock` and WAL; nothing tests two writers, and the Dash callback path is inherently multi-threaded. |
| Hypothesis | **used, optional** | Two property tests (`parse_grid` plausibility, the pa delta bound), both behind `importorskip` so the suite is green without it. `hypothesis` is **not** in `requirements.txt` — `data`/`dev` own that file; adding it is a one-line request. |

---

## 5. Where this codebase is most likely to be wrong next

Re-ranked after covering the portfolio, signals, backtest and store layers.

1. **The live adapters, and specifically the ETF→pair vol basis.** Every layer above them is
   now fenced; they are not tested at all. An FXY inversion done once in the wrong direction
   produces a smile that is a mirror image of the truth, and nothing downstream would flag
   it — the surface would build, the density would be positive, the calendar clean.
2. **Two readings of `band_pct` in the same repo (F-8).** Until it is fixed, the Risk page
   and the Lab disagree about what a hedge band is. That is worse than either reading being
   wrong, because a user tuning the band in the Lab will carry the wrong number to the desk.
3. **The residual alarm crying wolf (F-10).** Not the money; the habituation.
4. **Elapsed-time theta above the library line.** `daily_pnl` charges theta over wall clock
   correctly (fenced in §3.9). The exposure has moved up a layer: whatever the app passes
   as `asof`. A naive `datetime.now()` in a callback or a date picker yielding a local date
   reintroduces the whole class above the tested boundary.
5. **Listed-OI mechanics (W-11).** Contract multipliers, futures-vs-spot, American vs
   European, the inversion Jacobian and the CME expiry calendar are each a silent
   factor-of-something. `gex` is now tested for internal consistency, sign discipline and
   its own honesty caveats — but every one of those tests runs on *synthetic* OI. The
   arithmetic that turns a real CME settlement file into a strike in pair space is untested.
6. **A 200-position book at UI latency.** No test asserts the refresh budget, and
   `gamma_zones` runs a 321-node ladder per pair.
7. **Vanna and volga units on the cards (MISS-11).** The library's units are correct,
   documented and now cross-checked against a second-order scenario cell. The risk is
   entirely in how they are labelled on screen; "quote ccy per vol point per 1 unit of spot"
   means a 100% spot move for EURUSD.

---

## 6. Conventions for anyone extending `tests/`

* Test through the **contract-frozen public API** only. Four agents edit this repo
  concurrently; internal names move. (Two modules were fixed underneath this pass while it
  was running — the tests survived because they were written against the contract.)
* **Offline.** `get_provider("synthetic")` and the shipped fixtures. A test that needs the
  network is a test that will be skipped forever.
* **Parameterise, do not copy-paste.** Shared grids, the finite-difference harness and the
  `eur_book` / `mixed_book` fixtures live in `tests/conftest.py`; add an axis there and
  every test gains it.
* **Name the test for what it protects**, not for the function it calls, and put the *why*
  in the docstring — a failing test should explain the risk without a git blame.
* **Optional dependencies** (Hypothesis) go behind `pytest.importorskip` with a reason, so
  the suite is green on a bare checkout.
* **A failing test is a finding, not a chore.** Record it here, name the owner, leave it
  red. `xfail(strict=True)` only where the desired behaviour is a judgement call the PM has
  not ruled on — there are exactly two (F-9, F-13).
