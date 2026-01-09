"""
Anomaly Detector for unusual market conditions.

This module provides the AnomalyDetector class that monitors for various
anomalies including volatility spikes, abnormal signal frequency, and loss streaks.

User Story 4: Anomaly detection alerts for unusual conditions

Constitution Compliance:
- I: Async-first architecture (all methods are async)
"""

import asyncio
import logging
import math
from collections import deque
from datetime import datetime
from typing import Optional, Dict, List, Deque

from src.common.metrics import get_metrics
from src.monitoring.models import Alert, AlertType, Anomaly, AnomalyType
from src.monitoring.alert_service import AlertService

logger = logging.getLogger(__name__)


# Redis keys for anomaly state persistence
REDIS_KEY_ANOMALY_PREFIX = "monitoring:anomaly:"
REDIS_KEY_PRICE_HISTORY = "monitoring:price_history:"
REDIS_KEY_SIGNAL_HISTORY = "monitoring:signal_history:"


class AnomalyDetector:
    """
    Anomaly detection service for trading system.

    Features:
    - Price volatility detection (Z-score > 2.0)
    - Signal frequency anomaly detection (> 3x historical average)
    - Loss streak detection (> 5 consecutive losses)
    - State persistence in Redis
    - Prometheus metrics for Grafana visualization

    Usage:
        detector = AnomalyDetector(alert_service, redis_client)
        await detector.start()

        # Record data points
        await detector.record_price(symbol, price)
        await detector.record_signal(strategy)
        await detector.record_trade_result(strategy, pnl)

        await detector.stop()
    """

    # Detection thresholds
    VOLATILITY_ZSCORE_THRESHOLD = 2.0
    SIGNAL_FREQUENCY_THRESHOLD = 3.0  # 3x normal
    LOSS_STREAK_THRESHOLD = 5

    # Rolling window sizes
    PRICE_WINDOW = 60  # 60 price points
    SIGNAL_WINDOW = 100  # 100 signals for average calculation

    # Detection loop interval
    DETECTION_INTERVAL = 10.0  # seconds

    def __init__(
        self,
        alert_service: AlertService,
        redis_client=None,
    ):
        """
        Initialize anomaly detector.

        Args:
            alert_service: AlertService for sending alerts
            redis_client: Redis client for state persistence (async)
        """
        self.alert_service = alert_service
        self.redis = redis_client
        self.metrics = get_metrics()

        # Price history for volatility detection (symbol -> prices)
        self._price_history: Dict[str, Deque[float]] = {}

        # Signal history for frequency detection (strategy -> timestamps)
        self._signal_history: Dict[str, Deque[float]] = {}

        # Loss streak tracking (strategy -> count)
        self._loss_streak: Dict[str, int] = {}

        # Last known volatility Z-scores (symbol -> zscore)
        self._volatility_zscore: Dict[str, float] = {}

        # Active anomalies for deduplication
        self._active_anomalies: Dict[str, Anomaly] = {}

        # Running state
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the anomaly detection loop."""
        if self._running:
            logger.warning("AnomalyDetector already running")
            return

        self._running = True

        # Restore state from Redis
        await self._restore_state()

        self._task = asyncio.create_task(self._detection_loop())

        # Update detector state metrics
        for detector_type in ["price_volatility", "signal_frequency", "loss_streak"]:
            self.metrics.set_anomaly_detector_state(detector_type, True)

        logger.info("AnomalyDetector started")

    async def stop(self) -> None:
        """Stop the anomaly detection loop."""
        if not self._running:
            return

        self._running = False

        # Persist state to Redis
        await self._persist_state()

        # Update detector state metrics
        for detector_type in ["price_volatility", "signal_frequency", "loss_streak"]:
            self.metrics.set_anomaly_detector_state(detector_type, False)

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info("AnomalyDetector stopped")

    async def _detection_loop(self) -> None:
        """Main detection loop."""
        logger.info("Anomaly detection loop started")

        while self._running:
            try:
                # Run all detectors
                await self._detect_all_anomalies()
            except Exception as e:
                logger.error(f"Error in anomaly detection loop: {e}")

            await asyncio.sleep(self.DETECTION_INTERVAL)

        logger.info("Anomaly detection loop stopped")

    async def record_price(self, symbol: str, price: float) -> None:
        """
        Record a price data point for volatility detection.

        Args:
            symbol: Trading symbol (e.g., "A05601")
            price: Current price
        """
        if symbol not in self._price_history:
            self._price_history[symbol] = deque(maxlen=self.PRICE_WINDOW)

        self._price_history[symbol].append(price)

        # Calculate and update volatility Z-score
        zscore = await self._calculate_volatility_zscore(symbol)
        self._volatility_zscore[symbol] = zscore

        # Update Prometheus metric
        self.metrics.set_price_volatility_zscore(symbol, zscore)

        # Detect if anomalous
        if abs(zscore) > self.VOLATILITY_ZSCORE_THRESHOLD:
            await self._create_volatility_anomaly(symbol, zscore)

    async def record_signal(self, strategy: str) -> None:
        """
        Record a signal generation event for frequency detection.

        Args:
            strategy: Strategy identifier
        """
        import time

        if strategy not in self._signal_history:
            self._signal_history[strategy] = deque(maxlen=self.SIGNAL_WINDOW)

        self._signal_history[strategy].append(time.time())

        # Calculate signal frequency ratio
        ratio = await self._calculate_signal_frequency_ratio(strategy)

        # Update Prometheus metric
        self.metrics.set_signal_frequency_ratio(strategy, ratio)

        # Detect if anomalous
        if ratio > self.SIGNAL_FREQUENCY_THRESHOLD:
            await self._create_signal_frequency_anomaly(strategy, ratio)

    async def record_trade_result(self, strategy: str, pnl: float) -> None:
        """
        Record a trade result for loss streak detection.

        Args:
            strategy: Strategy identifier
            pnl: Realized P&L from the trade
        """
        if strategy not in self._loss_streak:
            self._loss_streak[strategy] = 0

        if pnl < 0:
            self._loss_streak[strategy] += 1
        else:
            self._loss_streak[strategy] = 0

        count = self._loss_streak[strategy]

        # Update Prometheus metric
        self.metrics.set_loss_streak_count(strategy, count)

        # Detect if anomalous
        if count >= self.LOSS_STREAK_THRESHOLD:
            await self._create_loss_streak_anomaly(strategy, count)

    async def _calculate_volatility_zscore(self, symbol: str) -> float:
        """Calculate price volatility Z-score."""
        prices = list(self._price_history.get(symbol, []))

        if len(prices) < 10:  # Need minimum data
            return 0.0

        # Calculate returns
        returns = []
        for i in range(1, len(prices)):
            ret = (prices[i] - prices[i - 1]) / prices[i - 1] if prices[i - 1] != 0 else 0
            returns.append(ret)

        if not returns:
            return 0.0

        # Calculate mean and std
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
        std_return = math.sqrt(variance) if variance > 0 else 0.0

        if std_return == 0:
            return 0.0

        # Latest return Z-score
        latest_return = returns[-1]
        zscore = (latest_return - mean_return) / std_return

        return zscore

    async def _calculate_signal_frequency_ratio(self, strategy: str) -> float:
        """Calculate signal frequency relative to historical average."""
        import time

        timestamps = list(self._signal_history.get(strategy, []))

        if len(timestamps) < 10:  # Need minimum data
            return 1.0

        now = time.time()

        # Count signals in last 5 minutes
        recent_count = sum(1 for t in timestamps if now - t < 300)

        # Average count over full window (normalized to 5 minutes)
        window_duration = timestamps[-1] - timestamps[0] if len(timestamps) > 1 else 300
        if window_duration == 0:
            return 1.0

        avg_count = (len(timestamps) / window_duration) * 300

        if avg_count == 0:
            return 1.0

        return recent_count / avg_count

    async def _create_volatility_anomaly(self, symbol: str, zscore: float) -> None:
        """Create and alert on price volatility anomaly."""
        anomaly_key = f"price_volatility:{symbol}"

        # Skip if already active
        if anomaly_key in self._active_anomalies:
            return

        severity = "critical" if abs(zscore) > 3.0 else "high"

        anomaly = Anomaly(
            anomaly_type=AnomalyType.PRICE_VOLATILITY,
            severity=severity,
            detected_value=zscore,
            threshold=self.VOLATILITY_ZSCORE_THRESHOLD,
            description=f"Price volatility Z-score of {zscore:.2f} exceeds threshold",
            recommended_action="Review market conditions and consider reducing position size",
        )

        self._active_anomalies[anomaly_key] = anomaly

        # Update metrics
        self.metrics.record_anomaly_detected(
            anomaly_type=AnomalyType.PRICE_VOLATILITY.value,
            severity=severity,
        )

        # Send alert
        await self._send_anomaly_alert(anomaly, symbol=symbol)

        logger.warning(f"Price volatility anomaly detected for {symbol}: Z-score={zscore:.2f}")

    async def _create_signal_frequency_anomaly(self, strategy: str, ratio: float) -> None:
        """Create and alert on signal frequency anomaly."""
        anomaly_key = f"signal_frequency:{strategy}"

        # Skip if already active
        if anomaly_key in self._active_anomalies:
            return

        severity = "high" if ratio > 5.0 else "medium"

        anomaly = Anomaly(
            anomaly_type=AnomalyType.SIGNAL_FREQUENCY,
            severity=severity,
            detected_value=ratio,
            threshold=self.SIGNAL_FREQUENCY_THRESHOLD,
            description=f"Signal frequency is {ratio:.1f}x normal rate",
            recommended_action="Check for data feed issues or strategy logic errors",
        )

        self._active_anomalies[anomaly_key] = anomaly

        # Update metrics
        self.metrics.record_anomaly_detected(
            anomaly_type=AnomalyType.SIGNAL_FREQUENCY.value,
            severity=severity,
        )

        # Send alert
        await self._send_anomaly_alert(anomaly, strategy=strategy)

        logger.warning(f"Signal frequency anomaly for {strategy}: {ratio:.1f}x normal")

    async def _create_loss_streak_anomaly(self, strategy: str, count: int) -> None:
        """Create and alert on loss streak anomaly."""
        anomaly_key = f"loss_streak:{strategy}"

        # Skip if already active
        if anomaly_key in self._active_anomalies:
            return

        severity = "critical" if count >= 8 else "high"

        anomaly = Anomaly(
            anomaly_type=AnomalyType.LOSS_STREAK,
            severity=severity,
            detected_value=float(count),
            threshold=float(self.LOSS_STREAK_THRESHOLD),
            description=f"{count} consecutive losing trades",
            recommended_action="Consider pausing trading and reviewing strategy parameters",
        )

        self._active_anomalies[anomaly_key] = anomaly

        # Update metrics
        self.metrics.record_anomaly_detected(
            anomaly_type=AnomalyType.LOSS_STREAK.value,
            severity=severity,
        )

        # Send alert
        await self._send_anomaly_alert(anomaly, strategy=strategy)

        logger.warning(f"Loss streak anomaly for {strategy}: {count} consecutive losses")

    async def _send_anomaly_alert(
        self,
        anomaly: Anomaly,
        symbol: Optional[str] = None,
        strategy: Optional[str] = None,
    ) -> None:
        """Send alert for detected anomaly."""
        # Severity to priority mapping
        priority_map = {
            "low": "normal",
            "medium": "high",
            "high": "high",
            "critical": "urgent",
        }

        # Build context line
        context = ""
        if symbol:
            context = f"<b>Symbol:</b> {symbol}\n"
        if strategy:
            context = f"<b>Strategy:</b> {strategy}\n"

        message = f"""{context}<b>Type:</b> {anomaly.anomaly_type.value}
