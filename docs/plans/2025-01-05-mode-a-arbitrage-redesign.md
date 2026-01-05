# MODE_A Sniper Arbitrage Redesign

**Date:** 2025-01-05
**Status:** Approved for Implementation

## Overview

Transform MODE_A from ML-dependent hybrid to pure statistical basis arbitrage targeting retail-friendly execution.

## Architecture

```
┌─────────────────┐     ┌──────────────────┐
│ KOSPI200 Index  │────▶│  BasisCalculator │
│ (KIS WebSocket) │     │                  │
└─────────────────┘     │  basis_gap =     │
                        │  futures - fair  │
┌─────────────────┐     │                  │     ┌─────────────────┐
│ Futures Price   │────▶│  fair_value =    │────▶│ ArbitrageEngine │
│ (existing feed) │     │  spot×(1+r×T)-D  │     │   (MODE_A)      │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                                         │
                                                         ▼
                                                 ┌───────────────┐
                                                 │ Entry Filters │
                                                 │ • Spread ≤2   │
                                                 │ • Depth ≥5x   │
                                                 │ • Time OK     │
                                                 │ • Not Blackout│
                                                 └───────────────┘
                                                         │
                                                         ▼
                                                 ┌───────────────┐
                                                 │ Maker Order   │
                                                 │ @ best bid/ask│
                                                 │ timeout: 10s  │
                                                 └───────────────┘
```

## Components

### New Files

| File | Purpose |
|------|---------|
| `src/strategy/arbitrage/__init__.py` | Module exports |
| `src/strategy/arbitrage/basis_calculator.py` | Fair value, basis, z-score |
| `src/strategy/arbitrage/arbitrage_engine.py` | Entry filters, signal logic |
| `src/strategy/arbitrage/order_manager.py` | Maker orders, timeout handling |
| `src/collector/index_collector.py` | KOSPI200 WebSocket subscription |

### Modified Files

| File | Changes |
|------|---------|
| `config/settings.py` | Add `ArbitrageConfig` |
| `src/strategy/strategy_manager.py` | Integrate ArbitrageEngine |
| `src/processor/feature_processor.py` | Ensure depth fields published |

## Entry Filters

All filters must pass for entry:

1. **Time Filter** - Use existing `TradingHoursFilter` (09:15-15:30, excluding lunch)
2. **Dividend Blackout** - No trading 14 days before quarterly expiry (Mar/Jun/Sep/Dec)
3. **Spread Filter** - Enter only when spread ≤ 2 ticks
4. **Depth Filter** - Require bid/ask depth (1-3) ≥ 5× order size
5. **Basis Signal** - Enter only when |basis_zscore| > 2.5σ

## Basis Calculation

```
fair_value = spot_index × (1 + risk_free_rate × days_to_expiry / 365)
basis = futures_price - fair_value
basis_zscore = (basis - rolling_mean) / rolling_std
```

Parameters:
- `risk_free_rate`: 3.5% annual (configurable)
- `rolling_window`: 60 minutes
- `threshold`: 2.5σ

## Signal Direction

- `basis_zscore > +2.5` → Futures overpriced → **SELL** futures
- `basis_zscore < -2.5` → Futures underpriced → **BUY** futures

## Order Execution

- **Order Type**: Limit (maker)
- **Price**: Best bid (for BUY) / Best ask (for SELL)
- **Timeout**: 10 seconds, then cancel
- **No chase**: If not filled, skip opportunity

## Configuration

```python
@dataclass
class ArbitrageConfig:
    max_spread_ticks: int = 2
    depth_multiplier: float = 5.0
    basis_threshold: float = 2.5
    order_size: float = 5.0
    order_timeout_sec: float = 10.0
    risk_free_rate: float = 0.035
    basis_rolling_window: int = 60
    quarterly_blackout_days: int = 14
    index_stream: str = "INDEX_STREAM"
```

## Decisions Made

1. **Spot Index Source**: KIS API Real-time (KOSPI200 index)
2. **Order Management**: Aggressive timeout (10 seconds)
3. **Dividend Protection**: Quarterly only (14 days before Mar/Jun/Sep/Dec)
4. **Time Filter**: Use existing TradingHoursFilter as-is
