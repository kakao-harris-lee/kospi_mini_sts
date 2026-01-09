"""
Export KOSPI futures data from ClickHouse to CSV.

Queries the kospi database and exports 1-minute OHLCV data for training.
Supports both KOSPI 200 Futures (101S6000) and Mini Futures (A05601).
"""
import os
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# Add parent path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Load .env file from main project
try:
    from dotenv import load_dotenv
    # Try multiple locations
    for env_path in [
        Path(__file__).parent.parent.parent / ".env",  # main project .env
        Path(__file__).parent.parent / ".env",  # docker-training .env
        Path.cwd() / ".env",
    ]:
        if env_path.exists():
            load_dotenv(env_path)
            break
except ImportError:
    pass

try:
    import clickhouse_connect
except ImportError:
    print("ERROR: clickhouse-connect not installed.")
    print("Install with: pip install clickhouse-connect")
    sys.exit(1)


def get_clickhouse_client():
    """Create ClickHouse client from environment variables."""
    return clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        database=os.getenv("CLICKHOUSE_DATABASE", "kospi"),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
    )


# Valid instrument codes and their table mappings
VALID_CODES = {
    "101S6000": "kospi200f_1m",
    "A05601": "kospi_mini_1m",
    "A05602": "kospi_mini_1m",
}


def get_table_for_code(code: str) -> str:
    """
    Determine the ClickHouse table name based on instrument code.

    Args:
        code: KIS instrument code

    Returns:
        Table name in ClickHouse

    Raises:
        ValueError: If code is not in VALID_CODES
    """
    if code not in VALID_CODES:
        raise ValueError(
            f"Unknown instrument code: '{code}'. "
            f"Supported codes: {list(VALID_CODES.keys())}"
        )
    return VALID_CODES[code]


def export_data(
    code: str,
    output_path: str,
    days: int = None,
    start_date: str = None,
    end_date: str = None,
) -> pd.DataFrame:
    """
    Export data from ClickHouse to CSV.

    Args:
        code: KIS instrument code (e.g., '101S6000', 'A05601')
        output_path: Path to output CSV file
        days: Number of recent days to export (optional)
        start_date: Start date in YYYY-MM-DD format (optional)
        end_date: End date in YYYY-MM-DD format (optional)

    Returns:
        DataFrame with exported data
    """
    client = get_clickhouse_client()
    table = get_table_for_code(code)

    # Build parameterized query
    params = {"code": code}
    date_filter = ""
    filter_desc = ""

    if days:
        if not isinstance(days, int) or days <= 0:
            raise ValueError(f"days must be a positive integer, got: {days}")
        date_filter = f"AND datetime >= now() - INTERVAL {int(days)} DAY"
        filter_desc = f"last {days} days"
    elif start_date:
        # Validate date format
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"start_date must be YYYY-MM-DD format, got: {start_date}")
        params["start_date"] = start_date
        date_filter = "AND datetime >= {start_date:String}"
        filter_desc = f"from {start_date}"
        if end_date:
            try:
                datetime.strptime(end_date, "%Y-%m-%d")
            except ValueError:
                raise ValueError(f"end_date must be YYYY-MM-DD format, got: {end_date}")
            params["end_date"] = end_date + " 23:59:59"
            date_filter += " AND datetime <= {end_date:String}"
            filter_desc += f" to {end_date}"

    # Table name is validated via get_table_for_code(), safe to interpolate
    query = f"""
        SELECT
            toUnixTimestamp(datetime) as timestamp,
            datetime,
            open,
            high,
            low,
            close,
            volume
        FROM {table}
        WHERE code = {{code:String}}
        {date_filter}
        ORDER BY datetime ASC
    """

    print(f"Querying ClickHouse table: {table}")
    print(f"Code: {code}")
    if filter_desc:
        print(f"Filter: {filter_desc}")

    try:
        result = client.query(query, parameters=params)

        if not result.result_rows:
            print(f"WARNING: No data found for code {code}")
            return pd.DataFrame()

        df = pd.DataFrame(
            result.result_rows,
            columns=['timestamp', 'datetime', 'open', 'high', 'low', 'close', 'volume']
        )

        print(f"Exported {len(df)} rows")
        print(f"Date range: {df['datetime'].min()} to {df['datetime'].max()}")

        # Ensure output directory exists
        output_dir = Path(output_path).parent
        output_dir.mkdir(parents=True, exist_ok=True)

        # Save to CSV
        df.to_csv(output_path, index=False)
        print(f"Saved to: {output_path}")

        return df

    except Exception as e:
        print(f"ERROR: Failed to query ClickHouse: {e}")
        raise


def check_table_exists(code: str) -> bool:
    """Check if the required table exists in ClickHouse."""
    client = get_clickhouse_client()
    table = get_table_for_code(code)

    try:
        result = client.query(f"EXISTS TABLE {table}")
        exists = result.result_rows[0][0] == 1
        if not exists:
            print(f"WARNING: Table {table} does not exist in ClickHouse")
        return exists
    except Exception as e:
        print(f"ERROR: Failed to check table existence: {e}")
        return False


def get_data_stats(code: str) -> dict:
    """Get statistics about available data for a code."""
    client = get_clickhouse_client()
    table = get_table_for_code(code)

    # Table name validated via get_table_for_code(), safe to interpolate
    query = f"""
        SELECT
            count(*) as total_rows,
            min(datetime) as min_date,
            max(datetime) as max_date,
            countDistinct(toDate(datetime)) as trading_days
        FROM {table}
        WHERE code = {{code:String}}
    """

    try:
        result = client.query(query, parameters={"code": code})
        row = result.result_rows[0]
        return {
            'total_rows': row[0],
            'min_date': row[1],
            'max_date': row[2],
            'trading_days': row[3],
        }
    except Exception as e:
        print(f"ERROR: Failed to get stats: {e}")
        return {}


def main():
    parser = argparse.ArgumentParser(
        description="Export KOSPI futures data from ClickHouse to CSV"
    )
    parser.add_argument(
        "--code",
        type=str,
        default="101S6000",
        help="KIS instrument code (default: 101S6000 for KOSPI 200 Futures)"
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output CSV file path (required unless --stats)"
    )
    parser.add_argument(
        "--days",
        type=int,
        help="Export only last N days of data"
    )
    parser.add_argument(
        "--start-date",
        type=str,
        help="Start date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end-date",
        type=str,
        help="End date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show data statistics and exit"
    )

    args = parser.parse_args()

    # Show stats only
    if args.stats:
        stats = get_data_stats(args.code)
        if stats:
            print(f"\n=== Data Statistics for {args.code} ===")
            print(f"Total rows: {stats['total_rows']:,}")
            print(f"Date range: {stats['min_date']} to {stats['max_date']}")
            print(f"Trading days: {stats['trading_days']}")
        return

    # Validate output is provided for non-stats mode
    if not args.output:
        print("ERROR: --output is required (unless using --stats)")
        sys.exit(1)

    # Check table exists
    if not check_table_exists(args.code):
        print(f"ERROR: Cannot proceed - table for {args.code} not found")
        sys.exit(1)

    # Export data
    print(f"\n=== Exporting Data ===")
    df = export_data(
        code=args.code,
        output_path=args.output,
        days=args.days,
        start_date=args.start_date,
        end_date=args.end_date,
    )

    if df.empty:
        print("ERROR: No data exported")
        sys.exit(1)

    print("\n=== Export Complete ===")


if __name__ == "__main__":
    main()
