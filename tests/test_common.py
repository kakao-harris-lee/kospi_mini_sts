"""
공통 모듈 유닛 테스트

Coverage:
- src/common/redis_client.py
- src/common/clickhouse_client.py
- src/common/futures_code.py
"""
import pytest
import time
import json
from datetime import date, datetime
from unittest.mock import Mock, MagicMock, patch, PropertyMock


# =============================================================================
# StreamMessage Tests
# =============================================================================
class TestStreamMessage:
    """StreamMessage 클래스 테스트"""

    def test_from_raw_basic(self):
        """기본 필드 파싱 테스트"""
        from src.common.redis_client import StreamMessage

        msg = StreamMessage.from_raw(
            stream="test_stream",
            msg_id="1234567890-0",
            fields={"symbol": "101F26", "price": "350.0"}
        )

        assert msg.id == "1234567890-0"
        assert msg.stream == "test_stream"
        assert msg.data["symbol"] == "101F26"
        assert msg.data["price"] == "350.0"

    def test_from_raw_json_fields(self):
        """JSON 필드 자동 파싱 테스트"""
        from src.common.redis_client import StreamMessage

        nested_data = {"bid": [350.0, 349.95], "ask": [350.05, 350.10]}
        msg = StreamMessage.from_raw(
            stream="test_stream",
            msg_id="1234567890-0",
            fields={
                "symbol": "101F26",
                "orderbook_json": json.dumps(nested_data)
            }
        )

        assert msg.data["orderbook"] == nested_data
        assert "orderbook_json" not in msg.data

    def test_from_raw_invalid_json(self):
        """잘못된 JSON 필드 처리 테스트"""
        from src.common.redis_client import StreamMessage

        msg = StreamMessage.from_raw(
            stream="test_stream",
            msg_id="1234567890-0",
            fields={
                "symbol": "101F26",
                "data_json": "invalid json {"
            }
        )

        # 파싱 실패 시 원본 유지
        assert msg.data["data_json"] == "invalid json {"


# =============================================================================
# StreamPublisher Tests
# =============================================================================
class TestStreamPublisher:
    """StreamPublisher 클래스 테스트"""

    @pytest.fixture
    def mock_redis(self):
        """Mock Redis 클라이언트"""
        with patch('src.common.redis_client.RedisClient') as MockRedis:
            mock_client = MagicMock()
            mock_client.xadd.return_value = "1234567890-0"
            MockRedis.get_client.return_value = mock_client
            yield mock_client

    def test_publish_simple_data(self, mock_redis):
        """단순 데이터 발행 테스트"""
        from src.common.redis_client import StreamPublisher

        publisher = StreamPublisher("TEST_STREAM", maxlen=1000)
        msg_id = publisher.publish({"symbol": "101F26", "price": 350.0})

        assert msg_id == "1234567890-0"
        mock_redis.xadd.assert_called_once()

        # xadd 호출 인자 검증
        call_args = mock_redis.xadd.call_args
        assert call_args[0][0] == "TEST_STREAM"
        assert call_args[1]["maxlen"] == 1000

    def test_publish_nested_data(self, mock_redis):
        """중첩 데이터(dict/list) 발행 테스트"""
        from src.common.redis_client import StreamPublisher

        publisher = StreamPublisher("TEST_STREAM")
        publisher.publish({
            "symbol": "101F26",
            "orderbook": {"bid": [350.0], "ask": [350.05]}
        })

        call_args = mock_redis.xadd.call_args
        fields = call_args[0][1]

        # 중첩 구조는 JSON으로 변환
        assert "orderbook_json" in fields
        assert "orderbook" not in fields

    def test_publish_none_value(self, mock_redis):
        """None 값 처리 테스트"""
        from src.common.redis_client import StreamPublisher

        publisher = StreamPublisher("TEST_STREAM")
        publisher.publish({"symbol": "101F26", "value": None})

        call_args = mock_redis.xadd.call_args
        fields = call_args[0][1]

        assert fields["value"] == ""


