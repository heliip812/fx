# 07 — Credit Gamma (v2 design)

**Owner:** `credit` (Credit Analyst / Quant) • **Status:** design, no code written
**Supersedes:** `docs/00_charter.md` §7 (that note is optimistic in two specific places; §3.6 and §9
below say where and why).
**Reads against:** `docs/01_architecture.md` (frozen contract + amendments v1.1, v1.2),
`fxgamma/types.py`, `fxgamma/conventions.py`, `fxgamma/models/*`, `fxgamma/data/*`.

> **The one-line version.** The credit engine is ~60% of the FX engine plus a translation layer.
> The *maths* is easy and reusable. The *data* is not there. There is no free CDS index option
> volatility surface, there never has been, and no amount of ETF cleverness manufactures one.
> What free data does support is a genuine, self-consistent gamma book **in listed credit-ETF
> options** — plus a correct CDX pricer fed by hand-typed marks, and 25 years of free spread
> history for regime context. Build that. Do not build, and do not label, "CDX gamma analytics".

---

## 1. What gamma trading means in credit, and how it differs from FX

### 1.1 The instrument set

| | FX (v1) | Credit (v2) |
|---|---|---|
| Underlying | Spot FX rate, continuously tradable | A **5y CDS index swap** — CDX IG / CDX HY (US), iTraxx Main / Crossover (EU) |
| Option | European vanilla call/put on spot | European **payer / receiver swaption** on the index |
| Payer | — | Right to **buy** protection at strike spread `K`. Bearish credit. A *call on spread*, a *put on price*. |
| Receiver | — | Right to **sell** protection at `K`. Bullish credit. |
| Expiry | Any date, NY10 / TKY15 / LDN16 cut | **Third Wednesday of the month**, standardised. Liquidity in the front 1–3 expiries. |
| Quoting | Lognormal vol on spot, ATM / 25d RR / 25d BF | **bp vol** (normal/Bachelier on spread) or **% spread vol** (lognormal) for IG/Main; **price vol** for HY |
| Strike | Spot units (1.0850) | **Spread bp** for IG/Main/Xover; **price points** for CDX HY |
| Index conventions | — | IG: 125 names, 100bp coupon, spread-quoted. HY: 100 names, 500bp coupon, **price**-quoted. Main: 125, 100bp, spread. Xover: 75, 500bp, spread. |
| Roll | none | Series rolls **20 Mar / 20 Sep**. The on-the-run underlying *changes identity* twice a year. |

### 1.2 The five structural differences that matter

**(a) The underlying is a swap, so the numeraire is a risky annuity, not a bank account.**
An index CDS has (approximately, and exactly under the ISDA standard model's flat-hazard
convention) upfront value to the protection buyer

```
U(s) = (s − c) · A(s)                     c = fixed coupon (100bp IG / 500bp HY)
                                          A = risky annuity (RPV01), decreasing in s
```

`A` is *stochastic and negatively correlated with `s`*: wider spreads mean lower survival
probability and a shorter expected life, so the annuity shrinks. Consequence:

```
dU/ds = A + (s − c)·A'        with A' < 0
d²U/ds² = 2A' + (s − c)A''  ≈ 2A' < 0  near s ≈ c
```

**A flat, unhedged index position already has convexity in spread before any option is
involved.** Long risk (protection seller, long price) is *long* spread convexity — the same sign
as a long bond. In FX, spot has exactly zero gamma by construction; that is why "delta hedge"
and "gamma neutral" are cleanly separable. In credit they are not: your hedge instrument
contributes second-order P&L, and "gamma" is only defined once you name the coordinate
(spread bp, spread %, or price).

**(b) Spread-vs-price duality is a real fork in the road, not a cosmetic one.**
CDX IG quotes in spread; CDX HY quotes in price. Vol on the same underlying can be quoted three
ways, related by the chain rule:

```
σ_bp   ≈ s · σ_lognormal-spread                  (bp vol from % spread vol)
σ_price ≈ (SpreadDV01 / Price) · σ_bp            (price vol from bp vol)
```

These are *first-order* identities. They are exact only for infinitesimal moves and a
deterministic annuity; over a 3-month option they disagree by a few percent of vol, and much
more in a stress regime. Any screen that shows a vol number must state which of the three it is.
This is the single most common source of confident wrong numbers in credit options.

**(c) Credit convexity is asymmetric, for four separate reasons.**
1. The `s → U` map is itself curved (above), so equal bp moves are not equal P&L.
2. The spread distribution has a hard floor near zero and an unbounded right tail. Gamma in bp
   space is not symmetric about the strike even for a symmetric model.
3. **Jump to default** puts a point mass far out in the right tail that no diffusion reproduces.
   Payer wings are structurally, permanently bid.
4. **Flow is one-sided.** Real money is structurally long credit and buys payers/protection as a
   hedge; dealers are structurally short those payers. The payer skew is persistently rich and
   does *not* mean-revert the way an FX 25d risk reversal does.

**(d) Front-end protection and jump risk break the "option on a forward" picture.**
A CDX index option does **not** knock out on default. If constituents default between trade and
expiry, the payer holder is compensated for those losses on exercise ("front-end protection",
FEP). Therefore a payer struck absurdly wide is never worth zero, and the correct forward is a
**loss-adjusted** forward spread, not the plain forward. (Single-name CDS options historically
*did* knock out — do not carry that convention across.) FX has no analogue whatsoever. There is
no FX event that pays the option holder a lump sum for something that happened before expiry.

