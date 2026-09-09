# Overnight Gamma Ladder & Band Optimisation — PM requirement brief

**Source:** user, 2026-09-09. This is a scope addition to v1 and the highest-value feature yet
requested, because it turns the tool from a monitor into something that produces an *order*.

## 1. The user's actual workflow

- They **actively monitor London hours**. That is the window in which they trade by hand.
- Outside it they are asleep. They want to leave **resting orders** so the book keeps monetising
  gamma overnight: "how many pips move to trade the delta for that following night".
- They want the band **optimised to maximise monetisation of their options**, not a rule of thumb.
- They want it informed by **gamma zones and technical levels**, with some predictive element.

So the deliverable is a concrete thing they read before going home:

> *"You are long EUR 4.0mm gamma per 1%. Leave these orders tonight: sell 1.2mm EUR at 1.1698,
> sell 1.4mm at 1.1731, buy 1.2mm at 1.1622, buy 1.4mm at 1.1589. Expected capture USD 4,100
> against USD 900 of cost and USD 2,870 of theta. 1.1731 is snapped 3 pips inside yesterday's high."*

## 2. PM steer — what we will and will not model

**We will NOT build a directional forecast of spot.** Daily FX returns are close to unforecastable,
and a directional signal is the fastest way to make this tool worse than useless while looking
sophisticated. Any component that implies "spot will go up" is out of scope.

**We WILL forecast the thing that actually pays a gamma book: the overnight RANGE.** How far spot
travels is materially forecastable — realised volatility is strongly autocorrelated, implied vol
carries information, and scheduled events shift the distribution. A gamma trader does not need to
know direction; they need to know how much it will move and where it is likely to turn. That is the
honest and useful version of "predictive model", and it maps directly onto rung spacing.

**Technical levels must be MEASURED, not asserted.** Prior-session high/low/close, round numbers,
pivots, swing points and open-interest/strike clusters enter as *candidate anchors*. Whether
snapping a rung to them improves realised capture is an empirical question, and we have a
backtester. Ship the measured reversal statistic next to the level. If it does not beat an unsnapped
ladder, say so and leave snapping off by default.

## 3. Frozen interfaces for this feature

```python
# fxgamma/portfolio/overnight.py                                     [quant-overnight]
@dataclass(frozen=True)
class LadderRung:
    level: float; side: int; clip_base: float; cum_delta_base: float
    pips_from_spot: float; p_touch: float; exp_pnl: float
    anchor: str = ""; anchor_dist_pips: float = 0.0

@dataclass(frozen=True)
class PassiveWindow:
    start: datetime; end: datetime; label: str = "London close -> London open"
    var_fraction: float = 1.0      # share of a full day's variance in this window

def passive_window(asof, tz="Europe/London", close_h=17, open_h=7) -> PassiveWindow
def session_variance_weight(start, end, pair, profile=None) -> float
def overnight_ladder(book, mkt, pair, *, window=None, rule=None, levels=None,
                     n_rungs=4, cost_bp=None) -> list[LadderRung]
def ladder_summary(rungs, book, mkt, pair) -> dict   # capture, cost, theta, net

# fxgamma/portfolio/bandopt.py                                       [quant-overnight]
@dataclass(frozen=True)
class BandResult:
    band_pips: float; band_delta_base: float; method: str
    exp_capture: float; exp_cost: float; exp_rehedges: float
    utility: float; curve: "pd.DataFrame"; note: str = ""

def optimal_band(book, mkt, pair, *, cost_bp=None, risk_aversion=1e-6,
                 horizon_days=1.0, method="zakamouline") -> BandResult
#   method in {"zakamouline", "whalley_wilmott", "empirical", "fixed_grid"}
```

```python
# fxgamma/signals/rangeforecast.py                                   [quant-signals]
@dataclass(frozen=True)
class RangeForecast:
    sigma_window: float          # stdev of the log move over the window
    exp_abs_move_pips: float
    quantiles: dict[float, float]
    components: dict[str, float] # har, implied, event, session
    basis: str

def har_rv(rv_series, *, horizon=1) -> tuple[float, dict]
def overnight_range_forecast(pair, mkt, hist, window, events=None,
                             blend="har+implied") -> RangeForecast

# fxgamma/signals/levels.py                                          [quant-signals]
def technical_levels(hist, pair, asof, *, kinds=None) -> pd.DataFrame
    # columns: level, kind, strength, age_days, source
def oi_levels(oi, spot, *, top=8) -> pd.DataFrame
def measure_reversal_stats(hist, levels, *, horizon_h=12) -> pd.DataFrame
    # MEASURED: touch count, reversal rate, mean excursion beyond, vs a random-level control
```

