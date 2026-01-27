"""
Decision logging module for trading analysis.

Logs all trading decisions with full context to:
- JSON files (logs/decisions/YYYY-MM-DD.jsonl)
- ClickHouse (kospi.trading_decisions)
- Telegram (throttled, detailed format)
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

from .telegram import TelegramNotifier

logger = logging.getLogger(__name__)


@dataclass
class DecisionLog:
    """Full context for a trading decision."""

    # Core
    timestamp: datetime
    signal: str  # BUY, SELL, CLOSE
    price: float
    mode: str  # MODE_B
    reason: str

    # DL Probabilities
    up_prob_h1: float = 0.5
    up_prob_h3: float = 0.5
    up_prob_h5: float = 0.5
    up_prob_h10: float = 0.5

    # Z-scores (calibrated)
    zscore_h1: float = 0.0
    zscore_h10: float = 0.0

    # Technical indicators
    ma_fast: float = 0.0
    ma_slow: float = 0.0
    ma_bullish: bool = False
    rsi: float = 50.0
    atr: float = 0.0
    cloud_top: float = 0.0
    cloud_bottom: float = 0.0
    above_cloud: bool = False

    # Market context
    spread: float = 0.0
    ofi_zscore: float = 0.0
    regime: str = "unknown"

    # Position tracking
    trade_id: str = ""
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    pnl_points: Optional[float] = None
    hold_time_minutes: Optional[int] = None

    # Metadata
    strategy: str = "dual_mode"
    session_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        d = asdict(self)
        d['timestamp'] = self.timestamp.isoformat()
        return d


class DecisionLogger:
    """
    Logs trading decisions to multiple backends.

    Usage:
        logger = DecisionLogger(session_id="paper_20260108")
        logger.log_entry(decision_log)
        logger.log_close(decision_log)
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        log_dir: str = "logs/decisions",
        enable_telegram: bool = True,
        enable_clickhouse: bool = True,
        telegram_cooldown: float = 30.0,
    ):
        """
        Initialize decision logger.

        Args:
            session_id: Unique session identifier
            log_dir: Directory for JSON log files
            enable_telegram: Send Telegram notifications
            enable_clickhouse: Write to ClickHouse
            telegram_cooldown: Minimum seconds between Telegram messages
        """
        self.session_id = session_id or f"session_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.enable_telegram = enable_telegram
        self.enable_clickhouse = enable_clickhouse
        self.telegram_cooldown = telegram_cooldown

        # Telegram throttling
        self._last_telegram_time = 0.0
        self._telegram_queue: list = []
        self._notifier = TelegramNotifier() if enable_telegram else None

        # ClickHouse client
        self._ch_client = None
        if enable_clickhouse:
            self._init_clickhouse()

        # Trade tracking
        self._active_trade_id: Optional[str] = None
        self._entry_log: Optional[DecisionLog] = None

        # Daily stats
        self._daily_trades = 0
        self._daily_pnl = 0.0
        self._daily_wins = 0

        logger.info(f"DecisionLogger initialized: session={self.session_id}")

    def _init_clickhouse(self) -> None:
        """Initialize ClickHouse client."""
        try:
            from dotenv import load_dotenv
            load_dotenv()

            import clickhouse_connect
            self._ch_client = clickhouse_connect.get_client(
                host=os.getenv('CLICKHOUSE_HOST', 'localhost'),
                port=int(os.getenv('CLICKHOUSE_PORT', '8123')),
                username=os.getenv('CLICKHOUSE_USER', 'default'),
                password=os.getenv('CLICKHOUSE_PASSWORD', ''),
                database='kospi'
            )
            logger.info("ClickHouse client connected")
        except Exception as e:
            logger.warning(f"ClickHouse connection failed: {e}")
            self._ch_client = None

    def generate_trade_id(self) -> str:
        """Generate unique trade ID."""
        return f"t_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    def log_entry(self, decision: DecisionLog) -> None:
        """
        Log a position entry (BUY or SELL).

        Args:
            decision: DecisionLog with entry details
        """
        decision.session_id = self.session_id
        decision.trade_id = self.generate_trade_id()
        decision.entry_price = decision.price

        self._active_trade_id = decision.trade_id
        self._entry_log = decision

        # Write to all backends
        self._write_json(decision)
        self._write_clickhouse(decision)
        self._send_telegram_entry(decision)

        logger.info(f"Entry logged: {decision.signal} @ {decision.price} [{decision.trade_id}]")

    def log_close(
        self,
        decision: DecisionLog,
        entry_price: Optional[float] = None,
        entry_time: Optional[datetime] = None,
    ) -> None:
        """
        Log a position close.

        Args:
            decision: DecisionLog with close details
            entry_price: Override entry price (if not using active trade)
            entry_time: Override entry time for hold time calculation
        """
        decision.session_id = self.session_id
        decision.signal = "CLOSE"

        # Link to entry
        if self._active_trade_id and self._entry_log:
            decision.trade_id = self._active_trade_id
            decision.entry_price = self._entry_log.entry_price
            entry_time = self._entry_log.timestamp
        elif entry_price:
            decision.entry_price = entry_price
            decision.trade_id = self.generate_trade_id()

        decision.exit_price = decision.price

        # Calculate PnL
        if decision.entry_price:
            # Determine direction from entry
            if self._entry_log and self._entry_log.signal == "SELL":
                decision.pnl_points = decision.entry_price - decision.exit_price
            else:
                decision.pnl_points = decision.exit_price - decision.entry_price

        # Calculate hold time
        if entry_time:
            hold_delta = decision.timestamp - entry_time
            decision.hold_time_minutes = int(hold_delta.total_seconds() / 60)

        # Write to all backends
        self._write_json(decision)
        self._write_clickhouse(decision)
        self._send_telegram_close(decision)

        # Update daily stats
        self._daily_trades += 1
        if decision.pnl_points:
            self._daily_pnl += decision.pnl_points
            if decision.pnl_points > 0:
                self._daily_wins += 1

        # Clear active trade
        self._active_trade_id = None
        self._entry_log = None

        pnl_str = f"{decision.pnl_points:.2f}" if decision.pnl_points is not None else "N/A"
        logger.info(f"Close logged: {decision.exit_price} PnL={pnl_str}pts [{decision.trade_id}]")

    def log_mode_change(self, prev_mode: str, new_mode: str, bar_data: Dict[str, Any]) -> None:
        """Log a mode change event."""
        decision = DecisionLog(
            timestamp=datetime.now(timezone.utc),
            signal="MODE_CHANGE",
            price=bar_data.get('close', 0.0),
            mode=new_mode,
            reason=f"{prev_mode} -> {new_mode}",
            spread=bar_data.get('spread', 0.0),
            ofi_zscore=bar_data.get('ofi_zscore', 0.0),
            session_id=self.session_id,
        )
        self._write_json(decision)
        # Mode changes handled by existing DualModeStrategy telegram

    def log_rejected(self, decision: DecisionLog, reject_reason: str) -> None:
        """Log a rejected signal (filter blocked)."""
        decision.session_id = self.session_id
        decision.reason = f"REJECTED: {reject_reason}"
        self._write_json(decision)
        # No Telegram for rejected signals

    def _write_json(self, decision: DecisionLog) -> None:
        """Write decision to daily JSON file."""
        try:
            date_str = decision.timestamp.strftime('%Y-%m-%d')
            file_path = self.log_dir / f"{date_str}.jsonl"

            with open(file_path, 'a') as f:
                f.write(json.dumps(decision.to_dict()) + '\n')
        except Exception as e:
            logger.error(f"JSON write failed: {e}")

    def _write_clickhouse(self, decision: DecisionLog) -> None:
        """Write decision to ClickHouse."""
        if not self._ch_client:
            return

        try:
            data = {
                'timestamp': decision.timestamp,
                'signal': decision.signal,
                'price': decision.price,
                'mode': decision.mode,
                'reason': decision.reason,
                'up_prob_h1': decision.up_prob_h1,
                'up_prob_h3': decision.up_prob_h3,
                'up_prob_h5': decision.up_prob_h5,
                'up_prob_h10': decision.up_prob_h10,
                'zscore_h1': decision.zscore_h1,
                'zscore_h10': decision.zscore_h10,
                'ma_fast': decision.ma_fast,
                'ma_slow': decision.ma_slow,
                'ma_bullish': 1 if decision.ma_bullish else 0,
                'rsi': decision.rsi,
                'atr': decision.atr,
                'cloud_top': decision.cloud_top,
                'cloud_bottom': decision.cloud_bottom,
                'above_cloud': 1 if decision.above_cloud else 0,
                'spread': decision.spread,
                'ofi_zscore': decision.ofi_zscore,
                'regime': decision.regime or 'unknown',
                'trade_id': decision.trade_id,
                'entry_price': decision.entry_price or 0.0,
                'exit_price': decision.exit_price or 0.0,
                'pnl_points': decision.pnl_points or 0.0,
                'hold_time_minutes': decision.hold_time_minutes or 0,
                'strategy': decision.strategy,
                'session_id': decision.session_id,
            }

            self._ch_client.insert(
                'trading_decisions',
                [list(data.values())],
                column_names=list(data.keys())
            )
        except Exception as e:
            logger.error(f"ClickHouse write failed: {e}")

    def _can_send_telegram(self) -> bool:
        """Check if we can send Telegram (throttling)."""
        now = time.time()
        return (now - self._last_telegram_time) >= self.telegram_cooldown

    def _format_telegram_entry(self, decision: DecisionLog) -> str:
        """Format detailed Telegram message for entry."""
        icon = "🟢" if decision.signal == "BUY" else "🔴"
        action = "LONG" if decision.signal == "BUY" else "SHORT"

        # Technical checks
        ma_check = "✓" if decision.ma_bullish else "✗"
        cloud_check = "✓" if decision.above_cloud else "✗"

        msg = (
            f"{icon} <b>{action} @ {decision.price:.2f}</b> | {decision.mode}\n"
            f"─────────────\n"
            f"DL: h1={decision.up_prob_h1:.2f} h3={decision.up_prob_h3:.2f} "
            f"h5={decision.up_prob_h5:.2f} h10={decision.up_prob_h10:.2f}\n"
            f"Z: h1={decision.zscore_h1:.1f} h10={decision.zscore_h10:.1f}\n"
            f"Tech: MA{ma_check} RSI={decision.rsi:.0f} ATR={decision.atr:.2f}\n"
            f"Ichimoku: {'Above' if decision.above_cloud else 'Below'} cloud {cloud_check}\n"
            f"─────────────\n"
            f"<i>{decision.reason}</i>"
        )
        return msg

    def _format_telegram_close(self, decision: DecisionLog) -> str:
        """Format detailed Telegram message for close."""
        pnl = decision.pnl_points or 0.0
        pnl_icon = "💰" if pnl > 0 else "💸" if pnl < 0 else "➖"
        pnl_krw = pnl * 50000  # Point value

        hold_str = f"{decision.hold_time_minutes}m" if decision.hold_time_minutes else "?"

        msg = (
            f"🔒 <b>CLOSE @ {decision.price:.2f}</b>\n"
            f"─────────────\n"
            f"{pnl_icon} PnL: {pnl:+.2f}pts ({pnl_krw:+,.0f}원)\n"
            f"Entry: {decision.entry_price:.2f} → Exit: {decision.exit_price:.2f}\n"
            f"Hold: {hold_str}\n"
            f"─────────────\n"
            f"<i>{decision.reason}</i>"
        )
        return msg

    def _send_telegram_entry(self, decision: DecisionLog) -> None:
        """Send Telegram for entry (with throttling)."""
        if not self._notifier or not self.enable_telegram:
            return

        if not self._can_send_telegram():
            logger.debug("Telegram throttled")
            return

        msg = self._format_telegram_entry(decision)
        if self._notifier.send(msg):
            self._last_telegram_time = time.time()

    def _send_telegram_close(self, decision: DecisionLog) -> None:
        """Send Telegram for close (with throttling)."""
        if not self._notifier or not self.enable_telegram:
            return

        if not self._can_send_telegram():
            logger.debug("Telegram throttled")
            return

        msg = self._format_telegram_close(decision)
        if self._notifier.send(msg):
            self._last_telegram_time = time.time()

    def send_daily_summary(self) -> None:
        """Send daily summary to Telegram."""
        if not self._notifier or self._daily_trades == 0:
            return

        win_rate = (self._daily_wins / self._daily_trades * 100) if self._daily_trades > 0 else 0
        pnl_krw = self._daily_pnl * 50000

        msg = (
            f"📊 <b>Daily Summary</b>\n"
            f"─────────────\n"
            f"Trades: {self._daily_trades}\n"
            f"Win Rate: {win_rate:.1f}%\n"
            f"PnL: {self._daily_pnl:+.2f}pts ({pnl_krw:+,.0f}원)\n"
            f"Session: {self.session_id}"
        )
        self._notifier.send(msg, force=True)

    def reset_daily_stats(self) -> None:
        """Reset daily statistics."""
        self._daily_trades = 0
        self._daily_pnl = 0.0
        self._daily_wins = 0


# Convenience functions
_default_logger: Optional[DecisionLogger] = None


def get_decision_logger(session_id: Optional[str] = None) -> DecisionLogger:
    """Get or create default decision logger."""
    global _default_logger
    if _default_logger is None:
        _default_logger = DecisionLogger(session_id=session_id)
    return _default_logger


def log_entry(decision: DecisionLog) -> None:
    """Log entry using default logger."""
    get_decision_logger().log_entry(decision)


def log_close(decision: DecisionLog, **kwargs) -> None:
    """Log close using default logger."""
    get_decision_logger().log_close(decision, **kwargs)
