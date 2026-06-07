"""Tests for the paper portfolio tracker (M4).

All DB tests use a temp DuckDB file — the real data/quant.db is never touched.

Known-value assertions:
  - BUY trade: open at $100, close at $110, 10 shares → pnl = $100
  - SHORT trade: short at $100, close at $90, 10 shares → pnl = $100
  - Win rate: 2 wins + 1 loss → 0.6667
  - Max drawdown: cumulative PnL [100, 80, 120, 60] → 0.5
  - purge_stale_portfolios: is_primary portfolio is never deleted
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from portfolio.paper_trades import (
    close_trade,
    create_portfolio,
    get_closed_trades,
    get_leaderboard,
    get_open_trades,
    get_portfolio,
    open_trade,
    purge_stale_portfolios,
    STALE_THRESHOLD_DAYS,
    _ensure_tables,
)
from portfolio.performance import (
    avg_loss,
    avg_win,
    compute_summary,
    max_drawdown,
    sharpe_ratio,
    total_return,
    win_rate,
)

# ── fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_db(tmp_path) -> str:
    """Fresh temp DuckDB file for each test."""
    return str(tmp_path / "test_portfolio.db")


@pytest.fixture()
def portfolio_id(tmp_db: str) -> str:
    """A freshly created portfolio in a temp DB."""
    return create_portfolio(tmp_db, display_name="Test Portfolio")


_SIGNAL_SNAPSHOT = {"momentum_rank": 72.5, "rsi": 58.3, "regime": "RISK_ON"}
_THESIS = "Strong momentum with healthy RSI; macro regime is supportive."


def _open_buy_trade(
    tmp_db: str,
    portfolio_id: str,
    entry_price: float = 100.0,
    shares: int = 10,
) -> str:
    return open_trade(
        tmp_db,
        portfolio_id,
        ticker="AAPL",
        action="BUY",
        entry_price=entry_price,
        shares=shares,
        stop=95.0,
        target=115.0,
        signal_snapshot=_SIGNAL_SNAPSHOT,
        thesis=_THESIS,
    )


def _open_short_trade(
    tmp_db: str,
    portfolio_id: str,
    entry_price: float = 100.0,
    shares: int = 10,
) -> str:
    return open_trade(
        tmp_db,
        portfolio_id,
        ticker="MSFT",
        action="SHORT",
        entry_price=entry_price,
        shares=shares,
        stop=107.0,
        target=85.0,
        signal_snapshot=_SIGNAL_SNAPSHOT,
        thesis="Weak momentum; distribution pattern in volume.",
    )


# ── paper_trades tests ─────────────────────────────────────────────────────────

def test_open_trade_writes_to_db(tmp_db: str, portfolio_id: str) -> None:
    """A newly opened trade must appear in get_open_trades."""
    trade_id = _open_buy_trade(tmp_db, portfolio_id)

    open_trades = get_open_trades(tmp_db, portfolio_id)
    assert len(open_trades) == 1
    trade = open_trades[0]
    assert trade["id"] == trade_id
    assert trade["ticker"] == "AAPL"
    assert trade["action"] == "BUY"
    assert trade["entry_price"] == 100.0
    assert trade["shares"] == 10
    assert trade["exit_date"] is None
    # signal_snapshot must be returned as dict, not raw JSON string
    assert isinstance(trade["signal_snapshot"], dict)
    assert trade["signal_snapshot"]["regime"] == "RISK_ON"


def test_close_trade_computes_pnl_correctly(tmp_db: str, portfolio_id: str) -> None:
    """BUY at $100, close at $110, 10 shares → realized_pnl = $100."""
    trade_id = _open_buy_trade(tmp_db, portfolio_id, entry_price=100.0, shares=10)
    close_trade(tmp_db, trade_id, exit_price=110.0, exit_reason="HIT_TARGET")

    closed = get_closed_trades(tmp_db, portfolio_id)
    assert len(closed) == 1
    assert closed[0]["realized_pnl"] == pytest.approx(100.0)
    assert closed[0]["exit_reason"] == "HIT_TARGET"
    assert closed[0]["exit_price"] == pytest.approx(110.0)
    assert closed[0]["exit_date"] is not None


def test_close_trade_short_pnl(tmp_db: str, portfolio_id: str) -> None:
    """SHORT at $100, close at $90, 10 shares → realized_pnl = $100."""
    trade_id = _open_short_trade(tmp_db, portfolio_id, entry_price=100.0, shares=10)
    close_trade(tmp_db, trade_id, exit_price=90.0, exit_reason="HIT_TARGET")

    closed = get_closed_trades(tmp_db, portfolio_id)
    assert len(closed) == 1
    assert closed[0]["realized_pnl"] == pytest.approx(100.0)


def test_close_trade_fails_if_already_closed(tmp_db: str, portfolio_id: str) -> None:
    """Closing an already-closed trade must raise an AssertionError."""
    trade_id = _open_buy_trade(tmp_db, portfolio_id)
    close_trade(tmp_db, trade_id, exit_price=110.0, exit_reason="MANUAL")

    with pytest.raises(AssertionError, match="already closed"):
        close_trade(tmp_db, trade_id, exit_price=120.0, exit_reason="MANUAL")


def test_open_trade_rejects_zero_price(tmp_db: str, portfolio_id: str) -> None:
    """entry_price = 0 must raise AssertionError."""
    with pytest.raises(AssertionError, match="entry_price must be > 0"):
        _open_buy_trade(tmp_db, portfolio_id, entry_price=0.0)


def test_open_trade_rejects_zero_shares(tmp_db: str, portfolio_id: str) -> None:
    """shares = 0 must raise AssertionError."""
    with pytest.raises(AssertionError, match="shares must be > 0"):
        _open_buy_trade(tmp_db, portfolio_id, shares=0)


def test_get_portfolio_returns_none_for_missing(tmp_db: str) -> None:
    """get_portfolio returns None when portfolio_id does not exist."""
    result = get_portfolio(tmp_db, "00000000-0000-0000-0000-000000000000")
    assert result is None


def test_get_portfolio_returns_correct_row(tmp_db: str, portfolio_id: str) -> None:
    """get_portfolio returns a dict matching the created portfolio."""
    result = get_portfolio(tmp_db, portfolio_id)
    assert result is not None
    assert result["id"] == portfolio_id
    assert result["display_name"] == "Test Portfolio"
    assert result["is_primary"] is False


def test_leaderboard_includes_closed_trades_only(
    tmp_db: str, portfolio_id: str
) -> None:
    """Leaderboard excludes portfolios with only open trades."""
    _open_buy_trade(tmp_db, portfolio_id)
    # No closed trades yet — should not appear on leaderboard
    board = get_leaderboard(tmp_db)
    assert not any(r["portfolio_id"] == portfolio_id for r in board)


def test_leaderboard_appears_after_close(tmp_db: str, portfolio_id: str) -> None:
    """Portfolio appears on leaderboard only after at least one trade is closed."""
    trade_id = _open_buy_trade(tmp_db, portfolio_id, entry_price=100.0, shares=10)
    close_trade(tmp_db, trade_id, exit_price=110.0, exit_reason="HIT_TARGET")

    board = get_leaderboard(tmp_db)
    assert len(board) == 1
    assert board[0]["portfolio_id"] == portfolio_id
    assert board[0]["total_pnl"] == pytest.approx(100.0)
    assert board[0]["trade_count"] == 1
    assert board[0]["win_rate"] == pytest.approx(1.0)


def test_purge_stale_portfolios_skips_primary(tmp_db: str) -> None:
    """is_primary portfolios are never purged, regardless of last_active_at."""
    conn = duckdb.connect(tmp_db)
    _ensure_tables(conn)

    import uuid
    from datetime import timezone

    primary_id = str(uuid.uuid4())
    stale_ts = datetime.now(timezone.utc) - timedelta(days=STALE_THRESHOLD_DAYS + 10)

    conn.execute(
        "INSERT INTO portfolios (id, display_name, is_primary, created_at, last_active_at) "
        "VALUES (?, ?, TRUE, ?, ?)",
        [primary_id, "Primary", stale_ts, stale_ts],
    )
    conn.close()

    deleted = purge_stale_portfolios(tmp_db)
    assert deleted == 0
    assert get_portfolio(tmp_db, primary_id) is not None


def test_purge_stale_portfolios_deletes_stale_non_primary(tmp_db: str) -> None:
    """Stale, non-primary, no-open-trades portfolios are deleted."""
    conn = duckdb.connect(tmp_db)
    _ensure_tables(conn)

    import uuid

    stale_id = str(uuid.uuid4())
    stale_ts = datetime.now(timezone.utc) - timedelta(days=STALE_THRESHOLD_DAYS + 10)

    conn.execute(
        "INSERT INTO portfolios (id, display_name, is_primary, created_at, last_active_at) "
        "VALUES (?, ?, FALSE, ?, ?)",
        [stale_id, "Stale", stale_ts, stale_ts],
    )
    conn.close()

    deleted = purge_stale_portfolios(tmp_db)
    assert deleted == 1
    assert get_portfolio(tmp_db, stale_id) is None


def test_purge_stale_portfolios_skips_portfolio_with_open_trade(tmp_db: str) -> None:
    """A stale portfolio that still has an open trade must not be purged."""
    conn = duckdb.connect(tmp_db)
    _ensure_tables(conn)

    import uuid

    stale_id = str(uuid.uuid4())
    stale_ts = datetime.now(timezone.utc) - timedelta(days=STALE_THRESHOLD_DAYS + 10)

    conn.execute(
        "INSERT INTO portfolios (id, display_name, is_primary, created_at, last_active_at) "
        "VALUES (?, ?, FALSE, ?, ?)",
        [stale_id, "Stale with open trade", stale_ts, stale_ts],
    )
    conn.close()

    # Open a trade on this portfolio — should protect it from purge
    open_trade(
        tmp_db,
        stale_id,
        ticker="NVDA",
        action="BUY",
        entry_price=500.0,
        shares=5,
        stop=480.0,
        target=550.0,
        signal_snapshot={},
        thesis="Test",
    )

    deleted = purge_stale_portfolios(tmp_db)
    assert deleted == 0
    assert get_portfolio(tmp_db, stale_id) is not None


def test_last_active_at_updated_on_open(tmp_db: str, portfolio_id: str) -> None:
    """Opening a trade must update portfolio.last_active_at."""
    before = get_portfolio(tmp_db, portfolio_id)["last_active_at"]
    _open_buy_trade(tmp_db, portfolio_id)
    after = get_portfolio(tmp_db, portfolio_id)["last_active_at"]
    assert after >= before


# ── performance tests ──────────────────────────────────────────────────────────

_WIN = {"realized_pnl": 200.0}
_LOSS = {"realized_pnl": -100.0}
_BREAKEVEN = {"realized_pnl": 0.0}


def test_total_return_empty() -> None:
    assert total_return([]) == 0.0


def test_total_return_known_value() -> None:
    trades = [_WIN, _WIN, _LOSS]
    assert total_return(trades) == pytest.approx(300.0)


def test_win_rate_known_values() -> None:
    """2 wins, 1 loss → win_rate = 2/3 ≈ 0.667."""
    trades = [_WIN, _WIN, _LOSS]
    assert win_rate(trades) == pytest.approx(2 / 3, rel=1e-3)


def test_win_rate_empty() -> None:
    assert win_rate([]) == 0.0


def test_win_rate_all_wins() -> None:
    trades = [_WIN, _WIN]
    assert win_rate(trades) == pytest.approx(1.0)


def test_avg_win_returns_none_for_no_winners() -> None:
    assert avg_win([_LOSS, _BREAKEVEN]) is None


def test_avg_win_known_value() -> None:
    trades = [{"realized_pnl": 100.0}, {"realized_pnl": 200.0}]
    assert avg_win(trades) == pytest.approx(150.0)


def test_avg_loss_returns_none_for_no_losers() -> None:
    assert avg_loss([_WIN]) is None


def test_avg_loss_known_value() -> None:
    trades = [{"realized_pnl": -50.0}, {"realized_pnl": -150.0}]
    assert avg_loss(trades) == pytest.approx(-100.0)


def test_sharpe_ratio_returns_none_for_single_trade() -> None:
    """Fewer than 2 trades → Sharpe must return None."""
    assert sharpe_ratio([_WIN]) is None


def test_sharpe_ratio_returns_none_for_empty() -> None:
    assert sharpe_ratio([]) is None


def test_sharpe_ratio_constant_returns_is_none() -> None:
    """Zero std dev in returns → return None rather than divide by zero."""
    trades = [{"realized_pnl": 100.0}, {"realized_pnl": 100.0}]
    # With risk_free_rate=0, excess returns are constant → std=0 → None
    result = sharpe_ratio(trades, risk_free_rate=0.0)
    assert result is None


def test_sharpe_ratio_positive_for_consistent_winners() -> None:
    """A series of positive, varied returns must produce a positive Sharpe."""
    trades = [{"realized_pnl": v} for v in [100, 120, 80, 150, 90]]
    result = sharpe_ratio(trades, risk_free_rate=0.0)
    assert result is not None
    assert result > 0.0


def test_max_drawdown_known_values() -> None:
    """Cumulative PnL path: 100, 80, 120, 60 → peak=120, trough=60 → dd=60/120=0.5."""
    # Individual PnLs that produce that cumulative path:
    # +100, -20, +40, -60  → cumulative: 100, 80, 120, 60
    trades = [
        {"realized_pnl": 100.0},
        {"realized_pnl": -20.0},
        {"realized_pnl": 40.0},
        {"realized_pnl": -60.0},
    ]
    result = max_drawdown(trades)
    assert result == pytest.approx(0.5, rel=1e-6)


def test_max_drawdown_empty() -> None:
    assert max_drawdown([]) == 0.0


def test_max_drawdown_no_drawdown() -> None:
    """Strictly increasing cumulative PnL → max drawdown = 0."""
    trades = [{"realized_pnl": 50.0}, {"realized_pnl": 50.0}]
    assert max_drawdown(trades) == pytest.approx(0.0)


def test_compute_summary_returns_all_keys() -> None:
    """compute_summary must return a dict with all expected keys."""
    trades = [_WIN, _WIN, _LOSS]
    result = compute_summary(trades, open_count=2)
    expected_keys = {
        "total_pnl", "win_rate", "avg_win", "avg_loss",
        "sharpe_ratio", "max_drawdown", "trade_count", "open_count",
    }
    assert set(result.keys()) == expected_keys
    assert result["trade_count"] == 3
    assert result["open_count"] == 2
    assert result["win_rate"] == pytest.approx(2 / 3, rel=1e-3)
