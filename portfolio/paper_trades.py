"""Paper trade log for the Quant Intelligence platform.

Manages two DuckDB tables:
  portfolios  — one row per portfolio (primary or ephemeral)
  paper_trades — one row per logged trade (open or closed)

All timestamps are UTC. All SQL lives in named functions. All queries are
parameterized — no string interpolation.

Not financial advice. All trades are simulated.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Any

import duckdb

# ── constants ──────────────────────────────────────────────────────────────────

STALE_THRESHOLD_DAYS: int = 90
EXIT_REASON_HIT_TARGET: str = "HIT_TARGET"
EXIT_REASON_HIT_STOP: str = "HIT_STOP"
EXIT_REASON_MANUAL: str = "MANUAL"
VALID_EXIT_REASONS: frozenset[str] = frozenset(
    [EXIT_REASON_HIT_TARGET, EXIT_REASON_HIT_STOP, EXIT_REASON_MANUAL]
)
LEADERBOARD_MIN_CLOSED_TRADES: int = 1


# ── schema ─────────────────────────────────────────────────────────────────────

_CREATE_PORTFOLIOS = """
CREATE TABLE IF NOT EXISTS portfolios (
    id              TEXT PRIMARY KEY,
    display_name    TEXT,
    is_primary      BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMP NOT NULL,
    last_active_at  TIMESTAMP NOT NULL
)
"""

_CREATE_PAPER_TRADES = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id               TEXT PRIMARY KEY,
    portfolio_id     TEXT NOT NULL REFERENCES portfolios(id),
    ticker           TEXT NOT NULL,
    action           TEXT NOT NULL,
    entry_date       DATE NOT NULL,
    entry_price      FLOAT NOT NULL,
    shares           INT NOT NULL,
    stop             FLOAT NOT NULL,
    target           FLOAT NOT NULL,
    signal_snapshot  TEXT NOT NULL,
    thesis           TEXT NOT NULL,
    exit_date        DATE,
    exit_price       FLOAT,
    exit_reason      TEXT,
    realized_pnl     FLOAT,
    created_at       TIMESTAMP NOT NULL,
    last_updated_at  TIMESTAMP NOT NULL
)
"""


# ── internal helpers ───────────────────────────────────────────────────────────

def _connect(db_path: str) -> duckdb.DuckDBPyConnection:
    """Return a writable DuckDB connection."""
    return duckdb.connect(db_path)


def _now_utc() -> datetime:
    """Current timestamp in UTC, timezone-aware."""
    return datetime.now(timezone.utc)


def _today() -> date:
    """Today's date in UTC."""
    return datetime.now(timezone.utc).date()


def _ensure_tables(conn: duckdb.DuckDBPyConnection) -> None:
    """Create tables if they do not exist (idempotent)."""
    conn.execute(_CREATE_PORTFOLIOS)
    conn.execute(_CREATE_PAPER_TRADES)


def _touch_portfolio(
    conn: duckdb.DuckDBPyConnection,
    portfolio_id: str,
    ts: datetime,
) -> None:
    """Update last_active_at for a portfolio."""
    conn.execute(
        "UPDATE portfolios SET last_active_at = ? WHERE id = ?",
        [ts, portfolio_id],
    )


def _row_to_dict(columns: list[str], row: tuple) -> dict[str, Any]:
    """Zip column names and a row tuple into a dict."""
    return dict(zip(columns, row))


def _deserialize_snapshot(row_dict: dict[str, Any]) -> dict[str, Any]:
    """Parse signal_snapshot JSON string back to dict in a trade row."""
    if "signal_snapshot" in row_dict and isinstance(row_dict["signal_snapshot"], str):
        row_dict["signal_snapshot"] = json.loads(row_dict["signal_snapshot"])
    return row_dict


def _compute_realized_pnl(
    action: str,
    entry_price: float,
    exit_price: float,
    shares: int,
) -> float:
    """Compute realized P&L for a closing trade."""
    if action == "BUY":
        return (exit_price - entry_price) * shares
    # SHORT: profit when price falls
    return (entry_price - exit_price) * shares


# ── public API ─────────────────────────────────────────────────────────────────

def create_portfolio(db_path: str, display_name: str | None = None) -> str:
    """Create a new portfolio and return its UUID.

    Args:
        db_path: Path to the DuckDB database file.
        display_name: Optional human-readable label for the leaderboard.

    Returns:
        UUID string of the newly created portfolio.
    """
    portfolio_id = str(uuid.uuid4())
    now = _now_utc()

    conn = _connect(db_path)
    _ensure_tables(conn)
    conn.execute(
        "INSERT INTO portfolios (id, display_name, is_primary, created_at, last_active_at) "
        "VALUES (?, ?, FALSE, ?, ?)",
        [portfolio_id, display_name, now, now],
    )
    conn.close()
    return portfolio_id


