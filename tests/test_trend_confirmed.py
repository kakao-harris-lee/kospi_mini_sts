"""
Trend Confirmed strategy tests.
"""
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from src.strategy.base import Signal, BarData
from src.strategy.strategies.trend_confirmed import (
    TrendConfirmedStrategy,
    TrendConfirmedConfig,
    TradingMode,
)


def create_bar(
    close: float = 350.0,
    high: float = 351.0,
    low: float = 349.0,
    open_price: float = 350.0,
    volume: float = 1000.0,
    up_prob: float = 0.5,
    up_prob_h1: float = 0.5,
    up_prob_h3: float = 0.5,
    up_prob_h5: float = 0.5,
    up_prob_h10: float = 0.5,
    dt: datetime = None,
) -> BarData:
    if dt is None:
        dt = datetime(2025, 1, 2, 10, 10)
    return BarData(
        datetime=dt,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        up_prob=up_prob,
        up_prob_h1=up_prob_h1,
        up_prob_h3=up_prob_h3,
        up_prob_h5=up_prob_h5,
        up_prob_h10=up_prob_h10,
    )


class TestTrendConfirmedStrategySignals:
    @pytest.fixture
    def strategy(self):
        config = TrendConfirmedConfig(enable_decision_logging=False)
        with patch('src.strategy.strategies.trend_confirmed.TRIPLE_BARRIER_AVAILABLE', False):
            strategy = TrendConfirmedStrategy(config)
        return strategy

    def test_trend_confirmed_uses_process_trend_confirmed(self, strategy):
        strategy._process_trend_confirmed = Mock(return_value=Signal.BUY)
        bar = create_bar()
        signal = strategy.generate_signal(bar)
        assert signal == Signal.BUY
        assert strategy.current_mode == TradingMode.TREND_CONFIRMED
        strategy._process_trend_confirmed.assert_called_once()
