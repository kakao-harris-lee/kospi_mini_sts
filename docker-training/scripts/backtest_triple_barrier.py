#!/usr/bin/env python3
"""
Backtest script for Triple Barrier Classification Model.

Simulates trading on mini futures using probability-based signals.
Key difference from regression backtest: uses confidence thresholds
on class probabilities instead of tick predictions.
"""

import argparse
import json
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List
from collections import Counter

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F


# ============ Model Architecture (must match training) ============
class CNNLSTMClassifier(nn.Module):
    """CNN-LSTM for triple barrier classification"""

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

        self.lstm = nn.LSTM(
            cnn_channels[1],
            hidden_dim,
            num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0,
            bidirectional=False
        )

        self.attention = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.Tanh(),
            nn.Linear(hidden_dim // 2, 1),
            nn.Softmax(dim=1)
        )

        self.fc = nn.Sequential(
            nn.Linear(hidden_dim, 32),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(32, num_classes)
        )

    def forward(self, x):
        x = x.transpose(1, 2)
        x = self.cnn(x)
        x = x.transpose(1, 2)
        lstm_out, _ = self.lstm(x)
        attn_weights = self.attention(lstm_out)
        context = torch.sum(attn_weights * lstm_out, dim=1)
        logits = self.fc(context)
        return logits


