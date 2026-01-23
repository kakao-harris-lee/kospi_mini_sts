"""
DualModeStrategy 테스트

Tests for:
- Hysteresis state machine logic
- Mode transitions and boundary conditions
- Position handling during mode switches
- Triple Barrier caching
"""
import pytest
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock
from dataclasses import dataclass

from src.strategy.base import Signal, PositionSide, BarData
from src.strategy.strategies.dual_mode import (
    DualModeStrategy,
    DualModeConfig,
    TradingMode,
)


def create_bar(
    ofi_zscore: float = 0.0,
    close: float = 350.0,
    spread: float = 0.05,
    best_bid: float = 349.95,
    best_ask: float = 350.05,
    bid_qty1: float = 10.0,
    bid_qty2: float = 10.0,
    bid_qty3: float = 10.0,
    ask_qty1: float = 10.0,
    ask_qty2: float = 10.0,
    ask_qty3: float = 10.0,
    high: float = 351.0,
    low: float = 349.0,
    open_price: float = 350.0,
    volume: float = 1000.0,
    up_prob: float = 0.5,
    up_prob_h1: float = 0.5,
    up_prob_h3: float = 0.5,
    up_prob_h5: float = 0.5,
    up_prob_h10: float = 0.5,
) -> BarData:
    """Create a test BarData with configurable fields."""
    return BarData(
        datetime=datetime.now(),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        ofi_zscore=ofi_zscore,
        spread=spread,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_qty1=bid_qty1,
        bid_qty2=bid_qty2,
        bid_qty3=bid_qty3,
        ask_qty1=ask_qty1,
        ask_qty2=ask_qty2,
        ask_qty3=ask_qty3,
        up_prob=up_prob,
        up_prob_h1=up_prob_h1,
        up_prob_h3=up_prob_h3,
        up_prob_h5=up_prob_h5,
        up_prob_h10=up_prob_h10,
    )


class TestDualModeHysteresis:
    """Test hysteresis prevents mode oscillation."""

    @pytest.fixture
    def strategy(self):
        """Create a DualModeStrategy with mocked dependencies."""
        config = DualModeConfig(
            basis_threshold=1.5,
            basis_exit_threshold=1.0,
            enable_decision_logging=False,
        )
        with patch('src.strategy.strategies.dual_mode.TRIPLE_BARRIER_AVAILABLE', False):
            strategy = DualModeStrategy(config)
        return strategy

    def test_initial_mode_is_avoid(self, strategy):
        """Initial mode should be AVOID."""
        assert strategy.current_mode == TradingMode.AVOID

    def test_mode_b_to_mode_a_requires_entry_threshold(self, strategy):
        """MODE_B → MODE_A requires |ofi_z| > 1.5 (entry threshold)."""
        # Start in MODE_B (simulate by setting current_mode)
        strategy.current_mode = TradingMode.MODE_B

        # Test: ofi_z = 1.4 should stay in MODE_B (below entry threshold)
        bar = create_bar(ofi_zscore=1.4)
        mode = strategy._determine_mode(bar)
        assert mode == TradingMode.MODE_B

        # Test: ofi_z = 1.5 should stay in MODE_B (at threshold, not above)
        bar = create_bar(ofi_zscore=1.5)
        mode = strategy._determine_mode(bar)
        assert mode == TradingMode.MODE_B

        # Test: ofi_z = 1.6 should switch to MODE_A (above entry threshold)
        bar = create_bar(ofi_zscore=1.6)
        mode = strategy._determine_mode(bar)
        assert mode == TradingMode.MODE_A

    def test_mode_a_stays_until_exit_threshold(self, strategy):
        """MODE_A should persist until |ofi_z| drops below 1.0 (exit threshold)."""
        # Start in MODE_A
        strategy.current_mode = TradingMode.MODE_A

        # Test: ofi_z = 1.2 should stay in MODE_A (above exit threshold)
        bar = create_bar(ofi_zscore=1.2)
        mode = strategy._determine_mode(bar)
        assert mode == TradingMode.MODE_A

        # Test: ofi_z = 1.0 should stay in MODE_A (at exit threshold, >= means stay)
        bar = create_bar(ofi_zscore=1.0)
        mode = strategy._determine_mode(bar)
        assert mode == TradingMode.MODE_A

        # Test: ofi_z = 0.9 should exit to MODE_B (below exit threshold)
        bar = create_bar(ofi_zscore=0.9)
        mode = strategy._determine_mode(bar)
        assert mode == TradingMode.MODE_B

    def test_no_oscillation_in_dead_zone(self, strategy):
        """OFI oscillating between 1.0-1.5 should not cause mode flips."""
        # Sequence: start in MODE_B, then oscillate in dead zone
        strategy.current_mode = TradingMode.MODE_B

        # These values are all in the dead zone (1.0-1.5)
        ofi_sequence = [1.2, 1.4, 1.1, 1.3, 1.2, 1.4, 1.0, 1.3]

        for ofi_z in ofi_sequence:
            bar = create_bar(ofi_zscore=ofi_z)
            mode = strategy._determine_mode(bar)
            # Should always stay in MODE_B when starting from MODE_B in dead zone
            assert mode == TradingMode.MODE_B, f"Unexpected mode switch at ofi_z={ofi_z}"
            strategy.current_mode = mode

    def test_negative_ofi_zscore(self, strategy):
        """Negative ofi_zscore should use absolute value for thresholds."""
        strategy.current_mode = TradingMode.MODE_B

        # Test: ofi_z = -1.4 should stay in MODE_B
        bar = create_bar(ofi_zscore=-1.4)
        mode = strategy._determine_mode(bar)
        assert mode == TradingMode.MODE_B

        # Test: ofi_z = -1.6 should switch to MODE_A
        bar = create_bar(ofi_zscore=-1.6)
        mode = strategy._determine_mode(bar)
        assert mode == TradingMode.MODE_A

    def test_wide_spread_triggers_avoid(self, strategy):
        """Spread > 0.5 should trigger AVOID mode."""
        strategy.current_mode = TradingMode.MODE_B

        bar = create_bar(ofi_zscore=2.0, spread=0.6)
        mode = strategy._determine_mode(bar)
        assert mode == TradingMode.AVOID


