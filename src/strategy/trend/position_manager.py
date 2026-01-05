"""
Position Manager for MODE_B Trend Following

Manages:
- Position state tracking (entry price, time, side)
- ATR trailing stop
- Time cut (30 min no movement)
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class PositionSide(Enum):
    """Position direction"""
    FLAT = "FLAT"
    LONG = "LONG"
    SHORT = "SHORT"


class ExitReason(Enum):
    """Reason for position exit"""
    ATR_STOP = "ATR_STOP"
    TIME_CUT = "TIME_CUT"
    MANUAL = "MANUAL"
    SIGNAL = "SIGNAL"
    DL_REVERSAL = "DL_REVERSAL"  # Multi-horizon h10 reversal


@dataclass
class TrendPosition:
    """Active trend position state"""
    side: PositionSide
    entry_price: float
    entry_time: float         # Unix timestamp
    size: float
    atr_at_entry: float
    stop_price: float         # Current stop level
    highest_price: float      # For long trailing
    lowest_price: float       # For short trailing
    symbol: str = ""

    @property
    def is_long(self) -> bool:
        return self.side == PositionSide.LONG

    @property
    def is_short(self) -> bool:
        return self.side == PositionSide.SHORT

    def unrealized_pnl(self, current_price: float) -> float:
        """Calculate unrealized P&L in points"""
        if self.is_long:
            return current_price - self.entry_price
        elif self.is_short:
            return self.entry_price - current_price
        return 0.0


@dataclass
class ExitSignal:
    """Signal to exit position"""
    should_exit: bool
    reason: ExitReason
    message: str
    exit_price: float = 0.0


@dataclass
class PositionStats:
    """Position manager statistics"""
    total_positions: int = 0
    total_exits: int = 0
    atr_stop_exits: int = 0
    time_cut_exits: int = 0
    profitable_exits: int = 0
    total_pnl_points: float = 0.0


class TrendPositionManager:
    """
    Manages trend position lifecycle with ATR trailing stop and time cut.

    ATR Trailing Stop:
    - Initial stop: entry_price - 2×ATR (for long)
    - Trail: When price makes new high, move stop up to price - 2×ATR
    - Never move stop backwards
    - Exit immediately when price <= stop

    Time Cut:
    - Trigger: 30 minutes after entry
    - Condition: Price hasn't moved 0.5×ATR in favorable direction
    - Action: Exit immediately
    """

    def __init__(
        self,
        atr_stop_multiplier: float = 2.0,
        time_cut_minutes: int = 30,
        time_cut_atr_threshold: float = 0.5,
    ):
        """
        Args:
            atr_stop_multiplier: ATR multiplier for stop (default 2.0)
            time_cut_minutes: Minutes before time cut check (default 30)
            time_cut_atr_threshold: Min favorable move as ATR multiple (default 0.5)
        """
        self.atr_stop_multiplier = atr_stop_multiplier
        self.time_cut_minutes = time_cut_minutes
        self.time_cut_atr_threshold = time_cut_atr_threshold

        self._position: Optional[TrendPosition] = None
        self.stats = PositionStats()

    @property
    def has_position(self) -> bool:
        """Check if there's an active position"""
        return self._position is not None

    @property
    def position(self) -> Optional[TrendPosition]:
        """Get current position"""
        return self._position

    @property
    def position_side(self) -> PositionSide:
        """Get current position side"""
        return self._position.side if self._position else PositionSide.FLAT

    def open_position(
        self,
        side: str,
        entry_price: float,
        size: float,
        atr: float,
        symbol: str = "",
        timestamp: Optional[float] = None,
    ) -> TrendPosition:
        """
        Open a new position.

        Args:
            side: "LONG" or "SHORT"
            entry_price: Entry price
            size: Position size
            atr: Current ATR for stop calculation
            symbol: Trading symbol
            timestamp: Entry timestamp (default: now)

        Returns:
            Created TrendPosition
        """
        if self.has_position:
            logger.warning("Position already exists, closing first")
            self.close_position(entry_price)

        timestamp = timestamp or time.time()
        position_side = PositionSide.LONG if side == "LONG" else PositionSide.SHORT

        # Calculate initial stop
        if position_side == PositionSide.LONG:
            stop_price = entry_price - self.atr_stop_multiplier * atr
        else:
            stop_price = entry_price + self.atr_stop_multiplier * atr

        self._position = TrendPosition(
            side=position_side,
            entry_price=entry_price,
            entry_time=timestamp,
            size=size,
            atr_at_entry=atr,
            stop_price=stop_price,
            highest_price=entry_price,
            lowest_price=entry_price,
            symbol=symbol,
        )

        self.stats.total_positions += 1

        logger.info(
            f"Opened {side} position: entry={entry_price:.2f}, "
            f"stop={stop_price:.2f}, ATR={atr:.2f}"
        )

        return self._position

    def update_and_check_exit(
        self,
        current_price: float,
        current_atr: float,
        current_time: Optional[float] = None,
    ) -> ExitSignal:
        """
        Update position tracking and check for exit conditions.

        Called on every tick/bar.

        Args:
            current_price: Current market price
            current_atr: Current ATR value
            current_time: Current timestamp (default: now)

        Returns:
            ExitSignal indicating if position should be closed
        """
        if not self.has_position:
            return ExitSignal(
                should_exit=False,
                reason=ExitReason.MANUAL,
                message="No position",
            )

        current_time = current_time or time.time()
        pos = self._position

        # Update extreme prices and trail stop
        self._update_trailing_stop(current_price, current_atr)

        # Check 1: ATR Stop
        stop_signal = self._check_atr_stop(current_price)
        if stop_signal.should_exit:
            return stop_signal

        # Check 2: Time Cut
        time_signal = self._check_time_cut(current_price, current_time)
        if time_signal.should_exit:
            return time_signal

        return ExitSignal(
            should_exit=False,
            reason=ExitReason.MANUAL,
            message="Position OK",
        )

    def _update_trailing_stop(self, current_price: float, current_atr: float):
        """Update trailing stop based on price movement."""
        pos = self._position

        if pos.is_long:
            # Update highest price
            if current_price > pos.highest_price:
                pos.highest_price = current_price
                # Trail stop up (never down)
                new_stop = current_price - self.atr_stop_multiplier * current_atr
                if new_stop > pos.stop_price:
                    old_stop = pos.stop_price
                    pos.stop_price = new_stop
                    logger.debug(
                        f"Trailing stop up: {old_stop:.2f} -> {new_stop:.2f}"
                    )

        elif pos.is_short:
            # Update lowest price
            if current_price < pos.lowest_price:
                pos.lowest_price = current_price
                # Trail stop down (never up)
                new_stop = current_price + self.atr_stop_multiplier * current_atr
                if new_stop < pos.stop_price:
                    old_stop = pos.stop_price
                    pos.stop_price = new_stop
                    logger.debug(
                        f"Trailing stop down: {old_stop:.2f} -> {new_stop:.2f}"
                    )

    def _check_atr_stop(self, current_price: float) -> ExitSignal:
        """Check if ATR stop is hit."""
        pos = self._position

        if pos.is_long and current_price <= pos.stop_price:
            return ExitSignal(
                should_exit=True,
                reason=ExitReason.ATR_STOP,
                message=f"ATR stop hit: {current_price:.2f} <= {pos.stop_price:.2f}",
                exit_price=current_price,
            )

        if pos.is_short and current_price >= pos.stop_price:
            return ExitSignal(
                should_exit=True,
                reason=ExitReason.ATR_STOP,
                message=f"ATR stop hit: {current_price:.2f} >= {pos.stop_price:.2f}",
                exit_price=current_price,
            )

        return ExitSignal(
            should_exit=False,
            reason=ExitReason.ATR_STOP,
            message="Stop not hit",
        )

    def _check_time_cut(self, current_price: float, current_time: float) -> ExitSignal:
        """Check if time cut condition is met."""
        pos = self._position
        elapsed_seconds = current_time - pos.entry_time
        elapsed_minutes = elapsed_seconds / 60

        # Only check after threshold time
        if elapsed_minutes < self.time_cut_minutes:
            return ExitSignal(
                should_exit=False,
                reason=ExitReason.TIME_CUT,
                message=f"Time: {elapsed_minutes:.0f}/{self.time_cut_minutes} min",
            )

        # Check if price moved enough in favorable direction
        min_move = self.time_cut_atr_threshold * pos.atr_at_entry

        if pos.is_long:
            favorable_move = current_price - pos.entry_price
        else:
            favorable_move = pos.entry_price - current_price

        if favorable_move < min_move:
            return ExitSignal(
                should_exit=True,
                reason=ExitReason.TIME_CUT,
                message=(
                    f"Time cut: {elapsed_minutes:.0f}min elapsed, "
                    f"move {favorable_move:.2f} < required {min_move:.2f}"
                ),
                exit_price=current_price,
            )

        return ExitSignal(
            should_exit=False,
            reason=ExitReason.TIME_CUT,
            message="Sufficient movement",
        )

    def close_position(self, exit_price: float, reason: ExitReason = ExitReason.MANUAL) -> float:
        """
        Close the current position.

        Args:
            exit_price: Exit price
            reason: Reason for exit

        Returns:
            Realized P&L in points
        """
        if not self.has_position:
            return 0.0

        pos = self._position
        pnl = pos.unrealized_pnl(exit_price)

        # Update stats
        self.stats.total_exits += 1
        self.stats.total_pnl_points += pnl

        if reason == ExitReason.ATR_STOP:
            self.stats.atr_stop_exits += 1
        elif reason == ExitReason.TIME_CUT:
            self.stats.time_cut_exits += 1

        if pnl > 0:
            self.stats.profitable_exits += 1

        logger.info(
            f"Closed {pos.side.value} position: "
            f"entry={pos.entry_price:.2f}, exit={exit_price:.2f}, "
            f"PnL={pnl:.2f}, reason={reason.value}"
        )

        self._position = None
        return pnl

    def get_position_info(self) -> dict:
        """Get current position information."""
        if not self.has_position:
            return {'has_position': False, 'side': 'FLAT'}

        pos = self._position
        elapsed = (time.time() - pos.entry_time) / 60

        return {
            'has_position': True,
            'side': pos.side.value,
            'entry_price': pos.entry_price,
            'current_stop': pos.stop_price,
            'highest_price': pos.highest_price,
            'lowest_price': pos.lowest_price,
            'elapsed_minutes': elapsed,
            'atr_at_entry': pos.atr_at_entry,
            'size': pos.size,
            'symbol': pos.symbol,
        }

    def get_stats(self) -> dict:
        """Get position manager statistics."""
        total = self.stats.total_exits
        return {
            'total_positions': self.stats.total_positions,
            'total_exits': total,
            'atr_stop_exits': self.stats.atr_stop_exits,
            'time_cut_exits': self.stats.time_cut_exits,
            'profitable_exits': self.stats.profitable_exits,
            'win_rate': self.stats.profitable_exits / total if total > 0 else 0.0,
            'total_pnl_points': self.stats.total_pnl_points,
            'avg_pnl': self.stats.total_pnl_points / total if total > 0 else 0.0,
        }

    def reset(self):
        """Reset position and statistics."""
        self._position = None
        self.stats = PositionStats()
