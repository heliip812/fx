# Forecast & Level Evaluation — what was measured, and the verdict on each piece

**Owner:** quant-signals. **Files:** `fxgamma/signals/rangeforecast.py`, `fxgamma/signals/levels.py`.
**Provider:** `get_provider("synthetic")`, seed 20260101, 1500 business days per pair, asof 2026-09-09.
**Pairs:** EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD.

Read §0 before anything else. Then §7 if you only want the verdicts.

---

## 0. The one thing that governs how to read every number here

The synthetic provider generates spot from a **random walk with Ornstein-Uhlenbeck stochastic
volatility**, and it generates each day's high/low from a seeded bridge around that day's close.
It therefore contains, *by construction*:

- vol clustering (so HAR has something to find),
- **no** microstructure, no order-book memory, no support/resistance, no round-number behaviour,
- **no** deviation from Brownian path roughness (returns are independent, so `kappa = 1`),
- an implied vol that is a **fixed linear function of trailing realized vol** plus a constant
  premium (`synthetic.py:312`: `0.45*rv21 + 0.30*rv63 + 0.25*lr + 0.0065`), i.e. implied here
  is a noiseless copy of HAR's own regressors and cannot possibly add information,
- an event calendar that bumps *implied* vol but never touches the realized path.

So this document proves two different kinds of thing, and they must not be confused:

| Kind | What it establishes | Trustworthy here? |
|---|---|---|
| **Machinery** | the estimator recovers a known answer from known data | **Yes** — this is what synthetic data is *for* |
| **Market fact** | levels work / implied adds / paths are rough | **No** — the generator has no such facts in it |

Every "do not use" below that rests on a market fact is provisional and §8 says exactly what the
user must run on their own data to overturn it. Every "use it" rests on machinery.

---

## 1. HAR-RV — the vol forecast

### 1.1 Machinery self-test: can the code recover a known process?

`rangeforecast.simulate_rv` generates log-variance as an OU process with tunable measurement
noise on the observed proxy. Walk-forward, 1500 bars, `min_train=500`, refit every 21.

| proxy noise `sd(log v)` | ceiling `R²_logvar` | HAR `R²_logvar` vs proxy | HAR `R²_logvar` vs **latent** | HAR `R²_OOS(QLIKE)` vs latent |
|---|---|---|---|---|
| 0.00 (noiseless) | 1.000 | **0.975** | 0.975 | — |
| 0.80 | 0.153 | 0.082 | **0.583** | 0.859 |
| 1.15 (= this feed) | 0.081 | 0.021 | **0.391** | 0.834 |

Read the last row. Against a proxy that is 92% measurement noise, HAR's R² *against the proxy*
is 0.021 — and against the **latent truth it is 0.391**, while the random-walk benchmark scores
−9.39. The estimator is working; the target is noisy. This is why §1.3 leads with QLIKE against a
named benchmark and not with R² against the proxy: on a noisy variance proxy, R² is bounded near
zero no matter how good the forecast is, and quoting it alone would understate every model here.

### 1.2 In-sample fit (reported because it was asked for; it is **not** evidence)

Log-variance HAR(1/5/22) on one-bar Rogers-Satchell RV, full 1478 usable rows, HAC t-stats:

| pair | b_d | b_w | b_m | t(b_d) | t(b_w) | t(b_m) | persistence | R²_in |
|---|---|---|---|---|---|---|---|---|
| EURUSD | 0.031 | 0.004 | 0.287 | 1.05 | 0.07 | **3.28** | 0.322 | 0.014 |
| GBPUSD | 0.037 | −0.021 | 0.366 | 1.32 | −0.38 | **3.94** | 0.381 | 0.018 |
| USDJPY | −0.008 | −0.001 | 0.373 | −0.26 | −0.01 | **3.70** | 0.365 | 0.015 |
| AUDUSD | 0.061 | 0.015 | 0.191 | **2.14** | 0.23 | 1.67 | 0.267 | 0.009 |
| USDCAD | 0.015 | −0.005 | 0.272 | 0.51 | −0.09 | **3.40** | 0.282 | 0.012 |

The monthly component carries all the signal and the daily component is insignificant on 4 of 5
pairs. That is the *correct* answer for this data: the simulator's log-vol mean-reverts at
`kappa=4/yr`, so its one-day persistence is `exp(-4/252) = 0.984`, but the daily proxy is
92% noise, so only the 22-day average is clean enough to load on. **On real FX the daily
component is usually the largest of the three** — if the user fits this on their own data and
still sees `b_d ≈ 0`, their RV proxy is too noisy, not their market.

### 1.3 Out-of-sample, against two named benchmarks

Walk-forward, expanding window, `min_train=500`, refit every 21 bars, n_oos = 999 per pair.
Benchmarks: **`rw`** = yesterday's RV (the random walk), **`mean`** = trailing expanding mean.
Headline loss is QLIKE on variance; `R²` is `1 − L(model)/L(benchmark)`.

