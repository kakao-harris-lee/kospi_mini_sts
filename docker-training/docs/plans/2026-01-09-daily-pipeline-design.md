# Daily Data Collection Pipeline Design

**Date**: 2026-01-09
**Status**: Approved
**Author**: Claude + User collaboration

## Overview

Automated daily pipeline that extracts KOSPI 200 Futures data from ClickHouse, applies roll adjustments for contract continuity, and retrains the CNN-LSTM triple barrier model.

## Pipeline Flow

```
ClickHouse (kospi.kospi200f_1m)
        │
        ▼
[1] export_clickhouse.py ──► kospi200f_1m_raw_{timestamp}.csv
        │
        ▼
[2] adjust_rolls.py ──► kospi200f_1m_adjusted_{timestamp}.csv
        │
        ▼
[3] train_triple_barrier.py ──► models/triple_barrier/model.pth
```

## Schedule

| Component | Cron | Time | Description |
|-----------|------|------|-------------|
| Data backfill | `0 16 * * 1-5` | 16:00 | Fetch new 1m candles from KIS API |
| Training pipeline | `0 17 * * 1-5` | 17:00 | Export, adjust, retrain |

## Instruments

| Instrument | KIS Code | Purpose |
|------------|----------|---------|
| KOSPI 200 Futures (full) | `101S6000` | Training data (primary) |
| KOSPI 200 Mini Futures | `A05601` | Live trading (existing) |

## Components

### 1. Data Export (`scripts/export_clickhouse.py`)

**Purpose**: Query ClickHouse and export to CSV.

**Input**: ClickHouse `kospi.kospi200f_1m` table
**Output**: `data/kospi200f_1m_raw_{timestamp}.csv`

**Features**:
- Full export each run (not incremental)
- Configurable date range filtering
- Environment-based ClickHouse connection

### 2. Roll Adjustment (`scripts/adjust_rolls.py`)

**Purpose**: Apply ratio-based adjustment for futures contract continuity.

**Method**: Ratio-based adjustment
- Detect roll points by price gaps > threshold (default 2%)
- Calculate ratio: `new_price / old_price`
- Multiply all historical prices by cumulative ratio

**Formula**:
```
At roll point i:
  ratio = close[i] / close[i-1]  (when gap detected)

For all bars j < i:
  adjusted[j] = raw[j] * cumulative_ratio
```

**Why ratio-based**:
- Preserves percentage returns: `log(P2/P1)` unchanged
- Better for ML models trained on returns/ratios
- Standard practice for futures backtesting

### 3. Orchestration (`scripts/daily_pipeline.sh`)

**Purpose**: Run export → adjust → train in sequence.

**Error handling**:
- `set -euo pipefail` - stop on any error
- Clear logging at each step
- Symlinks updated only on success

### 4. Cron Wrapper (`cron/cron_daily_training.sh`)

**Purpose**: Cron-friendly wrapper with logging.

**Features**:
- Trading day check (weekends skipped)
- Virtual environment activation
- Daily log files: `logs/daily_training_YYYYMMDD.log`
- Exit code propagation

## File Structure

```
docker-training/
├── scripts/
│   ├── export_clickhouse.py      # NEW: ClickHouse → CSV
│   ├── adjust_rolls.py           # NEW: Roll adjustment
│   ├── daily_pipeline.sh         # NEW: Orchestration
│   └── training/
│       └── train_triple_barrier.py  # Existing
├── cron/
│   └── cron_daily_training.sh    # NEW: Cron wrapper
├── data/
│   ├── kospi200f_1m_raw_*.csv
│   ├── kospi200f_1m_adjusted_*.csv
│   └── *_latest.csv (symlinks)
├── models/
│   └── triple_barrier/
│       ├── model.pth
│       ├── model.json
│       └── scaler.json
└── logs/
    └── daily_training_*.log
```

## Configuration

### Environment Variables

```bash
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8123
CLICKHOUSE_DATABASE=kospi
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=
```

### Training Parameters

From existing `train_triple_barrier.py`:
- Sequence length: 60
- ATR period: 20
- Barrier K: 1.5
- Max horizon: 30
- Epochs: 100
- Batch size: 64

## Prerequisites

1. **ClickHouse table for KOSPI 200 Futures**:
   - Table `kospi.kospi200f_1m` must exist
   - Schema: `code, datetime, open, high, low, close, volume`

2. **Daily backfill must include `101S6000`**:
   - Update `cron_backfill.sh` to fetch full-size futures
   - Ensure data flows to ClickHouse before 17:00

## Deployment

```bash
# 1. Make scripts executable
chmod +x scripts/daily_pipeline.sh
chmod +x cron/cron_daily_training.sh

# 2. Register cron job
(crontab -l 2>/dev/null | grep -v "cron_daily_training"; \
 echo "0 17 * * 1-5 /home/deploy/project/kospi_mini_sts/docker-training/cron/cron_daily_training.sh") | crontab -

# 3. Verify
crontab -l
```

## Manual Execution

```bash
# Run full pipeline manually
cd /home/deploy/project/kospi_mini_sts/docker-training
./scripts/daily_pipeline.sh

# Run individual steps
python scripts/export_clickhouse.py --code 101S6000 --output data/test_raw.csv
python scripts/adjust_rolls.py --input data/test_raw.csv --output data/test_adjusted.csv
```

## Monitoring

- **Logs**: `logs/daily_training_YYYYMMDD.log`
- **Latest data**: Check symlinks in `data/` directory
- **Model freshness**: Check `model.json` timestamp

## Future Enhancements

1. Korean holiday calendar integration
2. Telegram notifications on success/failure
3. Model performance comparison (new vs old)
4. Automatic rollback if new model underperforms
