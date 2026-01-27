"""
ModeB strategy tests.
"""
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from src.strategy.base import Signal, BarData
from src.strategy.strategies.dual_mode import ModeBStrategy, DualModeConfig, TradingMode


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


class TestModeBStrategySignals:
    @pytest.fixture
    def strategy(self):
        config = DualModeConfig(enable_decision_logging=False)
        with patch('src.strategy.strategies.dual_mode.TRIPLE_BARRIER_AVAILABLE', False):
            strategy = ModeBStrategy(config)
        return strategy

    def test_mode_b_uses_process_mode_b(self, strategy):
        strategy._process_mode_b = Mock(return_value=Signal.BUY)
        bar = create_bar()
        signal = strategy.generate_signal(bar)
        assert signal == Signal.BUY
        assert strategy.current_mode == TradingMode.MODE_B
        strategy._process_mode_b.assert_called_once()
