"""Shared test fixtures.

All fixtures use deterministic, hard-coded data — no live API calls in tests.

Deterministic price series:
  AAPL — steady uptrend: 100 → ~157 over 300 business days (compound 0.15%/day)
  MSFT — downtrend:       100 → ~82  over 300 business days (compound -0.07%/day)
  JPM  — flat:            100.0 throughout

These produce known, verifiable signal outputs documented in each test.
"""

from __future__ import annotations

import math
from datetime import date

import duckdb
import numpy as np
import pandas as pd
import pytest

from etl.schema import initialize_schema

# Base date for all test data — fixed so tests never depend on "today"
_BASE_DATE = date(2024, 1, 2)
_TRADING_DAYS = 300

# Price growth rates per trading day
_AAPL_DAILY_RATE = 0.0015   # ~50% over 300 days
_MSFT_DAILY_RATE = -0.0007  # ~-19% over 300 days
_JPM_PRICE       = 100.0    # flat throughout


def _business_dates(start: date, n: int) -> list[date]:
    """Return n business dates starting from start."""
    return pd.bdate_range(start=start, periods=n).date.tolist()


def _make_prices(dates: list[date]) -> list[tuple]:
    """Generate deterministic OHLCV rows for AAPL, MSFT, JPM.

    AAPL and MSFT use a sinusoidal oscillation on top of the trend so that
    daily returns have non-zero variance — required for realized vol to differ
    from JPM's perfectly flat (zero vol) series.
    """
    rows = []
    for i, d in enumerate(dates):
        for ticker, rate in [("AAPL", _AAPL_DAILY_RATE), ("MSFT", _MSFT_DAILY_RATE), ("JPM", 0.0)]:
            if ticker == "JPM":
                close = _JPM_PRICE
            else:
                # Sinusoidal noise (amplitude 1%) makes daily returns non-constant
                oscillation = 1 + 0.01 * math.sin(i * 0.7)
                close = 100.0 * ((1 + rate) ** i) * oscillation
            rows.append((
                ticker, d,
                round(close * 0.99, 4),
                round(close * 1.01, 4),
                round(close * 0.98, 4),
                round(close, 4),
                round(close, 4),
                1_000_000,
            ))
    return rows


def _make_macro(dates: list[date], vix: float = 15.0, t10y2y: float = 0.5) -> list[tuple]:
    """Generate macro rows — constant values for predictable regime classification."""
    return [(d, t10y2y, 4.5, 300.0, vix) for d in dates]


@pytest.fixture(scope="session")
def test_db(tmp_path_factory) -> str:
    """Populated DuckDB in a temp directory. Reused across all tests in a session."""
    tmp = tmp_path_factory.mktemp("data")
    db_path = str(tmp / "test_quant.db")

    initialize_schema(db_path)
    dates = _business_dates(_BASE_DATE, _TRADING_DAYS)

    conn = duckdb.connect(db_path)

    conn.executemany(
        "INSERT INTO universe (ticker, company_name, sector, industry) VALUES (?, ?, ?, ?)",
        [
            ("AAPL", "Apple Inc", "Information Technology", "Technology Hardware"),
            ("MSFT", "Microsoft Corp", "Information Technology", "Systems Software"),
            ("JPM",  "JPMorgan Chase", "Financials", "Diversified Banks"),
        ],
    )

    conn.executemany(
        "INSERT INTO prices (ticker, date, open, high, low, close, adj_close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        _make_prices(dates),
    )

    conn.executemany(
        "INSERT INTO macro (date, t10y2y, fed_funds_rate, cpi, vix) VALUES (?, ?, ?, ?, ?)",
        _make_macro(dates),
    )

    conn.close()
    return db_path


@pytest.fixture(scope="session")
def crisis_db(tmp_path_factory) -> str:
    """DB with CRISIS-level macro (VIX=40) for regime tests."""
    tmp = tmp_path_factory.mktemp("crisis")
    db_path = str(tmp / "crisis.db")

    initialize_schema(db_path)
    dates = _business_dates(_BASE_DATE, 10)

    conn = duckdb.connect(db_path)
    conn.executemany(
        "INSERT INTO macro (date, t10y2y, fed_funds_rate, cpi, vix) VALUES (?, ?, ?, ?, ?)",
        _make_macro(dates, vix=40.0, t10y2y=0.5),
    )
    conn.close()
    return db_path


@pytest.fixture(scope="session")
def risk_off_db(tmp_path_factory) -> str:
    """DB with RISK_OFF macro: VIX=28, normal yield curve."""
    tmp = tmp_path_factory.mktemp("riskoff")
    db_path = str(tmp / "riskoff.db")

    initialize_schema(db_path)
    dates = _business_dates(_BASE_DATE, 10)

    conn = duckdb.connect(db_path)
    conn.executemany(
        "INSERT INTO macro (date, t10y2y, fed_funds_rate, cpi, vix) VALUES (?, ?, ?, ?, ?)",
        _make_macro(dates, vix=28.0, t10y2y=0.5),
    )
    conn.close()
    return db_path


@pytest.fixture(scope="session")
def inverted_yield_db(tmp_path_factory) -> str:
    """DB with inverted yield curve (t10y2y=-0.2) — should trigger RISK_OFF."""
    tmp = tmp_path_factory.mktemp("inverted")
    db_path = str(tmp / "inverted.db")

    initialize_schema(db_path)
    dates = _business_dates(_BASE_DATE, 10)

    conn = duckdb.connect(db_path)
    conn.executemany(
        "INSERT INTO macro (date, t10y2y, fed_funds_rate, cpi, vix) VALUES (?, ?, ?, ?, ?)",
        _make_macro(dates, vix=15.0, t10y2y=-0.2),
    )
    conn.close()
    return db_path
