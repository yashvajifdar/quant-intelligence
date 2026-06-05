# Quant Intelligence — Operations Runbook

Step-by-step procedures for every operational task on this project.
All commands assume the working directory is the project root: `/Users/yashvajifdar/Workspace/projects/quant-intelligence`.

---

## 1. Prerequisites

Before running any command in this project:

1. Python 3.11+ installed
2. A FRED API key — free at https://fred.stlouisfed.org/docs/api/api_key.html
3. The virtual environment activated (see Section 2 for creation)

```bash
# Verify Python version
python --version
# Expected: Python 3.11.x or higher
```

---

## 2. First-Time Setup

Run these steps once after cloning the repository.

```bash
# 1. Create the virtual environment
python -m venv venv

# 2. Activate it
source venv/bin/activate
# Prompt shows (venv) when active. If you see ModuleNotFoundError on any command below, the venv is not active.

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create your .env file
cp .env.example .env
```

Open `.env` and fill in your FRED API key:

```
FRED_API_KEY=your_fred_api_key_here
QUANT_DB_PATH=data/quant.db
```

The `.env` file is gitignored. Never commit it.

---

## 3. Run the ETL

### Full refresh (first run or data repair)

```bash
python -m etl.loader --full-refresh
```

Expected runtime: ~10 minutes. Downloads 2 years of OHLCV data for ~500 tickers in batches of 100, then pulls 2 years of FRED macro data.

### Incremental update (daily use)

```bash
python -m etl.loader
```

Pulls the last 7 days of prices and the last 45 days of macro data. Runs in under 2 minutes.

### Reading the quality report

Both commands print a quality report on completion:

```
── ETL Quality Report ──────────────────────────────────
  Mode:       FULL REFRESH
  Universe:   503 tickers
  Prices:     253,412 rows  |  0 batches failed  |  503 tickers attempted
  Macro:      731 rows (yield curve, fed funds, CPI, VIX)
  Elapsed:    587.3s
────────────────────────────────────────────────────────
```

| Field | What it means |
|---|---|
| `Universe` | Number of tickers loaded from Wikipedia's S&P 500 list. Expected: ~500 (constituents change over time). |
| `Prices rows` | Total OHLCV rows written. For a 2-year full refresh: expect 250,000–260,000 rows (~503 tickers × ~502 trading days). |
| `batches failed` | Number of 100-ticker batches where yfinance returned an error. Nonzero means some tickers are missing data. Check logs above the report for batch-specific errors. |
| `tickers attempted` | Should match the universe count. |
| `Macro rows` | Days of macro data written. For a 2-year full refresh: expect ~730 rows. |
| `Elapsed` | Total pipeline runtime in seconds. |

---

## 4. Verify Data Loaded

Connect to the database and run these queries to confirm the ETL wrote correctly.

```bash
# Open DuckDB CLI
python -c "import duckdb; conn = duckdb.connect('data/quant.db'); print(conn.execute('SELECT COUNT(*) FROM universe').fetchone())"
```

Or use the DuckDB CLI directly if installed:

```bash
duckdb data/quant.db
```

Then run:

```sql
-- Check universe
SELECT COUNT(*) FROM universe;
-- Expected: ~500

-- Check prices
SELECT COUNT(*), MIN(date), MAX(date) FROM prices;
-- Expected: ~250,000+ rows; MIN(date) ~2 years ago; MAX(date) yesterday or today

-- Check macro
SELECT COUNT(*), MIN(date), MAX(date) FROM macro;
-- Expected: ~700+ rows; MIN(date) ~2 years ago; MAX(date) within last week

-- Spot-check for gaps (should return no tickers with fewer than 400 trading days on a full refresh)
SELECT ticker, COUNT(*) AS days FROM prices GROUP BY ticker HAVING days < 400 ORDER BY days;
```

---

## 5. Render Deployment

### 5.1 Create the web service

