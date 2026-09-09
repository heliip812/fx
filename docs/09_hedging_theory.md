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
3. **Overnight is usually negative carry.** You pay 58% of a day's theta for 35–50% of a day's
   variance. On the calibrated EURUSD profile the window carries **0.382** of a day's variance
   against 0.583 of the clock — **1.53× the theta per day of variance** — and needs realised vol
   **~3% above implied** to break even; over a weekend it needs **2.2× implied**. The
   `crossover_vol` number is the go/no-go. §3.
4. **The delta cap, not the band, is the decision worth money.** At the user's real (retail)
   cost the analytically optimal EURUSD band is ~180 pips — five overnight sigmas. It is
   unusable as an overnight ladder. The cap binds and it *should*. §6, §9.
5. **Analytic vs empirical:** Zakamouline lands within **10–23%** of the backtester's argmax at
   interbank cost and **19–21%** at retail cost; Whalley–Wilmott is **23–51% too tight**
   everywhere, and most of that gap is one identified, correctable constant. §5.
6. **Every band model here assumes independent increments**, so it answers the random-walk
   case and nothing else. If spot trends, a wider band captures *more*; if it chops, *less*.
   `optimal_band` is therefore asymmetric- and persistence-capable, but the persistence
   response has to be clamped hard and the real answer is a state-dependent ratchet, not a
   static multiplier. §4.6.

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
bill. Conflating them is not a rounding issue: on the calibrated EURUSD profile the correct
weeknight crossover needs realised vol **+3%** over implied; running both legs on the same clock
turns that into **+24%**, i.e. it would tell you to go flat every night.

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

EURUSD default, after the day/night calibration of §3.2 (variance weight by UTC hour,
average = 1.00):

```
h   00   01   02   03   04   05   06   07   08   09   10   11
   0.59 0.54 0.49 0.43 0.43 0.49 0.58 1.21 1.57 1.52 1.30 1.17
h   12   13   14   15   16   17   18   19   20   21   22   23
   1.61 1.97 2.06 1.84 1.73 1.19 0.86 0.70 0.59 0.43 0.32 0.38
```

Peak/trough 6.37 (GBPUSD 6.68, USDJPY 5.11, AUDUSD 3.32).

### 3.2 What the overnight window is worth

London close 17:00 → open 07:00, `asof` in BST so 16:00Z → 06:00Z:

| pair | clock share (= **theta** share) | variance share `var_fraction` | theta paid per day of variance | sigma multiplier | weekend `var_fraction` (2.583 cal days) | weekend ratio |
|---|---|---|---|---|---|---|
| EURUSD | 0.583 | **0.3820** | **1.53×** | 1.236 | 0.3607 | 7.16× |
| USDJPY | 0.583 | **0.4484** | **1.30×** | 1.141 | 0.4227 | 6.11× |
| GBPUSD | 0.583 | **0.3497** | **1.67×** | 1.291 | 0.3307 | 7.81× |
| AUDUSD | 0.583 | **0.4950** | **1.18×** | 1.086 | 0.4616 | 5.60× |

14/24 = 58% is wrong by 1.53× in variance (1.236× in sigma) on EURUSD. A ladder built on clock
time puts every rung 24% too far out and understates every touch probability.

**Calibration and provenance of the level, as distinct from the shape.** The hour-by-hour
*shape* above is modelled. The day/night *level* is anchored on the one measurement available:
the forecasting quant's estimator puts the EURUSD window at **0.382** of a day's variance
(against 0.583 of the clock — ratio **1.53**, sigma multiplier **1.236**). The raw modelled
shape gave 0.339, i.e. it made the night too quiet. `NIGHT_CALIBRATION = 1.2033` scales the
night hours of *every* shipped profile and renormalises, which reproduces 0.382 exactly on
EURUSD and moves the other pairs consistently while leaving the relative pair tilts as modelled
as they were. So: **the EURUSD level is measured; every pair tilt and every other pair's level
is not.** A user-supplied or `estimate_hour_profile` profile is used verbatim — the calibration
factor applies only to the shipped defaults.

*(An earlier PM note quoted 0.34 / 1.72 for this row; 0.583/0.382 = 1.53 and the forecaster's own
sigma multiplier of 1.236 is consistent with 1.53, so 1.53 is what the code and this document
use. The qualitative conclusion is unchanged and still leads the screen.)*

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
| 12,759 | 731 days | **0.9912** | **8.5%** | 0.3820 | **0.3832** (+0.3%) |

