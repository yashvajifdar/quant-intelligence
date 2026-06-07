"""Portfolio performance metrics for the Quant Intelligence platform.

All functions are pure — they take trade data as input and perform no DB reads.
Input is a list[dict] as returned by paper_trades.get_closed_trades().

Each dict is expected to contain at minimum: realized_pnl (float).

Not financial advice. All metrics are for paper portfolio evaluation only.
"""

from __future__ import annotations

import math
from typing import Any

# ── constants ──────────────────────────────────────────────────────────────────

TRADING_DAYS_PER_YEAR: int = 252


# ── helpers ────────────────────────────────────────────────────────────────────

def _pnl_values(closed_trades: list[dict]) -> list[float]:
    """Extract realized_pnl from each trade. Requires the key to be present."""
    return [t["realized_pnl"] for t in closed_trades]


def _winning_trades(closed_trades: list[dict]) -> list[dict]:
    """Return trades where realized_pnl > 0."""
    return [t for t in closed_trades if t["realized_pnl"] > 0]


def _losing_trades(closed_trades: list[dict]) -> list[dict]:
    """Return trades where realized_pnl <= 0."""
    return [t for t in closed_trades if t["realized_pnl"] <= 0]


def _mean(values: list[float]) -> float:
    """Arithmetic mean of a non-empty list."""
    return sum(values) / len(values)


def _std(values: list[float], mean: float) -> float:
    """Population standard deviation."""
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


# ── public API ─────────────────────────────────────────────────────────────────

def total_return(closed_trades: list[dict]) -> float:
    """Sum of all realized P&L across closed trades.

    Args:
        closed_trades: List of closed trade dicts, each with a realized_pnl key.

    Returns:
        Total realized P&L. Returns 0.0 if no closed trades.
    """
    if not closed_trades:
        return 0.0
    return sum(_pnl_values(closed_trades))


def win_rate(closed_trades: list[dict]) -> float:
    """Fraction of closed trades with realized_pnl > 0.

    Args:
        closed_trades: List of closed trade dicts.

    Returns:
        Win rate in [0, 1]. Returns 0.0 if no closed trades.
    """
    if not closed_trades:
        return 0.0
    winners = len(_winning_trades(closed_trades))
    return winners / len(closed_trades)


def avg_win(closed_trades: list[dict]) -> float | None:
    """Average P&L of winning trades.

    Args:
        closed_trades: List of closed trade dicts.

    Returns:
        Average P&L of winning trades, or None if there are no winners.
    """
    winners = _winning_trades(closed_trades)
    if not winners:
        return None
    return _mean(_pnl_values(winners))


def avg_loss(closed_trades: list[dict]) -> float | None:
    """Average P&L of losing trades (will be negative or zero).

    Args:
        closed_trades: List of closed trade dicts.

    Returns:
        Average P&L of losing trades, or None if there are no losers.
    """
    losers = _losing_trades(closed_trades)
    if not losers:
        return None
    return _mean(_pnl_values(losers))


def sharpe_ratio(
    closed_trades: list[dict],
    risk_free_rate: float = 0.05,
) -> float | None:
    """Annualized Sharpe ratio using per-trade P&L as the return series.

    Uses TRADING_DAYS_PER_YEAR to annualize. Per-trade risk-free cost is
    risk_free_rate / TRADING_DAYS_PER_YEAR.

    Args:
        closed_trades: List of closed trade dicts, each with realized_pnl.
        risk_free_rate: Annual risk-free rate (default 5%).

    Returns:
        Annualized Sharpe ratio, or None if fewer than 2 trades.
    """
    if len(closed_trades) < 2:
        return None

    pnls = _pnl_values(closed_trades)
    daily_rf = risk_free_rate / TRADING_DAYS_PER_YEAR
    excess = [p - daily_rf for p in pnls]

    mean_excess = _mean(excess)
    std_excess = _std(excess, mean_excess)

    if std_excess == 0.0:
        return None

    return (mean_excess / std_excess) * math.sqrt(TRADING_DAYS_PER_YEAR)


def max_drawdown(closed_trades: list[dict]) -> float:
    """Maximum peak-to-trough drawdown of the cumulative P&L curve.

    Drawdown is expressed as a fraction: (peak - trough) / peak.
    Returns 0.0 if the peak is zero or there are no trades.

    Args:
        closed_trades: List of closed trade dicts ordered by entry_date.

    Returns:
        Max drawdown in [0, 1]. Returns 0.0 if no closed trades.
    """
    if not closed_trades:
        return 0.0

    pnls = _pnl_values(closed_trades)
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0

    for pnl in pnls:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        if peak > 0:
            drawdown = (peak - cumulative) / peak
            if drawdown > max_dd:
                max_dd = drawdown

    return max_dd


def compute_summary(
    closed_trades: list[dict],
    open_count: int,
) -> dict[str, Any]:
    """Compute all performance metrics and return as a single dict.

    Args:
        closed_trades: List of closed trade dicts.
        open_count: Number of currently open positions (provided by caller).

    Returns:
        Dict with keys: total_pnl, win_rate, avg_win, avg_loss, sharpe_ratio,
        max_drawdown, trade_count, open_count.
    """
    return {
        "total_pnl": total_return(closed_trades),
        "win_rate": win_rate(closed_trades),
        "avg_win": avg_win(closed_trades),
        "avg_loss": avg_loss(closed_trades),
        "sharpe_ratio": sharpe_ratio(closed_trades),
        "max_drawdown": max_drawdown(closed_trades),
        "trade_count": len(closed_trades),
        "open_count": open_count,
    }
