"""Provider-neutral tool definitions and dispatch map for the recommendation engine.

Three tools, one responsibility each:
  get_macro_regime         — classify current market regime before evaluating any name
  get_top_factor_candidates — return the highest-ranked tickers by composite factor score
  get_technical_signals    — compute entry/stop/sizing signals for a list of tickers

TOOL_DEFINITIONS is passed verbatim to the Anthropic API.
TOOL_DISPATCH maps tool name → callable for the engine to invoke.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from signals.factors import compute_combined_factor_score
from signals.macro_regime import classify_regime, regime_position_size_multiplier
from signals.technical import compute_technical_signals, stop_price, suggested_position_size

DB_PATH = os.environ.get("QUANT_DB_PATH", "data/quant.db")
PAPER_ACCOUNT_VALUE = float(os.environ.get("PAPER_ACCOUNT_VALUE", "100000"))


# ── Tool definitions (passed to Anthropic API) ────────────────────────────────

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "get_macro_regime",
        "description": (
            "Returns the current macro regime (RISK_ON, TRANSITIONAL, RISK_OFF, or CRISIS) "
            "and the underlying indicators: VIX, 10Y-2Y yield spread, fed funds rate, and CPI. "
            "Call this first before evaluating any individual stock. The regime modifies how "
            "all other signals should be interpreted and scales position sizing. "
            "Do not use to get stock-level data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "as_of_date": {
                    "type": "string",
                    "description": "ISO format date YYYY-MM-DD. Omit to use the latest available data.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_top_factor_candidates",
        "description": (
            "Returns the top-ranked S&P 500 tickers by composite factor score (momentum + low volatility). "
            "A composite_score of 90 means the ticker ranks better than 90% of the universe. "
            "Use this to identify candidate tickers before drilling into technicals. "
            "Do not use to get technical entry signals or risk parameters — call get_technical_signals for that."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "top_n": {
                    "type": "integer",
                    "description": "Number of top candidates to return. Use 10 to have enough to filter after technical review.",
                },
                "as_of_date": {
                    "type": "string",
                    "description": "ISO format date YYYY-MM-DD. Omit to use the latest available data.",
                },
            },
            "required": ["top_n"],
        },
    },
    {
        "name": "get_technical_signals",
        "description": (
            "Returns technical signals, stop loss, and suggested position size for a list of tickers. "
            "Includes: close price, RSI(14), MA50/MA200 trend, MACD histogram, ATR(14), volume ratio, "
            "ATR-based stop price, and suggested share count based on 1% account risk rule. "
            "Use this after get_top_factor_candidates to determine whether a candidate has a valid entry. "
            "Do not use for macro regime — call get_macro_regime for that."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tickers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of S&P 500 ticker symbols to analyze. Example: [\"AAPL\", \"MSFT\", \"NVDA\"]",
                },
                "as_of_date": {
                    "type": "string",
                    "description": "ISO format date YYYY-MM-DD. Omit to use the latest available data.",
                },
            },
            "required": ["tickers"],
        },
    },
]


# ── Dispatch functions ─────────────────────────────────────────────────────────

def _get_macro_regime(as_of_date: str | None = None) -> dict[str, Any]:
    """Return macro regime snapshot as a JSON-serializable dict."""
    as_of = date.fromisoformat(as_of_date) if as_of_date else None
    snap = classify_regime(DB_PATH, as_of)
    size_multiplier = regime_position_size_multiplier(snap.regime)
    return {
        "regime": snap.regime,
        "vix": snap.vix,
        "t10y2y": snap.t10y2y,
        "fed_funds_rate": snap.fed_funds_rate,
        "cpi": snap.cpi,
        "as_of_date": str(snap.date),
        "position_size_multiplier": size_multiplier,
        "interpretation": _regime_interpretation(snap.regime),
    }


def _get_top_factor_candidates(
    top_n: int,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """Return top-N tickers by composite factor score."""
    as_of = date.fromisoformat(as_of_date) if as_of_date else None
    df = compute_combined_factor_score(DB_PATH, as_of)
    if df.empty:
        return {"candidates": [], "error": "Insufficient price data to compute factor scores"}

    top = df.head(top_n)[["ticker", "momentum_rank", "lowvol_rank", "composite_score", "as_of_date"]]
    records = top.to_dict(orient="records")
    for r in records:
        if hasattr(r.get("as_of_date"), "isoformat"):
            r["as_of_date"] = r["as_of_date"].isoformat()
    return {"candidates": records, "universe_size": len(df)}


def _get_technical_signals(
    tickers: list[str],
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """Return technical signals and risk parameters for each requested ticker."""
    as_of = date.fromisoformat(as_of_date) if as_of_date else None
    df = compute_technical_signals(DB_PATH, tickers, as_of)
    if df.empty:
        return {"signals": [], "error": "No price data found for requested tickers"}

    results = []
    for _, row in df.iterrows():
        entry = row["close"]
        atr = row["atr"]

        stop = stop_price(entry, atr) if (entry and atr) else None
        target = round(entry + 2 * (entry - stop), 4) if stop else None
        reward_risk = round((target - entry) / (entry - stop), 2) if stop else None

        # ATR sizing gives 1% account risk per trade, but can produce large dollar
        # exposure on low-volatility stocks. Cap at 10% of account per quant-finance rules.
        _MAX_POSITION_PCT = 0.10
        shares = suggested_position_size(PAPER_ACCOUNT_VALUE, entry, atr) if (entry and atr) else None
        if shares:
            max_shares = int(PAPER_ACCOUNT_VALUE * _MAX_POSITION_PCT / entry)
            shares = min(shares, max_shares)
        size_pct = round((shares * entry / PAPER_ACCOUNT_VALUE) * 100, 1) if shares else None

        results.append({
            "ticker":           row["ticker"],
            "as_of_date":       str(row["as_of_date"]),
            "close":            entry,
            "rsi":              row.get("rsi"),
            "ma50_above_ma200": row.get("ma50_above_ma200"),
            "macd_hist":        row.get("macd_hist"),
            "volume_ratio":     row.get("volume_ratio"),
            "atr":              atr,
            "stop":             stop,
            "target":           target,
            "reward_risk":      reward_risk,
            "suggested_shares": shares,
            "suggested_size_pct": size_pct,
        })

    return {"signals": results}


def _regime_interpretation(regime: str) -> str:
    """Plain-English summary of what the regime means for new positions."""
    return {
        "RISK_ON":      "Favorable conditions. Normal position sizing (up to 10% per name).",
        "TRANSITIONAL": "Mixed signals. Reduce new positions to 70% of normal size. Require higher conviction.",
        "RISK_OFF":     "Elevated risk. Half-size all new positions. Increase cash allocation.",
        "CRISIS":       "No new long positions. Preserve capital.",
    }[regime]


# ── Dispatch map ──────────────────────────────────────────────────────────────

TOOL_DISPATCH: dict[str, Any] = {
    "get_macro_regime":          _get_macro_regime,
    "get_top_factor_candidates": _get_top_factor_candidates,
    "get_technical_signals":     _get_technical_signals,
}
