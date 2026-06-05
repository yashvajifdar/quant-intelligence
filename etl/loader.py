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


def run(full_refresh: bool = False) -> None:
    """Run the full ETL pipeline and print a data quality report."""
    _validate_env()
    start_time = datetime.now()
    mode = "FULL REFRESH" if full_refresh else "INCREMENTAL"
    logger.info("ETL started — mode=%s db=%s", mode, DB_PATH)

    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    initialize_schema(DB_PATH)

    universe_result   = load_universe(DB_PATH)
    prices_result     = load_prices(DB_PATH, full_refresh=full_refresh)
    macro_result      = load_macro(DB_PATH, FRED_API_KEY, full_refresh=full_refresh)

    elapsed = (datetime.now() - start_time).total_seconds()

    print()
    print("── ETL Quality Report ──────────────────────────────────")
    print(f"  Mode:       {mode}")
    print(f"  Universe:   {universe_result.rows_written} tickers")
    print(f"  Prices:     {prices_result.rows_written:,} rows  |  "
          f"{prices_result.batches_failed} batches failed  |  "
          f"{prices_result.tickers_attempted} tickers attempted")
    print(f"  Macro:      {macro_result.rows_written} rows (yield curve, fed funds, CPI, VIX)")
    print(f"  Elapsed:    {elapsed:.1f}s")
    print("────────────────────────────────────────────────────────")
    print()

    if prices_result.batches_failed > 0:
        logger.warning(
            "%d price batches failed — check logs above for details",
            prices_result.batches_failed,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Quant Intelligence ETL pipeline")
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Pull full 2-year price and macro history (slow, use once)",
    )
    args = parser.parse_args()
    run(full_refresh=args.full_refresh)
