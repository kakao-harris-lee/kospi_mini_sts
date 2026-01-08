"""
Ensemble Filter for MODE_B Trend Following

All 3 conditions must agree for entry:
1. Deep Learning: P(Up) > 85% or P(Down) > 85%
2. Moving Average: MA(20) > MA(60) for long, MA(20) < MA(60) for short
3. Ichimoku: Price above cloud for long, price below cloud for short

Multi-Horizon Mode ("Shortest Confirms Longest"):
- Uses z-score thresholds to handle model bias
- h10 sets direction (z-score > threshold for LONG, z-score < -threshold for SHORT)
- h1 or h3 confirms timing with lower z-score threshold
"""

import logging
from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple, Dict

import numpy as np

from .technical_indicators import TechnicalData

logger = logging.getLogger(__name__)


class ProbabilityCalibrator:
    """
    Running calibrator that tracks prediction distribution and computes z-scores.

    Handles model bias by normalizing predictions relative to their actual distribution.
    If a model consistently predicts 20% up_prob, a prediction of 30% would have a positive
    z-score indicating "more bullish than usual".
    """

    def __init__(self, window_size: int = 200, min_samples: int = 50):
        """
        Args:
            window_size: Number of recent predictions to track
            min_samples: Minimum samples needed before calibration kicks in
        """
        self.window_size = window_size
        self.min_samples = min_samples
        self._history: Dict[int, deque] = {}  # horizon -> recent predictions

    def update(self, horizon: int, prob: float) -> None:
        """Add new prediction to history."""
        if horizon not in self._history:
            self._history[horizon] = deque(maxlen=self.window_size)
        self._history[horizon].append(prob)

    def get_zscore(self, horizon: int, prob: float) -> Optional[float]:
        """
        Compute z-score for a prediction.

        Returns None if not enough samples yet.
        """
        if horizon not in self._history:
            return None

        history = self._history[horizon]
        if len(history) < self.min_samples:
            return None

        mean = np.mean(history)
        std = np.std(history)

        if std < 0.001:  # Avoid division by zero only
            return None

        return (prob - mean) / std

    def get_stats(self, horizon: int) -> Dict[str, float]:
        """Get statistics for a horizon."""
        if horizon not in self._history or len(self._history[horizon]) == 0:
            return {'mean': 0.5, 'std': 0.1, 'count': 0}

        history = self._history[horizon]
        return {
            'mean': float(np.mean(history)),
            'std': float(np.std(history)),
            'count': len(history),
        }

    def reset(self) -> None:
        """Reset all history."""
        self._history.clear()


