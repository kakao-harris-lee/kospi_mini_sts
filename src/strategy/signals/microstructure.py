"""
마이크로스트럭처 시그널 계산
OFI, 호가 불균형, 스프레드, 체결 흐름 분석
"""
from dataclasses import dataclass, field
from typing import Deque, Optional, List, Dict, Any
from collections import deque
import numpy as np
import logging

logger = logging.getLogger(__name__)


@dataclass
class OrderBookSnapshot:
    """호가창 스냅샷"""
    bid_price: float      # 최우선 매수호가
    ask_price: float      # 최우선 매도호가
    bid_qty: int          # 매수 잔량
    ask_qty: int          # 매도 잔량
    timestamp: float = 0  # 타임스탬프

    @property
    def spread(self) -> float:
        """스프레드 (포인트)"""
        return self.ask_price - self.bid_price

    @property
    def mid_price(self) -> float:
        """중간가"""
        return (self.bid_price + self.ask_price) / 2

    @property
    def imbalance(self) -> float:
        """호가 불균형 (-1 ~ 1)"""
        total = self.bid_qty + self.ask_qty
        if total == 0:
            return 0.0
        return (self.bid_qty - self.ask_qty) / total


class OFICalculator:
    """
    OFI (Order Flow Imbalance) 계산기

    OFI = Σ (bid_change - ask_change)

    - bid_change: 매수호가 상승 시 +bid_qty, 하락 시 -bid_qty
    - ask_change: 매도호가 상승 시 -ask_qty, 하락 시 +ask_qty

    양수 OFI → 매수 압력
    음수 OFI → 매도 압력
    """

    def __init__(self, lookback: int = 20, zscore_period: int = 60):
        self.lookback = lookback
        self.zscore_period = zscore_period

        self.prev_snapshot: Optional[OrderBookSnapshot] = None
        self.ofi_values: Deque[float] = deque(maxlen=zscore_period)
        self.cumulative_ofi: float = 0.0

    def update(self, snapshot: OrderBookSnapshot) -> float:
        """
        호가 스냅샷 업데이트 및 OFI 계산

        Args:
            snapshot: 현재 호가 스냅샷

        Returns:
            현재 OFI 값
        """
        if self.prev_snapshot is None:
            self.prev_snapshot = snapshot
            return 0.0

        ofi = self._calculate_ofi(self.prev_snapshot, snapshot)
        self.ofi_values.append(ofi)
        self.cumulative_ofi += ofi

        self.prev_snapshot = snapshot
        return ofi

    def _calculate_ofi(
        self,
        prev: OrderBookSnapshot,
        curr: OrderBookSnapshot
    ) -> float:
        """OFI 계산 (단일 틱)"""
        bid_change = 0.0
        ask_change = 0.0

        # Bid side
        if curr.bid_price > prev.bid_price:
            bid_change = curr.bid_qty
        elif curr.bid_price < prev.bid_price:
            bid_change = -prev.bid_qty
        else:
            bid_change = curr.bid_qty - prev.bid_qty

        # Ask side
        if curr.ask_price > prev.ask_price:
            ask_change = -prev.ask_qty
        elif curr.ask_price < prev.ask_price:
            ask_change = curr.ask_qty
        else:
            ask_change = curr.ask_qty - prev.ask_qty

        return bid_change - ask_change

    @property
    def current_ofi(self) -> float:
        """현재 OFI"""
        return self.ofi_values[-1] if self.ofi_values else 0.0

    @property
    def rolling_ofi(self) -> float:
        """롤링 OFI (lookback 기간 합계)"""
        if len(self.ofi_values) < self.lookback:
            return sum(self.ofi_values)
        return sum(list(self.ofi_values)[-self.lookback:])

    @property
    def ofi_zscore(self) -> float:
        """OFI Z-Score"""
        if len(self.ofi_values) < 10:
            return 0.0

        values = list(self.ofi_values)
        mean = np.mean(values)
        std = np.std(values, ddof=1)

        if std == 0:
            return 0.0

        return (self.current_ofi - mean) / std

    def reset(self):
        """상태 초기화"""
        self.prev_snapshot = None
        self.ofi_values.clear()
        self.cumulative_ofi = 0.0


