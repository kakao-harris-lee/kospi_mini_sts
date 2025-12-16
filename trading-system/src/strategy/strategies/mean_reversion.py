"""
Mean Reversion Strategy (역추세 전략)

저변동성 레짐에서 사용
볼린저 밴드 터치 시 반대 방향 진입
"""
from dataclasses import dataclass
from typing import Dict, Any, Optional
import numpy as np
import logging

from ..base import BaseStrategy, Signal, BarData, PositionSide
from ..regime_detector import VolatilityRegime

logger = logging.getLogger(__name__)


@dataclass
class MeanReversionConfig:
    """Mean Reversion 전략 설정"""
    # 볼린저 밴드 설정
    bb_period: int = 20           # 볼린저밴드 기간
    bb_std: float = 2.0           # 볼린저밴드 표준편차 배수

    # 진입 조건
    min_history: int = 25         # 최소 히스토리 바 수
    ofi_confirm: bool = True      # OFI 확인 필요 여부
    ofi_threshold: float = 0.0    # OFI 임계값 (0이면 방향만 확인)

    # 청산 조건
    exit_at_middle: bool = True   # 중심선 도달 시 청산
    exit_at_opposite: bool = False  # 반대 밴드 도달 시 청산

    # 리스크
    max_bars_in_position: int = 30  # 최대 보유 바 수 (시간 손절)

    # 레짐 필터
    allowed_regimes: tuple = (VolatilityRegime.LOW,)


class MeanReversionStrategy(BaseStrategy):
    """
    Mean Reversion (역추세) 전략

    저변동성 박스권에서:
    - 볼린저밴드 하단 터치 + OFI > 0 → 매수
    - 볼린저밴드 상단 터치 + OFI < 0 → 매도

    청산:
    - 중심선(SMA) 도달 시
    - 시간 손절 (30분)
    """

    def __init__(self, config: MeanReversionConfig = None):
        super().__init__(name="MeanReversion")
        self.config = config or MeanReversionConfig()
        self.max_history = max(50, self.config.bb_period + 10)

        # 진입 정보
        self.entry_band: Optional[str] = None  # "lower" or "upper"

    def generate_signal(self, bar: BarData) -> Signal:
        """시그널 생성"""
        # 최소 히스토리 체크
        if len(self.history) < self.config.min_history:
            return Signal.HOLD

        # 레짐 체크
        regime = VolatilityRegime(bar.regime) if bar.regime else VolatilityRegime.MEDIUM
        if regime not in self.config.allowed_regimes:
            # 레짐이 맞지 않으면 포지션 있을 때 청산
            if self.state.position != PositionSide.FLAT:
                logger.debug(f"Regime mismatch ({regime.value}), closing position")
                return self._get_exit_signal()
            return Signal.HOLD

        # 볼린저 밴드 계산
        upper, middle, lower = self.bollinger_bands(
            self.config.bb_period,
            self.config.bb_std
        )

        if upper == 0 or lower == 0:
            return Signal.HOLD

        current_price = bar.close

        # 포지션 있으면 청산 체크
        if self.state.position != PositionSide.FLAT:
            return self._check_exit(bar, upper, middle, lower)

        # 포지션 없으면 진입 체크
        return self._check_entry(bar, upper, middle, lower)

    def _check_entry(
        self,
        bar: BarData,
        upper: float,
        middle: float,
        lower: float
    ) -> Signal:
        """진입 조건 체크"""
        current_price = bar.close
        ofi = bar.ofi

        # 하단 터치 → 매수
        if current_price <= lower:
            # OFI 확인 (매수 압력 있는지)
            if self.config.ofi_confirm:
                if ofi <= self.config.ofi_threshold:
                    logger.debug(f"Lower band touch but OFI negative ({ofi:.2f})")
                    return Signal.HOLD

            logger.info(
                f"[MeanReversion] BUY signal: price={current_price:.2f}, "
                f"lower={lower:.2f}, ofi={ofi:.2f}"
            )
            self.entry_band = "lower"
            return Signal.BUY

        # 상단 터치 → 매도
        if current_price >= upper:
            # OFI 확인 (매도 압력 있는지)
            if self.config.ofi_confirm:
                if ofi >= -self.config.ofi_threshold:
                    logger.debug(f"Upper band touch but OFI positive ({ofi:.2f})")
                    return Signal.HOLD

            logger.info(
                f"[MeanReversion] SELL signal: price={current_price:.2f}, "
                f"upper={upper:.2f}, ofi={ofi:.2f}"
            )
            self.entry_band = "upper"
            return Signal.SELL

        return Signal.HOLD

    def _check_exit(
        self,
        bar: BarData,
        upper: float,
        middle: float,
        lower: float
    ) -> Signal:
        """청산 조건 체크"""
        current_price = bar.close

        # 시간 손절
        if self.state.bars_in_position >= self.config.max_bars_in_position:
            logger.info(
                f"[MeanReversion] Time stop: bars={self.state.bars_in_position}"
            )
            return self._get_exit_signal()

        # 중심선 도달 시 청산
        if self.config.exit_at_middle:
            if self.state.position == PositionSide.LONG:
                if current_price >= middle:
                    logger.info(
                        f"[MeanReversion] Exit at middle: price={current_price:.2f}, "
                        f"middle={middle:.2f}"
                    )
                    return Signal.SELL

            elif self.state.position == PositionSide.SHORT:
                if current_price <= middle:
                    logger.info(
                        f"[MeanReversion] Exit at middle: price={current_price:.2f}, "
                        f"middle={middle:.2f}"
                    )
                    return Signal.BUY

        # 반대 밴드 도달 시 청산
        if self.config.exit_at_opposite:
            if self.state.position == PositionSide.LONG:
                if current_price >= upper:
                    logger.info(
                        f"[MeanReversion] Exit at opposite band: price={current_price:.2f}"
                    )
                    return Signal.SELL

            elif self.state.position == PositionSide.SHORT:
                if current_price <= lower:
                    logger.info(
                        f"[MeanReversion] Exit at opposite band: price={current_price:.2f}"
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
        self.entry_band = None
