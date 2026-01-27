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
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from threading import Thread

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config.settings import settings
from src.common import setup_logging, init_metrics, get_metrics, ClickHouseClient, RedisClient
from src.collector.kis_websocket import KISWebSocketAdapter, KISConfig, KISMarket
from src.collector.data_collector import TickData
from src.common.futures_code import get_front_month_code, get_kis_code, get_kospi200f_code, get_table_for_symbol

logger = setup_logging("tick_collector")


@dataclass
class TickCollectorConfig:
    """틱 수집기 설정"""
    symbols: List[str]
    orderbook_batch_size: int = 500
    trade_batch_size: int = 500
    flush_interval_sec: float = 1.0
    publish_to_redis: bool = True  # Redis Stream에도 발행 여부
    persist_candles: bool = True   # ClickHouse에 1분봉 저장 여부


@dataclass
class CandleState:
    """심볼별 캔들 상태"""
    open_price: Optional[float] = None
    high_price: float = 0.0
    low_price: float = float('inf')
    close_price: float = 0.0
    volume: int = 0
    tick_count: int = 0
    candle_start_minute: Optional[int] = None

    def reset(self):
        self.open_price = None
        self.high_price = 0.0
        self.low_price = float('inf')
        self.close_price = 0.0
        self.volume = 0
        self.tick_count = 0
        self.candle_start_minute = None


