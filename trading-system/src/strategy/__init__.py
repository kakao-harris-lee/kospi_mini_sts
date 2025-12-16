"""
Strategy 패키지

실시간 트레이딩 및 백테스트용 전략 모듈
- 레짐 판단기 (변동성 기반)
- 마이크로스트럭처 시그널 (OFI, 호가불균형)
- 다양한 전략 구현 (Mean Reversion, Breakout, Hybrid)
"""
from .strategy_manager import (
    StrategyManager,
    TradingMode,
    OrderCommand,
    OrderSide,
    OrderType,
    BaseOrderExecutor,
    DryRunOrderExecutor
)

from .base import (
    BaseStrategy,
    Signal,
    PositionSide,
    StrategyState,
    BarData,
)

from .regime_detector import (
    RegimeDetector,
    RegimeConfig,
    VolatilityRegime,
)

from .signals import (
    OFICalculator,
    OrderBookImbalance,
    SpreadAnalyzer,
    TradeFlowAnalyzer,
    MicrostructureSignals,
)

from .strategies import (
    MeanReversionStrategy,
    BreakoutStrategy,
    OFIMomentumStrategy,
    HybridStrategy,
    PureMicrostructureStrategy,
    AdaptiveMicrostructureStrategy,
    PureMicroConfig,
)

__all__ = [
    # Strategy Manager
    "StrategyManager",
    "TradingMode",
    "OrderCommand",
    "OrderSide",
    "OrderType",
    "BaseOrderExecutor",
    "DryRunOrderExecutor",
    # Base
    "BaseStrategy",
    "Signal",
    "PositionSide",
    "StrategyState",
    "BarData",
    # Regime
    "RegimeDetector",
    "RegimeConfig",
    "VolatilityRegime",
    # Signals
    "OFICalculator",
    "OrderBookImbalance",
    "SpreadAnalyzer",
    "TradeFlowAnalyzer",
    "MicrostructureSignals",
    # Strategies
    "MeanReversionStrategy",
    "BreakoutStrategy",
    "OFIMomentumStrategy",
    "HybridStrategy",
    "PureMicrostructureStrategy",
    "AdaptiveMicrostructureStrategy",
    "PureMicroConfig",
]
