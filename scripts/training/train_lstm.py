"""
LSTM 모델 학습 스크립트
ClickHouse에서 1분봉 데이터를 가져와 가격 방향 예측 모델 학습
"""
import os
import sys
import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# PyTorch
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

# 프로젝트 경로 추가
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings


# ============================================================
# 데이터 로딩
# ============================================================

def load_data_from_clickhouse(
    host: str = "localhost",
    port: int = 18123,
    database: str = "kospi",
    password: str = "@1tidh6ls6ls",
    days: int = 30
) -> pd.DataFrame:
    """
    ClickHouse에서 1분봉 데이터 로드

    Args:
        host: ClickHouse 호스트
        port: ClickHouse 포트
        database: 데이터베이스명
        password: 비밀번호
        days: 최근 N일 데이터

    Returns:
        DataFrame with columns: datetime, code, open, high, low, close, volume
    """
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=host,
        port=port,
        database=database,
        username="default",
        password=password
    )

    query = f"""
        SELECT
            datetime,
            code,
            open,
            high,
            low,
            close,
            volume
        FROM kospi_mini_1m
        WHERE datetime >= now() - INTERVAL {days} DAY
        ORDER BY datetime ASC
    """

    result = client.query(query)
    df = pd.DataFrame(
        result.result_rows,
        columns=['datetime', 'code', 'open', 'high', 'low', 'close', 'volume']
    )

    print(f"Loaded {len(df)} rows from ClickHouse")
    return df


def load_data_from_csv(filepath: str) -> pd.DataFrame:
    """CSV 파일에서 데이터 로드 (테스트용)"""
    df = pd.read_csv(filepath, parse_dates=['datetime'])
    print(f"Loaded {len(df)} rows from {filepath}")
    return df


# ============================================================
# Feature 엔지니어링
# ============================================================

