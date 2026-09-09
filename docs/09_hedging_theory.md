# Hedging theory: rebalance bands, session variance, and the overnight ladder

Owner: quant-overnight. Code: `fxgamma/portfolio/bandopt.py`, `fxgamma/portfolio/overnight.py`.
Brief: `docs/08_overnight_gamma.md`. Contract: `docs/01_architecture.md` (+ amendments v1.1–v1.9).

Everything numeric in this document was produced against `get_provider("synthetic")` on
`asof = 2026-09-09 16:00Z`, seed-stable per amendment v1.5 Q-1. Re-running reproduces it.

---

## 0. If you read one page, read this one

1. **The band does not do what "maximise monetisation" implies.** For a delta-hedged
   position the *expected* gamma P&L over a window is `Γ·V/2` and it is **independent of the
   rebalance band**. Widening the band saves transaction cost and adds hedging-error variance.
   That is the entire trade-off. §2.
2. **The ladder does not create P&L either.** Under driftless spot, leaving resting orders
   changes the expected overnight P&L by exactly **minus their transaction cost**. What the
   orders buy is *conversion* (mark-to-market becomes cash you keep even if spot round-trips)
   and *variance reduction* (you do not wake up holding a delta you never chose). §7.
3. **Overnight is usually negative carry.** You pay 58% of a day's theta for 30–45% of a day's
   variance. On the shipped EURUSD profile the window needs realised vol **9% above implied**
   to break even; over a weekend it needs **2.4× implied**. The `crossover_vol` number is the
   go/no-go. §3.
4. **The delta cap, not the band, is the decision worth money.** At the user's real (retail)
   cost the analytically optimal EURUSD band is ~180 pips — five overnight sigmas. It is
   unusable as an overnight ladder. The cap binds and it *should*. §6, §9.
5. **Analytic vs empirical:** Zakamouline lands within **10–23%** of the backtester's argmax at
   interbank cost and **19–21%** at retail cost; Whalley–Wilmott is **23–51% too tight**
   everywhere, and most of that gap is one identified, correctable constant. §5.

---

## 1. Units, stated once, because most band errors are unit errors

| symbol | meaning | units |
|---|---|---|
| `S` | spot | quote ccy per 1 base ccy |
| `λ` (`lam`) | **one-way** proportional spot cost | dimensionless fraction, `= cost_bp / 2 / 1e4` |
| `Γ` (`gamma`) | `d(delta_base)/dS` | base² / quote |
| `Γ₁` (`gamma_1pct`) | delta change per +1% spot | base ccy |
| `h` | band half-width **in spot** | quote per base |
| `H` (`band_delta_base`) | band half-width **in delta** = `|Γ|·h` | base ccy |
| `γ` (`risk_aversion`) | absolute risk aversion, `U = E[P] − (γ/2)Var[P]` | 1 / **report** ccy |
| `V` | spot variance over the window, `σ²·τ·S²` | quote² |
| `τ` | horizon | **trading** years, `days/252` |

Dimension check on the headline result: `λ·S·Γ²/γ = 1 · (quote/base) · (base⁴/quote²) · quote =
base³`, so its cube root is a base-ccy delta. Good.

**`risk_aversion` is in 1/report-ccy here and is converted internally** (`γ_quote = γ_rep · fx`).
A JPY-denominated variance is ~2.2e4× a USD one; using the same coefficient for both would put
the USDJPY band out by a factor of ~28 in `h`. **`zones.hedge_bands` reads its own
`risk_aversion` in 1/quote-ccy** — see §10, RFC-1.

**Two clocks, on purpose** (amendment v1.4, trader W-7): spot travels on **252** trading days;
theta is paid on **365** calendar days. `bandopt` uses 252 throughout (which is also what the
backtest engine steps on, so analytic and empirical are directly comparable). `overnight`
uses 252-based session variance for distance and 365-based pro-rata calendar theta for the
bill. Conflating them is not a rounding issue: it turns the weeknight crossover ratio from
1.09 into 1.31.

---

## 2. The result everyone gets wrong: the band is not a P&L knob

Rebalance a delta-hedged position on a fixed grid of spacing `h` (in spot). Two facts:

**(a) Expected number of grid crossings.** By Tanaka's formula, for a driftless diffusion
started at 0 with terminal variance `V`, the expected local time at level `x` is

```
E[L_T(x)] = E|X_T − x| − |x| = 2·s·[ φ(u) − u(1 − Φ(u)) ],    u = |x|/s,  s = √V
```

and the number of times an `h`-spaced grid line at `x` is crossed is `E[L_T(x)]/h`. Summing
over the grid, `Σ_k E[L(kh)] ≈ (1/h)∫E[L(x)]dx = V/h` (because `∫L_T(x)dx` is the quadratic
variation), so

```
E[N_crossings] = V / h²
```

