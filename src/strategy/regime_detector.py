"""
변동성 레짐 판단기
ATR, HV 기반으로 시장 상태를 분류
"""
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Deque
from collections import deque
import numpy as np
import logging

logger = logging.getLogger(__name__)


class VolatilityRegime(Enum):
    """변동성 레짐"""
    LOW = "LOW"         # 저변동성 (박스권) - Mean Reversion
    MEDIUM = "MEDIUM"   # 일반 - 관망 또는 약한 시그널
    HIGH = "HIGH"       # 고변동성 (추세) - Breakout/Momentum


@dataclass
class RegimeConfig:
    """레짐 판단 설정"""
    # ATR 기반 설정
    atr_period: int = 14              # ATR 계산 기간
    atr_low_threshold: float = 0.3    # 저변동성 임계값 (ATR 백분위)
    atr_high_threshold: float = 0.7   # 고변동성 임계값 (ATR 백분위)

    # HV (Historical Volatility) 설정
    hv_period: int = 20               # 변동성 계산 기간
    hv_lookback: int = 60             # 백분위 계산 룩백

    # 거래량 급증 감지
    volume_surge_threshold: float = 2.0  # 평균 대비 배수

    # 레인지 확장
    range_expansion_threshold: float = 1.5  # ATR 대비 배수

    # 스무딩 (급격한 레짐 변화 방지)
    regime_smoothing: int = 3         # N분 연속 같은 레짐이어야 전환


