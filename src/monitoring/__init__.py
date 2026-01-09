"""
Monitoring and Alerting module for KOSPI Mini Trading System.

This module provides comprehensive monitoring capabilities:
- Trade execution notifications (Telegram alerts with P&L)
- System health monitoring (Redis, ClickHouse, KIS API, pipeline services)
- Strategy performance tracking (win rate, Sharpe ratio, drawdown)
- Anomaly detection (volatility spikes, signal frequency, loss streaks)
"""

from src.monitoring.models import (
    Alert,
    AlertType,
    DeliveryStatus,
    TradeExecution,
    ServiceHealth,
    PerformanceMetric,
    Anomaly,
    AnomalyType,
)
from src.monitoring.alert_service import AlertService
from src.monitoring.trade_notifier import TradeNotifier
from src.monitoring.health_checker import HealthChecker
from src.monitoring.performance_tracker import PerformanceTracker
from src.monitoring.anomaly_detector import AnomalyDetector

__all__ = [
    # Models
    "Alert",
    "AlertType",
    "DeliveryStatus",
    "TradeExecution",
    "ServiceHealth",
    "PerformanceMetric",
    "Anomaly",
    "AnomalyType",
    # Services
    "AlertService",
    "TradeNotifier",
    "HealthChecker",
    "PerformanceTracker",
    "AnomalyDetector",
]
