#!/bin/bash
#
# Daily Training Pipeline
# Orchestrates: ClickHouse export -> Roll adjustment -> Model training
#
# Usage:
#   ./scripts/daily_pipeline.sh              # Full pipeline
#   ./scripts/daily_pipeline.sh --export     # Export only
#   ./scripts/daily_pipeline.sh --adjust     # Adjust only (requires existing raw data)
#   ./scripts/daily_pipeline.sh --train      # Train only (requires existing adjusted data)
#

set -euo pipefail

# Paths
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="$PROJECT_DIR/data"
MODEL_DIR="$PROJECT_DIR/models/triple_barrier"

# Timestamp for this run
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# File names
RAW_FILE="$DATA_DIR/kospi200f_1m_raw_${TIMESTAMP}.csv"
ADJUSTED_FILE="$DATA_DIR/kospi200f_1m_adjusted_${TIMESTAMP}.csv"
RAW_LATEST="$DATA_DIR/kospi200f_1m_raw_latest.csv"
ADJUSTED_LATEST="$DATA_DIR/kospi200f_1m_adjusted_latest.csv"

# Training parameters
INSTRUMENT_CODE="101S6000"
EPOCHS="${EPOCHS:-100}"
BATCH_SIZE="${BATCH_SIZE:-64}"
DEVICE="${DEVICE:-auto}"

# Parse arguments
RUN_EXPORT=true
RUN_ADJUST=true
RUN_TRAIN=true

if [[ "${1:-}" == "--export" ]]; then
    RUN_ADJUST=false
    RUN_TRAIN=false
elif [[ "${1:-}" == "--adjust" ]]; then
    RUN_EXPORT=false
    RUN_TRAIN=false
    # Use latest raw file
    RAW_FILE="$RAW_LATEST"
elif [[ "${1:-}" == "--train" ]]; then
    RUN_EXPORT=false
    RUN_ADJUST=false
    # Use latest adjusted file
    ADJUSTED_FILE="$ADJUSTED_LATEST"
fi

echo "========================================"
echo "Daily Training Pipeline"
echo "Started: $(date)"
echo "========================================"
echo ""
echo "Configuration:"
echo "  Instrument: $INSTRUMENT_CODE"
echo "  Epochs: $EPOCHS"
echo "  Batch size: $BATCH_SIZE"
echo "  Device: $DEVICE"
echo ""

# Ensure directories exist
mkdir -p "$DATA_DIR"
mkdir -p "$MODEL_DIR"

# Step 1: Export from ClickHouse
if [[ "$RUN_EXPORT" == true ]]; then
    echo "========================================"
    echo "[1/3] Exporting data from ClickHouse"
    echo "========================================"

    python "$SCRIPT_DIR/export_clickhouse.py" \
        --code "$INSTRUMENT_CODE" \
        --output "$RAW_FILE"

    # Update symlink
    ln -sf "$(basename "$RAW_FILE")" "$RAW_LATEST"
    echo "Updated symlink: $RAW_LATEST -> $(basename "$RAW_FILE")"
    echo ""
else
    echo "[1/3] Skipping export (using existing data)"
    echo ""
fi

# Step 2: Apply roll adjustment
if [[ "$RUN_ADJUST" == true ]]; then
    echo "========================================"
    echo "[2/3] Applying roll adjustment"
    echo "========================================"

    # Check input file exists
    if [[ ! -f "$RAW_FILE" ]]; then
        echo "ERROR: Raw data file not found: $RAW_FILE"
        exit 1
    fi

    python "$SCRIPT_DIR/adjust_rolls.py" \
        --input "$RAW_FILE" \
        --output "$ADJUSTED_FILE" \
        --gap-threshold 0.02

    # Update symlink
    ln -sf "$(basename "$ADJUSTED_FILE")" "$ADJUSTED_LATEST"
    echo "Updated symlink: $ADJUSTED_LATEST -> $(basename "$ADJUSTED_FILE")"
    echo ""
else
    echo "[2/3] Skipping adjustment (using existing data)"
    echo ""
fi

# Step 3: Train model
if [[ "$RUN_TRAIN" == true ]]; then
    echo "========================================"
    echo "[3/3] Training CNN-LSTM model"
    echo "========================================"

    # Check input file exists
    if [[ ! -f "$ADJUSTED_FILE" ]]; then
        echo "ERROR: Adjusted data file not found: $ADJUSTED_FILE"
        exit 1
    fi

    python "$SCRIPT_DIR/training/train_triple_barrier.py" \
        --csv-path "$ADJUSTED_FILE" \
        --output "$MODEL_DIR/model.pth" \
        --epochs "$EPOCHS" \
        --batch-size "$BATCH_SIZE" \
        --device "$DEVICE"

    echo ""
else
    echo "[3/3] Skipping training"
    echo ""
fi

echo "========================================"
echo "Pipeline Complete"
echo "Finished: $(date)"
echo "========================================"
echo ""
echo "Output files:"
if [[ "$RUN_EXPORT" == true ]]; then
    echo "  Raw data: $RAW_FILE"
fi
if [[ "$RUN_ADJUST" == true ]]; then
    echo "  Adjusted data: $ADJUSTED_FILE"
fi
if [[ "$RUN_TRAIN" == true ]]; then
    echo "  Model: $MODEL_DIR/model.pth"
    echo "  Metadata: $MODEL_DIR/model.json"
fi
