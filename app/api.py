"""FastAPI backend for the Quant Intelligence platform.

Exposes:
  GET  /health       — liveness check + database row counts
  POST /recommend    — AI investment recommendations
  GET  /portfolio    — paper portfolio performance (stub — M4)

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
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ── request / response models ─────────────────────────────────────────────────

class RecommendRequest(BaseModel):
    question: str


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


@app.get("/portfolio")
def portfolio() -> dict:
    """Paper portfolio performance. Coming in M4."""
    return {"status": "coming_soon", "message": "Paper portfolio tracker is being built."}
