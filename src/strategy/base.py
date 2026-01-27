"""
전략 베이스 클래스
백테스트 엔진과 실시간 시스템 모두에서 사용 가능한 Strategy 인터페이스
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, List, Optional
from enum import Enum
import numpy as np


class Signal(Enum):
    """거래 시그널"""
    HOLD = 0
    BUY = 1
    SELL = -1


class PositionSide(Enum):
    """포지션 방향"""
    FLAT = "FLAT"
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class StrategyState:
    """전략 상태"""
    position: PositionSide = PositionSide.FLAT
    entry_price: float = 0.0
    entry_time: Optional[datetime] = None
    bars_in_position: int = 0

    # 전략별 커스텀 상태
    custom: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BarData:
    """1분봉 데이터 + 피처"""
    # 기본 OHLCV
    datetime: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    # 마이크로스트럭처 (옵션)
    ofi: float = 0.0
    ofi_zscore: float = 0.0  # OFI Z-Score
    bid_ask_imbalance: float = 0.0
    spread: float = 0.0
    buy_volume_ratio: float = 0.5

    # 변동성 지표 (옵션)
    atr: float = 0.0
    hv: float = 0.0  # Historical Volatility

    # LSTM 예측 (옵션) - single horizon (legacy)
    up_prob: float = 0.5
    down_prob: float = 0.5

    # Multi-horizon predictions (ensemble)
    up_prob_h1: float = 0.5   # 1-min horizon
    up_prob_h3: float = 0.5   # 3-min horizon
    up_prob_h5: float = 0.5   # 5-min horizon
    up_prob_h10: float = 0.5  # 10-min horizon

    # 변동성 레짐 (None = not yet determined, requires warm-up period)
    regime: Optional[str] = None

    # Orderbook data
    best_bid: float = 0.0
    best_ask: float = 0.0
    bid_qty1: float = 0.0
    bid_qty2: float = 0.0
    bid_qty3: float = 0.0
    ask_qty1: float = 0.0
    ask_qty2: float = 0.0
    ask_qty3: float = 0.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BarData":
        """딕셔너리에서 BarData 생성"""
        return cls(
            datetime=data.get('datetime'),
            open=data.get('open', 0),
            high=data.get('high', 0),
            low=data.get('low', 0),
            close=data.get('close', 0),
            volume=data.get('volume', 0),
            ofi=data.get('ofi', 0),
            ofi_zscore=data.get('ofi_zscore', 0),
            bid_ask_imbalance=data.get('bid_ask_imbalance', 0),
            spread=data.get('spread', 0),
            buy_volume_ratio=data.get('buy_volume_ratio', 0.5),
            atr=data.get('atr', 0),
            hv=data.get('hv', 0),
            up_prob=data.get('up_prob', 0.5),
            down_prob=data.get('down_prob', 0.5),
            # Multi-horizon predictions
            up_prob_h1=data.get('up_prob_h1', 0.5),
            up_prob_h3=data.get('up_prob_h3', 0.5),
            up_prob_h5=data.get('up_prob_h5', 0.5),
            up_prob_h10=data.get('up_prob_h10', 0.5),
            regime=data.get('regime'),  # None if not provided (warm-up period)
            # Orderbook data
            best_bid=data.get('best_bid', data.get('bid_price_1', 0)),
            best_ask=data.get('best_ask', data.get('ask_price_1', 0)),
            bid_qty1=data.get('bid_qty1', data.get('bid_qty_1', 0)),
            bid_qty2=data.get('bid_qty2', data.get('bid_qty_2', 0)),
            bid_qty3=data.get('bid_qty3', data.get('bid_qty_3', 0)),
            ask_qty1=data.get('ask_qty1', data.get('ask_qty_1', 0)),
            ask_qty2=data.get('ask_qty2', data.get('ask_qty_2', 0)),
            ask_qty3=data.get('ask_qty3', data.get('ask_qty_3', 0)),
        )


class BaseStrategy(ABC):
    """
    전략 베이스 클래스

    모든 전략은 이 클래스를 상속받아 구현
    백테스트 엔진의 Strategy 프로토콜과 호환
    """

    def __init__(self, name: str = "BaseStrategy"):
        self.name = name
        self.state = StrategyState()
        self.history: List[BarData] = []
        self.max_history: int = 100  # 최대 보관 바 수

    @abstractmethod
    def generate_signal(self, bar: BarData) -> Signal:
        """
        시그널 생성 (서브클래스에서 구현)

        Args:
            bar: 현재 바 데이터

        Returns:
            Signal: BUY, SELL, or HOLD
        """
        pass

    def on_bar(self, bar: Dict[str, Any]) -> Signal:
        """
        백테스트 엔진 호환 인터페이스

        Args:
            bar: 딕셔너리 형태의 바 데이터

        Returns:
            Signal
        """
        bar_data = BarData.from_dict(bar)

        # 히스토리 업데이트
        self.history.append(bar_data)
        if len(self.history) > self.max_history:
            self.history.pop(0)

        # 포지션 보유 중이면 바 카운트 증가
        if self.state.position != PositionSide.FLAT:
            self.state.bars_in_position += 1

        return self.generate_signal(bar_data)

    def update_position(self, side: PositionSide, price: float, time: datetime):
        """포지션 상태 업데이트"""
        self.state.position = side
        self.state.entry_price = price
        self.state.entry_time = time
        self.state.bars_in_position = 0

    def close_position(self):
        """포지션 청산"""
        self.state = StrategyState()

    def get_recent_prices(self, n: int = 20) -> List[float]:
        """최근 N개 종가 반환"""
        return [bar.close for bar in self.history[-n:]]

    def get_recent_highs(self, n: int = 20) -> List[float]:
        """최근 N개 고가 반환"""
        return [bar.high for bar in self.history[-n:]]

    def get_recent_lows(self, n: int = 20) -> List[float]:
        """최근 N개 저가 반환"""
        return [bar.low for bar in self.history[-n:]]

    def get_recent_volumes(self, n: int = 20) -> List[int]:
        """최근 N개 거래량 반환"""
        return [bar.volume for bar in self.history[-n:]]

    def sma(self, period: int) -> float:
        """단순 이동평균"""
        prices = self.get_recent_prices(period)
        if len(prices) < period:
            return 0.0
        return np.mean(prices)

    def ema(self, period: int) -> float:
        """지수 이동평균"""
        prices = self.get_recent_prices(period)
        if len(prices) < period:
            return 0.0

        multiplier = 2 / (period + 1)
        ema = prices[0]
        for price in prices[1:]:
            ema = (price - ema) * multiplier + ema
        return ema

    def std(self, period: int) -> float:
        """표준편차"""
        prices = self.get_recent_prices(period)
        if len(prices) < period:
            return 0.0
        return np.std(prices, ddof=1)

    def bollinger_bands(self, period: int = 20, num_std: float = 2.0) -> tuple:
        """
        볼린저 밴드 계산

        Returns:
            (upper, middle, lower)
        """
        middle = self.sma(period)
        std = self.std(period)
        upper = middle + num_std * std
        lower = middle - num_std * std
        return upper, middle, lower

    def highest(self, period: int) -> float:
        """기간 내 최고가"""
        highs = self.get_recent_highs(period)
        return max(highs) if highs else 0.0

    def lowest(self, period: int) -> float:
        """기간 내 최저가"""
        lows = self.get_recent_lows(period)
        return min(lows) if lows else float('inf')

    def reset(self):
        """전략 상태 초기화"""
        self.state = StrategyState()
        self.history.clear()
