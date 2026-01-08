"""
CNN-LSTM Regression Model for Log Return Prediction

Predicts log returns at t+horizon instead of classification (Up/Down/Hold).
This model outputs continuous return values for direct application to mini futures.
"""
import os
import sys
import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from sklearn.preprocessing import StandardScaler


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

def create_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Create technical indicators as features.
    Uses rolling window calculations to prevent data leakage.
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

    return df


def create_targets(
    df: pd.DataFrame,
    horizon: int = 5,
    tick_size: float = 0.05
) -> pd.DataFrame:
    """
    Create target variable: tick-based return at t+horizon

    Instead of simple log return, we normalize by tick size to predict
    the number of ticks the price will move. This is more interpretable
    for trading and allows easy conversion between main/mini futures.

    Main futures tick: 0.05 points
    Mini futures tick: 0.02 points

    Args:
        df: DataFrame with close prices
        horizon: prediction horizon in minutes
        tick_size: tick size for normalization (default: 0.05 for main futures)

    Returns:
        DataFrame with target column (in tick units)
    """
    df = df.copy()

    # Future price change in tick units
    # target = (future_price - current_price) / tick_size
    df['target'] = (df['close'].shift(-horizon) - df['close']) / tick_size

    return df


# ============================================================
# Dataset Class
# ============================================================

class TimeSeriesRegressionDataset(Dataset):
    """Time series dataset for regression"""

    def __init__(self, features: np.ndarray, targets: np.ndarray, seq_len: int = 60):
        self.features = features
        self.targets = targets
        self.seq_len = seq_len

    def __len__(self):
        return len(self.features) - self.seq_len

    def __getitem__(self, idx):
        x = self.features[idx:idx + self.seq_len]
        y = self.targets[idx + self.seq_len - 1]

        return (
            torch.tensor(x, dtype=torch.float32),
            torch.tensor(y, dtype=torch.float32)
        )


# ============================================================
# Model Definition
# ============================================================

class CNNLSTMRegressor(nn.Module):
    """
    CNN-LSTM for return regression

    Architecture:
    - CNN: Extract local patterns (momentum, crossovers)
    - LSTM: Learn temporal dependencies
    - Attention: Weight important timesteps
    - Linear: Output predicted return
    """

    def __init__(
        self,
        input_dim: int,
        hidden_dim: int = 64,
        num_layers: int = 2,
        dropout: float = 0.2,
        cnn_channels: tuple = (32, 64),
        kernel_size: int = 3
    ):
        super().__init__()

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

        # Regression head - outputs single value (predicted return)
        self.fc = nn.Sequential(
            nn.Linear(hidden_dim, 32),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(32, 1)  # Single output for return prediction
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

        # Regression output
        output = self.fc(context)
        return output.squeeze(-1)


# ============================================================
# Training Functions
# ============================================================

def train_epoch(model, loader, criterion, optimizer, device):
    """Train one epoch"""
    model.train()
    total_loss = 0
    predictions = []
    actuals = []

    for batch_x, batch_y in loader:
        batch_x = batch_x.to(device)
        batch_y = batch_y.to(device)

        optimizer.zero_grad()
        outputs = model(batch_x)
        loss = criterion(outputs, batch_y)
        loss.backward()

        # Gradient clipping
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)

        optimizer.step()

        total_loss += loss.item()
        predictions.extend(outputs.detach().cpu().numpy())
        actuals.extend(batch_y.detach().cpu().numpy())

    predictions = np.array(predictions)
    actuals = np.array(actuals)

    # Directional accuracy
    dir_acc = np.mean(np.sign(predictions) == np.sign(actuals)) * 100

    return total_loss / len(loader), dir_acc


def evaluate(model, loader, criterion, device):
    """Evaluate model"""
    model.eval()
    total_loss = 0
    predictions = []
    actuals = []

    with torch.no_grad():
        for batch_x, batch_y in loader:
            batch_x = batch_x.to(device)
            batch_y = batch_y.to(device)

            outputs = model(batch_x)
            loss = criterion(outputs, batch_y)

            total_loss += loss.item()
            predictions.extend(outputs.cpu().numpy())
            actuals.extend(batch_y.cpu().numpy())

    predictions = np.array(predictions)
    actuals = np.array(actuals)

    # Metrics
    mse = np.mean((predictions - actuals) ** 2)
    mae = np.mean(np.abs(predictions - actuals))
    dir_acc = np.mean(np.sign(predictions) == np.sign(actuals)) * 100

    # R² score
    ss_res = np.sum((actuals - predictions) ** 2)
    ss_tot = np.sum((actuals - np.mean(actuals)) ** 2)
    r2 = 1 - (ss_res / (ss_tot + 1e-10))

    return total_loss / len(loader), mse, mae, dir_acc, r2


