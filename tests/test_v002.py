"""
v0.0.2 Unit Tests
- RawDataLogger
- OFI Warm-up
- MockPredictionEngine
"""
import pytest
import time
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock
from collections import deque

import numpy as np


# =============================================================================
# RawDataLogger Tests
# =============================================================================

class TestRawDataLogger:
    """RawDataLogger 테스트"""

    @pytest.fixture
    def mock_logger(self):
        """Mock RawDataLogger (ClickHouse/Redis 없이)"""
        with patch('src.db_logger.raw_data_logger.StreamConsumer.__init__'):
            with patch('src.db_logger.raw_data_logger.BatchInserter') as MockInserter:
                with patch('src.db_logger.raw_data_logger.init_tables'):
                    from src.db_logger.raw_data_logger import RawDataLogger

                    mock_inserter = MagicMock()
                    MockInserter.return_value = mock_inserter

                    logger = RawDataLogger.__new__(RawDataLogger)
                    logger.stream = "RAW_DATA_STREAM"
                    logger.group = "raw_data_logger"
                    logger.consumer = "logger_1"
                    logger.orderbook_inserter = mock_inserter
                    logger.trade_inserter = mock_inserter
                    logger._orderbook_count = 0
                    logger._trade_count = 0
                    logger._last_report = time.time()
                    logger._last_orderbook = {}

                    yield logger

    def test_orderbook_columns_defined(self):
        """호가 테이블 컬럼 정의 확인"""
        from src.db_logger.raw_data_logger import RawDataLogger

        columns = RawDataLogger.ORDERBOOK_COLUMNS
        assert "timestamp" in columns
        assert "symbol" in columns
        assert "bid_price_1" in columns
        assert "ask_price_5" in columns
        assert "spread" in columns
        assert "imbalance" in columns
        assert len(columns) == 25  # timestamp, symbol, 10 bids, 10 asks, spread, mid_price, imbalance

    def test_trade_columns_defined(self):
        """체결 테이블 컬럼 정의 확인"""
        from src.db_logger.raw_data_logger import RawDataLogger

        columns = RawDataLogger.TRADE_COLUMNS
        assert "timestamp" in columns
        assert "symbol" in columns
        assert "price" in columns
        assert "volume" in columns
        assert "side" in columns
        assert len(columns) == 9

    def test_process_orderbook(self, mock_logger):
        """호가 데이터 처리 테스트"""
        data = {
            'symbol': '101F26',
            'bid_price_1': 350.0,
            'bid_qty_1': 10,
            'bid_price_2': 349.95,
            'bid_qty_2': 15,
            'ask_price_1': 350.05,
            'ask_qty_1': 12,
            'ask_price_2': 350.10,
            'ask_qty_2': 18,
        }

        result = mock_logger._process_orderbook(data, time.time())

        assert result is True
        assert mock_logger._orderbook_count == 1
        assert '101F26' in mock_logger._last_orderbook
        mock_logger.orderbook_inserter.add.assert_called_once()

    def test_process_trade(self, mock_logger):
        """체결 데이터 처리 테스트"""
        # 먼저 호가 설정
        mock_logger._last_orderbook['101F26'] = {
            'bid_price_1': 350.0,
            'ask_price_1': 350.05,
        }

        data = {
            'symbol': '101F26',
            'bid_price_1': 350.05,  # 매도호가에 체결 -> BUY
            'ask_price_1': 350.05,
            'tick_volume': 5,
        }

        result = mock_logger._process_trade(data, time.time())

        assert result is True
        assert mock_logger._trade_count == 1
        mock_logger.trade_inserter.add.assert_called_once()

    def test_trade_side_detection_buy(self, mock_logger):
        """체결 방향 추정 - 매수"""
        mock_logger._last_orderbook['101F26'] = {
            'bid_price_1': 350.0,
            'ask_price_1': 350.05,
        }

        data = {
            'symbol': '101F26',
            'bid_price_1': 350.05,  # 매도호가와 같음 -> BUY
            'ask_price_1': 350.05,
            'tick_volume': 5,
        }

        mock_logger._process_trade(data, time.time())

        call_args = mock_logger.trade_inserter.add.call_args[0][0]
        assert call_args['side'] == 'BUY'

    def test_trade_side_detection_sell(self, mock_logger):
        """체결 방향 추정 - 매도"""
        mock_logger._last_orderbook['101F26'] = {
            'bid_price_1': 350.0,
            'ask_price_1': 350.05,
        }

        data = {
            'symbol': '101F26',
            'bid_price_1': 350.0,  # 매수호가와 같음 -> SELL
            'ask_price_1': 350.05,
            'tick_volume': 5,
        }

        mock_logger._process_trade(data, time.time())

        call_args = mock_logger.trade_inserter.add.call_args[0][0]
        assert call_args['side'] == 'SELL'

    def test_spread_calculation(self, mock_logger):
        """스프레드 계산 테스트"""
        data = {
            'symbol': '101F26',
            'bid_price_1': 350.0,
            'bid_qty_1': 10,
            'ask_price_1': 350.10,  # 스프레드 = 0.10
            'ask_qty_1': 12,
            'bid_price_2': 349.95,
            'ask_price_2': 350.15,
        }

        mock_logger._process_orderbook(data, time.time())

        call_args = mock_logger.orderbook_inserter.add.call_args[0][0]
        assert abs(call_args['spread'] - 0.10) < 0.001
        assert abs(call_args['mid_price'] - 350.05) < 0.001

    def test_imbalance_calculation(self, mock_logger):
        """불균형 계산 테스트"""
        data = {
            'symbol': '101F26',
            'bid_price_1': 350.0,
            'bid_qty_1': 100,
            'bid_qty_2': 0,
            'bid_qty_3': 0,
            'ask_price_1': 350.05,
            'ask_qty_1': 50,
            'ask_qty_2': 0,
            'ask_qty_3': 0,
            'bid_price_2': 349.95,
            'ask_price_2': 350.10,
        }

        mock_logger._process_orderbook(data, time.time())

        call_args = mock_logger.orderbook_inserter.add.call_args[0][0]
        expected_imbalance = (100 - 50) / (100 + 50)
        assert abs(call_args['imbalance'] - expected_imbalance) < 0.001


