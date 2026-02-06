"""
Technical Indicators for Trend Confirmed Strategy

Calculates:
- Simple Moving Average (SMA)
- Ichimoku Kinko Hyo (Cloud)
- Average True Range (ATR)
"""

import logging
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class TechnicalData:
    """Current technical indicator values"""
    ma_fast: float              # MA(20)
    ma_slow: float              # MA(60)
    ichimoku_span_a: float      # Senkou Span A
    ichimoku_span_b: float      # Senkou Span B
    atr: float                  # Average True Range
    is_ready: bool              # Enough bars for calculation
    current_price: float = 0.0  # Latest close price

    @property
    def cloud_top(self) -> float:
        """Top of Ichimoku cloud"""
        return max(self.ichimoku_span_a, self.ichimoku_span_b)

    @property
    def cloud_bottom(self) -> float:
        """Bottom of Ichimoku cloud"""
        return min(self.ichimoku_span_a, self.ichimoku_span_b)

    @property
    def is_bullish_ma(self) -> bool:
        """MA fast > slow (uptrend)"""
        return self.ma_fast > self.ma_slow

    @property
    def is_above_cloud(self) -> bool:
        """Price above Ichimoku cloud"""
        return self.current_price > self.cloud_top

    @property
    def is_below_cloud(self) -> bool:
        """Price below Ichimoku cloud"""
        return self.current_price < self.cloud_bottom


@dataclass
class BarInput:
    """Minimal bar data for indicator calculation"""
    high: float
    low: float
    close: float


class TechnicalCalculator:
    """
    Calculates technical indicators from price history.

    Indicators:
    - SMA(20) and SMA(60) for trend direction
    - Ichimoku Cloud for support/resistance
    - ATR(14) for volatility-based stops
    """

    # Ichimoku standard periods
    TENKAN_PERIOD = 9
    KIJUN_PERIOD = 26
    SENKOU_B_PERIOD = 52

    def __init__(
        self,
        ma_fast_period: int = 20,
        ma_slow_period: int = 60,
        atr_period: int = 14,
    ):
        """
        Args:
            ma_fast_period: Fast MA period (default 20)
            ma_slow_period: Slow MA period (default 60)
            atr_period: ATR period (default 14)
        """
        self.ma_fast_period = ma_fast_period
        self.ma_slow_period = ma_slow_period
        self.atr_period = atr_period

        # Need enough bars for slowest indicator
        max_period = max(ma_slow_period, self.SENKOU_B_PERIOD)
        self.price_history: Deque[BarInput] = deque(maxlen=max_period + 10)

        # ATR needs previous close
        self._prev_close: Optional[float] = None
        self._atr_values: Deque[float] = deque(maxlen=atr_period)

        self._last_data: Optional[TechnicalData] = None

    def update(self, high: float, low: float, close: float) -> TechnicalData:
        """
        Update with new bar data.

        Args:
            high: Bar high price
            low: Bar low price
            close: Bar close price

        Returns:
            TechnicalData with current indicator values
        """
        bar = BarInput(high=high, low=low, close=close)
        self.price_history.append(bar)

        # Calculate ATR component (True Range)
        if self._prev_close is not None:
            tr = self._calc_true_range(high, low, self._prev_close)
            self._atr_values.append(tr)
        self._prev_close = close

        # Check if ready
        is_ready = len(self.price_history) >= self.ma_slow_period

        # Calculate indicators
        ma_fast = self._calc_sma(self.ma_fast_period)
        ma_slow = self._calc_sma(self.ma_slow_period)
        span_a, span_b = self._calc_ichimoku()
        atr = self._calc_atr()

        self._last_data = TechnicalData(
            ma_fast=ma_fast,
            ma_slow=ma_slow,
            ichimoku_span_a=span_a,
            ichimoku_span_b=span_b,
            atr=atr,
            is_ready=is_ready,
            current_price=close,
        )

        return self._last_data

    def _calc_sma(self, period: int) -> float:
        """Calculate Simple Moving Average."""
        if len(self.price_history) < period:
            return 0.0

        closes = [bar.close for bar in list(self.price_history)[-period:]]
        return float(np.mean(closes))

    def _calc_ichimoku(self) -> tuple[float, float]:
        """
        Calculate Ichimoku Senkou Spans.

        Senkou Span A = (Tenkan + Kijun) / 2
        Senkou Span B = (52-period high + 52-period low) / 2

        Note: We use current values without the 26-period shift
        since we're checking current price vs current cloud.
        """
        bars = list(self.price_history)

        # Tenkan-sen (Conversion Line) - 9-period
        if len(bars) >= self.TENKAN_PERIOD:
            recent = bars[-self.TENKAN_PERIOD:]
            tenkan = (max(b.high for b in recent) + min(b.low for b in recent)) / 2
        else:
            tenkan = bars[-1].close if bars else 0.0

        # Kijun-sen (Base Line) - 26-period
        if len(bars) >= self.KIJUN_PERIOD:
            recent = bars[-self.KIJUN_PERIOD:]
            kijun = (max(b.high for b in recent) + min(b.low for b in recent)) / 2
        else:
            kijun = bars[-1].close if bars else 0.0

        # Senkou Span A
        span_a = (tenkan + kijun) / 2

        # Senkou Span B - 52-period
        if len(bars) >= self.SENKOU_B_PERIOD:
            recent = bars[-self.SENKOU_B_PERIOD:]
            span_b = (max(b.high for b in recent) + min(b.low for b in recent)) / 2
        else:
            span_b = bars[-1].close if bars else 0.0

        return span_a, span_b

    def _calc_true_range(self, high: float, low: float, prev_close: float) -> float:
        """
        Calculate True Range.

        TR = max(high - low, |high - prev_close|, |low - prev_close|)
        """
        return max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

    def _calc_atr(self) -> float:
        """Calculate Average True Range."""
        if len(self._atr_values) < self.atr_period:
            # Not enough data, return simple range
            if self.price_history:
                recent = list(self.price_history)[-min(len(self.price_history), 14):]
                ranges = [b.high - b.low for b in recent]
                return float(np.mean(ranges)) if ranges else 0.0
            return 0.0

        return float(np.mean(list(self._atr_values)))

    def get_current_data(self) -> Optional[TechnicalData]:
        """Get most recent technical data."""
        return self._last_data

    def get_atr(self) -> float:
        """Get current ATR value."""
        return self._last_data.atr if self._last_data else 0.0

    def is_ready(self) -> bool:
        """Check if enough data for valid calculations."""
        return len(self.price_history) >= self.ma_slow_period

    def reset(self):
        """Clear history and reset state."""
        self.price_history.clear()
        self._atr_values.clear()
        self._prev_close = None
        self._last_data = None

    def get_stats(self) -> dict:
        """Get diagnostic statistics."""
        return {
            'bars_count': len(self.price_history),
            'is_ready': self.is_ready(),
            'ma_fast': self._last_data.ma_fast if self._last_data else 0.0,
            'ma_slow': self._last_data.ma_slow if self._last_data else 0.0,
            'atr': self._last_data.atr if self._last_data else 0.0,
            'cloud_top': self._last_data.cloud_top if self._last_data else 0.0,
            'cloud_bottom': self._last_data.cloud_bottom if self._last_data else 0.0,
        }
