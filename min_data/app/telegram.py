"""Telegram notification wrapper for KOSPI Mini data collector."""
import sys
import os

# Add parent directory to path for common module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from common.telegram import send_message, notify, TelegramNotifier

SOURCE = "KOSPI Mini"


def notify_collection_start(date_str: str, codes: list):
    """Notify collection start."""
    msg = f"<b>[{SOURCE}]</b> 수집 시작\n날짜: {date_str}\n종목: {len(codes)}개"
    send_message(msg)


def notify_collection_complete(date_str: str, rows: int, errors: int = 0):
    """Notify collection complete."""
    status = "완료" if errors == 0 else f"완료 (에러 {errors}건)"
    msg = f"<b>[{SOURCE}]</b> 수집 {status}\n날짜: {date_str}\n수집: {rows}건"
    send_message(msg)


def notify_collection_error(date_str: str, error: str):
    """Notify collection error."""
    msg = f"<b>[{SOURCE}]</b> 수집 실패\n날짜: {date_str}\n에러: {error}"
    send_message(msg)


def notify_daily_summary(total_rows: int, trading_days: int, latest_date: str):
    """Send daily summary."""
    msg = (
        f"<b>[{SOURCE}]</b> 일일 리포트\n"
        f"총 데이터: {total_rows:,}건\n"
        f"거래일: {trading_days}일\n"
        f"최신 날짜: {latest_date}"
    )
    send_message(msg)
