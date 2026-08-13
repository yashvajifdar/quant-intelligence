# Quant Intelligence — Architecture & Design Document

*Last updated: 2026-08-12. Reflects actual codebase state.*

**Not financial advice.** All recommendations are for paper trading and educational/demonstration purposes only.

---

## 1. Problem Statement

Personal investment research is fragmented. Price data lives in one tab, macroeconomic indicators in another, options flow in a third, and the mental model for synthesizing them into a trade idea lives entirely in the trader's head. No single tool integrates factor signals, macro regime classification, unusual options activity, and AI-assisted reasoning into one workflow with a public track record.

This platform solves that for one investor: ingest daily S&P 500 data, compute hedge-fund-style quantitative signals, run them through an AI engine, and expose structured investment recommendations with a verifiable paper portfolio on yashvajifdar.com.

---

## 2. System Overview

```
Data Sources
────────────────────────────────────────────────────────────────
  yfinance (unofficial)          FRED API
  S&P 500 OHLCV                  T10Y2Y / FEDFUNDS
  Fundamentals (P/E, ROE…)       CPIAUCSL
  ^VIX
         │                              │
         └──────────────────┬───────────┘
                            ▼
ETL Pipeline  (etl/)
────────────────────────────────────────────────────────────────
  universe.py ──► Wikipedia S&P 500 list
  ingest_prices.py ──► OHLCV batch download (100 tickers/batch)
  ingest_macro.py  ──► FRED + VIX, daily forward-fill
  loader.py        ──► orchestrator, quality report
         │
         ▼
DuckDB Warehouse  (data/quant.db)
────────────────────────────────────────────────────────────────
  universe   prices   fundamentals   macro
  [portfolios + paper_trades — portfolio layer]
         │
         ▼
Signals Layer  (signals/)
────────────────────────────────────────────────────────────────
  factors.py        ──► Momentum 12-1, Low-Vol, Value composite, Quality composite
  technical.py      ──► MA50/200, RSI, MACD histogram, ATR, volume ratio
  macro_regime.py   ──► RISK_ON / TRANSITIONAL / RISK_OFF / CRISIS
  options_flow.py   ──► NOT YET BUILT (planned)
         │
         ▼  (called at query time via TOOL_DISPATCH — not persisted)
AI Engine  (engine/)
──────────────────────────────────────────────────────────────────
  engine_tools.py      ──► 3 provider-neutral tool definitions, TOOL_DISPATCH map
  anthropic_engine.py  ──► new-picks engine: multi-tool loop, structured JSON output
  review_engine.py     ──► position review engine: HOLD/ADD/TRIM/EXIT per position
  recommendation.py    ──► typed dataclasses: Recommendation, RecommendationSet,
                           PositionReview, PortfolioReview, PortfolioInsights,
                           HedgeSuggestion; reward_risk ≥ 2.0 enforced
         │
         ▼
FastAPI  (app/api.py)
──────────────────────────────────────────────────────────────────
  GET  /health
  POST /recommend
  GET  /signals                           (4h in-memory cache)
  POST /internal/run-etl                  (ETL_SECRET header required)
  POST /portfolio
  GET  /portfolio/{id}                    (includes live unrealized P&L)
  POST /portfolio/{id}/trades
  PATCH /portfolio/{id}/trades/{trade_id}
  POST /portfolio/{id}/review             (AI position review)
  GET  /leaderboard
  Deployed on Render at https://quant-intelligence.onrender.com
         │
         ▼
  Next.js Dashboard (personal-website/app/demos/quant/)
  Deployed on Vercel at yashvajifdar.com/demos/quant
  4-tab UI: Picks | Portfolio | Universe | Learn
  Zod runtime validation on all API responses (lib/quant-api.ts)
```

---

## 3. Data Model

All tables live in `data/quant.db` (DuckDB). Schema is defined in `etl/schema.py` and initialized by `initialize_schema(db_path)`.

### 3.1 `universe`

**Grain:** one row = one S&P 500 constituent ticker

| Column | Type | Source | Notes |
|---|---|---|---|
| `ticker` | VARCHAR PK | Wikipedia | Dots replaced with hyphens (`BRK.B` → `BRK-B`) |
| `company_name` | VARCHAR | Wikipedia | |
| `sector` | VARCHAR | Wikipedia | GICS sector |
| `industry` | VARCHAR | Wikipedia | GICS sub-industry |
| `gics_sub_industry` | VARCHAR | Wikipedia | Same as industry; retained for explicitness |
| `headquarters` | VARCHAR | Wikipedia | |
| `date_added` | DATE | Wikipedia | Date added to S&P 500 |
| `cik` | VARCHAR | Wikipedia | SEC CIK for EDGAR lookup |
| `loaded_at` | TIMESTAMP | ETL | Auto-set at insert |