At the volume Yahoo actually serves, the estimator recovers the window's variance share to
better than 1%. The per-hour error is 8% RMS, which is why `smooth=True` (circular 1-2-1) and
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
| EURUSD weeknight | 0.974 | **0.971** (7.73% vs 7.96% marked) | needs realised **+3.0%** over implied |
| USDJPY weeknight | 1.055 | **1.048** (10.31% vs 9.84%) | **positive** carry — the Tokyo fix pays for the night |
| EURUSD weekend | 0.449 | **0.437** (3.48% vs 7.96%) | needs realised **2.2× implied** |

That is the honest headline: on EURUSD, holding front gamma through an ordinary night is
marginally negative carry; on USDJPY, on this profile, it is marginally *positive*; over a
weekend it is expensive on any pair. Note how close EURUSD and USDJPY sit to the line — the
sign of the weeknight answer is decided by the pair tilt in the hour profile, which is the part
that is **not** measured. Treat "positive on USDJPY" as a hypothesis to test with
`estimate_hour_profile` on real bars, not as a result.
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

### 4.5 What all of them assume, and what the user noticed

Whalley–Wilmott, Zakamouline and the Leland grid are all derived for a **driftless diffusion
with independent increments**. That is not a technical footnote here — it is the assumption that
makes §2's "expected capture is band-independent" result true, and therefore the assumption that
makes the whole cost-versus-variance framing the *only* thing a band trades off.

The user's question — *"if it keeps going one direction you want to optimise the level
dynamically so that we don't hedge too early"* — is precisely a question about that assumption,
and it is not a directional call. The gamma kernel is the sum of **squared moves between
hedges**, and squares are not additive: `(a+b)² > a² + b²` for same-sign legs and
`(a+b)² < a² + b²` for opposite-sign ones. +25 then +25 pips captures 2,500 hedged once against
1,250 hedged each leg; +25 then −25 captures 0 hedged once against 1,250 hedged each. So under
serial correlation the band *does* move expected capture.

PM simulation, 4,000 AR(1) paths per cell, 240 steps, **variance equalised across φ** so only
persistence differs. Captured sum of squared moves:

| band | trending φ=+0.3 | random walk | choppy φ=−0.3 |
|---|---|---|---|
| 0.25 | 240.6 | 240.0 | 239.4 |
| 1.00 | 261.5 | 240.1 | 215.8 |
| 2.00 | 302.8 | 239.4 | 173.9 |
| 4.00 | 348.1 | 237.0 | 148.4 |

The **random-walk column is flat**, which reproduces §2 and validates the simulation. The other
two are not: **+45% captured at a wide band when trending, −38% when choppy**, and the optimal
band is monotone in φ.

### 4.6 The persistence correction, and why it must be clamped

Parameterise the memory by a Hurst exponent. For a self-similar path with exponent `H`,
traversing `h` takes time `~ h^{1/H}`, so over a fixed window the number of rebalances is
`~ h^{−1/H}` and each is worth `Γh²/2`:

```
E[capture](h)  ~  (Γ/2) · h^{e},      e = 2 − 1/H
```

`H = 1/2` gives `e = 0` and the textbook band-independence; `H > 1/2` (trending) gives `e > 0`;
`H < 1/2` (choppy) gives `e < 0`. Fitting the table above to a power law in the band gives
`e = +0.136` at φ=+0.3 and `e = −0.169` at φ=−0.3, i.e. **H = 0.536 and H = 0.461**, so
`(H − 0.5)/φ` is 0.121 and 0.130. `HURST_PER_PHI = 0.125` is the round number between them, and
the fit reproduces the measured capture ratios to a few per cent across a 16× range of bands.

The objective, normalised so the multiplier is 1 at the Brownian optimum `h₀`:

```
U(h) = (ΓV/2)(h/h₀)^e  −  λS|Γ|V/h  −  (γ/12)Γ²h²V
```

`V` cancels out of `dU/dh = 0`, which `persistence_adjusted_band()` solves by bisection.

**The raw solution is violent and must not be shipped as-is.** On the reference EURUSD book at
interbank cost it returns **×6.97** at φ=+0.30 and **×0.03** at φ=−0.30. That is the model
outside its range, not a result: capture dominates cost and risk by two orders of magnitude, so
any `e > 0` pushes the band out until the variance penalty finally catches it — which assumes
AR(1) memory still operates at *seven times* the Brownian band. It does not; an AR(1)'s memory
dies after a few steps, and the power law was fitted over a 16× range of bands, not a 400× one.

