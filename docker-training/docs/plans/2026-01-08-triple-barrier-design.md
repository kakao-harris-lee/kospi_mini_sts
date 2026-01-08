# Triple Barrier Labeling Method Design

## Problem Statement

Current regression model collapsed to near-constant output (~0.78 ticks) due to strong upward bias in training data. The model learned to predict average drift rather than conditional patterns, resulting in:
- 100% long-only trades
- ~49% directional accuracy (random)
- No tradeable predictive power

## Solution: Triple Barrier Method

Replace binary/regression targets with volatility-adaptive 3-class classification.

### Label Definitions

| Class | Condition | Meaning |
|-------|-----------|---------|
| 1 (Buy) | Price hits upper barrier first | Volatility breakout, rising |
| 2 (Sell) | Price hits lower barrier first | Volatility breakout, falling |
| 0 (Hold) | Neither barrier hit within max horizon | Sideways/noise |

### Key Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Volatility Measure | ATR (20 bars) | Captures true range including gaps |
| K Multiplier | 1.5 | threshold = ATR × 1.5 |
| Max Horizon | 30 bars | Time limit before forcing Hold |
| Labeling Method | First-touch | Which barrier hit first within horizon |
| Class Imbalance | Class weights | Inverse frequency weighting in loss |

## Implementation Details

### 1. Labeling Logic

```python
def create_triple_barrier_labels(df, atr_period=20, k=1.5, max_horizon=30):
    # Compute ATR
    high_low = df['high'] - df['low']
    high_close = abs(df['high'] - df['close'].shift(1))
    low_close = abs(df['low'] - df['close'].shift(1))
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = true_range.rolling(atr_period).mean()

    # Barriers
    upper_barrier = df['close'] + atr * k
    lower_barrier = df['close'] - atr * k

    # First-touch labeling
    labels = []
    for i in range(len(df) - max_horizon):
        future_highs = df['high'].iloc[i+1:i+1+max_horizon]
        future_lows = df['low'].iloc[i+1:i+1+max_horizon]

        upper_hit = (future_highs >= upper_barrier.iloc[i]).idxmax()
        lower_hit = (future_lows <= lower_barrier.iloc[i]).idxmax()

        # Determine which hit first
        ...
    return labels
```

### 2. Model Architecture

```python
class CNNLSTMClassifier(nn.Module):
    def __init__(self, input_dim, num_classes=3):
        # CNN + LSTM + Attention (unchanged)
        self.fc = nn.Sequential(
            nn.Linear(hidden_dim, 32),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(32, num_classes)  # 3 classes
        )
```

### 3. Features (13 total)

Existing 12 features plus:
- `atr_normalized`: ATR / close price (scale-invariant)

### 4. Training

```python
# Class weights from inverse frequency
class_weights = compute_class_weights(labels)
criterion = nn.CrossEntropyLoss(weight=class_weights)
```

### 5. Inference

```python
def generate_signal(model, sequence, confidence_threshold=0.5):
    probs = F.softmax(model(sequence), dim=-1)
    p_hold, p_buy, p_sell = probs

    if p_buy > confidence_threshold and p_buy > p_sell:
        return "BUY", p_buy
    elif p_sell > confidence_threshold and p_sell > p_buy:
        return "SELL", p_sell
    return "HOLD", p_hold
```

## File Structure

```
docker-training/
├── scripts/training/
│   └── train_triple_barrier.py
├── models/triple_barrier/
│   ├── model.pth
│   ├── model.json
│   └── scaler.json
└── scripts/
    ├── inference_triple_barrier.py
    └── backtest_triple_barrier.py
```

## Expected Outcomes

- Balanced Buy/Sell/Hold distribution (~20%/20%/60%)
- Model learns volatility-relative patterns, not trend bias
- Backtest produces both long and short trades
- Signal quality tied to market volatility conditions

## Implementation Order

1. `train_triple_barrier.py` - Core labeling + training
2. `inference_triple_barrier.py` - Signal generation
3. `backtest_triple_barrier.py` - Historical validation
