"""Typed output model for AI investment recommendations.

Every field that appears on the dashboard or gets logged to paper_trades
lives here. The AI engine produces a list[Recommendation]; the API serializes
it to JSON; the dashboard renders it.

Not financial advice. All recommendations are for paper trading only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

Action = Literal["BUY", "HOLD", "AVOID"]

# Reward/risk below this threshold is suppressed before reaching the caller.
MIN_REWARD_RISK = 2.0


@dataclass
class MacroContext:
    """Regime snapshot at the time the recommendation was generated."""

    regime: Literal["RISK_ON", "TRANSITIONAL", "RISK_OFF", "CRISIS"]
    vix: float | None
    t10y2y: float | None
    fed_funds_rate: float | None
    cpi: float | None
    as_of_date: date


@dataclass
class SignalSummary:
    """All signal scores for one ticker at recommendation time.

    Ranks are cross-sectional percentiles 0–100 (higher = better).
    None means the signal could not be computed (insufficient data).
    """

    # Factor signals
    momentum_rank: float | None       # 12-1 month momentum percentile
    momentum_return: float | None     # raw 12-1 month return
    lowvol_rank: float | None         # low-volatility percentile
    realized_vol: float | None        # annualized 252-day vol
    composite_score: float | None     # weighted combo of momentum + low-vol

    # Technical signals
    close: float | None               # latest close price
    rsi: float | None                 # RSI(14)
    ma50_above_ma200: bool | None     # golden cross flag
    macd_hist: float | None           # positive = bullish momentum
    volume_ratio: float | None        # today's vol / 20-day avg


@dataclass
class RiskParameters:
    """Position sizing and exit levels per quant-finance risk framework."""

    entry: float                  # current close — logged at day's close (no lookahead)
    stop: float                   # entry - 1.25 × ATR(14)
    target: float                 # entry + 2 × (entry - stop) at minimum
    reward_risk_ratio: float      # (target - entry) / (entry - stop)
    atr: float                    # ATR(14) used to derive stop
    suggested_shares: int         # shares, based on 1% account risk rule
    suggested_size_pct: float     # suggested_shares × entry / account_value


@dataclass
class Recommendation:
    """A single AI investment recommendation for one ticker.

    Produced by the engine; serialized by the API; rendered on the dashboard;
    logged to paper_trades when a position is opened.
    """

    ticker: str
    action: Action
    conviction: int               # 1 (low) to 5 (high)
    macro: MacroContext
    signals: SignalSummary
    thesis: str                   # 2-3 sentence AI-generated rationale
    risks_to_thesis: str          # what would invalidate this call
    risk_params: RiskParameters
    as_of_date: date              # signals computed as of this date
    generated_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        """Enforce reward/risk minimum. Raises if recommendation should not ship."""
        if self.risk_params.reward_risk_ratio < MIN_REWARD_RISK:
            raise ValueError(
                f"{self.ticker}: reward/risk {self.risk_params.reward_risk_ratio:.2f} "
                f"is below minimum {MIN_REWARD_RISK}. Recommendation suppressed."
            )
        if not (1 <= self.conviction <= 5):
            raise ValueError(f"{self.ticker}: conviction must be 1–5, got {self.conviction}")


@dataclass
class RecommendationSet:
    """The full response from one engine query — up to 3 recommendations."""

    recommendations: list[Recommendation]
    macro: MacroContext           # shared regime context for the whole set
    query: str                    # original user question
    generated_at: datetime = field(default_factory=datetime.utcnow)
    note: str | None = None       # set when engine returns no recs (off-topic query)
