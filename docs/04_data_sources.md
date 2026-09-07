# 04 — Data Sources

**Owner:** data · **Covers:** `fxgamma/data/`, `data/calendar/`, `data/manual/`, `scripts/verify_live_sources.py`
**Contract:** `docs/01_architecture.md` §7 (provenance), §8 (`SmileQuotes` → `build_surface`),
AMENDMENT v1.1 CG-6/CG-7, **AMENDMENT v1.2 T-1 (manual marks are the primary vol input)**.

---

## 0. Read this first — verification status

> **No live endpoint in this repository has ever been contacted.**
> The build sandbox blocks every market-data host: the egress proxy answers `CONNECT` with
> `403` for Yahoo, Stooq, ECB, FRED, CBOE and CME. Everything below that is marked
> **UNVERIFIED** is an implementation written from the endpoint's documented/known shape and
> exercised only against recorded or hand-built fixtures. It is *not* evidence that the
> endpoint exists today, that the URL is current, or that the series ID resolves.

Run this on a machine with real network before trusting any live number:

```bash
python scripts/verify_live_sources.py            # everything, pass/fail table, exit != 0 on failure
python scripts/verify_live_sources.py --spot --rates
python scripts/verify_live_sources.py --vol-indices   # probes every EVZ/JYVIX/BPVIX candidate id
python scripts/verify_live_sources.py --cme           # probes the undocumented CmeWS routes
python scripts/verify_live_sources.py --json report.json -v
```

It reports, per source: status, latency, the row count or value actually fetched, and a
diagnosis that distinguishes **BLOCKED** (proxy/DNS/TLS — you never left the building) from
**HTTP 404** (the endpoint moved) from **NO SERIES** (the id does not exist) from **SCHEMA**
(200 OK, unparseable body). When a source passes, the script tells you which module's
`VERIFIED = False` flag to flip and reminds you to update the table below.

| Adapter | Module | Status **in this environment** | What would change it |
|---|---|---|---|
| Yahoo chart (spot) | `spot_yahoo.py` | **UNVERIFIED** — host blocked | `verify_live_sources.py --spot` passes |
| Stooq CSV (spot) | `spot_stooq.py` | **UNVERIFIED** — host blocked | same |
| ECB reference rates | `spot_ecb.py` | **UNVERIFIED** — host blocked | same |
| FRED short rates | `rates_fred.py` | **UNVERIFIED** — host blocked; **several series IDs are unconfirmed guesses** | `--rates` names which IDs resolve |
| Yahoo ETF option chains | `vol_etf_options.py` | **UNVERIFIED** — host blocked; cookie/crumb behaviour changes without notice | `--vol` |
| CBOE FX vol indices | `vol_indices.py` | **UNVERIFIED**; `EVZCLS`/`VIXCLS` are believed-good, **`JYVIX`/`BPVIX` IDs are guesses** | `--vol-indices` |
| CME settlements / OI | `cme_options.py` | **UNVERIFIED**; the CmeWS paths are undocumented and have changed before | `--cme` |
| Curated event calendar | `events.py` + `data/calendar/events.csv` | **VERIFIED offline** (loads, schema-checked, 409 rows) — but the *dates themselves* are curated and must be checked against official calendars | manual check against `LIVE_SOURCES` |
| Manual vol marks | `manual.py` + `data/manual/marks.json` | **VERIFIED offline** — round-trips, wins in `ChainProvider`, badged `user_override` | n/a (local file, no network) |
| Synthetic market | `synthetic.py` | **VERIFIED offline** — deterministic, badged `synthetic` | n/a |
| Parsers vs fixtures | `fxgamma/data/fixtures/` | **VERIFIED** — 18 parser checks pass offline | n/a |

Where I was not sure something exists, this document says so in those words. Nothing here is
presented as a fact I could not check.

---

## 1. The honest summary

