"""
KIS API 토큰 관리 (파일 캐시)
- 앱키별 독립 토큰 캐시 파일
- 24시간 재사용 가능
- domain별 앱키 분리 지원 (stock/futures)
"""
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# 프로젝트 루트 디렉토리
PROJECT_ROOT = Path(__file__).parent.parent


def _token_cache_path(app_key: str, is_real: bool) -> Path:
    """앱키별 고유한 토큰 캐시 파일 경로 반환.

    app_key의 SHA-256 해시 첫 8자를 파일명에 포함하여
    서로 다른 앱키 간 토큰 충돌 방지.
    """
    mode = "real" if is_real else "mock"
    if app_key:
        key_hash = hashlib.sha256(app_key.encode()).hexdigest()[:8]
        return PROJECT_ROOT / f".kis_token_{mode}_{key_hash}"
    return PROJECT_ROOT / f".kis_token_{mode}"


class KISToken:
    """
    한국투자증권 API 토큰 관리 (파일 캐시)

    토큰을 .kis_token_{mode}_{hash} 파일에 저장하여 24시간 동안 재사용.
    프로세스 재시작 시에도 유효한 토큰 유지.

    앱키별 독립 인스턴스를 유지하여 stock/futures 동시 사용 가능.
    """

    _instances: dict = {}

    def __new__(cls, app_key: str = "", app_secret: str = "", is_real: bool = True):
        resolved_key = app_key or os.getenv("KIS_APP_KEY", "")
        instance_key = (resolved_key, is_real)
        if instance_key not in cls._instances:
            instance = super().__new__(cls)
            instance._initialized = False
            cls._instances[instance_key] = instance
        return cls._instances[instance_key]

    def __init__(self, app_key: str = "", app_secret: str = "", is_real: bool = True):
        if self._initialized:
            return

        self._app_key = app_key or os.getenv("KIS_APP_KEY", "")
        self._app_secret = app_secret or os.getenv("KIS_APP_SECRET", "")
        self._is_real = is_real
        self._token: Optional[str] = None
        self._expires_at: float = 0
        self._token_file = _token_cache_path(self._app_key, self._is_real)
        self._initialized = True

        # 파일에서 토큰 로드 시도
        self._load_from_file()
        logger.info(f"[KISToken] Initialized (cache={self._token_file.name})")

    @property
    def _api_base(self) -> str:
        if self._is_real:
            return "https://openapi.koreainvestment.com:9443"
        return "https://openapivts.koreainvestment.com:29443"

    def _load_from_file(self) -> bool:
        """파일에서 토큰 로드"""
        if not self._token_file.exists():
            return False

        try:
            with open(self._token_file, "r") as f:
                data = json.load(f)

            token = data.get("access_token")
            expires_at = data.get("expires_at", 0)

            # 만료 1분 전까지 유효하면 사용
            if token and time.time() < expires_at - 60:
                self._token = token
                self._expires_at = expires_at
                remaining = int(expires_at - time.time())
                logger.info(f"[KISToken] Loaded from file, {remaining}s remaining")
                return True

            logger.info("[KISToken] Token file expired, will refresh")
            return False

        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"[KISToken] Failed to load token file: {e}")
            return False

    def _save_to_file(self):
        """토큰을 파일에 저장"""
        try:
            data = {
                "access_token": self._token,
                "expires_at": self._expires_at,
            }
            with open(self._token_file, "w") as f:
                json.dump(data, f)

            # 권한 제한 (소유자만 읽기/쓰기)
            os.chmod(self._token_file, 0o600)
            logger.info(f"[KISToken] Saved to {self._token_file}")
        except IOError as e:
            logger.warning(f"[KISToken] Failed to save token file: {e}")

    def get(self) -> Optional[str]:
        """현재 유효한 토큰 반환 (필요시 갱신)"""
        # 만료 1분 전까지 유효하면 반환
        if self._token and time.time() < self._expires_at - 60:
            return self._token

        # 토큰 갱신
        try:
            self._refresh()
            return self._token
        except Exception as e:
            logger.error(f"[KISToken] Failed to refresh: {e}")
            return None

    def _refresh(self):
        """토큰 갱신 및 파일 저장"""
        if not self._app_key or not self._app_secret:
            raise ValueError("KIS_APP_KEY and KIS_APP_SECRET must be set")

        url = f"{self._api_base}/oauth2/tokenP"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self._app_key,
            "appsecret": self._app_secret,
        }

        resp = requests.post(url, json=payload, timeout=30)
        data = resp.json()

        if "access_token" not in data:
            raise ValueError(f"Token refresh failed: {data}")

        self._token = data["access_token"]
        expires_in = int(data.get("expires_in", 86400))  # 기본 24시간
        self._expires_at = time.time() + expires_in

        logger.info(f"[KISToken] Refreshed, expires in {expires_in}s (~{expires_in // 3600}h)")

        # 파일에 저장
        self._save_to_file()

    def invalidate(self):
        """토큰 무효화 (재발급 강제)"""
        self._token = None
        self._expires_at = 0
        if self._token_file.exists():
            self._token_file.unlink()
            logger.info("[KISToken] Token invalidated")

    def is_token_valid(self) -> bool:
        """토큰 유효 여부 확인"""
        return bool(self._token and time.time() < self._expires_at - 60)

    def refresh_token(self) -> bool:
        """토큰 갱신. 성공 시 True 반환."""
        try:
            self._refresh()
            return True
        except Exception as e:
            logger.warning(f"[KISToken] Token refresh failed: {e}")
            return False


def get_kis_token(domain: str = "stock", is_real: Optional[bool] = None) -> KISToken:
    """도메인별 KISToken 인스턴스 팩토리.

    Args:
        domain: "stock" or "futures"
        is_real: True=실전, False=모의. None이면 KIS_MARKET 환경변수 사용.

    Returns:
        해당 도메인의 KISToken 인스턴스
    """
    if is_real is None:
        is_real = os.getenv("KIS_MARKET", "real") != "mock"

    if domain == "futures":
        app_key = os.getenv("KIS_FUTURES_APP_KEY", "") or os.getenv("KIS_APP_KEY", "")
        app_secret = os.getenv("KIS_FUTURES_APP_SECRET", "") or os.getenv("KIS_APP_SECRET", "")
    else:
        app_key = os.getenv("KIS_APP_KEY", "")
        app_secret = os.getenv("KIS_APP_SECRET", "")

    logger.debug(f"[KISToken] get_kis_token domain={domain}, is_real={is_real}")
    return KISToken(app_key=app_key, app_secret=app_secret, is_real=is_real)
