# The Overnight Delta Cap — derivation, measurement, and the number to run

**Owner:** quant (position sizing). **Files:** `fxgamma/portfolio/deltacap.py`, this document.
**Reads against:** `08_overnight_gamma.md` (brief + all four amendments), `09_hedging_theory.md`,
`11_overnight_desk_spec.md`, `12_trend_conditional_hedging.md`, and
`fxgamma/portfolio/{overnight,bandopt,ratchet,risk,zones}.py`, `fxgamma/backtest/`.
**Data:** everything numeric here is produced offline against `get_provider("synthetic")` at
`asof = 2026-09-09 16:00Z`. Market-data hosts are blocked in this build. §7 says what to re-run.

The user was asked for their cap and answered *"any as long as you think it's optimal size and
makes sense in regards to the options I have."* That is the correct answer and it means the cap
has to be **derived from the book**. This document derives it, measures whether it matters, and
ships the derivation as `deltacap.recommend_cap`.

---

## 0. Verdict, up front

| question | answer |
|---|---|
| **Is the objective flat in the cap, as it was in the band?** | **No** — and in `docs/09` §5's own units. Being **30% off the cap** moves the night by **1.2-16.3% of its gamma P&L**, against **0.1-0.9%** for being 30% off the band. That is one to two orders of magnitude, it holds at *both* cost tiers (at the trader's 1.03bp it shows up in the tail, at the repo's 5bp in the mean), and the project's consensus stands. §4.14. |
| What cap should this user run? | **EUR 0.54mm** on the reference EURUSD 1M 10mm/leg straddle — rule R2, `Gamma_1pct x sigma_on / 2`. Scaling in §6. |
| Is that an *optimum*? | **No, and the document says so.** The cap's effect on the expected P&L is exactly **minus its transaction cost** (measured to 0.4%). On a long-gamma book it buys CVaR-95 at an average of about **2 USD of mean per 1 USD of tail** from no cap down to the recommended one, so a mean-CVaR objective only prefers capping if it weights the 5% tail at more than twice the mean. It is a priced preference, not an optimum. |
| What *is* preference-free? | The **floor**. Below EUR 0.26mm a tighter cap makes the expected P&L **and** the tail worse. That region is strictly dominated, it has a closed form (§4.4), and the argmin survives five different tail measures (§4.15). And the **ceiling**: above EUR 1.08mm the cap improves CVaR-95 by exactly zero. |
| Is the shipped `hedge_bands` 15%-of-gross default wrong for overnight? | **Yes, plainly.** EUR 3.0mm here — 5.6x the derived cap and 2.8x the delta the *unhedged* book accumulates in a whole one-sigma night. Measured: it costs USD 10/night and improves CVaR-95 by **zero**. It is **strictly dominated by having no cap at all**. It is an intraday default. §4.8. |
| What costs the most to be wrong about? | **The cost assumption, not the cap.** At the trader's own 1.03bp the cap costs USD 107/night and buys tail at 0.31; at the repo's 5bp retail default it costs USD 533 and the rate is 2.03. The cost tier changes the *character* of the recommendation. §4.11. |
| Does the vol mark matter? | **Not to the cap.** R1/R2/R3 are *exactly* invariant to the vol level and move <0.1% for a full vol point of mark error. The money moves by ~USD 375/vol point; the cap does not move at all. §4.7. |
| Short gamma? | **A different object.** CVaR-95 runs USD -8,775 uncapped to -2,558 capped, ten times the long-gamma effect — but with realistic stop slippage most tightening is whipsaw, and the trader's refusal conditions, not a cleverer cap, are the answer. §5. |

**One sentence for the screen:** *"Your cap is EUR 0.54mm. It costs about USD 530 a night at your
spread and buys about USD 260 of 5%-tail — you should decide whether you want that trade; what you
should not do is go below EUR 0.26mm, where it makes both worse, or above EUR 1.1mm, where it does
nothing at all."*

---

## 1. Correcting the PM's candidate table before using it

The PM's three candidates were computed with an **overnight sigma of 0.256%**. That is
`sigma * sqrt(vf / 365)`. `docs/09` §1 ("two clocks, on purpose") establishes that spot variance is
delivered on **252 trading days** and theta is paid on **365 calendar days**; the repo's own
`overnight.py` uses `sqrt(vf / TRADING_DAYS)` throughout. On the 252 clock,

```
sigma_on = 7.944% * sqrt(0.3820 / 252) = 0.3093%       (not 0.256%)
```

— 20.8% larger. The error does not cancel across the three rules, because two are linear in
`sigma_on` and one is inverse in it:

| rule | PM (365 basis) | **corrected (252 basis)** | % gross notional | direction of the error |
|---|---|---|---|---|
| R1 one-sigma accumulation, `Γ₁ σ_on` | 0.89mm | **1.08mm** | 5.4% | PM **too tight** by 20.8% |
| R2 delta-P&L = gamma-P&L, `½ Γ₁ σ_on` | 0.45mm | **0.54mm** | 2.7% | PM **too tight** by 20.8% |
| R3 theta-anchored, `θ_w /(σ_on S)` | 0.68mm | **0.57mm** | 2.8% | PM **too loose** by 17.2% |

Reproduced on my own build of the reference book (EURUSD 1M ATM straddle, EUR 10mm/leg, spot
1.1650, ATM 7.944%): `Γ₁ = EUR 3.5025mm/1%`, `Γ = dΔ/dS = 300.65e6`, `θ = USD −3,504/cal day`,
window `var_fraction = 0.38196`, `D_cal = 0.5833`, `σ_on = 0.3093% = 36.0 pips`. Those differ
from the PM's stated `Γ₁ = 3.49mm` and `θ = −3,454` by 0.3% and 1.4% (tenor convention); every
number below is on mine and reproduces.

**The correction narrows the disagreement rather than widening it.** R2 and R3 converge to within
5% — and that is not luck:

```
R3 / R2 = 252 * D_cal / (365 * vf) = 1.047
```

which is the night's **theta paid per day of variance**, i.e. the inverse carry ratio. **R2 and R3
are the same rule under two anchors.** They coincide exactly when the night is carry-neutral, and
they diverge by precisely as much as the night is expensive. That is a feature: when they pull
apart, the night is telling you something.

---

## 2. The rules, derived, with their assumptions

Notation: `N` gross option notional (both legs), `T` tenor in years, `sigma` ATM, `vf` the
window's variance share, `D_cal` its calendar days, `S` spot, `Gamma = d(delta_base)/dS`,
`Gamma_1pct = Gamma * 0.01 S`, `lambda = cost_bp / 2 / 1e4` (one-way), `sigma_on = sigma sqrt(vf/252)`.

### R1 — one-sigma accumulation. `cap = Γ₁ · σ_on(%)` = **EUR 1.08mm**

*What it says.* Set the cap where an unhedged book would be after a one-sigma night. It then binds
only on nights that move more than one sigma.

*Assumption.* That being hedged once per one-sigma night is the right frequency. Nothing in R1
trades cost against risk, so it is a **scale, not an optimum** — and the measured efficient zone
sits well below it (§4.3). Its honest use is as a **ceiling**: a cap above R1 cannot bind.

*Scaling.* At the money `Γ₁ = 0.3989 N /(σ√T)`, so

```
R1 = 0.3989 * N * sqrt( vf / (252 T) )
```

