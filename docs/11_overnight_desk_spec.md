# Overnight Desk Spec — the leaving-the-desk ritual, the order ladder, and what "optimal band" means

**Owner:** Trader (reviewer, `trader`) · **Stage:** pre-build spec for the overnight feature
(`docs/08_overnight_gamma.md`), written before `app/pages/` gets an overnight screen.
**Reads against:** `08_overnight_gamma.md`, my own `06_trader_review.md` (Parts I and II),
`01_architecture.md` AMENDMENTS v1.1–v1.9, `fxgamma/portfolio/zones.py`, `fxgamma/conventions.py`,
`data/calendar/events.csv`.
**Owns:** this file only. Nothing here is a code change. Everything here is either a demand on the
screen, a contract-change request (CR-9…CR-16, §9), or a question for the real user (§10).

The two quant workstreams (`portfolio/overnight.py`, `portfolio/bandopt.py`,
`signals/rangeforecast.py`, `signals/levels.py`) are in flight. This document is the desk reality
they have to land inside. Where I think the frozen interfaces cannot express what the desk needs, I
say so in §9 rather than letting it be discovered after the screen is built.

I have left overnight ladders on a G10 gamma book. I am blunt on purpose.

---

## 1. Verdict, and the one fact that reframes the whole feature

**This is the right feature and the brief's own worked example teaches the user something false.**

The example in `08_overnight_gamma.md` §1 reads:

> *"Expected capture USD 4,100 against USD 900 of cost and USD 2,870 of theta."*

Three things are wrong with that sentence, and they are not cosmetic — it is the sentence the user
will read every evening.

**1.1 — The capture is not produced by the ladder. It is produced by the gamma.** Under a driftless
spot every hedge trade has zero expected P&L, so the expected total P&L over the window is
`0.5·Γ·E[ΔS²] − θ` **for any ladder, including no ladder at all**. Placing orders cannot raise the
expectation. It can only change the *shape* of the outcome and it always subtracts the cost.
Printing "expected capture 4,100 against 900 of cost" invites the reading "the ladder made 3,200",
and the honest reading is "**the ladder cost 900 and changed the distribution**". That is still a
good trade — see §4 — but the reason has to be stated correctly or the user will size it wrong and
will blame the tool on the first trending night when the ladder demonstrably *lost* him money
versus doing nothing.

**1.2 — The cost is off by roughly an order of magnitude in one direction or the other, and nobody
has said which.** Four rungs of ~1.3mm EUR is ~5.2mm traded. At the library's own per-pair table
(`zones.COST_BP["EURUSD"] = 0.2` bp round-trip ≈ **0.12 pips**) that is **USD 61**. At a
prime-of-prime or retail platform's Asia-hours all-in spread (0.5–1.0 pips) it is **USD 260–520**.
USD 900 implies about **1.7 pips all-in**, which is nobody's EURUSD. The number is not derivable
from anything in the repo. See §4.4 and CR-13: **execution cost is the single largest unmodelled
input in this feature, it is 15–40× different between account types, and this user has no OTC
broker access — so the interbank table is certainly the wrong one for him.**

**1.3 — The theta is a full day charged against a fourteen-hour window.** USD 2,870 is the golden
fixture's *calendar day* theta (amendment v1.4). The window is 17:00→07:00 London. Charged
pro-rata that is **USD 1,674**. This is W-14 from my first review, live again in the new brief's
headline example. Every money number on this screen must be struck over the window, with the
elapsed time printed next to it.

**1.4 — And here is the fact the whole feature has to be built around.**

> **Overnight you pay 58% of a day's theta and you receive about 34% of a day's variance.**

14 of 24 clock hours is 0.583 of a calendar day of decay. The London-close-to-London-open window
carries, on measurement, roughly a third of a day's variance for EURUSD — the brief's own §4
acceptance criterion says exactly this and it is the most important line in the brief. The ratio is
**~1.7**: overnight is structurally the worst part of the day to be long gamma and the best part to
be short it.

That is not an arbitrage — implied vol is quoted in calendar time and the market knows the night is
quiet, so the *level* of implied already embeds it. What it means is narrower and more useful: **the
overnight ladder has to clear a higher bar than a daytime hedge, and on a lot of nights it will not
clear it.** A screen that does not say so on the nights it does not clear it is a screen that will
lose the user money politely.

**Verdict:** build it. Lead with §1.4 and §4.6. Do not ship the brief's example sentence.

---

## 2. The leaving-the-desk ritual

This takes **six minutes**, 16:45–17:05 London, and it has to fit in six minutes or it will not
survive contact with a Wednesday. Numbers below are the EURUSD example carried through §3.

The ritual is seven steps. Each step has one question, one place on the screen it is answered, and
an abort condition. **Any abort condition means no ladder for that pair tonight** — not a smaller
ladder, no ladder — and the screen says which one fired.

### 16:45 · Step 1 — Mark (30 seconds)

Re-paste the vol grid. London close is when the OTC vol market marks; the mark you leave orders
against must be struck at the same time you leave them.

- Screen shows `surface_status` per pair. **`INDICATIVE` ⇒ no ladder for that pair.** This is the
  same two-tier rule that already governs the hedge instruction (my Q-3, honoured in the build). A
  ladder is a hedge instruction with a delay fuse on it; it gets the same gate, not a weaker one.
- The paste must run through the B-1 plausibility check: any pair whose mark moved >1.5 vol points
  since the last save is queried before it is saved. I found the parser stamping EURUSD rows with
  the yen vol; a ladder built on that would be sized 30% wrong and left unattended for fourteen
  hours. The fix is in flight; the ladder screen must not be reachable without it.

**Abort:** status not `MARK`; parse skipped any row; mark moved >1.5 vol pts unexplained.

### 16:47 · Step 2 — Position truth (45 seconds)

Three numbers per pair, from `book_greeks` on every render, never carried (v1.4 ruling 2):

- **Γ₁, signed, with the word LONG or SHORT.** `EUR +3.91mm per +1%`.
- **Delta now**, in base mm, on the hedge convention, named.
- **Gamma by expiry bucket** — specifically *how much of tonight's gamma dies at tomorrow's NY10
  cut*. This is MISS-3 and overnight it stops being a nice-to-have. An option expiring at 10:00 NY
  tomorrow contributes its full gamma tonight and none of it by lunchtime; a ladder sized on
  tonight's Γ will have over-hedged you by the time you can see the screen again. And if that
  strike is near spot, the ladder is sitting on top of a pin.

**Abort:** >25% of the pair's gamma expires at the next cut and a strike sits inside the ladder's
range. That is a pin, not a range, and it wants a person. (CR-15.)

### 16:49 · Step 3 — What kind of night is it (60 seconds)

One block, in the user's own clock, covering:

- **Window**: `17:00 BST → 07:00 BST · 14h 00m`, with the measured **variance fraction** and the
  sample size behind it (`0.34, n = 250 nights`). Never a hardcoded 14/24. Never an unlabelled
  constant.
- **Tier-3 events inside the window**, from `data/calendar/events.csv`, with times converted.
  **132 of the 409 events in the shipped calendar (32%) fall between 16:00 and 06:00 UTC.** This is
  not an edge case; a third of nights have something in them. §7 covers what changes.
- **The value-date roll at 22:00 BST** (17:00 NY) and the liquidity hole around it.
- **The Tokyo fix at 00:55 BST** (09:55 JST) for any JPY pair — inside the window, real, and absent
  from the calendar file (§7.4).
- **Asian holiday tomorrow?** A Japan holiday removes most of the window's variance. There is **no
  holiday calendar anywhere in the repo** (CR-7, still open) and for this feature that is now
  blocking, not cosmetic (CR-16).
- **Is it a Friday / pre-holiday?** Changes the window from 14 hours to 65 and changes every order's
  time-in-force (§7.5).

