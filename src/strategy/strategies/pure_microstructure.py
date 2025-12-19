"""
Pure Microstructure Strategy (순수 마이크로스트럭처 전략)

LSTM 없이 마이크로스트럭처 시그널만으로 트레이딩
- OFI (Order Flow Imbalance)
- 호가 불균형 (Order Book Imbalance)
- 스프레드 분석
- 변동성 레짐

복합 스코어링 방식으로 신뢰도 높은 시그널 생성
"""
from dataclasses import dataclass, field
from typing import Dict, Any, Deque, Optional
from collections import deque
import numpy as np
import logging

from ..base import BaseStrategy, Signal, BarData, PositionSide
from ..regime_detector import VolatilityRegime

logger = logging.getLogger(__name__)


@dataclass
class PureMicroConfig:
    """Pure Microstructure 전략 설정"""
    # 시그널 가중치
    ofi_weight: float = 0.4
    imbalance_weight: float = 0.3
    spread_weight: float = 0.2
    regime_weight: float = 0.1

    # OFI 조건
    ofi_zscore_threshold: float = 2.0    # ±2σ
    ofi_consecutive_bars: int = 3         # 연속 확인 바 수

    # 호가 불균형 조건
    imbalance_threshold: float = 0.5      # 50% 이상 불균형
    imbalance_trend_threshold: float = 0.1  # 추세 임계값

    # 스프레드 조건
    max_spread_ticks: int = 2             # 최대 2틱
    spread_percentile_threshold: float = 0.7  # 70% 이하

    # 진입 임계값
    entry_score_threshold: float = 0.6    # 복합 점수 60% 이상

    # 리스크 관리
    stop_loss_points: float = 1.5         # 손절폭
    take_profit_points: float = 3.0       # 익절폭
    trailing_stop_points: float = 1.0     # 트레일링 스탑
    max_bars_in_position: int = 30        # 시간 손절

    # 레짐별 동작
    low_regime_bias: str = "mean_reversion"   # LOW 레짐: 역추세
    high_regime_bias: str = "breakout"        # HIGH 레짐: 추세

    # 기타
    min_history: int = 20
    cooldown_bars: int = 5                # 진입 후 쿨다운


@dataclass
class SignalScore:
    """시그널 점수"""
    ofi_score: float = 0.0
    imbalance_score: float = 0.0
    spread_score: float = 0.0
    regime_score: float = 0.0
    total_score: float = 0.0
    direction: int = 0  # 1: BUY, -1: SELL, 0: HOLD

    def to_dict(self) -> Dict[str, Any]:
        return {
            'ofi': self.ofi_score,
            'imbalance': self.imbalance_score,
            'spread': self.spread_score,
            'regime': self.regime_score,
            'total': self.total_score,
            'direction': self.direction,
        }


