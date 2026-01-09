# CLAUDE.md

KOSPI Mini Futures Trading System.

## 1. Project Overview

**System:** Real-time Algorithmic Trading System for KOSPI 200 Mini Futures.
**Core Architecture:** Asyncio-based Event-Driven Architecture using Redis Streams.
**Tech Stack:** Python 3.11+, Redis Streams (Msg Broker), ClickHouse (Data Lake), KIS Open API.

## 2. Architecture & Data Flow

### Real-time Pipeline

```
RAW_DATA_STREAM ──► FEATURE_STREAM ──► PREDICTION_STREAM ──► ORDER_COMMAND_STREAM
      │                   │                    │
  [Collector]         [Processor]          [Prediction]
      │                   │                  [Engine]
      ▼                   ▼
 ClickHouse           ClickHouse
 (Raw Data)           (Features)

```

### Key Modules

| Directory | Module | Role |
| --- | --- | --- |
| `src/collector/` | **Data Collector** | Websocket/REST API ingestion (`market:ticks`) |
| `src/processor/` | **Feature Processor** | Calculates OFI, Liquidity Scores (`market:features`) |
| `src/prediction/` | **Prediction Engine** | LSTM Inference / Signal Generation (`signals:detected`) |
| `src/strategy/` | **Strategy Manager** | Execution Logic (Mode A: Arb, Mode B: Trend) |
| `src/db_logger/` | **DB Logger** | Async Batch Insertion into ClickHouse |
| `src/common/` | **Common Utils** | Redis, Tokens, Metrics, Telegram |

---

## 3. Essential Commands

### Setup & Development

```bash
# Install dependencies
pip install -e ".[all]"

# Run Unit/Integration Tests
pytest tests/ -v
pytest tests/test_integration.py -v

# Code Quality
black src/ tests/
ruff check src/
mypy src/

```

### CLI Tool (`sts`) usage

```bash
# Backfilling Data (Caution: KIS API limited to recent 15 days)
sts backfill run --days 15      # Fetch recent data
sts backfill today              # Fetch today's 1m candles
sts backfill status             # Check data continuity

# Backtesting
sts backtest run --strategy pure_micro --start 2024-01-01
sts backtest quick --days 30 --strategy pure_micro

# Paper Trading / Live Ops
sts paper run --strategy pure_micro --duration 1h
sts report show <run_id>

```

---

## 4. Critical Conventions

### A. KIS API Futures Codes (Crucial)

This project uses **KIS "Short Codes"** which represent relative maturity positions.

* **`A05601` (Front Month):** The nearest contract. **Primary trading target.**
* **`A05602` (Next Month):** The second nearest contract.
* **Logic:** These codes are dynamic. When `A05601` expires, the underlying asset automatically rolls over to the next contract.
* **Validation:** Always validate if a code is tradeable via API before collecting. **Do not** attempt to collect data for expired contracts (returns empty/error).

### B. KIS API Limitations

* **Rate Limit:** 20 requests/second. (Handled by `asyncio.Semaphore`).
* **1-Minute Data:** Only available for the **last 15 days**. Historical backfill beyond this is impossible via API.
* **Token:** Issued once per minute (max). Stored in file cache (`common/kis_token.py`) with auto-refresh.

### C. Database (ClickHouse)

* **Batch Only:** Never insert single rows. Use `BatchInserter` (buffer size: 1000).
* **Schema:** `kospi.kospi_mini_1m` (primary key: `code`, `datetime`).

### D. Redis Streams

* **Consumer Groups:** All services must use `XREADGROUP`.
* **Reliability:** On startup, always process the **PEL (Pending Entry List)** first.
* **Keys:**
* `market:ticks` (Raw data)
* `market:features` (Computed indicators)
* `orders:requests` (Execution triggers)

---

## 5. Implementation Rules

### Engineering Rules

1. **Async Only:** Never use `time.sleep()`; always use `await asyncio.sleep()`.
2. **Safety:** Wrap KIS API calls in `try/except`. Handle rate limits gracefully.
3. **Testing:** New features must have unit tests (`pytest tests/`). Use `scripts/test_e2e.py` for sanity checks.

### Coding Standards

1. **KIS API References:** Always refer to the [KIS Official Python Sample](https://github.com/koreainvestment/open-trading-api) first.
2. **Strict Separation of Concerns:**

* **Collector:** No business logic. Just ingest.
* **Strategy:** No feature calculation. Use the input stream.
* **Prediction:** No direct DB queries.

1. **Safety:**

* Never connect DL output directly to orders without a logic filter.
* Slippage and Fees must be accounted for in Strategies.

### Server & Deployment

**Deployment Rules:**

* **Automatic server deployment is prohibited.**
* **SSH/rsync bulk file transfers are prohibited.**
* **Source code sync must be done via `git pull` on the target server.**
* **Always run and test on local machine first. Server configuration is handled manually by user.**

**Server Info:**

* **Paths:** `/home/deploy/project/kospi_mini_sts/`
* **Services:** Redis (6379), ClickHouse (9000/8123).

**Deployment Process:**

```bash
# 전체 배포 (push → pull → deps)
./deploy/deploy.sh deploy

# 개별 명령
./deploy/deploy.sh push         # 로컬에서 push만
./deploy/deploy.sh pull         # 서버에서 pull만
./deploy/deploy.sh setup        # 초기 설정 (git clone + venv + cron)
./deploy/deploy.sh status       # 상태 확인 (git, cron, 프로세스)
./deploy/deploy.sh logs         # 최근 로그 확인
```

### Automation (Crontab-based)

All services run via **crontab**, not systemd. The deploy script manages cron registration.

| Schedule | Script | Description |
|----------|--------|-------------|
| `50 8 * * 1-5` | `cron_paper_trading.sh` | Paper trading (09:00-15:45) |
| `0 16 * * 1-5` | `cron_backfill.sh` | Daily 1m candle backfill |

* Scripts check for trading days (holidays/weekends skipped).
* Logs: `logs/paper_trading_YYYYMMDD.log`, `logs/backfill_YYYYMMDD.log`

---

## 6. Active Strategies

| Strategy ID | Description | Status |
| --- | --- | --- |
| **`pure_micro`** | **Pure Microstructure.** Uses Order Flow Imbalance (OFI) + Orderbook Imbalance. | **Recommended** |
| `adaptive_micro` | Adaptive Microstructure. Adjusts thresholds dynamically. | Active |
| `mean_reversion` | Bollinger Band logic. Good for low volatility. | Active |
| `breakout` | High/Low breakout. Good for high volatility. | Active |
| `hybrid` | LSTM + Logic. | *Deprecated* |

## Active Technologies
- Python 3.11+ + prometheus-client (existing), requests (for Telegram), redis-py, clickhouse-driver, grafana-client (for dashboard provisioning) (001-monitoring-alerting)
- ClickHouse (performance metrics persistence), Redis (alert queue for retry) (001-monitoring-alerting)

## Recent Changes
- 001-monitoring-alerting: Added Python 3.11+ + prometheus-client (existing), requests (for Telegram), redis-py, clickhouse-driver, grafana-client (for dashboard provisioning)
