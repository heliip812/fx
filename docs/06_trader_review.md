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


---
---

# Part II — the sections lost to the interruption, plus the second pass on the built app

**Written:** 2026-09-07, after the infrastructure failure that truncated Part I at §3c.
**Reads against:** `01_architecture.md` AMENDMENTS v1.1–v1.7, `02_requirements.md` rev 3,
`05_test_report.md`, and the running app (`run.py --port 8077`, synthetic provider, demo book).

**Numbering note before anyone goes hunting.** The `§4.1`, `§4.6`, `§4.7`, `§5.2`, `§5.4` and
`§6.3` references in Part I are cross-references to **`02_requirements.md`**, not to this
document. The one exception is `§5.3` in §1.5, which is a typo: those two defects (the REQ-045
pin formula and `year_fraction`) are **W-1 and W-2 in §3a** and always were. The sections Part I
genuinely promised and never delivered are **§4** (the screens), **§7** (the Lab), **§8** (the
contract change requests CR-1…CR-8) and **§9** (the questions for the real user). All four are
below, followed by **§10**, the second pass on the app as built, and **§11**, the report to the PM.

---

## 3d. Amendment v1.4 checked — the PM is right and I was wrong

I have re-derived it rather than taken it on trust, and the correction stands. For EUR 10mm per
leg, S = 1.084, σ = 7.05%, T = 1/12:

- Γ per unit = `n(d₁)/(S·σ√T)` = `0.3989/(1.084 × 0.02035)` = 18.08, so
  Γ₁ per leg = `Γ·S/100 × N` = EUR 1.96mm, **both legs EUR 3.92mm**. The priced 3.914mm is right
  and my 3.95mm was right.
- Cash gamma `Γ·S²` = `Γ₁·S·100` = USD 4.243e8. Gamma-theta = `−0.5·Γ·S²·σ²/365`
  = `−0.5 × 4.243e8 × 0.004970 / 365` = **USD −2,889/day**, which is the priced −2,868 once the
  rate terms are added back. **USD 5,800 was roughly twice the truth.**
- Friday→Monday is therefore ~USD 8,600, not 17,400. Withdraw 17,400 wherever it appears.

The diagnosis is also right: I quoted Γ₁ for both legs and then, separately, doubled a
single-leg theta. It is the same class of error as REQ-046's factor of 100 and my own W-7 —
a quantity that is already aggregated, aggregated again. **I have no disagreement.** Ruling 2
(the header card prices theta from `book_greeks`, never from a constant) is the right structural
response, and ruling 3 (adopt the trade as a golden fixture that cross-checks Γ₁, θ and BE
against each other) is the thing that would have caught me. It is now
`tests/test_golden_reference_trade.py` and it is the most valuable test in the suite.

One correction *to* the correction, which matters for §4.3. Ruling 3's fixture and my §2 table
both compute the breakeven against **total** theta. On a straddle at rd 4% / rf 2% the call and
put rate terms very nearly cancel, so the identity `BE = σ/√365` survives to 0.3% and the
fixture passes. It does **not** cancel on a single option or on anything skewed: my W-15 stands
and the breakeven must be struck against the **gamma-theta component only**. `signals.richness`
has this right already (`theta_mode="gamma"`); §10 is where I found the app not printing it.

---

## 4. The two screens

Most dashboards fail by showing everything, and the failure mode is specific: a screen that
shows everything makes me *derive* the answer, and derivation at 07:05 with coffee in one hand
is where errors come from. Below are the only two screens I would actually keep open. They have
different jobs and therefore different designs, and neither of them is a superset of the other.

### 4.1 What the morning screen exists to answer

Three questions, in this order, in the first thirty seconds:

1. **Am I long or short gamma, in what size, per pair?** — the sign of everything else I do today.
2. **What does today cost me, and what move pays for it?** — the theta bill and the breakeven.
3. **Did anything gap, expire or get added while I was asleep?** — the overnight diff.

Everything else on the screen is evidence for one of those three. If a panel cannot be traced to
one of them it belongs on another page. Note that "is gamma cheap or expensive" is **not** on
that list: it is the 07:15 question, not the 07:00 one, and it belongs on the Market Monitor.

### 4.2 The morning screen — exact layout

One 1440-wide laptop screen, no scrolling for rows 0–2. Rows 3–5 may scroll.

**Row 0 — the strip that is on every page, not just this one (height ~28px).**

| Field | Format | Why |
|---|---|---|
| snapshot id + `asof` | `S-0912 · 07:03:41 BST` | REQ-001, one stamped snapshot |
| spot age, per G3 | `EUR 12s · GBP 12s · JPY 12s` | with 15-min-delayed free data (R-2) this is the first thing I check |
| spot source | `live` / `cached` / `synthetic` / `override`, as a **word**, coloured | never colour alone |
| mark status, per pair | `EURUSD MARK 07:01 · GBPUSD MARK 07:01 · USDJPY INDICATIVE` | I must never wonder whether I am looking at my curve or an ETF's |
| next cut | `NY10 in 2h 57m` and, if I hold JPY, `TKY15 in 6h 12m` | Q-9; suppressed for cuts where I hold nothing |
| book | `6 opt / 1 spot` | catches a failed load silently returning an empty book |

**Row 1 — the answer line. One row per pair with a live position. This is the screen.**

Fixed-width, one line each, no wrapping, no cards, no icons:

