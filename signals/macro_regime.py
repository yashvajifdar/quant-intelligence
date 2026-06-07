"""Macro regime classifier.

Reads the macro table and returns one of four regime states:
  RISK_ON      — bull market conditions, low fear, normal yield curve
  TRANSITIONAL — mixed signals, require higher conviction
  RISK_OFF     — elevated fear or inverted yield curve
  CRISIS       — VIX > 35, reduce all risk immediately

Regime context modifies every downstream signal — a momentum signal in
RISK_OFF means something different than the same signal in RISK_ON.
"""

from __future__ import annotations

from datetime import date
from typing import NamedTuple

import duckdb

# Thresholds from quant-finance agent standards
_VIX_CRISIS       = 35.0
_VIX_RISK_OFF     = 25.0
_VIX_TRANSITIONAL = 18.0
_YIELD_INVERTED   = 0.0   # t10y2y < 0 = inverted = recession signal
_YIELD_FLAT       = 0.25  # t10y2y < 0.25 = flattening = caution

REGIMES = ("RISK_ON", "TRANSITIONAL", "RISK_OFF", "CRISIS")


class MacroSnapshot(NamedTuple):
    date: date
    t10y2y: float | None
    vix: float | None
    fed_funds_rate: float | None
    cpi: float | None
    regime: str


def _latest_macro_row(conn: duckdb.DuckDBPyConnection, as_of: date | None) -> tuple:
    """Return the most recent macro row with non-null t10y2y on or before as_of."""
    if as_of:
        row = conn.execute(
            "SELECT date, t10y2y, vix, fed_funds_rate, cpi FROM macro "
            "WHERE date <= ? ORDER BY date DESC LIMIT 1",
            [as_of],
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT date, t10y2y, vix, fed_funds_rate, cpi FROM macro "
            "ORDER BY date DESC LIMIT 1"
        ).fetchone()
    if not row:
        raise ValueError("No macro data in database — run ETL first")
    return row


def _classify(t10y2y: float | None, vix: float | None) -> str:
    """Apply regime rules given yield curve spread and VIX. Pure function — no I/O."""
    if vix is not None and vix > _VIX_CRISIS:
        return "CRISIS"
    if (vix is not None and vix > _VIX_RISK_OFF) or \
       (t10y2y is not None and t10y2y < _YIELD_INVERTED):
        return "RISK_OFF"
    if (vix is not None and vix > _VIX_TRANSITIONAL) or \
       (t10y2y is not None and t10y2y < _YIELD_FLAT):
        return "TRANSITIONAL"
    return "RISK_ON"


def classify_regime(db_path: str, as_of_date: date | None = None) -> MacroSnapshot:
    """Return the macro regime and full snapshot for a given date (default: latest).

    Args:
        db_path: Path to DuckDB warehouse.
        as_of_date: Classify regime as of this date. Defaults to most recent row.

    Returns:
        MacroSnapshot with regime string and raw indicator values.
    """
    conn = duckdb.connect(db_path, read_only=True)
    row = _latest_macro_row(conn, as_of_date)
    conn.close()

    row_date, t10y2y, vix, fed_funds, cpi = row
    regime = _classify(t10y2y, vix)

    return MacroSnapshot(
        date=row_date,
        t10y2y=t10y2y,
        vix=vix,
        fed_funds_rate=fed_funds,
        cpi=cpi,
        regime=regime,
    )


def regime_position_size_multiplier(regime: str) -> float:
    """Return position size multiplier for a given regime.

    Per quant-finance agent standards:
      RISK_ON      → 1.0  (normal sizing)
      TRANSITIONAL → 0.7  (reduce new positions)
      RISK_OFF     → 0.5  (half sizing, increase cash)
      CRISIS       → 0.0  (no new positions)
    """
    return {"RISK_ON": 1.0, "TRANSITIONAL": 0.7, "RISK_OFF": 0.5, "CRISIS": 0.0}[regime]