**(b) P&L per crossing.** A grid position is `Γh` of delta per rung; each crossing is half a
round trip of size `h`, worth `Γh²/2`.

Multiply:

```
E[gamma P&L] = (V/h²)·(Γh²/2) = Γ·V/2      ← independent of h
```

which is exactly the continuous-hedging gamma P&L. **The band cancels.** So does the ladder:
mark-to-market already contains `0.5Γ dS²`, and every hedge trade is a fair bet whose expected
contribution is zero. What the band changes:

```
cost(h)  = λ·S·|Γ|·V / h                (falls as h widens)
Var(h)   = Γ²·h²·V / 6                  (rises as h widens)   [hedge-to-target occupation]
```

so the optimisation is `max_h  ΓV/2 − λS|Γ|V/h − (γ/2)·Γ²h²V/6`, whose first-order condition
is `λS|Γ|V/h² = (γ/6)Γ²hV`, i.e.

```
h³ = 6λS / (γ|Γ|)        ⟺        H³ = 6 λ S Γ² / γ
```

Note that **`V` and therefore `σ` and the horizon cancel out**. That is a property of the
asymptotic expansion, not of the market, and §4 is about putting `σ` back.

*Verification of (a), `bandopt`/`overnight` code path* — summing `expected_crossings` over a
grid of ±2000 rungs, `s = 1`:

| `h` (in `s`) | `Σ E[L]/h` | `V/h²` | ratio |
|---|---|---|---|
| 0.05 | 400.167 | 400.000 | 1.0004 |
| 0.10 | 100.167 | 100.000 | 1.0017 |
| 0.25 | 16.167 | 16.000 | 1.0104 |
| 0.50 | 4.167 | 4.000 | 1.0417 |

The excess is exactly `h²/(6V)` — the Euler–Maclaurin correction from the `x = 0` term, i.e.
`+1/6` of a crossing however fine the grid. It is why the identity is asymptotic in `h/s` and
why a ladder whose spacing exceeds ~0.5σ should not be described by it without the correction.

---

## 3. Session variance time, and the number that actually decides the night

### 3.1 The hour-of-day profile

`DEFAULT_HOUR_PROFILES` holds 24 relative **variance** weights per UTC hour, normalised so the
day sums to 24 (average hour = 1). Shape, per pair, follows the standard FX intraday
seasonality (Andersen & Bollerslev 1997/1998; Bollerslev & Domowitz on quote arrival): an Asian
trough at 03:00–05:00 UTC, a step at the London open (07:00–08:00 UTC), the London/New York
overlap peak at 12:00–16:00 UTC (US data at 12:30/13:30 UTC, the 4pm London WM/R fix), and the
genuine trough of the day at 21:00–23:00 UTC around the New York close. JPY carries a real
Tokyo-fix bump at 00:00 UTC; GBP is the most London-centric major; AUD/NZD keep more variance
in Asia.

**PROVENANCE — read this before quoting any number below.** These are **modelled defaults**.
Yahoo serves ~730 days of hourly FX bars, which is ~12,000 usable returns (~500 per bucket),
and `estimate_hour_profile()` fits exactly this object from them — but **hosts are blocked in
this sandbox, so nothing here has been fitted to real data**. `SessionProfile.source` is
`"default"` and every consumer must badge it (architecture §7). The estimator is written,
tested against a known profile, and is a one-line swap when the user runs it on their own
machine.

EURUSD default (variance weight by UTC hour, average = 1.00):

```
h   00   01   02   03   04   05   06   07   08   09   10   11
   0.53 0.48 0.43 0.38 0.38 0.43 0.62 1.29 1.68 1.63 1.39 1.25
h   12   13   14   15   16   17   18   19   20   21   22   23
   1.72 2.11 2.20 1.96 1.53 1.05 0.77 0.62 0.53 0.38 0.29 0.34
```

Peak/trough 7.67 (GBPUSD 8.04, USDJPY 6.14, AUDUSD 4.00).

### 3.2 What the overnight window is worth

London close 17:00 → open 07:00, `asof` in BST so 16:00Z → 06:00Z:

| pair | clock share (= **theta** share) | variance share `var_fraction` | theta paid per day of variance | weekend `var_fraction` (2.583 cal days) | weekend ratio |
|---|---|---|---|---|---|
| EURUSD | 0.583 | **0.3393** | **1.72×** | 0.3205 | 8.06× |
| USDJPY | 0.583 | **0.4032** | **1.45×** | 0.3800 | 6.80× |
| GBPUSD | 0.583 | **0.3089** | **1.89×** | 0.2921 | 8.84× |
| AUDUSD | 0.583 | **0.4489** | **1.30×** | 0.4186 | 6.17× |

14/24 = 58% is wrong by 1.72× in variance (1.31× in sigma) on EURUSD. A ladder built on clock
time puts every rung 31% too far out and understates every touch probability.

