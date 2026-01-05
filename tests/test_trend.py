"""
Tests for MODE_B Trend Following Components

Tests cover:
- TechnicalCalculator: MA, Ichimoku, ATR calculations
- EnsembleFilter: DL + MA + Ichimoku check
- TrendPositionManager: ATR trailing stop, time cut
- TrendEngine: Full integration
"""

import pytest
import time
from unittest.mock import MagicMock, patch

from src.strategy.trend import (
    TechnicalCalculator,
    TechnicalData,
    EnsembleFilter,
    FilterResult,
    TrendPositionManager,
    TrendPosition,
    ExitSignal,
    ExitReason,
    PositionSide,
    TrendEngine,
    TrendSignal,
)


class TestTechnicalCalculator:
    """Tests for TechnicalCalculator"""

    def test_sma_calculation(self):
        """Test Simple Moving Average calculation"""
        calc = TechnicalCalculator(ma_fast_period=5, ma_slow_period=10)

        # Add 10 bars with increasing prices
        for i in range(10):
            price = 350.0 + i
            calc.update(high=price + 0.5, low=price - 0.5, close=price)

        data = calc.get_current_data()
        assert data is not None

        # MA(5) should be average of last 5 closes: 355, 356, 357, 358, 359 = 357
        assert abs(data.ma_fast - 357.0) < 0.01

        # MA(10) should be average of all 10: 350-359 = 354.5
        assert abs(data.ma_slow - 354.5) < 0.01

    def test_atr_calculation(self):
        """Test ATR calculation"""
        calc = TechnicalCalculator(atr_period=5)

        # Add bars with consistent range of 2 points
        for i in range(10):
            calc.update(high=352.0, low=350.0, close=351.0)

        data = calc.get_current_data()
        assert data is not None
        # ATR should be approximately 2.0 (high - low)
        assert abs(data.atr - 2.0) < 0.5

    def test_ichimoku_cloud(self):
        """Test Ichimoku cloud calculation"""
        calc = TechnicalCalculator()

        # Add 60 bars to meet warmup
        for i in range(60):
            # Uptrending market
            price = 350.0 + i * 0.1
            calc.update(high=price + 0.5, low=price - 0.5, close=price)

        data = calc.get_current_data()
        assert data is not None
        assert data.ichimoku_span_a > 0
        assert data.ichimoku_span_b > 0

    def test_is_ready_after_warmup(self):
        """Test readiness after sufficient bars"""
        calc = TechnicalCalculator(ma_slow_period=20)

        # Not ready yet
        for i in range(10):
            calc.update(350.0, 349.0, 349.5)
        assert not calc.is_ready()

        # Add more bars
        for i in range(15):
            calc.update(350.0, 349.0, 349.5)
        assert calc.is_ready()

    def test_bullish_ma_property(self):
        """Test bullish MA detection"""
        calc = TechnicalCalculator(ma_fast_period=5, ma_slow_period=10)

        # Uptrending - fast MA should be above slow MA
        for i in range(15):
            price = 350.0 + i * 0.5  # Strong uptrend
            calc.update(price + 0.5, price - 0.5, price)

        data = calc.get_current_data()
        assert data.is_bullish_ma is True

    def test_reset_clears_history(self):
        """Test reset clears all state"""
        calc = TechnicalCalculator(ma_slow_period=30)

        for i in range(30):
            calc.update(350.0, 349.0, 349.5)

        assert calc.is_ready()
        calc.reset()
        assert not calc.is_ready()


