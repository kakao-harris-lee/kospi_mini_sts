#!/bin/bash
# Redis Streams 상태 확인 스크립트

echo "=========================================="
echo "Redis Streams Status"
echo "=========================================="

echo ""
echo "[RAW_DATA_STREAM]"
redis-cli XINFO STREAM RAW_DATA_STREAM 2>/dev/null || echo "  (not created yet)"

echo ""
echo "[FEATURE_STREAM]"
redis-cli XINFO STREAM FEATURE_STREAM 2>/dev/null || echo "  (not created yet)"

echo ""
echo "[PREDICTION_STREAM]"
redis-cli XINFO STREAM PREDICTION_STREAM 2>/dev/null || echo "  (not created yet)"

echo ""
echo "[ORDER_COMMAND_STREAM]"
redis-cli XINFO STREAM ORDER_COMMAND_STREAM 2>/dev/null || echo "  (not created yet)"

echo ""
echo "=========================================="
echo "Consumer Groups"
echo "=========================================="

for stream in RAW_DATA_STREAM FEATURE_STREAM PREDICTION_STREAM; do
    echo ""
    echo "[$stream Groups]"
    redis-cli XINFO GROUPS $stream 2>/dev/null || echo "  (no groups)"
done

echo ""
echo "=========================================="
echo "Recent Messages (last 3)"
echo "=========================================="

for stream in RAW_DATA_STREAM FEATURE_STREAM PREDICTION_STREAM; do
    echo ""
    echo "[$stream]"
    redis-cli XREVRANGE $stream + - COUNT 3 2>/dev/null || echo "  (empty)"
done