**Abort:** tier-3 event in the window and the book is short gamma (§5). Friday and the platform
cannot express a time-in-force that dies before the Sunday open.

### 16:52 · Step 4 — Go / no-go (30 seconds, and this is the decision)

Two numbers and one comparison, on one line:

```
forecast overnight range 30 pips (1σ)  ·  overnight breakeven 32 pips  ·  MARGINAL
crossover vol 6.74%  —  this ladder is worth leaving only if true ATM is below 6.74%
```

The **crossover vol** is the single best output of this whole feature (§6.3). It is the implied vol
at which the window's breakeven equals the forecast range — i.e. the vol at which tonight's gamma
is exactly worth its own theta. It converts a mark you cannot verify into a decision you can check:
you typed 7.05, the crossover is 6.74, CME says 7.31 — so on every available reading the night does
not pay, and the ladder is a risk-control device tonight rather than a monetisation device.

**Abort:** none. This step never blocks the ladder — §4 explains why you leave one anyway — but it
sets the expectation, and it changes the *width*: a night that does not pay gets fewer, wider rungs.

### 16:54 · Step 5 — Read the ladder (60 seconds)

Three checks, in order, on the ladder as printed (§3):

1. **Is rung 1 further from spot than the all-in spread plus a tick?** If not, you are paying to
   churn. On the numbers below, rung 1 at 15 pips against a 0.6-pip Asia spread is fine; on USDNOK
   with a 3-pip spread it would not be.
2. **Does any rung sit on a figure, on one of my own strikes, on a chatter strike, or on a level I
   know is defended?** The screen must draw them; I decide. A resting sell *at* 1.1700 is a
   different order from a resting sell at 1.1698.
3. **Does the outermost rung reach ±2σ of the forecast?** If it stops at ±1σ, a third of nights end
   outside the ladder with delta running (§3.7).

Then the anchors, each with its **measured** statistic and the control next to it (§8, and my
disagreement with how the control is defined is in §9.3).

**Abort:** the ladder needs more than 8 orders per pair. Nobody types 14 orders correctly at 17:00.
Widen instead.

### 16:56 · Step 6 — Type it, or hand it over (2 minutes)

The screen produces the order block in §3.2. Copy, type, read back. The read-back is one line:
**cumulative delta at the top rung and at the bottom rung**, in base mm. If the platform shows a
different total from the screen, something was fat-fingered and you find it in five seconds instead
of at 03:00.

If handing to a night desk, the screen produces the handover note (§3.9) — which includes what they
must *not* do.

### 16:58 · Step 7 — Exceptions and stamp (60 seconds)

- **Wake-me level** (alert only, no order) at ±2.5σ, both sides, plus the hard stop if short gamma.
- **Stamp the ladder**: id, timestamp, spot, mark, forecast, var fraction, cost assumption, every
  rung as placed. Without this stamp the morning reconciliation (§2.8) is impossible and the whole
  feature is unauditable — the same discipline as "does yesterday still say what it said yesterday"
  from my Q-10, applied to an order instead of a P&L.

### What would make me change the orders after they are written

Any one of these, checked at 16:58 and again at 17:04 before I stand up:

| Trigger | What changes |
|---|---|
| Spot moved >0.5 rung since the ladder was built | Rebuild. A ladder centred 8 pips off is a ladder with a permanent bias. |
| A tier-3 event appeared in the window (calendar refresh, unscheduled speaker) | Re-run §3; if short gamma, cancel entirely. |
| Mark moved >0.3 vol pts on the close auction | Re-run. Clips scale ~1:1 with the vol mark (§6.2). |
| The book changed — a trade printed after 16:45 | Rebuild. Never leave a ladder against a stale Γ. |
| Expiry chatter lands showing a large notional inside the ladder's range | Move the rung off the strike, or drop it. |
| The platform rejected a rung (size, level, TIF) | Re-cut the *whole* ladder, not just that rung — cumulative delta must stay right (§3.4). |
| I will not be at the desk by 07:00 | Extend the window and rebuild. The window is the TIF; the TIF is not a preference. |

### 2.8 · The other half of the ritual: 07:00 reconciliation (3 minutes)

The ladder is worthless unless it is marked afterwards, and this is what turns the feature from a
gadget into a measurement. At 07:00, against the stamp:

1. **Fills vs plan.** Which rungs filled, at what level, at what slippage vs the rung price.
2. **Delta now vs delta predicted.** The ladder predicted a cumulative delta at wherever spot is;
   the book says something. **The ratio of the two is a free, broker-independent measurement of
   whether the vol mark is right** (§6.4). Over twenty nights it is worth more than any ETF basis.
3. **Realised range vs forecast.** Feeds back into the range model and into the user's trust.
4. **Capture vs expectation**, split: gamma P&L realised by the fills, gamma P&L still sitting in
   mark-to-market, theta, cost.
5. **Cancel every unfilled order.** If the platform has no GTD, this is not optional and the screen
   says so in words.

---

## 3. The order ladder as an actual order

### 3.1 Order type — and why long and short gamma differ completely

This is the part a quant will not think about and it is the part that decides whether the ladder is
safe.

| | Long gamma | Short gamma |
|---|---|---|
| What hedging requires | **Sell into strength, buy into weakness** | **Buy into strength, sell into weakness** |
| Order type | **LIMIT** (sell limits above, buy limits below) | **STOP** (buy stops above, sell stops below) |
| You are | providing liquidity | taking liquidity |
| Slippage | zero or favourable — a limit can only fill at your price or better | guaranteed adverse, and unbounded in a gap |
| Not being filled | **safe** — you keep the gamma and the convexity | **the loss** — the stop exists precisely for the move that skips it |
| A gap through the ladder | you miss fills, you keep delta, P&L is *good* | every stop fills at the far side; loss is convex |
| Placement vs an anchor level | place **inside** the level (fill-first) | place **beyond** the level (avoid the noise touch) |
| Correct behaviour if the platform is down | nothing happens; you are long gamma | you are naked short gamma; this is the scenario that ends books |

Consequences the screen must implement, not merely mention:

- **The order type is derived from the sign of Γ₁ at that rung, not from a global setting**, and it
  is printed on every rung in words. A book can be long gamma above spot and short below it (a
  one-sided strike ladder does this routinely). Deriving the type from a book-level sign is wrong
  and will place a limit where a stop belongs.
- **Stop-market, not stop-limit**, for the short-gamma case, with the expected slippage stated. A
  stop-limit does not fill in the gap it was bought for. If the user's platform only offers
  stop-limit, the screen says so and widens the limit offset to a stated number of pips.
- **Never place a short-gamma stop inside the 21:00–22:00 BST value-date rollover band** if the
  platform can time-condition it. That hour has the widest spreads and the thinnest book of the
  night and it is where resting stops are cheapest to run.

### 3.2 The order block — exactly what the screen must output

Per pair, a monospace block that is **copy-pasteable and typeable**, with a CSV twin for platforms
that take an upload. Every rung carries, without exception:

| Field | Format | Why it must be there |
|---|---|---|
| `rung_id` | `LDR-0909-EURUSD-1/+2` | so a fill can be reconciled at 07:00 and a rejection re-cut |
| side | the word `SELL` / `BUY` | never a sign, never a colour |
| amount | **base ccy, mm, to the platform increment** | `EUR 0.50mm`. Not a percentage. Not a delta. |
| level | **whole pips**, pair precision (JPY 2dp, others 4dp) | a resting order at 1.16785 is a tell and half of platforms reject it |
| order type | `LIMIT` / `STOP` in words | §3.1 |
| TIF | `GTD 07:00 BST 10-Sep` or `GTC — CANCEL AT 07:00` | the window is the TIF |
| value date | `T+2 = 11-Sep` (and the post-roll date for rungs likely to fill after 22:00) | a hedge is a T+2 position and it rolls (MISS-7) |
| cumulative delta | base mm at that level, signed | the read-back number |
| distance | pips **and** as a fraction of the forecast σ | pips to trade, σ to judge |
| P(touch) | % with the basis printed | §3.8 |
| anchor | the level it was snapped to, the distance, and the measured stat | or `—` |