The weekend column is the one worth staring at: `session_variance_weight` zeroes the 48 hours
the market is shut (Fri 21:00Z → Sun 21:00Z, with a `SUNDAY_THIN_FACTOR = 0.55` on the reopen),
so a Friday-to-Monday window contains barely more tradeable variance than a Tuesday night —
while charging **2.583 calendar days** of theta.

**Events.** `var_fraction` as a single scalar cannot describe a night containing a BoJ
decision, and a large share of the shipped calendar falls in this window. So the window is
built hour by hour and `EVENT_VAR_UPLIFT = {3: 4.0, 2: 1.5, 1: 0.4}` (average-hour units) is
added to the hour containing each in-window event whose `ccy` is one of the pair's two.
`PassiveWindow` carries `hour_var`, `event_var` and `events` alongside the scalar. These
uplifts are modelled defaults; when `signals/rangeforecast.RangeForecast` is available its
`sigma_window` overrides all of this and is preferred, since it already contains the event
term as a measurement.

### 3.3 `estimate_hour_profile` — does it recover a known profile?

Synthesised two years of hourly bars from the EURUSD default profile (weekends removed,
σ = 8%), then re-estimated with `blend_default=False`:

| n obs | span | corr(est, truth) | RMS relative error | window share, truth | window share, estimated |
|---|---|---|---|---|---|
| 12,759 | 731 days | **0.9933** | **8.2%** | 0.3393 | **0.3449** (+1.6%) |

At the volume Yahoo actually serves, the estimator recovers the window's variance share to
under 2%. The per-hour error is 8% RMS, which is why `smooth=True` (circular 1-2-1) and
`blend_default=True` (shrink to the shipped profile with weight `min_obs/(min_obs + n_h)`) are
both on by default: the profile is a smooth diurnal function and the estimator's noise is not.

### 3.4 The crossover vol — the go/no-go number

The window's gamma P&L on a forecast range of standard deviation `sd` is `0.5·|Γ(σ)|·sd²`; the
bill is `|θ(σ)|·D_cal`. Gamma falls and theta rises with implied, so the two cross once.
`crossover_vol()` solves it by **bisection on a parallel vol shift with a full book
repricing** (rates, skew, several expiries, premium-adjusted deltas and all). The closed form,
for intuition only:

```
σ* = σ_window · √(365 / D_cal)
```

and if the range forecast is just implied put through the session clock,
`σ_window = σ√(vf/252)`, so

```
σ*/σ = √( 365·vf / (252·D_cal) )      ← a pure calendar fact, book-independent
```

| window | closed form `σ*/σ` | measured (full reprice) | reading |
|---|---|---|---|
| EURUSD weeknight | 0.918 | **0.915** (7.28% vs 7.96% marked) | needs realised **+9.3%** over implied |
| USDJPY weeknight | 1.001 | **0.993** (9.77% vs 9.84%) | essentially break-even — the Tokyo fix pays for the night |
| EURUSD weekend | 0.424 | **0.413** (3.29% vs 7.96%) | needs realised **2.4× implied** |

That is the honest headline: on EURUSD, holding front gamma through an ordinary night is
negative carry; on USDJPY it is roughly free; over a weekend it is expensive on any pair.
**The ladder does not change this** — see §7 — which is why `ladder_summary` reports the carry
decision (`gamma_pnl`, `theta`, `carry`, `crossover_vol`) *separately* from what the orders buy.

---

## 4. The band models

### 4.1 Whalley–Wilmott (1997)

Exponential utility, proportional costs, asymptotic as `λ → 0`. The no-transaction band around
the Black–Scholes delta is

```
H_WW = ( (3/2) · e^{−r_d τ} · λ · S · Γ² / γ )^{1/3}          [base ccy]
```

Derivation, so the constant is not a magic number. WW's policy is **singular control: on
touching the boundary, trade back to the boundary**. For a reflected process on `[−H, H]` with
variance rate `v = (ΓσS)²`, the stationary density is uniform, so
`E[e²] = H²/3` and the boundary local time rate is `v/(2H)` (total, both boundaries). Cost rate
`= λS·v/(2H)`; risk penalty rate `= (γ/2)·(H²/3)·σ²S²`. Minimising their sum:

```
−λS(ΓσS)²/(2H²) + (γ/3)Hσ²S² = 0   ⟹   H³ = (3/2)·λSΓ²/γ
```

`σ` cancels identically. Discounting `e^{−r_d τ}` because the cost is paid now and the risk is
terminal.

**Where it breaks.** (i) `σ` cancelling is an artefact of `λ → 0`. (ii) The expansion assumes
`H` is small relative to the option's own delta scale; the diagnostic is
`asymptotic_ratio = h / (Sσ√τ)`, and at retail cost on EURUSD it is ~1.0 — the band is one
horizon-sigma wide and the asymptotics are out of their regime. (iii) `γ` is unobservable.
(iv) **The policy is not the policy we run**, which is the next point and the biggest one.

