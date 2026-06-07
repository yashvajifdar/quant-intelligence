"""Anthropic-backed recommendation engine.

Multi-tool loop flow:
  1. Model calls get_macro_regime (always first)
  2. Model calls get_top_factor_candidates to find ranked candidates
  3. Model calls get_technical_signals on those candidates
  4. Model synthesizes tool results into structured JSON
  5. Engine parses JSON + merges with collected signal data → typed RecommendationSet

Usage:
  from engine.anthropic_engine import run
  result = run("What should I buy this week?")
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
    MacroContext,
    Recommendation,
    RecommendationSet,
    RiskParameters,
    SignalSummary,
)

logger = logging.getLogger("quant_engine")

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048

SYSTEM_PROMPT = """[Role]
You are a quantitative investment analyst for a personal paper trading portfolio.
You have access to real-time S&P 500 factor scores, technical signals, and macro indicators.
Your job is to identify the top 3 investment opportunities backed by signal alignment.
Not financial advice — all recommendations are for paper trading only.

[Context]
Universe: ~503 S&P 500 constituents.
Paper account: $100,000.
Three tools available:
- get_macro_regime: current macro regime and indicators. Call this first, always.
- get_top_factor_candidates: top-ranked tickers by momentum + low-volatility composite score.
- get_technical_signals: entry, stop, target, RSI, MA, ATR signals for a list of tickers.

[Constraints]
1. Call get_macro_regime first on every query without exception.
2. If regime is CRISIS, return an empty recommendations list — no new positions in a crisis.
3. Call get_top_factor_candidates with top_n=10.
4. Call get_technical_signals on all 10 candidates.
5. Only recommend tickers where ALL of the following are true:
   - reward_risk >= 2.0
   - ma50_above_ma200 is true
   - rsi is below 80
6. Require two signal layers to agree: composite_score > 70 AND technical entry must confirm.
7. Conviction scale: 5=all signals strongly aligned, 4=most aligned, 3=mixed with strong primary, 2=marginal. Never use 1.

