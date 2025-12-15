"""ClickHouse database connection and operations."""
import clickhouse_connect
from datetime import date
from typing import List, Tuple, Set
from app import config


def get_client(database: str = None):
    """Get ClickHouse client connection."""
    return clickhouse_connect.get_client(
        host=config.CLICKHOUSE_HOST,
        port=config.CLICKHOUSE_PORT,
        database=database,
        username=config.CLICKHOUSE_USER or None,
        password=config.CLICKHOUSE_PASSWORD or None,
    )


def ensure_database():
    """Create database if not exists."""
    # Connect without specifying database
    client = get_client(database=None)
    client.command(f"CREATE DATABASE IF NOT EXISTS {config.CLICKHOUSE_DATABASE}")
    client.close()


def ensure_table(client=None):
    """Create table if not exists with ReplacingMergeTree for deduplication."""
    if client is None:
        client = get_client(database=config.CLICKHOUSE_DATABASE)

    client.command(f"""
        CREATE TABLE IF NOT EXISTS {config.CLICKHOUSE_DATABASE}.kospi_mini_1m (
            code String,
            datetime DateTime,
            open Float64,
            high Float64,
            low Float64,
            close Float64,
            volume UInt64
        ) ENGINE = ReplacingMergeTree()
        ORDER BY (code, datetime)
    """)


def insert_batch(client, rows: List[Tuple]):
    """
    Batch insert OHLCV data using raw SQL to avoid DESCRIBE TABLE compatibility issues.

    Args:
        client: ClickHouse client
        rows: List of tuples (code, datetime, open, high, low, close, volume)
    """
    if not rows:
        return

    # Build VALUES clause
    values = []
    for row in rows:
        code, dt, open_, high, low, close, volume = row
        dt_str = dt.strftime('%Y-%m-%d %H:%M:%S') if hasattr(dt, 'strftime') else str(dt)
        values.append(f"('{code}', '{dt_str}', {open_}, {high}, {low}, {close}, {volume})")

    sql = f"""
        INSERT INTO kospi_mini_1m (code, datetime, open, high, low, close, volume)
        VALUES {', '.join(values)}
    """
    client.command(sql)


def get_collected_dates(client=None) -> Set[Tuple[str, date]]:
    """
    Get already collected (code, date) combinations.

    Returns:
        Set of (code, date) tuples
    """
    if client is None:
        client = get_client()

    result = client.query("""
        SELECT DISTINCT code, toDate(datetime) as dt
        FROM kospi_mini_1m
    """)

    return {(row[0], row[1]) for row in result.result_rows}


def get_collected_pairs_in_range(client, start: date, end: date) -> Set[Tuple[str, date]]:
    """Get collected (code, date) pairs for a date range (inclusive)."""
    result = client.query(
        """
        SELECT DISTINCT code, toDate(datetime) as dt
        FROM kospi_mini_1m
        WHERE dt >= %(start)s AND dt <= %(end)s
        """,
        parameters={"start": start, "end": end},
    )
    return {(row[0], row[1]) for row in result.result_rows}


def get_collected_dates_for_code(client, code: str) -> Set[date]:
    """
    Get collected dates for a specific futures code.

    Args:
        client: ClickHouse client
        code: Futures code (e.g., '101M25')

    Returns:
        Set of dates that have been collected
    """
    result = client.query(f"""
        SELECT DISTINCT toDate(datetime) as dt
        FROM kospi_mini_1m
        WHERE code = '{code}'
    """)

    return {row[0] for row in result.result_rows}


# ============================================================
# ES (E-mini S&P 500) 선물 데이터용 함수들
# ============================================================

def ensure_es_table(client=None):
    """Create ES futures table if not exists."""
    if client is None:
        client = get_client(database=config.CLICKHOUSE_DATABASE)

    client.command(f"""
        CREATE TABLE IF NOT EXISTS {config.CLICKHOUSE_DATABASE}.es_mini_1m (
            code String,
            datetime DateTime,
            open Float64,
            high Float64,
            low Float64,
            close Float64,
            volume UInt64
        ) ENGINE = ReplacingMergeTree()
        ORDER BY (code, datetime)
    """)


def insert_es_batch(client, rows: List[Tuple]):
    """
    Batch insert ES futures OHLCV data using raw SQL.

    Args:
        client: ClickHouse client
        rows: List of tuples (code, datetime, open, high, low, close, volume)
    """
    if not rows:
        return

    # Build VALUES clause
    values = []
    for row in rows:
        code, dt, open_, high, low, close, volume = row
        dt_str = dt.strftime('%Y-%m-%d %H:%M:%S') if hasattr(dt, 'strftime') else str(dt)
        values.append(f"('{code}', '{dt_str}', {open_}, {high}, {low}, {close}, {volume})")

    sql = f"""
        INSERT INTO es_mini_1m (code, datetime, open, high, low, close, volume)
        VALUES {', '.join(values)}
    """
    client.command(sql)


def get_es_collected_codes(client=None) -> Set[str]:
    """
    Get already collected ES futures codes.

    Returns:
        Set of ES ticker codes (e.g., {'ESZ5', 'ESH6', ...})
    """
    if client is None:
        client = get_client(database=config.CLICKHOUSE_DATABASE)

    try:
        result = client.query("""
            SELECT DISTINCT code
            FROM es_mini_1m
        """)
        return {row[0] for row in result.result_rows}
    except Exception:
        # Table doesn't exist yet
        return set()


def get_es_row_count(client=None) -> int:
    """Get total row count in es_mini_1m table."""
    if client is None:
        client = get_client(database=config.CLICKHOUSE_DATABASE)

    try:
        result = client.query("SELECT count() FROM es_mini_1m")
        return result.result_rows[0][0]
    except Exception:
        return 0


def get_es_date_range(client=None) -> Tuple[date, date]:
    """Get min and max dates in es_mini_1m table."""
    if client is None:
        client = get_client(database=config.CLICKHOUSE_DATABASE)

    try:
        result = client.query("""
            SELECT min(toDate(datetime)), max(toDate(datetime))
            FROM es_mini_1m
        """)
        row = result.result_rows[0]
        return (row[0], row[1])
    except Exception:
        return (None, None)
