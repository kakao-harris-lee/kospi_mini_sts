# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

KOSPI Mini 선물 트레이딩 시스템 - Redis Streams 기반 실시간 트레이딩 파이프라인

## Commands

```bash
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

# 코드 스타일
black src/ tests/
ruff check src/
mypy src/

# CLI 명령어
sts --help
sts backfill run --days 180      # 과거 데이터 수집
sts backfill today               # 오늘 데이터 수집
sts backfill status              # 데이터 현황
sts backtest run --strategy pure_micro --start 2024-01-01
sts paper run --strategy pure_micro --duration 1h
sts runs list
sts report show <run_id>
```

## Architecture

### 실시간 파이프라인

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
  - `data_collector.py`: 실시간 WebSocket 데이터 수집
  - `tick_collector.py`: 틱/호가 데이터 수집
  - `historical/`: 과거 데이터 백필 (1분봉 OHLCV)
- `src/processor/`: OFI, 유동성 점수 계산 → FEATURE_STREAM
- `src/db_logger/`: ClickHouse 배치 삽입 (1000건 단위)
- `src/prediction/`: LSTM 모델 예측 (60분 시퀀스)
- `src/strategy/`: 이원화 전략 (Mode A: 차익거래, Mode B: 추세매매)
- `src/backtest/`: 백테스트 엔진
- `src/cli/`: CLI 명령어 (sts)
- `src/common/`: 공통 유틸리티 (Redis, ClickHouse, Telegram, Metrics)

## Key Conventions

### 선물 코드 형식

**중요**: KIS API와 KRX에서 사용하는 코드 형식이 다릅니다.

| 용도 | 형식 | 예시 | 설명 |
|------|------|------|------|
| **KIS API** | `A056XX` | `A05601`, `A05602` | 한국투자증권 API에서 사용하는 KOSPI Mini 선물 코드 |
| **KRX/내부** | `101{월코드}{년도}` | `101Z24`, `101F25` | 한국거래소 표준 코드 형식 |

**KIS API 선물 코드 (A056XX)**:
- `A05601`: KOSPI Mini 선물 근월물
- `A05602`: KOSPI Mini 선물 차월물
- `A05603` ~ `A05606`: 원월물

**KRX 월물 코드**:
- F(1월), G(2월), H(3월), J(4월), K(5월), M(6월)
- N(7월), Q(8월), U(9월), V(10월), X(11월), Z(12월)
- 예: `101Z24` = 2024년 12월물, `101F25` = 2025년 1월물

**ClickHouse 저장 시**: KIS API 코드(A056XX) 그대로 저장

### 토큰 관리
`src/collector/historical/backfill.py`의 `KISToken` 클래스 사용:
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

환경변수 또는 `config/settings.py`:
- `REDIS_HOST`, `REDIS_PORT`
- `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`
- `KIS_APP_KEY`, `KIS_APP_SECRET`
- `MODEL_PATH`, `MODEL_DEVICE`
- `DRY_RUN` (기본값: true)

## Testing

