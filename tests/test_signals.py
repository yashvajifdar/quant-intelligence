"""Signal function tests — all assertions on known, deterministic values.

Test data is defined in conftest.py:
  AAPL: compound growth 0.15%/day → strong positive momentum, elevated RSI
  MSFT: compound decline 0.07%/day → negative momentum
  JPM:  flat at $100 → zero momentum

Macro fixtures:
  test_db      → VIX=15, t10y2y=0.5  → RISK_ON
  crisis_db    → VIX=40               → CRISIS
  risk_off_db  → VIX=28               → RISK_OFF
  inverted_yield_db → t10y2y=-0.2     → RISK_OFF (inverted curve)
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from signals.macro_regime import (
    MacroSnapshot,
    classify_regime,
    regime_position_size_multiplier,
    _classify,
)
from signals.technical import (
    compute_technical_signals,
    stop_price,
    suggested_position_size,
    _rsi,
)
from signals.factors import (
    compute_momentum_scores,
    compute_lowvol_scores,
    compute_combined_factor_score,
)


# ── macro regime ──────────────────────────────────────────────────────────────

class TestClassifyRegime:
    def test_risk_on_returns_correct_regime(self, test_db):
        snap = classify_regime(test_db)
        assert snap.regime == "RISK_ON"

    def test_risk_on_returns_macro_snapshot(self, test_db):
        snap = classify_regime(test_db)
        assert isinstance(snap, MacroSnapshot)
        assert snap.vix == pytest.approx(15.0)
        assert snap.t10y2y == pytest.approx(0.5)

    def test_crisis_when_vix_above_35(self, crisis_db):
        snap = classify_regime(crisis_db)
        assert snap.regime == "CRISIS"

    def test_risk_off_when_vix_above_25(self, risk_off_db):
        snap = classify_regime(risk_off_db)
        assert snap.regime == "RISK_OFF"

    def test_risk_off_when_yield_curve_inverted(self, inverted_yield_db):
        snap = classify_regime(inverted_yield_db)
        assert snap.regime == "RISK_OFF"

    def test_empty_db_raises(self, tmp_path):
        from etl.schema import initialize_schema
        db = str(tmp_path / "empty.db")
        initialize_schema(db)
        with pytest.raises(ValueError, match="No macro data"):
            classify_regime(db)


class TestClassifyPureFunction:
    """Test _classify() in isolation — no DB required."""

    def test_crisis_vix_threshold(self):
        assert _classify(t10y2y=0.5, vix=36.0) == "CRISIS"

    def test_crisis_boundary_exactly_35_is_not_crisis(self):
        assert _classify(t10y2y=0.5, vix=35.0) != "CRISIS"

    def test_risk_off_high_vix(self):
        assert _classify(t10y2y=0.5, vix=26.0) == "RISK_OFF"

    def test_risk_off_inverted_curve(self):
        assert _classify(t10y2y=-0.1, vix=14.0) == "RISK_OFF"

    def test_transitional_flat_curve(self):
        assert _classify(t10y2y=0.1, vix=14.0) == "TRANSITIONAL"

    def test_transitional_elevated_vix(self):
        assert _classify(t10y2y=0.5, vix=20.0) == "TRANSITIONAL"

    def test_risk_on_all_clear(self):
        assert _classify(t10y2y=0.5, vix=14.0) == "RISK_ON"

    def test_none_vix_uses_yield_curve_only(self):
        assert _classify(t10y2y=-0.5, vix=None) == "RISK_OFF"

    def test_none_t10y2y_uses_vix_only(self):
        assert _classify(t10y2y=None, vix=40.0) == "CRISIS"


class TestPositionSizeMultiplier:
    def test_risk_on_is_1(self):
        assert regime_position_size_multiplier("RISK_ON") == 1.0

    def test_crisis_is_0(self):
        assert regime_position_size_multiplier("CRISIS") == 0.0

    def test_risk_off_is_half(self):
        assert regime_position_size_multiplier("RISK_OFF") == 0.5

    def test_transitional_is_0_7(self):
        assert regime_position_size_multiplier("TRANSITIONAL") == 0.7


# ── technical signals ─────────────────────────────────────────────────────────

class TestRsi:
    def test_all_up_days_rsi_near_100(self):
        """14 consecutive $1 gains from $100 → avg_loss=0 → RSI=100."""
        prices = pd.Series([100.0 + i for i in range(30)])
        assert _rsi(prices) == pytest.approx(100.0, abs=0.01)

    def test_all_down_days_rsi_near_0(self):
        """14 consecutive $1 losses → avg_gain=0 → RSI=0."""
        prices = pd.Series([100.0 - i for i in range(30)])
        assert _rsi(prices) == pytest.approx(0.0, abs=0.01)

    def test_alternating_equal_rsi_near_50(self):
        """Equal up/down moves → avg_gain = avg_loss → RSI ≈ 50."""
        prices = pd.Series([100.0 + (1 if i % 2 == 0 else -1) for i in range(40)])
        assert _rsi(prices) == pytest.approx(50.0, abs=2.0)

    def test_insufficient_data_returns_nan(self):
        prices = pd.Series([100.0, 101.0, 102.0])  # fewer than 14 periods
        assert math.isnan(_rsi(prices))


class TestComputeTechnicalSignals:
    def test_returns_dataframe(self, test_db):
        df = compute_technical_signals(test_db, tickers=["AAPL"])
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 1

    def test_expected_columns_present(self, test_db):
        df = compute_technical_signals(test_db, tickers=["AAPL"])
        for col in ["ticker", "close", "ma50", "ma200", "rsi", "atr", "macd_hist"]:
            assert col in df.columns, f"Missing column: {col}"

    def test_aapl_rsi_elevated_in_uptrend(self, test_db):
        """AAPL trends up consistently — RSI should be well above 50."""
        df = compute_technical_signals(test_db, tickers=["AAPL"])
        assert df.iloc[0]["rsi"] > 60

    def test_msft_rsi_lower_than_aapl_rsi(self, test_db):
        """MSFT trends down, AAPL trends up — MSFT RSI must be lower than AAPL RSI."""
        aapl_rsi = compute_technical_signals(test_db, tickers=["AAPL"]).iloc[0]["rsi"]
        msft_rsi = compute_technical_signals(test_db, tickers=["MSFT"]).iloc[0]["rsi"]
        assert msft_rsi < aapl_rsi

    def test_aapl_ma50_above_ma200_in_uptrend(self, test_db):
        """AAPL uptrend → MA50 should be above MA200 (golden cross)."""
        df = compute_technical_signals(test_db, tickers=["AAPL"])
        assert df.iloc[0]["ma50_above_ma200"] == True  # noqa: E712 — avoids np.bool_ identity issue

    def test_msft_ma50_below_ma200_in_downtrend(self, test_db):
        """MSFT downtrend → MA50 should be below MA200 (death cross)."""
        df = compute_technical_signals(test_db, tickers=["MSFT"])
        assert df.iloc[0]["ma50_above_ma200"] == False  # noqa: E712

    def test_atr_is_positive(self, test_db):
        df = compute_technical_signals(test_db, tickers=["AAPL"])
        assert df.iloc[0]["atr"] > 0

    def test_multiple_tickers_returns_one_row_each(self, test_db):
        df = compute_technical_signals(test_db, tickers=["AAPL", "MSFT", "JPM"])
        assert len(df) == 3
        assert set(df["ticker"]) == {"AAPL", "MSFT", "JPM"}

    def test_empty_ticker_list_returns_empty(self, test_db):
        df = compute_technical_signals(test_db, tickers=["FAKE_TICKER"])
        assert len(df) == 0


class TestStopPrice:
    def test_stop_below_entry(self):
        assert stop_price(entry=100.0, atr=2.0) == pytest.approx(97.5)

    def test_custom_multiplier(self):
        assert stop_price(entry=100.0, atr=2.0, multiplier=2.0) == pytest.approx(96.0)


class TestSuggestedPositionSize:
    def test_known_position_size(self):
        """Account $10,000, 1% risk, entry=$100, ATR=$2 → stop=$97.50 → risk/share=$2.50
        → size = ($10,000 × 0.01) / $2.50 = 40 shares."""
        assert suggested_position_size(
            account_value=10_000, entry=100.0, atr=2.0, account_risk_pct=0.01
        ) == 40

    def test_higher_atr_smaller_position(self):
        size_low_atr  = suggested_position_size(10_000, 100.0, 1.0)
        size_high_atr = suggested_position_size(10_000, 100.0, 4.0)
        assert size_low_atr > size_high_atr


# ── factor signals ────────────────────────────────────────────────────────────

class TestMomentumScores:
    def test_returns_dataframe(self, test_db):
        df = compute_momentum_scores(test_db)
        assert isinstance(df, pd.DataFrame)
        assert not df.empty

    def test_expected_columns(self, test_db):
        df = compute_momentum_scores(test_db)
        for col in ["ticker", "momentum_return", "momentum_rank", "as_of_date"]:
            assert col in df.columns

    def test_aapl_positive_momentum(self, test_db):
        """AAPL compounds up 0.15%/day → momentum return must be positive."""
        df = compute_momentum_scores(test_db)
        aapl = df[df["ticker"] == "AAPL"].iloc[0]
        assert aapl["momentum_return"] > 0

    def test_msft_negative_momentum(self, test_db):
        """MSFT compounds down 0.07%/day → momentum return must be negative."""
        df = compute_momentum_scores(test_db)
        msft = df[df["ticker"] == "MSFT"].iloc[0]
        assert msft["momentum_return"] < 0

    def test_jpm_near_zero_momentum(self, test_db):
        """JPM is flat → momentum return ≈ 0."""
        df = compute_momentum_scores(test_db)
        jpm = df[df["ticker"] == "JPM"].iloc[0]
        assert abs(jpm["momentum_return"]) < 0.001

    def test_aapl_ranks_above_msft(self, test_db):
        """AAPL has better momentum than MSFT — must rank higher."""
        df = compute_momentum_scores(test_db)
        aapl_rank = df[df["ticker"] == "AAPL"].iloc[0]["momentum_rank"]
        msft_rank = df[df["ticker"] == "MSFT"].iloc[0]["momentum_rank"]
        assert aapl_rank > msft_rank

    def test_momentum_ranks_between_0_and_100(self, test_db):
        df = compute_momentum_scores(test_db)
        assert df["momentum_rank"].between(0, 100).all()

    def test_returns_empty_if_insufficient_data(self, tmp_path):
        from etl.schema import initialize_schema
        db = str(tmp_path / "short.db")
        initialize_schema(db)
        df = compute_momentum_scores(db)
        assert df.empty


class TestLowVolScores:
    def test_returns_dataframe(self, test_db):
        df = compute_lowvol_scores(test_db)
        assert isinstance(df, pd.DataFrame)
        assert not df.empty

    def test_jpm_lower_vol_than_aapl(self, test_db):
        """JPM is flat (realized vol ≈ 0), AAPL trends up (non-zero vol)."""
        df = compute_lowvol_scores(test_db)
        jpm_vol  = df[df["ticker"] == "JPM"].iloc[0]["realized_vol"]
        aapl_vol = df[df["ticker"] == "AAPL"].iloc[0]["realized_vol"]
        assert jpm_vol < aapl_vol

    def test_jpm_highest_lowvol_rank(self, test_db):
        """JPM (flattest) should have the highest low-vol rank."""
        df = compute_lowvol_scores(test_db)
        top_ticker = df.sort_values("lowvol_rank", ascending=False).iloc[0]["ticker"]
        assert top_ticker == "JPM"

    def test_lowvol_ranks_between_0_and_100(self, test_db):
        df = compute_lowvol_scores(test_db)
        assert df["lowvol_rank"].between(0, 100).all()

    def test_realized_vol_is_positive(self, test_db):
        df = compute_lowvol_scores(test_db)
        assert (df["realized_vol"] >= 0).all()


class TestCombinedFactorScore:
    def test_returns_dataframe(self, test_db):
        df = compute_combined_factor_score(test_db)
        assert isinstance(df, pd.DataFrame)
        assert not df.empty

    def test_expected_columns(self, test_db):
        df = compute_combined_factor_score(test_db)
        for col in ["ticker", "momentum_rank", "lowvol_rank", "composite_score"]:
            assert col in df.columns

    def test_composite_score_between_0_and_100(self, test_db):
        df = compute_combined_factor_score(test_db)
        assert df["composite_score"].between(0, 100).all()

    def test_aapl_scores_above_msft(self, test_db):
        """AAPL has strong upward momentum — should outscore MSFT overall."""
        df = compute_combined_factor_score(test_db)
        aapl_score = df[df["ticker"] == "AAPL"].iloc[0]["composite_score"]
        msft_score = df[df["ticker"] == "MSFT"].iloc[0]["composite_score"]
        assert aapl_score > msft_score
