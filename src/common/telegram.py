"""
Telegram notification module for KOSPI Mini trading system.

Usage:
    from src.common.telegram import TelegramNotifier, send_message

    notifier = TelegramNotifier()
    notifier.send("Hello!")

    # Or use simple function
    send_message("Hello!")
"""
import os
import requests
from typing import Optional


class TelegramNotifier:
    """Telegram bot notifier."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None
    ):
        """
        Initialize Telegram notifier.

        Args:
            bot_token: Telegram bot token (default: env TELEGRAM_BOT_TOKEN)
            chat_id: Telegram chat ID (default: env TELEGRAM_CHAT_ID)
        """
        self.bot_token = bot_token or os.getenv(
            "TELEGRAM_BOT_TOKEN",
            "8062667299:AAEKQWFev-T6aoPiLcfuYA_XdRCow2lfRUY"
        )
        self.chat_id = chat_id or os.getenv(
            "TELEGRAM_CHAT_ID",
            "5940357912"
        )

    def send(self, text: str, parse_mode: Optional[str] = "HTML") -> bool:
        """
        Send a message via Telegram bot.

        Args:
            text: Message text (supports HTML/Markdown formatting)
            parse_mode: "HTML" or "Markdown" or None

        Returns:
            True if sent successfully, False otherwise
        """
        if not self.bot_token or not self.chat_id:
            print("Telegram not configured")
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            resp = requests.post(url, json=payload, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            print(f"Telegram send failed: {e}")
            return False

    def notify(self, title: str, message: str) -> bool:
        """
        Send a formatted notification.

        Args:
            title: Notification title
            message: Notification body

        Returns:
            True if sent successfully
        """
        text = f"<b>{title}</b>\n{message}"
        return self.send(text)


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