class TickDataCollector:
    """
    틱/호가 데이터 수집기 (v0.0.3 - Candle Persistence)

    WebSocket → Redis Stream (fire-and-forget)
    실시간 1분봉 → ClickHouse (kospi_mini_1m / kospi200f_1m)
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

        # ClickHouse client for candle persistence
        self._db_client = None
        if config.persist_candles:
            self._db_client = ClickHouseClient.get_client()
            self._ensure_tables()

        # 심볼별 캔들 상태
        self._candle_states: Dict[str, CandleState] = {s: CandleState() for s in config.symbols}

        # 통계
        self._orderbook_count = 0
        self._trade_count = 0
        self._candle_count = 0
        self._last_report = time.time()
        self._running = False
        self._last_orderbook: Dict[str, TickData] = {}  # 심볼별 마지막 호가

        # Heartbeat for health monitoring
        self._last_heartbeat = 0.0
        self._redis_client = RedisClient.get_client()

    def _ensure_tables(self):
        """ClickHouse 테이블 생성 확인"""
        if not self._db_client:
            return

        for symbol in self.config.symbols:
            table_name = get_table_for_symbol(symbol)
            try:
                self._db_client.command(f"""
                    CREATE TABLE IF NOT EXISTS {settings.clickhouse.database}.{table_name} (
                        code String,
                        datetime DateTime,
                        open Float64,
                        high Float64,
                        low Float64,
                        close Float64,
                        volume UInt64
                    ) ENGINE = ReplacingMergeTree()
                    ORDER BY (code, datetime)
                """)
                logger.info(f"Ensured table: {table_name}")
            except Exception as e:
                logger.error(f"Failed to ensure table {table_name}: {e}")

    def _publish_heartbeat(self):
        """Publish heartbeat to Redis for health monitoring."""
        now = time.time()
        if now - self._last_heartbeat < 30.0:  # Every 30 seconds
            return

        try:
            self._redis_client.set("heartbeat:tick_collector", str(now), ex=120)
            self._last_heartbeat = now
            logger.debug("Heartbeat published: tick_collector")
        except Exception as e:
            logger.warning(f"Failed to publish heartbeat: {e}")

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
        체결 데이터 수신 콜백 → Redis 발행 + 캔들 집계 + 메트릭 기록
        OHLC 데이터 (current_price, open_price, high_price, low_price) 포함
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

            # Redis 발행 (OHLC 데이터 포함)
            self._redis_publisher.publish(tick.to_dict())

            # 1분봉 캔들 집계
            self._update_candle(tick)

        except Exception as e:
            logger.error(f"Error processing trade: {e}")

    def _update_candle(self, tick: TickData):
        """체결 데이터로 1분봉 캔들 업데이트"""
        if not self.config.persist_candles:
            return

        symbol = tick.symbol
        price = tick.current_price or tick.bid_price_1
        volume = int(tick.tick_volume or 0)

        if not price or price <= 0:
            return

        # 심볼별 캔들 상태 가져오기
        if symbol not in self._candle_states:
            self._candle_states[symbol] = CandleState()

        state = self._candle_states[symbol]
        now = datetime.now(timezone.utc)
        current_minute = now.hour * 60 + now.minute

        # 새 캔들 시작 또는 분 변경 시 기존 캔들 저장
        if state.candle_start_minute is not None and state.candle_start_minute != current_minute:
            self._flush_candle(symbol, state)

        # 새 캔들 시작
        if state.open_price is None:
            state.candle_start_minute = current_minute
            state.open_price = price
            state.high_price = price
            state.low_price = price

        # OHLCV 업데이트
        state.high_price = max(state.high_price, price)
        state.low_price = min(state.low_price, price)
        state.close_price = price
        state.volume += volume
        state.tick_count += 1

    def _flush_candle(self, symbol: str, state: CandleState):
        """완성된 1분봉을 ClickHouse에 저장"""
        if not self._db_client or state.open_price is None:
            return

        try:
            # 캔들 시작 시간 계산
            now = datetime.now(timezone.utc)
            candle_dt = now.replace(
                hour=state.candle_start_minute // 60,
                minute=state.candle_start_minute % 60,
                second=0,
                microsecond=0
            )

            table_name = get_table_for_symbol(symbol)
            dt_str = candle_dt.strftime('%Y-%m-%d %H:%M:%S')

            sql = f"""
                INSERT INTO {settings.clickhouse.database}.{table_name}
                (code, datetime, open, high, low, close, volume)
                VALUES
                ('{symbol}', '{dt_str}', {state.open_price}, {state.high_price},
                 {state.low_price}, {state.close_price}, {state.volume})
            """

            self._db_client.command(sql)
            self._candle_count += 1

            logger.debug(
                f"Candle saved: {symbol} {candle_dt} "
                f"O={state.open_price:.2f} H={state.high_price:.2f} "
                f"L={state.low_price:.2f} C={state.close_price:.2f} V={state.volume}"
            )

        except Exception as e:
            logger.error(f"Failed to save candle for {symbol}: {e}")

        finally:
            state.reset()

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

        # Publish heartbeat for health monitoring
        self._publish_heartbeat()

        # 주기적 리포트
        now = time.time()
        if now - self._last_report > 60:
            logger.info(
                f"Tick Collector Stats: "
                f"orderbook={self._orderbook_count}, "
                f"trades={self._trade_count}, "
                f"candles={self._candle_count}"
            )
            self._last_report = now

    def start(self):
        """수집기 시작 (v0.0.3 - Candle Persistence)"""
        logger.info(f"Starting Tick Collector for symbols: {self.config.symbols}")

        # 메트릭 서버 시작
        init_metrics("tick_collector", port=8080)
        metrics = get_metrics()
        metrics.set_websocket_status("kis_websocket", False)

        self._running = True

        # Publish initial heartbeat
        self._publish_heartbeat()

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

        # 남은 캔들 플러시
        for symbol, state in self._candle_states.items():
            if state.open_price is not None:
                self._flush_candle(symbol, state)

        logger.info(
            f"Tick Collector stopped. "
            f"Total: orderbook={self._orderbook_count}, "
            f"trades={self._trade_count}, candles={self._candle_count}"
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

    now = datetime.now(timezone.utc)
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

    # 수집할 심볼 목록 결정
    # 환경변수 FUTURES_CODES로 쉼표 구분 코드 지정 가능
    # 기본값: KOSPI Mini 차월물(A05602) + KOSPI 200 근월물(101S6000)
    futures_codes_str = os.getenv("FUTURES_CODES", "")
    if futures_codes_str:
        symbols = [s.strip() for s in futures_codes_str.split(",") if s.strip()]
    else:
        # 단일 코드 지정 (레거시 호환)
        single_code = os.getenv("FUTURES_CODE")
        if single_code:
            symbols = [single_code]
        else:
            # 기본값: Mini 차월물(백테스트용) + KOSPI200 근월물(학습용)
            symbols = [
                get_kis_code("back"),       # A05602 (Mini, for backtesting)
                get_kospi200f_code("front") # 101S6000 (Full, for CNN-LSTM training)
            ]

    # 캔들 저장 여부
    persist_candles = os.getenv("PERSIST_CANDLES", "true").lower() == "true"

    logger.info(f"Tick Collector Configuration:")
    logger.info(f"  Symbols: {symbols}")
    logger.info(f"  Market: {market_str}")
    logger.info(f"  Persist Candles: {persist_candles}")

    kis_config = KISConfig(
        app_key=app_key,
        app_secret=app_secret,
        market=KISMarket.REAL if market_str == "real" else KISMarket.MOCK
    )

    collector_config = TickCollectorConfig(
        symbols=symbols,
        orderbook_batch_size=500,
        trade_batch_size=500,
        publish_to_redis=True,
        persist_candles=persist_candles
    )

    collector = TickDataCollector(collector_config, kis_config)

    try:
        collector.start()
    except KeyboardInterrupt:
        collector.stop()


if __name__ == "__main__":
    main()
