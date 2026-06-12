# Quant Intelligence — Operations Runbook

Step-by-step procedures for every operational task on this project.
All commands assume the working directory is the project root: `/Users/yashvajifdar/Workspace/projects/quant-intelligence`.

---

## 1. Prerequisites

Before running any command in this project:

1. Python 3.11+ installed
2. A FRED API key — free at https://fred.stlouisfed.org/docs/api/api_key.html
3. An Anthropic API key — https://console.anthropic.com (required for `/recommend`)
4. `PAPER_ACCOUNT_VALUE` set to `100000` in environment (controls max position size)
5. The virtual environment activated (see Section 2 for creation)

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

Open `.env` and fill in your keys:

```
FRED_API_KEY=your_fred_api_key_here
QUANT_DB_PATH=data/quant.db
ANTHROPIC_API_KEY=your_anthropic_api_key_here
PAPER_ACCOUNT_VALUE=100000
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
| `ANTHROPIC_API_KEY` | Your Anthropic API key (required — `/recommend` returns 503 without it) |
| `PAPER_ACCOUNT_VALUE` | `100000` |
| `ETL_SECRET` | A secret string shared between Render and GitHub Actions. Must match the `ETL_SECRET` repository secret in GitHub (Settings → Secrets → Actions). |

### 5.4 Configure the daily ETL (GitHub Actions)

The daily ETL runs via GitHub Actions, not a Render cron job. Render persistent disks attach to one service at a time — a Render cron job writes to a separate database instance that the web service cannot read from.

The workflow file already exists at `.github/workflows/daily-etl.yml`. Schedule: `0 11 * * 1-5` (11:00 UTC = 6:00 AM ET, weekdays only). On each run, it issues `curl -X POST /internal/run-etl` with an `X-Etl-Secret` header to authenticate.

**Add the ETL secret to GitHub:**

1. Go to the repo on GitHub → **Settings → Secrets and variables → Actions**.
2. Click **New repository secret**.
3. Name: `ETL_SECRET`. Value: the same secret set in the Render environment as `ETL_SECRET`.
4. Click **Add secret**.

**Trigger a manual run:**

1. Go to the repo on GitHub → **Actions → Daily ETL**.
2. Click **Run workflow** → **Run workflow**.

The workflow runs the incremental ETL. Confirm success by checking `/health` after the run completes.

### 5.5 Verify the deployment

```bash
curl https://quant-intelligence.onrender.com/health
```

Expected response:

```json
{"status":"ok","db":{"universe":503,"prices":250986,"macro":735}}
```

---

## 6. Test the AI Engine

### 6.1 Call /recommend

```bash
curl -X POST https://quant-intelligence.onrender.com/recommend \
  -H "Content-Type: application/json" \
  -d '{"question":"What should I buy this week?"}'
```

Expected: JSON with keys `recommendations` (array of recommendation objects) and `macro` (object with regime and macro data). Takes 15–45 seconds — cold start plus 3 sequential tool calls (macro regime, factor candidates, technical signals).

Common errors:

| Error | Cause | Fix |
|---|---|---|
| `503 {"detail":"ANTHROPIC_API_KEY environment variable is not set"}` | Key missing from Render environment | Add `ANTHROPIC_API_KEY` in Render dashboard under **Environment**, then click **Manual Deploy** to pick it up. |
| `500` | Engine failure | Check Render logs. Most common cause: `JSONDecodeError` from model response. Check `engine/anthropic_engine.py` `_parse_json()` output in logs. |
| Response takes > 60 seconds | Render free tier cold start | Expected behavior after inactivity. First request after idle wakes the service (30–60 seconds). Subsequent requests are fast. |

---

## 7. Portfolio API

All endpoints are on `https://quant-intelligence.onrender.com`.

### 7.1 Create a portfolio

