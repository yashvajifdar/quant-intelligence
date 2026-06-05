# Quant Intelligence — Product Roadmap

*Last updated: 2026-06-05. Milestones are sequential; each gates the next.*

**Not financial advice.** All recommendations are for paper trading and educational/demonstration purposes only.

---

## Press Release (Working Backwards)

**FOR IMMEDIATE RELEASE**

**Yash Vajifdar Launches Public Investment Intelligence Platform Powered by Quantitative Signals and AI**

*Personal paper portfolio beats SPY by 4.2 percentage points in its first six months*

BOSTON — Yash Vajifdar today opened public access to Quant Intelligence, a personal investment research platform that combines institutional-grade factor signals with AI-assisted analysis and a fully transparent paper portfolio.

The platform ingests daily S&P 500 price data, computes momentum, value, quality, and low-volatility factor scores across all 500 constituents, layers in macro regime classification and unusual options activity, and uses an AI engine to produce structured weekly recommendations with explicit risk parameters. Every recommendation includes a price target, stop-loss level, and reward/risk ratio. Every paper trade is logged at market close on the day of recommendation, with no lookahead bias.

"I wanted to know whether the quant signals I read about in finance research actually hold up in practice," said Vajifdar. "The only way to find out is to run them live, log every trade, and publish the results. Six months in, the momentum and quality factor combination is working."

The public portfolio page at yashvajifdar.com/demos/quant shows current open positions, closed trade history with P&L, and performance metrics including Sharpe ratio, max drawdown, and win rate. The platform is open for inspection: methodology, signal logic, and trade log are all public.

---

## Milestone Map

### M1 — Data Foundation (current)

**Goal:** A clean, queryable DuckDB warehouse with 2-year S&P 500 price history and macro indicators.

**Scope:**
- DuckDB schema: `universe`, `prices`, `fundamentals`, `macro` tables
- ETL pipeline: `etl/loader.py` orchestrates universe, prices, and macro ingestion
- S&P 500 universe from Wikipedia (500 tickers)
- 2-year daily OHLCV history via yfinance batch download (100 tickers/batch)
- Macro indicators from FRED: T10Y2Y, FEDFUNDS, CPIAUCSL; VIX from yfinance
- Daily incremental update mode (7-day lookback for prices, 45-day for macro)

**Done when:**
```
python -m etl.loader --full-refresh
```
Completes without batch failures and the quality report shows:
- Universe: 500 tickers
- Prices: ≥ 250,000 rows, MIN(date) approximately 2 years prior
- Macro: ≥ 700 rows, no null streaks longer than 5 days on `t10y2y`

**Status:** In progress. ETL pipeline is implemented and tested.

---

### M2 — Signal Layer

**Goal:** Every S&P 500 ticker has a daily composite signal score backed by factor, technical, and macro inputs.

**Scope:**
- `signals/factors.py`: momentum 12-1, value composite (P/E, P/B, P/S, EV/EBITDA), quality composite (ROE, gross margin, debt/equity, FCF), low volatility (252-day std dev)
- `signals/technical.py`: MA50/200 crossover flag, RSI 14, MACD (12/26/9) crossover flag, ATR 14
- `signals/macro_regime.py`: four-state classifier (RISK_ON / RISK_OFF / TRANSITIONAL / CRISIS)
- Gold layer: `signals` table in `quant.db` with all scores joined per ticker per date
- `quant-finance` agent review on every signal function before merge

**Done when:** Every signal function has a known-value test. Example:
```python
# Given AAPL prices from a fixed 6-month window:
# momentum_score(aapl_prices) == pytest.approx(0.62, abs=0.01)
```
Running `python -m pytest tests/test_signals.py -v` passes with 0 failures.

**Gate:** Macro regime classifier must be complete before M3. The AI engine reads the current regime from the `signals` table to gate recommendations.

---

### M3 — AI Recommendation Engine

**Goal:** An AI engine that ingests signal scores and returns structured investment recommendations.

**Scope:**
- `engine/engine_tools.py`: provider-neutral tool definitions for all signal functions
- `engine/anthropic_engine.py`: two-turn Anthropic tool-use flow (same pattern as lumber-ai-analytics)
- `engine/recommendation.py`: typed dataclass with ticker, action, conviction, signal summary, thesis, entry price, stop loss, target, reward/risk ratio
- Enforcement: recommendations with reward/risk < 2.0 are suppressed before return
- `app/main.py`: Streamlit local UI for interactive querying
- No lookahead bias: signals use prior day's close; entries logged at current day's close

**Done when:** Running `streamlit run app/main.py` and asking "what should I buy this week?" returns a response with:
- At least one ticker with action, conviction (1–5), macro regime label
- Signal summary showing numeric scores for ≥ 3 factors
- A 1–3 sentence thesis
- Entry price, stop loss, target, and reward/risk ratio

---

### M4 — Paper Portfolio Tracker

**Goal:** A persistent trade log that records paper positions and computes verified performance metrics.

**Scope:**
- `portfolio/paper_trades.py`: write entries to `paper_trades` table; enforce close-price-only entry rule
- `portfolio/performance.py`: Sharpe ratio, max drawdown, win rate, avg win/loss ratio, total return vs SPY
- Render cron job updated to run signal computation and any new paper trade entries daily at 6 AM ET
- 6-month performance report format defined

**Done when:** 5 paper trades are logged (entries and exits), and `performance.py` renders a report showing Sharpe, max drawdown, win rate, and total return for the logged trades.

---

### M5 — Public Deployment

**Goal:** Quant Intelligence is live on yashvajifdar.com with a publicly visible paper portfolio and current recommendations.

**Scope:**
- FastAPI backend deployed on Render with persistent disk (`QUANT_DB_PATH=/data/quant.db`)
- Render cron job: `python -m etl.loader` daily at 6:00 AM ET
- `yashvajifdar.com/demos/quant` Next.js page: current week's recommendations, open positions, closed trade log, 6-month performance chart
- Methodology page: plain-English description of every signal, not-financial-advice disclaimer prominent

**Done when:** `https://yashvajifdar.com/demos/quant` loads without error, shows at least one open paper position, and the closed trade log has a minimum of 5 entries with P&L.

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| yfinance rate-limiting or API breakage | High | High | Data source isolated to `etl/ingest_prices.py`. Batch size of 100 already limits request rate. Polygon.io is the fallback if Yahoo cuts access; cost is $29/month. |
| DuckDB persistence on Render | Low (mitigated) | High | Render persistent disk ($1/month) mounts at `/data`. `QUANT_DB_PATH=/data/quant.db`. Without the disk, every deploy destroys price history. |
| Unusual Whales API uncertainty | High | Medium | No official API. Options flow is a +1 modifier, not a core signal. MVP ships without it if a clean integration path is not found before M3. Write an ADR before any implementation work. |
| Survivorship bias in signal backtesting | Certain | Medium | Wikipedia universe reflects current constituents only. Any performance number computed over historical data overstates returns because failed companies are absent. Document prominently on every public-facing page. |
| Not-financial-advice legal exposure | Low | High | Disclaimer on every public page and every recommendation output. No price targets framed as guarantees. Paper portfolio clearly labeled as simulated. No brokerage integration in scope. |