```
EURUSD  LONG GAMMA   EUR +3.91mm /1%   BE 40p (0.369%)   1σ-day 48p   θ→Mon  USD  −8,604 (3d)   Δ  EUR +2.41mm
GBPUSD  SHORT GAMMA  GBP −3.59mm /1%   BE 63p (0.467%)   1σ-day 53p   θ→Mon  USD +15,753 (3d)   Δ  GBP −0.01mm
USDJPY  LONG GAMMA   USD +0.31mm /1%   BE 81p (0.550%)   1σ-day 93p   θ→Mon  JPY +620,382 (3d)  Δ  USD +10.99mm
```

Field by field, with the units that must be printed and the ones that must not be inferred:

| Field | Unit, printed | Rule |
|---|---|---|
| side | the words `LONG GAMMA` / `SHORT GAMMA` / `FLAT` | never a `+`/`−` alone, never a colour alone |
| Γ₁ | **base ccy mm per +1% spot**, signed, with the ccy code | `EUR +3.91mm /1%`. `/1%` is part of the number, not a header |
| BE | **pips first, percent in brackets** | pips is what I trade in; the percent is what compares across pairs |
| 1σ-day | **pips**, on **√252** | printed adjacent to BE precisely because the two bases differ (W-7). If BE > 1σ-day I am paying more than a normal day delivers |
| θ→next mark | signed money in the **pair's quote ccy**, with the **day count in brackets** | Friday reads `(3d)`. Sign in the number, verb in the tooltip, never `\|θ\|` |
| Δ | **base ccy mm**, on my **hedge convention**, with the convention named on hover and the pair-convention delta one keystroke away | Q-4 |

Sign discipline, because this is where the app is currently wrong (§10.3, B-2). Four cases, four
sentences, no shared template:

- long gamma, pay theta → `BE 40p — above 40 pips today you make money`
- short gamma, collect theta → `BE 63p — you keep the theta below 63 pips; above it you pay`
- long gamma, collect theta → `BE 81p vs gamma-theta; you also collect JPY 206,794/day of carry — no net breakeven`
- short gamma, pay theta → `CHECK THE MARK — short gamma and paying theta should not happen on a vanilla book`

**Row 2 — where the gamma is (this is MISS-3 and it is not optional).**
Per pair, two small horizontal bars, numbers on the bars, no legend:

```
EURUSD  gamma by expiry:  today —   tomorrow —   this week EUR 3.91mm   next week —   beyond —
        vega by tenor:    ON —  1W —  2W —  1M USD 26.4k  2M —  3M —  6M —  1Y —
```

A gamma book's defining question is *how much of my gamma dies this week*. A single aggregate
gamma number and a single aggregate vega number cannot answer it, and adding ON vega to 1Y vega
is a parallel-shift assumption I did not make.

**Row 3 — the overnight diff (MISS-8).** Since the last stamped EOD mark, per pair:
spot then → now in **pips and percent**; ATM 1M then → now in **vol points**; positions that
**expired overnight and the spot delta I inherited from them, as an action line**; positions
added; and a first-cut P&L estimate split delta / gamma / theta / vega, marked `ESTIMATE` until
the full attribution runs. The elapsed period is printed — `17:00 NY → 07:03 LON = 14h 03m` —
and theta is charged over *that*, not over a day (W-14).

**Row 4 — one small ladder per pair, ±2%, 30 rows.** Spot centred and highlighted. Columns:
spot level, Δ at that level, cumulative gamma P&L from here, and a marker at the next hedge
trigger above and below. This is a *preview* of the fast-market screen, not a replacement:
it exists so I know where today's first hedge is before spot moves.

**Row 5 — today.** Events in my clock with the currency and importance; cuts today with the
notional expiring at each; and the residual-check line from Q-10:
`yesterday published PV USD 1,284,551 · recomputed now 1,284,551 · diff 0.00`.

### 4.3 The top-left number, and why it is that one

**Top-left is the signed Γ₁ of the pair I trade most, in base-ccy millions per +1% spot, with
the word LONG or SHORT next to it.**

Not PV: PV is the score of decisions I already made and it changes nothing I do today. Not
theta: theta is a *consequence* of gamma, and putting a consequence above its cause is how you
end up managing the symptom. Not delta: delta is a task, not a state — it is what I do about
gamma, and it is stale seconds after I read it. Not "gamma cheap or expensive": that is a
*trade* question and it belongs on the Market Monitor at 07:15, after I know what I already own.

Γ₁ is the only number on the screen whose **sign changes the meaning of every other number**.
Read it first and the rest of the screen interprets itself: with long gamma the breakeven is a
target and the theta is a cost; with short gamma the breakeven is a stop and the theta is
income. Read anything else first and you have to come back.

Two constraints on it. It is **per pair**, never a book total — I hedge per pair and a netted
G3 gamma number is an artefact of the reporting currency, not a position. And it is **priced on
every render from `book_greeks`**, never carried, never cached, never written down (amendment
v1.4 ruling 2 — that ruling exists because of my own arithmetic).

### 4.4 The fast-market screen

Different job. When spot is doing 30 pips a minute I am not asking whether gamma is cheap; I am
asking *what am I now, what do I do, at what level, in what size*. Nothing that takes a click,
nothing that re-fits a surface, nothing that needs me to read a legend.

**One pair. Full screen. No cross-pair anything.**

