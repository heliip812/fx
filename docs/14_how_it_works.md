# 14 — How It Works: what the tool does, why, and the knowledge underneath

**Owner:** technical writer / analyst. **Audience:** you, the trader who runs the book.
**Status:** synthesis. Nothing here is new analysis; every number is carried from the document
named next to it, and where two documents disagree this one says so rather than picking (§9).

This is the one document to read to understand your own tool. It assumes you know what gamma is.
It explains only what *this* tool defines, computes or refuses to compute — and it is deliberately
blunt about how much of what you originally asked for turned out not to survive measurement.

**Contents:** [1 What it does](#1-what-it-does-in-one-page) · [2 The knowledge it
encodes](#2-the-knowledge-it-encodes) · [3 The overnight ladder](#3-the-overnight-ladder-explained-properly) ·
[4 What was tested and rejected](#4-what-was-tested-and-rejected-and-why-this-is-the-most-valuable-section) ·
[5 Hedging theory, honestly stated](#5-the-hedging-theory-honestly-stated) ·
[6 Data, and what you cannot have for free](#6-data-and-what-you-cannot-have-for-free) ·
[7 How it defends itself against being wrong](#7-how-it-defends-itself-against-being-wrong) ·
[8 Limits and what to do next](#8-limits-and-what-to-do-next) ·
[9 Where the documents disagree](#9-where-the-documents-disagree-open-for-the-pm)

---

## 1. What it does, in one page

Eight Dash screens, local, SQLite, free data only. Each answers exactly one question.

| # | Path | Screen | The single question it answers |
|---|---|---|---|
| 1 | `/` | Market Monitor | **Is gamma cheap or expensive today?** RV vs IV on matched horizons, vol cones, RR/BF z-scores, cross-pair ranking |
| 2 | `/surface` | Vol Surface | **What shape am I trading?** Smile by tenor, term structure, calibration residuals, arbitrage diagnostics |
| 3 | `/gamma-map` | Gamma Map | **Where are the listed strikes clustered?** CME open interest by strike and expiry, spot overlay. *Not* where the market is short gamma — it cannot know that |
| 4 | `/book` | Position Book | **What do I own?** Trade entry, the strike grammar, CSV in/out, live repricing |
| 5 | `/risk` | Risk | **What happens if spot moves?** Greeks, spot ladder under an explicit sticky convention, scenario grid, gamma zones, hedge bands, pin risk, expiry ladder and cut clock |
| 6 | `/pnl` | P&L | **Why did I make that?** Daily attribution waterfall — delta / gamma / theta / vega / veta / vanna / volga / rates / carry / hedge — with the unexplained residual printed every day, not only on breach |
| 7 | `/lab` | Signals & Backtest | **Would a rule have worked?** Delta-hedged strategies, hedge-frequency sweep, costed, with a look-ahead certificate |
| 8 | `/data` | Data & Settings | **Where did this number come from, and how old is it?** Your paste-in vol marks, source status, provenance |

**Plus the overnight ladder**, which answers: **"what orders do I leave tonight, and should I
bother?"** It is a library feature (`fxgamma/portfolio/{overnight,bandopt,deltacap,ratchet}.py`)
built from the brief in `docs/08` and the desk spec in `docs/11`. It produces a costed, typeable
order block with a per-rung order type, a delta cap, a carry verdict and a set of refusals.
**It does not yet have a screen in `app/pages/` — see §8.**

The tool's scope boundary is worth stating once: it prices, measures and refuses. It has no broker
connectivity, no order entry, and **no directional view of spot anywhere in it, by design**
(`docs/08` §2). A component that implied "spot will go up" was ruled out before any of it was built.

---

## 2. The knowledge it encodes

### 2.1 Greeks in desk units

Every monetary Greek returned by `gk_greeks` is already multiplied by notional × direction. The
units are chosen so you rebalance against them directly (`docs/03` §3):

| field | definition | unit |
|---|---|---|
| `gamma_1pct` (Γ₁) | `gamma × S / 100` | **base ccy of delta gained per +1% spot move** |
| `vega` | `S e^{−r_f T} φ(d1) √T × 0.01` | **quote ccy per 1 vol point** (0.01 of σ), not per unit of σ |
| `theta` | `∂V/∂t ÷ 365` | **quote ccy per calendar day** |
| `vanna` | `∂vega/∂S` | quote ccy per vol point per 1.00 of spot |
| `volga` | `∂vega/∂σ` | quote ccy per vol point squared |

Γ₁ is the unit the whole tool is built on because it is comparable across pairs: raw `gamma` for
USDJPY at 147.5 and EURUSD at 1.165 differ by two orders of magnitude for identical risk
(`docs/03` §9.1). The identity `Γ₁ = gamma·S/100` verifies at 0.0 relative error — it is algebraic,
not numerical.

Two aggregation rules that exist because a wrong number is worse than "n/a": `delta_pct` and
`dual_delta` are **intensive** (per-unit, per-strike) and aggregate to `nan`, so a book-level card
reads n/a rather than a confident wrong figure (amendment v1.2 T-2). And base-ccy amounts
(`delta_base`, `gamma`, `gamma_1pct`, `vanna`) aggregate natively only within a single base ccy;
across mixed bases they return `nan` (amendment v1.6 CR-2). Monetary Greeks are converted through
`report_ccy` before summing — on a EURUSD+USDJPY book the naive theta sum is **−209,974** (USD added
to JPY) against **−3,424** converted, i.e. **61× wrong** (amendment v1.6).

### 2.2 The daily breakeven identity, and when it stops being true

A delta-hedged position earns `½·∂²V/∂S²·(ΔS)²`. Writing `ΔS = S·x` and substituting
`gamma = 100Γ₁/S` gives `gamma P&L = 50·Γ₁·S·x²` with `x` in percent. Setting that equal to one
calendar day's decay:

```
BE% = √( |θ| / (0.005 · Γ₁ · S) )
```

Exact — verified to 2.9e−16 relative (`docs/03` §9.2). For an ATM option, substituting the
**carry-free** part of theta collapses it to

```
BE (fractional) = σ_ATM / √365
```

independent of tenor, spot and notional, verified to 2.4e−16 across nine pair/tenor combinations
(`docs/03` §9.3). That is the familiar "a 10 vol option breaks even on a 0.52% daily move".

**The caveat is large and the tool states it on the panel.** The reduction holds only for the
carry-free theta. Using the *total* theta, which carries the
`cp·r_f S e^{−r_f T}Φ(cp d1) − cp·r_d K e^{−r_d T}Φ(cp d2)` rate terms, the two diverge with the
rate differential (`docs/03` §9.3):

| pair | `r_d − r_f` | error in the `σ/√365` shorthand |
|---|---|---|
| GBPUSD | 0.25% | 0.3–0.8% |
| EURUSD | 2.0% | 8–25% |
| **USDJPY 1Y** | **−3.25%** | **54%** |

On a high-carry pair the carry term *dominates* long-dated theta and the shorthand is simply wrong.
Any breakeven on any screen says which theta it was built on.

**Golden reference trade** (amendment v1.4, now a QA fixture): EURUSD 1M ATM straddle, EUR 10mm per
leg, spot 1.084, σ 7.05% → Γ₁ **3.914mm**, θ **−2,868/day** (≈ −8,600 Friday→Monday), BE **39.9
pips**, cross-check `σ/√365` = 40.0 pips. The trader's original table said θ = USD 5,800/day; the PM
priced the trade and the 5,800 (and the 17,400 weekend figure) are **withdrawn everywhere they
appear**. The three numbers are not independent — given Γ₁ and the breakeven, theta is determined,
and the trader's own Γ₁ and BE imply 2,915, not 5,800. This matters beyond a table: the
always-visible header card would have overstated the daily cost of carry by 2× in the most-read
number in the app, which is why that card is now bound to compute theta from `book_greeks` and never
from a constant.

### 2.3 Premium-adjusted delta, and why a 30-delta USDJPY call may not exist

Six of the twelve pairs are premium-adjusted (`spot_pa`): **USDJPY, USDCHF, USDCAD, USDSEK, USDNOK,
EURJPY** (`docs/03` §5.2). Premium-adjusted means the premium is paid in the *base* currency, so it
is itself an FX position and must be netted out of the hedge: `Δ_pa = Δ_spot − V_dom/S`.

For a **call** the premium-adjusted delta is `|Δ_pa| = (K/S)e^{−r_d T}Φ(d2)`. As `K → 0` the
`Φ(d2) → 1` but the `K` prefactor wins and delta → 0; as `K → ∞`, `Φ(d2) → 0` faster than `K` grows
and delta → 0 again. **It is zero at both ends with an interior maximum: the map from strike to
delta is two-to-one, and every attainable non-zero delta has two roots** (`docs/03` §5.3). The market
always quotes the root on the *decreasing* branch (Reiswich & Wystup 2010 §3.2), so
`strike_from_delta` restricts Brent to `[K_peak, K_hi]`. Concretely, USDJPY 1Y at 10.5 vol,
S = 147.5: `K_peak = 119.24`, the market 25-delta call strike is **152.76**, and the spurious
left-branch root at K = 100 carries a delta of **0.67** — the solver would have returned a strike
35% below spot for a 25-delta call.

Because the branch has a maximum, **a requested delta can be unattainable**. Max attainable
premium-adjusted call delta, USDJPY (`docs/03` §5.3):

| tenor | 5 vol | 10 vol | 20 vol | 40 vol |
|---|---|---|---|---|
| 1M | 0.955 | 0.922 | 0.866 | 0.778 |
| 3M | 0.925 | **0.874** | 0.794 | 0.676 |
| 1Y | 0.849 | 0.771 | 0.656 | 0.508 |
| 5Y | 0.643 | 0.540 | 0.411 | **0.276** |

At 5Y/40 vol a 25-delta call is quotable and **a 30-delta call does not exist**. Amendment v1.7 makes
it binding on the UI: any delta-based strike entry calls `gk.max_attainable_delta` and renders
**"unattainable at this tenor/vol"** — never a blank, a zero or a `nan` strike. Silently coercing an
unattainable delta is how you end up with a strike you did not ask for. (`app/pricing.py` implements
this; `app/pages/book.py` surfaces it on the ticket.)

Two numerical details are load-bearing here and are worth knowing because they took down five of
nine surfaces once. `Φ` is computed as `0.5·erfc(−x/√2)`, never `0.5(1+erf(x/√2))`: the `erf`
spelling forms `1.0 + (−1.0)` in the lower tail and returns **exactly 0.0** from about x = −8.3
downward, where the true `Φ(−8.3) = 5.2e−17`. Since `Φ(d2)` *is* the premium-adjusted delta and *is*
the quantity whose sign brackets the delta peak, that spurious zero converted a strictly negative
quantity into a tie and made every premium-adjusted call strike unsolvable (`docs/03` §2.1,
amendment v1.7). Similarly the ATM convention flips sign with the delta convention —
`atm_strike = F e^{+σ²T/2}` for `dns` but `F e^{−σ²T/2}` for `dns_pa` — and is derived from the
delta convention in code so it cannot be got backwards by hand; at 1Y/10 vol getting it wrong moves
the ATM pillar by ~1% of spot.

### 2.4 Sticky-strike vs sticky-delta, and the skew-gamma gap

The spot ladder takes `sticky ∈ {"strike", "delta", "none"}` and the active convention is printed on
every figure. It is not cosmetic. Under sticky-strike, `σ(K)` is held and Black-Scholes gamma is the
whole story. Under sticky-delta the vol moves with spot, so the *true* gamma — the derivative of the
ladder's own repriced delta — is not Black-Scholes gamma. The engine measures a **4.1%** gap on the
reference book and reports the difference explicitly as `skew_gamma_1pct`
(amendment v1.6, "recorded finding"). That is the part of the hedge a trader sizing off BS gamma
under a sticky-delta assumption silently mis-sizes. `sticky="none"` is numerically identical to
`"strike"` for a strike-parameterised surface and ships as the documented pinned-vol fast path.

### 2.5 Two clocks: √365 governs economics, √252 governs distance

This is trader finding **W-7**, ratified as binding in amendment v1.4 ruling 5, and it is asserted
in code (`zones.CALENDAR_DAYS = 365`, `zones.TRADING_DAYS = 252`, `realized.ANNUAL = 252`) and fenced
by tests (`docs/05` §3.4).

* **Economics** — theta, the breakeven, the decay bill — annualises on **√365**, because theta is
  paid on calendar days.
* **Distance and probability** — sigma-days, touch probability, zone distance, the delta cap —
  annualises on **√252**, because spot only moves on trading days.

On the golden fixture the two are **39.9 pips** (breakeven) and **48.1 pips** (one trading-day
sigma) on the same book (`docs/05` §3.4). Using √365 for distance overstates every
distance-in-sigma-days by ~20% and makes far strikes look safer than they are (`docs/02` §0). The
basis string is printed on every panel that exposes either.

It is an easy error to make and the PM made it: the first candidate delta caps were computed with
`σ√(vf/365)` = 0.256% instead of `σ√(vf/252)` = 0.3093%, understating the overnight sigma by **20.8%**
and every candidate cap with it (`docs/13` §1). See §7.

### 2.6 Vanna-volga, and why it is the default

FX brokers quote three liquid instruments per tenor. Vanna-volga answers the market maker's own
question: *given that I can hedge vega, vanna and volga with those three, what does a fourth option
cost?* It is exact at the three pillars by construction, model-free (no calibration, no optimiser)
and fast enough to rebuild a surface on every tick (`docs/03` §6.3). It is implemented in the
Castagna-Mercurio conjugate form, which is algebraically identical to the literal spelling and
numerically stable at `d1·d2 = 0` — two strikes that in FX sit within a few tenths of a percent of
the money, where the old `|d1 d2| > 1e−12` guard put a small but real discontinuity in the smile.

Measured: VV reprices its own ATM / 25RR / 25BF / 10RR / 10BF to **7.7e−11 vol** under the harder
fixed-point test (`docs/03` §10.2). SABR, fitting 5 pillars with 2 free parameters (`β` fixed at 1.0
and never fitted, because `β` and `ρ` are not jointly identifiable from one smile), lands at
**1.5e−03 vol** RMSE — about 0.15 vol points, and it misses the 25d butterfly by ~0.09 vol points,
**a quarter of the BF** (amendment v1.5 Q-5). That is acceptable for interpolation and never
acceptable as a mark, so SABR is badged in the UI and VV is the default.

What VV is **not**: it carries no arbitrage guarantee whatsoever. Butterfly arbitrage is *measured,
not prevented*. On 172 desk-plausible randomised quote sets, the 5-quote (10d-anchored) VV produced
negative Breeden-Litzenberger density in **17 (9.9%)**; the same quotes with the 10d anchor dropped
produced **0/172**, and SABR **0/172** (`docs/03` §10.4). **The residual arbitrage is caused by
insisting an inconsistent 10-delta broker quote be repriced exactly, not by the wing.** Run
`diagnostics()`; it is the check that decides whether a wing price is usable.

The wings themselves are a shared C1, Lee-bounded construction used by both VV and the chain
interpolator. The bug it replaced is the cleanest illustration in the repo of why "looks fine"
is not a test: on a 3M USDJPY smile (atm 12 / rr25 −5 / bf25 +0.4 / rr10 −9 / bf10 +1.2) a negative
right-wing slope drove total variance into a `max(w, 1e−12)` floor at **K = 171.9**, and every strike
above it priced at **0.01% vol** — roughly **9,500 of 20,001** strikes scanned. Far-OTM yen calls
looked nearly free. After the fix the same surface returns 8.70–9.14% across strikes 100–300 with
**zero** floor hits, slope-jump at the joins falling as h² to 7.7e−09, density minimum −1.8e−08
(−2.2e−10 of peak) over 27 G3 slices (amendment v1.7, `docs/03` §7, §10.3).

### 2.7 What a "gamma zone" means operationally

A zone is not a picture; it is an objective rule on the book's own repriced gamma profile
(`fxgamma/portfolio/zones.py` docstring, REQ-042):

1. sweep `gamma_1pct` over spot via `spot_ladder` **under the active sticky convention**;
2. keep nodes where `|Γ₁| ≥ 0.20 × max|Γ₁|` — material relative to the book's own peak, not to an
   absolute number that would need a currency;
3. cut into **sign-homogeneous** contiguous runs — a zone mixing long and short gamma has a
   meaningless average, so the sign is part of the definition, not a label;
4. merge same-sign runs separated by less than 0.25% of spot;
5. split at an interior trough below 0.60 of the smaller adjacent peak;
6. drop zones below 5% of total gross gamma area;
7. centre = the `|gamma|`-weighted centroid, so it lands on the strike cluster, not the arithmetic
   middle.

Each zone then reports, per REQ-042 and amendment v1.6 CR-4: lo/hi/centre, `gamma_1pct` and peak,
share of total, distance in **percent and in sigma-days (√252)**, the **first-passage probability of
touching it** before the zone's nearest expiry, the **P&L if spot goes to the zone centre** under the
active sticky convention, the **delta the book would carry inside the zone**, and the contributing
strikes and expiries. That is what "how sensitive is each gamma zone" means here: a region you can
be told the distance to, the probability of reaching, the money at, and the delta inside.

Because the profile is generated by the strikes, the rule recovers strike-cluster zones without ever
clustering strikes — and stays correct for a zone that is *between* strikes, such as a short-gamma
hole between two long strikes, which has no strike of its own.

---

## 3. The overnight ladder, explained properly

### 3.1 The structural fact that leads the screen

You are awake for London. Overnight you are not, and the window London close 17:00 → open 07:00 is
**0.583 of the clock** — so you pay 58% of a day's theta — while carrying materially less than that
share of the day's variance (`docs/09` §3.2):

| pair | clock (= theta) share | variance share `var_fraction` | **theta paid per day of variance** | sigma multiplier vs clock | weekend ratio (2.583 cal days) |
|---|---|---|---|---|---|
| **EURUSD** | 0.583 | **0.3820** | **1.53×** | 1.236 | 7.16× |
| USDJPY | 0.583 | 0.4484 | 1.30× | 1.141 | 6.11× |
| GBPUSD | 0.583 | 0.3497 | 1.67× | 1.291 | 7.81× |
| AUDUSD | 0.583 | 0.4950 | 1.18× | 1.086 | 5.60× |

**Overnight is structurally the worst part of the day to be long front gamma.** This is not an
arbitrage — implied is quoted in calendar time and the market knows the night is quiet, so the level
already embeds it. What it means is narrower and more useful: *the overnight ladder has to clear a
higher bar than a daytime hedge, and on a lot of nights it will not clear it* (`docs/11` §1.4).

A ladder built on clock time puts every rung **24% too far out** on EURUSD and understates every
touch probability. The window is therefore built hour by hour from a 24-hour variance profile, not
from a scalar.

The weekend column is the one to stare at: `session_variance_weight` zeroes the 48 hours the market
is shut (Fri 21:00Z → Sun 21:00Z, with a 0.55 thin factor on the reopen), so a Friday-to-Monday
window contains barely more tradeable variance than a Tuesday night while charging **2.583 calendar
days** of theta. Friday nights are a different trade from Tuesday nights and should be sized as one.

### 3.2 The crossover vol — the go/no-go

The window's gamma P&L on a forecast range of standard deviation `sd` is `0.5·|Γ(σ)|·sd²`; the bill
is `|θ(σ)|·D_cal`. Gamma falls and theta rises with implied, so the two cross exactly once.
`crossover_vol()` solves it by bisection on a parallel vol shift **with a full book repricing** —
rates, skew, several expiries, premium-adjusted deltas and all. The closed form, for intuition only:

```
σ* / σ  =  √( 365 · vf / (252 · D_cal) )        ← a pure calendar fact, book-independent
```

Measured against the full reprice (`docs/09` §3.4):

| window | closed form | measured | reading |
|---|---|---|---|
| EURUSD weeknight | 0.974 | **7.73%** vs 7.96% marked | needs realised **+3.0%** over implied — marginally **negative** carry |
| USDJPY weeknight | 1.055 | **10.31%** vs 9.84% marked | **positive** carry — the Tokyo fix pays for the night |
| EURUSD weekend | 0.449 | **3.48%** vs 7.96% | needs realised **2.2× implied** |

**The USDJPY positive-carry sign is a hypothesis, not a result, and must be badged as one.**
EURUSD and USDJPY sit close to the line, and the *sign* of the weeknight answer is decided by the
pair tilt in the hour profile — which is modelled, not measured. Recalibrating the hour profiles to
hit the measured 0.382 EURUSD share is what flips USDJPY positive (+31,872 JPY), and that rests on a
modelled tilt in synthetic data (`docs/08` band amendment; `docs/09` §3.2, §3.4). No pair-level
overnight carry claim ships as fact until it is measured on your own hourly history.

The crossover vol is the number to read first, because it is a **position** decision, not a ladder
decision. If the marked ATM sits above it, the honest answer may be "sell the front gamma into the
close", not "leave better orders".

### 3.3 The delta cap — the dial that actually moves money

You were asked for your cap and answered *"any as long as you think it's optimal size and makes
sense in regards to the options I have"*. That is the correct answer and it means the cap has to be
derived from the book. The shipped rule (`docs/13`, binding amendment in `docs/08`) is:

```
cap  =  ½ · Γ₁ · σ_on(%)          σ_on = σ · √(vf / 252)        rule "delta_equals_gamma" (R2)
```

*Derivation.* The night's gamma P&L is `50·Γ₁·σ_on(dec)·S`; the directional P&L on a carried delta
`D` over a further one-sigma move is `D·σ_on·S`. Equate them. **The assumption is simply that the
directional bet you did not choose should not be bigger than the convexity bet you did** — no loss
limit, no risk aversion, no view on how long you cannot deal. It has the fewest assumptions of any
candidate, which is why it ships.

On the reference book (EURUSD 1M ATM straddle, EUR 10mm/leg, spot 1.1650, ATM 7.944%, Γ₁ 3.50mm,
`vf` 0.3820, σ_on 0.3093% = 36.0 pips) it returns **EUR 0.54mm**, an **18-pip** ladder, binding on
**~99%** of nights (`docs/13` §1, §4.6).

Three sibling rules are reported next to it (`docs/13` §2): **R1** = `Γ₁·σ_on` = 1.08mm (the delta an
*unhedged* book carries after a one-sigma night — an honest ceiling, since a cap above it cannot
bind); **R3** = `θ_window/(σ_on·S)` = 0.57mm (the same rule with the anchor moved from gamma to
theta). R2 and R3 are **one rule under two anchors**: `R3/R2 = 252·D_cal/(365·vf) = 1.047`, exactly
the night's theta-per-day-of-variance. They coincide when the night is carry-neutral and diverge by
precisely how expensive the night is — when they pull apart, the night is telling you something.
**R5** (premium-at-risk, 8.6mm) is printed so you can see it fail: it scales as `√T` where every
other rule scales as `1/√T`, so it would hand a 3M book a larger cap than a 1W book that accumulates
2.1× more delta per pip.

**Scaling**, so it stays right as the book changes (`docs/13` §6):

```
cap = 0.199 · gross_notional · √( var_fraction / (252 · T) )      [base ccy]
```

| what changes | what the cap does |
|---|---|
| gross notional | **linear** |
| tenor | **1/√T** — a 1W book gets 2.07× a 1M book's cap at the same notional; a 3M gets 0.57× |
| window variance share | **√vf** |
| **the vol level** | **nothing at all** |
| your broker's spread | **nothing** (only the *floor* moves, as cost^{2/3}) |
| book skew | **two numbers**, from the repriced delta profile |

**Vol-mark invariance, and why it matters more to you than to anyone else.** `Γ ∝ 1/σ`, `σ_on ∝ σ`
and `θ ∝ σ`, so the cancellations are algebraic. Marked a full vol point either side of 7.944%, Γ₁
moves **29%** and R1/R2/R3 move **0.0%** — identical to four significant figures (`docs/13` §4.7).
You have no OTC access, so the vol mark is the one input you cannot get right; the cap is the one
number that does not need it. What is *not* mark-invariant is the money (about **USD 375 per vol
point** on the reference night) and the **floor** (`D* ∝ σ^{−1/2}`, ±7% over ±1 vol point).

**Where the cap is defensible, and where it is not** (`docs/13` §4.3, §4.15, n = 40,000–60,000
nights, 5-minute bars, common random numbers):

* **Below ~EUR 0.26–0.30mm the cap is strictly dominated** — tightening makes the expected night
  *and* the 5% tail worse. No risk aversion justifies it. The argmin survives p05, CVaR-90, CVaR-95,
  CVaR-99 and p01. It has a closed form, and it is a different object from the mean-variance band:
  `h* = S·√( (√2/z) · λ · σ_on )` — the geometric mean of the spread and the window sigma, with no
  risk-aversion parameter in it at all, matching measurement to ~10% from 0.5bp to 5bp.
* **Above ~EUR 1.1–1.4mm the cap is strictly dominated by having no cap at all** — it costs money
  and improves CVaR-95 by literally zero, because the tail nights are the quiet ones and a loose cap
  never binds on them.
* **In between it is a priced preference, not an optimum.** From no cap down to 0.54mm the exchange
  rate is about **USD 2 of mean per USD of 5%-tail**; a mean-CVaR objective prefers capping only if
  you weight the tail at more than twice the mean. Traders do, for institutional reasons
  (`docs/11` §4.7). The tool prices the preference; it does not disguise it as an optimum.

**The shipped 15%-of-gross default is wrong overnight and has been replaced.**
`zones.DEFAULT_BAND_PCT = 15` gives EUR 3.00mm on the reference book — 5.6× the derived cap, 2.8×
the delta the *unhedged* book accumulates in a whole one-sigma night. Measured: it costs **USD 10 a
night and improves CVaR-95 by exactly zero**, binding on 0.9% of nights. It is not a loose risk
limit; it is strictly dominated by no cap at all. It stays as the *intraday* default (changing it
would move the intraday screen); the overnight path now derives its default from
`deltacap.recommend_cap` instead, and `override_effect` classifies any number you type as
**DOMINATED / DEFENSIBLE / DECORATIVE** (`docs/13` §4.8, §8).

**On a skewed book the cap is two numbers.** `accumulated_delta` reads the delta accumulated over
±1 window sigma off a full repricing, not off `|Γ|·σ_on·S`. On a 25-delta risk reversal the up-side
and down-side accumulations differ by **2.2×** and the single-Γ₁ linear proxy is out by **261%** —
because an RR's net gamma at the money is nearly zero while its *accumulation* is not (`docs/13`
§4.10). `DeltaCap` therefore carries `cap_up` and `cap_dn` and prints the reason in words:
*"down-side cap 0.024mm is 55% smaller than the up-side 0.054mm because your book accumulates less
delta on the way down. THAT IS YOUR STRIKES, NOT A VIEW."* It also detects two degenerate cases:
gamma changing sign across the window (a scalar cap is the wrong object, and the order type must
come per rung), and the smallest dealable clip exceeding the whole night's accumulation, where it
returns **NO OVERNIGHT LADDER** — there is nothing to hedge and no cap will change that.

### 3.4 The order type is derived, not chosen

Per rung, from the **local** gamma sign at that rung — never from a book-level sign, because a book
can be long gamma above spot and short below it (`docs/11` §3.1, ruled in `docs/08`):

| | Long gamma | Short gamma |
|---|---|---|
| hedging requires | sell into strength, buy into weakness | buy into strength, sell into weakness |
| order type | **LIMIT** | **STOP-MARKET** |
| you are | providing liquidity | taking liquidity |
| slippage | zero or favourable | guaranteed adverse, unbounded in a gap |
| **not being filled** | **safe** — you keep the gamma | **is the loss** |
| placement vs an anchor | **inside** the level (fill first) | **beyond** it (avoid the noise touch) |
| platform down | nothing happens | you are naked short gamma |

> **Long gamma: an unfilled ladder costs you nothing. Short gamma: an unfilled ladder is the loss.**

Stop-*limit* does not fill in the gap it was bought for; if your platform offers only stop-limit the
screen must say so and widen the limit offset by a stated number of pips.

### 3.5 Short gamma: refusals, not optimisation

For a short book everything inverts. The expected gamma term is negative and the theta is the entire
edge; the crossover vol is read the other way. Measured, the tail effect is an order of magnitude
larger than on the long book: CVaR-95 runs **−8,775 uncapped to −2,558 capped**, a USD 6,216 swing
against USD 451 on the long book (`docs/13` §5.1). **But with stops slipped at twice the spread the
tail optimum moves *wider*, to ~0.75mm, and everything tighter is dominated** — the whipsaw of
stopping in and out costs more than the convexity it saves. Do not quote the frictionless
short-gamma numbers.

R6 makes the structure explicit: stopping out through a gap `g` in clips of `D` leaves
`½·g·D` of unhedged quadratic loss — **exactly linear in the cap** — plus `|Γ|·g·(Sλ + slip)` of
friction that is **cap-independent**. So once the friction alone breaches your loss limit, *no cap
can fix it* and the only answers are to reduce the position or buy the wing. `short_gamma_cap`
returns exactly that: the cap, or the fraction of the short gamma that has to go.

Hence the seven **refusal** conditions, adopted from the desk spec (`docs/11` §5.2), which produce a
named refusal rather than a warning you can click through: an `INDICATIVE` surface for that pair; a
**tier-3 event** inside the window; a 99th-percentile slipped loss above your stated limit; >25% of
the pair's gamma expiring at the next cut with a strike inside the ladder range (a resting order
cannot hedge a delta discontinuity); the window spanning a **weekend or holiday**; an outermost clip
above what the pair trades in that hour; stop-limit-only platforms unacknowledged. And when it
refuses it offers the alternatives — reduce the position, buy the tail, alarms plus a person, or
hedge to flat and accept the theta.

`recommend_cap` prints the refusals **above** the cap and — this is the part that matters — also
lists the ones it **cannot** check (pin risk at the next cut, the outermost clip against 03:00
liquidity, stop-limit-only platforms, the stop-fill assumption). *A refusal list that silently drops
the conditions it cannot check is worse than no refusal list.*

### 3.6 The worked ladder, and how to read every line

EURUSD 10mm/leg 30-day ATM straddle, spot 1.1650, ATM 7.96%, long gamma 3.48mm/1%, window
`vf` 0.3820 and 0.5833 calendar days, **1σ = 36 pips**, breakeven 37 pips, cap 0.5mm, 4 rungs/side,
retail 5bp, κ = 1 (`docs/09` §7.3):

```
CARRY   gamma +1,949 + theta −2,067 = −117 USD    crossover ATM 7.73% vs 7.96% marked
E[fills] assume a BROWNIAN path (kappa 1) — roughness is an input here, not a forecast
LIMIT orders, spacing 17 pips  [DELTA CAP 0.50mm (16.7 pips) over zakamouline optimum 181.5 pips]
  SELL 0.48mm EUR at 1.1717  (+67p, 1.85σ, touch  7%, E[fills] 0.05)
  SELL 0.49mm EUR at 1.1700  (+50p, 1.39σ, touch 17%, E[fills] 0.16)
  SELL 0.49mm EUR at 1.1683  (+33p, 0.93σ, touch 36%, E[fills] 0.41)
  SELL 0.50mm EUR at 1.1667  (+17p, 0.46σ, touch 65%, E[fills] 0.90)
  BUY  ... mirrored below spot
```

| line | USD |
|---|---|
| the **position's** expected gamma P&L over the window, `0.5·Γ·V` | **+1,949** |
| theta, pro-rata on 0.5833 **calendar** days | **−2,067** |
| **carry (hold-or-not; nothing to do with the ladder)** | **−117** |
| of the 1,949, **converted to cash** by the ladder | 1,278 (**66%**) |
| ladder transaction cost | −445 |
| **the ladder's effect on the EXPECTED P&L** | **−445** (exactly minus the cost) |
| overnight P&L standard deviation, no ladder → with ladder | **−73%** |
| 3σ gap (108 pips), full reprice + fills | +4,010 up / +4,206 down |

The −117 is the decision. The ladder's contribution to the mean is −445. What you get for that 445
is 1,278 of mark-to-market turned into cash you keep even if spot round-trips, and a 73% cut in the
standard deviation of the overnight outcome. **Calling the 1,278 "capture" would be a lie: it is
money the position already owned.** The conversion identity is the arithmetic proof — a finite
4-rung ladder reaches 65.5% of `0.5·Γ·V`, the share rises toward 1 as rungs are added, and it can
never exceed 1.

USDJPY is the instructive counter-case (`docs/09` §7.4): USD 10mm/leg at 147.50, `vf` 0.4484, 1σ =
61 pips, spacing 26 pips. Gamma +357,381, theta −325,509, **carry +31,872** (positive), converted
241,125 (67%), cost −67,876, sd −75%, night −36,004. **The position is worth holding overnight and
the orders are still a net cost.** Two separate decisions, kept separate by the summary.

Three mechanical points on the rungs (`docs/09` §7.1):

* **Clips come from the repriced delta profile**, `clip_k = |delta(L_k) − delta(L_{k−1})|`, never
  from `Γ₁ × spacing`, so cumulative delta returns to target at every rung. The linear proxy is only
  ~2% out on a symmetric straddle at 1W — and 13%+ on a skewed book, which is exactly what this
  feature is for. If a rung is snapped to a level, the delta is **re-read at the moved level**, or
  the cumulative delta is wrong there and at every rung beyond it.
* **`p_touch` is the first-passage probability**, not the terminal one. The terminal probability is
  roughly half, and using it would halve every rung's expected contribution, worst at the near rungs
  that do most of the work. Verified against `2·N(−d/σ_window)` to 1.0000–1.04 over 0.25σ–3σ.
* **`exp_crossings` is the expected number of *fills*** and is not bounded by 1 — the nearest rung of
  a tight ladder fills ~0.9 times on an ordinary night and several times on a choppy one. It assumes
  **κ = 1, the Brownian baseline**, and the panel says so, because roughness cannot be forecast (§4).

Spacing is chosen in this priority order, and which constraint bound is recorded in
`ladder_summary()["spacing_source"]`: caller-supplied → `optimal_band` → **clamped above by the
delta cap** → **floored below by the minimum dealable clip**. On the reference book the answer is
**always the cap** — at retail cost the analytic optimum is 181 pips = 5.3 overnight sigmas and the
unconstrained ladder would never fill.

### 3.7 Cost is the input that owns the answer

`zones.COST_BP` is an **interbank** table (EURUSD 0.2bp ≈ 0.12 pips round trip). You have no OTC
prime relationship; the desk review puts your real all-in cost at **15–40×** it. The shipped default
in the overnight modules is `RETAIL_COST_BP` (EURUSD 5.0bp = 5.83 pips round trip), and it is an
assumption, not a measurement (`docs/09` §6; `docs/11` §4.4).

| `cost_bp` | round trip | conversion (USD) | cost (USD) | night net |
|---|---|---|---|---|
| 0.2 (interbank) | 0.23 pips | 1,278 | 18 | −135 |
| 2.5 | 2.91 pips | 1,278 | 222 | −340 |
| **5.0 (retail default)** | **5.83 pips** | 1,278 | **445** | **−562** |
| 20.0 | 23.30 pips | 1,278 | 1,779 | −1,897 |

At about **14bp round trip the ladder's cost exceeds everything it converts** and the orders are pure
value destruction at that spacing. Note the conversion column does not move: the cap sets the
spacing, so cost changes the bill without changing the ladder.

Two different shapes of sensitivity, worth internalising: the **band scales as cost^{1/3}** (a 25×
cost error is a 2.9× band error — forgiving), while the **cost line is linear** in it. So a wrong
cost estimate changes what the cap *costs* without changing what the cap should *be* — a comfortable
place to be wrong (`docs/13` §4.11). But it changes the *character* of the recommendation: at 1.03bp
the cap costs USD 107/night and buys tail at a rate of 0.31 (easy yes); at 5.0bp it costs USD 533 at
a rate of 2.03 (a genuine judgement call).

**The repo contradicts itself on your cost by about 5× and it has not been resolved** — see §9,
RFC-3. Answering *"what is your all-in cost, in pips, to trade EUR 0.5mm of spot at three in the
morning — the number your platform actually fills you at, not the spread it shows"* is worth more
than any refinement in the overnight modules.

---

## 4. What was tested and rejected, and why this is the most valuable section

You asked for technical levels, a predictive element, and a rule that does not hedge too early in a
trend. Each was built, measured against a control, and **turned off**. The numbers are below,
because "we tried it and it did not work, here is the evidence" is the part of this project you
should trust most.

An overriding caveat, stated once and applying to every line of this section: the negative results
were measured on the **synthetic provider**, which contains vol clustering (so HAR has something to
find) and, by construction, **no microstructure, no support/resistance, no round-number behaviour
and no deviation from Brownian path roughness** (`docs/10` §0). Synthetic data proves *machinery* —
that an estimator recovers a known answer — and it cannot prove a *market fact*. So every "do not
use" is a statement about what this data can show, and §8 gives the exact test that would overturn
each one on your own data.

### 4.1 Snapping rungs to technical levels — FAILED against its own control

16 level kinds (`prior_high/low/close`, `round_big/half/quarter`, `pivot`, `pivot_r1/s1/r2/s2`,
`swing_high/low`, `sma_20/50/100/200`), rebuilt at every bar using only bars at or before it, 1,249
dates per pair, 30k–42k level observations, 5 pairs (`docs/10` §6).

The decision statistic is **not** reversal rate. A resting order does not care whether price
eventually reverses; it cares, conditional on being touched, how far price runs *against* it. So
`measure_fill_quality` measures `fill_rate`, `mark_pips` per fill at the horizon, `adverse_pips`,
and `ev_pips = fill_rate × mark_pips` — the value of leaving the order at all — against a
**distance-matched** control, block-bootstrapped over dates and Benjamini-Hochberg adjusted.

**Result: 4 cells of 80 clear BH p < 0.05 under the scale-matched control, 2 of 80 under the jitter
control, no kind is significant on more than one pair, and the two sets do not overlap.** Effect
sizes on `d_ev` are **+0.2 to +0.4 pips**. Three kinds (`swing_high`, `round_big`, `round_half`) are
positive on 5/5 pairs at +1.2 to +1.5 pips per fill — but 16 kinds are being screened, so ~0.5 such
runs are expected by chance, and the five synthetic pairs are not independent (EUR/GBP correlate at
+0.78). **Four scattered, non-replicating hits out of eighty is what "nothing is here" looks like.**

Ruling: **snapping ships OFF by default.** The levels are still *displayed* and still label a rung
("1.1731 is 3 pips inside yesterday's high"); `strength` is documented as a display prior, not a
probability. The mechanism (`nearest_level(..., kinds=)`) exists so specific kinds can be enabled
narrowly if they ever earn it on your own data (§8.5).

### 4.2 Trend-conditional ratcheting — a bet on persistence, not a hedging improvement

Your question was the right question and **the mechanism is real**. The gamma kernel is a sum of
*squared* moves between hedges and squares are not additive: `(a+b)² > a² + b²` for same-sign legs.
+25 then +25 pips captures 2,500 hedged once against 1,250 hedged each leg; +25 then −25 captures 0
hedged once against 1,250 hedged each. Reproduced at 4,000 AR(1) paths per cell with variance
equalised across φ (`docs/09` §4.5, `docs/12` §2):

| band (in step sd) | trending φ=+0.3 | random walk | choppy φ=−0.3 |
|---|---|---|---|
| 0.25 | 240.9 | 239.9 | 238.4 |
| 1.00 | 261.9 | 239.9 | 214.7 |
| 4.00 | **349.4** | **238.0** | **146.6** |

**+45% captured at a wide band when trending, −38% when choppy — and the random-walk column is
flat**, which is the band-independence result of §5 validating the simulation.

The decomposition that settles it is exact per path (residual 0.0):

```
capture  =  QV  +  2 · Σ_legs Σ_{i<j in the same leg} r_i r_j
```

Quadratic variation is untouchable by any hedging rule. **Therefore every penny a trend rule can
earn is the return autocovariance inside its own legs** — which is the variance ratio, which is the
`kappa` that `docs/10` independently ruled unforecastable. Two negative results, arrived at from
different directions, are the same result.

Against a **risk-matched** symmetric band (matched on mean delta, which lands at 28–29 pips, i.e. at
the cap), 12,000 nights per cell (`docs/12` §7):

```
ratchet edge  =  199 · φ  −  3.8     USD per night     (R² = 0.99, break-even φ = +0.019)
```

* Significant only above **φ = +0.10**.
* **Cost of calling the regime wrong: −25 USD/night at φ = −0.10, −69 at φ = −0.30** — the latter is
  **5.7% of that night's gamma P&L**, handed back for a wrong regime call.
* And it is worse on *both* axes simultaneously: p95 |delta| **0.98mm against 0.91mm** matched, with
  the cap binding on **32–46% of nights against 9–18%**. Worse P&L and worse risk.
* `TimeAndState` measures **zero everywhere** (|t| < 1.1) — at a 20-pip band there are only ~1.5 legs
  in a night, so the state variable is usually undefined. A static asymmetric persistence tilt never
  reaches |t| = 2.

**Could it be known in advance? No.** A single 14-hour session's variance ratio has **sd 1.25 around
a mean of 1.0, with a median of 0.49** — and sampling finer does not help, because the estimator's
noise is driven by the number of independent window-length moves, which is one however you slice it.
One-night regime calls are **55% accurate at |φ| = 0.10**. Pooled over 20 nights the sd falls to
0.303, so **persistence is a measurable property of the pair, not of tonight** — and a stable
property does not need a forecast, it needs a measurement. Walk-forward over 19,960 nights per cell,
**R² is negative against the Brownian null in 7 of 7 cells**, including when true φ is held constant
at +0.2; the null control at φ = 0 reads **+0.45 ± 2.84**, i.e. nothing (`docs/12` §5, §6).

**And you could never verify it on your own book.** The per-night sd of the paired difference is USD
593, so at a plausible φ = 0.05 establishing the edge at t = 2 takes **37,444 nights — 149 years**;
even *detecting* the persistence takes 785 nights. The one honest use of that arithmetic is the
reverse: it says how big φ would have to be before the question is worth reopening, and the answer
is φ ≥ +0.15, i.e. VR(13) ≥ 1.32. Nothing in liquid G10 at hourly frequency is near that.

Ruling: **ship the symmetric band at the delta cap.** `Ratchet` and `TimeAndState` remain in the
library, documented and **off**. The one asymmetry that ships *on* is **book skew**, because that is
repricing, not a view — and the rule prints the sentence saying so.

A practical objection that precedes all the P&L: **you are asleep.** `Ratchet` needs a server-side
trailing stop or an OCO chain; `TimeAndState` needs conditional amend-on-fill. Only
`SymmetricBand` and `AsymmetricBand` are leaveable as two resting limits. If your platform cannot
hold a conditional order unattended, the economics above are not merely thin — they are unavailable.

### 4.3 Forecasting path roughness from daily bars — loses to the Brownian null

This one hurts, because roughness is *exactly* the statistic that decides how often a ladder fills.
Two nights with an identical 40-pip range pay completely differently: a smooth trend fills each rung
once, a choppy night refills them repeatedly. The desk spec was right that this, not range, is the
missing term (`docs/11` §9.1).

The machinery works. The identity `kappa = QV/D² = 1/(n·ER²)` reproduces to **1–5%** across 5 pairs ×
4 block lengths, and the estimator detects real roughness at the right magnitude: κ = **1.97** on a
strongly mean-reverting process, **0.32** on a strongly trending one (`docs/10` §5.1, §5.3).

The forecast does not. Walk-forward with vol held at its realised value so the roughness question is
isolated, **the kappa-scaled model loses to a trailing mean on 4 of 5 pairs and to the Brownian null
on all 5** (`docs/10` §5.4). The diagnosis is not a modelling failure — it is sampling. Counting
completed h-moves on Brownian motion recovers only:

| sampling steps per unit variance | fraction of the true count recovered |
|---|---|
| 1 | **0.463** |
| 4 | **0.627** |
| 64 | 0.879 |
| 1024 | 0.968 |

On FX daily closes the observed ratio is **0.62–0.79**, exactly what the table predicts — that is
*sampling*, not roughness. Hourly bars recover **1.8–2.2×** more of the true crossing count
(1,579 vs 719 completed 25-pip moves over 730 days), which fixes the **measurement** problem
(`docs/12` §10) — and still does not make tonight predictable (§4.2 reproduced the negative result at
hourly frequency).

Ruling: **κ defaults to 1.0, the Brownian baseline, and stays there.** It is an explicit override for
a user with a view. `ladder_summary()["fills_basis"]` and the printed panel both say *"E[fills]
assume a Brownian path"*.

### 4.4 Directional forecasting — never attempted, and why

No component of this tool forecasts the direction of spot. The PM's steer (`docs/08` §2) is the
strongest sentence in the brief and the trader's review holds it to it: *daily FX returns are close
to unforecastable, and a directional signal is the fastest way to make this tool worse than useless
while looking sophisticated.* The only thing forecast is the **range** — how far spot travels — which
is materially forecastable and is what a gamma book actually needs.

Correspondingly, the desk spec lists "a 'predictive model' that turns out to have direction in it" as
a trust-ending failure: *the moment a rung is placed asymmetrically for a reason that is not the
book's own gamma profile, the tool is a signal service* (`docs/11` §8.2).

### 4.5 What did survive

| component | verdict | evidence |
|---|---|---|
| **HAR-RV** range forecast | **USE** | beats yesterday's RV by R²_OOS **+0.703** (QLIKE) and a trailing mean by **+0.087**, Diebold-Mariano **t = −3.06, p = 0.004**, on 5/5 pairs; recovers a known process at R² 0.975; **calibrated in level** — all four coverage quantiles within ~1.5 se, E\|move\| within 5% on 5/5 (`docs/10` §1) |
| Implied vol in the blend | **USE at a FIXED 0.5, do not fit** | fitted weight swings **0.22 → 1.00** across pairs, honest OOS gain **−1.3% to +0.4%**, nothing at p < 0.08. On this generator implied *is* a linear function of trailing RV, so the code correctly declining to find information that is not there is a **passed machinery check**, not a market fact (`docs/10` §2) |
| Session variance time | **USE** | 0.382 vs 0.583 clock; ×1.236 on sigma (`docs/10` §3) |
| Event **segmentation** | **USE** | **32.3%** of the shipped calendar's 409 events fall inside the window, and the concentration is total not diffuse — **100%** of JPY/AUD/NZD events are in-window, **0%** of EUR/GBP/CHF/CAD/SEK/NOK. A scalar `var_fraction` cannot describe a BoJ night (`docs/10` §4.1) |
| Event uplift **magnitude** | **PRIOR ONLY** | n ≈ 25 event days, inconsistent signs, no \|t\| > 2 (`docs/10` §4.3) |
| κ / efficiency ratio / crossings as **outputs** | **USE** | identity verified to 1–5% (`docs/10` §5.5) |
| `technical_levels` / `oi_levels` as **anchors and labels** | **USE** | mechanically correct, forward-clean (`docs/10` §6.4) |
| `recycle_rate` | **DO NOT QUOTE** | degenerate at exactly 1.000 — the synthetic bar generator stamps `open = previous close`, so "spot came back" is true by construction (`docs/10` §6.3) |

---

## 5. The hedging theory, honestly stated

### 5.1 The band is not a P&L knob

Rebalance a delta-hedged position on a grid of spacing `h`. By Tanaka's formula the expected number
of crossings over a window with terminal variance `V` is `E[N] = V/h²`; each crossing is half a round
trip worth `Γh²/2`. Multiply (`docs/09` §2):

```
E[gamma P&L] = (V/h²)·(Γh²/2) = Γ·V/2          ← independent of h
```

**The band cancels.** So does the ladder: mark-to-market already contains `0.5Γ dS²`, and every hedge
trade is a fair bet whose expected contribution is zero. What the band actually changes:

```
cost(h) = λ·S·|Γ|·V / h          (falls as h widens)
Var(h)  = Γ²·h²·V / 6            (rises as h widens)   [hedge-to-target occupation]
```

so the whole optimisation is cost against hedging-error variance, and the first-order condition is

```
H³ = 6 · λ · S · Γ² / γ
```

**Note what this is not.** It is not "maximise monetisation". Under driftless spot the expected
overnight P&L is `0.5·Γ·E[ΔS²] − θ` **with or without any ladder**; leaving orders changes the
expectation by **exactly minus their transaction cost**. What the orders buy is *conversion*
(mark-to-market becomes cash you keep even if spot round-trips) and *variance reduction* (you do not
wake up holding a delta you never chose). The trader's own check: the ladder makes USD 2,830 on a
night that runs to 45 pips and comes back, and gives up USD 4,500 versus doing nothing on a night
that trends 60 pips one way. **It pays you for mean reversion and charges you for trend, and over
many nights the two cancel to the penny** (`docs/11` §4.1).

That identity is verified through a full option repricing, not just a quadratic: the cap's paired
effect on the mean reproduces minus the measured transaction cost cell by cell, ratios 0.995–1.024
(`docs/13` §4.1).

### 5.2 The policy constant — the largest single correction

Whalley-Wilmott's `3/2` constant is for **hedge-to-edge**: on touching the boundary, trade back to
the boundary. Everything in this repo — `hedge_suggestion`, `backtest/engine.py`, and a resting-order
ladder — hedges **back to target**. Redo the derivation for that policy: hitting time from centre to
±H is `H²/v` so turnover is twice WW's, and the occupation density is triangular so `E[e²] = H²/6`,
half WW's. Result `H³ = 6λSΓ²/γ`, i.e. **the correct band for a hedge-to-flat desk is
`4^{1/3} = 1.587×` wider than the textbook WW band** (`docs/09` §4.2).

That single correction explains essentially the whole WW-versus-empirical gap, and `zones.py` had
restated the formula with the edge constant and was therefore 1.587× too tight for the policy the
tool actually runs. It is fixed to *take* the constant from `bandopt.POLICY_CONST` rather than
restate it — because a duplicated formula is exactly how `band_pct` and `COMPONENTS` drifted earlier
on this project (see §7).

### 5.3 Analytic against the empirical referee

10 trading days, 64 common-random-number paths × 24 steps/day, ATM straddle EUR/USD 10mm per leg
(`docs/09` §5):

| pair / tier | WW | **Zakamouline** | fixed grid | **empirical argmax** | zak/emp | ww/emp |
|---|---|---|---|---|---|---|
| EURUSD interbank 0.2bp | 38.8p | **61.6p** | 58.4p | **56.0p** | 1.10 | 0.69 |
| EURUSD retail 5bp | 113.4p | **181.5p** | 58.4p | **231.1p** | 0.79 | 0.49 |
| USDJPY interbank 0.3bp | 63.5p | **101.0p** | 91.4p | **82.1p** | 1.23 | 0.77 |
| USDJPY retail 5bp | 162.3p | **259.6p** | 91.4p | **321.2p** | 0.81 | 0.51 |

WW is **23–51% too tight** everywhere and `4^{1/3}` applied to it recovers the gap. Zakamouline
drifts from slightly wide to slightly tight as cost rises, and the reason is stated rather than
tuned away: at retail cost `asymptotic_ratio ≈ 1.0`, the band is a full horizon-sigma wide, and the
`λ → 0` expansion the whole family rests on is out of its regime. The fixed one-sigma grid is fine
when costs are tiny and gives up **4–5% of the gamma P&L** at retail cost.

One methodological note worth keeping: the empirical referee **imposes** `E[hedging error] = 0`
rather than estimating it — resolving a USD 200 cost difference through the sample mean of a USD 38k-sd
residual would need order 1e5 paths — and then **tests** the imposition. Worst |z| across all four
validation runs: **1.92**. That is how to make a small effect measurable without fooling yourself.

### 5.4 The flatness result, and why the cap dominates the band

**The objective is extremely flat near its maximum: being 30% off in band width costs ~0.1% of the
gamma P&L at interbank cost and ~0.9% at retail** (`docs/09` §5). This is the single most important
practical fact in the band work, it corroborates the trader's independent estimate that the entire
band decision is worth **~USD 60 a night**, and it is why the third significant figure of a band
does not matter.

The same question asked of the **cap**, in the same units — hold the book fixed, move the cap ±30%
around EUR 0.54mm, express the change as a percentage of the window's gamma P&L (USD 1,951)
(`docs/13` §4.14):

| cost tier | | −30% | +30% |
|---|---|---|---|
| 1.03bp | mean | −2.1% | +1.2% |
| | **CVaR-95** | **+15.4%** | **−16.3%** |
| 5.00bp | mean | **−11.0%** | **+6.5%** |
| | CVaR-95 | +8.0% | −12.2% |

**Being 30% off the cap moves the night by 1.2–16.3% of its gamma P&L; being 30% off the band moves
it by 0.1–0.9%.** One to two orders of magnitude, and the conclusion does not depend on which cost
tier you believe — only *which moment* it is loud in changes. Three workstreams asserted this
independently; it is now measured, and the earlier 15/30/45-pip band sweep is revealed to have been a
0.5/1.0/1.5mm **cap** sweep all along — the right dial under the wrong name.

Hence the ruling: **use Zakamouline intraday** (~60 pips EURUSD interbank, ~180 retail — which is
itself telling you something uncomfortable: at retail spreads, gamma scalping EURUSD in 30-pip clips
does not pay). **Overnight, ignore the band and let `max_overnight_delta` bind.** And never ask
yourself for a risk-aversion coefficient; `bandopt.risk_aversion_for_band()` goes the other way —
give it your delta cap and it returns the coefficient consistent with it.

### 5.5 Two results that cut against the project's own framing

Recorded rather than buried (`docs/13` §4.5, §5):

1. **On a long-gamma book the delta you wake up holding is insured by the gamma that made it.** The
   standard justification for a cap is that an unchosen delta is dangerous. Measured: three
   unhedgeable hours after the open, or a 5%-chance 30-pip gap, or a 2%-chance 60-pip gap, move the
   answer by **under USD 100** and do not move the optimum at all. Two reasons: the 5% tail nights
   are the *quiet* nights, on which you are not carrying much delta; and carrying EUR 2.4mm into a
   150-pip gap costs USD 36,000 while the convexity hands back `½·300.6e6·0.015² = USD 33,800`. That
   near-cancellation *is* what being long gamma means, and it is invisible in any framing that treats
   the overnight delta as a naked directional position. The cap's measurable value is the conversion
   effect in §3.3, not the folklore.
2. **Short gamma inverts the sign and then defeats the remedy.** The tail effect is fourteen times
   larger, but with realistic stop slippage the optimum moves *wider* and most tightening is
   whipsaw. The answer to short gamma overnight is **refusals and position reduction, not a cleverer
   cap** — which vindicates the trader's seven refusal conditions over any amount of optimisation.

A useful control that could have failed and did not: **is the cap doing anything a hedging *clock*
could not?** Scored against time rules at matched expected P&L, EUR 0.40mm beats hedging hourly on
**both** the mean (−817 vs −1,012) and the tail (−1,626 vs −1,929) on a third of the trades, and EUR
0.55mm does the same against a two-hourly clock. The cap is state-dependent where a clock is not
(`docs/13` §4.13).

---

## 6. Data, and what you cannot have for free

### 6.1 The one gap that decides everything

The charter forbids paid data. **There is no free source of OTC FX implied vol**, at any latency —
the surface is a licensed dealer product. Everything else the tool needs (spot, OHLC, short rates, an
event calendar, listed OI) is obtainable at acceptable quality for nothing. The vol mark is not
(`docs/04` §1).

Amendment v1.2 T-1 resolved it the only honest way: **your own ATM / 25d RR / 25d BF grid is the
primary vol input**, and every free vol source is demoted to what it is actually good for — z-scores,
cones, richness ranking, term shape. The chain is:

```
manual  →  live  →  cache  →  synthetic
 mark     indicative  stale     fake
```

A mark you type always wins and is never overwritten by a data pull. A pair with no mark falls
straight through to the indicative tier, badged as such. The paste parser accepts tabs, commas,
spaces, `%` signs, bid/ask pairs, parenthesised negatives, transposed grids and multi-pair blocks,
infers **one unit scale for the whole grid from the ATM column** (inferring per column is how you end
up with a 15-vol risk reversal), reports every assumption it made, and **refuses** an ATM outside
0.5%–150% rather than storing it. Partial grids are served as-is — if only 1M and 3M are marked, only
those go to `build_surface`; ETF tenors are never spliced into the gaps, because the result would be
a term structure nobody quoted under a badge that could not describe it (`docs/04` §2.7).

### 6.2 The free substitutes, ranked, with their basis

| source | what it is | basis to OTC ATM | use it for | substitute quality |
|---|---|---|---|---|
| **CME settlements** (6E/6B/6J options) | daily public settlement, model-derived, on options **on futures** | ~0.2–0.5 vol | the best free mark proxy; cross-check | 3/5 for OI, best available as a mark |
| **ETF chains** (FXE/FXB/FXY/FXA/FXC/FXF) | the only free *traded* FX smile; American options on an ETF | **0.3–1.0 vol** in G3, sign varying; skew proportionally worse | z-scores, term shape, the Lab | **1/5 as a mark, 4/5 as a z-score input** |
| **CBOE EVZ etc. via FRED** | a 30-day variance-swap-style index, **not an ATM** | ~+0.2–0.8 vol, biased **high** | ATM *history* for cones and z-scores | 3/5 for EUR history, 1/5 for beta proxies |
| **your typed view** | your mark | zero by definition | the mark | **5/5** |

**The ETF basis in detail** (`docs/04` §2.3), because it is the number that decides how much of this
you can trust. (i) The options are **American**, the OTC vanillas European; we build only from OTM
contracts where the conventions agree well inside the bid/ask. (ii) **The ETF is not the currency** —
FXE holds a euro deposit net of a 0.40%/yr expense ratio, so NAV drifts against spot; vol is
first-order invariant to a deterministic drift but the drift biases the *forward*, and moneyness off
a wrong forward biases the **skew**, which is where RR, BF and therefore all vanna/volga come from.
The forward is recovered from put-call parity rather than assumed, which removes most but not all of
it. (iii) **Borrow** is embedded as a synthetic dividend and does not net out of a risk reversal.
(iv) **Strikes are $0.50–$1 apart, about 0.5% of spot**, and wing markets run **30–60% wide** relative
to mid — so the 25d BF, and therefore the entire volga/vanna of your book, is fitted to two poor
prints. (v) **The chain stops updating at 16:00 ET** while the pair trades 24h: a European morning
read of an FXY chain is a 14-hour-old picture of yen vol, through the entire Tokyo session, which is
when yen vol actually happens. (vi) **FXY is USD-per-JPY**: vol is invariant to inversion at first
order, **skew is not** (an FXY call is a USDJPY put), and getting the reflection wrong would put the
yen risk reversal on the wrong side of the market — the single most dangerous silent error in the
data layer. (vii) Listed expiries are third Fridays at a 16:00 ET cut, never relabelled as "1M".

Sizing it: on a EUR 100mm 1M straddle one vol point is ~USD 248,000 of PV, so **half a point of basis
is ~USD 124k of mark error on one position, every day**, propagating into delta through the smile,
into hedge size, and into the attribution residual.

Two traps to keep in mind: **EVZ is not an ATM vol** — feeding it in as one biases the mark high,
which biases Γ *low*, which under-sizes every clip by the same percentage. And **CME options are on
futures**, needing a futures→spot conversion before the implied vol means what you think.

Rates are the quiet weak spot: FRED serves USD properly (`SOFR`/`EFFR`, high confidence) and
essentially nothing else reliably — the EUR/GBP IDs are medium confidence, JPY/CHF/CAD/AUD/NZD/SEK/NOK
are monthly OECD series with **low** confidence and several MEI series were discontinued in 2022–23
(`docs/04` §2.2). A 50bp rate error moves a 1Y USDJPY forward ~0.7 yen — four big figures. That is
the T-4 defect the trader caught, and it is why `rd_rf` now **raises with the currency named** rather
than defaulting to 0.0.

### 6.3 What specifically changes because you have no OTC access

**Unaffected — exact, from your own trades:** every Greek, the spot ladder, gamma zones, hedge bands,
pin risk, P&L attribution, the expiry ladder. You typed the trades; nothing there needs a broker.

**Affected — the vol *level*,** and PV with everything derived from it.

The sensitivities split three ways, and the split is the useful part (`docs/11` §6.2):

| quantity | sensitivity to a **full vol point** of mark error |
|---|---|
| **rung levels / spacing** | ±4.5% in the cost-bound regime (band ∝ σ^{1/3}), ±14% in the clip-floor-bound regime — **one to two pips, which disappears into whole-pip rounding** |
| **clip sizes** | ~±14%, one-for-one with Γ — a real but survivable mis-hedge |
| **the delta cap** | **0.0% — exactly zero** (`docs/13` §4.7) |
| the money and the go/no-go | **most.** Theta scales with σ, breakeven scales with σ, and the net is a difference of two large numbers |

**So the levels are the robust part of the ladder and the decision is the fragile part.** The trader's
worked illustration: at a 7.05% mark the overnight breakeven is 31.6 pips against a 30.2-pip forecast
range — marginal; at 6.05% the ladder pays; at 8.05% it is nowhere near. A two-vol-point uncertainty
in an unverifiable mark flips the recommendation. Which is why the **crossover vol** is a first-class
number on the panel: it converts *"I cannot verify my vol"* into *"I need to be confident I am below
6.74%"*, a question you can actually answer.

**And the one property that makes the whole setup workable: the cap is exactly vol-mark invariant.**
Γ₁ moves 29% over a ±1 vol point range and the cap moves 0.0%. The single input you cannot get right
is the single input the dial that matters does not need.

### 6.4 The substitute for a broker curve: your own book, over twenty nights

Free, and available from data the tool already stamps (`docs/11` §6.4):

* **Delta reconciliation.** The stamped ladder predicted a cumulative delta at every level. At 07:00
  the book has an actual delta at wherever spot is. `actual / predicted` is a direct estimate of the
  **gamma error** and therefore of the vol-mark error, because Γ ∝ 1/σ. Twenty nights of it and you
  know whether you are systematically marking a point high.
* **Range reconciliation.** Realised overnight range vs forecast, per pair, with the running bias —
  this calibrates `var_fraction` and the range model out of your own data.
* **Fill reconciliation.** Rungs touched vs `p_touch`, slippage vs assumed cost — this calibrates the
  cost model, the largest unmodelled input in the feature.

After a month those three panels are worth more than any ETF basis series.

---

## 7. How it defends itself against being wrong

### 7.1 Provenance

Every number surfaced carries a `Provenance` in `MarketSnapshot.meta` under a frozen key grammar
(`spot.<PAIR>`, `rate.<CCY>`, `fwd.<PAIR>.<TENOR>`, `surface.<PAIR>[.<TENOR>]`, `oi.<PAIR>`,
`events`), looked up most-specific-first: `live`, `cached`, `synthetic` or `user_override`. An
unbadged number renders `UNKNOWN` rather than `live`. A surface that fails to build is **omitted**
and the failure recorded as `kind="unavailable"`. **Synthetic data is never silently substituted for
live data** (architecture §7; `docs/04` §4).

That rule has teeth and it caught a real violation. QA finding **F-9**: `ChainProvider` hoisted
manual marks to the front but never *demoted* synthetic, so `ChainProvider([synthetic, live])`
answered every unmarked pair from the simulator while a live source sat behind it. Synthetic is now
pushed to the back however the chain is constructed, mirroring the manual hoist (amendment v1.9).
A related discipline: a stale cache entry is still served when everything live fails, badged
`cached` with its age in hours — *degrade visibly, never silently*.

### 7.2 The unexplained residual as an instrument

The P&L page prints `|unexplained| / Σ|components|` **every day**, not only when it breaches, against
reference bands of <1% overnight and <2–3% on a big smile day, with a 5% alarm and the three
positions contributing most (REQ-052). *A threshold that only speaks when it is already broken
teaches nothing.*

The residual is not decoration — it is the instrument that catches the next bug, and the project's
most instructive episode is what happened when someone tried to improve it:

* **v1.8.** The PM moved vega to the interval midpoint `0.5(vega_t0 + vega_t1)` to close a residual
  that an ordinary quarter-vol-point overnight was breaching. Theta stayed pro-rata (theta *is* the
  time-derivative term; averaging double-counts the curvature it already represents). Gamma, vanna
  and volga stayed at t0 — extending the trapezoid to them was tried and reverted because it breaks
  "gamma is quadratic in the move".
* **v1.9. QA dissented from the PM's own fix, and QA was right.** Evaluating vega at the midpoint
  adds an uplift equal to **0.95× the volga bar** at a quarter vol point — and that uplift *is* the
  volga term, so the explicit volga bar then counted it a second time. The v1.8 combination was
  **worse than no fix at all** at one vol point.
* **The diagnostic that proved it is the technique worth stealing.** The residual grew **×3.96 per
  doubling** of the vol move — *second* order in dσ, not the third order a genuine truncation error
  shows — while the spot leg, which has no double count, grew ×8.9–10.6 as dS³ should. Order-of-growth,
  not magnitude, is what tells a double count from a truncation.
* **The correction.** Vega and volga return to t0, and the real effect — that vega *decays* across
  the interval — becomes its own named bar, **`veta`**, isolated by stripping the vol and spot moves
  out of the observed vega change. Measured residual after: **0.124%** at a quarter vol point (was
  1.12%), **0.387%** at a half, **1.44%** at one point, **4.66%** at two.
* **And the budget was restated rather than widened.** REQ-052's flat 1% now carries an envelope:
  it holds for |dσ| ≤ 0.5 vol pt, |dS/S| ≤ 1%, dt ≤ 3 days; beyond that the figures above are the
  expectation. *A flat budget independent of move size was the wrong specification.*

One more detail from the same episode, because it is the shape of a whole class of bug: **a test
that hardcoded a copy of `COMPONENTS` silently drifted** when `veta` was added, and the
reconciliation test then passed over a component it was not summing. It now imports the tuple.

### 7.3 Six manufactured effects, caught by null controls

Every one of these produced a clean, plausible, publishable-looking positive. Every one was wrong.

| # | Where | The artefact | What gave it away |
|---|---|---|---|
| 1 | `docs/10` §6.1 — levels | Real statistic pooled over *touches*, control pooled over *observations*, up-weighting rarely-touched far levels | A **uniform −10pp "anti-effect" on all 16 kinds** — a result too tidy to be real |
| 2 | `docs/10` §6.1 — levels | Control matched on distance **in pips** rather than in units of current vol. Several kinds have a distance proportional to today's vol, so the control landed too close on quiet days and too far on busy ones — and **fabricated a positive for the real level** | The scale-matched control killed it; `permuted_raw` is retained so the artefact can be reproduced, and it puts `pivot_s2` over the line on 3 of 5 pairs where the correct control does not |
| 3 | `docs/12` §4.1 — ratchet | The ratchet **silently degenerated into a symmetric band**: unarmed, it returned `ref ± h` as the trigger, so on close-sampled data it filled on the arming bar and was byte-identical to the baseline | **Every give-back produced the same number.** Unarmed, the rule must have *no* fill level at all |
| 4 | `docs/12` §4.2 — ratchet | Comparing a ratchet at `h` against a symmetric band at the same `h`: a trailing rule hedges less often, so it is **a wider band in disguise**, and at retail cost a wider band saves money for reasons that have nothing to do with trend. It "earned" **+27 USD/night at φ = 0**, where the expected gain is provably zero | A symmetric band widened to the cap earned **+28** on the same paths — the same number. Under `match_symmetric` the random-walk gain collapses to **−2.9 ± 5.4** |
| 5 | `docs/12` §4.3 — ratchet | `vr = n·d²/qv` instead of `vr = d²/qv`. Inflated every reading by a factor of 13 and **would have had the tool declaring a strong trend regime every single night** | Arithmetic, once someone compared it to the identity in `docs/10` §5.1 |
| 6 | `docs/13` §3.2 — delta cap | Filling at the **resting level** on close-observed bars. The fill is conditioned on the close having crossed the level and then priced *at* the level, so `E[S_end − fill_px \| filled] = E[overshoot] > 0` against you on every clip. It produced a clean, monotone phantom cost of **USD 430–990 a night that scaled with the cap** — exactly the shape of a real result, and it would have been the headline | The **zero-cost null control**: with λ = 0 the mean night P&L must be independent of the cap by the band-independence identity. It read **t = −51**. Fixed by filling at the close (the shipped engine's convention, internally consistent because the fill price and the marking price are the same observation); re-run over 100,000 paths × 3 seeds the null control passes at \|t\| ≤ 0.2 |

A seventh trap, not a bug: the aggregate variance-ratio estimator is **biased under the null in small
samples** — pooling 13-bar sessions, `Σd²/Σqv` reads 0.95–1.03 under a generator whose true VR is
exactly 1.0. Comparisons must be made against a **simulated null, never against the number 1.0**;
equivalently, κ between 0.9 and 1.1 reads as "Brownian" (`docs/12` §4.4, `docs/10` §5.3).

### 7.4 Three errors the PM made and corrected in its own amendments

Recorded because a document that shows where the analysis went wrong and was caught is worth more
than one that presents a clean story:

1. **A full day's theta charged against a fourteen-hour window.** The brief's own headline worked
   example (`docs/08` §1) read *"expected capture USD 4,100 against USD 900 of cost and USD 2,870 of
   theta"*. The correct pro-rata figure is **USD 1,673**. This is trader finding W-14, made by the
   PM, in the brief that exists to prevent such errors — and the same example also labelled the
   result "capture" (attributing to the ladder P&L the position already owned) and quoted a cost
   implying ~1.7 pips all-in against the repo's own 0.12-pip table. **The example is withdrawn and
   must not become a fixture.**
2. **√365 used where √252 belongs.** The PM's candidate delta caps used `σ√(vf/365)` — a *distance*
   question answered on the *economics* clock, two turns after the PM had published the W-7 ruling
   that separates them. The correct σ_on is **0.3093% not 0.256%**, so the overnight sigma was
   understated by **20.8%** and every candidate cap with it. The error did not even cancel: R1 and R2
   are linear in σ_on (too tight by 20.8%) while R3 is inverse in it (too loose by 17.2%)
   (`docs/13` §1).
3. **An AR(1) simulation that dropped the leg still open at the end of each path.** The PM's
   trend/chop table read 236.1 at a 4-sigma band where theory requires a flat 240. Marking the open
   leg makes the random-walk column **flat at 240 at every band**, as band-independence requires. The
   trending and choppy effects (+45%, −38%) dwarf the ~1.6% artefact so the conclusion was
   unaffected — but **a rule that hedges less often looked ~1% worse for purely mechanical reasons**,
   which is precisely the direction that would have flattered the wrong answer (`docs/12` §2).

Add to that the v1.8-reversed-by-v1.9 episode above, in which the PM's own fix was overturned by QA's
dissent and the PM verified the dissent before accepting it.

### 7.5 The general lesson

> **Any positive result on this project should be assumed to be a bug until someone has tried to kill
> it.** (`docs/08`, trend-conditional amendment.)

Operationally that means four things, all of which are now standing practice in the repo:

* **Run a null control on the dial you are testing.** Five of the six artefacts above were caught by
  a comparison that had to come out at zero and did not. The sixth was caught by the same instinct
  applied to a control's own pooling.
* **Match the comparator on the thing you are not testing.** Risk-matched, distance-matched,
  scale-matched, common random numbers. An unmatched comparison measures the confound.
* **Look at the order of growth, not just the size.** ×3.96 per doubling versus ×8.9 is what
  distinguished a double count from a truncation error.
* **Verify the mirror bit-for-bit.** Both the ratchet's and the delta cap's simulators reproduce
  `backtest.engine.run_backtest` to `max_abs_diff = 0.0` before any result from them is believed —
  and in the cap's case the first attempt differed by USD 2–5 a night, traced to a 0.3% difference
  in `T` between two day-count conventions, which is invisible until you difference two accounting
  chains.

And one that is easy to skip: **the look-ahead certificate certifies the *engine*, not the
strategy.** QA demonstrated a cheat rule that gains +3.2mm and still passes `lookahead_report`. The
Lab panel must state which of the two it is attesting, so a green badge is never read as "this
strategy is clean" (amendment v1.9, F-11).

---

## 8. Limits, and what to do next

### 8.1 Known limits, stated plainly

* **No live source has ever been contacted.** The build sandbox's egress proxy answers `CONNECT` with
  403 for Yahoo, Stooq, ECB, FRED, CBOE and CME. Every live adapter is fixture-tested only, several
  FRED series IDs are **unconfirmed guesses**, the `JYVIX`/`BPVIX` IDs are guesses (CBOE discontinued
  those indices some years ago), the CME CmeWS routes are undocumented and have changed before, and
  two CME fixtures are **hand-built rather than recorded** — they pin the shape the parser expects
  and are not evidence that CME serves it. Run `python scripts/verify_live_sources.py` on your own
  machine; it distinguishes BLOCKED from HTTP 404 from NO SERIES from SCHEMA and tells you which
  module's `VERIFIED = False` to flip (`docs/04` §0, §5). This is also QA's largest untested area,
  and specifically the **ETF→pair vol basis and the FXY/FXC inversion**: done once in the wrong
  direction it produces a smile that is a mirror image of the truth, and nothing downstream would
  flag it — the surface would build, the density would be positive, the calendar clean
  (`docs/05` §4, §5).
* **ETF chains have no back-history.** Yahoo serves only *today's* chain, so nothing built on it is
  backtestable until you have been recording it. Skew/RR/BF z-scores and vol cones built on ETF
  chains therefore start from your first run. A daily chain **snapshotter** is required so history
  accumulates, and any z-score must state how many days it has (amendment v1.3 C-6). This is also
  the blocker on fitting the implied-vol blend weight.
* **Flat rate curves**, single continuously-compounded zeros per currency. Term structure in the rate
  differential is not represented, so long-dated forwards and `rho` are approximate. European
  vanillas only; no barriers, digitals or American features; no correlation, no cross-gamma, no
  quanto.
* **The P&L residual budget is an envelope, not a constant:** 1% holds for |dσ| ≤ 0.5 vol pt,
  |dS/S| ≤ 1%, dt ≤ 3 days; beyond that expect 1.44% at one vol point and 4.66% at two.
* **The hour profile is modelled, and it is the largest single source of error in the ladder** —
  every distance, probability and crossing count scales with it. The hour-by-hour *shape* is a
  standard FX diurnal prior; only the EURUSD day/night *level* is anchored, on a single measurement
  (0.382), with `NIGHT_CALIBRATION = 1.2033` scaling the night hours of every shipped profile to
  reproduce it. **Every pair tilt and every other pair's level is unmeasured** — which is exactly
  the thing that decides the sign of the USDJPY carry answer (`docs/09` §3.2).
* **The USDJPY positive overnight carry is a hypothesis, not a result** (§3.2), and must be badged as
  one in the UI.
* **Vanna-volga carries no arbitrage guarantee**; the 10-delta anchor can create butterfly arbitrage
  between the 25d and 10d strikes in ~9.9% of desk-plausible quote sets. Prefer SABR below 1W and for
  wing pricing. SABR has its own well-documented Hagan low-strike negative density for large ν²T.
  Run `diagnostics()`.
* **The market-strangle converter fails silently** — `market_to_smile_bf` returns `bf_market`
  unchanged if the Brent solve does not bracket. Verify with a reprice when the distinction matters.
  Similarly `vol_by_delta` and `atm_strike` return the last iterate rather than raising if they do
  not converge.
* **There is no overnight screen.** The ladder, band optimiser, delta cap and ratchet are library
  modules with a formatted text output; `app/pages/` has the eight screens and no ninth. The desk
  spec in `docs/11` was written as the specification for that screen and its requirements — the
  typeable order block, the six-minute leaving-the-desk ritual, the 07:00 reconciliation — are not
  yet built.
* **Open defect worth knowing about, recorded and not yet fixed:** the backtest engine's
  `synthetic_path` advances the index by one **calendar** day per step but steps variance at
  `dt = 1/252`, so a 252-step "year" delivers 1.45 years of variance against 1 year of theta. It does
  not affect any band argmax (both terms are band-independent) and did not affect the validation —
  but **it inflates the reference straddle's P&L on the Lab page by ~45%, which someone will
  eventually read as an edge** (`docs/09` §10).
* **Data gaps, flagged rather than absorbed:** there is **no holiday calendar** (zero holiday rows),
  so a half-day or Golden Week session is forecast as a normal night; the shipped calendar asserts a
  precise 03:00 for BoJ, which **has no fixed announcement time** (the statement lands anywhere from
  roughly 11:30 to 15:00 JST) — the boundary is marked `time_certain=False` and the same rule fires
  on every row whose source begins `approx:`, which is **every hand-entered row in the file**. The
  Tokyo fix and a Gotobi flag are absent.
* **Open RFCs:** `risk_aversion` is in 1/quote-ccy in `zones.hedge_bands` and 1/report-ccy in
  `bandopt.optimal_band` — the two must not be compared until reconciled (RFC-1, assigned to the PM).
  `DeltaCap.binds_pct` holds **percent** while `HedgeRule.band_pct` holds a **fraction**; two
  identically-suffixed fields with different units in one codebase is precisely how the 60× band
  error happened. And the repo states your execution cost three ways that differ by ~5× (RFC-3, §9).

### 8.2 What to run on your own data, in priority order

Each item names the test and the result that would change a verdict.

**1. `overnight.estimate_hour_profile` on two years of your own hourly bars** (Yahoo serves ~730 days
of hourly, free, no key). This is the highest-leverage half-hour available. Every sigma, distance,
touch probability and crossing count in the ladder scales with `var_fraction`, and only one number in
it is measured. The estimator recovers a known profile's window share to **+0.3%** at the volume
Yahoo actually serves (corr 0.9912, 8.5% RMS per hour), so it works; it has simply never seen real
data. *Expect the numbers in §3.1 to move — probably by under 10% on the window share.* The cap goes
as √vf so a 10% error there is a 5% error in the cap, which is tolerable; **the carry and the
crossover vol move much more**, and the pair tilt is what decides the USDJPY sign.

**2. Your broker's actual all-in cost, in pips, to deal EUR 0.5mm at 03:00.** Not a research problem —
a question. It is the single largest uncertainty in the overnight feature, it decides whether the cap
is cheap insurance or a genuine judgement call, and the repo currently contradicts itself about it by
5×.

**3. Re-run the vol evaluation** (30 minutes, needs only your spot history):
`rangeforecast.rv_daily` → `har_walk_forward(min_train=500, refit_every=21)` → `evaluate_forecasts`.
*Expect on real G10 daily data:* HAR `R²_logvar` of **0.35–0.55**, daily log-RV lag-1 autocorrelation
of **0.5–0.7**, and `b_d` the **largest** of the three coefficients (here the monthly component
carries all the signal because the daily proxy is 92% noise). Also check `proxy_scale`: expect it
slightly **above** 1.0; the 0.596–0.634 seen here means the synthetic bar generator is not consistent
with its own closes. If you see lag-1 autocorrelation near 0.05, your RV proxy is too noisy — switch
`rv_method` or check the feed's highs and lows.

**4. Get intraday history and re-measure roughness.** `crossings_series(...)` and
`roughness_kappa(intraday, window_bars=14)`. Hourly bars recover 1.8–2.2× more of the true crossing
count than daily. *Would change the ladder immediately, even without a forecast:* an overnight κ
measured materially away from 1 — κ = 1.97 doubles the expected fills at the same range, κ = 0.32
cuts them to a third. *Would justify a roughness forecast:* a trailing κ that beats **both** the
Brownian null and a trailing mean out of sample. The same data is the only way to measure the
session profile in item 1 rather than assume it.

**5. Re-run the level test — the verdict most likely to change**, because the generator has no
microstructure at all. `levels.level_panel` → `measure_fill_quality(control="permuted")` (the
decision test) and `control="jitter"` (the second opinion) → `measure_reversal_stats` (colour).
*Turning snapping on for a kind requires all five, and the conjunction is deliberate because any one
of them will be met by chance somewhere in a 16 × N-pair grid:* (i) `d_mark > 0` with BH-adjusted
p < 0.05 on the same kind on **at least three pairs**; (ii) same sign and significance under **both**
controls; (iii) `d_ev > 0` too, so the value survives the fill-rate cost of moving the rung; (iv) an
effect **larger than the spread** — everything found here was 0.2–0.4 pips of `d_ev`, which a 0.5-pip
spread eats; (v) `n_fill ≥ 100` on each qualifying pair. Then enable it narrowly, per kind, never for
all kinds.

**6. If you want to revisit the ratchet at all**, the bar is: pooled overnight `VR(13)` **above
1.15** — not merely above 1 — on your own hourly history with a Lo-MacKinlay z above 2; same sign on
at least three pairs; holding in both halves of the sample; against a **simulated null on the same
session lengths**, never against the number 1.0; and an implied edge (`199·φ` USD/night on the
reference book) larger than your own cost uncertainty, which at 15–40× the interbank table it
currently is not. *What would change nothing:* a good backtest over a few months. The effect is
invisible at that sample size, and a positive one should be assumed to be the band-widening confound
in a new costume.

**7. Measure the event uplift once ~2 years of overlap exists** (`calibrate_event_uplift`). *Would
justify replacing `EVENT_SIGMA`:* a variance ratio consistently above 1 with |t| > 2 and the **same
sign on several pairs**. Note the daily-vs-window caveat: a 14:15 ECB is a daily uplift entirely
outside the overnight window.

**8. Fit the implied-vol blend weight — but only after the chain snapshotter has run** for ~250 days.
*Would justify moving off 0.5:* a fitted weight **stable across pairs and across both halves**, with
DM p < 0.05 against HAR alone on the held-out half. A weight that swings 0.22→1.00 is fitting noise.

**9. Start the three reconciliation panels tonight** (§6.4). They cost nothing, they are built from
data the tool already stamps, and after a month they are the only calibration mechanism a trader
without a broker curve has.

---

## 9. Where the documents disagree (open, for the PM)

Flagged rather than silently resolved, per the standard this project has set for itself.

**9.1 The "reference book" is three different books.** All three documents say *EURUSD 1M ATM
straddle, EUR 10mm per leg*, and they do not agree:

| source | spot | ATM | Γ₁ | θ /cal day |
|---|---|---|---|---|
| amendment v1.4 golden fixture / `docs/06` Q-2 | 1.084 | 7.05% | 3.914mm | −2,868 |
| `docs/09` §7.3 | 1.1650 | 7.96% | 3.48mm | ≈ −3,543 (implied by −2,067 over 0.5833d) |
| `docs/13` §1 | 1.1650 | 7.944% | 3.5025mm | −3,504 |
| **`docs/12` §7** | **1.1650** | not stated | **3.91mm** | **−3,978** |

The first three are mutually consistent once the vol mark is accounted for (Γ₁ ∝ 1/σ). **`docs/12`
§7 is not internally consistent:** a Γ₁ of 3.91mm at spot 1.1650 implies σ ≈ 7.05%, at which the
straddle's theta is ≈ −3,100/day, not −3,978 (−3,978 would need σ ≈ 9%). `docs/12`'s gamma side is
self-consistent (G = 335.6mm, window gamma P&L 1,692 at σ_window 0.2726%); only its theta is out, by
~28%. The visible consequence is that `docs/12` implies an overnight carry of about **−629/night**
on a book that `docs/09` prices at **−117/night**. It does not affect `docs/12`'s conclusions — the
ratchet results are all *differences* against a matched baseline — but the reference-book line should
be restated, and the project should adopt **one** reference book with **one** mark.

**9.2 The theta-per-day-of-variance ratio: 1.72 / 1.7 vs the ruled 1.53.** The PM's first amendment
quoted **1.72** at a 34% variance share; the forecasting work measured the share at **0.382**, giving
**1.53**, and the PM verified and ruled that 1.53 stands. But **`docs/09` §9 still prints "1.7× on
EURUSD on a weeknight"** in its plain-language section, and **`docs/11` §1.4 still states "about 34%
of a day's variance… the ratio is ~1.7"**. Both are superseded text, both are in sections a user
would read, and the qualitative conclusion is unchanged either way. Recommend a one-line correction
in each.

**9.3 RFC-3 — your execution cost is stated three ways that differ by ~5×,** and it is unresolved.
`docs/11` §4.4 says both *"0.4–1.0 pips all-in on EURUSD"* (≈0.34–0.86bp) **and** *"15–40× the
[0.2bp interbank] table"* (≈3–8bp). `docs/12` §7 works at **1.03bp**. `bandopt.RETAIL_COST_BP` ships
**5.0bp**. These are not the same number, and the difference is the difference between a cap that is
obviously worth having and one that is arguable. Assigned to the trader; not answered.

**9.4 The delta cap's floor and ceiling are quoted at two precisions.** `docs/13` §0 gives the floor
as **EUR 0.26mm** (the closed form predicts 0.255mm) and the ceiling as **EUR 1.08mm** (= rule R1);
§4.3's measured frontier puts the dominance boundaries at **0.30mm** (CVaR argmin, stable under five
tail measures) and **~1.4mm** (where the CVaR gain is exactly zero). Both are defensible statements
about slightly different things. This document quotes them as ranges; a single convention would be
better.

**9.5 RFC-2 appears resolved but `docs/09` §10 still lists it open.** `docs/09` flags
`zones.hedge_bands.band_ww_base` as using the edge constant 3/2; the band amendment in `docs/08`
records that the PM has already fixed it to read `bandopt.POLICY_CONST`. `docs/09` §10 is stale on
this point.

**9.6 Test counts.** `docs/03` §10.2 reports "4306 passed"; `docs/05` and the README report **4,829
tests** with, at that time, 2 failures (F-10) and 2 strict xfails (F-9, F-13). Amendments v1.9 fixed
all three findings. **No document records a green full-suite run taken after v1.9's changes**, and
the README states the test count without stating that vintage. Worth one confirming run and a line in
the README.

---

## Source map

| This document's § | Built from |
|---|---|
| 1 | `README.md`; `docs/00_charter.md` §1–2; `docs/01_architecture.md` §6; `docs/02_requirements.md` §1.2 |
| 2 | `docs/03_model_spec.md` §2–§10; `docs/02_requirements.md` §0, REQ-042; amendments v1.4, v1.5 Q-5, v1.6, v1.7; `fxgamma/portfolio/zones.py` |
| 3 | `docs/08_overnight_gamma.md` (brief + all five amendments); `docs/09_hedging_theory.md` §3, §6, §7, §8; `docs/11_overnight_desk_spec.md` §1, §3, §5; `docs/13_delta_cap.md` §1–§6 |
| 4 | `docs/10_forecast_evaluation.md` §0–§8; `docs/12_trend_conditional_hedging.md` §0–§12 |
| 5 | `docs/09_hedging_theory.md` §2, §4, §5; `docs/11_overnight_desk_spec.md` §4; `docs/13_delta_cap.md` §4.1, §4.3, §4.5, §4.13, §4.14 |
| 6 | `docs/04_data_sources.md` §0–§4; `docs/11_overnight_desk_spec.md` §6; `docs/13_delta_cap.md` §4.7 |
| 7 | `docs/01_architecture.md` §7 and amendments v1.8, v1.9; `docs/05_test_report.md` §1–§2; `docs/10` §6.1; `docs/12` §4; `docs/13` §3.2 |
| 8 | `docs/04_data_sources.md` §0; `docs/05_test_report.md` §4–§5; `docs/09` §10; `docs/10` §8; `docs/12` §11; `docs/13` §7 |

Credit (`docs/07_credit_gamma.md`) is a v2 design and is deliberately out of scope here. Its one
relevant ruling: a CDX book cannot be marked from free data — a 1-point error in HYG implied vol
becomes a ~9.5-point error in implied *spread* vol — so "CDX gamma analytics from free data" was
**rejected**, and what ships instead is a gamma book in the listed credit-ETF options themselves,
which are the instrument rather than a proxy (amendment v1.3).