The charter forbids paid data. **There is no free source of OTC FX implied vol.** Everything
else the dashboard needs (spot, OHLC history, short rates, an event calendar, listed open
interest) is obtainable for $0 at acceptable quality. The vol mark is not, and that is the
one gap that decides whether the tool is usable.

Amendment v1.2 T-1 resolves it the only honest way: **the user's own ATM / 25d RR / 25d BF
grid is the primary vol input** (`fxgamma/data/manual.py`), and every free vol source is
demoted to what it is actually good for — z-scores, cones, richness ranking, term-structure
shape. The provider chain is therefore:

```
manual  ->  live  ->  cache  ->  synthetic      (ChainProvider, v1.2 T-1)
 mark      indicative  stale     fake
```

A pair the desk has marked is priced off that mark, and a live pull can never overwrite it.
A pair with no mark falls straight through to the indicative tier, badged as such.

---

## 2. Source-by-source

Legend for **"substitute quality"**: how good this is as a stand-in for the number a bank's
risk system would use. 5 = indistinguishable, 1 = only useful as a shape.

### 2.1 Spot & OHLC history

| | Yahoo chart | Stooq CSV | ECB reference rates |
|---|---|---|---|
| **Module** | `spot_yahoo.py` | `spot_stooq.py` | `spot_ecb.py` |
| **Provides** | daily OHLC + last price, all 12 pairs | daily OHLC, all 12 pairs | one official fix/day, EUR-base |
| **Endpoint** | `https://query1.finance.yahoo.com/v8/finance/chart/EURUSD%3DX?period1=&period2=&interval=1d` | `https://stooq.com/q/d/l/?s=eurusd&i=d&d1=YYYYMMDD&d2=YYYYMMDD` | `.../stats/eurofxref/eurofxref-daily.xml`, `-hist-90d.xml`, `-hist.zip`; portal `https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A?format=csvdata` |
| **Frequency** | intraday quote; 1 bar/day | 1 bar/day | 1 fix/day, TARGET days only |
| **Lag** | ~15 min on the last price; bars settle after the 17:00 NY roll | end of day, sometimes T+1 for the last bar | published ~16:00 CET, struck at the 14:15 CET concertation |
| **Cost** | $0, no key | $0, no key | $0, no key |
| **Terms of use** | Yahoo ToS: personal, non-commercial. No redistribution licence. Undocumented endpoint — can vanish. | Free personal use, **daily hit limit per IP** (failure arrives as `200 OK` with body `Exceeded the daily hits limit`) | Free reuse **with attribution** ("Source: European Central Bank"); Data Portal is CC BY 4.0. The only source here with a real licence. |
| **Known limitations** | undocumented; aggressive throttling; occasional bad ticks and gaps on thin crosses; FX "close" is a 17:00 ET-ish snap, not a fix | same close ambiguity; no intraday; symbol coverage for crosses is patchy | **one price a day, no OHLC** — we set `open=high=low=close` and say so in the provenance note. Range estimators (Parkinson, Garman-Klass, Rogers-Satchell) must **not** run on ECB bars. Crosses are exact triangulations, useless for cross-basis work. |
| **Substitute quality** | **4/5** for a research tool. Wrong by a few pips vs a bank's official EOD; irrelevant next to the vol error. | **4/5** | **3/5** as a level (authoritative but not tradable, and 14:15 CET is not any desk's mark time); **5/5** as a revision-free history |

Fallback order in `LiveProvider`: **Yahoo → Stooq → ECB**, then the newest cached bar (badged
`cached`, with the age in the note). ECB last because a single fix cannot feed the RV
estimators.

**The thing to know about spot:** none of these is a *fix* the way WMR 16:00 London is. For
P&L attribution the trader marks at 17:00 NY (`06_trader_review.md` Q-1); a Yahoo/Stooq daily
close is close to that but not it. Sub-pip differences do not move a gamma decision; they do
show up in the attribution residual, so the residual policing must not be tuned so tight that
it flags the data source.

### 2.2 Short rates (`r_d` / `r_f`)

