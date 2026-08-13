"""ETL orchestrator — runs all ingestion steps and emits a data quality report.

Usage:
  python -m etl.loader                 # incremental update
  python -m etl.loader --full-refresh  # 2-year historical backfill (~10 min)
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from etl.schema import initialize_schema
from etl.universe import load_universe
from etl.ingest_prices import load_prices
from etl.ingest_macro import load_macro
from etl.ingest_fundamentals import load_fundamentals

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("QUANT_DB_PATH", "data/quant.db")
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")


def _validate_env() -> None:
    """Fail loudly if required environment variables are missing."""
    if not FRED_API_KEY:
        raise EnvironmentError(
            "FRED_API_KEY is not set. "
            "Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html "
            "and add it to .env"
        )


def run(full_refresh: bool = False, with_fundamentals: bool = False) -> dict:
    """Run the full ETL pipeline, print a data quality report, and return it as a dict.

    Fundamentals is opt-in via --with-fundamentals rather than running on every
    incremental call: it's a serial per-ticker yfinance call (no batch endpoint),
    so bundling it into the daily job would multiply runtime and rate-limit risk
    for data that doesn't move fast enough to justify a daily refresh. Intended
    to be scheduled weekly, separate from the daily prices/macro cron.
    """
    _validate_env()
    start_time = datetime.now()
    mode = "FULL REFRESH" if full_refresh else "INCREMENTAL"
    logger.info("ETL started — mode=%s db=%s fundamentals=%s", mode, DB_PATH, with_fundamentals)

    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    initialize_schema(DB_PATH)

    universe_result   = load_universe(DB_PATH)
    prices_result     = load_prices(DB_PATH, full_refresh=full_refresh)
    macro_result      = load_macro(DB_PATH, FRED_API_KEY, full_refresh=full_refresh)
    fundamentals_result = load_fundamentals(DB_PATH) if with_fundamentals else None

    elapsed = (datetime.now() - start_time).total_seconds()

    print()
    print("── ETL Quality Report ──────────────────────────────────")
    print(f"  Mode:       {mode}")
    print(f"  Universe:   {universe_result.rows_written} tickers")
    print(f"  Prices:     {prices_result.rows_written:,} rows  |  "
          f"{prices_result.batches_failed} batches failed  |  "
          f"{prices_result.tickers_attempted} tickers attempted")
    print(f"  Macro:      {macro_result.rows_written} rows (yield curve, fed funds, CPI, VIX)")
    if fundamentals_result is not None:
        print(f"  Fundamentals: {fundamentals_result.rows_written} rows  |  "
              f"{fundamentals_result.tickers_failed} tickers failed  |  "
              f"{fundamentals_result.tickers_attempted} tickers attempted")
    print(f"  Elapsed:    {elapsed:.1f}s")
    print("────────────────────────────────────────────────────────")
    print()

    if prices_result.batches_failed > 0:
        logger.warning(
            "%d price batches failed — check logs above for details",
            prices_result.batches_failed,
        )

    return {
        "mode": mode,
        "universe_rows": universe_result.rows_written,
        "price_rows": prices_result.rows_written,
        "price_batches_failed": prices_result.batches_failed,
        "macro_rows": macro_result.rows_written,
        "fundamentals_rows": fundamentals_result.rows_written if fundamentals_result else None,
        "fundamentals_failed": fundamentals_result.tickers_failed if fundamentals_result else None,
        "elapsed_s": round(elapsed, 1),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Quant Intelligence ETL pipeline")
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Pull full 2-year price and macro history (slow, use once)",
    )
    parser.add_argument(
        "--with-fundamentals",
        action="store_true",
        help="Also fetch fundamentals snapshot (slow — serial per-ticker calls; run weekly, not daily)",
    )
    args = parser.parse_args()
    run(full_refresh=args.full_refresh, with_fundamentals=args.with_fundamentals)
