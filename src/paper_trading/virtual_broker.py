"""
Virtual Broker - 가상 주문 실행기

실시간 호가 데이터를 사용하여 가상의 주문을 체결합니다.
- 시장가 주문: 즉시 체결 (슬리피지 적용)
- 지정가 주문: 호가 도달 시 체결
- 포지션 관리: 진입/청산 추적
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Callable
import uuid

from src.common.metrics import get_metrics


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


@dataclass
class VirtualOrder:
    """가상 주문"""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.MARKET
    price: float = 0.0  # 지정가일 때만 사용
    quantity: int = 1
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    filled_at: Optional[datetime] = None
    filled_price: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'side': self.side.value,
            'order_type': self.order_type.value,
            'price': self.price,
            'quantity': self.quantity,
            'status': self.status.value,
            'created_at': self.created_at.isoformat(),
            'filled_at': self.filled_at.isoformat() if self.filled_at else None,
            'filled_price': self.filled_price,
            'commission': self.commission,
        }


@dataclass
class VirtualPosition:
    """가상 포지션"""
    side: PositionSide = PositionSide.FLAT
    quantity: int = 0
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.side != PositionSide.FLAT and self.quantity > 0

    def to_dict(self) -> dict:
        return {
            'side': self.side.value,
            'quantity': self.quantity,
            'entry_price': self.entry_price,
            'entry_time': self.entry_time.isoformat() if self.entry_time else None,
            'unrealized_pnl': self.unrealized_pnl,
            'realized_pnl': self.realized_pnl,
        }


@dataclass
class TradeRecord:
    """거래 기록"""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    entry_time: datetime = field(default_factory=datetime.now)
    exit_time: Optional[datetime] = None
    side: PositionSide = PositionSide.LONG
    entry_price: float = 0.0
    exit_price: float = 0.0
    quantity: int = 1
    pnl: float = 0.0  # 포인트
    pnl_amount: float = 0.0  # KRW
    commission: float = 0.0
    exit_reason: str = ""


class VirtualBroker:
    """
    가상 브로커

    실시간 데이터 기반 가상 주문 실행기
    """

    def __init__(
        self,
        initial_capital: float = 10_000_000,
        tick_value: float = 250_000,  # KOSPI200 선물 1틱 = 0.05포인트 * 50만원 = 25,000원
        tick_size: float = 0.05,
        commission_rate: float = 0.00015,  # 0.015%
        slippage_ticks: int = 1,
        strategy_name: str = "paper_trading",
        symbol: str = "A05601",
    ):
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.tick_value = tick_value
        self.tick_size = tick_size
        self.commission_rate = commission_rate
        self.slippage_ticks = slippage_ticks
        self.strategy_name = strategy_name
        self.symbol = symbol

        # Prometheus metrics
        self._metrics = get_metrics()

        # 상태
        self.position = VirtualPosition()
        self.pending_orders: List[VirtualOrder] = []
        self.filled_orders: List[VirtualOrder] = []
        self.trades: List[TradeRecord] = []

        # 콜백
        self.on_fill: Optional[Callable[[VirtualOrder], None]] = None
        self.on_trade_close: Optional[Callable[[TradeRecord], None]] = None

        # 현재 시장 가격
        self._current_price: float = 0.0
        self._current_bid: float = 0.0
        self._current_ask: float = 0.0

    def update_price(self, price: float, bid: float = None, ask: float = None):
        """
        시장 가격 업데이트

        :param price: 현재가
        :param bid: 매수 호가 (없으면 price 사용)
        :param ask: 매도 호가 (없으면 price 사용)
        """
        self._current_price = price
        self._current_bid = bid or price
        self._current_ask = ask or price

        # 미결 주문 체크
        self._check_pending_orders()

        # 포지션 평가
        self._update_position_pnl()

    def submit_order(
        self,
        side: OrderSide,
        quantity: int = 1,
        order_type: OrderType = OrderType.MARKET,
        price: float = None,
    ) -> VirtualOrder:
        """
        주문 제출

        :param side: BUY or SELL
        :param quantity: 수량
        :param order_type: MARKET or LIMIT
        :param price: 지정가 (LIMIT일 때만)
        :return: 주문 객체
        """
        order = VirtualOrder(
            side=side,
            order_type=order_type,
            price=price or 0.0,
            quantity=quantity,
            status=OrderStatus.PENDING,
        )

        if order_type == OrderType.MARKET:
            # 시장가 즉시 체결
            self._fill_order(order)
        else:
            # 지정가는 대기열에 추가
            self.pending_orders.append(order)

        return order

    def cancel_order(self, order_id: str) -> bool:
        """주문 취소"""
        for order in self.pending_orders:
            if order.id == order_id:
                order.status = OrderStatus.CANCELLED
                self.pending_orders.remove(order)
                return True
        return False

    def close_position(self, reason: str = "MANUAL") -> Optional[TradeRecord]:
        """현재 포지션 청산"""
        if not self.position.is_open:
            return None

        # 청산 방향 결정
        close_side = OrderSide.SELL if self.position.side == PositionSide.LONG else OrderSide.BUY

        # 시장가 청산 주문
        order = self.submit_order(
            side=close_side,
            quantity=self.position.quantity,
            order_type=OrderType.MARKET,
        )

        # 마지막 거래 기록에 청산 사유 추가
        if self.trades and self.trades[-1].exit_time:
            self.trades[-1].exit_reason = reason

        return self.trades[-1] if self.trades else None

    def _fill_order(self, order: VirtualOrder):
        """주문 체결 처리"""
        # 체결 가격 계산 (슬리피지 포함)
        slippage = self.slippage_ticks * self.tick_size
        if order.side == OrderSide.BUY:
            fill_price = self._current_ask + slippage
        else:
            fill_price = self._current_bid - slippage

        # 수수료 계산
        notional = fill_price * self.tick_value / self.tick_size * order.quantity
        commission = notional * self.commission_rate

        # 주문 업데이트
        order.status = OrderStatus.FILLED
        order.filled_at = datetime.now()
        order.filled_price = fill_price
        order.commission = commission
        order.slippage = slippage * order.quantity

        self.filled_orders.append(order)

        # 포지션 처리
        self._update_position(order)

        # 콜백
        if self.on_fill:
            self.on_fill(order)

    def _update_position(self, order: VirtualOrder):
        """주문 체결 후 포지션 업데이트"""
        if not self.position.is_open:
            # 신규 진입
            self.position = VirtualPosition(
                side=PositionSide.LONG if order.side == OrderSide.BUY else PositionSide.SHORT,
                quantity=order.quantity,
                entry_price=order.filled_price,
                entry_time=order.filled_at,
            )
            # 거래 기록 시작
            self.trades.append(TradeRecord(
                entry_time=order.filled_at,
                side=self.position.side,
                entry_price=order.filled_price,
                quantity=order.quantity,
                commission=order.commission,
            ))
            self.capital -= order.commission

        else:
            # 청산 또는 반대매매
            is_closing = (
                (self.position.side == PositionSide.LONG and order.side == OrderSide.SELL) or
                (self.position.side == PositionSide.SHORT and order.side == OrderSide.BUY)
            )

            if is_closing:
                # PnL 계산
                if self.position.side == PositionSide.LONG:
                    pnl_points = order.filled_price - self.position.entry_price
                else:
                    pnl_points = self.position.entry_price - order.filled_price

                pnl_amount = (pnl_points / self.tick_size) * self.tick_value * order.quantity

                # 거래 기록 완료
                if self.trades:
                    trade = self.trades[-1]
                    trade.exit_time = order.filled_at
                    trade.exit_price = order.filled_price
                    trade.pnl = pnl_points
                    trade.pnl_amount = pnl_amount
                    trade.commission += order.commission

                    # 콜백
                    if self.on_trade_close:
                        self.on_trade_close(trade)

                # 자본 업데이트
                self.capital += pnl_amount - order.commission
                self.position.realized_pnl += pnl_amount

                # 포지션 초기화
                self.position = VirtualPosition()
            else:
                # 증거래 (동일 방향 추가)
                total_qty = self.position.quantity + order.quantity
                avg_price = (
                    self.position.entry_price * self.position.quantity +
                    order.filled_price * order.quantity
                ) / total_qty

                self.position.quantity = total_qty
                self.position.entry_price = avg_price
                self.capital -= order.commission

    def _check_pending_orders(self):
        """대기 중인 지정가 주문 체결 확인"""
        filled = []
        for order in self.pending_orders:
            if order.order_type == OrderType.LIMIT:
                # 매수 지정가: 현재가가 주문가 이하
                if order.side == OrderSide.BUY and self._current_price <= order.price:
                    order.filled_price = order.price
                    self._fill_limit_order(order)
                    filled.append(order)
                # 매도 지정가: 현재가가 주문가 이상
                elif order.side == OrderSide.SELL and self._current_price >= order.price:
                    order.filled_price = order.price
                    self._fill_limit_order(order)
                    filled.append(order)

        for order in filled:
            self.pending_orders.remove(order)

    def _fill_limit_order(self, order: VirtualOrder):
        """지정가 주문 체결"""
        notional = order.price * self.tick_value / self.tick_size * order.quantity
        commission = notional * self.commission_rate

        order.status = OrderStatus.FILLED
        order.filled_at = datetime.now()
        order.commission = commission

        self.filled_orders.append(order)
        self._update_position(order)

        if self.on_fill:
            self.on_fill(order)

    def _update_position_pnl(self):
        """포지션 평가손익 업데이트 및 Prometheus 메트릭 발행"""
        if not self.position.is_open:
            # No position - set metrics to 0
            self._metrics.update_position_pnl(self.strategy_name, self.symbol, 0)
            return

        if self.position.side == PositionSide.LONG:
            pnl_points = self._current_price - self.position.entry_price
        else:
            pnl_points = self.position.entry_price - self._current_price

        self.position.unrealized_pnl = (
            (pnl_points / self.tick_size) * self.tick_value * self.position.quantity
        )

        # Update Prometheus metrics
        self._metrics.update_position_pnl(
            self.strategy_name, self.symbol, self.position.unrealized_pnl
        )
        self._metrics.update_daily_pnl(self.strategy_name, self.total_pnl)

    @property
    def equity(self) -> float:
        """현재 평가 자산"""
        return self.capital + self.position.unrealized_pnl

    @property
    def total_pnl(self) -> float:
        """총 손익 (실현 + 미실현)"""
        return sum(t.pnl_amount for t in self.trades if t.exit_time) + self.position.unrealized_pnl

    @property
    def total_return(self) -> float:
        """총 수익률 (%)"""
        return (self.equity - self.initial_capital) / self.initial_capital * 100

    def get_summary(self) -> dict:
        """거래 요약"""
        closed_trades = [t for t in self.trades if t.exit_time]
        winning = [t for t in closed_trades if t.pnl_amount > 0]
        losing = [t for t in closed_trades if t.pnl_amount < 0]

        return {
            'initial_capital': self.initial_capital,
            'current_capital': self.capital,
            'equity': self.equity,
            'total_return': self.total_return,
            'total_pnl': self.total_pnl,
            'total_trades': len(closed_trades),
            'winning_trades': len(winning),
            'losing_trades': len(losing),
            'win_rate': len(winning) / len(closed_trades) * 100 if closed_trades else 0,
            'position': self.position.to_dict() if self.position.is_open else None,
            'pending_orders': len(self.pending_orders),
        }
