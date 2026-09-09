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