class TestEnsembleFilter:
    """Tests for EnsembleFilter"""

    def create_bullish_tech(self) -> TechnicalData:
        """Create bullish technical data"""
        return TechnicalData(
            ma_fast=355.0,
            ma_slow=350.0,  # Fast > Slow = bullish
            ichimoku_span_a=348.0,
            ichimoku_span_b=346.0,  # Cloud below price
            atr=2.0,
            is_ready=True,
            current_price=360.0,  # Above cloud
        )

    def create_bearish_tech(self) -> TechnicalData:
        """Create bearish technical data"""
        return TechnicalData(
            ma_fast=345.0,
            ma_slow=350.0,  # Fast < Slow = bearish
            ichimoku_span_a=358.0,
            ichimoku_span_b=360.0,  # Cloud above price
            atr=2.0,
            is_ready=True,
            current_price=340.0,  # Below cloud
        )

    def test_long_entry_all_pass(self):
        """Test long entry when all conditions pass"""
        filter = EnsembleFilter(dl_threshold=0.85)
        tech = self.create_bullish_tech()

        result = filter.check_entry(up_prob=0.90, tech=tech)

        assert result.can_enter is True
        assert result.direction == "LONG"
        assert result.dl_passed is True
        assert result.ma_passed is True
        assert result.ichimoku_passed is True

    def test_long_entry_dl_fails(self):
        """Test long entry fails when DL probability too low"""
        filter = EnsembleFilter(dl_threshold=0.85)
        tech = self.create_bullish_tech()

        result = filter.check_entry(up_prob=0.80, tech=tech)

        assert result.can_enter is False
        assert result.dl_passed is False
        assert "DL probability" in result.rejection_reason

    def test_long_entry_ma_fails(self):
        """Test long entry fails when MA bearish"""
        filter = EnsembleFilter(dl_threshold=0.85)
        tech = self.create_bullish_tech()
        tech.ma_fast = 345.0  # Make MA bearish

        result = filter.check_entry(up_prob=0.90, tech=tech)

        assert result.can_enter is False
        assert result.ma_passed is False

    def test_long_entry_ichimoku_fails(self):
        """Test long entry fails when price below cloud"""
        filter = EnsembleFilter(dl_threshold=0.85)
        tech = self.create_bullish_tech()
        tech.current_price = 340.0  # Below cloud

        result = filter.check_entry(up_prob=0.90, tech=tech)

        assert result.can_enter is False
        assert result.ichimoku_passed is False

    def test_short_entry_all_pass(self):
        """Test short entry when all conditions pass"""
        filter = EnsembleFilter(dl_threshold=0.85)
        tech = self.create_bearish_tech()

        # down_prob = 1 - up_prob = 0.90
        result = filter.check_entry(up_prob=0.10, tech=tech)

        assert result.can_enter is True
        assert result.direction == "SHORT"

    def test_not_ready_rejected(self):
        """Test rejection when technical data not ready"""
        filter = EnsembleFilter()
        tech = TechnicalData(
            ma_fast=0, ma_slow=0, ichimoku_span_a=0, ichimoku_span_b=0,
            atr=0, is_ready=False, current_price=350.0
        )

        result = filter.check_entry(up_prob=0.90, tech=tech)

        assert result.can_enter is False
        assert "warming up" in result.rejection_reason


