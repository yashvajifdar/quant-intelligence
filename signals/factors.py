"""Factor signal computation — cross-sectional ranking across S&P 500 universe.

Factors implemented:
  momentum    — 12-1 month price return (252-day minus 21-day lookback).
                Last month excluded to avoid short-term reversal effect.
  low_vol     — 252-day annualized realized volatility. Lower = better.
  value       — inverse-rank average of P/E, P/B, EV/EBITDA. Cheaper = better.
  quality     — rank average of ROE, gross margin, inverse(debt/equity), FCF.

All scores are cross-sectionally percentile-ranked 0–100 across the universe
on the as_of_date. A score of 90 means the ticker ranks better than 90% of
the universe on that factor.

Value and quality use each ticker's *latest* fundamentals row as of the
as_of_date (fundamentals are fetched weekly — see etl/ingest_fundamentals.py —
so "latest" means most recent fetch, not same-day). Tickers missing any of
the required metrics (yfinance frequently omits fields for a subset of
tickers — financials/banks are especially prone to null gross_margin) are
dropped from that factor's ranking rather than imputed, consistent with how
compute_momentum_scores handles insufficient price history.
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


def _load_latest_fundamentals(
    conn: duckdb.DuckDBPyConnection,
    as_of: date | None,
) -> pd.DataFrame:
    """Load each ticker's most recent fundamentals row as of as_of_date (or overall latest).

    Fundamentals are fetched weekly, so a ticker can have several historical
    snapshot rows — this dedups to exactly one row per ticker via ROW_NUMBER,
    not GROUP BY/MAX, so a same-date duplicate can't silently fan out the join.
    """
    date_filter = "AND fetched_date <= ?" if as_of else ""
    params      = [as_of] if as_of else []

    sql = f"""
        SELECT * EXCLUDE (rn) FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY fetched_date DESC) AS rn
            FROM fundamentals
            WHERE 1=1 {date_filter}
        )
        WHERE rn = 1
    """
    return conn.execute(sql, params).df()


def compute_value_scores(
    db_path: str,
    as_of_date: date | None = None,
) -> pd.DataFrame:
    """Compute value composite scores: inverse-rank average of P/E, P/B, EV/EBITDA.

    Lower valuation multiples score higher. Tickers missing any of the three
    metrics, or with a non-positive P/E or EV/EBITDA (negative earnings make
    the multiple meaningless for a cheap/expensive comparison), are dropped.

    Returns:
        DataFrame with columns: ticker, value_score, as_of_date.
        value_score is 0-100; higher means cheaper relative to the universe.
    """
    conn = duckdb.connect(db_path, read_only=True)
    fund = _load_latest_fundamentals(conn, as_of_date)
    conn.close()

    cols = ["ticker", "pe_ratio", "pb_ratio", "ev_ebitda", "fetched_date"]
    if fund.empty or not set(cols).issubset(fund.columns):
        return pd.DataFrame(columns=["ticker", "value_score", "as_of_date"])

    df = fund[cols].dropna(subset=["pe_ratio", "pb_ratio", "ev_ebitda"]).copy()
    df = df[(df["pe_ratio"] > 0) & (df["ev_ebitda"] > 0)]
    if df.empty:
        return pd.DataFrame(columns=["ticker", "value_score", "as_of_date"])

    for metric in ["pe_ratio", "pb_ratio", "ev_ebitda"]:
        pct = df[metric].rank(pct=True, ascending=True)
        df[f"_{metric}_rank"] = (1 - pct) * 100

    df["value_score"] = df[[f"_{m}_rank" for m in ["pe_ratio", "pb_ratio", "ev_ebitda"]]].mean(axis=1).round(1)
    df["as_of_date"]  = df["fetched_date"].max()

    return df[["ticker", "value_score", "as_of_date"]].sort_values(
        "value_score", ascending=False
    ).reset_index(drop=True)


def compute_quality_scores(
    db_path: str,
    as_of_date: date | None = None,
) -> pd.DataFrame:
    """Compute quality composite scores: rank average of ROE, gross margin,
    inverse(debt/equity), and free cash flow.

    Higher profitability/lower leverage scores higher. Tickers missing any of
    the four metrics are dropped — this disproportionately affects financials
    (yfinance frequently omits gross_margin for banks), a known limitation
    worth calling out rather than imputing a value.

    Returns:
        DataFrame with columns: ticker, quality_score, as_of_date.
        quality_score is 0-100; higher means more profitable / less levered
        relative to the universe.
    """
    conn = duckdb.connect(db_path, read_only=True)
    fund = _load_latest_fundamentals(conn, as_of_date)
    conn.close()

    cols = ["ticker", "roe", "gross_margin", "debt_equity", "free_cashflow", "fetched_date"]
    if fund.empty or not set(cols).issubset(fund.columns):
        return pd.DataFrame(columns=["ticker", "quality_score", "as_of_date"])

    df = fund[cols].dropna(subset=["roe", "gross_margin", "debt_equity", "free_cashflow"]).copy()
    if df.empty:
        return pd.DataFrame(columns=["ticker", "quality_score", "as_of_date"])

    for metric in ["roe", "gross_margin", "free_cashflow"]:
        df[f"_{metric}_rank"] = df[metric].rank(pct=True).mul(100)

    debt_pct = df["debt_equity"].rank(pct=True, ascending=True)
    df["_debt_equity_rank"] = (1 - debt_pct) * 100

    rank_cols = ["_roe_rank", "_gross_margin_rank", "_debt_equity_rank", "_free_cashflow_rank"]
    df["quality_score"] = df[rank_cols].mean(axis=1).round(1)
    df["as_of_date"]    = df["fetched_date"].max()

    return df[["ticker", "quality_score", "as_of_date"]].sort_values(
        "quality_score", ascending=False
    ).reset_index(drop=True)


def compute_combined_factor_score(
    db_path: str,
    as_of_date: date | None = None,
    momentum_weight: float = 0.40,
    quality_weight: float = 0.25,
    lowvol_weight: float = 0.20,
    value_weight: float = 0.15,
) -> pd.DataFrame:
    """Combine four factors into a single composite score.

    Default weights per ADR-0002:
      momentum 40% / quality 25% / low-vol 20% / value 15%

    Tickers must appear in all four factor DataFrames (inner join). Tickers
    missing fundamentals data — because the weekly fundamentals ETL has not
    run yet or yfinance returned insufficient data — are excluded rather than
    imputed. If value or quality DataFrames are empty (fundamentals table has
    no rows), the function falls back to the two-factor composite so the API
    layer is not broken by an empty return.

    Returns:
        DataFrame with columns: ticker, momentum_rank, lowvol_rank,
        value_score, quality_score, composite_score, composite_rank, as_of_date.
    """
    mom  = compute_momentum_scores(db_path, as_of_date)[["ticker", "momentum_rank"]]
    vol  = compute_lowvol_scores(db_path, as_of_date)[["ticker", "lowvol_rank"]]
    val  = compute_value_scores(db_path, as_of_date)[["ticker", "value_score"]]
    qual = compute_quality_scores(db_path, as_of_date)[["ticker", "quality_score"]]

    if mom.empty or vol.empty:
        return pd.DataFrame()

    # If fundamentals are not yet loaded, fall back to the two-factor composite
    # so the API layer keeps returning results during the weekly fundamentals gap.
    if val.empty or qual.empty:
        df = mom.merge(vol, on="ticker", how="inner")
        two_factor_total = momentum_weight + lowvol_weight
        df["composite_score"] = (
            df["momentum_rank"] * (momentum_weight / two_factor_total) +
            df["lowvol_rank"]   * (lowvol_weight   / two_factor_total)
        ).round(1)
        df["value_score"]   = float("nan")
        df["quality_score"] = float("nan")
    else:
        df = (
            mom
            .merge(vol,  on="ticker", how="inner")
            .merge(val,  on="ticker", how="inner")
            .merge(qual, on="ticker", how="inner")
        )
        df["composite_score"] = (
            df["momentum_rank"]  * momentum_weight +
            df["quality_score"]  * quality_weight  +
            df["lowvol_rank"]    * lowvol_weight    +
            df["value_score"]    * value_weight
        ).round(1)

    df["composite_rank"] = df["composite_score"].rank(pct=True).mul(100).round(1)
    df["as_of_date"]     = as_of_date or date.today()

    return df.sort_values("composite_score", ascending=False).reset_index(drop=True)