| Region | Content | Rule |
|---|---|---|
| Top left, large | **Spot**, with **age in seconds** and source word beside it; below it the **manual spot box**, focused by one key | With delayed data the manual box is the *primary* input, not an override. If the feed is >60s old the whole spot block turns amber and states the age in words |
| Under it | **Δ at the spot in the box**, in base mm, plus the same in quote-ccy equivalent and in P&L-per-pip | MISS-10. Three readings of one number, always together |
| Centre, full height | **The ladder as a vertical price strip**, spot pinned to the vertical centre, ~40 rows at pip granularity that matches the pair | A ladder, not a heat map. In a fast market I read down a column; I do not read a 21×13 matrix |
| Ladder columns | level · Δ at level · cumulative gamma P&L from here · **hedge trigger marker** · **my strikes** (size, expiry, cut) · **expiry chatter notionals** (MISS-6) | Everything I need to place an order sits on one row |
| Right rail | **The instruction**: `SELL EUR 2.0mm at 1.1690` and `BUY EUR 2.0mm at 1.1610` — a side, a size and a level, never a percentage | A band expressed as "15% of gross notional" is not an order. Convert it for me |
| Right rail, below | cost of hedging now vs waiting for the band, in **pips and money**, from the per-pair cost table | J4 |
| Bottom strip | **Cut countdown**, seconds inside the last 10 minutes, and the two pin numbers per strike: **Δ if above / Δ if below**, plus the jump | W-1 as ruled in v1.6 |
| Nowhere on this screen | theta, vega, vanna, volga, PV, richness, cones, the surface, the event calendar | They are all true and none of them changes what I do in the next 90 seconds |

Three behavioural rules that matter more than the layout:

1. **Every number recomputes from a typed spot in under 200ms.** If I type 1.1720 and wait, the
   screen is useless. This is the one place the latency budget is a *feature*, not an NFR.
2. **The sticky convention is chosen once and pinned**, with the difference between sticky-strike
   and sticky-delta delta shown once, in base mm, at the top. The engine measures a 4.1% gap on
   the reference book (v1.6); that is a real mis-size and I want to see it, but I do not want a
   toggle I might knock in a fast market.
3. **Nothing on this screen may be `INDICATIVE` without saying so on the instruction itself.**
   If the pair is not marked, the right rail reads `NO MARK — instruction suppressed` and shows
   the delta anyway. Delta off an ETF surface is still roughly right; a *hedge size* off one is
   a decision I did not make.

### 4.5 What is deliberately on neither screen

The 3-D surface, the vol cone, the RV–IV heat strip, the scenario matrix, the HHI concentration
table, the regime tag, implied correlation, listed open interest, five RV estimators, the equity
curve. Every one of them is worth having. None of them belongs on a screen whose job is measured
in seconds. They live on pages 1, 2, 3 and 7, and I open those deliberately.

### 4.6 The two keys that are Musts

Part I demoted REQ-005 (keyboard navigation) to a Could with one exception, and this is it:
**`r` restamps the snapshot** and **`s` focuses the manual spot box**. Both are one keystroke
because in a fast market a mouse trip to a text field is the difference between hedging at 1.1690
and hedging at 1.1710. Everything else in REQ-005 can go.

---

## 7. The Lab: why an unconditional Sharpe is a number about the sample

Part I promised this at §3b and never wrote it. Short, because the point is simple.

A delta-hedged gamma strategy backtested over 2021–2026 spans one of the largest realised-vol
regime shifts in G10 history. A single Sharpe over that window tells me about the window, not
about the rule. Four demands, all cheap:

1. **Bucket everything (REQ-065, which is why I promoted it to Must).** Report the equity curve
   and the stats *conditional* on: implied-vol tercile at entry, realised/implied ratio at entry,
   event days vs non-event days, and calendar year. A rule that makes all its money in the top
   vol tercile is a rule I size differently, not a rule I reject.
2. **The decomposition is the audit, not a nice-to-have (REQ-064).** `Σ (gamma P&L + theta +
   hedge slippage + cost) = equity curve`, printed, with a residual. If that does not close, the
   engine is wrong and the Sharpe is decoration. This is the same discipline as REQ-052 on the
   P&L page and it should share the helper.
3. **Costs per pair, or the net curve is fiction (REQ-063, W-13).** A single global `cost_bp`
   makes USDNOK look like EURUSD. v1.6 moved this to `zones.COST_BP` — good; the Lab must
   *display* the table it used, per pair, in pips, on the results page.
4. **Say how much history is actually behind it (C-6).** ETF chains give today's smile and no
   past. A hedge-frequency sweep run on 11 days of self-collected surface history is not a sweep;
   it is a plot. Print the effective sample size next to every statistic, and grey anything
   computed on fewer than 60 observations rather than printing a confident number.

And one thing the sweep must output in the units I trade: the optimal band in **millions and
pips per pair**, not as a percentage of notional. A band of "15%" is a research output; "hedge
EURUSD in 2mm clips every 35 pips" is a rule I can follow.

---

## 8. Contract change requests CR-1 … CR-8 — register and current status

Referenced throughout Part I, never tabulated. Status as of amendments v1.1–v1.7.

