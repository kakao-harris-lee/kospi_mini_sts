#!/usr/bin/env python
"""
Collector Stats Report Script

Checks collector status and candle counts, sends hourly Telegram report.

Usage:
    python scripts/collector_stats_report.py          # One-time report
    python scripts/collector_stats_report.py --loop   # Hourly loop

Cron (hourly during trading hours):
    0 9-15 * * 1-5 cd /home/deploy/project/kospi_mini_sts && python scripts/collector_stats_report.py
    0 18-23 * * 1-5 cd /home/deploy/project/kospi_mini_sts && python scripts/collector_stats_report.py
    0 0-6 * * 2-6 cd /home/deploy/project/kospi_mini_sts && python scripts/collector_stats_report.py
"""
import os
import sys
import time
import argparse
import subprocess
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.common.telegram import send_message
from src.common.clickhouse_client import ClickHouseClient
from config.settings import settings


def get_collector_status() -> dict:
    """Check if collector process is running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "tick_collector"],
            capture_output=True,
            text=True
        )
        pids = result.stdout.strip().split('\n') if result.stdout.strip() else []
        return {
            "running": len(pids) > 0,
            "pids": pids
        }
    except Exception as e:
        return {"running": False, "error": str(e)}


def get_candle_stats() -> dict:
    """Get candle counts from ClickHouse."""
    try:
        client = ClickHouseClient.get_client()

        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        hour_ago = now - timedelta(hours=1)

        stats = {}

        for table, label in [("kospi_mini_1m", "Mini"), ("kospi200f_1m", "KOSPI200")]:
            # Total count
            total = client.query(f"SELECT count(*) FROM {settings.clickhouse.database}.{table}").result_rows[0][0]

            # Today's count
            today = client.query(
                f"SELECT count(*) FROM {settings.clickhouse.database}.{table} WHERE datetime >= %(dt)s",
                parameters={"dt": today_start}
            ).result_rows[0][0]

            # Last hour count
            last_hour = client.query(
                f"SELECT count(*) FROM {settings.clickhouse.database}.{table} WHERE datetime >= %(dt)s",
                parameters={"dt": hour_ago}
            ).result_rows[0][0]

            # Latest candle
            latest = client.query(
                f"SELECT datetime, close FROM {settings.clickhouse.database}.{table} ORDER BY datetime DESC LIMIT 1"
            ).result_rows

            stats[label] = {
                "total": total,
                "today": today,
                "last_hour": last_hour,
                "latest_time": str(latest[0][0]) if latest else "N/A",
                "latest_close": latest[0][1] if latest else 0
            }

        return stats
    except Exception as e:
        return {"error": str(e)}


def get_redis_stats() -> dict:
    """Get Redis stream stats."""
    try:
        import redis
        r = redis.Redis(host=settings.redis.host, port=settings.redis.port, decode_responses=True)

        raw_len = r.xlen("RAW_DATA_STREAM")
        feature_len = r.xlen("FEATURE_STREAM")

        return {
            "raw_stream": raw_len,
            "feature_stream": feature_len
        }
    except Exception as e:
        return {"error": str(e)}


def format_report() -> str:
    """Generate formatted stats report."""
    now = datetime.now()

    collector = get_collector_status()
    candles = get_candle_stats()
    redis_stats = get_redis_stats()

    # Determine market session
    hour = now.hour
    if 9 <= hour < 16:
        session = "Regular (09:00-15:45)"
    elif 18 <= hour or hour < 6:
        session = "Night (18:00-06:00)"
    else:
        session = "Closed"

    lines = [
        f"<b>[Collector Stats] {now.strftime('%Y-%m-%d %H:%M')}</b>",
        f"Session: {session}",
        "",
        f"<b>Collector:</b> {'Running' if collector.get('running') else 'STOPPED'}",
    ]

    if "error" not in candles:
        for label, stats in candles.items():
            lines.append(f"\n<b>{label}:</b>")
            lines.append(f"  Total: {stats['total']:,}")
            lines.append(f"  Today: {stats['today']:,}")
            lines.append(f"  Last Hour: {stats['last_hour']:,}")
            lines.append(f"  Latest: {stats['latest_time']}")
            if stats['latest_close']:
                lines.append(f"  Close: {stats['latest_close']:.2f}")
    else:
        lines.append(f"\nClickHouse Error: {candles['error']}")

    if "error" not in redis_stats:
        lines.append(f"\n<b>Redis Streams:</b>")
        lines.append(f"  RAW: {redis_stats['raw_stream']:,}")
        lines.append(f"  FEATURE: {redis_stats['feature_stream']:,}")

    return "\n".join(lines)


def send_report():
    """Generate and send stats report via Telegram."""
    report = format_report()
    print(report.replace("<b>", "").replace("</b>", ""))

    success = send_message(report, parse_mode="HTML")
    if success:
        print("\n[Telegram] Report sent successfully")
    else:
        print("\n[Telegram] Failed to send report")

    return success


def run_hourly_loop():
    """Run hourly report loop."""
    print("Starting hourly stats report loop...")

    while True:
        try:
            send_report()
        except Exception as e:
            print(f"Error sending report: {e}")

        # Wait until next hour
        now = datetime.now()
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        wait_seconds = (next_hour - now).total_seconds()
        print(f"\nNext report at {next_hour.strftime('%H:%M')} (waiting {wait_seconds:.0f}s)")
        time.sleep(wait_seconds)


def main():
    parser = argparse.ArgumentParser(description="Collector Stats Report")
    parser.add_argument("--loop", action="store_true", help="Run hourly loop")
    args = parser.parse_args()

    if args.loop:
        run_hourly_loop()
    else:
        send_report()


if __name__ == "__main__":
    main()
