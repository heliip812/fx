# Test Report — regression net and numerical validation

**Owner:** QA / Test Engineer (`qa`) · **Scope of this pass:** `fxgamma/types.py`,
`fxgamma/conventions.py`, `fxgamma/models/`, `fxgamma/data/`, plus shallow contract
checks on `fxgamma/portfolio/`, `fxgamma/signals/`, `fxgamma/backtest/`,
`fxgamma/store.py` and `app/`, which landed while this pass was running.
**Suite:** `tests/` (8 files, 4,306 tests). **Runtime:** 32 s.

```
PYTHONPATH=/home/user/fx python3 -m pytest tests -q            # everything, ~32s
PYTHONPATH=/home/user/fx python3 -m pytest tests -q -m "not slow"   # skips the subprocess probe
```

## 1. Result of the run

```
2 failed, 4303 passed, 1 xfailed in 31.57s
FAILED tests/test_data_layer.py::test_synthetic_market_is_reproducible_across_processes
FAILED tests/test_regressions.py::test_greeks_sum_builtin_aggregates_a_book
XFAIL tests/test_conventions.py::test_unknown_cut_does_not_silently_become_ny10
```

| File | Tests | What it protects |
|---|---:|---|
| `test_regressions.py` | 43 | the four bugs that already shipped once |
| `test_gk_numerics.py` | 3,973 | Greeks vs finite differences, parity, implied vol, delta conventions |
| `test_surfaces.py` | 63 | quote repricing, SABR recovery, no-arbitrage, pickling |
| `test_properties.py` | 100 | limits, signs, monotonicity, no-NaN |
| `test_conventions.py` | 69 | cuts, DST, day count, pips, tenors |
| `test_data_layer.py` | 34 | schemas, provenance grammar, determinism, the section-8 boundary |
| `test_golden_reference_trade.py` | 10 | amendment v1.4's golden fixture (assigned to QA) |
| `test_pending_modules.py` | 14 | shallow contract checks on the modules that landed mid-pass |

Both failures are **findings, not test bugs**. Both are reproduced below in three lines.

---

## 2. Findings, ranked by what the bug could cost

### F-1 — The synthetic market is not reproducible across processes. *(fails)*

`SyntheticProvider` seeds several generators with `self.seed ^ abs(hash(pair))`.
Python salts `str.__hash__` per process (PEP 456), so **the same seed produces a
different market in every run**: the smile wiggle (`rr25`, `bf25`, `rr10`, `bf10`),
the intraday high/low of every OHLC bar, and the entire open-interest table. Only
`atm` and the close path are stable (they come from the seeded simulation).

```
$ for i in 1 2 3; do PYTHONPATH=/home/user/fx python3 -c "
from fxgamma.data.synthetic import SyntheticProvider
p = SyntheticProvider(); q = p.smile_quotes('EURUSD')[0]
print(q.atm, q.rr25, float(p.open_interest('USDJPY')['oi'].iloc[0]))"; done
0.079669 -0.000764 47.0
0.079669 -0.001181 62.0
0.079669 -0.00087  42.0
```

Test: `tests/test_data_layer.py::test_synthetic_market_is_reproducible_across_processes`
(runs the same probe under three `PYTHONHASHSEED` values, so it fails deterministically).

**Why it is first.** The module docstring promises "bit-for-bit reproducible from the
seed", and everything demo-mode does depends on it: a backtest re-run gives different
numbers, a cached snapshot never matches a fresh pull, and the trader's first trust
check (Q-10 §1: *"does yesterday still say what it said yesterday?"*) fails on the
demo book by construction. It also makes any bug found in synthetic mode
irreproducible, which is the compounding cost.

*Fix is one line per call site:* a stable hash — `zlib.crc32(pair.encode())` — instead
of `hash(pair)`. **Owner: `data`.** Grep for `hash(` in `fxgamma/data/synthetic.py`
(three sites: `_pair_ohlc`, `smile_quotes`, `open_interest`).

### F-2 — `sum(list_of_greeks)` raises `AttributeError`. *(fails)*

`Greeks.__add__` special-cases `None` but not `0`, and `__radd__ = __add__`. The
builtin `sum` seeds with the integer `0`, so the natural book aggregation crashes:

```
>>> from fxgamma.types import Greeks
>>> sum([Greeks(pv=1.0), Greeks(pv=2.0)])
AttributeError: 'int' object has no attribute 'pv'
```

