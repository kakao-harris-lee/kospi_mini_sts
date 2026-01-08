"""common 패키지"""
from .redis_client import (
    RedisClient,
    StreamPublisher,
    StreamConsumer,
    MultiStreamConsumer,
    StreamMessage
)
from .clickhouse_client import (
    ClickHouseClient,
    BatchInserter,
    BatchConfig,
    init_tables
)
from .logging_config import setup_logging, MetricsLogger
from .metrics import (
    TradingMetrics,
    get_metrics,
    init_metrics,
    REGISTRY,
)
from .telegram import (
    TelegramNotifier,
    send_message,
    notify,
    notify_error,
    notify_success,
    get_notifier,
)
from .detailed_logger import (
    trading_logger,
    enable_detailed_logging,
    disable_detailed_logging,
    DetailedTradingLogger,
)
from .decision_logger import (
    DecisionLogger,
    DecisionLog,
    get_decision_logger,
    log_entry,
    log_close,
)

# Architecture improvements (v0.0.3)
from .circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    CircuitOpenError,
    get_kis_api_breaker,
    get_kis_websocket_breaker,
    get_clickhouse_breaker,
    get_redis_breaker,
)
from .backpressure import (
    BackpressureMonitor,
    LagThresholds,
    LagInfo,
    get_backpressure_monitor,
)
from .state_snapshot import (
    StateSnapshotManager,
    SnapshotConfig,
    SnapshotMetadata,
    PeriodicSnapshotSaver,
    create_processor_snapshot_manager,
)
from .stream_contracts import (
    StreamContract,
    FieldSpec,
    ValidationMode,
    ContractValidationError,
    validate_message,
    get_contract,
    set_validation_mode,
    RAW_DATA_CONTRACT,
    FEATURE_CONTRACT,
    PREDICTION_CONTRACT,
    ORDER_COMMAND_CONTRACT,
)

__all__ = [
    # Redis
    "RedisClient",
    "StreamPublisher",
    "StreamConsumer",
    "MultiStreamConsumer",
    "StreamMessage",
    # ClickHouse
    "ClickHouseClient",
    "BatchInserter",
    "BatchConfig",
    "init_tables",
    # Logging
    "setup_logging",
    "MetricsLogger",
    "TradingMetrics",
    "get_metrics",
    "init_metrics",
    "REGISTRY",
    # Telegram
    "TelegramNotifier",
    "send_message",
    "notify",
    "notify_error",
    "notify_success",
    "get_notifier",
    # Detailed logging
    "trading_logger",
    "enable_detailed_logging",
    "disable_detailed_logging",
    "DetailedTradingLogger",
    # Decision logging
    "DecisionLogger",
    "DecisionLog",
    "get_decision_logger",
    "log_entry",
    "log_close",
    # Circuit Breaker
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitState",
    "CircuitOpenError",
    "get_kis_api_breaker",
    "get_kis_websocket_breaker",
    "get_clickhouse_breaker",
    "get_redis_breaker",
    # Backpressure
    "BackpressureMonitor",
    "LagThresholds",
    "LagInfo",
    "get_backpressure_monitor",
    # State Snapshot
    "StateSnapshotManager",
    "SnapshotConfig",
    "SnapshotMetadata",
    "PeriodicSnapshotSaver",
    "create_processor_snapshot_manager",
    # Stream Contracts
    "StreamContract",
    "FieldSpec",
    "ValidationMode",
    "ContractValidationError",
    "validate_message",
    "get_contract",
    "set_validation_mode",
    "RAW_DATA_CONTRACT",
    "FEATURE_CONTRACT",
    "PREDICTION_CONTRACT",
    "ORDER_COMMAND_CONTRACT",
]