Mean across the five pairs:

| model | QLIKE | R²_OOS vs **rw** | R²_OOS vs **mean** | R²_logvar | DM t vs mean | DM p |
|---|---|---|---|---|---|---|
| **har** | 0.733 | **+0.703** | **+0.087** | 0.006 | −3.06 | 0.004 |
| implied | 0.733 | +0.702 | +0.086 | 0.006 | −2.51 | 0.062 |
| blend 50/50 | **0.729** | **+0.705** | **+0.092** | 0.010 | −2.88 | 0.015 |
| mean | 0.804 | +0.675 | 0.000 | −0.001 | — | — |
| rw | 2.676 | 0.000 | −2.221 | −0.885 | +9.18 | 0.000 |

Per pair, `R²_OOS(QLIKE)` — HAR vs rw / HAR vs mean:
EURUSD +0.692 / +0.102 · GBPUSD +0.694 / +0.099 · USDJPY +0.791 / +0.100 ·
AUDUSD +0.745 / +0.077 · USDCAD +0.592 / +0.056.

**HAR beats yesterday's RV by a mile and beats the trailing mean by ~9% of QLIKE, on all five
pairs, with a Diebold-Mariano t of −3.06 (p = 0.004).** The margin over the trailing mean is
small in absolute terms *because this data has almost no forecastable vol variation left after
the proxy noise* — the ceiling is 8% of log-variance (§1.1). On real FX, where daily log-RV
autocorrelation at lag 1 is 0.5–0.7 rather than the 0.02–0.06 here, the same code has far more to
work with.

Scored against the **latent** simulator vol instead of the noisy proxy, HAR's `R²_OOS(QLIKE)` vs
the trailing mean is **+0.615** and `R²_logvar` is **+0.240**. That is the real size of the effect
the proxy is hiding.

### 1.4 Choice of fitting space — measured, not assumed

Out-of-sample QLIKE and `R²_OOS` vs the trailing mean, same walk-forward, by fitting space:

| pair | QLIKE log | QLIKE vol | QLIKE var | R² log | R² vol | R² var |
|---|---|---|---|---|---|---|
| EURUSD | 0.7151 | 0.7785 | 0.7135 | 0.1019 | 0.0222 | 0.1039 |
| GBPUSD | 0.6843 | 0.7398 | 0.6780 | 0.0991 | 0.0261 | 0.1075 |
| USDJPY | 0.8678 | 0.9482 | 0.8629 | 0.0995 | 0.0161 | 0.1046 |
| AUDUSD | 0.8418 | 0.9187 | 0.8374 | 0.0772 | −0.0071 | 0.0821 |
| USDCAD | 0.5559 | 0.5864 | 0.5585 | 0.0562 | 0.0045 | 0.0518 |
| **mean** | **0.7330** | 0.7943 | **0.7300** | **0.0868** | 0.0123 | **0.0900** |

Plain-variance OLS is better than log by 0.4% of QLIKE — inside the noise — and vol space is
clearly worse than both. **We ship `space="log"` anyway**, because an unconstrained linear fit in
variance can return a *negative* variance, and a negative variance is not a recoverable error in a
rung distance. The price is a Jensen correction on the back-transform (`exp(mu + s²/2)`, applied by
default, worth ~20–30% of the level on these residuals). This is a deliberate trade of 0.4% of
QLIKE for a hard positivity guarantee, not an assumption that logs fit better.

### 1.5 The level, not just the ranking — calibration

The PM's note 4 is right that the *level* of this forecast has go/no-go consequences, so it is
tested separately from the ranking. A one-bar range estimator forecasts **its own** level, not the
level of the close-to-close move a rung is filled by; `rangeforecast.proxy_scale` measures the
single correction constant and applies it openly (`components['proxy_scale']`, printed in `basis`).

On this feed it is **0.596–0.634** — the synthetic bar generator is not internally consistent with
its own close path and its ranges are ~60% too wide. On real FX the same constant is normally just
*above* 1 (discrete sampling makes ranges read slightly low). **Either way it must be measured, not
assumed**, and getting it wrong scales every rung by the same factor.

Coverage of the walk-forward daily forecast against realised close-to-close moves, n ≈ 999:

| pair | q05 (exp 0.05) | q25 (0.25) | q75 (0.75) | q95 (0.95) | realised E\|r\| / predicted |
|---|---|---|---|---|---|
| EURUSD | 0.046 | 0.238 | 0.746 | 0.938 | 0.987 |
| GBPUSD | 0.056 | 0.247 | 0.749 | 0.950 | 1.007 |
| USDJPY | 0.049 | 0.244 | 0.777 | 0.957 | 0.954 |
| AUDUSD | 0.048 | 0.251 | 0.778 | 0.958 | 0.956 |
| USDCAD | 0.050 | 0.251 | 0.776 | 0.945 | 0.970 |