[Output]
After calling all tools, output ONLY a JSON object — no prose before or after:
{
  "recommendations": [
    {
      "ticker": "AAPL",
      "action": "BUY",
      "conviction": 4,
      "thesis": "Two or three sentences referencing specific signal values from the tool results.",
      "risks_to_thesis": "One or two sentences on what would invalidate this call."
    }
  ]
}
Return at most 3 recommendations. Fewer is acceptable if fewer than 3 pass all signal gates.
If the user asks about selling, shorting, existing holdings, or anything outside your BUY-signal scope,
still call all three tools, then return: {"recommendations": [], "note": "This engine identifies BUY opportunities only. Try: 'find me healthcare picks' or 'show me defensive stocks'."}"""


def run(query: str) -> RecommendationSet:
    """Run the recommendation engine for a user query.

    Executes the multi-tool loop, collects signal data, parses model output,
    and returns a typed RecommendationSet. Logs every tool call and token count.

    Args:
        query: Natural language question from the user.

    Returns:
        RecommendationSet with up to 3 typed Recommendation objects.

    Raises:
        ValueError: If ANTHROPIC_API_KEY is not set.
        json.JSONDecodeError: If the model returns malformed JSON.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set")

    client = anthropic.Anthropic(api_key=api_key)
    t0 = time.time()
    logger.info("engine.run query=%r", query)

    messages: list[dict] = [{"role": "user", "content": query}]

    # Signal data collected during tool execution — merged into Recommendation objects later
    macro_data: dict = {}
    factor_data: dict[str, dict] = {}    # ticker → factor scores
    technical_data: dict[str, dict] = {} # ticker → technical signals
    total_input_tokens = 0
    total_output_tokens = 0

    # ── Multi-tool execution loop ─────────────────────────────────────────────
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
                raise ValueError(f"Unknown tool requested by model: {block.name!r}")

            t_tool = time.time()
            result = fn(**block.input)
            logger.info(
                "tool=%s input=%s latency_ms=%d",
                block.name,
                json.dumps(block.input),
                int((time.time() - t_tool) * 1000),
            )

            # Store for later merging into typed dataclasses
            if block.name == "get_macro_regime":
                macro_data = result
            elif block.name == "get_top_factor_candidates":
                for c in result.get("candidates", []):
                    factor_data[c["ticker"]] = c
            elif block.name == "get_technical_signals":
                for s in result.get("signals", []):
                    technical_data[s["ticker"]] = s

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        messages.append({"role": "user", "content": tool_results})

    # ── Parse structured JSON from final response ─────────────────────────────
    raw = _extract_text(response)
    logger.info(
        "engine.run complete latency_ms=%d input_tokens=%d output_tokens=%d",
        int((time.time() - t0) * 1000),
        total_input_tokens,
        total_output_tokens,
    )

    parsed = _parse_json(raw)
    macro_ctx = _build_macro_context(macro_data)

    recommendations = []
    for rec in parsed.get("recommendations", []):
        ticker = rec["ticker"]
        tech = technical_data.get(ticker)
        factor = factor_data.get(ticker, {})

        if not tech:
            logger.warning("ticker=%s missing technical data — skipping", ticker)
            continue

        try:
            recommendation = Recommendation(
                ticker=ticker,
                action=rec["action"],
                conviction=rec["conviction"],
                macro=macro_ctx,
                signals=_build_signal_summary(factor, tech),
                thesis=rec["thesis"],
                risks_to_thesis=rec["risks_to_thesis"],
                risk_params=_build_risk_params(tech),
                as_of_date=date.fromisoformat(tech["as_of_date"][:10]),
            )
            recommendations.append(recommendation)
        except ValueError as exc:
            # Recommendation.__post_init__ blocks reward/risk < 2.0 or bad conviction
            logger.warning("ticker=%s suppressed: %s", ticker, exc)

    return RecommendationSet(
        recommendations=recommendations,
        macro=macro_ctx,
        query=query,
        generated_at=datetime.utcnow(),
        note=parsed.get("note"),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_text(response: anthropic.types.Message) -> str:
    """Pull the first text block from a response."""
    return next((b.text for b in response.content if hasattr(b, "text")), "")


def _parse_json(raw: str) -> dict:
    """Extract and parse the JSON object from the model's response.

    The model sometimes prefixes the JSON with reasoning prose. This finds
    the outermost {...} block regardless of surrounding text. Returns an
    empty recommendations dict if no JSON block is found (off-topic query).
    """
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        logger.warning("No JSON found in model response — returning empty set. raw=%r", raw[:300])
        return {"recommendations": [], "note": raw[:300]}
    return json.loads(match.group())


def _build_macro_context(data: dict) -> MacroContext:
    return MacroContext(
        regime=data.get("regime", "RISK_ON"),
        vix=data.get("vix"),
        t10y2y=data.get("t10y2y"),
        fed_funds_rate=data.get("fed_funds_rate"),
        cpi=data.get("cpi"),
        as_of_date=date.fromisoformat(data["as_of_date"]) if data.get("as_of_date") else date.today(),
    )


def _build_signal_summary(factor: dict, tech: dict) -> SignalSummary:
    return SignalSummary(
        momentum_rank=factor.get("momentum_rank"),
        momentum_return=factor.get("momentum_return"),
        lowvol_rank=factor.get("lowvol_rank"),
        realized_vol=factor.get("realized_vol"),
        composite_score=factor.get("composite_score"),
        close=tech.get("close"),
        rsi=tech.get("rsi"),
        ma50_above_ma200=tech.get("ma50_above_ma200"),
        macd_hist=tech.get("macd_hist"),
        volume_ratio=tech.get("volume_ratio"),
    )


def _build_risk_params(tech: dict) -> RiskParameters:
    entry = tech["close"]
    stop  = tech["stop"]
    target = tech["target"]
    return RiskParameters(
        entry=entry,
        stop=stop,
        target=target,
        reward_risk_ratio=tech["reward_risk"],
        atr=tech["atr"],
        suggested_shares=tech["suggested_shares"],
        suggested_size_pct=tech["suggested_size_pct"],
    )
