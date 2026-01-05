"""
Dual Mode Strategy - Combines MODE_A and MODE_B with State Machine

MODE_A: Pure Basis Arbitrage (ArbitrageEngine)
MODE_B: Deep Learning Trend Following (TrendEngine)

State Machine:
1. Check liquidity - if below threshold, AVOID
2. Check basis gap - if extreme, MODE_A (arbitrage opportunity)
3. Otherwise, MODE_B (trend following with DL)
"""
import time
import logging
from datetime import datetime
from typing import Dict, Any, Optional
from dataclasses import dataclass
from enum import Enum

from ..base import BaseStrategy, Signal, PositionSide, BarData
from ..arbitrage import ArbitrageEngine
from ..trend import TrendEngine

# Telegram notifications
try:
    from src.common.telegram import TelegramNotifier
    _notifier = TelegramNotifier(check_trading_day=False)  # Always send during trading
except ImportError:
    _notifier = None

logger = logging.getLogger(__name__)


def _send_telegram(message: str) -> None:
    """Send Telegram notification (non-blocking)."""
    if _notifier:
        try:
            _notifier.send(message)
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")


class TradingMode(Enum):
    """Trading mode for state machine"""
    AVOID = "AVOID"      # Low liquidity - avoid trading
    MODE_A = "MODE_A"    # Arbitrage opportunity
    MODE_B = "MODE_B"    # Trend following


@dataclass
class DualModeConfig:
    """Configuration for Dual Mode Strategy"""
    # Liquidity thresholds
    liquidity_avoid_threshold: float = 50.0    # Below this = AVOID
    liquidity_mode_a_threshold: float = 80.0   # Above this = MODE_A eligible

    # Basis thresholds for MODE_A
    basis_threshold: float = 2.5  # Z-score threshold for arbitrage

    # MODE_A: Arbitrage settings
    arb_max_spread_ticks: int = 2
    arb_depth_multiplier: float = 5.0

    # MODE_B: Trend settings
    trend_dl_threshold: float = 0.85
    trend_ma_fast: int = 20
    trend_ma_slow: int = 60
    trend_atr_period: int = 14
    trend_atr_multiplier: float = 2.0
    trend_time_cut_minutes: int = 30

    # Order size
    order_size: float = 1.0


