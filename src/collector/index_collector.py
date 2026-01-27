"""
KOSPI200 Index Collector

Subscribes to KOSPI200 index real-time data via KIS WebSocket.
Publishes index values to INDEX_STREAM for model training/backtests.
"""

import json
import time
import asyncio
import logging
from typing import Callable, Optional
from dataclasses import dataclass

import websockets
import requests

from config.settings import settings
from src.common import StreamPublisher, setup_logging

logger = setup_logging("index_collector")


@dataclass
class IndexData:
    """KOSPI200 Index data point"""
    symbol: str           # Index code (e.g., "0001" for KOSPI200)
    timestamp: float
    value: float          # Current index value
    change: float         # Change from previous close
    change_pct: float     # Change percentage
    open_value: float     # Today's open
    high_value: float     # Today's high
    low_value: float      # Today's low

    def to_dict(self) -> dict:
        return {
            'symbol': self.symbol,
            'timestamp': self.timestamp,
            'value': self.value,
            'change': self.change,
            'change_pct': self.change_pct,
            'open_value': self.open_value,
            'high_value': self.high_value,
            'low_value': self.low_value,
        }


class IndexCollector:
    """
    KOSPI200 Index Real-time Collector

    Subscribes to KIS WebSocket for index data and publishes to Redis.
    Used by model training/backtests that require index streams.
    """

    # KIS WebSocket tr_id for index
    TR_INDEX_PRICE = "H0UPCNT0"  # 업종 현재가 체결

    # KOSPI200 index code
    KOSPI200_CODE = "0001"

    def __init__(
        self,
        app_key: str = None,
        app_secret: str = None,
        is_mock: bool = False,
        index_symbol: str = None,
        stream_name: str = None,
    ):
        """
        Args:
            app_key: KIS API app key (default from settings)
            app_secret: KIS API app secret (default from settings)
            is_mock: Whether to use mock server
            index_symbol: Index symbol to subscribe (default: KOSPI200)
            stream_name: Redis stream to publish (default from IndexConfig)
        """
        self.app_key = app_key or settings.kis.app_key
        self.app_secret = app_secret or settings.kis.app_secret
        self.is_mock = is_mock or settings.kis.is_mock

        self.index_symbol = index_symbol or settings.index.index_symbol
        self.stream_name = stream_name or settings.index.index_stream

        self._ws = None
        self._approval_key: Optional[str] = None
        self._running = False
        self._callback: Optional[Callable[[IndexData], None]] = None

        # Publisher for Redis stream
        self._publisher = StreamPublisher(self.stream_name)

        # Latest data cache
        self._latest_data: Optional[IndexData] = None

        # Statistics
        self._message_count = 0
        self._last_update_time = 0.0

    @property
    def ws_url(self) -> str:
        """WebSocket URL based on market type"""
        if self.is_mock:
            return "ws://ops.koreainvestment.com:31000"
        return "ws://ops.koreainvestment.com:21000"

    @property
    def api_base(self) -> str:
        """REST API Base URL"""
        return "https://openapi.koreainvestment.com:9443"

    def _get_approval_key(self) -> str:
        """Get WebSocket approval key"""
        url = f"{self.api_base}/oauth2/Approval"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret
        }

        try:
            resp = requests.post(url, headers=headers, json=body, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            approval_key = data.get("approval_key")
            if not approval_key:
                raise ValueError(f"approval_key not found: {data}")

            logger.info("Index collector approval_key obtained")
            return approval_key

        except Exception as e:
            logger.error(f"Failed to get approval_key: {e}")
            raise

    def _build_subscribe_message(self, tr_id: str, tr_key: str, tr_type: str = "1") -> str:
        """Build subscription message"""
        message = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": tr_type,
                "content-type": "utf-8"
            },
            "body": {
                "input": {
                    "tr_id": tr_id,
                    "tr_key": tr_key
                }
            }
        }
        return json.dumps(message)

    def _parse_index_data(self, data: str) -> Optional[IndexData]:
        """
        Parse index price data from WebSocket message.

        업종 현재가 체결 (H0UPCNT0) 형식:
        0: 업종코드
        1: 현재가 시간
        2: 업종지수
        3: 전일대비부호
        4: 전일대비
        5: 등락률
        6: 시가
        7: 고가
        8: 저가
        ... (추가 필드)
        """
        try:
            fields = data.split('|')
            if len(fields) < 9:
                logger.debug(f"Insufficient fields: {len(fields)}")
                return None

            symbol = fields[0]

            # Parse numeric values safely
            def safe_float(val: str) -> float:
                try:
                    return float(val) if val else 0.0
                except ValueError:
                    return 0.0

            index_data = IndexData(
                symbol=symbol,
                timestamp=time.time(),
                value=safe_float(fields[2]),
                change=safe_float(fields[4]),
                change_pct=safe_float(fields[5]),
                open_value=safe_float(fields[6]),
                high_value=safe_float(fields[7]),
                low_value=safe_float(fields[8]),
            )

            return index_data

        except Exception as e:
            logger.error(f"Failed to parse index data: {e}")
            return None

    def _process_message(self, message: str):
        """Process incoming WebSocket message"""
        try:
            # JSON response (subscription confirmation, etc.)
            if message.startswith('{'):
                data = json.loads(message)
                if "header" in data:
                    header = data["header"]
                    tr_id = header.get("tr_id", "")

                    if tr_id == "PINGPONG":
                        logger.debug("Received PINGPONG")
                        return

                    msg_cd = header.get("msg_cd", "")
                    if msg_cd:
                        logger.info(f"Response: {msg_cd} - {data.get('body', {}).get('msg1', '')}")
                return

            # Pipe-delimited real-time data
            # Format: encrypted|tr_id|count|data
            parts = message.split('|', 3)
            if len(parts) < 4:
                return

            tr_id = parts[1]
            raw_data = parts[3]

            if tr_id != self.TR_INDEX_PRICE:
                return

            # Parse index data
            index_data = self._parse_index_data(raw_data)
            if not index_data:
                return

            # Only process our target index
            if index_data.symbol != self.index_symbol:
                return

            self._latest_data = index_data
            self._message_count += 1
            self._last_update_time = time.time()

            # Publish to Redis stream
            self._publisher.publish(index_data.to_dict())

            # Call registered callback
            if self._callback:
                self._callback(index_data)

            # Periodic logging
            if self._message_count % 100 == 0:
                logger.info(
                    f"Index update #{self._message_count}: "
                    f"KOSPI200={index_data.value:.2f} ({index_data.change_pct:+.2f}%)"
                )

        except Exception as e:
            logger.error(f"Error processing message: {e}")

    async def _connect_and_subscribe(self):
        """Connect to WebSocket and subscribe to index"""
        try:
            logger.info(f"Connecting to {self.ws_url}")

            async with websockets.connect(
                self.ws_url,
                ping_interval=30,
                ping_timeout=10
            ) as ws:
                self._ws = ws
                logger.info("WebSocket connected")

                # Subscribe to index
                msg = self._build_subscribe_message(self.TR_INDEX_PRICE, self.index_symbol)
                await ws.send(msg)
                logger.info(f"Subscribed to index {self.index_symbol}")

                # Message receive loop
                while self._running:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=60)
                        self._process_message(message)
                    except asyncio.TimeoutError:
                        await ws.send("PING")
                        logger.debug("Sent PING")
                    except websockets.ConnectionClosed:
                        logger.warning("WebSocket connection closed")
                        break

        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            raise

    def connect(self):
        """Initialize connection (get approval key)"""
        self._approval_key = self._get_approval_key()
        logger.info("Index collector ready")

    def start(self, callback: Optional[Callable[[IndexData], None]] = None):
        """
        Start collecting index data.

        Args:
            callback: Optional callback for each index update
        """
        self._callback = callback
        self._running = True

        # Run event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._connect_and_subscribe())
        except KeyboardInterrupt:
            pass
        finally:
            loop.close()

    async def start_async(self, callback: Optional[Callable[[IndexData], None]] = None):
        """Async version of start for integration with existing event loops."""
        self._callback = callback
        self._running = True
        await self._connect_and_subscribe()

    def stop(self):
        """Stop collecting"""
        self._running = False
        logger.info("Index collector stopped")

    def get_latest(self) -> Optional[IndexData]:
        """Get most recent index data"""
        return self._latest_data

    def get_latest_value(self) -> Optional[float]:
        """Get most recent index value"""
        return self._latest_data.value if self._latest_data else None

    def get_stats(self) -> dict:
        """Get collector statistics"""
        return {
            'message_count': self._message_count,
            'last_update_time': self._last_update_time,
            'latest_value': self._latest_data.value if self._latest_data else None,
            'index_symbol': self.index_symbol,
            'stream_name': self.stream_name,
            'is_running': self._running,
        }


