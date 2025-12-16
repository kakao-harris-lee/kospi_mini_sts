# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

KOSPI Mini 선물 트레이딩 시스템 - 두 개의 주요 서브 프로젝트로 구성:

1. **min_data/**: 한국투자증권 Open API를 통한 1분봉 OHLCV 데이터 수집기
2. **trading-system/**: Redis Streams 기반 실시간 트레이딩 파이프라인

## Commands

### min_data (데이터 수집기)

```bash
cd min_data

# 의존성 설치
pip install -r requirements.txt

# 백필 (과거 데이터 수집)
python collector.py --backfill
python collector.py --backfill --days 180  # 특정 기간

# 오늘 데이터 수집 (장 마감 후)
python collector.py --today

# 데이터 검증
python scripts/validate_data.py
python scripts/validate_data.py --verbose

# API 제한 테스트
python scripts/test_api_limit.py

# DB 마이그레이션 (MergeTree -> ReplacingMergeTree)
python scripts/migrate_to_replacing.py --dry-run
python scripts/migrate_to_replacing.py
```

### trading-system (실시간 트레이딩)

```bash
cd trading-system

# 의존성 설치
pip install -e ".[all]"

# 각 모듈 실행 (별도 터미널)
python -m src.collector.data_collector
python -m src.processor.feature_processor
python -m src.db_logger.db_logger
python -m src.prediction.prediction_engine
python -m src.strategy.strategy_manager

# 테스트
pytest tests/ -v
pytest tests/test_integration.py::TestIntegration::test_collector_publishes_to_stream -v

# 코드 스타일
black src/ tests/
ruff check src/
mypy src/
```

## Architecture

### min_data - 데이터 수집 파이프라인

```
collector.py (Entry Point)
    ├── app/token.py      # OAuth2 토큰 관리 (자동 갱신, 캐싱)
    ├── app/fetch_minute.py   # 한투 API 호출 (비동기)
    ├── app/futures.py    # 선물 코드 생성 (101{월코드}{년도})
    ├── app/calendar.py   # 거래일 관리 (공휴일 처리)
    └── app/db.py         # ClickHouse 배치 삽입
```

### trading-system - 실시간 파이프라인

```
RAW_DATA_STREAM ──► FEATURE_STREAM ──► PREDICTION_STREAM ──► ORDER_COMMAND_STREAM
      │                   │                   │
   Collector          Processor          Prediction
                          │               Engine
                          ▼
                     ClickHouse
                     (DB Logger)
```

**주요 모듈**:
- `src/collector/`: 거래소 API → RAW_DATA_STREAM
- `src/processor/`: OFI, 유동성 점수 계산 → FEATURE_STREAM
- `src/db_logger/`: ClickHouse 배치 삽입 (1000건 단위)
- `src/prediction/`: LSTM 모델 예측 (60분 시퀀스)
- `src/strategy/`: 이원화 전략 (Mode A: 차익거래, Mode B: 추세매매)

## Key Conventions

### 선물 코드 형식
`101{월코드}{년도}` (예: `101M25` = 2025년 6월물)

월 코드: F(1), G(2), H(3), J(4), K(5), M(6), N(7), Q(8), U(9), V(10), X(11), Z(12)

### 토큰 관리
`app/token.py`의 `KoreaInvestToken` 클래스 사용:
- 만료 60초 전 자동 갱신
- 항상 `tok.get()` 호출 (raw token 저장 금지)

### ClickHouse
- 개별 INSERT 금지 - 배치(1000건) 단위 삽입만 사용
- `BatchInserter` 클래스 또는 `db.insert_batch()` 사용

### Redis Streams
- 모든 Consumer는 `XREADGROUP` 사용 (메시지 처리 보장)
- 재시작 시 PEL(Pending Entry List) 먼저 처리
- 멱등성(Idempotency) 고려 필수

### API Rate Limit
- 한투 API: 초당 20건 제한
- `asyncio.Semaphore` 사용, 자동 재시도 구현됨

## Configuration

### min_data
`app/config.py`에서 API 키 및 DB 설정

### trading-system
환경변수 또는 `config/settings.py`:
- `REDIS_HOST`, `REDIS_PORT`
- `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`
- `MODEL_PATH`, `MODEL_DEVICE`
- `DRY_RUN` (기본값: true)

## Testing

```bash
# trading-system 통합 테스트
cd trading-system && pytest tests/ -v

# min_data 데이터 검증
cd min_data && python scripts/validate_data.py --verbose
```

## Implementation Rules

### Git Workflow
- 새로운 기능 구현 시 반드시 feature 브랜치 사용
- 테스트 통과 후 머지

### 금지 사항
- Strategy에서 Feature 계산 금지
- Prediction Engine에서 ClickHouse 쿼리 금지
- Collector에 비즈니스 로직 추가 금지
- DL output을 그대로 주문으로 연결 금지

### 필수 사항
- 모든 Stream은 Consumer Group 사용
- ACK 누락 시 재처리 가능하도록 설계
- Feature Processor에 가장 많은 테스트 작성
- 슬리피지/수수료를 Strategy에서 반영
- 새 기능 구현 시 테스트 코드 필수

## Database Schema

```sql
-- KOSPI Mini 1분봉 데이터
CREATE TABLE kospi.kospi_mini_1m (
    code String,
    datetime DateTime,
    open Float64,
    high Float64,
    low Float64,
    close Float64,
    volume UInt64
) ENGINE = ReplacingMergeTree()
ORDER BY (code, datetime);
```

---

## Development Phases

### Phase 1 ✅ 완료
**데이터 수집 인프라 구축**

- [x] ClickHouse 설정 및 스키마 생성
- [x] 한투 REST API 연동 (OAuth2 토큰 관리)
- [x] 1분봉 OHLCV 데이터 백필
- [x] 거래일 캘린더 관리
- [x] Telegram 알림 연동

### Phase 2 ✅ 완료
**실시간 파이프라인 구축**

- [x] Redis Streams 기반 메시지 파이프라인
- [x] Feature Processor (OFI, 유동성 점수)
- [x] LSTM 모델 학습 스크립트
- [x] Prediction Engine (기술적 지표 10개 feature)
- [x] Strategy Manager (Mode A/B 이원화)
- [x] 통합 테스트 (DryRun 모드)

### Phase 3 🔄 진행 중
**실시간 데이터 수집 및 주문 연동**

- [ ] 한투 실시간 WebSocket 어댑터 연동
  - 호가 수신: `H0IFASP0`
  - 체결 수신: `H0IFCNT0`
- [ ] KOSPI Mini 실시간 호가/체결 수신
- [ ] 한투 주문 API 연동
  - 시장가/지정가 주문
  - 주문 취소/정정

### Phase 4
**백테스팅 및 리스크 관리**

- [ ] 모의투자 백테스팅 시스템
  - 수수료/슬리피지 반영
  - 틱 단위 시뮬레이션
- [ ] 포지션 관리
  - 실시간 포지션 추적
  - 평균 진입가 계산
- [ ] 손절/익절, 리스크 관리
  - Stop Loss / Take Profit
  - 최대 손실 한도
  - 포지션 사이징

#### 리스크 관리 (수익률 80% 결정 요소)
| 항목 | 설명 |
|------|------|
| 포지션 사이즈 | 계좌 대비 최대 노출 비율 |
| 손절 규칙 | 가격 기반 Stop Loss |
| 시간 손절 | N분 경과 시 강제 청산 |

#### 거래하지 않는 규칙 ✅
| 구간 | 이유 |
|------|------|
| 노이즈 구간 | 변동성 낮고 방향성 없음 |
| 점심 시간 (11:30~13:00) | 거래량 급감 |
| 이벤트 직전 | FOMC, 금통위, 옵션만기 등 |

#### 백테스트 엔진 필수 기능
```
┌─────────────────────────────────────────────────────────┐
│  백테스트 엔진 구조                                      │
├─────────────────────────────────────────────────────────┤
│  • 1분 단위 이벤트 루프                                  │
│  • 슬리피지 모델 (틱 단위 보정)                          │
│  • 수수료 모델 (왕복 수수료 반영)                        │
│  • 부분 체결 가정 ❌ → 단일 체결 가정 ⭕                 │
│  • 포지션 상태 머신 (FLAT → LONG/SHORT → FLAT)          │
└─────────────────────────────────────────────────────────┘
```

### Phase 5
**로지컬 전략 구현**

#### 핵심 철학
> **가격을 예측하지 말고, 시장 참가자의 행동을 따라간다 (지연 최소화)**

#### 마이크로스트럭처 시그널
| 시그널 | 설명 | 활용 |
|--------|------|------|
| OFI (Order Flow Imbalance) | 호가 변화 기반 매수/매도 압력 | 단기 방향성 |
| 호가 잔량 변화 | Bid/Ask 수량 변화 | 지지/저항 강도 |
| 체결 속도 | 틱 빈도 변화 | 모멘텀 감지 |
| 스프레드 변화 | Bid-Ask 스프레드 | 유동성 상태 |

#### 변동성 레짐 기반 전략
```
┌─────────────────────────────────────────────────────────┐
│                    변동성 레짐 판단                      │
│                          │                              │
│         Low Vol ─────────┼───────── High Vol            │
│            │             │             │                │
│            ▼             ▼             ▼                │
│     Mean Reversion    No Trade     Breakout            │
│     (역추세 매매)     (관망)       (추세 추종)          │
└─────────────────────────────────────────────────────────┘
```

#### 레짐 판단 지표
- **ATR / HV**: 변동성 수준
- **거래량 증가율**: 시장 활성도
- **Range Expansion**: 가격대 확장 여부

#### 옵션 정보 활용 (고급)
| 지표 | 해석 |
|------|------|
| ATM IV 상승 | 방향성 임박 |
| Skew 변화 | 하방 리스크 증가 |
| Put/Call Ratio | 시장 심리 |

---

## Current Status

**현재 단계**: Phase 3 진행 중

**완료된 작업**:
- Phase 1: 데이터 수집 인프라 (min_data/)
- Phase 2: 실시간 파이프라인 (trading-system/)

**다음 작업**:
- 한투 WebSocket 실제 연동 테스트
- 주문 API 구현
