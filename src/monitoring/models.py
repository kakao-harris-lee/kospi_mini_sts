"""
Data models for the monitoring and alerting system.

This module defines all dataclasses and enums used by the monitoring system:
- Alert: Notification record for audit and delivery tracking
- AlertType, DeliveryStatus: Enums for alert classification
- TradeExecution: Completed trade with P&L
- ServiceHealth: Health status of monitored services
- PerformanceMetric: Aggregated strategy performance data
- Anomaly, AnomalyType: Detected unusual conditions
"""

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, Literal
import json


# =============================================================================
# Enums
# =============================================================================

class AlertType(str, Enum):
    """Types of alerts the system can generate."""
    TRADE_EXECUTION = "trade_execution"
    HEALTH_WARNING = "health_warning"
    HEALTH_CRITICAL = "health_critical"
    ANOMALY_DETECTED = "anomaly_detected"
    DAILY_SUMMARY = "daily_summary"
    SYSTEM_START = "system_start"
    SYSTEM_STOP = "system_stop"


class DeliveryStatus(str, Enum):
    """Alert delivery status."""
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class AnomalyType(str, Enum):
    """Types of anomalies the system can detect."""
    PRICE_VOLATILITY = "price_volatility"
    SIGNAL_FREQUENCY = "signal_frequency"
    LOSS_STREAK = "loss_streak"
    API_RATE_LIMIT = "api_rate_limit"
    DATA_GAP = "data_gap"
    LATENCY_SPIKE = "latency_spike"