| CR | Where | Ask | Status |
|---|---|---|---|
| **CR-1** | `types.HedgeRule` | `band_pct=0.25` documented as "% of notional" is ~60× too tight; `cost_bp` must be per pair | **RESOLVED (v1.6).** Ruled a *fraction*, not a percent — 25%, which is the intent and is inside the working range. `cost_bp` moved to `zones.COST_BP` per pair with a per-rule override. Correct on both counts. The actual band stays Q-2 for the real user (§9). |
| **CR-2** | `conventions` / `gk` | derive the delta convention per position from `premium_ccy`; make it tenor-dependent (spot delta to 1Y, forward beyond); bracket `strike_from_delta` above the pa maximum rather than clamping | **PART DONE.** The pa non-monotonicity is fixed and hardened (v1.7: the `erf` underflow root cause, `max_attainable_delta`, the strict peak comparison). The **per-position derivation from `premium_ccy` is still open**, and the tenor-dependence is moot only if 2Y is dropped from `TENORS` — which has not happened. |
| **CR-3** | data / UI | let the user enter **forward points** per pair per tenor in the same grid as the vol quotes; derive `rd − rf` from them, fall back to the flat rate, badged | **OPEN.** Referred by the BA, not ruled. REQ-068(f) reserves the cell; the built mark grid has ATM/RR/BF only (§10.5). Still the cheapest fix for R-9/NG-6 in the project. |
| **CR-4** | `conventions.year_fraction` | must return `0.0` past the cut; expired legs leave every aggregate; the inherited spot delta becomes an action | **DONE (v1.2 T-3),** verified in `05_test_report.md` §3.1. The one-hour floor correctly survives only on the live side. |
| **CR-5** | `types.Greeks.__add__` | stop summing `delta_pct` and `dual_delta` | **DONE (v1.2 T-2)** — they aggregate to `nan` and render "n/a". Drop dual delta from REQ-038's aggregate card as well; that half is a spec edit, not code. |
| **CR-6** | `types.MarketSnapshot.rd_rf` | raise on a missing rate rather than defaulting to `0.0` | **DONE (v1.2 T-4),** and it now names the missing currency. |
| **CR-7** | `conventions.TENORS` / new data file | real-date tenors (spot+lag, month roll, business-day roll, ACT/365 to the cut) and a per-currency **settlement/holiday calendar** | **OPEN.** `1M` is still a constant `1/12` (a 2% error in T on a 31-day month) and **`ON` is still `1/365` on a Friday**, when it is three calendar days — on the single most leveraged line on the book. QA confirms no calendar exists. This is the largest un-actioned correctness item left. |
| **CR-8** | UI units | vanna in **base-ccy delta change per +1 vol point**; volga in quote ccy per vol point per vol point | **PART DONE.** `fmt.GREEK_UNITS` states the raw contract unit honestly, including "1 unit = a 100% spot move" — which is the right disclosure but is not the desk unit. Convert on the card; keep the raw unit on hover. |

---

## 9. Questions only the real user can answer

Part I promised to push everything that is genuinely my taste rather than market convention to
this section. Eight `[AWAITING USER]` items survive in `02_requirements.md` §10.1. They are not
equal: four of them block trading and four of them are corrections that can ship as defaults and
be fixed in a week. **Ask the first four before the user trades off this tool. The rest can wait
for the first review.**

**Must be answered before they trade off it:**

1. **"Every morning, can you get ATM, 25-delta risk reversal and 25-delta butterfly for your
   pairs from anywhere — a broker run, a chat, an email, a screenshot — yes or no?"**
   *(Q-3, charter-level, `02_requirements.md:1401`.)* If yes, the whole product works and the
   paste grid is the front door. If no, the charter must be rewritten today to promise "cheap
   versus its own history" and never "cheap versus the market", and the PM must say so out loud.

2. **"What time, in which timezone, is your official end-of-day mark — and do you also mark when
   you get in in the morning?"** *(Q-1, `:1396`.)* This sets the P&L boundary, the elapsed window
   theta is charged over, and whether the AM mark is persisted or scratch. Every number on the
   P&L page depends on it and `Europe/London` is currently a guess.

3. **"For each pair you trade, how far does delta have to drift before you hedge — in millions,
   and in pips?"** *(Q-2, `:1399`.)* 25% of gross notional is the shipping default and it is a
   plausible desk number, not their number. Also worth one line: do they tighten into events, and
   do they ever hedge a pair in a proxy?

4. **"Which expiry cuts do you actually trade — New York 10:00 only, or Tokyo 15:00 and London
   16:00 as well?"** *(Q-9, `:1416`.)* Blocking on any book with JPY in it: a mis-set cut moves
   `T`, theta and the pin clock by up to nine hours, and after the v1.2 T-3 fix it is the
   difference between carrying an inherited delta and not. Ask which cuts to put a clock on, too;
   a countdown for a cut where they hold nothing is noise.

**Should be asked, but ship a default and correct it later:**

5. **"Do you trade anything past three months, and do you run a separate vega book?"**
   *(Q-7, `:1411`.)* If yes, the vega ladder (MISS-3) outranks most of page 2.

6. **"Do you get the morning expiry chatter — '1.0850, EUR 1.2bn, NY cut' — and would you type it
   in if it took thirty seconds?"** *(Q-8, `:1414`.)* If yes, MISS-6 replaces most of the value
   the CME adapter was ever going to deliver. Add: do they trade listed FX options at all?

7. **"When you buy a EURJPY, EURGBP or EURCHF option, which currency do you pay the premium in?"**
   *(Q-4, `:1404`.)* It decides premium adjustment per position, and it is one line to answer and
   a wrong delta on half the crosses to guess.

