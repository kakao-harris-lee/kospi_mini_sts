"""Fetch KOSPI Mini futures minute OHLCV from Korea Investment OpenAPI."""

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app import config
from app.token import KoreaInvestToken


tok = KoreaInvestToken()


class _RateLimiter:
    """Simple global rate limiter (requests per second) for async callers."""

    def __init__(self, rps: float):
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
_rate_limiter: Optional[_RateLimiter] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(config.API_SEMAPHORE_SIZE)
    return _semaphore


def _get_rate_limiter() -> _RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = _RateLimiter(config.API_RATE_LIMIT)
    return _rate_limiter


def _first_present(item: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    for k in keys:
        v = item.get(k)
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        return v
    return default


def fetch_minute(code: str, date: str) -> dict:
    """
    Fetch minute data synchronously.

    Args:
        code: Futures code (e.g., '101M25')
        date: Date string in YYYYMMDD format

    Returns:
        API response as dict
    """
    import requests

    url = f"{config.KI_API_BASE}/uapi/domestic-futureoption/v1/quotations/inquire-time-fuopchartprice"
    headers = {
        "Authorization": f"Bearer {tok.get()}",
        "appkey": config.KI_APP_KEY,
        "appsecret": config.KI_APP_SECRET,
        "tr_id": "FHKIF03020200",  # 국내선물옵션 분봉
        "content-type": "application/json; charset=utf-8",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "F",
        "FID_INPUT_ISCD": code,
        "FID_HOUR_CLS_CODE": "1",  # 1분봉
        "FID_PW_DATA_INCU_YN": "Y",
        "FID_FAKE_TICK_INCU_YN": "N",
        "FID_INPUT_DATE_1": date,
        "FID_INPUT_HOUR_1": "",
    }
    r = requests.get(url, headers=headers, params=params)
    if not r.text:
        return {"error": f"empty response (status {r.status_code})"}
    try:
        return r.json()
    except Exception as e:
        return {"error": f"json parse: {e}", "status": r.status_code, "text": r.text[:200]}


async def fetch_minute_async(
    client: httpx.AsyncClient,
    code: str,
    date: str,
    max_retries: int = 3
) -> Tuple[str, str, dict]:
    """
    Fetch minute data asynchronously with rate limiting and retry.

    Args:
        client: httpx async client
        code: Futures code (e.g., '101M25')
        date: Date string in YYYYMMDD format
        max_retries: Maximum retry attempts on transient errors

    Returns:
        Tuple of (code, date, response_data)
    """
    url = f"{config.KI_API_BASE}/uapi/domestic-futureoption/v1/quotations/inquire-time-fuopchartprice"
    params = {
        "FID_COND_MRKT_DIV_CODE": "F",
        "FID_INPUT_ISCD": code,
        "FID_HOUR_CLS_CODE": "1",
        "FID_PW_DATA_INCU_YN": "Y",
        "FID_FAKE_TICK_INCU_YN": "N",
        "FID_INPUT_DATE_1": date,
        "FID_INPUT_HOUR_1": "",
    }

    last_error = None
    for attempt in range(max_retries):
        headers = {
            "Authorization": f"Bearer {tok.get()}",
            "appkey": config.KI_APP_KEY,
            "appsecret": config.KI_APP_SECRET,
            "tr_id": "FHKIF03020200",
            "content-type": "application/json; charset=utf-8",
        }

        async with _get_semaphore():
            try:
                await _get_rate_limiter().wait()
                resp = await client.get(url, headers=headers, params=params)

                # Check for empty response
                if not resp.text or resp.text.strip() == "":
                    return (code, date, {"error": f"empty response (status {resp.status_code})"})

                # Try to parse JSON
                try:
                    data = resp.json()
                except Exception as json_err:
                    print(f"JSON parse error for {code} {date}: {resp.text[:200]}")
                    return (code, date, {"error": f"json parse: {json_err}"})

                # Check for API error
                if data.get("rt_cd") != "0":
                    msg = data.get("msg1", "Unknown error")
                    return (code, date, {"error": msg})
                return (code, date, data)

            except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError) as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2  # 2, 4, 6 seconds
                    print(f"Retry {attempt + 1}/{max_retries} for {code} {date} after {wait_time}s: {e}")
                    await asyncio.sleep(wait_time)
                continue

            except Exception as e:
                print(f"Error fetching {code} {date}: {e}")
                return (code, date, {"error": str(e)})

    print(f"Failed after {max_retries} retries for {code} {date}: {last_error}")
    return (code, date, {"error": str(last_error)})


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

    output = data.get("output2", []) or data.get("output", [])
    if not output:
        return rows

    for item in output:
        try:
            # Time (HHMMSS or HHMM)
            time_str = _first_present(
                item,
                [
                    "stck_cntg_hour",
                    "futs_cntg_hour",
                    "cntg_hour",
                    "bsop_hour",
                    "hour",
                ],
                default="",
            )
            if not time_str:
                continue

            # Normalize HHMM -> HHMMSS
            if len(time_str) == 4:
                time_str = f"{time_str}00"

            dt_str = f"{date_str}{time_str}"
            dt = datetime.strptime(dt_str, "%Y%m%d%H%M%S")

            o = _first_present(item, ["futs_oprc", "open", "stck_oprc"], 0)
            h = _first_present(item, ["futs_hgpr", "high", "stck_hgpr"], 0)
            l = _first_present(item, ["futs_lwpr", "low", "stck_lwpr"], 0)
            c = _first_present(item, ["futs_prpr", "close", "stck_prpr", "stck_clpr"], 0)
            v = _first_present(item, ["cntg_vol", "acml_vol", "volume"], 0)

            row = (
                code,
                dt,
                float(o or 0),
                float(h or 0),
                float(l or 0),
                float(c or 0),
                int(v or 0),
            )
            rows.append(row)
        except (ValueError, TypeError) as e:
            continue

    return rows