Binomial se on the tail quantiles is 0.007. Every tail is inside ~1.5 se and the expected absolute
move is within 5% on all five pairs. **The level is calibrated.** The slight over-coverage of q75/q95
(0.75→0.78, 0.95→0.958) says the forecast is a touch *wide* in the upper tail, which for a resting
ladder is the safe direction to be wrong in.

### 1.6 Verdict — HAR

**USE IT.** It beats both named benchmarks out of sample on every pair, the machinery recovers a
known process at R² 0.975, and the level is calibrated to within 5%. The honest caveat is that its
margin over a trailing mean is small *on this data*, and the user should re-run §1.3 on their own
history before assuming the same margin.

---

## 2. Implied vol in the blend

Blend weight fitted by QLIKE grid search on the **first half** of the walk-forward, scored on the
**second half** it never saw:

| pair | w_implied (train) | test QLIKE har | blend(fit) | blend 50/50 | gain vs HAR | DM t | DM p |
|---|---|---|---|---|---|---|---|
| EURUSD | 0.22 | 0.7552 | 0.7524 | 0.7520 | **+0.37%** | −0.86 | 0.387 |
| GBPUSD | 0.88 | 0.6231 | 0.6312 | 0.6267 | **−1.31%** | +1.73 | 0.084 |
| USDJPY | 1.00 | 0.8528 | 0.8540 | 0.8512 | −0.13% | +0.12 | 0.901 |
| AUDUSD | 0.57 | 0.8249 | 0.8217 | 0.8216 | +0.39% | −0.52 | 0.606 |
| USDCAD | 0.29 | 0.5363 | 0.5349 | 0.5361 | +0.25% | −0.32 | 0.748 |

The fitted weight is **unstable across pairs (0.22 → 1.00)** and the honest out-of-sample gain is
between −1.3% and +0.4% of QLIKE, with no p-value below 0.08. **On this data, implied adds nothing
over HAR, and the fitted weight is fitting noise.**

That is exactly what the generator guarantees (§0): its ATM *is* a linear combination of trailing
RV. This measurement is therefore a **machinery check that passed** — the code correctly declined
to find information that is not there — and it is **not** evidence about real markets, where implied
carries a genuine event and flow signal that daily RV cannot see.

### 2.1 What ships, and why

`DEFAULT_BLEND = {"har": 0.5, "implied": 0.5}`, declared in the module as a **prior, not a
measurement**. Rationale: on synthetic the measurement is uninformative, published encompassing
regressions on liquid G10 put the weight on implied broadly in the 0.4–0.6 range, and neither
input subsumes the other. Fifty-fifty is also the choice that is least wrong if the user never
re-fits. `fit_blend_weights(actual, har, implied)` replaces it the moment they have a recorded
implied history.

### 2.2 Verdict — implied blend

**USE IT AT A FIXED 0.5, DO NOT FIT IT YET.** The blending machinery works and is honest about its
weights (`components['w_har']`, `components['w_implied']`, both printed in `basis`). Fitting the
weight is blocked on data, not on code — see §8 and architecture amendment **C-6**, which is the
binding constraint: FXE/FXB/FXY chains give today's smile and no history, so there is nothing to
fit on until the daily chain snapshotter has been running.

---

## 3. Session variance time

`SESSION_VAR_PROFILE` is a 24-hour UTC variance-intensity profile (London open step, London/NY
overlap peak, Asia-Pacific handover trough). It is a **prior**, printed wherever it is used, and
`window_var_fraction(start, end, profile=...)` accepts a substitute.

For London 17:00 → 07:00:

| quantity | value |
|---|---|
| clock fraction of a day | 0.583 (14/24) |
| **variance fraction of a trading day** | **0.382** |
| sigma ratio if you use clock time | **×1.236 too wide** |
| theta share ÷ variance share | 1.527 |
| overnight breakeven vol multiple `sqrt(0.583/0.382)` | **1.236** |

This reproduces the PM's note 4 exactly: the window pays 58% of a day's theta for 38% of its
variance, so **overnight realised vol has to run ~24% above the daily breakeven vol before the
window pays for itself**. (The PM's 1.72 corresponds to a 34% variance share; 0.583/0.34 = 1.72.
Our profile gives 38%, hence 1.53.) Acceptance criterion §4 of the brief — "overnight variance uses
session variance time, not clock time" — is met, and the two numbers differ by enough to reprice
every rung.

**Verdict: USE IT**, with the profile itself flagged as a prior in the UI.

---

## 4. Scheduled events

### 4.1 CR-12 — the profile is no longer a scalar

`overnight_range_forecast` returns `segments: tuple[RangeSegment, ...]`, the window cut at every
in-window event in the pair's own two currencies, each piece carrying its own `var_fraction`,
`sigma` and `exp_abs_move_pips`. Verified count: **32.3% of the shipped calendar's 409 events
(29.6% of the 223 top-tier ones) fall inside London 17:00–07:00** — the trader's figure is right.
The concentration is total, not diffuse: **100%** of JPY, AUD and NZD events are in-window and
**0%** of EUR, GBP, CHF, CAD, SEK and NOK events are.