class PureMicrostructureStrategy(BaseStrategy):
    """
    Pure Microstructure 전략

    LSTM 없이 마이크로스트럭처 시그널만으로 트레이딩

    진입 조건:
    1. OFI Z-Score > ±2σ (연속 3분)
    2. 호가 불균형 > ±0.5
    3. 스프레드 < 평균 (좁을 때만)
    4. 레짐 방향 일치

    복합 점수 = Σ(시그널 * 가중치) > 임계값
    """

    def __init__(self, config: PureMicroConfig = None):
        super().__init__(name="PureMicrostructure")
        self.config = config or PureMicroConfig()

        # OFI 히스토리 (연속 확인용)
        self.ofi_zscore_history: Deque[float] = deque(maxlen=10)

        # 진입 정보
        self.entry_price: float = 0.0
        self.entry_score: SignalScore = SignalScore()
        self.highest_since_entry: float = 0.0
        self.lowest_since_entry: float = float('inf')

        # 쿨다운
        self.bars_since_last_trade: int = 0

    def generate_signal(self, bar: BarData) -> Signal:
        """시그널 생성"""
        self.bars_since_last_trade += 1

        # 최소 히스토리 체크
        if len(self.history) < self.config.min_history:
            return Signal.HOLD

        # OFI Z-Score 히스토리 업데이트
        self._update_ofi_history(bar)

        # 포지션 있으면 청산 체크
        if self.state.position != PositionSide.FLAT:
            self._update_extremes(bar)
            return self._check_exit(bar)

        # 쿨다운 체크
        if self.bars_since_last_trade < self.config.cooldown_bars:
            return Signal.HOLD

        # 진입 체크
        return self._check_entry(bar)

    def _update_ofi_history(self, bar: BarData):
        """OFI Z-Score 히스토리 업데이트"""
        ofi_zscore = getattr(bar, 'ofi_zscore', 0.0)
        if ofi_zscore == 0 and bar.ofi != 0:
            # Z-Score가 없으면 OFI 값으로 대체 추정
            ofi_zscore = bar.ofi / 1000 if abs(bar.ofi) > 0 else 0
        self.ofi_zscore_history.append(ofi_zscore)

    def _update_extremes(self, bar: BarData):
        """포지션 진입 후 최고/최저가 업데이트"""
        self.highest_since_entry = max(self.highest_since_entry, bar.high)
        self.lowest_since_entry = min(self.lowest_since_entry, bar.low)

    def _check_entry(self, bar: BarData) -> Signal:
        """진입 조건 체크"""
        # 스프레드 필터 (너무 넓으면 진입 안함)
        spread_ticks = bar.spread / 0.02 if bar.spread > 0 else 1
        if spread_ticks > self.config.max_spread_ticks:
            logger.debug(f"Spread too wide: {spread_ticks:.1f} ticks")
            return Signal.HOLD

        # 각 시그널 점수 계산
        buy_score = self._calculate_signal_score(bar, direction=1)
        sell_score = self._calculate_signal_score(bar, direction=-1)

        # 매수 시그널
        if buy_score.total_score >= self.config.entry_score_threshold:
            self._enter_position(bar, buy_score)
            logger.info(
                f"[PureMicro] BUY signal: score={buy_score.total_score:.2f}, "
                f"ofi={buy_score.ofi_score:.2f}, imb={buy_score.imbalance_score:.2f}"
            )
            return Signal.BUY

        # 매도 시그널
        if sell_score.total_score >= self.config.entry_score_threshold:
            self._enter_position(bar, sell_score)
            logger.info(
                f"[PureMicro] SELL signal: score={sell_score.total_score:.2f}, "
                f"ofi={sell_score.ofi_score:.2f}, imb={sell_score.imbalance_score:.2f}"
            )
            return Signal.SELL

        return Signal.HOLD

    def _calculate_signal_score(self, bar: BarData, direction: int) -> SignalScore:
        """
        시그널 점수 계산

        Args:
            bar: 현재 바 데이터
            direction: 1 (BUY) or -1 (SELL)

        Returns:
            SignalScore 객체
        """
        score = SignalScore(direction=direction)

        # 1. OFI 점수 (0~1)
        score.ofi_score = self._calculate_ofi_score(direction)

        # 2. 호가 불균형 점수 (0~1)
        score.imbalance_score = self._calculate_imbalance_score(bar, direction)

        # 3. 스프레드 점수 (0~1)
        score.spread_score = self._calculate_spread_score(bar)

        # 4. 레짐 점수 (0~1)
        score.regime_score = self._calculate_regime_score(bar, direction)

        # 가중 합계
        score.total_score = (
            score.ofi_score * self.config.ofi_weight +
            score.imbalance_score * self.config.imbalance_weight +
            score.spread_score * self.config.spread_weight +
            score.regime_score * self.config.regime_weight
        )

        return score

    def _calculate_ofi_score(self, direction: int) -> float:
        """
        OFI 점수 계산

        연속 N분 동안 같은 방향 Z-Score > 임계값이면 1.0
        """
        if len(self.ofi_zscore_history) < self.config.ofi_consecutive_bars:
            return 0.0

        recent = list(self.ofi_zscore_history)[-self.config.ofi_consecutive_bars:]
        threshold = self.config.ofi_zscore_threshold

        if direction == 1:  # BUY
            # 연속 양수 Z-Score
            consecutive = all(z > threshold for z in recent)
            if consecutive:
                return 1.0
            # 부분 충족
            positive_count = sum(1 for z in recent if z > 0)
            return positive_count / len(recent) * 0.7
        else:  # SELL
            # 연속 음수 Z-Score
            consecutive = all(z < -threshold for z in recent)
            if consecutive:
                return 1.0
            negative_count = sum(1 for z in recent if z < 0)
            return negative_count / len(recent) * 0.7

    def _calculate_imbalance_score(self, bar: BarData, direction: int) -> float:
        """호가 불균형 점수 계산"""
        imbalance = bar.bid_ask_imbalance
        threshold = self.config.imbalance_threshold

        if direction == 1:  # BUY (매수 우위)
            if imbalance >= threshold:
                return 1.0
            elif imbalance > 0:
                return imbalance / threshold * 0.8
            else:
                return 0.0
        else:  # SELL (매도 우위)
            if imbalance <= -threshold:
                return 1.0
            elif imbalance < 0:
                return abs(imbalance) / threshold * 0.8
            else:
                return 0.0

    def _calculate_spread_score(self, bar: BarData) -> float:
        """
        스프레드 점수 계산

        스프레드가 좁을수록 높은 점수
        """
        spread = bar.spread if bar.spread > 0 else 0.02
        avg_spread = 0.04  # 기본 평균 스프레드 (2틱)

        # 스프레드가 평균보다 좁으면 높은 점수
        if spread <= 0.02:  # 1틱
            return 1.0
        elif spread <= avg_spread:
            return 0.8
        elif spread <= avg_spread * 1.5:
            return 0.5
        else:
            return 0.2

    def _calculate_regime_score(self, bar: BarData, direction: int) -> float:
        """
        레짐 점수 계산

        레짐에 맞는 방향이면 높은 점수
        """
        regime_str = bar.regime if bar.regime else "MEDIUM"
        try:
            regime = VolatilityRegime(regime_str)
        except ValueError:
            regime = VolatilityRegime.MEDIUM

        # LOW 레짐: 역추세 선호 (극단에서 반대 방향)
        if regime == VolatilityRegime.LOW:
            # 평균회귀: 가격이 낮으면 BUY, 높으면 SELL 선호
            if self.config.low_regime_bias == "mean_reversion":
                return 0.8  # 레짐 적합
            return 0.5

        # HIGH 레짐: 추세 추종 선호
        elif regime == VolatilityRegime.HIGH:
            if self.config.high_regime_bias == "breakout":
                return 0.9  # 레짐 적합
            return 0.6

        # MEDIUM 레짐: 중립
        else:
            return 0.5

    def _enter_position(self, bar: BarData, score: SignalScore):
        """포지션 진입 처리"""
        self.entry_price = bar.close
        self.entry_score = score
        self.highest_since_entry = bar.high
        self.lowest_since_entry = bar.low
        self.bars_since_last_trade = 0

    def _check_exit(self, bar: BarData) -> Signal:
        """청산 조건 체크"""
        current_price = bar.close

        # 시간 손절
        if self.state.bars_in_position >= self.config.max_bars_in_position:
            logger.info(f"[PureMicro] Time stop: bars={self.state.bars_in_position}")
            return self._get_exit_signal()

        if self.state.position == PositionSide.LONG:
            return self._check_long_exit(bar, current_price)
        elif self.state.position == PositionSide.SHORT:
            return self._check_short_exit(bar, current_price)

        return Signal.HOLD

    def _check_long_exit(self, bar: BarData, current_price: float) -> Signal:
        """롱 포지션 청산 조건"""
        # 손절
        if current_price <= self.entry_price - self.config.stop_loss_points:
            logger.info(
                f"[PureMicro] Stop loss (LONG): entry={self.entry_price:.2f}, "
                f"current={current_price:.2f}"
            )
            return Signal.SELL

        # 익절
        if current_price >= self.entry_price + self.config.take_profit_points:
            logger.info(
                f"[PureMicro] Take profit (LONG): entry={self.entry_price:.2f}, "
                f"current={current_price:.2f}"
            )
            return Signal.SELL

        # 트레일링 스탑 (최고가 대비 하락)
        if self.config.trailing_stop_points > 0:
            trailing_stop = self.highest_since_entry - self.config.trailing_stop_points
            if current_price <= trailing_stop and current_price > self.entry_price:
                logger.info(
                    f"[PureMicro] Trailing stop (LONG): highest={self.highest_since_entry:.2f}, "
                    f"current={current_price:.2f}"
                )
                return Signal.SELL

        # 반전 시그널 체크 (선택적)
        sell_score = self._calculate_signal_score(bar, direction=-1)
        if sell_score.total_score >= self.config.entry_score_threshold * 1.2:
            logger.info(f"[PureMicro] Reverse signal (LONG→SHORT): score={sell_score.total_score:.2f}")
            return Signal.SELL

        return Signal.HOLD

    def _check_short_exit(self, bar: BarData, current_price: float) -> Signal:
        """숏 포지션 청산 조건"""
        # 손절
        if current_price >= self.entry_price + self.config.stop_loss_points:
            logger.info(
                f"[PureMicro] Stop loss (SHORT): entry={self.entry_price:.2f}, "
                f"current={current_price:.2f}"
            )
            return Signal.BUY

        # 익절
        if current_price <= self.entry_price - self.config.take_profit_points:
            logger.info(
                f"[PureMicro] Take profit (SHORT): entry={self.entry_price:.2f}, "
                f"current={current_price:.2f}"
            )
            return Signal.BUY

        # 트레일링 스탑 (최저가 대비 상승)
        if self.config.trailing_stop_points > 0:
            trailing_stop = self.lowest_since_entry + self.config.trailing_stop_points
            if current_price >= trailing_stop and current_price < self.entry_price:
                logger.info(
                    f"[PureMicro] Trailing stop (SHORT): lowest={self.lowest_since_entry:.2f}, "
                    f"current={current_price:.2f}"
                )
                return Signal.BUY

        # 반전 시그널 체크
        buy_score = self._calculate_signal_score(bar, direction=1)
        if buy_score.total_score >= self.config.entry_score_threshold * 1.2:
            logger.info(f"[PureMicro] Reverse signal (SHORT→LONG): score={buy_score.total_score:.2f}")
            return Signal.BUY

        return Signal.HOLD

    def _get_exit_signal(self) -> Signal:
        """현재 포지션에 맞는 청산 시그널"""
        if self.state.position == PositionSide.LONG:
            return Signal.SELL
        elif self.state.position == PositionSide.SHORT:
            return Signal.BUY
        return Signal.HOLD

    def get_current_score(self, bar: BarData) -> Dict[str, SignalScore]:
        """현재 시그널 점수 조회 (디버깅용)"""
        return {
            'buy': self._calculate_signal_score(bar, direction=1),
            'sell': self._calculate_signal_score(bar, direction=-1),
        }

    def reset(self):
        """상태 초기화"""
        super().reset()
        self.ofi_zscore_history.clear()
        self.entry_price = 0.0
        self.entry_score = SignalScore()
        self.highest_since_entry = 0.0
        self.lowest_since_entry = float('inf')
        self.bars_since_last_trade = 0


