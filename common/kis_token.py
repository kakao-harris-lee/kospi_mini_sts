"""
한국투자증권 API 토큰 관리 (공유 캐시)

min_data와 trading-system에서 공유하여 사용.
토큰 발급은 1분당 1회 제한이 있으므로, 파일 캐시를 통해 공유.
"""
import os
import time
import json
import requests
import fcntl
from typing import Optional
from pathlib import Path


# 프로젝트 루트의 토큰 캐시 파일
PROJECT_ROOT = Path(__file__).parent.parent
TOKEN_CACHE_FILE = PROJECT_ROOT / ".kis_token_cache.json"


class KISToken:
    """
    한국투자증권 OAuth 토큰 관리자

    파일 기반 캐시를 사용하여 여러 프로세스에서 토큰 공유.
    토큰 발급은 1분당 1회 제한이 있으므로 캐시 필수.

    Usage:
        token = KISToken(app_key, app_secret)
        access_token = token.get()
    """

    # 한투 API Base URL
    API_BASE = "https://openapi.koreainvestment.com:9443"

    def __init__(
        self,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        cache_file: Optional[Path] = None
    ):
        """
        Args:
            app_key: 한투 API 앱키 (None이면 환경변수에서 로드)
            app_secret: 한투 API 앱시크릿 (None이면 환경변수에서 로드)
            cache_file: 토큰 캐시 파일 경로 (기본값: 프로젝트 루트/.kis_token_cache.json)
        """
        self.app_key = app_key or os.getenv("KIS_APP_KEY", "")
        self.app_secret = app_secret or os.getenv("KIS_APP_SECRET", "")
        self.cache_file = cache_file or TOKEN_CACHE_FILE

        self._token: Optional[str] = None
        self._expires_at: float = 0

        # 캐시에서 토큰 로드
        self._load_cache()

    def _load_cache(self) -> bool:
        """파일에서 캐시된 토큰 로드"""
        try:
            if not self.cache_file.exists():
                return False

            with open(self.cache_file, "r") as f:
                # 파일 잠금 (읽기)
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    cache = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

            token = cache.get("token")
            expires_at = cache.get("expires_at", 0)

            # 토큰이 유효한지 확인 (만료 60초 전까지만 유효)
            if token and time.time() < expires_at - 60:
                self._token = token
                self._expires_at = expires_at
                remaining = int(expires_at - time.time())
                print(f"[KISToken] Loaded cached token, expires in {remaining}s")
                return True

            return False

        except Exception as e:
            print(f"[KISToken] Failed to load cache: {e}")
            return False

    def _save_cache(self):
        """토큰을 파일에 저장"""
        try:
            # 디렉토리 생성
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)

            with open(self.cache_file, "w") as f:
                # 파일 잠금 (쓰기)
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump({
                        "token": self._token,
                        "expires_at": self._expires_at
                    }, f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        except Exception as e:
            print(f"[KISToken] Failed to save cache: {e}")

    def refresh(self, retry_on_rate_limit: bool = True) -> Optional[str]:
        """
        새 토큰 발급

        Args:
            retry_on_rate_limit: 레이트 리밋 시 60초 대기 후 재시도

        Returns:
            액세스 토큰 (실패 시 None)
        """
        url = f"{self.API_BASE}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }

        try:
            resp = requests.post(url, json=body, timeout=10)
            data = resp.json()

            # 레이트 리밋 에러 처리
            if "error_code" in data:
                error_code = data.get("error_code")
                error_desc = data.get("error_description", error_code)
                print(f"[KISToken] Token error: {error_desc}")

                if error_code == "EGW00133" and retry_on_rate_limit:
                    print("[KISToken] Rate limited. Waiting 60s...")
                    time.sleep(60)
                    return self.refresh(retry_on_rate_limit=False)
                return None

            # 토큰 저장
            self._token = data.get("access_token")
            expires_in = int(data.get("expires_in", 86400))
            self._expires_at = time.time() + expires_in

            # 캐시에 저장
            self._save_cache()

            print(f"[KISToken] Token refreshed, expires in {expires_in}s")
            return self._token

        except Exception as e:
            print(f"[KISToken] Failed to refresh token: {e}")
            return None

    def get(self) -> Optional[str]:
        """
        유효한 토큰 반환

        캐시된 토큰이 유효하면 재사용, 아니면 새로 발급.

        Returns:
            액세스 토큰 (실패 시 None)
        """
        # 캐시된 토큰이 유효하면 반환
        if self._token and time.time() < self._expires_at - 60:
            return self._token

        # 파일 캐시 다시 확인 (다른 프로세스가 갱신했을 수 있음)
        if self._load_cache():
            return self._token

        # 새 토큰 발급
        return self.refresh()

    @property
    def is_valid(self) -> bool:
        """토큰이 유효한지 확인"""
        return self._token is not None and time.time() < self._expires_at - 60


# 싱글톤 인스턴스 (환경변수에서 자동 로드)
_default_token: Optional[KISToken] = None


def get_token() -> KISToken:
    """기본 토큰 인스턴스 반환 (싱글톤)"""
    global _default_token
    if _default_token is None:
        _default_token = KISToken()
    return _default_token


def get_access_token() -> Optional[str]:
    """액세스 토큰 문자열 반환 (편의 함수)"""
    return get_token().get()
