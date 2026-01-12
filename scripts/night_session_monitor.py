#!/usr/bin/env python
"""
Night Session Monitor

Monitors for night session start and notifies when data begins flowing.

Usage:
    python scripts/night_session_monitor.py

Features:
    - Waits for 18:00 (night session start)
    - Monitors Redis stream for new data
    - Sends Telegram notification when first data arrives
    - Reports candle counts after data starts flowing
"""
import os
import sys
import time
import redis
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.common.telegram import send_message
from src.common.clickhouse_client import ClickHouseClient
from config.settings import settings


def get_redis_client():
    """Get Redis client."""
    return redis.Redis(
        host=settings.redis.host,
        port=settings.redis.port,
        decode_responses=True
    )


def get_stream_length(r, stream_name):
    """Get Redis stream length."""
    try:
        return r.xlen(stream_name)
    except Exception:
        return 0


def get_latest_candle_time(table_name):
    """Get latest candle timestamp from ClickHouse."""
    try:
        client = ClickHouseClient.get_client()
        result = client.query(
            f"SELECT max(datetime) FROM {settings.clickhouse.database}.{table_name}"
        ).result_rows
        return result[0][0] if result and result[0][0] else None
    except Exception as e:
        print(f"Error getting latest candle: {e}")
        return None


def check_collector_running():
    """Check if collector process is running."""
    import subprocess
    try:
        result = subprocess.run(
            ["pgrep", "-f", "tick_collector"],
            capture_output=True,
            text=True
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def wait_for_session_start():
    """Wait until 18:00 (night session start)."""
    now = datetime.now()
    target = now.replace(hour=18, minute=0, second=0, microsecond=0)

    # If already past 18:00, we're good
    if now >= target:
        print(f"[{now.strftime('%H:%M:%S')}] Already past 18:00, proceeding...")
        return

    wait_seconds = (target - now).total_seconds()
    print(f"[{now.strftime('%H:%M:%S')}] Waiting for night session (18:00)...")
    print(f"  Time until session: {wait_seconds/60:.1f} minutes")

    # Send waiting notification
    send_message(
        f"<b>[Night Session Monitor]</b>\n"
        f"Waiting for night session start at 18:00\n"
        f"Time remaining: {wait_seconds/60:.1f} minutes",
        parse_mode="HTML"
    )

    time.sleep(wait_seconds)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Night session time reached!")


def monitor_data_flow():
    """Monitor for data flow and notify when data starts."""
    r = get_redis_client()

    # Get initial counts
    initial_raw = get_stream_length(r, "RAW_DATA_STREAM")
    initial_mini = get_latest_candle_time("kospi_mini_1m")
    initial_kospi = get_latest_candle_time("kospi200f_1m")

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Monitoring for data flow...")
    print(f"  Initial RAW_DATA_STREAM: {initial_raw}")
    print(f"  Initial Mini candle: {initial_mini}")
    print(f"  Initial KOSPI200 candle: {initial_kospi}")

    # Check collector status
    if not check_collector_running():
        msg = "<b>[Night Session Monitor] WARNING</b>\nCollector is NOT running!"
        print(f"\n{msg}")
        send_message(msg, parse_mode="HTML")
        return False

    print("  Collector: Running")

    # Monitor for new data (check every 10 seconds for up to 30 minutes)
    start_time = datetime.now()
    max_wait = timedelta(minutes=30)
    check_interval = 10  # seconds

    data_detected = False
    first_data_time = None

    while datetime.now() - start_time < max_wait:
        current_raw = get_stream_length(r, "RAW_DATA_STREAM")

        if current_raw > initial_raw:
            data_detected = True
            first_data_time = datetime.now()
            new_messages = current_raw - initial_raw
            print(f"\n[{first_data_time.strftime('%H:%M:%S')}] DATA DETECTED!")
            print(f"  New messages: {new_messages}")
            break

        # Show progress
        elapsed = (datetime.now() - start_time).total_seconds()
        print(f"  [{datetime.now().strftime('%H:%M:%S')}] Waiting... (elapsed: {elapsed:.0f}s, stream: {current_raw})")

        time.sleep(check_interval)

    if not data_detected:
        msg = (
            f"<b>[Night Session Monitor] WARNING</b>\n"
            f"No data detected after 30 minutes!\n"
            f"Session start: 18:00\n"
            f"Current time: {datetime.now().strftime('%H:%M')}\n"
            f"RAW_DATA_STREAM: {get_stream_length(r, 'RAW_DATA_STREAM')}"
        )
        print(f"\n{msg}")
        send_message(msg, parse_mode="HTML")
        return False

    # Wait a bit for candles to accumulate
    print("\nWaiting 2 minutes for candles to accumulate...")
    time.sleep(120)

    # Get final stats
    final_raw = get_stream_length(r, "RAW_DATA_STREAM")
    final_mini = get_latest_candle_time("kospi_mini_1m")
    final_kospi = get_latest_candle_time("kospi200f_1m")

    # Count today's candles
    client = ClickHouseClient.get_client()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    mini_today = client.query(
        f"SELECT count(*) FROM {settings.clickhouse.database}.kospi_mini_1m WHERE datetime >= %(dt)s",
        parameters={"dt": today}
    ).result_rows[0][0]

    kospi_today = client.query(
        f"SELECT count(*) FROM {settings.clickhouse.database}.kospi200f_1m WHERE datetime >= %(dt)s",
        parameters={"dt": today}
    ).result_rows[0][0]

    # Send success notification
    msg = (
        f"<b>[Night Session Started]</b>\n"
        f"Data flow confirmed at {first_data_time.strftime('%H:%M:%S')}\n\n"
        f"<b>RAW_DATA_STREAM:</b>\n"
        f"  Before: {initial_raw:,}\n"
        f"  After: {final_raw:,}\n"
        f"  New: {final_raw - initial_raw:,}\n\n"
        f"<b>Mini (A05602):</b>\n"
        f"  Today's candles: {mini_today}\n"
        f"  Latest: {final_mini}\n\n"
        f"<b>KOSPI200 (101S6000):</b>\n"
        f"  Today's candles: {kospi_today}\n"
        f"  Latest: {final_kospi}"
    )

    print(f"\n{msg.replace('<b>', '').replace('</b>', '')}")
    send_message(msg, parse_mode="HTML")

    return True


def main():
    print("=" * 50)
    print("Night Session Monitor")
    print("=" * 50)

    # Wait for 18:00
    wait_for_session_start()

    # Monitor for data flow
    success = monitor_data_flow()

    if success:
        print("\n[SUCCESS] Night session monitoring complete!")
    else:
        print("\n[WARNING] Night session monitoring completed with issues.")

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
