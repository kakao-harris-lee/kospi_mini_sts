"""
트레이딩 시스템 설정
환경변수로 오버라이드 가능
"""
import os
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

# .env 파일 자동 로드
try:
    from dotenv import load_dotenv
    # 여러 위치에서 .env 찾기
    for env_path in [
        Path(__file__).parent.parent / ".env",  # trading-system/.env
        Path.cwd() / ".env",                     # 현재 디렉토리
    ]:
        if env_path.exists():
            load_dotenv(env_path)
            break
except ImportError:
    pass


@dataclass
class KISConfig:
    """한국투자증권 API 설정"""
    app_key: str = os.getenv("KIS_APP_KEY", "")
    app_secret: str = os.getenv("KIS_APP_SECRET", "")
    account_no: str = os.getenv("KIS_ACCOUNT_NO", "")
    is_mock: bool = os.getenv("KIS_MARKET", "real") == "mock"


@dataclass
class RedisConfig:
    host: str = os.getenv("REDIS_HOST", "localhost")
    port: int = int(os.getenv("REDIS_PORT", "6379"))
    password: Optional[str] = os.getenv("REDIS_PASSWORD")
    db: int = int(os.getenv("REDIS_DB", "0"))
    decode_responses: bool = True
    
    # Stream 설정
    raw_stream: str = "RAW_DATA_STREAM"
    feature_stream: str = "FEATURE_STREAM"
    prediction_stream: str = "PREDICTION_STREAM"
    order_stream: str = "ORDER_COMMAND_STREAM"
    
    # Stream 최대 길이 (메모리 관리)
    stream_maxlen: int = 10000


@dataclass
class ClickHouseConfig:
    host: str = os.getenv("CLICKHOUSE_HOST", "localhost")
    port: int = int(os.getenv("CLICKHOUSE_PORT", "8123"))
    database: str = os.getenv("CLICKHOUSE_DATABASE", "kospi")
    user: str = os.getenv("CLICKHOUSE_USER", "default")
    password: str = os.getenv("CLICKHOUSE_PASSWORD", "")
    
    # 배치 설정
    batch_size: int = 1000
    batch_timeout_sec: float = 1.0


@dataclass
class ModelConfig:
    model_path: str = os.getenv("MODEL_PATH", "models/trading_lstm.pth")
    device: str = os.getenv("MODEL_DEVICE", "auto")  # cuda, mps, cpu, auto
    lookback_window: int = 60  # 60분 시퀀스
    model_version: str = "v1.0"


@dataclass
class StrategyConfig:
    # 모드 A: 스나이퍼 차익거래 (legacy - see ArbitrageConfig for new implementation)
    mode_a_liquidity_threshold: float = 80.0
    mode_a_basis_gap_sigma: float = 2.5
    mode_a_up_prob_threshold: float = 0.65
    mode_a_order_size: float = 5.0

    # 모드 B: 딥러닝 추세 매매
    mode_b_basis_gap_sigma: float = 1.0
    mode_b_liquidity_min: float = 50.0
    mode_b_up_prob_buy: float = 0.85
    mode_b_down_prob_sell: float = 0.15
    mode_b_order_size: float = 1.0

    # 회피 구간
    liquidity_avoid_threshold: float = 50.0


@dataclass
class ArbitrageConfig:
    """MODE_A Sniper Arbitrage Settings (Pure Basis Arbitrage)"""

    # Entry Filters
    max_spread_ticks: int = int(os.getenv("ARBITRAGE_MAX_SPREAD_TICKS", "2"))
    depth_multiplier: float = float(os.getenv("ARBITRAGE_DEPTH_MULTIPLIER", "5.0"))
    basis_threshold: float = float(os.getenv("ARBITRAGE_BASIS_THRESHOLD", "2.5"))

    # Order Execution
    order_size: float = float(os.getenv("ARBITRAGE_ORDER_SIZE", "5.0"))
    order_timeout_sec: float = float(os.getenv("ARBITRAGE_ORDER_TIMEOUT", "10.0"))

    # Basis Calculation
    risk_free_rate: float = float(os.getenv("ARBITRAGE_RISK_FREE_RATE", "0.035"))
    basis_rolling_window: int = int(os.getenv("ARBITRAGE_ROLLING_WINDOW", "60"))

    # Blackout Periods
    quarterly_blackout_days: int = int(os.getenv("ARBITRAGE_BLACKOUT_DAYS", "14"))

    # KOSPI200 Index
    index_symbol: str = os.getenv("ARBITRAGE_INDEX_SYMBOL", "0001")  # KOSPI200 index code

    # Redis Streams
    index_stream: str = "INDEX_STREAM"


