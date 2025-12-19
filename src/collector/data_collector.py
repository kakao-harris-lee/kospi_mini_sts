"""
Data Collector 모듈
API에서 수신한 원본 데이터를 RAW_DATA_STREAM에 적재
"""
import time
import json
import logging
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass, asdict
from abc import ABC, abstractmethod

import sys
from config.settings import settings
from src.common import StreamPublisher, setup_logging

logger = setup_logging("collector")


@dataclass
class TickData:
    """틱 데이터 표준 구조"""
    symbol: str
    timestamp: float
    bid_price_1: float
    bid_qty_1: float
    ask_price_1: float
    ask_qty_1: float
    # L5 호가 (옵션)
    bid_price_2: Optional[float] = None
    bid_qty_2: Optional[float] = None
    bid_price_3: Optional[float] = None
    bid_qty_3: Optional[float] = None
    bid_price_4: Optional[float] = None
    bid_qty_4: Optional[float] = None
    bid_price_5: Optional[float] = None
    bid_qty_5: Optional[float] = None
    ask_price_2: Optional[float] = None
    ask_qty_2: Optional[float] = None
    ask_price_3: Optional[float] = None
    ask_qty_3: Optional[float] = None
    ask_price_4: Optional[float] = None
    ask_qty_4: Optional[float] = None
    ask_price_5: Optional[float] = None
    ask_qty_5: Optional[float] = None
    # 추가 정보
    tick_volume: Optional[float] = None
    open_interest: Optional[float] = None  # 미결제약정
    
    def to_dict(self) -> Dict[str, Any]:
        """None이 아닌 필드만 딕셔너리로 변환"""
        return {k: v for k, v in asdict(self).items() if v is not None}


class BaseAPIAdapter(ABC):
    """
    API 어댑터 베이스 클래스
    각 거래소/데이터 제공자별로 구현
    """
    
    @abstractmethod
    def connect(self):
        """API 연결"""
        pass
    
    @abstractmethod
    def subscribe(self, symbols: list, callback: Callable[[TickData], None]):
        """
        심볼 구독 및 콜백 등록
        
        Args:
            symbols: 구독할 심볼 리스트
            callback: 틱 데이터 수신 시 호출할 함수
        """
        pass
    
    @abstractmethod
    def disconnect(self):
        """연결 해제"""
        pass


class DataCollector:
    """
    Data Collector 메인 클래스
    API 어댑터에서 받은 데이터를 Redis Stream에 적재
    """
    
    def __init__(self, api_adapter: BaseAPIAdapter):
        self.adapter = api_adapter
        self.publisher = StreamPublisher(
            settings.redis.raw_stream,
            maxlen=settings.redis.stream_maxlen
        )
        self._running = False
        self._message_count = 0
    
    def _on_tick(self, tick: TickData):
        """
        틱 데이터 수신 콜백
        Redis Stream에 발행
        """
        try:
            data = tick.to_dict()
            self.publisher.publish(data)
            self._message_count += 1
            
            if self._message_count % 1000 == 0:
                logger.info(f"Published {self._message_count} messages")
                
        except Exception as e:
            logger.error(f"Error publishing tick: {e}")
    
    def start(self, symbols: list):
        """
        Collector 시작
        
        Args:
            symbols: 수집할 심볼 리스트
        """
        logger.info(f"Starting Data Collector for {len(symbols)} symbols")
        self._running = True
        
        try:
            self.adapter.connect()
            self.adapter.subscribe(symbols, self._on_tick)
            
            # 메인 루프 (어댑터가 비동기일 경우 유지)
            while self._running:
                time.sleep(1)
                
        except KeyboardInterrupt:
            logger.info("Shutdown requested")
        finally:
            self.stop()
    
    def stop(self):
        """Collector 종료"""
        self._running = False
        self.adapter.disconnect()
        logger.info(f"Data Collector stopped. Total messages: {self._message_count}")


# ============================================================
# 예시: 업비트 WebSocket 어댑터 (실제 구현 시 참고)
# ============================================================

class UpbitWebSocketAdapter(BaseAPIAdapter):
    """
    업비트 WebSocket API 어댑터 예시
    실제 구현 시 pyupbit 또는 직접 WebSocket 연결 필요
    """
    
    def __init__(self, api_key: str = None, secret_key: str = None):
        self.api_key = api_key
        self.secret_key = secret_key
        self._ws = None
        self._callback = None
    
    def connect(self):
        """WebSocket 연결 (구현 필요)"""
        logger.info("Connecting to Upbit WebSocket...")
        # TODO: 실제 WebSocket 연결 구현
        # import websockets
        # self._ws = await websockets.connect(...)
        pass
    
    def subscribe(self, symbols: list, callback: Callable[[TickData], None]):
        """
        심볼 구독
        
        Args:
            symbols: ["KRW-BTC", "KRW-ETH", ...]
            callback: 콜백 함수
        """
        self._callback = callback
        logger.info(f"Subscribing to {symbols}")
        # TODO: 구독 메시지 전송 및 수신 루프 구현
        pass
    
    def _parse_message(self, raw_msg: dict) -> TickData:
        """업비트 메시지를 TickData로 변환"""
        # TODO: 실제 메시지 파싱 로직
        return TickData(
            symbol=raw_msg.get('code', ''),
            timestamp=time.time(),
            bid_price_1=raw_msg.get('orderbook_units', [{}])[0].get('bid_price', 0),
            bid_qty_1=raw_msg.get('orderbook_units', [{}])[0].get('bid_size', 0),
            ask_price_1=raw_msg.get('orderbook_units', [{}])[0].get('ask_price', 0),
            ask_qty_1=raw_msg.get('orderbook_units', [{}])[0].get('ask_size', 0),
        )
    
    def disconnect(self):
        """연결 해제"""
        if self._ws:
            # self._ws.close()
            pass
        logger.info("Disconnected from Upbit")


# ============================================================
# 테스트용 Mock 어댑터
# ============================================================

class MockAPIAdapter(BaseAPIAdapter):
    """테스트용 Mock 어댑터"""
    
    def __init__(self, tick_interval: float = 0.1):
        self.tick_interval = tick_interval
        self._running = False
        self._callback = None
    
    def connect(self):
        logger.info("Mock API connected")
    
    def subscribe(self, symbols: list, callback: Callable[[TickData], None]):
        self._callback = callback
        self._running = True
        
        import random
        base_prices = {s: 50000000 + random.random() * 1000000 for s in symbols}
        
        while self._running:
            for symbol in symbols:
                base = base_prices[symbol]
                tick = TickData(
                    symbol=symbol,
                    timestamp=time.time(),
                    bid_price_1=base - 100,
                    bid_qty_1=random.random() * 10,
                    ask_price_1=base + 100,
                    ask_qty_1=random.random() * 10,
                    tick_volume=random.random() * 100
                )
                self._callback(tick)
            time.sleep(self.tick_interval)
    
    def disconnect(self):
        self._running = False
        logger.info("Mock API disconnected")


if __name__ == "__main__":
    # 테스트 실행
    adapter = MockAPIAdapter(tick_interval=0.5)
    collector = DataCollector(adapter)
    collector.start(symbols=["KRW-BTC", "KRW-ETH"])
