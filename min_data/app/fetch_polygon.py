"""Polygon.io API client for fetching ES futures data."""
import time
from datetime import datetime, date
from typing import List, Tuple, Dict, Any, Optional
from polygon import RESTClient

from . import config


class PolygonRateLimiter:
    """Rate limiter for Polygon.io API (무료 플랜: 5 req/min)."""

    def __init__(self, requests_per_minute: int = None):
        self.requests_per_minute = requests_per_minute or config.POLYGON_RATE_LIMIT
        self.interval = 60.0 / self.requests_per_minute
        self.last_request_time = 0.0

    def wait(self):
        """Wait if necessary to respect rate limit."""
        now = time.time()
        elapsed = now - self.last_request_time
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self.last_request_time = time.time()


# Global rate limiter instance
_rate_limiter: Optional[PolygonRateLimiter] = None


def _get_rate_limiter() -> PolygonRateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = PolygonRateLimiter()
    return _rate_limiter


def get_client() -> RESTClient:
    """Get Polygon REST client."""
    return RESTClient(config.POLYGON_API_KEY)


def fetch_es_minute(
    code: str,
    start_date: date,
    end_date: date,
    limit: int = 50000
) -> Dict[str, Any]:
    """
    Fetch ES futures 1-minute OHLCV data from Polygon.io.

    Args:
        code: ES futures ticker (e.g., 'ESZ5')
        start_date: Start date
        end_date: End date
        limit: Maximum results per request (default: 50000)

    Returns:
        Dict with 'data' key containing list of OHLCV dicts,
        or 'error' key if failed
    """
    _get_rate_limiter().wait()

    try:
        client = get_client()
        aggs = []

        for a in client.list_aggs(
            ticker=code,
            multiplier=1,
            timespan="minute",
            from_=start_date.strftime("%Y-%m-%d"),
            to=end_date.strftime("%Y-%m-%d"),
            limit=limit,
        ):
            aggs.append({
                'timestamp': a.timestamp,
                'open': a.open,
                'high': a.high,
                'low': a.low,
                'close': a.close,
                'volume': a.volume,
            })

        return {'data': aggs, 'count': len(aggs)}

    except Exception as e:
        return {'error': str(e)}


def parse_polygon_ohlcv(code: str, data: Dict[str, Any]) -> List[Tuple]:
    """
    Parse Polygon API response to OHLCV tuples for DB insertion.

    Args:
        code: ES futures ticker (e.g., 'ESZ5')
        data: API response dict with 'data' key

    Returns:
        List of (code, datetime, open, high, low, close, volume) tuples
    """
    if 'error' in data:
        return []

    rows = []
    for item in data.get('data', []):
        try:
            # Polygon timestamp is in milliseconds
            ts_ms = item['timestamp']
            dt = datetime.utcfromtimestamp(ts_ms / 1000)

            rows.append((
                code,
                dt,
                float(item['open']),
                float(item['high']),
                float(item['low']),
                float(item['close']),
                int(item.get('volume', 0)),
            ))
        except (KeyError, ValueError, TypeError) as e:
            # Skip invalid rows
            continue

    return rows


def fetch_and_parse(
    code: str,
    start_date: date,
    end_date: date
) -> Tuple[str, List[Tuple], Optional[str]]:
    """
    Fetch ES futures data and parse to DB-ready tuples.

    Args:
        code: ES futures ticker
        start_date: Start date
        end_date: End date

    Returns:
        Tuple of (code, rows, error_message)
        - rows is empty list if error occurred
        - error_message is None if successful
    """
    result = fetch_es_minute(code, start_date, end_date)

    if 'error' in result:
        return (code, [], result['error'])

    rows = parse_polygon_ohlcv(code, result)
    return (code, rows, None)
