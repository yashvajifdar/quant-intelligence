"""Portfolio review engine — assesses existing positions and provides portfolio-level insights.

Flow:
  1. Model calls get_macro_regime
  2. Model calls get_technical_signals on held tickers
  3. Model calls get_top_factor_candidates (top 20) to check factor rank + find hedges
  4. Model synthesizes: HOLD/ADD/TRIM/EXIT per position + portfolio insights

Usage:
  from engine.review_engine import run_review
  result = run_review(open_trades)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, datetime

import anthropic

from engine.engine_tools import TOOL_DEFINITIONS, TOOL_DISPATCH
from engine.recommendation import (
    HedgeSuggestion,
    MacroContext,
    PortfolioInsights,
    PortfolioReview,
    PositionReview,
)

logger = logging.getLogger("quant_review")

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 3072

SYSTEM_PROMPT = """[Role]
You are a quantitative portfolio manager reviewing existing open positions for a $100,000 paper trading account.
You assess whether each position's original thesis still holds and provide portfolio-level risk guidance.
Not financial advice — paper trading only.

[Tools]
- get_macro_regime: always call this first. Regime change since entry is a key review signal.
- get_technical_signals: call on ALL held tickers to get current RSI, MA trend, MACD, and updated stops.
- get_top_factor_candidates: call with top_n=20 to check if held tickers still rank well, and to find hedge/diversifier candidates outside the current portfolio.

[Per-Position Verdict Rules]
EXIT if ANY of: MA50 crossed below MA200, RSI > 82, current price within 1.5% of stop, or composite_score dropped below 30.
TRIM if: composite_score dropped >15 points since entry OR RSI > 72 OR regime changed to RISK_OFF.
ADD if: composite_score improved AND RSI < 65 AND MA50 still above MA200 AND regime is RISK_ON or TRANSITIONAL.
HOLD: everything else — thesis intact, no strong signal either way.

[Portfolio Insight Rules]
- Note any sector concentration (2+ tickers in the same sector = concentration risk)
- Regime impact: explain how the current macro regime affects the overall portfolio
- hedge_suggestions: 1-2 tickers that reduce portfolio risk. Use broad instruments like GLD, TLT, XLV, XLU, or low-beta S&P names — NOT tickers already in the portfolio
- diversifier_suggestions: 1-2 HIGH-RANKING tickers from get_top_factor_candidates that fill a sector gap NOT covered by existing holdings

