# KOSPI Mini 선물 데이터 수집기 가이드

한국투자증권 Open API를 이용한 KOSPI Mini 선물 분봉 데이터 수집 프로젝트입니다.

## 목차

1. [프로젝트 개요](#1-프로젝트-개요)
2. [설치 및 설정](#2-설치-및-설정)
3. [선물 코드 체계](#3-선물-코드-체계)
4. [데이터 수집 실행](#4-데이터-수집-실행)
5. [유틸리티 스크립트](#5-유틸리티-스크립트)
6. [문제 해결](#6-문제-해결)

---

## 1. 프로젝트 개요

### 목적
- 딥러닝 학습을 위한 KOSPI Mini 선물 과거 분봉(1분) 데이터 수집
- ClickHouse DB에 시계열 데이터 저장

### 구조
```
min_data/
├── collector.py          # 메인 수집기
├── app/
│   ├── config.py         # API/DB 설정
│   ├── token.py          # OAuth2 토큰 관리
│   ├── fetch_minute.py   # API 호출
│   ├── calendar.py       # 거래일 관리
│   ├── futures.py        # 선물 코드 생성
│   └── db.py             # ClickHouse 연동
├── scripts/
│   ├── test_api_limit.py      # API 제한 테스트
│   ├── validate_data.py       # 데이터 검증
│   └── migrate_to_replacing.py # DB 마이그레이션
└── docs/
    └── GUIDE.md          # 이 문서
```

---

## 2. 설치 및 설정

### 2.1 의존성 설치

```bash
pip install -r requirements.txt
```

필요 패키지:
- `clickhouse-connect`: ClickHouse 클라이언트
- `httpx`: 비동기 HTTP 클라이언트
- `requests`: 동기 HTTP 클라이언트
- `pytz`: 타임존 처리

### 2.2 API 키 설정

`app/config.py` 파일에서 한국투자증권 API 키를 설정합니다:

```python
KI_API_BASE = "https://openapi.koreainvestment.com:9443"
KI_APP_KEY = "발급받은_APP_KEY"
KI_APP_SECRET = "발급받은_APP_SECRET"
```

### 2.3 ClickHouse 설정

```python
CLICKHOUSE_HOST = "호스트_주소"
CLICKHOUSE_PORT = 8123  # HTTP 포트
CLICKHOUSE_DATABASE = "kospi"
CLICKHOUSE_USER = ""
CLICKHOUSE_PASSWORD = ""
```

---

## 3. 선물 코드 체계

### 3.1 코드 형식

KOSPI Mini 선물 코드는 다음 형식을 따릅니다:

```
101{월코드}{년도}
```

예: `101M25` = 2025년 6월물

### 3.2 월 코드표

| 월 | 코드 | 월 | 코드 |
|----|-----|----|----|
| 1월 | F | 7월 | N |
| 2월 | G | 8월 | Q |
| 3월 | H | 9월 | U |
| 4월 | J | 10월 | V |
| 5월 | K | 11월 | X |
| 6월 | M | 12월 | Z |

### 3.3 상장 및 만기

- **상장 기간**: 연속 6개 월물 동시 상장
- **만기일**: 매월 둘째 주 목요일

예시:
```
2025년 6월물 (101M25)
- 만기일: 2025-06-12 (6월 둘째 목요일)
- 상장 시작: 약 2024년 12월 (만기 6개월 전)
```

---

## 4. 데이터 수집 실행

### 4.1 백필 (과거 데이터 수집)

```bash
# 최근 1년 데이터 수집 (기본값)
python collector.py --backfill

# 특정 기간 지정
python collector.py --backfill --days 180
```

### 4.2 오늘 데이터 수집

```bash
python collector.py --today
```

### 4.3 정기 수집 (cron 설정 예시)

```cron
# 매일 16:00에 오늘 데이터 수집
0 16 * * 1-5 cd /path/to/min_data && python collector.py --today
```

---

## 5. 유틸리티 스크립트

### 5.1 API 과거 데이터 제한 테스트

API가 얼마나 오래된 데이터까지 제공하는지 확인:

```bash
python scripts/test_api_limit.py
```

출력 예시:
```
테스트 중: 101Z25 / 20251206 (7일 전)... OK (데이터 390건)
테스트 중: 101Z25 / 20251113 (30일 전)... OK (데이터 390건)
...
추정 API 제한: 최소 365일 전까지 조회 가능
```

### 5.2 데이터 완결성 검증

수집된 데이터의 누락 여부 확인:

```bash
# 기본 (1년)
python scripts/validate_data.py

# 상세 출력
python scripts/validate_data.py --verbose

# 특정 기간
python scripts/validate_data.py --days 180
```

### 5.3 테이블 마이그레이션

기존 MergeTree 테이블을 ReplacingMergeTree로 변환:

```bash
# 예상 결과 확인 (실제 실행 안 함)
python scripts/migrate_to_replacing.py --dry-run

# 실제 마이그레이션
python scripts/migrate_to_replacing.py
```

---

## 6. 문제 해결

### 6.1 토큰 오류

**증상**: `401 Unauthorized` 또는 토큰 관련 오류

**해결**:
1. `app/.token_cache.json` 삭제
2. API 키가 올바른지 확인
3. 한국투자증권 API 접근 권한 확인

### 6.2 Rate Limit 오류

**증상**: `EGW00133` 오류 코드

**해결**:
- 자동으로 60초 대기 후 재시도됨
- `app/config.py`에서 `API_RATE_LIMIT` 조정

### 6.3 데이터 누락

**증상**: 특정 날짜/월물의 데이터가 없음

**원인 및 해결**:
1. **API 제한**: `test_api_limit.py`로 API 제한 확인
2. **거래일 아님**: 공휴일은 데이터 없음
3. **월물 미상장**: 만기 6개월 전부터만 데이터 존재

### 6.4 중복 데이터

**증상**: 동일 시간의 데이터가 여러 행 존재

**해결**:
```bash
# 마이그레이션으로 중복 제거
python scripts/migrate_to_replacing.py
```

또는 ClickHouse에서 직접 최적화:
```sql
OPTIMIZE TABLE kospi.kospi_mini_1m FINAL;
```

### 6.5 공휴일 관련

새 연도 공휴일 추가 방법:

`app/calendar.py`의 `KOREAN_HOLIDAYS` set에 추가:
```python
# 2027년 예시
date(2027, 1, 1),   # 신정
date(2027, 2, 6),   # 설날 (음력 확인 필요)
...
```

---

## 데이터베이스 스키마

```sql
CREATE TABLE kospi.kospi_mini_1m (
    code String,           -- 선물 코드 (예: '101M25')
    datetime DateTime,     -- 분봉 시간
    open Float64,          -- 시가
    high Float64,          -- 고가
    low Float64,           -- 저가
    close Float64,         -- 종가
    volume UInt64          -- 거래량
) ENGINE = ReplacingMergeTree()
ORDER BY (code, datetime)
```

---

## 참고 자료

- [한국투자증권 Open API 문서](https://apiportal.koreainvestment.com/)
- [KRX 파생상품 안내](http://www.krx.co.kr/)
- [ClickHouse 문서](https://clickhouse.com/docs)
