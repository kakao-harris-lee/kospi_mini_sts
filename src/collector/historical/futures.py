"""
KOSPI Mini futures code generation utilities.

한국투자증권 API에서 지원하는 선물 코드 형식:

1. 레거시 형식 (기본값, 더 안정적):
   - A{상품코드(2자리)}{연도(1자리)}{월(2자리)}
   - 예: A05601 = 미니 KOSPI200 2026년 1월물
   - KOSPI200 선물: A01, 미니 KOSPI200 선물: A05

2. 표준 형식:
   - {상품코드(3자리)}{연도코드(1자리)}{월(2자리)}
   - 예: 105X01 = 미니 KOSPI200 2026년 1월물
   - KOSPI200 선물: 101, 미니 KOSPI200 선물: 105
"""
from datetime import date, timedelta
from typing import List, Tuple

# ============================================================
# 상품 코드 접두사
# ============================================================
KOSPI200_PREFIX = "101"
KOSPI_MINI_PREFIX = "105"

KOSPI200_LEGACY_PREFIX = "A01"
KOSPI_MINI_LEGACY_PREFIX = "A05"

# KIS "Short codes" - relative position codes (auto-rolling)
# These codes always refer to the Nth nearest contract
KIS_SHORT_CODES = {
    # KOSPI 200 Mini Futures
    "mini_front": "A05601",   # 근월물 (front month)
    "mini_back": "A05602",    # 차월물 (next month)
    # KOSPI 200 Futures (full-size)
    "kospi_front": "101S6000",  # 근월물 (front month)
    "kospi_back": "101S6001",   # 차월물 (next month)
}

# 연도코드 매핑 (표준 형식용)
YEAR_CODES = {
    2020: 'Q', 2021: 'R', 2022: 'S', 2023: 'T', 2024: 'V',
    2025: 'W', 2026: 'X', 2027: 'Y', 2028: 'Z', 2029: 'A', 2030: 'B',
}
CODE_TO_YEAR = {v: k for k, v in YEAR_CODES.items()}

# Mini KOSPI200 선물은 연속 6개 월물이 상장됨
MINI_KOSPI_LISTING_MONTHS = 6


def get_expiry_date(year: int, month: int) -> date:
    """
    Get the expiry date for a futures contract.
    KOSPI Mini futures expire on the 2nd Thursday of the month.

    Args:
        year: Year (e.g., 2025)
        month: Month (1-12)

    Returns:
        Expiry date
    """
    first_day = date(year, month, 1)
    first_weekday = first_day.weekday()

    # Thursday = 3
    if first_weekday <= 3:
        first_thursday = 1 + (3 - first_weekday)
    else:
        first_thursday = 1 + (7 - first_weekday + 3)

    second_thursday = first_thursday + 7
    return date(year, month, second_thursday)


def make_code(year: int, month: int, prefix: str = None, legacy: bool = True) -> str:
    """
    Generate futures code for Korea Investment API.

    Args:
        year: Full year (e.g., 2025)
        month: Month (1-12)
        prefix: Product prefix (default: KOSPI_MINI_LEGACY_PREFIX)
        legacy: Use legacy A-code format (default: True - 더 안정적)

    Returns:
        Futures code
        - Legacy (기본값): 'A05601' for 미니 KOSPI200 2026년 1월물
        - Standard: '105X01' for 미니 KOSPI200 2026년 1월물
    """
    if legacy:
        return make_code_legacy(year, month, prefix)

    if prefix is None:
        prefix = KOSPI_MINI_PREFIX

    year_code = YEAR_CODES.get(year)
    if year_code is None:
        raise ValueError(f"Unsupported year: {year}")

    return f"{prefix}{year_code}{month:02d}"


def make_code_legacy(year: int, month: int, prefix: str = None) -> str:
    """
    Generate futures code using legacy A-code format.

    Args:
        year: Full year (e.g., 2025)
        month: Month (1-12)
        prefix: Product prefix (default: KOSPI_MINI_LEGACY_PREFIX)

    Returns:
        Legacy futures code (e.g., 'A05601')
    """
    if prefix is None:
        prefix = KOSPI_MINI_LEGACY_PREFIX

    year_digit = str(year)[-1]
    return f"{prefix}{year_digit}{month:02d}"


def parse_code(code: str) -> Tuple[int, int]:
    """
    Parse futures code to year and month.

    Args:
        code: Futures code (e.g., '105X01' or 'A05601')

    Returns:
        Tuple of (year, month)
    """
    if code.startswith('A'):
        # 레거시 형식: A{상품코드(2자리)}{연도(1자리)}{월(2자리)}
        year_digit = int(code[3])
        month = int(code[4:6])
        year = 2020 + year_digit
    else:
        # 표준 형식
        year_code = code[3]
        month = int(code[4:6])
        year = CODE_TO_YEAR.get(year_code)
        if year is None:
            raise ValueError(f"Unknown year code: {year_code}")

    return year, month


def get_listing_start(expiry: date) -> date:
    """
    선물 상장 시작일 계산.

    Args:
        expiry: 만기일

    Returns:
        상장 시작일 (추정)
    """
    month = expiry.month - MINI_KOSPI_LISTING_MONTHS
    year = expiry.year
    if month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def get_active_codes_for_date(target_date: date) -> List[str]:
    """
    Get all active (tradeable) futures codes for a specific date.

    Args:
        target_date: The date to check

    Returns:
        List of active futures codes
    """
    codes = []
    current = date(target_date.year, target_date.month, 1)

    for _ in range(12):
        expiry = get_expiry_date(current.year, current.month)

        if target_date <= expiry:
            codes.append(make_code(current.year, current.month))

        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)

    return codes


def get_all_codes_in_range(start: date, end: date) -> List[str]:
    """
    Get all futures codes that were traded during a date range.

    Args:
        start: Start date
        end: End date

    Returns:
        List of unique futures codes sorted chronologically
    """
    codes_set = set()

    check_start = date(start.year, start.month, 1)
    if check_start.month == 1:
        check_start = date(check_start.year - 1, 12, 1)
    else:
        check_start = date(check_start.year, check_start.month - 1, 1)

    check_end = date(end.year, end.month, 1)
    for _ in range(12):
        if check_end.month == 12:
            check_end = date(check_end.year + 1, 1, 1)
        else:
            check_end = date(check_end.year, check_end.month + 1, 1)

    current = check_start
    while current <= check_end:
        expiry = get_expiry_date(current.year, current.month)
        listing_start = get_listing_start(expiry)

        if listing_start <= end and expiry >= start:
            codes_set.add(make_code(current.year, current.month))

        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)

    codes = list(codes_set)
    codes.sort(key=lambda c: (parse_code(c)[0], parse_code(c)[1]))
    return codes


def get_past_year_codes(from_date: date = None) -> List[str]:
    """Get all futures codes for the past year."""
    if from_date is None:
        from_date = date.today()

    start = from_date - timedelta(days=365)
    return get_all_codes_in_range(start, from_date)