def create_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    기술적 지표 및 Feature 생성

    Features:
    - 가격 변화율 (returns)
    - 이동평균 (MA)
    - RSI
    - 볼린저 밴드
    - 거래량 변화
    - OFI (간소화 버전)
    """
    df = df.copy()
    df = df.sort_values('datetime').reset_index(drop=True)

    # 1. 가격 변화율
    df['returns'] = df['close'].pct_change()

    # 2. 이동평균
    for window in [5, 10, 20]:
        df[f'ma_{window}'] = df['close'].rolling(window=window).mean()
        df[f'ma_ratio_{window}'] = df['close'] / df[f'ma_{window}']

    # 3. RSI (14 기간)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / (loss + 1e-10)
    df['rsi'] = 100 - (100 / (1 + rs))

    # 4. 볼린저 밴드
    df['bb_mid'] = df['close'].rolling(window=20).mean()
    df['bb_std'] = df['close'].rolling(window=20).std()
    df['bb_upper'] = df['bb_mid'] + 2 * df['bb_std']
    df['bb_lower'] = df['bb_mid'] - 2 * df['bb_std']
    df['bb_position'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'] + 1e-10)

    # 5. 거래량 변화
    df['volume_ma'] = df['volume'].rolling(window=20).mean()
    df['volume_ratio'] = df['volume'] / (df['volume_ma'] + 1)

    # 6. 변동성
    df['volatility'] = df['returns'].rolling(window=20).std()

    # 7. 고가-저가 범위
    df['hl_range'] = (df['high'] - df['low']) / df['close']

    # 8. 캔들 패턴 (간소화)
    df['candle_body'] = (df['close'] - df['open']) / (df['high'] - df['low'] + 1e-10)

    return df


def create_labels(df: pd.DataFrame, horizon: int = 5, threshold: float = 0.001) -> pd.DataFrame:
    """
    레이블 생성: 미래 N분 후 가격 방향

    Labels:
    - 0: Hold (변화 < threshold)
    - 1: Up (상승 >= threshold)
    - 2: Down (하락 <= -threshold)

    Args:
        df: Feature DataFrame
        horizon: 예측 기간 (분)
        threshold: 방향 결정 임계값 (0.1% = 0.001)
    """
    df = df.copy()

    # 미래 가격
    df['future_close'] = df['close'].shift(-horizon)
    df['future_return'] = (df['future_close'] - df['close']) / df['close']

    # 레이블 생성
    df['label'] = 0  # Hold
    df.loc[df['future_return'] >= threshold, 'label'] = 1  # Up
    df.loc[df['future_return'] <= -threshold, 'label'] = 2  # Down

    return df


# ============================================================
# 데이터셋 클래스
# ============================================================

class TimeSeriesDataset(Dataset):
    """시계열 데이터셋"""

    def __init__(self, features: np.ndarray, labels: np.ndarray, seq_len: int = 60):
        """
        Args:
            features: (N, num_features) 형태
            labels: (N,) 형태
            seq_len: 시퀀스 길이
        """
        self.features = features
        self.labels = labels
        self.seq_len = seq_len

    def __len__(self):
        return len(self.features) - self.seq_len

    def __getitem__(self, idx):
        # 시퀀스 추출
        x = self.features[idx:idx + self.seq_len]
        y = self.labels[idx + self.seq_len - 1]

        return (
            torch.tensor(x, dtype=torch.float32),
            torch.tensor(y, dtype=torch.long)
        )


# ============================================================
# 모델 정의
# ============================================================

class TradingLSTM(nn.Module):
    """LSTM 기반 가격 방향 예측 모델"""

    def __init__(
        self,
        input_dim: int,
        hidden_dim: int = 64,
        num_layers: int = 2,
        num_classes: int = 3,
        dropout: float = 0.2
    ):
        super().__init__()

        self.lstm = nn.LSTM(
            input_dim,
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
        # LSTM
        lstm_out, _ = self.lstm(x)  # (batch, seq_len, hidden)

        # Attention
        attn_weights = self.attention(lstm_out)  # (batch, seq_len, 1)
        context = torch.sum(attn_weights * lstm_out, dim=1)  # (batch, hidden)

        # Classification
        logits = self.fc(context)
        return logits


# ============================================================
# 학습 함수
# ============================================================

def train_epoch(model, loader, criterion, optimizer, device):
    """한 에폭 학습"""
    model.train()
    total_loss = 0
    correct = 0
    total = 0

    for batch_x, batch_y in loader:
        batch_x = batch_x.to(device)
        batch_y = batch_y.to(device)

        optimizer.zero_grad()
        outputs = model(batch_x)
        loss = criterion(outputs, batch_y)
        loss.backward()
        optimizer.step()

        total_loss += loss.item()
        _, predicted = outputs.max(1)
        total += batch_y.size(0)
        correct += predicted.eq(batch_y).sum().item()

    return total_loss / len(loader), 100. * correct / total


def evaluate(model, loader, criterion, device):
    """평가"""
    model.eval()
    total_loss = 0
    correct = 0
    total = 0

    with torch.no_grad():
        for batch_x, batch_y in loader:
            batch_x = batch_x.to(device)
            batch_y = batch_y.to(device)

            outputs = model(batch_x)
            loss = criterion(outputs, batch_y)

            total_loss += loss.item()
            _, predicted = outputs.max(1)
            total += batch_y.size(0)
            correct += predicted.eq(batch_y).sum().item()

    return total_loss / len(loader), 100. * correct / total


def train_model(
    train_loader: DataLoader,
    val_loader: DataLoader,
    input_dim: int,
    epochs: int = 100,
    lr: float = 0.001,
    device: str = "cpu",
    save_path: str = "model.pth"
):
    """모델 학습"""
    model = TradingLSTM(input_dim=input_dim).to(device)
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=lr)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=10, factor=0.5)

    best_val_acc = 0
    patience_counter = 0
    early_stop_patience = 20

    print(f"\nTraining on {device}")
    print(f"Model parameters: {sum(p.numel() for p in model.parameters()):,}")

    for epoch in range(epochs):
        train_loss, train_acc = train_epoch(model, train_loader, criterion, optimizer, device)
        val_loss, val_acc = evaluate(model, val_loader, criterion, device)

        scheduler.step(val_loss)

        print(f"Epoch {epoch+1:3d}/{epochs} | "
              f"Train Loss: {train_loss:.4f} Acc: {train_acc:.2f}% | "
              f"Val Loss: {val_loss:.4f} Acc: {val_acc:.2f}%")

        # Best model 저장
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            patience_counter = 0
            torch.save(model.state_dict(), save_path)
            print(f"  -> Best model saved (Val Acc: {val_acc:.2f}%)")
        else:
            patience_counter += 1

        # Early stopping
        if patience_counter >= early_stop_patience:
            print(f"Early stopping at epoch {epoch+1}")
            break

    print(f"\nBest Validation Accuracy: {best_val_acc:.2f}%")
    return model


# ============================================================
# 메인 함수
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Train LSTM model for trading")
    parser.add_argument("--data-source", choices=["clickhouse", "csv"], default="clickhouse")
    parser.add_argument("--csv-path", type=str, help="CSV file path (if data-source=csv)")
    parser.add_argument("--days", type=int, default=30, help="Days of data to load")
    parser.add_argument("--seq-len", type=int, default=60, help="Sequence length")
    parser.add_argument("--horizon", type=int, default=5, help="Prediction horizon (minutes)")
    parser.add_argument("--threshold", type=float, default=0.001, help="Label threshold")
    parser.add_argument("--epochs", type=int, default=100, help="Training epochs")
    parser.add_argument("--batch-size", type=int, default=64, help="Batch size")
    parser.add_argument("--lr", type=float, default=0.001, help="Learning rate")
    parser.add_argument("--device", type=str, default="auto", help="Device (cpu/cuda/mps/auto)")
    parser.add_argument("--output", type=str, default="models/trading_lstm.pth", help="Output model path")

    args = parser.parse_args()

    # Device 설정
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

    # 1. 데이터 로드
    print("\n=== Loading Data ===")
    if args.data_source == "clickhouse":
        df = load_data_from_clickhouse(days=args.days)
    else:
        df = load_data_from_csv(args.csv_path)

    if len(df) < 1000:
        print(f"Warning: Only {len(df)} rows. Need more data for training.")
        return

    # 2. Feature 생성
    print("\n=== Creating Features ===")
    df = create_features(df)
    df = create_labels(df, horizon=args.horizon, threshold=args.threshold)

    # NaN 제거
    df = df.dropna()
    print(f"After feature creation: {len(df)} rows")

    # Label 분포 확인
    label_counts = df['label'].value_counts().sort_index()
    print(f"Label distribution:\n{label_counts}")

    # 3. Feature 선택
    feature_cols = [
        'returns', 'ma_ratio_5', 'ma_ratio_10', 'ma_ratio_20',
        'rsi', 'bb_position', 'volume_ratio', 'volatility',
        'hl_range', 'candle_body'
    ]

    # 사용 가능한 컬럼만 선택
    feature_cols = [c for c in feature_cols if c in df.columns]
    print(f"Features: {feature_cols}")

    X = df[feature_cols].values
    y = df['label'].values

    # 4. 정규화
    scaler = StandardScaler()
    X = scaler.fit_transform(X)

    # Scaler 저장
    scaler_path = Path(args.output).parent / "scaler.json"
    scaler_path.parent.mkdir(parents=True, exist_ok=True)
    scaler_params = {
        "mean": scaler.mean_.tolist(),
        "scale": scaler.scale_.tolist(),
        "features": feature_cols
    }
    with open(scaler_path, "w") as f:
        json.dump(scaler_params, f)
    print(f"Scaler saved to {scaler_path}")

    # 5. Train/Val 분할 (시계열이므로 shuffle=False)
    split_idx = int(len(X) * 0.8)
    X_train, X_val = X[:split_idx], X[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]

    print(f"Train: {len(X_train)}, Val: {len(X_val)}")

    # 6. 데이터셋 생성
    train_dataset = TimeSeriesDataset(X_train, y_train, seq_len=args.seq_len)
    val_dataset = TimeSeriesDataset(X_val, y_val, seq_len=args.seq_len)

    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False)

    print(f"Train batches: {len(train_loader)}, Val batches: {len(val_loader)}")

    # 7. 모델 학습
    print("\n=== Training Model ===")
    model = train_model(
        train_loader,
        val_loader,
        input_dim=len(feature_cols),
        epochs=args.epochs,
        lr=args.lr,
        device=device,
        save_path=args.output
    )

    # 8. 모델 메타데이터 저장
    meta_path = Path(args.output).with_suffix(".json")
    meta = {
        "input_dim": len(feature_cols),
        "seq_len": args.seq_len,
        "horizon": args.horizon,
        "threshold": args.threshold,
        "features": feature_cols,
        "created_at": datetime.now().isoformat(),
        "train_samples": len(train_dataset),
        "val_samples": len(val_dataset)
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"Model metadata saved to {meta_path}")

    print("\n=== Training Complete ===")
    print(f"Model saved to: {args.output}")


if __name__ == "__main__":
    main()
