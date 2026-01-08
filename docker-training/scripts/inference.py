"""
Inference Module for KOSPI Mini Futures Return Prediction

Loads trained CNN-LSTM regression ensemble and predicts tick-based returns
for mini futures based on main futures patterns.

Tick Size Reference:
- Main futures: 0.05 points per tick
- Mini futures: 0.02 points per tick

The model is trained on main futures with tick_size=0.05, predicting
"number of ticks" movement. For mini futures, we convert:
    mini_price_change = predicted_ticks * 0.02
"""
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import torch
import torch.nn as nn


class CNNLSTMRegressor(nn.Module):
    """CNN-LSTM for return regression (must match training architecture)"""

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
            nn.Linear(32, 1)
        )

    def forward(self, x):
        x = x.transpose(1, 2)
        x = self.cnn(x)
        x = x.transpose(1, 2)
        lstm_out, _ = self.lstm(x)
        attn_weights = self.attention(lstm_out)
        context = torch.sum(attn_weights * lstm_out, dim=1)
        output = self.fc(context)
        return output.squeeze(-1)


class ReturnPredictor:
    """
    Ensemble predictor for KOSPI Mini Futures returns.

    Uses trained CNN-LSTM models to predict tick-based returns at multiple horizons.
    The model is trained on main futures (tick=0.05) and outputs are converted
    to mini futures (tick=0.02) for direct application.

    Tick Conversion:
    - Model output: predicted number of ticks (trained on main futures)
    - For mini futures: price_change = predicted_ticks * MINI_TICK_SIZE
    """

    MAIN_TICK_SIZE = 0.05  # Main futures tick size
    MINI_TICK_SIZE = 0.02  # Mini futures tick size

    def __init__(
        self,
        model_dir: str = "models/regression",
        horizons: List[int] = [1, 3, 5, 10],
        device: str = "auto"
    ):
        """
        Initialize the predictor.

        Args:
            model_dir: Directory containing trained models
            horizons: List of prediction horizons to load
            device: Device to use (auto/cpu/cuda/mps)
        """
        self.model_dir = Path(model_dir)
        self.horizons = horizons

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

        # Load scaler
        self.scaler = self._load_scaler()

        # Load models and their metadata
        self.models = {}
        self.model_meta = {}
        for h in horizons:
            self.models[h], self.model_meta[h] = self._load_model(h)

        print(f"Loaded {len(self.models)} models on {self.device}")

    def _load_scaler(self) -> Dict:
        """Load feature scaler parameters"""
        scaler_path = self.model_dir / "scaler.json"
        with open(scaler_path) as f:
            scaler = json.load(f)
        return scaler

    def _load_model(self, horizon: int) -> Tuple[nn.Module, Dict]:
        """Load a single horizon model and its metadata"""
        model_path = self.model_dir / f"model_h{horizon}.pth"
        meta_path = self.model_dir / f"model_h{horizon}.json"

        # Load metadata
        with open(meta_path) as f:
            meta = json.load(f)

        # Create model
        model = CNNLSTMRegressor(input_dim=meta["input_dim"])
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

        return df

    def prepare_input(self, df: pd.DataFrame, seq_len: int = 60) -> torch.Tensor:
        """
        Prepare input tensor for model inference.

        Args:
            df: DataFrame with features
            seq_len: Sequence length (default 60 for 1-hour lookback)

        Returns:
            Tensor of shape (1, seq_len, num_features)
        """
        feature_cols = self.scaler['features']

        # Get last seq_len rows
        if len(df) < seq_len:
            raise ValueError(f"Need at least {seq_len} rows, got {len(df)}")

        features = df[feature_cols].iloc[-seq_len:].values

        # Apply scaler (Z-score normalization)
        mean = np.array(self.scaler['mean'])
        scale = np.array(self.scaler['scale'])
        features = (features - mean) / scale

        # Convert to tensor
        tensor = torch.tensor(features, dtype=torch.float32).unsqueeze(0)
        return tensor.to(self.device)

    def predict(
        self,
        df: pd.DataFrame,
        horizons: Optional[List[int]] = None
    ) -> Dict[int, float]:
        """
        Predict tick-based returns for specified horizons.

        Args:
            df: DataFrame with OHLCV data (needs at least 60 rows after feature creation)
            horizons: List of horizons to predict (default: all loaded)

        Returns:
            Dict mapping horizon -> predicted ticks (number of tick movements)
        """
        if horizons is None:
            horizons = self.horizons

        # Create features
        df_features = self.create_features(df)
        df_features = df_features.dropna()

        # Prepare input
        x = self.prepare_input(df_features)

        # Predict for each horizon
        predictions = {}
        with torch.no_grad():
            for h in horizons:
                if h in self.models:
                    pred = self.models[h](x)
                    predictions[h] = float(pred.cpu().item())

        return predictions

    def predict_mini_futures(
        self,
        df: pd.DataFrame,
        horizons: Optional[List[int]] = None
    ) -> Dict[int, Dict[str, float]]:
        """
        Predict price changes for mini futures.

        Converts tick-based predictions to mini futures price changes.

        Args:
            df: DataFrame with OHLCV data
            horizons: List of horizons to predict

        Returns:
            Dict mapping horizon -> {ticks, price_change, direction}
        """
        tick_predictions = self.predict(df, horizons)

        results = {}
        for h, ticks in tick_predictions.items():
            price_change = ticks * self.MINI_TICK_SIZE
            results[h] = {
                "ticks": ticks,
                "price_change": price_change,
                "direction": "UP" if ticks > 0 else "DOWN" if ticks < 0 else "HOLD"
            }

        return results

    def predict_ensemble(
        self,
        df: pd.DataFrame,
        weights: Optional[Dict[int, float]] = None
    ) -> Tuple[float, Dict[int, float]]:
        """
        Predict weighted ensemble tick return.

        Args:
            df: DataFrame with OHLCV data
            weights: Dict of horizon -> weight (default: equal weights)

        Returns:
            Tuple of (ensemble_ticks, individual_tick_predictions)
        """
        predictions = self.predict(df)

        if weights is None:
            # Default: equal weights
            weights = {h: 1.0 / len(predictions) for h in predictions}

        # Weighted average of tick predictions
        ensemble_ticks = sum(
            predictions[h] * weights.get(h, 0)
            for h in predictions
        )

        return ensemble_ticks, predictions

    def predict_ensemble_mini(
        self,
        df: pd.DataFrame,
        weights: Optional[Dict[int, float]] = None
    ) -> Dict[str, float]:
        """
        Predict ensemble for mini futures with price conversion.

        Args:
            df: DataFrame with OHLCV data
            weights: Dict of horizon -> weight

        Returns:
            Dict with ensemble_ticks, mini_price_change, direction
        """
        ensemble_ticks, individual = self.predict_ensemble(df, weights)

        return {
            "ensemble_ticks": ensemble_ticks,
            "mini_price_change": ensemble_ticks * self.MINI_TICK_SIZE,
            "main_price_change": ensemble_ticks * self.MAIN_TICK_SIZE,
            "direction": "UP" if ensemble_ticks > 0 else "DOWN" if ensemble_ticks < 0 else "HOLD",
            "individual_ticks": individual
        }


