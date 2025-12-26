# KOSPI Mini 선물 트레이딩 시스템

Redis Streams 기반 실시간 트레이딩 파이프라인

## 시스템 개요

### 핵심 특징

- **마이크로스트럭처 기반 전략**: OFI, 호가불균형 등 주문흐름 분석
- **Redis Streams 백본**: 실시간 메시지 파이프라인
- **한국투자증권(KIS) API 연동**: 실시간 호가/체결, 모의투자 지원
- **crontab 기반 프로덕션 운영**: 안정적인 서비스 운영

### 기술 스택

| 구성요소 | 기술 |
|---------|------|
| 메시지 브로커 | Redis Streams |
| 데이터 저장소 | ClickHouse |
| 모니터링 | Prometheus + Grafana |
| 배포 | systemd |
| 언어 | Python 3.10+ |

---

## 아키텍처

```
RAW_DATA_STREAM ──► FEATURE_STREAM ──► PREDICTION_STREAM ──► ORDER_COMMAND_STREAM
      │                   │                   │
   Collector          Processor          Prediction
                          │               Engine
                          ▼
                     ClickHouse
                     (DB Logger)
```

### 주요 모듈

| 모듈 | 역할 |
|------|------|
| `src/collector/` | 거래소 API → RAW_DATA_STREAM |
| `src/processor/` | OFI, 유동성 점수 계산 → FEATURE_STREAM |
| `src/db_logger/` | ClickHouse 배치 삽입 |
| `src/prediction/` | 가격 방향 예측 |
| `src/strategy/` | 트레이딩 전략 실행 |
| `src/backtest/` | 백테스트 엔진 |
| `src/cli/` | CLI 명령어 (sts) |

### Redis Streams 구조

| Stream | 목적 | 주요 필드 |
|--------|------|----------|
| `RAW_DATA_STREAM` | API 원본 데이터 | symbol, bid/ask prices, volumes |
| `FEATURE_STREAM` | 전처리된 Feature | ofi_z_score, liquidity_score |
| `PREDICTION_STREAM` | 예측 결과 | up_prob, down_prob |
| `ORDER_COMMAND_STREAM` | 주문 명령 | side, size, price |

---

## Quick Start

### 1. 환경 설정

```bash
# 가상환경 생성 및 활성화
python -m venv venv
source venv/bin/activate

# 의존성 설치
pip install -e ".[all]"

# 환경변수 설정
cp .env.example .env
# .env 파일 수정 (KIS API 키, DB 접속 정보 등)
```

### 2. CLI 명령어 (sts)

```bash
# 도움말
sts --help

# 데이터 수집
sts backfill today               # 오늘 데이터 수집
sts backfill run --days 15       # 최근 15일 수집 (API 제한)
sts backfill status              # 데이터 현황

# 백테스트
sts strategies                   # 전략 목록
sts backtest run --strategy pure_micro --start 2024-01-01
sts backtest quick --days 30     # 최근 30일 빠른 백테스트

# 결과 조회
sts runs list                    # 실행 기록
sts runs show <run_id>           # 상세 결과

# 모의투자
sts paper run --strategy pure_micro --duration 1h
```

### 3. 모듈 개별 실행

```bash
# 각 터미널에서 실행
python -m src.collector.data_collector
python -m src.processor.feature_processor
python -m src.db_logger.db_logger
python -m src.prediction.prediction_engine
python -m src.strategy.strategy_manager
```

---

## 사용 가능한 전략

| 전략 | 설명 | 추천 |
|------|------|------|
| `pure_micro` | 순수 마이크로스트럭처 (OFI + 호가불균형) | 추천 |
| `adaptive_micro` | 적응형 마이크로스트럭처 | |
| `mean_reversion` | 볼린저 밴드 평균회귀 | 저변동성 |
| `breakout` | N기간 고저가 돌파 | 고변동성 |
| `ofi_momentum` | OFI 기반 모멘텀 | |

---

## 백테스트

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

---

## 모의투자 (Paper Trading)

### 수동 실행

```bash
# 자동 실행 스크립트 (tick_collector, feature_processor 자동 관리)
python scripts/run_paper_trading.py -s pure_micro -d 1h

# 시뮬레이션 모드 (Redis 없이)
python scripts/run_paper_trading.py --simulation -d 30m

# CLI 직접 사용
sts paper run --strategy pure_micro --duration 1h
```

### 자동 실행 (crontab 기반)

장 시간에 맞춰 자동으로 거래하고 텔레그램 알림을 보냅니다.

```bash
# 수동 실행 (오늘만)
python scripts/paper_trading_service.py --once

# 선물 정보 확인
python scripts/paper_trading_service.py --show-futures

# 텔레그램 알림 테스트
python scripts/paper_trading_service.py --test-notification
```

---

## 서버 배포

### 서버 정보

- **호스트**: `deploy@chsvr.duckdns.org`
- **배포 경로**: `/home/deploy/project/kospi_mini_sts/`

### 배포 명령어

```bash
# 전체 프로젝트 배포 (로컬에서 실행)
rsync -avz --delete \
  --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'venv' --exclude '.env' --exclude '.history' --exclude 'logs' \
  ./ deploy@chsvr.duckdns.org:/home/deploy/project/kospi_mini_sts/

# 또는 배포 스크립트 사용
cd deploy
./deploy.sh setup    # 초기 설정 (최초 1회)
./deploy.sh deploy   # 전체 배포
./deploy.sh start    # 서비스 시작
./deploy.sh stop     # 서비스 중지
./deploy.sh status   # 상태 확인
```

---

## 개발

### 코드 스타일

```bash
# 포맷팅
black src/ tests/

# 린팅
ruff check src/

# 타입 체크
mypy src/
```

### 테스트

```bash
pytest tests/ -v
pytest tests/test_integration.py -v
```

### 주요 제약사항

1. **KIS API 1분봉 데이터**: 최근 15일만 조회 가능
2. **API 호출 제한**: 초당 20건
3. **토큰 발급 제한**: 1분당 1회
4. **ClickHouse**: 개별 INSERT 금지, 배치(1000건) 단위만 사용

---

## 프로젝트 구조

```
kospi_mini_sts/
├── config/
│   └── settings.py          # 전역 설정
├── src/
│   ├── common/              # 공통 유틸리티
│   │   ├── redis_client.py
│   │   ├── clickhouse_client.py
│   │   ├── kis_token.py     # KIS API 토큰 관리
│   │   ├── telegram.py      # 텔레그램 알림
│   │   └── metrics.py       # Prometheus 메트릭
│   ├── collector/           # 데이터 수집
│   │   ├── data_collector.py
│   │   ├── tick_collector.py
│   │   └── historical/      # 과거 데이터 백필
│   ├── processor/           # Feature 처리
│   ├── db_logger/           # ClickHouse 로거
│   ├── prediction/          # 예측 엔진
│   ├── strategy/            # 전략
│   │   ├── strategies/      # 전략 구현체
│   │   └── signals/         # 시그널 생성기
│   ├── backtest/            # 백테스트 엔진
│   └── cli/                 # CLI 명령어
├── scripts/                 # 운영 스크립트
├── deploy/                  # 배포 스크립트
├── monitoring/              # Grafana 대시보드
├── tests/                   # 테스트
├── pyproject.toml
└── CLAUDE.md               # 상세 개발 가이드
```

---

## 상세 문서

개발 규칙, API 제약사항, 선물 코드 형식 등 상세 정보는 [CLAUDE.md](./CLAUDE.md)를 참조하세요.