8. **"How much of a normal day's theta do you think a weekend day is worth — and an FOMC or CPI
   day?"** *(Q-6, `:1408`.)* Shipping 0.15 / 2.0; the observed G10 range is 0.10–0.25 and event
   multipliers vary with how event-driven the book is. Display-only in v1, so a wrong guess is
   cosmetic rather than dangerous — which is why it is last.

**One question that is not on the list and should be:** *"When the tool and your broker disagree
by half a vol point, which do you want it to do — show both, or take yours?"* The answer is
almost certainly "show both", but MISS-5's reconcile box is now built (§10.2) and the follow-on
behaviour is undefined.

---

## 10. Second pass — the app as built

**Method.** Started it myself (`PYTHONPATH=/home/user/fx python3 run.py --port 8077`), fetched
every route, drove the callbacks in-process against the synthetic provider and the seeded demo
book, and read `app/pages/`, `app/components/fmt.py`, `app/components/badges.py`,
`app/pricing.py`, `app/layout.py` and `fxgamma/signals/richness.py`.

**Timestamp, and it matters.** The tree changed under me twice during this pass: `app/pricing.py`
was rewritten mid-review to delegate to `fxgamma.portfolio.risk`, and `app/layout.py` was
rewritten to a full eight-page nav. Everything below is against the tree as of **22:45 UTC**.
Where a finding may already be in flight I say so. Pages **5 Risk, 6 P&L and 7 Lab did not exist
at any point during this pass** — `app/pages/` holds `market.py`, `surface.py`, `gamma_map.py`,
`book.py`, `data.py` and nothing else, though the nav now links all eight.

### 10.1 Verdict

**The parts that exist are better than the spec I reviewed, and the app is not yet a tool I would
hedge off — mostly because the screen I would hedge off has not been written, and partly because
the one path to a real mark silently corrupts it.**

The build has taken the review seriously in a way I did not expect. MISS-1 (the manual vol grid)
is the top panel of the Data page, not a buried dialog. MISS-5 (the reconcile box) exists and
works. REQ-021 (the what-if pricer) has been promoted out of "Could" into a real panel with all
four premium units. The Gamma Map has been rewritten unsigned with the disclaimer in the panel
rather than in a caption. `fmt.py` is the best-executed file in the repo. And amendment v1.4's
ruling has been honoured literally: the headline theta is priced from `book_greeks` on every
render, never carried as a constant.

Against that: the single most-read sentence in the app currently prints "breakeven unavailable"
for the plainest position on the demo book, prints a breakeven struck against a theta of the
opposite sign to the one it prints beside it, and refuses to print a breakeven at all for a short
gamma book — which is the book where the breakeven is the whole decision. And the paste parser
that feeds the primary mark path will silently mark EURUSD at the yen vol.

### 10.2 What is right, and should be defended in review

- **`app/components/fmt.py`.** The `×100` for vols happens in exactly one function; JPY pairs
  price to 3dp with a 1e-2 pip and everything else to 5dp; `move_both` refuses to print a move in
  pips *or* percent and always prints both; `dash_if_none` makes a missing value an em dash that
  can never be read as a zero; `GREEK_UNITS` carries a unit sentence for every field of `Greeks`
  so `gamma_1pct` cannot reach a screen without "delta change per +1% spot"; scientific notation
  is never emitted. I tried to make it print a notional as a premium and could not: notionals
  render as `EUR 10.00mm` and money as `USD 106,430` — different shapes on purpose.
- **`app/components/badges.py` and the header.** Kind as a **word** as well as a colour, source,
  age, a page-level banner on any page holding a synthetic field, and — the detail that matters —
  a field with no provenance renders `UNKNOWN`, never `live`. The header carries snapshot id,
  asof in my timezone, provider, the count of synthetic and override fields, and a
  `NO MANUAL MARK` chip whose tooltip says "paste your grid before hedging off this screen". That
  is the discipline I asked for in Q-10 and it is on screen, not on hover.
- **The mark grid (MISS-1) works end to end.** I pasted a grid, saved it, and watched
  `surface_status` flip `INDICATIVE → MARK`, the header chip change to `MARKED: EURUSD`, and the
  book reprice off my curve. Two homes (SQLite versioned + the provider's marks file), badged
  `user_override`, never overwritten by a live pull. This was my blocking objection in Part I and
  it is delivered.
- **The reconcile box (MISS-5).** Typed 106.4 pips against `opt-9001`: returned implied 7.32% vs
  surface 7.96%, `−0.65 vol pts`, flagged `warn (>0.5 vol pts)`. Typed 130 pips: `+1.14 vol pts`,
  warned. Bad id: "nope is not an option in this book". Two hours of work, exactly as I said, and
  it is the check I would run first every morning.
- **The what-if pricer.** Resolved strike with the **delta convention named** (`delta +25.0%
  (spot_pa)`), the vol used and where it came from, premium in four units, breakeven in percent
  **and** pips **and** sigma-days with the √252 basis printed on the panel. This is what a
  pre-trade panel should look like.
- **Calibration residuals (REQ-016)** with the money sentence attached: "on a EUR 100mm 1M
  straddle one vol point is about USD 248,000 of PV — residuals are money, not housekeeping."
  Whoever wrote that understood why the requirement exists.
- **The Gamma Map** is unsigned, and the disclaimer — "OPEN INTEREST IS NOT DEALER POSITIONING…
  the side each contract was opened on is not published" — is in the panel where it will be read.

### 10.3 Wrong — these will print a plausible number that is not true

Ordered by what the error costs. "Wrong" here means wrong, not "I'd prefer".

**B-1 — The paste parser silently re-labels one pair's vols as another pair's. This is the
primary mark path and it is a P0.**

Paste the app's **own template** (`app/pages/data.py:40`, the same text used as the textarea
placeholder and loaded by the "Load template" button):

