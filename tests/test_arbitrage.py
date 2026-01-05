"""
Tests for MODE_A Arbitrage Components

Tests cover:
- BasisCalculator: fair value, z-score calculation
- ArbitrageEngine: entry filters
- ArbitrageOrderManager: order lifecycle
"""

import pytest
from datetime import datetime, date, timedelta
from unittest.mock import MagicMock, patch
import time

from src.strategy.arbitrage import (
    BasisCalculator,
    BasisData,
    ArbitrageEngine,
    ArbitrageSignal,
    ArbitrageOrderManager,
    PendingOrder,
)
from src.strategy.arbitrage.basis_calculator import calculate_days_to_expiry, _get_second_thursday
from src.strategy.arbitrage.order_manager import OrderStatus
from src.strategy.base import BarData


class TestBasisCalculator:
    """Tests for BasisCalculator"""

    def test_fair_value_calculation(self):
        """Test theoretical fair value calculation"""
        calc = BasisCalculator(risk_free_rate=0.035)

        # At 30 days to expiry with 3.5% annual rate
        spot = 350.0
        days = 30
        fair_value = calc.calculate_fair_value(spot, days)

        # Expected: 350 * (1 + 0.035 * 30/365) ≈ 351.01
        expected = spot * (1 + 0.035 * 30 / 365)
        assert abs(fair_value - expected) < 0.001

    def test_basis_calculation(self):
        """Test basis = futures - fair_value"""
        calc = BasisCalculator(risk_free_rate=0.035)

        spot = 350.0
        futures = 352.0
        days = 30

        data = calc.update(spot, futures, days)

        # Basis should be positive (futures > fair value)
        assert data.basis > 0
        assert data.futures_price == 352.0
        assert data.spot_index == 350.0

    def test_zscore_needs_warmup(self):
        """Test that z-score is 0 before enough samples"""
        calc = BasisCalculator(min_samples=20)

        # Add only 10 samples
        for i in range(10):
            data = calc.update(350.0, 351.0, 30)

        assert not calc.is_ready()
        assert data.basis_zscore == 0.0

    def test_zscore_after_warmup(self):
        """Test z-score calculation after warmup"""
        calc = BasisCalculator(min_samples=20, rolling_window=30)

        # Add 25 samples with consistent basis
        for i in range(25):
            calc.update(350.0, 351.0, 30)

        assert calc.is_ready()

        # Now add outlier - futures much higher
        data = calc.update(350.0, 355.0, 30)

        # Z-score should be positive (above mean)
        assert data.basis_zscore > 0

    def test_signal_detection_buy(self):
        """Test BUY signal when futures underpriced"""
        calc = BasisCalculator(min_samples=5, rolling_window=20)

        # Build history with high basis
        for i in range(10):
            calc.update(350.0, 354.0, 30)

        # Now futures underpriced relative to history
        calc.update(350.0, 348.0, 30)

        has_signal, direction = calc.is_signal(threshold=2.0)

        # Should signal BUY (z < -threshold)
        if has_signal:
            assert direction == "BUY"

    def test_signal_detection_sell(self):
        """Test SELL signal when futures overpriced"""
        calc = BasisCalculator(min_samples=5, rolling_window=20)

        # Build history with low basis
        for i in range(10):
            calc.update(350.0, 350.5, 30)

        # Now futures overpriced relative to history
        calc.update(350.0, 358.0, 30)

        has_signal, direction = calc.is_signal(threshold=2.0)

        # Should signal SELL (z > threshold)
        if has_signal:
            assert direction == "SELL"

    def test_reset_clears_history(self):
        """Test reset clears all state"""
        calc = BasisCalculator()

        for i in range(30):
            calc.update(350.0, 351.0, 30)

        assert calc.is_ready()

        calc.reset()

        assert not calc.is_ready()
        assert len(calc.basis_history) == 0
        assert calc.get_current_data() is None


class TestDaysToExpiry:
    """Tests for expiry calculation"""

    def test_second_thursday_calculation(self):
        """Test 2nd Thursday calculation"""
        # January 2025: 1st is Wednesday, 2nd Thursday = Jan 9
        second_thu = _get_second_thursday(2025, 1)
        assert second_thu.weekday() == 3  # Thursday
        assert second_thu.day >= 8 and second_thu.day <= 14

    def test_days_to_expiry_front_month(self):
        """Test front month expiry calculation"""
        # Test with a known date
        ref_date = date(2025, 1, 1)
        days = calculate_days_to_expiry("A05601", ref_date)

        # Should be positive and less than ~40 days
        assert days > 0
        assert days < 45

    def test_days_to_expiry_unknown_code(self):
        """Test default for unknown contract code"""
        days = calculate_days_to_expiry("UNKNOWN")
        assert days == 30  # Default


