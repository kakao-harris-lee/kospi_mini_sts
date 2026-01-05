"""
Hybrid Strategy (LSTM + 로지컬 혼합 전략)

LSTM 예측 확률 + 마이크로스트럭처 시그널을 복합적으로 활용
다중 조건 필터로 신뢰도 높은 시그널만 사용
"""
from dataclasses import dataclass
from typing import Dict, Any, List, Optional
import numpy as np
import logging

from ..base import BaseStrategy, Signal, BarData, PositionSide
from ..regime_detector import VolatilityRegime

logger = logging.getLogger(__name__)


@dataclass
class HybridConfig:
    """Hybrid 전략 설정"""
    # LSTM 예측 임계값
    lstm_buy_threshold: float = 0.7     # 매수 확률 임계값
    lstm_sell_threshold: float = 0.3    # 매도 확률 임계값 (= 1 - 0.7)

    # OFI 조건
    ofi_confirm: bool = True
    ofi_threshold: float = 0.0          # 방향 일치만 확인

    # 호가 불균형 조건
    imbalance_confirm: bool = True
    imbalance_threshold: float = 0.2    # 20% 이상 불균형

    # 스프레드 조건
    spread_filter: bool = True
    max_spread_ticks: int = 2           # 최대 2틱

    # 최소 조건 충족 수
    min_conditions_met: int = 2         # 3개 조건 중 2개 이상

    # 진입 조건
    min_history: int = 20

    # 리스크 설정
    stop_loss_points: float = 2.0       # 손절폭
    take_profit_points: float = 4.0     # 익절폭
    max_bars_in_position: int = 30      # 시간 손절

    # 레짐 필터 (관망 레짐 제외)
    avoid_medium_regime: bool = False   # MEDIUM 레짐에서도 거래


class HybridStrategy(BaseStrategy):
    """
    Hybrid (LSTM + 로지컬) 전략

    매수 조건 (3개 중 2개 이상):
    1. LSTM up_prob > 0.7
    2. OFI > 0 (매수 압력)
    3. 호가 불균형 > 0.2 (매수 우위)

    매도 조건 (3개 중 2개 이상):
    1. LSTM up_prob < 0.3 (= down_prob > 0.7)
    2. OFI < 0 (매도 압력)
    3. 호가 불균형 < -0.2 (매도 우위)

    추가 필터:
    - 스프레드 2틱 이내
    - 레짐 체크 (선택)
    """

    def __init__(self, config: HybridConfig = None):
        super().__init__(name="Hybrid")
        self.config = config or HybridConfig()

        # 진입 정보
        self.entry_price: float = 0.0
        self.entry_conditions: Dict[str, bool] = {}

    def generate_signal(self, bar: BarData) -> Signal:
        """시그널 생성"""
        # 최소 히스토리 체크
        if len(self.history) < self.config.min_history:
            return Signal.HOLD

        # 레짐 체크 (선택)
        if self.config.avoid_medium_regime:
            if bar.regime is None:
                logger.debug("Regime not available (warm-up period), skipping")
                return Signal.HOLD
            try:
                regime = VolatilityRegime(bar.regime)
            except ValueError:
                logger.warning(f"Invalid regime value: {bar.regime}, using MEDIUM")
                regime = VolatilityRegime.MEDIUM
            if regime == VolatilityRegime.MEDIUM:
                if self.state.position != PositionSide.FLAT:
                    return Signal.HOLD  # 관망 (포지션 유지)
                return Signal.HOLD

        # 포지션 있으면 청산 체크
        if self.state.position != PositionSide.FLAT:
            return self._check_exit(bar)

        # 포지션 없으면 진입 체크
        return self._check_entry(bar)

    def _check_entry(self, bar: BarData) -> Signal:
        """진입 조건 체크"""
        # 스프레드 필터
        if self.config.spread_filter:
            spread_ticks = bar.spread / 0.02 if bar.spread > 0 else 1
            if spread_ticks > self.config.max_spread_ticks:
                logger.debug(f"Spread too wide: {spread_ticks:.1f} ticks")
                return Signal.HOLD

        # 매수 조건 체크
        buy_conditions = self._check_buy_conditions(bar)
        buy_count = sum(1 for v in buy_conditions.values() if v)

        if buy_count >= self.config.min_conditions_met:
            self.entry_price = bar.close
            self.entry_conditions = buy_conditions
            logger.info(
                f"[Hybrid] BUY signal: conditions={buy_conditions}, "
                f"count={buy_count}/{len(buy_conditions)}"
            )
            return Signal.BUY

        # 매도 조건 체크
        sell_conditions = self._check_sell_conditions(bar)
        sell_count = sum(1 for v in sell_conditions.values() if v)

        if sell_count >= self.config.min_conditions_met:
            self.entry_price = bar.close
            self.entry_conditions = sell_conditions
            logger.info(
                f"[Hybrid] SELL signal: conditions={sell_conditions}, "
                f"count={sell_count}/{len(sell_conditions)}"
            )
            return Signal.SELL

        return Signal.HOLD

    def _check_buy_conditions(self, bar: BarData) -> Dict[str, bool]:
        """매수 조건 체크"""
        conditions = {}

        # 1. LSTM 예측
        conditions['lstm'] = bar.up_prob > self.config.lstm_buy_threshold

        # 2. OFI
        if self.config.ofi_confirm:
            conditions['ofi'] = bar.ofi > self.config.ofi_threshold
        else:
            conditions['ofi'] = True

        # 3. 호가 불균형
        if self.config.imbalance_confirm:
            conditions['imbalance'] = bar.bid_ask_imbalance > self.config.imbalance_threshold
        else:
            conditions['imbalance'] = True

        return conditions

    def _check_sell_conditions(self, bar: BarData) -> Dict[str, bool]:
        """매도 조건 체크"""
        conditions = {}

        # 1. LSTM 예측 (up_prob < threshold = down_prob > 1-threshold)
        conditions['lstm'] = bar.up_prob < self.config.lstm_sell_threshold

        # 2. OFI
        if self.config.ofi_confirm:
            conditions['ofi'] = bar.ofi < -self.config.ofi_threshold
        else:
            conditions['ofi'] = True

        # 3. 호가 불균형
        if self.config.imbalance_confirm:
            conditions['imbalance'] = bar.bid_ask_imbalance < -self.config.imbalance_threshold
        else:
            conditions['imbalance'] = True

        return conditions

    def _check_exit(self, bar: BarData) -> Signal:
        """청산 조건 체크"""
        current_price = bar.close

        # 시간 손절
        if self.state.bars_in_position >= self.config.max_bars_in_position:
            logger.info(
                f"[Hybrid] Time stop: bars={self.state.bars_in_position}"
            )
            return self._get_exit_signal()

        if self.state.position == PositionSide.LONG:
            # 손절
            if current_price <= self.entry_price - self.config.stop_loss_points:
                logger.info(
                    f"[Hybrid] Stop loss: entry={self.entry_price:.2f}, "
                    f"current={current_price:.2f}"
                )
                return Signal.SELL

            # 익절
            if current_price >= self.entry_price + self.config.take_profit_points:
                logger.info(
                    f"[Hybrid] Take profit: entry={self.entry_price:.2f}, "
                    f"current={current_price:.2f}"
                )
                return Signal.SELL

            # 조건 악화 시 청산 (선택적)
            # sell_conditions = self._check_sell_conditions(bar)
            # if sum(sell_conditions.values()) >= 2:
            #     return Signal.SELL

        elif self.state.position == PositionSide.SHORT:
            # 손절
            if current_price >= self.entry_price + self.config.stop_loss_points:
                logger.info(
                    f"[Hybrid] Stop loss: entry={self.entry_price:.2f}, "
                    f"current={current_price:.2f}"
                )
                return Signal.BUY

            # 익절
            if current_price <= self.entry_price - self.config.take_profit_points:
                logger.info(
                    f"[Hybrid] Take profit: entry={self.entry_price:.2f}, "
                    f"current={current_price:.2f}"
                )
                return Signal.BUY

        return Signal.HOLD

    def _get_exit_signal(self) -> Signal:
        """현재 포지션에 맞는 청산 시그널"""
        if self.state.position == PositionSide.LONG:
            return Signal.SELL
        elif self.state.position == PositionSide.SHORT:
            return Signal.BUY
        return Signal.HOLD

    def reset(self):
        """상태 초기화"""
        super().reset()
        self.entry_price = 0.0
        self.entry_conditions = {}