### 4.2 The rebalance-policy constant — the largest single correction

`fxgamma.portfolio.hedging.hedge_suggestion`, `backtest/engine.py` and a resting-order ladder
all **rebalance back to target**, not to the band edge. Redo §4.1 for that policy: hitting time
from centre to `±H` is `H²/v`, each hit trades `H`, so turnover rate is `v/H` (twice WW's), and
the occupation density is triangular so `E[e²] = H²/6` (half WW's). Result:

```
H³ = 6 · λ S Γ² / γ                          POLICY_CONST = {"edge": 1.5, "center": 6.0}
```

**The correct band for a hedge-to-flat desk is `4^{1/3} = 1.587×` wider than the textbook WW
band.** This is not a refinement; it is most of the WW-vs-empirical gap in §5. A fixed grid of
spacing `h` is the *same* object as a hedge-to-target band of half-width `h` in spot — which is
why the overnight ladder is a `center`-policy problem, not an `edge`-policy one.

### 4.3 Zakamouline-form (the default)

Three corrections, in the order they matter here.

1. **Policy constant 6, not 3/2** (§4.2). Worth 1.587×.
2. **Volatility, via a Leland-modified vol.** Leland's number at rehedge interval `dt` is
   `Le = √(8/π)·λ/(σ√dt)`, and `σ̂² = σ²(1 − sign(Γ)·Le)`: a long-gamma book cannot afford to
   hedge at the full implied once costs are charged and so behaves like a smaller-gamma book
   (band widens); a short-gamma book must hedge as if vol were higher (band tightens). For an
   approximately at-the-money book `Γ ∝ 1/(Sσ√T)`, so `Γ̂ = Γ·σ/σ̂`. `Le` is evaluated at the
   rehedge interval the band itself implies, making this a fixed point; the map is a
   contraction for `Le < 1` and three iterations converge. `Le ≥ 0.9` is clamped and flagged —
   at that point the cost of rehedging exceeds the entire option variance and no band is
   economic.
3. **A floor at the round-trip breakeven.** A rehedge triggered by a move `h` captures
   `|Γ|h²/2` and pays `λS|Γ|h`, so it loses money for `h < 2λS` **regardless of risk
   aversion**. Blended as a smooth cube, `H = (H_util³ + H_be³)^{1/3}`.

**Honesty note on attribution.** Zakamouline's published approximation has the structure used
here — modified volatility for the hedge, a bandwidth term that does not vanish with gamma, and
policy-consistent constants — but his additive term is calibrated to a model with **fixed**
transaction costs. Our cost model has no fixed component, so the additive term implemented here
is **derived from the round-trip breakeven rather than taken from his calibration**. It is
labelled `"zakamouline"` because it is his structure; the constant `2λS` is ours and is derived
above. `BandResult.note` says so at runtime.

Breakeven floor magnitudes: EURUSD 0.23 pips (never binds), NZDUSD 0.48, USDSEK 28.05 pips
(binds against any tight band).

### 4.4 Fixed grid / Leland comparator

The desk rule of thumb: rehedge on a grid of one `n`-day sigma, `h = S·σ·√dt`. It ignores cost,
gamma and risk aversion entirely. Reported with its Leland number and the vol drag
`50·Le·σ` in vol points, so the cost of the rule is visible. It exists to be beaten, and §5
shows by how much.

### 4.5 The empirical referee

`method="empirical"` sweeps `HedgeRule(mode="band", band_delta=H)` through `backtest.engine`.
Four design choices make it a referee rather than a second opinion:

* **Common random numbers** — every band walks the same paths, so the band-independent P&L,
  which dominates the variance by two orders of magnitude, differences away.
* **The position is held, not rolled**, with expiry ≥ 4× the horizon, so gamma is roughly
  constant. Otherwise the sweep optimises a band for an average of several gammas.
* **The straddle is sized to the book's own gamma**, so it is a band for *this* book.
* **The objective imposes `E[discretisation error] = 0` instead of estimating it.** This is the
  important one. Realised P&L is `capture − cost(h) + e(h)` with `e` mean-zero and standard
  deviation in the tens of thousands of dollars; resolving a $200 cost difference through the
  sample mean of `e` would need order `1e5` paths. So the objective is
  `U(h) = capture − E[cost(h)] − (γ/2)·Var[e(h)]`, with `E[cost]` and `Var[e]` both measured
  (a cost is nearly deterministic; a variance is far cheaper to estimate than a mean).
  The zero-mean assumption is then **tested, not assumed**: `mean_resid`/`se_resid` in the
  curve must straddle zero. Worst |z| across all four validation runs was **1.92** — consistent
  with zero. If it were not, the number would not be trustworthy and the note says so.

Residual known bias: monitoring is discrete (24 steps/day), so a band is breached with
overshoot and measured cost sits ~25–30% below the continuous-monitoring analytic. Measured
cost converges towards the analytic as steps/day rises (EURUSD, 15-pip band, 10 days: 349 →
484 → 551 for 8 → 24 → 48 steps/day against an analytic 793) and the **argmax does not move**
(54.8 pips at all three). That is the right invariance: overshoot inflates the *effective*
band uniformly.

---

## 5. Validation: analytic vs empirical

10-trading-day horizon, `risk_aversion = 1e-6` per USD, 64 CRN paths × 24 steps/day, ATM
straddle EUR/USD 10mm per leg struck at spot, 30-day expiry. Synthetic provider, `asof`
2026-09-09.

| pair | cost tier | `cost_bp` | WW (pips) | **Zakamouline** | fixed grid | **empirical argmax** | zak/emp | ww/emp | grid/emp |
|---|---|---|---|---|---|---|---|---|---|
| EURUSD | interbank | 0.2 | 38.8 | **61.6** | 58.4 | **56.0** | **1.10** | 0.69 | 1.04 |
| EURUSD | retail | 5.0 | 113.4 | **181.5** | 58.4 | **231.1** | **0.79** | 0.49 | 0.25 |
| USDJPY | interbank | 0.3 | 63.5 | **101.0** | 91.4 | **82.1** | **1.23** | 0.77 | 1.11 |
| USDJPY | retail | 5.0 | 162.3 | **259.6** | 91.4 | **321.2** | **0.81** | 0.51 | 0.29 |

Zero-mean residual test, worst |z|: 1.24 / 1.92 / 1.87 / 1.57. All pass.

**Utility give-up**, every band scored on one common analytic utility curve, as a % of the
position's gamma P&L over the horizon (`compare_bands(...).utility_giveup_pct`):

| pair / tier | Whalley–Wilmott | Zakamouline | fixed grid |
|---|---|---|---|
| EURUSD interbank | 0.109% | 0 | 0.002% |
| EURUSD retail | 0.928% | 0 | **5.287%** |
| USDJPY interbank | 0.126% | 0 | 0.006% |
| USDJPY retail | 0.819% | 0 | **3.955%** |

### Reading the table honestly

* **Whalley–Wilmott is 23–51% too tight.** At interbank cost the gap is 0.69–0.77, and
  `4^{1/3} = 1.587` applied to WW gives 1.10 and 1.23 — i.e. **the policy constant explains the
  whole gap and slightly overshoots**. This is a real, identified, correctable error, not
  tuning noise.
* **Zakamouline goes from slightly wide (1.10–1.23) to slightly tight (0.79–0.81) as cost
  rises.** The reason is stated, not fitted around: at retail cost `asymptotic_ratio ≈ 1.0`,
  the band is a full horizon-sigma wide, and the `λ → 0` expansion the whole family is built on
  is out of its regime. There is no way to fix that inside an asymptotic band formula, and I
  have not tried to; the fix is to use the empirical sweep when
  `asymptotic_ratio > 0.5`, which `BandResult` reports on every call.
* **The objective is extremely flat near its maximum.** Being 30% off in band width costs
  ~0.1% of the gamma P&L at interbank cost and ~0.9% at retail. This is the single most
  important practical fact in this section: it is why the third significant figure of a band
  does not matter, and why the delta cap (§6) is a much bigger decision than the band.
* **The fixed grid is fine when costs are tiny and catastrophic when they are not** — 4–5% of
  the gamma P&L given up at retail cost, because a one-day-sigma grid rehedges far too often
  when each rehedge costs 5.8 pips round trip.

**Not tested here, and it matters:** every number above is on GBM with constant vol.
Real spot has jumps, intraday vol seasonality (§3, which the band formula ignores entirely) and
mean reversion at some horizons — all of which move the optimum. The synthetic validation
proves the *maths and the code* are right; it does not prove the band is right for the market.

---

## 6. Cost, and why it owns the answer

`zones.COST_BP` is an **interbank** table. This user has no OTC prime relationship; the trader
review puts their real all-in cost at 15–40× it. `bandopt.RETAIL_COST_BP` is the shipped retail
default (EURUSD 5.0bp = 5.8 pips round trip, USDJPY 5.0bp, USDSEK 25bp) and `cost_tier="retail"`
is the default everywhere in these two modules. It is an assumption, not a measurement:
`cost_bp` is an explicit argument on every entry point and should carry the user's own broker
number.

Sensitivity has two very different shapes:

* **The band scales as `cost^{1/3}`** — a 25× cost error is a 2.9× band error. Slow.
* **The cost line is linear in it** — and it is the cost line that decides whether the ladder
  is worth running at all.

EURUSD reference ladder, 0.5mm delta cap, 4 rungs/side, one night (`ladder_cost_sensitivity`):

| `cost_bp` | round trip (pips) | conversion (USD) | cost (USD) | night net |
|---|---|---|---|---|
| 0.2 (interbank) | 0.23 | 1,109 | 15 | −350 |
| 1.0 | 1.17 | 1,109 | 77 | −412 |
| 2.5 | 2.91 | 1,109 | 193 | −528 |
| **5.0 (retail default)** | 5.83 | 1,109 | **386** | **−721** |
| 10.0 | 11.65 | 1,109 | 772 | −1,107 |
| 20.0 | 23.30 | 1,109 | 1,545 | −1,879 |

At about **14bp round trip the ladder's cost exceeds everything it converts** and the orders are
pure value destruction on this spacing. That is the number to check against the user's actual
broker before shipping any of this.

---

## 7. The ladder

### 7.1 What the rungs are

Rungs sit at `spot ± k·h`. At each rung the clip is the **delta increment read off a full
repricing** of the book (`risk.spot_ladder`), never `Γ₁ × spacing`:

```
clip_k = | delta(L_k) − delta(L_{k−1}) |
```

so the book returns to the target delta at every rung — `cum_delta_base` is 0 at all eight rungs
in the worked example below, which is the "sensibly hedged at each rung, not overhedged at the
first" test. On a symmetric ATM straddle the linear approximation is only ~2% out at 1W; on a
**skewed** book it is not, and a skewed book is exactly what this feature is for. When a rung is
snapped to a technical level, the delta is re-read **at the moved level** — reusing the unsnapped
clip would leave the cumulative delta wrong there and at every rung beyond it.

`p_touch` is the GBM first-passage probability under the forward drift
(`zones.touch_probability`). `exp_crossings` is `E[L(x)]/h` from §2 — the expected number of
**fills**, which is not a probability and is not bounded by 1: the nearest rung of a tight
ladder fills 0.8–0.9 times on an ordinary night and several times on a choppy one. `p_touch`
tells you whether you get filled once; `exp_crossings` tells you how often, and that is what
determines what the rung is worth.

### 7.2 Spacing, in priority order

1. caller-supplied `band_pips`, else
2. `bandopt.optimal_band` at the chosen method over *this window's* variance, at the user's cost,
3. **clamped above by `max_overnight_delta / |Γ|`** — the delta cap wins over the optimiser,
4. **floored below by `min_clip_base / |Γ|`** (default one standard lot, `RETAIL_LOT_BASE`) —
   a clip you cannot deal is not a band.

Which constraint bound is recorded in `ladder_summary()["spacing_source"]`. On the reference
book the answer is **always the cap**, and that is the right answer: at retail cost the
analytic optimum is 181 pips = 5.3 overnight sigmas, so the unconstrained ladder would never
fill.

### 7.3 Worked ladder — EURUSD long straddle

Book: EUR/USD 10mm per leg, ATM 1.1650, 30 days. Spot 1.1650, ATM 7.96%. Long gamma **3.48mm
per 1%**. Window: London close → open, `var_fraction` 0.3393, 0.5833 calendar days,
**1σ = 34 pips**, breakeven move 37 pips. Delta cap 0.5mm, 4 rungs/side, retail 5bp.

```
CARRY   gamma +1,732 + theta −2,067 = −335 USD    crossover ATM 7.28% vs 7.96% marked
LIMIT orders, spacing 17 pips  [DELTA CAP 0.50mm (16.7 pips) over zakamouline optimum 181.5 pips]
  SELL 0.48mm EUR at 1.1717  (+67p, 1.97σ, touch  5%, E[fills] 0.04, banks   +10)
  SELL 0.49mm EUR at 1.1700  (+50p, 1.47σ, touch 14%, E[fills] 0.13, banks   +33)
  SELL 0.49mm EUR at 1.1683  (+33p, 0.98σ, touch 33%, E[fills] 0.35, banks   +94)
  SELL 0.50mm EUR at 1.1667  (+17p, 0.49σ, touch 63%, E[fills] 0.82, banks  +221)
  BUY  0.50mm EUR at 1.1633  (−17p, 0.49σ, touch 62%, E[fills] 0.82, banks  +223)
  BUY  0.50mm EUR at 1.1617  (−33p, 0.98σ, touch 32%, E[fills] 0.35, banks   +96)
  BUY  0.50mm EUR at 1.1600  (−50p, 1.47σ, touch 14%, E[fills] 0.13, banks   +35)
  BUY  0.50mm EUR at 1.1583  (−67p, 1.97σ, touch  5%, E[fills] 0.04, banks   +10)
```

| line | USD |
|---|---|
| position's expected gamma P&L over the window (`0.5·Γ·V`) | **+1,732** |
| theta, pro-rata on 0.5833 **calendar** days | **−2,067** |
| **carry (hold-or-not; nothing to do with the ladder)** | **−335** |
| of the 1,732, converted to cash by the ladder | 1,109 (**64%**) |
| ladder transaction cost | −386 |
| **ladder's effect on the EXPECTED P&L** | **−386** (exactly minus the cost) |
| overnight P&L standard deviation, no ladder → with ladder | **−72%** |
| night, all in | **−721** |
| 3σ gap scenario (102 pips), full reprice + fills | +3,356 up / +3,507 down |

**How to read this.** The `−335` is the decision: this position is negative carry overnight and
it would be negative carry if you left no orders at all. The ladder's contribution to the mean
is `−386`. What you get for that 386 is 1,109 of mark-to-market turned into cash you keep even
if spot round-trips, and a 72% cut in the standard deviation of the overnight outcome. That is
a risk-control trade, and it is worth doing on those grounds — but calling the 1,109 "capture"
would be a lie, because it is money the position already owned.

**Conversion identity check:** `Σ exp_realised = 1,109.0` against `0.5·Γ·V = 1,731.8`, i.e. a
finite 4-rung ladder reaches 64.0% of the theoretical maximum. The share rises towards 1 as
rungs are added and the spacing tightens; it can never exceed 1, which is the arithmetic proof
that the ladder converts rather than creates.

### 7.4 Worked ladder — USDJPY

Same structure, USD 10mm per leg ATM 147.50. Long gamma 2.81mm per 1%. `var_fraction` **0.4032**
(Tokyo fix), 1σ = 58 pips, breakeven 58 pips. Spacing 26 pips (cap binds over a 259.6-pip
optimum).

| line | JPY |
|---|---|
| gamma P&L | +321,344 |
| theta (0.5833 cal days) | −325,509 |
| **carry** | **−4,165** — essentially flat; crossover 9.77% vs 9.84% marked |
| converted by the ladder | 213,167 (**66%**) |
| cost | −60,005 |
| sd reduction | **−74%** |
| 3σ gap (174 pips) | +1,097,129 up / +282,139 down |

USDJPY is the interesting case: its overnight window carries 40% of the day's variance against
58% of the theta, and the two nearly cancel. **On this pair the night is close to free; on
EURUSD and GBPUSD it is not.** That difference comes entirely from the hour profile, which is
why §3's provenance warning matters.

---

## 8. Short gamma — a different object, framed differently

For a short-gamma book the ladder inverts: above spot delta *falls*, so you must **buy** above
the market, and below you must **sell**. Those are **stop** orders, not limits. Everything
about the framing changes:

* The expected gamma term is **negative** (`0.5ΓV < 0`) and the theta is the entire edge. The
  carry line flips sign; the crossover vol is read the other way (you want implied *above* it).
* Stops fill **at or beyond** your level, never at it — and in the gap you are actually worried
  about, nowhere near it. Every number in the ladder assumes a fill at the level, so every
  number is optimistic by construction. `_gap_scenario` explicitly does **not** model slippage
  through a gap and says so, rather than producing a comforting number.
* The loss beyond the last rung is unbounded. `delta_beyond_last_rung` is reported for exactly
  this reason.
* On the mirror-image EURUSD book: carry **+335**, ladder cost **386**, 3σ gap **−3,356 / −3,507**.
  The expectation is positive and the tail is the trade. Sizing off the expectation is how
  short-gamma books die.

`ladder_summary` returns `gamma_side="short"` and puts the warning first. It will build the
ladder; it will not present it as an income strategy.

---

## 9. What to actually do — plain language

**The band you should use.** Zakamouline, at *your* broker's cost, capped by the delta you are
willing to wake up holding. Concretely, on the reference EUR/USD 10mm straddle:

* **Intraday, hedging by hand during London:** the analytic band is the right object. At
  interbank-like cost that is **~60 pips** on EURUSD (~1.05 daily sigmas) and **~100 pips** on
  USDJPY. At a realistic retail 5bp it is **~180 pips** and **~260 pips** — which is telling you
  something true and uncomfortable: *at retail spreads, gamma scalping EURUSD in 30-pip
  clips does not pay*. Widen or do not do it.
* **Overnight:** ignore the analytic band. **Set `max_overnight_delta` to the delta you would
  be unhappy to find on your screen at 07:00, and let it bind.** On the reference book, 0.5mm
  gives a 17-pip ladder that converts 64% of the night's gamma and cuts the overnight standard
  deviation by 72% for 386 USD. 2.0mm gives a 67-pip ladder that converts 7%, cuts the standard
  deviation by 0%, and is decoration — because an unhedged book only accumulates 1.0mm of delta
  at one overnight sigma anyway, so a 2mm cap never binds.
* **Never ask yourself for a risk-aversion coefficient.** Nobody can introspect one.
  `bandopt.risk_aversion_for_band()` goes the other way: give it your delta cap and it returns
  the coefficient consistent with it, for anything downstream that needs one.

**Before you leave the orders, look at three numbers, in this order.**

1. **Crossover vol.** Is the marked ATM above or below it? Above = you are paying to hold the
   gamma tonight. That is a position decision, not a ladder decision, and the answer may be
   "sell the front gamma into the close" rather than "leave better orders".
2. **Theta paid per day of variance.** 1.7× on EURUSD on a weeknight, **8×** over a weekend.
   Friday nights are a different trade from Tuesday nights and should be sized as one.
3. **Your clip against the 1σ delta.** If the clip you are leaving is bigger than the delta the
   book accumulates at one overnight sigma, the ladder will not fill and is not doing anything.

**What not to believe.**

* Do not read "expected capture" as profit. Under driftless spot the ladder's effect on your
  expected P&L is minus its cost, full stop. It buys conversion and variance reduction, which
  are worth having on their own terms.
* Do not believe the hour profile to three figures. It is modelled, not measured. Run
  `estimate_hour_profile` on two years of Yahoo hourly bars on your own machine and the numbers
  in §3 will move — probably by less than 10% on the window share, but they will move.
* Do not believe the touch probabilities in a gap. They are GBM.
* Do not use `zones.COST_BP` for your own ladder. It is an interbank table.

---

## 10. Open items, contract notes, and things I think are wrong

**RFC-1 — `risk_aversion` units are inconsistent across the codebase.**
`zones.hedge_bands` documents `risk_aversion` as 1/quote-ccy; `bandopt.optimal_band` takes it in
1/report-ccy and converts. Both are defensible in isolation; having both is not. A EURUSD and a
USDJPY band computed from the same number mean different things today. Recommend the
report-ccy convention (it is the only one that makes cross-pair bands comparable) and a
one-line change in `zones.py`. Not my file; flagged, not touched.

**RFC-2 — `zones.hedge_bands.band_ww_base` uses the edge constant (3/2) while the whole repo
hedges to target.** By §4.2 the indicative band it prints is `1.587×` too tight for the policy
the tool actually runs. One-line fix (`1.5 → 6.0`, or expose `POLICY_CONST`). Not my file.

**Recorded, no action requested — the backtest engine's two clocks disagree.**
`synthetic_path` advances the index by one **calendar** day per step but steps variance at
`dt = 1/252`, so a 252-step "year" delivers 1.45 years of variance against 1 year of theta. It
does not affect any band argmax (both terms are band-independent) and it did not affect §5. It
does inflate the reference straddle's P&L on the Lab page by ~45%, which someone will
eventually read as an edge.

**Where the brief is wrong.** `docs/08` §1's worked example should not become a fixture: it
charges a full day's theta against a 14-hour window (the correct pro-rata figure is ~58% of it,
the same error as W-14), its cost implies ~1.7 pips all-in against a 0.12-pip interbank
assumption, and it labels the ladder's contribution "capture" when the ladder's effect on
expected P&L is minus its cost. The PM has already ruled on all three; recorded here so the
example is not quietly reused.

**Additive fields on the frozen types.** `LadderRung` gains `pair, k, order_type,
spacing_pips, sigma_dist, exp_crossings, exp_realised, exp_cost, exp_marginal, delta_at_level,
ccy, cost_bp, fx_to_report, note`; `BandResult` gains `pair, band_spot, band_pct,
band_sigma_days, exp_var, exp_sd, gamma, gamma_1pct, sigma, horizon_days, cost_bp, lam,
risk_aversion, risk_aversion_quote, policy, ccy, report_ccy, fx_to_report, leland,
vol_drag_pts, breakeven_pips, max_delta, cap_binds, implied_risk_aversion, exp_marginal,
cost_tier, asymptotic_ratio, diagnostics`. All appended with defaults, so the frozen positional
signatures in `docs/08` §3 still construct. No frozen field changed name, type or meaning.
`PassiveWindow` gains `clock_hours, calendar_days, open_hours, tz, profile_source,
spans_weekend, note, hour_var, event_var, events`.

**Still open.**

* Nothing here has touched real hourly data. The whole session-variance layer is a modelled
  default until `estimate_hour_profile` is run on the user's machine, and every consumer must
  badge it. This is the single largest source of error in the ladder, because every distance,
  probability and crossing count scales with it.
* The band models are all GBM. Jumps, intraday vol seasonality and the fact that the overnight
  distribution is *not* the day distribution scaled are unmodelled. The session weight fixes
  the second moment; it does nothing for the shape.
* `snap=True` remains unmeasured and therefore **off by default**, pending
  `signals/levels.measure_reversal_stats` and its random-level control (PM steer, `docs/08` §2).
  The mechanics are implemented, tested for delta consistency, and will stay off until the
  measurement justifies them.
* `asymptotic_ratio > 0.5` should probably force a fallback to the empirical sweep rather than
  a warning. Left as a warning for now because the sweep takes ~70s and the ladder is
  interactive.
