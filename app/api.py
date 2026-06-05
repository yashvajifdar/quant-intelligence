"""FastAPI backend for the Quant Intelligence platform.

Exposes:
  GET  /health       — liveness check + database row counts
  POST /recommend    — AI investment recommendations (stub — returns placeholder until engine is built)
  GET  /portfolio    — paper portfolio performance (stub — returns placeholder until portfolio is built)

Deployed on Render. Personal website at yashvajifdar.com proxies here.

Run locally:
  cd /path/to/quant-intelligence
  source venv/bin/activate
  uvicorn app.api:app --reload --port 8002
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import duckdb
from dotenv import load_dotenv
from fastapi import FastAPI
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
        "universe":  conn.execute("SELECT COUNT(*) FROM universe").fetchone()[0],
        "prices":    conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0],
        "macro":     conn.execute("SELECT COUNT(*) FROM macro").fetchone()[0],
    }
    conn.close()
    return stats


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Quant Intelligence API starting — db=%s", DB_PATH)
    stats = _db_stats()
    if not stats:
        logger.warning("Database not found at %s — ETL has not run yet", DB_PATH)
    else:
        logger.info("Database ready: %s", stats)
    yield
    logger.info("Shutting down")


app = FastAPI(title="Quant Intelligence API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# ── models ────────────────────────────────────────────────────────────────────

class RecommendRequest(BaseModel):
    question: str


class RecommendResponse(BaseModel):
    text: str
    status: str


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    """Liveness check. Returns database row counts so we can verify ETL ran."""
    return {"status": "ok", "db": _db_stats()}


@app.post("/recommend", response_model=RecommendResponse)
def recommend(req: RecommendRequest) -> RecommendResponse:
    """Investment recommendation engine. Signal layer and AI engine coming in M2/M3."""
    logger.info("recommend request: %r", req.question)
    return RecommendResponse(
        text="Recommendation engine is being built. Check back soon.",
        status="coming_soon",
    )


@app.get("/portfolio")
def portfolio() -> dict:
    """Paper portfolio performance. Coming in M4."""
    return {"status": "coming_soon", "message": "Paper portfolio tracker is being built."}
