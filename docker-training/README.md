# Docker Training Package for KOSPI CNN-LSTM

## Quick Start

```bash
# Build the image
docker-compose build

# Train single model (default)
docker-compose up trainer

# Train ensemble models (4 horizons: 1, 3, 5, 10 min)
docker-compose up trainer-ensemble
```

## Output

Single model:
- `models/trading_lstm.pth`
- `models/trading_lstm.json`

Ensemble models:
- `models/ensemble/model_h1.pth` (1-min horizon)
- `models/ensemble/model_h3.pth` (3-min horizon)
- `models/ensemble/model_h5.pth` (5-min horizon)
- `models/ensemble/model_h10.pth` (10-min horizon)

## Configuration

Environment variables (set in docker-compose.yml or command line):
- `EPOCHS`: Training epochs (default: 100)
- `BATCH_SIZE`: Batch size (default: 64)
- `MODEL_TYPE`: `cnn-lstm` or `lstm` (default: cnn-lstm)
- `ENSEMBLE`: `true` for ensemble, `false` for single model

## Manual Training

```bash
# Enter container
docker run -it --gpus all -v $(pwd)/models:/app/models -v $(pwd)/data:/app/data kospi-trainer bash

# Train single model
./train.sh

# Train ensemble
ENSEMBLE=true ./train.sh

# Custom epochs
EPOCHS=50 ENSEMBLE=true ./train.sh
```

## Requirements

- NVIDIA GPU with CUDA 11.8+
- Docker with nvidia-container-toolkit
- Data file: `data/kospi_mini_1m.csv`
