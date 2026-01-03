"""
State Snapshot Management for Crash Recovery

Provides periodic state snapshots to Redis for fast recovery
after service restarts or crashes.

Usage:
    manager = StateSnapshotManager(redis_client, "feature_processor")

    # Save state periodically (auto-throttled)
    manager.save_snapshot({
        "ofi_history": list(ofi_deque),
        "zscore_mean": mean,
        "zscore_std": std,
    })

    # Restore on startup
    state = manager.load_snapshot()
    if state:
        ofi_deque = deque(state["ofi_history"], maxlen=100)
"""
import json
import pickle
import gzip
import logging
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, Optional, Callable
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class SnapshotMetadata:
    """Snapshot metadata"""
    service: str
    timestamp: float
    version: str = "1.0"
    size_bytes: int = 0
    compressed: bool = True

    def age_seconds(self) -> float:
        """Get age of snapshot in seconds"""
        return time.time() - self.timestamp

    def age_human(self) -> str:
        """Get human-readable age"""
        age = self.age_seconds()
        if age < 60:
            return f"{age:.0f}s"
        elif age < 3600:
            return f"{age/60:.1f}m"
        else:
            return f"{age/3600:.1f}h"


@dataclass
class SnapshotConfig:
    """Snapshot configuration"""
    interval_sec: float = 60.0      # Minimum interval between saves
    ttl_sec: int = 86400            # 24 hours TTL
    compress: bool = True           # Compress with gzip
    max_size_mb: float = 10.0       # Max snapshot size
    key_prefix: str = "state_snapshot:"


class StateSnapshotManager:
    """
    Manages state snapshots in Redis for crash recovery

    Features:
    - Automatic save throttling (respects interval)
    - Gzip compression for large states
    - TTL-based expiration
    - Versioned snapshots
    - Size monitoring
    """

    def __init__(
        self,
        redis_client,
        service_name: str,
        config: SnapshotConfig = None
    ):
        self.client = redis_client
        self.service = service_name
        self.config = config or SnapshotConfig()

        self._last_snapshot_time = 0
        self._last_snapshot_size = 0
        self._snapshot_count = 0

    def _key(self, suffix: str = "") -> str:
        """Generate Redis key for snapshot"""
        return f"{self.config.key_prefix}{self.service}{suffix}"

    def save_snapshot(
        self,
        state: Dict[str, Any],
        force: bool = False
    ) -> bool:
        """
        Save state snapshot if interval has passed

        Args:
            state: State dictionary to save
            force: Save even if interval hasn't passed

        Returns:
            True if snapshot was saved, False if skipped
        """
        now = time.time()

        # Check interval (unless forced)
        if not force:
            elapsed = now - self._last_snapshot_time
            if elapsed < self.config.interval_sec:
                return False

        try:
            # Create metadata
            metadata = SnapshotMetadata(
                service=self.service,
                timestamp=now,
                compressed=self.config.compress
            )

            # Serialize state
            payload = {
                "metadata": asdict(metadata),
                "state": state
            }

            # Use pickle for complex objects (deques, numpy arrays, etc.)
            serialized = pickle.dumps(payload)

            # Compress if enabled
            if self.config.compress:
                serialized = gzip.compress(serialized)

            # Check size limit
            size_mb = len(serialized) / (1024 * 1024)
            if size_mb > self.config.max_size_mb:
                logger.error(
                    f"Snapshot too large ({size_mb:.2f}MB > {self.config.max_size_mb}MB), "
                    f"skipping save for {self.service}"
                )
                return False

            # Save to Redis with TTL
            self.client.setex(
                self._key(),
                self.config.ttl_sec,
                serialized
            )

            # Update stats
            self._last_snapshot_time = now
            self._last_snapshot_size = len(serialized)
            self._snapshot_count += 1

            logger.debug(
                f"Snapshot saved for {self.service}: "
                f"{len(serialized)} bytes, count={self._snapshot_count}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to save snapshot for {self.service}: {e}")
            return False

    def load_snapshot(
        self,
        max_age_sec: float = None
    ) -> Optional[Dict[str, Any]]:
        """
        Load the latest snapshot if available

        Args:
            max_age_sec: Maximum age of snapshot to accept (None = any age)

        Returns:
            State dictionary or None if not found/too old
        """
        try:
            serialized = self.client.get(self._key())
            if not serialized:
                logger.info(f"No snapshot found for {self.service}")
                return None

            # Decompress if needed
            try:
                serialized = gzip.decompress(serialized)
            except gzip.BadGzipFile:
                pass  # Not compressed

            # Deserialize
            payload = pickle.loads(serialized)
            metadata_dict = payload["metadata"]
            state = payload["state"]

            metadata = SnapshotMetadata(**metadata_dict)
            age = metadata.age_seconds()

            # Check age limit
            if max_age_sec is not None and age > max_age_sec:
                logger.warning(
                    f"Snapshot for {self.service} too old "
                    f"({metadata.age_human()} > {max_age_sec}s), ignoring"
                )
                return None

            logger.info(
                f"Loaded snapshot for {self.service}, "
                f"age={metadata.age_human()}, size={metadata.size_bytes}B"
            )

            # Warn if snapshot is stale
            if age > 300:  # 5 minutes
                logger.warning(
                    f"Snapshot is {metadata.age_human()} old, "
                    f"data may be stale"
                )

            return state

        except Exception as e:
            logger.error(f"Failed to load snapshot for {self.service}: {e}")
            return None

    def get_metadata(self) -> Optional[SnapshotMetadata]:
        """Get metadata of current snapshot without loading full state"""
        try:
            serialized = self.client.get(self._key())
            if not serialized:
                return None

            # Decompress if needed
            try:
                serialized = gzip.decompress(serialized)
            except gzip.BadGzipFile:
                pass

            payload = pickle.loads(serialized)
            return SnapshotMetadata(**payload["metadata"])

        except Exception as e:
            logger.error(f"Failed to get snapshot metadata: {e}")
            return None

    def clear_snapshot(self):
        """Delete the snapshot"""
        try:
            self.client.delete(self._key())
            logger.info(f"Snapshot cleared for {self.service}")
        except Exception as e:
            logger.error(f"Failed to clear snapshot: {e}")

    def exists(self) -> bool:
        """Check if a snapshot exists"""
        return self.client.exists(self._key()) > 0

    def get_stats(self) -> Dict[str, Any]:
        """Get snapshot statistics"""
        metadata = self.get_metadata()
        return {
            "service": self.service,
            "exists": metadata is not None,
            "age": metadata.age_human() if metadata else None,
            "last_save_time": self._last_snapshot_time,
            "last_size_bytes": self._last_snapshot_size,
            "total_saves": self._snapshot_count,
        }


