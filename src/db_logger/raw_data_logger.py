"""
Raw Data Logger 모듈 (v0.0.2)
RAW_DATA_STREAM → ClickHouse (orderbook_snapshots, trade_ticks)

Collector가 Redis로만 발행하고, 이 Logger가 ClickHouse 저장 담당.
"""
import time
import logging
from datetime import datetime
from typing import Dict, Any, Optional

from config.settings import settings
from src.common import (
    StreamConsumer,
    StreamMessage,
    BatchInserter,
    BatchConfig,
    setup_logging,
    init_tables,
    init_metrics,
    get_metrics,
)

logger = setup_logging("raw_data_logger")


class RawDataLogger(StreamConsumer):
    """
    Raw Data Logger
    RAW_DATA_STREAM을 소비하여 ClickHouse에 배치 삽입

    - orderbook_snapshots: 호가 데이터 (L2 이상)
    - trade_ticks: 체결 데이터 (tick_volume > 0)
    """

    # 호가 테이블 컬럼
    ORDERBOOK_COLUMNS = [
        "timestamp", "symbol",
        "bid_price_1", "bid_qty_1", "bid_price_2", "bid_qty_2",
        "bid_price_3", "bid_qty_3", "bid_price_4", "bid_qty_4",
        "bid_price_5", "bid_qty_5",
        "ask_price_1", "ask_qty_1", "ask_price_2", "ask_qty_2",
        "ask_price_3", "ask_qty_3", "ask_price_4", "ask_qty_4",
        "ask_price_5", "ask_qty_5",
        "spread", "mid_price", "imbalance"
    ]

    # 체결 테이블 컬럼
    TRADE_COLUMNS = [
        "timestamp", "symbol", "price", "volume", "side",
        "bid_price", "ask_price", "open_interest", "cumulative_volume"
    ]

    def __init__(self):
        super().__init__(
            stream_name=settings.redis.raw_stream,
            group_name="raw_data_logger",
            consumer_name="logger_1"
        )

        # 호가 배치 삽입기
        self.orderbook_inserter = BatchInserter(
            BatchConfig(
                table_name="orderbook_snapshots",
                column_names=self.ORDERBOOK_COLUMNS,
                batch_size=500,
                flush_interval_sec=1.0
            )
        )

        # 체결 배치 삽입기
        self.trade_inserter = BatchInserter(
            BatchConfig(
                table_name="trade_ticks",
                column_names=self.TRADE_COLUMNS,
                batch_size=500,
                flush_interval_sec=1.0
            )
        )

        # 통계
        self._orderbook_count = 0
        self._trade_count = 0
        self._last_report = time.time()

        # 심볼별 마지막 호가 (체결 방향 추정용)
        self._last_orderbook: Dict[str, Dict[str, Any]] = {}

    def start(self):
        """로거 시작"""
        logger.info("Starting Raw Data Logger...")

        # 메트릭 서버 시작 (포트 8082)
        init_metrics("raw_data_logger", port=8082)

        # ClickHouse 테이블 초기화
        init_tables()

        # 배치 삽입기 시작
        self.orderbook_inserter.start()
        self.trade_inserter.start()

        # Consumer 루프 시작
        self.run()

    def _process_orderbook(self, data: Dict[str, Any], timestamp: float) -> bool:
        """호가 데이터 처리"""
        try:
            symbol = data.get('symbol', '')
            bid_p1 = float(data.get('bid_price_1', 0) or 0)
            ask_p1 = float(data.get('ask_price_1', 0) or 0)

            # 스프레드 & 불균형 계산
            spread = ask_p1 - bid_p1 if bid_p1 and ask_p1 else 0
            mid_price = (bid_p1 + ask_p1) / 2 if bid_p1 and ask_p1 else 0

            bid_qtys = [float(data.get(f'bid_qty_{i}', 0) or 0) for i in range(1, 4)]
            ask_qtys = [float(data.get(f'ask_qty_{i}', 0) or 0) for i in range(1, 4)]
            total_bid = sum(bid_qtys)
            total_ask = sum(ask_qtys)
            imbalance = (total_bid - total_ask) / (total_bid + total_ask) if (total_bid + total_ask) > 0 else 0

            record = {
                "timestamp": datetime.fromtimestamp(timestamp),
                "symbol": symbol,
                "bid_price_1": float(data.get('bid_price_1', 0) or 0),
                "bid_qty_1": float(data.get('bid_qty_1', 0) or 0),
                "bid_price_2": float(data.get('bid_price_2', 0) or 0),
                "bid_qty_2": float(data.get('bid_qty_2', 0) or 0),
                "bid_price_3": float(data.get('bid_price_3', 0) or 0),
                "bid_qty_3": float(data.get('bid_qty_3', 0) or 0),
                "bid_price_4": float(data.get('bid_price_4', 0) or 0),
                "bid_qty_4": float(data.get('bid_qty_4', 0) or 0),
                "bid_price_5": float(data.get('bid_price_5', 0) or 0),
                "bid_qty_5": float(data.get('bid_qty_5', 0) or 0),
                "ask_price_1": float(data.get('ask_price_1', 0) or 0),
                "ask_qty_1": float(data.get('ask_qty_1', 0) or 0),
                "ask_price_2": float(data.get('ask_price_2', 0) or 0),
                "ask_qty_2": float(data.get('ask_qty_2', 0) or 0),
                "ask_price_3": float(data.get('ask_price_3', 0) or 0),
                "ask_qty_3": float(data.get('ask_qty_3', 0) or 0),
                "ask_price_4": float(data.get('ask_price_4', 0) or 0),
                "ask_qty_4": float(data.get('ask_qty_4', 0) or 0),
                "ask_price_5": float(data.get('ask_price_5', 0) or 0),
                "ask_qty_5": float(data.get('ask_qty_5', 0) or 0),
                "spread": spread,
                "mid_price": mid_price,
                "imbalance": imbalance,
            }

            self.orderbook_inserter.add(record)
            self._orderbook_count += 1

            # 마지막 호가 저장 (체결 방향 추정용)
            self._last_orderbook[symbol] = {
                'bid_price_1': bid_p1,
                'ask_price_1': ask_p1,
            }

            return True

        except Exception as e:
            logger.error(f"Error processing orderbook: {e}")
            return False

    def _process_trade(self, data: Dict[str, Any], timestamp: float) -> bool:
        """체결 데이터 처리"""
        try:
            symbol = data.get('symbol', '')
            price = float(data.get('bid_price_1', 0) or 0)  # 현재가

            # 매수/매도 추정
            last_ob = self._last_orderbook.get(symbol)
            if last_ob:
                if last_ob['ask_price_1'] and price >= last_ob['ask_price_1']:
                    side = "BUY"
                elif last_ob['bid_price_1'] and price <= last_ob['bid_price_1']:
                    side = "SELL"
                else:
                    side = "UNKNOWN"
            else:
                side = "UNKNOWN"

            record = {
                "timestamp": datetime.fromtimestamp(timestamp),
                "symbol": symbol,
                "price": price,
                "volume": float(data.get('tick_volume', 0) or 0),
                "side": side,
                "bid_price": float(data.get('bid_price_1', 0) or 0),
                "ask_price": float(data.get('ask_price_1', 0) or 0),
                "open_interest": float(data.get('open_interest', 0) or 0),
                "cumulative_volume": 0,
            }

            self.trade_inserter.add(record)
            self._trade_count += 1

            return True

        except Exception as e:
            logger.error(f"Error processing trade: {e}")
            return False

    def process_message(self, message: StreamMessage) -> bool:
        """
        Raw 메시지 처리 및 ClickHouse 배치 큐에 추가
        """
        try:
            data = message.data

            # data 필드가 중첩된 경우 처리
            if 'data' in data and isinstance(data['data'], dict):
                data = data['data']

            timestamp = float(data.get('timestamp', time.time()))

            # 호가 데이터 (L2 이상이 있으면)
            if data.get('bid_price_2') is not None or data.get('ask_price_2') is not None:
                self._process_orderbook(data, timestamp)

            # 체결 데이터 (tick_volume이 있으면)
            tick_vol = data.get('tick_volume')
            if tick_vol is not None and float(tick_vol) > 0:
                self._process_trade(data, timestamp)

            # 주기적 리포트
            now = time.time()
            if now - self._last_report > 60:
                logger.info(
                    f"Raw Data Logger Stats: "
                    f"orderbook={self._orderbook_count}, "
                    f"trades={self._trade_count}"
                )
                self._last_report = now

                # 메트릭 기록
                metrics = get_metrics()
                metrics.record_clickhouse_insert("orderbook_snapshots", self._orderbook_count)
                metrics.record_clickhouse_insert("trade_ticks", self._trade_count)

            return True

        except Exception as e:
            logger.error(f"Error processing message {message.id}: {e}")
            return False

    def stop(self):
        """로거 종료"""
        super().stop()
        self.orderbook_inserter.stop()
        self.trade_inserter.stop()

        logger.info(
            f"Raw Data Logger stopped. "
            f"Total: orderbook={self._orderbook_count}, trades={self._trade_count}"
        )


def main():
    """Raw Data Logger 실행"""
    raw_logger = RawDataLogger()

    try:
        raw_logger.start()
    except KeyboardInterrupt:
        raw_logger.stop()


if __name__ == "__main__":
    main()