class AdaptiveHybridStrategy(HybridStrategy):
    """
    적응형 Hybrid 전략

    레짐에 따라 다른 파라미터 사용:
    - LOW: LSTM 비중 낮춤, 마이크로스트럭처 비중 높임
    - HIGH: LSTM 비중 높임, 손익비 확대
    """

    def __init__(self, config: HybridConfig = None):
        super().__init__(config)
        self.name = "AdaptiveHybrid"

        # 레짐별 설정
        self.regime_configs = {
            VolatilityRegime.LOW: {
                'lstm_buy_threshold': 0.6,  # 낮춤
                'lstm_sell_threshold': 0.4,
                'min_conditions_met': 2,
                'stop_loss_points': 1.5,
                'take_profit_points': 2.0,
            },
            VolatilityRegime.MEDIUM: {
                'lstm_buy_threshold': 0.7,
                'lstm_sell_threshold': 0.3,
                'min_conditions_met': 2,
                'stop_loss_points': 2.0,
                'take_profit_points': 3.0,
            },
            VolatilityRegime.HIGH: {
                'lstm_buy_threshold': 0.75,  # 높임
                'lstm_sell_threshold': 0.25,
                'min_conditions_met': 2,
                'stop_loss_points': 2.5,
                'take_profit_points': 5.0,  # 손익비 확대
            },
        }

    def _apply_regime_config(self, regime: VolatilityRegime):
        """레짐별 설정 적용"""
        if regime in self.regime_configs:
            regime_cfg = self.regime_configs[regime]
            self.config.lstm_buy_threshold = regime_cfg['lstm_buy_threshold']
            self.config.lstm_sell_threshold = regime_cfg['lstm_sell_threshold']
            self.config.min_conditions_met = regime_cfg['min_conditions_met']
            self.config.stop_loss_points = regime_cfg['stop_loss_points']
            self.config.take_profit_points = regime_cfg['take_profit_points']

    def generate_signal(self, bar: BarData) -> Signal:
        """시그널 생성 (레짐 적응)"""
        # 레짐 파악 및 설정 적용 (None = warm-up period)
        if bar.regime is None:
            regime = VolatilityRegime.MEDIUM
        else:
            try:
                regime = VolatilityRegime(bar.regime)
            except ValueError:
                logger.warning(f"Invalid regime value: {bar.regime}, using MEDIUM")
                regime = VolatilityRegime.MEDIUM
        self._apply_regime_config(regime)

        return super().generate_signal(bar)