Worked output, EURUSD and USDJPY, night of 2026-09-17 → 18 (Japan CPI 23:30Z, BoJ 03:00Z):

```
EURUSD  1 segment   [16:00-06:00Z]  vf=0.3819  sigma=0.293%  E|move|=27.3p
USDJPY  3 segments  [16:00-23:30Z]  vf=0.2373  sigma=0.304%  E|move|=35.8p
                    [23:30-03:00Z]  vf=0.0868  sigma=0.395%  E|move|=46.5p  post-Japan national CPI
                    [03:00-06:00Z]  vf=0.0579  sigma=0.381%  E|move|=44.8p  post-BoJ policy decision
```

The second segment carries less than a quarter of the first's clock time and a *larger* sigma. A
scalar `var_fraction` cannot express that, and a ladder spaced off the scalar would put its rungs
in the wrong place on both sides of the print.

### 4.2 Two data problems, flagged not absorbed

`RangeForecast.warnings` carries, for the night above:

- `BoJ policy decision (JPY) has no fixed announcement time; the 03:00 UTC boundary is nominal,
  treat it as soft` — the calendar asserts 12:00 Tokyo; the BoJ statement in reality lands anywhere
  from roughly 11:30 to 15:00 JST. `RangeSegment.time_certain=False` marks the boundary. The same
  rule fires on any row whose `source` begins `approx:`, which is **every row in the shipped file**.
- `no holiday calendar: data/calendar/events.csv has zero holiday rows, so a thin pre-holiday or
  half-day session is forecast as a normal one` — confirmed, zero holiday rows. **This is a request
  to `data`**: a half-day or a Golden Week session has a fraction of a normal night's variance and
  nothing in this repo knows it.

### 4.3 The event uplift size is a prior, and the measurement says so

`EVENT_SIGMA = {3: 0.0035, 2: 0.0015, 1: 0.0}` (extra instantaneous log-move sd per event).
`calibrate_event_uplift` measures it from history instead. Result:

| pair | var ratio (event / non-event days) | n_event | n_other | t on log variance |
|---|---|---|---|---|
| EURUSD | 1.158 | 25 | 1475 | +0.70 |
| GBPUSD | 0.618 | 25 | 1475 | −1.63 |
| USDJPY | 1.704 | 25 | 1475 | +1.50 |
| AUDUSD | 0.754 | 17 | 1483 | −0.31 |
| USDCAD | 1.111 | 18 | 1482 | +1.11 |

**Not measurable here, and correctly so.** The signs are inconsistent, no |t| reaches 2, and the
sample is ~25 event days because the shipped calendar only spans 2025-12-31 → 2027-12-31 while the
price history runs back to ~2020. On top of that the simulator applies its event bump to *implied*
only and never to the realized path, so there is genuinely nothing to find. The measurement not
inventing an effect is the machinery check.

**Verdict — events: USE THE SEGMENTATION (machinery, and CR-12 is satisfied). Treat the uplift
SIZE as an unvalidated prior** and re-run `calibrate_event_uplift` on real history before trusting
the magnitude. Flag both data problems to the user.

---

## 5. Path roughness and crossings (CR-11)

### 5.1 The identity the module rests on

For a path with log returns `r_i`, quadratic variation `QV = Σr²` and net displacement `D = Σr`:

```
kappa := E[QV] / E[D²]                     (= 1 for a random walk)
E[completed h-moves] = kappa * (S*sigma/h)²
Kaufman ER = |D| / Σ|r|                     (= 1/sqrt(n) for a random walk)
kappa = 1 / (n * ER²)                       <- the two are the same number
```

So the ladder needs exactly one roughness input, and the trader's two candidate quantities are
algebraically the same thing. **This is the term the frozen `RangeForecast` was missing**: two
nights with an identical range and different `kappa` refill a rung a different number of times.

**The identity reproduces on the data.** `kappa` estimated directly against `kappa` recovered from
ER, across 5 pairs × 4 block lengths (n = 2, 5, 10, 21):

| pair | n=2 | n=5 | n=10 | n=21 | max relative gap |
|---|---|---|---|---|---|
| EURUSD | 0.970 / 0.956 | 0.960 / 0.951 | 0.979 / 0.925 | 0.984 / 0.986 | 5.5% |
| GBPUSD | 1.015 / 1.026 | 0.989 / 1.015 | 0.934 / 0.944 | 0.954 / 0.974 | 2.7% |
| USDJPY | 1.036 / 1.015 | 1.077 / 1.038 | 1.172 / 1.127 | 1.188 / 1.154 | 3.8% |
| AUDUSD | 0.967 / 0.993 | 0.893 / 0.927 | 0.883 / 0.899 | 0.944 / 0.954 | 3.9% |
| USDCAD | 1.000 / 0.973 | 0.993 / 0.961 | 1.023 / 0.991 | 0.962 / 0.943 | 3.2% |