| | |
|---|---|
| **Module** | `rates_fred.py` |
| **Endpoint** | no key: `https://fred.stlouisfed.org/graph/fredgraph.csv?id=SOFR&cosd=&coed=` · with key: `https://api.stlouisfed.org/fred/series/observations` (`FRED_API_KEY` from env only, never hard-coded) |
| **Frequency / lag** | daily for USD (T+1); **monthly, 1–2 months in arrears** for most of the rest |
| **Cost** | $0; the API key is free |
| **Terms of use** | FRED redistributes with attribution; underlying series carry their own owners' terms (OECD/BoE/ECB) |

Series and my confidence that the ID exists, taken from `rates_fred.SERIES`:

| Ccy | IDs tried, in order | Confidence | Comment |
|---|---|---|---|
| USD | `SOFR`, `EFFR`, `DGS3MO` | **high** | the one currency FRED does properly |
| EUR | `ECBESTRVOLWGTTRMDMNRT`, `ECBMRRFR`, `IR3TIB01EZM156N` | medium | the €STR ID is long and I could **not** confirm it; `ECBMRRFR` is a policy rate, not a fixing |
| GBP | `IUDSOIA`, `IR3TIB01GBM156N` | medium | BoE IADB code mirrored on FRED — **unconfirmed** |
| JPY | `IRSTCI01JPM156N`, `IR3TIB01JPM156N` | low | monthly OECD; **no free daily TONA on FRED that I could confirm** |
| CHF | `IR3TIB01CHM156N` | low | SARON is not on FRED |
| CAD | `IRSTCI01CAM156N`, `IR3TIB01CAM156N` | low | CORRA is not on FRED |
| AUD/NZD/SEK/NOK | `IR3TIB01{AU,NZ,SE,NO}M156N` | low | OECD MEI; **several MEI series were discontinued in 2022–23 and these may be dead** |

Fallbacks, in order: (1) the next ID in the list; (2) the cache; (3) `STATIC_FALLBACK` — a
table of plausible mid-2026 policy levels, used **only** when the caller passes
`allow_static_rates=True`, badged `kind="user_override"`, note `"static fallback"`. Contract
§7 forbids dressing these as live. (4) The user's own rate override in
`ManualQuoteStore.set_rate()` (`data/manual/marks.json`), which is the recommended answer.

`NATIVE_FALLBACK` records each central bank's own free endpoint (BoE IADB, SNB `data.snb.ch`,
BoC Valet, RBA `f1-data.csv`, Riksbank SWEA, Norges Bank SDMX, BoJ time-series, ECB `EST`).
**Documented, not implemented in v1** — each needs its own parser. That is the cheapest
quality win available to v1.1 and I recommend it.

**Substitute quality: 4/5 for USD, 2/5 for everything else.** A monthly OECD 3M interbank
rate is not a discount rate. Sizing it: on a 1Y USDJPY forward a 50bp rate error moves the
forward ~0.7 yen — four big figures out (this is exactly the T-4 defect the trader caught).
For 1M–3M gamma the rate error is second-order next to the vol error, but it is not zero, and
it is why `MarketSnapshot.rd_rf` now raises instead of defaulting to 0.0.

### 2.3 Implied vol — listed ETF option chains (**INDICATIVE, never a mark**)

| | |
|---|---|
| **Module** | `vol_etf_options.py` |
| **Provides** | the only free *traded* FX smile: bid/ask/IV/OI per listed contract |
| **Endpoint** | `https://query2.finance.yahoo.com/v7/finance/options/FXE` (`?date=<unix>` per expiry); cookie+crumb via `https://fc.yahoo.com` + `https://query2.finance.yahoo.com/v1/test/getcrumb` |
| **Coverage** | FXE→EURUSD, FXB→GBPUSD, FXY→USDJPY, FXA→AUDUSD, FXC→USDCAD, FXF→USDCHF (+UUP for DXY). NZD/SEK/NOK and the crosses have **no ETF at all** — `VOL_BETA` scales a proxy ETF's vol by a desk rule of thumb; anything using it is badged "MODELLED, not observed". |
| **Frequency / lag** | quotes only during US listed hours 09:30–16:00 ET; stale outside them; cached 15 min |
| **Cost** | $0, no key |
| **Terms of use** | Yahoo personal/non-commercial, no redistribution; rate-limited; crumb requirement changes without notice |

