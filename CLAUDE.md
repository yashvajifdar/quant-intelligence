# Quant Intelligence — Project Context

Read `../../CLAUDE.md` (Workspace root) first. This file adds project-specific context.

---

## What This Is

A personal investment intelligence platform that ingests daily S&P 500 market data,
computes hedge-fund-style signals, and uses an AI engine to produce structured
investment recommendations with a public paper portfolio tracker.

**Framing:** This is a quantitative research platform with an AI interface —
not a trading bot. All recommendations are for paper trading and educational purposes only.
Not financial advice.

---

## Architecture

```
etl/
  schema.py             ← DuckDB table definitions (universe, prices, fundamentals, macro)
  universe.py           ← S&P 500 constituent list from Wikipedia
  ingest_prices.py      ← Daily OHLCV via yfinance batch download
  ingest_fundamentals.py← P/E, ROE, margins, earnings dates via yfinance Ticker
  ingest_macro.py       ← FRED (yield curve, fed funds, CPI) + VIX from yfinance
  loader.py             ← Orchestrator: runs all ingestion, emits quality report

signals/
  factors.py            ← Momentum (12-1), value, quality, low-vol factor scores
  technical.py          ← MA, RSI, MACD, ATR, volume signals
  macro_regime.py       ← Regime classifier (RISK_ON / RISK_OFF / TRANSITIONAL / CRISIS)
  options_flow.py       ← Unusual Whales integration for sweep/block signals

engine/
  engine_tools.py       ← Tool definitions, signal dispatch map
  anthropic_engine.py   ← AI recommendation engine (Anthropic tool use)
  recommendation.py     ← Structured recommendation output format

portfolio/
  paper_trades.py       ← Trade log: entry, thesis, exit, P&L
  performance.py        ← Sharpe, drawdown, win rate, 6-month return reports

app/
  main.py               ← Streamlit UI (local)
  api.py                ← FastAPI: POST /recommend, GET /portfolio, GET /health

tests/
  conftest.py           ← Shared fixtures
  test_signals.py       ← Signal function tests with known-value assertions
  test_etl.py           ← ETL idempotency and schema tests
```

---

## Data Model

Core tables in DuckDB (`data/quant.db`):

- `universe` — S&P 500 constituents (ticker, sector, industry)
- `prices` — Daily OHLCV, one row per ticker × date
- `fundamentals` — P/E, ROE, margins, earnings date, fetched weekly
- `macro` — Daily yield curve, fed funds rate, CPI, VIX
- `signals` — Computed factor + technical scores per ticker per date (gold layer)
- `paper_trades` — Trade log for model portfolio

---

## Agent Routing for This Project

| Task type | Agent |
|---|---|
| Signal functions, factor logic, options flow | `quant-finance` → then `data-engineer` |
| ETL pipeline, DuckDB schema, ingestion | `data-engineer` |
| AI engine, tool definitions, prompts | `ai-engineer` |
| Performance dashboard, Next.js charts | `frontend-engineer` |
| ADRs, architecture docs | `technical-writer` |

Always run `quant-finance` review before merging any signal function.

---

## How to Run

```bash
cd /Users/yashvajifdar/Workspace/projects/quant-intelligence
source venv/bin/activate
cp .env.example .env       # fill in FRED_API_KEY
python -m etl.loader --full-refresh   # initial 2-year backfill (~10 min)
python -m etl.loader                  # daily incremental update
```

Tests:
```bash
python -m pytest tests/ -v
```

---

## Key Docs

| Doc | Purpose |
|---|---|
| `docs/glossary.md` | Plain-English definitions of every signal, indicator, and metric |
| `docs/decisions/` | ADRs — architecture decisions |

---

## Non-Negotiables

- Every paper trade is logged at **closing price on recommendation day** — no lookahead bias
- Every signal function has a known-value test before it ships
- `FRED_API_KEY` and any future brokerage keys go in `.env` only — never in code
- `quant-finance` agent reviews every signal function before merge
