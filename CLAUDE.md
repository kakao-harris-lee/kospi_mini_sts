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

### Phase 3 ✅ 완료
**실시간 데이터 수집 및 주문 연동**

- [x] 한투 실시간 WebSocket 어댑터 연동
  - 호가 수신: `H0IFASP0`
  - 체결 수신: `H0IFCNT0`
- [x] KOSPI Mini 실시간 호가/체결 수신
- [x] 한투 주문 API 연동 (`src/collector/kis_order.py`)
  - 시장가/지정가 주문
  - 주문 취소/정정

### Phase 4 ✅ 완료
**백테스팅 및 리스크 관리**

- [x] 백테스트 엔진 (`src/backtest/engine.py`)
  - 1분 단위 이벤트 루프
  - 슬리피지/수수료 모델
- [x] 포지션 관리 (`src/backtest/position.py`)
  - 상태 머신: FLAT → LONG/SHORT → FLAT
  - 거래 통계 (승률, Profit Factor)
- [x] 리스크 관리 (`src/backtest/risk.py`)
  - Stop Loss / Take Profit
  - 시간 손절, 트레일링 스탑
  - 일일 최대 손실/거래 횟수 제한
- [x] 거래 시간 필터 (`src/backtest/filters.py`)

### Phase 5 ✅ 완료
**로지컬 전략 구현**

- [x] 전략 베이스 클래스 (`src/strategy/base.py`)
- [x] 변동성 레짐 판단기 (`src/strategy/regime_detector.py`)
- [x] 마이크로스트럭처 시그널 (`src/strategy/signals/`)
- [x] 전략 구현 (`src/strategy/strategies/`)
  - MeanReversionStrategy (저변동성 역추세)
  - BreakoutStrategy (고변동성 돌파)
  - OFIMomentumStrategy (OFI 모멘텀)
  - HybridStrategy (LSTM + 로지컬)

### Phase 7 🔄 진행 중
**모의투자 & 백테스팅 상시 운영 시스템**

#### 목표
- CLI 기반 백테스트/모의투자 실행
- 성과 기록 및 리포트 자동 생성
- 스케줄러를 통한 자동 실행

#### 아키텍처
```
┌─────────────────────────────────────────────────────────────┐
│  CLI Tool ──► Execution Engine ──► Result DB (SQLite)      │
│      │              │                    │                 │
│      │              ├── Backtest Runner  │                 │
│      │              ├── Paper Trading    │                 │
│      │              └── Live Trading     │                 │
│      │                                   │                 │
│      └───────────► Report Generator ◄────┘                 │
└─────────────────────────────────────────────────────────────┘
```

#### 파일 구조
```
trading-system/
├── cli/                      # CLI 도구
│   ├── main.py              # 진입점 (Typer)
│   └── commands/            # 명령어 모듈
├── paper_trading/            # 모의투자 엔진
│   ├── engine.py
│   └── virtual_broker.py
├── reporting/                # 리포트 생성
│   └── generator.py
├── database/                 # Result DB
│   ├── models.py            # SQLAlchemy 모델
│   └── repository.py        # 데이터 접근 레이어
└── scheduler/                # 자동 실행
    └── jobs.py
```

#### CLI 명령어
```bash
# 백테스트
python -m cli backtest --strategy MeanReversion --days 30
python -m cli backtest --strategy all --output html

# 모의투자
python -m cli paper --strategy Hybrid --duration 1d

# 리포트
python -m cli report --run-id <uuid>
python -m cli compare --strategies MeanReversion,Breakout
```

#### Result DB 스키마 (SQLite)
```sql
-- 실행 기록
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    strategy TEXT,
    mode TEXT,  -- backtest, paper, live
    start_date TEXT,
    end_date TEXT,
    config TEXT,  -- JSON
    created_at TEXT
);

-- 거래 기록
CREATE TABLE trades (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    timestamp TEXT,
    side TEXT,
    price REAL,
    quantity INTEGER,
    pnl REAL,
    exit_reason TEXT
);

-- 일별 성과
CREATE TABLE daily_metrics (
    id INTEGER PRIMARY KEY,
    run_id TEXT,
    date TEXT,
    pnl REAL,
    trades INTEGER,
    win_rate REAL,
    max_drawdown REAL
);
```

#### 스케줄 작업
| 시간 | 작업 |
|------|------|
| 매일 16:00 | 당일 데이터 백필 + 30일 백테스트 |
| 매주 토요일 | 전체 기간 백테스트 + 주간 리포트 |
| 장중 (09:00~15:45) | Paper Trading 실행 |

---

## Current Status

**현재 단계**: Phase 7 진행 중

**완료된 작업**:
- Phase 1: 데이터 수집 인프라 (min_data/)
- Phase 2: 실시간 파이프라인 (trading-system/)
- Phase 3: WebSocket + 주문 API
- Phase 4: 백테스트 엔진 + 리스크 관리
- Phase 5: 로지컬 전략 구현

**다음 작업**:
- CLI Tool 구현 (Typer)
- Result DB (SQLite) 구현
- Paper Trading 엔진 구현