class TestDualModeStateTransitions:
    """Test full state machine transitions."""

    @pytest.fixture
    def strategy(self):
        """Create a DualModeStrategy with mocked dependencies."""
        config = DualModeConfig(
            basis_threshold=1.5,
            basis_exit_threshold=1.0,
            enable_decision_logging=False,
        )
        with patch('src.strategy.strategies.dual_mode.TRIPLE_BARRIER_AVAILABLE', False):
            strategy = DualModeStrategy(config)
        return strategy

    def test_full_transition_cycle(self, strategy):
        """Test full cycle: AVOID → MODE_B → MODE_A → MODE_B → MODE_A."""
        # Start in AVOID
        assert strategy.current_mode == TradingMode.AVOID

        # Normal spread, low ofi_z → should go to MODE_B
        bar = create_bar(ofi_zscore=0.5)
        mode = strategy._determine_mode(bar)
        assert mode == TradingMode.MODE_B
        strategy.current_mode = mode

        # High ofi_z → should go to MODE_A
        bar = create_bar(ofi_zscore=2.0)
        mode = strategy._determine_mode(bar)
        assert mode == TradingMode.MODE_A
        strategy.current_mode = mode

        # Moderate ofi_z (in dead zone) → should stay in MODE_A
        bar = create_bar(ofi_zscore=1.2)
        mode = strategy._determine_mode(bar)
        assert mode == TradingMode.MODE_A
        strategy.current_mode = mode

        # Low ofi_z → should exit to MODE_B
        bar = create_bar(ofi_zscore=0.5)
        mode = strategy._determine_mode(bar)
        assert mode == TradingMode.MODE_B
        strategy.current_mode = mode

        # High ofi_z again → should return to MODE_A
        bar = create_bar(ofi_zscore=1.8)
        mode = strategy._determine_mode(bar)
        assert mode == TradingMode.MODE_A

    def test_mode_persists_across_generate_signal(self, strategy):
        """Mode state should persist correctly across generate_signal() calls."""
        # Warm up technical indicators (need enough bars)
        for i in range(30):
            bar = create_bar(ofi_zscore=0.5, close=350.0 + i * 0.1)
            strategy.generate_signal(bar)

        # Verify we're in MODE_B
        assert strategy.current_mode == TradingMode.MODE_B

        # Push ofi_z above entry threshold
        bar = create_bar(ofi_zscore=2.0)
        strategy.generate_signal(bar)
        assert strategy.current_mode == TradingMode.MODE_A

        # Stay in dead zone for multiple bars
        for _ in range(5):
            bar = create_bar(ofi_zscore=1.2)
            strategy.generate_signal(bar)
            assert strategy.current_mode == TradingMode.MODE_A


