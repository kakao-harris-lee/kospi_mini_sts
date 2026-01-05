#!/bin/bash
# Train ensemble of CNN-LSTM models for multi-horizon prediction
# Horizons: 1, 3, 5, 10 minutes

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Default parameters
DATA_SOURCE="${DATA_SOURCE:-clickhouse}"
CSV_PATH="${CSV_PATH:-}"
DAYS="${DAYS:-30}"
EPOCHS="${EPOCHS:-100}"
BATCH_SIZE="${BATCH_SIZE:-64}"
DEVICE="${DEVICE:-auto}"
MODEL_TYPE="${MODEL_TYPE:-cnn-lstm}"

# Output directory
OUTPUT_DIR="${PROJECT_ROOT}/models/ensemble"
mkdir -p "$OUTPUT_DIR"

echo "=============================================="
echo " Multi-Horizon Ensemble Training"
echo " Horizons: 1, 3, 5, 10 minutes"
echo "=============================================="
echo "Data source: $DATA_SOURCE"
echo "Epochs: $EPOCHS"
echo "Device: $DEVICE"
echo "Output: $OUTPUT_DIR"
echo ""

# Build common args
COMMON_ARGS="--model-type $MODEL_TYPE --epochs $EPOCHS --batch-size $BATCH_SIZE --device $DEVICE"
if [ "$DATA_SOURCE" = "csv" ] && [ -n "$CSV_PATH" ]; then
    COMMON_ARGS="$COMMON_ARGS --data-source csv --csv-path $CSV_PATH"
else
    COMMON_ARGS="$COMMON_ARGS --data-source clickhouse --days $DAYS"
fi

# Train each horizon
HORIZONS=(1 3 5 10)

for H in "${HORIZONS[@]}"; do
    echo ""
    echo "=============================================="
    echo " Training H=${H} minute model"
    echo "=============================================="

    python "$SCRIPT_DIR/train_lstm.py" \
        $COMMON_ARGS \
        --horizon "$H" \
        --output "$OUTPUT_DIR/model_h${H}.pth"

    echo "H=${H} complete: $OUTPUT_DIR/model_h${H}.pth"
done

# Copy scaler (same for all models since features are shared)
if [ -f "$OUTPUT_DIR/scaler.json" ]; then
    echo ""
    echo "Scaler already exists in ensemble directory"
else
    # Scaler is saved during first training, copy it
    cp "$PROJECT_ROOT/models/scaler.json" "$OUTPUT_DIR/" 2>/dev/null || true
fi

echo ""
echo "=============================================="
echo " Ensemble Training Complete"
echo "=============================================="
echo "Models saved to: $OUTPUT_DIR/"
ls -la "$OUTPUT_DIR/"
