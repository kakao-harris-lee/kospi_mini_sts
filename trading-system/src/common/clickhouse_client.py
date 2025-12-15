"""
ClickHouse 클라이언트 및 배치 처리 유틸리티
"""
import logging
import time
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from threading import Thread, Lock
from queue import Queue, Empty

try:
    from clickhouse_connect import get_client
    from clickhouse_connect.driver import Client
except ImportError:
    raise ImportError("clickhouse-connect 패키지가 필요합니다: pip install clickhouse-connect")

import sys
sys.path.insert(0, '/Users/harris/Development/trading-system')
from config.settings import settings

logger = logging.getLogger(__name__)


class ClickHouseClient:
    """ClickHouse 연결 관리 싱글톤"""
    _instance: Optional[Client] = None
    
    @classmethod
    def get_client(cls) -> Client:
        if cls._instance is None:
            cfg = settings.clickhouse
            cls._instance = get_client(
                host=cfg.host,
                port=cfg.port,
                database=cfg.database,
                username=cfg.user,
                password=cfg.password
            )
            logger.info(f"ClickHouse 연결 성공: {cfg.host}:{cfg.port}/{cfg.database}")
        return cls._instance
    
    @classmethod
    def close(cls):
        if cls._instance:
            cls._instance.close()
            cls._instance = None


@dataclass
class BatchConfig:
    """배치 삽입 설정"""
    table_name: str
    column_names: List[str]
    batch_size: int = 1000
    flush_interval_sec: float = 1.0


class BatchInserter:
    """
    ClickHouse 배치 삽입기
    - 일정 크기(batch_size)가 되거나
    - 일정 시간(flush_interval)이 지나면 자동으로 삽입
    """
    
    def __init__(self, config: BatchConfig):
        self.config = config
        self.client = ClickHouseClient.get_client()
        self._buffer: List[List[Any]] = []
        self._lock = Lock()
        self._last_flush = time.time()
        self._running = False
        self._flush_thread: Optional[Thread] = None
    
    def start(self):
        """백그라운드 플러시 스레드 시작"""
        self._running = True
        self._flush_thread = Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()
        logger.info(f"BatchInserter 시작: {self.config.table_name}")
    
    def stop(self):
        """종료 및 남은 데이터 플러시"""
        self._running = False
        if self._flush_thread:
            self._flush_thread.join(timeout=5)
        self._flush()  # 남은 데이터 강제 플러시
        logger.info(f"BatchInserter 종료: {self.config.table_name}")
    
    def add(self, record: Dict[str, Any]):
        """레코드 추가 (컬럼 순서대로 변환)"""
        row = [record.get(col) for col in self.config.column_names]
        with self._lock:
            self._buffer.append(row)
            if len(self._buffer) >= self.config.batch_size:
                self._flush()
    
    def add_many(self, records: List[Dict[str, Any]]):
        """여러 레코드 한번에 추가"""
        for record in records:
            self.add(record)
    
    def _flush(self):
        """버퍼의 데이터를 ClickHouse에 삽입"""
        with self._lock:
            if not self._buffer:
                return
            
            data_to_insert = self._buffer.copy()
            self._buffer.clear()
            self._last_flush = time.time()
        
        try:
            self.client.insert(
                self.config.table_name,
                data_to_insert,
                column_names=self.config.column_names
            )
            logger.debug(f"ClickHouse 삽입 완료: {len(data_to_insert)}건 -> {self.config.table_name}")
        except Exception as e:
            logger.error(f"ClickHouse 삽입 실패: {e}")
            # 실패한 데이터 복구 (선택적)
            with self._lock:
                self._buffer = data_to_insert + self._buffer
    
    def _flush_loop(self):
        """주기적 플러시 루프"""
        while self._running:
            time.sleep(0.1)  # 100ms 체크 주기
            elapsed = time.time() - self._last_flush
            if elapsed >= self.config.flush_interval_sec:
                with self._lock:
                    if self._buffer:
                        self._flush()


# ClickHouse 테이블 DDL 템플릿
TABLE_SCHEMAS = {
    "raw_ticks": """
        CREATE TABLE IF NOT EXISTS raw_ticks (
            timestamp DateTime64(3),
            symbol LowCardinality(String),
            bid_price_1 Float64,
            bid_qty_1 Float64,
            ask_price_1 Float64,
            ask_qty_1 Float64,
            tick_volume Float64,
            data_json String
        ) ENGINE = MergeTree()
        PARTITION BY toYYYYMMDD(timestamp)
        ORDER BY (symbol, timestamp)
    """,
    
    "features_1min": """
        CREATE TABLE IF NOT EXISTS features_1min (
            timestamp DateTime64(3),
            symbol LowCardinality(String),
            ofi_z_score Float64,
            liquidity_score Float64,
            basis_gap Float64,
            rsi_1m Float64,
            volume_1m Float64,
            price_change_1m Float64
        ) ENGINE = MergeTree()
        PARTITION BY toYYYYMMDD(timestamp)
        ORDER BY (symbol, timestamp)
    """,
    
    "predictions": """
        CREATE TABLE IF NOT EXISTS predictions (
            timestamp DateTime64(3),
            symbol LowCardinality(String),
            up_prob Float64,
            down_prob Float64,
            model_version LowCardinality(String),
            decision_time Float64
        ) ENGINE = MergeTree()
        PARTITION BY toYYYYMMDD(timestamp)
        ORDER BY (symbol, timestamp)
    """,
    
    "orders": """
        CREATE TABLE IF NOT EXISTS orders (
            timestamp DateTime64(3),
            symbol LowCardinality(String),
            strategy_id LowCardinality(String),
            order_type LowCardinality(String),
            side LowCardinality(String),
            size Float64,
            price Float64,
            status LowCardinality(String)
        ) ENGINE = MergeTree()
        PARTITION BY toYYYYMMDD(timestamp)
        ORDER BY (symbol, timestamp)
    """
}


def init_tables():
    """모든 테이블 초기화"""
    client = ClickHouseClient.get_client()
    for table_name, ddl in TABLE_SCHEMAS.items():
        try:
            client.command(ddl)
            logger.info(f"테이블 생성/확인: {table_name}")
        except Exception as e:
            logger.error(f"테이블 생성 실패 {table_name}: {e}")
