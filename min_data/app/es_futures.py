"""E-mini S&P 500 (ES) futures code generation utilities for Polygon.io API."""
from datetime import date, timedelta
from typing import List, Tuple


# ES 선물 월 코드 (CME 표준)
MONTH_CODES = {
    1: 'F', 2: 'G', 3: 'H', 4: 'J', 5: 'K', 6: 'M',
    7: 'N', 8: 'Q', 9: 'U', 10: 'V', 11: 'X', 12: 'Z'
}

CODE_TO_MONTH = {v: k for k, v in MONTH_CODES.items()}

# ES 선물 분기 계약 월 (주요 유동성)
QUARTERLY_MONTHS = [3, 6, 9, 12]  # H, M, U, Z

# ES 선물 상장 기간 (분기 계약은 약 5분기 전부터 거래)
ES_LISTING_QUARTERS = 5


def get_expiry_date(year: int, month: int) -> date:
    """
    Get the expiry date for an ES futures contract.
    ES futures expire on the 3rd Friday of the contract month.

    Args:
        year: Year (e.g., 2025)
        month: Month (1-12)

    Returns:
        Expiry date
    """
    # Find third Friday of the month
    first_day = date(year, month, 1)
    first_weekday = first_day.weekday()  # Monday = 0

    # Friday = 4
    if first_weekday <= 4:
        first_friday = 1 + (4 - first_weekday)
    else:
        first_friday = 1 + (7 - first_weekday + 4)

    third_friday = first_friday + 14

    return date(year, month, third_friday)


def make_code(year: int, month: int) -> str:
    """
    Generate ES futures ticker for Polygon.io API.

    Args:
        year: Full year (e.g., 2025)
        month: Month (1-12)

    Returns:
        ES futures ticker (e.g., 'ESZ5' for December 2025)
    """
    month_code = MONTH_CODES[month]
    year_digit = str(year)[-1]  # 마지막 1자리 (2025 -> 5)
    return f"ES{month_code}{year_digit}"


def parse_code(code: str) -> Tuple[int, int]:
    """
    Parse ES futures ticker to year and month.

    Args:
        code: ES ticker (e.g., 'ESZ5')

    Returns:
        Tuple of (year, month)
    """
    if not code.startswith("ES") or len(code) != 4:
        raise ValueError(f"Invalid ES ticker format: {code}")

    month_code = code[2]
    year_digit = int(code[3])

    month = CODE_TO_MONTH.get(month_code)
    if month is None:
        raise ValueError(f"Invalid month code: {month_code}")

    # 연도 추정 (현재 2020년대 기준)
    current_decade = 2020
    year = current_decade + year_digit

    return year, month


def get_listing_start(expiry: date) -> date:
    """
    ES 선물 상장 시작일 추정.
    분기 계약은 약 15개월(5분기) 전부터 거래가 시작됨.

    Args:
        expiry: 만기일

    Returns:
        상장 시작일 (추정)
    """
    # 약 15개월 전
    months_back = ES_LISTING_QUARTERS * 3
    month = expiry.month - months_back
    year = expiry.year

    while month <= 0:
        month += 12
        year -= 1

    return date(year, month, 1)


def get_quarterly_codes_in_range(start: date, end: date) -> List[str]:
    """
    Get all quarterly ES futures codes that were traded during a date range.
    Quarterly contracts (H, M, U, Z) have the highest liquidity.

    Args:
        start: Start date
        end: End date

    Returns:
        List of unique ES futures codes sorted chronologically
    """
    codes = []

    # 시작일 기준으로 시작 분기 계산
    start_year = start.year - 2  # 여유있게 2년 전부터 체크
    end_year = end.year + 1  # 1년 후까지 체크

    for year in range(start_year, end_year + 1):
        for month in QUARTERLY_MONTHS:
            expiry = get_expiry_date(year, month)
            listing_start = get_listing_start(expiry)

            # 계약이 우리 범위와 겹치는지 확인
            # listing_start <= end AND expiry >= start
            if listing_start <= end and expiry >= start:
                codes.append(make_code(year, month))

    # 만기일 기준 정렬
    codes.sort(key=lambda c: get_expiry_date(*parse_code(c)))

    return codes


def get_all_codes_in_range(start: date, end: date, quarterly_only: bool = True) -> List[str]:
    """
    Get all ES futures codes that were traded during a date range.

    Args:
        start: Start date
        end: End date
        quarterly_only: If True, only return quarterly contracts (default: True)

    Returns:
        List of unique ES futures codes sorted chronologically
    """
    if quarterly_only:
        return get_quarterly_codes_in_range(start, end)

    codes = []
    start_year = start.year - 2
    end_year = end.year + 1

    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            expiry = get_expiry_date(year, month)
            listing_start = get_listing_start(expiry)

            if listing_start <= end and expiry >= start:
                codes.append(make_code(year, month))

    codes.sort(key=lambda c: get_expiry_date(*parse_code(c)))
    return codes


def get_active_codes_for_date(target_date: date, quarterly_only: bool = True) -> List[str]:
    """
    Get all active (tradeable) ES futures codes for a specific date.

    Args:
        target_date: The date to check
        quarterly_only: If True, only return quarterly contracts

    Returns:
        List of active ES futures codes
    """
    codes = []
    months = QUARTERLY_MONTHS if quarterly_only else range(1, 13)

    # Check contracts for the next 2 years
    for year_offset in range(-1, 3):
        year = target_date.year + year_offset
        for month in months:
            expiry = get_expiry_date(year, month)
            listing_start = get_listing_start(expiry)

            # Contract is active if target_date is between listing and expiry
            if listing_start <= target_date <= expiry:
                codes.append(make_code(year, month))

    codes.sort(key=lambda c: get_expiry_date(*parse_code(c)))
    return codes


def get_codes_for_years(years: int = 5, from_date: date = None) -> List[str]:
    """
    Get all quarterly ES futures codes for the past N years.

    Args:
        years: Number of years to look back (default: 5)
        from_date: Reference date (default: today)

    Returns:
        List of ES futures codes
    """
    if from_date is None:
        from_date = date.today()

    start = date(from_date.year - years, from_date.month, from_date.day)
    return get_quarterly_codes_in_range(start, from_date)
