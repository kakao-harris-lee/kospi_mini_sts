"""
한국투자증권 선물옵션 주문 API
KOSPI Mini 선물 주문/정정/취소
"""
import os
import time
import json
import logging
import requests
from typing import Dict, Any, Optional
from dataclasses import dataclass
from enum import Enum
from datetime import datetime

import sys
# trading-system 루트
TRADING_SYSTEM_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 프로젝트 루트 (kospi_mini_sts)
PROJECT_ROOT = os.path.dirname(TRADING_SYSTEM_ROOT)

if TRADING_SYSTEM_ROOT not in sys.path:
    sys.path.insert(0, TRADING_SYSTEM_ROOT)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.common import setup_logging

# 공유 토큰 모듈 import
try:
    from common.kis_token import KISToken
except ImportError:
    KISToken = None

logger = setup_logging("kis_order")


class OrderSide(Enum):
    """주문 방향"""
    BUY = "02"   # 매수
    SELL = "01"  # 매도


class OrderType(Enum):
    """주문 유형"""
    LIMIT = "00"   # 지정가
    MARKET = "01"  # 시장가


@dataclass
class OrderRequest:
    """주문 요청"""
    symbol: str           # 종목코드 (예: 101Z25)
    side: OrderSide       # 매수/매도
    order_type: OrderType # 지정가/시장가
    quantity: int         # 수량
    price: float = 0      # 가격 (시장가일 경우 0)

    def __post_init__(self):
        """Validate minimum order quantity (1 contract for KOSPI Mini Futures)"""
        if self.quantity < 1:
            raise ValueError(f"Order quantity must be at least 1 contract, got {self.quantity}")


@dataclass
class OrderResponse:
    """주문 응답"""
    success: bool
    order_no: Optional[str] = None  # 주문번호
    message: Optional[str] = None
    raw_response: Optional[Dict] = None


@dataclass
class KISOrderConfig:
    """한투 주문 API 설정"""
    app_key: str
    app_secret: str
    account_no: str        # 계좌번호 (8자리-2자리)
    is_mock: bool = False  # 모의투자 여부

    @property
    def api_base(self) -> str:
        """실서버/모의투자 URL"""
        if self.is_mock:
            return "https://openapivts.koreainvestment.com:29443"
        return "https://openapi.koreainvestment.com:9443"

    @property
    def account_prefix(self) -> str:
        """계좌번호 앞 8자리"""
        return self.account_no.split('-')[0]

    @property
    def account_suffix(self) -> str:
        """계좌번호 뒤 2자리"""
        return self.account_no.split('-')[1]