@dataclass
class ConsumerGroupConfig:
    """각 모듈의 Consumer Group 설정"""
    collector_group: str = "collector_group"
    processor_group: str = "feature_processor_group"
    logger_group: str = "db_logger_group"
    raw_data_logger_group: str = "raw_data_logger"  # v0.0.2: RAW_DATA_STREAM → ClickHouse
    prediction_group: str = "prediction_engine_group"
    strategy_group: str = "strategy_manager_group"

    # 재시작 시 읽기 시작 위치
    # "0" = 처음부터, "$" = 새 메시지만, ">" = 미처리 메시지부터
    start_id: str = ">"

    # 블로킹 읽기 타임아웃 (ms)
    block_ms: int = 1000

    # 한 번에 읽어올 메시지 수
    read_count: int = 100


@dataclass
class MockPredictionConfig:
    """Mock Prediction Engine 설정 (v0.0.2) - 테스트/백테스트 전용"""
    enabled: bool = os.getenv("USE_MOCK_PREDICTION", "false").lower() == "true"
    mode: str = os.getenv("MOCK_PREDICTION_MODE", "ofi_based")  # random | ma_cross | ofi_based


@dataclass
class ResilienceConfig:
    """Resilience settings (v0.0.3) - Circuit Breaker, Backpressure, State Snapshot"""
    # Circuit Breaker
    circuit_breaker_enabled: bool = os.getenv("CIRCUIT_BREAKER_ENABLED", "true").lower() == "true"
    circuit_breaker_failure_threshold: int = int(os.getenv("CIRCUIT_BREAKER_FAILURES", "5"))
    circuit_breaker_recovery_timeout: float = float(os.getenv("CIRCUIT_BREAKER_RECOVERY", "60.0"))

    # Backpressure
    backpressure_warning_lag: int = int(os.getenv("BACKPRESSURE_WARNING", "1000"))
    backpressure_critical_lag: int = int(os.getenv("BACKPRESSURE_CRITICAL", "5000"))
    backpressure_emergency_lag: int = int(os.getenv("BACKPRESSURE_EMERGENCY", "8000"))

    # State Snapshots
    state_snapshot_enabled: bool = os.getenv("STATE_SNAPSHOT_ENABLED", "true").lower() == "true"
    state_snapshot_interval: float = float(os.getenv("STATE_SNAPSHOT_INTERVAL", "60.0"))
    state_snapshot_ttl: int = int(os.getenv("STATE_SNAPSHOT_TTL", "21600"))  # 6 hours

    # Contract Validation: disabled | warn | strict
    contract_validation_mode: str = os.getenv("CONTRACT_VALIDATION_MODE", "warn")


@dataclass
class TrendConfig:
    """MODE_B Deep Learning Trend Following Settings"""

    # Entry Filters
    dl_threshold: float = float(os.getenv("TREND_DL_THRESHOLD", "0.85"))
    ma_fast_period: int = int(os.getenv("TREND_MA_FAST", "20"))
    ma_slow_period: int = int(os.getenv("TREND_MA_SLOW", "60"))

    # ATR Settings
    atr_period: int = int(os.getenv("TREND_ATR_PERIOD", "14"))
    atr_stop_multiplier: float = float(os.getenv("TREND_ATR_MULTIPLIER", "2.0"))

    # Time Cut
    time_cut_minutes: int = int(os.getenv("TREND_TIME_CUT_MIN", "30"))
    time_cut_atr_threshold: float = float(os.getenv("TREND_TIME_CUT_ATR", "0.5"))

    # Order Execution
    order_size: float = float(os.getenv("TREND_ORDER_SIZE", "1.0"))

    # Warmup
    min_bars_required: int = int(os.getenv("TREND_MIN_BARS", "60"))


@dataclass
class Settings:
    kis: KISConfig = field(default_factory=KISConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    clickhouse: ClickHouseConfig = field(default_factory=ClickHouseConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    arbitrage: ArbitrageConfig = field(default_factory=ArbitrageConfig)  # MODE_A redesign
    trend: TrendConfig = field(default_factory=TrendConfig)  # MODE_B redesign
    consumer: ConsumerGroupConfig = field(default_factory=ConsumerGroupConfig)
    mock_prediction: MockPredictionConfig = field(default_factory=MockPredictionConfig)  # v0.0.2
    resilience: ResilienceConfig = field(default_factory=ResilienceConfig)  # v0.0.3

    # 로깅 레벨
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # 운영 모드
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"


# 전역 설정 인스턴스
settings = Settings()
