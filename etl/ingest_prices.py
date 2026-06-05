"""Download daily OHLCV prices for the S&P 500 universe via yfinance batch download.

Grain: one row = one ticker × one trading day.
Idempotent: INSERT OR REPLACE on (ticker, date) PK.
Full refresh pulls 2 years. Incremental pulls last 7 days.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import NamedTuple

import duckdb
import pandas as pd
import yfinance as yf

BATCH_SIZE = 100       # yfinance handles ~100 tickers per batch reliably
HISTORY_YEARS = 2
INCREMENTAL_DAYS = 7   # overlap ensures we catch late-arriving corrections

logger = logging.getLogger(__name__)


class PricesResult(NamedTuple):
    tickers_attempted: int
    rows_written: int
    batches_failed: int


def _get_tickers(db_path: str) -> list[str]:
    """Return all tickers from universe table, sorted."""
    conn = duckdb.connect(db_path)
    rows = conn.execute("SELECT ticker FROM universe ORDER BY ticker").fetchall()
    conn.close()
    return [r[0] for r in rows]


def _download_batch(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    """Download OHLCV for a list of tickers. Returns long-format DataFrame.

    yfinance returns MultiIndex columns for multiple tickers.
    Normalizes to: ticker, date, open, high, low, close, adj_close, volume.
    """
    raw = yf.download(
        tickers,
        start=start,
        end=end,
        auto_adjust=True,   # close = adj_close
        progress=False,
        threads=True,
    )
    if raw.empty:
        return pd.DataFrame()

    if isinstance(raw.columns, pd.MultiIndex):
        df = raw.stack(level="Ticker", future_stack=True).rename_axis(["date", "ticker"]).reset_index()
    else:
        df = raw.reset_index().rename(columns={"Date": "date"})
        df["ticker"] = tickers[0]

    df.columns = [str(c).lower() for c in df.columns]
    df = df.rename(columns={"price": "close"})
    df["adj_close"] = df["close"]   # auto_adjust=True means close IS adj_close
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df[["ticker", "date", "open", "high", "low", "close", "adj_close", "volume"]]


def load_prices(db_path: str, full_refresh: bool = False) -> PricesResult:
    """Download and upsert OHLCV prices for all S&P 500 tickers."""
    tickers = _get_tickers(db_path)
    if not tickers:
        raise RuntimeError("Universe table is empty — run load_universe first")

    lookback = HISTORY_YEARS * 365 if full_refresh else INCREMENTAL_DAYS
    start = (date.today() - timedelta(days=lookback)).isoformat()
    end = date.today().isoformat()

    conn = duckdb.connect(db_path)
    rows_written = 0
    batches_failed = 0

    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE

        try:
            df = _download_batch(batch, start=start, end=end)
            if df.empty:
                logger.warning("Batch %d/%d returned no data", batch_num, total_batches)
                continue

            df = df.dropna(subset=["close"])
            conn.execute("""
                INSERT OR REPLACE INTO prices
                    (ticker, date, open, high, low, close, adj_close, volume)
                SELECT ticker, date, open, high, low, close, adj_close, volume
                FROM df
            """)
            rows_written += len(df)
            logger.info("Batch %d/%d: %d rows written", batch_num, total_batches, len(df))

        except Exception as exc:
            logger.error("Batch %d/%d failed (%s...): %s", batch_num, total_batches, batch[0], exc)
            batches_failed += 1

    conn.close()
    return PricesResult(
        tickers_attempted=len(tickers),
        rows_written=rows_written,
        batches_failed=batches_failed,
    )