def predict_mini_futures_return(
    ohlcv_data: pd.DataFrame,
    model_dir: str = "models/regression",
    horizon: int = 5
) -> Dict[str, float]:
    """
    Convenience function to predict mini futures price change.

    Args:
        ohlcv_data: DataFrame with columns [datetime, open, high, low, close, volume]
                   Needs at least 80 rows of 1-minute data
        model_dir: Path to model directory
        horizon: Prediction horizon in minutes

    Returns:
        Dict with ticks, price_change, direction

    Example:
        >>> df = pd.read_csv('main_futures_1m.csv')
        >>> result = predict_mini_futures_return(df, horizon=5)
        >>> print(f"Predicted ticks: {result['ticks']:.2f}")
        >>> print(f"Mini futures price change: {result['price_change']:.4f}")
        >>> print(f"Direction: {result['direction']}")
    """
    predictor = ReturnPredictor(model_dir=model_dir, horizons=[horizon])
    results = predictor.predict_mini_futures(ohlcv_data, horizons=[horizon])
    return results[horizon]


# Example usage
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Predict KOSPI Mini Futures returns")
    parser.add_argument("--csv", type=str, required=True, help="Input CSV with OHLCV data")
    parser.add_argument("--model-dir", type=str, default="models/regression")
    parser.add_argument("--horizon", type=int, default=5, help="Prediction horizon")

    args = parser.parse_args()

    # Load data
    df = pd.read_csv(args.csv, parse_dates=['datetime'])
    print(f"Loaded {len(df)} rows from {args.csv}")

    # Create predictor
    predictor = ReturnPredictor(model_dir=args.model_dir)

    # Predict ensemble for mini futures
    result = predictor.predict_ensemble_mini(df)

    print("\n=== Tick-Based Predictions ===")
    print(f"(Model trained on main futures tick size: {predictor.MAIN_TICK_SIZE})")
    print(f"(Converting to mini futures tick size: {predictor.MINI_TICK_SIZE})")

    print("\nIndividual horizon predictions (ticks):")
    for h, ticks in sorted(result["individual_ticks"].items()):
        mini_change = ticks * predictor.MINI_TICK_SIZE
        print(f"  H={h:2d}: {ticks:+.2f} ticks -> Mini: {mini_change:+.4f} pts")

    print(f"\n=== Ensemble Result ===")
    print(f"Predicted ticks: {result['ensemble_ticks']:+.2f}")
    print(f"Direction: {result['direction']}")
    print(f"Main futures change: {result['main_price_change']:+.4f} pts")
    print(f"Mini futures change: {result['mini_price_change']:+.4f} pts")

    # Current price context
    current_price = df['close'].iloc[-1]
    print(f"\n=== Price Context ===")
    print(f"Current price: {current_price:.2f}")
    print(f"Expected mini price: {current_price + result['mini_price_change']:.2f}")
