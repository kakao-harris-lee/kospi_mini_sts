# MODE_B Deep Learning Trend Following Redesign

**Date:** 2025-01-05
**Status:** Approved for Implementation

## Overview

Transform MODE_B from probability-only entry to a complete trend following system with ensemble filters and proper risk management.

## Architecture

```
PREDICTION_STREAM
       │
       ▼
┌──────────────────┐
│  TrendEngine     │
│                  │
│  ┌────────────┐  │     ┌─────────────────┐
│  │ Ensemble   │──┼────▶│ Technical Calc  │
│  │ Filter     │  │     │ • MA(20), MA(60)│
│  └────────────┘  │     │ • Ichimoku Cloud│
│        │         │     │ • ATR(14)       │
│        ▼         │     └─────────────────┘
│  ┌────────────┐  │
│  │ Position   │  │     Tracks: entry_price, entry_time,
│  │ Manager    │  │     stop_price, highest_price, side
│  └────────────┘  │
│        │         │
│        ▼         │
│  ┌────────────┐  │
│  │ Exit       │  │     Checks every tick:
│  │ Manager    │  │     • ATR trailing stop
│  └────────────┘  │     • Time cut (30 min)
└──────────────────┘
       │
       ▼
ORDER_COMMAND_STREAM (Aggressive taker orders)
```

## Components

### New Files

| File | Purpose |
|------|---------|
| `src/strategy/trend/__init__.py` | Module exports |
| `src/strategy/trend/technical_indicators.py` | MA, Ichimoku, ATR calculations |
| `src/strategy/trend/ensemble_filter.py` | Combines DL + MA + Ichimoku |
| `src/strategy/trend/position_manager.py` | Tracks position state, entry, stops |
| `src/strategy/trend/trend_engine.py` | Main engine, orchestrates all components |

### Modified Files

| File | Changes |
|------|---------|
| `config/settings.py` | Add `TrendConfig` dataclass |
| `src/strategy/strategy_manager.py` | Delegate MODE_B to TrendEngine |

## Entry Filters (Ensemble)

All 3 conditions must agree for entry:

### Long Entry
1. **DL Prediction:** P(Up) > 85%
2. **Moving Average:** MA(20) > MA(60)
3. **Ichimoku:** Price > Cloud (both Span A and Span B)

### Short Entry
1. **DL Prediction:** P(Down) > 85%
2. **Moving Average:** MA(20) < MA(60)
3. **Ichimoku:** Price < Cloud (both Span A and Span B)

## Exit Logic

### ATR Trailing Stop
- Initial stop: Entry price - 2×ATR (for long)
- Trail: When price makes new high, move stop up to price - 2×ATR
- Never move stop backwards
- Exit immediately when price <= stop

### Time Cut
- Trigger: 30 minutes after entry
- Condition: Price hasn't moved 0.5×ATR in favorable direction
- Action: Exit immediately (prediction considered failed)

## Technical Indicators

### Moving Average
- Type: Simple Moving Average (SMA)
- Fast period: 20 bars
- Slow period: 60 bars

### Ichimoku Kinko Hyo
- Tenkan-sen: (9-period high + low) / 2
- Kijun-sen: (26-period high + low) / 2
- Senkou Span A: (Tenkan + Kijun) / 2
- Senkou Span B: (52-period high + low) / 2
- Cloud = area between Span A and Span B

### ATR (Average True Range)
- Period: 14 bars
- Used for: Stop loss calculation, time cut threshold

## Order Execution

- **Entry:** Aggressive taker (buy at ask, sell at bid)
- **Exit:** Market order for immediate execution
- **Rationale:** Trend trades need quick fills; missing entries is costly

## Configuration

```python
@dataclass
class TrendConfig:
    # Entry Filters
    dl_threshold: float = 0.85
    ma_fast_period: int = 20
    ma_slow_period: int = 60

    # ATR Settings
    atr_period: int = 14
    atr_stop_multiplier: float = 2.0

    # Time Cut
    time_cut_minutes: int = 30
    time_cut_atr_threshold: float = 0.5

    # Order
    order_size: float = 1.0

    # Warmup
    min_bars_required: int = 60
```

## Decisions Made

| Decision | Choice | Rationale |
|----------|--------|-----------|
| MA Type | SMA(20) vs SMA(60) | Simple, low lag, widely understood |
| Ichimoku | Price > Cloud only | Clear signal, most common interpretation |
| ATR Stop | Trail up only, 2×ATR | Standard trailing stop, prevents giving back gains |
| Time Cut | 30 min, 0.5×ATR move | Accounts for volatility, not arbitrary % |
| Order Type | Aggressive taker | Trend needs quick fills |