# =============================================================================
# OFI Warm-up Tests
# =============================================================================

class TestOFIWarmup:
    """OFI 워밍업 테스트"""

    @pytest.fixture
    def ofi_calculator(self):
        """OFICalculator 인스턴스"""
        from src.processor.feature_processor import OFICalculator
        return OFICalculator(lookback=100)

    def test_ofi_calculator_initial_state(self, ofi_calculator):
        """초기 상태 테스트"""
        assert len(ofi_calculator.ofi_history) == 0
        assert ofi_calculator.prev_snapshot is None
        assert ofi_calculator.get_z_score() == 0.0

    def test_ofi_calculation_bid_increase(self, ofi_calculator):
        """매수호가 상승 시 OFI 계산"""
        # 첫 번째 틱
        ofi_calculator.calculate_tick_ofi(350.0, 10, 350.05, 10, time.time())

        # 두 번째 틱 - 매수호가 상승
        ofi = ofi_calculator.calculate_tick_ofi(350.05, 15, 350.10, 10, time.time())

        # 매수호가 상승 -> OFI 양수 (bid_qty 추가)
        assert ofi > 0
        assert len(ofi_calculator.ofi_history) == 2

    def test_ofi_calculation_ask_decrease(self, ofi_calculator):
        """매도호가 하락 시 OFI 계산"""
        # 첫 번째 틱
        ofi_calculator.calculate_tick_ofi(350.0, 10, 350.10, 10, time.time())

        # 두 번째 틱 - 매도호가 하락
        ofi = ofi_calculator.calculate_tick_ofi(350.0, 10, 350.05, 15, time.time())

        # 매도호가 하락 -> OFI 음수 (ask_qty 추가됨)
        assert ofi < 0

    def test_z_score_calculation(self, ofi_calculator):
        """Z-Score 계산 테스트"""
        # 충분한 데이터 생성
        for i in range(50):
            ofi_calculator.calculate_tick_ofi(
                350.0 + i * 0.01, 10 + i,
                350.05 + i * 0.01, 10,
                time.time()
            )

        z_score = ofi_calculator.get_z_score()

        # Z-score는 실수여야 함
        assert isinstance(z_score, float)
        # 데이터가 충분하므로 0이 아닐 것
        assert z_score != 0.0

    def test_cumulative_ofi(self, ofi_calculator):
        """누적 OFI 테스트"""
        # 여러 틱 처리
        for i in range(30):
            ofi_calculator.calculate_tick_ofi(
                350.0 + i * 0.01, 10,
                350.05 + i * 0.01, 10,
                time.time()
            )

        cumulative = ofi_calculator.get_cumulative_ofi(window=20)

        assert isinstance(cumulative, float)
        # 최근 20개의 합
        expected = sum(list(ofi_calculator.ofi_history)[-20:])
        assert abs(cumulative - expected) < 0.001

    def test_warmup_with_mock_clickhouse(self, ofi_calculator):
        """ClickHouse 모킹으로 워밍업 테스트"""
        mock_rows = [
            (datetime.now(), 350.0 + i * 0.01, 10, 350.05 + i * 0.01, 10)
            for i in range(50)
        ]

        with patch('src.processor.feature_processor.ClickHouseClient') as MockCH:
            mock_client = MagicMock()
            mock_result = MagicMock()
            mock_result.result_rows = mock_rows
            mock_client.query.return_value = mock_result
            MockCH.get_client.return_value = mock_client

            count = ofi_calculator.warm_up('101F26')

            assert count == 50
            assert len(ofi_calculator.ofi_history) == 50

    def test_warmup_empty_result(self, ofi_calculator):
        """워밍업 - 데이터 없음"""
        with patch('src.processor.feature_processor.ClickHouseClient') as MockCH:
            mock_client = MagicMock()
            mock_result = MagicMock()
            mock_result.result_rows = []
            mock_client.query.return_value = mock_result
            MockCH.get_client.return_value = mock_client

            count = ofi_calculator.warm_up('101F26')

            assert count == 0
            assert len(ofi_calculator.ofi_history) == 0

    def test_warmup_error_handling(self, ofi_calculator):
        """워밍업 - 에러 처리"""
        with patch('src.processor.feature_processor.ClickHouseClient') as MockCH:
            MockCH.get_client.side_effect = Exception("Connection failed")

            count = ofi_calculator.warm_up('101F26')

            assert count == 0  # 에러 시 0 반환