And one line under the block that is not a rung: **what you are if the whole thing fills** (§3.6).

### 3.3 Clip size and rounding to something tradeable

**Clips come from the repriced delta profile, never from Γ₁ × spacing.**

`clip_i = delta(level_i) − delta(level_{i−1})`, where `delta()` is the book repriced at that spot
under the chosen sticky convention. For a 1M ATM book the difference from the linear-Γ answer is
~3% and you would never notice. For a 1W book it is **13% smaller on the outer rung**, and for a
risk-reversal book the up-side and down-side clips are genuinely different sizes — which is not a
directional view, it is the book. Amendment v1.6's recorded 4.1% sticky-delta gamma gap lands
directly on the clip sizes here.

**Prediction: the quants will size clips off a single Γ₁ and the ladder will be symmetric on a
skewed book.** That is wrong and it is visible on the first RR position.

Rounding, in this order:

1. Compute the **cumulative target delta** at every rung from the repriced profile.
2. Round the *cumulative* target to the platform increment (default **0.05mm**, configurable).
3. Derive `clip_i = round(cum_i) − Σ round(clip_{<i})`. **Round the cumulative, derive the clip** —
   never round each clip independently, or the rounding error accumulates and the ladder is
   mis-hedged at the far end.
4. **Minimum clip**: `max(platform minimum, 0.10mm)`. If a computed clip falls below it, **do not
   place a sub-minimum rung** — widen the spacing and re-cut. An order for EUR 47k is not worth the
   ticket, the spread or the line on the screen.
5. Print the **residual** at the final rung: "cumulative 2.00mm placed vs 1.94mm required, +60k
   over-hedged at the bottom rung". Small, but it must not be silent.

Level rounding: **whole pips, snapped away from the exact figure** by default. A limit resting
exactly on 1.1600 competes with every option barrier and every stop cluster in the market; two pips
inside it is a materially better fill and costs nothing.

### 3.4 Partial fills

- **Every rung is an independent order.** No OCO between rungs on a long-gamma ladder. A fill at
  +15 must not cancel the order at +30 — the whole point is that they fill in sequence.
- A partially filled rung (platforms do this on thin books at 03:00) leaves a residual. The 07:00
  reconciliation must show *filled vs placed* per rung, not just a fill list.
- **A rejection is not a partial fill.** If the platform rejects one rung, the cumulative delta at
  every rung beyond it is wrong. The screen's rule: re-cut the whole ladder from the rejected rung
  outward. Say this on the screen, because at 16:59 the instinct is to fix the one rung.

### 3.5 The re-entry problem — and it is the biggest hole in the brief

Here is the case nobody specifies and everyone hits on night one.

You are flat at 1.1660. Spot rises to 1.1690: your two sell rungs fill, you are short 1.0mm of
hedge against 1.0mm of option delta, you are flat again — correct. **Spot comes back to 1.1660.**
Your option delta falls back by 1.0mm, so you are now **short 1.0mm of delta** — and your resting
buy orders are at 1.1645 and 1.1630, fifteen and thirty pips *below* where you need to buy.

The static ladder has hedged the outward move and has no order at the level it needs to unwind. It
has converted a round trip that should have paid you `0.5·Γ·(30p)²` twice into a short delta
position. **A one-shot ladder captures excursions; it does not capture chop.**

Two implementations, and the screen must say which one it is producing:

- **`mode="one_shot"`** — the ladder as frozen in the brief. Each level fills once. Safe, typeable,
  works on any platform, and under-monetises a mean-reverting night badly.
- **`mode="grid"`** — each fill immediately arms a child order one rung back the other way (sell at
  1.1675 ⇒ on fill, place buy 0.5mm at 1.1660). This is what a gamma scalping grid actually is, it
  captures the chop, and it **requires a platform that supports conditional / bracket / OCO-child
  orders, or a night desk**. Most retail and prime-of-prime platforms do; some do not.

`LadderRung` as frozen cannot express the child order — there is no `on_fill` and no order type at
all. **CR-9.** The screen must ask the user once which mode their platform supports and never
silently produce a one-shot ladder while describing grid economics.

### 3.6 If the whole ladder fills

Spot is at 1.1720 (or 1.1602). Every rung is done, you are **delta flat**, and:

- you have **no orders left**, and
- you are **still long EUR 3.9mm per 1% of gamma, running naked**, and
- the next 30 pips accrue delta with nothing to catch it.

The screen must print this as its own line, not leave it to be inferred:

```
IF IT ALL FILLS   1.1720 (or 1.1602) — you are FLAT, no orders remain, gamma still running.
                  Wake level 1.1760 / 1.1560.
```

And it must place the **tail rung**: one extra rung per side at ~3σ with a larger clip. For a
long-gamma book a far-out limit is free optionality — if a flash move happens you are filled at a
level you would never get in daylight. For a short-gamma book the tail rung is not optional, it is
the catastrophe stop (§5).

### 3.7 A gap through the ladder

Overnight FX gaps: BoJ, an Asian headline, the Sunday open, the 00:00 BST liquidity hole. What
actually happens to each order type:

- **Long gamma, limit orders.** A gap *through* your sell rungs may fill them all at your prices
  (good) or, on a true gap-open, fill them at **better** prices (better). The real damage is
  subtler: you were filled on the way through and then spot kept going, so you sold your gamma into
  the start of the move and hold no delta for the rest of it. Your P&L is fine; your *capture* is
  poor. The ladder cannot fix this and the screen should not pretend it can.
- **Short gamma, stop orders.** A gap means every stop fills at the far side, all at once, in the
  thinnest hour, and the loss is quadratic in the gap. This is the scenario §5 exists for.

Screen requirement: a **gap row** in the money panel — P&L of the ladder as placed under a
one-standard jump (say 1.5% instantaneous, or the largest overnight gap in the pair's own history,
whichever is larger), with **fills assumed at the worst plausible level, not at the rung price**.
The model's `p_touch` says nothing useful about a jump and must not be used to price it.

### 3.8 `p_touch` — say what it is

`P(touch)` per rung must print its basis or it will be misread. Mine, for the worked example, is the
driftless reflection result `2·Φ(−d/σ_window)` on **√252** variance (W-7 / v1.4 ruling 5), with the
window's measured variance fraction, no drift, no smile, no jumps. Print exactly that string. It is
an approximation that is too low in the tails and too high on a night with an event in it, and the
user needs to know which way it errs before he leans on it.

### 3.9 The night-desk handover note

If the orders are handed to a person rather than a platform, the screen produces a note that
includes the ladder, plus:

- **What they may not do**: do not average the levels, do not improve on the price, do not chase a
  missed rung, do not net the ladder into one ticket, do not cancel the far rungs because they look
  silly.
- **The one call condition** — the wake level and the phone number — and nothing else escalates.
- **The read-back**: cumulative delta at both extremes.
- **The value dates**, both sides of the 22:00 roll.

### 3.10 Worked example, laid out as I want to read it at 17:00

