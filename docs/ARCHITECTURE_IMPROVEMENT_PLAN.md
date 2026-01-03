# Architecture Improvement Plan

Based on the architecture review, this document outlines concrete implementation plans for each identified improvement area.

---

## Overview

| Priority | Improvement | Effort | Impact |
|----------|-------------|--------|--------|
| **P0** | Circuit Breaker Pattern | Medium | High |
| **P1** | Distributed Tracing | Low | Medium |
| **P1** | Backpressure Handling | Medium | High |
| **P2** | State Recovery | Medium | Medium |
| **P2** | Asyncio Unification | High | Medium |
| **P3** | Contract Testing | Low | Low |

---

## P0: Circuit Breaker Pattern

### Problem
KIS API failures can cascade through the system. If the API becomes unresponsive, the collector keeps retrying indefinitely, potentially causing:
- Memory buildup from queued requests
- Blocking of other operations
- No graceful degradation

### Solution
Implement a Circuit Breaker with three states: CLOSED (normal), OPEN (failing), HALF_OPEN (testing recovery).

### Implementation

#### 1. Create `src/common/circuit_breaker.py`

```python
"""
Circuit Breaker Pattern Implementation
Prevents cascading failures from external API outages
"""
import time
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Callable, Optional, Any
from functools import wraps
import threading

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject calls
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreakerConfig:
    """Circuit Breaker configuration"""
    failure_threshold: int = 5          # Failures before opening
    recovery_timeout: float = 30.0      # Seconds before half-open
    success_threshold: int = 3          # Successes before closing
    timeout: float = 10.0               # Call timeout in seconds


@dataclass
class CircuitBreakerStats:
    """Runtime statistics"""
    failures: int = 0
    successes: int = 0
    last_failure_time: float = 0
    state: CircuitState = CircuitState.CLOSED
    total_calls: int = 0
    total_failures: int = 0
    total_rejections: int = 0


class CircuitBreaker:
    """
    Circuit Breaker for external API calls

    Usage:
        breaker = CircuitBreaker("kis_api")

        @breaker
        def call_kis_api():
            ...

        # Or manual usage:
        if breaker.can_execute():
            try:
                result = call_api()
                breaker.record_success()
            except Exception as e:
                breaker.record_failure()
    """

    _instances: dict = {}  # Registry of all breakers

    def __init__(
        self,
        name: str,
        config: CircuitBreakerConfig = None
    ):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self.stats = CircuitBreakerStats()
        self._lock = threading.Lock()

        # Register instance
        CircuitBreaker._instances[name] = self

    @classmethod
    def get(cls, name: str) -> Optional["CircuitBreaker"]:
        """Get a registered circuit breaker by name"""
        return cls._instances.get(name)

    @classmethod
    def get_all_stats(cls) -> dict:
        """Get stats for all circuit breakers"""
        return {
            name: {
                "state": cb.stats.state.value,
                "failures": cb.stats.failures,
                "total_calls": cb.stats.total_calls,
                "total_failures": cb.stats.total_failures,
                "total_rejections": cb.stats.total_rejections,
            }
            for name, cb in cls._instances.items()
        }

    @property
    def state(self) -> CircuitState:
        return self.stats.state

    def can_execute(self) -> bool:
        """Check if a call can be made"""
        with self._lock:
            if self.stats.state == CircuitState.CLOSED:
                return True

            if self.stats.state == CircuitState.OPEN:
                # Check if recovery timeout has passed
                if time.time() - self.stats.last_failure_time >= self.config.recovery_timeout:
                    self._transition_to(CircuitState.HALF_OPEN)
                    return True
                return False

            # HALF_OPEN: allow limited calls
            return True

    def record_success(self):
        """Record a successful call"""
        with self._lock:
            self.stats.total_calls += 1
            self.stats.successes += 1

            if self.stats.state == CircuitState.HALF_OPEN:
                if self.stats.successes >= self.config.success_threshold:
                    self._transition_to(CircuitState.CLOSED)
            elif self.stats.state == CircuitState.CLOSED:
                # Reset failure count on success
                self.stats.failures = 0

    def record_failure(self):
        """Record a failed call"""
        with self._lock:
            self.stats.total_calls += 1
            self.stats.total_failures += 1
            self.stats.failures += 1
            self.stats.last_failure_time = time.time()

            if self.stats.state == CircuitState.HALF_OPEN:
                # Any failure in half-open goes back to open
                self._transition_to(CircuitState.OPEN)
            elif self.stats.state == CircuitState.CLOSED:
                if self.stats.failures >= self.config.failure_threshold:
                    self._transition_to(CircuitState.OPEN)

    def record_rejection(self):
        """Record a rejected call (circuit open)"""
        with self._lock:
            self.stats.total_rejections += 1

    def _transition_to(self, new_state: CircuitState):
        """Transition to a new state"""
        old_state = self.stats.state
        self.stats.state = new_state
        self.stats.failures = 0
        self.stats.successes = 0

        logger.warning(
            f"Circuit Breaker [{self.name}]: {old_state.value} -> {new_state.value}"
        )

        # Notify via Telegram if configured
        if new_state == CircuitState.OPEN:
            self._notify_open()

    def _notify_open(self):
        """Send notification when circuit opens"""
        try:
            from src.common.telegram import TelegramNotifier
            notifier = TelegramNotifier()
            notifier.send_alert(
                f"Circuit Breaker OPEN: {self.name}\n"
                f"Failures: {self.stats.total_failures}\n"
                f"Recovery in: {self.config.recovery_timeout}s"
            )
        except Exception:
            pass  # Notification is best-effort

    def reset(self):
        """Manual reset to closed state"""
        with self._lock:
            self._transition_to(CircuitState.CLOSED)
            self.stats.failures = 0
            self.stats.successes = 0

    def __call__(self, func: Callable) -> Callable:
        """Decorator usage"""
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not self.can_execute():
                self.record_rejection()
                raise CircuitOpenError(
                    f"Circuit breaker [{self.name}] is OPEN"
                )

            try:
                result = func(*args, **kwargs)
                self.record_success()
                return result
            except Exception as e:
                self.record_failure()
                raise

        return wrapper


class CircuitOpenError(Exception):
    """Raised when circuit breaker is open"""
    pass


# Pre-configured breakers for common use cases
def get_kis_api_breaker() -> CircuitBreaker:
    """Get or create the KIS API circuit breaker"""
    breaker = CircuitBreaker.get("kis_api")
    if breaker is None:
        breaker = CircuitBreaker(
            "kis_api",
            CircuitBreakerConfig(
                failure_threshold=5,
                recovery_timeout=60.0,  # 1 minute recovery
                success_threshold=3,
                timeout=10.0
            )
        )
    return breaker


def get_clickhouse_breaker() -> CircuitBreaker:
    """Get or create the ClickHouse circuit breaker"""
    breaker = CircuitBreaker.get("clickhouse")
    if breaker is None:
        breaker = CircuitBreaker(
            "clickhouse",
            CircuitBreakerConfig(
                failure_threshold=3,
                recovery_timeout=30.0,
                success_threshold=2,
                timeout=5.0
            )
        )
    return breaker
```