**(e) Carry is first-order.** In FX, hedge carry is forward points — a small correction. In
credit, your delta hedge is a CDS index position accruing the 100bp/500bp coupon against the
market spread, plus curve roll-down. On a 1-month delta-hedged straddle in CDX HY, carry is
comparable in size to the gamma P&L you are trying to harvest. It cannot be a footnote in the
attribution; it is a headline bucket.

### 1.3 Which FX intuitions transfer, and which are actively misleading

**Transfer cleanly:**
- Implied vs realized, vol cones, richness z-scores, term-structure shape.
- The delta-hedged gamma P&L identity `∫ ½Γ(dX² − σ²dt)` — with `X` the chosen coordinate and an
  extra annuity/carry term (§5.4).
- Book → Greeks → ladder → scenario → attribution architecture, verbatim.
- The v1.2 T-1 ruling (manual vol marks are the primary input). It applies *a fortiori* in credit.
- Skew as a crash-risk signal — arguably *more* informative in credit than in FX.

**Actively misleading — carrying these across will lose money:**
- **"Fade the risk reversal."** Credit payer skew is a structural, flow-driven, permanent feature.
  Z-scoring it and selling the rich wing is a short-vol-of-vol carry trade with a fat left tail,
  not a mean-reversion trade. The FX RR reflex is wrong here.
- **"Delta-hedge with the underlying and you're gamma-clean."** The index hedge has its own
  convexity and its own carry (§1.2a, §1.2e).
- **"The 5d wing is worth ~zero."** Front-end protection plus JTD means the far payer wing has a
  floor. Lognormal-with-flat-vol will price it at zero and you will be run over.
- **"Put-call parity gives me the forward."** Credit parity is `Payer − Receiver = A·(F − K)`
  under the **annuity** measure, not `S − K·e^{−rT}`. Same shape, different numeraire; using the
  FX form gives you a forward that is wrong by the annuity.
- **"Pin risk at the big strike."** There is no CDX equivalent of the CME OI gamma map. See §3.7:
  the entire `/gamma-map` page has no free-data credit analogue for the *index*. It does have one
  for the *ETF*.
- **"Premium-adjusted delta."** Meaningless. Delete the concept, do not port `spot_pa`.
- **"The time series is continuous."** The index rolls every six months into a new basket. A
  spliced "CDX HY 5y spread" history contains roll jumps that are composition changes, not market
  moves. Realized vol computed naively across a roll is overstated.

---

## 2. Modelling

### 2.1 The pricing equation

Under the **risky annuity numeraire** (the survival measure), the exercise value of a payer at
expiry `T` on an index maturing at `M` is `A_T·(s_T − K)⁺`, so

```
Payer_0 = A(0; T, M) · E^A[ (s_T − K)⁺ ]
Recvr_0 = A(0; T, M) · E^A[ (K − s_T)⁺ ]
Payer − Recvr = A · (F* − K)                      (parity, annuity measure)
```

With a lognormal forward spread this is **Black-76 on `F*`, scaled by `A`**:

```
Payer_0 = A · [ F*·N(d1) − K·N(d2) ],   d1,2 = [ln(F*/K) ± ½σ²T] / (σ√T)
```

With a normal (bp-vol) forward spread it is **Bachelier**:

```
Payer_0 = A · [ (F* − K)·N(d) + σ_bp·√T·φ(d) ],   d = (F* − K)/(σ_bp√T)
```

**Front-end protection.** `F*` is the *loss-adjusted* forward spread (Pedersen 2003):

```
F* = F + FEP / A(0; T, M)
FEP = E[ discounted protection payments on defaults occurring before T ]
```

For CDX HY, quoted in price, the equivalent statement is Black-76 on the **forward index price**
under the ordinary discount numeraire, with the forward price adjusted downward for expected
front-end losses. Both parameterisations must exist and must agree through the price↔spread map.

**Strike convention.** IG/Main/Xover strikes are spread bp; CDX HY strikes are price points. On
exercise, a spread strike is converted to an upfront by the ISDA standard model at the strike
spread — which is why the payoff is `A_T(s_T−K)⁺` and not `(U(s_T)−U(K))⁺`; the two differ by the
convexity of `U`, and the market convention is the former. Get this backwards and your ATM is
fine and your wings are 10–20% off.

### 2.2 Reuse map — existing module → credit analogue

