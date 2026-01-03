"""
Backpressure Monitoring and Handling

Monitors consumer lag across Redis Streams and provides:
- Lag metrics for Prometheus
- Alerts when lag exceeds thresholds
- Producer throttling when lag is critical

Usage:
    monitor = BackpressureMonitor(redis_client)

    # Check lag for a specific group
    lag = monitor.get_lag("FEATURE_STREAM", "processor_group")

    # Check if producer should slow down
    if monitor.should_throttle("RAW_DATA_STREAM"):
        await asyncio.sleep(0.1)  # Back off
"""
import time
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from threading import Lock

logger = logging.getLogger(__name__)


@dataclass
class LagThresholds:
    """Lag thresholds for alerting and throttling"""
    warning: int = 1000      # Log warning
    critical: int = 5000     # Send alert
    emergency: int = 8000    # Slow down producer


@dataclass
class LagInfo:
    """Lag information for a consumer group"""
    stream: str
    group: str
    pending_count: int
    oldest_pending_id: Optional[str] = None
    oldest_pending_time: Optional[float] = None
    consumer_count: int = 0
    timestamp: float = 0


class BackpressureMonitor:
    """
    Monitors consumer lag across Redis Streams

    Features:
    - Real-time lag monitoring via XPENDING
    - Three-tier alerting (warning/critical/emergency)
    - Producer throttling recommendations
    - Rate-limited alerts (1 per 5 minutes per stream/group)
    """

    def __init__(
        self,
        redis_client,
        thresholds: LagThresholds = None,
        alert_cooldown: float = 300.0  # 5 minutes
    ):
        self.client = redis_client
        self.thresholds = thresholds or LagThresholds()
        self.alert_cooldown = alert_cooldown

        self._last_alerts: Dict[str, float] = {}
        self._lag_cache: Dict[str, LagInfo] = {}
        self._lock = Lock()

    def get_lag(self, stream: str, group: str) -> int:
        """
        Get pending message count for a consumer group

        Args:
            stream: Stream name
            group: Consumer group name

        Returns:
            Number of pending messages (0 if error or not found)
        """
        try:
            # XPENDING returns: [pending_count, min_id, max_id, [[consumer, count], ...]]
            info = self.client.xpending(stream, group)

            if info and info['pending']:
                pending_count = info['pending']

                # Cache the info
                with self._lock:
                    self._lag_cache[f"{stream}/{group}"] = LagInfo(
                        stream=stream,
                        group=group,
                        pending_count=pending_count,
                        oldest_pending_id=info.get('min'),
                        consumer_count=len(info.get('consumers', [])),
                        timestamp=time.time()
                    )

                return pending_count
            return 0

        except Exception as e:
            # Group might not exist yet
            if "NOGROUP" in str(e):
                logger.debug(f"Consumer group {group} not found on {stream}")
            else:
                logger.error(f"Failed to get lag for {stream}/{group}: {e}")
            return 0

    def get_lag_info(self, stream: str, group: str) -> Optional[LagInfo]:
        """Get detailed lag information"""
        self.get_lag(stream, group)  # Refresh cache
        return self._lag_cache.get(f"{stream}/{group}")

    def check_lag(
        self,
        stream: str,
        group: str
    ) -> Tuple[int, str]:
        """
        Check lag and return level

        Returns:
            Tuple of (lag_count, level) where level is:
            - "normal": Below warning threshold
            - "warning": Above warning, below critical
            - "critical": Above critical, below emergency
            - "emergency": Above emergency threshold
        """
        lag = self.get_lag(stream, group)

        if lag >= self.thresholds.emergency:
            level = "emergency"
        elif lag >= self.thresholds.critical:
            level = "critical"
        elif lag >= self.thresholds.warning:
            level = "warning"
        else:
            level = "normal"

        return lag, level

    def check_all_lags(
        self,
        stream_groups: Dict[str, str]
    ) -> Dict[str, Tuple[int, str]]:
        """
        Check lag for multiple stream/group pairs

        Args:
            stream_groups: Dict of {stream_name: group_name}

        Returns:
            Dict of {stream/group: (lag, level)}
        """
        results = {}

        for stream, group in stream_groups.items():
            lag, level = self.check_lag(stream, group)
            key = f"{stream}/{group}"
            results[key] = (lag, level)

            # Handle alerts based on level
            if level == "critical":
                self._send_alert(stream, group, lag, level)
            elif level == "emergency":
                self._send_alert(stream, group, lag, level)
                logger.error(
                    f"EMERGENCY LAG on {stream}/{group}: {lag} messages"
                )
            elif level == "warning":
                logger.warning(f"High lag on {stream}/{group}: {lag} messages")

        return results

    def should_throttle(
        self,
        stream: str,
        groups: List[str] = None
    ) -> bool:
        """
        Check if producer should slow down due to consumer lag

        Args:
            stream: Stream being written to
            groups: Consumer groups to check (auto-detect if None)

        Returns:
            True if producer should throttle
        """
        if groups is None:
            groups = self._get_groups_for_stream(stream)

        for group in groups:
            lag = self.get_lag(stream, group)
            if lag >= self.thresholds.emergency:
                logger.warning(
                    f"Backpressure: throttling producer for {stream}, "
                    f"group={group}, lag={lag}"
                )
                return True

        return False

    def get_throttle_delay(
        self,
        stream: str,
        groups: List[str] = None,
        base_delay: float = 0.01,
        max_delay: float = 1.0
    ) -> float:
        """
        Calculate adaptive throttle delay based on lag

        Returns delay in seconds (0 if no throttling needed)
        """
        if groups is None:
            groups = self._get_groups_for_stream(stream)

        max_lag = 0
        for group in groups:
            lag = self.get_lag(stream, group)
            max_lag = max(max_lag, lag)

        if max_lag < self.thresholds.warning:
            return 0.0

        # Linear scale from warning to emergency
        if max_lag >= self.thresholds.emergency:
            return max_delay

        # Scale between warning and emergency
        lag_range = self.thresholds.emergency - self.thresholds.warning
        lag_above_warning = max_lag - self.thresholds.warning
        ratio = lag_above_warning / lag_range

        return min(base_delay + (ratio * (max_delay - base_delay)), max_delay)

    def _get_groups_for_stream(self, stream: str) -> List[str]:
        """Get all consumer groups for a stream"""
        try:
            groups_info = self.client.xinfo_groups(stream)
            return [g['name'] for g in groups_info]
        except Exception as e:
            logger.debug(f"Could not get groups for {stream}: {e}")
            return []

    def _send_alert(self, stream: str, group: str, lag: int, level: str):
        """Send alert for high lag (rate limited)"""
        key = f"{stream}/{group}"
        now = time.time()

        # Check cooldown
        with self._lock:
            if key in self._last_alerts:
                if now - self._last_alerts[key] < self.alert_cooldown:
                    return
            self._last_alerts[key] = now

        try:
            from src.common.telegram import TelegramNotifier
            notifier = TelegramNotifier()
            notifier.send_alert(
                f"{level.upper()} LAG: {stream}/{group}\n"
                f"Pending: {lag} messages\n"
                f"Threshold: {getattr(self.thresholds, level)}"
            )
        except Exception as e:
            logger.debug(f"Could not send alert: {e}")

    def get_all_lags(self) -> Dict[str, LagInfo]:
        """Get cached lag info for all monitored streams"""
        with self._lock:
            return dict(self._lag_cache)

    def get_metrics(self) -> Dict[str, int]:
        """Get lag metrics for Prometheus"""
        metrics = {}
        for key, info in self._lag_cache.items():
            metrics[key] = info.pending_count
        return metrics


# Global instance
_monitor: Optional[BackpressureMonitor] = None


def get_backpressure_monitor(redis_client=None) -> BackpressureMonitor:
    """Get or create the global backpressure monitor"""
    global _monitor

    if _monitor is None:
        if redis_client is None:
            from src.common.redis_client import RedisClient
            redis_client = RedisClient.get_client()
        _monitor = BackpressureMonitor(redis_client)

    return _monitor