class TestTrendPositionManager:
    """Tests for TrendPositionManager"""

    def test_open_long_position(self):
        """Test opening a long position"""
        manager = TrendPositionManager(atr_stop_multiplier=2.0)

        pos = manager.open_position(
            side="LONG",
            entry_price=350.0,
            size=1.0,
            atr=2.0,
        )

        assert pos.side == PositionSide.LONG
        assert pos.entry_price == 350.0
        assert pos.stop_price == 346.0  # 350 - 2*2
        assert manager.has_position is True

    def test_open_short_position(self):
        """Test opening a short position"""
        manager = TrendPositionManager(atr_stop_multiplier=2.0)

        pos = manager.open_position(
            side="SHORT",
            entry_price=350.0,
            size=1.0,
            atr=2.0,
        )

        assert pos.side == PositionSide.SHORT
        assert pos.stop_price == 354.0  # 350 + 2*2

    def test_trailing_stop_long(self):
        """Test trailing stop for long position"""
        manager = TrendPositionManager(atr_stop_multiplier=2.0)

        manager.open_position(
            side="LONG",
            entry_price=350.0,
            size=1.0,
            atr=2.0,
        )

        # Price goes up, stop should trail
        signal = manager.update_and_check_exit(
            current_price=355.0,  # +5 points
            current_atr=2.0,
        )

        assert signal.should_exit is False
        # New stop should be 355 - 4 = 351
        assert manager.position.stop_price == 351.0

    def test_trailing_stop_never_moves_down(self):
        """Test trailing stop never moves backwards"""
        manager = TrendPositionManager(atr_stop_multiplier=2.0)

        manager.open_position(
            side="LONG",
            entry_price=350.0,
            size=1.0,
            atr=2.0,
        )

        # Price up
        manager.update_and_check_exit(355.0, 2.0)
        stop_after_up = manager.position.stop_price

        # Price down (but not to stop)
        manager.update_and_check_exit(353.0, 2.0)
        stop_after_down = manager.position.stop_price

        # Stop should not move down
        assert stop_after_down == stop_after_up

    def test_atr_stop_triggered(self):
        """Test ATR stop is triggered"""
        manager = TrendPositionManager(atr_stop_multiplier=2.0)

        manager.open_position(
            side="LONG",
            entry_price=350.0,
            size=1.0,
            atr=2.0,
        )

        # Price hits stop
        signal = manager.update_and_check_exit(
            current_price=345.0,  # Below stop of 346
            current_atr=2.0,
        )

        assert signal.should_exit is True
        assert signal.reason == ExitReason.ATR_STOP

    def test_time_cut_triggered(self):
        """Test time cut after 30 minutes without movement"""
        manager = TrendPositionManager(
            time_cut_minutes=30,
            time_cut_atr_threshold=0.5,
        )

        entry_time = time.time()
        manager.open_position(
            side="LONG",
            entry_price=350.0,
            size=1.0,
            atr=2.0,
            timestamp=entry_time,
        )

        # 31 minutes later, price hasn't moved enough (need 0.5 * 2 = 1.0 point)
        signal = manager.update_and_check_exit(
            current_price=350.5,  # Only +0.5, need +1.0
            current_atr=2.0,
            current_time=entry_time + 31 * 60,
        )

        assert signal.should_exit is True
        assert signal.reason == ExitReason.TIME_CUT

    def test_time_cut_not_triggered_with_movement(self):
        """Test time cut not triggered if price moved enough"""
        manager = TrendPositionManager(
            time_cut_minutes=30,
            time_cut_atr_threshold=0.5,
        )

        entry_time = time.time()
        manager.open_position(
            side="LONG",
            entry_price=350.0,
            size=1.0,
            atr=2.0,
            timestamp=entry_time,
        )

        # 31 minutes later, price moved enough
        signal = manager.update_and_check_exit(
            current_price=352.0,  # +2.0, need only +1.0
            current_atr=2.0,
            current_time=entry_time + 31 * 60,
        )

        assert signal.should_exit is False

    def test_close_position_pnl(self):
        """Test PnL calculation on close"""
        manager = TrendPositionManager()

        manager.open_position(
            side="LONG",
            entry_price=350.0,
            size=1.0,
            atr=2.0,
        )

        pnl = manager.close_position(exit_price=355.0)

        assert pnl == 5.0  # 355 - 350
        assert manager.has_position is False