[Output]
After calling all three tools, return ONLY a JSON object — no prose before or after:
{
  "position_reviews": [
    {
      "ticker": "JNJ",
      "verdict": "HOLD",
      "conviction": 3,
      "signal_summary": "RSI 58, MA uptrend intact, composite 71",
      "updated_thesis": "Original thesis holds. Momentum stable since entry.",
      "risk_note": "Stop at $227.50 is 2.3% below current price. Watch for RSI approaching 75."
    }
  ],
  "portfolio_insights": {
    "regime_impact": "TRANSITIONAL regime: hold existing positions but avoid adding new risk.",
    "concentration_risk": "JNJ and ETR are defensive/utilities-adjacent. Portfolio skews low-beta.",
    "hedge_suggestions": [
      {"ticker": "GLD", "rationale": "Inflation/volatility hedge; low correlation to current holdings."}
    ],
    "diversifier_suggestions": [
      {"ticker": "NVDA", "rationale": "Top composite score; adds tech exposure to balance defensive tilt."}
    ]
  }
}"""


def run_review(open_trades: list[dict]) -> PortfolioReview:
    """Run the portfolio review engine against a list of open trades.

    Args:
        open_trades: List of trade dicts from paper_trades.get_open_trades().

    Returns:
        PortfolioReview with per-position verdicts and portfolio insights.

    Raises:
        ValueError: If ANTHROPIC_API_KEY is not set or no open trades provided.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set")
    if not open_trades:
        raise ValueError("No open trades to review")

    client = anthropic.Anthropic(api_key=api_key)
    t0 = time.time()

    # Build context message describing the portfolio
    positions_text = "\n".join(
        f"- {t['ticker']}: {t['shares']} shares, {t['action']} @ ${t['entry_price']:.2f} "
        f"on {t['entry_date']}, stop ${t['stop']:.2f}, target ${t['target']:.2f}. "
        f"Thesis: {t['thesis']}"
        for t in open_trades
    )
    held_tickers = [t["ticker"] for t in open_trades]
    user_message = (
        f"Review my current open positions and provide portfolio-level insights.\n\n"
        f"Open positions:\n{positions_text}\n\n"
        f"Held tickers: {held_tickers}"
    )

    logger.info("review_engine.run tickers=%s", held_tickers)

    messages: list[dict] = [{"role": "user", "content": user_message}]
    macro_data: dict = {}
    factor_data: dict[str, dict] = {}
    total_input_tokens = 0
    total_output_tokens = 0

    # ── Multi-tool loop ───────────────────────────────────────────────────────
    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )
        total_input_tokens  += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        if response.stop_reason != "tool_use":
            break

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            fn = TOOL_DISPATCH.get(block.name)
            if fn is None:
                raise ValueError(f"Unknown tool: {block.name!r}")

            t_tool = time.time()
            result = fn(**block.input)
            logger.info(
                "tool=%s input=%s latency_ms=%d",
                block.name,
                json.dumps(block.input),
                int((time.time() - t_tool) * 1000),
            )

            if block.name == "get_macro_regime":
                macro_data = result
            elif block.name == "get_top_factor_candidates":
                for c in result.get("candidates", []):
                    factor_data[c["ticker"]] = c

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        messages.append({"role": "user", "content": tool_results})

    raw = next((b.text for b in response.content if hasattr(b, "text")), "")
    logger.info(
        "review_engine.run complete latency_ms=%d input_tokens=%d output_tokens=%d",
        int((time.time() - t0) * 1000),
        total_input_tokens,
        total_output_tokens,
    )

    return _parse_review(raw, macro_data)


# ── Parsing ───────────────────────────────────────────────────────────────────

def _parse_review(raw: str, macro_data: dict) -> PortfolioReview:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        logger.warning("No JSON in review response — raw=%r", raw[:300])
        return _empty_review(macro_data, note=raw[:300])

    parsed = json.loads(match.group())
    macro_ctx = _build_macro(macro_data)

    position_reviews = [
        PositionReview(
            ticker=r["ticker"],
            verdict=r["verdict"],
            conviction=r.get("conviction", 3),
            signal_summary=r.get("signal_summary", ""),
            updated_thesis=r.get("updated_thesis", ""),
            risk_note=r.get("risk_note", ""),
        )
        for r in parsed.get("position_reviews", [])
    ]

    raw_insights = parsed.get("portfolio_insights", {})
    insights = PortfolioInsights(
        regime_impact=raw_insights.get("regime_impact", ""),
        concentration_risk=raw_insights.get("concentration_risk", ""),
        hedge_suggestions=[
            HedgeSuggestion(ticker=h["ticker"], rationale=h["rationale"])
            for h in raw_insights.get("hedge_suggestions", [])
        ],
        diversifier_suggestions=[
            HedgeSuggestion(ticker=h["ticker"], rationale=h["rationale"])
            for h in raw_insights.get("diversifier_suggestions", [])
        ],
    )

    return PortfolioReview(
        position_reviews=position_reviews,
        portfolio_insights=insights,
        macro=macro_ctx,
        generated_at=datetime.utcnow(),
    )


def _build_macro(data: dict) -> MacroContext:
    return MacroContext(
        regime=data.get("regime", "RISK_ON"),
        vix=data.get("vix"),
        t10y2y=data.get("t10y2y", 0.0),
        fed_funds_rate=data.get("fed_funds_rate", 0.0),
        cpi=data.get("cpi"),
        as_of_date=date.fromisoformat(data["as_of_date"]) if data.get("as_of_date") else date.today(),
    )


def _empty_review(macro_data: dict, note: str = "") -> PortfolioReview:
    return PortfolioReview(
        position_reviews=[],
        portfolio_insights=PortfolioInsights(
            regime_impact=note,
            concentration_risk="",
            hedge_suggestions=[],
            diversifier_suggestions=[],
        ),
        macro=_build_macro(macro_data),
    )
