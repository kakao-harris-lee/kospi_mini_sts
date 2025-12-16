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

### Phase 7 ✅ 완료
**모의투자 & 백테스팅 상시 운영 시스템**

- [x] Result DB (SQLite + SQLAlchemy ORM)
- [x] CLI Tool (Typer 기반)
  - `sts backtest run --strategy hybrid --start 2024-01-01 --end 2024-12-31`
  - `sts paper run --strategy hybrid --duration 1h`
  - `sts runs list/show/compare/delete`
  - `sts report show/export/compare`
- [x] Paper Trading Engine (VirtualBroker)
- [x] Report Generator (TEXT/HTML/JSON/Markdown)

### Phase 8 🔄 진행 중
**마이크로스트럭처 전략 강화 & 프로덕션 배포**

#### 배경
- LSTM 학습 데이터 부족 (현재 2,268 샘플, 1년 이상 필요)
- 분봉 과거 데이터 수집 불가
- 대안: 마이크로스트럭처 시그널 기반 전략으로 전환

#### 목표

**즉시 (Phase 8.1)**: 마이크로스트럭처 전략 강화
- LSTM 의존도 제거
- OFI/호가불균형/스프레드 기반 순수 로지컬 전략

**중기 (Phase 8.2)**: 틱/호가 데이터 수집 시작
- 실시간 호가 데이터 저장 (1일 = 수만 건)
- API Rate Limit 준수 (초당 20건)
- 향후 ML 모델 학습용 데이터 축적

**인프라 (Phase 8.3)**: Docker + 배포 자동화
- Docker Compose (Redis, ClickHouse, App)
- deploy_to_server.sh 스크립트
- 모니터링 대시보드

#### 아키텍처 (Phase 8)
```
┌─────────────────────────────────────────────────────────────────────┐
│                         Production Server                           │
├─────────────────────────────────────────────────────────────────────┤
│  Docker Compose                                                     │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────────┐ │
│  │   Redis     │  │ ClickHouse  │  │  Trading System             │ │
│  │  (Streams)  │  │ (TimeSeries)│  │  ├─ Collector               │ │
│  └─────────────┘  └─────────────┘  │  ├─ Processor               │ │
│                                     │  ├─ Strategy (No LSTM)     │ │
│  ┌─────────────┐  ┌─────────────┐  │  ├─ Tick Collector (NEW)    │ │
│  │  Prometheus │  │   Grafana   │  │  └─ Order Executor         │ │
│  │ (Metrics)   │  │(Dashboard)  │  └─────────────────────────────┘ │
│  └─────────────┘  └─────────────┘                                   │
└─────────────────────────────────────────────────────────────────────┘
```

#### 8.1 마이크로스트럭처 전략 강화

**변경 전 (Hybrid)**:
```
LSTM 예측 (70%) + OFI + 호가불균형 → 2/3 조건 충족 시 진입
```

**변경 후 (PureMicrostructure)**:
```
OFI (강화) + 호가불균형 + 스프레드 + 레짐 → 복합 스코어링
```

**새 전략 파라미터**:
| 시그널 | 가중치 | 임계값 |
|--------|--------|--------|
| OFI | 0.4 | ±2σ 연속 3분 |
| 호가불균형 | 0.3 | > 0.6 or < -0.6 |
| 스프레드 | 0.2 | < 평균 스프레드 |
| 레짐 | 0.1 | LOW → Mean Rev, HIGH → Breakout |

#### 8.2 틱/호가 데이터 수집

**API 제약사항**:
- 한투 API: 초당 20건 제한
- WebSocket: 실시간 호가/체결 수신 (제한 없음)

**저장 스키마**:
```sql
-- 호가 스냅샷 (WebSocket 수신)
CREATE TABLE kospi.orderbook_snapshots (
    code String,
    timestamp DateTime64(3),  -- ms 단위
    bid_price1 Float64,
    bid_qty1 UInt32,
    ask_price1 Float64,
    ask_qty1 UInt32,
    -- ... 5호가까지
    total_bid_qty UInt64,
    total_ask_qty UInt64
) ENGINE = MergeTree()
ORDER BY (code, timestamp);

-- 체결 틱 (WebSocket 수신)
CREATE TABLE kospi.trade_ticks (
    code String,
    timestamp DateTime64(3),
    price Float64,
    volume UInt32,
    side String  -- BUY/SELL
) ENGINE = MergeTree()
ORDER BY (code, timestamp);
```

**예상 데이터량**:
- 호가: 약 1초당 1-2건 → 1일 약 25,000건
- 체결: 변동적, 1일 약 10,000-50,000건
- 1주일 수집 ≈ 분봉 1년치 데이터량

#### 8.3 Docker Compose 구성

```yaml
# docker-compose.yml
services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    volumes: ["redis-data:/data"]

  clickhouse:
    image: clickhouse/clickhouse-server:24.1
    ports: ["8123:8123", "9000:9000"]
    volumes: ["clickhouse-data:/var/lib/clickhouse"]

  trading-app:
    build: ./trading-system
    depends_on: [redis, clickhouse]
    env_file: .env

  prometheus:
    image: prom/prometheus
    volumes: ["./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml"]

  grafana:
    image: grafana/grafana
    ports: ["3000:3000"]
    volumes: ["grafana-data:/var/lib/grafana"]
```

#### 8.4 배포 스크립트

```bash
# deploy_to_server.sh
#!/bin/bash
SERVER="deploy@your-server"
APP_DIR="/home/deploy/kospi-trading"

# 1. 코드 동기화
rsync -avz --exclude '.git' --exclude '__pycache__' \
  ./ $SERVER:$APP_DIR/

# 2. Docker 재시작
ssh $SERVER "cd $APP_DIR && docker-compose down && docker-compose up -d"

# 3. 헬스체크
ssh $SERVER "curl -s localhost:8080/health"
```

#### 8.5 모니터링 메트릭

| 메트릭 | 설명 | 알림 조건 |
|--------|------|-----------|
| `strategy_signals_total` | 생성된 시그널 수 | - |
| `orders_executed_total` | 실행된 주문 수 | - |
| `position_pnl` | 현재 포지션 손익 | < -500,000 |
| `daily_pnl` | 일일 손익 | < -1,000,000 |
| `redis_lag_seconds` | Redis 처리 지연 | > 5초 |
| `api_errors_total` | API 에러 수 | > 10/분 |

---

## Current Status

**현재 단계**: Phase 8 진행 중

**완료된 작업**:
- Phase 1: 데이터 수집 인프라 (min_data/)
- Phase 2: 실시간 파이프라인 (trading-system/)
- Phase 3: WebSocket + 주문 API
- Phase 4: 백테스트 엔진 + 리스크 관리
- Phase 5: 로지컬 전략 구현
- Phase 7: CLI, Paper Trading, 리포트 시스템

**다음 작업 (Phase 8)**:
1. 마이크로스트럭처 전략 강화 (LSTM 제거)
2. 틱/호가 데이터 수집기 구현
3. Docker Compose 구성
4. 배포 스크립트 (deploy_to_server.sh)
5. 모니터링 시스템 (Prometheus + Grafana)
