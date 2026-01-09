"""
Alert Service for the monitoring system.

This module provides the AlertService class that manages alert delivery
with queue-based processing, retry logic, and Prometheus metrics.

Constitution Compliance:
- I: Async-first architecture (all methods are async)
- III: Batch-only database operations (alert audit logging is batched)
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional, List

from src.common.telegram import TelegramNotifier
from src.common.metrics import get_metrics
from src.monitoring.models import Alert, AlertType, DeliveryStatus

logger = logging.getLogger(__name__)


class AlertService:
    """
    Alert service with queue-based delivery and retry logic.

    Features:
    - Async delivery loop for non-blocking alert processing
    - Exponential backoff retry strategy (10s, 30s, 90s)
    - Prometheus metrics for monitoring alert delivery
    - Redis-backed queue for persistence (via AlertQueue)

    Usage:
        service = AlertService(redis_client)
        await service.start()

        # Queue an alert
        alert = Alert(
            alert_type=AlertType.TRADE_EXECUTION,
            channel="telegram",
            priority="normal",
            title="Trade Executed",
            message="BUY 1 A05601 @ 350.00"
        )
        await service.queue_alert(alert)

        # Stop when done
        await service.stop()
    """

    MAX_RETRIES = 3
    RETRY_DELAYS = [10, 30, 90]  # seconds
    BATCH_SIZE = 100  # For ClickHouse audit logging

    def __init__(
        self,
        redis_client=None,
        clickhouse_client=None,
        notifier: Optional[TelegramNotifier] = None,
        check_trading_day: bool = True,
    ):
        """
        Initialize alert service.

        Args:
            redis_client: Redis client instance (async). Optional for in-memory queue.
            clickhouse_client: ClickHouse client for audit logging.
            notifier: TelegramNotifier instance (default: creates new one)
            check_trading_day: If True, skip sending on non-trading days
        """
        self.redis = redis_client
        self.clickhouse = clickhouse_client
        self.notifier = notifier or TelegramNotifier(check_trading_day=check_trading_day)
        self.metrics = get_metrics()

        # In-memory queue for alerts
        self._queue: asyncio.Queue[Alert] = asyncio.Queue()
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Batch buffer for audit logging
        self._audit_buffer: List[Alert] = []
        self._audit_lock = asyncio.Lock()

        # Redis keys
        self.PENDING_KEY = "alerts:pending"
        self.FAILED_KEY = "alerts:failed"

    async def start(self) -> None:
        """Start the alert delivery loop."""
        if self._running:
            logger.warning("AlertService already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._delivery_loop())
        logger.info("AlertService started")

    async def stop(self) -> None:
        """Stop the alert delivery loop gracefully."""
        if not self._running:
            return

        self._running = False

        # Process remaining alerts in queue
        while not self._queue.empty():
            try:
                alert = self._queue.get_nowait()
                await self._deliver_alert(alert)
            except asyncio.QueueEmpty:
                break

        # Flush audit buffer
        await self._flush_audit_buffer()

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info("AlertService stopped")

    async def queue_alert(self, alert: Alert) -> bool:
        """
        Add alert to the delivery queue.

        Args:
            alert: Alert to queue

        Returns:
            True if queued successfully
        """
        try:
            await self._queue.put(alert)
            self.metrics.set_alerts_pending(self._queue.qsize())
            logger.debug(f"Alert queued: {alert.id} ({alert.alert_type.value})")
            return True
        except Exception as e:
            logger.error(f"Failed to queue alert: {e}")
            return False

    async def send_immediate(
        self,
        alert_type: AlertType,
        title: str,
        message: str,
        priority: str = "normal",
        source_id: Optional[str] = None,
        source_type: Optional[str] = None,
    ) -> bool:
        """
        Send an alert immediately (bypassing the queue).

        Use for urgent alerts that must be delivered without delay.

        Args:
            alert_type: Type of alert
            title: Alert title
            message: Alert message body
            priority: Alert priority (low, normal, high, urgent)
            source_id: ID of the source entity
            source_type: Type of the source entity

        Returns:
            True if sent successfully
        """
        alert = Alert(
            alert_type=alert_type,
            channel="telegram",
            priority=priority,
            title=title,
            message=message,
            source_id=source_id,
            source_type=source_type,
        )
        return await self._deliver_alert(alert)

    async def _delivery_loop(self) -> None:
        """Main delivery loop that processes alerts from the queue."""
        logger.info("Alert delivery loop started")

        while self._running:
            try:
                # Wait for alert with timeout to allow periodic checks
                try:
                    alert = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                # Update pending count
                self.metrics.set_alerts_pending(self._queue.qsize())

                # Deliver the alert
                await self._deliver_alert(alert)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in alert delivery loop: {e}")
                await asyncio.sleep(1.0)

        logger.info("Alert delivery loop stopped")

    async def _deliver_alert(self, alert: Alert) -> bool:
        """
        Deliver a single alert.

        Args:
            alert: Alert to deliver

        Returns:
            True if delivered successfully
        """
        start_time = time.monotonic()

        try:
            # Format message for Telegram
            text = self._format_alert_message(alert)

            # Determine if we should force send (urgent alerts ignore trading day check)
            force = alert.priority == "urgent"

            # Send via Telegram
            success = await self.notifier.send_async(text, force=force)

            # Calculate latency
            latency = time.monotonic() - start_time

            if success:
                alert.mark_sent()
                self.metrics.record_alert_sent(
                    alert_type=alert.alert_type.value,
                    channel=alert.channel,
                    status="success"
                )
                self.metrics.observe_alert_delivery_latency(alert.channel, latency)
                logger.info(f"Alert {alert.id} delivered successfully")
            else:
                await self._handle_delivery_failure(alert, "Telegram API returned failure")

            # Add to audit buffer
            await self._add_to_audit_buffer(alert)

            return success

        except Exception as e:
            error_msg = str(e)
            await self._handle_delivery_failure(alert, error_msg)
            await self._add_to_audit_buffer(alert)
            return False

    async def _handle_delivery_failure(self, alert: Alert, error: str) -> None:
        """Handle a failed alert delivery."""
        alert.mark_failed(error)

        self.metrics.record_alert_sent(
            alert_type=alert.alert_type.value,
            channel=alert.channel,
            status="failed"
        )
        self.metrics.record_alert_retry(alert.alert_type.value)

        if alert.delivery_status == DeliveryStatus.DEAD_LETTER:
            logger.warning(f"Alert {alert.id} exceeded max retries, moving to dead letter")
            if self.redis:
                await self.redis.lpush(self.FAILED_KEY, alert.to_json())
        else:
            # Schedule retry with exponential backoff
            delay = self.RETRY_DELAYS[min(alert.retry_count - 1, len(self.RETRY_DELAYS) - 1)]
            logger.info(f"Retrying alert {alert.id} in {delay}s (attempt {alert.retry_count})")
            asyncio.create_task(self._schedule_retry(alert, delay))

    async def _schedule_retry(self, alert: Alert, delay: float) -> None:
        """Schedule a retry for a failed alert."""
        await asyncio.sleep(delay)
        if self._running:
            # Reset status for retry
            alert.delivery_status = DeliveryStatus.PENDING
            await self._queue.put(alert)
            self.metrics.set_alerts_pending(self._queue.qsize())

    def _format_alert_message(self, alert: Alert) -> str:
        """Format alert for Telegram delivery."""
        # Priority emoji
        priority_emoji = {
            "low": "",
            "normal": "",
            "high": "\u26a0\ufe0f ",  # Warning sign
            "urgent": "\ud83d\udea8 ",  # Police car light
        }.get(alert.priority, "")

        # Alert type icon
        type_icon = {
            AlertType.TRADE_EXECUTION: "\ud83d\udcb9",  # Chart
            AlertType.HEALTH_WARNING: "\u26a0\ufe0f",  # Warning
            AlertType.HEALTH_CRITICAL: "\ud83d\uded1",  # Stop sign
            AlertType.ANOMALY_DETECTED: "\ud83d\udd0d",  # Magnifying glass
            AlertType.DAILY_SUMMARY: "\ud83d\udcca",  # Bar chart
            AlertType.SYSTEM_START: "\u25b6\ufe0f",  # Play
            AlertType.SYSTEM_STOP: "\u23f9\ufe0f",  # Stop
        }.get(alert.alert_type, "\ud83d\udce2")  # Loudspeaker default

        return f"{priority_emoji}{type_icon} <b>{alert.title}</b>\n{alert.message}"

    async def _add_to_audit_buffer(self, alert: Alert) -> None:
        """Add alert to audit buffer for batch insertion."""
        async with self._audit_lock:
            self._audit_buffer.append(alert)

            if len(self._audit_buffer) >= self.BATCH_SIZE:
                await self._flush_audit_buffer()

    async def _flush_audit_buffer(self) -> None:
        """Flush audit buffer to ClickHouse (T064)."""
        async with self._audit_lock:
            if not self._audit_buffer:
                return

            alerts_to_flush = self._audit_buffer.copy()
            self._audit_buffer.clear()

        if not self.clickhouse:
            logger.debug(f"Would flush {len(alerts_to_flush)} alerts to ClickHouse audit log")
            return

        try:
            # Convert alerts to ClickHouse rows
            rows = []
            for alert in alerts_to_flush:
                rows.append((
                    alert.id,
                    alert.created_at,
                    alert.alert_type.value,
                    alert.channel,
                    alert.priority,
                    alert.title,
                    alert.message,
                    alert.delivery_status.value,
                    alert.delivered_at,
                    alert.retry_count,
                    alert.last_error,
                    alert.source_id,
                    alert.source_type,
                ))

            # Batch insert (run in executor to not block)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: self.clickhouse.insert(
                    "kospi.alert_log",
                    rows,
                    column_names=[
                        "id", "created_at", "alert_type", "channel", "priority",
                        "title", "message", "delivery_status", "delivered_at",
                        "retry_count", "last_error", "source_id", "source_type"
                    ]
                )
            )
            logger.debug(f"Flushed {len(alerts_to_flush)} alerts to ClickHouse audit log")

        except Exception as e:
            logger.error(f"Failed to flush alerts to ClickHouse: {e}")
            # Re-add to buffer for retry
            async with self._audit_lock:
                self._audit_buffer.extend(alerts_to_flush)

    async def get_queue_size(self) -> int:
        """Get number of pending alerts."""
        return self._queue.qsize()

    async def get_failed_count(self) -> int:
        """Get number of failed (dead letter) alerts."""
        if self.redis:
            try:
                return await self.redis.llen(self.FAILED_KEY)
            except Exception:
                pass
        return 0
