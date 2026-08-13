"""ETL orchestrator — runs all ingestion steps and emits a data quality report.

Usage:
  python -m etl.loader                      # incremental update (prices + macro)
  python -m etl.loader --full-refresh       # 2-year historical backfill (~10 min)
  python -m etl.loader --fundamentals-only  # fundamentals snapshot only, no prices
  python -m etl.loader --with-fundamentals  # incremental + fundamentals (local use)
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


def run(
    full_refresh: bool = False,
    with_fundamentals: bool = False,
    fundamentals_only: bool = False,
) -> dict:
    """Run the ETL pipeline and emit a data quality report.

    fundamentals_only=True: skips prices and macro entirely — only refreshes
    the fundamentals snapshot. Used by the Render subprocess so it does not
    re-download 500 tickers of price history on top of a running web server.
    """
    _validate_env()
    start_time = datetime.now()

    if fundamentals_only:
        mode = "FUNDAMENTALS ONLY"
    elif full_refresh:
        mode = "FULL REFRESH"
    else:
        mode = "INCREMENTAL"

    logger.info("ETL started — mode=%s db=%s", mode, DB_PATH)

    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    initialize_schema(DB_PATH)

    prices_result = None
    macro_result = None

    universe_result = load_universe(DB_PATH)

    if not fundamentals_only:
        prices_result = load_prices(DB_PATH, full_refresh=full_refresh)
        macro_result  = load_macro(DB_PATH, FRED_API_KEY, full_refresh=full_refresh)

    run_fundamentals = with_fundamentals or fundamentals_only
    fundamentals_result = load_fundamentals(DB_PATH) if run_fundamentals else None

    elapsed = (datetime.now() - start_time).total_seconds()

    print()
    print("── ETL Quality Report ──────────────────────────────────")
    print(f"  Mode:       {mode}")
    print(f"  Universe:   {universe_result.rows_written} tickers")
    if prices_result is not None:
        print(f"  Prices:     {prices_result.rows_written:,} rows  |  "
              f"{prices_result.batches_failed} batches failed  |  "
              f"{prices_result.tickers_attempted} tickers attempted")
    if macro_result is not None:
        print(f"  Macro:      {macro_result.rows_written} rows (yield curve, fed funds, CPI, VIX)")
    if fundamentals_result is not None:
        print(f"  Fundamentals: {fundamentals_result.rows_written} rows  |  "
              f"{fundamentals_result.tickers_failed} tickers failed  |  "
              f"{fundamentals_result.tickers_attempted} tickers attempted")
    print(f"  Elapsed:    {elapsed:.1f}s")
    print("────────────────────────────────────────────────────────")
    print()

    if prices_result is not None and prices_result.batches_failed > 0:
        logger.warning(
            "%d price batches failed — check logs above for details",
            prices_result.batches_failed,
        )

    return {
        "mode": mode,
        "universe_rows": universe_result.rows_written,
        "price_rows": prices_result.rows_written if prices_result else None,
        "price_batches_failed": prices_result.batches_failed if prices_result else None,
        "macro_rows": macro_result.rows_written if macro_result else None,
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
        help="Also fetch fundamentals snapshot (slow — serial per-ticker calls)",
    )
    parser.add_argument(
        "--fundamentals-only",
        action="store_true",
        help="Fetch fundamentals snapshot only — skip prices and macro (low memory, used by Render subprocess)",
    )
    args = parser.parse_args()
    run(
        full_refresh=args.full_refresh,
        with_fundamentals=args.with_fundamentals,
        fundamentals_only=args.fundamentals_only,
    )