All 20 estimates sit in 0.88–1.19, i.e. **Brownian to within estimator noise — which is the right
answer, because the simulator's returns are independent.**

### 5.2 The counter had to be replaced, and the error is instructive

The first implementation counted, for each step, how many lines of a **fixed** grid it stepped
over. That count has no limit: a path wiggling across one grid line racks up crossings without
bound as you sample it more finely, so it cannot be compared with the analytic `QV/h²` — and it
over-read the analytic baseline by **2.6×** on a 25-pip grid, which is how the error surfaced.
`grid_crossings` now counts **completed h-moves with a moving reference** (Lévy h-oscillations,
"renko bricks"), for which `h²·N_h → QV`.

Convergence check on simulated Brownian motion, `h = 1.0`, 300 paths:

| sampling steps per unit variance | `N_h·h² / QV` |
|---|---|
| 1 | 0.463 |
| 4 | 0.627 |
| 16 | 0.772 |
| 64 | 0.879 |
| 256 | 0.929 |
| 1024 | 0.968 |

**This table is the whole limitation of measuring roughness on daily bars.** If `h` is about one
daily move you recover 46% of the true count; at `h` = 4 daily moves, 63%. On FX daily closes the
observed ratio of counted moves to the analytic baseline is **0.62–0.79** across all five pairs and
two grid sizes — exactly what the convergence table predicts, and it is *sampling*, not roughness.

### 5.3 Does the estimator detect real roughness? Yes.

AR(1) in log price (mean-reverting = choppy) and AR(1) in returns (trending), 4000 bars, n=21:

| process | `kappa` | ER (Brownian = 0.218) | crossings scale |
|---|---|---|---|
| random walk | 0.897 | 0.234 | ×0.90 |
| mildly mean-reverting (φ=0.97) | 1.116 | 0.208 | ×1.12 |
| strongly mean-reverting (φ=0.90) | **1.974** | 0.158 | **×1.97** |
| +0.25 AR in returns (trending) | 0.555 | 0.295 | ×0.56 |
| +0.50 AR in returns (trending) | **0.324** | 0.385 | **×0.32** |

The estimator moves the right way and by roughly the right amount: a strongly trending path fills a
ladder **a third** as often as a Brownian one at the same range. The random-walk baseline reads
0.90 rather than 1.00 — a ~10% small-sample downward bias in the aggregate ratio at n=21 — so
**`kappa` between 0.9 and 1.1 should be read as "Brownian"**.

### 5.4 Can we *forecast* crossings out of sample? No — and here is why

Walk-forward, `kappa` from a trailing 250-block aggregate, vol held at its **realised** value for
every model so the roughness question is isolated from the vol question. Benchmarks: the Brownian
null (`kappa = 1`) and a trailing mean of the observed count.

| pair | h | n_bars | mean actual | mean Brownian | R² vs trailing mean: kappa-model | Brownian |
|---|---|---|---|---|---|---|
| EURUSD | 163p | 21 | 1.45 | 2.17 | −0.81 | +0.07 |
| GBPUSD | 218p | 21 | 1.30 | 1.95 | −0.36 | −0.24 |
| USDJPY | 373p | 21 | 1.39 | 2.27 | −5.41 | −0.94 |
| AUDUSD | 176p | 21 | 1.43 | 2.08 | +0.03 | −0.06 |
| USDCAD | 166p | 21 | 1.42 | 2.24 | −1.51 | −0.11 |

**The kappa-scaled model loses to a trailing mean on 4 of 5 pairs, and to the Brownian null on all
5.** The diagnosis is clean and is not a modelling failure: at daily resolution a window contains
O(1) completed moves, so the target is a small integer dominated by discreteness and by the
within-bar oscillation §5.2 shows daily bars cannot see. The `kappa` estimate itself is then noisy
(walk-forward values ranged 0.38–1.06 on USDCAD alone), and multiplying a good baseline by a noisy
number makes it worse. On a finer grid (25 pips, the spacing a real ladder uses) it is worse still:
the daily path simply does not contain the information.

### 5.5 Verdict — roughness

- **`kappa` / `efficiency_ratio` / `expected_crossings` as OUTPUTS: USE THEM.** The identity is
  verified to 1–5%, the estimator detects real roughness at the right magnitude, and the ladder
  needs the number. `expected_level_crossings` additionally gives the per-rung count via Tanaka's
  local-time formula, which is why outer rungs refill less than inner ones and should not be sized
  identically.
- **A forecast of `kappa` from daily data: DO NOT USE.** It does not beat the Brownian null out of
  sample. `forecast_roughness` therefore returns the trailing aggregate and nothing cleverer, the
  value is stamped into `components['kappa']` and `basis`, and `RangeForecast.warnings` says on
  every call that it is measured on daily closes and is an **assumption** for the overnight window.