# =============================================================================
# MockPredictionEngine Tests
# =============================================================================

class TestMockPredictionEngine:
    """MockPredictionEngine 테스트"""

    @pytest.fixture
    def mock_engine(self):
        """Mock 엔진 (Redis 없이)"""
        with patch('src.prediction.mock_engine.StreamConsumer.__init__'):
            with patch('src.prediction.mock_engine.StreamPublisher') as MockPub:
                with patch('src.prediction.mock_engine.RedisClient'):
                    from src.prediction.mock_engine import MockPredictionEngine, MockMode

                    mock_publisher = MagicMock()
                    MockPub.return_value = mock_publisher

                    engine = MockPredictionEngine.__new__(MockPredictionEngine)
                    engine.stream = "FEATURE_STREAM"
                    engine.group = "prediction_engine_group"
                    engine.consumer = "mock_prediction_1"
                    engine.publisher = mock_publisher
                    engine.mode = MockMode.OFI_BASED
                    engine._inference_count = 0
                    engine._total_inference_time = 0.0

                    yield engine

    def test_random_mode_probabilities(self):
        """랜덤 모드 확률 범위"""
        from src.prediction.mock_engine import MockPredictionEngine, MockMode

        with patch('src.prediction.mock_engine.StreamConsumer.__init__'):
            with patch('src.prediction.mock_engine.StreamPublisher'):
                with patch('src.prediction.mock_engine.RedisClient'):
                    engine = MockPredictionEngine.__new__(MockPredictionEngine)
                    engine.mode = MockMode.RANDOM

                    for _ in range(100):
                        up, down, hold = engine._generate_random()

                        # 합이 1에 가까워야 함
                        assert abs(up + down + hold - 1.0) < 0.01
                        # 각 확률은 0-1 범위
                        assert 0 <= up <= 1
                        assert 0 <= down <= 1
                        assert 0 <= hold <= 1

    def test_ma_cross_golden_cross(self, mock_engine):
        """MA 교차 - 골든크로스"""
        features = {
            'ma_ratio_5': 1.01,  # MA5 > MA20
            'ma_ratio_20': 1.00,
        }

        up, down, hold = mock_engine._generate_ma_cross(features)

        assert up > down  # 상승 확률이 더 높아야 함
        assert up == 0.6
        assert down == 0.2

    def test_ma_cross_death_cross(self, mock_engine):
        """MA 교차 - 데드크로스"""
        features = {
            'ma_ratio_5': 0.99,  # MA5 < MA20
            'ma_ratio_20': 1.00,
        }

        up, down, hold = mock_engine._generate_ma_cross(features)

        assert down > up  # 하락 확률이 더 높아야 함
        assert up == 0.2
        assert down == 0.6

    def test_ofi_based_strong_buy(self, mock_engine):
        """OFI 기반 - 강한 매수 압력"""
        features = {'ofi_z_score': 2.0}

        up, down, hold = mock_engine._generate_ofi_based(features)

        assert up > 0.5  # 상승 확률 높음
        assert down < 0.2
        assert abs(up + down + hold - 1.0) < 0.01

    def test_ofi_based_strong_sell(self, mock_engine):
        """OFI 기반 - 강한 매도 압력"""
        features = {'ofi_z_score': -2.0}

        up, down, hold = mock_engine._generate_ofi_based(features)

        assert down > 0.5  # 하락 확률 높음
        assert up < 0.2
        assert abs(up + down + hold - 1.0) < 0.01

    def test_ofi_based_neutral(self, mock_engine):
        """OFI 기반 - 중립"""
        features = {'ofi_z_score': 0.5}

        up, down, hold = mock_engine._generate_ofi_based(features)

        assert hold > up  # hold 확률이 가장 높음
        assert hold > down
        assert abs(up + down + hold - 1.0) < 0.01

    def test_process_message_publishes_prediction(self, mock_engine):
        """메시지 처리 시 예측 발행"""
        from src.common import StreamMessage

        message = MagicMock()
        message.data = {
            'symbol': '101F26',
            'ofi_z_score': 1.8,
            'timestamp': time.time(),
        }

        result = mock_engine.process_message(message)

        assert result is True
        assert mock_engine._inference_count == 1
        mock_engine.publisher.publish.assert_called_once()

        # 발행된 데이터 확인
        published_data = mock_engine.publisher.publish.call_args[0][0]
        assert published_data['symbol'] == '101F26'
        assert 'up_prob' in published_data
        assert 'down_prob' in published_data
        assert 'hold_prob' in published_data
        assert published_data['model_version'].startswith('mock_')

    def test_mode_enum_values(self):
        """MockMode enum 값 확인"""
        from src.prediction.mock_engine import MockMode

        assert MockMode.RANDOM.value == "random"
        assert MockMode.MA_CROSS.value == "ma_cross"
        assert MockMode.OFI_BASED.value == "ofi_based"