class KISOrderAPI:
    """
    한국투자증권 선물옵션 주문 API

    API 문서:
    - 선물옵션 주문: /uapi/domestic-futureoption/v1/trading/order
    - 선물옵션 정정/취소: /uapi/domestic-futureoption/v1/trading/order-rvsecncl
    """

    # tr_id 상수
    TR_ORDER_BUY = "TTTT1001U"      # 선물옵션 매수
    TR_ORDER_SELL = "TTTT1002U"     # 선물옵션 매도
    TR_ORDER_MODIFY = "TTTT1003U"   # 선물옵션 정정
    TR_ORDER_CANCEL = "TTTT1004U"   # 선물옵션 취소

    # 모의투자 tr_id
    TR_MOCK_ORDER_BUY = "VTTT1001U"
    TR_MOCK_ORDER_SELL = "VTTT1002U"
    TR_MOCK_ORDER_MODIFY = "VTTT1003U"
    TR_MOCK_ORDER_CANCEL = "VTTT1004U"

    def __init__(self, config: KISOrderConfig):
        self.config = config
        self._access_token: Optional[str] = None
        self._token_expires: float = 0

        # 공유 토큰 모듈 사용
        if KISToken is not None:
            self._shared_token = KISToken(
                app_key=config.app_key,
                app_secret=config.app_secret
            )
        else:
            self._shared_token = None

    def _get_access_token(self) -> str:
        """OAuth 액세스 토큰 발급 (공유 캐시 사용)"""
        # 공유 토큰 모듈 사용
        if self._shared_token:
            token = self._shared_token.get()
            if token:
                return token
            raise RuntimeError("Failed to get access token from shared cache")

        # Fallback: 직접 발급
        if self._access_token and time.time() < self._token_expires - 60:
            return self._access_token

        url = f"{self.config.api_base}/oauth2/tokenP"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret
        }

        try:
            resp = requests.post(url, headers=headers, json=body, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            self._access_token = data["access_token"]
            expires_in = data.get("expires_in", 86400)
            self._token_expires = time.time() + expires_in

            logger.info("Access token obtained (direct)")
            return self._access_token

        except Exception as e:
            logger.error(f"Failed to get access token: {e}")
            raise

    def _get_headers(self, tr_id: str) -> Dict[str, str]:
        """API 요청 헤더 생성"""
        token = self._get_access_token()
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
            "tr_id": tr_id,
        }

    def _get_tr_id(self, base_tr_id: str) -> str:
        """실전/모의 tr_id 반환"""
        if not self.config.is_mock:
            return base_tr_id

        # 모의투자 tr_id로 변환
        mapping = {
            self.TR_ORDER_BUY: self.TR_MOCK_ORDER_BUY,
            self.TR_ORDER_SELL: self.TR_MOCK_ORDER_SELL,
            self.TR_ORDER_MODIFY: self.TR_MOCK_ORDER_MODIFY,
            self.TR_ORDER_CANCEL: self.TR_MOCK_ORDER_CANCEL,
        }
        return mapping.get(base_tr_id, base_tr_id)

    def place_order(self, request: OrderRequest) -> OrderResponse:
        """
        선물옵션 주문

        Args:
            request: 주문 요청

        Returns:
            OrderResponse: 주문 응답
        """
        url = f"{self.config.api_base}/uapi/domestic-futureoption/v1/trading/order"

        # tr_id 결정 (매수/매도)
        base_tr_id = self.TR_ORDER_BUY if request.side == OrderSide.BUY else self.TR_ORDER_SELL
        tr_id = self._get_tr_id(base_tr_id)

        headers = self._get_headers(tr_id)

        body = {
            "CANO": self.config.account_prefix,
            "ACNT_PRDT_CD": self.config.account_suffix,
            "SLL_BUY_DVSN_CD": request.side.value,
            "SHTN_PDNO": request.symbol,
            "ORD_QTY": str(request.quantity),
            "UNIT_PRICE": str(int(request.price)) if request.order_type == OrderType.LIMIT else "0",
            "NMPR_TYPE_CD": request.order_type.value,
        }

        try:
            logger.info(f"Placing order: {request.side.name} {request.quantity} {request.symbol} @ {request.price}")
            resp = requests.post(url, headers=headers, json=body, timeout=10)
            data = resp.json()

            if data.get("rt_cd") == "0":
                order_no = data.get("output", {}).get("ODNO")
                logger.info(f"Order placed successfully: {order_no}")
                return OrderResponse(
                    success=True,
                    order_no=order_no,
                    message=data.get("msg1"),
                    raw_response=data
                )
            else:
                error_msg = data.get("msg1", "Unknown error")
                logger.error(f"Order failed: {error_msg}")
                return OrderResponse(
                    success=False,
                    message=error_msg,
                    raw_response=data
                )

        except Exception as e:
            logger.error(f"Order request failed: {e}")
            return OrderResponse(success=False, message=str(e))

    def cancel_order(self, order_no: str, symbol: str, quantity: int) -> OrderResponse:
        """
        선물옵션 주문 취소

        Args:
            order_no: 원주문번호
            symbol: 종목코드
            quantity: 취소수량

        Returns:
            OrderResponse: 취소 응답
        """
        url = f"{self.config.api_base}/uapi/domestic-futureoption/v1/trading/order-rvsecncl"

        tr_id = self._get_tr_id(self.TR_ORDER_CANCEL)
        headers = self._get_headers(tr_id)

        body = {
            "CANO": self.config.account_prefix,
            "ACNT_PRDT_CD": self.config.account_suffix,
            "ORGN_ODNO": order_no,
            "SHTN_PDNO": symbol,
            "ORD_QTY": str(quantity),
            "UNIT_PRICE": "0",
            "NMPR_TYPE_CD": "00",
        }

        try:
            logger.info(f"Cancelling order: {order_no}")
            resp = requests.post(url, headers=headers, json=body, timeout=10)
            data = resp.json()

            if data.get("rt_cd") == "0":
                logger.info(f"Order cancelled: {order_no}")
                return OrderResponse(
                    success=True,
                    order_no=data.get("output", {}).get("ODNO"),
                    message=data.get("msg1"),
                    raw_response=data
                )
            else:
                error_msg = data.get("msg1", "Unknown error")
                logger.error(f"Cancel failed: {error_msg}")
                return OrderResponse(success=False, message=error_msg, raw_response=data)

        except Exception as e:
            logger.error(f"Cancel request failed: {e}")
            return OrderResponse(success=False, message=str(e))

    def modify_order(
        self,
        order_no: str,
        symbol: str,
        quantity: int,
        price: float
    ) -> OrderResponse:
        """
        선물옵션 주문 정정

        Args:
            order_no: 원주문번호
            symbol: 종목코드
            quantity: 정정수량
            price: 정정가격

        Returns:
            OrderResponse: 정정 응답
        """
        url = f"{self.config.api_base}/uapi/domestic-futureoption/v1/trading/order-rvsecncl"

        tr_id = self._get_tr_id(self.TR_ORDER_MODIFY)
        headers = self._get_headers(tr_id)

        body = {
            "CANO": self.config.account_prefix,
            "ACNT_PRDT_CD": self.config.account_suffix,
            "ORGN_ODNO": order_no,
            "SHTN_PDNO": symbol,
            "ORD_QTY": str(quantity),
            "UNIT_PRICE": str(int(price)),
            "NMPR_TYPE_CD": "00",  # 지정가
        }

        try:
            logger.info(f"Modifying order: {order_no} -> {quantity} @ {price}")
            resp = requests.post(url, headers=headers, json=body, timeout=10)
            data = resp.json()

            if data.get("rt_cd") == "0":
                logger.info(f"Order modified: {order_no}")
                return OrderResponse(
                    success=True,
                    order_no=data.get("output", {}).get("ODNO"),
                    message=data.get("msg1"),
                    raw_response=data
                )
            else:
                error_msg = data.get("msg1", "Unknown error")
                logger.error(f"Modify failed: {error_msg}")
                return OrderResponse(success=False, message=error_msg, raw_response=data)

        except Exception as e:
            logger.error(f"Modify request failed: {e}")
            return OrderResponse(success=False, message=str(e))


