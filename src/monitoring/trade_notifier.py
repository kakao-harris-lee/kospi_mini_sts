"""
Trade Notifier for real-time trade execution alerts.

This module provides the TradeNotifier class that sends Telegram notifications
when trades are executed, including P&L information and daily tracking.

User Story 1: Real-time Telegram alerts with P&L when trades execute

Constitution Compliance:
- I: Async-first architecture (all methods are async)
"""

import asyncio
import logging
from datetime import date, datetime
from typing import Optional

from src.common.metrics import get_metrics
from src.monitoring.models import Alert, AlertType, TradeExecution
from src.monitoring.alert_service import AlertService

logger = logging.getLogger(__name__)


# Redis keys for daily tracking
REDIS_KEY_DAILY_PNL = "monitoring:trade:daily_pnl"
REDIS_KEY_LOSS_STREAK = "monitoring:trade:loss_streak"
REDIS_KEY_TRADE_COUNT = "monitoring:trade:count_today"


class TradeNotifier:
    """
    Trade notification service with P&L tracking.

    Features:
    - Real-time trade execution notifications via Telegram
    - Daily P&L tracking with Redis persistence
    - Loss streak monitoring with warning indicators
    - Queue-based delivery via AlertService

    Usage:
        notifier = TradeNotifier(alert_service, redis_client)

        trade = TradeExecution(
            symbol="A05601",
            side="BUY",
            quantity=1,
            price=350.0,
            strategy="pure_micro",
            realized_pnl=25000.0
        )
        await notifier.notify_trade(trade)
    """

    # Loss threshold for warning (in KRW)
    LOSS_WARNING_THRESHOLD = -500000  # -50만원
    LOSS_CRITICAL_THRESHOLD = -1000000  # -100만원

    # Point value for KOSPI 200 Mini Futures
    POINT_VALUE = 50000  # 1 point = 50,000 KRW

    def __init__(
        self,
        alert_service: AlertService,
        redis_client=None,
    ):
        """
        Initialize trade notifier.

        Args:
            alert_service: AlertService for queuing alerts
            redis_client: Redis client for daily P&L tracking (async)
        """
        self.alert_service = alert_service
        self.redis = redis_client
        self.metrics = get_metrics()

        # In-memory fallback if Redis not available
        self._daily_pnl: float = 0.0
        self._loss_streak: int = 0
        self._trade_count: int = 0
        self._last_reset_date: Optional[date] = None

    async def notify_trade(self, trade: TradeExecution) -> bool:
        """
        Send trade execution notification.

        Args:
            trade: Completed trade execution

        Returns:
            True if notification queued successfully
        """
        # Check if daily counters need reset
        await self._check_daily_reset()

        # Update daily P&L
        daily_pnl = await self._update_daily_pnl(trade.realized_pnl)
        loss_streak = await self._update_loss_streak(trade.realized_pnl)
        trade_count = await self._increment_trade_count()

        # Update metrics
        self.metrics.set_strategy_total_pnl(trade.strategy, trade.symbol, daily_pnl)
        self.metrics.set_loss_streak_count(trade.strategy, loss_streak)
        self.metrics.set_strategy_trade_count_today(trade.strategy, trade_count)

        # Format notification message
        message = self._format_trade_message(trade, daily_pnl, loss_streak, trade_count)

        # Determine priority based on loss level
        priority = self._determine_priority(daily_pnl, loss_streak)

        # Create and queue alert
        alert = Alert(
            alert_type=AlertType.TRADE_EXECUTION,
            channel="telegram",
            priority=priority,
            title=self._format_trade_title(trade),
            message=message,
            source_id=trade.id,
            source_type="TradeExecution",
        )

        success = await self.alert_service.queue_alert(alert)
        if success:
            logger.info(f"Trade notification queued: {trade.id}")
        else:
            logger.error(f"Failed to queue trade notification: {trade.id}")

        return success

    async def notify_trade_immediate(self, trade: TradeExecution) -> bool:
        """
        Send trade notification immediately (bypass queue).

        Use for urgent trades that require immediate notification.

        Args:
            trade: Completed trade execution

        Returns:
            True if notification sent successfully
        """
        await self._check_daily_reset()
        daily_pnl = await self._update_daily_pnl(trade.realized_pnl)
        loss_streak = await self._update_loss_streak(trade.realized_pnl)
        trade_count = await self._increment_trade_count()

        message = self._format_trade_message(trade, daily_pnl, loss_streak, trade_count)
        priority = self._determine_priority(daily_pnl, loss_streak)

        return await self.alert_service.send_immediate(
            alert_type=AlertType.TRADE_EXECUTION,
            title=self._format_trade_title(trade),
            message=message,
            priority=priority,
            source_id=trade.id,
            source_type="TradeExecution",
        )

    def _format_trade_title(self, trade: TradeExecution) -> str:
        """Format trade notification title."""
        # Direction emoji
        direction_emoji = "\ud83d\udfe2" if trade.side == "BUY" else "\ud83d\udd34"  # Green/Red circle

        # P&L indicator
        if trade.realized_pnl > 0:
            pnl_indicator = "\u2795"  # Plus
        elif trade.realized_pnl < 0:
            pnl_indicator = "\u2796"  # Minus
        else:
            pnl_indicator = "\u2796\u2795"  # Plus-minus

        return f"{direction_emoji} {trade.side} {trade.quantity}계약 {pnl_indicator}"

    def _format_trade_message(
        self,
        trade: TradeExecution,
        daily_pnl: float,
        loss_streak: int,
        trade_count: int,
    ) -> str:
        """
        Format trade notification message with HTML.

        Args:
            trade: Trade execution details
            daily_pnl: Current daily P&L
            loss_streak: Current consecutive loss count
            trade_count: Number of trades today

        Returns:
            HTML-formatted message
        """
        # P&L formatting with color indicators
        def format_pnl(pnl: float) -> str:
            if pnl > 0:
                return f"+{pnl:,.0f}"
            elif pnl < 0:
                return f"{pnl:,.0f}"
            else:
                return "0"

        # Loss warning indicator
        loss_warning = ""
        if daily_pnl <= self.LOSS_CRITICAL_THRESHOLD:
            loss_warning = "\n\n\ud83d\udea8 <b>CRITICAL: 일일 손실 한도 초과!</b>"
        elif daily_pnl <= self.LOSS_WARNING_THRESHOLD:
            loss_warning = "\n\n\u26a0\ufe0f <b>WARNING: 일일 손실 주의!</b>"

        # Loss streak warning
        streak_warning = ""
        if loss_streak >= 5:
            streak_warning = f"\n\ud83d\udd25 연속 손실: {loss_streak}회"

        # Build message
        lines = [
            f"<b>종목:</b> {trade.symbol}",
            f"<b>방향:</b> {trade.side}",
            f"<b>수량:</b> {trade.quantity}계약",
            f"<b>가격:</b> {trade.price:,.2f}",
            f"<b>전략:</b> {trade.strategy}",
            "",
            f"<b>실현손익:</b> {format_pnl(trade.realized_pnl)}원",
            f"<b>일일손익:</b> {format_pnl(daily_pnl)}원",
            f"<b>금일거래:</b> {trade_count}회",
        ]

        message = "\n".join(lines)

        if streak_warning:
            message += streak_warning
        if loss_warning:
            message += loss_warning

        # Add timestamp
        message += f"\n\n<i>{datetime.now().strftime('%H:%M:%S')}</i>"

        return message

    def _determine_priority(self, daily_pnl: float, loss_streak: int) -> str:
        """Determine alert priority based on P&L and loss streak."""
        if daily_pnl <= self.LOSS_CRITICAL_THRESHOLD:
            return "urgent"
        elif daily_pnl <= self.LOSS_WARNING_THRESHOLD or loss_streak >= 5:
            return "high"
        else:
            return "normal"

    async def _check_daily_reset(self) -> None:
        """Check if daily counters need to be reset."""
        today = date.today()

        if self._last_reset_date != today:
            self._daily_pnl = 0.0
            self._loss_streak = 0
            self._trade_count = 0
            self._last_reset_date = today

            # Also reset in Redis if available
            if self.redis:
                try:
                    await self.redis.delete(REDIS_KEY_DAILY_PNL)
                    await self.redis.delete(REDIS_KEY_LOSS_STREAK)
                    await self.redis.delete(REDIS_KEY_TRADE_COUNT)
                except Exception as e:
                    logger.error(f"Failed to reset Redis daily counters: {e}")

    async def _update_daily_pnl(self, pnl: float) -> float:
        """Update and return daily P&L."""
        self._daily_pnl += pnl

        if self.redis:
            try:
                result = await self.redis.incrbyfloat(REDIS_KEY_DAILY_PNL, pnl)
                self._daily_pnl = float(result)
                # Set TTL to expire at midnight
                await self.redis.expire(REDIS_KEY_DAILY_PNL, 86400)
            except Exception as e:
                logger.error(f"Failed to update Redis daily P&L: {e}")

        return self._daily_pnl

    async def _update_loss_streak(self, pnl: float) -> int:
        """Update and return loss streak count."""
        if pnl < 0:
            self._loss_streak += 1
        else:
            self._loss_streak = 0

        if self.redis:
            try:
                if pnl < 0:
                    result = await self.redis.incr(REDIS_KEY_LOSS_STREAK)
                    self._loss_streak = int(result)
                else:
                    await self.redis.set(REDIS_KEY_LOSS_STREAK, 0)
                    self._loss_streak = 0
                await self.redis.expire(REDIS_KEY_LOSS_STREAK, 86400)
            except Exception as e:
                logger.error(f"Failed to update Redis loss streak: {e}")

        return self._loss_streak

    async def _increment_trade_count(self) -> int:
        """Increment and return daily trade count."""
        self._trade_count += 1

        if self.redis:
            try:
                result = await self.redis.incr(REDIS_KEY_TRADE_COUNT)
                self._trade_count = int(result)
                await self.redis.expire(REDIS_KEY_TRADE_COUNT, 86400)
            except Exception as e:
                logger.error(f"Failed to update Redis trade count: {e}")

        return self._trade_count

    async def get_daily_stats(self) -> dict:
        """
        Get current daily trading statistics.

        Returns:
            Dictionary with daily_pnl, loss_streak, trade_count
        """
        await self._check_daily_reset()

        return {
            "daily_pnl": self._daily_pnl,
            "loss_streak": self._loss_streak,
            "trade_count": self._trade_count,
            "date": self._last_reset_date.isoformat() if self._last_reset_date else None,
        }

    async def send_daily_summary(self, strategy: str) -> bool:
        """
        Send end-of-day trading summary.

        Args:
            strategy: Strategy name

        Returns:
            True if notification sent successfully
        """
        stats = await self.get_daily_stats()

        # Format summary message
        pnl = stats["daily_pnl"]
        pnl_str = f"+{pnl:,.0f}" if pnl > 0 else f"{pnl:,.0f}"
        result_emoji = "\ud83d\udfe2" if pnl >= 0 else "\ud83d\udd34"  # Green/Red circle

        message = f"""<b>일일 거래 요약</b>

{result_emoji} <b>전략:</b> {strategy}
<b>일일 손익:</b> {pnl_str}원
<b>총 거래:</b> {stats['trade_count']}회
<b>최대 연속손실:</b> {stats['loss_streak']}회

<i>{datetime.now().strftime('%Y-%m-%d %H:%M')}</i>"""

        return await self.alert_service.send_immediate(
            alert_type=AlertType.DAILY_SUMMARY,
            title=f"일일 요약: {pnl_str}원",
            message=message,
            priority="normal",
        )
