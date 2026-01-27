"""
Historical Data Backfill Module

과거 1분봉 OHLCV 데이터를 수집합니다.

사용법:
    # CLI
    sts backfill --days 180
    sts backfill --today

    # Python
    from src.collector.historical import backfill, collect_today
    await backfill(days=180)
    await collect_today()
"""
import os
import sys
import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import List, Tuple, Set, Dict, Any, Optional

import httpx
import clickhouse_connect

# 프로젝트 경로 설정
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from config.settings import settings
from .calendar import get_trading_days_range, is_after_market_close
from .futures import get_active_codes_for_date

logger = logging.getLogger(__name__)


# =============================================================================
# KIS API Token Management
# =============================================================================

class KISToken:
    """한국투자증권 API 토큰 관리"""

    _instance = None
    _token: Optional[str] = None
    _expires_at: float = 0
    _cache_path: Optional[str] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._cache_path = os.path.expanduser("~/.cache/kis_token.json")
            cls._instance._load_cache()
        return cls._instance

    def get(self) -> str:
        """현재 유효한 토큰 반환 (필요시 갱신)"""
        import time

        if self._token and time.time() < self._expires_at - 60:
            return self._token

        self._refresh()
        return self._token

    def _load_cache(self) -> None:
        """Load cached token if present and valid."""
        import json
        try:
            if not self._cache_path or not os.path.exists(self._cache_path):
                return
            with open(self._cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            token = data.get("access_token")
            expires_at = data.get("expires_at", 0)
            if token and expires_at:
                self._token = token
                self._expires_at = float(expires_at)
        except Exception:
            return

    def _save_cache(self) -> None:
        """Persist token to cache for reuse across runs."""
        import json
        if not self._cache_path or not self._token:
            return
        os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
        payload = {"access_token": self._token, "expires_at": self._expires_at}
        try:
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except Exception:
            return

    def _refresh(self):
        """토큰 갱신"""
        import requests
        import time

        url = f"{settings.kis.app_key and 'https://openapi.koreainvestment.com:9443' or 'https://openapivts.koreainvestment.com:29443'}/oauth2/tokenP"

        app_key = settings.kis.app_key or os.getenv("KIS_APP_KEY", "")
        app_secret = settings.kis.app_secret or os.getenv("KIS_APP_SECRET", "")

        if not app_key or not app_secret:
            raise ValueError("KIS_APP_KEY and KIS_APP_SECRET must be set")

        payload = {
            "grant_type": "client_credentials",
            "appkey": app_key,
            "appsecret": app_secret,
        }

        resp = requests.post(url, json=payload, timeout=30)
        data = resp.json()

        if "access_token" not in data:
            raise ValueError(f"Token refresh failed: {data}")

        self._token = data["access_token"]
        expires_in = int(data.get("expires_in", 86400))
        self._expires_at = time.time() + expires_in
        self._save_cache()

        logger.info(f"[KISToken] Token refreshed, expires in {expires_in}s")

    def is_token_valid(self) -> bool:
        """Check if current token is valid."""
        import time
        return bool(self._token and time.time() < self._expires_at - 60)

    def refresh_token(self) -> bool:
        """Refresh the token. Returns True on success."""
        try:
            self._refresh()
            return True
        except Exception as e:
            logger.warning(f"[KISToken] Token refresh failed: {e}")
            return False


_token = KISToken()


# =============================================================================
# Rate Limiter
# =============================================================================

class RateLimiter:
    """API Rate Limiter (requests per second)"""

    def __init__(self, rps: float = 20):
        self._min_interval = 1.0 / max(rps, 1e-6)
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait(self):
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            if now < self._next_allowed:
                await asyncio.sleep(self._next_allowed - now)
                now = loop.time()
            self._next_allowed = now + self._min_interval


_semaphore: Optional[asyncio.Semaphore] = None
_rate_limiter: Optional[RateLimiter] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(10)
    return _semaphore


def _get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(20)
    return _rate_limiter


# =============================================================================
# API Fetching
# =============================================================================

async def fetch_minute_async(
    client: httpx.AsyncClient,
    code: str,
    date_str: str,
    max_retries: int = 3
) -> Tuple[str, str, dict]:
    """
    Fetch minute data asynchronously with rate limiting and retry.

    Args:
        client: httpx async client
        code: Futures code (e.g., 'A05601')
        date_str: Date string in YYYYMMDD format
        max_retries: Maximum retry attempts

    Returns:
        Tuple of (code, date, response_data)
    """
    app_key = settings.kis.app_key or os.getenv("KIS_APP_KEY", "")
    app_secret = settings.kis.app_secret or os.getenv("KIS_APP_SECRET", "")
    base_url = "https://openapi.koreainvestment.com:9443"

    url = f"{base_url}/uapi/domestic-futureoption/v1/quotations/inquire-time-fuopchartprice"
    params = {
        "FID_COND_MRKT_DIV_CODE": "F",
        "FID_INPUT_ISCD": code,
        "FID_HOUR_CLS_CODE": "1",
        "FID_PW_DATA_INCU_YN": "Y",
        "FID_FAKE_TICK_INCU_YN": "N",
        "FID_INPUT_DATE_1": date_str,
        "FID_INPUT_HOUR_1": "",
    }

    last_error = None
    for attempt in range(max_retries):
        headers = {
            "Authorization": f"Bearer {_token.get()}",
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": "FHKIF03020200",
            "content-type": "application/json; charset=utf-8",
        }

        async with _get_semaphore():
            try:
                await _get_rate_limiter().wait()
                resp = await client.get(url, headers=headers, params=params)

                if not resp.text or resp.text.strip() == "":
                    return (code, date_str, {"error": f"empty response (status {resp.status_code})"})

                try:
                    data = resp.json()
                except Exception as json_err:
                    return (code, date_str, {"error": f"json parse: {json_err}"})

                if data.get("rt_cd") != "0":
                    msg = data.get("msg1", "Unknown error")
                    return (code, date_str, {"error": msg})

                return (code, date_str, data)

            except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError) as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2
                    logger.warning(f"Retry {attempt + 1}/{max_retries} for {code} {date_str}: {e}")
                    await asyncio.sleep(wait_time)
                continue

            except Exception as e:
                logger.error(f"Error fetching {code} {date_str}: {e}")
                return (code, date_str, {"error": str(e)})

    return (code, date_str, {"error": str(last_error)})


