"""Ingest fundamental metrics into the fundamentals table.

Source: yfinance Ticker().info — there is no batch endpoint for fundamentals
(unlike ingest_prices.py's yf.download(), this is a serial per-ticker call).
That's why fundamentals are fetched weekly rather than daily: 500+ sequential
.info calls at ~1-2s each is a multi-minute job, and fundamental metrics
(P/E, ROE, margins) don't move fast enough to justify running it daily and
multiplying rate-limit risk against an unofficial API.

Grain: one row = one ticker x one fetch date.
Idempotent: INSERT OR REPLACE on (ticker, fetched_date) PK.
No full_refresh/incremental distinction like prices/macro — yfinance has no
historical fundamentals endpoint to backfill from, so every run is a fresh
snapshot for the requested tickers.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import NamedTuple

import duckdb
import pandas as pd
import yfinance as yf

REQUEST_DELAY_SECONDS = 0.5  # polite pacing against yfinance's unofficial endpoint

logger = logging.getLogger(__name__)


class FundamentalsResult(NamedTuple):
    tickers_attempted: int
    rows_written: int
    tickers_failed: int


def _get_tickers(db_path: str) -> list[str]:
    """Return all tickers from universe table, sorted."""
    conn = duckdb.connect(db_path)
    rows = conn.execute("SELECT ticker FROM universe ORDER BY ticker").fetchall()
    conn.close()
    return [r[0] for r in rows]


def _fetch_one(ticker: str) -> dict | None:
    """Fetch one ticker's fundamentals via yfinance .info. Returns None on failure.

    yfinance returns a near-empty dict for delisted/invalid tickers rather than
    raising — checking truthiness of `info` alone isn't enough, so we require a
    field that's only populated for a real, actively-traded security.
    """
    try:
        info = yf.Ticker(ticker).info
        if not info or info.get("regularMarketPrice") is None:
            return None

        earnings_date = None
        try:
            cal = yf.Ticker(ticker).calendar
            if cal is not None and "Earnings Date" in cal:
                ed = cal["Earnings Date"]
                earnings_date = ed[0] if isinstance(ed, list) and ed else ed
        except Exception:
            pass  # earnings calendar is frequently unavailable on yfinance — non-fatal

        return {
            "ticker":            ticker,
            "market_cap":        info.get("marketCap"),
            "pe_ratio":          info.get("trailingPE"),
            "forward_pe":        info.get("forwardPE"),
            "pb_ratio":          info.get("priceToBook"),
            "ev_ebitda":         info.get("enterpriseToEbitda"),
            "revenue_growth":    info.get("revenueGrowth"),
            "gross_margin":      info.get("grossMargins"),
            "operating_margin":  info.get("operatingMargins"),
            "debt_equity":       info.get("debtToEquity"),
            "roe":               info.get("returnOnEquity"),
            "free_cashflow":     info.get("freeCashflow"),
            "earnings_date":     earnings_date,
        }
    except Exception as exc:
        logger.warning("Fundamentals fetch failed for %s: %s", ticker, exc)
        return None


def load_fundamentals(db_path: str, tickers: list[str] | None = None) -> FundamentalsResult:
    """Fetch and upsert a fundamentals snapshot for the given (or full universe) tickers.

    Intended to run on a weekly cadence, separate from the daily prices/macro
    incremental — see loader.py's --with-fundamentals flag.
    """
    if tickers is None:
        tickers = _get_tickers(db_path)
    if not tickers:
        raise RuntimeError("Universe table is empty — run load_universe first")

    fetched_date = date.today()
    conn = duckdb.connect(db_path)
    rows_written = 0
    tickers_failed = 0

    for i, ticker in enumerate(tickers):
        record = _fetch_one(ticker)
        if record is None:
            tickers_failed += 1
            time.sleep(REQUEST_DELAY_SECONDS)
            continue

        record["fetched_date"] = fetched_date
        df = pd.DataFrame([record])
        conn.execute("""
            INSERT OR REPLACE INTO fundamentals
                (ticker, fetched_date, market_cap, pe_ratio, forward_pe, pb_ratio,
                 ev_ebitda, revenue_growth, gross_margin, operating_margin,
                 debt_equity, roe, free_cashflow, earnings_date)
            SELECT ticker, fetched_date, market_cap, pe_ratio, forward_pe, pb_ratio,
                   ev_ebitda, revenue_growth, gross_margin, operating_margin,
                   debt_equity, roe, free_cashflow, earnings_date
            FROM df
        """)
        rows_written += 1

        if (i + 1) % 50 == 0:
            logger.info("Fundamentals: %d/%d tickers processed", i + 1, len(tickers))
        time.sleep(REQUEST_DELAY_SECONDS)

    conn.close()
    logger.info("Fundamentals: %d rows written, %d failed", rows_written, tickers_failed)
    return FundamentalsResult(
        tickers_attempted=len(tickers),
        rows_written=rows_written,
        tickers_failed=tickers_failed,
    )
