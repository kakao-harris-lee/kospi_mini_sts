"""
Breakout Strategy (돌파 전략)

고변동성 레짐에서 사용
N분 고점/저점 돌파 시 방향 진입
"""
from dataclasses import dataclass
from typing import Dict, Any, Optional
import numpy as np
import logging

from ..base import BaseStrategy, Signal, BarData, PositionSide
from ..regime_detector import VolatilityRegime

logger = logging.getLogger(__name__)


@dataclass
class BreakoutConfig:
    """Breakout 전략 설정"""
    # 돌파 기준
    lookback_period: int = 20     # N분 고점/저점 기준
    breakout_buffer: float = 0.02  # 돌파 확인 버퍼 (포인트)

    # 진입 조건
    min_history: int = 25         # 최소 히스토리 바 수
    volume_confirm: bool = True   # 거래량 확인 필요 여부
    volume_threshold: float = 1.5  # 평균 대비 거래량 배수

    # 체결 강도 확인
    trade_flow_confirm: bool = True
    buy_ratio_threshold: float = 0.55  # 매수 체결 비율 임계값

    # ATR 기반 손익
    atr_stop_multiplier: float = 1.5   # 손절 = ATR × 1.5
    atr_target_multiplier: float = 3.0  # 익절 = ATR × 3.0

    # 시간 손절
    max_bars_in_position: int = 60     # 최대 보유 바 수

    # 레짐 필터
    allowed_regimes: tuple = (VolatilityRegime.HIGH,)


