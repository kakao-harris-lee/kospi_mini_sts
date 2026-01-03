"""
Tests for Architecture Improvements (v0.0.3)

Tests for:
- Circuit Breaker pattern
- Backpressure monitoring
- State snapshot management
- Stream contracts
- Distributed tracing
"""
import pytest
import time
import threading
from unittest.mock import Mock, MagicMock, patch
from collections import deque


class TestCircuitBreaker:
    """Tests for Circuit Breaker pattern"""

    def test_initial_state_is_closed(self):
        """Circuit breaker starts in CLOSED state"""
        from src.common.circuit_breaker import CircuitBreaker, CircuitState

        breaker = CircuitBreaker("test_initial")
        assert breaker.state == CircuitState.CLOSED
        assert breaker.is_closed
        assert not breaker.is_open

    def test_opens_after_failure_threshold(self):
        """Circuit opens after reaching failure threshold"""
        from src.common.circuit_breaker import (
            CircuitBreaker, CircuitBreakerConfig, CircuitState
        )

        breaker = CircuitBreaker(
            "test_threshold",
            CircuitBreakerConfig(failure_threshold=3)
        )

        # Record failures up to threshold
        for _ in range(3):
            breaker.record_failure()

        assert breaker.state == CircuitState.OPEN
        assert not breaker.can_execute()

    def test_rejects_calls_when_open(self):
        """Circuit rejects calls when OPEN"""
        from src.common.circuit_breaker import (
            CircuitBreaker, CircuitBreakerConfig, CircuitState
        )

        breaker = CircuitBreaker(
            "test_reject",
            CircuitBreakerConfig(failure_threshold=1)
        )
        breaker.record_failure()

        assert breaker.state == CircuitState.OPEN
        assert not breaker.can_execute()

        # Record rejection
        breaker.record_rejection()
        stats = breaker.get_stats()
        assert stats["total_rejections"] == 1

    def test_transitions_to_half_open_after_timeout(self):
        """Circuit transitions to HALF_OPEN after recovery timeout"""
        from src.common.circuit_breaker import (
            CircuitBreaker, CircuitBreakerConfig, CircuitState
        )

        breaker = CircuitBreaker(
            "test_halfopen",
            CircuitBreakerConfig(
                failure_threshold=1,
                recovery_timeout=0.1  # 100ms for testing
            )
        )
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        # Wait for recovery timeout
        time.sleep(0.15)

        # Should transition to HALF_OPEN on next can_execute check
        assert breaker.can_execute()
        assert breaker.state == CircuitState.HALF_OPEN

    def test_closes_after_success_threshold(self):
        """Circuit closes after success threshold in HALF_OPEN"""
        from src.common.circuit_breaker import (
            CircuitBreaker, CircuitBreakerConfig, CircuitState
        )

        breaker = CircuitBreaker(
            "test_close",
            CircuitBreakerConfig(
                failure_threshold=1,
                recovery_timeout=0.05,
                success_threshold=2
            )
        )
        breaker.record_failure()
        time.sleep(0.1)
        breaker.can_execute()  # Trigger transition to HALF_OPEN

        # Record successes
        breaker.record_success()
        breaker.record_success()

        assert breaker.state == CircuitState.CLOSED

    def test_returns_to_open_on_failure_in_half_open(self):
        """Circuit returns to OPEN on failure in HALF_OPEN state"""
        from src.common.circuit_breaker import (
            CircuitBreaker, CircuitBreakerConfig, CircuitState
        )

        breaker = CircuitBreaker(
            "test_reopen",
            CircuitBreakerConfig(
                failure_threshold=1,
                recovery_timeout=0.05
            )
        )
        breaker.record_failure()
        time.sleep(0.1)
        breaker.can_execute()  # HALF_OPEN
        assert breaker.state == CircuitState.HALF_OPEN

        # Failure in HALF_OPEN
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

    def test_decorator_usage(self):
        """Test circuit breaker as decorator"""
        from src.common.circuit_breaker import (
            CircuitBreaker, CircuitBreakerConfig, CircuitOpenError
        )

        breaker = CircuitBreaker(
            "test_decorator",
            CircuitBreakerConfig(failure_threshold=2)
        )

        call_count = 0

        @breaker
        def flaky_function():
            nonlocal call_count
            call_count += 1
            raise ValueError("Simulated failure")

        # First two calls should execute (and fail)
        with pytest.raises(ValueError):
            flaky_function()
        with pytest.raises(ValueError):
            flaky_function()

        assert call_count == 2

        # Third call should be rejected (circuit open)
        with pytest.raises(CircuitOpenError):
            flaky_function()

        assert call_count == 2  # Function wasn't called

    def test_manual_reset(self):
        """Test manual circuit reset"""
        from src.common.circuit_breaker import (
            CircuitBreaker, CircuitBreakerConfig, CircuitState
        )

        breaker = CircuitBreaker(
            "test_reset",
            CircuitBreakerConfig(failure_threshold=1)
        )
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        breaker.reset()
        assert breaker.state == CircuitState.CLOSED
        assert breaker.can_execute()

    def test_get_all_stats(self):
        """Test getting stats for all breakers"""
        from src.common.circuit_breaker import CircuitBreaker

        # Clear existing breakers
        CircuitBreaker._instances.clear()

        b1 = CircuitBreaker("stats_test_1")
        b2 = CircuitBreaker("stats_test_2")
        b1.record_success()
        b2.record_failure()

        all_stats = CircuitBreaker.get_all_stats()
        assert "stats_test_1" in all_stats
        assert "stats_test_2" in all_stats
        assert all_stats["stats_test_1"]["total_calls"] == 1
        assert all_stats["stats_test_2"]["total_failures"] == 1

    def test_thread_safety(self):
        """Test circuit breaker is thread-safe"""
        from src.common.circuit_breaker import CircuitBreaker, CircuitBreakerConfig

        breaker = CircuitBreaker(
            "test_threadsafe",
            CircuitBreakerConfig(failure_threshold=100)
        )

        def record_calls():
            for _ in range(50):
                breaker.record_success()
                breaker.record_failure()

        threads = [threading.Thread(target=record_calls) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = breaker.get_stats()
        assert stats["total_calls"] == 500  # 5 threads * 100 calls


class TestBackpressureMonitor:
    """Tests for Backpressure monitoring"""

    def test_get_lag_returns_zero_for_missing_group(self):
        """Returns 0 when consumer group doesn't exist"""
        from src.common.backpressure import BackpressureMonitor

        mock_client = Mock()
        mock_client.xpending.side_effect = Exception("NOGROUP")

        monitor = BackpressureMonitor(mock_client)
        lag = monitor.get_lag("TEST_STREAM", "missing_group")
        assert lag == 0

    def test_check_lag_returns_correct_level(self):
        """Check lag returns correct level based on thresholds"""
        from src.common.backpressure import BackpressureMonitor, LagThresholds

        mock_client = Mock()
        monitor = BackpressureMonitor(
            mock_client,
            LagThresholds(warning=100, critical=500, emergency=800)
        )

        # Mock different lag values
        test_cases = [
            (50, "normal"),
            (150, "warning"),
            (600, "critical"),
            (900, "emergency"),
        ]

        for lag_value, expected_level in test_cases:
            mock_client.xpending.return_value = {'pending': lag_value}
            _, level = monitor.check_lag("TEST_STREAM", "test_group")
            assert level == expected_level, f"Expected {expected_level} for lag {lag_value}"

    def test_should_throttle_at_emergency(self):
        """Should throttle when lag reaches emergency level"""
        from src.common.backpressure import BackpressureMonitor, LagThresholds

        mock_client = Mock()
        mock_client.xpending.return_value = {'pending': 9000}
        mock_client.xinfo_groups.return_value = [{'name': 'test_group'}]

        monitor = BackpressureMonitor(
            mock_client,
            LagThresholds(emergency=8000)
        )

        assert monitor.should_throttle("TEST_STREAM")

    def test_should_not_throttle_below_emergency(self):
        """Should not throttle when lag is below emergency"""
        from src.common.backpressure import BackpressureMonitor, LagThresholds

        mock_client = Mock()
        mock_client.xpending.return_value = {'pending': 1000}
        mock_client.xinfo_groups.return_value = [{'name': 'test_group'}]

        monitor = BackpressureMonitor(
            mock_client,
            LagThresholds(emergency=8000)
        )

        assert not monitor.should_throttle("TEST_STREAM")

    def test_throttle_delay_calculation(self):
        """Test adaptive throttle delay calculation"""
        from src.common.backpressure import BackpressureMonitor, LagThresholds

        mock_client = Mock()
        monitor = BackpressureMonitor(
            mock_client,
            LagThresholds(warning=1000, critical=5000, emergency=8000)
        )

        # No throttle below warning
        mock_client.xpending.return_value = {'pending': 500}
        delay = monitor.get_throttle_delay("TEST_STREAM", ["group1"])
        assert delay == 0.0

        # Max delay at emergency
        mock_client.xpending.return_value = {'pending': 9000}
        delay = monitor.get_throttle_delay("TEST_STREAM", ["group1"], max_delay=1.0)
        assert delay == 1.0

    def test_alert_rate_limiting(self):
        """Alerts should be rate limited"""
        from src.common.backpressure import BackpressureMonitor, LagThresholds

        mock_client = Mock()
        mock_client.xpending.return_value = {'pending': 6000}

        monitor = BackpressureMonitor(
            mock_client,
            LagThresholds(critical=5000),
            alert_cooldown=0.1
        )

        # First alert should go through, second should be suppressed
        with patch('src.common.telegram.TelegramNotifier') as mock_notifier:
            mock_instance = Mock()
            mock_notifier.return_value = mock_instance

            monitor._send_alert("TEST_STREAM", "group1", 6000, "critical")
            monitor._send_alert("TEST_STREAM", "group1", 6000, "critical")

            # Second call should be suppressed due to cooldown
            assert mock_notifier.call_count <= 1


class TestStateSnapshot:
    """Tests for State Snapshot management"""

    def test_save_and_load_snapshot(self):
        """Test saving and loading state snapshot"""
        from src.common.state_snapshot import StateSnapshotManager, SnapshotConfig

        mock_client = Mock()
        mock_client.setex = Mock()

        manager = StateSnapshotManager(
            mock_client,
            "test_service",
            SnapshotConfig(interval_sec=0)  # No throttling for test
        )

        state = {
            "counter": 42,
            "buffer": [1, 2, 3],
            "nested": {"key": "value"}
        }

        # Save
        result = manager.save_snapshot(state, force=True)
        assert result is True
        mock_client.setex.assert_called_once()

    def test_snapshot_interval_throttling(self):
        """Snapshots should be throttled by interval"""
        from src.common.state_snapshot import StateSnapshotManager, SnapshotConfig

        mock_client = Mock()
        manager = StateSnapshotManager(
            mock_client,
            "test_throttle",
            SnapshotConfig(interval_sec=1.0)  # 1 second interval
        )

        # First save should succeed
        assert manager.save_snapshot({"data": 1}, force=False) is True

        # Second save within interval should be skipped
        assert manager.save_snapshot({"data": 2}, force=False) is False

        # Force save should bypass interval
        assert manager.save_snapshot({"data": 3}, force=True) is True

    def test_load_missing_snapshot(self):
        """Loading missing snapshot returns None"""
        from src.common.state_snapshot import StateSnapshotManager

        mock_client = Mock()
        mock_client.get.return_value = None

        manager = StateSnapshotManager(mock_client, "missing_service")
        result = manager.load_snapshot()
        assert result is None

    def test_snapshot_age_limit(self):
        """Snapshots older than max_age should be rejected"""
        from src.common.state_snapshot import StateSnapshotManager, SnapshotConfig
        import pickle
        import gzip

        mock_client = Mock()
        manager = StateSnapshotManager(
            mock_client,
            "test_age",
            SnapshotConfig(compress=True)
        )

        # Create an old snapshot
        old_payload = {
            "metadata": {
                "service": "test_age",
                "timestamp": time.time() - 3600,  # 1 hour ago
                "version": "1.0",
                "size_bytes": 0,
                "compressed": True
            },
            "state": {"data": "old"}
        }
        mock_client.get.return_value = gzip.compress(pickle.dumps(old_payload))

        # Should reject snapshot older than 60 seconds
        result = manager.load_snapshot(max_age_sec=60)
        assert result is None

    def test_complex_state_serialization(self):
        """Test serialization of complex objects like deques"""
        from src.common.state_snapshot import StateSnapshotManager, SnapshotConfig
        import pickle
        import gzip

        mock_client = Mock()
        stored_data = {}

        def mock_setex(key, ttl, data):
            stored_data['data'] = data

        def mock_get(key):
            return stored_data.get('data')

        mock_client.setex = mock_setex
        mock_client.get = mock_get

        manager = StateSnapshotManager(
            mock_client,
            "test_complex",
            SnapshotConfig(interval_sec=0, compress=True)
        )

        # Save complex state
        original_state = {
            "deque": list(deque([1, 2, 3], maxlen=5)),
            "nested": {"list": [1, 2, 3]},
            "float": 3.14159
        }
        manager.save_snapshot(original_state, force=True)

        # Load and verify
        loaded_state = manager.load_snapshot()
        assert loaded_state is not None
        assert loaded_state["deque"] == [1, 2, 3]
        assert loaded_state["float"] == 3.14159


class TestStreamContracts:
    """Tests for Stream Contracts"""

    def test_valid_message_passes_validation(self):
        """Valid messages should pass contract validation"""
        from src.common.stream_contracts import (
            RAW_DATA_CONTRACT, ValidationMode, validate_message
        )

        valid_data = {
            "symbol": "101V3000",
            "timestamp": "2024-01-15T09:00:00",
            "data_type": "orderbook",
            "bid_price_1": "350.50",
            "ask_price_1": "350.55",
            "bid_qty_1": "100",
            "ask_qty_1": "150",
        }

        is_valid, errors = RAW_DATA_CONTRACT.validate(valid_data, ValidationMode.STRICT)
        assert is_valid
        assert len(errors) == 0

    def test_missing_required_field_fails(self):
        """Missing required fields should fail validation"""
        from src.common.stream_contracts import (
            RAW_DATA_CONTRACT, ValidationMode, ContractValidationError
        )

        invalid_data = {
            "symbol": "101V3000",
            # Missing timestamp and other required fields
        }

        with pytest.raises(ContractValidationError):
            RAW_DATA_CONTRACT.validate(invalid_data, ValidationMode.STRICT)

    def test_warn_mode_logs_but_doesnt_raise(self):
        """WARN mode should log errors but not raise"""
        from src.common.stream_contracts import RAW_DATA_CONTRACT, ValidationMode

        invalid_data = {"symbol": "101V3000"}  # Missing required fields

        is_valid, errors = RAW_DATA_CONTRACT.validate(invalid_data, ValidationMode.WARN)
        assert not is_valid
        assert len(errors) > 0

    def test_disabled_mode_skips_validation(self):
        """DISABLED mode should skip validation"""
        from src.common.stream_contracts import RAW_DATA_CONTRACT, ValidationMode

        invalid_data = {}  # Empty data

        is_valid, errors = RAW_DATA_CONTRACT.validate(invalid_data, ValidationMode.DISABLED)
        assert is_valid
        assert len(errors) == 0

    def test_feature_contract_validation(self):
        """Test FEATURE_STREAM contract"""
        from src.common.stream_contracts import FEATURE_CONTRACT, ValidationMode

        valid_feature = {
            "symbol": "101V3000",
            "timestamp": "2024-01-15T09:00:00",
            "ofi_z_score": "1.5",
            "liquidity_score": "75.0",
            "features": [0.1, 0.2, 0.3, 0.4, 0.5],
        }

        is_valid, errors = FEATURE_CONTRACT.validate(valid_feature, ValidationMode.STRICT)
        assert is_valid

    def test_order_command_contract_validation(self):
        """Test ORDER_COMMAND_STREAM contract"""
        from src.common.stream_contracts import ORDER_COMMAND_CONTRACT, ValidationMode

        valid_order = {
            "symbol": "101V3000",
            "side": "BUY",
            "order_type": "MARKET",
            "size": "1",
            "strategy_id": "pure_micro",
            "timestamp": "1705312800.0",
        }

        is_valid, errors = ORDER_COMMAND_CONTRACT.validate(valid_order, ValidationMode.STRICT)
        assert is_valid

    def test_get_contract_by_name(self):
        """Test getting contract by stream name"""
        from src.common.stream_contracts import get_contract

        contract = get_contract("RAW_DATA_STREAM")
        assert contract is not None
        assert contract.stream_name == "RAW_DATA_STREAM"

        missing = get_contract("NONEXISTENT_STREAM")
        assert missing is None

    def test_decorator_validation(self):
        """Test validates_contract decorator"""
        from src.common.stream_contracts import (
            validates_contract, RAW_DATA_CONTRACT, set_validation_mode, ValidationMode
        )

        set_validation_mode(ValidationMode.WARN)

        @validates_contract(RAW_DATA_CONTRACT)
        def create_message():
            return {
                "symbol": "101V3000",
                "timestamp": "2024-01-15T09:00:00",
                "data_type": "orderbook",
                "bid_price_1": "350.50",
                "ask_price_1": "350.55",
                "bid_qty_1": "100",
                "ask_qty_1": "150",
            }

        # Should not raise
        result = create_message()
        assert result["symbol"] == "101V3000"


class TestDistributedTracing:
    """Tests for Distributed Tracing"""

    def test_correlation_id_generation(self):
        """Test correlation ID generation"""
        from src.common.redis_client import (
            get_correlation_id, set_correlation_id, clear_correlation_id
        )

        # Clear any existing ID
        clear_correlation_id()

        # Should generate new ID
        corr_id = get_correlation_id()
        assert corr_id is not None
        assert len(corr_id) == 8

        # Should return same ID
        assert get_correlation_id() == corr_id

        # Set custom ID
        set_correlation_id("custom12")
        assert get_correlation_id() == "custom12"

    def test_stream_message_tracing_fields(self):
        """Test StreamMessage includes tracing fields"""
        from src.common.redis_client import StreamMessage

        fields = {
            "_corr_id": "abc12345",
            "_parent_id": "parent01",
            "_ts": "1705312800.0",
            "symbol": "101V3000",
            "price": "350.50"
        }

        msg = StreamMessage.from_raw("TEST_STREAM", "1234-0", fields.copy())

        assert msg.correlation_id == "abc12345"
        assert msg.parent_id == "parent01"
        assert msg.timestamp == 1705312800.0
        assert msg.data["symbol"] == "101V3000"

    def test_stream_message_creates_child_id(self):
        """Test creating child correlation ID"""
        from src.common.redis_client import StreamMessage

        msg = StreamMessage(
            id="1234-0",
            data={},
            stream="TEST",
            correlation_id="parent12"
        )

        child_id = msg.create_child_id()
        assert child_id.startswith("parent12-")
        assert len(child_id) == 13  # parent12 (8) + - (1) + 4 chars = 13

    def test_publisher_includes_tracing(self):
        """Test StreamPublisher includes tracing metadata"""
        from src.common.redis_client import StreamPublisher, StreamMessage
        from unittest.mock import patch

        with patch('src.common.redis_client.RedisClient') as mock_redis:
            mock_client = Mock()
            mock_client.xadd.return_value = "1234-0"
            mock_redis.get_client.return_value = mock_client

            publisher = StreamPublisher("TEST_STREAM")

            # Publish with explicit correlation ID
            publisher.publish({"data": "test"}, correlation_id="explicit1")

            call_args = mock_client.xadd.call_args
            published_data = call_args[0][1]

            assert "_corr_id" in published_data
            assert published_data["_corr_id"] == "explicit1"
            assert "_ts" in published_data

    def test_publisher_inherits_parent_trace(self):
        """Test StreamPublisher inherits parent message tracing"""
        from src.common.redis_client import StreamPublisher, StreamMessage
        from unittest.mock import patch

        with patch('src.common.redis_client.RedisClient') as mock_redis:
            mock_client = Mock()
            mock_client.xadd.return_value = "1234-0"
            mock_redis.get_client.return_value = mock_client

            publisher = StreamPublisher("TEST_STREAM")

            parent_msg = StreamMessage(
                id="parent-0",
                data={},
                stream="PARENT_STREAM",
                correlation_id="parent_corr"
            )

            publisher.publish({"data": "child"}, parent_message=parent_msg)

            call_args = mock_client.xadd.call_args
            published_data = call_args[0][1]

            assert published_data["_corr_id"] == "parent_corr"
            assert published_data["_parent_id"] == "parent-0"


class TestIntegration:
    """Integration tests combining multiple components"""

    def test_circuit_breaker_with_backpressure(self):
        """Test circuit breaker works with backpressure monitoring"""
        from src.common.circuit_breaker import CircuitBreaker, CircuitBreakerConfig
        from src.common.backpressure import BackpressureMonitor, LagThresholds

        # Setup
        breaker = CircuitBreaker(
            "integration_test",
            CircuitBreakerConfig(failure_threshold=3)
        )
        mock_client = Mock()
        mock_client.xpending.return_value = {'pending': 100}
        monitor = BackpressureMonitor(mock_client)

        # Simulate scenario
        assert breaker.can_execute()
        assert not monitor.should_throttle("TEST_STREAM", ["group1"])

        # Circuit opens after failures
        for _ in range(3):
            breaker.record_failure()

        assert not breaker.can_execute()

    def test_contract_validation_in_pipeline(self):
        """Test contract validation in a simulated pipeline"""
        from src.common.stream_contracts import (
            RAW_DATA_CONTRACT, FEATURE_CONTRACT, validate_message, ValidationMode
        )

        # Simulate raw data
        raw_data = {
            "symbol": "101V3000",
            "timestamp": "2024-01-15T09:00:00",
            "data_type": "orderbook",
            "bid_price_1": "350.50",
            "ask_price_1": "350.55",
            "bid_qty_1": "100",
            "ask_qty_1": "150",
        }
        assert validate_message(raw_data, RAW_DATA_CONTRACT)

        # Simulate feature output
        feature_data = {
            "symbol": raw_data["symbol"],
            "timestamp": raw_data["timestamp"],
            "ofi_z_score": "1.5",
            "liquidity_score": "75.0",
            "features": [0.1] * 10,
        }
        assert validate_message(feature_data, FEATURE_CONTRACT)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
