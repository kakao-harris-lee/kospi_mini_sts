"""
Health Checker for system monitoring.

This module provides the HealthChecker class that monitors the health of
all system services: Redis, ClickHouse, KIS API, and pipeline components.

User Story 2: System health dashboard with service status monitoring

Constitution Compliance:
- I: Async-first architecture (all methods are async)
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional, Dict, List, Callable, Awaitable

from src.common.metrics import get_metrics
from src.monitoring.models import (
    Alert,
    AlertType,
    ServiceHealth,
    HealthStatus,
    ALLOWED_SERVICES,
)
from src.monitoring.alert_service import AlertService

logger = logging.getLogger(__name__)


# Health check timeouts (seconds)
TIMEOUT_REDIS = 2.0
TIMEOUT_CLICKHOUSE = 3.0
TIMEOUT_KIS_API = 5.0
TIMEOUT_PIPELINE = 2.0


class HealthChecker:
    """
    System health monitoring service.

    Features:
    - Health checks for Redis, ClickHouse, KIS API, pipeline services
    - State transitions: healthy -> degraded -> down
    - Prometheus metrics for Grafana dashboard
    - Alert integration for service degradation/failures

    Usage:
        checker = HealthChecker(alert_service, redis_client, clickhouse_client)
        await checker.start()

        # Manual health check
        status = await checker.check_all()

        await checker.stop()
    """

    # Health check interval
    CHECK_INTERVAL = 15.0  # seconds

    # Consecutive failures before state transition
    DEGRADED_THRESHOLD = 1
    DOWN_THRESHOLD = 3

    def __init__(
        self,
        alert_service: AlertService,
        redis_client=None,
        clickhouse_client=None,
        kis_token_manager=None,
    ):
        """
        Initialize health checker.

        Args:
            alert_service: AlertService for sending health alerts
            redis_client: Redis client instance (async)
            clickhouse_client: ClickHouse client instance
            kis_token_manager: KIS token manager for API health check
        """
        self.alert_service = alert_service
        self.redis = redis_client
        self.clickhouse = clickhouse_client
        self.kis_token = kis_token_manager
        self.metrics = get_metrics()

        # Health status cache
        self._health_cache: Dict[str, ServiceHealth] = {}
        for service in ALLOWED_SERVICES:
            self._health_cache[service] = ServiceHealth(service=service)

        # Running state
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Previous state for alert deduplication
        self._previous_status: Dict[str, HealthStatus] = {}

    async def start(self) -> None:
        """Start the health check loop."""
        if self._running:
            logger.warning("HealthChecker already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info("HealthChecker started")

    async def stop(self) -> None:
        """Stop the health check loop."""
        if not self._running:
            return

        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info("HealthChecker stopped")

    async def _check_loop(self) -> None:
        """Main health check loop."""
        logger.info("Health check loop started")

        while self._running:
            try:
                await self.check_all()
            except Exception as e:
                logger.error(f"Error in health check loop: {e}")

            await asyncio.sleep(self.CHECK_INTERVAL)

        logger.info("Health check loop stopped")

    async def check_all(self) -> Dict[str, ServiceHealth]:
        """
        Check health of all services.

        Returns:
            Dictionary mapping service name to health status
        """
        # Run all health checks concurrently
        results = await asyncio.gather(
            self.check_redis(),
            self.check_clickhouse(),
            self.check_kis_api(),
            self.check_pipeline_services(),
            return_exceptions=True,
        )

        # Process results
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Health check error: {result}")

        return self._health_cache

    async def check_redis(self) -> ServiceHealth:
        """Check Redis health with PING command."""
        service = "redis"
        health = self._health_cache[service]
        self.metrics.record_health_check_run(service)

        if not self.redis:
            await self._record_failure(health, "Redis client not configured")
            return health

        start_time = time.monotonic()

        try:
            result = await asyncio.wait_for(
                self.redis.ping(),
                timeout=TIMEOUT_REDIS,
            )
            latency_ms = (time.monotonic() - start_time) * 1000

            if result:
                await self._record_success(health, latency_ms)
            else:
                await self._record_failure(health, "PING returned False")

        except asyncio.TimeoutError:
            await self._record_failure(health, f"Timeout after {TIMEOUT_REDIS}s")
        except Exception as e:
            await self._record_failure(health, str(e))

        return health

    async def check_clickhouse(self) -> ServiceHealth:
        """Check ClickHouse health with SELECT 1 query."""
        service = "clickhouse"
        health = self._health_cache[service]
        self.metrics.record_health_check_run(service)

        if not self.clickhouse:
            await self._record_failure(health, "ClickHouse client not configured")
            return health

        start_time = time.monotonic()

        try:
            # Run synchronous query in executor to not block
            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: self.clickhouse.command("SELECT 1")),
                timeout=TIMEOUT_CLICKHOUSE,
            )
            latency_ms = (time.monotonic() - start_time) * 1000

            if result == 1:
                await self._record_success(health, latency_ms)
            else:
                await self._record_failure(health, f"Unexpected result: {result}")

        except asyncio.TimeoutError:
            await self._record_failure(health, f"Timeout after {TIMEOUT_CLICKHOUSE}s")
        except Exception as e:
            await self._record_failure(health, str(e))

        return health

    async def check_kis_api(self) -> ServiceHealth:
        """Check KIS API health via token validity."""
        service = "kis_api"
        health = self._health_cache[service]
        self.metrics.record_health_check_run(service)

        if not self.kis_token:
            await self._record_failure(health, "KIS token manager not configured")
            return health

        start_time = time.monotonic()

        try:
            # Check token validity
            loop = asyncio.get_running_loop()
            is_valid = await asyncio.wait_for(
                loop.run_in_executor(None, self.kis_token.is_token_valid),
                timeout=TIMEOUT_KIS_API,
            )
            latency_ms = (time.monotonic() - start_time) * 1000

            if is_valid:
                await self._record_success(health, latency_ms)
            else:
                # Try to refresh token
                loop = asyncio.get_running_loop()
                refreshed = await asyncio.wait_for(
                    loop.run_in_executor(None, self.kis_token.refresh_token),
                    timeout=TIMEOUT_KIS_API,
                )
                if refreshed:
                    await self._record_success(health, latency_ms)
                else:
                    await self._record_failure(health, "Token invalid and refresh failed")

        except asyncio.TimeoutError:
            await self._record_failure(health, f"Timeout after {TIMEOUT_KIS_API}s")
        except Exception as e:
            await self._record_failure(health, str(e))

        return health

    async def check_pipeline_services(self) -> List[ServiceHealth]:
        """Check health of pipeline services via component_up metric."""
        services = [
            "tick_collector",
            "feature_processor",
            "prediction_engine",
            "strategy_manager",
        ]

        results = []
        for service in services:
            health = self._health_cache[service]
            self.metrics.record_health_check_run(service)

            # Check via Redis if available (services publish heartbeats)
            if self.redis:
                try:
                    heartbeat_key = f"heartbeat:{service}"
                    last_beat = await self.redis.get(heartbeat_key)

                    if last_beat:
                        # Check if heartbeat is recent (within 60 seconds)
                        last_time = float(last_beat)
                        age = time.time() - last_time

                        if age < 60:
                            await self._record_success(health, age * 1000)
                        else:
                            await self._record_failure(health, f"Stale heartbeat: {age:.0f}s old")
                    else:
                        # No heartbeat found - service may not be running
                        await self._record_failure(health, "No heartbeat found")

                except Exception as e:
                    await self._record_failure(health, str(e))
            else:
                # Without Redis, mark as unknown/degraded
                health.status = HealthStatus.DEGRADED
                health.error = "Cannot check: Redis not available"
                health.last_check = datetime.now(timezone.utc)

            results.append(health)

        return results

    async def _record_success(self, health: ServiceHealth, latency_ms: float) -> None:
        """Record successful health check."""
        previous_status = health.status
        health.record_success(latency_ms)

        # Update metrics
        self.metrics.set_health_check_status(health.service, 1.0)
        self.metrics.set_health_check_latency(health.service, latency_ms / 1000)

        # Send recovery alert if previously down/degraded
        if previous_status != HealthStatus.HEALTHY:
            await self._send_recovery_alert(health, previous_status)

    async def _record_failure(self, health: ServiceHealth, error: str) -> None:
        """Record failed health check."""
        previous_status = health.status
        health.record_failure(error)

        # Update metrics
        status_value = 0.5 if health.status == HealthStatus.DEGRADED else 0.0
        self.metrics.set_health_check_status(health.service, status_value)
        self.metrics.record_health_check_failure(health.service)

        logger.warning(f"Health check failed for {health.service}: {error}")

        # Send alert if status changed
        if health.status != previous_status:
            await self._send_health_alert(health, previous_status)

    async def _send_health_alert(
        self,
        health: ServiceHealth,
        previous_status: HealthStatus,
    ) -> None:
        """Send health degradation/failure alert."""
        if health.status == HealthStatus.DOWN:
            alert_type = AlertType.HEALTH_CRITICAL
            priority = "urgent"
            title = f"SERVICE DOWN: {health.service}"
        else:
            alert_type = AlertType.HEALTH_WARNING
            priority = "high"
            title = f"Service Degraded: {health.service}"

        message = f"""<b>Service:</b> {health.service}