```bash
curl -X POST https://quant-intelligence.onrender.com/portfolio \
  -H "Content-Type: application/json" \
  -d '{"display_name":"My Paper Portfolio","is_primary":true}'
```

Returns a `portfolio_id` UUID. Store it — all trade operations require it.

### 7.2 Get portfolio state

```bash
curl https://quant-intelligence.onrender.com/portfolio/{portfolio_id}
```

Returns open trades, closed trades, and computed performance metrics (Sharpe, max drawdown, win rate).

### 7.3 Open a trade

```bash
curl -X POST https://quant-intelligence.onrender.com/portfolio/{portfolio_id}/trades \
  -H "Content-Type: application/json" \
  -d '{
    "ticker": "NVDA",
    "action": "BUY",
    "entry_price": 875.50,
    "shares": 11.4,
    "stop": 840.00,
    "target": 947.00,
    "thesis": "Momentum 12-1 top decile, RISK_ON regime, RSI 58."
  }'
```

Returns the new trade record including its `trade_id`.

### 7.4 Close a trade

```bash
curl -X PATCH https://quant-intelligence.onrender.com/portfolio/{portfolio_id}/trades/{trade_id} \
  -H "Content-Type: application/json" \
  -d '{
    "exit_price": 942.00,
    "exit_reason": "Target hit"
  }'
```

Sets `exit_date` to today, computes and stores `realized_pnl`.

### 7.5 Review open positions (AI analysis)

```bash
curl -X POST https://quant-intelligence.onrender.com/portfolio/{portfolio_id}/review
```

Runs the review engine against all open positions. Takes 20–45 seconds. Returns per-position verdicts (`HOLD`, `ADD`, `TRIM`, `EXIT`) with updated thesis and risk notes, plus portfolio-level insights: regime impact, concentration risk, hedge suggestions, and diversifier picks from the top factor candidates.

Returns `422` if the portfolio has no open trades. Returns `503` if `ANTHROPIC_API_KEY` is missing. Returns `500` on engine failure — check Render logs.

### 7.6 Get the leaderboard

```bash
curl https://quant-intelligence.onrender.com/leaderboard
```

Returns portfolios ranked by Sharpe ratio. Only portfolios with at least 1 closed trade appear.

### 7.7 Get all tickers ranked by factor score

```bash
curl https://quant-intelligence.onrender.com/signals
```

Returns all 503 tickers ranked by composite factor score. Fields per ticker: `ticker`, `company_name`, `sector`, `composite_score`, `momentum_rank`, `lowvol_rank`, `as_of_date`. Results are cached in memory for 4 hours — the `as_of_date` field shows the date of the underlying price data, not the time of the API call.

---

## 8. Frontend / Vercel

**Live URL:** https://yashvajifdar.com/demos/quant

### 8.1 Environment variable

Set in the Vercel dashboard under **Settings → Environment Variables**:

| Variable | Value |
|---|---|
| `NEXT_PUBLIC_QUANT_API_URL` | `https://quant-intelligence.onrender.com` |

### 8.2 Local development

Add to `personal-website/.env.local`:

```
NEXT_PUBLIC_QUANT_API_URL=http://localhost:8002
```

Start the FastAPI backend locally first:

```bash
cd /Users/yashvajifdar/Workspace/projects/quant-intelligence
source venv/bin/activate
uvicorn app.api:app --host 0.0.0.0 --port 8002
```

Then start the Next.js dev server:

```bash
cd /Users/yashvajifdar/Workspace/projects/personal-website
npm run dev
```

Open http://localhost:3000/demos/quant.

### 8.3 Trigger a Vercel redeploy

Push to `main` on the `personal-website` repo. Vercel auto-deploys on every push to main.

To force a manual redeploy without a code change: go to the Vercel dashboard → **Deployments** → click **Redeploy** on the latest deployment.

---

## 9. Adding a New Signal Function

Follow every step. Skipping step 3 ships untested signal logic; skipping step 5 means the AI engine cannot call the function.

