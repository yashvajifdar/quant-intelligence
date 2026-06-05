# ADR-0001: Render + Persistent Disk as Deployment Target

**Status:** Accepted
**Date:** 2026-06-05
**Deciders:** Yash Vajifdar

---

## Context

The quant-intelligence platform needs:
1. A web service to serve the FastAPI recommendation API
2. A scheduled ETL job to refresh market data daily
3. A durable place to store `quant.db` (DuckDB) — the file cannot be regenerated on every deploy the way synthetic data can

The lumber-ai-analytics project already runs on Render (free tier, web service only). This project has the same deployment target but adds two constraints the lumber project did not have: persistent file storage and a scheduled background job.

### Options considered

**Option A: Render web service + persistent disk + cron job**
- Persistent disk ($1/month, 1GB) mounts at `/data` — `quant.db` survives deploys
- Render cron job runs `etl/loader.py` daily at 6am ET
- Same hosting pattern as lumber project — minimal new infrastructure
- Constraint: Render cron jobs run on separate instances from the web service; they access the same disk via the `QUANT_DB_PATH` env var pointing to `/data/quant.db`

**Option B: DuckDB on S3 — download on startup, upload after ETL**
- No persistent disk needed
- ETL downloads `quant.db` from S3, runs, uploads result
- Adds ~30s on every cold start and every ETL run
- Requires AWS credentials and S3 bucket management
- More moving parts for a personal project

**Option C: Migrate to Render Postgres**
- Free managed Postgres on Render
- Loses DuckDB columnar performance for analytical queries
- Requires full schema rewrite (DuckDB SQL → Postgres SQL)
- No compelling reason to pay that cost at this scale

---

## Decision

**Option A — Render persistent disk.**

The $1/month cost is negligible. The disk is transparent to the application: only `QUANT_DB_PATH` changes between local (`.env`) and production (`/data/quant.db`). No code changes required.

---

## Implementation

`render.yaml` defines:
- `web` service: FastAPI, persistent disk mounted at `/data`
- `cron` service: daily ETL at `0 11 * * 1-5` (6am ET, weekdays)

Both services read `QUANT_DB_PATH=/data/quant.db` from environment variables set in the Render dashboard.

**Environment variables — set in Render dashboard, never committed:**

| Variable | Service(s) | Source |
|---|---|---|
| `FRED_API_KEY` | web + cron | fred.stlouisfed.org |
| `ANTHROPIC_API_KEY` | web only | console.anthropic.com |
| `QUANT_DB_PATH` | web + cron | hardcoded to `/data/quant.db` in render.yaml |

---

## Consequences

**What becomes easier:**
- Zero infrastructure management — Render handles uptime, TLS, and scaling
- `QUANT_DB_PATH` is the only thing that changes between local dev and production
- Daily ETL runs automatically without any manual intervention

**What to watch:**
- Render cron jobs run on a fresh instance (not the same container as the web service). Both mount the same persistent disk via env var — this is the expected pattern and Render supports it, but if the disk path ever changes, both services must be updated in sync.
- 1GB disk is enough for ~2 years of S&P 500 daily OHLCV + macro. Monitor disk usage when the fundamentals and signals tables are added.

**What to revisit:**
- If the ETL ever needs to run more frequently than daily (e.g., intraday signals), Render cron minimum interval is 1 minute — this is sufficient. No platform change needed.
- If the project scales to multiple clients or requires real-time data, revisit S3-backed DuckDB (Option B) or a hosted analytical database.