Test: `tests/test_regressions.py::test_greeks_sum_builtin_aggregates_a_book`.

`__radd__` exists for exactly one reason — to make `sum()` work — so this is a defect
in the contract type, not a style preference. It is currently latent: `portfolio/risk.py`
aggregates through pandas column sums and never hits it. It will bite the first person
who writes the obvious `sum(...)` in the backtest, the P&L page or a notebook, and it
will bite them as a stack trace on a screen, not as a wrong number. Cost is therefore
low-single-digit hours, not money — but the fix is three characters:
`if other is None or other == 0: return self`. **Owner: `quant` (types.py).**

### F-3 — A mis-typed cut silently becomes NY10. *(xfail, strict)*

`conventions.expiry_datetime` does `CUTS.get(cut, CUTS["NY10"])`. `"NY1O"`, `"TKO15"`
or an empty string prices at the New York cut without a word. On a TKY15 cross that is
**9 hours of `T` in the wrong place** and, in the hours around the cut, the difference
between an option that is alive and one that is dead — which after the v1.2 T-3 fix is
the difference between carrying its delta and not.

Marked `xfail(strict=True)` rather than left red, because "raise on an unknown cut" is
my reading of arch §7 ("never silently substitute"), not an explicit contract clause.
**PM ruling wanted.** Test: `tests/test_conventions.py::test_unknown_cut_does_not_silently_become_ny10`.

### F-4 — `store.get_store(path)` ignores `path` after the first call. *(no test; observation)*

`get_store` is a process-wide singleton and only rebinds with `fresh=True`, so
`get_store("/a.db")` followed by `get_store("/b.db")` returns the store bound to
`/a.db` — silently, and if the first was `close()`d, as a `ProgrammingError: Cannot
operate on a closed database` from somewhere unrelated. It is documented behaviour, so
it is not a red test; it is a hazard for the Data page's "switch book file" control and
for anything that hands a path in expecting to get that file. Suggest `get_store` raise
(or rebind) when asked for a different path than the live singleton's.
**Owner: `dev`.** Tests use `store.Store(path)` directly to avoid it.

### F-5 — SABR is a fit, not an interpolation. *(no test failure; sizing note)*

`SABRSurface` with `pin_atm` has two free parameters against five quoted pillars.
On a realistic G10 quote set it misses the **25d butterfly by ~0.09 vol points on a
0.32 vol point butterfly** — roughly a quarter of the quantity itself — and the 25d RR
by ~0.05 vol points. That is correct behaviour for a smile-dynamics model and wrong for
a mark. `build_surface` already defaults to vanna-volga (which reprices its quotes to
1e-15), and `test_vanna_volga_is_the_tighter_fit_to_broker_quotes` pins the ordering.
The risk is a UI toggle that lets a user mark the book on SABR without saying what it
costs. **Owner: `dev` (badge it) / `quant-models` (document the budget).**

### F-6 — The §0 breakeven identity is only true in percent. *(pinned, not a bug)*

`BE% = sqrt(|θ| / (0.005·Γ₁·S))` evaluates to `100·σ/√365`, not `σ/√365`, because
`Γ₁` is already a per-1%-move quantity. Both spellings are now pinned in
`test_atm_breakeven_identity_at_zero_rates` and in the golden fixture. Same family as
the trader's W-5 (the 100x in REQ-046) and W-7 (√365 vs √252): **every panel that
prints one of these numbers must state its units on the panel**, or someone will
compare a 0.37 to a 40 and conclude the tool is broken.

---

## 3. What is verified

### 3.1 The four shipped bugs are fixed and fenced (`test_regressions.py`)