class PeriodicSnapshotSaver:
    """
    Helper class for automatic periodic snapshot saving

    Usage:
        saver = PeriodicSnapshotSaver(manager, get_state_func)
        saver.start()
        ...
        saver.stop()
    """

    def __init__(
        self,
        manager: StateSnapshotManager,
        get_state: Callable[[], Dict[str, Any]],
        interval_sec: float = 60.0
    ):
        self.manager = manager
        self.get_state = get_state
        self.interval = interval_sec
        self._running = False
        self._thread = None

    def start(self):
        """Start periodic saving in background thread"""
        import threading

        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info(f"Started periodic snapshot saver for {self.manager.service}")

    def stop(self):
        """Stop periodic saving"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info(f"Stopped periodic snapshot saver for {self.manager.service}")

    def _run(self):
        """Background save loop"""
        while self._running:
            try:
                state = self.get_state()
                self.manager.save_snapshot(state, force=True)
            except Exception as e:
                logger.error(f"Periodic snapshot save failed: {e}")

            # Sleep in small increments to allow quick shutdown
            for _ in range(int(self.interval)):
                if not self._running:
                    break
                time.sleep(1)


# Convenience function for processors
def create_processor_snapshot_manager(
    service_name: str,
    redis_client=None
) -> StateSnapshotManager:
    """Create a snapshot manager with processor defaults"""
    if redis_client is None:
        from src.common.redis_client import RedisClient
        redis_client = RedisClient.get_client()

    return StateSnapshotManager(
        redis_client,
        service_name,
        SnapshotConfig(
            interval_sec=60.0,
            ttl_sec=3600 * 6,  # 6 hours
            compress=True,
            max_size_mb=5.0
        )
    )
