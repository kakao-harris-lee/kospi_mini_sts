"""
Strategy Adapter - BaseStrategy를 백테스트 엔진과 호환

백테스트 엔진은 on_bar(dict) -> Signal 인터페이스를 사용하고,
전략들은 generate_signal(BarData) -> Signal 인터페이스를 사용.

이 어댑터가 두 인터페이스를 연결합니다.
"""
from typing import Dict, Any, Type
from enum import Enum

from src.strategy.base import BaseStrategy, BarData, Signal as StrategySignal, PositionSide
from src.backtest.engine import Signal as BacktestSignal


class StrategyAdapter:
    """
    BaseStrategy → 백테스트 엔진 호환 어댑터

    사용법:
        strategy = PureMicrostructureStrategy()
        adapter = StrategyAdapter(strategy)
        result = engine.run(adapter, data)
    """

    def __init__(self, strategy: BaseStrategy):
        """
        Args:
            strategy: BaseStrategy를 상속받은 전략 인스턴스
        """
        self.strategy = strategy

    def on_bar(self, bar: Dict[str, Any]) -> BacktestSignal:
        """
        백테스트 엔진용 인터페이스

        Args:
            bar: OHLCV + features 딕셔너리

        Returns:
            백테스트 엔진의 Signal enum
        """
        # dict → BarData 변환
        bar_data = BarData.from_dict(bar)

        # 히스토리 업데이트
        self.strategy.history.append(bar_data)

        # 포지션 상태 업데이트 (백테스트 엔진이 관리하지만 전략도 알아야 함)
        self.strategy.state.bars_in_position += 1

        # 시그널 생성
        strategy_signal = self.strategy.generate_signal(bar_data)

        # 포지션 상태 동기화
        if strategy_signal == StrategySignal.BUY:
            if self.strategy.state.position == PositionSide.FLAT:
                self.strategy.state.position = PositionSide.LONG
                self.strategy.state.entry_price = bar_data.close
                self.strategy.state.bars_in_position = 0
            elif self.strategy.state.position == PositionSide.SHORT:
                self.strategy.state.position = PositionSide.FLAT
                self.strategy.state.entry_price = 0
                self.strategy.state.bars_in_position = 0

        elif strategy_signal == StrategySignal.SELL:
            if self.strategy.state.position == PositionSide.FLAT:
                self.strategy.state.position = PositionSide.SHORT
                self.strategy.state.entry_price = bar_data.close
                self.strategy.state.bars_in_position = 0
            elif self.strategy.state.position == PositionSide.LONG:
                self.strategy.state.position = PositionSide.FLAT
                self.strategy.state.entry_price = 0
                self.strategy.state.bars_in_position = 0

        # Signal 변환 (전략 Signal → 백테스트 Signal)
        return self._convert_signal(strategy_signal)

    def _convert_signal(self, strategy_signal: StrategySignal) -> BacktestSignal:
        """Signal enum 변환"""
        if strategy_signal == StrategySignal.BUY:
            return BacktestSignal.BUY
        elif strategy_signal == StrategySignal.SELL:
            return BacktestSignal.SELL
        else:
            return BacktestSignal.HOLD

    def reset(self):
        """전략 상태 초기화"""
        self.strategy.reset()


def create_backtest_strategy(
    strategy_class: Type[BaseStrategy],
    **kwargs
) -> StrategyAdapter:
    """
    백테스트용 전략 생성 헬퍼

    Args:
        strategy_class: BaseStrategy 서브클래스
        **kwargs: 전략 생성자 인자

    Returns:
        백테스트 엔진과 호환되는 StrategyAdapter

    Example:
        adapter = create_backtest_strategy(
            PureMicrostructureStrategy,
            config=PureMicroConfig(entry_score_threshold=0.55)
        )
    """
    strategy = strategy_class(**kwargs)
    return StrategyAdapter(strategy)