| Existing | Reuse | How, and what changes |
|---|---|---|
| `models/gk.py` | **~100%, unmodified** | Garman–Kohlhagen with `rd = rf = 0` **is** undiscounted Black-76 on a forward. Call `gk_price(S=F*, K, T, rd=0, rf=0, σ, cp)` and multiply by `A`. You get price, the full vectorised Greek set (`gk_greeks_array`), the implied-vol solver, `strike_from_delta` and `no_arb_bounds` for free. This is the single largest piece of leverage in the whole extension. |
| `models/smile.py` | ~85% | `log_moneyness`, `total_variance`, `risk_neutral_density`, `pchip_*`, `SmileSurfaceMixin` all carry over. `atm_convention_for` / `delta_pillar_strikes` / `rr_bf_to_vols` are FX quoting furniture — drop. |
| `models/sabr.py` | **~95%, and it is the right model** | SABR is the market standard for swaption-style products and credit index options behave like swaptions. Use **β ≈ 0 (normal)** for bp-vol quoting, β ≈ 1 for % spread vol. `calibrate_sabr(F, T, strikes, vols)` needs no change. |
| `models/interp.py` (SVI) | ~80% | SVI on log-moneyness of spread works and gives the arbitrage checks (`svi_g`). This, not vanna–volga, is the right smile builder for a credit chain. |
| `models/vanna_volga.py` | **~25%** | VV is built around the FX quoting triple (ATM / 25d RR / 25d BF) and the market-BF↔smile-BF fixed point. Credit does not quote that triple. The *three-strike interpolation kernel* is reusable in principle; the FX conventions wrapped around it are not. Do not force it. |
| `models/surface.py` | ~90% | `SmileQuotes` / `build_surface` factory is the frozen data↔model boundary (arch §8) and should be honoured exactly. Credit needs a **fourth `method`** or, cleaner, a parallel `build_credit_surface` in `fxgamma/credit/` that returns the same `VolSurface` protocol. No contract change. |
| `data/vol_etf_options.py` | **~80%** | HYG/LQD/JNK/IEF chains come from the *same Yahoo endpoint* with the *same parser*. Fork it. Changes: no inversion logic; **discrete monthly dividends** replace the foreign rate (§3.1c); wider acceptable spreads. `implied_forward()` via put-call parity is exactly right and carries over unchanged. |
| `data/rates_fred.py` | ~90% | Same fetcher, same CSV parser, new `SERIES` table pointing at ICE BofA OAS series. |
| `data/cache.py`, `provider.py`, `base.py` | **~95%** | `ChainProvider` resolution order (manual → live → cache → synthetic), `Provenance`, `SourceStatus`, `MarketDataProvider` ABC — all directly reusable. |
| `data/cme_options.py`, `signals/gex.py` | **0%** | No public OI for OTC CDX. See §3.7. |
| `data/spot_*.py` | 0% | Replaced by ETF price history + FRED OAS. |
| `portfolio/*` (risk, zones, attribution, hedging) | ~80% structurally | Same algorithms; the x-axis becomes spread and two new P&L buckets appear (§5). |
| `signals/realized.py`, `cones.py`, `richness.py` | **~90%** | Estimator maths is coordinate-agnostic. Feed it ETF prices or FRED spreads. |
| `backtest/*` | ~70% | Engine and metrics carry over; the hedge instrument and its carry are new. |
| `conventions.py` PAIRS / delta conventions | **0%** | Replaced by `CreditSpec` (§2.4). |
| Dash shell, theme, components | ~85% | Two new pages. |

**Weighted reuse estimate: ~60% of the FX engine.** New credit-specific code: **~1,500–2,000 LOC**.

### 2.3 What is genuinely new code

1. **`credit/curve.py`** — flat-hazard survival curve from a single spread (the ISDA convention),
   risky annuity `A(0;T,M)`, forward spread, and the **upfront ↔ spread** conversion in both
   directions. ~300 LOC, closed form, fully unit-testable with zero data.
2. **`credit/blackspread.py`** — Black and Bachelier on spread over the annuity numeraire (thin
   wrapper over `gk.py`), FEP-adjusted forward, and the **Greek chain rule** from forward-spread
   space into desk credit units (§5.1). ~350 LOC.
3. **`credit/transform.py`** — the σ_bp ↔ σ_%spread ↔ σ_price triangle, and the ETF-price ↔
   spread map via option-adjusted spread duration. **This is where all the error lives** (§3.6);
   it must return an error band, not just a number.
4. **`credit/conventions.py`** — `CreditSpec`, index definitions, the Mar/Sep roll calendar, the
   third-Wednesday expiry calendar. ~200 LOC.
5. **`credit/jtd.py`** — jump-to-default and idiosyncratic-gap scenarios. ~150 LOC. No FX analogue.

### 2.4 Fitting the frozen contract — no amendment required

`docs/00_charter.md` §7 says "the architecture keeps `Underlying` abstract for this reason."
**It does not.** `types.py` has `PairSpec`, and `OptionPosition.pair: str`. That is a factual
error in the charter and the PM should correct it. It is not, however, a problem:

- `Greeks`, `Book`, `OptionPosition`, `SpotPosition`, `PnLBreakdown`, `Provenance`,
  `MarketSnapshot` are all **numerically generic**. Reuse them verbatim. `OptionPosition.pair`
  holds the credit symbol (`"HYG"`, `"CDX_HY_S45"`). `SpotPosition` holds the delta hedge
  (ETF shares or index notional).
- Add `fxgamma/credit/conventions.py::CreditSpec` **beside** `PairSpec`, not instead of it.
- **Do not rename `Greeks` fields.** They are FX-flavoured (`delta_base`, `gamma_1pct`) but the
  dataclass is frozen and its `__add__`/`__mul__` semantics are load-bearing. Put the credit
  units in a presentation adapter (`credit/units.py`) that maps `Greeks` → labelled credit
  quantities. Renaming would reopen a frozen contract for a cosmetic gain.
- The one field that needs care: `MarketSnapshot.meta` key grammar (CG-7) is `spot.<PAIR>` etc.
  Credit needs `spread.<INDEX>`, `surface.<SYMBOL>`, `oasd.<ETF>`. That **is** a contract
  extension — small, additive, and worth asking the PM for explicitly rather than squatting on
  `spot.HYG`.

### 2.5 The `cp` sign trap — write this on the wall

`cp = +1` means *call*. In credit that is ambiguous, because payer = call on **spread** = put on
**price**.

> **Ruling:** `CreditSpec.quote_space ∈ {"spread", "price"}` is mandatory and `cp` is always
> interpreted in that space. For `CDX_IG` (`quote_space="spread"`), payer → `cp=+1`. For `HYG`
> (`quote_space="price"`), a listed call → `cp=+1`, which is a *bullish credit* position.
> A book mixing both without the flag will report the sign of its credit direction backwards.

This is the single most likely catastrophic bug in the extension. It should have a dedicated test.

---

## 3. The free-data reality, source by source

