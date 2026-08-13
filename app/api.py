"""FastAPI backend for the Quant Intelligence platform.

Exposes:
  GET   /health                                  — liveness check + database row counts
  POST  /recommend                               — AI investment recommendations
  POST  /portfolio                               — create a new paper portfolio
  GET   /portfolio/{portfolio_id}                — portfolio detail + performance summary
  POST  /portfolio/{portfolio_id}/trades         — open a new paper trade
  PATCH /portfolio/{portfolio_id}/trades/{trade_id} — close an existing trade
  GET   /leaderboard                             — top 10 portfolios by realized P&L

Deployed on Render. Personal website at yashvajifdar.com proxies here.

Run locally:
  cd /path/to/quant-intelligence
  source venv/bin/activate
  uvicorn app.api:app --reload --port 8002
"""

from __future__ import annotations

import dataclasses
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import duckdb
import yfinance as yf
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from portfolio import paper_trades as pt
from portfolio.performance import compute_summary
from signals.factors import compute_combined_factor_score

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("quant_api")

DB_PATH = os.environ.get("QUANT_DB_PATH", "data/quant.db")
ETL_SECRET = os.environ.get("ETL_SECRET", "")

_ALLOWED_ORIGINS = [
    "https://yashvajifdar.com",
    "https://www.yashvajifdar.com",
    "http://localhost:3000",
    "http://localhost:3001",
]


def _fetch_current_prices(tickers: list[str]) -> dict[str, float]:
    """Return the latest available close price for each ticker via yfinance.

    Uses a 5-day window so the last valid close is available even on weekends
    or holidays when markets are closed. Returns empty dict on any failure
    so the portfolio endpoint still responds — unrealized P&L just shows None.
    """
    if not tickers:
        return {}
    try:
        import pandas as pd
        raw = yf.download(tickers, period="5d", auto_adjust=True, progress=False)
        if raw.empty:
            return {}
        prices: dict[str, float] = {}
        if isinstance(raw.columns, pd.MultiIndex):
            # Multiple tickers — raw["Close"] is a DataFrame keyed by ticker
            close = raw["Close"]
            for t in tickers:
                if t in close.columns:
                    series = close[t].dropna()
                    if not series.empty:
                        prices[t] = float(series.iloc[-1])
        else:
            # Single ticker — raw["Close"] is a Series
            series = raw["Close"].dropna()
            if not series.empty:
                prices[tickers[0]] = float(series.iloc[-1])
        return prices
    except Exception:
        logger.warning("Live price fetch failed for %s", tickers)
        return {}


def _db_stats() -> dict:
    """Return row counts for each warehouse table.

    Returns {"locked": true} if the ETL subprocess currently holds the write
    lock — this keeps /health returning 200 so Render does not restart the
    instance mid-ETL and kill the fundamentals fetch.
    """
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = duckdb.connect(DB_PATH, read_only=True)
        stats = {
            "universe": conn.execute("SELECT COUNT(*) FROM universe").fetchone()[0],
            "prices":   conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0],
            "macro":    conn.execute("SELECT COUNT(*) FROM macro").fetchone()[0],
        }
        conn.close()
        return stats
    except Exception:
        # DB is locked by the ETL subprocess — return a degraded response
        # so /health stays 200 and Render does not restart the instance.
        return {"locked": True, "reason": "ETL in progress"}


