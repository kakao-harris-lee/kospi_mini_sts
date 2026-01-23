"""
Dual Mode Strategy - Combines MODE_A and MODE_B with State Machine

MODE_A: OFI Mean Reversion (extreme z-scores)
MODE_B: Triple Barrier + TrendEngine (Combined Approach)
        - Triple Barrier CNN-LSTM for direction signal (BUY/SELL)
        - TrendEngine for technical confirmation (MA + Ichimoku)
        - TrendEngine PositionManager for risk management (ATR stops, time cuts)

State Machine:
1. Check liquidity - if below threshold, AVOID
2. Check basis gap - if extreme, MODE_A (arbitrage opportunity)
3. Otherwise, MODE_B (combined triple barrier + trend engine)
"""
import time
import logging
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

from ..base import BaseStrategy, Signal, PositionSide, BarData
from ..trend import TrendEngine

# Triple Barrier Predictor
try:
    import sys
    # Add docker-training to path for importing
    _docker_training_path = Path(__file__).parent.parent.parent.parent / "docker-training" / "scripts"
    if str(_docker_training_path) not in sys.path:
        sys.path.insert(0, str(_docker_training_path))
    from inference_triple_barrier import TripleBarrierPredictor
    TRIPLE_BARRIER_AVAILABLE = True
except ImportError as e:
    logging.getLogger(__name__).warning(f"Triple barrier predictor not available: {e}")
    TripleBarrierPredictor = None
    TRIPLE_BARRIER_AVAILABLE = False

# Decision logging (replaces simple Telegram)
try:
    from src.common.decision_logger import DecisionLogger, DecisionLog
except ImportError as e:
    logging.getLogger(__name__).error(
        f"Decision logging unavailable - import failed: {e}. "
        f"Trading decisions will NOT be logged."
    )
    DecisionLogger = None
    DecisionLog = None

# Prometheus metrics
try:
    from src.common.metrics import get_metrics
    METRICS_AVAILABLE = True
except ImportError:
    get_metrics = None
    METRICS_AVAILABLE = False

logger = logging.getLogger(__name__)


class TradingMode(Enum):
    """Trading mode for state machine"""
    AVOID = "AVOID"      # Low liquidity - avoid trading
    MODE_A = "MODE_A"    # Arbitrage opportunity
    MODE_B = "MODE_B"    # Trend following


@dataclass
class DualModeConfig:
    """Configuration for Dual Mode Strategy"""
    # Liquidity thresholds
    liquidity_avoid_threshold: float = 40.0    # Below this = AVOID
    liquidity_mode_a_threshold: float = 50.0   # Above this = MODE_A eligible

    # Basis thresholds for MODE_A (with hysteresis)
    basis_threshold: float = 1.5  # Z-score threshold to ENTER MODE_A
    basis_exit_threshold: float = 1.0  # Z-score threshold to EXIT MODE_A (hysteresis)

    # MODE_A: Arbitrage settings
    arb_max_spread_ticks: int = 6  # Allow wider spreads (was 4)
    arb_depth_multiplier: float = 2.0  # Relaxed depth requirement (was 3.0)

    # MODE_B: Triple Barrier Classification
    triple_barrier_model_dir: str = "docker-training/models/triple_barrier"
    triple_barrier_threshold: float = 0.60  # Lowered confidence threshold (was 0.70)
    triple_barrier_buffer_size: int = 100  # Rolling OHLCV buffer size
    triple_barrier_cache_bars: int = 3  # Cache prediction for N bars to reduce inference overhead

    # MODE_B: Trend settings (legacy - used as fallback)
    trend_dl_threshold: float = 0.60  # Lowered to 0.60~0.65 range
    trend_ma_fast: int = 20
    trend_ma_slow: int = 60
    trend_atr_period: int = 14
    trend_atr_multiplier: float = 2.0  # Reduced for tighter stops
    trend_time_cut_minutes: int = 45  # Extended to let winners run
    trend_max_stop_points: float = 1.5  # Cap max loss at 1.5 points = 75K KRW
    trend_time_cut_atr_threshold: float = 0.3  # Lower bar for favorable movement

    # Order size
    order_size: float = 1.0

    # Decision logging
    enable_decision_logging: bool = True
    enable_telegram: bool = True
    enable_clickhouse: bool = True