- **This is the single highest-value thing intraday history would unlock.** §8.

---

## 6. Technical levels (§B of the brief, and CR-14)

Kinds tested (16): `prior_high`, `prior_low`, `prior_close`, `round_big/half/quarter`, `pivot`,
`pivot_r1/s1/r2/s2`, `swing_high/low`, `sma_20/50/100/200`. Levels rebuilt at **every bar** using
only bars at or before it (1249 dates per pair, 30k–42k level observations). `oi_levels` produces
`oi_cluster` from CME open interest, which has no history on this feed and so is not in the panel.

### 6.1 The control, and a bias we found and fixed

The control is a placebo price at a **matched distance from spot**, evaluated by the identical code
path. Two errors were found and corrected during this work; both are recorded because both are
easy to make and each one manufactured an effect out of nothing:

1. **Inconsistent pooling.** The real statistic averaged over *touches*; the control averaged over
   *observations*, each contributing its own across-replicate mean. That silently up-weights far
   levels (rarely touched, but still counted once) and produced a **uniform −10pp "anti-effect" on
   all 16 kinds** — a result too tidy to be real, which is what gave it away. Both sides now pool
   over touch events.
2. **The control must be matched on distance *in units of current volatility*, not in pips.**
   Several kinds (`pivot_r2`/`pivot_s2`, anything proportional to the prior range) have a distance
   that is itself proportional to today's vol. A raw-pip control lands too close on quiet days and
   too far on busy ones; conditional on a touch it then overshoots more and reverts less, and
   **fabricates a positive effect for the real level**. `control="permuted"` now permutes the
   distance in **ATRs** and maps it back through today's ATR. `control="permuted_raw"` is retained
   so the artefact can be reproduced: it puts `pivot_s2` over the significance line on 3 of 5 pairs
   where the scale-matched control does not. `control="jitter"` (displace the real level by ±25% of
   its own distance) is the local second opinion and is automatically scale-matched.

Sanity check that the matching worked: real and control touch rates agree to within 0.01 on every
kind and every pair.

### 6.2 CR-14 — fill quality, the test that decides

The reversal statistic answers a question about *spot*. The ladder's question is whether a rung
**placed at the level** is better filled and better priced than the same rung placed at the same
distance from spot but snapped to nothing. `measure_fill_quality` measures exactly that, signed
from the point of view of the resting order (sell above spot, buy below):
`fill_rate`, `mark_pips` (mark-to-market per fill at the horizon), `adverse_pips`,
`recycle_rate`, and `ev_pips = fill_rate × mark_pips` (the value of leaving the order at all).
Horizon 12h → 1 daily bar. Block bootstrap over dates (block 21, 400 replicates) for the CI, BH
adjustment across the 16 kinds.

`d_mark` — pips per fill gained by snapping, real minus control, scale-matched control:

| kind | EURUSD | GBPUSD | USDJPY | AUDUSD | USDCAD | mean | pairs > 0 |
|---|---|---|---|---|---|---|---|
| sma_100 | +0.01 | +1.78 | +8.44 | −0.04 | +7.39 | +3.51 | 4/5 |
| sma_200 | +5.73 | −3.97 | +13.68 | −3.41 | −0.64 | +2.28 | 2/5 |
| pivot_s2 | −2.90 | +1.55 | +3.55 | +6.05 | +2.81 | +2.21 | 4/5 |
| swing_high | +1.06 | +1.53 | +3.14 | +0.33 | +1.26 | +1.46 | **5/5** |
| round_big | +0.38 | +1.71 | +1.14 | +1.58 | +1.54 | +1.27 | **5/5** |
| round_half | +3.10 | +0.51 | +0.38 | +0.82 | +1.34 | +1.23 | **5/5** |
| prior_high | −2.28 | −0.43 | +2.37 | −0.07 | −0.38 | −0.16 | 1/5 |
| prior_low | −0.67 | +1.44 | −0.28 | +0.68 | +0.36 | +0.31 | 3/5 |
| round_quarter | −0.69 | +0.10 | −0.21 | −0.08 | −0.07 | −0.19 | 1/5 |
| pivot | −1.09 | +2.00 | −2.45 | −0.80 | +0.35 | −0.40 | 2/5 |
| pivot_r1 | −1.25 | +0.03 | −2.23 | −2.08 | −1.14 | −1.33 | 1/5 |

(`swing_low`, `sma_20`, `sma_50`, `pivot_s1`, `pivot_r2` all mean within ±0.7p; full frame in the
function output.)