```bash
pytest tests/ -v
pytest tests/test_integration.py -v
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
- [x] KOSPI Mini 실시간 호가/체결 수신
- [x] 한투 주문 API 연동 (`src/collector/kis_order.py`)

### Phase 4 ✅ 완료
**백테스팅 및 리스크 관리**
- [x] 백테스트 엔진 (`src/backtest/engine.py`)
- [x] 포지션 관리 (`src/backtest/position.py`)
- [x] 리스크 관리 (`src/backtest/risk.py`)
- [x] 거래 시간 필터 (`src/backtest/filters.py`)

### Phase 5 ✅ 완료
**로지컬 전략 구현**
- [x] 전략 베이스 클래스 (`src/strategy/base.py`)
- [x] 변동성 레짐 판단기 (`src/strategy/regime_detector.py`)
- [x] 마이크로스트럭처 시그널 (`src/strategy/signals/`)
- [x] 전략 구현 (`src/strategy/strategies/`)

### Phase 7 ✅ 완료
**모의투자 & 백테스팅 상시 운영 시스템**
- [x] Result DB (SQLite + SQLAlchemy ORM)
- [x] CLI Tool (Typer 기반)
- [x] Paper Trading Engine (VirtualBroker)
- [x] Report Generator (TEXT/HTML/JSON/Markdown)

### Phase 8 ✅ 완료
**마이크로스트럭처 전략 강화 & 프로덕션 배포**
- [x] 8.1: PureMicrostructureStrategy 구현 (LSTM 제거)
- [x] 8.2: 틱/호가 데이터 수집기 (`src/collector/tick_collector.py`)
- [x] 8.3: Prometheus 메트릭 모듈 (`src/common/metrics.py`)
- [x] 8.4: Grafana 대시보드 (`monitoring/grafana/`)
- [x] 8.5: systemd 기반 배포 스크립트 (`deploy/deploy.sh`)

---

## 운영 가이드

### 서버 정보
- **호스트**: `deploy@49.247.171.64`
- **배포 경로**: `/home/deploy/project/kospi_mini_sts/`
- **Redis**: localhost:6379 (비밀번호: @1tidh6ls6ls)
- **ClickHouse**: localhost:8123 (비밀번호: @1tidh6ls6ls)

### 배포

```bash
# 전체 프로젝트 배포 (로컬에서 실행)
rsync -avz --delete \
  --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'venv' --exclude '.env' --exclude '.history' --exclude 'logs' \
  ./ deploy@49.247.171.64:/home/deploy/project/kospi_mini_sts/
```

### 데이터 수집

```bash
# 서버에서 실행
cd /home/deploy/project/kospi_mini_sts
source venv/bin/activate

# 백필 (과거 180일)
sts backfill run --days 180

# 오늘 데이터 수집
sts backfill today

# 데이터 현황 확인
sts backfill status
```

### 백테스트

```bash
# 전략 목록 확인
sts strategies

# 백테스트 실행
sts backtest run --strategy pure_micro --start 2024-01-01 --end 2024-12-31

# 빠른 백테스트 (최근 30일)
sts backtest quick --days 30 --strategy pure_micro

# 결과 확인
sts runs list
sts runs show <run_id>
```

### 모의투자 (Paper Trading)

```bash
# 자동 실행 스크립트 (tick_collector, feature_processor 자동 관리)
python scripts/run_paper_trading.py -s pure_micro -d 1h

# 시뮬레이션 모드 (Redis 없이)
python scripts/run_paper_trading.py --simulation -d 30m

# CLI 직접 사용
sts paper run --strategy pure_micro --duration 1h
```

### Paper Trading 서비스 (데몬 모드)

장 시간에 맞춰 자동으로 거래하고 텔레그램 알림을 보냅니다.

```bash
# 서비스 실행 (데몬 모드 - 매일 반복)
python scripts/paper_trading_service.py

# 오늘만 실행
python scripts/paper_trading_service.py --once

# systemd 서비스로 등록
sudo cp deploy/systemd/paper-trading.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable paper-trading
sudo systemctl start paper-trading
```

### 프로덕션 배포

```bash
cd deploy

# 초기 설정 (최초 1회)
./deploy.sh setup

# 전체 배포
./deploy.sh deploy

# 서비스 제어
./deploy.sh start|stop|restart|status
```

### 사용 가능한 전략

| 전략 | 설명 | 추천 |
|------|------|------|
| `pure_micro` | 순수 마이크로스트럭처 (OFI + 호가불균형) | **추천** |
| `adaptive_micro` | 적응형 마이크로스트럭처 | |
| `mean_reversion` | 볼린저 밴드 평균회귀 | 저변동성 |
| `breakout` | N기간 고저가 돌파 | 고변동성 |
| `ofi_momentum` | OFI 기반 모멘텀 | |
| `hybrid` | LSTM + 로지컬 (deprecated) | |