def train_model(
    train_loader: DataLoader,
    val_loader: DataLoader,
    input_dim: int,
    epochs: int = 100,
    lr: float = 0.001,
    device: str = "cpu",
    save_path: str = "model.pth"
):
    """Train regression model"""
    model = CNNLSTMRegressor(input_dim=input_dim).to(device)
    criterion = nn.MSELoss()
    optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=1e-5)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=10, factor=0.5)

    best_val_loss = float('inf')
    patience_counter = 0
    early_stop_patience = 20

    print(f"\nTraining on {device}")
    print(f"Model parameters: {sum(p.numel() for p in model.parameters()):,}")

    for epoch in range(epochs):
        train_loss, train_dir_acc = train_epoch(model, train_loader, criterion, optimizer, device)
        val_loss, val_mse, val_mae, val_dir_acc, val_r2 = evaluate(model, val_loader, criterion, device)

        scheduler.step(val_loss)

        print(f"Epoch {epoch+1:3d}/{epochs} | "
              f"Train Loss: {train_loss:.6f} DirAcc: {train_dir_acc:.1f}% | "
              f"Val Loss: {val_loss:.6f} DirAcc: {val_dir_acc:.1f}% R²: {val_r2:.4f}")

        # Save best model
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            patience_counter = 0
            torch.save(model.state_dict(), save_path)
            print(f"  -> Best model saved (Val Loss: {val_loss:.6f}, DirAcc: {val_dir_acc:.1f}%)")
        else:
            patience_counter += 1

        if patience_counter >= early_stop_patience:
            print(f"Early stopping at epoch {epoch+1}")
            break

    # Final evaluation
    model.load_state_dict(torch.load(save_path))
    _, val_mse, val_mae, val_dir_acc, val_r2 = evaluate(model, val_loader, criterion, device)

    print(f"\n=== Best Model Metrics ===")
    print(f"MSE: {val_mse:.8f}")
    print(f"MAE: {val_mae:.6f}")
    print(f"Directional Accuracy: {val_dir_acc:.2f}%")
    print(f"R² Score: {val_r2:.4f}")

    return model, {"mse": float(val_mse), "mae": float(val_mae), "dir_acc": float(val_dir_acc), "r2": float(val_r2)}


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Train CNN-LSTM regression model")
    parser.add_argument("--csv-path", type=str, required=True, help="CSV file path")
    parser.add_argument("--output", type=str, default="models/regression/model.pth")
    parser.add_argument("--seq-len", type=int, default=60, help="Sequence length")
    parser.add_argument("--horizon", type=int, default=5, help="Prediction horizon (minutes)")
    parser.add_argument("--epochs", type=int, default=100, help="Training epochs")
    parser.add_argument("--batch-size", type=int, default=64, help="Batch size")
    parser.add_argument("--lr", type=float, default=0.001, help="Learning rate")
    parser.add_argument("--tick-size", type=float, default=0.05,
                       help="Tick size for target normalization (main futures: 0.05)")
    parser.add_argument("--device", type=str, default="auto", help="Device (cpu/cuda/mps/auto)")

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

    # 2. Create features and targets
    print("\n=== Creating Features ===")
    df = create_features(df)
    df = create_targets(df, horizon=args.horizon, tick_size=args.tick_size)
    print(f"Tick size: {args.tick_size} (target in tick units)")

    # Drop NaN
    df = df.dropna()
    print(f"After feature creation: {len(df)} rows")

    # 3. Feature selection
    feature_cols = [
        'log_return', 'ma_ratio_5', 'ma_ratio_10', 'ma_ratio_20',
        'rsi_normalized', 'bb_position', 'volume_ratio', 'volatility',
        'hl_range', 'candle_body', 'momentum_5', 'momentum_10'
    ]
    feature_cols = [c for c in feature_cols if c in df.columns]
    print(f"Features ({len(feature_cols)}): {feature_cols}")

    X = df[feature_cols].values
    y = df['target'].values

    # 4. Normalize features (Z-score)
    scaler = StandardScaler()
    X = scaler.fit_transform(X)

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
    print(f"Scaler saved to {scaler_path}")

    # 5. Train/Val split (time series, no shuffle)
    split_idx = int(len(X) * 0.8)
    X_train, X_val = X[:split_idx], X[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]

    print(f"Train: {len(X_train)}, Val: {len(X_val)}")
    print(f"Target stats - Mean: {y_train.mean():.6f}, Std: {y_train.std():.6f}")

    # 6. Create datasets
    train_dataset = TimeSeriesRegressionDataset(X_train, y_train, seq_len=args.seq_len)
    val_dataset = TimeSeriesRegressionDataset(X_val, y_val, seq_len=args.seq_len)

    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False)

    # 7. Train model
    print("\n=== Training Model ===")
    model, metrics = train_model(
        train_loader,
        val_loader,
        input_dim=len(feature_cols),
        epochs=args.epochs,
        lr=args.lr,
        device=device,
        save_path=args.output
    )

    # 8. Save metadata
    meta_path = Path(args.output).with_suffix(".json")
    meta = {
        "model_type": "cnn-lstm-regression",
        "task": "tick_return_prediction",
        "input_dim": len(feature_cols),
        "seq_len": args.seq_len,
        "horizon": args.horizon,
        "tick_size": args.tick_size,
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