def get_portfolio(db_path: str, portfolio_id: str) -> dict | None:
    """Return the portfolio row as a dict, or None if not found.

    Args:
        db_path: Path to the DuckDB database file.
        portfolio_id: UUID of the portfolio to fetch.
    """
    conn = _connect(db_path)
    _ensure_tables(conn)
    row = conn.execute(
        "SELECT id, display_name, is_primary, created_at, last_active_at "
        "FROM portfolios WHERE id = ?",
        [portfolio_id],
    ).fetchone()
    conn.close()

    if row is None:
        return None
    columns = ["id", "display_name", "is_primary", "created_at", "last_active_at"]
    return _row_to_dict(columns, row)


def open_trade(
    db_path: str,
    portfolio_id: str,
    ticker: str,
    action: str,
    entry_price: float,
    shares: int,
    stop: float,
    target: float,
    signal_snapshot: dict,
    thesis: str,
) -> str:
    """Log a new open paper trade and return its UUID.

    The caller is responsible for passing today's closing price as entry_price.
    No price fetching occurs here — the function logs exactly what is given.

    Args:
        db_path: Path to the DuckDB database file.
        portfolio_id: UUID of the owning portfolio.
        ticker: Equity ticker symbol.
        action: "BUY" or "SHORT".
        entry_price: Closing price on recommendation day (no lookahead).
        shares: Number of shares to simulate.
        stop: Stop-loss price level.
        target: Price target.
        signal_snapshot: Full signals dict at entry time — stored as JSON.
        thesis: AI-generated or human rationale for the trade.

    Returns:
        UUID string of the newly created trade.
    """
    assert entry_price > 0, f"entry_price must be > 0, got {entry_price}"
    assert shares > 0, f"shares must be > 0, got {shares}"
    assert action in ("BUY", "SHORT"), f"action must be BUY or SHORT, got {action!r}"

    trade_id = str(uuid.uuid4())
    now = _now_utc()

    conn = _connect(db_path)
    _ensure_tables(conn)
    conn.execute(
        "INSERT INTO paper_trades "
        "(id, portfolio_id, ticker, action, entry_date, entry_price, shares, "
        " stop, target, signal_snapshot, thesis, created_at, last_updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            trade_id,
            portfolio_id,
            ticker,
            action,
            _today(),
            entry_price,
            shares,
            stop,
            target,
            json.dumps(signal_snapshot),
            thesis,
            now,
            now,
        ],
    )
    _touch_portfolio(conn, portfolio_id, now)
    conn.close()
    return trade_id


def close_trade(
    db_path: str,
    trade_id: str,
    exit_price: float,
    exit_reason: str,
) -> None:
    """Close an open trade, recording exit price and computing realized P&L.

    Args:
        db_path: Path to the DuckDB database file.
        trade_id: UUID of the trade to close.
        exit_price: Price at which the position is exited.
        exit_reason: One of HIT_TARGET, HIT_STOP, or MANUAL.
    """
    assert exit_reason in VALID_EXIT_REASONS, (
        f"exit_reason must be one of {sorted(VALID_EXIT_REASONS)}, got {exit_reason!r}"
    )

    conn = _connect(db_path)
    _ensure_tables(conn)

    row = conn.execute(
        "SELECT action, entry_price, shares, exit_date, portfolio_id "
        "FROM paper_trades WHERE id = ?",
        [trade_id],
    ).fetchone()

    assert row is not None, f"Trade {trade_id!r} does not exist"
    action, entry_price, shares, exit_date, portfolio_id = row
    assert exit_date is None, (
        f"Trade {trade_id!r} is already closed (exit_date={exit_date})"
    )

    now = _now_utc()
    pnl = _compute_realized_pnl(action, entry_price, exit_price, shares)

    conn.execute(
        "UPDATE paper_trades "
        "SET exit_date = ?, exit_price = ?, exit_reason = ?, realized_pnl = ?, "
        "    last_updated_at = ? "
        "WHERE id = ?",
        [_today(), exit_price, exit_reason, pnl, now, trade_id],
    )
    _touch_portfolio(conn, portfolio_id, now)
    conn.close()


