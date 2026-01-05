"""
Arbitrage Module - MODE_A Sniper Basis Arbitrage

Pure statistical basis arbitrage for KOSPI200 Mini Futures.
Independent of ML predictions, triggers on basis deviation > 2.5σ.
"""

from .basis_calculator import BasisCalculator, BasisData
from .arbitrage_engine import ArbitrageEngine, ArbitrageSignal
from .order_manager import ArbitrageOrderManager, PendingOrder

__all__ = [
    "BasisCalculator",
    "BasisData",
    "ArbitrageEngine",
    "ArbitrageSignal",
    "ArbitrageOrderManager",
    "PendingOrder",
]