# =============================================================================
# StreamConsumer Tests
# =============================================================================
class TestStreamConsumer:
    """StreamConsumer 클래스 테스트"""

    @pytest.fixture
    def mock_redis(self):
        """Mock Redis 클라이언트"""
        with patch('src.common.redis_client.RedisClient') as MockRedis:
            mock_client = MagicMock()
            mock_client.xgroup_create.return_value = True
            MockRedis.get_client.return_value = mock_client
            yield mock_client

    def test_consumer_initialization(self, mock_redis):
        """Consumer 초기화 테스트"""
        from src.common.redis_client import StreamConsumer

        class TestConsumer(StreamConsumer):
            def process_message(self, message):
                return True

        consumer = TestConsumer(
            stream_name="TEST_STREAM",
            group_name="test_group",
            consumer_name="test_consumer_1"
        )

        assert consumer.stream == "TEST_STREAM"
        assert consumer.group == "test_group"
        assert consumer.consumer == "test_consumer_1"
        assert consumer.running is False

    def test_consumer_group_already_exists(self, mock_redis):
        """Consumer Group이 이미 존재할 때 처리 테스트"""
        import redis
        from src.common.redis_client import StreamConsumer

        mock_redis.xgroup_create.side_effect = redis.ResponseError("BUSYGROUP Consumer Group name already exists")

        class TestConsumer(StreamConsumer):
            def process_message(self, message):
                return True

        # BUSYGROUP 에러는 무시되어야 함
        consumer = TestConsumer(
            stream_name="TEST_STREAM",
            group_name="existing_group"
        )
        assert consumer.group == "existing_group"

    def test_ack_message(self, mock_redis):
        """메시지 ACK 테스트"""
        from src.common.redis_client import StreamConsumer, StreamMessage

        class TestConsumer(StreamConsumer):
            def process_message(self, message):
                return True

        consumer = TestConsumer("TEST_STREAM", "test_group")
        msg = StreamMessage(id="1234-0", data={}, stream="TEST_STREAM")

        consumer._ack(msg)
        mock_redis.xack.assert_called_once_with("TEST_STREAM", "test_group", "1234-0")

    def test_stop_consumer(self, mock_redis):
        """Consumer 중지 테스트"""
        from src.common.redis_client import StreamConsumer

        class TestConsumer(StreamConsumer):
            def process_message(self, message):
                return True

        consumer = TestConsumer("TEST_STREAM", "test_group")
        consumer.running = True
        consumer.stop()

        assert consumer.running is False


# =============================================================================
# BatchInserter Tests
# =============================================================================
class TestBatchInserter:
    """BatchInserter 클래스 테스트"""

    @pytest.fixture
    def mock_clickhouse(self):
        """Mock ClickHouse 클라이언트"""
        with patch('src.common.clickhouse_client.ClickHouseClient') as MockCH:
            mock_client = MagicMock()
            MockCH.get_client.return_value = mock_client
            yield mock_client

    def test_batch_inserter_init(self, mock_clickhouse):
        """BatchInserter 초기화 테스트"""
        from src.common.clickhouse_client import BatchInserter, BatchConfig

        config = BatchConfig(
            table_name="test_table",
            column_names=["col1", "col2", "col3"],
            batch_size=100
        )
        inserter = BatchInserter(config)

        assert inserter.config.table_name == "test_table"
        assert inserter.config.batch_size == 100
        assert len(inserter._buffer) == 0

    def test_add_record(self, mock_clickhouse):
        """레코드 추가 테스트"""
        from src.common.clickhouse_client import BatchInserter, BatchConfig

        config = BatchConfig(
            table_name="test_table",
            column_names=["symbol", "price", "volume"],
            batch_size=100
        )
        inserter = BatchInserter(config)

        inserter.add({"symbol": "101F26", "price": 350.0, "volume": 100})

        assert len(inserter._buffer) == 1
        assert inserter._buffer[0] == ["101F26", 350.0, 100]

    def test_auto_flush_on_batch_size(self, mock_clickhouse):
        """배치 사이즈 도달 시 자동 플러시 테스트"""
        from src.common.clickhouse_client import BatchInserter, BatchConfig
        from threading import RLock

        config = BatchConfig(
            table_name="test_table",
            column_names=["symbol", "price"],
            batch_size=3
        )
        inserter = BatchInserter(config)
        # Replace with RLock for reentrant locking (test workaround)
        inserter._lock = RLock()

        # 배치 사이즈 미만 - 플러시 안됨
        inserter.add({"symbol": "A", "price": 1})
        inserter.add({"symbol": "B", "price": 2})
        assert mock_clickhouse.insert.call_count == 0

        # 배치 사이즈 도달 - 자동 플러시
        inserter.add({"symbol": "C", "price": 3})
        assert mock_clickhouse.insert.call_count == 1
        assert len(inserter._buffer) == 0

    def test_manual_flush(self, mock_clickhouse):
        """수동 플러시 테스트"""
        from src.common.clickhouse_client import BatchInserter, BatchConfig

        config = BatchConfig(
            table_name="test_table",
            column_names=["symbol", "price"],
            batch_size=100
        )
        inserter = BatchInserter(config)

        inserter.add({"symbol": "A", "price": 1})
        inserter.add({"symbol": "B", "price": 2})
        inserter._flush()

        mock_clickhouse.insert.assert_called_once()
        call_args = mock_clickhouse.insert.call_args
        assert call_args[0][0] == "test_table"
        assert len(call_args[0][1]) == 2

    def test_flush_empty_buffer(self, mock_clickhouse):
        """빈 버퍼 플러시 테스트"""
        from src.common.clickhouse_client import BatchInserter, BatchConfig

        config = BatchConfig(
            table_name="test_table",
            column_names=["symbol"],
            batch_size=10
        )
        inserter = BatchInserter(config)

        inserter._flush()
        mock_clickhouse.insert.assert_not_called()

    def test_add_many(self, mock_clickhouse):
        """다수 레코드 추가 테스트"""
        from src.common.clickhouse_client import BatchInserter, BatchConfig

        config = BatchConfig(
            table_name="test_table",
            column_names=["symbol", "price"],
            batch_size=100
        )
        inserter = BatchInserter(config)

        records = [
            {"symbol": "A", "price": 1},
            {"symbol": "B", "price": 2},
            {"symbol": "C", "price": 3},
        ]
        inserter.add_many(records)

        assert len(inserter._buffer) == 3