def _fetch_trades(
    conn: duckdb.DuckDBPyConnection,
    portfolio_id: str,
    *,
    closed: bool,
) -> list[dict]:
    """Shared query for open or closed trades."""
    if closed:
        condition = "exit_date IS NOT NULL"
    else:
        condition = "exit_date IS NULL"

    rows = conn.execute(
        f"SELECT id, portfolio_id, ticker, action, entry_date, entry_price, shares, "  # noqa: S608
        f"stop, target, signal_snapshot, thesis, exit_date, exit_price, exit_reason, "
        f"realized_pnl, created_at, last_updated_at "
        f"FROM paper_trades "
        f"WHERE portfolio_id = ? AND {condition} "
        f"ORDER BY entry_date ASC",
        [portfolio_id],
    ).fetchall()

    columns = [
        "id", "portfolio_id", "ticker", "action", "entry_date", "entry_price",
        "shares", "stop", "target", "signal_snapshot", "thesis",
        "exit_date", "exit_price", "exit_reason", "realized_pnl",
        "created_at", "last_updated_at",
    ]
    return [_deserialize_snapshot(_row_to_dict(columns, r)) for r in rows]


def get_open_trades(db_path: str, portfolio_id: str) -> list[dict]:
    """Return all open positions for a portfolio.

    Args:
        db_path: Path to the DuckDB database file.
        portfolio_id: UUID of the portfolio to query.
    """
    conn = _connect(db_path)
    _ensure_tables(conn)
    result = _fetch_trades(conn, portfolio_id, closed=False)
    conn.close()
    return result


def get_closed_trades(db_path: str, portfolio_id: str) -> list[dict]:
    """Return all closed trades for a portfolio.

    Args:
        db_path: Path to the DuckDB database file.
        portfolio_id: UUID of the portfolio to query.
    """
    conn = _connect(db_path)
    _ensure_tables(conn)
    result = _fetch_trades(conn, portfolio_id, closed=True)
    conn.close()
    return result


def get_leaderboard(db_path: str, limit: int = 10) -> list[dict]:
    """Return top portfolios by total realized P&L.

    Only portfolios with at least one closed trade are included.

    Args:
        db_path: Path to the DuckDB database file.
        limit: Maximum number of portfolios to return.
    """
    conn = _connect(db_path)
    _ensure_tables(conn)

    rows = conn.execute(
        "SELECT p.id, p.display_name, "
        "       SUM(t.realized_pnl) AS total_pnl, "
        "       COUNT(t.id) AS trade_count, "
        "       AVG(CASE WHEN t.realized_pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate "
        "FROM portfolios p "
        "JOIN paper_trades t ON t.portfolio_id = p.id "
        "WHERE t.exit_date IS NOT NULL "
        "GROUP BY p.id, p.display_name "
        "HAVING COUNT(t.id) >= ? "
        "ORDER BY total_pnl DESC "
        "LIMIT ?",
        [LEADERBOARD_MIN_CLOSED_TRADES, limit],
    ).fetchall()

    conn.close()

    columns = ["portfolio_id", "display_name", "total_pnl", "trade_count", "win_rate"]
    return [_row_to_dict(columns, r) for r in rows]


def purge_stale_portfolios(db_path: str) -> int:
    """Delete portfolios that are stale, non-primary, and have no open trades.

    Stale = last_active_at older than STALE_THRESHOLD_DAYS days.
    is_primary portfolios are never deleted.
    Portfolios with open trades are never deleted.

    Args:
        db_path: Path to the DuckDB database file.

    Returns:
        Number of portfolios deleted.
    """
    conn = _connect(db_path)
    _ensure_tables(conn)

    # DuckDB does not support parameterized INTERVAL literals (? placeholder
    # is rejected inside INTERVAL expressions). STALE_THRESHOLD_DAYS is a
    # module-level integer constant — not user input — so embedding it as a
    # string literal in the f-string is safe and injection-free.
    stale_ids_rows = conn.execute(
        f"SELECT id FROM portfolios "
        f"WHERE is_primary = FALSE "
        f"  AND last_active_at < (CURRENT_TIMESTAMP - INTERVAL '{int(STALE_THRESHOLD_DAYS)}' DAY) "
        f"  AND id NOT IN ("
        f"      SELECT DISTINCT portfolio_id FROM paper_trades WHERE exit_date IS NULL"
        f"  )",
    ).fetchall()

    if not stale_ids_rows:
        conn.close()
        return 0

    stale_ids = [r[0] for r in stale_ids_rows]

    for pid in stale_ids:
        conn.execute("DELETE FROM paper_trades WHERE portfolio_id = ?", [pid])
        conn.execute("DELETE FROM portfolios WHERE id = ?", [pid])

    conn.close()
    return len(stale_ids)
