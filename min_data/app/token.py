import requests, time, json, os
from app import config

# 토큰 캐시 파일 경로
TOKEN_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".token_cache.json")


class KoreaInvestToken:
    def __init__(self):
        self.token = None
        self.exp = 0
        self._load_cache()

    def _load_cache(self):
        """파일에서 캐시된 토큰 로드"""
        try:
            if os.path.exists(TOKEN_CACHE_FILE):
                with open(TOKEN_CACHE_FILE, "r") as f:
                    cache = json.load(f)
                    self.token = cache.get("token")
                    self.exp = cache.get("exp", 0)
                    if self.token and time.time() < self.exp:
                        print(f"Loaded cached token, expires in {int(self.exp - time.time())} seconds")
                    else:
                        self.token = None
                        self.exp = 0
        except Exception as e:
            print(f"Failed to load token cache: {e}")
            self.token = None
            self.exp = 0

    def _save_cache(self):
        """토큰을 파일에 캐시"""
        try:
            with open(TOKEN_CACHE_FILE, "w") as f:
                json.dump({"token": self.token, "exp": self.exp}, f)
        except Exception as e:
            print(f"Failed to save token cache: {e}")

    def refresh(self):
        url = f"{config.KI_API_BASE}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": config.KI_APP_KEY,
            "appsecret": config.KI_APP_SECRET
        }
        r = requests.post(url, json=body)
        j = r.json()

        # Check for rate limit error
        if "error_code" in j:
            print(f"Token error: {j.get('error_description', j.get('error_code'))}")
            # If rate limited, wait and retry
            if j.get("error_code") == "EGW00133":
                print("Rate limited. Waiting 60 seconds...")
                time.sleep(60)
                return self.refresh()
            return None

        self.token = j.get("access_token")
        # expires_in 필드 사용 (초 단위)
        self.exp = time.time() + int(j.get("expires_in", 86400)) - 60
        self._save_cache()
        print(f"Token refreshed, expires in {j.get('expires_in', 86400)} seconds")
        return self.token

    def get(self):
        if not self.token or time.time() > self.exp:
            return self.refresh()
        return self.token
