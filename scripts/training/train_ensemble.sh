#!/bin/bash
# Train ensemble of multi-horizon models
# Trains models for 1, 3, 5, 10 minute prediction horizons

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TRAINING_SCRIPT="$PROJECT_ROOT/docker-training/scripts/training/train_lstm.py"
OUTPUT_DIR="$PROJECT_ROOT/models/ensemble"

# Training parameters
DAYS=14
EPOCHS=50

echo "=========================================="
echo "  Multi-Horizon Ensemble Training"
echo "=========================================="
echo "Output directory: $OUTPUT_DIR"
echo "Training days: $DAYS"
echo "Epochs: $EPOCHS"
echo ""

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Train each horizon
for HORIZON in 1 3 5 10; do
    echo ""
    echo "=========================================="
    echo "  Training Horizon: ${HORIZON} minutes"
    echo "=========================================="

    python3 "$TRAINING_SCRIPT" \
        --days "$DAYS" \
        --horizon "$HORIZON" \
        --epochs "$EPOCHS" \
        --output "$OUTPUT_DIR/model_h${HORIZON}.pth"

    echo "Completed horizon $HORIZON"
done

echo ""
echo "=========================================="
echo "  Ensemble Training Complete!"
echo "=========================================="
echo ""
echo "Models saved to:"
ls -la "$OUTPUT_DIR"