Every source below is tagged with a confidence and a `VERIFY` flag where I could not check it
from this sandbox (egress to market-data hosts is blocked — charter §3). Follow the existing
`scripts/verify_live_sources.py` pattern: assume `VERIFIED = False` until the user confirms from
their own machine.

### 3.1 Listed credit-ETF option chains — HYG, LQD, JNK, IEF

**What it is.** Free, listed, exchange-quoted options with real two-sided markets, real implied
vols, real open interest, weekly and monthly expiries. Same Yahoo endpoint the FX side already
parses (`data/vol_etf_options.py`). HYG is the deepest credit-ETF option market that exists.
**This is not a proxy for a credit option. It *is* a credit option — just on a different
underlying than CDX.** That distinction is the whole design.

**What bites:**

**(a) It is an option on a bond-portfolio price, and that price has two drivers.**

```
dP/P ≈ − D_rate · dy − D_spread · ds  + (basket/liquidity noise)
```

Order-of-magnitude desk arithmetic (not fitted — replace with regression in P3):

| | eff. duration | OAS duration | typical OAS | daily σ(driver) | price σ from rates | price σ from spread | credit share of variance |
|---|---|---|---|---|---|---|---|
| **HYG** | ~3.2y | ~3.3y | ~300bp | y: 5bp, s: 8–10bp | ~16bp/day | ~26–33bp/day | **~65–75%** |
| **LQD** | ~8.4y | ~7.2y | ~100bp | y: 5bp, s: 2bp | ~42bp/day | ~14bp/day | **~10–15%** |

> **The most important sentence in this document:** *LQD implied vol is a rates vol. HYG implied
> vol is mostly a credit vol.* Using LQD as a free proxy for CDX IG spread vol is not
> approximately right — it is dominated by the wrong risk factor. **HY half-works. IG does not.**

**(b) Stripping rates is possible but introduces a correlation assumption you cannot observe.**
IEF and TLT have free, liquid option chains, so the rate leg is hedgeable with free listed
instruments — genuinely useful. But inverting for spread vol requires ρ(spread, rate), which is
typically −0.3 to −0.6, is regime-dependent, and flips sign in inflation shocks:

```
σ²_P = (D_r σ_y)² + (D_s σ_s)² − 2ρ D_r D_s σ_y σ_s
```

Solving for `σ_s` given implied `σ_P` and implied `σ_y` (from IEF options) is well-posed for HYG
and near-degenerate for LQD.

**(c) Discrete monthly dividends and American exercise.** HYG yields ~6–7% paid **monthly**;
LQD ~4–5%. That is not a continuous `q`. Misspecifying it by 50bp of yield on a 3M option moves
the implied forward by ~1 cent on an $80 ETF and biases measured skew by roughly 0.2–0.4 vol
points asymmetrically. Early exercise of ITM calls around ex-dates is economically real, not
theoretical. **Mitigation already exists in the codebase:** `vol_etf_options.implied_forward()`
recovers the forward from put–call parity instead of assuming one. Keep that; it is the correct
answer and it carries over unchanged. Restrict smile construction to OTM contracts, as the FX
module already does.

**(d) NAV basis and ETF liquidity vol.** HYG trades at a premium/discount to NAV of roughly
±0.1–0.3% normally, which blew to ≈ −5% in March 2020. The ETF price *leads* stale bond marks in
stress — which makes it a *better* real-time credit signal than NAV, but adds a liquidity
volatility component that CDX does not have. Your HYG implied vol contains a term that is about
the ETF's own plumbing.

**(e) Chain quality.** HYG strikes are $1 apart (~1.2% of spot), so a genuine 10-delta wing often
does not exist for short expiries; quotes are stale outside 09:30–16:00 ET; OI concentrates in
monthlies. Expect a usable smile of 6–15 OTM strikes per expiry for HYG, fewer for JNK.
**Decision point:** if fewer than ~8 two-sided OTM strikes survive filtering, you have an ATM
level and a crude skew, not a surface — and SVI/SABR calibration is over-parameterised.

**Confidence: HIGH that the data exists and parses (the FX module proves the path). HIGH that
HYG IV is a usable credit-vol signal. LOW that it maps to CDX IG.** `VERIFY`: Yahoo crumb
requirement, per-symbol chain depth.

### 3.2 FRED — ICE BofA OAS series

**What it gives.** Daily option-adjusted spreads, free, redistributable, back to ~1996–97:
`BAMLH0A0HYM2` (US HY OAS), `BAMLC0A0CM` (US IG OAS), `BAMLH0A3HYC` (CCC), `BAMLC0A4CBBB` (BBB),
`BAMLEMCBPIOAS` (EM), plus matching effective-yield series. **This is the highest-quality free
credit data that exists**, and it is the one place where credit is genuinely *better* served than
FX: 25+ years of clean daily history for realized-vol cones, percentile/regime mapping and
crisis-analogue work.

**What bites.** (i) It is a **cash bond index OAS**, not a CDX spread. Daily *changes* correlate
with CDX HY at roughly 0.9+, but the *level* differs by the cash-CDS basis, which has historically
run anywhere from −50bp to +100bp and moves most in exactly the stress you care about.
(ii) Published T+1, no intraday. (iii) Index composition drifts (rating migration, new issue).
(iv) FRED does **not** publish spread duration — you need it for every translation and must get
it elsewhere (§3.4).

**Confidence: HIGH.** Reuse `rates_fred.py` wholesale.

### 3.3 Cboe Credit VIX (VIXIG / VIXHY / VIXIE / VIXXO) — **verify this first**