**Populated by:** `etl/universe.py` → `load_universe()`. Full-refresh on every run. Upserts on ticker PK.

### 3.2 `prices`

**Grain:** one row = one ticker × one trading day

| Column | Type | Source | Notes |
|---|---|---|---|
| `ticker` | VARCHAR | yfinance | |
| `date` | DATE | yfinance | |
| `open` | DOUBLE | yfinance | |
| `high` | DOUBLE | yfinance | |
| `low` | DOUBLE | yfinance | |
| `close` | DOUBLE | yfinance | `auto_adjust=True`; close equals adj_close |
| `adj_close` | DOUBLE | yfinance | Set equal to close; retained for signal code clarity |
| `volume` | BIGINT | yfinance | |
| `loaded_at` | TIMESTAMP | ETL | Auto-set at insert |

**Populated by:** `etl/ingest_prices.py` → `load_prices()`. Full refresh: 2-year history. Incremental: 7-day lookback with overlap to catch late-arriving corrections.

### 3.3 `fundamentals`

**Grain:** one row = one ticker × one weekly fetch date

| Column | Type | Source | Notes |
|---|---|---|---|
| `ticker` | VARCHAR | yfinance | |
| `fetched_date` | DATE | ETL | Date data was pulled |
| `market_cap` | DOUBLE | yfinance | |
| `pe_ratio` | DOUBLE | yfinance | Trailing twelve months |
| `forward_pe` | DOUBLE | yfinance | |
| `pb_ratio` | DOUBLE | yfinance | |
| `ev_ebitda` | DOUBLE | yfinance | |
| `revenue_growth` | DOUBLE | yfinance | YoY |
| `gross_margin` | DOUBLE | yfinance | |
| `operating_margin` | DOUBLE | yfinance | |
| `debt_equity` | DOUBLE | yfinance | |
| `roe` | DOUBLE | yfinance | Return on equity |
| `free_cashflow` | DOUBLE | yfinance | |
| `earnings_date` | DATE | yfinance | Next scheduled earnings |

**Populated by:** `etl/ingest_fundamentals.py` → `load_fundamentals()`. Run with `python -m etl.loader --with-fundamentals`. Opt-in rather than daily default — serial per-ticker yfinance calls make it slow (~10–15 min for the full 500-ticker universe); fundamental data changes slowly enough that weekly scheduling is sufficient.

### 3.4 `macro`

**Grain:** one row = one calendar date

| Column | Type | Source | Notes |
|---|---|---|---|
| `date` | DATE PK | FRED / yfinance | |
| `t10y2y` | DOUBLE | FRED `T10Y2Y` | 10Y minus 2Y treasury spread; inversion < 0 |
| `fed_funds_rate` | DOUBLE | FRED `FEDFUNDS` | Monthly; forward-filled to daily |
| `cpi` | DOUBLE | FRED `CPIAUCSL` | Monthly; forward-filled to daily |
| `vix` | DOUBLE | yfinance `^VIX` | CBOE Volatility Index daily close |
| `loaded_at` | TIMESTAMP | ETL | Auto-set at insert |

**Populated by:** `etl/ingest_macro.py` → `load_macro()`. FRED series are daily or monthly; monthly series are forward-filled via `resample("D").last().ffill()` before writing.

### 3.5 `signals` (computed at query time — not a persisted table)

Signal functions in `signals/` are called at query time by the AI engine via `TOOL_DISPATCH` in `engine/engine_tools.py`. Results are passed directly to the model as tool results; they are not written to DuckDB. A persistent `signals` gold table has not been implemented and requires an ADR before it is added.

### 3.6 `portfolios` and `paper_trades`

Two tables managed by `portfolio/paper_trades.py`. Schema is created by `CREATE TABLE IF NOT EXISTS` (idempotent). Multi-user: each portfolio has a UUID primary key.

**`portfolios`**

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | UUID |
| `display_name` | TEXT | |
| `is_primary` | BOOLEAN | |
| `created_at` | TIMESTAMP | |
| `last_active_at` | TIMESTAMP | |