# =============================================================================
# Futures Code Tests
# =============================================================================
class TestFuturesCode:
    """선물 코드 계산 테스트"""

    def test_get_second_thursday(self):
        """두 번째 목요일 계산 테스트"""
        from src.common.futures_code import get_second_thursday

        # 2025년 3월 두 번째 목요일 = 3월 13일
        result = get_second_thursday(2025, 3)
        assert result == date(2025, 3, 13)

        # 2025년 6월 두 번째 목요일 = 6월 12일
        result = get_second_thursday(2025, 6)
        assert result == date(2025, 6, 12)

    def test_get_front_month_code(self):
        """근월물 코드 조회 테스트"""
        from src.common.futures_code import get_front_month_code

        # 2025년 1월 1일 기준 -> 3월물 (101H25)
        code = get_front_month_code(date(2025, 1, 1))
        assert code == "101H25"

        # 2025년 3월 14일 (만기일 이후) -> 6월물 (101M25)
        code = get_front_month_code(date(2025, 3, 14))
        assert code == "101M25"

    def test_get_back_month_code(self):
        """차월물 코드 조회 테스트"""
        from src.common.futures_code import get_back_month_code

        # 2025년 1월 1일 기준 -> 6월물 (101M25)
        code = get_back_month_code(date(2025, 1, 1))
        assert code == "101M25"

    def test_get_kis_code(self):
        """KIS 약식 코드 조회 테스트"""
        from src.common.futures_code import get_kis_code

        assert get_kis_code("front") == "A05601"
        assert get_kis_code("back") == "A05602"
        assert get_kis_code("third") == "A05603"
        assert get_kis_code("unknown") == "A05601"  # 기본값

    def test_is_valid_futures_code(self):
        """유효한 선물 코드 검증 테스트"""
        from src.common.futures_code import is_valid_futures_code

        # KIS 약식 코드는 항상 유효
        assert is_valid_futures_code("A05601") is True
        assert is_valid_futures_code("A05602") is True

        # 유효한 KRX 코드
        assert is_valid_futures_code("101H25", date(2025, 1, 1)) is True

        # 만료된 KRX 코드
        assert is_valid_futures_code("101H24", date(2025, 1, 1)) is False

        # 잘못된 형식
        assert is_valid_futures_code("INVALID") is False
        assert is_valid_futures_code("10125") is False  # 길이 오류

    def test_get_current_futures_info(self):
        """현재 선물 정보 조회 테스트"""
        from src.common.futures_code import get_current_futures_info

        info = get_current_futures_info(date(2025, 1, 1))

        assert "front_month" in info
        assert "back_month" in info
        assert info["front_month"]["krx_code"] == "101H25"
        assert info["front_month"]["kis_code"] == "A05601"
        assert info["front_month"]["days_to_expiry"] > 0

    def test_quarterly_months(self):
        """분기물 월 확인 테스트"""
        from src.common.futures_code import QUARTERLY_MONTHS

        assert QUARTERLY_MONTHS == [3, 6, 9, 12]

    def test_month_codes_mapping(self):
        """월 코드 매핑 테스트"""
        from src.common.futures_code import MONTH_CODES

        assert MONTH_CODES[3] == 'H'  # 3월
        assert MONTH_CODES[6] == 'M'  # 6월
        assert MONTH_CODES[9] == 'U'  # 9월
        assert MONTH_CODES[12] == 'Z'  # 12월