# =============================================================================
# Lightweight Collector Tests
# =============================================================================

class TestLightweightCollector:
    """경량화된 Collector 테스트"""

    def test_collector_has_no_clickhouse_attributes(self):
        """Collector에 ClickHouse 관련 속성 없음"""
        from src.collector.tick_collector import TickDataCollector

        # ORDERBOOK_COLUMNS, TRADE_COLUMNS이 없어야 함
        assert not hasattr(TickDataCollector, 'ORDERBOOK_COLUMNS')
        assert not hasattr(TickDataCollector, 'TRADE_COLUMNS')

    @pytest.fixture
    def mock_collector(self):
        """Mock 경량 Collector"""
        with patch('src.common.StreamPublisher') as MockPub:
            with patch('src.collector.tick_collector.KISWebSocketAdapter'):
                from src.collector.tick_collector import (
                    TickDataCollector,
                    TickCollectorConfig,
                )
                from src.collector.kis_websocket import KISConfig, KISMarket

                mock_publisher = MagicMock()
                MockPub.return_value = mock_publisher

                config = TickCollectorConfig(symbols=["101F26"])
                kis_config = KISConfig(
                    app_key="test",
                    app_secret="test",
                    market=KISMarket.MOCK
                )

                collector = TickDataCollector(config, kis_config)
                yield collector

    def test_collector_has_redis_publisher(self, mock_collector):
        """Collector에 Redis publisher 있음"""
        assert mock_collector._redis_publisher is not None

    def test_on_orderbook_publishes_to_redis(self, mock_collector):
        """호가 처리 시 Redis에 발행"""
        from src.collector.data_collector import TickData

        with patch.object(mock_collector, '_redis_publisher') as mock_pub:
            with patch('src.collector.tick_collector.get_metrics') as mock_metrics:
                mock_metrics.return_value = MagicMock()

                tick = TickData(
                    symbol="101F26",
                    timestamp=time.time(),
                    bid_price_1=350.0,
                    bid_qty_1=10,
                    ask_price_1=350.05,
                    ask_qty_1=12,
                )

                mock_collector._on_orderbook(tick)

                assert mock_collector._orderbook_count == 1
                mock_pub.publish.assert_called_once()

    def test_on_trade_does_not_insert_to_db(self, mock_collector):
        """체결 처리 시 DB 삽입 없음"""
        from src.collector.data_collector import TickData

        with patch('src.collector.tick_collector.get_metrics') as mock_metrics:
            mock_metrics.return_value = MagicMock()

            tick = TickData(
                symbol="101F26",
                timestamp=time.time(),
                bid_price_1=350.0,
                bid_qty_1=10,
                ask_price_1=350.05,
                ask_qty_1=12,
                tick_volume=5,
            )

            mock_collector._on_trade(tick)

            assert mock_collector._trade_count == 1
            # trade_inserter가 없어야 함
            assert not hasattr(mock_collector, 'trade_inserter')