Cboe publishes credit volatility indices computed from **actual CDS index option** markets
(CDX IG, CDX HY, iTraxx Main, iTraxx Crossover), launched in 2022 with backfilled history.
If Cboe's index-value download is free for these the way it is for other Cboe indices, the user
gets a **real implied credit vol level on the real instrument** — no proxy, no ETF, no rates
contamination.

**What it would give:** an ATM-ish implied bp-vol level per index, daily, with ~10 years of
history. Enough for implied-vs-realized, cones, richness z-scores and regime work on *CDX itself*.
**What it would not give:** any strike dimension (no skew), any surface, any term structure beyond
the index's fixed ~3M horizon, and — importantly — it is a **strike-integrated fair-variance
construction**, so it sits *above* true ATM by the skew premium. You cannot calibrate a smile to
it and you cannot price an option with it. You *can* anchor the level and calibrate your
HYG→CDX mapping against it, which is exactly the missing calibration in §3.6.

**Confidence: MEDIUM that free download works; HIGH on value if it does.** `VERIFY` — this is a
one-hour check with the highest information value in the entire project. It moves the
recommendation in §9 from "build the ETF desk" to "build the ETF desk **plus** a real CDX vol
richness screen."

### 3.4 ETF issuer data — iShares / SSGA fund pages and holdings files

Free daily: effective duration, option-adjusted spread, yield-to-worst, holdings count, and the
**full daily holdings CSV** for HYG / LQD / JNK. This is the *only* free source of the spread
duration every translation in §2.3.3 depends on, and it is good — the issuer's own number,
updated daily, no modelling on your part. The holdings file additionally lets you compute your
own portfolio spread duration and sanity-check composition drift.

**What bites.** Personal-use terms; page structure changes; the published OAS/duration are
end-of-prior-day. **Confidence: HIGH.** `VERIFY` endpoint stability.

### 3.5 The sources that do not carry their weight

| Source | Verdict |
|---|---|
| **TRACE (FINRA)** | Free access is a delayed, rate-limited **web query interface**; bulk historical files are paid. Building your own bond index from it is a multi-month project with a fragile ingest. **Useful for spot-checking one bond, not as an automated source.** Do not put it on the critical path. |
| **Markit/S&P index annexes & factsheets** | Composition and rules are free (registration). Daily composite **levels are not**. You can know exactly what is in CDX HY S45 and not what it closed at. |
| **ICE Clear Credit EOD settlement prices** | ICE publishes end-of-day marks for cleared CDS instruments. If freely downloadable this solves §3.6's "you can't even mark the underlying" problem. **Licence terms need reading before you build on it.** `VERIFY`. Confidence: MEDIUM. |
| **DTCC Trade Information Warehouse** | Free **weekly** gross/net notional outstanding by index/series. A crude participation gauge — the closest free thing to positioning. Weekly and lagged, so it is context, never a signal. `VERIFY`. |
| **OCC / Yahoo open interest for ETF options** | Per-contract OI already comes back in the Yahoo chain payload, and `vol_etf_options.open_interest()` already consumes it. This **does** resurrect a gamma-map page — for HYG, not for CDX. Dealer sign is unknown, exactly as in FX. |
| **A free credit vol surface of any kind** | **Does not exist.** Not partially, not with effort, not behind a registration wall. |

### 3.6 The ETF-option → CDX-option mapping problem, quantified

This is the section the charter's §7 is too breezy about. The claim there is that ETF chains are
"mapped to spread space via option-adjusted duration." That mapping exists. Here is what it costs.

Take HYG: `OASD ≈ 3.3`, `s ≈ 300bp = 0.03`, `P ≈ $80`. The credit contribution to price vol from
a lognormal spread vol `σ_ln` is

```
σ_P(credit) = OASD · s · σ_ln = 3.3 × 0.03 × σ_ln ≈ 0.099 · σ_ln
```

Worked example. With `σ_ln = 70%` (a normal HY regime) the credit part is 6.9% price vol; the
rate part is `D_r·σ_y ≈ 3.2 × 0.79% ≈ 2.5%`; combining at `ρ = −0.4` gives ≈ 6.3%, plus basket
and liquidity noise → ≈ 7–8%. That is squarely in the observed range for HYG 1M ATM in calm
markets, so **the forward map is sound.**

**The inversion is where it dies.** Because `OASD · s ≈ 0.10`, going backwards multiplies every
error by ~10×:

> **A 1-point error in HYG implied vol (7% vs 8%) becomes a ~10-point error in implied spread vol
> (70% vs 80%).**

And that is *before* the rate-strip error, the correlation assumption, the cash-CDS basis, the
duration mismatch (HYG OASD ~3.3 vs CDX HY risky annuity ~4.0–4.3, a ~25% gap), the ETF liquidity
premium and the composition difference (HYG holds ~1,200 bonds; CDX HY is 100 CDS names,
substantially higher quality on average). Compounding those:

| Quantity from free data | Honest quality |
|---|---|
| HYG implied vol **level** | Good. It is the traded instrument. |
| HYG implied vol **z-score / richness** | Good. Errors are largely common-mode and cancel. |
| Implied **spread** vol **level** for HY | Poor. ±20–30% relative, wider in stress. Not tradable information. |
| Implied **spread** vol **changes / z-scores** | Fair. Usable as a signal, not as a mark. |
| Implied spread vol for **IG** (via LQD) | Unusable. Wrong dominant factor (§3.1a). |
| CDX **skew** | Not obtainable. HYG skew ≠ CDX payer skew; direction correlates, magnitude does not. |
| iTraxx / European anything | Not obtainable. No listed EU credit-ETF option market of usable depth. **Europe is out of scope.** |