```
EURUSD
1W   7.35  -0.10  0.15
1M   7.05  -0.15  0.20
3M   7.30  -0.20  0.25
USDJPY
1M   9.25  -1.25  0.30
3M   9.60  -1.55  0.35
```

`fxgamma.data.manual.parse_grid` returns **three** marks, all labelled EURUSD:

```
EURUSD 1W atm=7.35%      <- correct
EURUSD 1M atm=9.25%      <- this is the USDJPY 1M row
EURUSD 3M atm=9.60%      <- this is the USDJPY 3M row
skipped: ["no tenor in row 'EURUSD'", "no tenor in row 'USDJPY'"]
```

Root cause, `manual.py` ~line 405: a bare pair token on its own line sets `row_pair` for **that
row only**, the row then fails the tenor test and is discarded, and `row_pair` resets to
`default_pair` on the next iteration. Subsequent rows are therefore stamped EURUSD, and because
`_stash` writes into a `(pair, tenor)` dict, the USDJPY 1M row **overwrites** the real EURUSD 1M
row in place. The true EURUSD 1M mark is not flagged, not warned, not skipped — it is gone.

What the trader sees: "SAVED — EURUSD now price off YOUR curve", header chip `MARKED: EURUSD`,
surface status `MARK`, badge `user_override` — the highest-trust state in the whole provenance
system — on a EURUSD surface marked **2.20 vol points** too high, while **USDJPY is silently not
marked at all** and stays `INDICATIVE`. I verified the mark reaches pricing: `surface.atm(1/12)`
for EURUSD returns 9.25%.

Cost: on a EUR 100mm 1M straddle, 2.2 vol points is ~USD 545,000 of PV, plus the wrong delta
through the smile, plus the wrong hedge size, plus an attribution residual nobody can explain.
And it happens on the *most careful* user — the one who marks his book before he trades.

Three fixes, all required: (a) a bare pair token on its own line sets the **sticky** pair for the
rows that follow; (b) `_stash` must never silently overwrite an existing `(pair, tenor)` — warn
and keep both for the user to resolve; (c) **"Save pasted marks" must not be reachable without
the parse preview**, or at minimum must refuse to save while `skipped` is non-empty. QA: this is
the first test `manual.py` gets, and `05_test_report.md` already flags `manual.py` as the
untested primary path.

**B-2 — The one-line answer is wrong or absent in three of its four cases.**

Driving `app.pricing.headline` against the demo book, at 22:45:

```
EURUSD | LONG GAMMA  · EUR +3.50mm per 1% · breakeven unavailable                          · costs USD 3,559 today
GBPUSD | SHORT GAMMA · GBP -3.59mm per 1% · no breakeven (you are short gamma — the move costs you) · earns USD 5,251 today
USDJPY | LONG GAMMA  · USD +0.31mm per 1% · pays above 81 pips today                        · earns JPY 206,794 today
```

Three separate defects:

- **EURUSD — the textbook long-gamma position prints no breakeven at all.** Cause:
  `signals.richness.daily_breakeven` takes a vega-weighted average vol across all live rows
  (`richness.py:112-113`), and the demo book's **spot hedge line carries `vol = NaN`**.
  `np.average` propagates the NaN regardless of the 1e-12 weight, so `sigma` is NaN,
  `gamma_theta` is NaN, and the card dies. **Every real gamma book has a spot hedge line**, so
  this is not a demo artefact — it is "the breakeven card is off whenever you are hedged". One
  line: restrict the average to option rows, or drop NaN vols before averaging. With it fixed,
  EURUSD reads BE 0.4166% / 48.5 pips, which is `σ/√365` to four figures.
- **GBPUSD — a short-gamma book does have a breakeven, and it is the number that matters most.**
  `breakeven_pct` returns `nan` for negative Γ₁ and the card says "no breakeven". Wrong: the
  breakeven of a short book is the move above which the gamma loss exceeds the theta collected —
  `sqrt(|θ_γ| / (0.005·|Γ₁|·S))` = **63 pips** on this book. That is precisely the number that
  tells me whether to buy gamma back this morning. Suppressing it is a regression on the earlier
  build, which at least printed the magnitude with the wrong verb. Take absolute values in the
  **maths** and put the sign in the **words** (§4.2).
- **USDJPY — the sentence contains two numbers of opposite sign and invites you to connect them.**
  "pays above 81 pips today · earns JPY 206,794 today". The 81 pips is struck against
  `theta_gamma = −69,074` (correct per W-15); the 206,794 is total theta including carry. Both
  are right and printing them adjacent without saying which theta the breakeven used is a trap.
  `daily_breakeven` already returns both fields — print them: *"BE 81p vs gamma-theta JPY
  −69,074/day; total theta +206,794/day incl. carry."*