```
EURUSD   OVERNIGHT LADDER                     built 17:02 BST  ·  Wed 09 Sep 2026
──────────────────────────────────────────────────────────────────────────────────
BOOK      LONG GAMMA    EUR +3.91mm per +1%   =   EUR 33.5k of delta per pip
          delta now     EUR +0.04mm  (hedged 16:58)
          expiring at tomorrow's NY10 cut:  EUR 0.00mm of gamma  (0%)          ok

MARK      ATM 1M  7.05%   YOUR MARK, typed 17:01        CME 6E settle  7.31  (+0.26)
                                                        FXE chain      7.88  (+0.83)
                                                        EVZ (index)    7.60   n/a as ATM

WINDOW    17:00 BST -> 07:00 BST   14h 00m
          variance fraction 0.34  (measured, EURUSD, n = 250 nights)
          value-date roll 22:00 BST · Tokyo fix n/a · no tier-3 event · no Asia holiday
          not month-end · not a Friday

THE NIGHT forecast range  30 pips (1σ)   ·   68% within ±30   ·   95% within ±62
          theta over the window          USD -1,674   (58% of a day's decay
                                                       for 34% of a day's variance)
          overnight breakeven            32 pips
          GO / NO-GO                     MARGINAL  — forecast 30p vs breakeven 32p
          crossover vol                  6.74%  — worth leaving only if true ATM < 6.74%
                                                  you marked 7.05, CME says 7.31

LADDER    mode ONE-SHOT · 8 orders · all LIMIT (long gamma) · GTD 07:00 BST 10-Sep
          value date T+2 = 11-Sep (13-Sep for fills after 22:00 BST)

    #   side   amount       level     type    cum delta    dist   P(touch)   anchor
   +4   SELL   EUR 0.50mm   1.1720    limit    -2.00mm     +60p      5%      —
   +3   SELL   EUR 0.50mm   1.1705    limit    -1.50mm     +45p     14%      —
   +2   SELL   EUR 0.50mm   1.1690    limit    -1.00mm     +30p     32%      —
   +1   SELL   EUR 0.50mm   1.1675    limit    -0.50mm     +15p     62%      —
   --   SPOT                1.1660            reference
   -1   BUY    EUR 0.50mm   1.1645    limit    +0.50mm     -15p     62%      —
   -2   BUY    EUR 0.50mm   1.1630    limit    +1.00mm     -30p     32%      —
   -3   BUY    EUR 0.50mm   1.1615    limit    +1.50mm     -45p     14%      —
   -4   BUY    EUR 0.45mm   1.1602    limit    +1.95mm     -58p      5%      figure 1.1600,
                                                                             snapped 2p above
   TAIL SELL   EUR 1.00mm   1.1750    limit    -3.00mm     +90p    0.3%      free option
   TAIL BUY    EUR 1.00mm   1.1570    limit    +2.95mm     -90p    0.3%      free option

          P(touch) basis: driftless 2*Phi(-d/sigma_window), sqrt(252), no jumps, no smile.
          Clips from the repriced delta profile, not from Gamma_1pct x spacing.
          Cumulative rounded to 0.05mm; clips derived from the cumulative. Residual at
          rung -4: +50k over-hedged.
          Snapping ON for 1 of 8 rungs (tolerance 3 pips). Measured: figure anchors reverse
          within 12h in 54% of touches (n=311) vs a DISTANCE-MATCHED control at 51%
          (n=2,140). Edge +3pts, p=0.21. On this evidence snapping is cosmetic.

MONEY     expected P&L over the window, WITH this ladder            USD   -145
          expected P&L over the window, WITH NO LADDER AT ALL       USD   -145
          the difference the ladder makes to the expectation        USD      0  <- read this
          cost of the ladder, 2.3 expected fills at 0.6p all-in     USD    -69
          ------------------------------------------------------------------
          what the ladder actually buys you:
            morning delta capped at             EUR 0.50mm  (vs up to EUR 3.0mm naked)
            sd of overnight P&L                 USD  1,410  (vs USD 3,120 naked)   simulated
            P(you must be woken)                      2.1%  (vs 8.7% naked)        simulated
          three nights, this ladder vs doing nothing:
            never leaves +/-30p    (36% of nights)   ladder   -950   naked  -1,674
            out to +/-45p and back                   ladder +2,830   naked  -1,674
            trends to +60p         ( 5% of nights)   ladder   -140   naked  +4,360
                                                     and naked leaves you EUR 2.0mm
                                                     long at 07:00, which is the point

          The ladder pays you on a mean-reverting night and costs you on a trending
          one. Over many nights the two cancel exactly. You are not buying capture;
          you are buying a bounded morning.

IF IT ALL FILLS    1.1720 / 1.1602 — FLAT, no orders remain, gamma still running.
WAKE ME            1.1760 / 1.1560  (+/-2.5 sigma) — alert only, no order.
STAMPED            LDR-0909-EURUSD-1 · reconcile against this block at 07:00
```

Two things about that layout that are not decoration. **The `difference the ladder makes to the
expectation: USD 0` line is the most honest line on the screen** and it is what stops the user
over-trading the ladder. And **the three-states block is what a trader actually reasons with** — not
a Sharpe, not a utility, three numbers he can hold in his head.

---

## 4. What "optimal band" means to a trader vs what it means to a quant

The quant's answer, which I expect and which is correct as far as it goes: expected gamma P&L is
band-independent in the continuous limit; the band trades transaction cost against the variance of
the hedging error; Whalley–Wilmott / Zakamouline give a band in delta units proportional to
`(λ·S·Γ²/γ)^(1/3)`.

All of that is true. Here is what it misses, in the order it costs money.

### 4.1 The expectation is not just band-independent, it is *strategy*-independent — so stop selling the band as monetisation

Under a driftless spot, every hedge trade has zero expected P&L. Therefore the expected P&L over the
window is the same whether you leave eight orders, two orders, or none. The band cannot be optimised
for expectation because there is no expectation to optimise: **the only thing hedging does to the
expectation is subtract the cost.**

Concretely, on the worked example: the ladder makes USD 2,830 on a night that runs out to 45 pips
and comes back, and gives up USD 4,500 versus doing nothing on a night that trends 60 pips one way.
**It pays you for mean reversion and charges you for trend, and over many nights the two cancel to
the penny.** That is the whole of the band-independence result, said in a way a trader can check.

So the correct question is not "which band maximises capture" — no band does — it is: **what
distribution of overnight outcomes do I want, and what am I willing to pay for it?** Everything
below follows from that.

### 4.2 You cannot hedge in infinitesimal clips, and on this book the clip floor is the binding constraint

With Γ₁ = EUR 3.91mm per 1% at spot 1.1660, the book accrues **EUR 33.5k of delta per pip**. A
0.5mm minimum clip therefore implies a **minimum band of 15 pips**; a 1mm clip implies 30 pips.

Run the analytic optimiser at this book's costs and it will return something well inside that,
because at interbank costs it should: the total expected cost across *every plausible band* is
USD 20–120 on a night whose theta is USD 1,674. **The analytic optimum is unreachable, and the
screen must snap the band up to the clip floor and say which constraint bound it.** A `BandResult`
that reports `band_pips` without reporting whether the answer came from the optimiser or from the
clip floor is reporting a number that did not decide anything. (CR-10.)

### 4.3 The band-optimisation decision is worth about USD 60 a night. The delta-cap decision is worth thousands

Cost of a band-`h` strategy over the window, on this book at a realistic 0.6-pip all-in Asia
spread: `cost ≈ 1,835/h` USD.

| band `h` | expected fills | expected cost | morning delta if unfilled |
|---|---|---|---|
| 15 pips | 4.1 | USD 122 | EUR 0.50mm |
| 30 pips | 1.0 | USD 61 | EUR 1.00mm |
| 45 pips | 0.5 | USD 41 | EUR 1.51mm |

The entire cost range across every band a desk would consider is **USD 81**. Meanwhile the
difference in the delta you can wake up holding is **EUR 1mm**, which on a further 30-pip move is
**USD 3,000**.

**Therefore: do not ask the user for a risk-aversion coefficient. Ask him for the largest delta he
is willing to wake up holding, in millions, and derive the band from it.**

```
band_pips = max_wake_delta_base / delta_per_pip     (then floor at the clip minimum)
```

