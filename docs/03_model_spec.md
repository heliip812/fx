# 03 — Model Specification

**Owner:** quant (pricing & volatility) · **Scope:** `fxgamma/models/` · **Status:** v1, validated
**Reviewers:** this document is written for a risk manager who has to sign off on the numbers.
Where something is approximate, fragile, or only measured rather than prevented, it says so.

Contents: [1 Notation](#1-notation-and-fx-conventions) · [2 Pricing](#2-pricing-garman-kohlhagen) ·
[3 Greeks & units](#3-greeks-and-their-desk-units) · [4 Implied vol](#4-implied-volatility) ·
[5 Delta conventions](#5-delta-conventions--the-most-dangerous-part-of-this-codebase) ·
[6 Smile construction](#6-smile-construction) · [7 Wings](#7-wings-c1-and-lee-bounded) ·
[8 Density](#8-risk-neutral-density-breeden-litzenberger) ·
[9 gamma_1pct & breakeven](#9-gamma_1pct-and-the-daily-breakeven-identity) ·
[10 Validation](#10-validation-results) · [11 Limitations](#11-model-limitations-read-this-one)

---

## 1. Notation and FX conventions

A pair is written `FORDOM`. `EURUSD` means **base / foreign = EUR**, **quote / domestic = USD**.

| symbol | meaning | units |
|---|---|---|
| `S` | spot | DOM per 1 FOR |
| `K` | strike | DOM per 1 FOR |
| `F` | outright forward, `F = S e^{(r_d − r_f)T}` | DOM per 1 FOR |
| `r_d` | domestic (quote ccy) continuously-compounded zero | decimal |
| `r_f` | foreign (base ccy) continuously-compounded zero | decimal |
| `T` | time to the expiry **cut**, ACT/365F | years |
| `σ` | implied volatility | decimal (`0.085` = 8.5%) |
| `cp` | `+1` call on base, `−1` put on base | — |
| `N` | notional, **always in base ccy** | base ccy |
| `k` | forward log-moneyness, `k = ln(K/F)` | — |
| `w` | total implied variance, `w = σ²T` | — |
| `φ`, `Φ` | standard normal pdf / cdf | — |

Two conventions are load-bearing and easy to get wrong:

* **Notional is base ccy, PV is quote ccy.** A long EURUSD call on EUR 10mm is the right to buy
  EUR 10mm; its PV is in USD.
* **Log-moneyness is measured against the *forward*, not spot.** A parallel shift in `r_d − r_f`
  then translates the smile rigidly instead of reshaping it. Every smile abscissa in this package
  is `ln(K/F)`.

Time uses the expiry **cut** (`conventions.expiry_datetime`), not midnight: NY 10:00 for G10, TKY
15:00 for EURJPY, LDN 16:00 for EURGBP. `year_fraction` returns exactly `0.0` once the cut has
passed, so an expired option prices to intrinsic with zero gamma, vega and theta.

---

## 2. Pricing: Garman–Kohlhagen

`gk.gk_price(S, K, T, rd, rf, sigma, cp)` — value **per 1 unit of base notional**, in quote ccy:

```
d1 = [ ln(S/K) + (r_d − r_f + σ²/2) T ] / (σ √T)
d2 = d1 − σ √T
V  = cp [ S e^{−r_f T} Φ(cp·d1) − K e^{−r_d T} Φ(cp·d2) ]
```

Degenerate inputs collapse to intrinsic rather than to `nan`: at `T = 0` to `max(cp(S−K), 0)`, and
for a zero-vol but still-live option to the discounted forward intrinsic
`max(cp(F−K), 0) e^{−r_d T}`.

No-arbitrage bounds (`gk.no_arb_bounds`) are `max(cp(S e^{−r_f T} − K e^{−r_d T}), 0)` below and
`S e^{−r_f T}` (call) / `K e^{−r_d T}` (put) above.

### 2.1 One numerical detail that is not a detail

`Φ` is computed as `0.5 · erfc(−x/√2)`, **never** as `0.5(1 + erf(x/√2))`. The two are
algebraically identical and differ catastrophically in floating point: the `erf` spelling forms
`1.0 + (−1.0)` in the lower tail and returns **exactly 0.0** from about `x = −8.3` downward, where
the true values are `Φ(−8.3) = 5.2e−17`, `Φ(−20) = 2.8e−89`. Since `Φ(d2)` *is* the premium-adjusted
delta and *is* the quantity whose sign decides the delta-peak bracket (§5), that spurious zero
converts a strictly negative quantity into a tie at `0.0` — which is precisely how the
premium-adjusted call-strike solver used to return `nan` for **every** USDJPY, USDCHF, USDCAD,
USDSEK and USDNOK strike and take those five surfaces down entirely. The same applies to
`Φ⁻¹`, which uses `−√2 · erfcinv(2p)` rather than `√2 · erfinv(2p − 1)`; the latter rounds `2p − 1`
to `−1.0` for `p ≤ 1e−17` and returns `−inf`.

---

## 3. Greeks and their desk units

`gk.gk_greeks(...)` returns the frozen `Greeks` dataclass; `gk.gk_greeks_array(...)` is the
vectorised twin used by the spot ladder. Every monetary Greek below is **already multiplied by
`notional_base × direction`**; `delta_pct` carries `direction` only.

| field | formula (per 1 base notional) | **desk units** |
|---|---|---|
| `pv` | `cp[S e^{−r_f T}Φ(cp d1) − K e^{−r_d T}Φ(cp d2)]` | **quote ccy**, total |
| `delta_pct` | convention-dependent, see §5 | spot delta **per 1 unit of base notional** (dimensionless) |
| `delta_base` | `N·dir·delta_pct` | **base ccy you are long**; hedge = sell this much base |
| `gamma` | `e^{−r_f T} φ(d1) / (S σ √T)` | **base ccy of delta per 1.00 of spot** |
| `gamma_1pct` | `gamma · S / 100` | **base ccy of delta gained per +1% spot move** ← the desk unit |
| `vega` | `S e^{−r_f T} φ(d1) √T × 0.01` | **quote ccy per 1 vol point** (per 0.01 of σ) |
| `theta` | `[−S e^{−r_f T}φ(d1)σ/(2√T) + cp·r_f S e^{−r_f T}Φ(cp d1) − cp·r_d K e^{−r_d T}Φ(cp d2)] / 365` | **quote ccy per calendar day** |
| `rho_d` | `cp·K·T·e^{−r_d T}Φ(cp d2) × 0.01` | quote ccy per **1 percentage point** (100bp) of `r_d` |
| `rho_f` | `−cp·S·T·e^{−r_f T}Φ(cp d1) × 0.01` | quote ccy per **1 percentage point** of `r_f` |
| `vanna` | `−e^{−r_f T} φ(d1) d2 / σ × 0.01` | quote ccy **per vol point per 1.00 of spot** |
| `volga` | `vega_raw · d1 d2 / σ × 0.01²` | quote ccy **per vol point squared** |
| `dual_delta` | `−cp e^{−r_d T} Φ(cp d2)` | quote ccy **per 1.00 of strike** |

Three unit traps worth stating explicitly, because everything downstream builds on them:

1. **`vega` is per vol *point*, not per unit of σ.** A 10mm EURUSD 3M ATM has vega ≈ USD 2,200 —
   that is the P&L for the vol going 8.0 → 9.0, not 8% → 108%.
2. **`theta` is per *calendar* day**, i.e. `∂V/∂t` divided by 365, not per business day. Business /
   event-weighted decay is opt-in at the portfolio layer (contract amendment CG-4) and must be
   badged in the UI.
3. **`gamma` is `∂(delta_base)/∂S`, not `∂²V/∂S²` in "dollars".** They are the same number here
   because `∂V/∂S` *is* the base-ccy delta, but the naming matters: `gamma × S/100` is the
   base-ccy delta you pick up per 1% spot, which is what a trader actually rebalances.

`delta_pct` and `dual_delta` are **intensive** (per-unit / per-strike) and are set to `nan` by
`Greeks.__add__`, so a book-level card reads "n/a" rather than a confident wrong number
(amendment T-2).

---

## 4. Implied volatility

`gk.implied_vol(price, S, K, T, rd, rf, cp, tol=1e-10)` inverts §2 for σ. Returns `nan` — never
raises — on bad input.

1. Reject prices strictly outside the no-arbitrage band → `nan`.
2. **Always invert on the out-of-the-money leg.** An ITM price is (forward intrinsic) + (time
   value); for a deep-ITM option the intrinsic is `O(1)` and the time value can be `O(1e−13)`, so
   the only σ-dependent part is destroyed by cancellation. Put–call parity is exact and free, so
   the deep-ITM case is converted into the deep-OTM one. Measured effect: a 1-day EURUSD
   0.92-moneyness call inverted to 3.6e−3 *relative* vol error before this, and to machine
   precision after.
3. Work on the undiscounted Black price with forward `F`, removing `r_d`/`r_f` from the iteration.
4. Seed with Brenner–Subrahmanyam (ATM) or Corrado–Miller (away), then safeguarded Newton.
5. Fall back to Brent on a grown bracket if Newton stalls, leaves the bracket, or vega underflows.

**Tolerances are scale-aware, and this matters.** Two different quantities were previously one
number: the *arbitrage* tolerance (how far outside the band a quote may stray and still be
accepted, `1e−12·max(1,|upper bound|)`) and the *price resolution* (`gk_price` is a difference of
two terms of size `~max(S e^{−r_f T}, K e^{−r_d T})` and so carries `~1e−16` of that in absolute
error). Using the arbitrage tolerance for the "zero time value → return 0.0" shortcut made that
shortcut 1.5e−10 wide for a USDJPY strike and 1.2e−12 for a EURUSD one, so perfectly solvable
deep-wing JPY options came back as a confident **0.0 vol**. The shortcut is now keyed to the
resolution, and the Newton stopping rule is tightened to `min(tol, max(1e−12·price, resolution))`
so a 1e−12-valued option is not declared converged at its seed.

**Honest limit.** Where the time value is at or below `~1e−15 · max(S e^{−r_f T}, K e^{−r_d T})`,
there is no information about σ left in a double-precision price. The function returns `0.0` there
(96 of 450 grid cases, all ≥ 20 standard deviations out) rather than inventing a vol. It never
returns a number outside the no-arb band.

---

## 5. Delta conventions — the most dangerous part of this codebase

### 5.1 The four conventions

`gk.delta_from_strike(K, S, T, rd, rf, sigma, cp, convention)`, per 1 base notional:

| convention | formula | premium settled in | hedge instrument |
|---|---|---|---|
| `spot` | `cp e^{−r_f T} Φ(cp d1)` | DOM (quote) | spot |
| `fwd` | `cp Φ(cp d1)` | DOM (quote) | forward |
| `spot_pa` | `cp (K/S) e^{−r_d T} Φ(cp d2)` | **FOR (base)** | spot |
| `fwd_pa` | `cp (K/F) Φ(cp d2)` | **FOR (base)** | forward |

Premium-adjusted (`_pa`) means the premium is paid in the *base* currency, so it is itself an FX
position and must be netted out of the hedge: `Δ_pa = Δ_spot − V_dom/S`.

### 5.2 Which pair uses which (from `conventions.PAIRS`, frozen)

| pair | base | quote | pip | delta convention | cut |
|---|---|---|---|---|---|
| EURUSD | EUR | USD | 1e−4 | `spot` | NY10 |
| GBPUSD | GBP | USD | 1e−4 | `spot` | NY10 |
| AUDUSD | AUD | USD | 1e−4 | `spot` | NY10 |
| NZDUSD | NZD | USD | 1e−4 | `spot` | NY10 |
| EURGBP | EUR | GBP | 1e−4 | `spot` | LDN16 |
| EURCHF | EUR | CHF | 1e−4 | `spot` | NY10 |
| **USDJPY** | USD | JPY | 1e−2 | **`spot_pa`** | NY10 |
| **USDCHF** | USD | CHF | 1e−4 | **`spot_pa`** | NY10 |
| **USDCAD** | USD | CAD | 1e−4 | **`spot_pa`** | NY10 |
| **USDSEK** | USD | SEK | 1e−4 | **`spot_pa`** | NY10 |
| **USDNOK** | USD | NOK | 1e−4 | **`spot_pa`** | NY10 |
| **EURJPY** | EUR | JPY | 1e−2 | **`spot_pa`** | TKY15 |

The rule of thumb behind the table: USD-base pairs quoted with USD premium are premium-adjusted.
Using spot delta for USDJPY moves the 25-delta pillar strikes by roughly 0.5–1% of spot, which is a
larger error than the entire butterfly the smile is trying to represent.

The ATM (delta-neutral straddle) convention must be chosen consistently:
`atm_strike = F e^{+σ²T/2}` for `dns` (`d1 = 0`) but `F e^{−σ²T/2}` for `dns_pa` (`d2 = 0`).
**The sign flips.** `smile.atm_convention_for()` derives it from the delta convention so it cannot
be got backwards by hand; at 1Y / 10 vol, getting it wrong moves the ATM pillar by ~1% of spot.

### 5.3 Why the premium-adjusted *call* delta is non-monotone

This is the single most dangerous convention in the codebase. Read this section before touching
anything that solves for a strike.

For a call, `|Δ_pa| = (K/S) e^{−r_d T} Φ(d2(K))`. As `K → 0` the `Φ(d2) → 1` but the `K` prefactor
wins, so the delta → 0. As `K → ∞`, `Φ(d2) → 0` faster than `K` grows, so the delta → 0 again. It
is therefore **zero at both ends with an interior maximum**: the map from strike to delta is
**two-to-one**. Every non-zero attainable delta has *two* roots.

The peak is the sign change of

```
d/dK [ K Φ(d2) ] = Φ(d2) − φ(d2) / (σ√T)
```

from `+` to `−`, found by bisection in log-strike (`gk.pa_call_delta_peak`). Note this expression is
exactly where the `erfc` accuracy of §2.1 earns its keep: far out of the money the two terms are
both tiny, and if `Φ(d2)` underflows to a spurious `0.0` the difference becomes a **tie at zero**
instead of a strictly negative number. A `>=` comparison then reads the tie as "still increasing",
returns the bracket edge as the peak, poisons the Brent bracket and makes every premium-adjusted
call strike unsolvable. Both defences are required and both are in place: the accurate lower tail,
and a strict `>` in the bracket guard and the bisection.

**Which root is market standard.** The market always quotes the root on the **decreasing branch**,
`K ≥ K_peak` — the out-of-the-money one. `gk.strike_from_delta` restricts Brent to
`[K_peak, K_hi]` accordingly. Reference: Reiswich & Wystup (2010) §3.2. Concretely, USDJPY 1Y at
10.5 vol, `S = 147.5`: `K_peak = 119.24` (0.835 × forward), the market 25-delta call strike is
**152.76**, and the spurious left-branch root at `K = 100` would carry a delta of **0.67** — the
solver would have returned a strike 35% below spot for a 25-delta call.

**The max-attainable-delta caveat.** Because the branch has a maximum, a requested delta can simply
be **unattainable**. `gk.max_attainable_delta(S, T, rd, rf, sigma, cp, convention)` returns it, and
`strike_from_delta` returns `nan` above it rather than a wrong root. This is not a corner case at
long tenors:

*Max attainable premium-adjusted call delta — USDJPY, `S = 147.5`, `r_d = 0.75%`, `r_f = 4%`*

| tenor | 5 vol | 10 vol | 20 vol | 40 vol |
|---|---|---|---|---|
| ON | 0.991 | 0.983 | 0.969 | 0.943 |
| 1M | 0.955 | 0.922 | 0.866 | 0.778 |
| 3M | 0.925 | 0.874 | 0.794 | 0.676 |
| 1Y | 0.849 | 0.771 | 0.656 | 0.508 |
| 2Y | 0.782 | 0.690 | 0.562 | 0.411 |
| 5Y | 0.643 | 0.540 | 0.411 | 0.276 |

At 5Y / 40 vol the maximum attainable is 0.276 — a **25-delta call is quotable but a 30-delta call
does not exist**. The application must surface "unattainable in this convention", not an empty cell
and not a silently substituted number.

---

## 6. Smile construction

Broker input is the frozen `SmileQuotes(T, atm, rr25, bf25, rr10, bf10)` (contract §8), all
decimals, `atm` on the DNS convention, `bf` interpreted as a **smile** butterfly. Every provider
funnels through `surface.build_surface(pair, asof, quotes, spot, rd, rf, method)`.

### 6.1 Smile vs market strangle

* **Smile strangle** (what this library consumes): `bf_ss = (σ_call + σ_put)/2 − σ_atm`. Purely
  algebraic; `rr_bf_to_vols` inverts it as `σ_call = atm + bf + rr/2`, `σ_put = atm + bf − rr/2`.
* **Market strangle** (what brokers usually trade): a *price* definition — the strangle whose two
  legs are struck at 25 delta computed with the single vol `σ_atm + bf_ms` and priced at that same
  vol.

They are different objects. `vanna_volga.market_to_smile_bf` converts by Brent-solving
`V(K_ms_p, σ_smile) + V(K_ms_c, σ_smile) = V(K_ms_p, σ_atm+bf_ms) + V(K_ms_c, σ_atm+bf_ms)`,
rebuilding the smile at each iteration so the pillar strikes stay self-consistent. Confusing the
two typically moves the 25d wings by 0.05–0.30 vol, more on high-skew pairs. Pass
`bf_convention="market"` if your source quotes MS. **If the solve fails it returns `bf_market`
unchanged** — a silent fallback; check with a reprice if it matters.

### 6.2 Pillar strikes

`smile.delta_pillar_strikes` solves each wing strike with **its own** vol (the smile vol at that
delta), not the ATM vol. Using the ATM vol for all three is a common and material bug on skewed
pairs, and it is what makes a surface fail to reprice its own risk reversal.

### 6.3 Vanna–volga — the FX market standard, and the default

FX brokers quote exactly three liquid instruments per tenor. VV answers the market maker's own
question: *given that I can hedge vega, vanna and volga with those three, what does a fourth option
cost?* It is exact at the three pillars by construction, model-free (no calibration, no optimiser),
and fast enough to rebuild a whole surface on every tick.

With `K1 < K2 < K3` the 25d put / DNS ATM / 25d call and Lagrange weights in log-strike
`y_i(K_j) = δ_ij`:

```
D1(K) = y1 σ1 + y2 σ2 + y3 σ3 − σ2
D2(K) = y1 d1(K1)d2(K1)(σ1−σ2)² + y3 d1(K3)d2(K3)(σ3−σ2)²
σ(K)  = σ2 + [ −σ2 + √( σ2² + d1(K)d2(K)(2σ2 D1 + D2) ) ] / (d1(K)d2(K))
```

with `d1, d2` at `σ2` (Castagna–Mercurio 2007 eq. 14). Implemented in the algebraically identical
but numerically stable conjugate form

```
σ(K) = σ2 + (2σ2 D1 + D2) / (σ2 + √rad)
```

The literal spelling is a difference of two nearly equal positive numbers divided by `d1 d2`, and
`d1 d2 = 0` at two strikes that in FX sit within a few tenths of a percent of the money. The old
code guarded that with `|d1 d2| > 1e−12` and *switched branch* to the first-order value `σ2 + D1`
below it — a small but real discontinuity in the smile a hair away from ATM. The conjugate form is
exact and continuous at `d1 d2 = 0` (collapsing to the correct limit `D1 + D2/(2σ2)`), so no branch
on `d1 d2` is needed at all. `method="exact"` instead prices the replication portfolio and inverts
Black–Scholes; the two agree to ~1e−5 vol over the 10d–10d range and `"approx"` is ~40× faster.

**Term structure**: ATM is interpolated in **total variance** linearly in `T` (the only
interpolation that cannot manufacture calendar arbitrage on the ATM line); RR and BF linearly in
`T`; flat-quote extrapolation outside the grid. *Quotes* are interpolated, not parameters, so the
wing behaviour stays anchored to something a trader can read.

### 6.4 SABR (Hagan lognormal) — the cross-check

```
dF = α F^β dW,   dα = ν α dZ,   d⟨W,Z⟩ = ρ dt
```

with Hagan et al. (2002)'s singular-perturbation expansion for the Black implied vol and the
Obłój `z/χ(z)` normalisation. `β` is **fixed at 1.0** (lognormal backbone) and never fitted: `β`
and `ρ` are not jointly identifiable from a single smile — the backbone and the skew trade off
almost perfectly. Calibration fits `(ρ, ν)` by trust-region least squares on the vol residuals with
`α` implied from the ATM quote at every step (`alpha_from_atm`, the ATM cubic), which pins the fit
to the most liquid quote and halves the search space. Deterministic: no random restarts.

`z/χ(z)` has a removable singularity at `z = 0`; near it the series
`1 + ρz/2 + (2−3ρ²)z²/12` is used, with the **series** (not the constant `1.0`) as the fallback if
`χ` still misbehaves. Returning `1.0` there is the ATM ratio and would silently price a skewed
strike as if it were at the money.

Use SABR for smile *dynamics* (how vega moves when spot moves) and as the wing model beyond 10
delta, where VV is unreliable — see §11.

### 6.5 Interpolated / SVI — for listed chains

`InterpolatedSurface.from_chain(strikes, expiries, vols, ...)` fits each expiry slice with **raw
SVI** in total variance,
`w(k) = a + b[ρ(k−m) + √((k−m)² + s²)]`, using the Zeliade quasi-explicit two-stage scheme (a
bounded linear least squares in `(a, d, c)` for fixed `(m, s)`, then a 2-D Nelder–Mead over
`(m, log s)`, then a full 5-parameter polish carrying the arbitrage penalties). Slices with fewer
than 5 usable quotes fall back to a monotone PCHIP in `k` on total variance.

Penalties in the polish: Gatheral's butterfly functional
`g(k) = (1 − k w'/2w)² − (w'²/4)(1/w + 1/4) + w''/2` going negative; positivity of
`a + b s √(1−ρ²)`; **Lee's bound** `b(1+|ρ|) ≤ 2` on the asymptotic slopes. That last one is
tightened from the Zeliade box's `≤ 4`, which in total-variance units is twice Lee's bound and
leaves enough slack for a fit to look clean on the quoted strikes and be arbitrageable in the
extrapolated wings.

In time: linear in total variance at fixed log-moneyness between slices, `∝ T` outside.
`enforce_calendar=True` clips each slice's input `w` up to its predecessor's; that is an
**adjustment to market data**, so the count and the maximum adjustment are now carried on the slice
and reported by `diagnostics()` (contract §7 provenance).

---

## 7. Wings: C1 and Lee-bounded

Outside the quoted pillars the smile must be continued, and the obvious continuations all fail in
the same way — as **density spikes**, not as visibly wrong vols.

### 7.1 What goes wrong

* **Raw linear continuation of the boundary slope.** If that slope points the wrong way — and for a
  10d-anchored right wing it often does, whenever the 10d call vol prints below the C1 continuation
  of the 25d point — total variance runs to zero and then hits the evaluator's floor. The floor
  join is a slope discontinuity: `w'` jumps, `w''` contains a Dirac, and Breeden–Litzenberger puts
  a delta function in the density there. On a 3M USDJPY smile with atm 12 / rr25 −5 / bf25 +0.4 /
  rr10 −9 / bf10 +1.2, the join fell at **K = 171.9** and *every strike above it priced at a 0.01%
  vol*; `vol(165)` read 3.79% against a 12% ATM. Roughly **half** of all strikes scanned over
  `k ∈ [−3, 3]` were pinned at the floor.
* **Clipping the slope to Lee's bound at the join.** This satisfies Lee but breaks C1 exactly where
  the pieces meet — the same Dirac, just moved inward to a strike people actually trade.

### 7.2 What is implemented

Three regions per side, all joined C1 in total variance (`smile.fit_wing` / `eval_wing`, shared by
`vanna_volga` and `interp`):

1. **Core**, `k1 ≤ k ≤ k3` (25d put to 25d call): the vanna–volga vol.
2. **Anchor**, `k3 < k ≤ kR`: the unique quadratic matching *value and slope* at `k3` **and passing
   through the 10d quote** at `kR`, so all five broker quotes reprice exactly. Empty (`kR = k3`)
   when no 10d quotes are supplied. A positivity guard raises the curvature if the quadratic would
   dip below 5% of the smaller endpoint; it fires only on a 10d quote inconsistent with its own 25d
   smile, and when it does the 10d is no longer repriced exactly.
3. **Tail**, `k > kR`: with `u = k − kR` and `q` the anchor's outward slope arriving at `kR`,

   ```
   w(u) = w(kR) + β u + (q − β) λ (1 − e^{−u/λ}),     β = clip(q, 0, lee_cap)
   ```

Left wing mirrored (`u = kL − k`, `q = −dw/dk`). This gives, exactly and unconditionally:

| property | why |
|---|---|
| `w(0) = w(kR)` | continuous |
| `w'(0) = β + (q − β) = q` | **C1 at the join, with no clipping applied there** — the market-quoted boundary slope is honoured |
| `w'(u) = β + (q−β)e^{−u/λ} → β` monotonically | asymptotic slope obeys **Lee's bound** and can never have the wrong sign |
| `w''(u) = −((q−β)/λ)e^{−u/λ}` | bounded and *continuous* — no Dirac, no density spike |
| `w(u) ≥ w(kR) + min(0,q)λ > 0` | positivity, enforced by shrinking `λ`; the `max(w, floor)` guard never engages |

`λ` is a pure shape parameter (`max(2|k_join|, 0.10)`) — the distance over which the slope relaxes.
It affects none of the guarantees above.

**Lee's bound** (Lee 2004): `limsup_{k→±∞} w(k)/|k| ≤ 2`. Steeper than that in the limit and the
implied density has no finite moments, so butterfly arbitrage is guaranteed far enough out. Note
the bound is **asymptotic**: at finite `k` the slope interpolates monotonically from the quoted join
slope toward the cap, so a smile quoted with a steeper join slope will legitimately exceed 2.0 over
a finite region before relaxing. Measured worst case over 2000 randomised smiles: `|dw/dk| = 2.0004`
at `k = ±8`, on one smile out of 2000.

On the reference defect case above, the right tail now leaves the 10d anchor with the same slope it
always had (`q = −0.01794`) and relaxes to `β = 0` over `λ = 0.079` — a flat total-variance
asymptote. `vol(165) = 6.64%`, `vol(172) = 5.82%`, `vol(300) = 4.35%`, **zero** floor hits, density
strictly non-negative.

---

## 8. Risk-neutral density (Breeden–Litzenberger)

```
q(K) = e^{r_d T} ∂²C/∂K²
```

with `C(K)` the undiscounted-notional call price at the *smile* vol `σ(K)`.
`smile.risk_neutral_density(vol_fn, S, T, rd, rf, n=801, n_std=6.0)` takes the second derivative by
non-uniform central differences on a **log-uniform** strike grid (far better conditioned than a
uniform one for a lognormal-ish underlying) and returns a `DensityReport` with the strikes, the
density, the trapezoidal integral, `min_density`, `peak_density`, `rel_min_density`, the violation
count and a note.

Negative density *is* the definition of butterfly (call-spread convexity) arbitrage. Two things
about how it is tested:

* **The tolerance is relative to the peak, not absolute.** `q` has units of 1/spot, so an absolute
  threshold means completely different things across pairs: a 1M EURUSD density peaks near 8.6 per
  USD while the same smile on USDJPY peaks near 0.027 per JPY — a factor of ~320. The old fixed
  `atol = −1e−8` therefore called USDJPY clean at 320× the relative violation it rejected on
  EURUSD. The default is now `rtol = −1e−6` applied as `q < rtol · peak`.
* **`n` has an optimum and more is not better.** The second difference amplifies the price's own
  round-off by `4ε|C|/h²`, so the noise floor of `q` grows like `n²` while truncation falls like
  `n⁻²`. An EURUSD overnight slice reports `min q = +1.2e−9` at `n = 401` and `−9.3e−6` at
  `n = 8001`, the negativity scaling *exactly* as `n²`. That is double precision, not arbitrage.
  `n = 401…801` is the sweet spot and is what the `diagnostics()` methods use.

Every surface exposes `diagnostics()` returning `SurfaceDiagnostics(butterfly, calendar,
fit_rmse_vol, ok)` with a one-line `summary()` for the UI badge. **Run it. It is not optional
housekeeping — it is the check that decides whether a wing price is usable.**

---

## 9. `gamma_1pct` and the daily breakeven identity

The risk engine and the app both build on these two primitives, so they are defined here once.

### 9.1 `gamma_1pct` (Γ₁)

```
Γ₁ = gamma × S / 100
```

**The change in `delta_base` (base ccy) for a +1% move in spot.** It is the number a trader
rebalances against, and unlike raw `gamma` it is comparable across pairs regardless of the level of
spot (`gamma` for USDJPY at 147.5 and for EURUSD at 1.165 differ by two orders of magnitude for
identical risk). Verified to be exactly `gamma·S/100` (0.0 relative error, it is an algebraic
identity, not a numerical one).

### 9.2 Daily breakeven

A delta-hedged position earns gamma P&L `½ · ∂²V/∂S² · (ΔS)²` in quote ccy. Writing `ΔS = S·x`
and substituting `gamma = 100 Γ₁ / S`:

```
gamma P&L = 50 · Γ₁ · S · x²
```

Setting that equal to one calendar day's decay `|θ|` and expressing the move as a **percentage**
`BE% = 100x`:

```
BE% = √( |θ| / (0.005 · Γ₁ · S) )
```

This rearrangement is exact — verified to 2.9e−16 relative. `θ` and `Γ₁` are the desk-unit fields
of §3 (quote ccy per calendar day; base ccy of delta per 1% spot).

### 9.3 Reduction to `σ_ATM/√365`

For an ATM option, take the pure time-decay part of theta,
`θ_γ = −S e^{−r_f T} φ(d1) σ / (2√T) / 365`, and `Γ₁ = 0.01 e^{−r_f T} φ(d1) / (σ√T)`. Substituting:

```
BE%² = 200 |θ_γ| / (Γ₁ S) = (200/730) σ²   ⟹   BE% = 100 · σ_ATM / √365
```

i.e. **BE (fractional) = σ_ATM/√365**, independent of tenor, spot and notional. Verified to
2.4e−16 relative across 9 pair/tenor combinations. This is the familiar "a 10 vol option breaks
even on a 0.52% daily move".

> **Caveat, and it is a big one.** The reduction holds only for the **carry-free** theta. Using the
> *total* `Greeks.theta` — which includes the `cp·r_f S e^{−r_f T}Φ(cp d1) − cp·r_d K e^{−r_d T}Φ(cp d2)`
> rate terms — the two differ by the rate differential. Measured: GBPUSD (`r_d − r_f = 0.25%`)
> agrees to 0.3–0.8%; EURUSD (2.0%) is 8–25% out; **USDJPY 1Y (−3.25%) is 54% out**. On a
> high-carry pair the carry term *dominates* long-dated theta. The app must therefore say which
> theta a breakeven number is built on, and for a carry pair the `σ_ATM/√365` shorthand is simply
> wrong.

---

## 10. Validation results

Grid: 450 pricing cases = 3 pairs (EURUSD 1.165 / USDJPY 147.5 / GBPUSD 1.345 with their real rate
pairs) × 5 moneyness (0.80–1.25) × 5 tenors (1D, 1M, 3M, 1Y, 2Y) × 3 vols (5/10/25) × call & put.
Surfaces from `fxgamma.data.get_provider("synthetic")`, G3, 9 tenors ON→1Y.
Harness: `scratchpad/quantmodels/{mv,stress,cmp}.py`.

### 10.1 Greeks vs finite differences

All nine analytic Greeks against **5-point central** finite differences (`O(h⁴)` truncation), with
step sizes scaled to each Greek's own feature width (`S σ√T` in spot; `σ/|d1 d2|` in vol — a fixed
step is a third of the feature 6 sd out). A case is scored only where the FD sits ≥1000× above its
own round-off floor; skipped counts are shown, because comparing an analytic Greek against an FD
noisier than the Greek tests the difference quotient, not the model.

| Greek | worst rel. err | best rel. err | skipped (FD noise) |
|---|---|---|---|
| `delta_pct` | 3.9e−07 | 1.2e−14 | 51/450 |
| `gamma` | 9.5e−06 | 2.1e−11 | 96/450 |
| `vega` | 1.9e−05 | 2.9e−13 | 112/450 |
| `theta` | 5.1e−08 | 3.6e−14 | 56/450 |
| `rho_d` | 7.8e−09 | 1.2e−14 | 60/450 |
| `rho_f` | 6.0e−09 | 3.5e−15 | 60/450 |
| `vanna` | 1.3e−07 | 8.6e−12 | 120/450 |
| `volga` | 1.6e−06 | 6.8e−13 | 122/450 |
| `dual_delta` | 3.8e−07 | 1.6e−14 | 51/450 |

**Worst 1.9e−05, best 3.5e−15.** Confirms the PM's "<1e−4 relative"; the best figure here is better
than the PM's 1.4e−9 because of the 5-point stencil. Independent cross-check: `gamma` against a
5-point **second** difference of the price agrees to 7.7e−4 worst / 4.2e−9 best (a second difference
is intrinsically noisier — this is the stencil's limit, not the model's).

### 10.2 Everything else

| check | measured | verdict |
|---|---|---|
| Put–call parity, `\|C−P−(Se^{−r_f T}−Ke^{−r_d T})\|/S` | **3.8e−16** | exact (1.7 ulp). Confirms PM |
| Parity in Greeks: `γ_C=γ_P`, `vega_C=vega_P`, `Δ_C−Δ_P=e^{−r_f T}` | **2.2e−16** | exact |
| `implied_vol` round-trip, well-conditioned | **2.0e−06** rel | see note ▼ |
| `implied_vol` round-trip, within 1000× the noise floor (6 cases) | 3.5e−05 rel | information-theoretic limit |
| `implied_vol` declined → `0.0` (time value < price resolution) | 96/450 | by design, ≥20 sd out |
| `implied_vol` non-convergences (`nan`) | **0** | |
| `strike_from_delta` ∘ `delta_from_strike`, `spot` | **3.3e−14** | Confirms PM |
| … `spot_pa` | **7.9e−14** | |
| … `fwd` | **2.4e−14** | |
| … `fwd_pa` | **8.4e−14** | |
| Premium-adjusted deltas correctly returning `nan` as unattainable | 0/2250 on this grid | lowest max-attainable pa-call delta on the grid: **0.515** |
| Vanna–volga reprices its own ATM / 25RR / 25BF / 10RR / 10BF | **7.7e−11** vol | Confirms PM's ~1e−13 order; see note ▼ |
| SABR fit RMSE to the 5 pillars (max over 27 slices) | **1.5e−03** vol | Confirms PM's ~2e−4 order; see note ▼ |
| SABR fit max single-pillar error | 2.9e−03 vol | |
| SABR parameter recovery `α` / `ρ` / `ν` from synthetic smiles | **3.8e−16 / 8.5e−16 / 1.1e−15** | exact |
| Scalar-path vs vectorised-path vol, and pickle round-trip | **1.2e−15** | |
| Full repo test suite | **4306 passed** | |

▼ **`implied_vol` 2.0e−06, not 8.2e−16.** The PM's figure is the *price* round-trip; the *vol*
round-trip is limited by `tol/vega`. The worst case is a deep-OTM 1Y GBPUSD put at 5 vol worth
2.5e−15 in quote ccy, against a price resolution of 1.3e−15 — the price is barely above its own
noise. This is a real and unavoidable limit, and the function is now honest about it rather than
returning `0.0`.

▼ **VV 7.7e−11 and SABR 1.5e−03, not 1e−13 and 2e−4.** Both differ from the PM's numbers because
the *fixed-point* reprice (solve `σ = surface(strike_from_delta(δ, σ))` at 25d **and 10d**, four
quantities per tenor) is a strictly harder test than repricing at fixed pillar strikes, and because
the synthetic provider is date-seeded so quotes differ run to run. 7.7e−11 vol is a hundredth of a
basis point — the fixed-point tolerance, not a modelling error. SABR at 1.5e−03 is fitting **5**
pillars with **2** free parameters (`α` pinned to ATM, `β` fixed): a ~0.15 vol point residual on a
skewed 5-point smile is the honest capacity limit of the model, not a calibration failure. Where
the PM fitted 3 pillars with 3 parameters it is near-exact.

### 10.3 Density, wing C1 and arbitrage — the deliverables of this fix

On all 27 G3 slices (VV, `n = 801`, ±8 ATM sd):

| quantity | measured | required |
|---|---|---|
| **min density** over all 27 slices | **−1.8e−08 absolute, −2.2e−10 relative to peak** | ≥ 0 ✔ |
| **density integral**, range over 27 slices | **0.99900 … 1.000000** | ≈ 1 ✔ |
| max `\|jump in dw/dk\|` across all 4 wing joins | **9.1e−08** (measurement floor) | 0 ✔ |
| strikes hitting the vol floor over `k ∈ [−3,3]` | **0** (was ~9,500 of 20,001 on the reference case) | 0 ✔ |
| min total variance over `k ∈ [−3,3]` | **6.0e−06 > 0** | > 0 ✔ |
| max `\|dw/dk\|` at `k = ±6` | **0.072** | ≤ 2.0 (Lee) ✔ |
| calendar + butterfly diagnostics, G3 × {VV, SABR, interp} | **9/9 clean** | clean ✔ |

The single negative value (−1.8e−08 absolute, EURUSD overnight) is −2.2e−10 of the peak density and
is the round-off of the second difference, per §8.

**Proof that the joins are genuinely C1**, not merely small. Measuring the one-sided limits of
`dw/dk` with an `O(h²)` three-point stencil on the USDJPY 1Y smile and shrinking `h`:

| `h` | jump @ `k1` (core join) | @ `k3` (core join) | @ `kL` (tail join) | @ `kR` (tail join) |
|---|---|---|---|---|
| 1e−2 | 7.26e−03 | 3.19e−03 | 9.5e−14 | 6.5e−14 |
| 1e−3 | 7.41e−05 | 1.90e−05 | 1.5e−12 | 4.6e−13 |
| 1e−4 | 7.39e−07 | 1.77e−07 | 1.2e−11 | 1.3e−12 |
| 1e−5 | **7.66e−09** | **1.33e−09** | 1.7e−10 | 5.2e−11 |
| 1e−6 | 1.31e−09 | 9.47e−10 | (round-off) | (round-off) |

The core joins fall **exactly as `h²`** (ratios 98, 100, 97) down to the round-off floor — the
signature of a perfectly C1 function measured with an `O(h²)` stencil. A genuine discontinuity would
plateau at a non-zero value. The tail joins (`kL`, `kR`), where C1 holds analytically, sit at
1e−12–1e−14 at every `h`, i.e. exactly zero. Residual relative slope discontinuity:
**≤1.3e−09** at the VV-core joins (set by the accuracy of the numerical derivative of the VV core,
which now steps at `ε^(1/3) × the smile's own 25d-to-25d span` rather than a fixed 1e−5) and
**≤1e−13** at the tail joins.

### 10.4 Randomised stress — 2000 quote sets

Quotes drawn over ATM 3–45 vol, `|RR| ≤ 0.5·ATM`, `BF ≤ 0.25·ATM`, `BF10` deliberately allowed
*below* `BF25`, all four delta conventions, ON→2Y. "Desk" = the subset a G10 broker would actually
print (ATM ≤ 20, `|RR| ≤ 0.40·ATM`, `BF ≤ 0.12·ATM`, `1.5·BF25 ≤ BF10 ≤ 4·BF25`).

| check | all (2000) | desk (172) |
|---|---|---|
| vol floor touched anywhere in `k ∈ [−3,3]` | **0** | **0** |
| `\|dw/dk\| > 2` at `\|k\| = 8` | 1 (2.0004, finite-`k` transition) | **0** |
| negative BL density | 985 (49%) | **17 (9.9%)**, worst min/peak −0.24 |
| … minimum inside the VV core | 54 | 0 |
| … minimum in the 10d-anchored quadratic | 638 | **15** |
| … minimum in the extrapolated tail | 293 | 2 |

**The residual arbitrage is caused by the 10-delta anchor, not by the wing.** Same 172 desk-plausible
quote sets, three constructions:

| construction | negative density | worst min/peak |
|---|---|---|
| VV, 5 quotes (10d anchored) | **17 / 172 (9.9%)** | −2.4e−01 |
| VV, 3 quotes (25d only) + this wing | **0 / 172** | 0 |
| SABR (Hagan), same 5 quotes | **0 / 172** | 0 |

Insisting that an inconsistent 10-delta broker quote be repriced *exactly* buys butterfly arbitrage
between the 25d and 10d strikes. That is a property of the quote set, not of the extrapolation, and
it is why `diagnostics()` exists. Mitigation: check it, and for wing pricing fall back to SABR or
drop the 10d anchor.

---

## 11. Model limitations (read this one)

### Garman–Kohlhagen
* **Flat rate curves in v1.** `r_d`, `r_f` are single continuously-compounded zeros per currency.
  Term structure in the rate differential is not represented, so long-dated forwards and `rho` are
  approximate. A missing rate now **raises with the currency named** rather than defaulting to 0.0
  (amendment T-4) — assuming zero put the USDJPY 1Y forward about four big figures out.
* **Deterministic vol, European exercise only.** No barriers, no digitals, no American features.
* **`implied_vol` cannot recover a vol from a price at its own numerical resolution** (§4). Beyond
  ~20 standard deviations it returns `0.0` by design.

### Vanna–volga (the default)
* **It is not a model and carries no arbitrage guarantee whatsoever.** It is an
  interpolation-by-hedging-cost rule. Butterfly arbitrage is possible for large `|RR|` with small
  `BF`; calendar arbitrage is possible between tenors. Both are **measured, not prevented**.
* **The 10-delta anchor can create butterfly arbitrage** between the 25d and 10d strikes — 9.9% of
  desk-plausible quote sets in §10.4. Exact repricing of five quotes and a non-negative density are
  not always simultaneously achievable.
* **Very short tenors (< ~1W).** `d1 d2` is large and the second-order term dominates; the radicand
  can turn negative and the construction falls back to first order. That fallback is a genuine
  discontinuity in the *core* (the `d1 d2 = 0` one has been removed; this one has not — it lives
  where the quadratic in σ has no real root). Prefer SABR below 1W.
* **Long tenors (> ~2Y).** VV systematically over-prices convexity: the vega/vanna/volga hedge is
  assumed held to maturity at constant cost.
* **The wings are an extrapolation rule, not an arbitrage-free model.** C1, Lee-bounded and
  positive by construction — necessary, not sufficient.

### SABR (Hagan expansion)
* **Negative density in the low-strike wing for large `ν²T`** (> ~0.5). This is the well-documented
  Hagan arbitrage, a property of the *approximation*, not of SABR itself.
  `SABRParams.arbitrage_check` measures it; call it after every calibration.
* **`β` and `ρ` are not jointly identifiable** from one smile. `β` is fixed at 1.0 and must stay
  fixed. Any `β` fitted from a single tenor is meaningless.
* **Two free parameters against five pillars** — a ~0.15 vol point residual on a skewed smile is
  the model's capacity, not a bug (§10.2).
* **Very short expiries (< 1W)**: the `T`-order correction is negligible and the fit becomes nearly
  degenerate in `ν` vs `ρ`.
* Parameters are **not** interpolated across tenors — parameter interpolation has no arbitrage
  meaning. Total variance at fixed log-moneyness is interpolated instead.

### Interpolated / SVI
* **`enforce_calendar` silently moves market data.** It clips each slice's input total variance up
  to its predecessor's. The count and magnitude are now reported in `diagnostics()`; look at them.
  A slice that had to be moved a long way is telling you the chain is stale, not that the surface
  is fine.
* **The arbitrage constraints are soft penalties, not hard constraints.** A sufficiently
  contradictory chain will produce a fit that violates them; `diagnostics()` says so.
* Slices with < 5 usable quotes fall back to PCHIP, which fits but does not extrapolate — its wings
  are the shared C1 Lee-bounded tail, which is safe but carries no information beyond the last
  knot.

### Cross-cutting
* **Everything is one-currency-pair-at-a-time.** No correlation, no cross-gamma, no basket or
  quanto adjustment.
* **The market-strangle converter fails silently.** `market_to_smile_bf` returns `bf_market`
  unchanged if the Brent solve does not bracket. Verify with a reprice when the distinction matters.
* **Fixed-point solvers give up quietly.** `vol_by_delta` and `atm_strike` iterate to a tolerance
  and, if they do not reach it in 50–60 damped iterations, return the last iterate rather than
  raising. Convergence is 3–6 iterations for `|δ| ∈ [0.02, 0.5]` in practice, but a pathological
  smile will produce a slightly-off pillar rather than an error.
* **The synthetic provider is date-seeded**, so surface-level validation numbers move by a few
  percent run to run. Greek and delta-convention numbers are deterministic.

---

## References

* Garman, M. & Kohlhagen, S. (1983). "Foreign Currency Option Values". *J. Int. Money & Finance* 2, 231–237.
* Clark, I. (2011). *Foreign Exchange Option Pricing: A Practitioner's Guide*. Wiley. Ch. 2–4.
* Reiswich, D. & Wystup, U. (2010). "A Guide to FX Options Quoting Conventions". *J. Derivatives* 18(2). — definitive on premium-adjusted delta and its non-monotonicity.
* Castagna, A. & Mercurio, F. (2007). "The Vanna-Volga Method for Implied Volatilities". *Risk* 20(1), 106–111.
* Hagan, P., Kumar, D., Lesniewski, A. & Woodward, D. (2002). "Managing Smile Risk". *Wilmott*, Sep, 84–108.
* Obłój, J. (2008). "Fine-tune your smile: correction to Hagan et al."
* Lee, R. (2004). "The Moment Formula for Implied Volatility at Extreme Strikes". *Math. Finance* 14(3), 469–480.
* Gatheral, J. & Jacquier, A. (2014). "Arbitrage-free SVI volatility surfaces". *Quantitative Finance* 14(1), 59–71.
* Zeliade Systems (2009). "Quasi-Explicit Calibration of Gatheral's SVI model".
* Breeden, D. & Litzenberger, R. (1978). "Prices of State-Contingent Claims Implicit in Option Prices". *J. Business* 51(4).
* Jäckel, P. (2015). "Let's Be Rational". *Wilmott*, Jan.
* Fritsch, F. & Carlson, R. (1980). "Monotone Piecewise Cubic Interpolation". *SIAM J. Numer. Anal.* 17(2).