So the applied multiplier is clamped into `[min(1, √R), max(1, √R)]` with
`R = (1+φ)/(1−φ)`, the AR(1) long-run variance ratio (1.86 at φ=+0.3, 0.54 at φ=−0.3, so
`√R` = 1.36 and 0.73). `R` is the most total variance persistence can add relative to a random
walk; its square root is the corresponding **distance** rescaling, and nothing about persistence
justifies moving a distance by more than that. The unclamped figure is returned as
`diagnostics["persistence_mult_raw"]`.

Applied result, EURUSD reference book, one-day horizon:

| φ | H | interbank band | retail band | applied ×| raw × |
|---|---|---|---|---|---|
| −0.30 | 0.463 | 45.2p | 133.2p | 0.734 | 0.017 |
| −0.15 | 0.481 | 53.0p | 156.1p | 0.860 | 0.038 |
| 0.00 | 0.500 | **61.6p** | **181.5p** | 1.000 | 1.000 |
| +0.15 | 0.519 | 71.7p | 211.2p | 1.163 | 4.645 |
| +0.30 | 0.537 | 84.0p | 247.4p | 1.363 | 2.280 |

**That the unclamped answer is absurd is itself the argument for the ratchet.** The right
response to "spot keeps going one way" is a *state-dependent rule* that stops hedging while the
move is still extending, not a static multiplier applied all night in both directions. That
logic belongs in `fxgamma/portfolio/ratchet.py`; `bandopt` deliberately provides only the
symmetric baseline plus the hooks:

* `optimal_band(..., persistence=φ, hurst=H, up_mult=, down_mult=)`,
* `BandResult.band_spot_up / band_spot_down / band_pips_up / band_pips_down /
  band_delta_up / band_delta_down / persistence / hurst / persistence_mult`,
* `BandResult.is_symmetric`,
* and `overnight_ladder(..., persistence=, hurst=, up_mult=, down_mult=)`, which spaces the
  rungs above spot on `band_spot_up` and those below on `band_spot_down`, each independently
  clamped by the delta cap and the clip floor.

Defaults are symmetric and Brownian throughout. **Nothing here estimates φ**, because the
forecasting quant measured that path roughness cannot be forecast from daily bars: it loses to
the Brownian null on 5 of 5 pairs, with daily sampling recovering only 46–63% of the true
crossing count. φ is an input a user with a view can set. It is not a prediction this tool makes.

### 4.7 The empirical referee

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

**These bands answer the random-walk column of §4.5 and nothing else.** Both Whalley–Wilmott
and Zakamouline assume a driftless diffusion with independent increments, and the empirical
referee runs on GBM, which has them by construction. On a trending path the optimal band is
wider than every number in the table; on a choppy one it is tighter (§4.6). The table validates
the maths, not the market.

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
| 0.2 (interbank) | 0.23 | 1,278 | 18 | −135 |
| 1.0 | 1.17 | 1,278 | 89 | −206 |
| 2.5 | 2.91 | 1,278 | 222 | −340 |
| **5.0 (retail default)** | 5.83 | 1,278 | **445** | **−562** |
| 10.0 | 11.65 | 1,278 | 890 | −1,007 |
| 20.0 | 23.30 | 1,278 | 1,779 | −1,897 |

At about **14bp round trip the ladder's cost exceeds everything it converts** and the orders are
pure value destruction on this spacing. That is the number to check against the user's actual
broker before shipping any of this. Note the conversion column does not move: the delta cap sets
the spacing here, so cost changes the bill without changing the ladder.

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

`p_touch` is the **first-passage** probability under the forward drift
(`zones.touch_probability`), i.e. the reflection-principle answer, **not** the terminal
probability — which is roughly half of it and would halve every rung's expected contribution,
worst at the near rungs that do most of the work. Verified against the driftless closed form
`2·N(−d/σ_window)`:

| distance | `touch_probability` | `2N(−d/σ)` | ratio | terminal `N(−d/σ)` |
|---|---|---|---|---|
| 0.25σ | 0.80236 | 0.80259 | 1.0000 | 0.40129 |
| 0.49σ | 0.62394 | 0.62413 | 1.0000 | 0.31207 |
| 0.98σ | 0.32731 | 0.32709 | 1.0007 | 0.16354 |
| 1.97σ | 0.04935 | 0.04884 | 1.0104 | 0.02442 |
| 3.00σ | 0.00281 | 0.00270 | 1.0392 | 0.00135 |

(The small excess further out is the lognormal-plus-drift correction, which is correct — the
implementation is the full GBM first-passage formula, not the driftless approximation.)

`exp_crossings` is `κ·E[L(x)]/h` from §2 — the expected number of **fills**, which is not a
probability and is not bounded by 1: the nearest rung of a tight ladder fills ~0.9 times on an
ordinary night and several times on a choppy one. `p_touch` tells you whether you get filled
once; `exp_crossings` tells you how often, and that is what determines what the rung is worth.