That is one dial, in units he already thinks in ("I don't want to come in more than a mio out"),
and it is the dial that actually moves the money. Run `optimal_band` alongside it as a
**cross-check and a cost display**, never as the driver. This is CR-10 and it is the most important
design decision in this document.

### 4.4 Spread is not the only cost, and the cost table in the repo is the wrong one for this user

`zones.COST_BP` is an interbank table — EURUSD at 0.2bp round-trip is about 0.12 pips, which is an
EBS/prime price in London hours in interbank size. This user **has no OTC broker access**. His
execution is a prime-of-prime or retail platform, at 03:00, in Asia. Realistically 0.4–1.0 pips
all-in on EURUSD, worse on GBP, several pips on the Scandies. That is **15–40× the table**.

Because the band scales as `cost^(1/3)`, being 25× wrong on cost is only ~3× wrong on the band — the
cube root is forgiving and that is genuinely reassuring. But the *cost line in the money panel* is
wrong by 25×, and that is the number the user judges the feature by.

And spread is not the whole cost. The overnight cost stack:

1. **Spread**, session-adjusted. Asia-hours EURUSD is ~2× London; the 21:00–22:00 rollover is
   3–10×; AUD/NZD at 06:00 Sydney is worse than at 02:00.
2. **Adverse selection on resting limits.** This is the real cost of a passive ladder and no model
   in the brief contains it. Your sell limit at 1.1675 is only filled when someone wants to buy
   through 1.1675 — that is, disproportionately at the start of a move rather than at the end of
   one. You are systematically selling your gamma to momentum. Measurable, from the same data
   `measure_reversal_stats` uses, and it belongs in the ladder's cost line, not in a footnote.
3. **Hedge carry / tom-next roll.** A spot hedge is T+2 and rolls. On a USDJPY hedge at a ~3.5%
   differential this is real money and today it lands in `unexplained` (MISS-7, still open).
4. **Financing and margin** on the hedge, which for a non-interbank account is not zero.
5. **Platform risk** — a rejected order, a disconnect, a repriced fill. Not a cost you can model,
   but a reason not to build a strategy that needs 14 orders to work.

**CR-13**: an `execution_profile` the user sets once (interbank / prime-of-prime / retail), driving
a session-multiplier table on top of `zones.COST_BP`, with the resulting all-in cost per fill
printed in pips on the ladder.

### 4.5 A gap costs more than the model says, and it costs differently by sign

Every band formula in the literature assumes a diffusion. Overnight FX is the part of the day with
the *most* jump and the *least* liquidity: central bank decisions, Asian data, the 00:00 hole, the
Sunday open. Under a jump:

- **Long gamma:** the model over-estimates the ladder's fills. Rungs are skipped, and skipped rungs
  are foregone realisation, not foregone P&L. Tolerable.
- **Short gamma:** the model under-estimates the loss badly. Stops fill at the far side, the loss is
  quadratic in the gap, and it all happens at once. The band formula's "cost" term contains a
  spread; the actual cost is a gap. §5.

