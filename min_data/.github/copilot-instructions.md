# Copilot Instructions for KOSPI Mini Minute Data Collector

## Project Overview

한국투자증권 Open API를 통해 코스피 미니 선물의 1분봉 OHLCV 데이터를 수집하는 프로젝트.

- **목표**: 머신러닝 학습용 데이터 - 과거 1년치 여러 월물 수집
- **저장소**: ClickHouse (49.247.171.64:9000)
- **수집 전략**:
  1. 백필: 누락된 과거 데이터 채우기 (갭 필링)
  2. 정기 수집: 장 마감 후 (15:45 이후) 당일 데이터 수집
- **성능**: 최대 성능 활용 (병렬/비동기 처리)

## Architecture

```
collector.py          # Entry point - 백필 및 정기 수집
app/
├── config.py         # API/DB 설정 (KI_API_*, CLICKHOUSE_*)
├── token.py          # OAuth2 토큰 관리 (자동 갱신)
├── fetch_minute.py   # API 호출 (domestic-futureoption/inquire-time-data)
└── db.py             # ClickHouse 연결 및 데이터 삽입 (TODO)
```

**Data Flow**:

1. DB에서 수집된 날짜 조회 → 누락 날짜 계산
2. 여러 월물 코드 × 누락 날짜 조합 생성
3. 병렬 API 호출 → Parse → Batch INSERT

## Key Conventions

### API Token Pattern

[app/token.py](app/token.py)의 `KoreaInvestToken` 클래스:

- 만료 60초 전 자동 갱신
- 항상 `tok.get()` 사용 - raw token 저장 금지

### Futures Code Format

코드 패턴: `101{월코드}{년도}` (예: `101M25` = 2025년 6월물)

- 월 코드: F(1월), G(2월), H(3월), J(4월), K(5월), M(6월), N(7월), Q(8월), U(9월), V(10월), X(11월), Z(12월)
- **수집 대상**: 과거 1년간 존재했던 모든 월물 (매월 월물 존재)
- 월물 만기: 매월 둘째 주 목요일

### Trading Calendar

- **거래일 조회**: 한국거래소 API 또는 공공데이터포털 활용
- **장 시간**: 09:00 ~ 15:45 (선물 정규장)
- **데이터 수집 시점**: 장 마감 후 15:45 이후

### Target Contracts (Example for 2024.12~2025.12)

```python
# 과거 1년간 거래된 월물 목록 생성
def get_target_codes(start_date, end_date):
    # 해당 기간 동안 거래된 모든 분기 월물 반환
    # 예: ["101Z24", "101H25", "101M25", "101U25", "101Z25"]
```

### ClickHouse Schema (Target)

```sql
CREATE TABLE kospi_mini_1m (
    code String,
    datetime DateTime64(3, 'Asia/Seoul'),
    open Float64,
    high Float64,
    low Float64,
    close Float64,
    volume UInt64
) ENGINE = MergeTree()
ORDER BY (code, datetime);
```

## Performance Guidelines

- **병렬 수집**: 여러 날짜를 동시에 수집 (API rate limit 고려)
- **Batch INSERT**: ClickHouse에 row 단위가 아닌 batch 단위 삽입
- **비동기 처리**: `asyncio` + `httpx` 또는 `aiohttp` 권장
- **Rate Limit**: 한투 API 초당 20건 제한 - `asyncio.Semaphore` 사용

## Configuration

[app/config.py](app/config.py)에서 설정:

```python
# Korea Investment API
KI_API_BASE = "https://openapi.koreainvestment.com:9443"
KI_CLIENT_ID = ""
KI_CLIENT_SECRET = ""

# ClickHouse
CLICKHOUSE_HOST = "49.247.171.64"
CLICKHOUSE_PORT = 9000
CLICKHOUSE_DATABASE = "kospi"
CLICKHOUSE_USER = ""
CLICKHOUSE_PASSWORD = ""
```

## Running

```bash
# 초기 1년치 백필 (누락 데이터 채우기)
python collector.py --backfill

# 지속적 수집 (장 마감 후 15:45 이후 실행)
python collector.py --continuous
```

## Gap Filling Strategy

```python
# 1. DB에서 이미 수집된 (code, date) 조합 조회
collected = db.query("SELECT DISTINCT code, toDate(datetime) FROM kospi_mini_1m")

# 2. 전체 대상 조합 생성 (codes × dates)
target = [(code, date) for code in target_codes for date in trading_days]

# 3. 누락된 조합만 수집
missing = target - collected
```

## Dependencies

```
clickhouse-connect  # ClickHouse 연동
httpx              # 비동기 HTTP (또는 aiohttp)
```

- **Rate limiting**: Add `time.sleep()` in collector loop if hitting API limits
