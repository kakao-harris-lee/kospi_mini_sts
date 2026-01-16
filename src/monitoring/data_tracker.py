"""
Data Collection Tracker

Queries ClickHouse for data collection status and publishes metrics.
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


class DataCollectionTracker:
    """
    Tracks data collection status from ClickHouse and publishes Prometheus metrics.
    """

    TRACKING_INTERVAL = 60.0  # seconds

    def __init__(self, clickhouse_client):
        self.clickhouse_client = clickhouse_client
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the tracker"""
        if not self.clickhouse_client:
            logger.warning("ClickHouse client not available, DataCollectionTracker disabled")
            return

        self._running = True
        self._task = asyncio.create_task(self._tracking_loop())
        logger.info("DataCollectionTracker started")

    async def stop(self):
        """Stop the tracker"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("DataCollectionTracker stopped")

    async def _tracking_loop(self):
        """Main tracking loop"""
        while self._running:
            try:
                await self._update_metrics()
            except Exception as e:
                logger.error(f"Error updating data collection metrics: {e}")

            await asyncio.sleep(self.TRACKING_INTERVAL)

    async def _update_metrics(self):
        """Query ClickHouse and update metrics"""
        from src.common.metrics import get_metrics
        metrics = get_metrics()

        # Tables to track
        tables = [
            ("kospi_mini_1m", "kospi.kospi_mini_1m"),
            ("kospi200f_1m", "kospi.kospi200f_1m"),
        ]

        for table_name, full_table in tables:
            try:
                await self._update_table_metrics(metrics, table_name, full_table)
            except Exception as e:
                logger.error(f"Error tracking {table_name}: {e}")

    async def _update_table_metrics(self, metrics, table_name: str, full_table: str):
        """Update metrics for a specific table"""
        # Query for overall stats by code
        query = f"""
            SELECT
                code,
                COUNT(*) as total_rows,
                toUnixTimestamp(MIN(datetime)) as first_ts,
                toUnixTimestamp(MAX(datetime)) as last_ts
            FROM {full_table}
            GROUP BY code
        """

        result = self.clickhouse_client.query(query)

        for row in result.result_rows:
            code, total_rows, first_ts, last_ts = row
            metrics.set_data_collection_rows(table_name, code, total_rows)
            metrics.set_data_collection_first_date(table_name, code, float(first_ts))
            metrics.set_data_collection_last_date(table_name, code, float(last_ts))

        # Query for today's candles
        today_query = f"""
            SELECT
                code,
                COUNT(*) as today_candles
            FROM {full_table}
            WHERE toDate(datetime) = today()
            GROUP BY code
        """

        today_result = self.clickhouse_client.query(today_query)

        for row in today_result.result_rows:
            code, today_candles = row
            metrics.set_data_collection_daily_candles(table_name, code, today_candles)

        logger.debug(f"Updated metrics for {table_name}")


class ModelTrainingTracker:
    """
    Tracks model training status and publishes Prometheus metrics.
    """

    TRACKING_INTERVAL = 30.0  # seconds

    def __init__(self, model_path: str = "models/trading_lstm.pth"):
        self.model_path = model_path
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the tracker"""
        self._running = True
        self._task = asyncio.create_task(self._tracking_loop())
        logger.info("ModelTrainingTracker started")

    async def stop(self):
        """Stop the tracker"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ModelTrainingTracker stopped")

    async def _tracking_loop(self):
        """Main tracking loop"""
        while self._running:
            try:
                await self._update_metrics()
            except Exception as e:
                logger.error(f"Error updating model training metrics: {e}")

            await asyncio.sleep(self.TRACKING_INTERVAL)

    async def _update_metrics(self):
        """Check model status and update metrics"""
        import os
        import json
        from pathlib import Path
        from src.common.metrics import get_metrics

        metrics = get_metrics()

        # Check for model files
        model_types = ["cnn-lstm", "lstm"]

        for model_type in model_types:
            model_file = Path(self.model_path)
            meta_file = model_file.with_suffix(".json")

            if meta_file.exists():
                try:
                    with open(meta_file, "r") as f:
                        meta = json.load(f)

                    # Model is trained
                    if meta.get("model_type") == model_type or (model_type == "lstm" and "model_type" not in meta):
                        metrics.set_model_training_status(model_type, 2)  # trained

                        # Get model version and input_dim
                        version = meta.get("created_at", "unknown")[:10]
                        input_dim = meta.get("input_dim", 10)
                        metrics.set_model_version(model_type, version, input_dim)

                        # Get last trained timestamp
                        created_at = meta.get("created_at")
                        if created_at:
                            try:
                                dt = datetime.fromisoformat(created_at)
                                metrics.set_model_last_trained(model_type, dt.timestamp())
                            except ValueError:
                                pass

                        # Get training accuracy if available
                        train_samples = meta.get("train_samples", 0)
                        val_samples = meta.get("val_samples", 0)
                        if train_samples > 0:
                            # Estimate based on samples
                            logger.debug(f"Model {model_type}: train={train_samples}, val={val_samples}")

                except Exception as e:
                    logger.warning(f"Error reading model metadata: {e}")
                    metrics.set_model_training_status(model_type, 0)  # not trained
            else:
                metrics.set_model_training_status(model_type, 0)  # not trained

        logger.debug("Updated model training metrics")