Linear in notional, **1/sqrt(tenor)**, sqrt of the variance share, and **exactly independent of the
vol level** — `Γ` falls as `1/σ` and `σ_on` rises as `σ`. Verified: 1.0834mm from the closed form
against 1.0832mm measured off the repriced ladder.

### R2 — the unchosen bet equals the chosen one. `cap = ½ Γ₁ σ_on(%)` = **EUR 0.54mm**

*Derivation.* The night's gamma P&L is `½ Γ σ_on² S² = 50 Γ₁ σ_on(dec) S`. The directional P&L on a
delta `D` over a further one-sigma move is `D σ_on S`. Equate:

```
D = 50 * Gamma_1pct * sigma_on(dec)  =  0.5 * Gamma_1pct * sigma_on(%)
```

*Assumption.* That **the directional bet you did not choose should not be bigger than the convexity
bet you did.** No loss limit, no risk aversion, no view on how long you cannot deal — the fewest
assumptions of any rule here, which is why it is the shipped default.

*Scaling.* Exactly R1/2, so identical: linear in notional, `1/√T`, **vol-invariant**.

### R3 — theta-anchored. `cap = θ_window / (σ_on · S)` = **EUR 0.57mm**

*Assumption.* That the unchosen bet should be no bigger than the bill you have already agreed to
pay tonight. Same rule as R2 with the anchor moved from gamma to theta.

*Scaling.* `R3 = 0.1995 N D_cal / (365 √T √(vf/252))` — also **vol-independent** (theta ∝ σ and
`σ_on` ∝ σ). Note it is **inverse** in the variance share where R1/R2 are direct in it: a quieter
window wants a *looser* theta-anchored cap, because the same theta is being defended against a
smaller move.

### R4 — tail budget (the better-founded version of R3). **EUR 0.83mm** at the default budget

R3 hides two things: a **1-sigma** move (an implicit tail statistic) and the **whole night's**
sigma (an implicit horizon). Neither is right. The delta you wake up holding is only genuinely
dangerous for as long as you cannot deal — the open gap plus however long it takes you to get to
the screen. Make both explicit:

```
CVaR_95(D * S * g)  =  D * S * sigma_m * phi(z95)/(1-0.95)  =  2.0627 * D * S * sigma_m
D  =  L / (2.0627 * sigma_m * S)
```

with `L` the user's overnight loss limit (`docs/11` §5.2 already requires one for the short-gamma
refusals) or, absent one, the window theta. At `sigma_m` = one London-open hour, `D = 0.83mm`.

*Assumption, and it is the weak point.* The horizon does the work: with `sigma_m = sigma_on` it
returns 0.26mm; with one hour, 0.83mm. **That 3.2x range is exactly why §4.5 goes and measures the
morning instead of asserting it** — and the measurement says the morning barely matters on a
long-gamma book, which retires the question rather than settling it.

### R5 — premium at risk. **EUR 8.6mm.** Reported, and rejected

`D = x * premium / (2.0627 sigma_m S)`. At 10% of the EUR 10mm/leg straddle's USD 211k premium it
returns 8.6mm — eight times the delta the book can even accumulate. Worse, **it scales the wrong
way**: premium goes as `√T` while every other rule goes as `1/√T`, so it would hand a 3M book a
cap 3.6x a 1W book's when the 1W book accumulates 2.1x more delta per pip. A stock is the wrong
anchor for a flow. It is printed so the reader can see it fail, not used.

### R6 — short gamma: the gap loss inside the loss limit. **Derived in §5**

### F — the cost floor. `2 λ S Γ` = **EUR 0.175mm**

Not a cap: a hard floor. A rehedge triggered by a move `h` converts `½|Γ|h²` and pays `λS|Γ|h`, so
it **loses money outright for `h < 2λS`, regardless of risk aversion** (`docs/09` §4.3). In delta
units that is `2λSΓ`. At the user's retail 5bp it is EUR 0.175mm — not a rounding error.

---

## 3. How it was measured

A one-night simulator, `night.py` (scratch), that reprices the book with the shipped pricer
(`models.gk.gk_greeks_array`) at every bar and books P&L with the shipped accounting
(`equity = cash + option_pv + spot_pos * S`; night P&L = the change in equity, so theta, gamma,
hedge P&L and cost are all in it and nothing can be double counted).

* **Bars.** The window is cut into 5-minute bars whose *variance* weights come from
  `overnight.hour_profile` — so Asia is quiet and the London open is not, and the cap binds when the
  variance actually arrives rather than uniformly. Total window variance is
  `sigma² * vf/252` by construction.
* **Rule.** Hedge back to target whenever `|delta| > cap`, filled at the **bar close** — the shipped
  engine's convention, "you see the price and you deal on it", which charges the rule for the
  overshoot. §3.2 explains why it is not filled at the level.
* **Common random numbers** across every cap, so `cap vs cap` differences are paired and their
  standard error is the se of the *paired* difference.
* **Costs** are the retail table (`bandopt.RETAIL_COST_BP`, EURUSD 5.0bp = 5.83 pips round trip),
  because this user has no OTC access. §4.11 sweeps it.
* n = 40,000 nights for headline numbers, 20,000 for sensitivities.

### 3.1 Verified against the shipped engine, bit for bit

Same convention as `ratchet.verify_against_engine`: build a path, run it through
`backtest.engine.run_backtest` with `HedgeRule(mode="band", band_delta=cap)`, and difference.

```
engine   -1505.27  sim   -1505.27  diff  0.0000  hedges 1/1
engine    -953.80  sim    -953.80  diff  0.0000  hedges 2/2
engine    -925.76  sim    -925.76  diff  0.0000  hedges 2/2
engine     161.75  sim     161.75  diff -0.0000  hedges 4/4
engine   -1329.81  sim   -1329.81  diff  0.0000  hedges 2/2
engine   -1008.59  sim   -1008.59  diff  0.0000  hedges 2/2
```

So the costs below are charged by the shipped accounting and the marks are the shipped pricer's.
(The first attempt differed by USD 2-5 a night; the cause was that my book used
`conventions.year_fraction` to the NY10 cut while the engine strikes its DNS at `tenor_days/365`.
Aligning the conventions produced the zeros above — worth recording because a 0.3% difference in
`T` is invisible until you difference two accounting chains.)

### 3.2 A manufactured effect, caught. **This project's sixth**

`docs/12` §4 records three control bugs and `docs/10` §6.1 two more, and warns that any positive
result here should be assumed to be a bug until someone has tried to kill it. Here is a sixth,
found the same way — by a null control.

I first modelled the fill **at the resting level**, which is what a limit order actually does, by
solving for the spot at which delta equals the cap and booking the trade there. It produced a clean,
plausible, monotone result: tightening the cap cost USD 430-990 a night **over and above the
transaction cost**, scaling neatly with the cap. It would have been the headline.

It is an artefact. The path is only observed at bar closes, so the fill is *conditioned on the close
having crossed the level* and then *priced at the level*. That makes

```
E[ S_end - fill_px | filled ]  =  E[ overshoot ]  >  0
```

against you on every single clip — a phantom loss proportional to the cap, in exactly the shape a
real effect would have. The **zero-cost null control** (with `lambda = 0` the mean night P&L must be
independent of the cap, by the band-independence identity of `docs/09` §2) read **t = −51**.

The fix is to fill at the close, the shipped engine's convention, which is internally consistent
because the fill price and the marking price are the same observation. The price of that consistency
is overshoot, which is why the bar grid has to be fine and why §4.1 reports grid invariance.
Re-run, 100,000 paths, three seeds:

| cap | vs uncapped, zero cost | se | t |
|---|---|---|---|
| 0.20mm | −1.0 | 19.4 | −0.1 |
| 0.40mm | +1.6 | 18.9 | +0.2 |
| 0.80mm | −1.0 | 17.2 | −0.1 |
| 1.60mm | +1.0 | 12.2 | +0.2 |

**The null control passes.** The band/cap-independence of the expected P&L is reproduced through a
full option repricing — a stronger check than the AR(1) kernel in `docs/12` §2, because gamma is not
held constant.

*If you take one methodological point from this document, take this one: the fill convention in a
hedging simulation is not a detail. Filling at the level on close-observed bars manufactures a loss
that looks exactly like the result you are looking for.*

---

## 4. Results — long gamma

### 4.1 The sweep

EURUSD 1M ATM straddle, EUR 10mm/leg, **long**. Retail 5.0bp. 5-minute bars, n = 40,000, CRN.

| cap (mm) | mean | sd | **CVaR-95** | cost | fills | binds % | p95 max\|Δ\| | p95 wake Δ |
|---|---|---|---|---|---|---|---|---|
| 0.10 | −2,139 | 130 | −2,363 | 2,046 | 46.5 | 100 | 0.1 | 0.1 |
| 0.15 | −1,687 | 147 | −1,934 | 1,595 | 27.2 | 100 | 0.1 | 0.1 |
| 0.20 | −1,390 | 188 | −1,721 | 1,296 | 17.7 | 100 | 0.2 | 0.2 |
| 0.25 | −1,181 | 244 | −1,624 | 1,087 | 12.4 | 100 | 0.2 | 0.2 |
| **0.30** | −1,028 | 307 | **−1,598** | 933 | 9.1 | 100 | 0.3 | 0.3 |
| 0.40 | −818 | 439 | −1,639 | 725 | 5.5 | 99.9 | 0.4 | 0.3 |
| **0.55 (recommended)** † | **−633** | — | **−1,788** | **533** | 3.2 | 99 | 0.6 | 0.4 |
| 0.60 | −584 | 709 | −1,870 | 490 | 2.6 | 95.9 | 0.6 | 0.5 |
| 0.75 | −483 | 916 | −2,031 | 385 | 1.7 | 86.9 | 0.8 | 0.6 |
| 0.90 | −407 | 1,120 | −2,044 | 312 | 1.1 | 74.6 | 0.9 | 0.7 |
| 1.10 | −332 | 1,370 | −2,047 | 242 | 0.7 | 57.9 | 1.1 | 0.9 |
| 1.40 | −257 | 1,722 | −2,049 | 165 | 0.4 | 36.5 | 1.4 | 1.1 |
| 1.80 | −181 | 2,126 | −2,049 | 97 | 0.2 | 17.7 | 1.8 | 1.4 |
| 2.40 | −135 | 2,494 | −2,049 | 32 | 0.04 | 4.5 | 2.3 | 1.8 |
| 3.00 (**shipped 15% default**) | −107 | 2,675 | −2,049 | 8 | 0.01 | 0.9 | 2.4 | 2.0 |
| no cap | −96 | 2,764 | −2,049 | 0 | 0 | 0 | 2.4 | 2.1 |

† the 0.55mm row is from the separate cost-tier run (n = 20,000, §4.11), not this grid; every other
row is the n = 40,000 sweep. Deltas are EUR mm and are printed at the precision the run reports.

**Is the objective flat in the cap?** No. Across the **defensible range** (the floor 0.26mm to the
ceiling 1.08mm) the expected night moves by **USD 696** — −1,028 at 0.30mm against −332 at 1.10mm —
and across the whole swept range by **USD 2,043**. §4.14 states this in `docs/09` §5's own units so
it is directly comparable with the band result, which is the only comparison that means anything.

**And the mechanism is exactly the one theory predicts.** The paired difference against no cap
reproduces minus the transaction cost, cell by cell:

| cap | paired Δmean vs no cap | measured cost | ratio |
|---|---|---|---|
| 0.30mm | −932.4 ± 13.5 | 932.7 | 1.000 |
| 0.50mm | −584.3 ± 13.2 | 587.7 | 1.006 |
| 0.75mm | −387.0 ± 12.6 | 385.0 | 0.995 |
| 1.10mm | −235.8 ± 11.3 | 241.5 | 1.024 |

**The cap's entire effect on the expected overnight P&L is minus what it costs to trade.** That is
`docs/09` §7's claim, verified for the cap dial through a full reprice rather than a quadratic.
It also means the cap cannot be chosen by maximising the expectation — the expectation is maximised
by never hedging — and it is why the rest of this section is about the tail.

### 4.2 Grid invariance

The CVaR-95 argmin does not move with the monitoring frequency; its *level* does, because a finer
grid harvests more local time and pays for more of it.

| cap | 60-min bars | 30-min | 15-min | 5-min |
|---|---|---|---|---|
| 0.20 | −1,795 | −1,732 | −1,702 | −1,720 |
| **0.30** | **−1,793** | **−1,704** | **−1,633** | **−1,589** |
| 0.40 | −1,861 | −1,768 | −1,701 | −1,633 |
| 0.50 | −1,952 | −1,851 | −1,806 | −1,744 |

Argmin at 0.20-0.30mm at every resolution over a 12x range. Same invariance `docs/09` §4.7 reports
for the band argmax under 8/24/48 steps per day. The measured *cost* still sits about 15-25% below
the continuous-monitoring analytic `λSΓ²V/D` at 5-minute bars, which is the same discretisation gap
`docs/09` records, and `deltacap._analytic_cost` reports the analytic unfudged as the upper bound
it is.

### 4.3 The efficient frontier — and it has a shape

Walking the frontier from wide to tight, the marginal exchange rate is **USD of expected P&L given
up per USD of CVaR-95 gained**:

| cap (mm) | mean | CVaR-95 | Δmean | ΔCVaR | **USD mean / USD tail** | |
|---|---|---|---|---|---|---|
| 4.00 | −96 | −2,049 | −1 | 0 | — | **DOMINATED** |
| 3.00 | −107 | −2,049 | −11 | 0 | — | **DOMINATED** |
| 2.40 | −135 | −2,049 | −28 | 0 | — | **DOMINATED** |
| 1.80 | −181 | −2,049 | −47 | 0.02 | 2,111 | decorative |
| 1.40 | −257 | −2,049 | −76 | 0.2 | 319 | decorative |
| 1.10 | −332 | −2,047 | −74 | 1.2 | 59 | decorative |
| 0.90 | −407 | −2,044 | −75 | 3.3 | 23 | |
| 0.75 | −483 | −2,031 | −76 | 13 | 5.9 | |
| **0.60** | −584 | −1,870 | −101 | 161 | **0.63** | **cheap** |
| **0.50** | −680 | −1,753 | −97 | 117 | **0.83** | **cheap** |
| 0.40 | −818 | −1,639 | −138 | 115 | 1.20 | |
| 0.30 | −1,028 | −1,598 | −211 | 41 | 5.2 | |
| 0.25 | −1,181 | −1,624 | −152 | −27 | — | **DOMINATED** |
| 0.20 | −1,390 | −1,721 | −209 | −97 | — | **DOMINATED** |
| 0.15 | −1,687 | −1,934 | −298 | −213 | — | **DOMINATED** |
| 0.10 | −2,139 | −2,363 | −452 | −429 | — | **DOMINATED** |

Bootstrap standard errors on the CVaR-95 (200 resamples) are 0.1-4.0 USD, so the shape is
comfortably resolved.

