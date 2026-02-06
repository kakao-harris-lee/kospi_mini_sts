"""
Telegram notification module for KOSPI Mini trading system.

Usage:
    from src.common.telegram import TelegramNotifier, send_message

    notifier = TelegramNotifier()
    notifier.send("Hello!")

    # Or use simple function
    send_message("Hello!")

    # Async usage
    await notifier.send_async("Hello async!")

    # Queue-based delivery with retry
    queue = AlertQueue(redis_client)
    await queue.enqueue(alert)
    await queue.process_queue()
"""
import os
import json
import asyncio
import logging
import requests
from datetime import date, datetime
from typing import Optional, Dict, Any
from dataclasses import dataclass, asdict

import aiohttp

logger = logging.getLogger(__name__)


def _is_trading_day(d: date = None) -> bool:
    """Check if today is a trading day (not weekend/holiday)."""
    try:
        from src.collector.historical.calendar import is_trading_day
        return is_trading_day(d)
    except ImportError:
        # Fallback: at least check weekends
        if d is None:
            d = date.today()
        return d.weekday() < 5


class TelegramNotifier:
    """Telegram bot notifier with sync and async support."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        check_trading_day: bool = True
    ):
        """
        Initialize Telegram notifier.

        Args:
            bot_token: Telegram bot token (default: env TELEGRAM_BOT_TOKEN)
            chat_id: Telegram chat ID (default: env TELEGRAM_CHAT_ID)
            check_trading_day: If True, skip sending on non-trading days
        """
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.check_trading_day = check_trading_day
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def api_url(self) -> str:
        """Get Telegram API URL for sendMessage."""
        return f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    def send(self, text: str, parse_mode: Optional[str] = "HTML", force: bool = False) -> bool:
        """
        Send a message via Telegram bot (synchronous).

        Args:
            text: Message text (supports HTML/Markdown formatting)
            parse_mode: "HTML" or "Markdown" or None
            force: If True, send even on non-trading days

        Returns:
            True if sent successfully, False otherwise
        """
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram not configured")
            return False

        # Skip sending on non-trading days (holidays/weekends)
        if self.check_trading_day and not force and not _is_trading_day():
            logger.info("Telegram skipped: not a trading day")
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            resp = requests.post(self.api_url, json=payload, timeout=10)
            if resp.status_code != 200:
                body = ""
                try:
                    body = resp.text
                except Exception:
                    body = "<unreadable>"
                logger.error(f"Telegram API error: {resp.status_code} - {body}")
                return False
            return True
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False

    async def send_async(
        self,
        text: str,
        parse_mode: Optional[str] = "HTML",
        force: bool = False
    ) -> bool:
        """
        Send a message via Telegram bot (asynchronous).

        Args:
            text: Message text (supports HTML/Markdown formatting)
            parse_mode: "HTML" or "Markdown" or None
            force: If True, send even on non-trading days

        Returns:
            True if sent successfully, False otherwise
        """
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram not configured")
            return False

        # Skip sending on non-trading days (holidays/weekends)
        if self.check_trading_day and not force and not _is_trading_day():
            logger.info("Telegram skipped: not a trading day")
            return False

        payload = {
            "chat_id": self.chat_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.api_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    success = resp.status == 200
                    if not success:
                        body = await resp.text()
                        logger.error(f"Telegram API error: {resp.status} - {body}")
                    return success
        except asyncio.TimeoutError:
            logger.error("Telegram send timed out")
            return False
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False

    def notify(self, title: str, message: str) -> bool:
        """
        Send a formatted notification (sync).

        Args:
            title: Notification title
            message: Notification body

        Returns:
            True if sent successfully
        """
        text = f"<b>{title}</b>\n{message}"
        return self.send(text)

    async def notify_async(self, title: str, message: str) -> bool:
        """
        Send a formatted notification (async).

        Args:
            title: Notification title
            message: Notification body

        Returns:
            True if sent successfully
        """
        text = f"<b>{title}</b>\n{message}"
        return await self.send_async(text)


@dataclass
class QueuedAlert:
    """Alert queued for delivery."""
    id: str
    title: str
    message: str
    priority: str  # low, normal, high, urgent
    created_at: str
    retry_count: int = 0
    last_error: Optional[str] = None

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> "QueuedAlert":
        """Deserialize from JSON string."""
        return cls(**json.loads(data))


class AlertQueue:
    """
    Redis-backed alert queue with retry logic.

    Uses Redis Lists for FIFO queue semantics:
    - alerts:pending - main queue for pending alerts
    - alerts:failed - failed alerts for retry

    Retry strategy: exponential backoff (10s, 30s, 90s)
    Max retries: 3
    """

    PENDING_KEY = "alerts:pending"
    FAILED_KEY = "alerts:failed"
    MAX_RETRIES = 3
    RETRY_DELAYS = [10, 30, 90]  # seconds

    def __init__(self, redis_client, notifier: Optional[TelegramNotifier] = None):
        """
        Initialize alert queue.

        Args:
            redis_client: Redis client instance (async)
            notifier: TelegramNotifier instance (default: creates new one)
        """
        self.redis = redis_client
        self.notifier = notifier or TelegramNotifier()
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def enqueue(self, alert: QueuedAlert) -> bool:
        """
        Add alert to the pending queue.

        Args:
            alert: Alert to queue

        Returns:
            True if queued successfully
        """
        try:
            await self.redis.lpush(self.PENDING_KEY, alert.to_json())
            logger.debug(f"Alert queued: {alert.id}")
            return True
        except Exception as e:
            logger.error(f"Failed to queue alert: {e}")
            return False

    async def dequeue(self) -> Optional[QueuedAlert]:
        """
        Get next alert from the queue.

        Returns:
            Alert or None if queue is empty
        """
        try:
            data = await self.redis.rpop(self.PENDING_KEY)
            if data:
                return QueuedAlert.from_json(data)
            return None
        except Exception as e:
            logger.error(f"Failed to dequeue alert: {e}")
            return None

    async def requeue_for_retry(self, alert: QueuedAlert, error: str) -> bool:
        """
        Requeue a failed alert for retry.

        Args:
            alert: Failed alert
            error: Error message

        Returns:
            True if requeued, False if max retries exceeded
        """
        alert.retry_count += 1
        alert.last_error = error

        if alert.retry_count >= self.MAX_RETRIES:
            # Move to dead letter queue
            logger.warning(f"Alert {alert.id} exceeded max retries, moving to failed queue")
            await self.redis.lpush(self.FAILED_KEY, alert.to_json())
            return False

        # Schedule retry with delay
        delay = self.RETRY_DELAYS[min(alert.retry_count - 1, len(self.RETRY_DELAYS) - 1)]
        logger.info(f"Retrying alert {alert.id} in {delay}s (attempt {alert.retry_count})")

        await asyncio.sleep(delay)
        await self.redis.lpush(self.PENDING_KEY, alert.to_json())
        return True

    async def process_one(self, alert: QueuedAlert) -> bool:
        """
        Process a single alert.

        Args:
            alert: Alert to send

        Returns:
            True if sent successfully
        """
        try:
            success = await self.notifier.notify_async(alert.title, alert.message)
            if success:
                logger.info(f"Alert {alert.id} sent successfully")
                return True
            else:
                await self.requeue_for_retry(alert, "Telegram API returned failure")
                return False
        except Exception as e:
            await self.requeue_for_retry(alert, str(e))
            return False

    async def process_queue(self) -> int:
        """
        Process all pending alerts (one pass).

        Returns:
            Number of alerts processed successfully
        """
        processed = 0
        while True:
            alert = await self.dequeue()
            if alert is None:
                break
            if await self.process_one(alert):
                processed += 1
        return processed

    async def start_processing_loop(self, interval: float = 1.0):
        """
        Start continuous queue processing loop.

        Args:
            interval: Polling interval in seconds
        """
        self._running = True
        logger.info("Alert queue processing loop started")

        while self._running:
            try:
                await self.process_queue()
            except Exception as e:
                logger.error(f"Error in alert queue processing: {e}")
            await asyncio.sleep(interval)

    def stop(self):
        """Stop the processing loop."""
        self._running = False
        logger.info("Alert queue processing loop stopped")

    async def get_queue_size(self) -> int:
        """Get number of pending alerts."""
        try:
            return await self.redis.llen(self.PENDING_KEY)
        except Exception:
            return 0

    async def get_failed_count(self) -> int:
        """Get number of failed (dead letter) alerts."""
        try:
            return await self.redis.llen(self.FAILED_KEY)
        except Exception:
            return 0


# Default instance
_default_notifier: Optional[TelegramNotifier] = None


def get_notifier() -> TelegramNotifier:
    """Get default notifier instance."""
    global _default_notifier
    if _default_notifier is None:
        _default_notifier = TelegramNotifier()
    return _default_notifier


def send_message(text: str, parse_mode: Optional[str] = "HTML") -> bool:
    """Send message using default notifier."""
    return get_notifier().send(text, parse_mode)


def notify(title: str, message: str) -> bool:
    """Send notification using default notifier."""
    return get_notifier().notify(title, message)


def notify_error(source: str, error: str) -> bool:
    """Send error notification."""
    return send_message(f"<b>[{source}] 에러</b>\n{error}")


def notify_success(source: str, message: str) -> bool:
    """Send success notification."""
    return send_message(f"<b>[{source}]</b> {message}")


async def send_message_async(text: str, parse_mode: Optional[str] = "HTML") -> bool:
    """Send message using default notifier (async)."""
    return await get_notifier().send_async(text, parse_mode)


async def notify_async(title: str, message: str) -> bool:
    """Send notification using default notifier (async)."""
    return await get_notifier().notify_async(title, message)