**B-3 — The what-if pricer's breakeven still uses total theta, and is 8% too wide.**
EURUSD 1M ATMF call, σ 7.95%, Γ₁ EUR 1.74mm, θ USD −2,059 → the panel prints **0.451% / 52.6
pips**. The gamma-theta is −1,755, giving **0.4166% / 48.5 pips**, which is exactly `σ/√365`.
Four pips out of 48 on a daily breakeven decision. The library got this right (`theta_mode
="gamma"`); `app/pages/surface.py` (22:22) is still calling the older total-theta helper. W-15,
verbatim, live on screen.

**B-4 — Two of the four premium units are the same number computed twice.**
Every premium echo prints `106.4 pips · 0.914% of base notional · 0.914% of quote notional`
(EURUSD), `210.3 pips · 1.426% · 1.426%` (USDJPY 25dc). Both are `P/(N·S)`. The quote-ccy
notional of an FX option is `N × K`, not `N × S` — it is the amount of quote currency actually
exchanged — so `%quote` should be `P/(N·K)`. On the USDJPY 25-delta call (K 149.76 vs S 147.50)
the two differ by 1.5%. As built, the fourth unit is a copy of the third, so it cannot catch the
unit error W-12 added it to catch. *(Flagging with one caveat: if the desk's convention is
spot-based, say so on the panel — but then do not call it a fourth unit.)*

**B-5 — One identity, two homes.** `02_requirements.md` §0 is explicit: the identities live once,
in `fxgamma/portfolio/risk.py`, and no screen re-derives them. `gamma_pnl_pct` now exists in both
`risk.py` and `app/pricing.py`, and `breakeven_daily_pct` lives **only** in `app/pricing.py` —
the identity most likely to be got wrong sits in the app layer, where the golden fixture does not
reach it. That is exactly how REQ-046's factor of 100 and my own factor-of-two theta happened.
One home, imported.

**B-6 — No RR25 or BF25 z-scores anywhere.** REQ-010 requires them in the cross-pair table; the
built table (`app/pages/market.py`) carries RV, ATM, spread and an **RV** z-score only. Skew
richness is the signal on a risk-reversal book and there is no screen for it. The C-6
history-disclosure requirement ("n days behind this z") attaches to these, not to RV — RV has two
years of synthetic history and shows `n=252` honestly; the skew z-scores, which are the ones with
no history, are simply absent.

**B-7 — Delta is shown once, with no convention named.** Q-4 is SETTLED as "show both the
pair-convention delta and the hedge delta side by side, difference in base mm, convention named
in words on every delta cell". The what-if panel does name it; the **blotter and the headline do
not**. On the demo USDJPY risk reversal that difference is ~1% of notional per leg — around
USD 200k of delta on a USD 20mm leg — and it does not cancel on a skewed structure.

**B-8 — The blotter prints money as bare floats.** `PV`, `vega`, `theta`, `vanna`, `volga`,
`premium` and `P&L` are `round(x, 0)` with no currency on the cell and no thousands separator:
`-29638667.0` (JPY) sits four columns from `-72525.0` (USD), with the `ccy` column eight columns
to the right. This is the one table in the app where `fmt.py`'s own discipline is not applied,
and it is the table I scan fastest. Use `fmt_money`.

### 10.4 Would I put a hedge on off this screen?

**No. Three things stop me, precisely:**

1. **There is no hedge screen.** Pages 5 (Risk), 6 (P&L) and 7 (Lab) do not exist. There is no
   spot ladder, no hedge band, no trigger level, no clip size, no pin panel, no cut clock, no
   scenario, no decay path, no manual spot box on a risk page. The app tells me my delta and
   nothing about what to do with it. J4, J6 and J8 in the requirements' own day-in-the-life table
   are unanswered end to end.
2. **The only route to a real mark silently corrupts it (B-1)** and then badges the corrupted
   result with the system's highest-trust label. A tool that can be wrong is survivable; a tool
   that is wrong *and* certain is not.
3. **The sentence I would act on is broken in three of its four cases (B-2).** For my most common
   position it prints nothing; for a short book it refuses the number I most need; for a skewed
   book it prints two contradictory thetas in one line.

**What would change the answer.** Fix B-1 and B-2, ship the Risk page with the §4.4 ladder and
the manual spot box, and I would hedge off it **for a pair whose status reads `MARK`**, with the
standing caveat that I would cross-check the first week's hedges against my own arithmetic. I
would not hedge off an `INDICATIVE` pair at any point, and the app is already built to refuse
that if the Risk page honours the two-tier rule.

### 10.5 What is missing, ranked by what it costs me

