"""
통합 테스트 스크립트
모든 모듈을 DryRun 모드로 실행하여 파이프라인 검증
"""
import os
import sys
import time
import json
import signal
import threading
import multiprocessing
from pathlib import Path
from datetime import datetime

# 프로젝트 경로 추가
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import redis

from config.settings import settings
from src.common import setup_logging

logger = setup_logging("integration_test")


class IntegrationTest:
    """통합 테스트 클래스"""

    def __init__(self):
        self.redis = redis.Redis(
            host=settings.redis.host,
            port=settings.redis.port,
            decode_responses=True
        )
        self.processes = []
        self.stop_event = threading.Event()

    def cleanup_streams(self):
        """Redis Streams 초기화"""
        streams = [
            settings.redis.raw_stream,
            settings.redis.feature_stream,
            settings.redis.prediction_stream,
            settings.redis.order_stream
        ]

        for stream in streams:
            try:
                self.redis.delete(stream)
                logger.info(f"Deleted stream: {stream}")
            except Exception as e:
                logger.warning(f"Failed to delete {stream}: {e}")

    def create_consumer_groups(self):
        """Consumer Group 생성"""
        stream_groups = [
            (settings.redis.raw_stream, settings.consumer.processor_group),
            (settings.redis.feature_stream, settings.consumer.prediction_group),
            (settings.redis.feature_stream, settings.consumer.logger_group),
            (settings.redis.prediction_stream, settings.consumer.strategy_group),
        ]

        for stream, group in stream_groups:
            try:
                self.redis.xgroup_create(stream, group, id='0', mkstream=True)
                logger.info(f"Created group {group} for {stream}")
            except redis.ResponseError as e:
                if "BUSYGROUP" in str(e):
                    logger.info(f"Group {group} already exists for {stream}")
                else:
                    raise

    def generate_mock_data(self, count: int = 100, interval: float = 0.1):
        """
        Mock 데이터 생성 및 RAW_DATA_STREAM에 발행
        테스트용으로 timestamp를 조작하여 1분봉이 빠르게 생성되도록 함
        """
        import random

        logger.info(f"Generating {count} mock messages (simulated time)...")

        base_price = 350.0  # KOSPI Mini 가격 (포인트)
        # 시뮬레이션 시작 시간 (과거 시점에서 시작하여 1분봉이 여러 개 형성되도록)
        simulated_time = time.time() - (count * 2)  # 2초 간격으로 시뮬레이션

        for i in range(count):
            if self.stop_event.is_set():
                break

            # 랜덤 가격 변동
            price_change = random.uniform(-0.5, 0.5)
            base_price += price_change

            bid_price = base_price - 0.05
            ask_price = base_price + 0.05

            # 시뮬레이션 시간 증가 (2초씩 - 30개 메시지당 1분봉 생성)
            simulated_time += 2.0

            data = {
                'symbol': '101Z25',  # 2025년 12월물
                'timestamp': str(simulated_time),
                'bid_price_1': str(bid_price),
                'bid_qty_1': str(random.randint(10, 100)),
                'ask_price_1': str(ask_price),
                'ask_qty_1': str(random.randint(10, 100)),
                'bid_price_2': str(bid_price - 0.05),
                'bid_qty_2': str(random.randint(5, 50)),
                'ask_price_2': str(ask_price + 0.05),
                'ask_qty_2': str(random.randint(5, 50)),
                'tick_volume': str(random.randint(1, 10))
            }

            self.redis.xadd(
                settings.redis.raw_stream,
                data,
                maxlen=10000
            )

            if (i + 1) % 50 == 0:
                candles_generated = (i + 1) // 30
                logger.info(f"Published {i + 1}/{count} messages (~{candles_generated} candles)")

            time.sleep(interval)

        total_candles = count // 30
        logger.info(f"Mock data generation complete: {count} messages (~{total_candles} candles)")

    def check_stream_counts(self) -> dict:
        """각 스트림의 메시지 수 확인"""
        streams = {
            'raw': settings.redis.raw_stream,
            'feature': settings.redis.feature_stream,
            'prediction': settings.redis.prediction_stream,
            'order': settings.redis.order_stream
        }

        counts = {}
        for name, stream in streams.items():
            try:
                count = self.redis.xlen(stream)
                counts[name] = count
            except Exception:
                counts[name] = 0

        return counts

    def run_processor(self):
        """Feature Processor 실행"""
        from src.processor.feature_processor import FeatureProcessor

        logger.info("Starting Feature Processor...")
        processor = FeatureProcessor()

        try:
            processor.run()
        except Exception as e:
            logger.error(f"Processor error: {e}")

    def run_prediction(self):
        """Prediction Engine 실행"""
        from src.prediction.prediction_engine import PredictionEngine

        logger.info("Starting Prediction Engine...")
        engine = PredictionEngine()

        try:
            engine.start()
        except Exception as e:
            logger.error(f"Prediction error: {e}")

    def run_strategy(self):
        """Strategy Manager 실행"""
        from src.strategy.strategy_manager import StrategyManager, DryRunOrderExecutor

        logger.info("Starting Strategy Manager (DryRun)...")
        executor = DryRunOrderExecutor()
        manager = StrategyManager(order_executor=executor)

        try:
            manager.run()
        except Exception as e:
            logger.error(f"Strategy error: {e}")

    def run_test(self, duration: int = 30, mock_count: int = 100):
        """
        통합 테스트 실행

        Args:
            duration: 테스트 지속 시간 (초)
            mock_count: 생성할 Mock 데이터 수
        """
        print("\n" + "="*60)
        print("  KOSPI Mini Trading System - Integration Test")
        print("="*60)

        # 1. 환경 준비
        print("\n[1] Setting up environment...")
        self.cleanup_streams()
        self.create_consumer_groups()

        # 2. 백그라운드 모듈 시작 (별도 스레드)
        print("\n[2] Starting modules in background...")

        threads = [
            threading.Thread(target=self.run_processor, daemon=True),
            threading.Thread(target=self.run_prediction, daemon=True),
            threading.Thread(target=self.run_strategy, daemon=True),
        ]

        for t in threads:
            t.start()
            time.sleep(0.5)

        # 모듈 초기화 대기
        time.sleep(2)

        # 3. Mock 데이터 생성 (빠른 생성을 위해 interval=0.01)
        print(f"\n[3] Generating {mock_count} mock messages...")
        self.generate_mock_data(count=mock_count, interval=0.01)

        # 4. 처리 대기
        print(f"\n[4] Waiting for processing ({duration}s)...")
        time.sleep(duration)

        # 5. 결과 확인
        print("\n[5] Checking results...")
        counts = self.check_stream_counts()

        print("\n" + "-"*40)
        print("  Stream Message Counts")
        print("-"*40)
        for name, count in counts.items():
            status = "OK" if count > 0 else "EMPTY"
            print(f"  {name:15} : {count:6} [{status}]")
        print("-"*40)

        # 6. 샘플 메시지 확인
        print("\n[6] Sample messages from each stream:")

        for name, stream in [
            ('FEATURE', settings.redis.feature_stream),
            ('PREDICTION', settings.redis.prediction_stream),
            ('ORDER', settings.redis.order_stream)
        ]:
            try:
                messages = self.redis.xrange(stream, count=1)
                if messages:
                    msg_id, data = messages[0]
                    print(f"\n  [{name}] {msg_id}")
                    for k, v in list(data.items())[:5]:
                        print(f"    {k}: {v}")
                else:
                    print(f"\n  [{name}] No messages")
            except Exception as e:
                print(f"\n  [{name}] Error: {e}")

        # 7. 테스트 결과 요약
        print("\n" + "="*60)
        print("  Test Summary")
        print("="*60)

        success = all(counts[s] > 0 for s in ['raw', 'feature'])
        pipeline_ok = counts['feature'] > 0

        print(f"\n  Data Generation:   {'PASS' if counts['raw'] > 0 else 'FAIL'}")
        print(f"  Feature Processing: {'PASS' if counts['feature'] > 0 else 'FAIL'}")
        print(f"  Prediction Engine:  {'PASS' if counts['prediction'] > 0 else 'N/A (needs model)'}")
        print(f"  Strategy Manager:   {'PASS' if counts['order'] > 0 else 'N/A (needs prediction)'}")

        print(f"\n  Overall Pipeline:   {'PASS' if pipeline_ok else 'FAIL'}")
        print("="*60 + "\n")

        # 종료
        self.stop_event.set()

        return pipeline_ok


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Integration Test")
    parser.add_argument("--duration", type=int, default=15, help="Test duration (seconds)")
    # Prediction에 60개 1분봉 필요 (lookback=60), 30 msg/candle -> 1800 msg 필요
    # 최소 테스트용으로 700 메시지 (약 23개 1분봉)
    parser.add_argument("--count", type=int, default=700, help="Mock message count")

    args = parser.parse_args()

    test = IntegrationTest()

    try:
        success = test.run_test(duration=args.duration, mock_count=args.count)
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\nTest interrupted")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Test failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