async def fetch_index_minute_async(
    client: httpx.AsyncClient,
    index_symbol: str,
    date_str: str,
    max_retries: int = 3,
) -> Tuple[str, str, dict]:
    """
    Fetch KOSPI200 index minute data asynchronously.
    """
    app_key = settings.kis.app_key or os.getenv("KIS_APP_KEY", "")
    app_secret = settings.kis.app_secret or os.getenv("KIS_APP_SECRET", "")
    base_url = "https://openapi.koreainvestment.com:9443"

    url = os.getenv(
        "KIS_INDEX_CHART_URL",
        f"{base_url}/uapi/domestic-stock/v1/quotations/inquire-time-indexchartprice",
    )
    tr_id = os.getenv("KIS_INDEX_CHART_TR_ID", "FHKUP03500100")

    params = {
        "FID_COND_MRKT_DIV_CODE": os.getenv("KIS_INDEX_COND_MRKT_DIV_CODE", "U"),
        "FID_INPUT_ISCD": index_symbol,
        "FID_INPUT_DATE_1": date_str,
        "FID_INPUT_HOUR_1": "",
        "FID_HOUR_CLS_CODE": os.getenv("KIS_INDEX_HOUR_CLS_CODE", "1"),
        "FID_PW_DATA_INCU_YN": "Y",
        "FID_FAKE_TICK_INCU_YN": os.getenv("KIS_INDEX_FAKE_TICK_INCU_YN", "N"),
    }

    last_error = None
    for attempt in range(max_retries):
        headers = {
            "Authorization": f"Bearer {_token.get()}",
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": tr_id,
            "content-type": "application/json; charset=utf-8",
        }

        async with _get_semaphore():
            try:
                await _get_rate_limiter().wait()
                resp = await client.get(url, headers=headers, params=params)

                if not resp.text or resp.text.strip() == "":
                    return (index_symbol, date_str, {"error": f"empty response (status {resp.status_code})"})

                try:
                    data = resp.json()
                except Exception as json_err:
                    return (index_symbol, date_str, {"error": f"json parse: {json_err}"})

                if data.get("rt_cd") != "0":
                    msg = data.get("msg1", "Unknown error")
                    return (index_symbol, date_str, {"error": msg})

                return (index_symbol, date_str, data)

            except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError) as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2
                    logger.warning(f"Retry {attempt + 1}/{max_retries} for {index_symbol} {date_str}: {e}")
                    await asyncio.sleep(wait_time)
                continue

            except Exception as e:
                logger.error(f"Error fetching {index_symbol} {date_str}: {e}")
                return (index_symbol, date_str, {"error": str(e)})

    return (index_symbol, date_str, {"error": str(last_error)})



