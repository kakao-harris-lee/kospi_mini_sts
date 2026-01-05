"""
Trend Module - MODE_B Deep Learning Trend Following

Complete trend following system with:
- Ensemble filter (DL + MA + Ichimoku)
- ATR trailing stop
- Time cut (30 min no movement)
"""

from .technical_indicators import TechnicalCalculator, TechnicalData
from .ensemble_filter import EnsembleFilter, FilterResult
from .position_manager import (
    TrendPositionManager,
    TrendPosition,
    ExitSignal,
    ExitReason,
    PositionSide,
)
from .trend_engine import TrendEngine, TrendSignal

__all__ = [
    # Main Engine
    "TrendEngine",
    "TrendSignal",
    # Technical Indicators
    "TechnicalCalculator",
    "TechnicalData",
    # Ensemble Filter
    "EnsembleFilter",
    "FilterResult",
    # Position Manager
    "TrendPositionManager",
    "TrendPosition",
    "ExitSignal",
    "ExitReason",
    "PositionSide",
]