#### 2. Update `src/collector/kis_websocket.py`

```python
# Add at top of file
from src.common.circuit_breaker import get_kis_api_breaker, CircuitOpenError

# Wrap API calls
class KISWebSocketAdapter:
    def __init__(self, config: KISConfig):
        self.breaker = get_kis_api_breaker()
        ...

    async def connect(self):
        if not self.breaker.can_execute():
            self.breaker.record_rejection()
            raise CircuitOpenError("KIS API circuit is open")

        try:
            # existing connection logic
            await self._do_connect()
            self.breaker.record_success()
        except Exception as e:
            self.breaker.record_failure()
            raise
```

#### 3. Add Prometheus metrics for circuit breaker

```python
# In src/common/metrics.py
from prometheus_client import Gauge

CIRCUIT_BREAKER_STATE = Gauge(
    'circuit_breaker_state',
    'Circuit breaker state (0=closed, 1=open, 2=half_open)',
    ['name']
)

CIRCUIT_BREAKER_FAILURES = Counter(
    'circuit_breaker_failures_total',
    'Total circuit breaker failures',
    ['name']
)
```

### Files to Create/Modify
- **Create**: `src/common/circuit_breaker.py`
- **Modify**: `src/collector/kis_websocket.py`
- **Modify**: `src/common/clickhouse_client.py`
- **Modify**: `src/common/metrics.py`
- **Modify**: `src/common/__init__.py`

