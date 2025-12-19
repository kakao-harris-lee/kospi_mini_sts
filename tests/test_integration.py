"""
통합 테스트: Mock 데이터로 전체 파이프라인 테스트
"""
import sys
import time
import threading
import pytest


from config.settings import settings
from src.common import RedisClient, StreamPublisher
from src.collector import DataCollector, MockAPIAdapter
from src.processor import FeatureProcessor
from src.prediction import PredictionEngine
from src.strategy import StrategyManager, DryRunOrderExecutor


class TestIntegration:
    """통합 테스트"""
    
    @pytest.fixture(autouse=True)
    def setup(self):
        """테스트 전 Redis 클리어"""
        try:
            client = RedisClient.get_client()
            # 테스트용 Stream 삭제
            for stream in [
                settings.redis.raw_stream,
                settings.redis.feature_stream,
                settings.redis.prediction_stream,
                settings.redis.order_stream
            ]:
                client.delete(stream)
        except Exception:
            pytest.skip("Redis not available")
        yield
    
    def test_collector_publishes_to_stream(self):
        """Collector가 RAW_DATA_STREAM에 데이터를 발행하는지 테스트"""
        adapter = MockAPIAdapter(tick_interval=0.1)
        collector = DataCollector(adapter)
        
        # 백그라운드에서 실행
        thread = threading.Thread(
            target=collector.start,
            args=(["KRW-BTC"],),
            daemon=True
        )
        thread.start()
        
        # 데이터 발행 대기
        time.sleep(1)
        collector.stop()
        
        # Stream 확인
        client = RedisClient.get_client()
        length = client.xlen(settings.redis.raw_stream)
        assert length > 0, "No messages in RAW_DATA_STREAM"
    
    def test_feature_processor_calculates_ofi(self):
        """Feature Processor가 OFI를 계산하는지 테스트"""
        from src.processor.feature_processor import OFICalculator
        
        calc = OFICalculator()
        
        # 첫 번째 틱 (OFI = 0)
        ofi1 = calc.calculate_tick_ofi(100, 10, 101, 10)
        assert ofi1 == 0  # 첫 틱은 비교 대상 없음
        
        # 두 번째 틱 - bid 상승
        ofi2 = calc.calculate_tick_ofi(100.5, 15, 101, 10)
        assert ofi2 > 0  # 매수 압력 증가
    
    def test_liquidity_calculator(self):
        """유동성 점수 계산 테스트"""
        from src.processor.feature_processor import LiquidityCalculator
        
        # 높은 유동성 (좁은 스프레드, 큰 잔량)
        high_liq = LiquidityCalculator.calculate_score(
            bid_prices=[100, 99, 98],
            bid_qtys=[10, 10, 10],
            ask_prices=[100.1, 100.2, 100.3],
            ask_qtys=[10, 10, 10]
        )
        
        # 낮은 유동성 (넓은 스프레드, 적은 잔량)
        low_liq = LiquidityCalculator.calculate_score(
            bid_prices=[100, 99, 98],
            bid_qtys=[1, 1, 1],
            ask_prices=[102, 103, 104],
            ask_qtys=[1, 1, 1]
        )
        
        assert high_liq > low_liq
    
    def test_strategy_mode_selection(self):
        """전략 모드 선택 테스트"""
        from src.strategy import StrategyManager, TradingMode
        
        executor = DryRunOrderExecutor()
        manager = StrategyManager(order_executor=executor)
        
        # Mode A 조건 (유동성↑, 괴리↑)
        mode_a = manager._determine_mode(
            liquidity_score=90,
            basis_gap=3.0,
            up_prob=0.7
        )
        assert mode_a == TradingMode.MODE_A
        
        # Mode B 조건 (일반)
        mode_b = manager._determine_mode(
            liquidity_score=60,
            basis_gap=0.5,
            up_prob=0.9
        )
        assert mode_b == TradingMode.MODE_B
        
        # Avoid 조건 (유동성↓)
        avoid = manager._determine_mode(
            liquidity_score=30,
            basis_gap=0.5,
            up_prob=0.9
        )
        assert avoid == TradingMode.AVOID


class TestStreamConsumer:
    """Stream Consumer 테스트"""
    
    def test_consumer_group_creation(self):
        """Consumer Group이 올바르게 생성되는지 테스트"""
        try:
            client = RedisClient.get_client()
        except Exception:
            pytest.skip("Redis not available")
        
        # 테스트용 Stream 생성
        test_stream = "TEST_STREAM"
        client.delete(test_stream)
        
        # Publisher로 메시지 추가
        publisher = StreamPublisher(test_stream)
        publisher.publish({"test": "data"})
        
        # Consumer Group 생성
        try:
            client.xgroup_create(test_stream, "test_group", id='0', mkstream=True)
        except Exception:
            pass  # 이미 존재
        
        # Group 확인
        groups = client.xinfo_groups(test_stream)
        assert len(groups) > 0
        
        # 정리
        client.delete(test_stream)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
