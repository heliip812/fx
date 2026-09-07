# Trader Review — Requirements Stage

**Owner:** Trader (reviewer, `trader`) · **Stage:** M6a, pre-build review of `docs/02_requirements.md`
(71 stories) against `00_charter.md`, `01_architecture.md` + AMENDMENT v1.1, `fxgamma/types.py`,
`fxgamma/conventions.py`.
**A second pass on the built app comes later.** Nothing here is a code change; everything is a
demand on the spec or a contract-change request for the PM.

I have run a G10 gamma book. I am reviewing this the way I would review a tool someone built for
me: what would change a trading decision, and what would get someone hurt. I am blunt on purpose.
Where I say "market standard" I mean the interbank G10 convention, not my personal taste; where
something is genuinely my taste I have said so and pushed it to §9 for the real user to confirm.

---

## 1. Verdict

1. **The economics are right, and that is rarer than it sounds.** The §0 breakeven identity
   `BE% = sqrt(|θ| / (0.005·Γ₁·S))` is algebraically correct and does reduce to `σ_ATM/√365` — I
   checked it. `gamma_1pct` as the desk unit, sigma-days, gamma/theta, vanna and volga as
   first-class P&L lines, sticky-strike vs sticky-delta as an explicit toggle, attribution residual
   policing with named offenders, soft delete so last week's P&L doesn't move: this is written by
   someone who has watched a book, not by someone who has read a textbook. The premium-adjusted
   delta risk (R-8), the ETF basis risk (R-6) and the OI-is-not-positioning risk (R-14) are all
   already on the register. Good.

2. **The provenance discipline is the best feature in the document.** REQ-002 / §5.2 / arch §7 —
   every number badged, staleness graded, never silently substitute synthetic for live. That is
   exactly the thing that decides whether a trader keeps a tool open after week two. Do not let
   anyone value-engineer it out.

3. **The trade-capture layer is desk-real.** The strike grammar (§3.3), the four premium units, the
   V-1…V-21 validation table, the CSV preview gate, and the USDJPY RR example with a negative
   premium on the short leg are all correct and are the sort of detail that normally gets discovered
   the hard way six weeks in.

4. **But: the whole thing is calibrated to a mark it does not have, and that is the single biggest
   thing that would stop me using it.** The charter promises "is gamma cheap or expensive" while
   forbidding paid data, and there is no free OTC FX vol surface — R-3 and R-6 admit this. Everything
   downstream (PV, delta, hedge size, P&L, attribution, backtest) is then computed off ETF-implied
   vols that are not my mark and cannot be reconciled to my broker. As specified, **manual quote
   entry is a fallback buried in REQ-068 "overrides".** It has to be the *primary* morning input: a
   30-second ATM / 25d RR / 25d BF grid per pair per tenor that I paste in, after which the app is
   marked to my curve and the ETF data becomes what it should always have been — a z-score input.
   Until that exists I will not put a hedge on off this screen, and if I will not hedge off it, I
   will not open it.

5. **And: there are five places where the spec will print a plausible number that is wrong.** In
   descending order of damage: the pin-risk "delta discontinuity" formula in REQ-045 is the
   *inherited-if-ITM* delta, not the discontinuity, and is sign-wrong for puts read as a
   discontinuity (§5.3); `conventions.year_fraction`'s one-hour floor means an expired option never
   dies and keeps showing gamma forever (§5.3); the REQ-046 decay identity `0.5·Γ$·(σ_r²−σ_i²)·Δt`
   is out by a factor of 100 given the spec's own definition of `Γ$` (§3a); `Greeks.__add__` sums
   `delta_pct` and `dual_delta`, which are per-unit / per-strike quantities that must not be added,
   so every book-level card built from them is garbage (§3a); and `MarketSnapshot.rd_rf` silently
   defaults a missing rate to `0.0`, which violates arch §7 and puts the USDJPY 1Y forward four big
   figures out of place (§3a). None of these are hard to fix now. All of them are expensive to
   discover live.

---

## 2. Answers to the BA's ten open questions (§9)

Format: **market standard** → **ship this default** → **confirm?**

Legend: **[STANDARD]** = the market answer, ship it, do not bother the user.
**[CONFIRM]** = genuinely trader-specific, must not be assumed.

---

### Q-1 — Trading-day boundary and mark cadence

**Market standard.** The FX day rolls at **17:00 America/New_York**. That is the value-date roll,
the standard EOD mark time for an FX options book, and the boundary every prime broker and every
risk system uses. It is *not* London close, and it is not midnight anywhere.

The wrinkle that matters more than the boundary itself: a London-based gamma trader marks
**twice** — an official EOD at 17:00 NY, and a working mark around **07:00–07:15 London** when
they arrive. The morning mark is not a day: it is the ~14 hours since the official close. If the
tool computes the morning P&L and charges a full day of theta against it, the theta bar is wrong
by roughly 40% every single morning.

**Ship this default.**
- Official day boundary: `17:00 America/New_York`, printed on the P&L page (REQ-051 already does).
- Store **two** snapshots a day: `EOD` (17:00 NY, the official mark, immutable) and `AM`
  (user-triggered morning working mark).
- Every P&L panel states the **elapsed wall-clock** between the two snapshots and computes theta
  over *that elapsed time*, not over an integer number of days. Add this to REQ-051's acceptance
  criteria — it is not there and it must be.
- The spot leg of the mark comes from the 17:00 NY close; the vol leg is conventionally struck at
  **London close (16:00 London)** because that is when the OTC vol market marks. Allow the two to
  differ and label them separately.

**[CONFIRM]** — the user's timezone (the spec's `Europe/London` default is a guess), whether their
official P&L is struck at NY 17:00 or at some internal firm cut, and whether they want the AM mark
persisted as a snapshot or treated as scratch.

---

### Q-2 — Hedging style and a real band size