### Testing
```python
# tests/test_circuit_breaker.py
def test_circuit_opens_after_failures():
    breaker = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=3))

    for _ in range(3):
        breaker.record_failure()

    assert breaker.state == CircuitState.OPEN
    assert not breaker.can_execute()
```

---

## P1: Distributed Tracing

### Problem
When debugging issues across the pipeline (Collector → Processor → Strategy), there's no way to trace a single message through all services.

### Solution
Add a `correlation_id` to all stream messages that propagates through the pipeline.

### Implementation

#### 1. Update `StreamMessage` in `src/common/redis_client.py`

```python
import uuid
from dataclasses import dataclass, field

@dataclass
class StreamMessage:
    """Stream message with tracing support"""
    id: str
    data: Dict[str, Any]
    stream: str
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    parent_id: Optional[str] = None  # For tracing lineage
    timestamp: float = field(default_factory=time.time)

    @classmethod
    def from_raw(cls, stream: str, msg_id: str, fields: Dict[str, str]) -> "StreamMessage":
        parsed = {}
        correlation_id = fields.pop('_correlation_id', str(uuid.uuid4())[:8])
        parent_id = fields.pop('_parent_id', None)
        timestamp = float(fields.pop('_timestamp', time.time()))

        for k, v in fields.items():
            if k.endswith('_json'):
                try:
                    parsed[k.replace('_json', '')] = json.loads(v)
                except json.JSONDecodeError:
                    parsed[k] = v
            else:
                parsed[k] = v

        return cls(
            id=msg_id,
            data=parsed,
            stream=stream,
            correlation_id=correlation_id,
            parent_id=parent_id,
            timestamp=timestamp
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict with tracing metadata"""
        result = dict(self.data)
        result['_correlation_id'] = self.correlation_id
        result['_parent_id'] = self.parent_id or ''
        result['_timestamp'] = str(self.timestamp)
        return result
```

#### 2. Update `StreamPublisher`

```python
class StreamPublisher:
    def publish(
        self,
        data: Dict[str, Any],
        correlation_id: str = None,
        parent_id: str = None
    ) -> str:
        """Publish with tracing metadata"""
        processed = {}

        # Add tracing metadata
        processed['_correlation_id'] = correlation_id or str(uuid.uuid4())[:8]
        processed['_parent_id'] = parent_id or ''
        processed['_timestamp'] = str(time.time())

        for k, v in data.items():
            if isinstance(v, (dict, list)):
                processed[f"{k}_json"] = json.dumps(v)
            else:
                processed[k] = str(v) if v is not None else ""

        msg_id = self.client.xadd(self.stream, processed, maxlen=self.maxlen)

        logger.debug(
            f"Published to {self.stream}: {msg_id} "
            f"[corr={processed['_correlation_id']}]"
        )
        return msg_id
```

#### 3. Add trace logging

