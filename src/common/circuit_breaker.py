"""
Circuit Breaker Pattern Implementation
Prevents cascading failures from external API outages

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
import time
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Callable, Optional, Dict, Any
from functools import wraps
import threading

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states"""
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Failing, reject calls
    HALF_OPEN = "half_open" # Testing recovery


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
    last_state_change: float = field(default_factory=time.time)


class CircuitBreaker:
    """
    Circuit Breaker for external API calls

    Implements the circuit breaker pattern with three states:
    - CLOSED: Normal operation, calls pass through
    - OPEN: Failure threshold exceeded, calls rejected immediately
    - HALF_OPEN: Testing if service recovered, limited calls allowed
    """

    _instances: Dict[str, "CircuitBreaker"] = {}

    def __init__(
        self,
        name: str,
        config: CircuitBreakerConfig = None
    ):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self.stats = CircuitBreakerStats()
        self._lock = threading.Lock()
        self._listeners: list = []

        # Register instance
        CircuitBreaker._instances[name] = self
        logger.info(f"Circuit breaker [{name}] initialized")

    @classmethod
    def get(cls, name: str) -> Optional["CircuitBreaker"]:
        """Get a registered circuit breaker by name"""
        return cls._instances.get(name)

    @classmethod
    def get_all(cls) -> Dict[str, "CircuitBreaker"]:
        """Get all registered circuit breakers"""
        return dict(cls._instances)

    @classmethod
    def get_all_stats(cls) -> Dict[str, Dict[str, Any]]:
        """Get stats for all circuit breakers"""
        return {
            name: cb.get_stats()
            for name, cb in cls._instances.items()
        }

    @classmethod
    def reset_all(cls):
        """Reset all circuit breakers (for testing)"""
        for cb in cls._instances.values():
            cb.reset()

    @property
    def state(self) -> CircuitState:
        return self.stats.state

    @property
    def is_closed(self) -> bool:
        return self.stats.state == CircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        return self.stats.state == CircuitState.OPEN

    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics"""
        with self._lock:
            return {
                "name": self.name,
                "state": self.stats.state.value,
                "failures": self.stats.failures,
                "successes": self.stats.successes,
                "total_calls": self.stats.total_calls,
                "total_failures": self.stats.total_failures,
                "total_rejections": self.stats.total_rejections,
                "last_failure_time": self.stats.last_failure_time,
                "last_state_change": self.stats.last_state_change,
            }

    def add_listener(self, callback: Callable[[str, CircuitState, CircuitState], None]):
        """Add state change listener: callback(name, old_state, new_state)"""
        self._listeners.append(callback)

    def can_execute(self) -> bool:
        """Check if a call can be made"""
        with self._lock:
            if self.stats.state == CircuitState.CLOSED:
                return True

            if self.stats.state == CircuitState.OPEN:
                # Check if recovery timeout has passed
                elapsed = time.time() - self.stats.last_failure_time
                if elapsed >= self.config.recovery_timeout:
                    self._transition_to(CircuitState.HALF_OPEN)
                    return True
                return False

            # HALF_OPEN: allow limited calls to test recovery
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

    def record_failure(self, error: Exception = None):
        """Record a failed call"""
        with self._lock:
            self.stats.total_calls += 1
            self.stats.total_failures += 1
            self.stats.failures += 1
            self.stats.last_failure_time = time.time()

            if error:
                logger.warning(f"Circuit [{self.name}] failure: {error}")

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
        """Transition to a new state (must hold lock)"""
        old_state = self.stats.state
        if old_state == new_state:
            return

        self.stats.state = new_state
        self.stats.failures = 0
        self.stats.successes = 0
        self.stats.last_state_change = time.time()

        logger.warning(
            f"Circuit Breaker [{self.name}]: {old_state.value} -> {new_state.value}"
        )

        # Notify listeners
        for listener in self._listeners:
            try:
                listener(self.name, old_state, new_state)
            except Exception as e:
                logger.error(f"Listener error: {e}")

        # Send notification if circuit opens
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
        except Exception as e:
            logger.debug(f"Could not send notification: {e}")

    def reset(self):
        """Manual reset to closed state"""
        with self._lock:
            old_state = self.stats.state
            self.stats = CircuitBreakerStats()
            logger.info(f"Circuit [{self.name}] manually reset from {old_state.value}")

    def force_open(self):
        """Force the circuit to open (for maintenance)"""
        with self._lock:
            self._transition_to(CircuitState.OPEN)

    def __call__(self, func: Callable) -> Callable:
        """Decorator usage"""
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not self.can_execute():
                self.record_rejection()
                raise CircuitOpenError(
                    f"Circuit breaker [{self.name}] is OPEN, "
                    f"recovery in {self.config.recovery_timeout - (time.time() - self.stats.last_failure_time):.1f}s"
                )

            try:
                result = func(*args, **kwargs)
                self.record_success()
                return result
            except Exception as e:
                self.record_failure(e)
                raise

        return wrapper

    def __repr__(self) -> str:
        return f"CircuitBreaker(name={self.name}, state={self.stats.state.value})"


class CircuitOpenError(Exception):
    """Raised when circuit breaker is open and call is rejected"""
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


def get_kis_websocket_breaker() -> CircuitBreaker:
    """Get or create the KIS WebSocket circuit breaker"""
    breaker = CircuitBreaker.get("kis_websocket")
    if breaker is None:
        breaker = CircuitBreaker(
            "kis_websocket",
            CircuitBreakerConfig(
                failure_threshold=3,
                recovery_timeout=30.0,
                success_threshold=2,
                timeout=15.0
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


def get_redis_breaker() -> CircuitBreaker:
    """Get or create the Redis circuit breaker"""
    breaker = CircuitBreaker.get("redis")
    if breaker is None:
        breaker = CircuitBreaker(
            "redis",
            CircuitBreakerConfig(
                failure_threshold=5,
                recovery_timeout=15.0,
                success_threshold=2,
                timeout=3.0
            )
        )
    return breaker