class MockIndexCollector:
    """
    Mock index collector for testing without KIS API.

    Simulates index movements based on a base value.
    """

    def __init__(self, base_value: float = 350.0, volatility: float = 0.001):
        """
        Args:
            base_value: Starting index value
            volatility: Per-tick volatility (std dev as fraction)
        """
        self.base_value = base_value
        self.volatility = volatility
        self._current_value = base_value
        self._running = False
        self._callback: Optional[Callable[[IndexData], None]] = None

    def start(self, callback: Optional[Callable[[IndexData], None]] = None):
        """Start generating mock index data"""
        import random

        self._callback = callback
        self._running = True

        while self._running:
            # Random walk
            change = random.gauss(0, self.base_value * self.volatility)
            self._current_value += change

            data = IndexData(
                symbol="0001",
                timestamp=time.time(),
                value=self._current_value,
                change=self._current_value - self.base_value,
                change_pct=(self._current_value / self.base_value - 1) * 100,
                open_value=self.base_value,
                high_value=max(self.base_value, self._current_value),
                low_value=min(self.base_value, self._current_value),
            )

            if self._callback:
                self._callback(data)

            time.sleep(1)  # 1 second intervals

    def stop(self):
        """Stop generating data"""
        self._running = False

    def get_latest_value(self) -> float:
        """Get current mock value"""
        return self._current_value


def create_index_collector_from_env() -> IndexCollector:
    """Create IndexCollector from environment variables"""
    return IndexCollector(
        app_key=settings.kis.app_key,
        app_secret=settings.kis.app_secret,
        is_mock=settings.kis.is_mock,
        index_symbol=settings.index.index_symbol,
        stream_name=settings.index.index_stream,
    )


if __name__ == "__main__":
    # Test execution
    print("Testing Index Collector...")

    collector = create_index_collector_from_env()

    def on_index_update(data: IndexData):
        print(f"KOSPI200: {data.value:.2f} ({data.change_pct:+.2f}%)")

    try:
        collector.connect()
        collector.start(callback=on_index_update)
    except KeyboardInterrupt:
        collector.stop()