class BreakoutStrategy(BaseStrategy):
    """
    Breakout (돌파) 전략

    고변동성 추세장에서:
    - N분 고점 돌파 + 체결강도 > 0.55 → 매수
    - N분 저점 이탈 + 체결강도 < 0.45 → 매도

    청산:
    - ATR × 1.5 손절
    - ATR × 3.0 익절
    - 시간 손절 (60분)
    """

    def __init__(self, config: BreakoutConfig = None):
        super().__init__(name="Breakout")
        self.config = config or BreakoutConfig()
        self.max_history = max(50, self.config.lookback_period + 10)

        # 진입 정보
        self.entry_atr: float = 0.0
        self.stop_price: float = 0.0
        self.target_price: float = 0.0

    def generate_signal(self, bar: BarData) -> Signal:
        """시그널 생성"""
        # 최소 히스토리 체크
        if len(self.history) < self.config.min_history:
            return Signal.HOLD

        # 레짐 체크 (None = warm-up period, skip trading)
        if bar.regime is None:
            logger.debug("Regime not available (warm-up period), skipping")
            return Signal.HOLD
        try:
            regime = VolatilityRegime(bar.regime)
        except ValueError:
            logger.warning(f"Invalid regime value: {bar.regime}, using MEDIUM")
            regime = VolatilityRegime.MEDIUM
        if regime not in self.config.allowed_regimes:
            # 레짐이 맞지 않으면 포지션 있을 때 청산
            if self.state.position != PositionSide.FLAT:
                logger.debug(f"Regime mismatch ({regime.value}), closing position")
                return self._get_exit_signal()
            return Signal.HOLD

        # N분 고점/저점 계산
        highest = self.highest(self.config.lookback_period)
        lowest = self.lowest(self.config.lookback_period)

        if highest == 0 or lowest == float('inf'):
            return Signal.HOLD

        current_price = bar.close
        atr = bar.atr if bar.atr > 0 else self._calculate_atr()

        # 포지션 있으면 청산 체크
        if self.state.position != PositionSide.FLAT:
            return self._check_exit(bar, current_price)

        # 포지션 없으면 진입 체크
        return self._check_entry(bar, highest, lowest, atr)

    def _check_entry(
        self,
        bar: BarData,
        highest: float,
        lowest: float,
        atr: float
    ) -> Signal:
        """진입 조건 체크"""
        current_price = bar.close
        buy_ratio = bar.buy_volume_ratio

        # 거래량 확인
        if self.config.volume_confirm:
            avg_volume = np.mean(self.get_recent_volumes(20))
            if bar.volume < avg_volume * self.config.volume_threshold:
                return Signal.HOLD

        # 고점 돌파 → 매수
        if current_price > highest + self.config.breakout_buffer:
            # 체결 강도 확인
            if self.config.trade_flow_confirm:
                if buy_ratio < self.config.buy_ratio_threshold:
                    logger.debug(
                        f"High breakout but weak buying ({buy_ratio:.2f})"
                    )
                    return Signal.HOLD

            # 손익가 설정
            self.entry_atr = atr
            self.stop_price = current_price - atr * self.config.atr_stop_multiplier
            self.target_price = current_price + atr * self.config.atr_target_multiplier

            logger.info(
                f"[Breakout] BUY signal: price={current_price:.2f}, "
                f"highest={highest:.2f}, atr={atr:.2f}, "
                f"stop={self.stop_price:.2f}, target={self.target_price:.2f}"
            )
            return Signal.BUY

        # 저점 이탈 → 매도
        if current_price < lowest - self.config.breakout_buffer:
            # 체결 강도 확인 (매도 우위)
            if self.config.trade_flow_confirm:
                if buy_ratio > (1 - self.config.buy_ratio_threshold):
                    logger.debug(
                        f"Low breakout but weak selling ({buy_ratio:.2f})"
                    )
                    return Signal.HOLD

            # 손익가 설정
            self.entry_atr = atr
            self.stop_price = current_price + atr * self.config.atr_stop_multiplier
            self.target_price = current_price - atr * self.config.atr_target_multiplier

            logger.info(
                f"[Breakout] SELL signal: price={current_price:.2f}, "
                f"lowest={lowest:.2f}, atr={atr:.2f}, "
                f"stop={self.stop_price:.2f}, target={self.target_price:.2f}"
            )
            return Signal.SELL

        return Signal.HOLD

    def _check_exit(self, bar: BarData, current_price: float) -> Signal:
        """청산 조건 체크"""
        # 시간 손절
        if self.state.bars_in_position >= self.config.max_bars_in_position:
            logger.info(
                f"[Breakout] Time stop: bars={self.state.bars_in_position}"
            )
            return self._get_exit_signal()

        if self.state.position == PositionSide.LONG:
            # 손절
            if current_price <= self.stop_price:
                logger.info(
                    f"[Breakout] Stop loss: price={current_price:.2f}, "
                    f"stop={self.stop_price:.2f}"
                )
                return Signal.SELL

            # 익절
            if current_price >= self.target_price:
                logger.info(
                    f"[Breakout] Take profit: price={current_price:.2f}, "
                    f"target={self.target_price:.2f}"
                )
                return Signal.SELL

        elif self.state.position == PositionSide.SHORT:
            # 손절
            if current_price >= self.stop_price:
                logger.info(
                    f"[Breakout] Stop loss: price={current_price:.2f}, "
                    f"stop={self.stop_price:.2f}"
                )
                return Signal.BUY

            # 익절
            if current_price <= self.target_price:
                logger.info(
                    f"[Breakout] Take profit: price={current_price:.2f}, "
                    f"target={self.target_price:.2f}"
                )
                return Signal.BUY

        return Signal.HOLD

    def _calculate_atr(self, period: int = 14) -> float:
        """ATR 계산"""
        if len(self.history) < period + 1:
            return 0.1  # 기본값

        trs = []
        for i in range(-period, 0):
            h = self.history[i].high
            l = self.history[i].low
            cp = self.history[i - 1].close

            tr = max(h - l, abs(h - cp), abs(l - cp))
            trs.append(tr)

        return np.mean(trs)

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
        self.entry_atr = 0.0
        self.stop_price = 0.0
        self.target_price = 0.0
