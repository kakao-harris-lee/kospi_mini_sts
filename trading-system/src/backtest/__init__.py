"""
백테스트 모듈

1분 단위 이벤트 루프 기반 백테스트 엔진
- 포지션 관리 (상태 머신)
- 리스크 관리 (손절/익절/시간손절/트레일링)
- 비용 모델 (수수료/슬리피지)
- 거래 시간 필터
"""
from .engine import (
    BacktestEngine,
    BacktestConfig,
    BacktestResult,
    Signal,
    Strategy,
    SimpleMovingAverageStrategy,
)
from .position import (
    PositionManager,
    Position,
    PositionSide,
    Trade,
)
from .risk import (
    RiskManager,
    RiskConfig,
    ExitReason,
)
from .cost import (
    CostModel,
    KOSPIMiniCostModel,
    ZeroCostModel,
    PercentageCostModel,
    TradeCost,
)
from .filters import (
    TradingHoursFilter,
    TradingHoursConfig,
    OptionsExpiryFilter,
)

__all__ = [
    # Engine
    'BacktestEngine',
    'BacktestConfig',
    'BacktestResult',
    'Signal',
    'Strategy',
    'SimpleMovingAverageStrategy',
    # Position
    'PositionManager',
    'Position',
    'PositionSide',
    'Trade',
    # Risk
    'RiskManager',
    'RiskConfig',
    'ExitReason',
    # Cost
    'CostModel',
    'KOSPIMiniCostModel',
    'ZeroCostModel',
    'PercentageCostModel',
    'TradeCost',
    # Filters
    'TradingHoursFilter',
    'TradingHoursConfig',
    'OptionsExpiryFilter',
]