```python
# In src/common/logging_config.py
class TraceFilter(logging.Filter):
    """Add correlation_id to log records"""
    _local = threading.local()

    @classmethod
    def set_correlation_id(cls, corr_id: str):
        cls._local.correlation_id = corr_id

    @classmethod
    def get_correlation_id(cls) -> str:
        return getattr(cls._local, 'correlation_id', '-')

    def filter(self, record):
        record.correlation_id = self.get_correlation_id()
        return True

# Update format
TRACE_FORMAT = "%(asctime)s [%(correlation_id)s] %(name)s %(levelname)s: %(message)s"
```

### Files to Modify
- `src/common/redis_client.py`
- `src/common/logging_config.py`
- `src/processor/feature_processor.py` (propagate correlation_id)
- `src/strategy/strategy_manager.py` (propagate correlation_id)

---

## P1: Backpressure Handling

### Problem
If downstream consumers (Processor, Strategy) slow down, upstream stream buffers may overflow despite `maxlen` limit, causing data loss.

### Solution
1. Monitor consumer lag
2. Alert when lag exceeds threshold
3. Optional: Slow down producer when lag is critical

### Implementation

#### 1. Create `src/common/backpressure.py`

```python
"""
Backpressure monitoring and handling
"""
import logging
from dataclasses import dataclass
from typing import Dict, Optional
from prometheus_client import Gauge

logger = logging.getLogger(__name__)

CONSUMER_LAG = Gauge(
    'stream_consumer_lag',
    'Number of pending messages for consumer group',
    ['stream', 'group']
)

@dataclass
class LagThresholds:
    """Lag thresholds for alerting"""
    warning: int = 1000      # Log warning
    critical: int = 5000     # Send alert
    emergency: int = 8000    # Slow down producer


class BackpressureMonitor:
    """
    Monitors consumer lag across streams

    Usage:
        monitor = BackpressureMonitor(redis_client)

        # Check lag for a specific group
        lag = monitor.get_lag("FEATURE_STREAM", "processor_group")

        # Check if we should slow down
        if monitor.should_throttle("RAW_DATA_STREAM"):
            await asyncio.sleep(0.1)  # Back off
    """

    def __init__(
        self,
        redis_client,
        thresholds: LagThresholds = None
    ):
        self.client = redis_client
        self.thresholds = thresholds or LagThresholds()
        self._last_alert: Dict[str, float] = {}

    def get_lag(self, stream: str, group: str) -> int:
        """Get pending message count for a consumer group"""
        try:
            info = self.client.xpending(stream, group)
            if info:
                pending_count = info['pending']
                CONSUMER_LAG.labels(stream=stream, group=group).set(pending_count)
                return pending_count
            return 0
        except Exception as e:
            logger.error(f"Failed to get lag for {stream}/{group}: {e}")
            return 0

    def check_all_lags(self, stream_groups: Dict[str, str]) -> Dict[str, int]:
        """Check lag for multiple stream/group pairs"""
        results = {}
        for stream, group in stream_groups.items():
            lag = self.get_lag(stream, group)
            results[f"{stream}/{group}"] = lag

            if lag >= self.thresholds.critical:
                self._send_critical_alert(stream, group, lag)
            elif lag >= self.thresholds.warning:
                logger.warning(f"High lag on {stream}/{group}: {lag} messages")

        return results

    def should_throttle(self, stream: str, groups: list = None) -> bool:
        """Check if producer should slow down"""
        if groups is None:
            groups = self._get_groups_for_stream(stream)

        for group in groups:
            lag = self.get_lag(stream, group)
            if lag >= self.thresholds.emergency:
                logger.warning(
                    f"EMERGENCY: Throttling producer for {stream}, lag={lag}"
                )
                return True
        return False

    def _get_groups_for_stream(self, stream: str) -> list:
        """Get all consumer groups for a stream"""
        try:
            groups_info = self.client.xinfo_groups(stream)
            return [g['name'] for g in groups_info]
        except Exception:
            return []

    def _send_critical_alert(self, stream: str, group: str, lag: int):
        """Send alert for critical lag (rate limited)"""
        import time
        key = f"{stream}/{group}"
        now = time.time()

        # Rate limit: 1 alert per 5 minutes per stream/group
        if key in self._last_alert:
            if now - self._last_alert[key] < 300:
                return

        self._last_alert[key] = now

        try:
            from src.common.telegram import TelegramNotifier
            notifier = TelegramNotifier()
            notifier.send_alert(
                f"CRITICAL LAG: {stream}/{group}\n"
                f"Pending: {lag} messages\n"
                f"Threshold: {self.thresholds.critical}"
            )
        except Exception:
            pass
```