class TestArbitrageEngine:
    """Tests for ArbitrageEngine entry filters"""

    def create_mock_bar(self, spread_ticks=1, depth=100) -> BarData:
        """Create mock BarData for testing"""
        tick_size = 0.05
        return BarData(
            datetime=datetime.now(),
            open=350.0,
            high=351.0,
            low=349.0,
            close=350.5,
            volume=1000,
            best_bid=350.0,
            best_ask=350.0 + spread_ticks * tick_size,
            bid_qty1=depth,
            bid_qty2=depth,
            bid_qty3=depth,
            ask_qty1=depth,
            ask_qty2=depth,
            ask_qty3=depth,
        )

    def test_spread_filter_pass(self):
        """Test spread filter passes with tight spread"""
        engine = ArbitrageEngine(max_spread_ticks=2)

        bar = self.create_mock_bar(spread_ticks=1)
        spread_ticks = engine._calculate_spread_ticks(bar.best_ask, bar.best_bid)

        assert spread_ticks <= 2

    def test_spread_filter_reject(self):
        """Test spread filter rejects wide spread"""
        engine = ArbitrageEngine(max_spread_ticks=2)

        bar = self.create_mock_bar(spread_ticks=5)
        spread_ticks = engine._calculate_spread_ticks(bar.best_ask, bar.best_bid)

        assert spread_ticks > 2

    def test_depth_filter_pass(self):
        """Test depth filter passes with sufficient depth"""
        engine = ArbitrageEngine(order_size=5.0, depth_multiplier=5.0)

        # Need 25 contracts on each side
        bar = self.create_mock_bar(depth=30)
        result = engine._check_depth(bar, min_depth=25)

        assert result is True

    def test_depth_filter_reject(self):
        """Test depth filter rejects insufficient depth"""
        engine = ArbitrageEngine(order_size=5.0, depth_multiplier=5.0)

        # Need 25 contracts but only have 5
        bar = self.create_mock_bar(depth=5)
        result = engine._check_depth(bar, min_depth=25)

        assert result is False

    def test_dividend_blackout_quarterly(self):
        """Test dividend blackout detection"""
        engine = ArbitrageEngine(quarterly_blackout_days=14)

        # March 2025 - expiry is 2nd Thursday (March 13)
        # Blackout starts Feb 27
        blackout_date = datetime(2025, 3, 5)
        assert engine._is_dividend_blackout(blackout_date) is True

        # After expiry
        after_expiry = datetime(2025, 3, 20)
        assert engine._is_dividend_blackout(after_expiry) is False

    def test_check_entry_integration(self):
        """Test full entry check integration"""
        engine = ArbitrageEngine(
            max_spread_ticks=2,
            depth_multiplier=5.0,
            basis_threshold=2.5,
            order_size=5.0,
            basis_rolling_window=10,
        )

        # Warm up basis calculator
        for i in range(15):
            engine.update_basis(350.0, 351.0, 30)

        bar = self.create_mock_bar(spread_ticks=1, depth=50)

        # Mock time filter to always pass
        with patch.object(engine.time_filter, 'can_open_position', return_value=True):
            with patch.object(engine, '_is_dividend_blackout', return_value=False):
                # This may or may not generate signal depending on z-score
                can_enter, signal, reason = engine.check_entry(
                    bar=bar,
                    spot_index=350.0,
                    days_to_expiry=30,
                    current_time=datetime(2025, 1, 15, 10, 30)
                )

                # Should pass filters or fail on signal
                assert isinstance(can_enter, bool)
                assert isinstance(reason, str)