**The basis, in detail. This is the section the trader asked for.**

1. **American vs European.** Listed ETF options are American; OTC FX vanillas are European.
   Yahoo's `impliedVolatility` is an American (binomial) IV. The early-exercise premium is
   small for low-carry windows but is **one-sided** and largest exactly where dividends/carry
   and moneyness make exercise attractive. We build only from OTM contracts, where the two
   conventions agree to well inside the bid/ask — but "inside the bid/ask" on an FXE wing is
   not a tight statement (see 4).
2. **The ETF is not the currency.** FXE holds a euro deposit **net of a 0.40%/yr expense
   ratio** and passes through euro interest; the trust's NAV therefore drifts against spot.
   Vol is first-order invariant to a deterministic drift, but the drift biases the *forward*,
   and moneyness measured off a wrong forward biases the **skew** — which is where RR and BF,
   and therefore all vanna/volga, come from. We recover the forward from **put-call parity**
   rather than assuming `F = S·exp((r−q)T)`, which removes most but not all of this.
3. **Fees and borrow.** Shorting the ETF (the natural hedge for a market maker quoting the
   wings) carries a borrow cost that is embedded in the option prices as a synthetic dividend.
   It is not observable and it does not net out of a risk reversal.
4. **Discrete strikes and wide markets.** FXE strikes are $0.50–$1 apart, i.e. **~0.5% of
   spot**; a genuine 10-delta wing often simply does not exist, and where it does the market
   can be 30–60% wide relative to mid (`_MAX_REL_SPREAD = 0.60` is not a typo — that is what
   these wings look like). **The 25d BF, and therefore the entire volga/vanna of the book, is
   fitted to two poor prints.** The 10d numbers, where we emit them at all, are worse.
5. **Quote staleness.** The chain stops updating at 16:00 ET while the OTC pair trades 24h.
   A European morning read of an FXY chain is a 14-hour-old picture of yen vol — through the
   entire Tokyo session, which is when yen vol actually happens.
6. **Inversion.** FXY is USD-per-JPY; USDJPY is JPY-per-USD. Vol is invariant to inversion at
   first order, **skew is not** (an FXY call is a USDJPY put). `conventions.PAIRS[...].inverted_etf`
   drives the reflection `k → −k`. Get this wrong and the RR sign flips — which would put the
   yen risk reversal on the wrong side of the market, the single most dangerous silent error
   in this whole layer.
7. **Expiry mismatch.** Listed expiries are third Fridays (plus weeklies) with a 16:00 ET
   cut; OTC tenors are 1M/2M/3M at the 10:00 NY cut. We return the listed `T` as observed and
   let `build_surface` interpolate. We never relabel a 24-day listed expiry as "1M".
8. **"FXY-implied vol is USDJPY vol" is only true to first order.** Beyond inversion, the ETF
   wraps a US-listed, USD-settled, exchange-cleared instrument around a foreign deposit: its
   implied vol contains a little US listed-market microstructure (pin effects into monthly
   expiry, retail flow, the 16:00 close auction) that USDJPY does not have, and misses the
   Tokyo-hours risk premium that USDJPY does.

**Observed magnitude:** roughly **0.3–1.0 vol points** away from the OTC ATM in G3, with the
sign varying; the skew error is proportionally worse. Sizing it the way the trader did: on a
EUR 100mm 1M straddle one vol point is ~USD 248,000 of PV, so half a point of basis is
**USD ~124k of mark error on one position, every day**, and it propagates into delta (through
the smile), into hedge size and into the attribution residual.

**Substitute quality: 1/5 as a mark, 4/5 as a z-score input.** This is the whole reason for
amendment v1.2 T-1.