#### 2. Integrate into collectors

```python
# In src/collector/tick_collector.py
class TickDataCollector:
    def __init__(self, ...):
        ...
        self._backpressure = BackpressureMonitor(
            RedisClient.get_client()
        )

    def _on_orderbook(self, tick: TickData):
        # Check backpressure before publishing
        if self._backpressure.should_throttle(settings.redis.raw_stream):
            logger.warning("Backpressure detected, slowing down")
            time.sleep(0.05)  # 50ms back-off

        self._redis_publisher.publish(tick.to_dict())
```

### Files to Create/Modify
- **Create**: `src/common/backpressure.py`
- **Modify**: `src/collector/tick_collector.py`
- **Modify**: `src/common/__init__.py`

---

## P2: State Recovery

### Problem
`FeatureProcessor` maintains rolling windows (OFI history, Z-score stats) in memory. On crash:
- 60-bar feature window lost
- Z-score normalization resets (mean/std)
- First ~60 minutes of data after restart produce invalid features

### Solution
Periodic state snapshots to Redis with automatic recovery on startup.

### Implementation

#### 1. Create `src/common/state_snapshot.py`

```python
"""
State snapshot management for crash recovery
"""
import json
import pickle
import logging
from typing import Any, Optional
from dataclasses import dataclass, asdict
import time

logger = logging.getLogger(__name__)


@dataclass
class SnapshotMetadata:
    """Snapshot metadata"""
    service: str
    timestamp: float
    version: str = "1.0"


class StateSnapshotManager:
    """
    Manages state snapshots in Redis

    Usage:
        manager = StateSnapshotManager(redis_client, "feature_processor")

        # Save state periodically
        manager.save_snapshot({
            "ofi_history": list(ofi_deque),
            "zscore_mean": mean,
            "zscore_std": std,
            "feature_window": list(window)
        })

        # Restore on startup
        state = manager.load_snapshot()
        if state:
            ofi_deque = deque(state["ofi_history"], maxlen=100)
    """

    SNAPSHOT_KEY_PREFIX = "state_snapshot:"
    SNAPSHOT_TTL = 3600 * 24  # 24 hours

    def __init__(
        self,
        redis_client,
        service_name: str,
        snapshot_interval: float = 60.0  # seconds
    ):
        self.client = redis_client
        self.service = service_name
        self.interval = snapshot_interval
        self._last_snapshot = 0

    def _key(self, suffix: str = "") -> str:
        return f"{self.SNAPSHOT_KEY_PREFIX}{self.service}{suffix}"

    def save_snapshot(self, state: dict, force: bool = False) -> bool:
        """
        Save state snapshot if interval has passed
        Returns True if snapshot was saved
        """
        now = time.time()
        if not force and (now - self._last_snapshot) < self.interval:
            return False

        try:
            metadata = SnapshotMetadata(
                service=self.service,
                timestamp=now
            )

            payload = {
                "metadata": asdict(metadata),
                "state": state
            }

            # Use pickle for complex objects (deques, etc.)
            serialized = pickle.dumps(payload)

            self.client.setex(
                self._key(),
                self.SNAPSHOT_TTL,
                serialized
            )

            self._last_snapshot = now
            logger.debug(f"State snapshot saved for {self.service}")
            return True

        except Exception as e:
            logger.error(f"Failed to save snapshot: {e}")
            return False

    def load_snapshot(self) -> Optional[dict]:
        """Load the latest snapshot if available"""
        try:
            serialized = self.client.get(self._key())
            if not serialized:
                logger.info(f"No snapshot found for {self.service}")
                return None

            payload = pickle.loads(serialized)
            metadata = payload["metadata"]
            state = payload["state"]

            age = time.time() - metadata["timestamp"]
            logger.info(
                f"Loaded snapshot for {self.service}, age={age:.1f}s"
            )

            # Warn if snapshot is old
            if age > 300:  # 5 minutes
                logger.warning(
                    f"Snapshot is {age:.1f}s old, data may be stale"
                )

            return state

        except Exception as e:
            logger.error(f"Failed to load snapshot: {e}")
            return None

    def clear_snapshot(self):
        """Delete the snapshot"""
        self.client.delete(self._key())
```

