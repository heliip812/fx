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
