"""Korean stock market trading calendar utilities."""
import functools
from datetime import date, datetime, timedelta
from typing import List

import pytz

# KRX market hours for futures
MARKET_OPEN = "09:00"
MARKET_CLOSE = "15:45"

# Korean public holidays (2024-2026)
KOREAN_HOLIDAYS = {
    # 2024
    date(2024, 1, 1), date(2024, 2, 9), date(2024, 2, 10), date(2024, 2, 11),
    date(2024, 2, 12), date(2024, 3, 1), date(2024, 4, 10), date(2024, 5, 5),
    date(2024, 5, 6), date(2024, 5, 15), date(2024, 6, 6), date(2024, 8, 15),
    date(2024, 9, 16), date(2024, 9, 17), date(2024, 9, 18), date(2024, 10, 3),
    date(2024, 10, 9), date(2024, 12, 25),
    # 2025
    date(2025, 1, 1), date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30),
    date(2025, 3, 1), date(2025, 3, 3), date(2025, 5, 5), date(2025, 5, 6),
    date(2025, 6, 6), date(2025, 8, 15), date(2025, 10, 3), date(2025, 10, 5),
    date(2025, 10, 6), date(2025, 10, 7), date(2025, 10, 8), date(2025, 10, 9),
    date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 3, 1), date(2026, 3, 2), date(2026, 5, 5), date(2026, 5, 24),
    date(2026, 6, 6), date(2026, 8, 15), date(2026, 9, 24), date(2026, 9, 25),
    date(2026, 9, 26), date(2026, 10, 3), date(2026, 10, 9), date(2026, 12, 25),
}


def is_trading_day(d: date = None) -> bool:
    """
    Check if a date is a trading day.

    Args:
        d: Date to check (default: today)

    Returns:
        True if trading day
    """
    if d is None:
        d = date.today()

    # Weekend check
    if d.weekday() >= 5:
        return False

    # Holiday check
    if d in KOREAN_HOLIDAYS:
        return False

    return True


def _get_weekdays_with_holidays(year: int, month: int) -> List[date]:
    """Return weekdays excluding Korean holidays for a month."""
    from calendar import monthrange

    days = []
    num_days = monthrange(year, month)[1]

    for day in range(1, num_days + 1):
        d = date(year, month, day)
        if d.weekday() < 5 and d not in KOREAN_HOLIDAYS:
            days.append(d)

    return days


@functools.lru_cache(maxsize=16)
def get_trading_days_from_krx(year: int, month: int) -> List[date]:
    """
    Get trading days for a specific month.

    Args:
        year: Year (e.g., 2025)
        month: Month (1-12)

    Returns:
        List of trading dates
    """
    return _get_weekdays_with_holidays(year, month)


def get_trading_days_range(start: date, end: date) -> List[date]:
    """
    Get trading days between start and end dates.

    Args:
        start: Start date (inclusive)
        end: End date (inclusive)

    Returns:
        List of trading dates
    """
    trading_days = []

    current = date(start.year, start.month, 1)
    while current <= end:
        month_days = get_trading_days_from_krx(current.year, current.month)
        for d in month_days:
            if start <= d <= end:
                trading_days.append(d)

        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)

    return sorted(trading_days)


def get_past_trading_days(days: int, from_date: date = None) -> List[date]:
    """
    Get trading days for the past N days.

    Args:
        days: Number of days to look back
        from_date: Reference date (default: today)

    Returns:
        List of trading dates
    """
    if from_date is None:
        from_date = date.today()

    start = from_date - timedelta(days=days)
    return get_trading_days_range(start, from_date)


def is_market_open() -> bool:
    """Check if the market is currently open."""
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.now(kst)

    if now.weekday() >= 5:
        return False

    if now.date() in KOREAN_HOLIDAYS:
        return False

    current_time = now.strftime("%H:%M")
    return MARKET_OPEN <= current_time <= MARKET_CLOSE


def is_after_market_close() -> bool:
    """Check if current time is after market close."""
    kst = pytz.timezone('Asia/Seoul')
    now = datetime.now(kst)

    if now.weekday() < 5 and now.date() not in KOREAN_HOLIDAYS:
        current_time = now.strftime("%H:%M")
        return current_time > MARKET_CLOSE

    return True


def get_kst_now() -> datetime:
    """Get current time in KST."""
    kst = pytz.timezone('Asia/Seoul')
    return datetime.now(kst)
