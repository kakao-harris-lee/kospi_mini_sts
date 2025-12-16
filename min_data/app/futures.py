"""KOSPI Mini futures code generation utilities.

한국투자증권 API에서 지원하는 두 가지 선물 코드 형식:

1. 표준 형식 (기본값):
   - {상품코드(3자리)}{연도코드(1자리)}{월(2자리)}
   - 예: 105X01 = 미니 KOSPI200 2026년 1월물
   - KOSPI200 선물: 101, 미니 KOSPI200 선물: 105

2. 레거시 형식 (A코드):
   - A{상품코드(2자리)}{연도(1자리)}{월(2자리)}
   - 예: A05601 = 미니 KOSPI200 2026년 1월물
   - KOSPI200 선물: A01, 미니 KOSPI200 선물: A05

두 형식 모두 API에서 유효하게 작동합니다.
"""
from datetime import date, timedelta
from typing import List, Tuple
from calendar import monthrange


# ============================================================
# 표준 형식 (권장): 101/105 + 연도코드(알파벳) + 월(2자리)
# ============================================================
KOSPI200_PREFIX = "101"
KOSPI_MINI_PREFIX = "105"

# 연도코드 매핑 (한투 API 기준)
# 검증된 매핑: 101V09 = KOSPI200 2024년 9월물, 105W12 = 미니KOSPI 2025년 12월물
YEAR_CODES = {
    2020: 'Q',
    2021: 'R',
    2022: 'S',
    2023: 'T',
    2024: 'V',
    2025: 'W',
    2026: 'X',
    2027: 'Y',
    2028: 'Z',
    2029: 'A',
    2030: 'B',
}

# 역매핑 (코드 → 연도)
CODE_TO_YEAR = {v: k for k, v in YEAR_CODES.items()}


# ============================================================
# 레거시 형식: A01/A05 + 연도(1자리) + 월(2자리)
# ============================================================
KOSPI200_LEGACY_PREFIX = "A01"
KOSPI_MINI_LEGACY_PREFIX = "A05"


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
    # Find second Thursday of the month
    first_day = date(year, month, 1)
    first_weekday = first_day.weekday()  # Monday = 0

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
        prefix: Product prefix (default: KOSPI_MINI_LEGACY_PREFIX for legacy)
        legacy: Use legacy A-code format (default: True - 더 안정적으로 작동)

    Returns:
        Futures code
        - Legacy (기본값): 'A05601' for 미니 KOSPI200 2026년 1월물
        - Standard: '105X01' for 미니 KOSPI200 2026년 1월물

    Note:
        레거시 형식(A05XXX)이 모든 월물에서 더 안정적으로 작동합니다.
        표준 형식(105XXX)은 일부 월물에서 데이터가 없을 수 있습니다.
    """
    if legacy:
        return make_code_legacy(year, month, prefix)

    if prefix is None:
        prefix = KOSPI_MINI_PREFIX

    # 형식: {상품코드(3자리)}{연도코드(1자리)}{월(2자리)}
    year_code = YEAR_CODES.get(year)
    if year_code is None:
        raise ValueError(f"Unsupported year: {year}. Supported: {list(YEAR_CODES.keys())}")

    month_str = f"{month:02d}"  # 2자리 월 (01, 02, ..., 12)
    return f"{prefix}{year_code}{month_str}"


def make_code_legacy(year: int, month: int, prefix: str = None) -> str:
    """
    Generate futures code using legacy A-code format.

    Args:
        year: Full year (e.g., 2025)
        month: Month (1-12)
        prefix: Product prefix (default: KOSPI_MINI_LEGACY_PREFIX)

    Returns:
        Legacy futures code (e.g., 'A05601' for 미니 KOSPI200 2026년 1월물)
    """
    if prefix is None:
        prefix = KOSPI_MINI_LEGACY_PREFIX

    # 형식: A{상품코드(2자리)}{연도(1자리)}{월(2자리)}
    year_digit = str(year)[-1]  # 마지막 1자리 (2026 -> 6)
    month_str = f"{month:02d}"  # 2자리 월 (01, 02, ..., 12)
    return f"{prefix}{year_digit}{month_str}"


def parse_code(code: str) -> Tuple[int, int]:
    """
    Parse futures code to year and month.
    Supports both standard format (105X01) and legacy format (A05601).

    Args:
        code: Futures code (e.g., '105X01' or 'A05601')

    Returns:
        Tuple of (year, month)
    """
    if code.startswith('A'):
        # 레거시 형식: A{상품코드(2자리)}{연도(1자리)}{월(2자리)}
        # 예: A05601 -> year_digit='6', month='01'
        year_digit = int(code[3])  # 4번째 문자
        month_str = code[4:6]  # 5-6번째 문자

        # 연도 추정 (현재 2020년대 기준)
        current_decade = 2020
        year = current_decade + year_digit

        month = int(month_str)
    else:
        # 표준 형식: {상품코드(3자리)}{연도코드(1자리)}{월(2자리)}
        # 예: 105X01 -> year_code='X', month='01'
        year_code = code[3]  # 4번째 문자
        month_str = code[4:6]  # 5-6번째 문자

        # 연도 변환
        year = CODE_TO_YEAR.get(year_code)
        if year is None:
            raise ValueError(f"Unknown year code: {year_code}")

        month = int(month_str)

    return year, month


def get_listing_start(expiry: date) -> date:
    """
    선물 상장 시작일 계산.
    Mini KOSPI200 선물은 연속 6개 월물이 상장되므로,
    만기 약 6개월 전부터 거래가 시작됨.

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
    Typically, multiple monthly contracts are listed simultaneously.

    Args:
        target_date: The date to check

    Returns:
        List of active futures codes
    """
    codes = []

    # Check contracts for the next 12 months
    current = date(target_date.year, target_date.month, 1)

    for _ in range(12):
        expiry = get_expiry_date(current.year, current.month)

        # Contract is active if target_date is before or on expiry
        if target_date <= expiry:
            codes.append(make_code(current.year, current.month))

        # Move to next month
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

    # Find all months that could have active contracts
    # Go back a bit to catch contracts that started before our range
    check_start = date(start.year, start.month, 1)
    if check_start.month == 1:
        check_start = date(check_start.year - 1, 12, 1)
    else:
        check_start = date(check_start.year, check_start.month - 1, 1)

    # Go forward to include all contracts expiring during our range
    check_end = date(end.year, end.month, 1)
    for _ in range(12):  # Look ahead up to 12 months
        if check_end.month == 12:
            check_end = date(check_end.year + 1, 1, 1)
        else:
            check_end = date(check_end.year, check_end.month + 1, 1)

    current = check_start
    while current <= check_end:
        expiry = get_expiry_date(current.year, current.month)

        # Check if this contract was active during any part of our range
        # Mini KOSPI200: 연속 6개 월물 상장 (만기 ~6개월 전부터 거래)
        listing_start = get_listing_start(expiry)

        # Contract overlaps with our range if:
        # listing_start <= end AND expiry >= start
        if listing_start <= end and expiry >= start:
            codes_set.add(make_code(current.year, current.month))

        # Move to next month
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)

    # Sort by expiry date
    codes = list(codes_set)
    codes.sort(key=lambda c: (parse_code(c)[0], parse_code(c)[1]))

    return codes


def get_past_year_codes(from_date: date = None) -> List[str]:
    """
    Get all futures codes for the past year.

    Args:
        from_date: Reference date (default: today)

    Returns:
        List of futures codes
    """
    if from_date is None:
        from_date = date.today()

    start = from_date - timedelta(days=365)
    return get_all_codes_in_range(start, from_date)