1. Go to [render.com](https://render.com) and sign in with GitHub.
2. Click **New → Web Service**.
3. Connect the `yashvajifdar/quant-intelligence` GitHub repo.
4. Configure:
   - **Name:** `quant-intelligence`
   - **Region:** US East
   - **Branch:** `main`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn app.api:app --host 0.0.0.0 --port $PORT`
   - **Instance type:** Starter ($7/month) or Free

### 5.2 Add the persistent disk

The persistent disk is required. Without it, `quant.db` is destroyed on every redeploy.

1. In the web service settings, click **Disks → Add Disk**.
2. Set:
   - **Name:** `quant-data`
   - **Mount Path:** `/data`
   - **Size:** 1 GB
3. Click **Save**. Cost: $1/month.

### 5.3 Set environment variables

In the Render web service settings under **Environment**:

| Variable | Value |
|---|---|
| `FRED_API_KEY` | Your FRED API key |
| `QUANT_DB_PATH` | `/data/quant.db` |
| `ANTHROPIC_API_KEY` | Your Anthropic key (add when AI engine ships) |

### 5.4 Add the daily ETL cron job

1. Go to [render.com](https://render.com) → **New → Cron Job**.
2. Connect the same repo.
3. Configure:
   - **Name:** `quant-etl-daily`
   - **Schedule:** `0 11 * * 1-5` (6:00 AM ET = 11:00 AM UTC, weekdays only)
   - **Build Command:** `pip install -r requirements.txt`
   - **Command:** `python -m etl.loader`
4. Under **Environment**, set the same variables as the web service.
5. Click **Save**.

The cron job runs the incremental ETL each weekday morning after US market open. It writes to the same persistent disk as the web service at `/data/quant.db`.

### 5.5 Verify the deployment

```bash
curl https://quant-intelligence.onrender.com/health
# Expected: {"status":"ok"}
```

---

## 6. Environment Variables

| Variable | Required | Where to get it | Notes |
|---|---|---|---|
| `FRED_API_KEY` | Yes | https://fred.stlouisfed.org/docs/api/api_key.html — free registration | ETL fails loudly with a clear error if this is missing |
| `QUANT_DB_PATH` | Yes | Set in `.env` | Local default: `data/quant.db`. Render: `/data/quant.db` |
| `ANTHROPIC_API_KEY` | When AI engine ships | https://console.anthropic.com | Not needed for ETL or signal layer |

Never put these values in code. Never commit `.env`. On Render, set them through the dashboard Environment tab.

---

## 7. Adding a New Signal Function

Follow every step. Skipping step 3 ships untested signal logic; skipping step 5 means the AI engine cannot call the function.

1. **Consult the `quant-finance` agent** for domain logic before writing any code. Signal construction errors compound; get the formula right before implementing.

2. **Add the function** to the appropriate file in `signals/`:
   - Factor signals: `signals/factors.py`
   - Technical indicators: `signals/technical.py`
   - Macro regime: `signals/macro_regime.py`
   - Options flow: `signals/options_flow.py`

   Each function must accept a DuckDB connection (or DataFrame) and return a normalized score between 0 and 1, ranked cross-sectionally unless it is a point-in-time indicator (e.g., RSI).

3. **Write a known-value test** in `tests/test_signals.py` before merging:
   ```python
   def test_momentum_score_known_value(fixed_prices_fixture):
       score = momentum_score(fixed_prices_fixture, ticker="AAPL")
       assert score == pytest.approx(0.62, abs=0.01)
   ```
   Row-count assertions are not sufficient. Test against a hard-coded expected value.

4. **Register the function** in `engine/engine_tools.py`:
   - Add a tool definition to `TOOL_DEFINITIONS`
   - Add the function to `SIGNAL_DISPATCH` map

5. **Run the `quant-finance` agent review** before merging. Post the function and its test to the agent for domain correctness check.

6. **Run the full test suite:**
   ```bash
   python -m pytest tests/ -v
   ```
   All tests must pass before merge.

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `EnvironmentError: FRED_API_KEY is not set` | `FRED_API_KEY` missing from `.env` or Render environment | Add the key to `.env` locally or Render environment dashboard. Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html |
| `N batches failed` in the quality report | yfinance rate-limited or Yahoo API returned an error for a batch | Check the logs above the quality report for the batch number and first ticker. Re-run `python -m etl.loader` — the 7-day lookback will fill gaps on the next incremental run. If persistent, reduce `BATCH_SIZE` in `etl/ingest_prices.py` to 50. |
| `duckdb.IOException: Cannot open file` | `QUANT_DB_PATH` points to a non-existent directory | Create the directory: `mkdir -p data`. Or verify `QUANT_DB_PATH` in `.env` matches the actual path. |
| `RuntimeError: Universe table is empty — run load_universe first` | `load_prices` was called before `load_universe` | Always run `python -m etl.loader` rather than calling ingestion modules directly. The orchestrator runs universe first. |
| `ModuleNotFoundError` on any command | Virtual environment not active | `source venv/bin/activate`. Confirm with `which python` — should show `.../venv/bin/python`. |
| Render cron job fails with `FRED_API_KEY not set` | Environment variables not set on the cron job | Render cron jobs have separate environment settings from the web service. Add `FRED_API_KEY` and `QUANT_DB_PATH` to the cron job's environment in the Render dashboard. |
| Render web service writes to the wrong DB path | `QUANT_DB_PATH` not set or set to `data/quant.db` instead of `/data/quant.db` | On Render, `QUANT_DB_PATH` must be `/data/quant.db` to use the persistent disk. The local default (`data/quant.db`) is not the persistent disk mount point. |
| VIX data missing from macro table | yfinance returned no data for `^VIX` | Verify the date range. `^VIX` data is available from 1990 onward. If the issue persists, check yfinance version: `pip show yfinance`. |