class DualModeStrategy(BaseStrategy):
    """
    Dual Mode Strategy with State Machine

    Automatically switches between:
    - MODE_A: Pure Basis Arbitrage (when basis gap is extreme)
    - MODE_B: Combined Triple Barrier + TrendEngine
        - Triple Barrier (CNN-LSTM) provides direction signal
        - TrendEngine provides technical confirmation (MA + Ichimoku)
        - TrendEngine PositionManager handles risk (ATR stops, time cuts)
    - AVOID: When liquidity is too low
    """

    def __init__(self, config: Optional[DualModeConfig] = None):
        super().__init__(name="DualMode")
        self.config = config or DualModeConfig()

        # Initialize Triple Barrier Predictor for MODE_B
        self._triple_barrier: Optional[TripleBarrierPredictor] = None
        if TRIPLE_BARRIER_AVAILABLE:
            try:
                self._triple_barrier = TripleBarrierPredictor(
                    model_dir=self.config.triple_barrier_model_dir,
                    confidence_threshold=self.config.triple_barrier_threshold,
                )
                logger.info(f"Triple barrier predictor loaded (threshold={self.config.triple_barrier_threshold})")
            except Exception as e:
                logger.error(f"Failed to load triple barrier predictor: {e}")
                self._triple_barrier = None

        # Rolling OHLCV buffer for triple barrier predictor
        self._ohlcv_buffer: deque = deque(maxlen=self.config.triple_barrier_buffer_size)

        # Initialize trend engine for MODE_B (fallback if triple barrier unavailable)
        self.trend_engine = TrendEngine(
            dl_threshold=self.config.trend_dl_threshold,
            ma_fast_period=self.config.trend_ma_fast,
            ma_slow_period=self.config.trend_ma_slow,
            atr_period=self.config.trend_atr_period,
            atr_stop_multiplier=self.config.trend_atr_multiplier,
            time_cut_minutes=self.config.trend_time_cut_minutes,
            time_cut_atr_threshold=self.config.trend_time_cut_atr_threshold,
            order_size=self.config.order_size,
            max_stop_points=self.config.trend_max_stop_points,
        )

        # Current mode
        self.current_mode = TradingMode.AVOID
        self._prev_mode = TradingMode.AVOID  # Track mode changes
        self.active_engine: Optional[str] = None  # "arb", "triple_barrier+trend", or "trend"

        # Statistics
        self._stats = {
            'total_bars': 0,
            'mode_a_bars': 0,
            'mode_b_bars': 0,
            'avoid_bars': 0,
            'mode_a_signals': 0,
            'mode_b_signals': 0,
            'triple_barrier_signals': 0,  # Signals where TB+Trend agreed
            'trend_fallback_signals': 0,  # Signals from legacy trend engine fallback
        }

        # Last notification time to avoid spam
        self._last_mode_notify = 0
        self._mode_notify_cooldown = 60  # seconds

        # Decision logger
        self._decision_logger: Optional[DecisionLogger] = None
        if self.config.enable_decision_logging and DecisionLogger:
            self._decision_logger = DecisionLogger(
                enable_telegram=self.config.enable_telegram,
                enable_clickhouse=self.config.enable_clickhouse,
            )

        # Track current bar for context
        self._current_bar: Optional[BarData] = None

        # Prometheus metrics
        self._metrics = get_metrics() if METRICS_AVAILABLE else None

        # Last emitted TB state for metrics
        self._last_tb_signal: int = 0  # -1=SELL, 0=HOLD, 1=BUY
        self._last_tb_confidence: float = 0.0
        self._last_tb_probs: Dict[str, float] = {"buy": 0.0, "sell": 0.0, "hold": 1.0}

        # Triple Barrier prediction cache (reduces inference from every bar to every N bars)
        self._tb_cache_result: Optional[Dict[str, Any]] = None
        self._tb_cache_bar_count: int = 0

    def _determine_mode(self, bar: BarData) -> TradingMode:
        """Determine trading mode based on current conditions with hysteresis."""
        # Check liquidity (use bid_ask_imbalance as proxy, or spread)
        spread = bar.spread if bar.spread > 0 else (bar.best_ask - bar.best_bid)

        # If spread is too wide, avoid (but allow in backtest mode where spread=0)
        if spread > 0.5:
            return TradingMode.AVOID

        # Check basis for arbitrage opportunity with hysteresis
        # Use OFI z-score as proxy for basis deviation
        ofi_abs = abs(bar.ofi_zscore)

        if self.current_mode == TradingMode.MODE_A:
            # Currently in MODE_A: stay until z-score drops below exit threshold
            if ofi_abs >= self.config.basis_exit_threshold:
                return TradingMode.MODE_A
            else:
                return TradingMode.MODE_B
        else:
            # Currently in MODE_B or AVOID: enter MODE_A only if exceeds entry threshold
            if ofi_abs > self.config.basis_threshold:
                return TradingMode.MODE_A
            else:
                return TradingMode.MODE_B

    def _emit_mode_metrics(self, bar: BarData) -> None:
        """Emit current mode and engine state to Prometheus metrics."""
        if not self._metrics:
            return

        strategy = "dual_mode"

        # Mode value: AVOID=0, MODE_A=1, MODE_B=2
        mode_map = {"AVOID": 0, "MODE_A": 1, "MODE_B": 2}
        mode_val = mode_map.get(self.current_mode.value, 0)
        self._metrics.set_trading_mode(strategy, mode_val)

        # Active engine indicators
        engines = ["arb", "triple_barrier", "trend", "none"]
        for eng in engines:
            if self.active_engine is None:
                active = (eng == "none")
            else:
                active = (eng in self.active_engine)
            self._metrics.set_active_engine(strategy, eng, active)

        # Triple Barrier state
        self._metrics.set_triple_barrier_signal(strategy, self._last_tb_signal)
        self._metrics.set_triple_barrier_confidence(strategy, self._last_tb_confidence)
        self._metrics.set_triple_barrier_probs(
            strategy,
            self._last_tb_probs.get("buy", 0.0),
            self._last_tb_probs.get("sell", 0.0),
            self._last_tb_probs.get("hold", 1.0),
        )

        # TrendEngine state
        tech = self.trend_engine.technical.get_current_data()
        if tech:
            self._metrics.set_trend_ma_direction(strategy, tech.is_bullish_ma)

            # Ichimoku position: -1=below, 0=in, 1=above
            if bar.close > tech.cloud_top:
                cloud_pos = 1
            elif bar.close < tech.cloud_bottom:
                cloud_pos = -1
            else:
                cloud_pos = 0
            self._metrics.set_trend_ichimoku_position(strategy, cloud_pos)
            self._metrics.set_trend_atr(strategy, tech.atr)

        # Position state
        if self.trend_engine.position_manager.has_position:
            pos = self.trend_engine.position_manager.position
            self._metrics.set_trend_position(strategy, pos.entry_price, pos.stop_price)
        else:
            self._metrics.set_trend_position(strategy, 0.0, 0.0)

    def generate_signal(self, bar: BarData) -> Signal:
        """Generate signal using state machine logic."""
        self._stats['total_bars'] += 1
        self._current_bar = bar  # Track for decision logging

        # ALWAYS update technical indicators and calibrator, regardless of mode
        # This ensures proper warm-up for when we do enter MODE_B
        self.trend_engine.update_bar(
            high=bar.high,
            low=bar.low,
            close=bar.close,
        )

        if bar.up_prob_h10 != 0.5:  # Has ensemble predictions
            self.trend_engine.ensemble_filter.calibrator.update(1, bar.up_prob_h1)
            self.trend_engine.ensemble_filter.calibrator.update(3, bar.up_prob_h3)
            self.trend_engine.ensemble_filter.calibrator.update(5, bar.up_prob_h5)
            self.trend_engine.ensemble_filter.calibrator.update(10, bar.up_prob_h10)

        # Determine current mode
        mode = self._determine_mode(bar)
        self.current_mode = mode

        # Notify on mode change (with cooldown to avoid spam)
        now = time.time()
        if mode != self._prev_mode and (now - self._last_mode_notify) > self._mode_notify_cooldown:
            self._notify_mode_change(self._prev_mode, mode, bar)
            self._last_mode_notify = now
        self._prev_mode = mode

        if mode == TradingMode.AVOID:
            self._stats['avoid_bars'] += 1
            signal = Signal.HOLD

        elif mode == TradingMode.MODE_A:
            self._stats['mode_a_bars'] += 1
            signal = self._process_mode_a(bar)

        else:  # MODE_B
            self._stats['mode_b_bars'] += 1
            signal = self._process_mode_b(bar)

        # Emit metrics after processing
        self._emit_mode_metrics(bar)
        return signal

    def _notify_mode_change(self, prev: TradingMode, curr: TradingMode, bar: BarData) -> None:
        """Log mode change event."""
        logger.info(f"Mode change: {prev.value} → {curr.value} @ {bar.close:.2f}")

        if self._decision_logger:
            self._decision_logger.log_mode_change(
                prev_mode=prev.value,
                new_mode=curr.value,
                bar_data={
                    'close': bar.close,
                    'spread': bar.spread,
                    'ofi_zscore': bar.ofi_zscore,
                }
            )

        # Record mode transition metric
        if self._metrics:
            self._metrics.record_mode_transition("dual_mode", prev.value, curr.value)

    def _process_mode_a(self, bar: BarData) -> Signal:
        """Process MODE_A: Arbitrage based on OFI z-score extremes."""
        # MODE_A simplified: use OFI z-score as basis proxy for mean reversion
        # When OFI z-score is extreme, expect price to revert
        ofi_z = bar.ofi_zscore

        # Check spread is acceptable
        spread = bar.spread if bar.spread > 0 else (bar.best_ask - bar.best_bid)
        max_spread = self.config.arb_max_spread_ticks * 0.05  # 6 * 0.05 = 0.30
        if spread > max_spread:
            logger.debug(f"MODE_A blocked: spread {spread:.3f} > {max_spread:.3f}")
            return Signal.HOLD

        # Check depth is sufficient
        total_bid_qty = bar.bid_qty1 + bar.bid_qty2 + bar.bid_qty3
        total_ask_qty = bar.ask_qty1 + bar.ask_qty2 + bar.ask_qty3
        min_depth = self.config.order_size * self.config.arb_depth_multiplier  # 1.0 * 2.0 = 2.0
        if total_bid_qty < min_depth or total_ask_qty < min_depth:
            logger.debug(f"MODE_A blocked: depth bid={total_bid_qty:.0f} ask={total_ask_qty:.0f} < {min_depth:.0f}")
            return Signal.HOLD

        # Arbitrage signal: extreme OFI z-score suggests mean reversion
        if ofi_z < -self.config.basis_threshold:
            # Oversold - expect bounce (BUY)
            self._stats['mode_a_signals'] += 1
            self.active_engine = "arb"
            self._notify_signal("BUY", "MODE_A", bar, f"OFI oversold (z={ofi_z:.2f})")
            return Signal.BUY
        elif ofi_z > self.config.basis_threshold:
            # Overbought - expect pullback (SELL)
            self._stats['mode_a_signals'] += 1
            self.active_engine = "arb"
            self._notify_signal("SELL", "MODE_A", bar, f"OFI overbought (z={ofi_z:.2f})")
            return Signal.SELL

        return Signal.HOLD

    def _update_ohlcv_buffer(self, bar: BarData) -> None:
        """Add bar to OHLCV buffer for triple barrier predictor."""
        self._ohlcv_buffer.append({
            'datetime': bar.datetime,
            'open': bar.open,
            'high': bar.high,
            'low': bar.low,
            'close': bar.close,
            'volume': bar.volume,
        })

    def _get_ohlcv_dataframe(self) -> Optional[pd.DataFrame]:
        """Convert OHLCV buffer to DataFrame for triple barrier predictor."""
        if len(self._ohlcv_buffer) < 80:  # Need at least 80 rows for features
            return None
        df = pd.DataFrame(list(self._ohlcv_buffer))
        # Validate price data - close must be positive for log calculations
        if (df['close'] <= 0).any():
            return None
        return df

    def _process_mode_b(self, bar: BarData) -> Signal:
        """
        Process MODE_B: Combined Triple Barrier + TrendEngine.

        Entry Flow:
        1. Triple Barrier provides direction signal (BUY/SELL)
        2. TrendEngine provides technical confirmation (MA + Ichimoku)
        3. Only enter if BOTH agree
        4. TrendEngine PositionManager handles the position (ATR stops, time cuts)

        Exit Flow:
        1. TrendEngine PositionManager checks ATR trailing stop
        2. TrendEngine PositionManager checks time cut
        3. If triggered, close position
        """
        # Update OHLCV buffer for Triple Barrier
        self._update_ohlcv_buffer(bar)

        # Update TrendEngine bid/ask for execution
        self.trend_engine.update_prices(
            bid=bar.best_bid if bar.best_bid > 0 else bar.close,
            ask=bar.best_ask if bar.best_ask > 0 else bar.close,
        )

        timestamp = bar.datetime.timestamp() if bar.datetime else time.time()

        # Get technical data for confirmation
        tech = self.trend_engine.technical.get_current_data()
        current_atr = tech.atr if tech else 0.0

        # === EXIT MANAGEMENT ===
        # If TrendEngine has a position, check for exit conditions
        if self.trend_engine.position_manager.has_position:
            exit_signal = self.trend_engine.position_manager.update_and_check_exit(
                current_price=bar.close,
                current_atr=current_atr,
                current_time=timestamp,
            )

            if exit_signal.should_exit:
                # Close position
                pos = self.trend_engine.position_manager.position
                exit_price = bar.best_bid if pos.is_long else bar.best_ask
                if exit_price <= 0:
                    exit_price = bar.close

                pnl = self.trend_engine.position_manager.close_position(
                    exit_price, exit_signal.reason, timestamp
                )

                self.active_engine = None
                self._notify_close(bar, f"{exit_signal.reason.value}: {exit_signal.message} (PnL: {pnl:.2f})")

                # Return opposite signal to close
                if pos.is_long:
                    return Signal.SELL
                else:
                    return Signal.BUY

            # Position OK, hold
            return Signal.HOLD

        # === ENTRY MANAGEMENT ===
        # No position - check for new entry

        # Check if technical indicators are ready
        if tech is None or not tech.is_ready:
            return Signal.HOLD

        # Step 1: Get Triple Barrier direction signal (with caching to reduce inference overhead)
        tb_signal = None
        tb_confidence = 0.0
        tb_probs = {}

        if self._triple_barrier is not None:
            # Check if we can use cached prediction
            self._tb_cache_bar_count += 1
            use_cache = (
                self._tb_cache_result is not None
                and self._tb_cache_bar_count <= self.config.triple_barrier_cache_bars
            )

            if use_cache:
                # Use cached prediction (saves 15-70ms inference time)
                tb_signal = self._tb_cache_result["signal"]
                tb_confidence = self._tb_cache_result["confidence"]
                tb_probs = self._tb_cache_result["probabilities"]
                logger.debug(f"Using cached TB prediction: {tb_signal} (bar {self._tb_cache_bar_count}/{self.config.triple_barrier_cache_bars})")
            else:
                # Run fresh inference
                df = self._get_ohlcv_dataframe()
                if df is not None:
                    try:
                        result = self._triple_barrier.generate_signal(df)
                        tb_signal = result["signal"]  # "BUY", "SELL", or "HOLD"
                        tb_confidence = result["confidence"]
                        tb_probs = result["probabilities"]

                        # Cache the result
                        self._tb_cache_result = result
                        self._tb_cache_bar_count = 1

                        # Track TB state for metrics
                        self._last_tb_signal = {"BUY": 1, "SELL": -1, "HOLD": 0}.get(tb_signal, 0)
                        self._last_tb_confidence = tb_confidence
                        self._last_tb_probs = tb_probs
                    except Exception as e:
                        logger.warning(f"Triple barrier prediction failed: {e}")
                        tb_signal = None
                        self._tb_cache_result = None

        # If no Triple Barrier signal, fallback to legacy
        if tb_signal is None or tb_signal == "HOLD":
            return self._process_mode_b_legacy(bar)

        # Step 2: Get TrendEngine technical confirmation
        # Check MA direction
        ma_bullish = tech.is_bullish_ma  # Fast MA > Slow MA

        # Check Ichimoku cloud position
        above_cloud = bar.close > tech.cloud_top
        below_cloud = bar.close < tech.cloud_bottom

        # Step 3: Accept TB signal with minimal confirmation (relaxed from requiring both MA + Ichimoku)
        # Now: accept TB signal if confidence is high enough, with logging of technical context
        confirmed = False
        reason = ""

        if tb_signal == "BUY":
            # Triple Barrier says BUY - accept with high confidence, log technical context
            tech_context = []
            if ma_bullish:
                tech_context.append("MA bullish")
            else:
                tech_context.append("MA bearish")
            if above_cloud:
                tech_context.append("above cloud")
            elif below_cloud:
                tech_context.append("below cloud")
            else:
                tech_context.append("in cloud")

            # Accept if confidence meets threshold (already checked by TB predictor)
            confirmed = True
            reason = f"TB BUY (conf: {tb_confidence:.1%}) [{', '.join(tech_context)}]"

        elif tb_signal == "SELL":
            # Triple Barrier says SELL - accept with high confidence, log technical context
            tech_context = []
            if ma_bullish:
                tech_context.append("MA bullish")
            else:
                tech_context.append("MA bearish")
            if above_cloud:
                tech_context.append("above cloud")
            elif below_cloud:
                tech_context.append("below cloud")
            else:
                tech_context.append("in cloud")

            # Accept if confidence meets threshold
            confirmed = True
            reason = f"TB SELL (conf: {tb_confidence:.1%}) [{', '.join(tech_context)}]"

        # Step 4: If confirmed, open position via TrendEngine
        if confirmed:
            direction = "LONG" if tb_signal == "BUY" else "SHORT"
            entry_price = bar.best_ask if direction == "LONG" else bar.best_bid
            if entry_price <= 0:
                entry_price = bar.close

            # Open position with ATR-based stop
            position = self.trend_engine.position_manager.open_position(
                side=direction,
                entry_price=entry_price,
                size=self.config.order_size,
                atr=current_atr,
                symbol="",
                timestamp=timestamp,
            )

            if position is None:
                # Blocked by cooldown
                return Signal.HOLD

            self._stats['triple_barrier_signals'] += 1
            self._stats['mode_b_signals'] += 1
            self.active_engine = "triple_barrier+trend"

            full_reason = f"{reason} | Stop: {position.stop_price:.2f} (ATR: {current_atr:.2f})"
            self._notify_signal(tb_signal, "MODE_B", bar, full_reason)

            return Signal.BUY if tb_signal == "BUY" else Signal.SELL

        # No confirmed entry
        return Signal.HOLD

    def _process_mode_b_legacy(self, bar: BarData) -> Signal:
        """
        Fallback MODE_B: Trend Following with multi-horizon ensemble.

        Used when:
        1. Triple Barrier predictor is not available
        2. Triple Barrier returns HOLD signal
        3. Triple Barrier prediction failed
        """
        # Technical indicators already updated in generate_signal()
        tech = self.trend_engine.technical.get_current_data()

        # Update bid/ask for execution
        self.trend_engine.update_prices(
            bid=bar.best_bid if bar.best_bid > 0 else bar.close,
            ask=bar.best_ask if bar.best_ask > 0 else bar.close,
        )

        timestamp = bar.datetime.timestamp() if bar.datetime else time.time()

        # Check if multi-horizon predictions are available
        has_ensemble = (
            bar.up_prob_h10 != 0.5 or bar.up_prob_h1 != 0.5 or
            bar.up_prob_h3 != 0.5 or bar.up_prob_h5 != 0.5
        )

        if has_ensemble:
            # Use multi-horizon "Shortest Confirms Longest" strategy
            horizon_probs = {
                1: bar.up_prob_h1,
                3: bar.up_prob_h3,
                5: bar.up_prob_h5,
                10: bar.up_prob_h10,
            }
            signal = self.trend_engine.check_multi_horizon(
                horizon_probs=horizon_probs,
                current_price=bar.close,
                timestamp=timestamp,
            )
            # Use h10 as the primary probability for logging
            up_prob = bar.up_prob_h10
        else:
            # Fallback: Use single up_prob or MA + Ichimoku-based momentum
            # Re-enabled blind fallback - better to have signals than none
            up_prob = bar.up_prob
            if up_prob == 0.5 and tech is not None:
                # Generate synthetic probability from technical indicators
                ma_bullish = tech.is_bullish_ma
                above_cloud = tech.current_price > tech.cloud_top
                below_cloud = tech.current_price < tech.cloud_bottom

                if ma_bullish and above_cloud:
                    up_prob = 0.75  # Slightly lower than before (was 0.80)
                elif ma_bullish:
                    up_prob = 0.65  # (was 0.70)
                elif not ma_bullish and below_cloud:
                    up_prob = 0.25  # (was 0.20)
                elif not ma_bullish:
                    up_prob = 0.35  # (was 0.30)

                logger.debug(f"MODE_B fallback: Using synthetic prob={up_prob:.1%} from MA+Ichimoku")

            signal = self.trend_engine.check(
                up_prob=up_prob,
                current_price=bar.close,
                timestamp=timestamp,
            )

        if signal.action == "OPEN_LONG":
            self._stats['mode_b_signals'] += 1
            self._stats['trend_fallback_signals'] += 1
            self.active_engine = "trend"
            reason = f"LONG entry [fallback] (prob: {up_prob:.1%})"
            self._notify_signal("BUY", "MODE_B", bar, reason)
            return Signal.BUY
        elif signal.action == "OPEN_SHORT":
            self._stats['mode_b_signals'] += 1
            self._stats['trend_fallback_signals'] += 1
            self.active_engine = "trend"
            reason = f"SHORT entry [fallback] (prob: {1-up_prob:.1%})"
            self._notify_signal("SELL", "MODE_B", bar, reason)
            return Signal.SELL
        elif signal.action == "CLOSE":
            self.active_engine = None
            self._notify_close(bar, signal.reason)
            # Return opposite signal to close
            if self.state.position == PositionSide.LONG:
                return Signal.SELL
            elif self.state.position == PositionSide.SHORT:
                return Signal.BUY

        return Signal.HOLD

    def _notify_signal(self, action: str, mode: str, bar: BarData, reason: str) -> None:
        """Log trading signal with full context."""
        logger.info(f"Signal: {action} @ {bar.close:.2f} ({mode}) - {reason}")

        if not self._decision_logger or not DecisionLog:
            return

        # Get technical data
        tech = self.trend_engine.technical.get_current_data()

        # Get z-scores from calibrator
        calibrator = self.trend_engine.ensemble_filter.calibrator
        zscore_h1 = calibrator.get_zscore(1, bar.up_prob_h1) if bar.up_prob_h1 != 0.5 else 0.0
        zscore_h10 = calibrator.get_zscore(10, bar.up_prob_h10) if bar.up_prob_h10 != 0.5 else 0.0

        decision = DecisionLog(
            timestamp=bar.datetime or datetime.now(),
            signal=action,
            price=bar.close,
            mode=mode,
            reason=reason,
            # DL probabilities
            up_prob_h1=bar.up_prob_h1,
            up_prob_h3=bar.up_prob_h3,
            up_prob_h5=bar.up_prob_h5,
            up_prob_h10=bar.up_prob_h10,
            # Z-scores
            zscore_h1=zscore_h1,
            zscore_h10=zscore_h10,
            # Technical indicators
            ma_fast=tech.ma_fast if tech else 0.0,
            ma_slow=tech.ma_slow if tech else 0.0,
            ma_bullish=tech.is_bullish_ma if tech else False,
            rsi=50.0,  # RSI not implemented in TechnicalCalculator
            atr=tech.atr if tech else 0.0,
            cloud_top=tech.cloud_top if tech else 0.0,
            cloud_bottom=tech.cloud_bottom if tech else 0.0,
            above_cloud=(tech.current_price > tech.cloud_top) if tech else False,
            # Market context
            spread=bar.spread,
            ofi_zscore=bar.ofi_zscore,
            regime=bar.regime or "unknown",
        )
        self._decision_logger.log_entry(decision)

    def _notify_close(self, bar: BarData, reason: str) -> None:
        """Log position close with full context."""
        logger.info(f"Close @ {bar.close:.2f} - {reason}")

        if not self._decision_logger or not DecisionLog:
            return

        # Get technical data
        tech = self.trend_engine.technical.get_current_data()

        # Get z-scores from calibrator
        calibrator = self.trend_engine.ensemble_filter.calibrator
        zscore_h1 = calibrator.get_zscore(1, bar.up_prob_h1) if bar.up_prob_h1 != 0.5 else 0.0
        zscore_h10 = calibrator.get_zscore(10, bar.up_prob_h10) if bar.up_prob_h10 != 0.5 else 0.0

        decision = DecisionLog(
            timestamp=bar.datetime or datetime.now(),
            signal="CLOSE",
            price=bar.close,
            mode=self.current_mode.value,
            reason=reason,
            # DL probabilities
            up_prob_h1=bar.up_prob_h1,
            up_prob_h3=bar.up_prob_h3,
            up_prob_h5=bar.up_prob_h5,
            up_prob_h10=bar.up_prob_h10,
            # Z-scores
            zscore_h1=zscore_h1,
            zscore_h10=zscore_h10,
            # Technical indicators
            ma_fast=tech.ma_fast if tech else 0.0,
            ma_slow=tech.ma_slow if tech else 0.0,
            ma_bullish=tech.is_bullish_ma if tech else False,
            rsi=50.0,  # RSI not implemented in TechnicalCalculator
            atr=tech.atr if tech else 0.0,
            cloud_top=tech.cloud_top if tech else 0.0,
            cloud_bottom=tech.cloud_bottom if tech else 0.0,
            above_cloud=(tech.current_price > tech.cloud_top) if tech else False,
            # Market context
            spread=bar.spread,
            ofi_zscore=bar.ofi_zscore,
            regime=bar.regime or "unknown",
        )
        self._decision_logger.log_close(decision)

    def get_mode_name(self) -> str:
        """Get current mode name."""
        return self.current_mode.value

    def get_stats(self) -> Dict[str, Any]:
        """Get strategy statistics."""
        total = self._stats['total_bars']
        return {
            **self._stats,
            'current_mode': self.current_mode.value,
            'active_engine': self.active_engine,
            'mode_a_ratio': self._stats['mode_a_bars'] / total if total > 0 else 0,
            'mode_b_ratio': self._stats['mode_b_bars'] / total if total > 0 else 0,
            'avoid_ratio': self._stats['avoid_bars'] / total if total > 0 else 0,
            'trend_stats': self.trend_engine.get_stats(),
        }

    def send_daily_summary(self) -> None:
        """Send daily trading summary via Telegram."""
        if self._decision_logger:
            self._decision_logger.send_daily_summary()

    def reset(self):
        """Reset strategy state."""
        super().reset()
        self.trend_engine.reset()
        self.current_mode = TradingMode.AVOID
        self.active_engine = None
        for key in self._stats:
            if isinstance(self._stats[key], int):
                self._stats[key] = 0
        if self._decision_logger:
            self._decision_logger.reset_daily_stats()
        # Reset TB prediction cache
        self._tb_cache_result = None
        self._tb_cache_bar_count = 0
