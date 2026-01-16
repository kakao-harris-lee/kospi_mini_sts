"""
Triple Barrier Labeling Method for KOSPI Mini Futures

Replaces regression targets with volatility-adaptive 3-class classification:
- Class 0 (Hold): Neither barrier hit within max horizon
- Class 1 (Buy): Price hits upper barrier first
- Class 2 (Sell): Price hits lower barrier first

This addresses the issue of regression models collapsing to mean prediction
by creating balanced, volatility-aware labels.
"""
import os
import sys
import argparse
import json
from datetime import datetime
from pathlib import Path
from collections import Counter

import numpy as np
import pandas as pd

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from sklearn.preprocessing import StandardScaler


# ============================================================
# Focal Loss for Class Imbalance
# ============================================================

class FocalLoss(nn.Module):
    """
    Focal Loss for handling class imbalance.

    FL(p_t) = -α_t * (1 - p_t)^γ * log(p_t)

    - Reduces loss for well-classified examples (easy)
    - Focuses training on hard, misclassified examples
    - gamma=2.0 is commonly used
    """
    def __init__(self, alpha: torch.Tensor = None, gamma: float = 2.0, reduction: str = 'mean'):
        super().__init__()
        self.alpha = alpha  # Class weights
        self.gamma = gamma  # Focusing parameter
        self.reduction = reduction

    def forward(self, inputs: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
        ce_loss = F.cross_entropy(inputs, targets, weight=self.alpha, reduction='none')
        pt = torch.exp(-ce_loss)  # p_t = probability of correct class
        focal_loss = ((1 - pt) ** self.gamma) * ce_loss

        if self.reduction == 'mean':
            return focal_loss.mean()
        elif self.reduction == 'sum':
            return focal_loss.sum()
        return focal_loss


# ============================================================
# Data Loading
# ============================================================

def load_data_from_csv(filepath: str) -> pd.DataFrame:
    """Load data from CSV file"""
    df = pd.read_csv(filepath, parse_dates=['datetime'])
    print(f"Loaded {len(df)} rows from {filepath}")
    return df


# ============================================================
# Feature Engineering
# ============================================================

def compute_atr(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """
    Compute Average True Range (ATR).

    ATR captures volatility including gaps, making it ideal for
    setting adaptive barriers.
    """
    high_low = df['high'] - df['low']
    high_close = abs(df['high'] - df['close'].shift(1))
    low_close = abs(df['low'] - df['close'].shift(1))

    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = true_range.rolling(period).mean()

    return atr


def create_features(df: pd.DataFrame, atr_period: int = 20) -> pd.DataFrame:
    """
    Create technical indicators as features.
    Includes ATR-normalized feature for volatility awareness.
    """
    df = df.copy()
    df = df.sort_values('datetime').reset_index(drop=True)

    # 1. Log returns
    df['log_return'] = np.log(df['close'] / df['close'].shift(1))

    # 2. Moving average ratios
    for window in [5, 10, 20]:
        df[f'ma_{window}'] = df['close'].rolling(window=window).mean()
        df[f'ma_ratio_{window}'] = df['close'] / df[f'ma_{window}']

    # 3. RSI (14 periods)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / (loss + 1e-10)
    df['rsi'] = 100 - (100 / (1 + rs))
    df['rsi_normalized'] = (df['rsi'] - 50) / 50  # Normalize to [-1, 1]

    # 4. Bollinger Bands position
    df['bb_mid'] = df['close'].rolling(window=20).mean()
    df['bb_std'] = df['close'].rolling(window=20).std()
    df['bb_upper'] = df['bb_mid'] + 2 * df['bb_std']
    df['bb_lower'] = df['bb_mid'] - 2 * df['bb_std']
    df['bb_position'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'] + 1e-10)

    # 5. Volume ratio
    df['volume_ma'] = df['volume'].rolling(window=20).mean()
    df['volume_ratio'] = df['volume'] / (df['volume_ma'] + 1)

    # 6. Volatility (rolling std of returns)
    df['volatility'] = df['log_return'].rolling(window=20).std()

    # 7. High-Low range ratio
    df['hl_range'] = (df['high'] - df['low']) / df['close']

    # 8. Candle body ratio
    df['candle_body'] = (df['close'] - df['open']) / (df['high'] - df['low'] + 1e-10)

    # 9. Momentum indicators
    df['momentum_5'] = df['close'] / df['close'].shift(5) - 1
    df['momentum_10'] = df['close'] / df['close'].shift(10) - 1

    # 10. Log volatility (stationary - used for triple barrier labeling)
    # This is the rolling std of log returns, which is scale-invariant
    df['log_volatility'] = df['log_return'].rolling(window=atr_period).std()

    # 11. ATR normalized (legacy - kept for compatibility)
    df['atr'] = compute_atr(df, period=atr_period)
    df['atr_normalized'] = df['atr'] / df['close']

    return df


# ============================================================
# Triple Barrier Labeling
# ============================================================

def create_triple_barrier_labels(
    df: pd.DataFrame,
    atr_period: int = 20,
    k: float = 1.5,
    max_horizon: int = 30
) -> np.ndarray:
    """
    Create triple barrier labels using LOG-RETURN based barriers (stationary).

    This ensures stationarity: barriers are defined in log-return space,
    making them scale-invariant across different price levels.

    Args:
        df: DataFrame with OHLC data
        atr_period: Period for volatility calculation
        k: Multiplier for barrier distance (barrier = k * volatility)
        max_horizon: Maximum bars to look ahead before labeling as Hold

    Returns:
        numpy array of labels: 0=Hold, 1=Buy, 2=Sell

    Logic (in log-return space):
        For each bar i:
        - volatility = rolling_std(log_returns, atr_period)
        - threshold = k * volatility  (stationary, scale-invariant)
        - log_return_high[j] = log(high[j] / close[i])
        - log_return_low[j] = log(low[j] / close[i])
        - If log_return_high >= threshold first -> Buy (1)
        - If log_return_low <= -threshold first -> Sell (2)
        - If neither within horizon -> Hold (0)

    Why log returns?
        - 1% move at price 200 = 1% move at price 400
        - Stationary time series (no unit root)
        - Additive property: log(P2/P0) = log(P2/P1) + log(P1/P0)
    """
    df = df.copy()

    # Compute log returns and rolling volatility (stationary measures)
    df['log_return'] = np.log(df['close'] / df['close'].shift(1))
    # IMPORTANT: Shift volatility by 1 to prevent look-ahead bias!
    # At bar i, we should only know volatility up to bar i-1
    df['log_volatility'] = df['log_return'].rolling(window=atr_period).std().shift(1)

    n = len(df)
    labels = np.full(n, -1, dtype=np.int64)  # -1 = invalid/not computed

    closes = df['close'].values
    highs = df['high'].values
    lows = df['low'].values
    log_volatility = df['log_volatility'].values

    valid_count = 0
    threshold_sum = 0.0  # For logging average threshold

    for i in range(n - max_horizon):
        vol = log_volatility[i]

        # Skip if volatility is invalid
        if np.isnan(vol) or vol <= 0:
            continue

        current_close = closes[i]
        log_threshold = k * vol  # Stationary threshold in log space
        threshold_sum += log_threshold

        # Find first touch in log-return space
        upper_touch_idx = None
        lower_touch_idx = None

        for j in range(i + 1, min(i + 1 + max_horizon, n)):
            # Log return from current close to future high/low
            log_return_high = np.log(highs[j] / current_close)
            log_return_low = np.log(lows[j] / current_close)

            # Check upper barrier (positive log return exceeds threshold)
            if upper_touch_idx is None and log_return_high >= log_threshold:
                upper_touch_idx = j

            # Check lower barrier (negative log return exceeds threshold)
            if lower_touch_idx is None and log_return_low <= -log_threshold:
                lower_touch_idx = j

            # Stop early if both found
            if upper_touch_idx is not None and lower_touch_idx is not None:
                break

        # Determine label based on first touch
        if upper_touch_idx is None and lower_touch_idx is None:
            # Neither barrier touched -> Hold
            labels[i] = 0
        elif upper_touch_idx is not None and lower_touch_idx is None:
            # Only upper touched -> Buy
            labels[i] = 1
        elif upper_touch_idx is None and lower_touch_idx is not None:
            # Only lower touched -> Sell
            labels[i] = 2
        else:
            # Both touched - which was first?
            if upper_touch_idx < lower_touch_idx:
                labels[i] = 1  # Buy
            elif lower_touch_idx < upper_touch_idx:
                labels[i] = 2  # Sell
            else:
                # Same bar - use close direction
                if closes[upper_touch_idx] > current_close:
                    labels[i] = 1
                else:
                    labels[i] = 2

        valid_count += 1

    avg_threshold = threshold_sum / valid_count if valid_count > 0 else 0
    avg_threshold_pct = (np.exp(avg_threshold) - 1) * 100  # Convert to percentage

    print(f"Created {valid_count} valid labels out of {n} rows")
    print(f"Average log threshold: {avg_threshold:.4f} (~{avg_threshold_pct:.2f}% price move)")
    return labels


def compute_class_weights(labels: np.ndarray) -> torch.Tensor:
    """
    Compute inverse frequency class weights for balanced training.
    """
    valid_labels = labels[labels >= 0]
    counter = Counter(valid_labels)
    total = len(valid_labels)

    weights = []
    for c in range(3):
        count = counter.get(c, 1)
        weight = total / (3 * count)
        weights.append(weight)

    print(f"Class distribution: Hold={counter.get(0, 0)}, Buy={counter.get(1, 0)}, Sell={counter.get(2, 0)}")
    print(f"Class weights: {weights}")

    return torch.tensor(weights, dtype=torch.float32)


# ============================================================
# Dataset Class
# ============================================================

class TripleBarrierDataset(Dataset):
    """Time series dataset for triple barrier classification"""

    def __init__(
        self,
        features: np.ndarray,
        labels: np.ndarray,
        seq_len: int = 60,
        undersample: bool = False,
        random_seed: int = 42
    ):
        self.seq_len = seq_len
        self.features = features
        self.labels = labels

        # Filter out invalid labels and group by class
        class_indices = {0: [], 1: [], 2: []}  # Hold, Buy, Sell

        for i in range(seq_len, len(features)):
            label_idx = i - 1  # Label at end of sequence
            label = labels[label_idx]
            if label >= 0:
                class_indices[label].append(i)

        # Original distribution
        orig_counts = {c: len(indices) for c, indices in class_indices.items()}
        print(f"  Original distribution: Hold={orig_counts[0]}, Buy={orig_counts[1]}, Sell={orig_counts[2]}")

        if undersample:
            # Undersample to match smallest class
            min_count = min(len(indices) for indices in class_indices.values())
            print(f"  Undersampling to {min_count} samples per class (total: {min_count * 3})")

            np.random.seed(random_seed)
            balanced_indices = []
            for c in range(3):
                sampled = np.random.choice(class_indices[c], size=min_count, replace=False)
                balanced_indices.extend(sampled)

            # Sort to maintain temporal order within batches
            self.valid_indices = sorted(balanced_indices)

            # Final distribution
            final_labels = [labels[i - 1] for i in self.valid_indices]
            final_counts = Counter(final_labels)
            print(f"  Balanced distribution: Hold={final_counts[0]}, Buy={final_counts[1]}, Sell={final_counts[2]}")
        else:
            # Use all valid indices
            self.valid_indices = []
            for indices in class_indices.values():
                self.valid_indices.extend(indices)
            self.valid_indices = sorted(self.valid_indices)

    def __len__(self):
        return len(self.valid_indices)

    def __getitem__(self, idx):
        end_idx = self.valid_indices[idx]
        start_idx = end_idx - self.seq_len

        x = self.features[start_idx:end_idx]
        y = self.labels[end_idx - 1]

        return (
            torch.tensor(x, dtype=torch.float32),
            torch.tensor(y, dtype=torch.long)
        )


# ============================================================
# Model Definition
# ============================================================

class CNNLSTMClassifier(nn.Module):
    """
    CNN-LSTM for triple barrier classification.

    Architecture mirrors the regression model but outputs 3 classes.
    """

    def __init__(
        self,
        input_dim: int,
        num_classes: int = 3,
        hidden_dim: int = 64,
        num_layers: int = 2,
        dropout: float = 0.2,
        cnn_channels: tuple = (32, 64),
        kernel_size: int = 3
    ):
        super().__init__()

        self.num_classes = num_classes

        # CNN Block
        self.cnn = nn.Sequential(
            nn.Conv1d(input_dim, cnn_channels[0], kernel_size=kernel_size, padding=1),
            nn.BatchNorm1d(cnn_channels[0]),
            nn.ReLU(),
            nn.Dropout(dropout),

            nn.Conv1d(cnn_channels[0], cnn_channels[1], kernel_size=kernel_size, padding=1),
            nn.BatchNorm1d(cnn_channels[1]),
            nn.ReLU(),
            nn.Dropout(dropout),

            nn.MaxPool1d(kernel_size=2, stride=2)
        )

        # LSTM
        self.lstm = nn.LSTM(
            cnn_channels[1],
            hidden_dim,
            num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0,
            bidirectional=False
        )

        # Attention
        self.attention = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.Tanh(),
            nn.Linear(hidden_dim // 2, 1),
            nn.Softmax(dim=1)
        )

        # Classification head - outputs num_classes logits
        self.fc = nn.Sequential(
            nn.Linear(hidden_dim, 32),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(32, num_classes)
        )

    def forward(self, x):
        # x: (batch, seq_len, features)

        # CNN: (batch, features, seq_len) -> (batch, cnn_out, seq_len//2)
        x = x.transpose(1, 2)
        x = self.cnn(x)
        x = x.transpose(1, 2)

        # LSTM
        lstm_out, _ = self.lstm(x)

        # Attention
        attn_weights = self.attention(lstm_out)
        context = torch.sum(attn_weights * lstm_out, dim=1)

        # Classification output (logits)
        logits = self.fc(context)
        return logits


# ============================================================
# Training Functions
# ============================================================

def train_epoch(model, loader, criterion, optimizer, device):
    """Train one epoch"""
    model.train()
    total_loss = 0
    correct = 0
    total = 0

    class_correct = [0, 0, 0]
    class_total = [0, 0, 0]

    for batch_x, batch_y in loader:
        batch_x = batch_x.to(device)
        batch_y = batch_y.to(device)

        optimizer.zero_grad()
        logits = model(batch_x)
        loss = criterion(logits, batch_y)
        loss.backward()

        # Gradient clipping
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)

        optimizer.step()

        total_loss += loss.item()

        # Accuracy
        preds = logits.argmax(dim=1)
        correct += (preds == batch_y).sum().item()
        total += batch_y.size(0)

        # Per-class accuracy
        for c in range(3):
            mask = batch_y == c
            class_correct[c] += (preds[mask] == batch_y[mask]).sum().item()
            class_total[c] += mask.sum().item()

    accuracy = 100 * correct / total if total > 0 else 0

    return total_loss / len(loader), accuracy


def evaluate(model, loader, criterion, device):
    """Evaluate model"""
    model.eval()
    total_loss = 0
    correct = 0
    total = 0

    class_correct = [0, 0, 0]
    class_total = [0, 0, 0]

    all_preds = []
    all_labels = []
    all_probs = []

    with torch.no_grad():
        for batch_x, batch_y in loader:
            batch_x = batch_x.to(device)
            batch_y = batch_y.to(device)

            logits = model(batch_x)
            loss = criterion(logits, batch_y)

            total_loss += loss.item()

            probs = F.softmax(logits, dim=1)
            preds = logits.argmax(dim=1)

            correct += (preds == batch_y).sum().item()
            total += batch_y.size(0)

            # Per-class accuracy
            for c in range(3):
                mask = batch_y == c
                class_correct[c] += (preds[mask] == batch_y[mask]).sum().item()
                class_total[c] += mask.sum().item()

            all_preds.extend(preds.cpu().numpy())
            all_labels.extend(batch_y.cpu().numpy())
            all_probs.extend(probs.cpu().numpy())

    accuracy = 100 * correct / total if total > 0 else 0

    # Per-class accuracy
    class_acc = []
    for c in range(3):
        acc = 100 * class_correct[c] / class_total[c] if class_total[c] > 0 else 0
        class_acc.append(acc)

    return total_loss / len(loader), accuracy, class_acc, all_preds, all_labels


def train_model(
    train_loader: DataLoader,
    val_loader: DataLoader,
    input_dim: int,
    class_weights: torch.Tensor,
    epochs: int = 100,
    lr: float = 0.001,
    device: str = "cpu",
    save_path: str = "model.pth",
    loss_type: str = "ce",
    focal_gamma: float = 2.0
):
    """Train classification model"""
    model = CNNLSTMClassifier(input_dim=input_dim, num_classes=3).to(device)

    # Select loss function
    if loss_type == "focal":
        criterion = FocalLoss(alpha=class_weights.to(device), gamma=focal_gamma)
        print(f"Using Focal Loss (gamma={focal_gamma})")
    else:
        criterion = nn.CrossEntropyLoss(weight=class_weights.to(device))
        print("Using CrossEntropy Loss")
    optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=1e-5)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=10, factor=0.5)

    best_val_loss = float('inf')
    best_val_acc = 0
    patience_counter = 0
    early_stop_patience = 20

    print(f"\nTraining on {device}")
    print(f"Model parameters: {sum(p.numel() for p in model.parameters()):,}")

    for epoch in range(epochs):
        train_loss, train_acc = train_epoch(model, train_loader, criterion, optimizer, device)
        val_loss, val_acc, val_class_acc, _, _ = evaluate(model, val_loader, criterion, device)

        scheduler.step(val_loss)

        print(f"Epoch {epoch+1:3d}/{epochs} | "
              f"Train Loss: {train_loss:.4f} Acc: {train_acc:.1f}% | "
              f"Val Loss: {val_loss:.4f} Acc: {val_acc:.1f}% "
              f"[H:{val_class_acc[0]:.0f}% B:{val_class_acc[1]:.0f}% S:{val_class_acc[2]:.0f}%]")

        # Save best model (by accuracy)
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_val_loss = val_loss
            patience_counter = 0
            torch.save(model.state_dict(), save_path)
            print(f"  -> Best model saved (Acc: {val_acc:.1f}%)")
        else:
            patience_counter += 1

        if patience_counter >= early_stop_patience:
            print(f"Early stopping at epoch {epoch+1}")
            break

    # Final evaluation
    model.load_state_dict(torch.load(save_path))
    val_loss, val_acc, val_class_acc, preds, labels = evaluate(model, val_loader, criterion, device)

    print(f"\n=== Best Model Metrics ===")
    print(f"Overall Accuracy: {val_acc:.2f}%")
    print(f"Hold Accuracy: {val_class_acc[0]:.2f}%")
    print(f"Buy Accuracy: {val_class_acc[1]:.2f}%")
    print(f"Sell Accuracy: {val_class_acc[2]:.2f}%")

    return model, {
        "accuracy": float(val_acc),
        "class_accuracy": {
            "hold": float(val_class_acc[0]),
            "buy": float(val_class_acc[1]),
            "sell": float(val_class_acc[2])
        }
    }


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Train Triple Barrier CNN-LSTM classifier")
    parser.add_argument("--csv-path", type=str, required=True, help="CSV file path")
    parser.add_argument("--output", type=str, default="models/triple_barrier/model.pth")
    parser.add_argument("--seq-len", type=int, default=60, help="Sequence length")
    parser.add_argument("--atr-period", type=int, default=20, help="ATR period")
    parser.add_argument("--k", type=float, default=1.5, help="Barrier multiplier (barrier = ATR * k)")
    parser.add_argument("--max-horizon", type=int, default=30, help="Max bars to look ahead")
    parser.add_argument("--epochs", type=int, default=100, help="Training epochs")
    parser.add_argument("--batch-size", type=int, default=64, help="Batch size")
    parser.add_argument("--lr", type=float, default=0.001, help="Learning rate")
    parser.add_argument("--device", type=str, default="auto", help="Device (cpu/cuda/mps/auto)")
    parser.add_argument("--balance", type=str, default="both",
                        choices=["none", "undersample", "weights", "both"],
                        help="Class balancing strategy: none, undersample, weights, or both (default: both)")
    parser.add_argument("--loss-type", type=str, default="ce",
                        choices=["ce", "focal"],
                        help="Loss function: ce (CrossEntropy) or focal (FocalLoss, better for imbalance)")
    parser.add_argument("--focal-gamma", type=float, default=2.0,
                        help="Focal loss gamma parameter (focusing strength, default: 2.0)")

    args = parser.parse_args()

    # Device setup
    if args.device == "auto":
        if torch.cuda.is_available():
            device = "cuda"
        elif torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"
    else:
        device = args.device

    print(f"Using device: {device}")

    # 1. Load data
    print("\n=== Loading Data ===")
    df = load_data_from_csv(args.csv_path)

    # 2. Create features
    print("\n=== Creating Features ===")
    df = create_features(df, atr_period=args.atr_period)

    # 3. Create triple barrier labels
    print("\n=== Creating Triple Barrier Labels ===")
    print(f"Parameters: ATR period={args.atr_period}, K={args.k}, Max horizon={args.max_horizon}")
    labels = create_triple_barrier_labels(
        df,
        atr_period=args.atr_period,
        k=args.k,
        max_horizon=args.max_horizon
    )
    df['label'] = labels

    # Drop NaN features
    df = df.dropna(subset=[c for c in df.columns if c not in ['label']])
    print(f"After feature creation: {len(df)} rows")

    # 4. Feature selection (13 features - all stationary)
    # NOTE: Do NOT include log_volatility - it's used for labeling (data leakage!)
    # Use atr_normalized instead as a volatility proxy
    feature_cols = [
        'log_return', 'ma_ratio_5', 'ma_ratio_10', 'ma_ratio_20',
        'rsi_normalized', 'bb_position', 'volume_ratio', 'volatility',
        'hl_range', 'candle_body', 'momentum_5', 'momentum_10',
        'atr_normalized'  # Volatility proxy (does NOT leak labeling mechanism)
    ]
    feature_cols = [c for c in feature_cols if c in df.columns]
    print(f"Features ({len(feature_cols)}): {feature_cols}")

    X = df[feature_cols].values
    y = df['label'].values

    # 5. Compute class weights (if using weights balancing)
    use_weights = args.balance in ["weights", "both"]
    use_undersample = args.balance in ["undersample", "both"]

    print(f"\n=== Balancing Strategy: {args.balance} ===")
    print(f"  Use undersampling: {use_undersample}")
    print(f"  Use class weights: {use_weights}")

    if use_weights:
        class_weights = compute_class_weights(y)
    else:
        # Equal weights (no weighting)
        class_weights = torch.tensor([1.0, 1.0, 1.0], dtype=torch.float32)
        print(f"Class weights: [1.0, 1.0, 1.0] (no weighting)")

    # 6. Train/Val split FIRST (before scaling to prevent data leakage!)
    split_idx = int(len(X) * 0.8)
    X_train, X_val = X[:split_idx], X[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]

    print(f"Train: {len(X_train)}, Val: {len(X_val)}")

    # 7. Normalize features (Z-score) - fit ONLY on training data
    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train)  # Fit on train only
    X_val = scaler.transform(X_val)  # Transform val (no fit!)

    # Save scaler
    output_dir = Path(args.output).parent
    output_dir.mkdir(parents=True, exist_ok=True)
    scaler_path = output_dir / "scaler.json"
    scaler_params = {
        "mean": scaler.mean_.tolist(),
        "scale": scaler.scale_.tolist(),
        "features": feature_cols
    }
    with open(scaler_path, "w") as f:
        json.dump(scaler_params, f, indent=2)
    print(f"Scaler saved to {scaler_path} (fit on training data only)")

    # 8. Create datasets
    print("\n=== Creating Datasets ===")
    print("Train set:")
    train_dataset = TripleBarrierDataset(
        X_train, y_train,
        seq_len=args.seq_len,
        undersample=use_undersample
    )
    print("Validation set:")
    val_dataset = TripleBarrierDataset(
        X_val, y_val,
        seq_len=args.seq_len,
        undersample=False  # Never undersample validation set
    )

    print(f"Train samples: {len(train_dataset)}, Val samples: {len(val_dataset)}")

    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False)

    # 9. Train model
    print("\n=== Training Model ===")
    model, metrics = train_model(
        train_loader,
        val_loader,
        input_dim=len(feature_cols),
        class_weights=class_weights,
        epochs=args.epochs,
        lr=args.lr,
        device=device,
        save_path=args.output,
        loss_type=args.loss_type,
        focal_gamma=args.focal_gamma
    )

    # 10. Save metadata
    meta_path = Path(args.output).with_suffix(".json")
    meta = {
        "model_type": "cnn-lstm-classifier",
        "task": "triple_barrier",
        "num_classes": 3,
        "class_names": ["hold", "buy", "sell"],
        "input_dim": len(feature_cols),
        "seq_len": args.seq_len,
        "atr_period": args.atr_period,
        "barrier_k": args.k,
        "max_horizon": args.max_horizon,
        "balance_strategy": args.balance,
        "loss_type": args.loss_type,
        "focal_gamma": args.focal_gamma if args.loss_type == "focal" else None,
        "features": feature_cols,
        "metrics": metrics,
        "created_at": datetime.now().isoformat(),
        "train_samples": len(train_dataset),
        "val_samples": len(val_dataset)
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"Metadata saved to {meta_path}")

    print("\n=== Training Complete ===")
    print(f"Model saved to: {args.output}")


if __name__ == "__main__":
    main()