class KISOrderExecutor:
    """
    한투 주문 실행기
    Strategy Manager에서 사용
    """

    def __init__(self, api: Optional[KISOrderAPI] = None, dry_run: bool = True):
        self.api = api
        self.dry_run = dry_run
        self._order_count = 0

    def execute(
        self,
        symbol: str,
        side: str,
        size: float,
        price: float,
        order_type: str = "market"
    ) -> Optional[str]:
        """
        주문 실행

        Args:
            symbol: 종목코드
            side: "buy" 또는 "sell"
            size: 수량
            price: 가격
            order_type: "market" 또는 "limit"

        Returns:
            주문번호 (실패 시 None)
        """
        self._order_count += 1

        if self.dry_run:
            logger.info(f"[DRY RUN] {side.upper()} {size} {symbol} @ {price}")
            return f"DRY_{self._order_count:06d}"

        if not self.api:
            logger.error("KISOrderAPI not configured")
            return None

        request = OrderRequest(
            symbol=symbol,
            side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
            order_type=OrderType.MARKET if order_type == "market" else OrderType.LIMIT,
            quantity=int(size),
            price=price
        )

        response = self.api.place_order(request)
        return response.order_no if response.success else None


def create_order_api_from_env() -> KISOrderAPI:
    """환경변수에서 설정을 읽어 주문 API 생성"""
    from dotenv import load_dotenv
    load_dotenv()

    config = KISOrderConfig(
        app_key=os.getenv("KIS_APP_KEY", ""),
        app_secret=os.getenv("KIS_APP_SECRET", ""),
        account_no=os.getenv("KIS_ACCOUNT_NO", ""),
        is_mock=os.getenv("KIS_MARKET", "real") == "mock"
    )

    return KISOrderAPI(config)


if __name__ == "__main__":
    # 테스트 (DRY RUN)
    print("Testing KIS Order API (DRY RUN)...")

    executor = KISOrderExecutor(api=None, dry_run=True)

    # 테스트 주문
    order_no = executor.execute(
        symbol="101Z25",
        side="buy",
        size=1,
        price=350.0,
        order_type="market"
    )
    print(f"Order No: {order_no}")

    order_no = executor.execute(
        symbol="101Z25",
        side="sell",
        size=1,
        price=351.0,
        order_type="limit"
    )
    print(f"Order No: {order_no}")