def _ensure_db() -> None:
    """Run full-refresh ETL if the database doesn't exist yet.

    Handles first deploy on Render where the persistent disk starts empty.
    Subsequent deploys skip this — the db persists on the disk.
    """
    if os.path.exists(DB_PATH):
        return
    logger.info("Database not found — running ETL full refresh (first deploy)")
    from etl.loader import run as etl_run
    etl_run(full_refresh=True)
    logger.info("ETL complete — database ready at %s", DB_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Quant Intelligence API starting — db=%s", DB_PATH)
    _ensure_db()
    stats = _db_stats()
    logger.info("Database ready: %s", stats)
    yield
    logger.info("Shutting down")


app = FastAPI(title="Quant Intelligence API", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type"],
)


# ── request / response models ─────────────────────────────────────────────────

class RecommendRequest(BaseModel):
    question: str


class CreatePortfolioRequest(BaseModel):
    display_name: str | None = None


class OpenTradeRequest(BaseModel):
    ticker: str
    action: str                  # BUY or SHORT
    entry_price: float
    shares: int
    stop: float
    target: float
    signal_snapshot: dict
    thesis: str


class CloseTradeRequest(BaseModel):
    exit_price: float
    exit_reason: str             # HIT_TARGET, HIT_STOP, or MANUAL


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    """Liveness check. Returns database row counts so we can verify ETL ran."""
    return {"status": "ok", "db": _db_stats()}


@app.post("/recommend")
def recommend(req: RecommendRequest) -> dict:
    """Run the AI recommendation engine for a natural language query.

    Calls the Anthropic engine, which runs the multi-tool signal loop and
    returns up to 3 typed recommendations with full signal summaries and
    risk parameters.

    Returns the RecommendationSet serialized to JSON-compatible dict.
    """
    logger.info("recommend request: %r", req.question)

    try:
        from engine.anthropic_engine import run
        result = run(req.question)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.exception("Engine error for query %r", req.question)
        raise HTTPException(status_code=500, detail="Recommendation engine error")

    return dataclasses.asdict(result)


@app.post("/portfolio")
def create_portfolio(req: CreatePortfolioRequest) -> dict:
    """Create a new paper portfolio.

    Returns the new portfolio_id and display_name.
    """
    portfolio_id = pt.create_portfolio(DB_PATH, display_name=req.display_name)
    logger.info("portfolio created: %s display_name=%r", portfolio_id, req.display_name)
    return {"portfolio_id": portfolio_id, "display_name": req.display_name}


@app.get("/portfolio/{portfolio_id}")
def get_portfolio(portfolio_id: str) -> dict:
    """Return open trades, closed trades, and performance summary for a portfolio."""
    row = pt.get_portfolio(DB_PATH, portfolio_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Portfolio {portfolio_id!r} not found")

    open_trades = pt.get_open_trades(DB_PATH, portfolio_id)
    closed_trades = pt.get_closed_trades(DB_PATH, portfolio_id)
    summary = compute_summary(closed_trades, open_count=len(open_trades))

    # Attach live unrealized P&L to each open trade
    tickers = [t["ticker"] for t in open_trades]
    current_prices = _fetch_current_prices(tickers)
    for trade in open_trades:
        cp = current_prices.get(trade["ticker"])
        if cp is not None:
            trade["unrealized_pnl"] = round((cp - trade["entry_price"]) * trade["shares"], 2)
            trade["current_price"] = round(cp, 2)
        else:
            trade["unrealized_pnl"] = None
            trade["current_price"] = None

    return {
        "portfolio": row,
        "open_trades": open_trades,
        "closed_trades": closed_trades,
        "performance": summary,
    }


@app.post("/portfolio/{portfolio_id}/review")
def review_portfolio(portfolio_id: str) -> dict:
    """Run the AI review engine against all open positions in a portfolio.

    Calls get_macro_regime, get_technical_signals on held tickers, and
    get_top_factor_candidates to produce per-position verdicts and
    portfolio-level insights (concentration, hedges, diversifiers).
    """
    row = pt.get_portfolio(DB_PATH, portfolio_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Portfolio {portfolio_id!r} not found")

    open_trades = pt.get_open_trades(DB_PATH, portfolio_id)
    if not open_trades:
        raise HTTPException(status_code=422, detail="No open positions to review")

    try:
        from engine.review_engine import run_review
        result = run_review(open_trades)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception:
        logger.exception("Review engine error for portfolio %s", portfolio_id)
        raise HTTPException(status_code=500, detail="Review engine error")

    return dataclasses.asdict(result)


@app.post("/portfolio/{portfolio_id}/trades")
def open_trade(portfolio_id: str, req: OpenTradeRequest) -> dict:
    """Open a new paper trade on the given portfolio.

    entry_price must be today's closing price — caller is responsible for
    passing the correct price. No price fetching occurs here.

    Returns the new trade_id.
    """
    row = pt.get_portfolio(DB_PATH, portfolio_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Portfolio {portfolio_id!r} not found")

    try:
        trade_id = pt.open_trade(
            DB_PATH,
            portfolio_id=portfolio_id,
            ticker=req.ticker,
            action=req.action,
            entry_price=req.entry_price,
            shares=req.shares,
            stop=req.stop,
            target=req.target,
            signal_snapshot=req.signal_snapshot,
            thesis=req.thesis,
        )
    except AssertionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    logger.info("trade opened: %s ticker=%s portfolio=%s", trade_id, req.ticker, portfolio_id)
    return {"trade_id": trade_id}


@app.patch("/portfolio/{portfolio_id}/trades/{trade_id}")
def close_trade(portfolio_id: str, trade_id: str, req: CloseTradeRequest) -> dict:
    """Close an open paper trade and record realized P&L.

    Returns the trade_id and the computed realized_pnl.
    """
    row = pt.get_portfolio(DB_PATH, portfolio_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Portfolio {portfolio_id!r} not found")

    try:
        pt.close_trade(DB_PATH, trade_id, req.exit_price, req.exit_reason)
    except AssertionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    closed = pt.get_closed_trades(DB_PATH, portfolio_id)
    trade = next((t for t in closed if t["id"] == trade_id), None)
    realized_pnl = trade["realized_pnl"] if trade else None

    logger.info(
        "trade closed: %s exit_price=%.2f reason=%s pnl=%s",
        trade_id, req.exit_price, req.exit_reason, realized_pnl,
    )
    return {"trade_id": trade_id, "realized_pnl": realized_pnl}


_signals_cache: dict = {"data": None, "expires_at": datetime.min}
_SIGNALS_TTL = timedelta(hours=4)


@app.get("/signals")
def get_signals() -> dict:
    """Return all S&P 500 tickers ranked by composite factor score.

    Scores are cross-sectionally ranked 0–100 (higher = better).
    Composite weights: momentum 40%, quality 25%, low-vol 20%, value 15%.
    Falls back to 2-factor (momentum 60%, low-vol 40%) when fundamentals table
    is empty. Results are cached for 4 hours — scores only change after daily ETL.
    """
    global _signals_cache
    now = datetime.utcnow()
    if _signals_cache["data"] is None or now > _signals_cache["expires_at"]:
        import math
        scores = compute_combined_factor_score(DB_PATH)
        if scores.empty:
            raise HTTPException(status_code=503, detail="Signal data not available — run ETL first")

        conn = duckdb.connect(DB_PATH, read_only=True)
        universe = conn.execute(
            "SELECT ticker, company_name, sector FROM universe"
        ).df()
        as_of_row = conn.execute("SELECT MAX(date) FROM prices").fetchone()
        conn.close()

        as_of = str(as_of_row[0]) if as_of_row and as_of_row[0] else None
        merged = scores.merge(universe, on="ticker", how="left")
        cols = ["ticker", "company_name", "sector", "composite_score", "momentum_rank", "lowvol_rank", "value_score", "quality_score"]
        # Replace NaN with None so JSON serialization is valid (NaN is not valid JSON)
        records = (
            merged[cols]
            .where(merged[cols].apply(lambda s: s.map(lambda v: not (isinstance(v, float) and math.isnan(v)))), other=None)
            .to_dict(orient="records")
        )

        _signals_cache = {
            "data": {"tickers": records, "count": len(records), "as_of_date": as_of},
            "expires_at": now + _SIGNALS_TTL,
        }
        logger.info("Signals cache refreshed: %d tickers", len(records))

    return _signals_cache["data"]


def _run_fundamentals_subprocess() -> None:
    """Spawn a separate process for the fundamentals ETL.

    DuckDB does not allow a read-write connection when read-only connections
    already exist in the same process. Running the ETL in a subprocess gives it
    its own DuckDB connection context, avoiding the conflict entirely.
    """
    import subprocess
    import sys
    try:
        result = subprocess.run(
            [sys.executable, "-m", "etl.loader", "--with-fundamentals"],
        )
        if result.returncode == 0:
            logger.info("Fundamentals subprocess complete (rc=0)")
        else:
            logger.error("Fundamentals subprocess failed (rc=%d)", result.returncode)
    except Exception:
        logger.exception("Failed to launch fundamentals subprocess")


@app.post("/internal/run-etl")
def run_etl(
    background_tasks: BackgroundTasks,
    x_etl_secret: str = Header(default=""),
    with_fundamentals: bool = False,
) -> dict:
    """Trigger an ETL run. Called by GitHub Actions on a daily schedule.

    Requires the X-Etl-Secret header to match the ETL_SECRET environment variable.

    with_fundamentals=false (default): runs prices+macro synchronously and returns
      the quality report directly.
    with_fundamentals=true: prices+macro run synchronously first, then fundamentals
      launches as a subprocess via BackgroundTask. A subprocess is required because
      DuckDB rejects a read-write connection when read-only connections already exist
      in the same process — the subprocess gets its own isolated connection.
    """
    if not ETL_SECRET or x_etl_secret != ETL_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info("ETL triggered via /internal/run-etl with_fundamentals=%s", with_fundamentals)

    try:
        from etl.loader import run as etl_run
        report = etl_run(full_refresh=False, with_fundamentals=False)
        logger.info("Sync ETL complete: %s", report)
    except Exception as exc:
        logger.exception("Sync ETL failed")
        raise HTTPException(status_code=500, detail=f"ETL error: {exc}")

    if with_fundamentals:
        background_tasks.add_task(_run_fundamentals_subprocess)
        return {"status": "ok", "report": report, "note": "fundamentals refresh started in background subprocess"}

    return {"status": "ok", "report": report}


@app.get("/leaderboard")
def leaderboard() -> dict:
    """Return the top 10 portfolios by total realized P&L."""
    board = pt.get_leaderboard(DB_PATH, limit=10)
    return {"leaderboard": board}
