"""
KOSPI Mini Futures 1-minute OHLCV Data Collector

Usage:
    python collector.py --backfill    # Initial 1-year backfill
    python collector.py --continuous  # Continuous collection after market close
"""
import asyncio
import argparse
from datetime import date, timedelta
from typing import List, Tuple, Set

import httpx

from app import db, config
from app.fetch_minute import fetch_minute_async, parse_ohlcv
from app.calendar import get_trading_days_range, is_after_market_close
from app.futures import get_active_codes_for_date
from app import telegram


async def collect_batch(
    client: httpx.AsyncClient,
    db_client,
    tasks: List[Tuple[str, date]]
) -> int:
    """
    Collect a batch of (code, date) combinations.

    Args:
        client: httpx async client
        db_client: ClickHouse client
        tasks: List of (code, date) to collect

    Returns:
        Number of rows inserted
    """
    if not tasks:
        return 0

    # Create async tasks
    coros = [
        fetch_minute_async(client, code, dt.strftime("%Y%m%d"))
        for code, dt in tasks
    ]

    # Execute in parallel
    results = await asyncio.gather(*coros)

    # Parse and collect all rows
    all_rows = []
    for code, date_str, data in results:
        if "error" not in data:
            rows = parse_ohlcv(code, date_str, data)
            all_rows.extend(rows)

    # Batch insert
    if all_rows:
        db.insert_batch(db_client, all_rows)
        print(f"Inserted {len(all_rows)} rows from {len(tasks)} tasks")

    return len(all_rows)


def _date_range_days(days: int) -> Tuple[date, date]:
    end = date.today()
    start = end - timedelta(days=max(days, 1))
    return start, end


async def backfill(days: int = 365):
    """Run backfill for the past N days (default: 365)."""
    print("Starting backfill...")

    start, end = _date_range_days(days)
    trading_days = get_trading_days_range(start, end)
    print(f"Trading days: {len(trading_days)}")

    # Connect to DB (ensure database and table exist)
    db.ensure_database()
    db_client = db.get_client(database=config.CLICKHOUSE_DATABASE)
    db.ensure_table(db_client)

    # Preload collected pairs for this range to skip existing quickly.
    try:
        collected = db.get_collected_pairs_in_range(db_client, start, end)
    except Exception:
        collected = db.get_collected_dates(db_client)

    total_rows = 0
    total_tasks = 0

    # Most useful first: collect recent days first so ML can start sooner.
    trading_days_iter = list(reversed(trading_days))

    async with httpx.AsyncClient(timeout=30.0) as client:
        for idx, day in enumerate(trading_days_iter, start=1):
            codes = get_active_codes_for_date(day)
            day_tasks = [(code, day) for code in codes if (code, day) not in collected]
            if not day_tasks:
                continue

            total_tasks += len(day_tasks)
            print(f"{idx}/{len(trading_days_iter)} {day} tasks={len(day_tasks)}")
            rows = await collect_batch(client, db_client, day_tasks)
            total_rows += rows

    print(f"Backfill complete. tasks={total_tasks}, rows={total_rows}")


async def collect_today():
    """Collect today's data (run after market close)."""
    if not is_after_market_close():
        print("Market is still open. Please run after 15:45 KST.")
        return

    today = date.today()
    today_str = today.strftime("%Y-%m-%d")
    codes = get_active_codes_for_date(today)

    print(f"Collecting data for {today} - codes: {codes}")
    telegram.notify_collection_start(today_str, codes)

    db.ensure_database()
    db_client = db.get_client(database=config.CLICKHOUSE_DATABASE)
    db.ensure_table(db_client)

    tasks = [(code, today) for code in codes]

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            rows = await collect_batch(client, db_client, tasks)

        print("Today's collection complete.")
        telegram.notify_collection_complete(today_str, rows)

    except Exception as e:
        print(f"Collection failed: {e}")
        telegram.notify_collection_error(today_str, str(e))


async def continuous():
    """Run continuous collection (daily after market close)."""
    import time

    print("Starting continuous collection mode...")
    print("Will collect data daily after 15:45 KST")

    while True:
        if is_after_market_close():
            await collect_today()
            # Sleep until next day
            print("Sleeping until next trading day...")
            await asyncio.sleep(3600 * 12)  # Sleep 12 hours
        else:
            print("Waiting for market close...")
            await asyncio.sleep(60)  # Check every minute


def main():
    parser = argparse.ArgumentParser(description="KOSPI Mini Futures Data Collector")
    parser.add_argument("--backfill", action="store_true", help="Run initial backfill")
    parser.add_argument("--continuous", action="store_true", help="Run continuous collection")
    parser.add_argument("--today", action="store_true", help="Collect today's data only")
    parser.add_argument("--days", type=int, default=365, help="Backfill last N days (default: 365)")

    args = parser.parse_args()

    if args.backfill:
        asyncio.run(backfill(days=args.days))
    elif args.continuous:
        asyncio.run(continuous())
    elif args.today:
        asyncio.run(collect_today())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
