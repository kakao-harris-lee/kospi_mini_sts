"""
Prometheus 메트릭 모듈 (Phase 8.5)

거래 시스템의 핵심 메트릭을 수집하고 Prometheus에 노출합니다.

메트릭 목록:
- strategy_signals_total: 생성된 시그널 수
- orders_executed_total: 실행된 주문 수
- position_pnl: 현재 포지션 손익
- daily_pnl: 일일 손익
- redis_lag_seconds: Redis 처리 지연
- api_errors_total: API 에러 수
- tick_data_received_total: 수신된 틱 데이터 수
- orderbook_updates_total: 호가 업데이트 수
"""

import os
import logging
import threading
from typing import Optional
from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    Summary,
    CollectorRegistry,
    start_http_server,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

logger = logging.getLogger(__name__)


# =============================================================================
# 메트릭 정의
# =============================================================================

# 전역 레지스트리
REGISTRY = CollectorRegistry(auto_describe=True)

# -----------------------------------------------------------------------------
# Strategy 메트릭
# -----------------------------------------------------------------------------

STRATEGY_SIGNALS = Counter(
    "strategy_signals_total",
    "Total number of trading signals generated",
    ["strategy", "signal_type", "direction"],  # BUY, SELL, EXIT
    registry=REGISTRY,
)

ORDERS_EXECUTED = Counter(
    "orders_executed_total",
    "Total number of orders executed",
    ["strategy", "order_type", "side", "status"],  # MARKET/LIMIT, BUY/SELL, SUCCESS/FAILED
    registry=REGISTRY,
)

POSITION_PNL = Gauge(
    "position_pnl",
    "Current position P&L in KRW",
    ["strategy", "symbol"],
    registry=REGISTRY,
)

DAILY_PNL = Gauge(
    "daily_pnl",
    "Daily P&L in KRW",
    ["strategy"],
    registry=REGISTRY,
)

POSITION_SIZE = Gauge(
    "position_size",
    "Current position size (contracts)",
    ["strategy", "symbol", "side"],
    registry=REGISTRY,
)

# -----------------------------------------------------------------------------
# Data Collection 메트릭
# -----------------------------------------------------------------------------

TICK_DATA_RECEIVED = Counter(
    "tick_data_received_total",
    "Total tick data points received",
    ["symbol", "data_type"],  # orderbook, trade
    registry=REGISTRY,
)

ORDERBOOK_UPDATES = Counter(
    "orderbook_updates_total",
    "Total orderbook snapshot updates",
    ["symbol"],
    registry=REGISTRY,
)

TRADE_TICKS = Counter(
    "trade_ticks_total",
    "Total trade ticks received",
    ["symbol", "side"],
    registry=REGISTRY,
)

WEBSOCKET_STATUS = Gauge(
    "websocket_connected",
    "WebSocket connection status (1=connected, 0=disconnected)",
    ["endpoint"],
    registry=REGISTRY,
)

# -----------------------------------------------------------------------------
# Processing 메트릭
# -----------------------------------------------------------------------------

FEATURE_CALCULATIONS = Counter(
    "feature_calculations_total",
    "Total feature calculations performed",
    ["feature_name"],
    registry=REGISTRY,
)

REDIS_LAG = Gauge(
    "redis_lag_seconds",
    "Redis stream processing lag in seconds",
    ["stream", "consumer_group"],
    registry=REGISTRY,
)

REDIS_MESSAGES_PROCESSED = Counter(
    "redis_messages_processed_total",
    "Total Redis stream messages processed",
    ["stream", "consumer_group"],
    registry=REGISTRY,
)

# -----------------------------------------------------------------------------
# Database 메트릭
# -----------------------------------------------------------------------------

CLICKHOUSE_INSERTS = Counter(
    "clickhouse_inserts_total",
    "Total ClickHouse batch inserts",
    ["table"],
    registry=REGISTRY,
)

CLICKHOUSE_INSERT_ROWS = Counter(
    "clickhouse_insert_rows_total",
    "Total rows inserted into ClickHouse",
    ["table"],
    registry=REGISTRY,
)

CLICKHOUSE_ERRORS = Counter(
    "clickhouse_errors_total",
    "Total ClickHouse errors",
    ["table", "error_type"],
    registry=REGISTRY,
)

# -----------------------------------------------------------------------------
# API 메트릭
# -----------------------------------------------------------------------------

API_REQUESTS = Counter(
    "api_requests_total",
    "Total API requests made",
    ["api", "endpoint", "status"],  # KIS_REST, KIS_WS / SUCCESS, ERROR
    registry=REGISTRY,
)

API_ERRORS = Counter(
    "api_errors_total",
    "Total API errors",
    ["api", "error_type"],
    registry=REGISTRY,
)