**`paper_trades`**

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | UUID |
| `portfolio_id` | TEXT FK | References `portfolios.id` |
| `ticker` | TEXT | |
| `action` | TEXT | `BUY`, `AVOID`, or `WATCH` |
| `entry_date` | DATE | |
| `entry_price` | DOUBLE | Prior day's close at time of recommendation |
| `shares` | DOUBLE | |
| `stop` | DOUBLE | Stop-loss price |
| `target` | DOUBLE | Target price |
| `signal_snapshot` | TEXT | JSON blob of signals at entry time |
| `thesis` | TEXT | 1–3 sentence plain-English rationale |
| `exit_date` | DATE | NULL for open trades |
| `exit_price` | DOUBLE | NULL for open trades |
| `exit_reason` | TEXT | NULL for open trades |
| `realized_pnl` | DOUBLE | NULL for open trades |

**Key functions:** `open_trade()`, `close_trade()`, `purge_stale_portfolios()` (90-day cutoff), `get_leaderboard()` (only portfolios with ≥1 closed trade).

---

## 4. ETL Pipeline

The orchestrator is `etl/loader.py`. Run it with `python -m etl.loader` (incremental) or `python -m etl.loader --full-refresh` (2-year backfill, ~10 min).

### 4.1 Module responsibilities

| Module | What it does |
|---|---|
| `etl/schema.py` | Defines all four CREATE TABLE statements. `initialize_schema(db_path)` creates tables if they do not exist. Idempotent. |
| `etl/universe.py` | Fetches S&P 500 constituent list from Wikipedia via `pandas.read_html`. Replaces `.` with `-` in tickers. Upserts on ticker PK. Called on every run. |
| `etl/ingest_prices.py` | Downloads OHLCV via `yfinance.download()` in batches of 100 tickers. `auto_adjust=True` means close equals adj_close. Incremental: last 7 days. Full refresh: 2 years. Batch failures are counted and reported; the run continues on partial failure. |
| `etl/ingest_macro.py` | Fetches `T10Y2Y`, `FEDFUNDS`, `CPIAUCSL` from FRED via `fredapi`. Fetches `^VIX` from yfinance. Merges on date; forward-fills monthly series to daily. Incremental: 45-day lookback (longer than prices to catch monthly FRED updates). `_fetch_vix()` flattens MultiIndex columns before selecting Close — yfinance ≥0.2 returns MultiIndex even for a single ticker; without flattening, `raw[["Close"]]` silently returns an empty frame (commit a36be54). |
| `etl/ingest_fundamentals.py` | Fetches P/E, P/B, EV/EBITDA, ROE, gross margin, debt/equity, FCF, and earnings date via `yfinance.Ticker.info` — one ticker at a time (no batch endpoint). Designed for weekly scheduling, not daily. `tickers_failed` counts yfinance failures (`.info` is flaky for some tickers); run continues. |
| `etl/loader.py` | Calls `_validate_env()` to fail loudly if `FRED_API_KEY` is absent. Runs universe → prices → macro → fundamentals (opt-in via `--with-fundamentals`) in sequence. Prints the quality report to stdout. |

### 4.2 Batch strategy

Price ingestion batches 100 tickers per `yfinance.download()` call. yfinance returns MultiIndex columns for multi-ticker batches; `ingest_prices.py` normalizes these to a long-format DataFrame with `stack(level="Ticker")`. A batch failure increments `batches_failed` and logs the error; subsequent batches continue. The quality report surfaces the failure count.

### 4.3 Full-refresh vs incremental

| Mode | Prices lookback | Macro lookback | Use when |
|---|---|---|---|
| `--full-refresh` | 2 years (730 days) | 2 years | First run, schema rebuild, or data repair |
| Incremental (default) | 7 days | 45 days | Daily cron; overlapping window catches corrections and late FRED updates |

---

## 5. Signals Layer

All signal functions live in `signals/`. They are called at query time by the AI engine via `TOOL_DISPATCH`; results are not persisted to DuckDB.

Every signal function requires a known-value test before merge. The `quant-finance` agent reviews all signal logic before it ships.

### 5.1 Factor signals (`signals/factors.py`)

