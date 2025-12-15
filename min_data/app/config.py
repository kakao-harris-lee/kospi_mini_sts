import os
from dotenv import load_dotenv

# .env 파일 로드 (있으면)
load_dotenv()

# Korea Investment API
KI_API_BASE = "https://openapi.koreainvestment.com:9443"
KI_APP_KEY = os.getenv("KIS_APP_KEY", "PSGnWoWbUwphmwiyt1UBAZVHAfnB0sdB9LT5")
KI_APP_SECRET = os.getenv("KIS_APP_SECRET", "LTEpng3OhKWHW4TapFaxG81DpD9uNXNEtF5f29Uii4U8IrlT2YC7I5ZmKhh2XCFwa5WOPLwx5W0fgoU0ldhWWavYuXYqMi+bQ3Ssj2SBG2q4kQZTF4OtsAFzkgkjYY3L8P/BpeSgr+VH0gFfnJDTpA7+8RsFQXXeT6fGxGZ7dc1wYHJrrSM=")

# ClickHouse (HTTP 프로토콜 사용)
CLICKHOUSE_HOST = os.getenv("CLICKHOUSE_HOST", "49.247.171.64")
CLICKHOUSE_PORT = int(os.getenv("CLICKHOUSE_PORT", "8123"))
CLICKHOUSE_DATABASE = os.getenv("CLICKHOUSE_DATABASE", "kospi")
CLICKHOUSE_USER = os.getenv("CLICKHOUSE_USER", "")
CLICKHOUSE_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")

# Rate limiting (Korea Investment)
API_RATE_LIMIT = 20  # requests per second
API_SEMAPHORE_SIZE = 10  # concurrent requests

# Polygon.io API (무료 플랜: 5 req/min)
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "bMXRQy96F4rWfq3JJtz7TpNgntE5HBO8")
POLYGON_RATE_LIMIT = int(os.getenv("POLYGON_RATE_LIMIT", "5"))