class TestDualModeModeASignals:
    """Test MODE_A signal generation with relaxed filters."""

    @pytest.fixture
    def strategy(self):
        """Create a DualModeStrategy."""
        config = DualModeConfig(
            basis_threshold=1.5,
            arb_max_spread_ticks=6,  # 0.30 spread allowed
            arb_depth_multiplier=2.0,  # 2 contracts minimum
            enable_decision_logging=False,
        )
        with patch('src.strategy.strategies.dual_mode.TRIPLE_BARRIER_AVAILABLE', False):
            strategy = DualModeStrategy(config)
        strategy.current_mode = TradingMode.MODE_A
        return strategy

    def test_mode_a_buy_signal_on_negative_ofi(self, strategy):
        """MODE_A should generate BUY when ofi_z < -1.5."""
        bar = create_bar(
            ofi_zscore=-2.0,
            spread=0.10,  # Within limit
            bid_qty1=5, bid_qty2=5, bid_qty3=5,  # 15 total > 2 min
            ask_qty1=5, ask_qty2=5, ask_qty3=5,
        )
        signal = strategy._process_mode_a(bar)
        assert signal == Signal.BUY

    def test_mode_a_sell_signal_on_positive_ofi(self, strategy):
        """MODE_A should generate SELL when ofi_z > 1.5."""
        bar = create_bar(
            ofi_zscore=2.0,
            spread=0.10,
            bid_qty1=5, bid_qty2=5, bid_qty3=5,
            ask_qty1=5, ask_qty2=5, ask_qty3=5,
        )
        signal = strategy._process_mode_a(bar)
        assert signal == Signal.SELL

    def test_mode_a_hold_when_spread_too_wide(self, strategy):
        """MODE_A should HOLD when spread > arb_max_spread_ticks * 0.05."""
        bar = create_bar(
            ofi_zscore=2.0,
            spread=0.35,  # > 0.30 limit (6 ticks * 0.05)
            bid_qty1=5, bid_qty2=5, bid_qty3=5,
            ask_qty1=5, ask_qty2=5, ask_qty3=5,
        )
        signal = strategy._process_mode_a(bar)
        assert signal == Signal.HOLD

    def test_mode_a_hold_when_depth_insufficient(self, strategy):
        """MODE_A should HOLD when depth < arb_depth_multiplier."""
        bar = create_bar(
            ofi_zscore=2.0,
            spread=0.10,
            bid_qty1=0.5, bid_qty2=0.5, bid_qty3=0.5,  # 1.5 total < 2 min
            ask_qty1=5, ask_qty2=5, ask_qty3=5,
        )
        signal = strategy._process_mode_a(bar)
        assert signal == Signal.HOLD

    def test_mode_a_hold_in_dead_zone(self, strategy):
        """MODE_A should HOLD when |ofi_z| is in signal dead zone."""
        bar = create_bar(
            ofi_zscore=1.2,  # Below signal threshold (1.5)
            spread=0.10,
            bid_qty1=5, bid_qty2=5, bid_qty3=5,
            ask_qty1=5, ask_qty2=5, ask_qty3=5,
        )
        signal = strategy._process_mode_a(bar)
        assert signal == Signal.HOLD


class TestTripleBarrierCaching:
    """Test Triple Barrier prediction caching."""

    @pytest.fixture
    def strategy(self):
        """Create strategy with mocked Triple Barrier."""
        config = DualModeConfig(
            triple_barrier_cache_bars=3,
            enable_decision_logging=False,
        )

        with patch('src.strategy.strategies.dual_mode.TRIPLE_BARRIER_AVAILABLE', True):
            with patch('src.strategy.strategies.dual_mode.TripleBarrierPredictor') as MockPredictor:
                mock_predictor = Mock()
                mock_predictor.generate_signal.return_value = {
                    "signal": "HOLD",
                    "confidence": 0.5,
                    "probabilities": {"buy": 0.3, "sell": 0.3, "hold": 0.4}
                }
                MockPredictor.return_value = mock_predictor
                strategy = DualModeStrategy(config)
                strategy._triple_barrier = mock_predictor
        return strategy

    def test_cache_initialized_empty(self, strategy):
        """Cache should be initialized as None."""
        assert strategy._tb_cache_result is None
        assert strategy._tb_cache_bar_count == 0

    def test_cache_reset_on_strategy_reset(self, strategy):
        """Cache should be cleared on strategy reset."""
        # Set some cache state
        strategy._tb_cache_result = {"signal": "BUY"}
        strategy._tb_cache_bar_count = 2

        strategy.reset()

        assert strategy._tb_cache_result is None
        assert strategy._tb_cache_bar_count == 0


class TestDualModeReset:
    """Test strategy reset behavior."""

    @pytest.fixture
    def strategy(self):
        """Create a DualModeStrategy."""
        config = DualModeConfig(enable_decision_logging=False)
        with patch('src.strategy.strategies.dual_mode.TRIPLE_BARRIER_AVAILABLE', False):
            strategy = DualModeStrategy(config)
        return strategy

    def test_reset_clears_mode(self, strategy):
        """Reset should clear mode to AVOID."""
        strategy.current_mode = TradingMode.MODE_A

        strategy.reset()

        assert strategy.current_mode == TradingMode.AVOID

    def test_reset_clears_stats(self, strategy):
        """Reset should clear statistics."""
        strategy._stats['total_bars'] = 100
        strategy._stats['mode_a_bars'] = 50

        strategy.reset()

        assert strategy._stats['total_bars'] == 0
        assert strategy._stats['mode_a_bars'] == 0

    def test_reset_clears_cache(self, strategy):
        """Reset should clear TB prediction cache."""
        strategy._tb_cache_result = {"signal": "BUY"}
        strategy._tb_cache_bar_count = 2

        strategy.reset()

        assert strategy._tb_cache_result is None
        assert strategy._tb_cache_bar_count == 0
