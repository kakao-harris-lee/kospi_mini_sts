"""
Ensemble Filter for MODE_B Trend Following

All 3 conditions must agree for entry:
1. Deep Learning: P(Up) > 85% or P(Down) > 85%
2. Moving Average: MA(20) > MA(60) for long, MA(20) < MA(60) for short
3. Ichimoku: Price above cloud for long, price below cloud for short
"""

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from .technical_indicators import TechnicalData

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    """Result of ensemble filter check"""
    can_enter: bool
    direction: Optional[str]  # "LONG", "SHORT", or None
    rejection_reason: Optional[str]
    dl_passed: bool
    ma_passed: bool
    ichimoku_passed: bool

    @property
    def all_passed(self) -> bool:
        return self.dl_passed and self.ma_passed and self.ichimoku_passed


class EnsembleFilter:
    """
    Ensemble filter requiring unanimous agreement for entry.

    Long Entry Requires ALL:
    1. DL: P(Up) > threshold (default 85%)
    2. MA: MA(20) > MA(60)
    3. Ichimoku: Price > Cloud (both spans)

    Short Entry Requires ALL:
    1. DL: P(Down) > threshold (= P(Up) < 1 - threshold)
    2. MA: MA(20) < MA(60)
    3. Ichimoku: Price < Cloud (both spans)
    """

    def __init__(self, dl_threshold: float = 0.85):
        """
        Args:
            dl_threshold: Minimum probability for entry (default 85%)
        """
        self.dl_threshold = dl_threshold

        # Statistics
        self._stats = {
            'total_checks': 0,
            'long_signals': 0,
            'short_signals': 0,
            'rejected_dl': 0,
            'rejected_ma': 0,
            'rejected_ichimoku': 0,
            'rejected_not_ready': 0,
        }

    def check_entry(
        self,
        up_prob: float,
        tech: TechnicalData,
    ) -> FilterResult:
        """
        Check if entry conditions are met.

        Args:
            up_prob: DL prediction probability for up move
            tech: Technical indicator data

        Returns:
            FilterResult with entry decision and details
        """
        self._stats['total_checks'] += 1
        down_prob = 1.0 - up_prob

        # Check if technical data is ready
        if not tech.is_ready:
            self._stats['rejected_not_ready'] += 1
            return FilterResult(
                can_enter=False,
                direction=None,
                rejection_reason="Technical indicators warming up",
                dl_passed=False,
                ma_passed=False,
                ichimoku_passed=False,
            )

        # Check for LONG entry
        long_result = self._check_long(up_prob, tech)
        if long_result.can_enter:
            self._stats['long_signals'] += 1
            logger.info(
                f"LONG signal: DL={up_prob:.1%}, "
                f"MA={tech.ma_fast:.2f}>{tech.ma_slow:.2f}, "
                f"price={tech.current_price:.2f}>cloud={tech.cloud_top:.2f}"
            )
            return long_result

        # Check for SHORT entry
        short_result = self._check_short(down_prob, tech)
        if short_result.can_enter:
            self._stats['short_signals'] += 1
            logger.info(
                f"SHORT signal: DL_down={down_prob:.1%}, "
                f"MA={tech.ma_fast:.2f}<{tech.ma_slow:.2f}, "
                f"price={tech.current_price:.2f}<cloud={tech.cloud_bottom:.2f}"
            )
            return short_result

        # No signal - return rejection reason from long check (most common path)
        return long_result

    def _check_long(self, up_prob: float, tech: TechnicalData) -> FilterResult:
        """Check long entry conditions."""
        dl_passed = up_prob > self.dl_threshold
        ma_passed = tech.ma_fast > tech.ma_slow
        ichimoku_passed = tech.current_price > tech.cloud_top

        # Determine rejection reason
        rejection_reason = None
        if not dl_passed:
            rejection_reason = f"DL probability {up_prob:.1%} <= {self.dl_threshold:.0%}"
            self._stats['rejected_dl'] += 1
        elif not ma_passed:
            rejection_reason = f"MA bearish: {tech.ma_fast:.2f} <= {tech.ma_slow:.2f}"
            self._stats['rejected_ma'] += 1
        elif not ichimoku_passed:
            rejection_reason = f"Price {tech.current_price:.2f} not above cloud {tech.cloud_top:.2f}"
            self._stats['rejected_ichimoku'] += 1

        can_enter = dl_passed and ma_passed and ichimoku_passed

        return FilterResult(
            can_enter=can_enter,
            direction="LONG" if can_enter else None,
            rejection_reason=rejection_reason,
            dl_passed=dl_passed,
            ma_passed=ma_passed,
            ichimoku_passed=ichimoku_passed,
        )

    def _check_short(self, down_prob: float, tech: TechnicalData) -> FilterResult:
        """Check short entry conditions."""
        dl_passed = down_prob > self.dl_threshold
        ma_passed = tech.ma_fast < tech.ma_slow
        ichimoku_passed = tech.current_price < tech.cloud_bottom

        # Determine rejection reason
        rejection_reason = None
        if not dl_passed:
            rejection_reason = f"DL down probability {down_prob:.1%} <= {self.dl_threshold:.0%}"
            self._stats['rejected_dl'] += 1
        elif not ma_passed:
            rejection_reason = f"MA bullish: {tech.ma_fast:.2f} >= {tech.ma_slow:.2f}"
            self._stats['rejected_ma'] += 1
        elif not ichimoku_passed:
            rejection_reason = f"Price {tech.current_price:.2f} not below cloud {tech.cloud_bottom:.2f}"
            self._stats['rejected_ichimoku'] += 1

        can_enter = dl_passed and ma_passed and ichimoku_passed

        return FilterResult(
            can_enter=can_enter,
            direction="SHORT" if can_enter else None,
            rejection_reason=rejection_reason,
            dl_passed=dl_passed,
            ma_passed=ma_passed,
            ichimoku_passed=ichimoku_passed,
        )

    def check_long_only(self, up_prob: float, tech: TechnicalData) -> Tuple[bool, str]:
        """
        Simplified check for long entry only.

        Returns:
            (can_enter, rejection_reason)
        """
        result = self._check_long(up_prob, tech)
        return result.can_enter, result.rejection_reason or ""

    def check_short_only(self, down_prob: float, tech: TechnicalData) -> Tuple[bool, str]:
        """
        Simplified check for short entry only.

        Returns:
            (can_enter, rejection_reason)
        """
        result = self._check_short(down_prob, tech)
        return result.can_enter, result.rejection_reason or ""

    def get_stats(self) -> dict:
        """Get filter statistics."""
        total = self._stats['total_checks']
        signals = self._stats['long_signals'] + self._stats['short_signals']
        return {
            **self._stats,
            'signal_rate': signals / total if total > 0 else 0.0,
        }

    def reset_stats(self):
        """Reset statistics counters."""
        for key in self._stats:
            self._stats[key] = 0