**`κ` (path roughness) defaults to 1.0, the Brownian baseline, and stays there.** A real path
crosses a fine grid more or fewer times than `E[L]/h` for the same terminal variance, and that
ratio is exactly what decides how many times a ladder pays — but the forecasting quant
implemented and verified the crossings/efficiency machinery (identity to 1–5%) and found that
**forecasting κ from daily bars loses to the Brownian null on 5 of 5 pairs**: daily sampling
recovers only 46–63% of the true crossing count. So κ is an explicit override for a user with a
view, never something this tool derives, and `ladder_summary()["fills_basis"]` and the printed
panel both say *"E[fills] assume a Brownian path"*.

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
per 1%**. Window: London close → open, `var_fraction` 0.3820, 0.5833 calendar days,
**1σ = 36 pips**, breakeven move 37 pips. Delta cap 0.5mm, 4 rungs/side, retail 5bp, κ = 1.

```
CARRY   gamma +1,949 + theta −2,067 = −117 USD    crossover ATM 7.73% vs 7.96% marked
E[fills] assume a BROWNIAN path (kappa 1) -- roughness is an input here, not a forecast
LIMIT orders, spacing 17 pips  [DELTA CAP 0.50mm (16.7 pips) over zakamouline optimum 181.5 pips]
  SELL 0.48mm EUR at 1.1717  (+67p, 1.85σ, touch  7%, E[fills] 0.05, banks   +14)
  SELL 0.49mm EUR at 1.1700  (+50p, 1.39σ, touch 17%, E[fills] 0.16, banks   +43)
  SELL 0.49mm EUR at 1.1683  (+33p, 0.93σ, touch 36%, E[fills] 0.41, banks  +111)
  SELL 0.50mm EUR at 1.1667  (+17p, 0.46σ, touch 65%, E[fills] 0.90, banks  +245)
  BUY  0.50mm EUR at 1.1633  (−17p, 0.46σ, touch 64%, E[fills] 0.90, banks  +247)
  BUY  0.50mm EUR at 1.1617  (−33p, 0.93σ, touch 35%, E[fills] 0.41, banks  +113)
  BUY  0.50mm EUR at 1.1600  (−50p, 1.39σ, touch 16%, E[fills] 0.16, banks   +44)
  BUY  0.50mm EUR at 1.1583  (−67p, 1.85σ, touch  6%, E[fills] 0.05, banks   +15)
```

| line | USD |
|---|---|
| position's expected gamma P&L over the window (`0.5·Γ·V`) | **+1,949** |
| theta, pro-rata on 0.5833 **calendar** days | **−2,067** |
| **carry (hold-or-not; nothing to do with the ladder)** | **−117** |
| of the 1,949, converted to cash by the ladder | 1,278 (**66%**) |
| ladder transaction cost | −445 |
| **ladder's effect on the EXPECTED P&L** | **−445** (exactly minus the cost) |
| overnight P&L standard deviation, no ladder → with ladder | **−73%** |
| night, all in | **−562** |
| 3σ gap scenario (108 pips), full reprice + fills | +4,010 up / +4,206 down |

**How to read this.** The `−117` is the decision: this position is (just) negative carry
overnight and it would be negative carry if you left no orders at all. The ladder's contribution
to the mean is `−445`. What you get for that 445 is 1,278 of mark-to-market turned into cash you
keep even if spot round-trips, and a 73% cut in the standard deviation of the overnight outcome.
That is a risk-control trade, and it is worth doing on those grounds — but calling the 1,278
"capture" would be a lie, because it is money the position already owned.

**Conversion identity check:** `Σ exp_realised = 1,277.6` against `0.5·Γ·V = 1,949.4`, i.e. a
finite 4-rung ladder reaches 65.5% of the theoretical maximum. The share rises towards 1 as
rungs are added and the spacing tightens; it can never exceed 1, which is the arithmetic proof
that the ladder converts rather than creates.

### 7.4 Worked ladder — USDJPY

Same structure, USD 10mm per leg ATM 147.50. Long gamma 2.81mm per 1%. `var_fraction` **0.4484**
(Tokyo fix), 1σ = 61 pips, breakeven 58 pips. Spacing 26 pips (cap binds over a 259.6-pip
optimum).

