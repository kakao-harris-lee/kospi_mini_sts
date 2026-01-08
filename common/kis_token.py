"""
KIS API 토큰 관리 (파일 캐시)
- .kis_token 파일에 토큰 저장
- 24시간 재사용 가능
"""
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
TOKEN_FILE = PROJECT_ROOT / ".kis_token"


class KISToken:
    """
    한국투자증권 API 토큰 관리 (파일 캐시)

    토큰을 .kis_token 파일에 저장하여 24시간 동안 재사용.
    프로세스 재시작 시에도 유효한 토큰 유지.
    """

    _instance: Optional["KISToken"] = None
    _token: Optional[str] = None
    _expires_at: float = 0

    def __new__(cls, app_key: str = "", app_secret: str = "", is_real: bool = True):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, app_key: str = "", app_secret: str = "", is_real: bool = True):
        if self._initialized:
            return

        self._app_key = app_key or os.getenv("KIS_APP_KEY", "")
        self._app_secret = app_secret or os.getenv("KIS_APP_SECRET", "")
        self._is_real = is_real
        self._initialized = True

        # 파일에서 토큰 로드 시도
        self._load_from_file()

    @property
    def _api_base(self) -> str:
        if self._is_real:
            return "https://openapi.koreainvestment.com:9443"
        return "https://openapivts.koreainvestment.com:29443"

    def _load_from_file(self) -> bool:
        """파일에서 토큰 로드"""
        if not TOKEN_FILE.exists():
            return False

        try:
            with open(TOKEN_FILE, "r") as f:
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
            with open(TOKEN_FILE, "w") as f:
                json.dump(data, f)

            # 권한 제한 (소유자만 읽기/쓰기)
            os.chmod(TOKEN_FILE, 0o600)
            logger.info(f"[KISToken] Saved to {TOKEN_FILE}")
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
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
            logger.info("[KISToken] Token invalidated")