# ============ Feature Engineering ============
def compute_atr(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Compute Average True Range (ATR)."""
    high_low = df['high'] - df['low']
    high_close = abs(df['high'] - df['close'].shift(1))
    low_close = abs(df['low'] - df['close'].shift(1))
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(period).mean()


def compute_features(df: pd.DataFrame, atr_period: int = 20) -> pd.DataFrame:
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
    df['rsi_normalized'] = (100 - (100 / (1 + rs)) - 50) / 50

    # Bollinger Bands position - MUST match training formula
    bb_mid = df['close'].rolling(20).mean()
    bb_std = df['close'].rolling(20).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std
    df['bb_position'] = (df['close'] - bb_lower) / (bb_upper - bb_lower + 1e-10)

    # Volume ratio - MUST match training (uses +1, not +1e-10)
    df['volume_ma'] = df['volume'].rolling(20).mean()
    df['volume_ratio'] = df['volume'] / (df['volume_ma'] + 1)

    df['volatility'] = df['log_return'].rolling(20).std()
    df['hl_range'] = (df['high'] - df['low']) / df['close']
    df['candle_body'] = (df['close'] - df['open']) / (df['high'] - df['low'] + 1e-10)
    df['momentum_5'] = df['close'] / df['close'].shift(5) - 1
    df['momentum_10'] = df['close'] / df['close'].shift(10) - 1

    # ATR normalized (matches training - NOT log_volatility to avoid data leakage)
    high_low = df['high'] - df['low']
    high_close = abs(df['high'] - df['close'].shift(1))
    low_close = abs(df['low'] - df['close'].shift(1))
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = true_range.rolling(atr_period).mean()
    df['atr_normalized'] = df['atr'] / df['close']

    return df


# ============ Position & Trade Tracking ============
@dataclass
class Position:
    direction: int  # 1=long, -1=short, 0=flat
    entry_price: float = 0.0
    entry_time: Optional[pd.Timestamp] = None
    entry_idx: int = 0
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
    exit_reason: str = ""


@dataclass
class BacktestConfig:
    tick_size: float = 0.02  # Mini futures
    confidence_threshold: float = 0.5  # Min probability for BUY/SELL
    stop_loss_ticks: float = 10.0  # Stop loss in ticks
    take_profit_ticks: float = 20.0  # Take profit in ticks
    max_hold_bars: int = 30  # Max bars to hold position
    commission_per_side: float = 0.01  # Commission in pts per side


# ============ Backtester ============
class TripleBarrierBacktester:
    """Backtester for triple barrier classification model."""

    def __init__(self, model_dir: str, device: str = "cpu"):
        self.model_dir = Path(model_dir)
        self.device = device
        self.model = None
        self.scaler = None
        self.meta = None
        self.feature_cols = None
        self._load_model()

    def _load_model(self):
        # Load scaler
        scaler_path = self.model_dir / "scaler.json"
        with open(scaler_path) as f:
            self.scaler = json.load(f)
        self.feature_cols = self.scaler["features"]

        # Load metadata
        meta_path = self.model_dir / "model.json"
        with open(meta_path) as f:
            self.meta = json.load(f)

        # Load model
        model_path = self.model_dir / "model.pth"
        self.model = CNNLSTMClassifier(
            input_dim=self.meta["input_dim"],
            num_classes=self.meta.get("num_classes", 3)
        )
        self.model.load_state_dict(torch.load(model_path, map_location=self.device))
        self.model.to(self.device)
        self.model.eval()

        print(f"Loaded model from {model_path}")

    def _normalize(self, features: np.ndarray) -> np.ndarray:
        mean = np.array(self.scaler["mean"])
        scale = np.array(self.scaler["scale"])
        # Match StandardScaler exactly - no epsilon needed (it handles zero internally)
        return (features - mean) / scale

    def predict_batch(self, sequences: np.ndarray) -> tuple:
        """
        Batch predict for multiple sequences.

        Returns:
            Tuple of (predicted_classes, probabilities)
            probabilities shape: (batch, 3) for [hold, buy, sell]
        """
        seq_norm = np.array([self._normalize(seq) for seq in sequences])
        x = torch.tensor(seq_norm, dtype=torch.float32).to(self.device)

        with torch.no_grad():
            logits = self.model(x)
            probs = F.softmax(logits, dim=1)
            preds = logits.argmax(dim=1)

        return preds.cpu().numpy(), probs.cpu().numpy()

    def run(self, df: pd.DataFrame, config: BacktestConfig, batch_size: int = 512) -> dict:
        """Run backtest on dataframe."""
        atr_period = self.meta.get("atr_period", 20)
        df = compute_features(df, atr_period=atr_period)
        df = df.dropna().reset_index(drop=True)

        seq_len = self.meta.get("seq_len", 60)
        if len(df) < seq_len + 1:
            raise ValueError(f"Need at least {seq_len + 1} rows")

        # Extract feature matrix
        features = df[self.feature_cols].values

        # Pre-compute all predictions in batches
        print("  Pre-computing predictions...")
        all_sequences = []
        for i in range(seq_len, len(df) - 1):
            all_sequences.append(features[i - seq_len:i])

        all_preds = []
        all_probs = []

        for batch_start in range(0, len(all_sequences), batch_size):
            batch_end = min(batch_start + batch_size, len(all_sequences))
            batch_seqs = np.array(all_sequences[batch_start:batch_end])
            batch_preds, batch_probs = self.predict_batch(batch_seqs)
            all_preds.extend(batch_preds)
            all_probs.extend(batch_probs)

            if batch_start % 5000 == 0:
                print(f"    Processed {batch_start}/{len(all_sequences)} sequences...")

        all_preds = np.array(all_preds)
        all_probs = np.array(all_probs)

        print(f"  Computed {len(all_sequences)} predictions")

        # Prediction distribution
        pred_counter = Counter(all_preds)
        print(f"  Prediction distribution: Hold={pred_counter.get(0, 0)}, "
              f"Buy={pred_counter.get(1, 0)}, Sell={pred_counter.get(2, 0)}")

        # Simulation
        position = Position(direction=0)
        trades: List[Trade] = []
        equity_curve = []
        cumulative_pnl = 0.0

        print("  Simulating trades...")
        for idx, i in enumerate(range(seq_len, len(df) - 1)):
            current_bar = df.iloc[i]
            next_bar = df.iloc[i + 1]
            current_price = current_bar['close']
            current_time = current_bar.get('datetime', pd.Timestamp.now())

            # Get probabilities
            p_hold, p_buy, p_sell = all_probs[idx]

            # Determine signal based on confidence threshold
            if p_buy > config.confidence_threshold and p_buy > p_sell:
                signal = 1  # BUY
            elif p_sell > config.confidence_threshold and p_sell > p_buy:
                signal = -1  # SELL
            else:
                signal = 0  # HOLD

            # Position management
            if position.direction != 0:
                bars_held = idx - position.entry_idx
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
                # Signal reversal (opposite signal with high confidence)
                elif (position.direction == 1 and signal == -1) or \
                     (position.direction == -1 and signal == 1):
                    should_exit = True
                    exit_reason = "signal_reversal"

                if should_exit:
                    exit_price = next_bar['open']
                    pnl_pts = (exit_price - position.entry_price) * position.direction
                    pnl_pts -= config.commission_per_side * 2
                    pnl_ticks = pnl_pts / config.tick_size

                    trades.append(Trade(
                        direction=position.direction,
                        entry_price=position.entry_price,
                        exit_price=exit_price,
                        entry_time=position.entry_time,
                        exit_time=current_time,
                        pnl_ticks=pnl_ticks,
                        pnl_pts=pnl_pts,
                        exit_reason=exit_reason
                    ))

                    cumulative_pnl += pnl_pts
                    position = Position(direction=0)

            # Entry logic (only if flat and have signal)
            if position.direction == 0 and signal != 0:
                position = Position(
                    direction=signal,
                    entry_price=next_bar['open'],
                    entry_time=current_time,
                    entry_idx=idx
                )

            equity_curve.append(cumulative_pnl)

        # Close any remaining position
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
                pnl_pts=pnl_pts,
                exit_reason="end_of_data"
            ))
            cumulative_pnl += pnl_pts

        return self._compute_metrics(trades, equity_curve, df, all_probs)

    def _compute_metrics(self, trades: List[Trade], equity_curve: list,
                         df: pd.DataFrame, all_probs: np.ndarray) -> dict:
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

        # Sharpe ratio
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

        long_pnl = sum(t.pnl_pts for t in long_trades)
        short_pnl = sum(t.pnl_pts for t in short_trades)

        # Exit reason breakdown
        exit_reasons = Counter(t.exit_reason for t in trades)

        # Average confidence when trading
        avg_buy_conf = np.mean(all_probs[:, 1])
        avg_sell_conf = np.mean(all_probs[:, 2])

        return {
            "total_trades": len(trades),
            "long_trades": len(long_trades),
            "short_trades": len(short_trades),
            "win_rate": round(win_rate, 2),
            "total_pnl_pts": round(total_pnl, 2),
            "total_pnl_ticks": round(sum(pnl_ticks), 2),
            "long_pnl_pts": round(long_pnl, 2),
            "short_pnl_pts": round(short_pnl, 2),
            "avg_pnl_per_trade_pts": round(np.mean(pnls), 4),
            "avg_pnl_per_trade_ticks": round(np.mean(pnl_ticks), 2),
            "avg_win_pts": round(avg_win, 4),
            "avg_loss_pts": round(avg_loss, 4),
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else "inf",
            "sharpe_ratio": round(sharpe, 2),
            "max_drawdown_pts": round(max_dd, 2),
            "exit_reasons": dict(exit_reasons),
            "avg_buy_confidence": round(avg_buy_conf, 4),
            "avg_sell_confidence": round(avg_sell_conf, 4),
            "data_range": f"{df.iloc[0].get('datetime', 'N/A')} to {df.iloc[-1].get('datetime', 'N/A')}",
            "total_bars": len(df)
        }


def main():
    parser = argparse.ArgumentParser(description="Backtest triple barrier classification model")
    parser.add_argument("--csv", required=True, help="Path to OHLCV CSV")
    parser.add_argument("--model-dir", default="models/triple_barrier", help="Model directory")
    parser.add_argument("--threshold", type=float, default=0.5, help="Confidence threshold for signals")
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
    print(f"Loading model from {args.model_dir}...")
    backtester = TripleBarrierBacktester(args.model_dir, device=args.device)

    # Configure backtest
    config = BacktestConfig(
        confidence_threshold=args.threshold,
        stop_loss_ticks=args.stop_loss,
        take_profit_ticks=args.take_profit,
        max_hold_bars=args.max_hold,
        commission_per_side=args.commission
    )

    print(f"\nBacktest Configuration:")
    print(f"  Confidence threshold: {config.confidence_threshold:.0%}")
    print(f"  Stop loss: {config.stop_loss_ticks} ticks")
    print(f"  Take profit: {config.take_profit_ticks} ticks")
    print(f"  Max hold: {config.max_hold_bars} bars")
    print(f"  Commission: {config.commission_per_side} pts/side")

    # Run backtest
    print("\nRunning backtest...")
    results = backtester.run(df, config)

    # Print results
    print("\n" + "=" * 60)
    print("TRIPLE BARRIER BACKTEST RESULTS")
    print("=" * 60)

    for key, value in results.items():
        if key == "exit_reasons":
            print(f"  {key}:")
            for reason, count in value.items():
                print(f"    {reason}: {count}")
        else:
            print(f"  {key}: {value}")

    print("=" * 60)

    # Summary
    if results["total_trades"] > 0:
        print("\n=== SUMMARY ===")
        print(f"Strategy: Triple Barrier Classification")
        print(f"Trades: {results['long_trades']} long, {results['short_trades']} short")
        print(f"Win Rate: {results['win_rate']}%")
        print(f"P&L: {results['total_pnl_pts']:.2f} pts ({results['total_pnl_ticks']:.0f} ticks)")
        print(f"Sharpe: {results['sharpe_ratio']}")


if __name__ == "__main__":
    main()