**This is the same conclusion the trader forced on the FX side in amendment v1.2 (T-1), only
harder.** There, ETF vol was demoted from "the mark" to "z-scores, cones and richness." In
credit, the demotion is not a refinement — it is the entire boundary of what is possible.

### 3.7 What has no credit analogue at all

The `/gamma-map` page (CME open interest → market gamma by strike → pin zones) is one of v1's
best ideas and it **does not port to CDX**. CDX options are OTC and bilaterally cleared; there is
no public strike-level open interest, ever. A HYG-options gamma map is buildable from the Yahoo
chain and is a legitimate — if smaller and noisier — cousin. Say so on the page. Do not imply it
shows anything about CDX positioning.

---

## 4. What can honestly be built vs what cannot

### 4.1 CAN build, on free data, and defend to a trader

1. **Credit-ETF vol surfaces** — HYG / LQD / JNK / IEF chains → `SmileQuotes` → `build_surface`.
   Real IVs. Reuses the existing path end to end.
2. **Realized vol, multi-estimator, on ETF prices** — `signals/realized.py` unchanged.
3. **Realized *spread* vol and 25-year spread cones from FRED OAS.** Unambiguously free,
   unambiguously good, and better history than anything on the FX side.
4. **IV–RV richness on HYG**, with z-scores and cones. The single best-supported analytic in the
   whole credit extension.
5. **Skew as a crash-risk signal** — HYG 25d put/call IV spread, and HYG skew vs SPY skew. Real,
   and one of the better free credit-stress indicators.
6. **Rate-stripped credit vol** — regress HYG on IEF (or use published durations), publish the
   residual credit vol **with an explicit error band** derived from §3.6, never a bare number.
7. **Spread-duration-scaled gamma** — translate ETF gamma into $ per bp² of spread. Internally
   consistent; badge the translation.
8. **Equity–credit vol relative value** — VIX vs HYG IV; Merton-style implied spread from equity
   vol vs actual OAS; residual as a rich/cheap signal. Entirely free-data, well documented.
9. **A fully correct CDX index option pricer driven by manual vol marks.** The v1.2 T-1 pattern:
   the user types the bp vol from their broker run, and the engine does annuity, FEP, Greeks,
   ladder, attribution properly. ~2 days. This is the highest value-per-day item on the list.
10. **A HYG gamma map by strike** from chain OI, with the dealer-sign caveat stated on the page.
11. **Delta-hedged straddle backtest on HYG**, using free daily OHLC — *forward from the day you
    start recording chains* (see 4.2.4).

### 4.2 CANNOT build on free data — say so plainly on the screen

1. **A CDX or iTraxx option surface.** No strikes, no expiries, no bid/ask. Full stop.
2. **Any history of CDX option prices** → no cone, no IV-RV history, no richness z-score and no
   backtest on the real instrument. (Unless §3.3 verifies, which gives a *level* series only.)
3. **Official daily CDX/iTraxx composite index levels**, free and reliably. Without them you
   cannot mark the underlying of the option you just priced. `VERIFY` §3.5's ICE line first.
4. **Historical listed option chains for HYG.** Yahoo serves *today's* chain only. This is a hard
   and badly underrated blocker: you can build every screen and still be unable to backtest any of
   them for a year. **Mitigation, and it must be day 1:** ship a daily chain snapshotter into the
   cache before anything else. Every day of delay is a permanently lost day of history.
5. **Dealer positioning / market gamma for CDX.** §3.7.
6. **Single-name CDS curves**, index-vs-intrinsic skew, per-name JTD decomposition.
7. **iTraxx Main / Crossover analytics of any kind.** No proxy of usable depth exists. Cut Europe.
8. **Intraday anything.** FRED is T+1; issuer analytics are prior close; the ETF chain is the only
   intraday object and it is stale outside RTH.

---

## 5. The position and risk view for a credit gamma book

### 5.1 Greeks in credit units

Computed in forward-spread space by `gk_greeks_array` (with `rd=rf=0`, scaled by `A`), then
chain-ruled into desk units by `credit/units.py`. `Greeks` itself is untouched.

| Credit unit | Definition | FX analogue | Status |
|---|---|---|---|
| **CS01 / spread DV01** | $ P&L per **+1bp** parallel spread widening | `delta_base` | renamed only |
| **Gamma per bp** | `d(CS01)/d(spread)`, $ per bp² | `gamma` | renamed only |
| **`gamma_10bp`** | change in CS01 for a +10bp widening | `gamma_1pct` | new desk unit |
| **`gamma_1pct_spread`** | change in CS01 for a +1% *relative* spread move | — | **new, and preferred** for cross-index comparison: IG at 60bp and HY at 350bp are not comparable in bp |
| **Vega (bp-vol)** | $ per 1bp of annualised bp vol | `vega` | must be labelled; two vega units coexist |
| **Vega (%-vol)** | $ per 1 point of lognormal spread vol | `vega` | ditto |
| **Theta** | $ per calendar day | `theta` | identical |
| **IR01** | $ per 1bp parallel rate move | `rho_d` | **promoted from rounding error to first-order** — mandatory for ETF-based books |
| **JTD** | $ P&L if one average constituent defaults | none | **genuinely new**; for ETF proxies, an idiosyncratic gap scenario |
| **Annuity risk** | $ per 1% change in `A` at fixed spread | none | second-order, but it is why your hedge is not clean |

### 5.2 The spread ladder replaces the spot ladder

Same function, same shape, three real changes:

- **X-axis in spread bp**, not spot.
- **Log-spaced and asymmetric.** Spreads are lognormal-ish with a floor near zero and an
  unbounded tail. A symmetric ±5% grid is an FX artefact and hides exactly the region you care
  about. Suggested default: `−40%` to `+150%` of current spread, log-spaced, 101 points. For HY
  that is roughly 180bp → 750bp from a 300bp start.
