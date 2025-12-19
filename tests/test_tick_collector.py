"""
틱 수집기 테스트 (Phase 8.2)
"""
import pytest
import time
from datetime import datetime
from unittest.mock import Mock, patch, MagicMock

from src.collector import (
    TickDataCollector,
    TickCollectorConfig,
    get_current_futures_code,
    TickData,
)
from src.collector.kis_websocket import KISConfig, KISMarket


class TestGetCurrentFuturesCode:
    """근월물 코드 생성 테스트"""

    def test_returns_valid_format(self):
        """코드 형식 검증"""
        code = get_current_futures_code()
        assert code.startswith("101")
        assert len(code) == 6
        # 월코드는 FGHJKMNQUVXZ 중 하나
        month_code = code[3]
        assert month_code in "FGHJKMNQUVXZ"
        # 년도는 숫자 2자리
        year = code[4:6]
        assert year.isdigit()

    def test_year_code_is_reasonable(self):
        """년도가 합리적인 범위인지"""
        code = get_current_futures_code()
        year = int(code[4:6])
        current_year = datetime.now().year % 100
        # 현재 년도 ~ +1년 범위
        assert current_year <= year <= current_year + 1


class TestTickCollectorConfig:
    """수집기 설정 테스트"""

    def test_default_config(self):
        """기본 설정 값"""
        config = TickCollectorConfig(symbols=["101F26"])
        assert config.symbols == ["101F26"]
        assert config.orderbook_batch_size == 500
        assert config.trade_batch_size == 500
        assert config.flush_interval_sec == 1.0
        assert config.publish_to_redis is True

    def test_custom_config(self):
        """사용자 정의 설정"""
        config = TickCollectorConfig(
            symbols=["101F26", "101G26"],
            orderbook_batch_size=1000,
            trade_batch_size=200,
            publish_to_redis=False
        )
        assert len(config.symbols) == 2
        assert config.orderbook_batch_size == 1000
        assert config.publish_to_redis is False