#### 2. Update `FeatureProcessor`

```python
# In src/processor/feature_processor.py
class FeatureProcessor(StreamConsumer):
    def __init__(self, ...):
        ...
        self._snapshot_manager = StateSnapshotManager(
            self.client,
            "feature_processor",
            snapshot_interval=60.0
        )
        self._restore_state()

    def _restore_state(self):
        """Restore state from snapshot on startup"""
        state = self._snapshot_manager.load_snapshot()
        if state:
            for symbol, symbol_state in state.get("symbols", {}).items():
                self.states[symbol] = SymbolState.from_dict(symbol_state)
            logger.info(f"Restored state for {len(self.states)} symbols")

    def _save_state(self):
        """Save current state to snapshot"""
        state = {
            "symbols": {
                symbol: s.to_dict()
                for symbol, s in self.states.items()
            }
        }
        self._snapshot_manager.save_snapshot(state)

    def process_message(self, message: StreamMessage) -> bool:
        result = self._process(message)

        # Periodically save state
        self._save_state()

        return result
```

### Files to Create/Modify
- **Create**: `src/common/state_snapshot.py`
- **Modify**: `src/processor/feature_processor.py`
- **Modify**: `src/processor/calculators.py` (add `to_dict`/`from_dict`)

---

## P2: Asyncio Unification

### Problem
The codebase mixes concurrency models:
- `asyncio` for KIS WebSocket (`tick_collector.py`)
- `threading` for batch flusher (`BatchInserter`)
- Synchronous Redis in `StreamConsumer`

This causes:
- Complexity in error handling
- Potential deadlocks
- Difficulty in testing

### Solution
Unify on `asyncio` throughout with `asyncio.Queue` for buffering.

### Implementation Strategy

This is a significant refactor. Recommended approach:

#### Phase 1: Add async variants (non-breaking)

```python
# src/common/redis_client.py
class AsyncStreamConsumer(ABC):
    """Async version of StreamConsumer"""

    async def run(self):
        self.running = True

        # Process pending first
        pending = await self._read_pending_async()
        for msg in pending:
            try:
                if await self.process_message(msg):
                    await self._ack_async(msg)
            except Exception as e:
                logger.error(f"Error processing pending: {e}")

        # Main loop
        while self.running:
            try:
                messages = await self._read_new_async()
                for msg in messages:
                    try:
                        if await self.process_message(msg):
                            await self._ack_async(msg)
                    except Exception as e:
                        logger.error(f"Error: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Consumer error: {e}")
                await asyncio.sleep(1)

    @abstractmethod
    async def process_message(self, message: StreamMessage) -> bool:
        pass
```

#### Phase 2: Convert BatchInserter to async