Three preference-free statements come out of this table, and they are the only preference-free
statements available:

1. **Below EUR 0.30mm the cap is strictly dominated.** Tightening makes the expected P&L *and* the
   5% tail worse. No risk aversion can justify it: it is paying spread to make the night worse in
   both directions.
2. **Above about EUR 1.4mm the cap is strictly dominated by having no cap at all.** It costs money
   and improves the tail by literally zero, because the tail nights are the quiet ones and a cap that
   loose never binds on them.
3. **Between EUR 0.40mm and 0.75mm the tail is cheapest**, at 0.6-1.2 USD of mean per USD of tail
   against 5-6x that on either side. The derived R2 (0.54mm) and R3 (0.57mm) land inside it.

What is **not** preference-free: whether to buy the tail at all. Averaged from no cap to 0.54mm the
rate is about **2 USD of mean per USD of tail**, and a mean-CVaR objective `mean + k·CVaR` prefers
capping only for `k > 2` — i.e. only if you weight the 5% tail more than twice the mean. Traders do
(`docs/11` §4.7 argues exactly that, on institutional rather than psychological grounds). But it is
a preference, and the tool's job is to price it, not to disguise it as an optimum.

### 4.4 A closed form for the floor, and it is a new object

The dominance floor is the CVaR-95 argmin, and it can be derived. The 5% tail of a **long**-gamma
night is the *quiet* night, so what the tail wants is not less variance but **more of the window's
quadratic variation actually converted into cash**. Split the night into `n = V/h²` legs; the
captured `Σ leg²` has mean `V` at every band (that is §4.1's identity) but concentrates like
`chi²_n / n`, so its lower tail is `V(1 − z√(2/n))` with `√(2/n) = h√2/√V`. Therefore

```
tail(h)  ~  0.5*G*V  -  (z/sqrt2)*G*sqrt(V)*h  -  lambda*S*G*V/h  -  theta_w
```

and `d/dh = 0` gives

```
h*  =  S * sqrt( sqrt(2)/z * lambda * sigma_on )        D* = |Gamma| * h*
```

**The tail-optimal band is the geometric mean of the spread and the window sigma**, where the
mean-variance band (`docs/09` §4.2) is a cube root of cost over risk aversion. Different object,
different exponent, and it does not contain a risk-aversion parameter at all.

| cost | h* (pips) | **D\* predicted** | **D\* measured** |
|---|---|---|---|
| 0.2bp | 1.70 | 0.051mm | 0.030mm |
| 0.5bp | 2.68 | 0.081mm | 0.070mm |
| 1.0bp | 3.79 | 0.114mm | 0.100mm |
| 2.5bp | 6.00 | 0.180mm | 0.200mm |
| **5.0bp (this user)** | **8.48** | **0.255mm** | **0.280mm** |
| 10bp | 12.00 | 0.361mm | 0.550mm |
| 20bp | 16.96 | 0.510mm | 0.750mm |

Good to ~10% from 0.5bp to 5bp — the tier that matters — and **30-45% light above 10bp**, where the
chi-square approximation for a handful of legs gives out. Stated with its range of validity rather
than tuned. Empirically the floor scales as **cost^0.67**, between the band cubic's `λ^{1/3}` and
the breakeven clip's `λ^1`; its ratio to the breakeven clip `2λSΓ` therefore falls from 4.3 at
interbank to 1.1 at 20bp, so **"a couple of times the breakeven clip" is not a scaling law** and the
formula above should be used instead.

### 4.5 The delta you wake up holding — I went looking for the cap's payoff and it is not there

Folklore, and the framing of this whole feature, says the cap earns its keep because a delta you did
not choose is dangerous. So: extend the measurement past 07:00. The user cannot deal for `M` hours
after the open (or the market gaps), and only then flattens at market.

| morning | CVaR-95 argmin | CVaR at argmin | CVaR uncapped | gain from capping | mean cost of that cap |
|---|---|---|---|---|---|
| none (window only) | 0.30mm | −1,619 | −2,064 | 445 | 750 |
| 1h London open, diffusive | 0.30mm | −1,717 | −2,211 | 494 | 774 |
| 3h, diffusive | 0.30mm | −1,965 | −2,506 | 541 | 794 |
| 5% chance of a 30-pip gap | 0.30mm | −1,614 | −2,064 | 449 | 756 |
| 2% chance of a 60-pip gap | 0.30mm | −1,617 | −2,064 | 446 | 752 |

**Three unhedgeable hours, or a 60-pip gap, move the answer by under USD 100 and do not move the
optimum at all.** Two reasons, and both are worth saying out loud:

1. **The tail nights are the quiet nights, and on a quiet night you are not carrying much delta.**
   The residual delta is *positively* correlated with a good night for a long-gamma book, so it
   loads onto the middle and the right of the distribution, not the left 5%.
2. **The gamma that created the delta also insures it.** Carry EUR 2.4mm into a 150-pip gap and the
   delta costs USD 36,000 while the convexity hands back `½ * 300.6e6 * 0.015² = USD 33,800`. That
   near-cancellation *is* what being long gamma means, and it is invisible in any framing that
   treats the overnight delta as a naked directional position.

So on a long-gamma book, the standard justification for the cap does not survive measurement. The
cap's measurable value is the §4.3 conversion effect, and the rest of its value — not waking up to a
position you did not choose, not having the risk conversation — is real and institutional and is
`docs/11` §4.6/§4.7's territory, not something this document can price.

**This is the one result here that cuts against the project's framing, and it is the reason R4's
"hours you cannot deal" parameter is exposed and defaulted rather than tuned.**

### 4.6 Where the cap binds

`deltacap` prints the binding frequency from the exact driftless two-sided first passage
`1 - (4/pi) Σ (-1)^k/(2k+1) exp(-(2k+1)²π²sd²/(8a²))`. Against the sweep:

| cap (mm) | 0.40 | 0.50 | 0.60 | 0.75 | 0.90 | 1.10 | 1.40 | 1.80 | 2.40 | 3.00 |
|---|---|---|---|---|---|---|---|---|---|---|
| formula % | 100.0 | 99.6 | 97.7 | 90.3 | 78.7 | 61.5 | 39.2 | 19.3 | 5.3 | 1.1 |
| measured % | 99.9 | 98.9 | 95.9 | 86.9 | 74.6 | 57.9 | 36.5 | 17.7 | 4.5 | 0.9 |

Within 4pp everywhere and always slightly above, because continuous monitoring catches crossings a
5-minute grid misses. At the recommended EUR 0.54mm the cap binds on **~99% of nights** — which is
the point: the cap, not the analytic band, is what sets the ladder, exactly as `docs/09` §7.2 and
`docs/12` §8.2 both concluded. (For context, `docs/12` reported the ratchet binding a EUR 1.0mm cap
on 32-46% of nights against 9-18% for its symmetric baseline; on this book a EUR 1.0mm cap binds
about 66% of the time under a plain band, so those figures are not comparable across books and
should not be read as a level.)

### 4.7 The vol mark, which this user cannot get right

Mark the book a full vol point either side while the night realises 7.944%:

| mark | Γ₁ | θ/day | σ_on | **R1** | **R2** | **R3** | expected night at R2 |
|---|---|---|---|---|---|---|---|
| 6.94% | 4.001mm | −3,059 | 0.2703% | **1.082** | **0.541** | **0.567** | −273 |
| 7.94% | 3.498mm | −3,499 | 0.3093% | **1.082** | **0.541** | **0.567** | −649 |
| 8.94% | 3.107mm | −3,940 | 0.3482% | **1.082** | **0.541** | **0.567** | −1,005 |

**The cap does not move at all.** Not approximately — exactly, to four significant figures, because
`Γ ∝ 1/σ`, `σ_on ∝ σ` and `θ ∝ σ`, and the cancellations are algebraic. Γ₁ moves 29% across that
range and the cap moves 0.0%.

For a user with no broker curve, marking off CME settlements or an ETF chain with a 0.2-1.5 vol
basis (`docs/11` §6.1), **this is the most useful property in the module**: the one input they cannot
get right is the one input the cap does not need. It also completes `docs/11` §6.2's picture — the
trader found the *levels* robust to the mark and the *money* not; the *cap* is robust too, and the
money moves about **USD 375 a vol point**, which is a position decision (`crossover_vol`), not a
cap decision.

What is *not* mark-invariant is the **floor** (`D* ∝ σ^{-1/2}`) — a book marked too low looks like it
has more gamma and its dominance floor rises. Over ±1 vol point that is ±7%, inside the grid.

### 4.8 The shipped 15%-of-gross default is wrong for overnight, plainly

`zones.DEFAULT_BAND_PCT = 15` gives `0.15 x 20mm = EUR 3.00mm` on the reference book. That is

* **5.6x** the derived cap (R2, 0.54mm),
* **2.8x** the delta the *unhedged* book accumulates over a whole one-sigma night (R1, 1.08mm),
* **2.8-5.6x** every rule in §2 (3.4-6.7x the PM's uncorrected candidates), and above **every**
  cap in the measured efficient zone.

Measured (n = 20,000, matched paths):

| cap | mean | CVaR-95 |
|---|---|---|
| EUR 3.00mm (the default) | −113 | −2,049 |
| no cap at all | −103 | −2,049 |

It costs **USD 10 a night and improves the 5% tail by exactly zero.** It is not a loose risk limit;
it is **strictly dominated by having no cap at all**, and it binds on 0.9% of nights. The default is
an *intraday* number, sensible enough for a desk hedging by hand in London where the band is
supposed to be wide and the trader is watching. Used overnight it is not a band. `deltacap` emits
this as a warning whenever it is what the caller would otherwise have run, and
`override_effect(cap, 3.0e6)` returns the verdict `DECORATIVE`.

**Recommendation to whoever owns `zones.py`:** do not change `DEFAULT_BAND_PCT` — it is a daytime
default and changing it would move the intraday screen. Instead, have the overnight screen refuse to
fall back to it, which `overnight_ladder` already half-does (it appends a `[cap NOT supplied]` note).
`deltacap.recommend_cap` exists so there is a derived number to fall back to instead.

### 4.9 Roughness — the optimum is invariant, the value of hedging is not

`docs/10` ruled path roughness unforecastable from daily bars and `docs/12` reproduced that at hourly
frequency; the Brownian baseline `kappa = 1` is what ships. But the §4.3 tail gain *is* a harvest of
local time, so it must depend on roughness. AR(1)-in-returns paths, **per-bar variance equalised**
so the quadratic variation is identical in every cell and only the ordering of the moves differs:

| φ | CVaR argmin | CVaR at argmin | CVaR uncapped | mean at 0.40mm | mean uncapped |
|---|---|---|---|---|---|
| −0.20 (choppy) | 0.30mm | −1,754 | −2,050 | −1,144 | −733 |
| −0.10 | 0.30mm | −1,673 | −2,049 | −985 | −438 |
| 0.00 | 0.30mm | −1,588 | −2,049 | −819 | −86 |
| +0.10 | 0.30mm | −1,498 | −2,049 | −634 | +342 |
| +0.20 (trending) | 0.30mm | −1,403 | −2,048 | −434 | +874 |

**The argmin does not move across the whole range.** The *level* moves a lot — a choppy night makes
capping look better in the tail and much worse in the mean, a trending night the reverse — which is
`docs/12` §2's table in a different metric. So: **where to put the cap is roughness-invariant; what
capping is worth is not.** That is the right split, because roughness cannot be forecast for tonight
and the cap has to be set anyway. Consistent with `docs/12`'s verdict from the other side: the
symmetric band at the delta cap is the answer, and nothing conditional on tonight's regime ships.

### 4.10 Skewed books — the linear proxy is not 2% wrong, it is 260% wrong

`deltacap.accumulated_delta` reads the delta accumulated over ±1 window sigma off a **full
repricing** (`risk.spot_ladder`), which `overnight`/`zones` already own. Against
`|Gamma| * sigma_on * S`:

| book | Γ₁ | accum **up** | accum **down** | linear proxy | up/dn | proxy error |
|---|---|---|---|---|---|---|
| ATM straddle 10mm/leg | +3.503mm | 1.0799mm | 1.0799mm | 1.0832mm | 1.000 | **0.3%** |
| ATM straddle, at 3σ | | 3.1619 | 3.1616 | 3.2497 | 1.000 | 2.7% |
| **25d risk reversal** (long call / short put) | +0.048mm | **0.0544mm** | **0.0244mm** | 0.0151mm | **2.23** | **261%** |
| 1x2 call spread (short the upper) | −1.049mm | 0.3673mm | 0.2823mm | 0.3246mm | 1.30 | 13% |
| 1x2 call spread, at 3σ | | 1.3490 | 0.6076 | 0.9739 | **2.22** | 39% |

The trader's prediction (`docs/11` §3.3: *"the quants will size clips off a single Γ₁ and the ladder
will be symmetric on a skewed book — that is wrong and it is visible on the first RR position"*) is
confirmed with a number. On a 25-delta risk reversal the up-side and down-side caps differ by
**2.2x**, and the single-Γ₁ proxy is out by **3.6x** in level — because a risk reversal's net gamma
at the money is nearly zero while its *accumulation* is not.

`DeltaCap` therefore carries `cap_up` and `cap_dn`, and prints the source in words, as `docs/11` §9.3
demands: *"down-side cap 0.024mm is 55% smaller than the up-side 0.054mm because your book
accumulates less delta on the way down. THAT IS YOUR STRIKES, NOT A VIEW."* The scalar `cap_base` is
`min(up, dn)` — the number to type when the platform takes one, chosen conservatively so a single
number never under-protects the tighter side — and the per-side numbers are the correct answer.

`recommend_cap` also detects two degenerate cases that the reference book hides. When the book's
gamma **changes sign across the window** (a one-sided strike ladder, or the risk reversal above) it
warns that a single scalar cap is the wrong object and that the order type must come per rung from
the local gamma sign. And when the **smallest dealable clip exceeds the whole night's accumulation**
— EUR 0.10mm against EUR 0.024mm on the risk reversal — it says `NO OVERNIGHT LADDER`: there is
nothing to hedge and no cap will change that. `zones.hedge_bands` would have printed EUR 3.00mm.

### 4.11 Cost — the input that owns the answer

| cost | round trip | cap 0.55mm: mean | CVaR-95 | cost/night | **USD mean per USD tail** |
|---|---|---|---|---|---|
| 0.2bp (interbank) | 0.23 pips | −121 | −1,699 | 21 | **0.05** |
| **1.03bp** (the trader's own 0.6-pip figure, `docs/12` §7) | 1.2 pips | −206 | −1,714 | 107 | **0.31** |
| 2.5bp | 2.9 pips | −366 | −1,741 | 266 | **0.85** |
| **5.0bp** (repo retail default) | 5.8 pips | −633 | −1,788 | 533 | **2.03** |
| (no cap, any tier) | | −103 | −2,049 | 0 | — |

**The cost tier changes the character of the recommendation, not just its size.** At the trader's
own number the cap is cheap insurance and an easy yes; at the repo's retail default it is expensive
and a genuine judgement call. The *cap itself* does not move with cost — R1/R2/R3 contain no cost
term — only the **floor** does (§4.4). So a wrong cost estimate changes what the cap **costs**
without changing what it should **be**, which is a comfortable place to be wrong.

**RFC-3 — the repo contradicts itself on this user's cost, by ~5x.** `docs/11` §4.4 says both
*"0.4-1.0 pips all-in on EURUSD"* (≈0.34-0.86bp) **and** *"15-40x the table"* (≈3-8bp on
`zones.COST_BP["EURUSD"] = 0.2`). `docs/12` §7 works at **1.03bp**; `bandopt.RETAIL_COST_BP` ships
**5.0bp**. Those are not the same number and the difference is the difference between a cap that is
obviously worth having and one that is arguable. `docs/11` §10's blocking question 1 is the right
question and it has not been answered. Until it is, `deltacap` defaults to 5.0bp (the repo's own
retail table, the conservative end) and says loudly that it is an assumption.

### 4.12 Realised vol away from implied

| realised | CVaR argmin | mean, no cap | mean at 0.55mm | cheapest rate in the sweep |
|---|---|---|---|---|
| 0.7x implied (5.56%) | 0.30mm | −1,074 | −1,347 | 1.08 at 0.40mm |
| 1.0x | 0.30mm | −60 | −623 | 0.57 at 0.55mm |
| 1.4x implied (11.1%) | 0.40mm | +1,834 | +754 | 0.33 at 0.75mm |

The efficient cap widens when realised runs hot and tightens when it runs cold, which is the right
direction and mild: the 0.40-0.75mm zone survives a 2x range of realised vol. Note the *level*
of the night is dominated by realised-vs-implied, not by the cap — a dead-vol regime is a position
problem, and `docs/11` §4.8 is right that the answer is to sell the gamma, not to hedge it harder.

### 4.13 Is the cap the right dial? A matched comparison against the clock

A tighter cap trivially reduces variance, so any "cap A beats cap B" claim has to be at matched risk
or stated as a preference — §4.3 does the latter honestly. But there is a genuine matched comparison
available: **is the cap doing anything a hedging *clock* could not?** Score a time rule (hedge to
flat every k bars, no cap) against the cap rule at **matched expected P&L**:

| rule | mean | CVaR-95 | fills | |
|---|---|---|---|---|
| TIME every 20 min | −1,673 | −2,180 | 42.0 | |
| TIME every 1h | −1,012 | −1,929 | 14.0 | |
| **CAP 0.40mm** | **−817** | **−1,626** | 5.5 | **beats it on BOTH** |
| TIME every 2h | −749 | −1,959 | 7.0 | |
| **CAP 0.55mm** | **−628** | **−1,804** | 3.1 | **beats it on BOTH** |
| TIME every 4h40 | −521 | −2,045 | 3.0 | |
| TIME once, at 14h | −376 | −2,064 | 1.0 | |

No interpolation is needed: **the cap strictly dominates the clock at two separate points** — EUR
0.40mm has both a better mean (−817 vs −1,012) and a better tail (−1,626 vs −1,929) than hedging
hourly, on a third of the trades, and EUR 0.55mm does the same against a two-hourly clock. So the
cap is not a re-parameterisation of "hedge more often": it is state-dependent where a clock is not,
and it spends its trades where the delta actually is. This is the classical band-beats-clock result
(`docs/09` §4.4's fixed-grid comparison from the other side), reproduced on the tail rather than the
variance, and it is here as a **control** — if it had failed, the whole dial would have been
suspect.

### 4.14 The cap against the band, in the band's own units — and reconciling the trader's USD 81

`docs/09` §5 prices the band as *"being 30% off in band width costs ~0.1% of the gamma P&L at
interbank cost and ~0.9% at retail"*. Same question, same units, for the cap: hold the book fixed
and move the cap ±30% around the derived EUR 0.54mm.

| cost tier | | cap 0.378mm | **0.540mm** | 0.702mm | −30% | +30% |
|---|---|---|---|---|---|---|
| **1.03bp** | mean | −252 | **−211** | −187 | −41 (**−2.1%**) | +24 (**+1.2%**) |
| | CVaR-95 | −1,400 | **−1,701** | −2,019 | +301 (**+15.4%**) | −318 (**−16.3%**) |
| **5.00bp** | mean | −856 | **−641** | −515 | −215 (**−11.0%**) | +127 (**+6.5%**) |
| | CVaR-95 | −1,624 | **−1,780** | −2,019 | +157 (**+8.0%**) | −238 (**−12.2%**) |

Percentages are of the window's gamma P&L (USD 1,951), exactly as `docs/09` §5 computes them.

**Being 30% off the cap moves the night by 1.2-16.3% of its gamma P&L. Being 30% off the band moves
it by 0.1-0.9%.** One to two orders of magnitude — and note that the result does **not** depend on
which cost tier you believe. At the trader's own 1.03bp the mean barely moves (−2.1%/+1.2%) and the
*tail* moves 15-16%; at the repo's 5bp the mean moves 11% and the tail 8-12%. The dial is loud at
both tiers; only *which* moment it is loud in changes. That is the strongest form the "the cap
dominates" claim can take, and it is the form the band result can be compared against.

**Reconciling the trader's USD 81** (`docs/11` §4.3). The trader's table sweeps a band of 15 / 30 /
45 pips at 0.6 pips all-in and gets costs of USD 122 / 61 / 41. On their book (Γ₁ = 3.91mm,
EUR 33.5k of delta per pip) those bands **are** delta caps of 0.50 / 1.00 / 1.51mm. Same sweep here,
at 1.03bp:

| cap | 0.50mm | 1.00mm | 1.50mm | range |
|---|---|---|---|---|
| measured cost | USD 121 | USD 56 | USD 30 | **USD 90** (trader: 81) |

Within 11% of a hand calculation done independently, from a different starting point, on a slightly
different book. **The trader's "the entire cost range across every band a desk would consider is
USD 81" was already a statement about the cap, not the band** — they had the right dial and priced
its cost component correctly. What the measurement adds is (i) that the same dial moves the *tail*
by 15% of the night's gamma P&L at that same cost tier, which their arithmetic could not see, and
(ii) that at the repo's own retail table it moves the *mean* by USD 696 rather than USD 81.

### 4.15 The frontier shape is not an artefact of choosing CVaR-95

n = 60,000. Best (least negative) cap under five different tail measures:

| cap (mm) | mean | p05 | CVaR-95 | CVaR-99 | CVaR-90 | p01 |
|---|---|---|---|---|---|---|
| 0.20 | −1,387 | −1,666 | −1,720 | −1,790 | −1,679 | −1,753 |
| 0.25 | −1,179 | −1,551 | −1,625 | −1,730 | −1,568 | −1,676 |
| **0.30** | −1,026 | −1,495 | **−1,591** | **−1,723** | **−1,519** | **−1,652** |
| **0.40** | −814 | **−1,494** | −1,633 | −1,816 | −1,529 | −1,713 |
| 0.50 | −679 | −1,595 | −1,748 | −1,977 | −1,605 | −1,789 |
| 0.75 | −478 | −1,987 | −2,030 | −2,051 | −1,960 | −2,049 |
| no cap | −88 | −2,044 | −2,049 | −2,051 | −2,041 | −2,051 |

**argmin: 0.30mm under CVaR-90, CVaR-95, CVaR-99 and the 1st percentile; 0.40mm under the 5th
percentile.** The dominance floor and the interior optimum survive every tail measure tried,
including the deep ones. The shape is a property of the problem, not of the statistic.

---

## 5. Short gamma — a different object, and more urgent

Same book, direction flipped. The orders are **stops**, not limits; they take liquidity, they slip,
and **not being filled is the loss rather than the safe outcome** (`docs/11` §5.3).

### 5.1 The sweep, with and without slippage

| cap (mm) | no slip: mean | CVaR-95 | | slipped 11.65p (2x spread): mean | CVaR-95 |
|---|---|---|---|---|---|
| 0.15 | −1,507 | −2,770 | | −7,895 | −11,222 |
| 0.30 | −852 | −2,559 | | −4,601 | −8,426 |
| 0.50 | −493 | −2,868 | | −2,841 | −7,420 |
| **0.75** | −292 | −3,467 | | **−1,837** | **−7,247** |
| 1.00 | −196 | −4,133 | | −1,297 | −7,314 |
| 1.40 | −90 | −5,321 | | −760 | −7,982 |
| 2.00 | +2 | −6,803 | | −281 | −9,231 |
| no cap | **+79** | **−8,775** | | +79 | −8,775 |

Everything inverts. The **mean rises** with the cap (the theta is the entire edge and every hedge
subtracts from it) and the **tail collapses**: CVaR-95 runs from −2,559 capped to **−8,775** uncapped,
a **USD 6,216** swing against USD 451 on the long book — **fourteen times** the effect, on a tail
three times as deep.

**But slippage changes the answer, and this is the part a naive read gets wrong.** With stops slipped
at twice the spread — `docs/11` §5.1.2's default, and optimistic — the tail optimum moves out to
**0.75mm** and everything tighter is *dominated*: below it both the mean and the tail get worse,
because the whipsaw of stopping in and out at 11.65 pips of slip costs more than the convexity it
saves. Against no cap at all, capping at 0.75mm buys USD 1,528 of tail for USD 1,916 of mean, a rate
of **1.25** — better than the long-gamma 2.0, but nothing like the 0.03-0.34 the no-slip table
suggests. **Do not quote the frictionless short-gamma numbers.** Under a jump (2% chance of a
100-pip gap, stops slipped) the optimum sits at 0.75-1.00mm and the same picture holds.

### 5.2 R6 — the rule that actually responds to the cap

Stopping out in clips of `D` through a jump of `g` cuts it into `n = g/h` legs with `h = D/|Γ|`, and
the un-hedged quadratic loss left inside the legs is `n * ½|Γ|h² = ½ g D` — **exactly linear in the
cap**. The turnover is `|Γ|g` whatever the cap, so spread and slippage cost `|Γ| g (Sλ + slip)` and
are **cap-independent**. Hence

```
loss(D)  =  0.5 * g * D  +  |Gamma| * g * (S*lambda + slip)          vs  0.5*|Gamma|*g^2 naked
D_max    =  2 * (L - friction) / g
```

Two things fall out, and both are decision-useful:

* the cap is the *only* term you control and it is **linear**, so halving it halves the convex part
  of the gap loss exactly — this is the one rule in this document whose tail responds to the cap
  one-for-one; and
* **the slippage floor does not respond to the cap at all.** Once `|Γ| g (Sλ + slip)` alone breaches
  the loss limit, **no cap can fix it** and the only answers are to reduce the position or buy the
  wing. `deltacap.short_gamma_cap` returns exactly that: the cap, or the fraction of the short gamma
  that has to go. That is `docs/11` §5.2's *"flatten 40% of the short gamma and the 99th-percentile
  loss falls inside your limit"*, made computable.

On the reference short book at a 3σ (108-pip) gap: the cap-free friction is **USD 4,732**; a
USD 25,000 limit gives `D_max = 3.75mm`, clamped to the 1.08mm the book can accumulate.

**It is optimistic by construction: it assumes every stop fills.** In the gap you are actually
worried about they do not. `docs/09` §8 declines to model slippage through a gap rather than produce
a comforting number, and this document does the same — read `loss(D)` as a **lower bound**.

### 5.3 Refusals

`recommend_cap` runs the trader's conditions (`docs/11` §5.2) before it returns a number, prints them
**above** the cap, and — this is the part that matters — **lists the ones it cannot check**:

*Evaluated:* indicative/unverified vol surface; window spans a weekend; a tier-3 event inside the
window; the 3σ gap loss with slipped stops against a stated `loss_limit`.
*Explicitly not evaluated:* pin risk at the next cut (`zones.pin_risk` owns it), the outermost clip
against what the pair trades at 03:00 (no liquidity data in the repo), stop-limit-only platforms (a
user setting), and the stop-fill assumption above.

A refusal list that silently drops the conditions it cannot check is worse than no refusal list, so
both lists print.

---

## 6. What this user should actually run

**On the reference book — EURUSD, 1M ATM straddle, EUR 10mm per leg, long gamma:**

> **Set your overnight delta cap to EUR 0.55mm.**
>
> That is half the delta your book picks up in a one-sigma night. It gives you an 18-pip ladder,
> it binds on about 99% of nights, and it means that when you sit down at 07:00 the biggest
> directional position you can be holding is worth about as much on the next one-sigma move as the
> whole night's gamma was worth. It is not a number I can call optimal, and here is what it costs:
> at your spread it takes about **USD 530 a night** off the expected result and hands back about
> **USD 260 of 5%-tail**. If you would rather have the money than the tail, run EUR 1.0mm and stop
> there — going wider buys nothing at all. **What you should not do is go below EUR 0.26mm**, where
> a tighter cap makes both the expected night and the bad night worse, or above EUR 1.4mm, which
> costs money and changes nothing.

**The scaling rule, so it stays right as the position changes:**

```
cap  =  0.199 * gross_notional * sqrt( var_fraction / (252 * T) )        [base ccy]
```

| what changes | what the cap does |
|---|---|
| gross notional | **linear** — double the position, double the cap |
| tenor | **1/sqrt(T)** — a 1W book gets **2.07x** the cap of a 1M at the same notional (it accumulates that much more delta per pip); a 3M gets **0.57x** |
| the window's variance share | **sqrt** — a weekend or an unusually quiet Asia session lowers it |
| **the vol level** | **nothing at all.** Gamma falls as 1/σ and the window sigma rises as σ; they cancel exactly. A full vol point of mark error moves the cap by <0.1% |
| your broker's spread | **nothing.** Only the *floor* moves (as cost^{2/3}), and only what the cap *costs* |
| book skew | **two numbers**, off the repriced delta profile — 2.2x apart on a 25-delta risk reversal |

Verified against the measurement across a 13x range of tenor, a 4x range of notional and a 2.2x
range of vol — R2 lands **inside the measured efficient zone in 7 of 7 cells**, and the zone itself
sits at a stable **0.74x to 1.39x the recommended cap** (shipped as
`deltacap.EFFICIENT_LO_MULT / EFFICIENT_HI_MULT` and printed on the panel):

| book | R1 | **R2** | R3 | measured efficient zone |
|---|---|---|---|---|
| 1W 10mm/leg | 2.24 | **1.12** | 1.18 | 0.80 – 1.50 |
| 1M 10mm/leg | 1.08 | **0.54** | 0.57 | 0.40 – 0.75 |
| 3M 10mm/leg | 0.62 | **0.31** | 0.32 | 0.23 – 0.43 |
| 1M 20mm/leg | 2.16 | **1.08** | 1.13 | 0.80 – 1.50 |
| 1M 5mm/leg | 0.54 | **0.27** | 0.28 | 0.20 – 0.38 |
| 1M 10mm at ATM 10.9% | 1.08 | **0.54** | 0.57 | 0.30 – 0.75 |
| 1M 10mm at ATM 4.9% | 1.08 | **0.54** | 0.57 | 0.55 – 0.75 |

**Three numbers to look at before you accept the cap**, in this order:

1. **The carry**, which is a position decision and has nothing to do with the cap. `−93 USD` on this
   book: it is marginally negative to hold this gamma overnight at all, and if `crossover_vol` says
   the marked ATM is above the crossover, the honest answer may be to sell the front gamma into the
   close rather than to leave better orders.
2. **What the cap costs** against **what the whole night's gamma is worth**. USD 533 against
   USD 1,951 is 27% of the night. That is a lot, and it is a lot *because of the spread*, not
   because of the cap. Answering `docs/11` §10's blocking question 1 is worth more than any
   refinement in this document.
3. **The clip against the one-sigma delta.** If the smallest clip you can deal is bigger than what
   the book accumulates in a whole night, there is no ladder to leave and the cap is not the thing
   that is wrong.

---

## 7. What to run on real data, and what would change the verdict

Everything here is synthetic. It validates the arithmetic, the code and the shape of the answer; it
does not settle any market question (`docs/10` §0's discipline).

1. **`overnight.estimate_hour_profile` on two years of your own hourly bars.** Every sigma in this
   document scales with `var_fraction = 0.3820`, which is a *modelled* level anchored on one
   measurement. `docs/12` §10 already gets 0.4212 on a different synthetic frame. The cap goes as
   `sqrt(vf)`, so a 10% error in `vf` is a 5% error in the cap — tolerable — but the *carry* and the
   crossover vol move much more.
2. **Your broker's actual all-in cost at 03:00, in pips.** §4.11 and RFC-3. This is the single
   largest uncertainty in the document and it is a question, not a research problem.
3. **The overnight gap distribution on your pairs.** §4.5's finding — that the delta you wake up
   holding is insured by the gamma that created it — is a *diffusion-plus-jump* statement tested at
   30-60 pips. It would change if real overnight gaps are much fatter than that, and that is
   measurable from the same hourly bars.
4. **Whether you ever run short gamma overnight** (`docs/11` §10, question 5), and your overnight
   loss limit. Without the limit, §5's refusals cannot fire and R6 has no budget.

**What would change the verdict wholesale:** a measurement that the overnight gap distribution has
enough mass beyond 3 sigma that the long-gamma convexity no longer offsets the carried delta. That
would move §4.5 from "the cap barely buys tail" to "the cap buys tail", and R4's explicit horizon
would become the right default instead of R2.

**What would change nothing:** a few months of live results. §4.1's night-to-night standard
deviation is USD 130-2,764 depending on the cap; distinguishing a USD 260 tail improvement inside
that would take thousands of nights, and `docs/12` §9 already did this arithmetic for the ratchet
and got 149 years.

---

## 8. Contract notes, and one thing I could not do

**Not my file, so flagged rather than touched:**

* **`fxgamma/portfolio/__init__.py`** does not export `deltacap`. `import fxgamma.portfolio.deltacap`
  works; the convenience re-export is a two-line change (`from . import deltacap`, and
  `recommend_cap`/`DeltaCap` in `__all__`). Requested, not made — this is the same courtesy the PM
  extended to `signals/__init__.py` in amendment three.
* **`zones.DEFAULT_BAND_PCT = 15`** should stay as it is (§4.8): it is a daytime default and moving
  it would change the intraday screen. What should change is that the *overnight* path never falls
  back to it. `overnight_ladder` already appends a `[cap NOT supplied]` note; with `deltacap` there
  is now a derived number to use instead, and the one-line change is for `overnight`'s owner.
* **RFC-3**, §4.11: the repo's three statements of this user's execution cost differ by ~5x.

**Everything in `deltacap.py` reuses rather than restates.** `TRADING_DAYS` from `zones`;
`book_gamma`, `cost_bp_for`, `RETAIL_COST_BP`, `risk_aversion_for_band` from `bandopt`;
`passive_window`, `PassiveWindow`, `RETAIL_LOT_BASE` from `overnight`; `spot_ladder`, `fx_rate` from
`risk`. No formula is duplicated — `docs/09` §4.2 records what happened the last time one was.

**Interface.**

```python
from fxgamma.portfolio import deltacap

cap = deltacap.recommend_cap(book, mkt, "EURUSD")       # or recommend_caps(book, mkt)
cap.cap_base            # -> feed to overnight_ladder(..., max_overnight_delta=)
cap.cap_up, cap.cap_dn  # -> per side; different on a skewed book
print(cap.reasoning)    # -> the panel, in words, with every rule and every warning
deltacap.override_effect(cap, 3.0e6)   # -> "DECORATIVE: ... improves CVaR-95 by zero"
deltacap.cap_frame(caps)               # -> one row per pair
deltacap.cap_scaling_note(cap)         # -> the paragraph in s6, for this book
```

The user never types a number; if they type one anyway, `override_effect` tells them which of the
three regions it lands in — **DOMINATED**, **DEFENSIBLE** or **DECORATIVE** — and what it changes.

---

## 9. Reproducing every number here

`PYTHONPATH=/home/user/fx`, offline, `get_provider("synthetic")`, `asof = 2026-09-09 16:00Z`.

| § | what to run |
|---|---|
| 1, 2, 6 | `deltacap.cap_candidates(book, mkt, "EURUSD")` and `recommend_cap(...).reasoning` |
| 3.1 | build a path, run it through `backtest.engine.run_backtest` with `HedgeRule(mode="band", band_delta=cap)` and difference against the night simulator — must be exactly 0 |
| 3.2 | the night sweep at `lam=0`: the paired difference against no cap must be 0 at every cap |
| 4.1-4.3 | the cap sweep, 5-min bars, n=40,000, common random numbers |
| 4.4 | `deltacap.tail_optimal_cap(bg, lam, sigma_window)` against the sweep's CVaR argmin, per cost tier |
| 4.6 | `deltacap._two_sided_first_passage(cap/\|Gamma\|, sd)` against the sweep's `bind_pct` |
| 4.14 | the same sweep at caps 0.378 / 0.540 / 0.702mm and at 0.50 / 1.00 / 1.50mm, at 1.03bp and 5.0bp |
| 4.15 | the same sweep at n = 60,000, scored under p05 / CVaR-90 / CVaR-95 / CVaR-99 / p01 |
| 4.10 | `deltacap.accumulated_delta(book, mkt, pair, sigma_window=...)` on a straddle, a 25d RR and a 1x2 |
| 5.2 | `deltacap.gap_loss(...)` and `deltacap.short_gamma_cap(...)` |

**The two checks to run first if anything stops reproducing** are the engine equivalence (§3.1, must
be exactly zero) and the zero-cost null control (§3.2, must be flat in the cap). If those pass and
the numbers move, the market model changed. If they fail, the code did — and §3.2 is a standing
reminder of how convincing a broken one can look.