**Significance: 4 cells of 80 clear BH p < 0.05 under the scale-matched control, 2 of 80 under the
jitter control, and no kind is significant on more than one pair.** The four are `round_half`
(EURUSD), `pivot` (GBPUSD), `pivot_s2` (AUDUSD) and — with the jitter control instead —
`swing_high` and `pivot_r2` (EURUSD, USDJPY). **They do not overlap.** A kind that were real would
show up on several pairs; these each show up once and then vanish when the control changes.

The multiple-comparisons arithmetic, stated plainly because the brief asks for it: 16 kinds × 5
pairs = 80 tests. BH at 5% controls the false-discovery rate *within* each pair's family of 16, so
under a complete null you expect a handful of discoveries across five families — which is what
happened. **Four scattered, non-replicating hits out of eighty is what "nothing is here" looks
like.**

Three kinds (`swing_high`, `round_big`, `round_half`) are positive on 5/5 pairs. Under independence
that is p = 0.031 each — but 16 kinds are being screened, so ~0.5 such runs are expected by chance
and three is not far outside that; and the five pairs are **not** independent (the simulator drives
them from correlated currency legs, EUR/GBP at +0.78), so the effective number of independent pairs
is materially fewer than five. The effect sizes are also economically negligible: +1.2 to +1.5 pips
per fill, on `d_ev` (value per order left) of +0.21 to +0.42 pips. **Not evidence.** It is the
right thing to re-check first on real data.

### 6.3 The reversal statistic (secondary colour, per CR-14)

Same engine, `measure_reversal_stats`: touch rate, reversal rate conditional on touch, mean
excursion beyond, all against the same control. Under the scale-matched control the mean
`d_reversal` across pairs is within ±0.03 for every kind, and nothing replicates:

`sma_20` +0.021 (4/5 pairs positive, BH p = 0.0012 on EURUSD alone), `pivot_s2` +0.028 (3/5),
`pivot_r2` +0.019 (3/5), `round_big` +0.004 (4/5), `prior_high` +0.002 (2/5),
`swing_low` −0.011 (1/5). Horizon sensitivity on EURUSD: at 12h one kind clears BH, at 48h a
*different* one does, at 120h none does — the signature of noise, not of a level.

One measurement is **degenerate on this feed and must not be quoted**: `recycle_rate` reads exactly
1.000 for every kind. The synthetic bar generator stamps `open = previous close`, so at a one-bar
horizon the bar always contains the reference close and "spot came back to the ladder centre" is
true by construction. It needs intraday data or a feed with real opens.

### 6.4 Verdict — levels

- **`technical_levels`, `oi_levels`: USE THEM AS ANCHORS AND LABELS.** They are correct, forward-
  clean, and give a rung something to say for itself ("1.1731 is 3 pips inside yesterday's high").
  The `strength` column is documented in the module as a **display prior, not a probability**.
- **SNAPPING: DO NOT TURN IT ON.** No level kind beats its distance-matched control on fill quality
  on this data, and the handful that clear BH do not replicate across pairs or across controls.
  Per the brief's acceptance criterion, snapping ships **off by default**.
- `nearest_level(...)` is the mechanism if snapping is later justified; it takes a `kinds=` filter
  so only kinds that beat their control **on the user's own data** can be enabled.

---

## 7. Verdict table

| Component | Verdict | On what evidence |
|---|---|---|
| `har_rv` | **USE** | beats rw by R²_OOS +0.70 and trailing mean by +0.087 (DM p = 0.004) on 5/5 pairs; recovers a known process at R² 0.975 |
| `proxy_scale` + calibration | **USE** | all four coverage quantiles within ~1.5 se; E\|move\| within 5% on 5/5 pairs |
| implied in the blend | **USE at fixed 0.5, DO NOT FIT** | fitted weight ranges 0.22–1.00 across pairs; honest OOS gain −1.3% to +0.4%, no p < 0.08 |
| session variance time | **USE** | 0.382 vs 0.583 clock; ×1.236 on sigma; reproduces the PM's theta/variance ratio |
| event **segmentation** (CR-12) | **USE** | 32.3% of calendar events are in-window; segments produce materially different sigmas within one night |
| event uplift **magnitude** | **PRIOR ONLY** | n≈25 event days, inconsistent signs, no \|t\| > 2 |
| `kappa` / ER / `expected_crossings` (CR-11) | **USE as outputs** | identity verified to 1–5% on 20 estimates; detects φ=0.90 mean reversion at κ=1.97 and momentum at κ=0.32 |
| **forecasting** `kappa` | **DO NOT USE** | loses to the Brownian null on 5/5 pairs; daily bars recover only 46–63% of the true count |
| `technical_levels` / `oi_levels` as anchors | **USE** | mechanically correct, forward-clean |
| **snapping** rungs to levels | **DO NOT USE — off by default** | 4/80 BH-significant, 2/80 under the alternative control, zero replication, effects +0.2–0.4 pips per order |
| `recycle_rate` | **DO NOT QUOTE** | degenerate at 1.000 on a feed where `open == previous close` |

---

## 8. What the user must run on their own data, and what would change a verdict

