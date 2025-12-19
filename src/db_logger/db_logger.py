"""
DB Logger 모듈
FEATURE_STREAM → ClickHouse (배치 삽입)
"""
import time
import logging
from typing import Dict, Any, List

import sys
from config.settings import settings
from src.common import (
    StreamConsumer, 
    StreamMessage, 
    BatchInserter, 
    BatchConfig,
    setup_logging,
    init_tables
)

logger = setup_logging("db_logger")


class DBLogger(StreamConsumer):
    """
    DB Logger 메인 클래스
    FEATURE_STREAM을 소비하여 ClickHouse에 배치 삽입
    """
    
    def __init__(self):
        super().__init__(
            stream_name=settings.redis.feature_stream,
            group_name=settings.consumer.logger_group,
            consumer_name="logger_1"
        )
        
        # 배치 삽입기 설정
        self.feature_inserter = BatchInserter(
            BatchConfig(
                table_name="features_1min",
                column_names=[
                    "timestamp", "symbol", "ofi_z_score", 
                    "liquidity_score", "basis_gap", "rsi_1m",
                    "volume_1m", "price_change_1m"
                ],
                batch_size=settings.clickhouse.batch_size,
                flush_interval_sec=settings.clickhouse.batch_timeout_sec
            )
        )
        
        self._message_count = 0
        self._last_report = time.time()
    
    def start(self):
        """로거 시작"""
        # ClickHouse 테이블 초기화
        init_tables()
        
        # 배치 삽입기 시작
        self.feature_inserter.start()
        
        # Consumer 루프 시작
        self.run()
    
    def process_message(self, message: StreamMessage) -> bool:
        """
        Feature 메시지 처리 및 ClickHouse 배치 큐에 추가
        """
        try:
            data = message.data
            
            # ClickHouse 레코드 구성
            record = {
                'timestamp': data.get('timestamp', time.time()),
                'symbol': data.get('symbol', ''),
                'ofi_z_score': float(data.get('ofi_z_score', 0)),
                'liquidity_score': float(data.get('liquidity_score', 0)),
                'basis_gap': float(data.get('basis_gap', 0)),
                'rsi_1m': float(data.get('rsi_1m', 0)),
                'volume_1m': float(data.get('volume_1m', 0)),
                'price_change_1m': float(data.get('price_change_1m', 0))
            }
            
            # 배치 큐에 추가
            self.feature_inserter.add(record)
            
            self._message_count += 1
            
            # 주기적 리포트
            if time.time() - self._last_report > 60:
                logger.info(f"DB Logger: {self._message_count} messages processed")
                self._last_report = time.time()
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing message {message.id}: {e}")
            return False
    
    def stop(self):
        """로거 종료"""
        super().stop()
        self.feature_inserter.stop()
        logger.info(f"DB Logger stopped. Total: {self._message_count} messages")


def main():
    """DB Logger 실행"""
    logger.info("Starting DB Logger...")
    db_logger = DBLogger()
    
    try:
        db_logger.start()
    except KeyboardInterrupt:
        db_logger.stop()


if __name__ == "__main__":
    main()
