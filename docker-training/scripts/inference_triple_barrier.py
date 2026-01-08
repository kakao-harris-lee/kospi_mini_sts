"""
Inference Module for Triple Barrier Classification

Loads trained CNN-LSTM classifier and generates trading signals
based on probability thresholds.

Signal Generation Logic:
- If P(Buy) > threshold and P(Buy) > P(Sell) -> BUY
- If P(Sell) > threshold and P(Sell) > P(Buy) -> SELL
- Otherwise -> HOLD
"""
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F


class CNNLSTMClassifier(nn.Module):
    """CNN-LSTM for triple barrier classification (must match training architecture)"""

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


def compute_atr(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Compute Average True Range (ATR)."""
    high_low = df['high'] - df['low']
    high_close = abs(df['high'] - df['close'].shift(1))
    low_close = abs(df['low'] - df['close'].shift(1))
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(period).mean()


class TripleBarrierPredictor:
    """
    Signal generator using triple barrier classification model.

    Generates BUY/SELL/HOLD signals based on model probabilities
    and configurable confidence thresholds.
    """

    CLASS_NAMES = ["HOLD", "BUY", "SELL"]

    def __init__(
        self,
        model_dir: str = "models/triple_barrier",
        device: str = "auto",
        confidence_threshold: float = 0.5
    ):
        """
        Initialize the predictor.

        Args:
            model_dir: Directory containing trained model
            device: Device to use (auto/cpu/cuda/mps)
            confidence_threshold: Minimum probability for BUY/SELL signals
        """
        self.model_dir = Path(model_dir)
        self.confidence_threshold = confidence_threshold

        # Set device
        if device == "auto":
            if torch.cuda.is_available():
                self.device = "cuda"
            elif torch.backends.mps.is_available():
                self.device = "mps"
            else:
                self.device = "cpu"
        else:
            self.device = device

        # Load scaler and model
        self.scaler = self._load_scaler()
        self.model, self.meta = self._load_model()

        print(f"Loaded triple barrier model on {self.device}")
        print(f"Confidence threshold: {self.confidence_threshold}")

    def _load_scaler(self) -> Dict:
        """Load feature scaler parameters"""
        scaler_path = self.model_dir / "scaler.json"
        with open(scaler_path) as f:
            return json.load(f)

    def _load_model(self) -> Tuple[nn.Module, Dict]:
        """Load model and metadata"""
        model_path = self.model_dir / "model.pth"
        meta_path = self.model_dir / "model.json"

        with open(meta_path) as f:
            meta = json.load(f)

        model = CNNLSTMClassifier(
            input_dim=meta["input_dim"],
            num_classes=meta.get("num_classes", 3)
        )
        model.load_state_dict(torch.load(model_path, map_location=self.device))
        model.to(self.device)
        model.eval()

        return model, meta

    def create_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Create features from OHLCV data.

        Args:
            df: DataFrame with columns [datetime, open, high, low, close, volume]

        Returns:
            DataFrame with computed features
        """
        df = df.copy()
        df = df.sort_values('datetime').reset_index(drop=True)

        # Log return
        df['log_return'] = np.log(df['close'] / df['close'].shift(1))

        # Moving average ratios
        for window in [5, 10, 20]:
            df[f'ma_{window}'] = df['close'].rolling(window=window).mean()
            df[f'ma_ratio_{window}'] = df['close'] / df[f'ma_{window}']

        # RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / (loss + 1e-10)
        df['rsi_normalized'] = (100 - (100 / (1 + rs)) - 50) / 50

        # Bollinger Bands position
        bb_mid = df['close'].rolling(window=20).mean()
        bb_std = df['close'].rolling(window=20).std()
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std
        df['bb_position'] = (df['close'] - bb_lower) / (bb_upper - bb_lower + 1e-10)

        # Volume ratio
        volume_ma = df['volume'].rolling(window=20).mean()
        df['volume_ratio'] = df['volume'] / (volume_ma + 1)

        # Volatility
        df['volatility'] = df['log_return'].rolling(window=20).std()

        # High-Low range
        df['hl_range'] = (df['high'] - df['low']) / df['close']

        # Candle body
        df['candle_body'] = (df['close'] - df['open']) / (df['high'] - df['low'] + 1e-10)

        # Momentum
        df['momentum_5'] = df['close'] / df['close'].shift(5) - 1
        df['momentum_10'] = df['close'] / df['close'].shift(10) - 1

        # ATR normalized (matches training - NOT log_volatility to avoid data leakage)
        atr_period = self.meta.get("atr_period", 20)
        high_low = df['high'] - df['low']
        high_close = abs(df['high'] - df['close'].shift(1))
        low_close = abs(df['low'] - df['close'].shift(1))
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['atr'] = true_range.rolling(atr_period).mean()
        df['atr_normalized'] = df['atr'] / df['close']

        return df

    def prepare_input(self, df: pd.DataFrame, seq_len: int = 60) -> torch.Tensor:
        """
        Prepare input tensor for model inference.

        Args:
            df: DataFrame with features
            seq_len: Sequence length (default 60)

        Returns:
            Tensor of shape (1, seq_len, num_features)
        """
        feature_cols = self.scaler['features']

        if len(df) < seq_len:
            raise ValueError(f"Need at least {seq_len} rows, got {len(df)}")

        features = df[feature_cols].iloc[-seq_len:].values

        # Apply scaler (Z-score normalization)
        mean = np.array(self.scaler['mean'])
        scale = np.array(self.scaler['scale'])
        features = (features - mean) / scale

        tensor = torch.tensor(features, dtype=torch.float32).unsqueeze(0)
        return tensor.to(self.device)

    def predict_probs(self, df: pd.DataFrame) -> Dict[str, float]:
        """
        Get probability distribution over classes.

        Args:
            df: DataFrame with OHLCV data (needs at least 80 rows for features + sequence)

        Returns:
            Dict with probabilities for each class
        """
        df_features = self.create_features(df)
        df_features = df_features.dropna()

        seq_len = self.meta.get("seq_len", 60)
        x = self.prepare_input(df_features, seq_len)

        with torch.no_grad():
            logits = self.model(x)
            probs = F.softmax(logits, dim=1).squeeze()

        return {
            "hold": float(probs[0].cpu().item()),
            "buy": float(probs[1].cpu().item()),
            "sell": float(probs[2].cpu().item())
        }

    def generate_signal(
        self,
        df: pd.DataFrame,
        confidence_threshold: Optional[float] = None
    ) -> Dict[str, any]:
        """
        Generate trading signal based on model prediction.

        Args:
            df: DataFrame with OHLCV data
            confidence_threshold: Override default threshold

        Returns:
            Dict with signal, confidence, and probability breakdown
        """
        if confidence_threshold is None:
            confidence_threshold = self.confidence_threshold

        probs = self.predict_probs(df)

        p_hold = probs["hold"]
        p_buy = probs["buy"]
        p_sell = probs["sell"]

        # Signal generation logic
        if p_buy > confidence_threshold and p_buy > p_sell:
            signal = "BUY"
            confidence = p_buy
        elif p_sell > confidence_threshold and p_sell > p_buy:
            signal = "SELL"
            confidence = p_sell
        else:
            signal = "HOLD"
            confidence = p_hold

        return {
            "signal": signal,
            "confidence": confidence,
            "probabilities": probs,
            "threshold": confidence_threshold
        }

    def predict_batch(
        self,
        sequences: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Batch prediction for multiple sequences.

        Args:
            sequences: Array of shape (batch, seq_len, features) - already normalized

        Returns:
            Tuple of (predicted_classes, probabilities)
        """
        x = torch.tensor(sequences, dtype=torch.float32).to(self.device)

        with torch.no_grad():
            logits = self.model(x)
            probs = F.softmax(logits, dim=1)
            preds = logits.argmax(dim=1)

        return preds.cpu().numpy(), probs.cpu().numpy()


def generate_triple_barrier_signal(
    ohlcv_data: pd.DataFrame,
    model_dir: str = "models/triple_barrier",
    confidence_threshold: float = 0.5
) -> Dict[str, any]:
    """
    Convenience function to generate a trading signal.

    Args:
        ohlcv_data: DataFrame with columns [datetime, open, high, low, close, volume]
                    Needs at least 80 rows of 1-minute data
        model_dir: Path to model directory
        confidence_threshold: Minimum probability for BUY/SELL signals

    Returns:
        Dict with signal, confidence, and probability breakdown

    Example:
        >>> df = pd.read_csv('main_futures_1m.csv')
        >>> result = generate_triple_barrier_signal(df)
        >>> print(f"Signal: {result['signal']} (confidence: {result['confidence']:.2%})")
    """
    predictor = TripleBarrierPredictor(
        model_dir=model_dir,
        confidence_threshold=confidence_threshold
    )
    return predictor.generate_signal(ohlcv_data)


# Example usage
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate trading signals using triple barrier model")
    parser.add_argument("--csv", type=str, required=True, help="Input CSV with OHLCV data")
    parser.add_argument("--model-dir", type=str, default="models/triple_barrier")
    parser.add_argument("--threshold", type=float, default=0.5, help="Confidence threshold")

    args = parser.parse_args()

    # Load data
    df = pd.read_csv(args.csv, parse_dates=['datetime'])
    print(f"Loaded {len(df)} rows from {args.csv}")

    # Create predictor
    predictor = TripleBarrierPredictor(
        model_dir=args.model_dir,
        confidence_threshold=args.threshold
    )

    # Generate signal
    result = predictor.generate_signal(df)

    print("\n=== Triple Barrier Signal ===")
    print(f"Signal: {result['signal']}")
    print(f"Confidence: {result['confidence']:.2%}")
    print(f"\nProbabilities:")
    print(f"  HOLD: {result['probabilities']['hold']:.2%}")
    print(f"  BUY:  {result['probabilities']['buy']:.2%}")
    print(f"  SELL: {result['probabilities']['sell']:.2%}")
    print(f"\nThreshold: {result['threshold']:.0%}")

    # Current price context
    current_price = df['close'].iloc[-1]
    print(f"\n=== Market Context ===")
    print(f"Current price: {current_price:.2f}")
    print(f"Last update: {df['datetime'].iloc[-1]}")
