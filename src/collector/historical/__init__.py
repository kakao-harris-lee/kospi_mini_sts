"""
Historical Data Collection Module

과거 1분봉 OHLCV 데이터 백필 기능

사용법:
    # CLI
    sts backfill --days 180
    sts backfill --today

    # Python
    from src.collector.historical import backfill, collect_today
    await backfill(days=180)
    await collect_today()
"""

from .backfill import backfill, collect_today
from .futures import (
    get_active_codes_for_date,
    get_all_codes_in_range,
    make_code,
    parse_code,
)
from .calendar import (
    get_trading_days_range,
    is_trading_day,
    is_market_open,
    is_after_market_close,
    KOREAN_HOLIDAYS,
)

__all__ = [
    # Backfill functions
    "backfill",
    "collect_today",
    # Futures utilities
    "get_active_codes_for_date",
    "get_all_codes_in_range",
    "make_code",
    "parse_code",
    # Calendar utilities
    "get_trading_days_range",
    "is_trading_day",
    "is_market_open",
    "is_after_market_close",
    "KOREAN_HOLIDAYS",
]