class HealthStatus(str, Enum):
    """Service health status."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


# =============================================================================
# Alert Model (T012)
# =============================================================================

@dataclass
class Alert:
    """
    Notification record for audit and delivery tracking.

    Attributes:
        id: Unique identifier
        created_at: Creation time (UTC)
        alert_type: Type of alert
        channel: Delivery channel (telegram, log)
        priority: Alert priority (low, normal, high, urgent)
        title: Alert title (max 100 chars)
        message: Alert body (max 4096 chars for Telegram)
        delivery_status: Current delivery status
        delivered_at: When the alert was delivered
        retry_count: Number of retry attempts (max 3)
        last_error: Last delivery error message
        source_id: ID of the source entity
        source_type: Type of the source entity
    """
    alert_type: AlertType
    channel: Literal["telegram", "log"]
    priority: Literal["low", "normal", "high", "urgent"]
    title: str
    message: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    delivery_status: DeliveryStatus = DeliveryStatus.PENDING
    delivered_at: Optional[datetime] = None
    retry_count: int = 0
    last_error: Optional[str] = None
    source_id: Optional[str] = None
    source_type: Optional[str] = None

    def __post_init__(self):
        """Validate alert fields."""
        if len(self.title) > 100:
            raise ValueError("Alert title must be at most 100 characters")
        if len(self.message) > 4096:
            raise ValueError("Alert message must be at most 4096 characters")
        if self.retry_count > 3:
            raise ValueError("Retry count cannot exceed 3")

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        data = asdict(self)
        data["alert_type"] = self.alert_type.value
        data["delivery_status"] = self.delivery_status.value
        data["created_at"] = self.created_at.isoformat()
        if self.delivered_at:
            data["delivered_at"] = self.delivered_at.isoformat()
        return data

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict) -> "Alert":
        """Create Alert from dictionary."""
        data = data.copy()
        data["alert_type"] = AlertType(data["alert_type"])
        data["delivery_status"] = DeliveryStatus(data["delivery_status"])
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        if data.get("delivered_at"):
            data["delivered_at"] = datetime.fromisoformat(data["delivered_at"])
        return cls(**data)

    @classmethod
    def from_json(cls, json_str: str) -> "Alert":
        """Deserialize from JSON string."""
        return cls.from_dict(json.loads(json_str))

    def mark_sent(self) -> None:
        """Mark alert as successfully delivered."""
        self.delivery_status = DeliveryStatus.SENT
        self.delivered_at = datetime.now(timezone.utc)

    def mark_failed(self, error: str) -> None:
        """Mark alert as failed with error message."""
        self.delivery_status = DeliveryStatus.FAILED
        self.last_error = error
        self.retry_count += 1
        if self.retry_count >= 3:
            self.delivery_status = DeliveryStatus.DEAD_LETTER


# =============================================================================
# TradeExecution Model (T014)
# =============================================================================

@dataclass
class TradeExecution:
    """
    Represents a completed trade with calculated P&L.

    Attributes:
        symbol: Futures code (e.g., "A05601")
        side: Trade direction (BUY or SELL)
        quantity: Number of contracts
        price: Execution price
        strategy: Strategy identifier
        order_type: Order type (MARKET or LIMIT)
        id: Unique identifier
        timestamp: Execution time (UTC)
        realized_pnl: Realized P&L from this trade
        commission: Trading commission
        slippage: Price slippage
    """
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int
    price: float
    strategy: str
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    realized_pnl: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0

    def __post_init__(self):
        """Validate trade execution fields."""
        if self.symbol not in ("A05601", "A05602"):
            raise ValueError(f"Invalid symbol: {self.symbol}. Must be A05601 or A05602")
        if self.quantity <= 0:
            raise ValueError("Quantity must be positive")
        if self.price <= 0:
            raise ValueError("Price must be positive")

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict) -> "TradeExecution":
        """Create TradeExecution from dictionary."""
        data = data.copy()
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return cls(**data)

    @classmethod
    def from_json(cls, json_str: str) -> "TradeExecution":
        """Deserialize from JSON string."""
        return cls.from_dict(json.loads(json_str))


# =============================================================================
# ServiceHealth Model (T022)
# =============================================================================

ALLOWED_SERVICES = {
    "redis",
    "clickhouse",
    "kis_api",
    "tick_collector",
    "feature_processor",
    "prediction_engine",
    "strategy_manager",
}


@dataclass
class ServiceHealth:
    """
    Represents health status of a monitored service.

    Attributes:
        service: Service name (from allowed list)
        status: Current status (healthy, degraded, down)
        last_check: Last health check time (UTC)
        latency_ms: Response latency in milliseconds
        error: Error message if unhealthy
        consecutive_failures: Number of consecutive failures
    """
    service: str
    status: HealthStatus = HealthStatus.HEALTHY
    last_check: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    latency_ms: Optional[float] = None
    error: Optional[str] = None
    consecutive_failures: int = 0

    def __post_init__(self):
        """Validate service health fields."""
        if self.service not in ALLOWED_SERVICES:
            raise ValueError(
                f"Invalid service: {self.service}. "
                f"Must be one of: {', '.join(sorted(ALLOWED_SERVICES))}"
            )
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError("Latency cannot be negative")

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        data = asdict(self)
        data["status"] = self.status.value
        data["last_check"] = self.last_check.isoformat()
        return data

    def record_success(self, latency_ms: float) -> None:
        """Record a successful health check."""
        self.status = HealthStatus.HEALTHY
        self.latency_ms = latency_ms
        self.last_check = datetime.now(timezone.utc)
        self.consecutive_failures = 0
        self.error = None

    def record_failure(self, error: str) -> None:
        """Record a failed health check."""
        self.consecutive_failures += 1
        self.last_check = datetime.now(timezone.utc)
        self.error = error

        if self.consecutive_failures >= 3:
            self.status = HealthStatus.DOWN
        else:
            self.status = HealthStatus.DEGRADED

    @property
    def is_healthy(self) -> bool:
        """Check if service is healthy."""
        return self.status == HealthStatus.HEALTHY


# =============================================================================
# PerformanceMetric Model (T036)
# =============================================================================

@dataclass
class PerformanceMetric:
    """
    Aggregated performance data for a strategy.

    Attributes:
        strategy: Strategy identifier
        symbol: Futures code
        period: Aggregation period (minute, hour, day)
        datetime: Aggregation timestamp (UTC)
        total_pnl: Total P&L
        realized_pnl: Realized P&L
        unrealized_pnl: Unrealized P&L
        trade_count: Number of trades
        win_count: Winning trades count
        loss_count: Losing trades count
        win_rate: Win percentage (0.0 - 1.0)
        avg_win: Average winning trade
        avg_loss: Average losing trade
        profit_factor: Gross profit / gross loss
        max_drawdown: Maximum drawdown
        sharpe_ratio: Risk-adjusted return
    """
    strategy: str
    symbol: str
    period: Literal["minute", "hour", "day"]
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    datetime: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    total_pnl: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0

    def __post_init__(self):
        """Validate performance metric fields."""
        if not 0.0 <= self.win_rate <= 1.0:
            raise ValueError("Win rate must be between 0.0 and 1.0")

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        data = asdict(self)
        data["datetime"] = self.datetime.isoformat()
        return data

    def to_clickhouse_row(self) -> tuple:
        """Convert to tuple for ClickHouse batch insertion."""
        return (
            self.datetime,
            self.strategy,
            self.symbol,
            self.period,
            self.total_pnl,
            self.realized_pnl,
            self.unrealized_pnl,
            self.trade_count,
            self.win_count,
            self.loss_count,
            self.win_rate,
            self.avg_win,
            self.avg_loss,
            self.profit_factor,
            self.max_drawdown,
            self.sharpe_ratio,
        )


# =============================================================================
# Anomaly Model (T053)
# =============================================================================

@dataclass
class Anomaly:
    """
    Detected unusual condition requiring attention.

    Attributes:
        anomaly_type: Type of anomaly
        severity: Severity level (low, medium, high, critical)
        detected_value: Observed value
        threshold: Threshold that was breached
        description: Human-readable description
        recommended_action: Suggested response
        id: Unique identifier
        timestamp: Detection time (UTC)
        acknowledged: Has been acknowledged
        acknowledged_at: Acknowledgement time
    """
    anomaly_type: AnomalyType
    severity: Literal["low", "medium", "high", "critical"]
    detected_value: float
    threshold: float
    description: str
    recommended_action: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    acknowledged: bool = False
    acknowledged_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        data = asdict(self)
        data["anomaly_type"] = self.anomaly_type.value
        data["timestamp"] = self.timestamp.isoformat()
        if self.acknowledged_at:
            data["acknowledged_at"] = self.acknowledged_at.isoformat()
        return data

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict) -> "Anomaly":
        """Create Anomaly from dictionary."""
        data = data.copy()
        data["anomaly_type"] = AnomalyType(data["anomaly_type"])
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        if data.get("acknowledged_at"):
            data["acknowledged_at"] = datetime.fromisoformat(data["acknowledged_at"])
        return cls(**data)

    def acknowledge(self) -> None:
        """Mark anomaly as acknowledged."""
        self.acknowledged = True
        self.acknowledged_at = datetime.now(timezone.utc)