@dataclass
class FilterResult:
    """Result of ensemble filter check"""
    can_enter: bool
    direction: Optional[str]  # "LONG", "SHORT", or None
    rejection_reason: Optional[str]
    dl_passed: bool
    ma_passed: bool
    ichimoku_passed: bool
    # Multi-horizon info
    trigger_horizon: int = 0  # Which horizon triggered (10)
    confirm_horizon: int = 0  # Which horizon confirmed (1 or 3)

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

    def __init__(self, dl_threshold: float = 0.85, max_atr_threshold: float = 1.5):
        """
        Args:
            dl_threshold: Minimum probability for entry (default 85%)
            max_atr_threshold: Skip trading if ATR exceeds this (default 1.5 points)
        """
        self.dl_threshold = dl_threshold
        self.max_atr_threshold = max_atr_threshold

        # Z-score based calibrator for multi-horizon mode
        self.calibrator = ProbabilityCalibrator(window_size=200, min_samples=60)

        # Z-score thresholds for calibrated signals
        # Note: h1/h3 have asymmetric distributions (rarely go negative), so we use
        # different thresholds for LONG vs SHORT confirmation
        self.zscore_trigger_threshold = 1.0  # h10 must be this many stdevs from mean
        self.zscore_long_confirm_threshold = 0.5  # h1/h3 for LONG confirmation
        self.zscore_short_confirm_threshold = 0.0  # h1/h3 for SHORT (just needs to be below mean)

        # Statistics
        self._stats = {
            'total_checks': 0,
            'long_signals': 0,
            'short_signals': 0,
            'rejected_dl': 0,
            'rejected_ma': 0,
            'rejected_ichimoku': 0,
            'rejected_not_ready': 0,
            'calibrated_signals': 0,
            'uncalibrated_signals': 0,
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

    def check_entry_multi_horizon(
        self,
        horizon_probs: Dict[int, float],
        tech: TechnicalData,
    ) -> FilterResult:
        """
        Check entry using multi-horizon "Shortest Confirms Longest" strategy with z-score calibration.

        Uses z-scores to handle model bias (e.g., if model always predicts ~20%, we look for
        deviations from that baseline rather than absolute thresholds).

        h10 sets direction (z-score > 1.5 for LONG, z-score < -1.5 for SHORT)
        h1/h3 confirm timing (z-score > 1.0 for LONG confirm, z-score < -1.0 for SHORT confirm)

        Falls back to absolute thresholds during warm-up period.

        Args:
            horizon_probs: {1: up_prob, 3: up_prob, 5: up_prob, 10: up_prob}
            tech: Technical indicator data

        Returns:
            FilterResult with entry decision
        """
        self._stats['total_checks'] += 1

        h1 = horizon_probs.get(1, 0.5)
        h3 = horizon_probs.get(3, 0.5)
        h5 = horizon_probs.get(5, 0.5)
        h10 = horizon_probs.get(10, 0.5)

        # Note: Calibrator is updated in TrendEngine.check_multi_horizon() before this method

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

        # Volatility filter: skip trading during high ATR periods (gap risk)
        if tech.atr > self.max_atr_threshold:
            return FilterResult(
                can_enter=False,
                direction=None,
                rejection_reason=f"ATR {tech.atr:.2f} > max {self.max_atr_threshold:.2f}",
                dl_passed=False,
                ma_passed=False,
                ichimoku_passed=False,
            )

        # Get z-scores for each horizon
        z10 = self.calibrator.get_zscore(10, h10)
        z1 = self.calibrator.get_zscore(1, h1)
        z3 = self.calibrator.get_zscore(3, h3)

        # Use z-score based thresholds if calibrated, otherwise fall back to absolute
        if z10 is not None and z1 is not None and z3 is not None:
            return self._check_calibrated(h1, h3, h10, z1, z3, z10, tech)
        else:
            return self._check_uncalibrated(h1, h3, h10, tech)

    def _check_calibrated(
        self,
        h1: float, h3: float, h10: float,
        z1: float, z3: float, z10: float,
        tech: TechnicalData,
    ) -> FilterResult:
        """Check entry using z-score calibrated thresholds."""
        # LONG: h10 z-score significantly above mean + confirmation
        if z10 > self.zscore_trigger_threshold:
            confirm_z1 = z1 > self.zscore_long_confirm_threshold
            confirm_z3 = z3 > self.zscore_long_confirm_threshold

            if confirm_z1 or confirm_z3:
                # DL calibrated - check MA + Ichimoku
                ma_passed = tech.ma_fast > tech.ma_slow
                ichimoku_passed = tech.current_price > tech.cloud_top

                if ma_passed and ichimoku_passed:
                    self._stats['long_signals'] += 1
                    self._stats['calibrated_signals'] += 1
                    h10_stats = self.calibrator.get_stats(10)
                    logger.info(
                        f"LONG (calibrated): z10={z10:.2f}, z1={z1:.2f}, z3={z3:.2f}, "
                        f"h10={h10:.1%} (mean={h10_stats['mean']:.1%}), "
                        f"confirm={'z1' if confirm_z1 else 'z3'}"
                    )
                    return FilterResult(
                        can_enter=True,
                        direction="LONG",
                        rejection_reason=None,
                        dl_passed=True,
                        ma_passed=True,
                        ichimoku_passed=True,
                        trigger_horizon=10,
                        confirm_horizon=1 if confirm_z1 else 3,
                    )
                else:
                    rejection = "MA bearish" if not ma_passed else "Price below cloud"
                    self._stats['rejected_ma' if not ma_passed else 'rejected_ichimoku'] += 1
                    return FilterResult(
                        can_enter=False,
                        direction=None,
                        rejection_reason=f"Calibrated LONG rejected: {rejection}",
                        dl_passed=True,
                        ma_passed=ma_passed,
                        ichimoku_passed=ichimoku_passed,
                    )

        # SHORT: h10 z-score significantly below mean + confirmation
        # Note: Use asymmetric threshold since h1/h3 rarely go very negative
        if z10 < -self.zscore_trigger_threshold:
            confirm_z1 = z1 < -self.zscore_short_confirm_threshold
            confirm_z3 = z3 < -self.zscore_short_confirm_threshold

            if confirm_z1 or confirm_z3:
                # DL calibrated - check MA + Ichimoku
                ma_passed = tech.ma_fast < tech.ma_slow
                ichimoku_passed = tech.current_price < tech.cloud_bottom

                if ma_passed and ichimoku_passed:
                    self._stats['short_signals'] += 1
                    self._stats['calibrated_signals'] += 1
                    h10_stats = self.calibrator.get_stats(10)
                    logger.info(
                        f"SHORT (calibrated): z10={z10:.2f}, z1={z1:.2f}, z3={z3:.2f}, "
                        f"h10={h10:.1%} (mean={h10_stats['mean']:.1%}), "
                        f"confirm={'z1' if confirm_z1 else 'z3'}"
                    )
                    return FilterResult(
                        can_enter=True,
                        direction="SHORT",
                        rejection_reason=None,
                        dl_passed=True,
                        ma_passed=True,
                        ichimoku_passed=True,
                        trigger_horizon=10,
                        confirm_horizon=1 if confirm_z1 else 3,
                    )
                else:
                    rejection = "MA bullish" if not ma_passed else "Price above cloud"
                    self._stats['rejected_ma' if not ma_passed else 'rejected_ichimoku'] += 1
                    return FilterResult(
                        can_enter=False,
                        direction=None,
                        rejection_reason=f"Calibrated SHORT rejected: {rejection}",
                        dl_passed=True,
                        ma_passed=ma_passed,
                        ichimoku_passed=ichimoku_passed,
                    )

        # No clear signal from z-scores
        self._stats['rejected_dl'] += 1
        h10_stats = self.calibrator.get_stats(10)
        return FilterResult(
            can_enter=False,
            direction=None,
            rejection_reason=f"No z-score signal: z10={z10:.2f}, z1={z1:.2f}, z3={z3:.2f} (h10_mean={h10_stats['mean']:.1%})",
            dl_passed=False,
            ma_passed=False,
            ichimoku_passed=False,
        )

    def _check_uncalibrated(
        self,
        h1: float, h3: float, h10: float,
        tech: TechnicalData,
    ) -> FilterResult:
        """Fall back to absolute thresholds during warm-up (before enough samples for calibration)."""
        # During warm-up, be very conservative - no signals
        self._stats['uncalibrated_signals'] += 1
        self._stats['rejected_dl'] += 1

        samples = self.calibrator.get_stats(10).get('count', 0)
        return FilterResult(
            can_enter=False,
            direction=None,
            rejection_reason=f"Calibration warm-up: {samples}/{self.calibrator.min_samples} samples",
            dl_passed=False,
            ma_passed=False,
            ichimoku_passed=False,
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
        """Reset statistics counters and calibrator."""
        for key in self._stats:
            self._stats[key] = 0
        self.calibrator.reset()
