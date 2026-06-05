"""Ingest macro indicators into the macro table.

Sources:
  FRED (via fredapi) — T10Y2Y (yield curve), FEDFUNDS (fed funds rate), CPIAUCSL (CPI)
  yfinance           — ^VIX (CBOE Volatility Index)

Grain: one row = one calendar date.
Idempotent: INSERT OR REPLACE on date PK.
FRED reports at daily or monthly frequency; we forward-fill to daily on merge.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import NamedTuple

import duckdb
import pandas as pd
import yfinance as yf
from fredapi import Fred

FRED_SERIES: dict[str, str] = {
    "t10y2y":        "T10Y2Y",     # 10Y minus 2Y treasury spread
    "fed_funds_rate": "FEDFUNDS",  # effective federal funds rate (monthly)
    "cpi":           "CPIAUCSL",   # CPI all urban consumers (monthly)
}
VIX_TICKER = "^VIX"
HISTORY_YEARS = 2
INCREMENTAL_DAYS = 45  # CPI and fed funds update monthly; pull extra overlap

logger = logging.getLogger(__name__)


class MacroResult(NamedTuple):
    rows_written: int


def _fetch_fred(api_key: str, start: str, end: str) -> pd.DataFrame:
    """Pull yield curve, fed funds rate, and CPI from FRED. Returns daily DataFrame."""
    fred = Fred(api_key=api_key)
    frames: dict[str, pd.Series] = {}
    for col, series_id in FRED_SERIES.items():
        frames[col] = fred.get_series(series_id, observation_start=start, observation_end=end)

    df = pd.DataFrame(frames)
    df.index = pd.to_datetime(df.index)
    df = df.resample("D").last().ffill()   # monthly series → daily, forward-filled
    df.index.name = "date"
    df = df.reset_index()
    df["date"] = df["date"].dt.date
    return df


def _fetch_vix(start: str, end: str) -> pd.DataFrame:
    """Pull VIX daily close from yfinance."""
    raw = yf.download(VIX_TICKER, start=start, end=end, auto_adjust=True, progress=False)
    if raw.empty:
        return pd.DataFrame(columns=["date", "vix"])
    df = raw[["Close"]].reset_index()
    df.columns = ["date", "vix"]
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def load_macro(db_path: str, fred_api_key: str, full_refresh: bool = False) -> MacroResult:
    """Fetch and upsert macro indicators into the macro table."""
    lookback = HISTORY_YEARS * 365 if full_refresh else INCREMENTAL_DAYS
    start = (date.today() - timedelta(days=lookback)).isoformat()
    end = date.today().isoformat()

    fred_df = _fetch_fred(fred_api_key, start=start, end=end)
    vix_df = _fetch_vix(start=start, end=end)

    df = pd.merge(fred_df, vix_df, on="date", how="outer").sort_values("date")

    conn = duckdb.connect(db_path)
    conn.execute("""
        INSERT OR REPLACE INTO macro (date, t10y2y, fed_funds_rate, cpi, vix)
        SELECT date, t10y2y, fed_funds_rate, cpi, vix
        FROM df
    """)
    conn.close()

    logger.info("Macro: %d rows written", len(df))
    return MacroResult(rows_written=len(df))
