"""
한국투자증권 WebSocket 어댑터
KOSPI Mini 선물 실시간 호가/체결 데이터 수신
"""
import json
import time
import asyncio
import logging
from typing import Dict, Any, Optional, Callable, List
from dataclasses import dataclass
from enum import Enum
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import base64
import websockets
import requests

import sys
import os
# trading-system 루트
TRADING_SYSTEM_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 프로젝트 루트 (kospi_mini_sts)
PROJECT_ROOT = os.path.dirname(TRADING_SYSTEM_ROOT)

if TRADING_SYSTEM_ROOT not in sys.path:
    sys.path.insert(0, TRADING_SYSTEM_ROOT)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config.settings import settings
from src.collector.data_collector import BaseAPIAdapter, TickData
from src.common import setup_logging, trading_logger

# 공유 토큰 모듈 import (WebSocket은 approval_key 사용하므로 참고용)
try:
    from common.kis_token import KISToken
except ImportError:
    KISToken = None

logger = setup_logging("kis_websocket")


class KISMarket(Enum):
    """시장 구분"""
    REAL = "real"      # 실전투자
    MOCK = "mock"      # 모의투자


@dataclass
class KISConfig:
    """한투 API 설정"""
    app_key: str
    app_secret: str
    market: KISMarket = KISMarket.REAL

    @property
    def ws_url(self) -> str:
        """WebSocket URL"""
        if self.market == KISMarket.REAL:
            return "ws://ops.koreainvestment.com:21000"
        return "ws://ops.koreainvestment.com:31000"

    @property
    def api_base(self) -> str:
        """REST API Base URL"""
        return "https://openapi.koreainvestment.com:9443"


