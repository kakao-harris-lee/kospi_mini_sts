"""
거래 시간 필터
노이즈 구간, 점심 시간, 이벤트 직전 등 거래 제외 시간 관리
"""
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


@dataclass
class TradingHoursConfig:
    """거래 시간 설정"""
    # 정규 거래 시간
    market_open: time = time(9, 0)
    market_close: time = time(15, 45)

    # 노이즈 구간 (시가 근처 변동성 큰 구간)
    noise_start: time = time(9, 0)
    noise_end: time = time(9, 15)

    # 점심 시간 (유동성 감소)
    lunch_start: time = time(11, 30)
    lunch_end: time = time(13, 0)

    # 장 마감 전 (청산 전용)
    close_warning: time = time(15, 30)  # 신규 진입 금지
    force_close: time = time(15, 43)     # 강제 청산

    # 이벤트 제외 (분 단위로 지정)
    # 예: 매시 정각 ±5분 제외
    exclude_around_hour: bool = True
    exclude_minutes_before_hour: int = 5
    exclude_minutes_after_hour: int = 5


class TradingHoursFilter:
    """
    거래 시간 필터

    거래하지 않는 구간:
    1. 노이즈 구간 (09:00~09:15): 시가 근처 높은 변동성
    2. 점심 시간 (11:30~13:00): 유동성 감소
    3. 장 마감 직전 (15:30 이후): 신규 진입 금지
    4. 이벤트 직전 (매시 정각 ±5분): 옵션 만기 등
    """

    def __init__(self, config: TradingHoursConfig = None):
        self.config = config or TradingHoursConfig()

        # 특정 날짜의 이벤트 시간 (수동 추가)
        self.event_blackouts: List[Tuple[datetime, datetime]] = []

    def add_event_blackout(self, start: datetime, end: datetime):
        """이벤트 제외 구간 추가"""
        self.event_blackouts.append((start, end))
        logger.info(f"Added event blackout: {start} ~ {end}")

    def is_trading_hours(self, dt: datetime) -> bool:
        """
        거래 가능 시간인지 확인 (장 시간 내)

        Args:
            dt: 확인할 시간

        Returns:
            장 시간 내 여부
        """
        t = dt.time()
        return self.config.market_open <= t < self.config.market_close

    def can_open_position(self, dt: datetime) -> bool:
        """
        신규 포지션 진입 가능 여부

        거래 제외 구간:
        - 장 시간 외
        - 노이즈 구간 (09:00~09:15)
        - 점심 시간 (11:30~13:00)
        - 장 마감 직전 (15:30 이후)
        - 이벤트 블랙아웃
        - 매시 정각 ±5분 (옵션)

        Args:
            dt: 확인할 시간

        Returns:
            진입 가능 여부
        """
        t = dt.time()

        # 1. 장 시간 체크
        if not self.is_trading_hours(dt):
            return False

        # 2. 노이즈 구간 체크
        if self.config.noise_start <= t < self.config.noise_end:
            return False

        # 3. 점심 시간 체크
        if self.config.lunch_start <= t < self.config.lunch_end:
            return False

        # 4. 장 마감 직전 체크 (신규 진입 금지)
        if t >= self.config.close_warning:
            return False

        # 5. 이벤트 블랙아웃 체크
        for start, end in self.event_blackouts:
            if start <= dt < end:
                return False

        # 6. 매시 정각 ±N분 체크
        if self.config.exclude_around_hour:
            minute = dt.minute
            if minute < self.config.exclude_minutes_after_hour:
                return False
            if minute >= (60 - self.config.exclude_minutes_before_hour):
                return False

        return True

    def should_force_close(self, dt: datetime) -> bool:
        """
        강제 청산해야 하는지 확인 (장 마감 직전)

        Args:
            dt: 확인할 시간

        Returns:
            강제 청산 필요 여부
        """
        t = dt.time()
        return t >= self.config.force_close

    def get_exclusion_reason(self, dt: datetime) -> Optional[str]:
        """
        거래 제외 사유 반환 (디버깅용)

        Args:
            dt: 확인할 시간

        Returns:
            제외 사유 또는 None (거래 가능 시)
        """
        t = dt.time()

        if not self.is_trading_hours(dt):
            return "시장 외 시간"

        if self.config.noise_start <= t < self.config.noise_end:
            return "노이즈 구간 (시가 변동)"

        if self.config.lunch_start <= t < self.config.lunch_end:
            return "점심 시간"

        if t >= self.config.close_warning:
            return "장 마감 직전"

        for start, end in self.event_blackouts:
            if start <= dt < end:
                return f"이벤트 블랙아웃 ({start}~{end})"

        if self.config.exclude_around_hour:
            minute = dt.minute
            if minute < self.config.exclude_minutes_after_hour:
                return f"매시 정각 직후 ({minute}분)"
            if minute >= (60 - self.config.exclude_minutes_before_hour):
                return f"매시 정각 직전 ({minute}분)"

        return None

    def get_tradeable_minutes(self, date: datetime) -> int:
        """
        해당 날짜의 거래 가능 시간(분) 계산

        Args:
            date: 날짜

        Returns:
            거래 가능 분 수
        """
        total_minutes = 0

        # 09:00 ~ 15:45 (405분)
        for hour in range(9, 16):
            for minute in range(60):
                if hour == 15 and minute >= 45:
                    break

                dt = datetime(date.year, date.month, date.day, hour, minute)
                if self.can_open_position(dt):
                    total_minutes += 1

        return total_minutes


class OptionsExpiryFilter:
    """
    옵션 만기일 필터

    KOSPI200 옵션 만기일 (매월 두 번째 목요일):
    - 만기일 당일: 14:30~15:30 거래 회피
    - 만기 주간: 변동성 증가 주의
    """

    def __init__(self):
        # 옵션 만기일 목록 (수동 관리 또는 계산)
        self.expiry_dates: List[datetime] = []

    def add_expiry_date(self, date: datetime):
        """옵션 만기일 추가"""
        self.expiry_dates.append(date.date())

    def is_expiry_day(self, dt: datetime) -> bool:
        """만기일인지 확인"""
        return dt.date() in [d.date() if hasattr(d, 'date') else d for d in self.expiry_dates]

    def should_avoid_trading(self, dt: datetime) -> bool:
        """
        만기일 거래 회피 시간인지 확인

        만기일 14:30~15:30: 옵션 정산 시간대
        """
        if not self.is_expiry_day(dt):
            return False

        t = dt.time()
        return time(14, 30) <= t <= time(15, 30)
