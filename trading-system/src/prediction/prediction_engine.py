"""
Prediction Engine 모듈
FEATURE_STREAM → PREDICTION_STREAM
딥러닝 모델 실시간 추론
"""
import time
import json
import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

import numpy as np

# PyTorch는 선택적 임포트
try:
    import torch
    import torch.nn as nn
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False
    torch = None
    nn = None

import sys
sys.path.insert(0, '/Users/harris/Development/trading-system')
from config.settings import settings
from src.common import (
    StreamConsumer, 
    StreamPublisher, 
    StreamMessage, 
    RedisClient,
    setup_logging
)

logger = setup_logging("prediction")


# ============================================================
# 딥러닝 모델 정의 (예시)
# ============================================================

if TORCH_AVAILABLE:
    class TradingLSTM(nn.Module):
        """
        LSTM 기반 가격 방향 예측 모델
        입력: (batch, seq_len, features)
        출력: (batch, 3) - [Hold, Up, Down] 확률
        """
        
        def __init__(
            self,
            input_dim: int = 8,
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
                dropout=dropout if num_layers > 1 else 0
            )
            
            self.fc = nn.Sequential(
                nn.Linear(hidden_dim, 32),
                nn.ReLU(),
                nn.Dropout(dropout),
                nn.Linear(32, num_classes)
            )
            
            self.softmax = nn.LogSoftmax(dim=1)
        
        def forward(self, x):
            # LSTM
            lstm_out, _ = self.lstm(x)
            # 마지막 타임스텝만 사용
            last_hidden = lstm_out[:, -1, :]
            # FC
            logits = self.fc(last_hidden)
            return self.softmax(logits)


@dataclass
class PredictionResult:
    """예측 결과"""
    symbol: str
    timestamp: float
    up_prob: float
    down_prob: float
    hold_prob: float
    model_version: str
    inference_time_ms: float


class ModelManager:
    """
    모델 로딩 및 관리
    """
    
    def __init__(self, model_path: str = None, device: str = None):
        self.model_path = model_path or settings.model.model_path
        self.device = device or settings.model.device
        self.model = None
        self.model_version = settings.model.model_version
        
        if not TORCH_AVAILABLE:
            logger.warning("PyTorch not available. Using mock predictions.")
            return
        
        # GPU 사용 가능 여부 확인
        if self.device == "cuda" and not torch.cuda.is_available():
            logger.warning("CUDA not available. Falling back to CPU.")
            self.device = "cpu"
    
    def load_model(self):
        """모델 로드"""
        if not TORCH_AVAILABLE:
            return
        
        try:
            # 모델 아키텍처 생성
            self.model = TradingLSTM()
            
            # 가중치 로드 (파일이 있는 경우)
            try:
                state_dict = torch.load(self.model_path, map_location=self.device)
                self.model.load_state_dict(state_dict)
                logger.info(f"Model loaded from {self.model_path}")
            except FileNotFoundError:
                logger.warning(f"Model file not found: {self.model_path}. Using random weights.")
            
            self.model.to(self.device)
            self.model.eval()
            logger.info(f"Model ready on {self.device}")
            
        except Exception as e:
            logger.error(f"Failed to load model: {e}")
            self.model = None
    
    def predict(self, sequence: np.ndarray) -> Optional[np.ndarray]:
        """
        추론 실행
        
        Args:
            sequence: (seq_len, features) 형태의 입력
        
        Returns:
            (3,) 형태의 확률 [hold, up, down]
        """
        if not TORCH_AVAILABLE or self.model is None:
            # Mock prediction
            return np.array([0.3, 0.5, 0.2])
        
        try:
            with torch.no_grad():
                # 입력 텐서 생성
                x = torch.tensor(sequence, dtype=torch.float32)
                x = x.unsqueeze(0).to(self.device)  # (1, seq_len, features)
                
                # 추론
                output = self.model(x)
                probs = torch.exp(output).cpu().numpy()[0]
                
                return probs
        except Exception as e:
            logger.error(f"Prediction failed: {e}")
            return None


