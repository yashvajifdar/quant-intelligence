"""Factor signal computation — cross-sectional ranking across S&P 500 universe.

Factors implemented:
  momentum    — 12-1 month price return (252-day minus 21-day lookback).
                Last month excluded to avoid short-term reversal effect.
  low_vol     — 252-day annualized realized volatility. Lower = better.

Factors not yet implemented (require fundamentals table):
  value       — EV/EBITDA, P/E, P/B vs sector median (needs ingest_fundamentals)
  quality     — ROE, gross margin, debt/equity (needs ingest_fundamentals)

All scores are cross-sectionally percentile-ranked 0–100 across the universe
on the as_of_date. A score of 90 means the ticker ranks better than 90% of
the universe on that factor.
"""

from __future__ import annotations

import math
from datetime import date

import duckdb
import pandas as pd

_MOMENTUM_LONG  = 252   # 12 months in trading days
_MOMENTUM_SHORT = 21    # 1 month — excluded from momentum window
_LOWVOL_WINDOW  = 252   # annualized vol lookback
_TRADING_DAYS_PER_YEAR = 252


def _load_close_prices(
    conn: duckdb.DuckDBPyConnection,
    as_of: date | None,
) -> pd.DataFrame:
    """Load adjusted close prices for all tickers. Pivots to wide format (date × ticker)."""
    date_filter = "AND date <= ?" if as_of else ""
    params      = [as_of] if as_of else []

    sql = f"""
        SELECT ticker, date, adj_close
        FROM prices
        WHERE adj_close IS NOT NULL {date_filter}
        ORDER BY date
    """
    long = conn.execute(sql, params).df()
    if long.empty:
        return pd.DataFrame()
    return long.pivot(index="date", columns="ticker", values="adj_close")


def compute_momentum_scores(
    db_path: str,
    as_of_date: date | None = None,
) -> pd.DataFrame:
    """Compute 12-1 month momentum scores, cross-sectionally ranked 0–100.

    Momentum = return from 252 trading days ago to 21 trading days ago.
    Requires at least 253 rows per ticker to compute.

    Returns:
        DataFrame with columns: ticker, momentum_return, momentum_rank, as_of_date.
        momentum_rank is a percentile 0–100 (higher = stronger momentum).
    """
    conn  = duckdb.connect(db_path, read_only=True)
    pivot = _load_close_prices(conn, as_of_date)
    conn.close()

    if pivot.empty or len(pivot) < _MOMENTUM_LONG + 1:
        return pd.DataFrame(columns=["ticker", "momentum_return", "momentum_rank", "as_of_date"])

    latest_date  = pivot.index[-1]
    price_now    = pivot.iloc[-_MOMENTUM_SHORT]    # price 21 days ago
    price_start  = pivot.iloc[-_MOMENTUM_LONG]     # price 252 days ago

    momentum = (price_now / price_start) - 1.0
    momentum = momentum.dropna()

    df = pd.DataFrame({
        "ticker":          momentum.index,
        "momentum_return": momentum.values.round(4),
    })
    df["momentum_rank"] = df["momentum_return"].rank(pct=True).mul(100).round(1)
    df["as_of_date"]    = latest_date
    return df.sort_values("momentum_rank", ascending=False).reset_index(drop=True)


def compute_lowvol_scores(
    db_path: str,
    as_of_date: date | None = None,
) -> pd.DataFrame:
    """Compute 252-day annualized realized volatility, ranked ascending (lower = better).

    Low volatility anomaly: lower-vol stocks persistently outperform on a
    risk-adjusted basis. A lowvol_rank of 90 means the ticker is less volatile
    than 90% of the universe.

    Returns:
        DataFrame with columns: ticker, realized_vol, lowvol_rank, as_of_date.
    """
    conn  = duckdb.connect(db_path, read_only=True)
    pivot = _load_close_prices(conn, as_of_date)
    conn.close()

    if pivot.empty or len(pivot) < _LOWVOL_WINDOW + 1:
        return pd.DataFrame(columns=["ticker", "realized_vol", "lowvol_rank", "as_of_date"])

    latest_date = pivot.index[-1]
    returns     = pivot.pct_change().tail(_LOWVOL_WINDOW + 1)
    annual_vol  = returns.std() * math.sqrt(_TRADING_DAYS_PER_YEAR)
    annual_vol  = annual_vol.dropna()

    df = pd.DataFrame({
        "ticker":       annual_vol.index,
        "realized_vol": annual_vol.values.round(4),
    })
    # Rank ascending: lowest vol gets highest rank (most desirable)
    df["lowvol_rank"] = df["realized_vol"].rank(pct=True, ascending=True)
    df["lowvol_rank"] = (1 - df["lowvol_rank"]).mul(100).round(1)
    df["as_of_date"]  = latest_date
    return df.sort_values("lowvol_rank", ascending=False).reset_index(drop=True)


def compute_combined_factor_score(
    db_path: str,
    as_of_date: date | None = None,
    momentum_weight: float = 0.6,
    lowvol_weight: float = 0.4,
) -> pd.DataFrame:
    """Combine momentum and low-vol into a single composite score.

    Weights default to 60/40 momentum/low-vol, reflecting that momentum
    contributes 25-35% of quant alpha vs low-vol's more defensive role.

    Returns:
        DataFrame with columns: ticker, momentum_rank, lowvol_rank,
        composite_score, composite_rank, as_of_date.
    """
    mom = compute_momentum_scores(db_path, as_of_date)[["ticker", "momentum_rank"]]
    vol = compute_lowvol_scores(db_path, as_of_date)[["ticker", "lowvol_rank"]]

    if mom.empty or vol.empty:
        return pd.DataFrame()

    df = mom.merge(vol, on="ticker", how="inner")
    df["composite_score"] = (
        df["momentum_rank"] * momentum_weight +
        df["lowvol_rank"]   * lowvol_weight
    ).round(1)
    df["composite_rank"] = df["composite_score"].rank(pct=True).mul(100).round(1)
    df["as_of_date"]     = as_of_date or df.get("as_of_date")

    return df.sort_values("composite_score", ascending=False).reset_index(drop=True)
