"""Korean stock market trading calendar utilities."""
import requests
from datetime import date, datetime, timedelta
from typing import List, Set
import functools


# KRX market hours for futures
MARKET_OPEN = "09:00"
MARKET_CLOSE = "15:45"

# Korean public holidays (2024-2025) - 수동 관리 필요
KOREAN_HOLIDAYS = {
    # 2024
    date(2024, 1, 1),   # 신정
    date(2024, 2, 9),   # 설날 연휴
    date(2024, 2, 10),  # 설날
    date(2024, 2, 11),  # 설날 연휴
    date(2024, 2, 12),  # 대체공휴일
    date(2024, 3, 1),   # 삼일절
    date(2024, 4, 10),  # 국회의원선거
    date(2024, 5, 5),   # 어린이날
    date(2024, 5, 6),   # 대체공휴일
    date(2024, 5, 15),  # 부처님오신날
    date(2024, 6, 6),   # 현충일
    date(2024, 8, 15),  # 광복절
    date(2024, 9, 16),  # 추석 연휴
    date(2024, 9, 17),  # 추석
    date(2024, 9, 18),  # 추석 연휴
    date(2024, 10, 3),  # 개천절
    date(2024, 10, 9),  # 한글날
    date(2024, 12, 25), # 성탄절
    # 2025
    date(2025, 1, 1),   # 신정
    date(2025, 1, 28),  # 설날 연휴
    date(2025, 1, 29),  # 설날
    date(2025, 1, 30),  # 설날 연휴
    date(2025, 3, 1),   # 삼일절
    date(2025, 3, 3),   # 대체공휴일
    date(2025, 5, 5),   # 어린이날/부처님오신날
    date(2025, 5, 6),   # 대체공휴일
    date(2025, 6, 6),   # 현충일
    date(2025, 8, 15),  # 광복절
    date(2025, 10, 3),  # 개천절
    date(2025, 10, 5),  # 추석 연휴
    date(2025, 10, 6),  # 추석
    date(2025, 10, 7),  # 추석 연휴
    date(2025, 10, 8),  # 대체공휴일
    date(2025, 10, 9),  # 한글날
    date(2025, 12, 25), # 성탄절
    # 2026 (음력 기반 공휴일은 추정치)
    date(2026, 1, 1),   # 신정
    date(2026, 2, 16),  # 설날 연휴 (음력 1.1 = 2026-02-17)
    date(2026, 2, 17),  # 설날
    date(2026, 2, 18),  # 설날 연휴
    date(2026, 3, 1),   # 삼일절 (일요일)
    date(2026, 3, 2),   # 대체공휴일
    date(2026, 5, 5),   # 어린이날
    date(2026, 5, 24),  # 부처님오신날 (음력 4.8)
    date(2026, 6, 6),   # 현충일 (토요일)
    date(2026, 8, 15),  # 광복절 (토요일)
    date(2026, 9, 24),  # 추석 연휴 (음력 8.15 = 2026-09-25)
    date(2026, 9, 25),  # 추석
    date(2026, 9, 26),  # 추석 연휴
    date(2026, 10, 3),  # 개천절 (토요일)
    date(2026, 10, 9),  # 한글날
    date(2026, 12, 25), # 성탄절
}


def _get_weekdays_with_holidays(year: int, month: int) -> List[date]:
    """Return weekdays excluding Korean holidays for a month."""
    from calendar import monthrange

    days = []
    num_days = monthrange(year, month)[1]

    for day in range(1, num_days + 1):
        d = date(year, month, day)
        if d.weekday() < 5 and d not in KOREAN_HOLIDAYS:  # Mon-Fri, not holiday
            days.append(d)

    return days


@functools.lru_cache(maxsize=16)
def get_trading_days_from_krx(year: int, month: int) -> List[date]:
    """
    Get trading days from KRX (Korea Exchange) for a specific month.
    Uses KRX open API to get business days.

    Args:
        year: Year (e.g., 2025)
        month: Month (1-12)

    Returns:
        List of trading dates
    """
    # 공공데이터포털 API 사용 (더 안정적)
    # 또는 직접 휴일 목록 사용
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

        # Move to next month
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)

    return sorted(trading_days)


def get_past_year_trading_days(from_date: date = None) -> List[date]:
    """
    Get trading days for the past year.

    Args:
        from_date: Reference date (default: today)

    Returns:
        List of trading dates for the past 365 days
    """
    if from_date is None:
        from_date = date.today()

    start = from_date - timedelta(days=365)
    end = from_date

    return get_trading_days_range(start, end)


def is_market_open() -> bool:
    """Check if the market is currently open."""
    from datetime import datetime
    import pytz

    kst = pytz.timezone('Asia/Seoul')
    now = datetime.now(kst)

    # Check if weekday
    if now.weekday() >= 5:
        return False

    # Check time
    current_time = now.strftime("%H:%M")
    return MARKET_OPEN <= current_time <= MARKET_CLOSE


def is_after_market_close() -> bool:
    """Check if current time is after market close (good time to collect data)."""
    from datetime import datetime
    import pytz

    kst = pytz.timezone('Asia/Seoul')
    now = datetime.now(kst)

    # Weekday after 15:45
    if now.weekday() < 5:
        current_time = now.strftime("%H:%M")
        return current_time > MARKET_CLOSE

    return True  # Weekend - always OK to collect