The screen must therefore price the ladder under **two** regimes and show both: diffusive (the
model) and jump (empirical worst overnight move in that pair's own history). One number is a lie by
omission.

### 4.6 Being called at 3am has a real cost, and it belongs in the objective

This is the user's actual stated problem — he wants to *not be awake* — and no published band
formula contains it. Put it in explicitly:

- The screen prints **`P(intervention)`**: the probability spot breaches the wake level in the
  window. On the worked example, 2.1% laddered vs 8.7% naked.
- The user sets his own price for it. Mine is roughly **half a night's theta** — if a wake-up costs
  me USD 800 in equivalent terms plus a degraded next day, then reducing `P(intervention)` from 8.7%
  to 2.1% is worth ~USD 53 a night in expectation, which is comparable to the *entire* cost of the
  ladder. That is the correct scale of the trade-off and it is invisible in a Zakamouline band.
- Practically: the ladder should be built so that **the ordinary night requires no decision**, and
  the wake level should be genuinely rare. A screen that wakes the user twice a month gets ignored;
  a screen that never wakes him gets trusted until the night it should have.

### 4.7 A trader's utility over overnight P&L is not symmetric — and not because of risk aversion

Mean-variance is the wrong objective, for reasons that are institutional rather than psychological:

- **A large overnight loss costs more than its size.** It triggers a risk conversation, a position
  cut, possibly a forced unwind at the worst level, and it removes your ability to take the next
  trade. A gain of the same size buys you nothing extra.
- **The unit of account is the month, not the night.** One bad night that takes the month negative
  changes behaviour for three weeks.
- **You are not paid for variance you did not choose.** A long-gamma trader is *happy* with variance
  in the direction of his convexity. What he wants removed is the variance from waking up with a
  directional position he never decided to have. Those are different variances and mean-variance
  cannot tell them apart.

So the objective `bandopt` should optimise, if it optimises anything, is **mean minus λ·CVaR(5%) of
overnight P&L, plus an intervention penalty** — not mean-variance. And the practical consequence for
the ladder's *shape* is concrete and asymmetric:

- **Near rungs tighter than mean-variance suggests** — realising early P&L is worth more than the
  spread it costs, because realised is realised.
- **Far rungs wider, and free** — the tail rungs cost nothing until they fill and they fill at
  levels you would never see in daylight.
- **Never a symmetric ladder on a skewed book** (§3.3).

### 4.8 One thing the theory gets right that traders get wrong

When realised is running below implied, the instinct is "hedge more, salvage something". It is
wrong: tighter hedging on a dead night converts an uncertain small loss into a certain slightly
larger one. When the forecast range is materially below the window's breakeven, the correct output
is **fewer rungs, wider spacing, and a note that the position — not the hedging — is the problem**.
If it is persistent, the trade is to sell the gamma, and the screen should say that out loud rather
than optimising the deckchairs.

---

## 5. Short gamma — a materially different and more dangerous activity

Leaving unattended orders while short gamma is not the same feature with a sign flipped. Everything
that makes the long-gamma ladder safe is inverted: the orders are stops not limits, they take
liquidity instead of providing it, they are slipped rather than improved, **and not being filled is
the loss rather than the safe outcome**. The gamma runs against you between rungs, so the exposure
between rungs is convex and grows, and the worst execution in the market coincides exactly with the
moment you need it.

### 5.1 What the screen must do differently

1. **Change the words, not just the numbers.** The header reads `SHORT GAMMA — THESE ARE STOPS. YOU
   WILL BE SLIPPED.` Order type on every rung in words. No colour-only signalling.
2. **Price the fills at slipped levels, not at the rung price.** Default 2× the all-in spread on a
   normal night, and a separate **gap row** at 10× or at the pair's worst historical overnight move,
   whichever is worse. The long-gamma money panel may quote fills at the rung; this one may not.
3. **Print the naked scenario and the laddered scenario side by side** at ±1σ, ±2σ, ±3σ and at the
   historical worst. For short gamma the interesting column is the loss, not the capture.
4. **A catastrophe line is mandatory** — REQ-047's stop-out spot level, applied to the night: the
   single level at which the trader must be woken, the P&L there, and the size he would have to
   trade to get flat in Asia liquidity. If that size is a material share of what the pair trades at
   03:00, the screen says so.
5. **No OCO on the outermost stop, ever.** It is the only thing standing between the book and the
   gap.
6. **Whipsaw must be quantified, because it is the actual dilemma.** Tighter stops cut the tail and
   bleed on a chop night; wider stops survive the chop and hand back the tail. Expected whipsaw cost
   = expected crossings × 2 × slipped spread. **This requires a forecast of path roughness, not of
   range** — see CR-11, §9.2. Without it, short-gamma band selection is guesswork wearing a formula.
7. **Place stops beyond the anchor, never at it.** The opposite of the long-gamma rule. Figures,
   your own strikes and chatter strikes are where stops get run; sit outside them.

### 5.2 When the screen must refuse to produce a passive ladder at all

**Yes — it should refuse, and here are the conditions.** Each one produces a named refusal, not a
warning the user can click through:

| Refuse when | Why |
|---|---|
| `surface_status` is `INDICATIVE` for that pair | you cannot size a stop off an ETF's opinion of your vol; this is the existing two-tier rule and it binds harder here |
| A **tier-3 event** falls inside the window | a decision is a jump; stops are the wrong instrument for a jump, and 32% of nights have a calendar event in them |
| The loss at the **99th-percentile** overnight move, with slipped fills, exceeds the user's stated overnight loss limit | this is the whole point of having a limit |
| **>25% of the pair's gamma expires at the next cut** and a strike sits inside the ladder's range | that is a pin; delta jumps discontinuously at expiry and no resting order can hedge a discontinuity (W-1) |
| The window spans a **weekend or a market holiday** | a stop resting through a Sunday-evening gap is the single most dangerous order type in FX |
| The outermost clip exceeds a stated share of what the pair trades in that hour | you cannot get out of a size the market does not have at 03:00 |
| The platform offers **stop-limit only** and the user has not acknowledged it | a stop-limit does not fill in the gap it was bought for |

**And when it refuses, it must offer the alternatives instead of just saying no**, because the risk
does not go away when the ladder does:

- **Reduce the position** — the honest first answer, and the screen should size it: "flatten 40% of
  the short gamma and the 99th-percentile loss falls inside your limit".
- **Buy the tail** — a cheap wing or overnight strangle covering the window. For a short-gamma book
  this is usually cheaper than the expected slippage on the stops, and the screen can price it with
  the what-if pricer that already exists.
- **Alarms plus a person** — the handover note, a hard stop-loss level and a phone number.
- **Hedge to flat and accept the theta** — sometimes the answer is that you do not own the risk
  overnight.

### 5.3 The asymmetry, stated once, for the screen copy

> **Long gamma: an unfilled ladder costs you nothing. Short gamma: an unfilled ladder is the loss.**

---

## 6. Without OTC access — how much to trust the spacing

The user cannot mark to a broker curve. His vol comes from CME settlements, ETF chains, CBOE indices
or his own typed view. The question is what that does to the ladder.

### 6.1 Rank the free sources honestly, on the screen, every night

| Source | What it is | Typical basis to OTC ATM | Use it for |
|---|---|---|---|
| **CME settlement-implied** (6E/6B/6J options on futures) | a daily settlement price, model-derived, on options **on futures** | ~0.2–0.5 vol | the best free mark proxy; cross-check |
| **CBOE EVZ / JYVIX / BPVIX** | a variance-swap-style 30-day index, **not ATM** — it sits above ATM by roughly the smile convexity | ~+0.2–0.8 vol, biased **high** | ATM *history* and z-scores, never a level |
| **ETF chains** (FXE/FXB/FXY) | American options on an ETF with an expense ratio and US-hours quoting | 0.5–1.5 vol, unstable | z-scores, term shape, the Lab |
| **Your typed view** | your mark | by definition zero | the mark |

Two traps to state in the UI. **EVZ is not an ATM vol** — feeding it in as one biases the mark high,
which biases Γ *low*, which under-sizes every clip by the same percentage. And **CME options are on
futures**, so they need a futures→spot conversion before the implied vol means what you think (my
Q-8 point 3, unchanged).

### 6.2 How wrong the ladder gets if the mark is off by a vol point

This is the useful part, and the answer is reassuring for the levels and alarming for everything
else. Base case σ = 7.05%, spot 1.1660, 1M ATM.

| σ mark | Γ₁ | delta/pip | spacing (clip-floor bound) | spacing (cost-bound) | clip at 15p | overnight θ | overnight BE |
|---|---|---|---|---|---|---|---|
| 6.05% | EUR 4.56mm | 39.1k | 12.8p | −4.4% | 0.59mm | USD −1,437 | 27.1p |
| 7.05% | EUR 3.91mm | 33.5k | 14.9p | — | 0.50mm | USD −1,674 | 31.6p |
| 8.05% | EUR 3.43mm | 29.4k | 17.0p | +4.4% | 0.44mm | USD −1,911 | 36.1p |

Read it as three separate sensitivities:

- **The spacing barely moves.** In the cost-bound regime the band goes as `σ^(1/3)` — a full vol
  point is **±4.5%**, i.e. 15 pips becomes 15.7. In the clip-floor-bound regime it goes as `σ`
  — ±14%, i.e. 15 becomes 17. **Either way the error is one to two pips and it disappears into the
  whole-pip rounding.** Tell the user this plainly: *the levels are the robust part of the ladder.*
- **The clip sizes move ~14% per vol point**, one-for-one with Γ. That is a real mis-hedge but a
  survivable one.
- **The money and the decision move most.** Theta scales with σ, breakeven scales with σ, and the
  net is a difference of two large numbers.

### 6.3 The crossover vol — the one number that carries the uncertainty

Because the breakeven scales linearly with the vol mark while the forecast range (from HAR-RV) does
not scale with it at all, the **go/no-go decision is the thing a bad mark breaks**:

```
forecast overnight range        30.2 pips
overnight breakeven at 7.05%    31.6 pips     -> marginal, slightly negative
overnight breakeven at 6.05%    27.1 pips     -> the ladder pays
overnight breakeven at 8.05%    36.1 pips     -> nowhere near
crossover vol                    6.74%        -> BE == forecast exactly here
```

**A two-vol-point uncertainty in an unverifiable mark flips the recommendation from "leave it" to
"don't".** So: **yes, the screen must show the sensitivity — but on the decision, not on the
spacing.** Print the crossover vol as a first-class number next to the mark and the free-source
readings. It converts "I can't verify my vol" into "I need to be confident I'm below 6.74%", which
is a question a trader can actually answer.

### 6.4 The substitute for a broker curve: your own book, measured over twenty nights

The user cannot reconcile to a broker. He can reconcile to reality, for free, and this is the single
best idea in this document after §4.3:

- **Delta reconciliation.** The stamped ladder predicted a cumulative delta at every level. At 07:00
  the book has an actual delta at wherever spot is. `actual / predicted` is a direct, unbiased
  estimate of the **gamma error**, and therefore of the vol mark error, because Γ ∝ 1/σ. Twenty
  nights of it and you know whether you are systematically marking a point too high.
- **Range reconciliation.** Realised overnight range vs forecast, per pair, with the running bias.
  This calibrates `var_fraction` and the range model out of the user's own data.
- **Fill reconciliation.** Rungs touched vs `P(touch)`, and slippage vs assumed cost. This calibrates
  the cost model, which §4.4 says is the largest unmodelled input.

Three panels, all free, all built from data the tool already stamps. After a month they are worth
more than an ETF basis series, and they are the honest answer to "you have no broker".

---

## 7. Events and the calendar

**32% of the shipped calendar's events (132 of 409) fall between 16:00 and 06:00 UTC.** An overnight
feature that treats the calendar as an overlay rather than an input is wrong a third of the time.

### 7.1 The structural problem: a scalar variance fraction cannot describe a night with an event in it

`PassiveWindow.var_fraction` is one number for the whole window. On a BoJ night, essentially all of
the window's variance lands in a two-minute interval and the rest of the night is quieter than
normal. A single scalar spreads it evenly, which:

- **over-states** `p_touch` on the near rungs (they may never trade),
- **under-states** the probability of ending far away,
- and produces a ladder whose intermediate rungs are decoration.

**CR-12**: the window must be **segmentable** — pre-event, event, post-event, plus the 21:00–22:00
rollover and the 00:55 Tokyo fix — each with its own variance weight and its own jump component.

### 7.2 RBA / BoJ / RBNZ overnight decision

- The distribution is **bimodal, not diffusive**. Rungs get skipped.
- **BoJ has no fixed announcement time.** The shipped calendar asserts `03:00` for every BoJ
  decision. In reality the release lands anywhere from roughly 02:30 to 06:00 London, and the market
  gets progressively more nervous as it waits. **The screen must render BoJ as a window, not an
  instant**, and the calendar file should carry a `time_uncertain` flag. RBA (04:30/03:30 BST) and
  RBNZ (01:00 BST) are fixed and can be drawn as instants.
- **Long gamma:** leave a ladder, but **wider and fewer rungs**, and expect a gap rather than a
  walk. The near rungs are the ones that will not fill.
- **Short gamma: refuse** (§5.2). This is not a preference.
- Either way, **place the outermost rung outside the event's own implied move**, not at 2σ of the
  ordinary night's range.

### 7.3 US CPI at 13:30 London *tomorrow*

Nothing happens in the window, and that is exactly the point, and it is the case a HAR-RV model gets
backwards:

- **Overnight ranges compress ahead of a tier-3 event the following day.** Asia and early London sit
  on their hands. HAR extrapolates recent realised vol and will not know this. The range forecast
  needs an explicit **pre-event compression** term, negative, distinct from the positive event term
  that applies when the event is *inside* the window. `RangeForecast.components` has an `event`
  field — it must be able to go negative and the screen must show it.
- **The trading consequence is the interesting one:** you would rather **not** monetise your gamma
  cheaply into a compressed pre-event night and arrive flat into the event. Widen the band, place
  fewer rungs, keep the position intact. This is the opposite of what "more range forecast ⇒ tighter
  ladder" would suggest, and it is a real desk behaviour.

### 7.4 Month-end, and the fix that is actually inside the window

- The **WMR 16:00 London fix** is *before* the window opens, so month-end's headline flow event is a
  daytime problem — but the day's whole shape changes and the 17:00 spot you build the ladder from
  may be a fix artefact that partially retraces in the first hour. Flag it; do not centre a ladder
  on a fix print without saying so.
- **The Tokyo fix at 09:55 JST = 00:55 BST is inside the window** and is not in
  `data/calendar/events.csv` at all (I checked: zero rows mention Tokyo). For JPY pairs it is a real,
  recurring, time-stamped flow event, and on **Gotobi days** (dates divisible by 5, and month-end)
  it carries a documented USDJPY demand bias. It must be drawn on the ladder for JPY pairs, with
  the Gotobi flag. This is a genuine gap in the data layer, not a modelling nicety.
- Month-end also elevates Asian-hours flow generally. The variance fraction for a month-end night is
  not the ordinary one and should be estimated separately.

### 7.5 Long weekends and Fridays

The worst risk/reward of the week for a long-gamma book and the best for a short one:

- **Theta over ~65 hours, variance over roughly one session.** The Friday ladder is not a 14-hour
  ladder and the money panel must say `θ to Monday 07:00, 3 calendar days` — the same fix as W-14 and
  the Friday theta card.
- **Time-in-force is the whole ballgame.** A GTC order resting through the Sunday-evening reopen
  fills against a 10–15 pip spread on the first tick. For a **long-gamma limit** that is favourable
  (you sell higher than you asked). For a **short-gamma stop** it is a disaster. So:
  - Long gamma: weekend GTC is acceptable and should be labelled as a *feature*, with the tail rungs
    pushed further out to catch a gap open.
  - **Short gamma: cancel everything before the Friday close.** No stop survives the weekend. If the
    position cannot survive a weekend gap unhedged, the position is the problem.
- **Re-arm on Sunday.** The screen needs a Sunday-evening mode: same ladder, rebuilt on the reopen
  spot, because Friday's levels are stale by then.

### 7.6 Holidays

There is **no holiday calendar in the repository** (CR-7, open since my first review). For this
feature it is now load-bearing in three places: Asian holidays collapse the window's variance; a
London or New York holiday changes when the window ends and when the fills settle; and value dates
for every fill roll over holidays. **CR-16.** Until it exists, the screen must at minimum let the
user tick "Asia closed tomorrow" and halve the variance fraction, badged as a manual override.

---

## 8. Ranked: what would make me use this, and what would make me stop

### 8.1 What would make me actually use it — in order

1. **It tells me on which nights not to bother.** The go/no-go line and the crossover vol (§2.4,
   §6.3). A tool that only ever says "here is your ladder" is a tool that is right by construction
   and useful by accident. The one that says "tonight your gamma is not worth its theta; here is a
   two-rung ladder to cap the delta and nothing more" is the one I keep open.
2. **The order block is typeable in ninety seconds and reads back against my platform.** Side, size,
   level, order type, TIF, value date, cumulative delta. §3.2. If I have to derive anything at 17:00,
   I will stop using it inside a week.
3. **The delta cap is the dial, not a risk-aversion coefficient.** §4.3. I know what "I don't want to
   come in more than a mio out" means. I do not know what γ = 1e-6 means and neither does anyone
   else.
4. **The morning reconciliation.** §2.8 and §6.4. Fills vs plan, delta vs predicted, range vs
   forecast, cost vs assumed. This is the thing that makes the tool *earn* trust over a month
   instead of asserting it on day one — and, for a user with no broker curve, it is the only
   calibration mechanism he has.
5. **It refuses to produce a short-gamma ladder in the conditions in §5.2**, and offers the
   alternatives when it refuses. A tool that will happily leave stops through a BoJ is a tool I have
   to check, and a tool I have to check is one I will replace with a spreadsheet.
6. **The night is described in three states, not one expectation.** §3.10's money block. Quiet,
   typical, trending, with what the ladder does in each.
7. **Anchors carry their measurement and the control, and the default is off if the edge is not
   there.** §9.3. I will believe "figures reverse 54% vs a distance-matched control at 51%, p=0.21,
   so this is off by default" long before I believe a snapped ladder with no evidence behind it.
8. **Everything is stamped and reproducible.** The ladder as placed, with the mark, the forecast and
   the cost assumption, so tomorrow's argument is about the market and not about what the screen said.

### 8.2 What would make me stop trusting it — in order

1. **A ladder produced off an `INDICATIVE` surface.** Same rule as the hedge instruction. If I ever
   catch it doing this, everything else on the screen becomes suspect.
2. **A repeat of B-1 in this path.** A ladder sized off a mark that silently belongs to another pair
   is the same failure with fourteen unattended hours attached to it. The parser fix and the
   plausibility check are prerequisites, not follow-ups.
3. **The screen implying the ladder generates the capture.** §1.1. The first trending night where I
   would have made more doing nothing, I will work out that the tool has been telling me a
   comfortable story, and I will not come back.
4. **Clips sized off Γ₁ × spacing on a skewed or short-dated book.** §3.3. Symmetric ladder, RR
   position: that is a visible, checkable error and it says nobody repriced anything.
5. **Cost quoted from the interbank table to a user with no interbank access.** §4.4. If the cost
   line is 25× wrong, every net number on the screen is decoration.
6. **Any number in the money panel struck over a day when the window is fourteen hours** — theta,
   breakeven, variance. W-14, third time of asking.
7. **`p_touch` printed without its basis**, or a `var_fraction` that is a hardcoded constant rather
   than a measurement with an `n` attached.
8. **A "predictive model" that turns out to have direction in it.** The PM is right in §2 of the
   brief and I will hold him to it; the moment a rung is placed asymmetrically for a reason that is
   not the book's own gamma profile, the tool is a signal service and I am not using it.
9. **A one-shot ladder described with grid economics.** §3.5. If the screen quotes a capture that
   assumes re-entry and the orders do not re-enter, the backtest and the reality diverge and I will
   find out at the worst time.

---

## 9. Where I disagree with the PM's §2 steer, and the contract changes it implies

**The steer is 80% right and I would defend it against most objections.** No directional model:
correct, and the strongest sentence in the brief. Forecast the range not the direction: correct, and
it is what a gamma book actually needs. Measure levels against a control: correct in principle. Three
things are wrong or missing, in descending order of how much they cost.

### 9.1 The brief forecasts the wrong statistic — range is not what determines fills

*This is the single biggest hole and it is worth more than everything else in this section.*

`RangeForecast` gives `sigma_window`, `exp_abs_move_pips` and `quantiles`. **None of those determine
whether a ladder fills.** Two nights with an identical 40-pip range pay completely differently:

- Night A trends 40 pips in one direction: a one-shot ladder fills 2–3 rungs and captures well.
- Night B chops ±40 pips four times: a **grid** ladder fills eight times and captures four times as
  much; a **one-shot** ladder fills two rungs and then sits mis-hedged for six hours.

What decides it is a **path statistic** — the number of band crossings, equivalently the ratio of
realised variance at high frequency to the terminal move squared (an efficiency / roughness ratio).
That statistic is autocorrelated, regime-dependent and **forecastable**, and forecasting it is not
directional in any sense. It is also exactly what `p_touch`, `exp_pnl`, `exp_rehedges` and the
short-gamma whipsaw cost all silently depend on today.

**CR-11 — `RangeForecast` must carry a path term:** `exp_crossings(band_pips)` and a roughness /
efficiency estimate, with the same measured-not-asserted discipline the brief demands of levels.
Without it, every expected-P&L number in `LadderRung` and `BandResult` is a guess with a formula
around it.

### 9.2 "Measure levels against a control" — right instinct, wrong statistic and wrong control

- **Wrong statistic.** `measure_reversal_stats` measures reversal rate. A resting order does not care
  whether price eventually reverses; it cares, conditional on being *touched*, how far price runs
  **against** it before it comes back. That is the adverse selection in §4.4.2 and it is the actual
  economics of a passive rung. A level can reverse 60% of the time and still be a bad place to rest
  an order if the 40% runs 50 pips. **CR-14: `measure_fill_quality(hist, levels, horizon_h)` →
  distribution of maximum adverse and favourable excursion beyond the level, conditional on touch.**
- **Wrong control.** A *random level* control is biased: random levels sit at different distances
  from spot, so they have different touch probabilities, and the comparison confounds "this level is
  special" with "this level is close". The control must be **distance-matched** — the same rung
  distance, unsnapped. That is also the decision the trader is actually making: snap or don't.
- And the brief's own instinct is right and should be enforced: **if the measured edge does not beat
  the distance-matched control, snapping ships OFF by default and the screen prints the number
  anyway.** Say `p=0.21` on the screen. It is the most credibility-building thing the tool can do.

### 9.3 "No directional model" is right, but the ladder must still be asymmetric — from the book, not from a view

A skewed book has a genuinely asymmetric delta profile, and under sticky-delta the vol moves with
spot (amendment v1.6's recorded 4.1% gap). Clip sizes and even rung spacing therefore *should*
differ up-side vs down-side. That is not a view; it is repricing. The risk is that a reviewer sees
an asymmetric ladder and reads it as a directional signal, so **the screen must state the source of
any asymmetry in words**: `up-side clips 8% smaller — your 1.1750 calls, not a view`.

### 9.4 Contract-change requests

| CR | Where | Ask |
|---|---|---|
| **CR-9** | `LadderRung` | Add `rung_id`, `order_type` (`limit`/`stop`, derived per rung from the local gamma sign), `expires_at` (TIF), `value_date`, and `on_fill` (the child order for grid mode). Redefine `clip_base` as **derived from the repriced cumulative delta after any snapping**, and `exp_pnl` with its definition printed (marginal vs total, one-shot vs grid). Today a rung cannot be typed into a platform from the fields it carries. |
| **CR-10** | `bandopt.optimal_band` / `overnight_ladder` | Make **`max_wake_delta_base` the primary input** and derive the band from it; keep `optimal_band` as a cross-check and a cost display. `BandResult` must report **which constraint bound** (analytic optimum, clip floor, order-count cap, delta cap) — a band that came from the clip floor is not an optimisation result. Add `mode ∈ {"one_shot","grid"}`. |
| **CR-11** | `signals.rangeforecast` | Add a **path statistic**: `exp_crossings(band)` and a roughness/efficiency forecast. §9.1. |
| **CR-12** | `PassiveWindow` | Add `segments` — sub-windows with their own variance weights and jump components (pre-event / event / post-event, the 21:00–22:00 roll, the 00:55 Tokyo fix). A scalar `var_fraction` cannot describe a third of nights. `var_fraction` must also carry its sample size and be **measured per pair**, never a constant. |
| **CR-13** | `zones` / UI | An `execution_profile` (interbank / prime-of-prime / retail) and a **session cost multiplier** on top of `COST_BP`. This user has no OTC access; the interbank table is 15–40× wrong for him and it is the largest unmodelled input in the feature. All-in cost per fill printed in pips on the ladder. |
| **CR-14** | `signals.levels` | `measure_fill_quality` (adverse/favourable excursion conditional on touch) against a **distance-matched** control, replacing reversal-rate-vs-random-level as the snap decision statistic. §9.2. |
| **CR-15** | `overnight_ladder` | Must consume the **gamma profile as of the window** and refuse/flag when a material share of the pair's gamma expires at the next cut with a strike inside the ladder range. A resting order cannot hedge a delta discontinuity (W-1). |
| **CR-16** | data | **Settlement/holiday calendar** (re-raising CR-7, now blocking) and a `time_uncertain` flag on calendar events — the shipped file asserts a precise 03:00 for every BoJ decision, which has no fixed announcement time. Add the **Tokyo 09:55 JST fix** and a Gotobi flag; it is inside the window and entirely absent today. |

---

## 10. Questions only the real user can answer

Four block the feature. The rest ship as defaults.

**Blocking:**

1. **"What is your all-in cost, in pips, to trade EUR 0.5mm of spot at three in the morning — the
   number your platform actually fills you at, not the spread it shows?"** Everything in the money
   panel scales with this and the repo's table is an interbank number he does not get. (§4.4.)
2. **"Can your platform leave a conditional order — one that places a second order when the first
   fills — overnight, unattended?"** This is the difference between a one-shot ladder and a grid,
   and between capturing chop and not. (§3.5.) If no: what is the maximum number of resting orders
   he will actually type?
3. **"What is the largest delta you are willing to wake up holding, per pair, in millions?"** This is
   the band. It replaces the risk-aversion coefficient entirely. (§4.3.)
4. **"When do you leave and when are you back at the screen — and does that change on a Friday?"**
   The window is the time-in-force on every order, and a wrong one leaves stops resting through a
   Sunday open.

**Ship a default, correct later:**

5. **"Do you ever run short gamma overnight, and do you have an overnight loss limit?"** Sets the
   §5.2 refusal thresholds. If he has never run short gamma overnight, say so and the feature gets
   simpler and safer.
6. **"Is there a night desk, or is it just you and a phone?"** Decides whether §3.9 is a real output
   or dead code.
7. **"What is a 3am wake-up worth to you, in money?"** Half a night's theta is my number. It belongs
   in the objective (§4.6) and nobody else can supply it.
8. **"Which of CME settlements, ETF chains or your own view do you want as the default vol source
   when you have not typed one?"** With the ranking and the biases in §6.1 on screen.

---

## 11. Report to the PM

In the task response. One line: **the brief's own worked example attributes the capture to the
ladder, charges a full day of theta against a fourteen-hour window, and quotes a cost that matches
neither the repo's table nor this user's platform — and the range forecast the brief specifies does
not contain the statistic that decides whether a rung ever fills.**
