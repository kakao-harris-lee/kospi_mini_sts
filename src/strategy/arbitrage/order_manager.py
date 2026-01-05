"""
Arbitrage Order Manager

Handles maker order execution with timeout-based cancellation.
No chasing - if order not filled within timeout, cancel and skip.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional
from collections import deque

from .arbitrage_engine import ArbitrageSignal

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    """Order lifecycle status"""
    PENDING = "PENDING"       # Created, not yet submitted
    SUBMITTED = "SUBMITTED"   # Sent to exchange
    PARTIAL = "PARTIAL"       # Partially filled
    FILLED = "FILLED"         # Completely filled
    CANCELLED = "CANCELLED"   # Cancelled (timeout or manual)
    REJECTED = "REJECTED"     # Rejected by exchange
    EXPIRED = "EXPIRED"       # Timeout expired


@dataclass
class PendingOrder:
    """Tracks a pending arbitrage order"""
    order_id: str
    signal: ArbitrageSignal
    submit_time: float
    timeout_sec: float
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    filled_price: float = 0.0
    cancel_time: Optional[float] = None
    fill_time: Optional[float] = None
    error_message: Optional[str] = None

    @property
    def is_active(self) -> bool:
        """Check if order is still active (can be filled or cancelled)"""
        return self.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL)

    @property
    def is_terminal(self) -> bool:
        """Check if order has reached terminal state"""
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED)

    @property
    def time_remaining(self) -> float:
        """Seconds remaining before timeout"""
        elapsed = time.time() - self.submit_time
        return max(0, self.timeout_sec - elapsed)

    @property
    def is_expired(self) -> bool:
        """Check if order has timed out"""
        return self.time_remaining <= 0 and self.is_active


@dataclass
class OrderStats:
    """Order execution statistics"""
    total_submitted: int = 0
    total_filled: int = 0
    total_partial: int = 0
    total_cancelled: int = 0
    total_timeout: int = 0
    total_rejected: int = 0
    total_contracts_traded: float = 0.0
    avg_fill_time_ms: float = 0.0
    fill_rate: float = 0.0

    def update_fill_rate(self):
        if self.total_submitted > 0:
            self.fill_rate = self.total_filled / self.total_submitted


class ArbitrageOrderManager:
    """
    Manages arbitrage order lifecycle with timeout cancellation.

    Key behaviors:
    1. Submit maker limit orders at best bid/ask
    2. Track order status with timeout
    3. Cancel unfilled orders after timeout (no chasing)
    4. Maintain position and order statistics
    """

    def __init__(
        self,
        timeout_sec: float = 10.0,
        max_pending_orders: int = 5,
        on_submit: Optional[Callable] = None,
        on_fill: Optional[Callable] = None,
        on_cancel: Optional[Callable] = None,
    ):
        """
        Args:
            timeout_sec: Default order timeout in seconds
            max_pending_orders: Maximum concurrent pending orders
            on_submit: Callback(order) when order is submitted
            on_fill: Callback(order) when order is filled
            on_cancel: Callback(order) when order is cancelled
        """
        self.timeout_sec = timeout_sec
        self.max_pending_orders = max_pending_orders

        # Callbacks
        self.on_submit = on_submit
        self.on_fill = on_fill
        self.on_cancel = on_cancel

        # Order tracking
        self._pending_orders: Dict[str, PendingOrder] = {}
        self._order_history: deque = deque(maxlen=1000)
        self._order_counter = 0

        # Statistics
        self.stats = OrderStats()

        # Position tracking (simple net position)
        self._position: float = 0.0

    def create_order(self, signal: ArbitrageSignal, timeout_sec: Optional[float] = None) -> Optional[PendingOrder]:
        """
        Create a new order from arbitrage signal.

        Args:
            signal: ArbitrageSignal from ArbitrageEngine
            timeout_sec: Override default timeout

        Returns:
            PendingOrder if created, None if rejected (e.g., max orders exceeded)
        """
        # Check pending order limit
        active_count = sum(1 for o in self._pending_orders.values() if o.is_active)
        if active_count >= self.max_pending_orders:
            logger.warning(f"Max pending orders ({self.max_pending_orders}) reached, rejecting new order")
            return None

        self._order_counter += 1
        order_id = f"ARB_{int(signal.timestamp)}_{self._order_counter}"

        order = PendingOrder(
            order_id=order_id,
            signal=signal,
            submit_time=time.time(),
            timeout_sec=timeout_sec or self.timeout_sec,
            status=OrderStatus.PENDING,
        )

        self._pending_orders[order_id] = order
        logger.info(
            f"Created order {order_id}: {signal.direction} {signal.order_size} @ {signal.entry_price:.2f}"
        )

        return order

    def submit_order(self, order: PendingOrder) -> bool:
        """
        Mark order as submitted to exchange.

        In production, this would call the actual exchange API.
        Returns True if submission successful.
        """
        if order.status != OrderStatus.PENDING:
            logger.warning(f"Cannot submit order {order.order_id}: status is {order.status}")
            return False

        order.status = OrderStatus.SUBMITTED
        order.submit_time = time.time()
        self.stats.total_submitted += 1

        logger.info(
            f"Submitted order {order.order_id}: {order.signal.direction} "
            f"{order.signal.order_size} @ {order.signal.entry_price:.2f}"
        )

        if self.on_submit:
            self.on_submit(order)

        return True

    def update_fill(
        self,
        order_id: str,
        filled_qty: float,
        filled_price: float,
        is_complete: bool = False
    ) -> bool:
        """
        Update order with fill information.

        Args:
            order_id: Order identifier
            filled_qty: Total filled quantity
            filled_price: Average fill price
            is_complete: True if order is completely filled

        Returns:
            True if update successful
        """
        order = self._pending_orders.get(order_id)
        if not order:
            logger.warning(f"Unknown order {order_id}")
            return False

        order.filled_qty = filled_qty
        order.filled_price = filled_price

        if is_complete:
            order.status = OrderStatus.FILLED
            order.fill_time = time.time()
            self.stats.total_filled += 1
            self.stats.total_contracts_traded += filled_qty

            # Update position
            if order.signal.direction == "BUY":
                self._position += filled_qty
            else:
                self._position -= filled_qty

            # Update average fill time
            fill_time_ms = (order.fill_time - order.submit_time) * 1000
            n = self.stats.total_filled
            self.stats.avg_fill_time_ms = (
                (self.stats.avg_fill_time_ms * (n - 1) + fill_time_ms) / n
            )

            logger.info(
                f"Filled order {order_id}: {filled_qty} @ {filled_price:.2f} "
                f"(fill_time={fill_time_ms:.0f}ms)"
            )

            if self.on_fill:
                self.on_fill(order)

            self._move_to_history(order_id)
        else:
            order.status = OrderStatus.PARTIAL
            self.stats.total_partial += 1

        self.stats.update_fill_rate()
        return True

    def cancel_order(self, order_id: str, reason: str = "manual") -> bool:
        """
        Cancel a pending order.

        Args:
            order_id: Order identifier
            reason: Cancellation reason

        Returns:
            True if cancellation successful
        """
        order = self._pending_orders.get(order_id)
        if not order:
            logger.warning(f"Unknown order {order_id}")
            return False

        if not order.is_active:
            logger.warning(f"Cannot cancel order {order_id}: status is {order.status}")
            return False

        if reason == "timeout":
            order.status = OrderStatus.EXPIRED
            self.stats.total_timeout += 1
        else:
            order.status = OrderStatus.CANCELLED
            self.stats.total_cancelled += 1

        order.cancel_time = time.time()
        order.error_message = reason

        logger.info(f"Cancelled order {order_id}: {reason}")

        if self.on_cancel:
            self.on_cancel(order)

        self._move_to_history(order_id)
        self.stats.update_fill_rate()
        return True

    def check_timeouts(self) -> List[str]:
        """
        Check all pending orders for timeout and cancel expired ones.

        Returns:
            List of cancelled order IDs
        """
        cancelled = []
        for order_id, order in list(self._pending_orders.items()):
            if order.is_expired:
                self.cancel_order(order_id, reason="timeout")
                cancelled.append(order_id)
        return cancelled

    def get_pending_orders(self) -> List[PendingOrder]:
        """Get all active pending orders."""
        return [o for o in self._pending_orders.values() if o.is_active]

    def get_order(self, order_id: str) -> Optional[PendingOrder]:
        """Get order by ID (pending or history)."""
        if order_id in self._pending_orders:
            return self._pending_orders[order_id]
        for order in self._order_history:
            if order.order_id == order_id:
                return order
        return None

    def get_position(self) -> float:
        """Get current net position."""
        return self._position

    def reset_position(self):
        """Reset position tracking (e.g., start of day)."""
        self._position = 0.0

    def _move_to_history(self, order_id: str):
        """Move completed order to history."""
        if order_id in self._pending_orders:
            order = self._pending_orders.pop(order_id)
            self._order_history.append(order)

    def get_stats(self) -> dict:
        """Get order statistics."""
        return {
            'total_submitted': self.stats.total_submitted,
            'total_filled': self.stats.total_filled,
            'total_cancelled': self.stats.total_cancelled,
            'total_timeout': self.stats.total_timeout,
            'fill_rate': f"{self.stats.fill_rate:.1%}",
            'avg_fill_time_ms': f"{self.stats.avg_fill_time_ms:.0f}",
            'total_contracts': self.stats.total_contracts_traded,
            'current_position': self._position,
            'pending_count': len(self.get_pending_orders()),
        }


class AsyncArbitrageOrderManager(ArbitrageOrderManager):
    """
    Async version with automatic timeout monitoring.

    Use this in async contexts for automatic order lifecycle management.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

    async def start_monitoring(self, check_interval: float = 0.5):
        """Start background timeout monitoring."""
        if self._running:
            return

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop(check_interval))
        logger.info("Started order timeout monitoring")

    async def stop_monitoring(self):
        """Stop background monitoring."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("Stopped order timeout monitoring")

    async def _monitor_loop(self, interval: float):
        """Background loop to check for order timeouts."""
        while self._running:
            try:
                cancelled = self.check_timeouts()
                if cancelled:
                    logger.debug(f"Timeout cancelled {len(cancelled)} orders")
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in timeout monitor: {e}")
                await asyncio.sleep(interval)


def create_order_from_signal(
    signal: ArbitrageSignal,
    order_manager: ArbitrageOrderManager,
    submit: bool = True
) -> Optional[PendingOrder]:
    """
    Helper function to create and optionally submit an order.

    Args:
        signal: ArbitrageSignal from ArbitrageEngine
        order_manager: ArbitrageOrderManager instance
        submit: Whether to submit immediately

    Returns:
        PendingOrder if successful, None otherwise
    """
    order = order_manager.create_order(signal)
    if order and submit:
        order_manager.submit_order(order)
    return order
