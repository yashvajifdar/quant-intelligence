"""Fetch and load the S&P 500 constituent list from Wikipedia.

Source: https://en.wikipedia.org/wiki/List_of_S%26P_500_companies
Refreshed on every full-refresh run. Idempotent — upserts on ticker PK.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

import io

import duckdb
import pandas as pd
import requests

WIKIPEDIA_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; quant-intelligence/1.0)"}

logger = logging.getLogger(__name__)


class UniverseResult(NamedTuple):
    rows_written: int


def fetch_sp500() -> pd.DataFrame:
    """Pull S&P 500 table from Wikipedia. Returns normalized DataFrame."""
    resp = requests.get(WIKIPEDIA_SP500_URL, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    raw = pd.read_html(io.StringIO(resp.text))[0]
    df = pd.DataFrame({
        "ticker":           raw["Symbol"].str.replace(".", "-", regex=False),
        "company_name":     raw["Security"],
        "sector":           raw["GICS Sector"],
        "industry":         raw["GICS Sub-Industry"],
        "gics_sub_industry": raw["GICS Sub-Industry"],
        "headquarters":     raw["Headquarters Location"],
        "date_added":       pd.to_datetime(raw["Date added"], errors="coerce").dt.date,
        "cik":              raw["CIK"].astype(str),
    })
    return df


def load_universe(db_path: str) -> UniverseResult:
    """Upsert S&P 500 constituents into the universe table."""
    df = fetch_sp500()
    conn = duckdb.connect(db_path)
    conn.execute("""
        INSERT OR REPLACE INTO universe
            (ticker, company_name, sector, industry, gics_sub_industry, headquarters, date_added, cik)
        SELECT ticker, company_name, sector, industry, gics_sub_industry, headquarters, date_added, cik
        FROM df
    """)
    conn.close()
    logger.info("Universe: %d tickers loaded", len(df))
    return UniverseResult(rows_written=len(df))