- **Dual axis.** Render spread on the primary axis and the corresponding index **price** on a
  secondary axis, computed through `credit/curve.py`. The two are not linearly related and the
  trader will want both.
- `sticky ∈ {"strike","delta","none"}` carries over unchanged, plus a credit-specific
  `sticky="bpvol"` (hold bp vol constant as spread moves, i.e. lognormal vol falls) which is much
  closer to how credit vol actually behaves than sticky-strike.

### 5.3 Gamma zones in spread space

`portfolio/zones.py` clusters unchanged; only the coordinate changes. Semantics differ:

- **Pin risk is weaker.** There is no NY-cut fixing with concentrated barrier flow. There *is*
  clustering at round-number strikes into the third-Wednesday expiry, and there is a genuine
  structural date the FX side has no equivalent of: the **20 Mar / 20 Sep roll**, which changes
  the underlying's identity. Both belong on the zone chart.
- **Zone labels flip meaning.** "Short gamma / pin risk" in FX becomes "short gamma into a
  widening tail" in credit, which is a materially worse place to be because of §1.2c/d. The label
  set should be credit-specific.

### 5.4 Daily P&L attribution

`PnLBreakdown` is reused **verbatim** — the fields already exist. Two change meaning and become
headline items rather than footnotes:

| Bucket | Change |
|---|---|
| `delta` | now CS01 × Δspread |
| `gamma` | now ½ × Γ_s × Δspread² |
| `vega`, `vanna`, `volga` | identical structure; state which vol unit |
| `theta` | identical |
| **`rates`** | **promoted.** For ETF books this is a top-three bucket, not a residual. |
| **`carry`** | **promoted.** Index coupon accrual + curve roll-down. On a 1M delta-hedged CDX HY straddle, comparable in size to the gamma P&L. |
| `hedge` | now the CDS index or ETF hedge, *including its own convexity* (§1.2a) — a genuine `unexplained` contributor if you model the hedge as linear |
| `unexplained` | add a **JTD/gap** line before this, so a default event is attributed rather than dumped into residual |

### 5.5 What stays literally identical

The provenance/badging policy (arch §7), the manual-mark-wins resolution order (v1.2 T-1), the
`Greeks.__add__` non-additivity rule (T-2), the expiry-death rule (T-3), the raise-on-missing-input
rule (T-4), the CSV import path, the SQLite store, the hedge log, the scenario grid, the Dash
shell and theme. None of these need a single line changed for credit. That is the payoff for the
architecture being coordinate-agnostic.

---

## 6. Phased build plan

| Phase | Work | Effort | Gate |
|---|---|---|---|
| **P0** | **Verification spike**, run by the user on their own machine: Cboe Credit VIX (§3.3), HYG/LQD/JNK/IEF chain depth, FRED OAS series, iShares duration/holdings, ICE Clear EOD, DTCC weekly. Extend `scripts/verify_live_sources.py`. | **0.5d** | **DP-1: does Cboe Credit VIX download free?** Yes → add a real CDX vol richness screen and P5 grows. No → ETF-only, and say so on every screen. |
| **P0.5** | **Ship the daily chain snapshotter immediately**, before any analytics. Every day of delay is a permanently lost day of option history (§4.2.4). | **0.5d** | none — do it regardless |
| **P1** | `fxgamma/credit/`: `conventions.py` (CreditSpec, roll + third-Wed calendars), `curve.py` (flat-hazard survival, risky annuity, upfront↔spread), `blackspread.py` (Black/Bachelier over `gk.py`, FEP-adjusted forward, Greek chain rule). **Zero data dependency; fully testable against closed form and finite differences.** | **2–3d** | Greeks match FD to <1e-4 (charter §6 standard) |
| **P2** | Data: `credit_etf_options.py` (fork of `vol_etf_options.py` — dividends, no inversion), `spreads_fred.py` (OAS `SERIES` table), `etf_analytics.py` (issuer durations/OAS). Synthetic credit provider for CI. | **1–2d** | **DP-2: does the HYG chain yield ≥8 two-sided OTM strikes per expiry?** No → ATM + crude skew only; drop SVI/SABR calibration to a 3-point smile. |
| **P3** | Signals: rate-strip regression with error band, IV–RV richness, FRED spread cones, skew crash signal, equity–credit RV. | **2d** | **DP-3: is the rate-stripped credit vol defensible, or is the residual noise?** Test: does the HYG-implied spread vol correlate with VIXHY (if P0 succeeded) or with realized OAS vol at >0.6? Below that, ship it as a diagnostic, not a signal, and badge it. |
| **P4** | Risk views: spread ladder, credit gamma zones, credit attribution, `credit/units.py`. Two Dash pages: `/credit` (monitor) and `/credit-risk`. | **1–2d** | trader review |
| **P5** | Manual-mark CDX pricer wired to P1 — paste a bp vol, get a fully correct payer/receiver with Greeks, ladder and attribution. | **1d** | trader review |
| | **Total** | **8–11 working days** | ≈ 40% of the FX build, because ≈ 60% of the engine is reused |

**Ordering rationale.** P1 before P2 deliberately: the credit maths is the part with no data risk,
it is verifiable against closed form, and it is what makes P5 possible. If the data verification
in P0 comes back badly, P1 + P5 still ship a genuinely useful calculator. If you build the data
layer first and it disappoints, you have nothing.

---

## 7. Recommendation

**Build it — but build half of what charter §7 implies, and relabel the other half.**