API_LATENCY = Histogram(
    "api_latency_seconds",
    "API request latency in seconds",
    ["api", "endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

# -----------------------------------------------------------------------------
# System 메트릭
# -----------------------------------------------------------------------------

COMPONENT_UP = Gauge(
    "component_up",
    "Component health status (1=up, 0=down)",
    ["component"],  # tick_collector, feature_processor, strategy_manager, db_logger
    registry=REGISTRY,
)

COMPONENT_RESTARTS = Counter(
    "component_restarts_total",
    "Total component restarts",
    ["component"],
    registry=REGISTRY,
)


# =============================================================================
# 메트릭 헬퍼 클래스
# =============================================================================

class TradingMetrics:
    """
    트레이딩 메트릭 관리 클래스

    각 컴포넌트에서 싱글톤으로 사용
    """
    _instance: Optional["TradingMetrics"] = None
    _lock = threading.Lock()
    _server_started = False

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        self._component_name: Optional[str] = None

    def set_component(self, name: str):
        """컴포넌트 이름 설정"""
        self._component_name = name
        COMPONENT_UP.labels(component=name).set(1)

    def start_server(self, port: int = 8080):
        """
        Prometheus 메트릭 HTTP 서버 시작

        Args:
            port: HTTP 서버 포트 (기본값: 8080)
        """
        with self._lock:
            if self._server_started:
                return

            try:
                start_http_server(port, registry=REGISTRY)
                self._server_started = True
                logger.info(f"Prometheus metrics server started on port {port}")
            except Exception as e:
                logger.error(f"Failed to start metrics server: {e}")

    # -------------------------------------------------------------------------
    # Strategy 메트릭 메서드
    # -------------------------------------------------------------------------

    def record_signal(self, strategy: str, signal_type: str, direction: str):
        """시그널 기록"""
        STRATEGY_SIGNALS.labels(
            strategy=strategy,
            signal_type=signal_type,
            direction=direction
        ).inc()

    def record_order(self, strategy: str, order_type: str, side: str, status: str):
        """주문 기록"""
        ORDERS_EXECUTED.labels(
            strategy=strategy,
            order_type=order_type,
            side=side,
            status=status
        ).inc()

    def update_position_pnl(self, strategy: str, symbol: str, pnl: float):
        """포지션 손익 업데이트"""
        POSITION_PNL.labels(strategy=strategy, symbol=symbol).set(pnl)

    def update_daily_pnl(self, strategy: str, pnl: float):
        """일일 손익 업데이트"""
        DAILY_PNL.labels(strategy=strategy).set(pnl)

    def update_position_size(self, strategy: str, symbol: str, side: str, size: float):
        """포지션 크기 업데이트"""
        POSITION_SIZE.labels(strategy=strategy, symbol=symbol, side=side).set(size)

    # -------------------------------------------------------------------------
    # Data Collection 메트릭 메서드
    # -------------------------------------------------------------------------

    def record_tick(self, symbol: str, data_type: str):
        """틱 데이터 기록"""
        TICK_DATA_RECEIVED.labels(symbol=symbol, data_type=data_type).inc()

    def record_orderbook_update(self, symbol: str):
        """호가 업데이트 기록"""
        ORDERBOOK_UPDATES.labels(symbol=symbol).inc()

    def record_trade_tick(self, symbol: str, side: str):
        """체결 틱 기록"""
        TRADE_TICKS.labels(symbol=symbol, side=side).inc()

    def set_websocket_status(self, endpoint: str, connected: bool):
        """WebSocket 상태 설정"""
        WEBSOCKET_STATUS.labels(endpoint=endpoint).set(1 if connected else 0)

    # -------------------------------------------------------------------------
    # Processing 메트릭 메서드
    # -------------------------------------------------------------------------

    def record_feature_calculation(self, feature_name: str):
        """피처 계산 기록"""
        FEATURE_CALCULATIONS.labels(feature_name=feature_name).inc()

    def update_redis_lag(self, stream: str, consumer_group: str, lag_seconds: float):
        """Redis 지연 업데이트"""
        REDIS_LAG.labels(stream=stream, consumer_group=consumer_group).set(lag_seconds)

    def record_redis_message(self, stream: str, consumer_group: str):
        """Redis 메시지 처리 기록"""
        REDIS_MESSAGES_PROCESSED.labels(
            stream=stream,
            consumer_group=consumer_group
        ).inc()

    # -------------------------------------------------------------------------
    # Database 메트릭 메서드
    # -------------------------------------------------------------------------

    def record_clickhouse_insert(self, table: str, row_count: int):
        """ClickHouse 삽입 기록"""
        CLICKHOUSE_INSERTS.labels(table=table).inc()
        CLICKHOUSE_INSERT_ROWS.labels(table=table).inc(row_count)

    def record_clickhouse_error(self, table: str, error_type: str):
        """ClickHouse 에러 기록"""
        CLICKHOUSE_ERRORS.labels(table=table, error_type=error_type).inc()

    # -------------------------------------------------------------------------
    # API 메트릭 메서드
    # -------------------------------------------------------------------------

    def record_api_request(self, api: str, endpoint: str, status: str):
        """API 요청 기록"""
        API_REQUESTS.labels(api=api, endpoint=endpoint, status=status).inc()

    def record_api_error(self, api: str, error_type: str):
        """API 에러 기록"""
        API_ERRORS.labels(api=api, error_type=error_type).inc()

    def observe_api_latency(self, api: str, endpoint: str, latency: float):
        """API 지연 측정"""
        API_LATENCY.labels(api=api, endpoint=endpoint).observe(latency)

    # -------------------------------------------------------------------------
    # System 메트릭 메서드
    # -------------------------------------------------------------------------

    def set_component_up(self, component: str, is_up: bool):
        """컴포넌트 상태 설정"""
        COMPONENT_UP.labels(component=component).set(1 if is_up else 0)

    def record_restart(self, component: str):
        """컴포넌트 재시작 기록"""
        COMPONENT_RESTARTS.labels(component=component).inc()


# 전역 메트릭 인스턴스
metrics = TradingMetrics()


def get_metrics() -> TradingMetrics:
    """전역 메트릭 인스턴스 반환"""
    return metrics


def init_metrics(component_name: str, port: int = 8080):
    """
    메트릭 초기화 및 서버 시작

    Args:
        component_name: 컴포넌트 이름 (tick_collector, feature_processor 등)
        port: 메트릭 HTTP 서버 포트
    """
    m = get_metrics()
    m.set_component(component_name)

    # 환경변수로 메트릭 서버 포트 오버라이드 가능
    metrics_port = int(os.getenv("METRICS_PORT", str(port)))
    m.start_server(metrics_port)

    return m