1. **Consult the `quant-finance` agent** for domain logic before writing any code. Signal construction errors compound; get the formula right before implementing.

2. **Add the function** to the appropriate file in `signals/`:
   - Factor signals: `signals/factors.py`
   - Technical indicators: `signals/technical.py`
   - Macro regime: `signals/macro_regime.py`
   - Options flow: `signals/options_flow.py` (NOT YET BUILT — write an ADR first)

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
   - Add the function to `TOOL_DISPATCH` map

5. **Run the `quant-finance` agent review** before merging. Post the function and its test to the agent for domain correctness check.

6. **Run the full test suite:**
   ```bash
   python -m pytest tests/ -v
   ```
   All 84 tests must pass before merge.

---

## 10. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `EnvironmentError: FRED_API_KEY is not set` | `FRED_API_KEY` missing from `.env` or Render environment | Add the key to `.env` locally or Render environment dashboard. Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html |
| `N batches failed` in the quality report | yfinance rate-limited or Yahoo API returned an error for a batch | Check the logs above the quality report for the batch number and first ticker. Re-run `python -m etl.loader` — the 7-day lookback will fill gaps on the next incremental run. If persistent, reduce `BATCH_SIZE` in `etl/ingest_prices.py` to 50. |
| `duckdb.IOException: Cannot open file` | `QUANT_DB_PATH` points to a non-existent directory | Create the directory: `mkdir -p data`. Or verify `QUANT_DB_PATH` in `.env` matches the actual path. |
| `RuntimeError: Universe table is empty — run load_universe first` | `load_prices` was called before `load_universe` | Always run `python -m etl.loader` rather than calling ingestion modules directly. The orchestrator runs universe first. |
| `ModuleNotFoundError` on any command | Virtual environment not active | `source venv/bin/activate`. Confirm with `which python` — should show `.../venv/bin/python`. |
| GitHub Actions ETL fails with `401 Unauthorized` | `ETL_SECRET` in Actions secrets does not match the value set in Render environment | Re-set `ETL_SECRET` in one location to match the other. See Section 5.4 for setup steps. |
| Render web service writes to the wrong DB path | `QUANT_DB_PATH` not set or set to `data/quant.db` instead of `/data/quant.db` | On Render, `QUANT_DB_PATH` must be `/data/quant.db` to use the persistent disk. The local default (`data/quant.db`) is not the persistent disk mount point. |
| VIX is null in `/recommend` macro snapshot | yfinance ≥0.2 returns MultiIndex columns even for a single ticker; `_fetch_vix()` was not flattening them, so `raw[["Close"]]` silently returned an empty frame | Already patched in commit a36be54. If VIX is null again, check yfinance version with `pip show yfinance` — a major yfinance upgrade may have changed the column structure again. |
| VIX data missing from macro table entirely | yfinance returned no data for `^VIX` | Verify the date range passed to `_fetch_vix()`. `^VIX` data is available from 1990 onward. If the issue persists, check yfinance version: `pip show yfinance`. |
| Live site shows "No recommendations yet" and never loads | `ANTHROPIC_API_KEY` not set on Render, or Render has not picked up a recently added env var | In the Render dashboard, verify `ANTHROPIC_API_KEY` is set under **Environment**. Then click **Manual Deploy** to force the service to restart with the new value. |
| `error: Port 8002 already in use` when running uvicorn locally | Previous uvicorn process still running in the background | `lsof -ti:8002 \| xargs kill -9` then restart uvicorn. |
| `/recommend` returns `500` in under 1 second | Anthropic credit balance is depleted — the API call is rejected before any tool runs | Top up credits at console.anthropic.com → Plans & Billing. No code change needed. |
| `/portfolio/{id}/review` returns `500` | Review engine failure — same causes as `/recommend` | Check Render logs for the traceback. Most common: depleted Anthropic credits or a malformed tool response. |