<b>Severity:</b> {anomaly.severity}
<b>Value:</b> {anomaly.detected_value:.2f}
<b>Threshold:</b> {anomaly.threshold:.2f}

{anomaly.description}

<b>Recommended Action:</b>
{anomaly.recommended_action}

<i>{anomaly.timestamp.strftime('%H:%M:%S')}</i>"""

        await self.alert_service.send_immediate(
            alert_type=AlertType.ANOMALY_DETECTED,
            title=f"Anomaly: {anomaly.anomaly_type.value.replace('_', ' ').title()}",
            message=message,
            priority=priority_map.get(anomaly.severity, "normal"),
            source_id=anomaly.id,
            source_type="Anomaly",
        )

    async def _detect_all_anomalies(self) -> None:
        """Run all anomaly detectors."""
        # Clear resolved anomalies
        await self._clear_resolved_anomalies()

    async def _clear_resolved_anomalies(self) -> None:
        """Clear anomalies that have resolved."""
        to_remove = []

        for key, anomaly in self._active_anomalies.items():
            parts = key.split(":")
            anomaly_type = parts[0]
            identifier = parts[1] if len(parts) > 1 else ""

            resolved = False

            if anomaly_type == "price_volatility":
                zscore = self._volatility_zscore.get(identifier, 0.0)
                if abs(zscore) < self.VOLATILITY_ZSCORE_THRESHOLD * 0.8:
                    resolved = True

            elif anomaly_type == "signal_frequency":
                ratio = await self._calculate_signal_frequency_ratio(identifier)
                if ratio < self.SIGNAL_FREQUENCY_THRESHOLD * 0.8:
                    resolved = True

            elif anomaly_type == "loss_streak":
                count = self._loss_streak.get(identifier, 0)
                if count < self.LOSS_STREAK_THRESHOLD:
                    resolved = True

            if resolved:
                to_remove.append(key)
                logger.info(f"Anomaly resolved: {key}")

        for key in to_remove:
            del self._active_anomalies[key]

    async def _restore_state(self) -> None:
        """Restore state from Redis."""
        if not self.redis:
            return

        try:
            # Restore loss streaks
            keys = await self.redis.keys(f"{REDIS_KEY_ANOMALY_PREFIX}loss_streak:*")
            for key in keys:
                strategy = key.split(":")[-1]
                value = await self.redis.get(key)
                if value:
                    self._loss_streak[strategy] = int(value)

            logger.debug("Restored anomaly detector state from Redis")

        except Exception as e:
            logger.warning(f"Failed to restore anomaly detector state: {e}")

    async def _persist_state(self) -> None:
        """Persist state to Redis."""
        if not self.redis:
            return

        try:
            # Persist loss streaks
            for strategy, count in self._loss_streak.items():
                key = f"{REDIS_KEY_ANOMALY_PREFIX}loss_streak:{strategy}"
                await self.redis.set(key, str(count), ex=86400)  # 24h TTL

            logger.debug("Persisted anomaly detector state to Redis")

        except Exception as e:
            logger.warning(f"Failed to persist anomaly detector state: {e}")

    def get_active_anomalies(self) -> List[Anomaly]:
        """Get list of currently active anomalies."""
        return list(self._active_anomalies.values())

    def acknowledge_anomaly(self, anomaly_id: str) -> bool:
        """
        Acknowledge an anomaly.

        Args:
            anomaly_id: Anomaly ID to acknowledge

        Returns:
            True if found and acknowledged
        """
        for anomaly in self._active_anomalies.values():
            if anomaly.id == anomaly_id:
                anomaly.acknowledge()
                return True
        return False
