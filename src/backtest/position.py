"""
포지션 관리자
상태 머신: FLAT → LONG/SHORT → FLAT
"""
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class PositionSide(Enum):
    """포지션 방향"""
    FLAT = "FLAT"
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class Trade:
    """거래 기록"""
    timestamp: datetime
    side: str              # "BUY" or "SELL"
    price: float
    quantity: int
    commission: float = 0.0
    slippage: float = 0.0
    pnl: float = 0.0       # 청산 시 손익


@dataclass
class Position:
    """포지션 상태"""
    side: PositionSide = PositionSide.FLAT
    entry_price: float = 0.0
    quantity: int = 0
    entry_time: Optional[datetime] = None
    unrealized_pnl: float = 0.0
    highest_price: float = 0.0  # 트레일링 스탑용
    lowest_price: float = 0.0   # 트레일링 스탑용

    def update_price(self, current_price: float, point_value: float = 1.0):
        """현재가 업데이트 및 미실현 손익 계산 (KRW 기준)"""
        if self.side == PositionSide.FLAT:
            self.unrealized_pnl = 0.0
            return

        # 미실현 손익 계산 (포인트 → KRW)
        if self.side == PositionSide.LONG:
            pnl_points = (current_price - self.entry_price) * self.quantity
        else:  # SHORT
            pnl_points = (self.entry_price - current_price) * self.quantity

        self.unrealized_pnl = pnl_points * point_value

        # 최고/최저가 업데이트
        if current_price > self.highest_price:
            self.highest_price = current_price
        if current_price < self.lowest_price or self.lowest_price == 0:
            self.lowest_price = current_price


class PositionManager:
    """
    포지션 관리자

    상태 머신:
    - FLAT: 포지션 없음
    - LONG: 매수 포지션
    - SHORT: 매도 포지션

    전환:
    - FLAT → LONG: 매수 진입
    - FLAT → SHORT: 매도 진입
    - LONG → FLAT: 매도 청산
    - SHORT → FLAT: 매수 청산
    - LONG → SHORT: 청산 후 반대 포지션 (선택적)
    - SHORT → LONG: 청산 후 반대 포지션 (선택적)
    """

    def __init__(self, initial_capital: float = 10_000_000, point_value: float = 50_000):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.point_value = point_value
        self.position = Position()
        self.trades: List[Trade] = []
        self.equity_curve: List[float] = [initial_capital]

    @property
    def current_equity(self) -> float:
        """현재 자산 (자본 + 미실현 손익)"""
        return self.capital + self.position.unrealized_pnl

    def open_position(
        self,
        side: PositionSide,
        price: float,
        quantity: int,
        timestamp: datetime,
        commission: float = 0.0,
        slippage: float = 0.0
    ) -> bool:
        """
        포지션 진입

        Args:
            side: LONG 또는 SHORT
            price: 진입가
            quantity: 수량
            timestamp: 진입 시간
            commission: 수수료
            slippage: 슬리피지

        Returns:
            성공 여부
        """
        if self.position.side != PositionSide.FLAT:
            logger.warning(f"Already in {self.position.side.value} position")
            return False

        if side == PositionSide.FLAT:
            logger.warning("Cannot open FLAT position")
            return False

        # 슬리피지 적용
        if side == PositionSide.LONG:
            execution_price = price + slippage
        else:
            execution_price = price - slippage

        # 포지션 설정
        self.position = Position(
            side=side,
            entry_price=execution_price,
            quantity=quantity,
            entry_time=timestamp,
            highest_price=execution_price,
            lowest_price=execution_price
        )

        # 수수료 차감 (KRW)
        self.capital -= commission

        # 거래 기록
        trade = Trade(
            timestamp=timestamp,
            side="BUY" if side == PositionSide.LONG else "SELL",
            price=execution_price,
            quantity=quantity,
            commission=commission,
            slippage=slippage
        )
        self.trades.append(trade)

        logger.debug(
            f"Opened {side.value} position: {quantity} @ {execution_price:.2f} "
            f"(commission: {commission:.0f})"
        )
        return True

    def close_position(
        self,
        price: float,
        timestamp: datetime,
        commission: float = 0.0,
        slippage: float = 0.0
    ) -> Optional[float]:
        """
        포지션 청산

        Args:
            price: 청산가
            timestamp: 청산 시간
            commission: 수수료
            slippage: 슬리피지

        Returns:
            실현 손익 (포지션 없으면 None)
        """
        if self.position.side == PositionSide.FLAT:
            logger.warning("No position to close")
            return None

        # 슬리피지 적용
        if self.position.side == PositionSide.LONG:
            execution_price = price - slippage  # 매도 시 불리하게
        else:
            execution_price = price + slippage  # 매수 청산 시 불리하게

        # 손익 계산 (포인트)
        if self.position.side == PositionSide.LONG:
            pnl_points = (execution_price - self.position.entry_price) * self.position.quantity
        else:
            pnl_points = (self.position.entry_price - execution_price) * self.position.quantity

        # 수수료 차감 (포인트로 환산)
        commission_points = commission / self.point_value if self.point_value else 0.0
        pnl_points -= commission_points

        # 자본 업데이트 (KRW)
        pnl_krw = pnl_points * self.point_value

        self.capital += pnl_krw

        # 거래 기록
        trade = Trade(
            timestamp=timestamp,
            side="SELL" if self.position.side == PositionSide.LONG else "BUY",
            price=execution_price,
            quantity=self.position.quantity,
            commission=commission,
            slippage=slippage,
            pnl=pnl_points
        )
        self.trades.append(trade)

        # 자산 곡선 업데이트
        self.equity_curve.append(self.capital)

        logger.debug(
            f"Closed {self.position.side.value} position: "
            f"{self.position.quantity} @ {execution_price:.2f} "
            f"(PnL: {pnl_points:+.0f}pts, commission: {commission:.0f}KRW)"
        )

        # 포지션 초기화
        self.position = Position()

        return pnl_points

    def update(self, current_price: float):
        """현재가 업데이트"""
        self.position.update_price(current_price, point_value=self.point_value)

    def get_stats(self) -> dict:
        """거래 통계 반환"""
        if not self.trades:
            return {
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate': 0.0,
                'total_pnl': 0.0,
                'avg_pnl': 0.0,
                'max_win': 0.0,
                'max_loss': 0.0,
                'profit_factor': 0.0,
            }

        # 청산된 거래만 필터링
        closed_trades = [t for t in self.trades if t.pnl != 0]

        if not closed_trades:
            return {
                'total_trades': len(self.trades),
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate': 0.0,
                'total_pnl': 0.0,
                'avg_pnl': 0.0,
                'max_win': 0.0,
                'max_loss': 0.0,
                'profit_factor': 0.0,
            }

        pnls = [t.pnl for t in closed_trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        total_wins = sum(wins) if wins else 0
        total_losses = abs(sum(losses)) if losses else 0

        return {
            'total_trades': len(closed_trades),
            'winning_trades': len(wins),
            'losing_trades': len(losses),
            'win_rate': len(wins) / len(closed_trades) * 100 if closed_trades else 0,
            'total_pnl': sum(pnls),
            'avg_pnl': sum(pnls) / len(pnls) if pnls else 0,
            'max_win': max(pnls) if pnls else 0,
            'max_loss': min(pnls) if pnls else 0,
            'profit_factor': total_wins / total_losses if total_losses > 0 else float('inf'),
        }