class DualModeStrategy(BaseStrategy):
    """
    Dual Mode Strategy with State Machine

    Automatically switches between:
    - MODE_A: Pure Basis Arbitrage (when basis gap is extreme)
    - MODE_B: Deep Learning Trend Following (normal conditions)
    - AVOID: When liquidity is too low
    """

    def __init__(self, config: Optional[DualModeConfig] = None):
        super().__init__(name="DualMode")
        self.config = config or DualModeConfig()

        # Initialize engines
        self.arb_engine = ArbitrageEngine(
            max_spread_ticks=self.config.arb_max_spread_ticks,
            depth_multiplier=self.config.arb_depth_multiplier,
            basis_threshold=self.config.basis_threshold,
            order_size=self.config.order_size,
        )

        self.trend_engine = TrendEngine(
            dl_threshold=self.config.trend_dl_threshold,
            ma_fast_period=self.config.trend_ma_fast,
            ma_slow_period=self.config.trend_ma_slow,
            atr_period=self.config.trend_atr_period,
            atr_stop_multiplier=self.config.trend_atr_multiplier,
            time_cut_minutes=self.config.trend_time_cut_minutes,
            order_size=self.config.order_size,
        )

        # Current mode
        self.current_mode = TradingMode.AVOID
        self._prev_mode = TradingMode.AVOID  # Track mode changes
        self.active_engine: Optional[str] = None  # "arb" or "trend"

        # Statistics
        self._stats = {
            'total_bars': 0,
            'mode_a_bars': 0,
            'mode_b_bars': 0,
            'avoid_bars': 0,
            'mode_a_signals': 0,
            'mode_b_signals': 0,
        }

        # Last notification time to avoid spam
        self._last_mode_notify = 0
        self._mode_notify_cooldown = 60  # seconds

    def _determine_mode(self, bar: BarData) -> TradingMode:
        """Determine trading mode based on current conditions."""
        # Check liquidity (use bid_ask_imbalance as proxy, or spread)
        spread = bar.spread if bar.spread > 0 else (bar.best_ask - bar.best_bid)

        # If spread is too wide, avoid
        if spread > 0.5:  # More than 0.5 point spread
            return TradingMode.AVOID

        # Check basis for arbitrage opportunity
        # Use OFI z-score as proxy for basis deviation
        if abs(bar.ofi_zscore) > self.config.basis_threshold:
            return TradingMode.MODE_A

        # Default to MODE_B (trend following)
        return TradingMode.MODE_B

    def generate_signal(self, bar: BarData) -> Signal:
        """Generate signal using state machine logic."""
        self._stats['total_bars'] += 1

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
            return Signal.HOLD

        elif mode == TradingMode.MODE_A:
            self._stats['mode_a_bars'] += 1
            return self._process_mode_a(bar)

        else:  # MODE_B
            self._stats['mode_b_bars'] += 1
            return self._process_mode_b(bar)

    def _notify_mode_change(self, prev: TradingMode, curr: TradingMode, bar: BarData) -> None:
        """Send Telegram notification for mode change."""
        mode_icons = {
            TradingMode.AVOID: "⏸️",
            TradingMode.MODE_A: "🎯",
            TradingMode.MODE_B: "📈",
        }
        msg = (
            f"{mode_icons.get(curr, '')} <b>Mode Change</b>\n"
            f"{prev.value} → {curr.value}\n"
            f"Price: {bar.close:.2f}\n"
            f"Spread: {bar.spread:.2f}\n"
            f"OFI Z: {bar.ofi_zscore:.2f}"
        )
        _send_telegram(msg)

    def _process_mode_a(self, bar: BarData) -> Signal:
        """Process MODE_A: Arbitrage."""
        # Update arbitrage engine with orderbook data
        self.arb_engine.update_orderbook(
            best_bid=bar.best_bid,
            best_ask=bar.best_ask,
            bid_qty=bar.bid_qty1 + bar.bid_qty2 + bar.bid_qty3,
            ask_qty=bar.ask_qty1 + bar.ask_qty2 + bar.ask_qty3,
        )

        # Check for arbitrage signal
        signal = self.arb_engine.check(
            current_price=bar.close,
            basis_zscore=bar.ofi_zscore,  # Using OFI z-score as basis proxy
            timestamp=bar.datetime.timestamp() if bar.datetime else time.time(),
        )

        if signal.action == "BUY":
            self._stats['mode_a_signals'] += 1
            self.active_engine = "arb"
            self._notify_signal("BUY", "MODE_A", bar, signal.reason)
            return Signal.BUY
        elif signal.action == "SELL":
            self._stats['mode_a_signals'] += 1
            self.active_engine = "arb"
            self._notify_signal("SELL", "MODE_A", bar, signal.reason)
            return Signal.SELL

        return Signal.HOLD

    def _process_mode_b(self, bar: BarData) -> Signal:
        """Process MODE_B: Trend Following."""
        # Update technical indicators with bar data
        self.trend_engine.update_bar(
            high=bar.high,
            low=bar.low,
            close=bar.close,
        )

        # Update bid/ask for execution
        self.trend_engine.update_prices(
            bid=bar.best_bid if bar.best_bid > 0 else bar.close,
            ask=bar.best_ask if bar.best_ask > 0 else bar.close,
        )

        # Check for trend signal
        signal = self.trend_engine.check(
            up_prob=bar.up_prob,
            current_price=bar.close,
            timestamp=bar.datetime.timestamp() if bar.datetime else time.time(),
        )

        if signal.action == "OPEN_LONG":
            self._stats['mode_b_signals'] += 1
            self.active_engine = "trend"
            self._notify_signal("BUY", "MODE_B", bar, f"LONG entry (DL: {bar.up_prob:.1%})")
            return Signal.BUY
        elif signal.action == "OPEN_SHORT":
            self._stats['mode_b_signals'] += 1
            self.active_engine = "trend"
            self._notify_signal("SELL", "MODE_B", bar, f"SHORT entry (DL: {1-bar.up_prob:.1%})")
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
        """Send Telegram notification for trading signal."""
        icon = "🟢" if action == "BUY" else "🔴"
        msg = (
            f"{icon} <b>{action} Signal</b> ({mode})\n"
            f"Price: {bar.close:.2f}\n"
            f"Reason: {reason}\n"
            f"Time: {datetime.now().strftime('%H:%M:%S')}"
        )
        _send_telegram(msg)
        logger.info(f"Signal: {action} @ {bar.close:.2f} ({mode}) - {reason}")

    def _notify_close(self, bar: BarData, reason: str) -> None:
        """Send Telegram notification for position close."""
        msg = (
            f"🔒 <b>Position Closed</b>\n"
            f"Price: {bar.close:.2f}\n"
            f"Reason: {reason}\n"
            f"Time: {datetime.now().strftime('%H:%M:%S')}"
        )
        _send_telegram(msg)
        logger.info(f"Close @ {bar.close:.2f} - {reason}")

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
            'arb_stats': self.arb_engine.get_stats(),
            'trend_stats': self.trend_engine.get_stats(),
        }

    def reset(self):
        """Reset strategy state."""
        super().reset()
        self.arb_engine.reset()
        self.trend_engine.reset()
        self.current_mode = TradingMode.AVOID
        self.active_engine = None
        for key in self._stats:
            if isinstance(self._stats[key], int):
                self._stats[key] = 0
