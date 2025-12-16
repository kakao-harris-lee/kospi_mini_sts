"""
한국투자증권 API 토큰 관리

공유 토큰 캐시 모듈(common.kis_token)을 사용하여
min_data와 trading-system에서 토큰을 공유합니다.
"""
import sys
import os

# 프로젝트 루트를 path에 추가하여 common 모듈 import
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app import config

# 공유 토큰 모듈 import
try:
    from common.kis_token import KISToken
except ImportError:
    # 공유 모듈이 없으면 로컬 구현 사용 (fallback)
    KISToken = None


class KoreaInvestToken:
    """
    한국투자증권 OAuth 토큰 관리자

    공유 캐시를 사용하여 min_data와 trading-system에서 토큰 공유.
    토큰 발급은 1분당 1회 제한이 있으므로 캐시 필수.
    """

    def __init__(self):
        if KISToken is not None:
            # 공유 토큰 모듈 사용
            self._shared_token = KISToken(
                app_key=config.KI_APP_KEY,
                app_secret=config.KI_APP_SECRET
            )
        else:
            # Fallback: 로컬 구현
            self._shared_token = None
            self.token = None
            self.exp = 0

    def refresh(self):
        """토큰 갱신"""
        if self._shared_token:
            return self._shared_token.refresh()
        else:
            # Fallback 구현
            import requests
            import time
            url = f"{config.KI_API_BASE}/oauth2/tokenP"
            body = {
                "grant_type": "client_credentials",
                "appkey": config.KI_APP_KEY,
                "appsecret": config.KI_APP_SECRET
            }
            r = requests.post(url, json=body)
            j = r.json()

            if "error_code" in j:
                print(f"Token error: {j.get('error_description', j.get('error_code'))}")
                if j.get("error_code") == "EGW00133":
                    print("Rate limited. Waiting 60 seconds...")
                    time.sleep(60)
                    return self.refresh()
                return None

            self.token = j.get("access_token")
            self.exp = time.time() + int(j.get("expires_in", 86400)) - 60
            return self.token

    def get(self):
        """유효한 토큰 반환"""
        if self._shared_token:
            return self._shared_token.get()
        else:
            import time
            if not self.token or time.time() > self.exp:
                return self.refresh()
            return self.token