class KISWebSocketAdapter(BaseAPIAdapter):
    """
    한국투자증권 WebSocket 어댑터
    KOSPI Mini 선물 실시간 데이터 수신
    """

    # 지수선물 tr_id
    TR_FUTURES_ASK = "H0IFASP0"  # 호가
    TR_FUTURES_CNT = "H0IFCNT0"  # 체결

    def __init__(self, config: KISConfig):
        self.config = config
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._callback: Optional[Callable[[TickData], None]] = None
        self._running = False
        self._approval_key: Optional[str] = None
        self._aes_key: Optional[str] = None
        self._aes_iv: Optional[str] = None
        self._subscribed_symbols: List[str] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _get_approval_key(self) -> str:
        """WebSocket 접속키 발급"""
        url = f"{self.config.api_base}/oauth2/Approval"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.config.app_key,
            "secretkey": self.config.app_secret
        }

        try:
            resp = requests.post(url, headers=headers, json=body, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            approval_key = data.get("approval_key")
            if not approval_key:
                raise ValueError(f"approval_key not found in response: {data}")

            logger.info("WebSocket approval_key obtained")
            return approval_key

        except Exception as e:
            logger.error(f"Failed to get approval_key: {e}")
            raise

    def _build_subscribe_message(self, tr_id: str, tr_key: str, tr_type: str = "1") -> str:
        """
        구독 메시지 생성

        Args:
            tr_id: 거래ID (H0IFASP0, H0IFCNT0 등)
            tr_key: 종목코드
            tr_type: "1"=구독, "2"=해제
        """
        message = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",  # 개인
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

    def _decrypt_data(self, encrypted_data: str) -> str:
        """AES256 복호화"""
        if not self._aes_key or not self._aes_iv:
            return encrypted_data

        try:
            cipher = AES.new(
                self._aes_key.encode('utf-8'),
                AES.MODE_CBC,
                self._aes_iv.encode('utf-8')
            )
            decoded = base64.b64decode(encrypted_data)
            decrypted = unpad(cipher.decrypt(decoded), AES.block_size)
            return decrypted.decode('utf-8')
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            return encrypted_data

    def _parse_futures_ask(self, data: str, symbol: str) -> Optional[TickData]:
        """
        선물 호가 데이터 파싱

        호가 데이터 형식 (|로 구분):
        0: 유가증권단축종목코드
        1: 영업시간
        2: 시간구분코드
        3~12: 매도호가1~10
        13~22: 매수호가1~10
        23~32: 매도호가잔량1~10
        33~42: 매수호가잔량1~10
        ... (추가 필드)
        """
        try:
            fields = data.split('|')
            if len(fields) < 43:
                return None

            # 호가 데이터 추출
            ask_prices = [float(fields[i]) if fields[i] else 0 for i in range(3, 8)]   # 매도 1~5
            bid_prices = [float(fields[i]) if fields[i] else 0 for i in range(13, 18)] # 매수 1~5
            ask_qtys = [float(fields[i]) if fields[i] else 0 for i in range(23, 28)]   # 매도잔량 1~5
            bid_qtys = [float(fields[i]) if fields[i] else 0 for i in range(33, 38)]   # 매수잔량 1~5

            tick = TickData(
                symbol=symbol,
                timestamp=time.time(),
                bid_price_1=bid_prices[0],
                bid_qty_1=bid_qtys[0],
                ask_price_1=ask_prices[0],
                ask_qty_1=ask_qtys[0],
                bid_price_2=bid_prices[1] if len(bid_prices) > 1 else None,
                bid_qty_2=bid_qtys[1] if len(bid_qtys) > 1 else None,
                bid_price_3=bid_prices[2] if len(bid_prices) > 2 else None,
                bid_qty_3=bid_qtys[2] if len(bid_qtys) > 2 else None,
                bid_price_4=bid_prices[3] if len(bid_prices) > 3 else None,
                bid_qty_4=bid_qtys[3] if len(bid_qtys) > 3 else None,
                bid_price_5=bid_prices[4] if len(bid_prices) > 4 else None,
                bid_qty_5=bid_qtys[4] if len(bid_qtys) > 4 else None,
                ask_price_2=ask_prices[1] if len(ask_prices) > 1 else None,
                ask_qty_2=ask_qtys[1] if len(ask_qtys) > 1 else None,
                ask_price_3=ask_prices[2] if len(ask_prices) > 2 else None,
                ask_qty_3=ask_qtys[2] if len(ask_qtys) > 2 else None,
                ask_price_4=ask_prices[3] if len(ask_prices) > 3 else None,
                ask_qty_4=ask_qtys[3] if len(ask_qtys) > 3 else None,
                ask_price_5=ask_prices[4] if len(ask_prices) > 4 else None,
                ask_qty_5=ask_qtys[4] if len(ask_qtys) > 4 else None,
            )
            return tick

        except Exception as e:
            logger.error(f"Failed to parse futures ask data: {e}")
            return None

    def _parse_futures_cnt(self, data: str, symbol: str) -> Optional[TickData]:
        """
        선물 체결 데이터 파싱

        체결 데이터 형식 (|로 구분):
        0: 유가증권단축종목코드
        1: 영업시간
        2: 현재가
        3: 전일대비부호
        4: 전일대비
        5: 등락률
        6: 시가
        7: 고가
        8: 저가
        9: 매도호가
        10: 매수호가
        11: 체결량
        12: 누적거래량
        13: 누적거래대금
        14: 미결제약정
        ... (추가 필드)
        """
        try:
            fields = data.split('|')
            if len(fields) < 15:
                return None

            tick = TickData(
                symbol=symbol,
                timestamp=time.time(),
                bid_price_1=float(fields[10]) if fields[10] else 0,  # 매수호가
                bid_qty_1=0,  # 체결 데이터에는 잔량 없음
                ask_price_1=float(fields[9]) if fields[9] else 0,   # 매도호가
                ask_qty_1=0,
                tick_volume=float(fields[11]) if fields[11] else 0,  # 체결량
                open_interest=float(fields[14]) if len(fields) > 14 and fields[14] else None  # 미결제약정
            )
            return tick

        except Exception as e:
            logger.error(f"Failed to parse futures cnt data: {e}")
            return None

    def _process_message(self, message: str):
        """수신 메시지 처리"""
        try:
            # 상세 로깅: 원본 WebSocket 메시지
            trading_logger.log_websocket_raw(message, parsed=True)

            # JSON 응답인지 확인
            if message.startswith('{'):
                data = json.loads(message)

                # 접속 성공 응답
                if "header" in data:
                    header = data["header"]
                    tr_id = header.get("tr_id", "")

                    # PINGPONG (Heartbeat)
                    if tr_id == "PINGPONG":
                        logger.debug("Received PINGPONG")
                        return

                    # 암호화 키 수신
                    if "output" in data.get("body", {}):
                        output = data["body"]["output"]
                        if "key" in output:
                            self._aes_key = output["key"]
                            self._aes_iv = output["iv"]
                            logger.info("AES key received")

                    # 구독 응답
                    msg_cd = header.get("msg_cd", "")
                    if msg_cd:
                        logger.info(f"Response: {msg_cd} - {data.get('body', {}).get('msg1', '')}")
                return

            # 파이프(|)로 구분된 실시간 데이터
            # 형식: 암호화여부|tr_id|데이터건수|데이터
            parts = message.split('|', 3)
            if len(parts) < 4:
                return

            encrypted = parts[0] == "1"
            tr_id = parts[1]
            count = int(parts[2])
            raw_data = parts[3]

            # 암호화 데이터 복호화
            if encrypted:
                raw_data = self._decrypt_data(raw_data)

            # 데이터 파싱
            # 여러 건의 데이터가 ^로 구분됨
            data_items = raw_data.split('^') if '^' in raw_data else [raw_data]

            for item in data_items:
                # 종목코드 추출 (첫 번째 필드)
                item_fields = item.split('|')
                if not item_fields:
                    continue
                symbol = item_fields[0]

                tick = None
                tick_type = "orderbook"
                if tr_id == self.TR_FUTURES_ASK:
                    tick = self._parse_futures_ask(item, symbol)
                    tick_type = "orderbook"
                elif tr_id == self.TR_FUTURES_CNT:
                    tick = self._parse_futures_cnt(item, symbol)
                    tick_type = "trade"

                if tick and self._callback:
                    # 상세 로깅: 수신된 틱 데이터
                    trading_logger.log_tick_received(tick, tick_type)
                    self._callback(tick)

        except Exception as e:
            logger.error(f"Error processing message: {e}")

    async def _connect_and_subscribe(self, symbols: List[str]):
        """WebSocket 연결 및 구독"""
        try:
            logger.info(f"Connecting to {self.config.ws_url}")

            async with websockets.connect(
                self.config.ws_url,
                ping_interval=30,
                ping_timeout=10
            ) as ws:
                self._ws = ws
                logger.info("WebSocket connected")

                # 각 종목에 대해 호가/체결 구독
                for symbol in symbols:
                    # 호가 구독
                    msg = self._build_subscribe_message(self.TR_FUTURES_ASK, symbol)
                    await ws.send(msg)
                    logger.info(f"Subscribed to {symbol} ASK")
                    await asyncio.sleep(0.1)

                    # 체결 구독
                    msg = self._build_subscribe_message(self.TR_FUTURES_CNT, symbol)
                    await ws.send(msg)
                    logger.info(f"Subscribed to {symbol} CNT")
                    await asyncio.sleep(0.1)

                self._subscribed_symbols = symbols

                # 메시지 수신 루프
                while self._running:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=60)
                        self._process_message(message)
                    except asyncio.TimeoutError:
                        # PINGPONG 전송 (Heartbeat)
                        await ws.send("PING")
                        logger.debug("Sent PING")
                    except websockets.ConnectionClosed:
                        logger.warning("WebSocket connection closed")
                        break

        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            raise

    def connect(self):
        """API 연결 (approval_key 발급)"""
        self._approval_key = self._get_approval_key()
        logger.info("KIS WebSocket adapter ready")

    def subscribe(self, symbols: List[str], callback: Callable[[TickData], None]):
        """
        심볼 구독 및 콜백 등록

        Args:
            symbols: KOSPI Mini 선물 종목코드 리스트 (예: ["101V24", "101Z24"])
            callback: 틱 데이터 수신 시 호출할 함수
        """
        self._callback = callback
        self._running = True

        # 이벤트 루프 실행
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._connect_and_subscribe(symbols))
        except KeyboardInterrupt:
            pass
        finally:
            self._loop.close()

    def disconnect(self):
        """연결 해제"""
        self._running = False
        if self._ws:
            # 구독 해제 메시지 전송 (선택)
            pass
        logger.info("KIS WebSocket disconnected")


# ============================================================
# 테스트 및 실행
# ============================================================

def create_kis_adapter_from_env() -> KISWebSocketAdapter:
    """환경변수에서 설정을 읽어 어댑터 생성"""
    import os
    from dotenv import load_dotenv

    load_dotenv()

    config = KISConfig(
        app_key=os.getenv("KIS_APP_KEY", ""),
        app_secret=os.getenv("KIS_APP_SECRET", ""),
        market=KISMarket.REAL if os.getenv("KIS_MARKET", "real") == "real" else KISMarket.MOCK
    )

    return KISWebSocketAdapter(config)


if __name__ == "__main__":
    # 테스트 실행
    from src.collector.data_collector import DataCollector

    print("Testing KIS WebSocket Adapter...")

    adapter = create_kis_adapter_from_env()
    collector = DataCollector(adapter)

    # KOSPI Mini 근월물 종목코드 (예시)
    symbols = ["101Z24"]  # 2024년 12월물

    try:
        collector.start(symbols=symbols)
    except KeyboardInterrupt:
        collector.stop()