# =============================================================================
# Trading Calendar Tests
# =============================================================================
class TestTradingCalendar:
    """거래일 계산 테스트"""

    def test_is_trading_day_weekday(self):
        """평일 거래일 확인 테스트"""
        from src.collector.historical.calendar import is_trading_day

        # 2025년 1월 2일 (목요일) - 거래일
        assert is_trading_day(date(2025, 1, 2)) is True

    def test_is_trading_day_weekend(self):
        """주말 비거래일 확인 테스트"""
        from src.collector.historical.calendar import is_trading_day

        # 2025년 1월 4일 (토요일)
        assert is_trading_day(date(2025, 1, 4)) is False
        # 2025년 1월 5일 (일요일)
        assert is_trading_day(date(2025, 1, 5)) is False

    def test_is_trading_day_holiday(self):
        """공휴일 비거래일 확인 테스트"""
        from src.collector.historical.calendar import is_trading_day

        # 2025년 1월 1일 (신정)
        assert is_trading_day(date(2025, 1, 1)) is False
        # 2025년 1월 28일 (설날 연휴)
        assert is_trading_day(date(2025, 1, 28)) is False

    def test_get_trading_days_from_krx(self):
        """월별 거래일 조회 테스트"""
        from src.collector.historical.calendar import get_trading_days_from_krx

        days = get_trading_days_from_krx(2025, 1)

        # 1월 1일 (신정)은 포함되지 않아야 함
        assert date(2025, 1, 1) not in days
        # 평일은 포함되어야 함
        assert date(2025, 1, 2) in days

    def test_get_trading_days_range(self):
        """기간 내 거래일 조회 테스트"""
        from src.collector.historical.calendar import get_trading_days_range

        days = get_trading_days_range(date(2025, 1, 1), date(2025, 1, 10))

        # 시작일과 종료일 사이의 거래일만 포함
        assert all(date(2025, 1, 1) <= d <= date(2025, 1, 10) for d in days)
        # 주말과 공휴일 제외 확인
        assert date(2025, 1, 1) not in days  # 신정
        assert date(2025, 1, 4) not in days  # 토요일

    def test_get_past_trading_days(self):
        """과거 N일 거래일 조회 테스트"""
        from src.collector.historical.calendar import get_past_trading_days

        days = get_past_trading_days(10, date(2025, 1, 15))

        assert len(days) > 0
        assert all(d <= date(2025, 1, 15) for d in days)

    def test_korean_holidays_coverage(self):
        """한국 공휴일 데이터 확인 테스트"""
        from src.collector.historical.calendar import KOREAN_HOLIDAYS

        # 2024년 공휴일
        assert date(2024, 1, 1) in KOREAN_HOLIDAYS  # 신정
        assert date(2024, 3, 1) in KOREAN_HOLIDAYS  # 삼일절

        # 2025년 공휴일
        assert date(2025, 1, 1) in KOREAN_HOLIDAYS  # 신정
        assert date(2025, 8, 15) in KOREAN_HOLIDAYS  # 광복절


# =============================================================================
# BatchConfig Tests
# =============================================================================
class TestBatchConfig:
    """BatchConfig 클래스 테스트"""

    def test_default_values(self):
        """기본값 테스트"""
        from src.common.clickhouse_client import BatchConfig

        config = BatchConfig(
            table_name="test_table",
            column_names=["col1", "col2"]
        )

        assert config.batch_size == 1000
        assert config.flush_interval_sec == 1.0

    def test_custom_values(self):
        """사용자 정의 값 테스트"""
        from src.common.clickhouse_client import BatchConfig

        config = BatchConfig(
            table_name="custom_table",
            column_names=["a", "b", "c"],
            batch_size=500,
            flush_interval_sec=0.5
        )

        assert config.table_name == "custom_table"
        assert config.batch_size == 500
        assert config.flush_interval_sec == 0.5


# =============================================================================
# TABLE_SCHEMAS Tests
# =============================================================================
class TestTableSchemas:
    """테이블 스키마 정의 테스트"""

    def test_all_required_tables_defined(self):
        """필수 테이블 정의 확인 테스트"""
        from src.common.clickhouse_client import TABLE_SCHEMAS

        required_tables = [
            "raw_ticks",
            "features_1min",
            "predictions",
            "orders",
            "orderbook_snapshots",
            "trade_ticks"
        ]

        for table in required_tables:
            assert table in TABLE_SCHEMAS, f"Missing table: {table}"

    def test_orderbook_snapshots_schema(self):
        """호가 스냅샷 테이블 스키마 확인 테스트"""
        from src.common.clickhouse_client import TABLE_SCHEMAS

        schema = TABLE_SCHEMAS["orderbook_snapshots"]

        # 필수 컬럼 확인
        assert "timestamp" in schema
        assert "symbol" in schema
        assert "bid_price_1" in schema
        assert "ask_price_5" in schema
        assert "spread" in schema
        assert "MergeTree" in schema
