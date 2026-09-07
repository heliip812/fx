# Project Charter — FX Gamma Desk (G3 + G10)

**Owner:** wingyiuip812  •  **Role of this doc:** PM source of truth. Scope, roles, milestones, constraints.

## 1. Problem statement

A discretionary/systematic FX options trader running a **gamma book** in G3 (EURUSD, GBPUSD,
USDJPY) and the wider G10 needs one screen that answers, every morning and intraday:

1. *Is gamma cheap or expensive?* — implied vs realized, vol cones, skew/convexity richness.
2. *Where is the market's gamma?* — strike concentration, expiry ladder, pin/barrier zones.
3. *What do I own?* — my spot + option book, full Greeks, and how they morph with spot/vol/time.
4. *What did I make and why?* — daily P&L attributed to delta/gamma/vega/theta/vanna/volga.
5. *Would a rule have made money?* — backtest of delta-hedged gamma strategies with real costs.

## 2. Scope

**In scope (v1)**
- Pairs: G3 = EURUSD, GBPUSD, USDJPY. G10 majors = + AUDUSD, NZDUSD, USDCAD, USDCHF, USDSEK, USDNOK.
- Instruments: FX spot/forward, European vanillas (calls/puts), straddles/strangles/RR/fly as combos.
- Analytics: Garman–Kohlhagen pricing, Vanna–Volga & SABR smiles, full Greek set incl. vanna/volga.
- Position book with manual entry + CSV import, persisted locally (SQLite).
- Risk views: spot ladder, scenario grid, gamma zones, hedge bands, expiry decay.
- P&L attribution and hedge log.
- Backtester for delta-hedged straddles / gamma-carry rules.
- Delivery: Plotly **Dash** multi-page app, runs locally, no paid data.

**Out of scope (v1)** — exotics beyond single barrier (v2), live broker connectivity, multi-user
auth, tick data, credit/CDS gamma (v2 — see §7).

## 3. Hard constraints

- **Public data only, no paid subscriptions, no scraping behind paywalls.** Every source must have
  a documented public/redistributable endpoint. See `docs/04_data_sources.md`.
- **Sandbox limitation (important):** this build environment allows PyPI but **blocks outbound
  access to market-data hosts** (Yahoo, Stooq, ECB, FRED, CME all return proxy 403). Therefore:
  - Data adapters are written to the real public APIs and are **unit-tested against recorded
    fixtures**, not live calls.
  - A deterministic `SyntheticProvider` generates realistic spot/vol/OI so every screen, model and
    backtest is fully exercisable here and in CI.
  - `python scripts/verify_live_sources.py` is shipped so the user can confirm every live adapter
    from their own machine in one command.
- No look-ahead in any backtest. All signals computed on data available at decision time.
- Everything reproducible: pinned deps, seeded randomness, cached raw pulls.

## 4. Team (subagents) and ownership

| Role | Agent | Owns (exclusive write access) |
|---|---|---|
| Project Manager | *this session* | `docs/00_charter.md`, `docs/01_architecture.md`, integration, git |
| Business Analyst | `ba` | `docs/02_requirements.md` |
| Quant | `quant` | `docs/03_model_spec.md`, `fxgamma/models/`, `fxgamma/portfolio/`, `fxgamma/signals/`, `fxgamma/backtest/` |
| Data Engineer | `data` | `docs/04_data_sources.md`, `fxgamma/data/`, `scripts/verify_live_sources.py` |
| Developer | `dev` | `app/`, `fxgamma/store.py`, `run.py` |
| QA / Tester | `qa` | `tests/`, `docs/05_test_report.md` |
| Trader (reviewer) | `trader` | `docs/06_trader_review.md` |

**Rule:** an agent may *read* anything, but only *write* inside its own paths. Cross-boundary
changes are requested through the PM. The interface contract in `docs/01_architecture.md` is
frozen — changes require PM sign-off.

## 5. Milestones

- **M0 — Charter & contracts** (PM). *Done when* `00_charter.md` + `01_architecture.md` exist.
- **M1 — Requirements & model spec** (BA, Quant, Data in parallel).
- **M2 — Core library** — pricing, surfaces, Greeks, portfolio risk, signals, backtest. Unit-tested.
- **M3 — Data layer** — adapters + synthetic provider + cache + live-source verifier.
- **M4 — Dash app** — 8 pages wired to the library.
- **M5 — QA** — test suite green, numerical validation vs closed-form/FD benchmarks.
- **M6 — Trader review** — desk-realism critique; PM triages into fixes.
- **M7 — Ship** — README, quickstart, committed and pushed to
  `claude/gamma-trading-fx-dashboard-wfo9gb`.

## 6. Definition of done

- `pip install -r requirements.txt && python run.py` serves the app with synthetic data, zero config.
- `pytest` green; Greeks agree with finite-difference bumps to <1e-4 relative.
- Every screen renders with an empty book *and* with the demo book.
- No hard-coded secrets; API keys optional and read from env.

## 7. Credit gamma (v2 note)

The user also asked about credit. Public credit data is far thinner than FX: no free CDS index
option vol surface exists. Credit is deferred to v2 and designed in `docs/07_credit_gamma.md`.

**Correction to an earlier draft of this section (PM, recording my own error).** This charter
previously claimed the architecture "keeps `Underlying` abstract for this reason". That was false —
there is no `Underlying` type in `fxgamma/types.py` and never was. The types happen to be
numerically generic, which is not the same as being designed for it. Flagged by the credit analyst.

It also proposed proxying CDX/iTraxx spread vol through HYG/LQD/JNK option chains. The credit
analyst's verdict is that this does not survive contact with the numbers, and the PM accepts it:
see AMENDMENT v1.3 in `docs/01_architecture.md`.