| Factor | Input data | Formula sketch | High score means | Status |
|---|---|---|---|---|
| Momentum 12-1 | `prices` | `(price_today / price_12m_ago) - 1`, excluding last 30 days | Stock outperformed peers over past year | Built |
| Low volatility | `prices` | `1 / std_dev(daily_returns, 252)`, cross-sectionally ranked | Low realized volatility over past year | Built |
| Value composite | `fundamentals` | Inverse-rank average of P/E, P/B, EV/EBITDA | Cheap relative to fundamentals | Built |
| Quality composite | `fundamentals` | Rank average of ROE, gross margin, inverse(debt_equity), FCF | Profitable, low-debt, cash-generating | Built |

Value and quality composites use cross-sectional ranking before averaging to prevent any single metric from dominating.

`compute_combined_factor_score` combines all four factors with weights from ADR-0002: momentum 40% / quality 25% / low-vol 20% / value 15%. If the fundamentals table is empty (weekly ETL has not yet run), the function falls back to a two-factor composite (momentum + low-vol, rescaled to sum to 100%) so the API layer continues returning results.

### 5.2 Technical signals (`signals/technical.py`)

All five signals are built.

| Signal | Parameters | What it captures |
|---|---|---|
| MA crossover | 50-day SMA, 200-day SMA | Intermediate vs long-term trend alignment |
| RSI | 14-period | Overbought (> 70) / oversold (< 30) conditions |
| MACD histogram | EMA(12), EMA(26), signal EMA(9) | Momentum acceleration and deceleration |
| ATR | 14-period true range average | Daily volatility magnitude; drives stop-loss placement |
| Volume ratio | 20-day average volume | Breakout validity: requires > 150% of average |

Technical signals are used as entry filters and for risk parameter computation (ATR stop placement). They do not rank cross-sectionally.

### 5.3 Macro regime classifier (`signals/macro_regime.py`)

Built. Classifies the current market environment into one of four states, gating which recommendations the AI engine surfaces.

| Regime | Conditions | AI engine behavior |
|---|---|---|
| `RISK_ON` | `t10y2y > 0`, `vix < 20`, SPX above 200-day SMA | Full long recommendations permitted |
| `TRANSITIONAL` | Mixed signals: yield curve flattening or VIX 20–25 | Reduced position sizes; defensive sector preference |
| `RISK_OFF` | `t10y2y < 0` or `vix > 25`, SPX near or below 200-day SMA | Only high-quality, low-volatility longs; no aggressive momentum |
| `CRISIS` | `vix > 35` | Defensive posture only; no new longs |

Regime is computed from the `macro` table and passed to the AI engine as a tool result on every query.

### 5.4 Options flow (`signals/options_flow.py`)

NOT YET BUILT. See Open Questions for the Unusual Whales API decision. Write an ADR before implementing.

---

## 6. AI Engine

The engine pattern is the same as lumber-ai-analytics: Anthropic tool use, structured output, provider-agnostic tool definitions. Model: `claude-sonnet-4-6`. Max tokens: 2048.

### 6.1 Architecture

`engine_tools.py` holds 3 provider-neutral tool definitions: `get_macro_regime`, `get_top_factor_candidates`, `get_technical_signals`. `TOOL_DISPATCH` maps each name to its callable in `signals/`.

`anthropic_engine.py` converts tool definitions to Anthropic wire format and runs a **multi-tool loop** — not a fixed two-turn exchange. The loop continues until `stop_reason != "tool_use"`. On a typical query, the model makes 3 sequential tool calls (macro regime, then factor candidates, then technical signals) before generating its final recommendation. `_parse_json()` uses regex to extract JSON from model prose.

The engine never writes SQL or reads raw tables. It calls signal functions via `TOOL_DISPATCH`.

**New picks engine** (`anthropic_engine.py`): `POST /recommend` calls `run(query)` and serializes the `RecommendationSet` via `dataclasses.asdict()`. Returns 503 on `ValueError` (missing API key), 500 on other errors. Off-topic queries (e.g. "what should I sell?") return `{"recommendations": [], "note": "..."}` rather than crashing — the model is instructed to call all three tools and then return the note-only JSON.

**Position review engine** (`review_engine.py`): `POST /portfolio/{id}/review` fetches open trades from the DB, formats them as context in the user message, then runs the same 3-tool loop (macro → technical signals on held tickers → top 20 factor candidates). Output is `PortfolioReview` with per-position verdicts (`HOLD/ADD/TRIM/EXIT`) and portfolio-level insights (regime impact, concentration risk, hedge suggestions, diversifier suggestions). Max tokens: 3072 (larger than picks engine to handle multi-position output).

