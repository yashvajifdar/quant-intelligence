"""Technical signal computation for S&P 500 universe.

Signals computed per ticker from the prices table:
  - MA50 / MA200     : 50-day and 200-day simple moving averages
  - RSI(14)          : momentum oscillator; <30 oversold, >70 overbought
  - MACD(12,26,9)    : momentum confirmation; histogram > 0 = bullish
  - ATR(14)          : volatility — used for stop placement and position sizing
  - volume_ratio     : today's volume vs 20-day average; >1.5 = institutional conviction

All functions accept an optional as_of_date for historical lookups (needed
by the paper portfolio tracker to avoid lookahead bias).
"""

from __future__ import annotations

import math
from datetime import date

import duckdb
import pandas as pd

_LOOKBACK_DAYS = 300   # enough for MA200 + buffer
_MA_SHORT      = 50
_MA_LONG       = 200
_RSI_PERIOD    = 14
_MACD_FAST     = 12
_MACD_SLOW     = 26
_MACD_SIGNAL   = 9
_ATR_PERIOD    = 14
_VOL_PERIOD    = 20


def _load_prices(
    conn: duckdb.DuckDBPyConnection,
    tickers: list[str] | None,
    as_of: date | None,
) -> pd.DataFrame:
    """Load OHLCV for requested tickers up to as_of date."""
    ticker_filter = "AND ticker = ANY(?) " if tickers else ""
    date_filter   = "AND date <= ? " if as_of else ""

    params: list = []
    if tickers:
        params.append(tickers)
    if as_of:
        params.append(as_of)

    sql = f"""
        SELECT ticker, date, open, high, low, close, volume
        FROM prices
        WHERE 1=1 {ticker_filter} {date_filter}
        ORDER BY ticker, date
    """
    return conn.execute(sql, params).df()


def _rsi(closes: pd.Series, period: int = _RSI_PERIOD) -> float:
    """Compute RSI for a price series. Returns the most recent value.

    Special cases:
      avg_loss == 0 → all gains in the window → RSI = 100
      avg_gain == 0 → all losses in the window → RSI = 0
    """
    delta = closes.diff().dropna()
    if len(delta) < period:
        return float("nan")
    avg_gain = float(delta.clip(lower=0).rolling(period).mean().iloc[-1])
    avg_loss = float((-delta.clip(upper=0)).rolling(period).mean().iloc[-1])
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _macd(closes: pd.Series) -> dict[str, float]:
    """Compute MACD line, signal line, and histogram."""
    ema_fast   = closes.ewm(span=_MACD_FAST,   adjust=False).mean()
    ema_slow   = closes.ewm(span=_MACD_SLOW,   adjust=False).mean()
    macd_line  = ema_fast - ema_slow
    signal     = macd_line.ewm(span=_MACD_SIGNAL, adjust=False).mean()
    histogram  = macd_line - signal
    return {
        "macd":      round(float(macd_line.iloc[-1]),  4),
        "macd_signal": round(float(signal.iloc[-1]),   4),
        "macd_hist": round(float(histogram.iloc[-1]),  4),
    }


def _atr(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = _ATR_PERIOD) -> float:
    """Compute Average True Range."""
    prev_close = closes.shift(1)
    tr = pd.concat([
        highs - lows,
        (highs - prev_close).abs(),
        (lows  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return round(float(atr.iloc[-1]), 4)


def _signals_for_ticker(group: pd.DataFrame) -> dict:
    """Compute all technical signals for a single ticker's price history."""
    closes  = group["close"]
    highs   = group["high"]
    lows    = group["low"]
    volumes = group["volume"]

    ma50  = round(float(closes.rolling(_MA_SHORT).mean().iloc[-1]), 4) if len(closes) >= _MA_SHORT  else None
    ma200 = round(float(closes.rolling(_MA_LONG).mean().iloc[-1]),  4) if len(closes) >= _MA_LONG   else None
    close = round(float(closes.iloc[-1]), 4)

    vol_ma20    = volumes.rolling(_VOL_PERIOD).mean().iloc[-1]
    vol_ratio   = round(float(volumes.iloc[-1] / vol_ma20), 2) if vol_ma20 > 0 else None

    macd_vals = _macd(closes)

    return {
        "close":              close,
        "ma50":               ma50,
        "ma200":              ma200,
        "ma50_above_ma200":   bool(ma50 > ma200) if (ma50 and ma200) else None,
        "rsi":                _rsi(closes),
        "macd":               macd_vals["macd"],
        "macd_signal":        macd_vals["macd_signal"],
        "macd_hist":          macd_vals["macd_hist"],
        "atr":                _atr(highs, lows, closes),
        "volume_ratio":       vol_ratio,
    }


def compute_technical_signals(
    db_path: str,
    tickers: list[str] | None = None,
    as_of_date: date | None = None,
) -> pd.DataFrame:
    """Compute technical signals for all (or specified) tickers.

    Args:
        db_path: Path to DuckDB warehouse.
        tickers: Subset of tickers to compute. None = full universe.
        as_of_date: Compute as of this date. None = latest available.

    Returns:
        DataFrame with one row per ticker and columns:
        ticker, close, ma50, ma200, ma50_above_ma200, rsi,
        macd, macd_signal, macd_hist, atr, volume_ratio, as_of_date.
    """
    conn = duckdb.connect(db_path, read_only=True)
    df   = _load_prices(conn, tickers, as_of_date)
    conn.close()

    if df.empty:
        return pd.DataFrame()

    records = []
    for ticker, group in df.groupby("ticker"):
        group = group.sort_values("date").tail(_LOOKBACK_DAYS)
        signals = _signals_for_ticker(group)
        signals["ticker"]     = ticker
        signals["as_of_date"] = group["date"].iloc[-1]
        records.append(signals)

    result = pd.DataFrame(records)
    cols = ["ticker", "as_of_date", "close", "ma50", "ma200", "ma50_above_ma200",
            "rsi", "macd", "macd_signal", "macd_hist", "atr", "volume_ratio"]
    return result[[c for c in cols if c in result.columns]]


def stop_price(entry: float, atr: float, multiplier: float = 1.25) -> float:
    """Compute ATR-based stop loss for a long position.

    Per quant-finance standards: stop = entry - 1.25 × ATR(14).
    """
    return round(entry - multiplier * atr, 4)


def suggested_position_size(
    account_value: float,
    entry: float,
    atr: float,
    account_risk_pct: float = 0.01,
) -> float:
    """Compute position size in shares based on ATR stop and 1% account risk rule.

    size = (account_value × account_risk_pct) / (entry - stop)
    """
    stop = stop_price(entry, atr)
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        raise ValueError(f"Invalid stop: entry={entry}, atr={atr}")
    dollar_risk = account_value * account_risk_pct
    return math.floor(dollar_risk / risk_per_share)