None of the "do not use" verdicts above is a statement about real FX. Each one is a statement about
what this data can show. Here is exactly how to overturn them.

**8.1 Re-run the vol evaluation (30 minutes, needs only their spot history).**
```python
rv = rangeforecast.rv_daily(hist)                       # their OHLC
wf = rangeforecast.har_walk_forward(rv, min_train=500, refit_every=21)
rangeforecast.evaluate_forecasts(wf["actual"],
    {"har": wf["har"], "rw": wf["rw"], "mean": wf["mean"]}, benchmark="mean")
```
*Expect*, on real G10 daily data: `R²_logvar` for HAR of **0.35–0.55**, daily log-RV lag-1
autocorrelation of **0.5–0.7**, and `b_d` the largest of the three coefficients. If they see
autocorrelation near 0.05 as we do here, their RV proxy is too noisy — switch `rv_method` or check
the feed's highs and lows. **Also check `proxy_scale`: expect it slightly above 1.0.** A value near
0.6 as here means the bar data is not consistent with its own closes.

**8.2 Fit the implied weight — but only after the snapshotter has run.**
Architecture amendment **C-6** is the blocker: no chain history exists on day one. Once ~250 days
of recorded ATM exist, `fit_blend_weights` on the first half and score on the second.
*Would justify moving off 0.5:* a fitted weight that is **stable across pairs and across the two
halves**, with a Diebold-Mariano p < 0.05 against HAR alone on the held-out half. A weight that
swings from 0.22 to 1.00 as it does here is fitting noise and 0.5 stays.

**8.3 Measure the event uplift once ~2 years of overlap exists.**
`calibrate_event_uplift(hist, events, pair, importance=3)`. *Would justify replacing
`EVENT_SIGMA`:* a variance ratio consistently above 1 with |t| > 2, and the **same sign on
several pairs**. Note the daily-vs-window caveat the function returns: a 14:15 ECB is a daily
uplift that is entirely outside the London-close-to-open window.

**8.4 The one that matters most: get intraday data, then re-measure roughness (§5).**
Everything in §5.4 fails for one reason — daily bars cannot see intraday oscillation, and §5.2
quantifies the loss precisely (46% of the true count at `h` ≈ one bar move, 63% at `h` = 4 bar
moves). With hourly or 5-minute history the *same functions* work unchanged:
```python
rangeforecast.crossings_series(intraday, spacing_pips=25, pair="EURUSD", window_bars=14)
rangeforecast.roughness_kappa(intraday, window_bars=14)     # 14 hourly bars = one overnight window
```
*Would justify a roughness forecast:* a trailing `kappa` that beats both the Brownian null and a
trailing mean out of sample. *Would change the ladder immediately even without a forecast:* an
overnight `kappa` measured materially away from 1 — recall a `kappa` of 1.97 doubles the expected
fills at the same range, and 0.32 cuts them to a third. **This is also the only way to measure the
overnight session profile in §3 rather than assuming it.**

**8.5 Re-run the level test — this is the one most likely to change.**
```python
panel = levels.level_panel(hist, pair, start=250)
levels.measure_fill_quality(hist, panel, pair=pair, control="permuted")   # the decision test
levels.measure_fill_quality(hist, panel, pair=pair, control="jitter")     # the second opinion
levels.measure_reversal_stats(hist, panel, pair=pair)                     # colour
```
*Would justify turning snapping on for a kind:* **all** of the following, and the conjunction is
deliberate because any one of them alone will be met by chance somewhere in a 16 × N-pair grid:
1. `d_mark > 0` with BH-adjusted p < 0.05 **on the same kind, on at least three pairs**;
2. the same sign and significance under **both** the `permuted` and the `jitter` control;
3. `d_ev > 0` as well, so the value survives the fill-rate cost of moving the rung;
4. an effect **larger than the spread** — a 1-pip edge does not survive a 0.5-pip spread plus
   brokerage, and everything found here was 0.2–0.4 pips of `d_ev`;
5. `n_fill >= 100` on each qualifying pair.

Then enable it narrowly: `nearest_level(..., kinds=[the qualifying kinds])`, never for all kinds.

**8.6 Ask `data` for two things.** A **holiday calendar** (§4.2 — nothing here knows about a
half-day), and a **soft-time flag on the calendar itself** so `SOFT_TIME_EVENTS` does not have to
pattern-match the event text to know the BoJ has no fixed announcement time.

---

## 9. Reproducing every number in this document

All figures come from `get_provider("synthetic")` at its default seed with `PYTHONPATH=/home/user/fx`,
and every function named above is public in the two modules. The self-tests that do not depend on
the provider — §1.1, §5.2, §5.3 — use `rangeforecast.simulate_rv` and short inline simulations, and
are the ones to run first if anything here ever stops reproducing: if the machinery checks pass and
the market results move, the data changed. If the machinery checks fail, the code did.
