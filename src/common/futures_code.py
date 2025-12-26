"""
선물 코드 자동 감지 모듈

코스피200 미니선물 근월물/차월물 코드를 자동으로 계산합니다.

사용법:
    from src.common.futures_code import get_front_month_code, get_kis_code

    # KRX 표준 코드 (101H25)
    front_code = get_front_month_code()

    # KIS API 약식 코드 (A05601)
    kis_code = get_kis_code("front")  # 근월물
    kis_code = get_kis_code("back")   # 차월물
"""
from datetime import date, timedelta
from typing import Tuple, Optional
import calendar


# 월별 코드 매핑 (KRX 표준)
MONTH_CODES = {
    1: 'F', 2: 'G', 3: 'H', 4: 'J', 5: 'K', 6: 'M',
    7: 'N', 8: 'Q', 9: 'U', 10: 'V', 11: 'X', 12: 'Z'
}

# 분기물 월 (3, 6, 9, 12월)
QUARTERLY_MONTHS = [3, 6, 9, 12]

# KIS API 약식 코드
KIS_CODES = {
    "front": "A05601",  # 근월물
    "back": "A05602",   # 차월물
    "third": "A05603",  # 원월물 1
    "fourth": "A05604", # 원월물 2
}


def get_second_thursday(year: int, month: int) -> date:
    """
    해당 월의 두 번째 목요일 (선물 만기일) 계산

    :param year: 연도
    :param month: 월
    :return: 두 번째 목요일 날짜
    """
    # 해당 월의 첫째 날
    first_day = date(year, month, 1)

    # 첫째 날의 요일 (0=월요일, 3=목요일)
    first_weekday = first_day.weekday()

    # 첫 번째 목요일까지의 일수
    days_until_thursday = (3 - first_weekday) % 7
    first_thursday = first_day + timedelta(days=days_until_thursday)

    # 두 번째 목요일
    second_thursday = first_thursday + timedelta(days=7)

    return second_thursday


def get_quarterly_months_for_year(year: int) -> list:
    """해당 연도의 분기물 만기 정보 반환"""
    result = []
    for month in QUARTERLY_MONTHS:
        expiry = get_second_thursday(year, month)
        result.append({
            'year': year,
            'month': month,
            'expiry': expiry,
            'code': f"101{MONTH_CODES[month]}{year % 100:02d}"
        })
    return result


def get_front_month_info(as_of: date = None) -> dict:
    """
    근월물 정보 반환

    :param as_of: 기준일 (기본: 오늘)
    :return: {'year': 2025, 'month': 3, 'expiry': date, 'code': '101H25'}
    """
    if as_of is None:
        as_of = date.today()

    # 현재 연도와 다음 연도의 분기물 조회
    candidates = []
    candidates.extend(get_quarterly_months_for_year(as_of.year))
    candidates.extend(get_quarterly_months_for_year(as_of.year + 1))

    # 만기일이 지나지 않은 가장 가까운 월물 찾기
    for info in candidates:
        if info['expiry'] >= as_of:
            return info

    # 모두 지났으면 다음 연도 첫 분기물
    return get_quarterly_months_for_year(as_of.year + 1)[0]


def get_back_month_info(as_of: date = None) -> dict:
    """
    차월물 정보 반환

    :param as_of: 기준일 (기본: 오늘)
    :return: {'year': 2025, 'month': 6, 'expiry': date, 'code': '101M25'}
    """
    if as_of is None:
        as_of = date.today()

    front = get_front_month_info(as_of)

    # 현재 연도와 다음 연도의 분기물 조회
    candidates = []
    candidates.extend(get_quarterly_months_for_year(as_of.year))
    candidates.extend(get_quarterly_months_for_year(as_of.year + 1))

    # 근월물 다음 월물 찾기
    found_front = False
    for info in candidates:
        if found_front and info['expiry'] > front['expiry']:
            return info
        if info['code'] == front['code']:
            found_front = True

    # 못 찾으면 다음 연도 두 번째 분기물
    return get_quarterly_months_for_year(as_of.year + 1)[1]


def get_front_month_code(as_of: date = None) -> str:
    """
    근월물 KRX 코드 반환

    :param as_of: 기준일
    :return: '101H25' 형식
    """
    return get_front_month_info(as_of)['code']


def get_back_month_code(as_of: date = None) -> str:
    """
    차월물 KRX 코드 반환

    :param as_of: 기준일
    :return: '101M25' 형식
    """
    return get_back_month_info(as_of)['code']


def get_kis_code(month_type: str = "front") -> str:
    """
    KIS API 약식 코드 반환

    :param month_type: "front" (근월물), "back" (차월물)
    :return: 'A05601' 또는 'A05602'
    """
    return KIS_CODES.get(month_type, KIS_CODES["front"])


def get_current_futures_info(as_of: date = None) -> dict:
    """
    현재 거래 가능한 선물 정보 반환

    :param as_of: 기준일
    :return: 근월물/차월물 정보 딕셔너리
    """
    if as_of is None:
        as_of = date.today()

    front = get_front_month_info(as_of)
    back = get_back_month_info(as_of)

    return {
        'as_of': as_of.isoformat(),
        'front_month': {
            'krx_code': front['code'],
            'kis_code': KIS_CODES['front'],
            'expiry': front['expiry'].isoformat(),
            'year': front['year'],
            'month': front['month'],
            'days_to_expiry': (front['expiry'] - as_of).days,
        },
        'back_month': {
            'krx_code': back['code'],
            'kis_code': KIS_CODES['back'],
            'expiry': back['expiry'].isoformat(),
            'year': back['year'],
            'month': back['month'],
            'days_to_expiry': (back['expiry'] - as_of).days,
        },
    }


def is_valid_futures_code(code: str, as_of: date = None) -> bool:
    """
    유효한 선물 코드인지 확인 (만기되지 않은 월물)

    :param code: KRX 코드 (예: '101H25')
    :param as_of: 기준일
    :return: 유효 여부
    """
    if as_of is None:
        as_of = date.today()

    # KIS 약식 코드는 항상 유효
    if code.startswith('A056'):
        return True

    # KRX 코드 파싱
    if not code.startswith('101') or len(code) != 6:
        return False

    try:
        month_code = code[3]
        year_suffix = int(code[4:6])

        # 월 코드 역매핑
        month = None
        for m, c in MONTH_CODES.items():
            if c == month_code:
                month = m
                break

        if month is None:
            return False

        # 연도 계산 (00-99 -> 2000-2099)
        year = 2000 + year_suffix

        # 만기일 계산
        expiry = get_second_thursday(year, month)

        # 만기일이 지났으면 무효
        return expiry >= as_of

    except (ValueError, IndexError):
        return False


def format_futures_info() -> str:
    """현재 선물 정보를 문자열로 포맷"""
    info = get_current_futures_info()

    lines = [
        f"=== 코스피200 미니선물 현황 ({info['as_of']}) ===",
        "",
        "▶ 근월물:",
        f"  KRX: {info['front_month']['krx_code']}",
        f"  KIS: {info['front_month']['kis_code']}",
        f"  만기: {info['front_month']['expiry']} (D-{info['front_month']['days_to_expiry']})",
        "",
        "▶ 차월물:",
        f"  KRX: {info['back_month']['krx_code']}",
        f"  KIS: {info['back_month']['kis_code']}",
        f"  만기: {info['back_month']['expiry']} (D-{info['back_month']['days_to_expiry']})",
    ]

    return "\n".join(lines)


if __name__ == "__main__":
    print(format_futures_info())
