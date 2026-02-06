"""
Feature Engineer - OHLCV에서 합성 마이크로스트럭처 피처 생성

실제 틱/호가 데이터가 없을 때 OHLCV로부터 프록시 지표를 추정:
- pseudo_ofi: 거래량 가중 가격 변화
- pseudo_imbalance: 캔들 형태 기반 수급 불균형
- pseudo_spread: 상대적 변동폭
- regime: ATR 백분위 기반 변동성 레짐
"""
import numpy as np
import pandas as pd
from typing import List, Optional
from dataclasses import dataclass


@dataclass
class FeatureConfig:
    """피처 생성 설정"""
    # OFI 관련
    ofi_window: int = 20          # OFI Z-Score 계산 윈도우
    ofi_volume_weight: bool = True  # 거래량 가중치 사용

    # 변동성 레짐
    atr_period: int = 14          # ATR 기간
    regime_low_threshold: float = 0.3   # LOW 레짐 임계값 (백분위)
    regime_high_threshold: float = 0.7  # HIGH 레짐 임계값 (백분위)

    # 스프레드
    spread_window: int = 20       # 평균 스프레드 계산 윈도우

    # 기타
    min_periods: int = 20         # 최소 데이터 수


class FeatureEngineer:
    """
    OHLCV → 합성 마이크로스트럭처 피처 변환

    생성되는 피처:
    - ofi: 추정 Order Flow Imbalance
    - ofi_zscore: OFI Z-Score
    - bid_ask_imbalance: 추정 호가 불균형 (-1 ~ 1)
    - spread: 추정 스프레드
    - regime: 변동성 레짐 (LOW/MEDIUM/HIGH)
    """

    def __init__(self, config: FeatureConfig = None):
        self.config = config or FeatureConfig()

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        OHLCV DataFrame에 합성 피처 추가

        Args:
            df: OHLCV DataFrame (datetime, open, high, low, close, volume 필수)

        Returns:
            피처가 추가된 DataFrame
        """
        df = df.copy()

        # 기본 피처 계산
        df = self._add_ofi_features(df)
        df = self._add_imbalance_features(df)
        df = self._add_spread_features(df)
        df = self._add_regime_features(df)

        return df

    def _add_ofi_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        추정 OFI (Order Flow Imbalance) 계산

        실제 OFI = Σ(호가 변화에 따른 매수/매도 압력)
        추정 OFI = (close - open) * volume

        가격이 올랐으면 매수 압력(+), 내렸으면 매도 압력(-)으로 추정
        """
        # 가격 변화 (양수=상승, 음수=하락)
        price_change = df['close'] - df['open']

        # 거래량 가중 OFI
        if self.config.ofi_volume_weight:
            # 로그 거래량으로 정규화 (극단값 완화)
            log_volume = np.log1p(df['volume'])
            df['ofi'] = price_change * log_volume
        else:
            df['ofi'] = price_change

        # Z-Score 계산
        window = self.config.ofi_window
        rolling_mean = df['ofi'].rolling(window=window, min_periods=window//2).mean()
        rolling_std = df['ofi'].rolling(window=window, min_periods=window//2).std()

        # std가 0인 경우 처리
        rolling_std = rolling_std.replace(0, np.nan)
        df['ofi_zscore'] = (df['ofi'] - rolling_mean) / rolling_std
        df['ofi_zscore'] = df['ofi_zscore'].fillna(0)

        return df

    def _add_imbalance_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        추정 호가 불균형 계산

        실제 불균형 = (매수잔량 - 매도잔량) / (매수잔량 + 매도잔량)
        추정 불균형 = 캔들 내 종가 위치 (-1 ~ 1)

        - 종가가 고가에 가까우면 매수 우위 (+)
        - 종가가 저가에 가까우면 매도 우위 (-)
        """
        high_low_range = df['high'] - df['low']

        # 0 division 방지
        high_low_range = high_low_range.replace(0, np.nan)

        # 종가의 상대적 위치 (0 ~ 1)
        close_position = (df['close'] - df['low']) / high_low_range

        # -1 ~ 1 범위로 변환
        df['bid_ask_imbalance'] = close_position * 2 - 1
        df['bid_ask_imbalance'] = df['bid_ask_imbalance'].fillna(0)

        # 거래량 기반 보정 (거래량 많으면 더 신뢰)
        volume_factor = df['volume'] / df['volume'].rolling(20).mean()
        volume_factor = volume_factor.clip(0.5, 2.0).fillna(1.0)

        # 거래량이 많을 때 불균형 신호 강화
        df['bid_ask_imbalance'] = df['bid_ask_imbalance'] * np.sqrt(volume_factor)
        df['bid_ask_imbalance'] = df['bid_ask_imbalance'].clip(-1, 1)

        return df

    def _add_spread_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        추정 스프레드 계산

        실제 스프레드 = ask_price - bid_price
        KOSPI Mini: 1틱 = 0.05포인트, 일반 스프레드 = 1~2틱

        합성 스프레드: 변동폭 기반으로 1~3틱 범위로 추정
        """
        # 상대 변동폭 (%)
        relative_range = (df['high'] - df['low']) / df['close']

        # 변동폭을 1~3틱 범위로 스케일링
        # 평균 변동폭을 2틱(0.10)으로 매핑
        mean_range = relative_range.mean()
        if mean_range > 0:
            scaling_factor = 0.10 / mean_range
        else:
            scaling_factor = 1.0

        # 스프레드 = 변동폭 비율 * 스케일링 팩터 (1~3틱 = 0.05~0.15 범위로 클리핑)
        df['spread'] = (relative_range * scaling_factor).clip(0.03, 0.20)

        # 이동 평균 대비 현재 스프레드
        window = self.config.spread_window
        avg_spread = df['spread'].rolling(window=window, min_periods=window//2).mean()
        df['spread_ratio'] = df['spread'] / avg_spread
        df['spread_ratio'] = df['spread_ratio'].fillna(1.0)

        return df

    def _add_regime_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        변동성 레짐 계산

        ATR 백분위 기반:
        - LOW: ATR < 30th percentile
        - HIGH: ATR > 70th percentile
        - MEDIUM: 나머지
        """
        # ATR 계산
        period = self.config.atr_period

        high_low = df['high'] - df['low']
        high_close = abs(df['high'] - df['close'].shift(1))
        low_close = abs(df['low'] - df['close'].shift(1))

        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(window=period, min_periods=period//2).mean()

        df['atr'] = atr

        # 전체 기간 대비 백분위 계산
        # rolling percentile 사용 (최근 데이터 기준)
        lookback = 100  # 100분 (약 1.5시간)

        def rolling_percentile(s, window):
            """Rolling percentile 계산"""
            result = []
            for i in range(len(s)):
                if i < window:
                    window_data = s[:i+1]
                else:
                    window_data = s[i-window+1:i+1]

                if len(window_data) < 2:
                    result.append(0.5)
                else:
                    percentile = (window_data < s.iloc[i]).sum() / len(window_data)
                    result.append(percentile)
            return pd.Series(result, index=s.index)

        atr_percentile = rolling_percentile(atr.fillna(0), lookback)

        # 레짐 결정
        def get_regime(p):
            if p < self.config.regime_low_threshold:
                return "LOW"
            elif p > self.config.regime_high_threshold:
                return "HIGH"
            else:
                return "MEDIUM"

        df['atr_percentile'] = atr_percentile
        df['regime'] = atr_percentile.apply(get_regime)

        return df

    def get_feature_summary(self, df: pd.DataFrame) -> dict:
        """피처 통계 요약"""
        return {
            'ofi': {
                'mean': df['ofi'].mean(),
                'std': df['ofi'].std(),
                'min': df['ofi'].min(),
                'max': df['ofi'].max(),
            },
            'ofi_zscore': {
                'mean': df['ofi_zscore'].mean(),
                'std': df['ofi_zscore'].std(),
                'pct_above_2': (df['ofi_zscore'] > 2).mean() * 100,
                'pct_below_minus2': (df['ofi_zscore'] < -2).mean() * 100,
            },
            'bid_ask_imbalance': {
                'mean': df['bid_ask_imbalance'].mean(),
                'std': df['bid_ask_imbalance'].std(),
                'pct_above_0.5': (df['bid_ask_imbalance'] > 0.5).mean() * 100,
                'pct_below_minus0.5': (df['bid_ask_imbalance'] < -0.5).mean() * 100,
            },
            'regime': {
                'LOW': (df['regime'] == 'LOW').mean() * 100,
                'MEDIUM': (df['regime'] == 'MEDIUM').mean() * 100,
                'HIGH': (df['regime'] == 'HIGH').mean() * 100,
            },
            'spread': {
                'mean': df['spread'].mean(),
                'std': df['spread'].std(),
            }
        }


def prepare_backtest_data(
    df: pd.DataFrame,
    feature_config: FeatureConfig = None
) -> pd.DataFrame:
    """
    백테스트용 데이터 준비

    OHLCV 데이터에 합성 피처를 추가하고
    전략에서 사용할 수 있는 형태로 변환

    Args:
        df: OHLCV DataFrame
        feature_config: 피처 설정

    Returns:
        피처가 추가된 DataFrame
    """
    engineer = FeatureEngineer(feature_config)
    return engineer.transform(df)


def add_dl_predictions(
    df: pd.DataFrame,
    model_path: str = "models/trading_lstm.pth",
    seq_len: int = 60,
) -> pd.DataFrame:
    """
    DL 모델로 up_prob 예측값 추가

    Args:
        df: OHLCV DataFrame (datetime, open, high, low, close, volume 필수)
        model_path: 모델 파일 경로
        seq_len: 시퀀스 길이 (default: 60)

    Returns:
        up_prob 컬럼이 추가된 DataFrame
    """
    import json
    from pathlib import Path

    try:
        import torch
        import torch.nn as nn
    except ImportError:
        print("PyTorch not available, using fallback")
        df['up_prob'] = 0.5
        return df

    df = df.copy()

    # Load model metadata
    meta_path = Path(model_path).with_suffix('.json')
    if not meta_path.exists():
        print(f"Model metadata not found: {meta_path}")
        df['up_prob'] = 0.5
        return df

    with open(meta_path) as f:
        meta = json.load(f)

    # Compute DL features
    df['returns'] = df['close'].pct_change().fillna(0)
    df['ma_ratio_5'] = df['close'] / df['close'].rolling(5).mean() - 1
    df['ma_ratio_10'] = df['close'] / df['close'].rolling(10).mean() - 1
    df['ma_ratio_20'] = df['close'] / df['close'].rolling(20).mean() - 1

    # RSI
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = (100 - 100 / (1 + rs)).fillna(50) / 100

    # Bollinger position
    ma20 = df['close'].rolling(20).mean()
    std20 = df['close'].rolling(20).std()
    df['bb_position'] = ((df['close'] - ma20) / (2 * std20)).fillna(0).clip(-1, 1)

    # Volume ratio
    df['volume_ratio'] = (df['volume'] / df['volume'].rolling(20).mean()).fillna(1).clip(0, 5) / 5

    # Volatility
    df['volatility'] = df['returns'].rolling(20).std().fillna(0) * 100

    # HL range
    df['hl_range'] = ((df['high'] - df['low']) / df['close']).fillna(0)

    # Candle body
    df['candle_body'] = ((df['close'] - df['open']) / (df['high'] - df['low']).replace(0, np.nan)).fillna(0).clip(-1, 1)

    # Fill NaN
    for col in meta['features']:
        if col in df.columns:
            df[col] = df[col].fillna(0)

    # Load model
    model_path = Path(model_path)
    if not model_path.exists():
        print(f"Model not found: {model_path}")
        df['up_prob'] = 0.5
        return df

    # Create model architecture (must match training script TradingCNNLSTM)
    class TradingCNNLSTM(nn.Module):
        def __init__(
            self,
            input_dim: int,
            hidden_dim: int = 64,
            num_layers: int = 2,
            num_classes: int = 3,
            dropout: float = 0.2,
            cnn_channels: tuple = (32, 64),
            kernel_size: int = 3
        ):
            super().__init__()
            self.input_dim = input_dim
            self.cnn_channels = cnn_channels
            self.kernel_size = kernel_size

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

            # Classification head
            self.fc = nn.Sequential(
                nn.Linear(hidden_dim, 32),
                nn.ReLU(),
                nn.Dropout(dropout),
                nn.Linear(32, num_classes)
            )

        def forward(self, x):
            # x: (batch, seq_len, features)
            x = x.transpose(1, 2)  # (batch, features, seq_len)
            x = self.cnn(x)  # (batch, cnn_channels[1], seq_len // 2)
            x = x.transpose(1, 2)  # (batch, seq_len // 2, cnn_channels[1])
            lstm_out, _ = self.lstm(x)  # (batch, seq_len // 2, hidden)
            attn_weights = self.attention(lstm_out)  # (batch, seq_len // 2, 1)
            context = torch.sum(attn_weights * lstm_out, dim=1)  # (batch, hidden)
            logits = self.fc(context)
            return logits

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = TradingCNNLSTM(
        input_dim=meta['input_dim'],
        cnn_channels=tuple(meta['cnn_channels']),
        kernel_size=meta['kernel_size']
    ).to(device)

    state_dict = torch.load(model_path, map_location=device, weights_only=True)
    model.load_state_dict(state_dict)
    model.eval()

    # Prepare sequences and predict
    features = meta['features']
    up_probs = []

    with torch.no_grad():
        for i in range(len(df)):
            if i < seq_len - 1:
                up_probs.append(0.5)
            else:
                seq_data = df[features].iloc[i-seq_len+1:i+1].values
                x = torch.tensor(seq_data, dtype=torch.float32).unsqueeze(0).to(device)
                output = model(x)
                probs = torch.exp(output).cpu().numpy()[0]
                up_probs.append(float(probs[1]))  # up probability

    df['up_prob'] = up_probs
    print(f"DL predictions added: mean up_prob={df['up_prob'].mean():.3f}")

    return df


def add_ensemble_predictions(
    df: pd.DataFrame,
    model_dir: str = "models/ensemble",
    seq_len: int = 60,
) -> pd.DataFrame:
    """
    Add multi-horizon ensemble predictions to DataFrame.

    DEPRECATED: Ensemble predictor has been removed. Use triple barrier classifier
    in TrendConfirmedStrategy instead. This function now just adds default 0.5 values.

    Args:
        df: OHLCV DataFrame (datetime, open, high, low, close, volume)
        model_dir: Directory containing ensemble models (unused)
        seq_len: Sequence length (unused)

    Returns:
        DataFrame with up_prob_h1, up_prob_h3, up_prob_h5, up_prob_h10 columns (all 0.5)
    """
    df = df.copy()

    print("Note: Ensemble predictor removed. Using triple barrier in strategy layer.")
    print("Adding default 0.5 values for up_prob_h* columns...")

    df['up_prob_h1'] = 0.5
    df['up_prob_h3'] = 0.5
    df['up_prob_h5'] = 0.5
    df['up_prob_h10'] = 0.5

    return df