class OrderBookImbalance:
    """
    호가 불균형 분석기

    Imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty)

    양수 → 매수 우위
    음수 → 매도 우위
    """

    def __init__(self, lookback: int = 20):
        self.lookback = lookback
        self.imbalances: Deque[float] = deque(maxlen=lookback)

    def update(self, bid_qty: int, ask_qty: int) -> float:
        """
        호가 잔량 업데이트

        Args:
            bid_qty: 매수 잔량
            ask_qty: 매도 잔량

        Returns:
            현재 불균형 값 (-1 ~ 1)
        """
        total = bid_qty + ask_qty
        if total == 0:
            imbalance = 0.0
        else:
            imbalance = (bid_qty - ask_qty) / total

        self.imbalances.append(imbalance)
        return imbalance

    @property
    def current(self) -> float:
        """현재 불균형"""
        return self.imbalances[-1] if self.imbalances else 0.0

    @property
    def average(self) -> float:
        """평균 불균형"""
        return np.mean(self.imbalances) if self.imbalances else 0.0

    @property
    def trend(self) -> float:
        """불균형 추세 (최근 - 이전 평균)"""
        if len(self.imbalances) < 5:
            return 0.0

        recent = np.mean(list(self.imbalances)[-5:])
        older = np.mean(list(self.imbalances)[:-5])
        return recent - older

    def is_strong_bid(self, threshold: float = 0.3) -> bool:
        """강한 매수 우위"""
        return self.current > threshold

    def is_strong_ask(self, threshold: float = 0.3) -> bool:
        """강한 매도 우위"""
        return self.current < -threshold

    def reset(self):
        """상태 초기화"""
        self.imbalances.clear()


class SpreadAnalyzer:
    """
    스프레드 분석기

    스프레드가 좁을 때 = 유동성 양호 = 진입 유리
    스프레드가 넓을 때 = 유동성 부족 = 진입 불리
    """

    def __init__(self, lookback: int = 60):
        self.lookback = lookback
        self.spreads: Deque[float] = deque(maxlen=lookback)
        self.tick_size: float = 0.02  # KOSPI Mini 1틱

    def update(self, bid_price: float, ask_price: float) -> float:
        """
        스프레드 업데이트

        Args:
            bid_price: 매수호가
            ask_price: 매도호가

        Returns:
            현재 스프레드 (포인트)
        """
        spread = ask_price - bid_price
        self.spreads.append(spread)
        return spread

    @property
    def current(self) -> float:
        """현재 스프레드"""
        return self.spreads[-1] if self.spreads else self.tick_size

    @property
    def average(self) -> float:
        """평균 스프레드"""
        return np.mean(self.spreads) if self.spreads else self.tick_size

    @property
    def spread_ticks(self) -> int:
        """스프레드 (틱 단위)"""
        return int(round(self.current / self.tick_size))

    def is_tight(self, threshold_ratio: float = 0.8) -> bool:
        """스프레드가 평균보다 좁은지"""
        return self.current < self.average * threshold_ratio

    def is_wide(self, threshold_ratio: float = 1.5) -> bool:
        """스프레드가 평균보다 넓은지"""
        return self.current > self.average * threshold_ratio

    @property
    def percentile(self) -> float:
        """현재 스프레드의 백분위 (0~1)"""
        if len(self.spreads) < 10:
            return 0.5

        sorted_spreads = sorted(self.spreads)
        rank = sum(1 for s in sorted_spreads if s < self.current)
        return rank / len(sorted_spreads)

    def reset(self):
        """상태 초기화"""
        self.spreads.clear()