## 4. Acceptance

- The ladder is reproducible, costed, and every rung states why it is where it is.
- `optimal_band` is validated against the backtester's empirical sweep: the analytic optimum must
  land near the empirical argmax on synthetic data, or the discrepancy must be explained.
- Every claim about levels is accompanied by the measurement that supports it, including the
  control. No asserted efficacy.
- Overnight variance uses **session variance time**, not clock time: the London-close-to-open window
  is not 14/24 of a day's variance, and treating it as such misprices every rung.

---

# AMENDMENT — PM ruling on the trader's desk spec (binding)

Source: `docs/11_overnight_desk_spec.md`. The PM verified the load-bearing arithmetic before ruling.

## The brief's own worked example (§1) is WITHDRAWN

The trader found it wrong three ways and is right on all three. Verified:
- It charged a **full day's theta (USD 2,870) against a 14-hour window**. The correct figure is
  **USD 1,673**. This is the pro-rata theta error of W-14, made by the PM, in the brief that exists
  to prevent such errors.
- Its USD 900 cost implies ~1.7 pips all-in against the repo's own 0.12 pips table.
- It labelled the result "expected capture", attributing P&L to the ladder. Under driftless spot the
  expected P&L is the same with or without any ladder: hedging subtracts cost and reshapes the
  distribution. Outputs must be labelled as realised-vs-unrealised conversion, cost, and variance
  reduction — never as capture the ladder created.

**Do not use it as a fixture.** The trader's §2 example replaces it.

## The structural fact that reframes the feature — ACCEPTED, and it leads the screen

Overnight you pay **~58% of a day's theta (14h/24h) for only ~30-40% of a day's variance**.
PM-verified ratio: **1.72** at a 34% variance share (1.94 at 30%, 1.46 at 40%). On many nights,
holding gamma overnight is negative carry in expectation and **the ladder is risk control, not
monetisation**. The tool must not imply every night is worth trading.

Required output: a **crossover vol** — the ATM at which the window breakeven equals the forecast
range — as the go/no-go. More decision-useful than the band itself.

## Rulings on the change requests

- **CR-9/10 — `LadderRung` extended (approved).** It could not be typed into a platform. Add order
  type, time-in-force and value date, and support grid re-entry: a one-shot ladder described with
  grid economics overstates what it earns.
- **CR-11 — `RangeForecast` extended (approved), and this is the deepest finding.** Range does not
  determine fills; **path roughness does**. Two nights with identical range pay completely
  differently — a smooth trend fills each rung once, a choppy night fills them repeatedly. Add an
  efficiency ratio and an expected-crossings term, forecast and evaluated out-of-sample like vol.
- **CR-12 — event-aware variance profile (approved).** A scalar `var_fraction` cannot describe a
  night containing a BoJ decision, and **32% of shipped calendar events fall inside this window**.
- **CR-14 — snapping is measured as fill quality against a distance-matched control (approved),**
  not as reversal rate versus random levels. Fill quality is what the ladder actually cares about.
- **CR-16 — data gaps, assigned to `data`.** No holiday calendar; no Tokyo 00:55 fix; and the
  calendar asserts a precise 03:00 for BoJ, which has **no fixed announcement time**. Flag, do not
  silently use.

## Order type is derived, not chosen — ACCEPTED

Per-rung from the local gamma sign: **long gamma takes limits** (an unfilled order is safe);
**short gamma takes stop-markets** (an unfilled order *is* the loss). The trader's seven refusal
conditions for an unattended short-gamma ladder are adopted, including any tier-3 event in the
window and any weekend.

## What actually matters — ACCEPTED, and it demotes the headline feature

