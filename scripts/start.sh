#!/bin/bash
# 전체 시스템 시작 스크립트

set -e

echo "=========================================="
echo "Trading System Startup"
echo "=========================================="

# 1. 인프라 시작
echo "[1/5] Starting infrastructure (Redis, ClickHouse)..."
docker-compose up -d redis clickhouse

# 2. 인프라 준비 대기
echo "[2/5] Waiting for infrastructure..."
sleep 5

# 3. Redis 연결 확인
echo "[3/5] Checking Redis connection..."
redis-cli ping || { echo "Redis connection failed"; exit 1; }

# 4. ClickHouse 연결 확인
echo "[4/5] Checking ClickHouse connection..."
curl -s "http://localhost:8123/ping" || { echo "ClickHouse connection failed"; exit 1; }

echo "[5/5] Infrastructure ready!"
echo ""
echo "=========================================="
echo "Start modules in separate terminals:"
echo "=========================================="
echo ""
echo "# Terminal 1 - Data Collector"
echo "python -m src.collector.data_collector"
echo ""
echo "# Terminal 2 - Feature Processor"
echo "python -m src.processor.feature_processor"
echo ""
echo "# Terminal 3 - DB Logger"
echo "python -m src.db_logger.db_logger"
echo ""
echo "# Terminal 4 - Prediction Engine (GPU 서버)"
echo "python -m src.prediction.prediction_engine"
echo ""
echo "# Terminal 5 - Strategy Manager"
echo "python -m src.strategy.strategy_manager"
echo ""
echo "=========================================="
