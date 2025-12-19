# 🚀 Redis Streams 기반 실시간 트레이딩 시스템

## 📋 목차

1. [시스템 개요](#시스템-개요)
2. [아키텍처](#아키텍처)
3. [Quick Start](#quick-start)
4. [모듈별 상세 가이드](#모듈별-상세-가이드)
5. [개발 지침](#개발-지침)
6. [운영 가이드](#운영-가이드)
7. [트러블슈팅](#트러블슈팅)

---

## 시스템 개요

### 핵심 특징

- **느슨하게 결합된 마이크로서비스**: 각 모듈이 독립적으로 확장 가능
- **Redis Streams 백본**: 실시간 메시지 파이프라인
- **이원화 전략**: Mode A (스나이퍼 차익거래) + Mode B (딥러닝 추세 매매)
- **Failover 지원**: Consumer Group을 통한 메시지 처리 보장

### 기술 스택

| 구성요소 | 기술 |
|---------|------|
| 메시지 브로커 | Redis Streams |
| 데이터 저장소 | ClickHouse |
| 딥러닝 | PyTorch (LSTM) |
| 언어 | Python 3.10+ |

---

## 아키텍처

```
┌─────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   API       │     │  RAW_DATA_      │     │  FEATURE_       │
│  (Exchange) │────▶│  STREAM         │────▶│  STREAM         │
└─────────────┘     └─────────────────┘     └─────────────────┘
                           │                        │
                    ┌──────┴──────┐          ┌──────┴──────┐
                    │  Collector  │          │  Processor  │
                    └─────────────┘          └─────────────┘
                                                    │
                    ┌───────────────────────────────┼───────────────────────────────┐
                    │                               │                               │
                    ▼                               ▼                               ▼
            ┌─────────────┐               ┌─────────────────┐             ┌─────────────┐
            │ ClickHouse  │               │  PREDICTION_    │             │  Strategy   │
            │  (DB Logger)│               │  STREAM         │────────────▶│  Manager    │
            └─────────────┘               └─────────────────┘             └─────────────┘
                                                 │                               │
                                          ┌──────┴──────┐                        ▼
                                          │  Prediction │              ┌─────────────────┐
                                          │  Engine     │              │  ORDER_COMMAND_ │
                                          │  (GPU)      │              │  STREAM         │
                                          └─────────────┘              └─────────────────┘
```

### Redis Streams 구조

| Stream | 목적 | 주요 필드 |
|--------|------|----------|
| `RAW_DATA_STREAM` | API 원본 데이터 | symbol, bid/ask prices, volumes |
| `FEATURE_STREAM` | 전처리된 Feature | ofi_z_score, liquidity_score |
| `PREDICTION_STREAM` | 모델 예측 결과 | up_prob, down_prob |
| `ORDER_COMMAND_STREAM` | 주문 명령 | side, size, price |

---

## Quick Start

### 1. 환경 설정

```bash
# 프로젝트 클론 후 디렉토리 이동
cd trading-system

# 가상환경 생성 및 활성화
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 의존성 설치
pip install -e ".[all]"

# 환경변수 설정
cp .env.example .env
# .env 파일 수정
```

### 2. 인프라 시작

```bash
# Docker로 Redis, ClickHouse 시작
docker-compose up -d

# 상태 확인
docker-compose ps
```

### 3. 모듈 실행 (각 터미널에서)

```bash
# Terminal 1: Data Collector
python -m src.collector.data_collector

# Terminal 2: Feature Processor
python -m src.processor.feature_processor

# Terminal 3: DB Logger
python -m src.db_logger.db_logger

# Terminal 4: Prediction Engine (GPU 서버)
python -m src.prediction.prediction_engine

# Terminal 5: Strategy Manager
python -m src.strategy.strategy_manager
```

### 4. 상태 확인

```bash
# Redis Streams 상태
bash scripts/check_streams.sh

# 테스트 실행
pytest tests/ -v
```

---

## 모듈별 상세 가이드

### 1. Data Collector (`src/collector/`)

**역할**: 거래소 API에서 실시간 데이터를 수신하여 `RAW_DATA_STREAM`에 적재

**핵심 클래스**:

- `DataCollector`: 메인 컨트롤러
- `BaseAPIAdapter`: API 어댑터 인터페이스
- `TickData`: 틱 데이터 표준 구조

**새 거래소 연동 방법**:

```python
from src.collector import BaseAPIAdapter, TickData

class BinanceAdapter(BaseAPIAdapter):
    def connect(self):
        # WebSocket 연결
        pass

    def subscribe(self, symbols, callback):
        # 심볼 구독 및 콜백 등록
        for msg in websocket_messages:
            tick = TickData(
                symbol=msg['s'],
                timestamp=msg['T'] / 1000,
                bid_price_1=msg['b'],
                # ...
            )
            callback(tick)

    def disconnect(self):
        pass
```

### 2. Feature Processor (`src/processor/`)

**역할**: 원본 데이터를 Feature로 변환

**핵심 계산**:

1. **OFI (Order Flow Imbalance)**
   - 호가 변화 기반 매수/매도 압력 측정
   - Z-Score로 정규화

2. **유동성 점수**
   - 스프레드, 호가 잔량, 균형도 종합

3. **캔들 집계**
   - 틱 → 1분봉, 5분봉

**Rolling Window**:

```python
# Feature Processor가 Redis에 최근 60개 Feature 유지
# Prediction Engine이 빠르게 조회 가능
key = f"feature_window:{symbol}"
redis.set(key, json.dumps(features), ex=300)
```

### 3. DB Logger (`src/db_logger/`)

**역할**: Feature를 ClickHouse에 배치 삽입

**⚠️ 주의사항**:

- 개별 INSERT 금지 → 배치(1000건) 단위 삽입
- `BatchInserter` 클래스 사용

```python
inserter = BatchInserter(BatchConfig(
    table_name="features_1min",
    column_names=[...],
    batch_size=1000,
    flush_interval_sec=1.0
))
inserter.start()
inserter.add(record)  # 자동 배치 처리
```

### 4. Prediction Engine (`src/prediction/`)

**역할**: LSTM 모델로 가격 방향 예측

**시퀀스 구성 (중요)**:

```python
# 방법 1: ClickHouse 쿼리 (지연 높음)
# 방법 2 (권장): Redis Rolling Window 사용
features = redis.get(f"feature_window:{symbol}")
sequence = prepare_sequence(features[-60:])  # 60분 Lookback
```

**모델 입력/출력**:

- 입력: `(batch, 60, 8)` - 60분 시퀀스, 8개 Feature
- 출력: `(batch, 3)` - [Hold, Up, Down] 확률

### 5. Strategy Manager (`src/strategy/`)

**역할**: 이원화 전략 실행

**모드 결정 로직**:

```
┌────────────────────────────────────────────────────────────┐
│                    유동성 < 50?                            │
│                         │                                  │
│              Yes ───────┴───────── No                      │
│               │                     │                      │
│               ▼                     ▼                      │
│          [AVOID]            유동성 > 80 AND                │
│          거래 정지           괴리 > 2.5σ?                  │
│                                    │                       │
│                         Yes ───────┴───────── No           │
│                          │                     │           │
│                          ▼                     ▼           │
│                     [MODE A]              [MODE B]         │
│                   스나이퍼 차익         딥러닝 추세        │
│                   고비중 (5.0)          저비중 (1.0)       │
└────────────────────────────────────────────────────────────┘
```

---

## 개발 지침

### 1. Consumer Group 필수 사용

모든 모듈은 `XREADGROUP`을 사용하여 메시지 처리 보장:

```python
class MyConsumer(StreamConsumer):
    def process_message(self, message: StreamMessage) -> bool:
        # 처리 로직
        return True  # True 반환 시 자동 ACK
```

### 2. 재시작 안전성

- 모듈 다운 시 미처리 메시지(PEL)가 보존됨
- 재시작 시 `_read_pending()` 먼저 실행
- 멱등성(Idempotency) 고려하여 구현

### 3. 에러 처리

```python
def process_message(self, message: StreamMessage) -> bool:
    try:
        # 처리 로직
        return True  # ACK
    except RecoverableError:
        return False  # 재시도 (ACK 안함)
    except FatalError:
        logger.error(f"Fatal: {e}")
        return True  # ACK하고 스킵 (무한 재시도 방지)
```

### 4. 코드 스타일

```bash
# 포맷팅
black src/ tests/

# 린팅
ruff check src/

# 타입 체크
mypy src/
```

### 5. 테스트

```bash
# 전체 테스트
pytest tests/ -v

# 특정 테스트
pytest tests/test_integration.py::TestIntegration::test_collector_publishes_to_stream -v
```

---

## 운영 가이드

### 인프라 요구사항

| 구성요소 | 최소 사양 | 권장 사양 |
|---------|----------|----------|
| Redis | 4GB RAM | 8GB+ RAM, SSD |
| ClickHouse | 8GB RAM, SSD | 32GB+ RAM, NVMe |
| GPU 서버 | GTX 1060 | RTX 3080+ |
| CPU 서버 | 4 Core | 8+ Core |

### 모니터링

1. **Redis Streams**

   ```bash
   # Stream 길이 모니터링
   redis-cli XLEN RAW_DATA_STREAM

   # Consumer Group Lag
   redis-cli XINFO GROUPS FEATURE_STREAM
   ```

2. **ClickHouse**

   ```sql
   -- 최근 데이터 확인
   SELECT * FROM features_1min ORDER BY timestamp DESC LIMIT 10;

   -- 파티션 상태
   SELECT partition, rows FROM system.parts WHERE table = 'features_1min';
   ```

### 백업

```bash
# Redis AOF
redis-cli BGREWRITEAOF

# ClickHouse 백업
clickhouse-client --query="BACKUP TABLE trading_db.features_1min TO Disk('backups', 'features_backup')"
```

---

## 트러블슈팅

### Redis 연결 실패

```bash
# Redis 상태 확인
docker logs trading-redis

# 메모리 확인
redis-cli INFO memory
```

### Consumer Group Lag 증가

원인: 처리 속도 < 수신 속도

해결:

1. Consumer 인스턴스 추가 (같은 그룹)
2. 배치 사이즈 증가
3. 처리 로직 최적화

### Prediction Engine 지연

1. GPU 메모리 확인
2. 배치 추론으로 변경
3. 모델 경량화 (quantization)

### ClickHouse 삽입 실패

```bash
# 연결 확인
curl http://localhost:8123/ping

# 로그 확인
docker logs trading-clickhouse
```

---

## 프로젝트 구조

```
trading-system/
├── config/
│   ├── __init__.py
│   └── settings.py          # 전역 설정
├── src/
│   ├── common/              # 공통 유틸리티
│   │   ├── redis_client.py  # Redis/Stream 관리
│   │   ├── clickhouse_client.py
│   │   └── logging_config.py
│   ├── collector/           # 모듈 1: Data Collector
│   │   └── data_collector.py
│   ├── processor/           # 모듈 2: Feature Processor
│   │   └── feature_processor.py
│   ├── db_logger/           # 모듈 3: DB Logger
│   │   └── db_logger.py
│   ├── prediction/          # 모듈 4: Prediction Engine
│   │   └── prediction_engine.py
│   └── strategy/            # 모듈 5: Strategy Manager
│       └── strategy_manager.py
├── models/                  # 학습된 모델 파일
├── scripts/                 # 운영 스크립트
├── tests/                   # 테스트
├── docker-compose.yml
├── pyproject.toml
├── .env.example
└── README.md
```

---

## 다음 단계

1. **거래소 API 연동**: `BaseAPIAdapter` 구현
2. **모델 학습**: 실제 데이터로 LSTM 학습
3. **백테스팅**: 수수료/슬리피지 포함 시뮬레이션
4. **모니터링 대시보드**: Grafana 연동
5. **알림 시스템**: Slack/Telegram 알림

---