The trader's judgement, which the PM endorses: the whole band decision is worth roughly **USD 60 a
night** on the reference book, while the **delta cap** — the most delta the user is willing to wake
up holding — is worth thousands, and the clip floor binds before the analytic optimum does.
So `max_overnight_delta` is a first-class input and the analytic band is a refinement inside it.
**Ask the user for a delta cap, never for a risk-aversion coefficient.**

Clip sizes come from the **repriced delta profile**, not `Γ₁ × spacing`. PM measurement: the linear
approximation is only 1.9% off at 1W and 0.6% at 1M on a *symmetric* straddle, which is why it looks
harmless — the trader's 13% figure is for a **skewed** book, exactly where a symmetric ladder built
off a single Γ₁ fails. When a rung is snapped to a level, the clip must be recomputed, or the
cumulative delta is wrong at every rung beyond it.

`zones.COST_BP` is an interbank table and **this user has no OTC access**; the trader puts their
real all-in cost 15-40x higher. Cost is an explicit input with a no-OTC default, and the ladder's
sensitivity to it must be shown.

---

# AMENDMENT — forecasting results; two components RULED OUT (binding)

Source: `docs/10_forecast_evaluation.md`. Evaluated out-of-sample on the synthetic provider
(n_oos=999 per pair, 5 pairs). The negative results below are the valuable part.

## Measured, and it corrects a PM figure

The London-close-to-London-open window is **0.382 of a day's variance** against **0.583 of the
clock**. The theta/variance ratio is therefore **1.53**, superseding the PM's earlier **1.72**,
which assumed a 0.34 share. Sigma scales by 1.236. (The agent's report states 0.382 "reproduces
1.72"; it does not — 0.583/0.382 = 1.53, and its own sigma multiplier of 1.236 is consistent with
1.53. The PM has verified and 1.53 stands.) **The qualitative conclusion is unchanged and still
leads the screen:** overnight is theta-expensive relative to the gamma it can pay.

Caveat that must appear in the UI: this profile is measured on **synthetic** data. The real number
needs the user's own hourly history, and the ratio ranges 1.46-1.94 across plausible session
profiles — enough to move the go/no-go on a marginal night.

## USE

**HAR-RV.** Beats yesterday's RV by +0.703 QLIKE R² and a trailing mean by +0.087, Diebold-Mariano
t=-3.06 (p=0.004). Calibration is good in *level*, not merely in ranking: all four coverage
quantiles within ~1.5 se and E|move| within 5% on 5/5 pairs. That matters because the forecast feeds
a go/no-go decision, not just rung ordering.

**Implied vol at a FIXED 0.5 weight**, not a fitted one. The fitted weight swings 0.22-1.00 with an
honest OOS gain of -1.3% to +0.4% and nothing at p<0.08. Correctly diagnosed: the simulator's ATM is
a linear function of trailing RV, so this is a machinery pass and not a market fact. Re-fit on real
data before trusting any weight.

**Session variance time, event segmentation, and kappa/efficiency/crossings as computed outputs**
(identity verified to 1-5%; detects mean reversion at kappa=1.97 and momentum at kappa=0.32).

## DO NOT USE

**Forecasting kappa (path roughness) from daily bars.** Loses to the Brownian null on 5/5 pairs;
daily sampling recovers only 46-63% of the true crossing count. Ruling: use the **Brownian baseline**
for expected crossings, expose kappa as a user override, and state on the panel that expected fills
assume a Brownian path. Do not present forecast roughness as predictive. Revisit only with intraday
data.

**Snapping rungs to technical levels — RULED OUT as a default.** 4 of 80 cells significant after
Benjamini-Hochberg, 2 of 80 under the alternative control, **zero replication**, effect sizes
0.2-0.4 pips. This is the direct answer to the user's request to combine technical analysis on
levels: on this evidence it does not earn its place. Levels remain **displayed** as context, and
snapping stays available but off. Honest in both directions: synthetic data validates the machinery,
not the market question — `docs/10` states what the user should run on their own data and what
result would justify turning it on.

**Event uplift magnitude.** n≈25 per event type, no |t|>2. Event *segmentation* is kept (it changes
where variance sits in the window); the *size* of the uplift is not estimated.

## Method note worth preserving

The agent found and fixed **two control bugs, each of which manufactured an effect**, and reports
that the raw distance-matched control the PM's brief specified is **biased for range-proportional
level kinds**. Recorded in `docs/10` §6.1. This is exactly the failure mode that makes level studies
untrustworthy, and finding it in one's own harness is worth more than the result it overturned.