**Universe endpoint**: GET `/signals` calls `compute_combined_factor_score(DB_PATH)`, joins with `universe` for company name and sector, and returns all 503 tickers ranked by composite score. Cached in-memory for 4 hours.

**Live unrealized P&L**: `GET /portfolio/{id}` calls `_fetch_current_prices(tickers)` via yfinance (5-day window) for each open trade and attaches `current_price` and `unrealized_pnl = (current_price - entry_price) × shares` to each trade dict before returning. Fails gracefully — if yfinance is unavailable, `unrealized_pnl` is `null`.

### 6.2 Recommendation output format

Every recommendation is a typed dataclass (`engine/recommendation.py`). `Recommendation.__post_init__` enforces `reward_risk ≥ 2.0` and `conviction` in range 1–5 at construction time.

| Field | Type | Example |
|---|---|---|
| `ticker` | str | `"NVDA"` |
| `action` | Literal["BUY", "AVOID", "WATCH"] | `"BUY"` |
| `conviction` | int (1–5) | `4` |
| `macro_regime` | str | `"RISK_ON"` |
| `signal_summary` | dict | `{"momentum": 0.87, "low_vol": 0.72, "rsi": 58, ...}` |
| `thesis` | str | 1–3 sentence plain-English rationale |
| `entry_price` | float | Closing price on recommendation date |
| `stop_loss` | float | Entry minus 1.25× ATR |
| `target` | float | Entry plus 2× (entry minus stop_loss) |
| `reward_risk` | float | Must be ≥ 2.0 — enforced in `__post_init__` |

The engine returns a `RecommendationSet` containing: `recommendations` (list of `Recommendation`), `macro` (`MacroContext`), and optional `note` (string — set when the engine returns no picks, e.g. off-topic query).

The review engine returns a `PortfolioReview` containing: `position_reviews` (list of `PositionReview` with `verdict`, `conviction`, `signal_summary`, `updated_thesis`, `risk_note`), `portfolio_insights` (`PortfolioInsights` with `regime_impact`, `concentration_risk`, `hedge_suggestions`, `diversifier_suggestions`), and `macro`.

### 6.3 Lookahead bias prevention

Signals are computed using the **prior trading day's close**. Paper trade entries are logged at the **current day's close** after signal generation. The engine has no access to same-day intraday data.

---

## 7. Paper Portfolio

### 7.1 Schema

`portfolio/paper_trades.py` manages two tables in `quant.db`: `portfolios` and `paper_trades`. See Section 3.6 for the full column definitions. Schema creation is idempotent (`CREATE TABLE IF NOT EXISTS`). Position size is capped at 10% of `PAPER_ACCOUNT_VALUE` (default $100,000, set via environment variable).

### 7.2 Performance metrics

`portfolio/performance.py` exposes pure functions.

| Function | Behavior |
|---|---|
| `sharpe_ratio(closed_trades)` | Annualized. Uses `TRADING_DAYS_PER_YEAR = 252`. Returns `None` if fewer than 2 closed trades. |
| `max_drawdown(closed_trades)` | Peak-to-trough decline across the closed trade sequence. |
| `compute_summary(closed_trades, open_count)` | Returns a dict with Sharpe, max drawdown, win rate, avg win/loss, and total return. |

Target benchmarks (not enforced in code):

| Metric | Target |
|---|---|
| Sharpe ratio | > 1.2 |
| Max drawdown | < -20% |
| Win rate | > 45% |
| Avg win/loss | > 2.0 |

### 7.3 Public reporting

Performance is visible at yashvajifdar.com/demos/quant via the Portfolio tab. All trades are timestamped and immutable once logged.

---

## 8. Deployment

### 8.1 Render configuration

| Component | Type | Details |
|---|---|---|
| Web service | FastAPI (`app/api.py`) | `uvicorn app.api:app --host 0.0.0.0 --port $PORT` |
| Persistent disk | 1 GB, mounted at `/data` | $1/month; `QUANT_DB_PATH=/data/quant.db` |

The persistent disk is required because Render's filesystem resets on redeploy. Without it, every deploy destroys the price history, and a full 10-minute backfill runs on every code push.

Daily ETL is triggered via GitHub Actions (see `.github/workflows/daily-etl.yml`), not a Render cron job. Render persistent disks attach to one service at a time — a Render cron job would write to a different database instance than the web service reads from, producing stale or split data.

### 8.2 Environment variables

