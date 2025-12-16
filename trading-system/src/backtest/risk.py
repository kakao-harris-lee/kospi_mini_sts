"""
리스크 관리자
손절, 시간손절, 포지션 사이징
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from enum import Enum
import logging

from .position import Position, PositionSide

logger = logging.getLogger(__name__)


class ExitReason(Enum):
    """청산 사유"""
    SIGNAL = "SIGNAL"           # 시그널에 의한 청산
    STOP_LOSS = "STOP_LOSS"     # 손절
    TAKE_PROFIT = "TAKE_PROFIT" # 익절
    TIME_STOP = "TIME_STOP"     # 시간 손절
    TRAILING_STOP = "TRAILING_STOP"  # 트레일링 스탑
    MAX_LOSS = "MAX_LOSS"       # 일일 최대 손실
    FORCED = "FORCED"           # 강제 청산 (장 마감 등)


@dataclass
class RiskConfig:
    """리스크 설정"""
    # 손절 설정
    stop_loss_points: float = 2.0        # 손절폭 (포인트)
    take_profit_points: float = 4.0      # 익절폭 (포인트)

    # 시간 손절
    time_stop_minutes: int = 30          # N분 경과 시 청산

    # 트레일링 스탑
    trailing_stop_points: float = 1.5    # 트레일링 스탑폭 (포인트)
    trailing_activation_points: float = 1.0  # 트레일링 활성화 수익 (포인트)

    # 일일 리스크 한도
    max_daily_loss: float = 500_000      # 일일 최대 손실 (원)
    max_daily_trades: int = 20           # 일일 최대 거래 횟수

    # 포지션 사이징
    max_position_size: int = 5           # 최대 포지션 크기
    risk_per_trade: float = 0.02         # 거래당 리스크 (자본의 2%)


class RiskManager:
    """
    리스크 관리자

    책임:
    1. 손절/익절 체크
    2. 시간 손절 체크
    3. 트레일링 스탑 관리
    4. 일일 리스크 한도 관리
    5. 포지션 사이징 계산
    """

    def __init__(self, config: RiskConfig = None, point_value: float = 50_000):
        self.config = config or RiskConfig()
        self.point_value = point_value

        # 일일 통계
        self.daily_pnl: float = 0.0
        self.daily_trades: int = 0
        self.current_date: Optional[datetime] = None

    def reset_daily(self, date: datetime):
        """일일 통계 리셋"""
        if self.current_date != date.date():
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self.current_date = date.date()

    def update_daily_stats(self, pnl: float):
        """일일 통계 업데이트"""
        self.daily_pnl += pnl
        self.daily_trades += 1

    def check_exit(
        self,
        position: Position,
        current_price: float,
        current_time: datetime
    ) -> Optional[ExitReason]:
        """
        청산 조건 체크

        Args:
            position: 현재 포지션
            current_price: 현재가
            current_time: 현재 시간

        Returns:
            청산 사유 (청산 조건 충족 시) 또는 None
        """
        if position.side == PositionSide.FLAT:
            return None

        # 1. 일일 최대 손실 체크
        if self.daily_pnl <= -self.config.max_daily_loss:
            logger.warning(f"Daily max loss reached: {self.daily_pnl:.0f}")
            return ExitReason.MAX_LOSS

        # 2. 손절 체크
        if self._check_stop_loss(position, current_price):
            return ExitReason.STOP_LOSS

        # 3. 익절 체크
        if self._check_take_profit(position, current_price):
            return ExitReason.TAKE_PROFIT

        # 4. 시간 손절 체크
        if self._check_time_stop(position, current_time):
            return ExitReason.TIME_STOP

        # 5. 트레일링 스탑 체크
        if self._check_trailing_stop(position, current_price):
            return ExitReason.TRAILING_STOP

        return None

    def _check_stop_loss(self, position: Position, current_price: float) -> bool:
        """손절 체크"""
        if position.side == PositionSide.LONG:
            stop_price = position.entry_price - self.config.stop_loss_points
            return current_price <= stop_price
        else:  # SHORT
            stop_price = position.entry_price + self.config.stop_loss_points
            return current_price >= stop_price

    def _check_take_profit(self, position: Position, current_price: float) -> bool:
        """익절 체크"""
        if position.side == PositionSide.LONG:
            target_price = position.entry_price + self.config.take_profit_points
            return current_price >= target_price
        else:  # SHORT
            target_price = position.entry_price - self.config.take_profit_points
            return current_price <= target_price

    def _check_time_stop(self, position: Position, current_time: datetime) -> bool:
        """시간 손절 체크"""
        if position.entry_time is None:
            return False

        elapsed = current_time - position.entry_time
        max_duration = timedelta(minutes=self.config.time_stop_minutes)
        return elapsed >= max_duration

    def _check_trailing_stop(self, position: Position, current_price: float) -> bool:
        """트레일링 스탑 체크"""
        if position.side == PositionSide.LONG:
            # 활성화 조건: 수익이 활성화 포인트 이상
            profit = current_price - position.entry_price
            if profit < self.config.trailing_activation_points:
                return False

            # 최고가에서 트레일링폭 이상 하락 시 청산
            trailing_stop = position.highest_price - self.config.trailing_stop_points
            return current_price <= trailing_stop

        else:  # SHORT
            profit = position.entry_price - current_price
            if profit < self.config.trailing_activation_points:
                return False

            trailing_stop = position.lowest_price + self.config.trailing_stop_points
            return current_price >= trailing_stop

    def can_trade(self) -> bool:
        """거래 가능 여부 체크"""
        # 일일 최대 손실 초과
        if self.daily_pnl <= -self.config.max_daily_loss:
            logger.warning("Trading disabled: daily max loss reached")
            return False

        # 일일 최대 거래 횟수 초과
        if self.daily_trades >= self.config.max_daily_trades:
            logger.warning("Trading disabled: daily max trades reached")
            return False

        return True

    def calculate_position_size(
        self,
        capital: float,
        entry_price: float,
        stop_price: float
    ) -> int:
        """
        포지션 사이즈 계산 (리스크 기반)

        Args:
            capital: 현재 자본
            entry_price: 진입 예정가
            stop_price: 손절가

        Returns:
            권장 포지션 사이즈 (계약 수)
        """
        # 거래당 최대 손실 금액
        max_risk = capital * self.config.risk_per_trade

        # 계약당 손실 (포인트 * 포인트가치)
        risk_per_contract = abs(entry_price - stop_price) * self.point_value

        if risk_per_contract <= 0:
            return 1

        # 포지션 사이즈 계산
        size = int(max_risk / risk_per_contract)

        # 최소 1계약, 최대 max_position_size
        size = max(1, min(size, self.config.max_position_size))

        return size

    def get_stop_loss_price(self, entry_price: float, side: PositionSide) -> float:
        """손절가 계산"""
        if side == PositionSide.LONG:
            return entry_price - self.config.stop_loss_points
        else:
            return entry_price + self.config.stop_loss_points

    def get_take_profit_price(self, entry_price: float, side: PositionSide) -> float:
        """익절가 계산"""
        if side == PositionSide.LONG:
            return entry_price + self.config.take_profit_points
        else:
            return entry_price - self.config.take_profit_points