class AdaptiveMicrostructureStrategy(PureMicrostructureStrategy):
    """
    적응형 마이크로스트럭처 전략

    레짐에 따라 파라미터 자동 조정:
    - LOW: 타이트한 손익비, 빠른 청산
    - HIGH: 넓은 손익비, 추세 추종
    """

    def __init__(self, config: PureMicroConfig = None):
        super().__init__(config)
        self.name = "AdaptiveMicrostructure"

        # 레짐별 설정
        self.regime_configs = {
            VolatilityRegime.LOW: {
                'entry_score_threshold': 0.55,  # 낮춤 (더 자주 진입)
                'stop_loss_points': 1.0,
                'take_profit_points': 1.5,
                'trailing_stop_points': 0.5,
            },
            VolatilityRegime.MEDIUM: {
                'entry_score_threshold': 0.6,
                'stop_loss_points': 1.5,
                'take_profit_points': 3.0,
                'trailing_stop_points': 1.0,
            },
            VolatilityRegime.HIGH: {
                'entry_score_threshold': 0.65,  # 높임 (신중하게)
                'stop_loss_points': 2.0,
                'take_profit_points': 5.0,  # 넓은 목표
                'trailing_stop_points': 1.5,
            },
        }

    def _apply_regime_config(self, regime: VolatilityRegime):
        """레짐별 설정 적용"""
        if regime in self.regime_configs:
            cfg = self.regime_configs[regime]
            self.config.entry_score_threshold = cfg['entry_score_threshold']
            self.config.stop_loss_points = cfg['stop_loss_points']
            self.config.take_profit_points = cfg['take_profit_points']
            self.config.trailing_stop_points = cfg['trailing_stop_points']

    def generate_signal(self, bar: BarData) -> Signal:
        """시그널 생성 (레짐 적응)"""
        regime_str = bar.regime if bar.regime else "MEDIUM"
        try:
            regime = VolatilityRegime(regime_str)
        except ValueError:
            regime = VolatilityRegime.MEDIUM

        self._apply_regime_config(regime)
        return super().generate_signal(bar)
