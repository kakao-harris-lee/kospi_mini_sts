#!/bin/bash
# CNN-LSTM Training Script
# Usage: ./train.sh [options]
#
# Modes:
#   ENSEMBLE=true  - Train all 4 horizon models (1, 3, 5, 10 min)
#   ENSEMBLE=false - Train single model (default)
#
# Options are passed directly to train_lstm.py

set -e

EPOCHS=${EPOCHS:-100}
BATCH_SIZE=${BATCH_SIZE:-64}
MODEL_TYPE=${MODEL_TYPE:-cnn-lstm}
ENSEMBLE=${ENSEMBLE:-false}

echo "=== KOSPI CNN-LSTM Training ==="
echo "Model Type: $MODEL_TYPE"
echo "Epochs: $EPOCHS"
echo "Batch Size: $BATCH_SIZE"
echo "Ensemble Mode: $ENSEMBLE"
echo ""

# Check for GPU
python -c "import torch; print(f'CUDA Available: {torch.cuda.is_available()}'); print(f'Device: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"CPU\"}')"
echo ""

if [ "$ENSEMBLE" = "true" ]; then
    echo "=== Training Ensemble Models (4 horizons) ==="

    # Create ensemble directory
    mkdir -p models/ensemble

    # Train each horizon
    for HORIZON in 1 3 5 10; do
        echo ""
        echo "=== Training H=$HORIZON model ==="
        python scripts/training/train_lstm.py \
            --data-source csv \
            --csv-path data/kospi_mini_1m.csv \
            --output "models/ensemble/model_h${HORIZON}.pth" \
            --model-type "$MODEL_TYPE" \
            --horizon "$HORIZON" \
            --epochs "$EPOCHS" \
            --batch-size "$BATCH_SIZE" \
            --device cuda \
            "$@"
        echo "H=$HORIZON complete: models/ensemble/model_h${HORIZON}.pth"
    done

    # Copy scaler from last model (all models use same scaler)
    if [ -f "models/ensemble/model_h10.json" ]; then
        echo ""
        echo "Ensemble training complete!"
        echo "Models saved to: models/ensemble/"
        ls -la models/ensemble/
    fi
else
    # Single model training
    python scripts/training/train_lstm.py \
        --data-source csv \
        --csv-path data/kospi_mini_1m.csv \
        --output models/trading_lstm.pth \
        --model-type "$MODEL_TYPE" \
        --epochs "$EPOCHS" \
        --batch-size "$BATCH_SIZE" \
        --device cuda \
        "$@"

    echo ""
    echo "=== Training Complete ==="
    echo "Model saved to: models/trading_lstm.pth"
    echo "Metadata saved to: models/trading_lstm.json"
fi