| # | Missing | What it costs |
|---|---|---|
| 1 | **The Risk page** — ladder, hedge bands and trigger levels, pin panel with `Δ_above`/`Δ_below`/jump, cut clock, decay path | The whole reason to open the app intraday. Everything else is analysis; this is the tool. |
| 2 | **The P&L page** — attribution waterfall, the always-displayed residual ratio, and the "yesterday's published PV vs recomputed now" line | Q-10 checks 1 and 2. These are the two that decide whether I still have the app open in week three. |
| 3 | **A manual spot box on a risk screen, one keystroke away** | With 15-minute-delayed free data the very first thing I do every morning is override spot. Today the only override path is the Data page, which is REQ-068 living in exactly the wrong place. |
| 4 | **The overnight diff (MISS-8)** | The literal first question of the day, and the fastest way to catch a bad mark before it reaches a hedge. |
| 5 | **Vega by tenor bucket and gamma by expiry bucket (MISS-3)** | "How much of my gamma dies this week" is unanswerable today. One aggregate vega hides the defining risk of a gamma book. |
| 6 | **Forward points in the mark grid (CR-3 / MISS-2)** | The grid takes ATM/RR/BF only. Without forward points the flat-rate assumption puts the ATMF and the delta-neutral strike wrong at 1Y on USDJPY and the Scandies, and hedge carry (MISS-7) lands in `unexplained`. |
| 7 | **RR25 / BF25 z-scores (B-6)** and the "n days of history" disclosure on them | The skew signal, absent. |
| 8 | **The expiry-notional table (MISS-6)** | What actually pins spot. Cheap, and worth more than the entire CME adapter. |
| 9 | **10-delta wings in the mark grid** | The grid is ATM/RR25/BF25. `SmileQuotes` carries `rr10`/`bf10` and `parse_grid` handles them; the UI does not offer the columns. On a book with wings that is a real gap, though a smaller one than the eight above. |
| 10 | **Real-date tenors and a holiday calendar (CR-7)** | `ON` is still `1/365` on a Friday. Silent, systematic, and on the most leveraged line on the book. |

### 10.6 Units audit — the specific questions

- **Could I mistake a notional for a premium?** No. Notionals render `EUR 10.00mm`, money renders
  `USD 106,430` — different shapes, deliberately, and `fmt.py` documents why.
- **Pips for percent?** No. `move_both` refuses to print one without the other. Spot precision
  follows the pip: JPY 3dp, everything else 5dp.
- **Per-1-unit gamma for per-1%?** No, and this is well done: `gamma_1pct` is the only gamma on
  any screen, it is always labelled `per +1%`, and the intensive fields (`delta_pct`,
  `dual_delta`) render "n/a" at book level per v1.2 T-2 rather than printing a confident sum.
- **Vol level for vol difference?** No. `fmt_vol_pts` always appends "vol pts" and always signs.
- **The two genuine unit defects** are B-4 (two identical "different" premium units) and B-8
  (money as bare floats in the blotter). Plus CR-8: vanna is labelled with the raw contract unit
  and the honest gloss "1 unit = a 100% spot move", which is correct disclosure of a unit no
  trader uses.

### 10.7 Provenance audit

Is it obvious when a number is synthetic, stale or my own override? **Yes — this is the strongest
part of the build.** Kind as a word and a colour; source and age on every badge; a page-level
banner naming the count of simulated fields ("119 field(s) on this page are SIMULATED, not market
data. Nothing here is a mark and nothing here is tradable"); surface status `MARK` /
`INDICATIVE` on the market table, the surface page and every headline card; `UNKNOWN` never
rendering as `live`; and the `NO MANUAL MARK` header chip telling me in words not to hedge off
the screen. The staleness ladder (live / delayed / stale / EOD) is implemented and outranked
correctly by synthetic and override.

**The one hole is B-1, and it is a hole in a different dimension.** The provenance system is
completely honest about *where* a number came from and completely blind to *what* it is. A EURUSD
surface marked at the yen vol is badged `user_override / MARK` — truthfully, since the user did
type it — and there is nothing on the screen that would make me doubt it. Provenance is not a
substitute for a plausibility check: the mark grid needs the same defence W-12 asked of the
premium field, which is to compare each saved mark against the last surface for that pair and
warn above, say, 1.5 vol points of change.

---

## 11. Report to the PM

The full report is in the task response. The one-line version: **the parts that exist are better
than the spec, the screen I would actually hedge off has not been written yet, and the paste
parser will mark EURUSD at the yen vol today.** Fix B-1 before anything else; fix B-2 before the
Risk page ships, because the Risk page will inherit the same sentence.

---

## 10.8 Addendum, 22:55 UTC — Risk and P&L landed while this was being written

`app/pages/risk.py` (869 lines) and `app/pages/pnl.py` (462 lines) appeared on disk during the
final hour of this pass. `app/main.py` still imports only the five original pages, so **neither
is reachable in the running app yet** — the nav links to `/risk` and the router 404s. Judge them
when they are wired; a first read of the Risk page layout says the shape is right:

- headline, aggregate cards, skew panel, spot ladder with a **sticky-strike / sticky-delta**
  selector, gamma zones, scenario grid, **hedge-rule panel** (band as % of gross option notional,
  target delta, per-pair cost table with a manual override), **pin panel**, **expiry ladder with
  the exact cut instant and a countdown**, and a decay path with the CG-4 calendar/business
  toggle. That is J4, J6 and J8 addressed on one page.
- **Two things to fix before it ships.** (1) There is **no manual spot box and no `s` key** on the
  page — my Q-10 continuous demand and #3 in the §10.5 ranking. With delayed data the first thing
  I do on a risk screen is override spot, and the only override today is on the Data page. (2)
  The band input defaults to **15%**, which is my §2/Q-2 recommendation; amendment v1.6 ruled the
  shipping default is **25%** of gross option notional floored at a 1mm clip. One of the two is
  wrong and it should be the amendment that wins until the real user answers §9 question 3.
- The headline defect **B-2 is inherited here** — `rk-headline` uses the same `headline()` helper.
  Fixing it once fixes both pages, which is the argument for B-5.

Everything else in §10 stands as written, including B-1, which I re-checked at 22:55 and which is
unchanged.
