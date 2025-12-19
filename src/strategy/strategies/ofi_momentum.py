"""
OFI Momentum Strategy

OFI (Order Flow Imbalance) 기반 단기 모멘텀 전략
연속적인 매수/매도 압력 감지 시 진입
"""
from dataclasses import dataclass
from typing import List, Deque
from collections import deque
import numpy as np
import logging

from ..base import BaseStrategy, Signal, BarData, PositionSide

logger = logging.getLogger(__name__)


@dataclass
class OFIMomentumConfig:
    """OFI Momentum 전략 설정"""
    # OFI 시그널 설정
    ofi_zscore_threshold: float = 2.0    # Z-Score 임계값
    consecutive_bars: int = 3            # 연속 바 수

    # 스프레드 필터
    require_tight_spread: bool = True    # 스프레드 좁을 때만 진입

    # 진입 조건
    min_history: int = 20                # 최소 히스토리 바 수

    # 시간 손절 (짧게)
    max_bars_in_position: int = 10       # 최대 보유 바 수

    # 손익 설정 (포인트)
    stop_loss_points: float = 1.0        # 손절폭
    take_profit_points: float = 2.0      # 익절폭

    # 호가 불균형 확인
    imbalance_confirm: bool = True
    imbalance_threshold: float = 0.2     # 불균형 임계값


class OFIMomentumStrategy(BaseStrategy):
    """
    OFI Momentum 전략

    OFI가 ±2σ를 연속 3분 이상 유지하면:
    - OFI > +2σ (연속 3분) + 스프레드 좁음 → 매수
    - OFI < -2σ (연속 3분) + 스프레드 좁음 → 매도

    청산:
    - 손절: 1포인트
    - 익절: 2포인트
    - 시간 손절: 10분
    """

    def __init__(self, config: OFIMomentumConfig = None):
        super().__init__(name="OFIMomentum")
        self.config = config or OFIMomentumConfig()

        # OFI 히스토리
        self.ofi_values: Deque[float] = deque(maxlen=60)
        self.ofi_zscore_history: Deque[float] = deque(maxlen=10)

        # 진입 정보
        self.entry_price: float = 0.0

    def generate_signal(self, bar: BarData) -> Signal:
        """시그널 생성"""
        # OFI 업데이트
        self.ofi_values.append(bar.ofi)

        # 최소 히스토리 체크
        if len(self.history) < self.config.min_history:
            return Signal.HOLD

        # OFI Z-Score 계산 및 저장
        ofi_zscore = self._calculate_ofi_zscore(bar.ofi)
        self.ofi_zscore_history.append(ofi_zscore)

        # 포지션 있으면 청산 체크
        if self.state.position != PositionSide.FLAT:
            return self._check_exit(bar)

        # 포지션 없으면 진입 체크
        return self._check_entry(bar, ofi_zscore)

    def _calculate_ofi_zscore(self, current_ofi: float) -> float:
        """OFI Z-Score 계산"""
        if len(self.ofi_values) < 20:
            return 0.0

        values = list(self.ofi_values)
        mean = np.mean(values)
        std = np.std(values, ddof=1)

        if std == 0:
            return 0.0

        return (current_ofi - mean) / std

    def _check_consecutive_ofi(self, direction: str) -> bool:
        """
        연속 OFI 체크

        Args:
            direction: "positive" or "negative"

        Returns:
            연속 조건 만족 여부
        """
        if len(self.ofi_zscore_history) < self.config.consecutive_bars:
            return False

        recent = list(self.ofi_zscore_history)[-self.config.consecutive_bars:]

        if direction == "positive":
            return all(z > self.config.ofi_zscore_threshold for z in recent)
        else:
            return all(z < -self.config.ofi_zscore_threshold for z in recent)

    def _check_entry(self, bar: BarData, ofi_zscore: float) -> Signal:
        """진입 조건 체크"""
        # 스프레드 필터
        if self.config.require_tight_spread:
            # spread가 0보다 크면 정상, 작으면 데이터 없음
            if bar.spread > 0.04:  # 2틱 이상이면 진입 안함
                return Signal.HOLD

        # 호가 불균형 확인
        imbalance = bar.bid_ask_imbalance

        # 연속 매수 압력 → 매수
        if self._check_consecutive_ofi("positive"):
            # 호가 불균형 확인
            if self.config.imbalance_confirm:
                if imbalance < self.config.imbalance_threshold:
                    logger.debug(
                        f"OFI positive but imbalance weak ({imbalance:.2f})"
                    )
                    return Signal.HOLD

            self.entry_price = bar.close
            logger.info(
                f"[OFIMomentum] BUY signal: ofi_z={ofi_zscore:.2f}, "
                f"imbalance={imbalance:.2f}"
            )
            return Signal.BUY

        # 연속 매도 압력 → 매도
        if self._check_consecutive_ofi("negative"):
            # 호가 불균형 확인
            if self.config.imbalance_confirm:
                if imbalance > -self.config.imbalance_threshold:
                    logger.debug(
                        f"OFI negative but imbalance weak ({imbalance:.2f})"
                    )
                    return Signal.HOLD

            self.entry_price = bar.close
            logger.info(
                f"[OFIMomentum] SELL signal: ofi_z={ofi_zscore:.2f}, "
                f"imbalance={imbalance:.2f}"
            )
            return Signal.SELL

        return Signal.HOLD

    def _check_exit(self, bar: BarData) -> Signal:
        """청산 조건 체크"""
        current_price = bar.close

        # 시간 손절
        if self.state.bars_in_position >= self.config.max_bars_in_position:
            logger.info(
                f"[OFIMomentum] Time stop: bars={self.state.bars_in_position}"
            )
            return self._get_exit_signal()

        if self.state.position == PositionSide.LONG:
            # 손절
            if current_price <= self.entry_price - self.config.stop_loss_points:
                logger.info(
                    f"[OFIMomentum] Stop loss: entry={self.entry_price:.2f}, "
                    f"current={current_price:.2f}"
                )
                return Signal.SELL

            # 익절
            if current_price >= self.entry_price + self.config.take_profit_points:
                logger.info(
                    f"[OFIMomentum] Take profit: entry={self.entry_price:.2f}, "
                    f"current={current_price:.2f}"
                )
                return Signal.SELL

        elif self.state.position == PositionSide.SHORT:
            # 손절
            if current_price >= self.entry_price + self.config.stop_loss_points:
                logger.info(
                    f"[OFIMomentum] Stop loss: entry={self.entry_price:.2f}, "
                    f"current={current_price:.2f}"
                )
                return Signal.BUY

            # 익절
            if current_price <= self.entry_price - self.config.take_profit_points:
                logger.info(
                    f"[OFIMomentum] Take profit: entry={self.entry_price:.2f}, "
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
        self.ofi_values.clear()
        self.ofi_zscore_history.clear()
        self.entry_price = 0.0