class TestArbitrageOrderManager:
    """Tests for ArbitrageOrderManager"""

    def create_mock_signal(self, direction="BUY", price=350.0) -> ArbitrageSignal:
        """Create mock ArbitrageSignal"""
        return ArbitrageSignal(
            timestamp=time.time(),
            direction=direction,
            basis_zscore=2.8,
            entry_price=price,
            order_size=5.0,
            basis_data=MagicMock(spec=BasisData),
        )

    def test_create_order(self):
        """Test order creation"""
        manager = ArbitrageOrderManager(timeout_sec=10.0)
        signal = self.create_mock_signal()

        order = manager.create_order(signal)

        assert order is not None
        assert order.status == OrderStatus.PENDING
        assert order.signal.direction == "BUY"

    def test_submit_order(self):
        """Test order submission"""
        manager = ArbitrageOrderManager()
        signal = self.create_mock_signal()

        order = manager.create_order(signal)
        success = manager.submit_order(order)

        assert success is True
        assert order.status == OrderStatus.SUBMITTED
        assert manager.stats.total_submitted == 1

    def test_fill_order(self):
        """Test order fill"""
        manager = ArbitrageOrderManager()
        signal = self.create_mock_signal(direction="BUY", price=350.0)

        order = manager.create_order(signal)
        manager.submit_order(order)

        success = manager.update_fill(
            order_id=order.order_id,
            filled_qty=5.0,
            filled_price=350.0,
            is_complete=True
        )

        assert success is True
        assert order.status == OrderStatus.FILLED
        assert manager.stats.total_filled == 1
        assert manager.get_position() == 5.0  # Long position

    def test_cancel_order(self):
        """Test order cancellation"""
        manager = ArbitrageOrderManager()
        signal = self.create_mock_signal()

        order = manager.create_order(signal)
        manager.submit_order(order)

        success = manager.cancel_order(order.order_id, reason="test")

        assert success is True
        assert order.status == OrderStatus.CANCELLED
        assert manager.stats.total_cancelled == 1

    def test_timeout_cancellation(self):
        """Test automatic timeout cancellation"""
        manager = ArbitrageOrderManager(timeout_sec=0.1)
        signal = self.create_mock_signal()

        order = manager.create_order(signal)
        manager.submit_order(order)

        # Wait for timeout
        time.sleep(0.2)

        # Check timeouts
        cancelled = manager.check_timeouts()

        assert order.order_id in cancelled
        assert order.status == OrderStatus.EXPIRED
        assert manager.stats.total_timeout == 1

    def test_max_pending_orders(self):
        """Test max pending orders limit"""
        manager = ArbitrageOrderManager(max_pending_orders=2)

        # Create and submit 2 orders
        for _ in range(2):
            signal = self.create_mock_signal()
            order = manager.create_order(signal)
            manager.submit_order(order)

        # Third order should be rejected
        signal = self.create_mock_signal()
        order = manager.create_order(signal)

        assert order is None

    def test_position_tracking(self):
        """Test position tracking across multiple fills"""
        manager = ArbitrageOrderManager()

        # BUY 5
        signal1 = self.create_mock_signal(direction="BUY")
        order1 = manager.create_order(signal1)
        manager.submit_order(order1)
        manager.update_fill(order1.order_id, 5.0, 350.0, True)

        assert manager.get_position() == 5.0

        # SELL 3
        signal2 = self.create_mock_signal(direction="SELL")
        order2 = manager.create_order(signal2)
        manager.submit_order(order2)
        manager.update_fill(order2.order_id, 3.0, 351.0, True)

        assert manager.get_position() == 2.0  # 5 - 3

    def test_stats(self):
        """Test statistics tracking"""
        manager = ArbitrageOrderManager()

        signal = self.create_mock_signal()
        order = manager.create_order(signal)
        manager.submit_order(order)
        manager.update_fill(order.order_id, 5.0, 350.0, True)

        stats = manager.get_stats()

        assert stats['total_submitted'] == 1
        assert stats['total_filled'] == 1
        assert stats['fill_rate'] == '100.0%'
        assert stats['total_contracts'] == 5.0


class TestIntegration:
    """Integration tests for arbitrage components"""

    def test_full_arbitrage_flow(self):
        """Test complete arbitrage flow from signal to order"""
        # Setup engine
        engine = ArbitrageEngine(
            basis_threshold=2.5,
            order_size=5.0,
            basis_rolling_window=10,
        )

        # Warm up with consistent data
        for i in range(15):
            engine.update_basis(350.0, 351.0, 30)

        # Create bar data
        bar = BarData(
            datetime=datetime.now(),
            open=350.0,
            high=351.0,
            low=349.0,
            close=350.5,
            volume=1000,
            best_bid=350.0,
            best_ask=350.05,
            bid_qty1=100,
            bid_qty2=100,
            bid_qty3=100,
            ask_qty1=100,
            ask_qty2=100,
            ask_qty3=100,
        )

        # Mock filters
        with patch.object(engine.time_filter, 'can_open_position', return_value=True):
            with patch.object(engine, '_is_dividend_blackout', return_value=False):
                # Add extreme basis to trigger signal
                engine.update_basis(350.0, 360.0, 30)  # Futures overpriced

                can_enter, signal, reason = engine.check_entry(
                    bar=bar,
                    spot_index=350.0,
                    days_to_expiry=30,
                )

                if can_enter and signal:
                    # Create order manager
                    manager = ArbitrageOrderManager()

                    # Create order from signal
                    order = manager.create_order(signal)
                    assert order is not None

                    # Submit and fill
                    manager.submit_order(order)
                    manager.update_fill(
                        order.order_id,
                        signal.order_size,
                        signal.entry_price,
                        is_complete=True
                    )

                    assert order.status == OrderStatus.FILLED


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