class TestTickDataCollector:
    """틱 데이터 수집기 테스트"""

    @pytest.fixture
    def mock_kis_config(self):
        """Mock KIS 설정"""
        return KISConfig(
            app_key="test_key",
            app_secret="test_secret",
            market=KISMarket.MOCK
        )

    @pytest.fixture
    def mock_collector(self, mock_kis_config):
        """Mock 수집기 (ClickHouse 없이)"""
        config = TickCollectorConfig(
            symbols=["101F26"],
            publish_to_redis=False
        )

        with patch('src.collector.tick_collector.BatchInserter') as MockInserter:
            mock_inserter = MagicMock()
            MockInserter.return_value = mock_inserter

            with patch('src.collector.tick_collector.init_tables'):
                collector = TickDataCollector(config, mock_kis_config)
                collector._mock_inserter = mock_inserter
                yield collector

    def test_collector_initialization(self, mock_collector):
        """수집기 초기화 테스트"""
        assert mock_collector.config.symbols == ["101F26"]
        assert mock_collector._orderbook_count == 0
        assert mock_collector._trade_count == 0

    def test_orderbook_columns(self, mock_collector):
        """호가 테이블 컬럼 확인"""
        columns = mock_collector.ORDERBOOK_COLUMNS
        assert "timestamp" in columns
        assert "symbol" in columns
        assert "bid_price_1" in columns
        assert "ask_price_5" in columns
        assert "spread" in columns
        assert "imbalance" in columns

    def test_trade_columns(self, mock_collector):
        """체결 테이블 컬럼 확인"""
        columns = mock_collector.TRADE_COLUMNS
        assert "timestamp" in columns
        assert "symbol" in columns
        assert "price" in columns
        assert "volume" in columns
        assert "side" in columns
        assert "open_interest" in columns

    def test_on_orderbook_processing(self, mock_collector):
        """호가 데이터 처리 테스트"""
        tick = TickData(
            symbol="101F26",
            timestamp=time.time(),
            bid_price_1=350.0,
            bid_qty_1=10,
            bid_price_2=349.95,
            bid_qty_2=15,
            bid_price_3=349.90,
            bid_qty_3=20,
            ask_price_1=350.05,
            ask_qty_1=12,
            ask_price_2=350.10,
            ask_qty_2=18,
            ask_price_3=350.15,
            ask_qty_3=25,
        )

        mock_collector._on_orderbook(tick)

        assert mock_collector._orderbook_count == 1
        assert "101F26" in mock_collector._last_orderbook
        mock_collector.orderbook_inserter.add.assert_called_once()

    def test_on_trade_processing(self, mock_collector):
        """체결 데이터 처리 테스트"""
        # 먼저 호가 데이터 설정
        mock_collector._last_orderbook["101F26"] = TickData(
            symbol="101F26",
            timestamp=time.time(),
            bid_price_1=350.0,
            bid_qty_1=10,
            ask_price_1=350.05,
            ask_qty_1=12,
        )

        tick = TickData(
            symbol="101F26",
            timestamp=time.time(),
            bid_price_1=350.05,  # 매도호가에 체결 -> BUY
            bid_qty_1=0,
            ask_price_1=350.05,
            ask_qty_1=0,
            tick_volume=5,
        )

        mock_collector._on_trade(tick)

        assert mock_collector._trade_count == 1
        mock_collector.trade_inserter.add.assert_called_once()

    def test_tick_classification_orderbook(self, mock_collector):
        """틱 분류 - 호가 데이터"""
        tick = TickData(
            symbol="101F26",
            timestamp=time.time(),
            bid_price_1=350.0,
            bid_qty_1=10,
            bid_price_2=349.95,  # L2 데이터 있음 -> 호가
            bid_qty_2=15,
            ask_price_1=350.05,
            ask_qty_1=12,
            ask_price_2=350.10,
            ask_qty_2=18,
        )

        mock_collector._on_tick(tick)

        assert mock_collector._orderbook_count == 1
        assert mock_collector._trade_count == 0  # 체결 아님

    def test_tick_classification_trade(self, mock_collector):
        """틱 분류 - 체결 데이터"""
        tick = TickData(
            symbol="101F26",
            timestamp=time.time(),
            bid_price_1=350.0,
            bid_qty_1=0,
            ask_price_1=350.05,
            ask_qty_1=0,
            tick_volume=10,  # 체결량 있음 -> 체결
        )

        mock_collector._on_tick(tick)

        assert mock_collector._orderbook_count == 0  # 호가 아님
        assert mock_collector._trade_count == 1

    def test_spread_calculation(self, mock_collector):
        """스프레드 계산 테스트"""
        tick = TickData(
            symbol="101F26",
            timestamp=time.time(),
            bid_price_1=350.0,
            bid_qty_1=10,
            bid_price_2=349.95,
            bid_qty_2=15,
            ask_price_1=350.10,  # 스프레드 = 0.10
            ask_qty_1=12,
            ask_price_2=350.15,
            ask_qty_2=18,
        )

        mock_collector._on_orderbook(tick)

        # 호출된 인자 확인
        call_args = mock_collector.orderbook_inserter.add.call_args[0][0]
        assert abs(call_args["spread"] - 0.10) < 0.001
        assert abs(call_args["mid_price"] - 350.05) < 0.001

    def test_imbalance_calculation(self, mock_collector):
        """불균형 계산 테스트"""
        tick = TickData(
            symbol="101F26",
            timestamp=time.time(),
            bid_price_1=350.0,
            bid_qty_1=100,  # 총 매수 = 100
            bid_qty_2=0,
            bid_qty_3=0,
            ask_price_1=350.05,
            ask_qty_1=50,  # 총 매도 = 50
            ask_qty_2=0,
            ask_qty_3=0,
            bid_price_2=349.95,
            ask_price_2=350.10,
        )

        mock_collector._on_orderbook(tick)

        call_args = mock_collector.orderbook_inserter.add.call_args[0][0]
        # imbalance = (100 - 50) / (100 + 50) = 0.333...
        expected_imbalance = (100 - 50) / (100 + 50)
        assert abs(call_args["imbalance"] - expected_imbalance) < 0.001