| Variable | Required | Set in |
|---|---|---|
| `FRED_API_KEY` | Yes | Render environment, `.env` locally |
| `QUANT_DB_PATH` | Yes | Render environment (`/data/quant.db`), `.env` locally |
| `ANTHROPIC_API_KEY` | Yes | Render environment, `.env` locally |
| `PAPER_ACCOUNT_VALUE` | Yes | Render environment (`100000`), `.env` locally |
| `ETL_SECRET` | Yes | Render environment + GitHub Actions secret — must match |

### 8.3 Frontend deployment

The Next.js dashboard is deployed on Vercel at yashvajifdar.com/demos/quant.

| Item | Value |
|---|---|
| Source | `personal-website/app/demos/quant/page.tsx` |
| API client | `personal-website/lib/quant-api.ts` |
| Design token | `quant.DEFAULT = #0d9488` (teal-600) |
| Env var (Vercel) | `NEXT_PUBLIC_QUANT_API_URL=https://quant-intelligence.onrender.com` |
| Local dev env var | `NEXT_PUBLIC_QUANT_API_URL=http://localhost:8002` |
| Portfolio ID persistence | `localStorage` key `"quant_portfolio_id"` |

The Universe tab (Tab 4) is live. `UniverseTab.tsx` fetches `GET /signals` on mount and renders all 503 tickers in a sortable (composite/momentum/low-vol), searchable, sector-filtered table. Sector color badges applied. Responsive: sector column hidden on mobile, momentum and low-vol columns hidden below md breakpoint. Interfaces `SignalTicker` and `SignalsResponse` and the `fetchSignals()` function live in `personal-website/lib/quant-api.ts`.

Trigger a Vercel redeploy by pushing to `main` on the `personal-website` repo.

---

## 9. Component Decisions and Tradeoffs

### 9.1 Storage: DuckDB

| Option | Pro | Con |
|---|---|---|
| **DuckDB** (current) | Columnar, fast analytical scans, in-process, no server | Single-writer; not suitable for concurrent multi-user production |
| SQLite | Simpler, wider ecosystem | Row-oriented; slower on wide analytical queries over 500K+ rows |
| PostgreSQL | Concurrent, cloud-native | Requires managed server; adds cost and setup for a single-user demo |

**Decision:** DuckDB is the right tool for columnar time-series analytics on S&P 500-scale data. Replacing it requires changing `QUANT_DB_PATH` and the `duckdb.connect()` call in each module; no business logic changes.

### 9.2 Price data: yfinance

| Option | Pro | Con |
|---|---|---|
| **yfinance** (current) | Free, no API key, covers full S&P 500 history | Unofficial; Yahoo can rate-limit or change the API without notice |
| Polygon.io | Official, reliable, full tick data | Paid; $29/month minimum for history |
| Alpha Vantage | Official, free tier | Rate-limited to 5 calls/min on free; too slow for 500 tickers |

**Decision:** yfinance for now. Data source is isolated to `etl/ingest_prices.py`. Swapping providers requires changing one file and no downstream code. See Open Questions for fragility risk.

### 9.3 AI engine: Anthropic tool use

Same rationale as lumber-ai-analytics. The model selects from pre-defined signal functions; it never generates SQL or reads raw price tables. Hallucination on financial data is architecturally prevented. Conviction scores and risk parameters are computed deterministically from signal output, not inferred by the LLM.

---

## 10. Open Questions

1. **Unusual Whales API:** No official public API exists. The community Python client and public feed are the current options. This is the most fragile dependency in the planned stack. Write an ADR before implementing `options_flow.py`. Options: (a) scrape the public feed, (b) use the unofficial Python client, (c) skip options flow for MVP.

2. **Survivorship bias in any backtesting:** The universe table is loaded from Wikipedia's current S&P 500 list. Historical constituents that have since been removed are absent. Any backtest using this universe will overstate returns. This limitation must be documented prominently on any public-facing performance page. True point-in-time universe requires a paid data source.

3. **Brokerage execution:** If the platform ever moves from paper to live trading, brokerage API integration (Alpaca, IBKR) introduces regulatory, security, and operational complexity that is out of scope for this project in its current form. No brokerage integration should be added without a separate architecture review.

4. **ETL scheduling:** Daily ETL runs via GitHub Actions on schedule `0 11 * * 1-5`. If the workflow needs to run outside market hours or on weekends, update the cron expression in `.github/workflows/daily-etl.yml` and verify the `ETL_SECRET` secret is set in repo Settings → Secrets and variables → Actions.