## Contract fixes (approved)

`measure_reversal_stats` gains `pair=` and a `level_panel` (a single-date frame carries no history,
so the frozen signature could not work); `oi_levels` gains an optional `pair=` so pip size is not
guessed; `fxgamma/signals/__init__.py` exports the new modules (**done by PM** — the owner had
finished).

---

# AMENDMENT — band results; the WW constant bug; overnight band DEMOTED (binding)

Source: `docs/09_hedging_theory.md`.

## A real bug in the classical formula, found and fixed

Whalley-Wilmott's `3/2` constant is for **hedge-to-edge**. Everything in this repo hedges **to
target**, whose constant is **6** — a band `4**(1/3) = 1.587x` wider. That single correction
explains essentially the whole WW-versus-empirical gap. `zones.py` had restated the formula with the
edge constant and was therefore **1.587x too tight** for the policy the repo actually runs; the PM
has fixed it to take the constant from `bandopt.POLICY_CONST` rather than restate it, because a
duplicated formula is exactly how `band_pct` and `COMPONENTS` drifted earlier on this project.

## Measured analytic-vs-empirical agreement

10-day horizon, 64 paths x 24 steps/day, common random numbers.

| | WW/emp | **Zakamouline/emp** | grid/emp |
|---|---|---|---|
| EURUSD interbank 0.2bp | 0.69 | **1.10** | 1.04 |
| EURUSD retail 5bp | 0.49 | **0.79** | 0.25 |
| USDJPY interbank 0.3bp | 0.77 | **1.23** | 1.11 |
| USDJPY retail 5bp | 0.51 | **0.81** | 0.29 |

Zakamouline drifts from slightly wide to slightly tight as cost rises because its
`asymptotic_ratio -> 1.0`: the lambda -> 0 expansion is out of regime at retail cost. Reported as a
diagnostic rather than tuned away, which is the right call.

Method note worth keeping: the empirical referee **imposes** `E[hedging error] = 0` rather than
estimating it — resolving a $200 cost difference through a $38k-sd sample mean would need ~1e5 paths
— and then *tests* the imposition (worst |z| = 1.92). That is how to make a small effect measurable
without fooling yourself.

## RULING — the overnight band is DEMOTED; the delta cap is the decision

**The objective is extremely flat: being 30% off the optimal band costs 0.1-0.9% of the gamma P&L.**
This corroborates the trader's independent estimate that the band decision is worth ~USD 60 a night.

Therefore: **use Zakamouline intraday** (~60 pips EURUSD interbank, ~180 retail); **overnight, ignore
the band and let `max_overnight_delta` bind.** The screen must present it that way. Selling band
optimisation as the answer would be selling the user the least valuable knob on the panel.

This does not contradict the trend-conditional work. Under a **random walk** the band barely matters,
which is exactly why the flat result appears; the whole value lives in the conditional case, which
`ratchet.py` is testing.

## Persistence cannot ship unclamped

Taken literally, the first-order condition gives a band multiplier of **x6.97** at phi=+0.3. Clamped
to `sqrt(R)`. The agent's conclusion is endorsed and relayed to `ratchet.py`: *that the raw answer is
absurd is the argument for a state-dependent ratchet rather than a static multiplier.*

## Crossover vols, and one flagged as a hypothesis not a result

EURUSD weeknight crossover **7.73%** against 7.96% marked -> marked above crossover, so long gamma
is **negative** overnight carry. USDJPY **10.31%** against 9.84% -> positive.

**Carried caveat, and it must be badged in the UI.** Recalibrating the hour profiles to hit the
measured 0.382 variance share on EURUSD is what flips USDJPY positive (+31,872 JPY), and that rests
on a *modelled* pair tilt in synthetic data, not a measurement. The agent flagged it as a hypothesis
rather than a result, which is correct. No pair-level overnight carry claim ships as fact until it is
measured on the user's own hourly history.

## RFC-1 (accepted, assigned to PM)

`zones.risk_aversion` and `bandopt`'s differ in units. Reconcile to one definition; until then the
two must not be compared. Recorded so the next reader does not assume they are interchangeable.
