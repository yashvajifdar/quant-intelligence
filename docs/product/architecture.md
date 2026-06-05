# Quant Intelligence — Architecture & Design Document

*Last updated: 2026-06-05. Reflects actual codebase state. Sections 5–7 describe planned components.*

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
  yfinance (unofficial)          FRED API               Unusual Whales
  S&P 500 OHLCV                  T10Y2Y / FEDFUNDS       Options sweeps
  Fundamentals (P/E, ROE…)       CPIAUCSL               Block trades
  ^VIX                                                   Dark pool prints
         │                              │                       │
         └──────────────────┬───────────┘                       │
                            ▼                                   │
ETL Pipeline  (etl/)                                            │
────────────────────────────────────────────────────────────────│──
  universe.py ──► Wikipedia S&P 500 list                        │
  ingest_prices.py ──► OHLCV batch download (100 tickers/batch) │
  ingest_macro.py  ──► FRED + VIX, daily forward-fill           │
  loader.py        ──► orchestrator, quality report             │
         │                                                       │
         ▼                                                       │
DuckDB Warehouse  (data/quant.db)                               │
────────────────────────────────────────────────────────────────│──
  universe   prices   fundamentals   macro                      │
  [signals — planned]   [paper_trades — planned]                │
         │                                                       │
         ▼                                                       │
Signals Layer  (signals/ — planned)                             │
────────────────────────────────────────────────────────────────│──
  factors.py        ──► Momentum 12-1, Value, Quality, Low-Vol  │
  technical.py      ──► MA50/200, RSI 14, MACD 12/26/9, ATR 14  │
  macro_regime.py   ──► RISK_ON / RISK_OFF / TRANSITIONAL / CRISIS
  options_flow.py ◄─────────────────────────────────────────────┘
         │
         ▼
AI Engine  (engine/ — planned)
──────────────────────────────────────────────────────────────────
  engine_tools.py      ──► tool definitions, signal dispatch map
  anthropic_engine.py  ──► Anthropic tool-use, structured output
  recommendation.py    ──► typed recommendation dataclass
         │
         ▼
API + UI
──────────────────────────────────────────────────────────────────
  FastAPI (app/api.py)   ──► POST /recommend, GET /portfolio, GET /health
  Streamlit (app/main.py) ──► local development UI
         │
         ▼
  yashvajifdar.com/demos/quant  (Next.js — calls FastAPI)
  Public paper portfolio + current recommendations
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

**Populated by:** `etl/ingest_fundamentals.py` (planned). Fetched weekly; fundamental data changes slowly enough that daily fetch is unnecessary.

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

### 3.5 `signals` (planned)

**Grain:** one row = one ticker × one date

Computed gold layer: all factor scores, technical indicators, and macro regime label joined per ticker per trading day. Written by the signals layer; read by the AI engine. Schema TBD — requires ADR before implementation.

### 3.6 `paper_trades` (planned)

**Grain:** one row = one paper trade entry or exit

Fields: `ticker`, `direction`, `entry_date`, `entry_price`, `exit_date`, `exit_price`, `pnl`, `recommendation_id`, `thesis`. Schema TBD.

---

## 4. ETL Pipeline

The orchestrator is `etl/loader.py`. Run it with `python -m etl.loader` (incremental) or `python -m etl.loader --full-refresh` (2-year backfill, ~10 min).

### 4.1 Module responsibilities

| Module | What it does |
|---|---|
| `etl/schema.py` | Defines all four CREATE TABLE statements. `initialize_schema(db_path)` creates tables if they do not exist. Idempotent. |
| `etl/universe.py` | Fetches S&P 500 constituent list from Wikipedia via `pandas.read_html`. Replaces `.` with `-` in tickers. Upserts on ticker PK. Called on every run. |
| `etl/ingest_prices.py` | Downloads OHLCV via `yfinance.download()` in batches of 100 tickers. `auto_adjust=True` means close equals adj_close. Incremental: last 7 days. Full refresh: 2 years. Batch failures are counted and reported; the run continues on partial failure. |
| `etl/ingest_macro.py` | Fetches `T10Y2Y`, `FEDFUNDS`, `CPIAUCSL` from FRED via `fredapi`. Fetches `^VIX` from yfinance. Merges on date; forward-fills monthly series to daily. Incremental: 45-day lookback (longer than prices to catch monthly FRED updates). |
| `etl/loader.py` | Calls `_validate_env()` to fail loudly if `FRED_API_KEY` is absent. Runs universe → prices → macro in sequence. Prints the quality report to stdout. |

