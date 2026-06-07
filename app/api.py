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

import duckdb
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from portfolio import paper_trades as pt
from portfolio.performance import compute_summary

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("quant_api")

DB_PATH = os.environ.get("QUANT_DB_PATH", "data/quant.db")

_ALLOWED_ORIGINS = [
    "https://yashvajifdar.com",
    "https://www.yashvajifdar.com",
    "http://localhost:3000",
    "http://localhost:3001",
]


def _db_stats() -> dict[str, int]:
    """Return row counts for each warehouse table."""
    if not os.path.exists(DB_PATH):
        return {}
    conn = duckdb.connect(DB_PATH, read_only=True)
    stats = {
        "universe": conn.execute("SELECT COUNT(*) FROM universe").fetchone()[0],
        "prices":   conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0],
        "macro":    conn.execute("SELECT COUNT(*) FROM macro").fetchone()[0],
    }
    conn.close()
    return stats


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

    return {
        "portfolio": row,
        "open_trades": open_trades,
        "closed_trades": closed_trades,
        "performance": summary,
    }


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


@app.get("/leaderboard")
def leaderboard() -> dict:
    """Return the top 10 portfolios by total realized P&L."""
    board = pt.get_leaderboard(DB_PATH, limit=10)
    return {"leaderboard": board}