| line | JPY |
|---|---|
| gamma P&L | +357,381 |
| theta (0.5833 cal days) | −325,509 |
| **carry** | **+31,872** — positive; crossover 10.31% vs 9.84% marked |
| converted by the ladder | 241,125 (**67%**) |
| cost | −67,876 |
| sd reduction | **−75%** |
| night, all in | −36,004 |
| 3σ gap (184 pips) | +1,247,481 up / +383,668 down |

USDJPY is the interesting case: its overnight window carries 45% of the day's variance against
58% of the theta, and the two more than cancel. **On this pair the night pays for itself; on
EURUSD and GBPUSD it does not.** The whole of that difference comes from the pair tilt in the
hour profile, which is the part that is *modelled rather than measured* (§3.2). Note also that
the ladder's cost (67,876) exceeds the carry (31,872): the position is worth holding overnight
and the orders are still a net cost. Those are two separate decisions and the summary keeps them
separate.

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
* On the mirror-image EURUSD book: carry **+117**, ladder cost **445**, 3σ gap **−4,010 / −4,206**.
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
  gives a 17-pip ladder that converts 66% of the night's gamma and cuts the overnight standard
  deviation by 73% for 445 USD. 2.0mm gives a 67-pip ladder that converts ~7%, cuts the standard
  deviation by 0%, and is decoration — because an unhedged book only accumulates ~1.1mm of delta
  at one overnight sigma anyway, so a 2mm cap never binds.
* **If you have a view that spot is trending or chopping, say so as a number.** Pass
  `persistence` (AR(1) φ of the increments) or `hurst`, and `up_mult`/`down_mult` for an
  asymmetric ladder. At φ = +0.3 the band widens 36%; at φ = −0.3 it tightens 27% (§4.6). Do not
  expect the tool to infer φ for you — it cannot be forecast from daily bars, and the ladder
  says so on the panel.
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
* Do not read `E[fills]` as a forecast of choppiness. It is the Brownian baseline (`κ = 1`), and
  the panel says so. Roughness is real and it is what decides how many times each rung pays —
  it just cannot be predicted from daily data.
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
ccy, cost_bp, fx_to_report, note`; `LadderRung` also gains `kappa`. `BandResult` gains `pair, band_spot, band_pct,
band_sigma_days, exp_var, exp_sd, gamma, gamma_1pct, sigma, horizon_days, cost_bp, lam,
risk_aversion, risk_aversion_quote, policy, ccy, report_ccy, fx_to_report, leland,
vol_drag_pts, breakeven_pips, band_spot_up, band_spot_down, band_pips_up, band_pips_down,
band_delta_up, band_delta_down, persistence, hurst, persistence_mult, max_delta, cap_binds,
implied_risk_aversion, exp_marginal, cost_tier, asymptotic_ratio, diagnostics`. All appended with defaults, so the frozen positional
signatures in `docs/08` §3 still construct. No frozen field changed name, type or meaning.
`PassiveWindow` gains `clock_hours, calendar_days, open_hours, tz, profile_source,
spans_weekend, note, hour_var, event_var, events`.

**Interface note for `ratchet.py`.** `optimal_band` is asymmetric- and state-capable and is
meant to be the ratchet's symmetric baseline: it accepts `persistence`, `hurst`, `up_mult`,
`down_mult` and returns `band_spot_up`/`band_spot_down` (plus pips and delta forms),
`is_symmetric`, `persistence_mult` and `diagnostics["persistence_mult_raw"]`.
`overnight_ladder` forwards all four and spaces each side on its own half-width, independently
clamped by the delta cap and the clip floor. No ratchet logic lives here, by design.

**Still open.**

* Nothing here has touched real hourly data. The whole session-variance layer is a modelled
  default until `estimate_hour_profile` is run on the user's machine, and every consumer must
  badge it. This is the single largest source of error in the ladder, because every distance,
  probability and crossing count scales with it.
* The band models are all GBM. Jumps, intraday vol seasonality and the fact that the overnight
  distribution is *not* the day distribution scaled are unmodelled. The session weight fixes
  the second moment; it does nothing for the shape.
* `snap=True` stays **off by default** and the measurement now supports that rather than merely
  leaving the question open: `signals/levels.measure_reversal_stats` reports **4 of 80 cells
  significant** after multiple-testing correction, **2 of 80** under an alternative control,
  **zero replication**, and effect sizes of 0.2–0.4 pips. That does not earn a place in the
  ladder economics. Caveat both ways: it is synthetic data, so it validates the machinery and
  does not settle the market question. The snapping mechanics are implemented and the clip is
  re-read at the moved level; the flag stays off.
* `asymptotic_ratio > 0.5` should probably force a fallback to the empirical sweep rather than
  a warning. Left as a warning for now because the sweep takes ~70s and the ladder is
  interactive.