```python
# src/common/clickhouse_client.py
class AsyncBatchInserter:
    """Async batch inserter using asyncio.Queue"""

    def __init__(self, config: BatchConfig):
        self.config = config
        self._queue: asyncio.Queue = asyncio.Queue()
        self._flush_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the background flush task"""
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def add(self, row: tuple):
        """Add a row to the buffer"""
        await self._queue.put(row)

    async def _flush_loop(self):
        """Background task that flushes periodically"""
        buffer = []
        last_flush = time.time()

        while True:
            try:
                # Wait for item with timeout
                try:
                    row = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=self.config.flush_interval_sec
                    )
                    buffer.append(row)
                except asyncio.TimeoutError:
                    pass

                # Flush if buffer full or interval passed
                now = time.time()
                should_flush = (
                    len(buffer) >= self.config.batch_size or
                    (buffer and now - last_flush >= self.config.flush_interval_sec)
                )

                if should_flush:
                    await self._flush(buffer)
                    buffer = []
                    last_flush = now

            except asyncio.CancelledError:
                # Final flush on shutdown
                if buffer:
                    await self._flush(buffer)
                break

    async def _flush(self, buffer: list):
        """Flush buffer to ClickHouse"""
        if not buffer:
            return

        try:
            client = ClickHouseClient.get_client()
            client.execute(
                f"INSERT INTO {self.config.table_name} VALUES",
                buffer
            )
            logger.debug(f"Flushed {len(buffer)} rows")
        except Exception as e:
            logger.error(f"Flush failed: {e}")
```

### Migration Path
1. Add `AsyncStreamConsumer` alongside `StreamConsumer`
2. Add `AsyncBatchInserter` alongside `BatchInserter`
3. Convert `FeatureProcessor` to async (pilot)
4. If successful, convert remaining consumers
5. Deprecate sync versions

### Files to Create/Modify
- **Modify**: `src/common/redis_client.py` (add async variants)
- **Modify**: `src/common/clickhouse_client.py` (add async batch inserter)
- **Create**: `src/processor/async_feature_processor.py` (pilot)

---

## P3: Contract Testing

### Problem
Stream producers and consumers share implicit contracts (field names, types, formats). Changes in one can break others silently.

### Solution
Define explicit schemas and validate at runtime (dev mode) or test time.

### Implementation

#### 1. Create `src/common/stream_contracts.py`

```python
"""
Stream message contracts (schemas)
"""
from dataclasses import dataclass
from typing import Dict, Any, List, Optional
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class ContractValidationMode(Enum):
    DISABLED = "disabled"   # Production: no validation
    WARN = "warn"           # Log warnings on mismatch
    STRICT = "strict"       # Raise exception on mismatch


@dataclass
class FieldSpec:
    """Field specification"""
    name: str
    type: type
    required: bool = True
    description: str = ""


@dataclass
class StreamContract:
    """Contract for a stream's messages"""
    stream_name: str
    version: str
    fields: List[FieldSpec]

    def validate(self, data: Dict[str, Any], mode: ContractValidationMode) -> bool:
        """Validate data against contract"""
        if mode == ContractValidationMode.DISABLED:
            return True

        errors = []

        # Check required fields
        for field in self.fields:
            if field.required and field.name not in data:
                errors.append(f"Missing required field: {field.name}")
                continue

            if field.name in data:
                value = data[field.name]
                if not isinstance(value, field.type) and value is not None:
                    errors.append(
                        f"Type mismatch for {field.name}: "
                        f"expected {field.type.__name__}, got {type(value).__name__}"
                    )

        if errors:
            msg = f"Contract validation failed for {self.stream_name}: {errors}"
            if mode == ContractValidationMode.STRICT:
                raise ContractValidationError(msg)
            else:
                logger.warning(msg)
            return False

        return True


class ContractValidationError(Exception):
    pass


# Define contracts
RAW_DATA_CONTRACT = StreamContract(
    stream_name="RAW_DATA_STREAM",
    version="1.0",
    fields=[
        FieldSpec("symbol", str, True, "Futures code (e.g., 101V3000)"),
        FieldSpec("timestamp", (str, float), True, "ISO timestamp or unix"),
        FieldSpec("bid_price_1", (str, float), True, "Best bid price"),
        FieldSpec("ask_price_1", (str, float), True, "Best ask price"),
        FieldSpec("bid_qty_1", (str, int), True, "Best bid quantity"),
        FieldSpec("ask_qty_1", (str, int), True, "Best ask quantity"),
    ]
)

FEATURE_CONTRACT = StreamContract(
    stream_name="FEATURE_STREAM",
    version="1.0",
    fields=[
        FieldSpec("symbol", str, True),
        FieldSpec("timestamp", str, True),
        FieldSpec("ofi_z_score", (str, float), True),
        FieldSpec("liquidity_score", (str, float), True),
        FieldSpec("rsi", (str, float), False),
        FieldSpec("features", list, True, "Feature vector for ML"),
    ]
)

CONTRACTS = {
    "RAW_DATA_STREAM": RAW_DATA_CONTRACT,
    "FEATURE_STREAM": FEATURE_CONTRACT,
}
```

