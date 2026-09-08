# FX Gamma Desk — G3 + G10

A local Plotly Dash app for running an FX options **gamma book**: mark it, see every Greek, watch
how they move with spot, find where your gamma is concentrated, explain yesterday's P&L, and
backtest delta-hedged gamma strategies. **Free/public data only, no paid subscriptions.**

Pairs: EURUSD, GBPUSD, USDJPY (G3) plus AUDUSD, NZDUSD, USDCAD, USDCHF, USDSEK, USDNOK, and the
EURJPY / EURGBP / EURCHF crosses.

## Quickstart

```bash
git clone <this repo> && cd fx
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py                                          # -> http://127.0.0.1:8050
```

That is the whole setup. It starts with **no network access and no configuration**, on a
deterministic synthetic market, with a demo book so every screen has something in it.

```bash
python run.py --no-demo            # start with an empty book
python run.py --provider live      # try the live public data adapters
python run.py --port 8060 --db mybook.sqlite
pytest                             # 4,829 tests, ~55s, fully offline
python scripts/verify_live_sources.py   # pass/fail table for every public source
```

**There is no hosted URL.** This runs locally on purpose: your position book is your P&L, and it
stays in a SQLite file on your own machine.

## The eight screens

| Path | Screen | What it answers |
|---|---|---|
| `/` | Market Monitor | Is gamma cheap or expensive? RV vs IV, RR/BF z-scores |
| `/surface` | Vol Surface | Smile, term structure, calibration residuals |
| `/gamma-map` | Gamma Map | Where the *listed* market's gamma sits, by strike and expiry |
| `/book` | Position Book | Enter spot trades and options; CSV import/export |
| `/risk` | Risk | Greeks, spot ladder, scenario grid, **gamma zones**, hedge bands, pin risk |
| `/pnl` | P&L | Daily attribution waterfall with an honest unexplained residual |
| `/lab` | Signals & Backtest | Delta-hedged strategies, hedge-frequency sweep |
| `/data` | Data & Settings | **Your vol marks**, source status, provider toggle |

## Marking the book — read this first

Everything downstream of a vol mark is only as good as that mark, so the app is explicit about
where every number came from. Each figure carries a provenance badge: `live`, `cached`,
`synthetic`, or `user_override`. **Synthetic data is never silently substituted for live data**,
and an unbadged number renders `UNKNOWN` rather than `live`.

Resolution order is **your own marks -> live -> cache -> synthetic**. A mark you type is never
overwritten by a data pull.

### If you have OTC broker quotes
Paste your run into the grid on `/data`. It accepts tabs, commas, spaces, `%` signs, bid/ask pairs,
parenthesised negatives, transposed grids and multi-pair blocks, and it tells you every assumption
it made. Target: under 30 seconds to mark G3.

### If you do NOT have OTC access
This is the common case and the app is still useful — but be clear about what changes.

**Unaffected (exact, from your own trades):** every Greek, the spot ladder, gamma zones, hedge
bands, pin risk, P&L attribution, the expiry ladder. You typed the trades; nothing here needs a
broker.

**Affected (the vol *level*):** PV, and anything derived from it. Your best free substitutes, in
descending order of fidelity:

1. **CME FX options settlements** (`fxgamma/data/cme_options.py`) — options on FX *futures*,
   publicly settled daily. Genuinely FX vol, not a proxy. The best free mark available.
2. **Listed currency-ETF option chains** — FXE, FXB, FXY, FXA, FXC, FXF. A live smile *shape*, but
   American options on an ETF: fees, borrow and discrete strikes all put a basis between this and
   OTC vol. Good for skew shape and richness, poor as a level.
3. **CBOE FX vol indices via FRED** (EVZ and friends) — history for vol cones and z-scores. ATM
   only, and only for some pairs.

You can also simply type your own view into the grid — it is then badged `user_override`, which is
honest, and it beats being silently marked off an ETF.

`docs/04_data_sources.md` documents every source, its lag, its licence and its basis, and says
plainly which adapters are unverified.

## Documentation

| File | Contents |
|---|---|
| `docs/00_charter.md` | Scope, team roles, constraints |
| `docs/01_architecture.md` | Interface contract + amendments v1.1-v1.9 (every ruling and why) |
| `docs/02_requirements.md` | 71 user stories, trade-entry model, CSV schema |
| `docs/03_model_spec.md` | Every formula, in desk units, with model limitations |
| `docs/04_data_sources.md` | Public sources, basis and licensing |
| `docs/05_test_report.md` | Coverage, and what is *not* covered |
| `docs/06_trader_review.md` | Desk review — the morning screen, and "numbers that lie" |
| `docs/07_credit_gamma.md` | Credit extension design (v2) |

## Known limits

- **No live source is verified.** The build environment blocked every market-data host, so the live
  adapters are fixture-tested only. Run `scripts/verify_live_sources.py` on your own machine.
- **ETF chains have no history.** Yahoo serves only today's chain, so skew z-scores accumulate from
  your first run rather than arriving with back-history.
- Flat rate curves; European vanillas only; no exotics.
- The P&L residual budget (1%) holds for |dvol| <= 0.5 vol pt, |dS/S| <= 1%, dt <= 3 days.
