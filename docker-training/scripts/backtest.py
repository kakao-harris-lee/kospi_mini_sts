#!/usr/bin/env python3
"""
Backtest script for tick-based regression models.
Simulates trading on mini futures using ensemble predictions.
"""

import argparse
import json
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd
import torch
import torch.nn as nn


# ============ Model Architecture (must match training) ============
class CNNLSTMRegressor(nn.Module):
    """
    CNN-LSTM for return regression (must match train_regression.py)
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

        # Regression head
        self.fc = nn.Sequential(
            nn.Linear(hidden_dim, 32),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(32, 1)
        )

    def forward(self, x):
        # x: (batch, seq_len, features)
        x = x.transpose(1, 2)
        x = self.cnn(x)
        x = x.transpose(1, 2)

        lstm_out, _ = self.lstm(x)

        attn_weights = self.attention(lstm_out)
        context = torch.sum(attn_weights * lstm_out, dim=1)

        output = self.fc(context)
        return output.squeeze(-1)


# ============ Feature Engineering ============
def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df['log_return'] = np.log(df['close'] / df['close'].shift(1))
    df['ma_5'] = df['close'].rolling(5).mean()
    df['ma_10'] = df['close'].rolling(10).mean()
    df['ma_20'] = df['close'].rolling(20).mean()
    df['ma_ratio_5'] = df['close'] / df['ma_5']
    df['ma_ratio_10'] = df['close'] / df['ma_10']
    df['ma_ratio_20'] = df['close'] / df['ma_20']

    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / (loss + 1e-10)
    df['rsi_normalized'] = (100 - (100 / (1 + rs))) / 100 - 0.5

    bb_mid = df['close'].rolling(20).mean()
    bb_std = df['close'].rolling(20).std()
    df['bb_position'] = (df['close'] - bb_mid) / (2 * bb_std + 1e-10)

    df['volume_ratio'] = df['volume'] / (df['volume'].rolling(20).mean() + 1e-10)
    df['volatility'] = df['log_return'].rolling(20).std()
    df['hl_range'] = (df['high'] - df['low']) / df['close']
    df['candle_body'] = (df['close'] - df['open']) / (df['high'] - df['low'] + 1e-10)
    df['momentum_5'] = df['close'] / df['close'].shift(5) - 1
    df['momentum_10'] = df['close'] / df['close'].shift(10) - 1

    return df


# ============ Position & Trade Tracking ============
@dataclass
class Position:
    direction: int  # 1=long, -1=short, 0=flat
    entry_price: float = 0.0
    entry_time: Optional[pd.Timestamp] = None
    size: int = 1


@dataclass
class Trade:
    direction: int
    entry_price: float
    exit_price: float
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    pnl_ticks: float
    pnl_pts: float


@dataclass
class BacktestConfig:
    tick_size: float = 0.02  # Mini futures
    entry_threshold: float = 0.5  # Min ticks to enter
    exit_threshold: float = 0.0  # Exit when prediction flips
    stop_loss_ticks: float = 10.0  # Stop loss in ticks
    take_profit_ticks: float = 20.0  # Take profit in ticks
    max_hold_bars: int = 30  # Max bars to hold position
    commission_per_side: float = 0.01  # Commission in pts per side


# ============ Backtester ============
class Backtester:
    MAIN_TICK_SIZE = 0.05
    MINI_TICK_SIZE = 0.02

    def __init__(self, model_dir: str, device: str = "cpu"):
        self.model_dir = Path(model_dir)
        self.device = device
        self.models = {}
        self.scaler = None
        self.feature_cols = None
        self._load_models()

    def _load_models(self):
        scaler_path = self.model_dir / "scaler.json"
        with open(scaler_path) as f:
            self.scaler = json.load(f)
        self.feature_cols = self.scaler["features"]

        for h in [1, 3, 5, 10]:
            model_path = self.model_dir / f"model_h{h}.pth"
            meta_path = self.model_dir / f"model_h{h}.json"

            if not model_path.exists():
                continue

            with open(meta_path) as f:
                meta = json.load(f)

            model = CNNLSTMRegressor(
                input_dim=meta["input_dim"]
            )
            model.load_state_dict(torch.load(model_path, map_location=self.device))
            model.to(self.device)
            model.eval()

            self.models[h] = {"model": model, "meta": meta}

    def _normalize(self, features: np.ndarray) -> np.ndarray:
        mean = np.array(self.scaler["mean"])
        scale = np.array(self.scaler["scale"])
        return (features - mean) / (scale + 1e-10)

    def predict(self, seq: np.ndarray) -> dict:
        """Predict for a single sequence, returns tick predictions per horizon."""
        seq_norm = self._normalize(seq)
        x = torch.tensor(seq_norm, dtype=torch.float32).unsqueeze(0).to(self.device)

        predictions = {}
        with torch.no_grad():
            for h, data in self.models.items():
                pred = data["model"](x)
                predictions[h] = float(pred.cpu().item())

        return predictions

    def predict_batch(self, sequences: np.ndarray) -> dict:
        """Batch predict for multiple sequences."""
        seq_norm = np.array([self._normalize(seq) for seq in sequences])
        x = torch.tensor(seq_norm, dtype=torch.float32).to(self.device)

        predictions = {h: [] for h in self.models.keys()}
        with torch.no_grad():
            for h, data in self.models.items():
                pred = data["model"](x)
                predictions[h] = pred.cpu().numpy().tolist()

        return predictions

    def run(self, df: pd.DataFrame, config: BacktestConfig, batch_size: int = 512) -> dict:
        """Run backtest on dataframe."""
        df = compute_features(df)
        df = df.dropna().reset_index(drop=True)

        seq_len = 60
        if len(df) < seq_len + 1:
            raise ValueError(f"Need at least {seq_len + 1} rows")

        # Extract feature matrix
        features = df[self.feature_cols].values

        # Pre-compute all predictions in batches
        print("  Pre-computing predictions...")
        all_sequences = []
        for i in range(seq_len, len(df) - 1):
            all_sequences.append(features[i - seq_len:i])

        all_predictions = {h: [] for h in self.models.keys()}
        for batch_start in range(0, len(all_sequences), batch_size):
            batch_end = min(batch_start + batch_size, len(all_sequences))
            batch_seqs = np.array(all_sequences[batch_start:batch_end])
            batch_preds = self.predict_batch(batch_seqs)
            for h in self.models.keys():
                all_predictions[h].extend(batch_preds[h])
            if batch_start % 5000 == 0:
                print(f"    Processed {batch_start}/{len(all_sequences)} sequences...")

        print(f"  Computed {len(all_sequences)} predictions")

        # Compute ensemble predictions
        weights = {h: 1.0 / h for h in self.models.keys()}
        total_weight = sum(weights.values())
        ensemble_preds = []
        for i in range(len(all_sequences)):
            ensemble_ticks = sum(all_predictions[h][i] * weights[h] for h in self.models.keys()) / total_weight
            ensemble_preds.append(ensemble_ticks)

        position = Position(direction=0)
        trades: list[Trade] = []
        equity_curve = []
        cumulative_pnl = 0.0
        predictions_log = []

        # Walk through data using pre-computed predictions
        print("  Simulating trades...")
        for idx, i in enumerate(range(seq_len, len(df) - 1)):
            current_bar = df.iloc[i]
            next_bar = df.iloc[i + 1]
            current_price = current_bar['close']
            current_time = current_bar.get('datetime', pd.Timestamp.now())

            ensemble_ticks = ensemble_preds[idx]

            predictions_log.append({
                "time": current_time,
                "price": current_price,
                "pred_ticks": ensemble_ticks,
                "position": position.direction
            })

            # Position management
            if position.direction != 0:
                # Check exit conditions
                bars_held = idx - getattr(position, 'entry_idx', idx)
                price_change_ticks = (current_price - position.entry_price) / config.tick_size
                unrealized_pnl_ticks = price_change_ticks * position.direction

                should_exit = False
                exit_reason = ""

                # Stop loss
                if unrealized_pnl_ticks <= -config.stop_loss_ticks:
                    should_exit = True
                    exit_reason = "stop_loss"
                # Take profit
                elif unrealized_pnl_ticks >= config.take_profit_ticks:
                    should_exit = True
                    exit_reason = "take_profit"
                # Max hold time
                elif bars_held >= config.max_hold_bars:
                    should_exit = True
                    exit_reason = "max_hold"
                # Signal reversal
                elif (position.direction == 1 and ensemble_ticks < -config.entry_threshold) or \
                     (position.direction == -1 and ensemble_ticks > config.entry_threshold):
                    should_exit = True
                    exit_reason = "signal_reversal"

                if should_exit:
                    exit_price = next_bar['open']  # Exit at next bar open
                    pnl_pts = (exit_price - position.entry_price) * position.direction
                    pnl_pts -= config.commission_per_side * 2  # Round trip commission
                    pnl_ticks = pnl_pts / config.tick_size

                    trades.append(Trade(
                        direction=position.direction,
                        entry_price=position.entry_price,
                        exit_price=exit_price,
                        entry_time=position.entry_time,
                        exit_time=current_time,
                        pnl_ticks=pnl_ticks,
                        pnl_pts=pnl_pts
                    ))

                    cumulative_pnl += pnl_pts
                    position = Position(direction=0)

            # Entry logic (only if flat)
            if position.direction == 0:
                if ensemble_ticks > config.entry_threshold:
                    # Long entry
                    position = Position(
                        direction=1,
                        entry_price=next_bar['open'],
                        entry_time=current_time
                    )
                    position.entry_idx = idx
                elif ensemble_ticks < -config.entry_threshold:
                    # Short entry
                    position = Position(
                        direction=-1,
                        entry_price=next_bar['open'],
                        entry_time=current_time
                    )
                    position.entry_idx = idx

            equity_curve.append(cumulative_pnl)

        # Close any remaining position at last price
        if position.direction != 0:
            exit_price = df.iloc[-1]['close']
            pnl_pts = (exit_price - position.entry_price) * position.direction
            pnl_pts -= config.commission_per_side * 2
            pnl_ticks = pnl_pts / config.tick_size

            trades.append(Trade(
                direction=position.direction,
                entry_price=position.entry_price,
                exit_price=exit_price,
                entry_time=position.entry_time,
                exit_time=df.iloc[-1].get('datetime', pd.Timestamp.now()),
                pnl_ticks=pnl_ticks,
                pnl_pts=pnl_pts
            ))
            cumulative_pnl += pnl_pts

        return self._compute_metrics(trades, equity_curve, df, predictions_log)

    def _compute_metrics(self, trades: list, equity_curve: list, df: pd.DataFrame, predictions_log: list) -> dict:
        if not trades:
            return {
                "total_trades": 0,
                "message": "No trades executed"
            }

        pnls = [t.pnl_pts for t in trades]
        pnl_ticks = [t.pnl_ticks for t in trades]

        winning_trades = [p for p in pnls if p > 0]
        losing_trades = [p for p in pnls if p < 0]

        total_pnl = sum(pnls)
        win_rate = len(winning_trades) / len(trades) * 100 if trades else 0

        avg_win = np.mean(winning_trades) if winning_trades else 0
        avg_loss = abs(np.mean(losing_trades)) if losing_trades else 0
        profit_factor = (sum(winning_trades) / abs(sum(losing_trades))) if losing_trades else float('inf')

        # Sharpe ratio (annualized, assuming 1-min bars)
        if len(pnls) > 1:
            returns = np.array(pnls)
            sharpe = np.mean(returns) / (np.std(returns) + 1e-10) * np.sqrt(252 * 6.5 * 60)
        else:
            sharpe = 0

        # Max drawdown
        equity = np.array(equity_curve) if equity_curve else np.array([0])
        cummax = np.maximum.accumulate(equity)
        drawdown = cummax - equity
        max_dd = np.max(drawdown) if len(drawdown) > 0 else 0

        # Long/Short breakdown
        long_trades = [t for t in trades if t.direction == 1]
        short_trades = [t for t in trades if t.direction == -1]

        return {
            "total_trades": len(trades),
            "long_trades": len(long_trades),
            "short_trades": len(short_trades),
            "win_rate": round(win_rate, 2),
            "total_pnl_pts": round(total_pnl, 2),
            "total_pnl_ticks": round(sum(pnl_ticks), 2),
            "avg_pnl_per_trade_pts": round(np.mean(pnls), 4),
            "avg_pnl_per_trade_ticks": round(np.mean(pnl_ticks), 2),
            "avg_win_pts": round(avg_win, 4),
            "avg_loss_pts": round(avg_loss, 4),
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else "inf",
            "sharpe_ratio": round(sharpe, 2),
            "max_drawdown_pts": round(max_dd, 2),
            "data_range": f"{df.iloc[0].get('datetime', 'N/A')} to {df.iloc[-1].get('datetime', 'N/A')}",
            "total_bars": len(df)
        }


def main():
    parser = argparse.ArgumentParser(description="Backtest tick-based regression models")
    parser.add_argument("--csv", required=True, help="Path to OHLCV CSV")
    parser.add_argument("--model-dir", default="models/regression", help="Model directory")
    parser.add_argument("--entry-threshold", type=float, default=0.5, help="Entry threshold in ticks")
    parser.add_argument("--stop-loss", type=float, default=10.0, help="Stop loss in ticks")
    parser.add_argument("--take-profit", type=float, default=20.0, help="Take profit in ticks")
    parser.add_argument("--max-hold", type=int, default=30, help="Max bars to hold")
    parser.add_argument("--commission", type=float, default=0.01, help="Commission per side in pts")
    parser.add_argument("--device", default="mps", help="Device (cpu, cuda, mps)")
    parser.add_argument("--limit", type=int, default=0, help="Limit rows for testing (0=all)")

    args = parser.parse_args()

    # Load data
    print(f"Loading data from {args.csv}...")
    df = pd.read_csv(args.csv)

    if 'datetime' in df.columns:
        df['datetime'] = pd.to_datetime(df['datetime'])

    if args.limit > 0:
        df = df.tail(args.limit).reset_index(drop=True)

    print(f"Loaded {len(df)} rows")

    # Initialize backtester
    print(f"Loading models from {args.model_dir}...")
    backtester = Backtester(args.model_dir, device=args.device)
    print(f"Loaded {len(backtester.models)} models")

    # Configure backtest
    config = BacktestConfig(
        entry_threshold=args.entry_threshold,
        stop_loss_ticks=args.stop_loss,
        take_profit_ticks=args.take_profit,
        max_hold_bars=args.max_hold,
        commission_per_side=args.commission
    )

    print(f"\nBacktest Configuration:")
    print(f"  Entry threshold: {config.entry_threshold} ticks")
    print(f"  Stop loss: {config.stop_loss_ticks} ticks")
    print(f"  Take profit: {config.take_profit_ticks} ticks")
    print(f"  Max hold: {config.max_hold_bars} bars")
    print(f"  Commission: {config.commission_per_side} pts/side")

    # Run backtest
    print("\nRunning backtest...")
    results = backtester.run(df, config)

    # Print results
    print("\n" + "=" * 50)
    print("BACKTEST RESULTS")
    print("=" * 50)

    for key, value in results.items():
        print(f"  {key}: {value}")

    print("=" * 50)


if __name__ == "__main__":
    main()
