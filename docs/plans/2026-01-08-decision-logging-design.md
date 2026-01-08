# Decision Logging & Analysis System

## Overview

Comprehensive logging and analysis of trading decisions for the dual_mode strategy.

## Goals

1. **Decision Forensics** - Log all factors that led to each decision
2. **Performance Attribution** - Track which factors correlate with wins/losses
3. **Real-time Transparency** - Detailed Telegram messages for trade signals

## Data Model

```python
@dataclass
class DecisionLog:
    # Core
    timestamp: datetime
    signal: str           # BUY, SELL, CLOSE
    price: float
    mode: str             # MODE_A, MODE_B
    reason: str

    # DL Probabilities
    up_prob_h1: float
    up_prob_h3: float
    up_prob_h5: float
    up_prob_h10: float

    # Z-scores (calibrated)
    zscore_h1: float
    zscore_h10: float

    # Technical
    ma_fast: float
    ma_slow: float
    ma_bullish: bool
    rsi: float
    atr: float
    cloud_top: float
    cloud_bottom: float
    above_cloud: bool

    # Market context
    spread: float
    ofi_zscore: float
    regime: str

    # Position tracking (filled after close)
    entry_price: Optional[float]
    exit_price: Optional[float]
    pnl_points: Optional[float]
    hold_time_minutes: Optional[int]
```

## Storage

- **ClickHouse**: `kospi.trading_decisions` table
- **JSON**: `logs/decisions/YYYY-MM-DD.jsonl` daily files

## Telegram Throttling

| Event | Send Telegram | Log to Storage |
|-------|---------------|----------------|
| Position OPEN | Yes | Yes |
| Position CLOSE | Yes (with PnL) | Yes |
| Mode change | Yes (60s cooldown) | Yes |
| HOLD decisions | No | No |
| Rejected signals | No | Yes |

Additional: Max 1 message per 30 seconds, daily summary at close.

## ClickHouse Schema

```sql
CREATE TABLE kospi.trading_decisions (
    timestamp DateTime64(3),
    signal String,
    price Float64,
    mode String,
    reason String,
    up_prob_h1 Float32,
    up_prob_h3 Float32,
    up_prob_h5 Float32,
    up_prob_h10 Float32,
    zscore_h1 Float32,
    zscore_h10 Float32,
    ma_fast Float64,
    ma_slow Float64,
    ma_bullish UInt8,
    rsi Float32,
    atr Float32,
    cloud_top Float64,
    cloud_bottom Float64,
    above_cloud UInt8,
    spread Float32,
    ofi_zscore Float32,
    regime String,
    trade_id String,
    entry_price Float64,
    exit_price Float64,
    pnl_points Float32,
    hold_time_minutes UInt16,
    strategy String DEFAULT 'dual_mode',
    session_id String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (timestamp, trade_id)
```

## Implementation Files

- `src/common/decision_logger.py` - DecisionLogger class
- `logs/decisions/` - Daily JSONL files
- ClickHouse table creation script