1. **Premium-adjusted call delta underflow.** `strike_from_delta(0.25, …, "spot_pa")`
   is finite and round-trips to 1e-8 across **6 pa pairs × 3 spot levels × 6 tenors ×
   5 vols × 4 deltas × 2 cp** (~8,600 solves); the historic failure point (S = 147.5,
   3M, 9 vol, which returned `nan` off a peak reported at K = 553) now returns a strike
   between F and 1.25 F. The underflow itself is fenced directly: at `d2 ≈ −20` the
   pa call delta must stay strictly positive (the `erf` spelling returned exactly `0.0`
   below `d2 ≈ −8.3`, which is what turned the peak search's strict `>` into a tie).
   Downstream, all 9 G3+G10 surfaces build from the synthetic provider and return finite
   25d call *and* put vols at every quoted tenor. A `nan` is still legitimate for a pa
   call above `max_attainable_delta`, and the test allows exactly that case and no other.
2. **`Greeks.__add__`.** `delta_pct` and `dual_delta` aggregate to `nan`; the ten
   extensive Greeks add exactly; `+ None` is the identity.
3. **Expired options.** `year_fraction` is exactly `0.0` at the cut, one second past it
   and a week past it, for all three cuts; `is_expired` agrees; the 1-hour floor still
   applies one second *before* the cut; an expired ITM call prices to intrinsic with the
   exercise delta and zero gamma/vega/theta; ITM/OTM × call/put all checked.
4. **`rd_rf`.** Raises `KeyError` naming the missing currency, returns `(domestic,
   foreign)` in the right order for USDJPY, and does *not* raise on a genuine `0.0` rate.

### 3.2 Numerical validation (`test_gk_numerics.py`, 3,973 tests)

* **All nine Greeks against central finite differences of `gk_price`** — delta, gamma,
  vega, theta, rho_d, rho_f, vanna, volga, dual_delta — over 4 markets (1-handle,
  100-handle JPY, sub-1, near-zero domestic rate; all with asymmetric rate
  differentials) × 6 tenors (1 day … 2Y) × 4 vols × 5 moneynesses (±2.5σ) × call/put.
  Tolerance **1e-4 relative**, worst observed **3.9e-5**. Step sizes are scaled to the
  option's own volatility scale (`h_S = 0.005·S·σ√T`, `h_T = T/1000`), which is what
  makes one harness work for a 1-day 4-vol option and a 2Y 30-vol one.
* **Put-call parity** to 1e-12 relative across the same grid; delta parity
  (`Δc − Δp = e^{−rf T}` spot, `1` fwd).
* **`implied_vol` round trip** price → vol → price to 1e-9 and vol to 1e-6 over the same
  960 cases; `nan` outside the no-arbitrage band and on `nan` input; `0.0` at intrinsic.
* **All four delta conventions round-trip** through `strike_from_delta` /
  `delta_from_strike` to 1e-9 (1,536 cases); signed and unsigned delta spellings agree;
  `Δ_pa = Δ_spot − V/S` verified directly; a typo'd convention raises.
* Notional/direction scaling (extensive vs intensive), `gamma_1pct = γ·S/100` checked
  against an actual 1% spot move, and vector/scalar path equality bit-for-bit.

### 3.3 Surfaces (`test_surfaces.py`)

Vanna-volga reprices its own ATM/25RR/25BF to **1e-8 vol** for 4 pairs × 2 tenors ×
{approx, exact}, and its 10d wings to 1e-7 when quoted; the pair's own delta convention
is used (`spot_pa` pairs place wings premium-adjusted); the ATM strike is genuinely
delta-neutral (`Δc + Δp = 0` to 1e-9); ATM interpolates in total variance and
extrapolates flat; total variance is non-decreasing across 8 tenors (no calendar
arbitrage); the Breeden-Litzenberger density is non-negative and integrates to 1 ± 5e-3;
a `FlatSurface` reproduces the exact lognormal density; SABR recovers `(α, ρ, ν)` from
its own vols to 1e-5 and is bit-for-bit deterministic; all four `build_surface` methods
satisfy the `VolSurface` protocol, reject bad input, pickle without changing value, and
evaluate a 10,000-point slice well inside the ladder's budget.

### 3.4 Conventions (`test_conventions.py`)

Every pair spec is internally consistent (`symbol == base + quote`, known ccys, valid
cut and convention); **JPY pairs pip at 1e-2 and everything else at 1e-4**, and
`pip_value` is pinned as *independent of spot* so a future "fix" that multiplies by `S`
(trader W-16) fails here first; the three cuts resolve to the right local wall-clock
time in UTC and **follow their own DST**: NY10 is 15:00Z in January and 14:00Z in July,
LDN16 is 16:00Z and 15:00Z, Tokyo never moves; on 2026-03-15 London is still GMT while
New York is already EDT; `year_fraction` shows a **23-hour** step across the US spring
forward and a **25-hour** step across the autumn one; naive `asof` is treated as UTC and
a Tokyo-aware `asof` gives the same answer as its UTC equivalent.

### 3.5 Data layer (`test_data_layer.py`)

`fxgamma.data.SmileQuotes` **is** the models class (the local mirror is not in play);
quotes are decimals with ascending tenors and sane RR/BF ordering; OHLC, OI and event
frames match the frozen schemas with no look-ahead; rates are decimals; spot is FORDOM
and triangulates (`EURJPY = EURUSD × USDJPY`); every filled field is badged; **nothing
synthetic is badged live**; every `meta` key obeys the CG-7 grammar and `meta_lookup`
resolves most-specific-first with fallback; forwards satisfy covered-interest parity
against `rd_rf`; and a provider whose smile feed throws **omits** the surface and records
the failure rather than substituting another source.

### 3.6 Golden reference trade (`test_golden_reference_trade.py`)

Amendment v1.4 ruling 3, which names QA as the owner. EURUSD 1M ATM straddle, EUR 10mm
per leg, spot 1.084, σ 7.05%, rd 4% / rf 2%, DNS strike: **Γ₁ = EUR 3.914mm**,
**θ = USD −2,869/day** (not the withdrawn −5,800), Friday→Monday −8,608, breakeven
39.9 pips, and `BE = σ/√365` to 0.3%. The four numbers are cross-checked against each
other, which is the test that would have caught the 2x theta error in the original
table. Ruling 4's delta-hedged carry identity (`50·Γ₁·S·(σ_r²−σ_i²)·Δt`) is verified
against `risk.dhedge_pnl`, including that it is flat at `σ_r = σ_i` and that
`gamma_pnl_pct` at the breakeven move equals exactly one day of theta. W-7's two bases
are pinned at 39.9 vs 48.1 pips.

---

## 4. What is **not** covered, and why

| Area | Status | Why |
|---|---|---|
| **Live data adapters** (`spot_yahoo`, `spot_stooq`, `spot_ecb`, `rates_fred`, `vol_etf_options`, `vol_indices`, `cme_options`, `_http`, `cache`) | **untested** | Market-data hosts are blocked in this environment. Only fixture JSON validity is checked. **This is the largest hole in the suite:** the parsers can and should be tested offline against `fxgamma/data/fixtures/*.json` (and against a couple more fixtures that do not exist yet — a stooq CSV, an ECB XML, a FRED series, a CME settlement file). Recommend `data` ships those fixtures; a later QA pass writes parser tests, including the FXY/FXC inversion (`inverted_etf`) and the ETF→pair vol basis. |
| `fxgamma/data/manual.py` (`ManualQuoteProvider`, v1.2 T-1) | **untested** | Landed during this pass. It is now the *primary* mark path (manual → live → cache → synthetic), so it deserves the next block of QA time: paste-grid parsing, per-pair/per-tenor precedence, `user_override` badging, and that a live pull never overwrites a manual mark. |
| `fxgamma/portfolio/*`, `signals/*`, `backtest/*`, `store.py`, `app/*` | **shallow only** (14 tests) | All landed while this pass was running. Covered today: `price_book` has the CG-1 `ccy`/`fx_to_report` columns, `book_greeks` returns `nan` for the intensive Greeks, `fx_rate` raises on a missing leg, expired legs leave the aggregates, the ladder is the requested length, `pin_risk` reports a labelled *jump*, attribution closes (on a trivial t0 = t1), `hedge_suggestion` returns the pair, RV annualises on 252, the store round-trips a book and holds mark vols in `position_marks`, and `app.main` imports. Everything else is next pass. |
| Attribution against a **real** market move | **not covered** | Today's test uses the same snapshot twice, so `Σ components = total` holds trivially. The real test is two genuinely different snapshots with an unexplained-residual budget (REQ-052; the trader wants < 1% overnight). Needs a stored pair of snapshots — recommend a fixture, not a live pull. |
| Sticky-strike vs sticky-delta ladder, scenario grid, time decay | **not covered** | The skew delta `ν·∂σ/∂S` is a large fraction of total delta on an RR book (W-10b); it needs its own harness comparing the two sticky modes against a bumped-surface reprice. |
| Holiday / settlement calendar, real-date tenors (W-9, MISS-9) | **not covered — does not exist** | `TENORS` is a constant year-fraction grid and `ON = 1/365` even on a Friday. The tests pin the *current frozen* behaviour; the defect is a spec item awaiting a PM ruling, not a code bug, and a test asserting the fix would fail for the wrong reason. |
| Options on crosses (MISS-14) | **not covered — policy undecided** | EURJPY/EURGBP/EURCHF price fine off the synthetic surface; whether they are supported at all in v1 is a PM decision. |
| Property-based testing (Hypothesis) | **not used** | Not installed. The parameterised grids are a poor substitute for a shrinking search; worth adding for `implied_vol` and `strike_from_delta`, which are the two solvers most likely to have a bad corner. |
| UI / Dash callbacks | **not covered** | Out of scope this pass. |
| Performance | **one crude guard** | A 10k-point slice must complete in < 2 s. There is no benchmark for the real target (arch §4: ~1e5 `vol()` calls per refresh). |
| Numerical accuracy in the far wings | **bounded, not eliminated** | `implied_vol` is exact in *price* to ~1e-10 per unit notional; the *vol* it returns beyond ~5σ is only as good as `tol / vega`. Documented in the pricer, pinned by the round-trip test at ±2.5σ, deliberately not asserted beyond that. |

---

## 5. Where this codebase is most likely to be wrong next

Ranked by expected damage, from someone who has now read the whole model layer.

1. **Premium-adjusted deltas that are simply unattainable.** For a pa call the delta map
   is unimodal, and at long tenors and high vols the maximum attainable delta drops
   *below the 25 delta a broker quotes*. `strike_from_delta` correctly returns `nan`, and
   `vol_by_delta` correctly propagates it — which means the app will get `nan` for a
   perfectly ordinary USDJPY 2Y 25d wing. Nothing downstream is tested for that path.
   If any screen renders it as a blank cell, or worse coerces it to 0.0, that is a wrong
   vol on a real strike. **Ask: what does the Surface page draw when the wing does not
   exist?** This is the single most likely place for the *next* pa bug.
2. **Elapsed-time theta (W-14).** The morning mark is ~14 hours after the close, not a
   day. Nothing in the suite yet forces theta and carry to be charged over wall-clock
   between two snapshot timestamps. The error is ~40% of the theta bar, it lands in
   `unexplained`, and it trains the trader to ignore the residual alarm — which is the
   real cost, because the residual alarm is the thing that catches the *next* bug.
3. **The reporting-currency conversion chain (CG-1).** `fx_rate` routes through USD.
   Every cross (EURJPY, EURGBP, EURCHF) needs two legs, and `price_book` multiplies
   quote-ccy Greeks and base-ccy Greeks by *different* rates. One transposed leg is a
   silently wrong book total that still looks plausible. It is tested today only for
   "raises on a missing leg".
4. **Time on the app side.** The library's cut handling is correct and now well fenced,
   but `year_fraction` is only as good as the `asof` it is handed. A naive
   `datetime.now()` from a callback, or a date picker that yields a local date,
   reintroduces the whole DST class of bug above the library line.
5. **Z-scores with no history (C-6).** ETF chains give today's smile and no past. A
   z-score computed on a handful of self-collected days is not a z-score. If the UI does
   not print the effective sample size, someone will trade a 2-sigma that is noise.
6. **Listed-OI mechanics (W-11).** Contract multipliers, futures-vs-spot, American vs
   European, the inversion Jacobian and CME expiry instants are each a silent
   factor-of-something. Nothing in the Gamma Map is covered by this suite at all.
7. **Vanna and volga units on the cards (MISS-11).** The library's units are correct and
   documented; the risk is entirely in how they are labelled on screen. "Quote ccy per
   vol point per 1 unit of spot" means *a 100% spot move* for EURUSD. A card that prints
   the raw number without restating the unit will be misread, and vanna/volga are the
   whole P&L on a risk-reversal book.

---

## 6. Conventions for anyone extending `tests/`

* Test through the **contract-frozen public API** only (`fxgamma.types`,
  `fxgamma.conventions`, `fxgamma.models`, `fxgamma.data`). No private helpers: four
  agents are editing this repo concurrently and internal names move.
* **Offline.** `get_provider("synthetic")` and the shipped fixtures. A test that needs
  the network is a test that will be skipped forever.
* **Parameterise, do not copy-paste.** Shared grids and the finite-difference harness
  live in `tests/conftest.py`; add an axis there and every test gains it.
* **Name the test for what it protects**, not for the function it calls, and put the
  *why* in the docstring — a failing test should explain the risk without a git blame.
* **Modules that may not exist yet** get `pytest.importorskip` with a reason.
* **A failing test is a finding, not a chore.** Record it here, name the owner, and
  leave it red. `xfail(strict=True)` only where the desired behaviour is a judgement
  call the PM has not ruled on (there is exactly one, F-3).