<b>Status:</b> {health.status.value}
<b>Previous:</b> {previous_status.value}
<b>Error:</b> {health.error or 'N/A'}
<b>Failures:</b> {health.consecutive_failures}
<b>Time:</b> {health.last_check.strftime('%H:%M:%S')}"""

        await self.alert_service.send_immediate(
            alert_type=alert_type,
            title=title,
            message=message,
            priority=priority,
        )

    async def _send_recovery_alert(
        self,
        health: ServiceHealth,
        previous_status: HealthStatus,
    ) -> None:
        """Send service recovery alert."""
        message = f"""<b>Service:</b> {health.service}
<b>Status:</b> {health.status.value} (recovered)
<b>Previous:</b> {previous_status.value}
<b>Latency:</b> {health.latency_ms:.1f}ms
<b>Time:</b> {health.last_check.strftime('%H:%M:%S')}"""

        await self.alert_service.send_immediate(
            alert_type=AlertType.HEALTH_WARNING,
            title=f"Service Recovered: {health.service}",
            message=message,
            priority="normal",
        )

    def get_health_status(self, service: str) -> Optional[ServiceHealth]:
        """Get cached health status for a service."""
        return self._health_cache.get(service)

    def get_all_status(self) -> Dict[str, ServiceHealth]:
        """Get cached health status for all services."""
        return self._health_cache.copy()

    def is_all_healthy(self) -> bool:
        """Check if all services are healthy."""
        return all(h.is_healthy for h in self._health_cache.values())

    def get_unhealthy_services(self) -> List[str]:
        """Get list of unhealthy service names."""
        return [
            name for name, health in self._health_cache.items()
            if not health.is_healthy
        ]
