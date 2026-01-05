"""
Ensemble Predictor for Multi-Horizon CNN-LSTM Models

Loads and runs multiple models (1, 3, 5, 10 minute horizons)
and combines predictions using "Shortest Confirms Longest" strategy.
"""
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass

import numpy as np

try:
    import torch
    import torch.nn as nn
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    torch = None
    nn = None

from src.common import setup_logging

logger = setup_logging("ensemble")

# Default horizons for mid-frequency trading
DEFAULT_HORIZONS = [1, 3, 5, 10]


@dataclass
class EnsembleSignal:
    """Ensemble prediction result"""
    direction: str  # "LONG", "SHORT", "HOLD"
    confidence: float  # 0.0 - 1.0
    probabilities: Dict[int, float]  # {horizon: up_prob}
    trigger_horizon: int  # Which horizon triggered (10 for direction)
    confirm_horizon: int  # Which horizon confirmed (1 or 3)


if TORCH_AVAILABLE:
    class TradingCNNLSTM(nn.Module):
        """CNN-LSTM model (same as in prediction_engine.py)"""

        def __init__(
            self,
            input_dim: int = 10,
            hidden_dim: int = 64,
            num_layers: int = 2,
            num_classes: int = 3,
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


class EnsemblePredictor:
    """
    Multi-horizon ensemble predictor.

    Loads multiple CNN-LSTM models for different prediction horizons
    and combines them using "Shortest Confirms Longest" strategy.
    """

    def __init__(
        self,
        model_dir: str = "models/ensemble",
        horizons: List[int] = None,
        device: str = "auto"
    ):
        self.model_dir = Path(model_dir)
        self.horizons = horizons or DEFAULT_HORIZONS
        self.device = self._resolve_device(device)
        self.models: Dict[int, nn.Module] = {}
        self.metadata: Dict[int, dict] = {}

        # Entry thresholds for "Shortest Confirms Longest"
        self.h10_long_threshold = 0.70   # h10 > 0.70 for LONG
        self.h10_short_threshold = 0.30  # h10 < 0.30 for SHORT
        self.confirm_long_threshold = 0.60  # h1/h3 > 0.60 confirms LONG
        self.confirm_short_threshold = 0.40  # h1/h3 < 0.40 confirms SHORT

        if not TORCH_AVAILABLE:
            logger.warning("PyTorch not available. Using mock predictions.")

    def _resolve_device(self, device: str) -> str:
        if device == "auto":
            if TORCH_AVAILABLE:
                if torch.cuda.is_available():
                    return "cuda"
                elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                    return "mps"
            return "cpu"
        return device

    def load_models(self) -> bool:
        """Load all horizon models."""
        if not TORCH_AVAILABLE:
            return False

        loaded = 0
        for h in self.horizons:
            model_path = self.model_dir / f"model_h{h}.pth"
            meta_path = self.model_dir / f"model_h{h}.json"

            if not model_path.exists():
                logger.warning(f"Model not found: {model_path}")
                continue

            try:
                # Load metadata
                meta = {}
                if meta_path.exists():
                    with open(meta_path) as f:
                        meta = json.load(f)
                self.metadata[h] = meta

                input_dim = meta.get('input_dim', 10)
                cnn_channels = tuple(meta.get('cnn_channels', [32, 64]))
                kernel_size = meta.get('kernel_size', 3)

                # Create and load model
                model = TradingCNNLSTM(
                    input_dim=input_dim,
                    cnn_channels=cnn_channels,
                    kernel_size=kernel_size
                )
                state_dict = torch.load(model_path, map_location=self.device, weights_only=True)
                model.load_state_dict(state_dict)
                model.to(self.device)
                model.eval()

                self.models[h] = model
                loaded += 1
                logger.info(f"Loaded model H={h}: {model_path}")

            except Exception as e:
                logger.error(f"Failed to load model H={h}: {e}")

        logger.info(f"Ensemble loaded: {loaded}/{len(self.horizons)} models")
        return loaded > 0

    def predict(self, sequence: np.ndarray) -> Dict[int, float]:
        """
        Run inference on all loaded models.

        Args:
            sequence: (seq_len, features) input array

        Returns:
            Dict mapping horizon -> up_probability
        """
        if not TORCH_AVAILABLE or not self.models:
            # Mock predictions
            return {h: 0.5 for h in self.horizons}

        results = {}
        try:
            with torch.no_grad():
                x = torch.tensor(sequence, dtype=torch.float32)
                x = x.unsqueeze(0).to(self.device)

                for h, model in self.models.items():
                    output = model(x)
                    probs = torch.softmax(output, dim=1).cpu().numpy()[0]
                    # probs: [hold, up, down]
                    results[h] = float(probs[1])  # up_prob

        except Exception as e:
            logger.error(f"Ensemble prediction failed: {e}")
            results = {h: 0.5 for h in self.horizons}

        return results

    def get_signal(self, sequence: np.ndarray) -> EnsembleSignal:
        """
        Get ensemble trading signal using "Shortest Confirms Longest" strategy.

        h10 sets direction, h1/h3 confirm timing.
        """
        probs = self.predict(sequence)

        h1 = probs.get(1, 0.5)
        h3 = probs.get(3, 0.5)
        h5 = probs.get(5, 0.5)
        h10 = probs.get(10, 0.5)

        # LONG: h10 bullish + short-term confirmation
        if h10 > self.h10_long_threshold:
            if h1 > self.confirm_long_threshold:
                confidence = (h10 + h1) / 2
                return EnsembleSignal(
                    direction="LONG",
                    confidence=confidence,
                    probabilities=probs,
                    trigger_horizon=10,
                    confirm_horizon=1
                )
            elif h3 > self.confirm_long_threshold:
                confidence = (h10 + h3) / 2
                return EnsembleSignal(
                    direction="LONG",
                    confidence=confidence,
                    probabilities=probs,
                    trigger_horizon=10,
                    confirm_horizon=3
                )

        # SHORT: h10 bearish + short-term confirmation
        if h10 < self.h10_short_threshold:
            if h1 < self.confirm_short_threshold:
                confidence = ((1 - h10) + (1 - h1)) / 2
                return EnsembleSignal(
                    direction="SHORT",
                    confidence=confidence,
                    probabilities=probs,
                    trigger_horizon=10,
                    confirm_horizon=1
                )
            elif h3 < self.confirm_short_threshold:
                confidence = ((1 - h10) + (1 - h3)) / 2
                return EnsembleSignal(
                    direction="SHORT",
                    confidence=confidence,
                    probabilities=probs,
                    trigger_horizon=10,
                    confirm_horizon=3
                )

        # HOLD: no clear signal
        return EnsembleSignal(
            direction="HOLD",
            confidence=0.5,
            probabilities=probs,
            trigger_horizon=0,
            confirm_horizon=0
        )

    def check_exit(self, probs: Dict[int, float], current_direction: str) -> Tuple[bool, str]:
        """
        Check if position should be exited based on ensemble.

        Args:
            probs: Current predictions {horizon: up_prob}
            current_direction: "LONG" or "SHORT"

        Returns:
            (should_exit, reason)
        """
        h1 = probs.get(1, 0.5)
        h3 = probs.get(3, 0.5)
        h10 = probs.get(10, 0.5)

        if current_direction == "LONG":
            # h10 reverses -> exit
            if h10 < 0.40:
                return True, "H10_REVERSAL"
            # h1 AND h3 both bearish -> tighten (return as warning)
            if h1 < 0.40 and h3 < 0.40:
                return False, "TIGHTEN_STOP"

        elif current_direction == "SHORT":
            # h10 reverses -> exit
            if h10 > 0.60:
                return True, "H10_REVERSAL"
            # h1 AND h3 both bullish -> tighten
            if h1 > 0.60 and h3 > 0.60:
                return False, "TIGHTEN_STOP"

        return False, "HOLD"

    def get_stats(self) -> dict:
        """Get ensemble statistics."""
        return {
            "loaded_models": list(self.models.keys()),
            "total_horizons": len(self.horizons),
            "device": self.device,
            "thresholds": {
                "h10_long": self.h10_long_threshold,
                "h10_short": self.h10_short_threshold,
                "confirm_long": self.confirm_long_threshold,
                "confirm_short": self.confirm_short_threshold,
            }
        }