class TradeFlowAnalyzer:
    """
    체결 흐름 분석기

    체결 강도 = buy_volume / total_volume
    > 0.5 → 매수 우위
    < 0.5 → 매도 우위
    """

    def __init__(self, lookback: int = 20):
        self.lookback = lookback
        self.buy_volumes: Deque[int] = deque(maxlen=lookback)
        self.sell_volumes: Deque[int] = deque(maxlen=lookback)
        self.trade_counts: Deque[int] = deque(maxlen=lookback)

    def update(
        self,
        price: float,
        volume: int,
        bid_price: float,
        ask_price: float
    ):
        """
        체결 데이터 업데이트

        Args:
            price: 체결가
            volume: 체결량
            bid_price: 매수호가
            ask_price: 매도호가
        """
        mid = (bid_price + ask_price) / 2

        # 체결가가 중간가보다 높으면 매수 체결로 판단
        if price >= mid:
            self.buy_volumes.append(volume)
            self.sell_volumes.append(0)
        else:
            self.buy_volumes.append(0)
            self.sell_volumes.append(volume)

        self.trade_counts.append(1)

    @property
    def buy_volume_ratio(self) -> float:
        """매수 체결 비율 (0~1)"""
        total_buy = sum(self.buy_volumes)
        total_sell = sum(self.sell_volumes)
        total = total_buy + total_sell

        if total == 0:
            return 0.5

        return total_buy / total

    @property
    def trade_velocity(self) -> float:
        """체결 속도 (분당 체결 수)"""
        return sum(self.trade_counts)

    def is_aggressive_buying(self, threshold: float = 0.6) -> bool:
        """공격적 매수 체결"""
        return self.buy_volume_ratio > threshold

    def is_aggressive_selling(self, threshold: float = 0.4) -> bool:
        """공격적 매도 체결"""
        return self.buy_volume_ratio < threshold

    def reset(self):
        """상태 초기화"""
        self.buy_volumes.clear()
        self.sell_volumes.clear()
        self.trade_counts.clear()


@dataclass
class MicrostructureSignals:
    """
    마이크로스트럭처 시그널 통합 클래스

    모든 시그널을 하나로 묶어서 관리
    """
    ofi_calculator: OFICalculator = field(default_factory=OFICalculator)
    imbalance: OrderBookImbalance = field(default_factory=OrderBookImbalance)
    spread: SpreadAnalyzer = field(default_factory=SpreadAnalyzer)
    trade_flow: TradeFlowAnalyzer = field(default_factory=TradeFlowAnalyzer)

    def update_orderbook(self, snapshot: OrderBookSnapshot):
        """호가창 업데이트"""
        self.ofi_calculator.update(snapshot)
        self.imbalance.update(snapshot.bid_qty, snapshot.ask_qty)
        self.spread.update(snapshot.bid_price, snapshot.ask_price)

    def update_trade(
        self,
        price: float,
        volume: int,
        bid_price: float,
        ask_price: float
    ):
        """체결 업데이트"""
        self.trade_flow.update(price, volume, bid_price, ask_price)

    def get_signals(self) -> Dict[str, Any]:
        """모든 시그널 반환"""
        return {
            # OFI
            'ofi': self.ofi_calculator.current_ofi,
            'ofi_rolling': self.ofi_calculator.rolling_ofi,
            'ofi_zscore': self.ofi_calculator.ofi_zscore,

            # 호가 불균형
            'imbalance': self.imbalance.current,
            'imbalance_avg': self.imbalance.average,
            'imbalance_trend': self.imbalance.trend,

            # 스프레드
            'spread': self.spread.current,
            'spread_avg': self.spread.average,
            'spread_tight': self.spread.is_tight(),
            'spread_wide': self.spread.is_wide(),

            # 체결 흐름
            'buy_volume_ratio': self.trade_flow.buy_volume_ratio,
            'trade_velocity': self.trade_flow.trade_velocity,
            'aggressive_buying': self.trade_flow.is_aggressive_buying(),
            'aggressive_selling': self.trade_flow.is_aggressive_selling(),
        }

    def get_composite_signal(self) -> float:
        """
        복합 시그널 (-1 ~ 1)

        양수 → 매수 시그널
        음수 → 매도 시그널
        """
        signals = []

        # OFI Z-Score (정규화)
        ofi_z = self.ofi_calculator.ofi_zscore
        signals.append(np.clip(ofi_z / 3, -1, 1))  # ±3σ 를 ±1로

        # 호가 불균형 (-1 ~ 1)
        signals.append(self.imbalance.current)

        # 체결 강도 (0~1 → -1~1)
        buy_ratio = self.trade_flow.buy_volume_ratio
        signals.append((buy_ratio - 0.5) * 2)

        # 가중 평균
        weights = [0.4, 0.3, 0.3]  # OFI, 불균형, 체결강도
        return sum(s * w for s, w in zip(signals, weights))

    def reset(self):
        """모든 시그널 초기화"""
        self.ofi_calculator.reset()
        self.imbalance.reset()
        self.spread.reset()
        self.trade_flow.reset()