def _first_present(item: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    """Get first present value from dict."""
    for k in keys:
        v = item.get(k)
        if v is not None and (not isinstance(v, str) or v.strip()):
            return v
    return default


def parse_ohlcv(code: str, date_str: str, data: dict) -> List[Tuple]:
    """
    Parse API response to OHLCV rows.

    Args:
        code: Futures code
        date_str: Date string (YYYYMMDD)
        data: API response

    Returns:
        List of tuples (code, datetime, open, high, low, close, volume)
    """
    rows = []
    output = data.get("output2", []) or data.get("output1", []) or data.get("output", [])

    if not output:
        return rows

    for item in output:
        try:
            time_str = _first_present(
                item,
                ["stck_cntg_hour", "futs_cntg_hour", "cntg_hour", "bsop_hour", "hour"],
                default="",
            )
            if not time_str:
                continue

            if len(time_str) == 4:
                time_str = f"{time_str}00"

            dt = datetime.strptime(f"{date_str}{time_str}", "%Y%m%d%H%M%S")

            o = _first_present(
                item,
                ["futs_oprc", "open", "stck_oprc", "bstp_nmix_oprc", "oprc"],
                0,
            )
            h = _first_present(
                item,
                ["futs_hgpr", "high", "stck_hgpr", "bstp_nmix_hgpr", "hgpr"],
                0,
            )
            l = _first_present(
                item,
                ["futs_lwpr", "low", "stck_lwpr", "bstp_nmix_lwpr", "lwpr"],
                0,
            )
            c = _first_present(
                item,
                ["futs_prpr", "close", "stck_prpr", "stck_clpr", "bstp_nmix_prpr", "prpr"],
                0,
            )
            v = _first_present(item, ["cntg_vol", "acml_vol", "volume"], 0)

            rows.append((
                code, dt,
                float(o or 0), float(h or 0), float(l or 0), float(c or 0),
                int(v or 0),
            ))
        except (ValueError, TypeError):
            continue

    return rows


# =============================================================================
# Database Operations
# =============================================================================

def get_db_client(database: str = None):
    """Get ClickHouse client."""
    return clickhouse_connect.get_client(
        host=settings.clickhouse.host,
        port=settings.clickhouse.port,
        database=database or settings.clickhouse.database,
        username=settings.clickhouse.user or None,
        password=settings.clickhouse.password or None,
    )


def ensure_database():
    """Create database if not exists."""
    client = get_db_client(database=None)
    client.command(f"CREATE DATABASE IF NOT EXISTS {settings.clickhouse.database}")
    client.close()


def ensure_table(client=None, table_name: str = "kospi_mini_1m"):
    """Create table if not exists."""
    if client is None:
        client = get_db_client()

    client.command(f"""
        CREATE TABLE IF NOT EXISTS {settings.clickhouse.database}.{table_name} (
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


def ensure_kospi200f_table(client=None):
    """Create KOSPI 200 Futures (full-size) table if not exists."""
    ensure_table(client, table_name="kospi200f_1m")


def ensure_kospi200_index_table(client=None):
    """Create KOSPI 200 Index table if not exists."""
    ensure_table(client, table_name="kospi200_index_1m")



def insert_batch(client, rows: List[Tuple], table_name: str = "kospi_mini_1m"):
    """Batch insert OHLCV data."""
    if not rows:
        return

    values = []
    for row in rows:
        code, dt, open_, high, low, close, volume = row
        dt_str = dt.strftime('%Y-%m-%d %H:%M:%S') if hasattr(dt, 'strftime') else str(dt)
        values.append(f"('{code}', '{dt_str}', {open_}, {high}, {low}, {close}, {volume})")

    sql = f"""
        INSERT INTO {table_name} (code, datetime, open, high, low, close, volume)
        VALUES {', '.join(values)}
    """
    client.command(sql)


def get_collected_pairs_in_range(
    client,
    start: date,
    end: date,
    table_name: str = "kospi_mini_1m",
) -> Set[Tuple[str, date]]:
    """Get collected (code, date) pairs for a date range."""
    result = client.query(
        f"""
        SELECT DISTINCT code, toDate(datetime) as dt
        FROM {table_name}
        WHERE dt >= %(start)s AND dt <= %(end)s
        """,
        parameters={"start": start, "end": end},
    )
    return {(row[0], row[1]) for row in result.result_rows}


# =============================================================================
# Backfill Functions
# =============================================================================

async def collect_batch(
    client: httpx.AsyncClient,
    db_client,
    tasks: List[Tuple[str, date]],
    table_name: str = "kospi_mini_1m"
) -> int:
    """
    Collect a batch of (code, date) combinations.

    Returns:
        Number of rows inserted
    """
    if not tasks:
        return 0

    coros = [
        fetch_minute_async(client, code, dt.strftime("%Y%m%d"))
        for code, dt in tasks
    ]

    results = await asyncio.gather(*coros)

    all_rows = []
    for code, date_str, data in results:
        if "error" in data:
            logger.warning(f"Fetch failed for {code} {date_str}: {data['error']}")
            continue
        rows = parse_ohlcv(code, date_str, data)
        all_rows.extend(rows)

    if all_rows:
        insert_batch(db_client, all_rows, table_name=table_name)
        print(f"Inserted {len(all_rows)} rows from {len(tasks)} tasks into {table_name}")

    return len(all_rows)


async def collect_index_batch(
    client: httpx.AsyncClient,
    db_client,
    tasks: List[Tuple[str, date]],
    table_name: str = "kospi200_index_1m",
) -> int:
    """
    Collect a batch of (index_symbol, date) combinations.
    """
    if not tasks:
        return 0

    coros = [
        fetch_index_minute_async(client, symbol, dt.strftime("%Y%m%d"))
        for symbol, dt in tasks
    ]

    results = await asyncio.gather(*coros)

    all_rows = []
    for symbol, date_str, data in results:
        if "error" in data:
            logger.warning(f"Index fetch failed for {symbol} {date_str}: {data['error']}")
            continue
        rows = parse_ohlcv(symbol, date_str, data)
        all_rows.extend(rows)

    if all_rows:
        insert_batch(db_client, all_rows, table_name=table_name)
        print(f"Inserted {len(all_rows)} rows from {len(tasks)} tasks into {table_name}")

    return len(all_rows)



async def backfill(days: int = 365, verbose: bool = True):
    """
    Run backfill for the past N days.

    Args:
        days: Number of days to backfill
        verbose: Print progress
    """
    if verbose:
        print("Starting backfill...")

    start = date.today() - timedelta(days=max(days, 1))
    end = date.today()
    trading_days = get_trading_days_range(start, end)

    if verbose:
        print(f"Trading days: {len(trading_days)}")

    ensure_database()
    db_client = get_db_client()
    ensure_table(db_client)

    try:
        collected = get_collected_pairs_in_range(db_client, start, end)
    except Exception as e:
        logger.warning(f"Failed to get collected pairs (proceeding with empty set): {e}")
        collected = set()

    total_rows = 0
    total_tasks = 0

    trading_days_iter = list(reversed(trading_days))

    async with httpx.AsyncClient(timeout=30.0) as client:
        for idx, day in enumerate(trading_days_iter, start=1):
            codes = get_active_codes_for_date(day)
            day_tasks = [(code, day) for code in codes if (code, day) not in collected]

            if not day_tasks:
                continue

            total_tasks += len(day_tasks)
            if verbose:
                print(f"{idx}/{len(trading_days_iter)} {day} tasks={len(day_tasks)}")

            rows = await collect_batch(client, db_client, day_tasks)
            total_rows += rows

    if verbose:
        print(f"Backfill complete. tasks={total_tasks}, rows={total_rows}")

    return total_rows


async def collect_today(verbose: bool = True):
    """
    Collect today's data (run after market close).
    """
    if not is_after_market_close():
        if verbose:
            print("Market is still open. Please run after 15:45 KST.")
        return 0

    today = date.today()

    if verbose:
        print(f"Collecting data for {today}")

    codes = get_active_codes_for_date(today)

    ensure_database()
    db_client = get_db_client()
    ensure_table(db_client)

    tasks = [(code, today) for code in codes]

    async with httpx.AsyncClient(timeout=30.0) as client:
        rows = await collect_batch(client, db_client, tasks)

    if verbose:
        print(f"Today's collection complete. Rows: {rows}")

    return rows


# =============================================================================
# KOSPI 200 Index Backfill
# =============================================================================

async def backfill_kospi200_index(days: int = 365, verbose: bool = True):
    """
    Run backfill for KOSPI 200 Index for the past N days.
    """
    if verbose:
        print("Starting KOSPI 200 Index backfill...")

    start = date.today() - timedelta(days=max(days, 1))
    end = date.today()
    trading_days = get_trading_days_range(start, end)

    if verbose:
        print(f"Trading days: {len(trading_days)}")

    ensure_database()
    db_client = get_db_client()
    ensure_kospi200_index_table(db_client)

    try:
        result = db_client.query(
            """
            SELECT DISTINCT toDate(datetime) as dt
            FROM kospi200_index_1m
            WHERE dt >= %(start)s AND dt <= %(end)s
            """,
            parameters={"start": start, "end": end},
        )
        collected_dates = {row[0] for row in result.result_rows}
    except Exception as e:
        logger.warning(f"Failed to get collected dates (proceeding with empty set): {e}")
        collected_dates = set()

    total_rows = 0
    trading_days_iter = list(reversed(trading_days))
    index_symbol = settings.index.index_symbol

    async with httpx.AsyncClient(timeout=30.0) as client:
        for idx, day in enumerate(trading_days_iter, start=1):
            if day in collected_dates:
                continue

            if verbose:
                print(f"{idx}/{len(trading_days_iter)} {day}")

            tasks = [(index_symbol, day)]
            rows = await collect_index_batch(client, db_client, tasks, table_name="kospi200_index_1m")
            total_rows += rows

    if verbose:
        print(f"KOSPI 200 Index backfill complete. rows={total_rows}")

    return total_rows


async def collect_today_kospi200_index(verbose: bool = True):
    """
    Collect today's KOSPI 200 Index data (run after market close).
    """
    if not is_after_market_close():
        if verbose:
            print("Market is still open. Please run after 15:45 KST.")
        return 0

    today = date.today()

    if verbose:
        print(f"Collecting KOSPI 200 Index data for {today}")

    ensure_database()
    db_client = get_db_client()
    ensure_kospi200_index_table(db_client)

    tasks = [(settings.index.index_symbol, today)]

    async with httpx.AsyncClient(timeout=30.0) as client:
        rows = await collect_index_batch(client, db_client, tasks, table_name="kospi200_index_1m")

    if verbose:
        print(f"Today's KOSPI 200 Index collection complete. Rows: {rows}")

    return rows


# =============================================================================
# KOSPI 200 Futures (Full-Size) Backfill
# =============================================================================

# KOSPI 200 Futures short code (relative position, auto-rolling)
KOSPI200F_FRONT_CODE = "101S6000"


async def backfill_kospi200f(days: int = 365, verbose: bool = True):
    """
    Run backfill for KOSPI 200 Futures (full-size) for the past N days.

    Uses the short code 101S6000 which always refers to the front month.

    Args:
        days: Number of days to backfill
        verbose: Print progress
    """
    if verbose:
        print("Starting KOSPI 200 Futures backfill...")

    start = date.today() - timedelta(days=max(days, 1))
    end = date.today()
    trading_days = get_trading_days_range(start, end)

    if verbose:
        print(f"Trading days: {len(trading_days)}")

    ensure_database()
    db_client = get_db_client()
    ensure_kospi200f_table(db_client)

    # Check what we already have
    try:
        result = db_client.query(
            """
            SELECT DISTINCT toDate(datetime) as dt
            FROM kospi200f_1m
            WHERE dt >= %(start)s AND dt <= %(end)s
            """,
            parameters={"start": start, "end": end},
        )
        collected_dates = {row[0] for row in result.result_rows}
    except Exception as e:
        logger.warning(f"Failed to get collected dates (proceeding with empty set): {e}")
        collected_dates = set()

    total_rows = 0
    trading_days_iter = list(reversed(trading_days))

    async with httpx.AsyncClient(timeout=30.0) as client:
        for idx, day in enumerate(trading_days_iter, start=1):
            if day in collected_dates:
                continue

            if verbose:
                print(f"{idx}/{len(trading_days_iter)} {day}")

            # Use the short code - it always refers to front month
            tasks = [(KOSPI200F_FRONT_CODE, day)]
            rows = await collect_batch(client, db_client, tasks, table_name="kospi200f_1m")
            total_rows += rows

    if verbose:
        print(f"KOSPI 200 Futures backfill complete. rows={total_rows}")

    return total_rows


async def collect_today_kospi200f(verbose: bool = True):
    """
    Collect today's KOSPI 200 Futures data (run after market close).
    """
    if not is_after_market_close():
        if verbose:
            print("Market is still open. Please run after 15:45 KST.")
        return 0

    today = date.today()

    if verbose:
        print(f"Collecting KOSPI 200 Futures data for {today}")

    ensure_database()
    db_client = get_db_client()
    ensure_kospi200f_table(db_client)

    tasks = [(KOSPI200F_FRONT_CODE, today)]

    async with httpx.AsyncClient(timeout=30.0) as client:
        rows = await collect_batch(client, db_client, tasks, table_name="kospi200f_1m")

    if verbose:
        print(f"Today's KOSPI 200 Futures collection complete. Rows: {rows}")

    return rows



# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="KOSPI Historical Data Backfill")
    parser.add_argument("--backfill", action="store_true", help="Run Mini futures backfill")
    parser.add_argument("--today", action="store_true", help="Collect today's Mini futures data")
    parser.add_argument("--kospi200-index", action="store_true", help="Run KOSPI 200 Index backfill")
    parser.add_argument("--kospi200-index-today", action="store_true", help="Collect today's KOSPI 200 Index data")
    parser.add_argument("--kospi200f", action="store_true", help="Run KOSPI 200 Futures (full-size) backfill")
    parser.add_argument("--kospi200f-today", action="store_true", help="Collect today's KOSPI 200 Futures data")
    parser.add_argument("--all", action="store_true", help="Backfill Mini, KOSPI 200 Index, and KOSPI 200 Futures")
    parser.add_argument("--days", type=int, default=365, help="Backfill last N days")

    args = parser.parse_args()

    if args.all:
        print("=== Backfilling Mini Futures ===")
        asyncio.run(backfill(days=args.days))
        print("\n=== Backfilling KOSPI 200 Index ===")
        asyncio.run(backfill_kospi200_index(days=args.days))
        print("\n=== Backfilling KOSPI 200 Futures ===")
        asyncio.run(backfill_kospi200f(days=args.days))
    elif args.backfill:
        asyncio.run(backfill(days=args.days))
    elif args.today:
        asyncio.run(collect_today())
    elif args.kospi200_index:
        asyncio.run(backfill_kospi200_index(days=args.days))
    elif args.kospi200_index_today:
        asyncio.run(collect_today_kospi200_index())
    elif args.kospi200f:
        asyncio.run(backfill_kospi200f(days=args.days))
    elif args.kospi200f_today:
        asyncio.run(collect_today_kospi200f())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
