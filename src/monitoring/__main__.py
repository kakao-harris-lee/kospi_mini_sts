"""
CLI entry point for the monitoring service.

Usage:
    python -m src.monitoring --help
    python -m src.monitoring run
    python -m src.monitoring test-alert
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

import typer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = typer.Typer(
    name="monitoring",
    help="KOSPI Mini Trading System - Monitoring & Alerting Service",
)


@app.command()
def run(
    redis_url: str = typer.Option(
        "redis://localhost:6379",
        help="Redis connection URL",
    ),
    clickhouse_host: str = typer.Option(
        "localhost",
        help="ClickHouse host",
    ),
    clickhouse_port: int = typer.Option(
        8123,
        help="ClickHouse HTTP port",
    ),
    health_interval: float = typer.Option(
        15.0,
        help="Health check interval in seconds",
    ),
    anomaly_interval: float = typer.Option(
        10.0,
        help="Anomaly detection interval in seconds",
    ),
):
    """Run the monitoring service."""
    asyncio.run(_run_service(
        redis_url=redis_url,
        clickhouse_host=clickhouse_host,
        clickhouse_port=clickhouse_port,
        health_interval=health_interval,
        anomaly_interval=anomaly_interval,
    ))


async def _run_service(
    redis_url: str,
    clickhouse_host: str,
    clickhouse_port: int,
    health_interval: float,
    anomaly_interval: float,
):
    """Run the monitoring service async main loop."""
    import redis.asyncio as aioredis

    from src.monitoring import (
        AlertService,
        HealthChecker,
        AnomalyDetector,
        PerformanceTracker,
    )

    logger.info("Starting monitoring service...")

    # Initialize Redis
    redis_client = None
    try:
        redis_client = aioredis.from_url(redis_url, decode_responses=True)
        await redis_client.ping()
        logger.info(f"Connected to Redis: {redis_url}")
    except Exception as e:
        logger.warning(f"Failed to connect to Redis: {e}")

    # Initialize ClickHouse
    clickhouse_client = None
    try:
        import clickhouse_connect
        clickhouse_client = clickhouse_connect.get_client(
            host=clickhouse_host,
            port=clickhouse_port,
        )
        clickhouse_client.command("SELECT 1")
        logger.info(f"Connected to ClickHouse: {clickhouse_host}:{clickhouse_port}")
    except Exception as e:
        logger.warning(f"Failed to connect to ClickHouse: {e}")

    # Initialize services
    alert_service = AlertService(
        redis_client=redis_client,
        clickhouse_client=clickhouse_client,
    )

    health_checker = HealthChecker(
        alert_service=alert_service,
        redis_client=redis_client,
        clickhouse_client=clickhouse_client,
    )
    health_checker.CHECK_INTERVAL = health_interval

    anomaly_detector = AnomalyDetector(
        alert_service=alert_service,
        redis_client=redis_client,
    )
    anomaly_detector.DETECTION_INTERVAL = anomaly_interval

    performance_tracker = PerformanceTracker(
        clickhouse_client=clickhouse_client,
        redis_client=redis_client,
    )

    # Start services
    await alert_service.start()
    await health_checker.start()
    await anomaly_detector.start()
    await performance_tracker.start()

    # Send startup notification
    from src.monitoring.models import AlertType
    await alert_service.send_immediate(
        alert_type=AlertType.SYSTEM_START,
        title="Monitoring Service Started",
        message=f"""Monitoring service is now running.

<b>Services:</b>
- Alert Service: Active
- Health Checker: {health_interval}s interval
- Anomaly Detector: {anomaly_interval}s interval
- Performance Tracker: 60s interval

<i>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>""",
        priority="normal",
    )

    logger.info("Monitoring service started. Press Ctrl+C to stop.")

    try:
        # Run forever until interrupted
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        # Send shutdown notification
        await alert_service.send_immediate(
            alert_type=AlertType.SYSTEM_STOP,
            title="Monitoring Service Stopped",
            message=f"Monitoring service shutting down.\n\n<i>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>",
            priority="normal",
        )

        # Stop services
        await performance_tracker.stop()
        await anomaly_detector.stop()
        await health_checker.stop()
        await alert_service.stop()

        # Close connections
        if redis_client:
            await redis_client.close()

        logger.info("Monitoring service stopped.")


@app.command()
def test_alert(
    message: str = typer.Argument(
        "Test alert from monitoring service",
        help="Alert message to send",
    ),
):
    """Send a test alert via Telegram."""
    from src.common.telegram import TelegramNotifier

    notifier = TelegramNotifier(check_trading_day=False)
    success = notifier.send(f"<b>Test Alert</b>\n\n{message}")

    if success:
        typer.echo("Test alert sent successfully!")
    else:
        typer.echo("Failed to send test alert.", err=True)
        raise typer.Exit(1)


@app.command()
def status():
    """Show monitoring service status."""
    asyncio.run(_show_status())


async def _show_status():
    """Show monitoring service status async."""
    import redis.asyncio as aioredis

    try:
        redis_client = aioredis.from_url("redis://localhost:6379", decode_responses=True)

        # Check pending alerts
        pending = await redis_client.llen("alerts:pending")
        failed = await redis_client.llen("alerts:failed")

        # Check daily P&L
        daily_pnl = await redis_client.get("monitoring:trade:daily_pnl") or "0"
        loss_streak = await redis_client.get("monitoring:trade:loss_streak") or "0"
        trade_count = await redis_client.get("monitoring:trade:count_today") or "0"

        await redis_client.close()

        typer.echo("Monitoring Service Status")
        typer.echo("=" * 40)
        typer.echo(f"Pending Alerts:    {pending}")
        typer.echo(f"Failed Alerts:     {failed}")
        typer.echo(f"Daily P&L:         {float(daily_pnl):,.0f} KRW")
        typer.echo(f"Loss Streak:       {loss_streak}")
        typer.echo(f"Trades Today:      {trade_count}")

    except Exception as e:
        typer.echo(f"Error getting status: {e}", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
