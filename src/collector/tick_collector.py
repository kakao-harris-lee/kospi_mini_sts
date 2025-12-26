"""
틱/호가 데이터 수집기 (Phase 8.2)

WebSocket으로 실시간 틱 데이터를 수집하여 ClickHouse에 저장
- 호가 데이터 → orderbook_snapshots 테이블
- 체결 데이터 → trade_ticks 테이블

사용법:
    python -m src.collector.tick_collector

API 제한:
    - WebSocket은 초당 호출 제한 없음 (REST API만 20건/초 제한)
    - 예상 데이터량: ~25,000 orderbook/day, ~10,000-50,000 ticks/day
"""
import os
import sys
import time
import asyncio
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from threading import Thread

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config.settings import settings
from src.common import setup_logging, init_metrics, get_metrics
from src.collector.kis_websocket import KISWebSocketAdapter, KISConfig, KISMarket
from src.collector.data_collector import TickData

logger = setup_logging("tick_collector")


@dataclass
class TickCollectorConfig:
    """틱 수집기 설정"""
    symbols: List[str]
    orderbook_batch_size: int = 500
    trade_batch_size: int = 500
    flush_interval_sec: float = 1.0
    publish_to_redis: bool = True  # Redis Stream에도 발행 여부


class TickDataCollector:
    """
    틱/호가 데이터 수집기 (v0.0.2 - Lightened)

    WebSocket → Redis Stream (fire-and-forget)
    ClickHouse 저장은 별도 RawDataLogger가 담당
    """

    def __init__(self, config: TickCollectorConfig, kis_config: KISConfig):
        self.config = config
        self.adapter = KISWebSocketAdapter(kis_config)

        # Redis Publisher (필수)
        from src.common import StreamPublisher
        self._redis_publisher = StreamPublisher(
            settings.redis.raw_stream,
            maxlen=settings.redis.stream_maxlen
        )

        # 통계
        self._orderbook_count = 0
        self._trade_count = 0
        self._last_report = time.time()
        self._running = False
        self._last_orderbook: Dict[str, TickData] = {}  # 심볼별 마지막 호가

    def _on_orderbook(self, tick: TickData):
        """
        호가 데이터 수신 콜백 → Redis Stream 발행
        """
        try:
            self._orderbook_count += 1
            self._last_orderbook[tick.symbol] = tick

            # 메트릭 기록
            metrics = get_metrics()
            metrics.record_tick(tick.symbol, "orderbook")
            metrics.record_orderbook_update(tick.symbol)

            # Redis 발행 (fire-and-forget)
            self._redis_publisher.publish(tick.to_dict())

        except Exception as e:
            logger.error(f"Error processing orderbook: {e}")

    def _on_trade(self, tick: TickData):
        """
        체결 데이터 수신 콜백 → 메트릭만 기록
        (체결 데이터는 호가와 함께 Redis로 이미 발행됨)
        """
        try:
            # 매수/매도 추정 (체결가와 호가 비교)
            last_ob = self._last_orderbook.get(tick.symbol)
            if last_ob and tick.bid_price_1:
                price = tick.bid_price_1
                if last_ob.ask_price_1 and price >= last_ob.ask_price_1:
                    side = "BUY"
                elif last_ob.bid_price_1 and price <= last_ob.bid_price_1:
                    side = "SELL"
                else:
                    side = "UNKNOWN"
            else:
                side = "UNKNOWN"

            self._trade_count += 1

            # 메트릭 기록
            metrics = get_metrics()
            metrics.record_tick(tick.symbol, "trade")
            metrics.record_trade_tick(tick.symbol, side)

        except Exception as e:
            logger.error(f"Error processing trade: {e}")

    def _on_tick(self, tick: TickData):
        """
        틱 데이터 분류 및 처리

        호가 데이터: bid_price_2 등 L2 이상 데이터가 있음
        체결 데이터: tick_volume이 있음
        """
        # 호가 데이터 (L2 이상이 있으면)
        if tick.bid_price_2 is not None or tick.ask_price_2 is not None:
            self._on_orderbook(tick)

        # 체결 데이터
        if tick.tick_volume is not None and tick.tick_volume > 0:
            self._on_trade(tick)

        # 주기적 리포트
        now = time.time()
        if now - self._last_report > 60:
            logger.info(
                f"Tick Collector Stats: "
                f"orderbook={self._orderbook_count}, "
                f"trades={self._trade_count}"
            )
            self._last_report = now

    def start(self):
        """수집기 시작 (v0.0.2 - Redis only)"""
        logger.info(f"Starting Tick Collector (lightweight) for symbols: {self.config.symbols}")

        # 메트릭 서버 시작
        init_metrics("tick_collector", port=8080)
        metrics = get_metrics()
        metrics.set_websocket_status("kis_websocket", False)

        self._running = True

        try:
            # WebSocket 연결 및 구독
            self.adapter.connect()
            metrics.set_websocket_status("kis_websocket", True)
            self.adapter.subscribe(self.config.symbols, self._on_tick)

        except KeyboardInterrupt:
            logger.info("Shutdown requested")
        finally:
            self.stop()

    def stop(self):
        """수집기 종료"""
        self._running = False
        self.adapter.disconnect()

        logger.info(
            f"Tick Collector stopped. "
            f"Total: orderbook={self._orderbook_count}, trades={self._trade_count}"
        )


def get_current_futures_code() -> str:
    """
    현재 근월물 선물 코드 반환

    월물 코드: F(1), G(2), H(3), J(4), K(5), M(6), N(7), Q(8), U(9), V(10), X(11), Z(12)
    """
    month_codes = {
        1: 'F', 2: 'G', 3: 'H', 4: 'J', 5: 'K', 6: 'M',
        7: 'N', 8: 'Q', 9: 'U', 10: 'V', 11: 'X', 12: 'Z'
    }

    now = datetime.now()
    year = now.year % 100
    month = now.month

    # 간단한 근월물 계산 (실제로는 만기일 고려 필요)
    # 매월 둘째 주 목요일이 만기
    if now.day > 15:  # 대략 15일 이후면 다음 월물
        month += 1
        if month > 12:
            month = 1
            year += 1

    month_code = month_codes[month]
    return f"101{month_code}{year:02d}"


def main():
    """틱 수집기 실행"""
    from dotenv import load_dotenv
    load_dotenv()

    # 환경변수에서 설정 로드
    app_key = os.getenv("KIS_APP_KEY", "")
    app_secret = os.getenv("KIS_APP_SECRET", "")
    market_str = os.getenv("KIS_MARKET", "real")

    if not app_key or not app_secret:
        logger.error("KIS_APP_KEY and KIS_APP_SECRET must be set")
        sys.exit(1)

    # 현재 근월물 코드
    futures_code = os.getenv("FUTURES_CODE", get_current_futures_code())

    logger.info(f"Tick Collector Configuration:")
    logger.info(f"  Futures Code: {futures_code}")
    logger.info(f"  Market: {market_str}")

    kis_config = KISConfig(
        app_key=app_key,
        app_secret=app_secret,
        market=KISMarket.REAL if market_str == "real" else KISMarket.MOCK
    )

    collector_config = TickCollectorConfig(
        symbols=[futures_code],
        orderbook_batch_size=500,
        trade_batch_size=500,
        publish_to_redis=True
    )

    collector = TickDataCollector(collector_config, kis_config)

    try:
        collector.start()
    except KeyboardInterrupt:
        collector.stop()


if __name__ == "__main__":
    main()