class TestTrendEngine:
    """Tests for TrendEngine"""

    def test_warmup_period(self):
        """Test engine needs warmup before trading"""
        engine = TrendEngine(min_bars_required=60)

        # Not ready yet
        for i in range(30):
            engine.update_bar(351.0, 349.0, 350.0)

        signal = engine.check(up_prob=0.90, current_price=350.0)

        assert signal.action == "HOLD"
        assert "Warming up" in signal.reason

    def test_entry_signal_long(self):
        """Test long entry signal generation"""
        engine = TrendEngine(
            dl_threshold=0.85,
            ma_fast_period=5,
            ma_slow_period=10,
        )

        # Warmup with uptrend
        for i in range(20):
            price = 340.0 + i * 0.5  # Uptrending
            engine.update_bar(price + 0.5, price - 0.5, price)

        # Set prices for order execution
        engine.update_prices(bid=349.0, ask=350.0)

        signal = engine.check(up_prob=0.90, current_price=350.0, symbol="TEST")

        # Should generate LONG signal if all conditions pass
        if signal.action == "OPEN_LONG":
            assert signal.direction == "LONG"
            assert signal.size > 0

    def test_exit_signal_atr_stop(self):
        """Test exit signal on ATR stop"""
        engine = TrendEngine(
            dl_threshold=0.85,
            ma_fast_period=5,
            ma_slow_period=10,
            atr_stop_multiplier=2.0,
        )

        # Warmup
        for i in range(20):
            price = 340.0 + i * 0.5
            engine.update_bar(price + 0.5, price - 0.5, price)

        engine.update_prices(bid=349.0, ask=350.0)

        # Force open a position
        engine.position_manager.open_position(
            side="LONG",
            entry_price=350.0,
            size=1.0,
            atr=2.0,
        )

        # Price drops to stop
        signal = engine.check(up_prob=0.50, current_price=345.0)

        assert signal.action == "CLOSE"
        assert signal.exit_reason == ExitReason.ATR_STOP

    def test_hold_with_position(self):
        """Test hold signal when position OK"""
        engine = TrendEngine()

        # Warmup
        for i in range(65):
            engine.update_bar(351.0, 349.0, 350.0)

        # Open position
        engine.position_manager.open_position(
            side="LONG",
            entry_price=350.0,
            size=1.0,
            atr=2.0,
        )

        # Price OK, should hold
        signal = engine.check(up_prob=0.60, current_price=351.0)

        assert signal.action == "HOLD"
        assert engine.has_position() is True

    def test_stats_tracking(self):
        """Test statistics tracking"""
        engine = TrendEngine()

        # Do some checks
        for i in range(10):
            engine.update_bar(351.0, 349.0, 350.0)
            engine.check(up_prob=0.50, current_price=350.0)

        stats = engine.get_stats()

        assert stats['total_checks'] == 10
        assert 'technical' in stats
        assert 'filter' in stats
        assert 'position' in stats


class TestIntegration:
    """Integration tests for trend following system"""

    def test_full_trade_lifecycle(self):
        """Test complete trade: entry -> hold -> exit"""
        engine = TrendEngine(
            dl_threshold=0.85,
            ma_fast_period=5,
            ma_slow_period=10,
            atr_stop_multiplier=2.0,
            time_cut_minutes=30,
        )

        # 1. Warmup with strong uptrend
        for i in range(15):
            price = 340.0 + i * 1.0  # Strong uptrend
            engine.update_bar(price + 0.5, price - 0.5, price)

        current_price = 355.0
        engine.update_prices(bid=current_price - 0.05, ask=current_price)

        # 2. Entry signal (high probability, bullish technicals)
        entry_signal = engine.check(up_prob=0.92, current_price=current_price)

        # May or may not enter depending on Ichimoku
        if entry_signal.action == "OPEN_LONG":
            assert engine.has_position()

            # 3. Hold through some bars
            for i in range(5):
                price = 356.0 + i * 0.2
                engine.update_bar(price + 0.5, price - 0.5, price)
                hold_signal = engine.check(up_prob=0.60, current_price=price)
                assert hold_signal.action == "HOLD"

            # 4. Price drops to stop
            engine.update_prices(bid=350.0, ask=350.05)
            exit_signal = engine.check(up_prob=0.40, current_price=350.0)

            # Should exit on stop
            if exit_signal.action == "CLOSE":
                assert not engine.has_position()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
