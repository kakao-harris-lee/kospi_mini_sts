"""
E-mini S&P 500 (ES) Futures 1-minute OHLCV Data Collector

Uses Polygon.io API to fetch historical ES futures data for backtesting.

Usage:
    python es_collector.py --backfill              # 5년 백필 (기본값)
    python es_collector.py --backfill --years 3    # 3년 백필
    python es_collector.py --from 2020-01-01 --to 2024-12-31  # 특정 기간
    python es_collector.py --status                # 수집 상태 확인
"""
import argparse
from datetime import date, timedelta
from typing import List

from app import db, config
from app.es_futures import (
    get_codes_for_years,
    get_quarterly_codes_in_range,
    parse_code,
    get_expiry_date,
)
from app.fetch_polygon import fetch_and_parse


def backfill(years: int = 5, start_date: date = None, end_date: date = None):
    """
    Run backfill for ES futures data.

    Args:
        years: Number of years to backfill (default: 5)
        start_date: Optional start date (overrides years)
        end_date: Optional end date (default: today)
    """
    print("=" * 60)
    print("ES Futures Data Collector - Backfill Mode")
    print("=" * 60)

    # Determine date range
    if end_date is None:
        end_date = date.today()

    if start_date is None:
        start_date = date(end_date.year - years, end_date.month, end_date.day)

    print(f"Date range: {start_date} ~ {end_date}")

    # Get all quarterly contracts in range
    codes = get_quarterly_codes_in_range(start_date, end_date)
    print(f"Quarterly contracts to collect: {len(codes)}")
    for code in codes:
        year, month = parse_code(code)
        expiry = get_expiry_date(year, month)
        print(f"  - {code} (expiry: {expiry})")

    # Setup database
    db.ensure_database()
    db_client = db.get_client(database=config.CLICKHOUSE_DATABASE)
    db.ensure_es_table(db_client)

    # Check already collected codes
    collected_codes = db.get_es_collected_codes(db_client)
    codes_to_collect = [c for c in codes if c not in collected_codes]

    if not codes_to_collect:
        print("\nAll contracts already collected!")
        return

    print(f"\nNew contracts to collect: {len(codes_to_collect)}")
    print(f"Already collected: {len(collected_codes)}")

    # Collect data for each contract
    total_rows = 0
    errors = []

    for idx, code in enumerate(codes_to_collect, start=1):
        year, month = parse_code(code)
        expiry = get_expiry_date(year, month)

        # Determine collection period for this contract
        # Collect from 3 months before expiry to expiry
        contract_start = date(expiry.year, expiry.month, 1) - timedelta(days=90)
        contract_end = expiry

        # Clamp to our date range
        collect_start = max(contract_start, start_date)
        collect_end = min(contract_end, end_date)

        print(f"\n[{idx}/{len(codes_to_collect)}] {code}")
        print(f"  Period: {collect_start} ~ {collect_end}")

        # Fetch data
        code, rows, error = fetch_and_parse(code, collect_start, collect_end)

        if error:
            print(f"  ERROR: {error}")
            errors.append((code, error))
            continue

        if rows:
            db.insert_es_batch(db_client, rows)
            total_rows += len(rows)
            print(f"  Inserted: {len(rows)} rows")
        else:
            print(f"  No data returned")

    # Summary
    print("\n" + "=" * 60)
    print("Backfill Complete!")
    print("=" * 60)
    print(f"Total rows inserted: {total_rows:,}")
    print(f"Contracts collected: {len(codes_to_collect) - len(errors)}")
    if errors:
        print(f"Errors: {len(errors)}")
        for code, error in errors:
            print(f"  - {code}: {error}")


def show_status():
    """Show collection status."""
    print("=" * 60)
    print("ES Futures Data Collection Status")
    print("=" * 60)

    try:
        db_client = db.get_client(database=config.CLICKHOUSE_DATABASE)
    except Exception as e:
        print(f"Cannot connect to database: {e}")
        return

    # Get collected codes
    collected_codes = db.get_es_collected_codes(db_client)
    row_count = db.get_es_row_count(db_client)
    date_range = db.get_es_date_range(db_client)

    print(f"\nCollected contracts: {len(collected_codes)}")
    if collected_codes:
        print("  " + ", ".join(sorted(collected_codes)))

    print(f"\nTotal rows: {row_count:,}")

    if date_range[0] and date_range[1]:
        print(f"Date range: {date_range[0]} ~ {date_range[1]}")

    # Show what would be collected for 5 years
    codes_5y = get_codes_for_years(5)
    missing = [c for c in codes_5y if c not in collected_codes]

    print(f"\n5-year coverage:")
    print(f"  Expected contracts: {len(codes_5y)}")
    print(f"  Collected: {len(collected_codes)}")
    print(f"  Missing: {len(missing)}")
    if missing:
        print("  Missing contracts: " + ", ".join(missing))


def main():
    parser = argparse.ArgumentParser(
        description="E-mini S&P 500 Futures Data Collector (Polygon.io)"
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="Run backfill (default: 5 years)"
    )
    parser.add_argument(
        "--years", type=int, default=5,
        help="Number of years to backfill (default: 5)"
    )
    parser.add_argument(
        "--from", dest="from_date", type=str,
        help="Start date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--to", dest="to_date", type=str,
        help="End date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show collection status"
    )

    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.backfill or args.from_date:
        start_date = None
        end_date = None

        if args.from_date:
            start_date = date.fromisoformat(args.from_date)
        if args.to_date:
            end_date = date.fromisoformat(args.to_date)

        backfill(years=args.years, start_date=start_date, end_date=end_date)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