class PredictionEngine(StreamConsumer):
    """
    Prediction Engine 메인 클래스
    FEATURE_STREAM → PREDICTION_STREAM
    """
    
    FEATURE_COLUMNS = [
        'ofi_z_score', 'ofi_cumulative', 'liquidity_score',
        'mid_price', 'spread', 'bid_qty_total', 'ask_qty_total'
    ]
    
    def __init__(self):
        super().__init__(
            stream_name=settings.redis.feature_stream,
            group_name=settings.consumer.prediction_group,
            consumer_name="prediction_1"
        )
        self.publisher = StreamPublisher(settings.redis.prediction_stream)
        self.redis = RedisClient.get_client()
        
        # 모델 매니저
        self.model_manager = ModelManager()
        self.lookback = settings.model.lookback_window
        
        self._inference_count = 0
        self._total_inference_time = 0.0
    
    def start(self):
        """Prediction Engine 시작"""
        # 모델 로드
        self.model_manager.load_model()
        
        # Consumer 루프 시작
        self.run()
    
    def _get_feature_window(self, symbol: str) -> Optional[List[Dict]]:
        """
        Redis에서 Feature Rolling Window 조회
        """
        key = f"feature_window:{symbol}"
        try:
            data = self.redis.get(key)
            if data:
                return json.loads(data)
        except Exception as e:
            logger.error(f"Failed to get feature window: {e}")
        return None
    
    def _prepare_sequence(self, features: List[Dict]) -> Optional[np.ndarray]:
        """
        Feature 리스트를 모델 입력용 시퀀스로 변환
        """
        if len(features) < self.lookback:
            logger.debug(f"Insufficient features: {len(features)} < {self.lookback}")
            return None
        
        # 최근 lookback개만 사용
        recent = features[-self.lookback:]
        
        # numpy array로 변환
        sequence = []
        for f in recent:
            row = []
            for col in self.FEATURE_COLUMNS:
                val = f.get(col, 0)
                row.append(float(val) if val else 0.0)
            sequence.append(row)
        
        return np.array(sequence)
    
    def process_message(self, message: StreamMessage) -> bool:
        """
        Feature 메시지 처리 및 예측 실행
        """
        try:
            data = message.data
            symbol = data.get('symbol')
            
            if not symbol:
                return True
            
            # 1. Feature Window 조회
            features = self._get_feature_window(symbol)
            if not features:
                return True  # 데이터 부족, 스킵
            
            # 2. 시퀀스 준비
            sequence = self._prepare_sequence(features)
            if sequence is None:
                return True  # 데이터 부족, 스킵
            
            # 3. 추론 실행
            start_time = time.time()
            probs = self.model_manager.predict(sequence)
            inference_time_ms = (time.time() - start_time) * 1000
            
            if probs is None:
                return True
            
            # 4. 결과 구성
            result = PredictionResult(
                symbol=symbol,
                timestamp=time.time(),
                hold_prob=float(probs[0]),
                up_prob=float(probs[1]),
                down_prob=float(probs[2]),
                model_version=self.model_manager.model_version,
                inference_time_ms=inference_time_ms
            )
            
            # 5. PREDICTION_STREAM에 발행
            self.publisher.publish({
                'symbol': result.symbol,
                'timestamp': result.timestamp,
                'up_prob': result.up_prob,
                'down_prob': result.down_prob,
                'hold_prob': result.hold_prob,
                'model_version': result.model_version,
                'inference_time_ms': result.inference_time_ms
            })
            
            # 통계 업데이트
            self._inference_count += 1
            self._total_inference_time += inference_time_ms
            
            if self._inference_count % 100 == 0:
                avg_time = self._total_inference_time / self._inference_count
                logger.info(
                    f"Predictions: {self._inference_count}, "
                    f"Avg inference: {avg_time:.2f}ms"
                )
            
            logger.debug(
                f"Prediction: {symbol} Up={result.up_prob:.2f} "
                f"Down={result.down_prob:.2f} ({inference_time_ms:.1f}ms)"
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Error in prediction: {e}", exc_info=True)
            return False


def main():
    """Prediction Engine 실행"""
    logger.info("Starting Prediction Engine...")
    engine = PredictionEngine()
    
    try:
        engine.start()
    except KeyboardInterrupt:
        engine.stop()


if __name__ == "__main__":
    main()