class RegimeDetector:
    """
    변동성 레짐 판단기

    레짐별 전략:
    - LOW: Mean Reversion (역추세) - 밴드 터치 시 반대 진입
    - MEDIUM: No Trade (관망) 또는 약한 시그널만 따름
    - HIGH: Breakout/Momentum (추세 추종) - 돌파 시 방향 진입
    """

    def __init__(self, config: RegimeConfig = None):
        self.config = config or RegimeConfig()

        # 가격 데이터 버퍼
        self.highs: Deque[float] = deque(maxlen=100)
        self.lows: Deque[float] = deque(maxlen=100)
        self.closes: Deque[float] = deque(maxlen=100)
        self.volumes: Deque[int] = deque(maxlen=100)

        # ATR 히스토리 (백분위 계산용)
        self.atr_history: Deque[float] = deque(maxlen=self.config.hv_lookback)

        # HV 히스토리
        self.hv_history: Deque[float] = deque(maxlen=self.config.hv_lookback)

        # 레짐 히스토리 (스무딩용)
        self.regime_history: Deque[VolatilityRegime] = deque(maxlen=10)

        # 현재 레짐
        self._current_regime = VolatilityRegime.MEDIUM
        self._confirmed_regime = VolatilityRegime.MEDIUM

    def update(self, high: float, low: float, close: float, volume: int):
        """
        가격 데이터 업데이트

        Args:
            high: 고가
            low: 저가
            close: 종가
            volume: 거래량
        """
        self.highs.append(high)
        self.lows.append(low)
        self.closes.append(close)
        self.volumes.append(volume)

        # ATR, HV 계산 및 저장
        atr = self.calculate_atr()
        if atr > 0:
            self.atr_history.append(atr)

        hv = self.calculate_hv()
        if hv > 0:
            self.hv_history.append(hv)

        # 레짐 판단
        regime = self._detect_regime()
        self.regime_history.append(regime)

        # 스무딩 적용
        self._update_confirmed_regime()

    def calculate_atr(self) -> float:
        """
        ATR (Average True Range) 계산

        TR = max(H-L, |H-Cp|, |L-Cp|)
        ATR = SMA(TR, period)
        """
        if len(self.highs) < self.config.atr_period + 1:
            return 0.0

        trs = []
        for i in range(-self.config.atr_period, 0):
            h = self.highs[i]
            l = self.lows[i]
            cp = self.closes[i - 1]  # 전일 종가

            tr = max(h - l, abs(h - cp), abs(l - cp))
            trs.append(tr)

        return np.mean(trs)

    def calculate_hv(self) -> float:
        """
        Historical Volatility (역사적 변동성) 계산

        수익률의 표준편차 * sqrt(252) (연환산)
        """
        if len(self.closes) < self.config.hv_period + 1:
            return 0.0

        closes = list(self.closes)[-self.config.hv_period - 1:]
        returns = []

        for i in range(1, len(closes)):
            ret = np.log(closes[i] / closes[i - 1])
            returns.append(ret)

        if not returns:
            return 0.0

        # 일중 변동성 (분 단위 데이터이므로 조정 필요)
        # 하루 약 400분 거래 가정
        return np.std(returns, ddof=1) * np.sqrt(400)

    def get_atr_percentile(self) -> float:
        """현재 ATR의 백분위 (0~1)"""
        if len(self.atr_history) < 10:
            return 0.5

        current_atr = self.atr_history[-1]
        sorted_atrs = sorted(self.atr_history)
        rank = sum(1 for a in sorted_atrs if a < current_atr)
        return rank / len(sorted_atrs)

    def get_hv_percentile(self) -> float:
        """현재 HV의 백분위 (0~1)"""
        if len(self.hv_history) < 10:
            return 0.5

        current_hv = self.hv_history[-1]
        sorted_hvs = sorted(self.hv_history)
        rank = sum(1 for h in sorted_hvs if h < current_hv)
        return rank / len(sorted_hvs)

    def is_volume_surge(self) -> bool:
        """거래량 급증 여부"""
        if len(self.volumes) < 20:
            return False

        avg_volume = np.mean(list(self.volumes)[-20:-1])  # 최근 20분 평균 (현재 제외)
        current_volume = self.volumes[-1]

        return bool(current_volume > avg_volume * self.config.volume_surge_threshold)

    def is_range_expansion(self) -> bool:
        """레인지 확장 여부 (현재 바의 범위가 ATR보다 큰지)"""
        if len(self.highs) < 2:
            return False

        current_range = self.highs[-1] - self.lows[-1]
        atr = self.calculate_atr()

        if atr <= 0:
            return False

        return bool(current_range > atr * self.config.range_expansion_threshold)

    def _detect_regime(self) -> VolatilityRegime:
        """레짐 판단 (내부)"""
        atr_pct = self.get_atr_percentile()
        hv_pct = self.get_hv_percentile()

        # 복합 변동성 점수 (ATR + HV 평균)
        vol_score = (atr_pct + hv_pct) / 2

        # 거래량 급증 시 HIGH 쪽으로 조정
        if self.is_volume_surge():
            vol_score = min(1.0, vol_score + 0.2)

        # 레인지 확장 시 HIGH 쪽으로 조정
        if self.is_range_expansion():
            vol_score = min(1.0, vol_score + 0.15)

        # 레짐 결정
        if vol_score < self.config.atr_low_threshold:
            return VolatilityRegime.LOW
        elif vol_score > self.config.atr_high_threshold:
            return VolatilityRegime.HIGH
        else:
            return VolatilityRegime.MEDIUM

    def _update_confirmed_regime(self):
        """스무딩 적용하여 확정 레짐 업데이트"""
        if len(self.regime_history) < self.config.regime_smoothing:
            return

        # 최근 N개가 모두 같으면 레짐 전환
        recent = list(self.regime_history)[-self.config.regime_smoothing:]
        if all(r == recent[0] for r in recent):
            if self._confirmed_regime != recent[0]:
                logger.info(f"Regime changed: {self._confirmed_regime.value} -> {recent[0].value}")
                self._confirmed_regime = recent[0]

    @property
    def current_regime(self) -> VolatilityRegime:
        """현재 확정 레짐"""
        return self._confirmed_regime

    @property
    def raw_regime(self) -> VolatilityRegime:
        """스무딩 전 원시 레짐"""
        return self._current_regime if self.regime_history else VolatilityRegime.MEDIUM

    @property
    def atr(self) -> float:
        """현재 ATR"""
        return self.atr_history[-1] if self.atr_history else 0.0

    @property
    def hv(self) -> float:
        """현재 HV"""
        return self.hv_history[-1] if self.hv_history else 0.0

    def get_regime_info(self) -> dict:
        """레짐 정보 반환"""
        return {
            'regime': self.current_regime.value,
            'raw_regime': self.raw_regime.value,
            'atr': self.atr,
            'atr_percentile': self.get_atr_percentile(),
            'hv': self.hv,
            'hv_percentile': self.get_hv_percentile(),
            'volume_surge': self.is_volume_surge(),
            'range_expansion': self.is_range_expansion(),
        }

    def reset(self):
        """상태 초기화"""
        self.highs.clear()
        self.lows.clear()
        self.closes.clear()
        self.volumes.clear()
        self.atr_history.clear()
        self.hv_history.clear()
        self.regime_history.clear()
        self._current_regime = VolatilityRegime.MEDIUM
        self._confirmed_regime = VolatilityRegime.MEDIUM