Three concrete rulings:

**1. Do not build "CDX gamma analytics from free data." It does not work.**
The HYG→CDX chain compounds rates contamination, an unobservable correlation, a ~25% duration
mismatch, an unstable cash-CDS basis and an ETF liquidity premium, and then the inversion
multiplies every error by ~10× (§3.6). Any *level* it produces is not tradable information.
Marking a CDX book off it would be wrong by more than the bid-offer. For CDX IG via LQD it is
worse than useless — it is measuring the wrong risk factor and calling it credit.

**2. Do build "credit-ETF gamma analytics." That is a real business, not a consolation prize.**
HYG, LQD and JNK options are themselves listed, liquid, tradable instruments with free, complete
data. A gamma book *in HYG options* can be supported at exactly the fidelity the FX side gets:
real surface, real skew, real OI, real IV-RV, real gamma map, real attribution. That is not a
proxy for anything — it is the instrument. Framed that way, credit v2 is honest and useful.
Framed as "CDX analytics," it is a story that collapses the first time the user compares a number
to a broker run.

**3. Do build the manual-mark CDX pricer.** ~1 day on top of P1, mathematically exact, and it
turns "no free surface" from a dead end into a workflow: the user brings the vol, the engine
brings the annuity, the FEP adjustment, the Greeks and the risk view. This is the same ruling the
trader already forced in v1.2 T-1, and it is even more clearly right here.

**Is FX well served by public data and credit not? Yes — and the asymmetry is structural, not a
research gap.** FX has listed ETF chains on the actual pairs, CME settlements with strike-level
OI, free rates and decades of clean spot. Credit's traded options are OTC, bilateral and
unpublished. No amount of cleverness closes that; it is an absence of a public market, not an
absence of effort. The correct posture is to be maximally rigorous about the instrument you can
see (HYG) rather than maximally optimistic about the one you cannot (CDX).

**The one thing that could change this materially** is §3.3. If Cboe Credit VIX downloads free,
the user gains a real implied bp-vol level on real CDX/iTraxx option markets with ~10 years of
history — no skew and no surface, but enough for implied-vs-realized, cones, richness and regime
work on the actual instrument, *and* enough to calibrate the ETF→index mapping instead of
assuming it. That single check is worth an hour and roughly doubles the value of the extension.
**Run it before writing any credit code.**

---

## 8. Requests to the PM

1. **Charter §7 correction.** It states "the architecture keeps `Underlying` abstract for this
   reason." `types.py` has no such abstraction (§2.4). No code change needed — the frozen types
   are numerically generic and credit reuses them as-is — but the charter sentence is false and
   should not be relied on by another agent.
2. **CG-7 meta key grammar — additive extension requested.** Credit needs `spread.<INDEX>`,
   `surface.<SYMBOL>`, `oasd.<ETF>`, `vol_index.<NAME>`. Additive only; no existing key changes.
   Requesting explicitly rather than squatting on `spot.HYG`.
3. **Scope ruling requested: cut iTraxx / Europe from v2 entirely.** No free proxy of usable depth
   exists (§3.6). Carrying it in scope guarantees a screen that cannot be populated.
4. **Naming ruling requested.** The pages and docs should say **"Credit ETF Gamma"**, not "Credit
   / CDX Gamma," everywhere the underlying is HYG/LQD/JNK. §7 ruling 2 depends on this.
5. **P0.5 (the chain snapshotter) should start before v2 is scheduled**, ideally this week. It is
   half a day of `data`-owned work and it is the only item where delay causes permanent,
   unrecoverable data loss (§4.2.4).

## 9. Where this document supersedes charter §7

| Charter §7 says | This document says |
|---|---|
| "CDX/iTraxx payer/receiver convexity **proxied via** HYG/LQD/JNK" | The proxy is not accurate enough for CDX *levels* (§3.6). Reframe: the ETF options are the instrument, not the proxy. |
| "mapped to spread space via option-adjusted duration" | The map works forwards and amplifies error ~10× backwards. Ship it with an error band or not at all. |
| "The same pricing/Greeks/portfolio engine is reused — only the underlying transform differs." | Broadly true and the best sentence in §7. ~60% reuse. But the annuity numeraire, the FEP adjustment and JTD are genuinely new, not a transform (§2.3). |
| "the architecture keeps `Underlying` abstract" | It does not. Not a problem (§2.4), but not true. |
| iTraxx in scope | Out. No free European credit-ETF option market. |

## References

- Pedersen, C. (2003), *Valuation of Portfolio Credit Default Swaptions*, Lehman Brothers QCR.
  (Front-end protection, the no-armageddon measure, the loss-adjusted forward.)
- Morini, M. and Brigo, D. (2011), "No-Armageddon Measure for Arbitrage-Free Pricing of Index
  Options in a Credit Crisis", *Mathematical Finance* 21(4).
- Rutkowski, M. and Armstrong, A. (2009), "Valuation of Credit Default Swaptions and Credit
  Default Index Swaptions", *IJTAF* 12(7).
- O'Kane, D. (2008), *Modelling Single-name and Multi-name Credit Derivatives*, Wiley.
  Ch. 6 (index conventions), Ch. 8 (index swaptions), Ch. 4 (the ISDA standard model conversion).
- Markit/S&P, *CDX and iTraxx Index Rules and Annexes* (free, registration).
- ISDA, *Standard CDS Model* documentation (upfront ↔ spread conversion).
- Existing repo: `fxgamma/models/gk.py` module docstring (desk Greek units — the credit units in
  §5.1 are defined to be consistent with it), `docs/01_architecture.md` amendments v1.1 / v1.2.