### 2.4 Implied vol history — CBOE FX vol indices

| | |
|---|---|
| **Module** | `vol_indices.py` |
| **Provides** | multi-year daily 30-day implied-vol history — the input the cones and richness z-scores need, which the chains cannot give (no free historical chains anywhere) |
| **Endpoints** | FRED mirror `https://fred.stlouisfed.org/graph/fredgraph.csv?id=EVZCLS`; CBOE `https://cdn.cboe.com/api/global/us_indices/daily_prices/EVZ_History.csv` (**pattern inferred from the VIX file — not a confirmed URL**) |
| **Frequency / lag** | daily close, T+1 |
| **Cost / ToS** | $0; FRED redistributes with attribution, indices are CBOE IP, free for non-commercial reference |

Honest ID status:

* **`EVZCLS`** (EuroCurrency ETF Volatility Index, 30-day IV of **FXE options**) — I am
  confident this exists on FRED. It inherits the *entire* ETF basis in §2.3.
* **`JYVIX` / `BPVIX`** (yen, sterling) — CBOE discontinued these some years ago and **I could
  not confirm a live FRED ID for either**. The entries in `vol_indices.CANDIDATES` are
  explicitly `confidence="low"` **guesses to be probed**, not facts.
  `verify_live_sources.py --vol-indices` probes every candidate (FRED id *and* CBOE ticker)
  and prints which resolve. Do not treat their presence in the code as evidence.
* **`VIXCLS`, `OVXCLS`, `GVZCLS`** (equity/oil/gold) — confident these exist; cheap and
  genuinely useful regime overlays.

Until a JPY/GBP index resolves, the fallback is `proxy_from_evz()` — EVZ × a fixed beta —
which is a **regime proxy only**, must be badged `kind="synthetic"`, note `"EVZ beta proxy"`,
and carries none of the pair-specific event risk (BoJ, gilt stress) that makes JPY/GBP vol
interesting. **Substitute quality: 3/5 for EUR history, 1/5 for the beta proxies.**

### 2.5 Listed open interest by strike (Gamma Map)