### 4.2 Batch strategy

Price ingestion batches 100 tickers per `yfinance.download()` call. yfinance returns MultiIndex columns for multi-ticker batches; `ingest_prices.py` normalizes these to a long-format DataFrame with `stack(level="Ticker")`. A batch failure increments `batches_failed` and logs the error; subsequent batches continue. The quality report surface the failure count.

### 4.3 Full-refresh vs incremental

| Mode | Prices lookback | Macro lookback | Use when |
|---|---|---|---|
| `--full-refresh` | 2 years (730 days) | 2 years | First run, schema rebuild, or data repair |
| Incremental (default) | 7 days | 45 days | Daily cron; overlapping window catches corrections and late FRED updates |

---

## 5. Signals Layer (planned)

All signal functions will live in `signals/`. Each function takes a DuckDB connection or DataFrame and returns a normalized score between 0 and 1, ranked cross-sectionally across the S&P 500 universe on a given date.

Every signal function requires a known-value test before merge. The `quant-finance` agent reviews all signal logic before it ships.

### 5.1 Factor signals (`signals/factors.py`)

| Factor | Input data | Formula sketch | High score means |
|---|---|---|---|
| Momentum 12-1 | `prices` | `(price_today / price_12m_ago) - 1`, excluding last 30 days | Stock outperformed peers over past year |
| Value composite | `fundamentals` | Inverse-rank average of P/E, P/B, P/S, EV/EBITDA | Cheap relative to fundamentals |
| Quality composite | `fundamentals` | Rank average of ROE, gross margin, inverse(debt_equity), FCF | Profitable, low-debt, cash-generating |
| Low volatility | `prices` | `1 / std_dev(daily_returns, 252)`, cross-sectionally ranked | Low realized volatility over past year |

Value and quality composites use cross-sectional ranking before averaging to prevent any single metric from dominating.

### 5.2 Technical signals (`signals/technical.py`)

| Signal | Parameters | What it captures |
|---|---|---|
| MA crossover | 50-day SMA, 200-day SMA | Intermediate vs long-term trend alignment |
| RSI | 14-period | Overbought (> 70) / oversold (< 30) conditions |
| MACD crossover | EMA(12), EMA(26), signal EMA(9) | Momentum acceleration and deceleration |
| ATR | 14-period true range average | Daily volatility magnitude; drives stop-loss placement |
| Volume confirmation | 20-day average volume | Breakout validity: requires > 150% of average |

Technical signals are used as entry filters and for risk parameter computation (ATR stop placement). They do not rank cross-sectionally.

### 5.3 Macro regime classifier (`signals/macro_regime.py`)

Classifies the current market environment into one of four states, gating which recommendations the AI engine surfaces.

| Regime | Conditions | AI engine behavior |
|---|---|---|
| `RISK_ON` | `t10y2y > 0`, `vix < 20`, SPX above 200-day SMA | Full long recommendations permitted |
| `TRANSITIONAL` | Mixed signals: yield curve flattening or VIX 20–25 | Reduced position sizes; defensive sector preference |
| `RISK_OFF` | `t10y2y < 0` or `vix > 25`, SPX near or below 200-day SMA | Only high-quality, low-volatility longs; no aggressive momentum |
| `CRISIS` | `vix > 35` | Defensive posture only; no new longs |

Regime is computed daily from `macro` table data and stored in the `signals` table alongside per-ticker scores.

### 5.4 Options flow (`signals/options_flow.py`)

Reads Unusual Whales data (API method TBD — see Open Questions). Flags tickers with:

- Unusual call sweep volume (> 3× 20-day average options volume)
- Above-ask aggressive sweeps on near-term OTM calls
- Rising open interest alongside volume spike (new positioning, not roll)

Options flow is an additive +1 modifier to the AI engine's conviction score. It never generates a standalone recommendation.

---

## 6. AI Engine (planned)

The engine pattern is the same as lumber-ai-analytics: Anthropic tool use, structured output, provider-agnostic tool definitions.

### 6.1 Architecture

`engine_tools.py` holds provider-neutral tool definitions. `anthropic_engine.py` converts these to Anthropic wire format and runs a two-turn flow:

- **Turn 1:** model selects which signal functions to call and with what parameters
- **Turn 2:** model reads the signal results and constructs a structured recommendation

The engine never writes SQL or reads raw tables. It calls signal functions via the dispatch map.

### 6.2 Recommendation output format

Every recommendation is a typed dataclass (`engine/recommendation.py`):

| Field | Type | Example |
|---|---|---|
| `ticker` | str | `"NVDA"` |
| `action` | Literal["BUY", "AVOID", "WATCH"] | `"BUY"` |
| `conviction` | int (1–5) | `4` |
| `macro_regime` | str | `"RISK_ON"` |
| `signal_summary` | dict | `{"momentum": 0.87, "quality": 0.72, "rsi": 58, ...}` |
| `thesis` | str | 1–3 sentence plain-English rationale |
| `entry_price` | float | Closing price on recommendation date |
| `stop_loss` | float | Entry minus 1.25× ATR |
| `target` | float | Entry plus 2× (entry minus stop_loss) |
| `reward_risk` | float | Must be ≥ 2.0 |
| `paper_trade` | bool | `True` if the engine is logging this to `paper_trades` |

Recommendations with `reward_risk < 2.0` are suppressed before returning to the caller.

### 6.3 Lookahead bias prevention

Signals are computed using the **prior trading day's close**. Paper trade entries are logged at the **current day's close** after signal generation. The engine has no access to same-day intraday data.

---

## 7. Paper Portfolio (planned)

### 7.1 Trade log

`portfolio/paper_trades.py` writes to the `paper_trades` table in `quant.db`. Every row records: ticker, entry date, entry price (prior day's close at time of recommendation), exit date, exit price, P&L in dollars and percent, and the `recommendation_id` linking back to the engine output.

### 7.2 Performance metrics

`portfolio/performance.py` computes:

| Metric | Target | Notes |
|---|---|---|
| Sharpe ratio | > 1.2 | Annualized; risk-free rate from FRED `FEDFUNDS` |
| Max drawdown | < -20% | Peak-to-trough decline |
| Win rate | > 45% | Profitable closed trades / total closed trades |
| Avg win/loss | > 2.0 | Average winner / average loser |
| Total return | Benchmark: SPY | Annualized, reported at 6-month intervals |

### 7.3 Public reporting

Performance reports are published to yashvajifdar.com/demos/quant at 6-month intervals. Reports include: total return vs SPY, Sharpe, max drawdown, win rate, and a trade-by-trade log. All trades are timestamped and immutable once logged.

---

## 8. Deployment

### 8.1 Render configuration

| Component | Type | Details |
|---|---|---|
| Web service | FastAPI (`app/api.py`) | `uvicorn app.api:app --host 0.0.0.0 --port $PORT` |
| Cron job | Daily ETL | `python -m etl.loader` at 6:00 AM ET |
| Persistent disk | 1 GB, mounted at `/data` | $1/month; `QUANT_DB_PATH=/data/quant.db` |

The persistent disk is required because Render's filesystem resets on redeploy. Without it, every deploy destroys the price history, and a full 10-minute backfill runs on every code push.

### 8.2 Environment variables

| Variable | Required | Set in |
|---|---|---|
| `FRED_API_KEY` | Yes | Render environment, `.env` locally |
| `QUANT_DB_PATH` | Yes | Render environment (`/data/quant.db`), `.env` locally |
| `ANTHROPIC_API_KEY` | When AI engine ships | Render environment, `.env` locally |

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

1. **Unusual Whales API:** No official public API exists. The community Python client and public feed are the current options. This is the most fragile dependency in the planned stack. Write an ADR before implementing `options_flow.py`. Options: (a) scrape the public feed, (b) use the unofficial Python client, (c) skip options flow for MVP and add it in M4.

2. **Survivorship bias in any backtesting:** The universe table is loaded from Wikipedia's current S&P 500 list. Historical constituents that have since been removed are absent. Any backtest using this universe will overstate returns. This limitation must be documented prominently on any public-facing performance page. True point-in-time universe requires a paid data source.

3. **Brokerage execution:** If the platform ever moves from paper to live trading, brokerage API integration (Alpaca, IBKR) introduces regulatory, security, and operational complexity that is out of scope for this project in its current form. No brokerage integration should be added without a separate architecture review.