**Market standard.** G10 gamma desks hedge on a **delta band with a level overlay**. Fixed-time
hedging (once a day at the fix) is a systematic-book convention, not a gamma-book one; gamma budgets
are a risk-management overlay, not a hedging rule. In practice: you set a band, you tighten it into
events and into the cut, and you override it discretionarily at levels you care about (round
numbers, option strikes, the previous day's range extremes).

**Concrete numbers for the reference trade — EURUSD 1M ATM straddle, EUR 10mm per leg,
S = 1.0850, σ = 7.05%:**

| Quantity | Value |
|---|---|
| Γ₁ (both legs) | **EUR 3.95mm per 1%** (EUR 0.395mm per 10 pips) |
| Straddle premium | ~175 pips = **1.75% of EUR notional** ≈ USD 190k |
| θ (calendar) | ~~USD 5,800/day, USD 17,400 Fri→Mon~~ **CORRECTED (PM, amendment v1.4): USD ~2,870/day, ~USD 8,600 Fri→Mon.** Pricing this very trade gives θ = -2,868 and Γ₁ = 3.914mm; the table's own Γ₁ and 40-pip breakeven imply 2,915, not 5,800. Likely a double-count of the two legs. |
| Daily breakeven | **σ/√365 = 0.369% = 40 pips** |
| Typical trading-day move at that vol | σ/√252 = 0.444% = **48 pips** |

A band of "0.25–0.5 of a daily sigma of delta drift" is the working rule. On this trade that is a
delta band of roughly **EUR ±1mm to ±2mm** — i.e. **10–20% of one leg's notional**, equivalently a
spot move of roughly **25–50 pips**. In clip terms: you hedge in 1mm and 2mm clips, not in 100k.

**Ship this default:** `HedgeRule(mode="band")` with the band expressed as **% of the pair's
gross option notional**, default **15%**, and a floor of the pair's minimum sensible clip
(EUR 1mm / USD 1mm / equivalent).

**This exposes a bug in the frozen contract.** `types.HedgeRule.band_pct = 0.25` is documented as
"rehedge when |delta| drifts this % of notional". 0.25% of EUR 10mm is EUR 25,000 — that triggers a
rehedge on a **0.6-pip** spot move. It is roughly **60× too tight** and would generate hundreds of
hedges a day and a cost line that swamps the whole P&L. Either the default is wrong or the docstring
is wrong (someone meant 0.25 = 25%). Fix the default *and* make the units unambiguous in the field
name (`band_pct_of_notional`). **Contract change request CR-1, §8.**

Similarly `HedgeRule.cost_bp = 0.2` as a single global number: 0.2bp round-trip is about right for
EURUSD in interbank size, roughly 2× too tight for AUD/NZD/CAD, and **10–25× too tight for
USDSEK/USDNOK** (2–5 pips a side is normal). Costs must be **per pair**, defaulted from a table, and
editable (which is REQ-063 — see §3b, it must be a Must).

**[CONFIRM]** — the user's actual band, in mm and in pips, per pair; whether they tighten into
events; whether they hedge in the pair or in a proxy (people hedge AUDUSD gamma in AUDUSD, but they
hedge USDNOK gamma in EURNOK or EURUSD+EURNOK more often than you'd think).

---

### Q-3 — Which vol mark is truth

**[STANDARD] — this one is not a matter of taste. ETF-implied vol is never a mark.**

FXE/FXB/FXY option vols differ from the OTC pair vol for reasons that do not net out: the ETF's
expense ratio and its interest accrual, US-listed trading hours (the chain stops quoting at 16:00
NY while the OTC pair trades 24h), American exercise on the listed options, creation/redemption
mechanics, and — the killer — the listed strike grid is coarse and the wings are illiquid, so BF
and therefore all your volga/vanna is fitted to two bad prints (R-5 already says this).

Sizing the consequence: on a **EUR 100mm 1M straddle, one vol point is USD ~248,000 of PV.** A
half-point ETF basis is USD 124k of mark error on one position, every day, and it feeds straight
into delta (through the smile), into the hedge size, and into the attribution residual.

**Ship this:** a **two-tier surface status**, badged everywhere:
- `MARK` — built from user-entered ATM / RR25 / BF25 (and 10d if given) per tenor. This is the only
  status from which the Risk page will issue a hedge instruction or the P&L page will publish an
  official mark.
- `INDICATIVE` — built from ETF chains / CBOE EVZ-JYVIX-BPVIX / CME. Usable for z-scores, richness
  ranking, term-structure shape, cones, and the Lab. Every number derived from it carries the badge,
  and REQ-043's hedge instruction is **disabled** (greyed with a reason) unless the user
  acknowledges once per session.
- The **ETF−MARK basis** is its own displayed series whenever both exist. That basis is the honest
  measure of how much you can trust the indicative path, and after a month it is genuinely useful.

This makes the manual quote grid a **Must** (see §3c, MISS-1). It is the single highest-value
change in this review.

**[CONFIRM]** — only this: does the user have *any* routine access to OTC quotes they could paste
each morning (a broker run, a chat, a screenshot, a daily email)? If yes, the quote grid is the
product. If genuinely no, the charter must be amended to say v1 answers "cheap versus its own
history", never "cheap versus the market", and the PM should say so out loud.

---

### Q-4 — Premium-adjusted delta

**[STANDARD] — yes, `spot_pa` for the USD-base pairs is correct, and `conventions.py` has it right**
for USDJPY / USDCHF / USDCAD / USDSEK / USDNOK. The rule underneath it: **premium adjustment applies
when the premium is paid in the base (foreign) currency.** For USDJPY the premium is paid in USD,
which is the base, so the premium itself carries spot risk and must be netted out of the delta. For
EURUSD the premium is paid in USD, the quote ccy, so no adjustment. That is the whole of it.

Three things the spec gets wrong or leaves open:

- **The adjustment is not decoration, it is size.** The pa/plain difference on one leg is *exactly*
  the premium expressed as a % of base notional: ~1% for a 1M ATM, ~2% for a 3M ATM, more at high
  vol. On a USD 100mm USDJPY position that is **USD 1–2mm of delta per leg**, permanently. It
  largely cancels on a straddle and does **not** cancel on anything skewed — an RR, a spread, a
  single directional option. Half the G10 book is exposed to it.
- **`PairSpec.delta_convention` as a static string is wrong twice over.** (a) The convention is a
  *consequence* of the premium currency, and `OptionPosition` already carries `premium_ccy` — so
  derive it per position, and warn loudly if a position's premium ccy contradicts the pair default.
  (b) The convention is **tenor-dependent**: G10 quotes spot delta out to 1Y and **forward delta
  beyond 1Y**. `TENORS` goes to 2Y and V-3 permits 2Y, so the tool will label a 2Y strike with the
  wrong delta. **Contract change request CR-2, §8** — `conventions.delta_convention(pair, T,
  premium_ccy) -> str`.
- **`strike_from_delta` will silently return the wrong strike for premium-adjusted calls.** Under
  premium adjustment, call delta is **not monotonic** in strike — it rises, peaks, then falls. A
  naive root-find on "find K such that Δ_pa = 0.25" has two roots, and the market convention is the
  one **above** the maximum-delta strike. Nothing in arch §4 or in the requirements mentions this.
  Consequence: your "25 delta" is a 35 delta, your RR and BF are read off the wrong strikes, and your
  skew signal is wrong by tenths of a vol point — which is the entire signal. **Fix:** bracket the
  search on `[K_at_max_delta, ∞)`, and raise (never clamp) if the requested delta exceeds the
  attainable maximum. Add a QA case per pa pair.

**Display:** show **both** — the pair-convention delta (the number you quote a broker) and the
**hedge delta** (what you actually trade), side by side, with the difference in base mm. Headline
the hedge delta. Convention named on every delta cell, in words, as REQ-017 already demands for the
smile.

**[CONFIRM]** — (a) EURJPY / EURGBP / EURCHF in `conventions.py` are set to `spot_pa` / `spot` /
`spot` respectively; the interbank convention for the EUR crosses depends on which leg the premium
is paid in and is worth one question rather than one assumption. (b) Does the user ever pay premium
in the non-standard leg (it happens on request)?

---

### Q-5 — Reporting currency

**[STANDARD] — USD for the aggregate, native quote ccy for the per-pair rows.** A G10 book P&Ls in
USD. Nobody wants a book total in JPY. Ship `report_ccy="USD"`, per-pair sub-totals in the pair's
quote ccy underneath, and the conversion rate disclosed — which is exactly what AMENDMENT CG-1
already specifies. That resolution is correct; keep it.

One demand on top: **the QA suite must assert that per-pair sub-totals converted at the disclosed
rate sum to the aggregate, to the penny.** R-15 says this; make it an acceptance criterion on
REQ-038 and REQ-051, not just a risk-register line. And `risk.fx_rate` raising rather than
defaulting to 1.0 (as the amendment specifies) is right — apply the same discipline to
`MarketSnapshot.rd_rf`, which currently defaults a missing rate to `0.0` (see §3a).

---

### Q-6 — Weekend and event-weighted time

**Market standard** is split, deliberately: the OTC market **prices** in business/event-weighted
time (that is *why* the term structure kinks around FOMC and why an overnight over a weekend is not
three times an overnight), but everyone **reports** theta in calendar days because that is when the
cash leaves. Both are correct for their purpose. The amendment's ruling — calendar as default,
weighted opt-in and badged — is the right ruling.

**But the headline card as specified is still wrong on Fridays**, and Fridays are 20% of mornings.
REQ-038/REQ-039 show θ "per calendar day". On a Friday morning that number is a lie by a factor of
three: you are about to pay three days. On the reference EUR 10mm straddle that is USD 8,600 (3 x 2,870) shown
against USD 17,400 actually owed.

**Ship this default:**
- The Greek card shows **"θ to next mark"**, with the number of calendar days in that period printed
  next to it. Friday reads "θ to Monday: USD 17,400 (3 days)". This is a one-line change to REQ-038
  and REQ-039 and it removes a recurring error.
- `time_decay` defaults to `calendar="calendar"` per the amendment, and the **weekend panel**
  (§4.6) shows both conventions side by side. Good as specified.
- Default weights: normal weekday 1.0, **weekend day 0.15**, market holiday 0.25, NFP 1.5, US CPI
  1.5, central-bank decision 2.0. The BA's proposed 0.10 weekend weight is on the low side of what
  G10 desks actually use; 0.10–0.25 is the observed range and 0.15 is the middle.

**[CONFIRM]** — the weights. They *are* desk-specific: some desks run weekend at 0.10, some at 0.25,
and event multipliers vary by how much of the desk's book is event-driven. Ask; do not assume. Also
ask whether they want event weighting to affect the *pricing* T (it changes every Greek) or only the
decay display (it doesn't). Those are very different builds — the requirements do not currently
distinguish them and they must.

---

### Q-7 — Tenor range

**Market standard for a gamma book: ON to 3M is where you live.** Overnight, 1W, 2W and 1M are the
gamma; 2M–3M is the fringe; 6M–1Y is where you put a vega hedge or a structural view, not gamma.
Under a third of gamma-book risk sits beyond 3M on a typical desk.

**Ship this:** full analytics ON–3M; 6M and 1Y priced and shown but excluded from the gamma-centric
headline screens; nothing beyond 1Y in v1 — **drop 2Y from `TENORS`** rather than price it badly
(it drags in the forward-delta convention of Q-4 and the flat-rate problem below for no benefit).

**On the flat-rate assumption (R-9, NG-6):** it is fine to ≤3M and it is *not* fine at 1Y for
USDJPY, USDCAD or the Scandies, where the differential moves the forward materially and therefore
moves the ATMF strike, the delta-neutral strike and every delta.

**Better fix than building rate curves — and it is cheaper.** Do not model curves. Let the user
enter **forward points (swap points) per pair per tenor** in the same grid as the vol quotes. That
is one row of numbers they can read off any screen, it is the number the market actually quotes, and
it removes the entire rate-curve problem including the discount/carry inconsistency. Derive
`rd − rf` from the forward point where present, and fall back to the flat rate (badged) where not.
**Contract change request CR-3, §8.**

**[CONFIRM]** — does the user actually trade past 3M, and do they run a separate vega book? If they
do, the vega ladder (MISS-3) matters more than anything on page 2.

---

### Q-8 — CME open interest as a market-gamma proxy

**My answer: no. It is a curiosity, not a positioning read. Demote page 3.**

Four independent reasons, any one of which is sufficient:

1. **Share.** CME FX options are a small single-digit percentage of the FX options market. OTC FX
   options turn over on the order of hundreds of billions a day; the listed venue is not where G10
   vanilla risk sits. You are reading a rounding error and calling it "the market".
2. **Sign is unknowable and the spec's disclaimer does not fix it.** REQ-023's stated assumption —
   "assumes customers buy calls/puts as tagged" — is not an assumption, it is a placeholder: open
   interest carries **no side tag at all**. Every contract has a buyer and a seller. Printing a
   *signed* dealer gamma with a disclaimer underneath is worse than not printing it, because people
   read the chart and not the caption. R-14 is right to flag this and the mitigation is too weak.
3. **Mechanics the requirements do not mention, all of which silently corrupt the number.** These
   are options **on futures**, not on spot — pricing them with a spot GK model without converting
   futures→spot double-counts carry. CME lists both European-style (10:00 ET, matching the NY cut)
   and American-style options on the same futures — the adapter cannot assume one. Contract
   multipliers are mandatory and absent from the spec (6E = EUR 125,000, 6B = GBP 62,500,
   **6J = JPY 12,500,000**, 6A/6C/6N = 100,000, 6S = CHF 125,000); OI is in *contracts* and is
   meaningless until multiplied. And **inverting the quote is not a rescale**: for 6J, quoted in USD
   per JPY, with `X = 1/F`, `d²V/dX² = F⁴·V_FF + 2F³·V_F` — the gamma conversion carries a **delta
   term**. Any adapter that "inverts" gamma by scaling it will be wrong, and wrong in a way nobody
   notices.
4. **It misses what actually pins.** Spot pins to **OTC expiry notionals at the 10:00 NY cut**, and
   those are not public. What a real desk uses is the expiry chatter that circulates each morning —
   "1.0850 EUR 1.2bn NY cut".

**Ship this instead:** keep page 3, rename it **"Listed positioning (indicative)"**, demote its
Musts (§3b), show OI **unsigned** as a strike-concentration and notional map with the multiplier and
conversion stated, and — the actually useful part — **add a manual expiry-notional entry table**
where the trader types the day's expiry chatter (pair, strike, cut, notional, source note). That
table, drawn on the Risk ladder next to their own strikes, is worth more than the entire CME
adapter. **MISS-6 in §3c.**

**[CONFIRM]** — does the user get expiry chatter, and would they type it in? (In my experience: yes,
and yes, if it takes under 30 seconds.) Also whether they trade listed FX options at all.

---

### Q-9 — Pin risk horizon and cuts

**Market standard.**
- **Horizon:** the pin panel matters from **T-3 business days** as a *watchlist*, becomes a real
  decision at **T-1**, and is a live, minute-by-minute concern from the **open of the cut day until
  the cut passes**. The requirement's ≤3 business-day trigger (REQ-045) is right; what it is missing
  is *escalation* — the panel must promote itself to a page-level banner inside 24h of a cut and
  into the header inside 2h.
- **Cuts:** **NY 10:00** is the cut for essentially all G10 vanilla — if you build one clock, build
  that one. **Tokyo 15:00** genuinely matters for USDJPY and the JPY crosses and is not optional on a
  book with JPY in it. **London 16:00** is a distant third (some EURGBP and legacy). `conventions.CUTS`
  has all three; the default per pair in `PAIRS` sets EURJPY to TKY15 and EURGBP to LDN16, which is
  defensible but is exactly the kind of default that should be confirmed rather than shipped silently.

**Ship this default:** pin panel arms at T-3 business days; header countdown for the next NY10 and
the next TKY15 always visible; banner inside 24h; the panel shows, per strike, **two explicit
numbers — the delta you inherit if spot finishes above, and the delta if below** (see the REQ-045
fix in §3a, this is currently mis-specified).

**[CONFIRM]** — whether the user trades TKY or LDN cuts at all, and whether they want a cut clock
for cuts on which they hold no position (I would not; it is noise).

---

### Q-10 — What would make me stop trusting a number, and the check I'd run first

**[STANDARD], and this is the most important question in the list.** Three checks, in order, and all
three should be *on the screen* rather than in the test suite:

1. **Does yesterday still say what it said yesterday?** Reprice the stored EOD book on the stored
   EOD marks and confirm the PV equals what the tool published. If yesterday's P&L moves overnight,
   I stop reading the P&L page permanently. REQ-054 gets this right by rebuilding from stored
   snapshots; make it an explicit *displayed* reconciliation line ("yesterday's published PV vs
   yesterday's PV recomputed now: 0.00"), not an internal invariant.
2. **Does the attribution close?** `Σ components = total`, with `|unexplained| / Σ|components|`
   displayed always, not just when it breaches. REQ-052's 5% threshold is generous — on a clean
   vanilla book with a good mark, the residual should be **under 1%** overnight and under 2–3% on a
   day with a big smile move. Show the number every day so I can see it drifting *before* it
   breaches; a threshold that only speaks when it's already broken teaches nothing.
3. **Does one option price agree with the market?** Give me a **reconcile box**: I type a broker's
   premium (in any of the four units) for one of my positions and the app tells me the implied vol
   that premium corresponds to and the difference vs the surface vol, in vol points. That single
   control checks the pricer, the day count, the premium convention, the delta convention and the
   surface in one keystroke. It is a two-hour build and it is the difference between a tool I trust
   and a tool I sanity-check in Excel.

Plus the continuous one: **spot age and spot source, always visible, never on hover.** With
15-minute-delayed free data (R-2) the very first thing I do is compare the app's spot to the price
I can actually deal on. Give me a manual spot box on the Risk page that overrides everything
downstream in one keystroke — that is REQ-068 but it must live *on the risk screen*, not on page 8.

---

## 3. Requirement triage

### 3a. Wrong or dangerous

These are ordered by how much money the error could cost. "Fix" is a demand on the spec, not a
suggestion.

| # | Where | What is wrong | Fix |
|---|---|---|---|
| **W-1** | **REQ-045** (pin risk, **M**) | The requirement labels `Σ direction·cp·notional_base` as "the **delta discontinuity** inherited at expiry". That expression is the **delta you inherit if the option finishes ITM** — a different number. The actual discontinuity as spot crosses a strike upward is `Σ direction·notional_base`, **independent of `cp`**: a long call gains +N crossing up, a long put loses its −N, also +N. Reading the ITM formula as a discontinuity gets the **sign wrong on every put**. This is the number a short-gamma trader stares at at 09:55 NY with minutes to act. | Report **two labelled numbers per strike**: `delta_if_above` = Σ over options at that strike of `direction·cp·N` for those ITM above, and `delta_if_below` likewise; and separately `jump_at_strike = Σ direction·N`. Never one number called "the discontinuity". QA fixture: long put, spot crossing the strike, assert the jump is `+N`. |
| **W-2** | `conventions.year_fraction` (contract) | `max(dt/(365·86400), floor)` with `floor = 1/(365·24)`. After the cut, `dt` is negative and the function returns **one hour of time value forever**. An option that expired last Tuesday still shows gamma, vega and theta, still contributes to the Greek cards, and still appears in the ladder. You will hedge a position you no longer own — or fail to hedge the spot you inherited from it. | `year_fraction` returns `0.0` past the cut. Pricing at `T=0` returns intrinsic. `price_book` carries a hard `EXPIRED` state; expired rows leave every aggregate, are shown in a separate strip for the day, and the inherited spot delta from W-1 is surfaced as an action ("you are now long USD 20mm from the 147.00 strike — book the hedge"). The 1-hour floor is fine as a *numerical* floor **before** the cut; it must not survive it. **CR-4.** |
| **W-3** | `types.Greeks.__add__` / `__mul__` (contract) | `_FIELDS` includes **`delta_pct`** (spot delta *per 1 unit of notional* — an intensive quantity) and **`dual_delta`** (`d(pv)/dK`, defined against a *specific strike*). Both are summed across positions. A book-level `delta_pct` is the sum of per-unit deltas and means nothing; a book-level `dual_delta` adds sensitivities to different strikes. **REQ-038 explicitly puts "dual delta" on the aggregate Greek card** — that card will print a confident, meaningless number. | Remove `delta_pct` and `dual_delta` from the `_FIELDS` used by `__add__`/`__mul__` (or force them to `nan` at book level so nothing renders). Drop dual delta from REQ-038's aggregate card; keep it per-position only, where it is a legitimate density proxy. **CR-5.** |
| **W-4** | `types.MarketSnapshot.rd_rf` (contract) | `self.rates.get(spec.quote, 0.0)` — a **missing rate silently becomes zero**. That directly violates arch §7 ("never silently substitute"). Consequence: the forward collapses to spot, so ATMF and the delta-neutral strike are wrong, so `strike_from_delta` is wrong, so the whole smile is looked up at wrong strikes. On USDJPY 1Y with a ~3.5% differential the forward is ~5 big figures away from spot. | Raise on a missing rate, exactly as AMENDMENT CG-1 already mandates for `risk.fx_rate`. Consistency: the same discipline everywhere a market input can be absent. **CR-6.** |
| **W-5** | **REQ-046** (decay path, **M**) | The stated identity `0.5·Γ$·(σ_r² − σ_i²)·Δt` is **out by a factor of 100** given the document's own definition `Γ$ = Γ₁·S` and `Γ₁ = Γ·0.01·S`. The correct delta-hedged carry is `0.5·Γ·S²·(σ_r²−σ_i²)·Δt` = `50·Γ$·(σ_r²−σ_i²)·Δt`. The units of `Δt` (years vs days) are also unstated. | Write the identity once, in years, in one helper, next to the §0 breakeven helper: `dhedge_pnl = 50·Γ₁·S·(σ_r² − σ_i²)·Δt_years`. QA: at `σ_r = σ_i` the path must equal the theta path to ε. |
| **W-6** | **REQ-039** (breakeven card, **M**) | Two problems. (a) The **gamma/theta ratio `Γ$/|θ|`** has units of *days per percent* — it is dimensionally meaningless and cannot be compared across pairs, which is the stated purpose (§4.1). (b) **`|θ|`**: `Greeks.theta` is unsigned in the contract and the requirement takes an absolute value, so a book that *collects* theta and one that *pays* it produce the same card. | (a) Replace with a **coverage ratio**: `gamma_pnl(yesterday's realized move) / |θ_to_next_mark|`, dimensionless, equal to 1.0 exactly at breakeven and equal to `(σ_r/σ_i)²` for an ATM book. That is a number a trader can read across pairs. (b) Theta is **signed**, negative = you pay, and the card says it in words: "you pay USD 17,400 to Monday". |
| **W-7** | **REQ-039 / REQ-025** (sigma-days) | Both define a sigma-day as `σ_ATM/√365`. That is the correct **option-economics** daily move (you pay theta on calendar days) but it is the **wrong denominator for "how far can spot travel"**, because spot moves on ~252 trading days. Using √365 understates the achievable daily move by **20%** (EURUSD at 7.05%: 40 pips vs 48 pips). Every "distance in sigma-days" on the screen is then 20% too large, which makes far strikes look safer than they are. | Two named quantities, never conflated: `BE_daily` uses **√365** (economics), `sigma_day_move` uses **√252** (distance/probability). Every screen that measures distance-to-a-level or probability-of-touch uses √252. Print the basis on the panel. |
| **W-8** | **REQ-008 + REQ-009** (RV/IV richness, both **M**) | REQ-008 annualises RV on √252 (correct, and it *is* comparable to an ACT/365 implied — I checked the arithmetic). But REQ-009 says only "ATM implied at the tenor matching the RV horizon", where the RV horizons offered are 10/21/63 **business** days and the tenors are 1W/1M/3M **calendar**. 21bd ≠ 1M; 63bd ≠ 3M. Worse, nothing requires the RV window to be the *option's actual calendar window*, and nothing addresses the overlapping-window problem in the z-score, which inflates every z by roughly √(window length). Typical EURUSD IV−RV spread is 0.5–1.5 vol points on a 7% vol; a mismatched window moves it by more than that and **flips the sign of "cheap or expensive"**. | Match on **calendar days to the actual expiry**, not on a nominal tenor label: for a 30-calendar-day option use the trailing 30 calendar days (≈21bd) of returns, annualise √252, and print "RV 21bd / 30cd, matched to expiry 07-Oct". Z-scores on non-overlapping windows or with an effective-sample-size correction, and print the effective n. |
| **W-9** | `conventions.TENORS` + `tenor_years` (contract) | Tenors are **constant year fractions**: `1M = 1/12 = 0.08333`, `3M = 0.25`, `ON = 1/365`. Real tenors are dates: spot+lag, roll forward N months, roll to the next good business day, then ACT/365 to the cut. A 31-day 1M is `0.08493` — a **2% error in T**, which is a 1% error in every vega and a bigger one in theta. **`ON = 1/365` is worse: an overnight struck on a Friday is three calendar days**, and overnight gamma is the single most leveraged position on the book. There is also **no holiday calendar anywhere** in the architecture (CG-6 gives an *event* calendar, not a settlement calendar), yet §3.1 requires tenor buttons "rolled to the next good business day". | `tenor_years(tenor, asof, pair)` resolves through real dates. Ship a per-currency holiday calendar as a versioned CSV alongside `events.csv` (same owner, `data`). Overnight on a Friday must show 3 days. **CR-7.** |
| **W-10** | **REQ-038** (aggregate Greek cards, **M**) | (a) A **single vega number for the whole book**. Adding ON vega to 1Y vega is a pure parallel-shift assumption and it hides the actual risk of a gamma book — that all the gamma is in one week and all the vega is somewhere else. (b) The **headline delta does not state its sticky convention.** REQ-040 makes sticky-strike/sticky-delta a toggle on the ladder, but the card that I read first has no convention on it. On a risk-reversal book the skew delta `ν·∂σ/∂S` is a large fraction of total delta; showing the wrong one means hedging the wrong size every day. | (a) Vega **bucketed by tenor** (ON/1W/2W/1M/2M/3M/6M/1Y) plus a √T-weighted total, on the card. See MISS-3. (b) The delta card names its convention in words and defaults to the convention the user hedges on; both are one click apart and the difference is displayed in base mm. |
| **W-11** | **REQ-022 / REQ-023 / REQ-024** (Gamma Map, all **M**) | Signed "dealer gamma" printed from open interest, with contract multipliers unmentioned, futures-vs-spot unmentioned, American-vs-European unmentioned, the inversion Jacobian unmentioned, and CME expiry times assumed to be the OTC `PAIRS[pair].cut` (REQ-024 says "each expiry's cut time"). See §2/Q-8 for the full argument. | Unsigned only. Multipliers table mandatory and displayed. Futures→spot conversion stated. Expiry instants come from the **CME product calendar**, not from `PAIRS[pair].cut`. Rename to "Listed positioning (indicative)". Demote (see 3b). |
| **W-12** | **§3.5** premium units + **V-16** | The importer accepts `premium_unit ∈ {total, pips, pct_base, pct_quote}` and `premium_ccy` **independently**, but the two are coupled: premium paid in the base ccy is exactly what triggers premium-adjusted delta (Q-4). And the validation net has a hole: on the BA's own USDJPY example, entering `1.05` as `pct_base` instead of `105` as `pips` gives JPY 30.9mm vs the true JPY 21.0mm — a **47% premium error that passes V-16** (whose threshold is 25% of notional). | (a) Derive pa-ness per position from `premium_ccy` and warn on any contradiction with the pair default. (b) Add the check that catches essentially every premium unit error: **back out the implied vol from the entered premium and compare it to the surface vol at that strike/tenor. Warn above 0.5 vol points, block above 2.0.** (c) Echo the premium in **all four** units before commit (§5.4 asks for three — make it four, adding % of base). This single check is worth more than V-14 through V-17 combined. |
| **W-13** | `types.HedgeRule` defaults (contract) | `band_pct = 0.25` documented as "% of notional" is ~60× too tight (§2/Q-2): it rehedges on a 0.6-pip move. `cost_bp = 0.2` as a single global constant is 10–25× too tight for USDSEK/USDNOK. Both are the defaults a first-time user will run the Lab and the Risk page on, and both make the tool's output nonsense in opposite directions (absurd turnover, absurdly cheap). | Band default 15% of gross option notional with an absolute clip floor; **per-pair** cost table as the default for `cost_bp`; field renamed so the unit is unambiguous. **CR-1.** |
| **W-14** | **REQ-051** (attribution, **M**) | The waterfall is specified between "two stamped snapshots" with the boundary printed, but nothing says theta is charged over the **actual elapsed time**. The morning mark is ~14 hours after the 17:00 NY close, not a day. Charging a calendar day of theta into a 14-hour P&L puts the theta bar ~40% out and pushes the error into `unexplained`, which then trips REQ-052 for the wrong reason and teaches the trader to ignore the residual alarm. | Theta over elapsed wall-clock between the two snapshot timestamps. Display the elapsed period on the waterfall. Same for `carry`. |
| **W-15** | **§6.3** BE identity acceptance test | "For an ATM-only book, `BE_daily = σ_ATM/√365` to < 1e-6 relative." That identity holds only when the theta used is the **gamma-theta** term `−0.5·Γ·S²·σ²`. Full Black–Scholes/GK theta also contains the rate terms `−r_d·K·e^{−r_d T}·N(d₂) + r_f·S·e^{−r_f T}·N(d₁)`, so with any non-zero rates the test **fails**. And the trader-relevant point is the same: the breakeven card must not include carry that the gamma does not pay back. | Define `BE_daily` off the **gamma-theta component** (total theta net of the rho/carry terms, which are separately displayed as carry). Run the §6.3 test at zero rates *and* a second test at realistic rates against the gamma-theta definition. |
| **W-16** | `conventions.pip_value` | Accepts `spot` and never uses it. Correct as written (`pip·notional_base` is the quote-ccy value of a pip) but the dead argument invites someone to "fix" it by multiplying by spot, producing a silent factor-of-S error on every JPY pip figure. | Drop the argument or use it to also return the base-ccy value; add a docstring example for both a JPY and a non-JPY pair, and a QA case for each. |

### 3b. Right, but mis-prioritised

**The frame:** in the first 30 seconds of the morning I answer three questions — *am I long or short
gamma and by how much*, *what does today cost me and what move pays for it*, and *did anything gap
or expire while I was asleep*. Anything that does not serve those three is not a morning feature,
however good it is. Conversely, anything the tool refuses to answer without live OTC data has no
business being a Must, because it will never be Done.

**Promote (Could/Should → Must):**

| REQ | Now | Should be | Why |
|---|---|---|---|
| **REQ-036** mark-vol override per position | S | **M** | With no OTC surface (Q-3), **the trader's own mark *is* the book**. Without this the P&L page publishes an ETF's opinion of my position. This is the highest-value promotion in the list and it is currently a Should with a contract gap attached. |
| **REQ-021** strike/tenor what-if pricer | **C** | **M** | "What does this straddle cost and how much gamma do I get" is used twenty times a day (J5) and needs no book, no persistence and no state. Making it a Could is the single clearest priority error in the document. Merge it with REQ-035 into one pre-trade panel that appears on both page 2 and page 4. |
| **REQ-012** event calendar overlay | S | **M** | Trading short gamma without knowing FOMC/CPI/NFP dates is not a preference, it is a hazard. It also drives the tenor choice, the weekend decision (J8) and the term-structure read. It is a static CSV — cheap. |
| **REQ-063** editable cost model | S | **M** | REQ-061 (M) reports a **net-of-cost** equity curve. A Must that depends on a Should is not a plan. And with the wrong `cost_bp` default (W-13) the net curve is fiction. |
| **REQ-064** realized-vs-implied backtest decomposition | S | **M** | It is the only thing that tells you whether the backtest P&L came from vol or from hedge luck, and it is the closure test that proves the engine is right. Without it the equity curve is unauditable. |
| **REQ-047** worst case / stop-out spot level | S | **M** | When I am short gamma, "the spot level at which I hit my stop" is a first-30-seconds number, not a Should. It is also cheap once the scenario grid exists. |
| **REQ-065** regime / event conditioning of backtest stats | S | **M** | An unconditional 5-year Sharpe over 2021–2026 is a number about the sample, not about the strategy (§7). Bucketing is what makes the Lab honest. |

**Demote (Must → Should/Could):**

| REQ | Now | Should be | Why |
|---|---|---|---|
| **REQ-022** market gamma by strike | M | **S** | See Q-8. Small share of the market, unknowable sign, heavy mechanics burden. It is interesting; it is not a blocker. |
| **REQ-023** dealer-gamma sign disclaimer | M | **S** (and rewrite) | The disclaimer as written ("assumes customers buy calls/puts as tagged") describes an assumption that cannot be made. Fold it into REQ-022 as an unsigned-only constraint. |
| **REQ-024** expiry ladder from CME OI | M | **S** | Follows REQ-022. The *book's own* expiry ladder (REQ-044) is the Must; the market's listed one is not. |
| **REQ-041** scenario grid | M | **S** | A 21×13 matrix is a Sunday-afternoon tool. In a fast market I read a ladder, not a heat map (§4). Keep it; it is not what blocks v1. |
| **REQ-018** vol cone | M | **S** | Cones are a weekly positioning tool. Nothing on a cone changes what I do in the next hour. |
| **REQ-014** implied correlation | C | **C** — fine, but move behind a flag | Correct priority already; noting only that it must not consume build time before MISS-1. |
| **REQ-005** keyboard-first navigation | S | **C** | Nice. Not a reason to keep or abandon the tool. (One exception: a single-key **refresh** and a single-key **spot override** are Must — see §4.) |
| **REQ-011** RV–IV heat strip | S | **C** | Pretty, and I would look at it once a month. |
| **REQ-013** regime tag | S | **C or W** | A label ("breakout") is exactly the kind of derived opinion an expert user ignores and a novice over-trusts. The *inputs* are already on the screen. If it ships at all, ship it as a filter for the Lab (REQ-065), not as a badge on the Monitor. |
| **REQ-070** run the live-source verifier from the UI | S | **C** | It is a command line away and it is documented to fail in the sandbox. |
| **REQ-008** five RV estimators | M (keep) | **M, but two on the front screen** | Keeping ≥3 estimators available is right (R-10). Showing five on the morning screen is noise; default to close-to-close plus Yang–Zhang, the rest one click away. |

**Note on REQ-071 (preferences, S):** three of its settings — **timezone**, **reporting currency**
and **trading-day boundary** — change the value of every number on every screen. They are not
preferences, they are configuration, and they belong in first-run setup at **M** priority. The rest
(theme, display units, default hedge rule) can stay S.

### 3c. Missing

Ranked by what I would notice absent on day one.

| # | Missing | Why it matters | Where it goes |
|---|---|---|---|
| **MISS-1** | **Manual OTC quote grid as a first-class morning input.** A per-pair, per-tenor grid of ATM / RR25 / BF25 (+ 10d optional) that the trader pastes or types in under 30 seconds, timestamped, versioned, feeding `build_surface` directly, and flipping the surface status from `INDICATIVE` to `MARK`. | Without it the app has no mark and cannot be used to hedge (§1.4, Q-3). With it, every other analytic in the document becomes trustworthy. | New Must on page 2, mirrored on page 8. This is the top build priority in the whole project. |
| **MISS-2** | **Manual forward-points entry per pair/tenor** (Q-7). | Removes the flat-rate problem (R-9, NG-6) for a fraction of the cost of a curve build, and fixes ATMF / delta-neutral strikes. | Same grid as MISS-1. **CR-3.** |
| **MISS-3** | **Vega ladder by tenor bucket**, and gamma bucketed by **expiry date** (today / tomorrow / this week / next week / beyond). | A gamma book's defining question is "how much of my gamma expires this week". One aggregate vega and one aggregate gamma number cannot answer it. REQ-048 does concentration but as an HHI heat table, which is a risk-management artefact, not a trading one. | Promote into REQ-038's card set; REQ-048 stays as the Should it is. |
| **MISS-4** | **The one-line answer.** A single, always-visible sentence per pair: "**LONG GAMMA · EUR 4.0mm per 1% · pays above 41 pips today · costs USD 2,870 (USD 8,600 to Monday)**". | The persona explicitly abandons a screen that takes >2s to answer "am I long or short gamma". No REQ produces that answer; REQ-038 produces a grid of cards from which the user must derive it. | New Must, top of page 5 and repeated in the app header. See §4. |
| **MISS-5** | **Reconcile box** — enter a broker premium (any of the four units) for one position, get the implied vol and the difference vs the surface, in vol points. | The single check that validates pricer, day count, premium convention, delta convention and surface at once. Answers Q-10. Two hours of work. | New Must on page 4. |
| **MISS-6** | **Manual OTC expiry-notional table** (pair, strike, cut, notional, note), drawn on the Risk ladder and the Gamma Map. | This is what actually pins spot, and it is what a desk actually watches. It replaces most of the value the CME adapter was supposed to provide (Q-8). | New Should on page 3, drawn on page 5. |
| **MISS-7** | **Hedge carry / roll cost.** A spot delta hedge is a T+2 position that must be rolled (tom-next). On USDJPY at a ~3.5% differential, carrying a USD 100mm hedge is ~USD 10k a day, and it is not gamma, theta or vega — today it lands in `unexplained`. | REQ-051's `carry` bar exists but nothing in the spec computes the *hedge's* carry, and §3.2's spot ticket has a `value_date` that nothing consumes. | Acceptance criterion on REQ-051 and REQ-055; forward points from MISS-2 make it a one-liner. |
| **MISS-8** | **Overnight / "what changed" diff.** Spot gap since the last mark, vol change per pair, positions that expired, positions added, and the resulting P&L estimate — before the full attribution runs. | It is literally the first question of the day (J1) and it is the fastest way to spot a bad mark. | New Must, top of page 6 or as a morning banner. |
| **MISS-9** | **Settlement / holiday calendar per currency.** | §3.1 requires tenor buttons "rolled to the next good business day" and V-2/V-3 imply business-day logic, but no calendar exists anywhere in the architecture. Without it, expiry dates are wrong around holidays — and a wrong expiry date is a wrong T, a wrong vol, a wrong Greek and a wrong cut clock. | `data/calendar/holidays.csv` alongside `events.csv`, same owner. **CR-7.** |
| **MISS-10** | **Delta shown in the units you trade in.** Base mm is right, but a hedge is executed as "sell EUR 4.2mm" *and* thought of as "USD 4.6mm equivalent" *and* checked as "P&L per pip". | REQ-038 gives `delta_base` only. Three representations of one number, always visible, costs nothing. | Acceptance criterion on REQ-038. |
| **MISS-11** | **Vanna and volga in desk units.** Arch §3 defines vanna as "quote ccy per 1 vol pt **per 1 unit spot**". For EURUSD, one unit of spot is a 100% move. REQ-038 puts vanna and volga on cards with no units specified. | Guaranteed misread, and vanna/volga are the whole P&L on an RR book (§4.7 says so itself). | Define vanna as **base-ccy delta change per +1 vol point** (equivalently vega change per +1% spot) and volga as **quote ccy per vol point per vol point**. State on the card. **CR-8.** |
| **MISS-12** | **Realized vol at my actual hedge frequency**, not close-to-close. | Close-to-close RV is not the vol you capture; the vol you capture depends on when you hedged. REQ-056's "realized vol I captured" is the ex-post version of this and is a Should; the ex-ante version (what a given band would have captured over the last N days) is missing. | Fold into REQ-062's sweep and surface it on the Risk page next to the band. |
| **MISS-13** | **Position-level P&L since *yesterday*, not only since trade.** | REQ-032 gives `PV − premium_paid` (P&L since inception). The daily question is "what did this line do today". | Acceptance criterion on REQ-032. |
| **MISS-14** | **A stated policy for options on crosses.** NG-11 says v1 prices crosses off their own quoted surface "where available, else flags as unsupported". No free source quotes EURJPY/EURGBP/EURCHF vol, so in practice **every cross is unsupported** — yet `conventions.PAIRS` ships all three and the demo book concept implies they work. | Either drop crosses from v1, or build them from the vol triangle and badge them `DERIVED` with the correlation assumption printed. Silently shipping unpriceable pairs in `PAIRS` is worse than either. | PM decision. |

