"""
Mock Prediction Engine (v0.0.2)
FEATURE_STREAM → PREDICTION_STREAM

For testing and backtesting ONLY. Not for production paper/live trading.

Use cases:
- Pipeline integration testing (verify data flows end-to-end)
- Backtesting strategy logic without LSTM dependency
- Development environment testing
"""
import time
import random
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass
from enum import Enum

from config.settings import settings
from src.common import (
    StreamConsumer,
    StreamPublisher,
    StreamMessage,
    RedisClient,
    setup_logging,
    init_metrics,
    get_metrics,
)

logger = setup_logging("mock_prediction")


class MockMode(Enum):
    """Mock 신호 생성 모드"""
    RANDOM = "random"
    MA_CROSS = "ma_cross"
    OFI_BASED = "ofi_based"


@dataclass
class MockPredictionResult:
    """예측 결과"""
    symbol: str
    timestamp: float
    up_prob: float
    down_prob: float
    hold_prob: float
    model_version: str
    inference_time_ms: float


class MockPredictionEngine(StreamConsumer):
    """
    Mock Prediction Engine
    FEATURE_STREAM → PREDICTION_STREAM

    간단한 시그널 생성기로 파이프라인 테스트용
    """

    def __init__(self, mode: str = "ofi_based"):
        super().__init__(
            stream_name=settings.redis.feature_stream,
            group_name=settings.consumer.prediction_group,
            consumer_name="mock_prediction_1"
        )
        self.publisher = StreamPublisher(settings.redis.prediction_stream)
        self.redis = RedisClient.get_client()

        # 모드 설정
        try:
            self.mode = MockMode(mode)
        except ValueError:
            logger.warning(f"Unknown mode '{mode}', falling back to 'random'")
            self.mode = MockMode.RANDOM

        self._inference_count = 0
        self._total_inference_time = 0.0

        logger.info(f"MockPredictionEngine initialized with mode: {self.mode.value}")

    def _generate_random(self) -> tuple:
        """랜덤 확률 생성 (hold 쪽으로 약간 치우침)"""
        hold = random.uniform(0.3, 0.5)
        up = random.uniform(0.2, 0.4)
        down = max(0.0, 1.0 - hold - up)
        return up, down, hold

    def _generate_ma_cross(self, features: Dict[str, Any]) -> tuple:
        """
        이동평균 교차 기반 시그널

        MA5/MA20 비율로 판단:
        - MA5 > MA20 * 1.001 → Golden Cross → Up
        - MA5 < MA20 * 0.999 → Death Cross → Down
        """
        ma_ratio_5 = float(features.get('ma_ratio_5', 1.0) or 1.0)
        ma_ratio_20 = float(features.get('ma_ratio_20', 1.0) or 1.0)

        # MA5가 MA20보다 높으면 상승 신호
        if ma_ratio_5 > ma_ratio_20 * 1.001:
            return 0.6, 0.2, 0.2  # up, down, hold
        elif ma_ratio_5 < ma_ratio_20 * 0.999:
            return 0.2, 0.6, 0.2
        else:
            return 0.33, 0.33, 0.34

    def _generate_ofi_based(self, features: Dict[str, Any]) -> tuple:
        """
        OFI Z-Score 기반 시그널

        - Z > 1.5 → 강한 매수 압력 → Up
        - Z < -1.5 → 강한 매도 압력 → Down
        """
        ofi_z = float(features.get('ofi_z_score', 0.0) or 0.0)

        if ofi_z > 1.5:
            # 강한 매수 압력
            strength = min(0.8, 0.5 + (ofi_z - 1.5) * 0.1)
            return strength, 0.1, 1.0 - strength - 0.1
        elif ofi_z < -1.5:
            # 강한 매도 압력
            strength = min(0.8, 0.5 + (-ofi_z - 1.5) * 0.1)
            return 0.1, strength, 1.0 - strength - 0.1
        else:
            # 중립
            return 0.3, 0.3, 0.4

    def _generate_signal(self, features: Dict[str, Any]) -> tuple:
        """모드에 따른 시그널 생성"""
        if self.mode == MockMode.RANDOM:
            return self._generate_random()
        elif self.mode == MockMode.MA_CROSS:
            return self._generate_ma_cross(features)
        elif self.mode == MockMode.OFI_BASED:
            return self._generate_ofi_based(features)
        else:
            return self._generate_random()

    def process_message(self, message: StreamMessage) -> bool:
        """
        Feature 메시지 처리 및 Mock 예측 실행
        """
        try:
            start_time = time.time()
            data = message.data
            symbol = data.get('symbol')

            if not symbol:
                return True

            # 시그널 생성
            up_prob, down_prob, hold_prob = self._generate_signal(data)

            inference_time_ms = (time.time() - start_time) * 1000

            # 결과 구성
            result = MockPredictionResult(
                symbol=symbol,
                timestamp=time.time(),
                up_prob=up_prob,
                down_prob=down_prob,
                hold_prob=hold_prob,
                model_version=f"mock_{self.mode.value}_v1",
                inference_time_ms=inference_time_ms
            )

            # PREDICTION_STREAM에 발행
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
                    f"Mock Predictions: {self._inference_count}, "
                    f"Avg time: {avg_time:.2f}ms"
                )

            logger.debug(
                f"Mock Prediction: {symbol} Up={result.up_prob:.2f} "
                f"Down={result.down_prob:.2f} Hold={result.hold_prob:.2f}"
            )

            return True

        except Exception as e:
            logger.error(f"Error in mock prediction: {e}", exc_info=True)
            return False


def main():
    """Mock Prediction Engine 실행"""
    import os

    # 환경변수로 모드 설정 가능
    mode = os.getenv("MOCK_PREDICTION_MODE", "ofi_based")

    logger.info(f"Starting Mock Prediction Engine (mode: {mode})...")

    # 메트릭 서버 시작 (포트 8083)
    init_metrics("mock_prediction", port=8083)

    engine = MockPredictionEngine(mode=mode)

    try:
        engine.run()
    except KeyboardInterrupt:
        engine.stop()


if __name__ == "__main__":
    main()