| | |
|---|---|
| **Module** | `cme_options.py` |
| **Provides** | exchange-official OI by strike/expiry — the only free picture of where listed FX gamma sits |
| **Endpoints (all unconfirmed)** | product slate `https://www.cmegroup.com/CmeWS/mvc/ProductSlate/V2/List?group=fx&pageSize=200`; settlements `.../CmeWS/mvc/Settlements/Options/Settlements/{productId}/OOF?monthYear=&tradeDate=`; VOI export `.../CmeWS/exp/voiProductDetailsViewExport.ctl?media=csv&...`; bulletin `https://www.cmegroup.com/ftp/pub/settle/stlcur` |
| **Frequency / lag** | once a day, **T+1** (previous session's settlement) |
| **Cost / ToS** | $0 for personal reference; redistribution/commercial use needs a licence. Be polite: one request per product per day, cached 12h. |

Caveats that matter more than the plumbing:

* **Product IDs are not public knowledge.** `PRODUCT_ID_HINTS` is deliberately **empty** —
  inventing numeric ids would be worse than failing loudly. They are discovered at runtime
  from the product slate and cached.
* **Options on futures, not on spot.** Strikes are futures strikes; `to_spot_strikes()`
  converts with the snapshot's CIP forwards. For 1M–3M G10 that is a few tenths of a percent
  — small, but enough to move a strike between two big figures on the Gamma Map.
* **Convention flips.** CME quotes USD per foreign unit, so 6J/6C/6S are the reciprocal of our
  FORDOM pair: `K_pair = 1/K_cme` and `cp` flips with it. 6J strikes are also published in
  points (`0.006700` vs `670`); `normalise_strikes()` rescales by powers of ten checked
  against spot rather than trusting a constant.
* **OI is not positioning** (already risk R-14): it is gross, it says nothing about who is
  long or short gamma, and CME is a *minority* of the FX options market — the OTC market is
  many times larger and completely invisible here. The Gamma Map is "where listed strikes
  cluster", never "where the market is short gamma".
* `parse_stlcur()` is the most speculative parser in the repo: the bulletin's layout is
  undocumented. It raises rather than returning junk.

**Substitute quality: 3/5 for listed strike clustering, 1/5 as a proxy for market positioning.**

### 2.6 Event calendar

| | |
|---|---|
| **Module / file** | `events.py` + `data/calendar/events.csv` (CG-6) |
| **Schema (frozen)** | `date, time_utc, ccy, event, importance, source`, `importance ∈ {1,2,3}` |
| **Contents (current file)** | **409 rows, 2026-01-01 → 2027-12-31; 231 rows in the next 12 months** |
| **Coverage** | all ten G10 central-bank decisions (FOMC, ECB, BoJ, BoE, SNB, BoC, RBA, RBNZ, Riksbank, Norges Bank) + FOMC minutes; US NFP/CPI/PCE/ISM/retail sales; EZ flash HICP + flash PMIs; UK CPI + labour market; Japan national CPI + Tankan; listed option expiries and month-end fixes |
| **Cost** | $0 — it is a file in the repo, user-editable |

**These dates are curated, not fetched, and must be checked against the official calendars
before you trade around them.** `events.LIVE_SOURCES` lists the authoritative page for each
(federalreserve.gov, ecb.europa.eu, boj.or.jp, bankofengland.co.uk, snb.ch,
bankofcanada.ca, rba.gov.au, rbnz.govt.nz, riksbank.se, norges-bank.no, Eurostat, ONS,
stat.go.jp). Every row carries its confidence in the `source` column:

* `rule:...` — deterministically derived and **exact**: NFP = first Friday 08:30 ET,
  listed expiry = third Friday 16:00 ET, month-end fix = last business day (all DST-aware).
* `approx:<host>` — hand-entered central-bank dates, or a "usual day of the month" rule
  (US CPI ≈ nearest weekday to the 12th, UK CPI ≈ third Wednesday, Japan CPI ≈ third Friday,
  EZ flash HICP = last working day). **Verify these.** The Riksbank and Norges Bank dates in
  particular are my best reconstruction and I could not check them against any source.

`date` is the **UTC** date: Japan's 08:30 JST CPI is stored as `23:30` on the *previous* UTC
day. Importance policy: **3** = every G10 rate decision plus the headline inflation print of
each G3 economy (US CPI, US NFP, EZ flash HICP, UK CPI, Japan CPI); **2** = everything else
that moves G10 spot on the day; **1** = background (unused so far).

Why a curated file at all: there is no free, stable, machine-readable calendar covering G10
central banks *and* US data. The Fed/BLS/ECB/BoJ/BoE publish HTML (and a BLS ICS whose URL has
moved between years); the aggregators (ForexFactory, Investing.com, TradingEconomics) are
behind ToS restrictions and/or Cloudflare, which the charter puts out of scope. Regenerate
with `python -m fxgamma.data.events`.

A wrong event date cannot corrupt a price. It corrupts event-weighted business time
(`time_decay(..., calendar="event")`, CG-4), which is opt-in and badged for exactly this
reason.

### 2.7 Manual marks — **the primary vol input** (v1.2 T-1)

| | |
|---|---|
| **Module / file** | `manual.py` + `data/manual/marks.json` (`$FXGAMMA_MANUAL_MARKS` overrides) |
| **Provides** | per-pair, per-tenor **ATM / 25d RR / 25d BF** (+ optional 10d), typed or pasted |
| **Provenance** | `Provenance(source="manual", kind="user_override")`, with the entry time and the age in hours in the note |
| **Cost / lag / limitation** | $0; as fresh as the user's last paste; **it is only as good as the run they pasted, and it goes stale silently** — hence the age in the badge and a "STALE, re-mark before hedging" note after 12h |

Programmatic API (the Data page is built on this):

```python
from fxgamma.data import ManualQuoteProvider, get_provider

m = ManualQuoteProvider()                       # loads data/manual/marks.json
rep = m.paste(clipboard, pair="EURUSD")         # -> ParsedGrid
print(rep.summary())                            # what it inferred + every warning
m.set_mark("USDJPY", "1M", 0.0925, -0.0125, 0.0030)
m.store.as_frame()                              # editable grid for a DataTable
m.marked("EURUSD"), m.age_hours("EURUSD")       # badge + staleness
m.clear("EURUSD")                               # back to the indicative tier
p = get_provider("chain", manual=m)             # manual -> live -> cache
p.marked_pairs()                                # ['EURUSD']
```

The paste parser accepts tab/comma/multi-space separation, `%` signs, bid/ask (`7.05/7.25` →
mid), accounting negatives (`(0.15)`), a leading pair column, header rows in the usual
spellings (`ATM`, `25d RR`, `RR25`, `BF`, `fly`), and both row-per-tenor and column-per-tenor
(transposed) grids. **Unit inference:** one scale for the whole grid, taken from the ATM
column — a `%` anywhere or a largest-ATM above 1 means percent (`7.05` → `0.0705`), otherwise
decimals (`0.0705`). The same scale is then applied to RR and BF, because a broker run quotes
them in the same unit; inferring per column is how you end up with a 15-vol risk reversal.
Everything inferred is reported in `ParsedGrid.inferred`, everything doubtful in `.warnings`
(missing RR/BF defaulted to zero, |RR| > ATM, negative BF — possibly a *market* strangle,
which `build_surface` can convert), and an ATM outside 0.5%–150% is **refused**, not stored.

**Partial grids are served as-is.** If only 1M and 3M are marked, only those tenors go to
`build_surface`; we never splice ETF tenors into the gaps, because the result would be a term
structure nobody quoted under a badge that could not describe it. A pair is either marked or
it is not.

**Substitute quality: 5/5 — it *is* the desk's mark.** The residual risk is operational, not
statistical: a mistyped digit, or a mark left over from yesterday.

### 2.8 Synthetic market

`synthetic.py` — deterministic from a seed, self-consistent (one factor per currency leg, so
EURJPY is exactly EURUSD × USDJPY and no triangular arbitrage exists), stochastic vol so cones
and RV–IV spreads actually move, a smile with the right skew signs, and an OI ladder that
clusters on big figures. **Everything it returns is badged `kind="synthetic"`.** It is reached
only when the caller explicitly passes `allow_synthetic=True` (or asks for the `auto`
provider). It exists so every screen and every backtest runs in CI and in this sandbox. It is
not a data source and must never be presented as one.

---

## 3. What we cannot get for free

| What | Why not | Honest workaround | Residual risk |
|---|---|---|---|
| **Real-time OTC ATM / 25d RR / 25d BF** | It is a dealer-to-dealer market; the surface is a licensed product (Bloomberg BVOL, Refinitiv, ICAP/Tullett, NEX). Nothing free exists at any latency. | **Manual marks** (§2.7) — the trader pastes a broker run each morning; ETF/CBOE vols are demoted to z-scores, cones and richness. | Only as fresh and as correct as the paste. Badge the age, refuse to issue a hedge instruction off an `INDICATIVE` surface without an explicit acknowledgement. |
| **Historical OTC vol surface** (for cones, richness z-scores, backtests) | Same licensing; no free archive of dealer surfaces exists. | `EVZCLS` for EUR (ETF-based, §2.4); beta proxies elsewhere; and — the one that actually improves over time — **store every manual mark**, so after a few months the tool has the desk's *own* history. `ManualQuoteStore` keeps the `asof` on every mark precisely so this becomes possible. | Cones built on EVZ measure FXE's vol regime, not EURUSD's. Badge them. The manual history starts empty. |
| **Tick / intraday data** | Free sources are daily bars; intraday FX tick data is a paid product (and 1-minute Yahoo history is capped at ~7 days and unreliable). | Daily close-to-close RV; Yahoo's last price for an intraday refresh. Hedge-frequency backtests below daily are **not credible** and the Lab must say so. | Any "optimal hedge frequency" result at sub-daily resolution is an artefact. |
| **True dealer positioning / market gamma** | Nobody publishes it. CME OI is a small, listed slice; the OTC market is far larger and invisible. | CME OI by strike as *listed strike clustering* (§2.5), clearly labelled. | Do not let the Gamma Map imply "the market is short gamma here". It cannot know that. |
| **Official FX fixes (WMR 16:00 LDN)** | Licensed (LSEG). | ECB 14:15 CET fix (free, official, but the wrong time and not tradable); Yahoo/Stooq daily close as a proxy for the 17:00 NY roll. | Small level differences that surface in the attribution residual, not in a hedge decision. |
| **A machine-readable G10 event calendar** | No free, stable, complete feed (§2.6). | The curated CSV, versioned in the repo and user-editable. | Dates drift; check against the official pages. |
| **Credit/borrow and true funding curves** | Paid. | Flat zero curves from FRED + manual rate overrides. | Forward points wrong by a few pips at short tenors; material only beyond ~6M. |

---

## 4. How the layer behaves (design rules)

**Provenance.** Every field written into a `MarketSnapshot` gets a `Provenance` in `meta`
under the CG-7 grammar (`spot.<PAIR>`, `rate.<CCY>`, `fwd.<PAIR>.<TENOR>`, `surface.<PAIR>`,
`surface.<PAIR>.<TENOR>`, `oi.<PAIR>`, `events`), looked up most-specific-first by
`meta_lookup`. A surface that fails to build is **omitted** and the failure recorded as
`kind="unavailable"` — we never quietly substitute another source's number (contract §7).

**Resolution order.** `manual → live → cache → synthetic`. `ChainProvider` hoists any
`ManualQuoteProvider` to the front regardless of the order it was constructed in. Synthetic is
unreachable unless the caller opts in.

**Caching** (`cache.py`, `data/cache/`, parquet when pyarrow is present, else CSV, each entry
with a sidecar `.meta.json` recording source/url/fetched_at). TTLs: last price 60s, daily OHLC
6h, rates 12h, option chains 15min, vol indices 12h, OI 12h, calendar 24h. A stale entry is
still served when everything live fails, badged `cached` with its age in hours — degrade
visibly, never silently.

**Politeness.** One shared `requests.Session` with an identifiable User-Agent, per-host
throttling (Yahoo 1s, Stooq 2s, CME 2s, ECB/FRED/CBOE 0.5–1s), 3 attempts with jittered
exponential backoff, retry only on 429/5xx. `FXGAMMA_OFFLINE=1` hard-disables every live call.

**Testability.** `fetch_*()` does network I/O and nothing else; `parse_*()` is pure. Every
parser is exercised against `fxgamma/data/fixtures/` with zero connectivity —
`verify_live_sources.py` runs 18 such checks as part of its `local` group. Two CME fixtures
(`cme_settlements_SYNTHETIC.json`, `cme_stlcur_SYNTHETIC.txt`) are **hand-built, not recorded**
— the host is blocked, so nothing genuine could be captured. They pin the shape the parser
expects; they are not evidence that CME serves it. Replace them with real bodies after a
successful `--cme` run.

---

## 5. Recommended verification order on a networked machine

1. `python scripts/verify_live_sources.py --local` — must be all-green with no network at all.
2. `--spot` — one of three passing is enough; note which.
3. `--rates` — expect USD to pass and several of the others to fail. **Record the exact IDs
   that resolve** and prune `SERIES` to them.
4. `--vol` — if the crumb handshake has changed, this is where it shows.
5. `--vol-indices` — settles the JYVIX/BPVIX question one way or the other.
6. `--cme` — the least likely to work first time; the Gamma Map degrades to empty without it.
7. Flip `VERIFIED = True` in each module that passed, and update the table in §0 with the date
   and the machine you ran it on.