#### 2. Add contract tests

```python
# tests/test_contracts.py
import pytest
from src.common.stream_contracts import (
    RAW_DATA_CONTRACT,
    FEATURE_CONTRACT,
    ContractValidationMode,
)


class TestRawDataContract:
    def test_valid_message(self):
        data = {
            "symbol": "101V3000",
            "timestamp": "2024-01-15T09:00:00",
            "bid_price_1": "350.50",
            "ask_price_1": "350.55",
            "bid_qty_1": "100",
            "ask_qty_1": "150",
        }
        assert RAW_DATA_CONTRACT.validate(data, ContractValidationMode.STRICT)

    def test_missing_required_field(self):
        data = {"symbol": "101V3000"}
        with pytest.raises(ContractValidationError):
            RAW_DATA_CONTRACT.validate(data, ContractValidationMode.STRICT)
```

### Files to Create
- **Create**: `src/common/stream_contracts.py`
- **Create**: `tests/test_contracts.py`

---

## Implementation Roadmap

### Week 1: Foundation
- [ ] Implement Circuit Breaker (`src/common/circuit_breaker.py`)
- [ ] Add tests for Circuit Breaker
- [ ] Integrate with KIS WebSocket adapter

### Week 2: Observability
- [ ] Add distributed tracing (correlation_id)
- [ ] Implement Backpressure Monitor
- [ ] Add Prometheus metrics for both

### Week 3: Resilience
- [ ] Implement State Snapshot Manager
- [ ] Add state recovery to FeatureProcessor
- [ ] Test crash recovery scenarios

### Week 4: Contracts & Testing
- [ ] Define stream contracts
- [ ] Add contract validation tests
- [ ] Document contract versions

### Future: Asyncio Migration
- [ ] Add async variants (non-breaking)
- [ ] Pilot with one service
- [ ] Gradual migration

---

## Configuration Updates

Add to `config/settings.py`:

```python
@dataclass
class ResilienceConfig:
    """Resilience settings"""
    # Circuit Breaker
    circuit_breaker_enabled: bool = True
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_recovery_timeout: float = 60.0

    # Backpressure
    backpressure_warning_lag: int = 1000
    backpressure_critical_lag: int = 5000
    backpressure_emergency_lag: int = 8000

    # State Snapshots
    state_snapshot_enabled: bool = True
    state_snapshot_interval: float = 60.0

    # Contract Validation
    contract_validation_mode: str = "warn"  # disabled | warn | strict


@dataclass
class Settings:
    ...
    resilience: ResilienceConfig = field(default_factory=ResilienceConfig)
```

---

## Success Metrics

| Improvement | Metric | Target |
|-------------|--------|--------|
| Circuit Breaker | Mean time to detect API failure | < 30s |
| Circuit Breaker | False positive rate | < 1% |
| Distributed Tracing | Message trace success rate | 100% |
| Backpressure | Lag alerts before overflow | 100% |
| State Recovery | Feature warmup time after restart | < 5 min |
| Contract Tests | Contract coverage | 100% |
