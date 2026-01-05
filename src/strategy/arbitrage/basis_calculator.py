"""
Basis Calculator for Statistical Arbitrage

Calculates:
- Fair value of futures based on spot index
- Basis (futures - fair value)
- Rolling z-score for entry signals
"""

import logging
import numpy as np
from dataclasses import dataclass
from datetime import date, datetime
from typing import Deque, Optional, Tuple
from collections import deque

logger = logging.getLogger(__name__)


@dataclass
class BasisData:
    """Current basis state"""
    timestamp: float
    spot_index: float           # KOSPI200 index
    futures_price: float        # Mini futures mid price
    fair_value: float           # Theoretical fair value
    basis: float                # futures - fair_value
    basis_zscore: float         # Standardized basis
    days_to_expiry: int
    rolling_mean: float
    rolling_std: float


class BasisCalculator:
    """
    Calculates basis and z-score for statistical arbitrage.

    Fair Value Formula:
        fair_value = spot × (1 + r × T/365)

    Where:
        spot = KOSPI200 index value
        r = risk-free rate (annual)
        T = days to expiry

    Note: Dividends are handled via blackout periods, not explicit calculation.
    """

    # KOSPI200 Mini futures multiplier
    FUTURES_MULTIPLIER = 50000  # KRW per point

    def __init__(
        self,
        risk_free_rate: float = 0.035,
        rolling_window: int = 60,
        min_samples: int = 20
    ):
        """
        Args:
            risk_free_rate: Annual risk-free rate (default 3.5%)
            rolling_window: Window size for rolling statistics (minutes)
            min_samples: Minimum samples before generating z-score
        """
        self.risk_free_rate = risk_free_rate
        self.rolling_window = rolling_window
        self.min_samples = min_samples

        self.basis_history: Deque[float] = deque(maxlen=rolling_window)
        self._last_data: Optional[BasisData] = None

    def calculate_fair_value(
        self,
        spot_index: float,
        days_to_expiry: int
    ) -> float:
        """
        Calculate theoretical fair value of futures.

        Args:
            spot_index: KOSPI200 index value
            days_to_expiry: Days until contract expiry

        Returns:
            Theoretical fair value
        """
        # Cost of carry model (simplified, no dividends)
        time_factor = days_to_expiry / 365.0
        fair_value = spot_index * (1 + self.risk_free_rate * time_factor)
        return fair_value

    def update(
        self,
        spot_index: float,
        futures_price: float,
        days_to_expiry: int,
        timestamp: Optional[float] = None
    ) -> BasisData:
        """
        Update basis calculation with new prices.

        Args:
            spot_index: Current KOSPI200 index value
            futures_price: Current futures mid price
            days_to_expiry: Days until expiry
            timestamp: Optional timestamp (defaults to now)

        Returns:
            BasisData with current state
        """
        import time
        timestamp = timestamp or time.time()

        # Calculate fair value and basis
        fair_value = self.calculate_fair_value(spot_index, days_to_expiry)
        basis = futures_price - fair_value

        # Update rolling history
        self.basis_history.append(basis)

        # Calculate rolling statistics
        rolling_mean, rolling_std = self._get_rolling_stats()

        # Calculate z-score
        if rolling_std > 0 and len(self.basis_history) >= self.min_samples:
            basis_zscore = (basis - rolling_mean) / rolling_std
        else:
            basis_zscore = 0.0

        self._last_data = BasisData(
            timestamp=timestamp,
            spot_index=spot_index,
            futures_price=futures_price,
            fair_value=fair_value,
            basis=basis,
            basis_zscore=basis_zscore,
            days_to_expiry=days_to_expiry,
            rolling_mean=rolling_mean,
            rolling_std=rolling_std
        )

        return self._last_data

    def _get_rolling_stats(self) -> Tuple[float, float]:
        """Calculate rolling mean and std of basis."""
        if len(self.basis_history) < 2:
            return 0.0, 0.0

        arr = np.array(self.basis_history)
        return float(np.mean(arr)), float(np.std(arr, ddof=1))

    def get_zscore(self) -> float:
        """Get current basis z-score."""
        return self._last_data.basis_zscore if self._last_data else 0.0

    def is_ready(self) -> bool:
        """Check if enough samples for valid z-score."""
        return len(self.basis_history) >= self.min_samples

    def is_signal(self, threshold: float = 2.5) -> Tuple[bool, Optional[str]]:
        """
        Check if basis deviation is significant.

        Args:
            threshold: Z-score threshold (default 2.5σ)

        Returns:
            (has_signal, direction) where direction is 'BUY', 'SELL', or None

        Signal Logic:
            - z > +threshold: Futures overpriced → SELL futures
            - z < -threshold: Futures underpriced → BUY futures
        """
        if not self.is_ready():
            return False, None

        z = self.get_zscore()

        if z > threshold:
            return True, "SELL"  # Futures overpriced, sell to capture convergence
        elif z < -threshold:
            return True, "BUY"   # Futures underpriced, buy to capture convergence
        else:
            return False, None

    def get_current_data(self) -> Optional[BasisData]:
        """Get most recent basis data."""
        return self._last_data

    def reset(self):
        """Clear history and reset state."""
        self.basis_history.clear()
        self._last_data = None

    def get_stats(self) -> dict:
        """Get diagnostic statistics."""
        return {
            'samples': len(self.basis_history),
            'is_ready': self.is_ready(),
            'rolling_mean': self._last_data.rolling_mean if self._last_data else 0.0,
            'rolling_std': self._last_data.rolling_std if self._last_data else 0.0,
            'current_zscore': self.get_zscore(),
            'risk_free_rate': self.risk_free_rate,
        }


def calculate_days_to_expiry(contract_code: str, reference_date: Optional[date] = None) -> int:
    """
    Calculate days to expiry for a futures contract.

    KOSPI200 Mini Futures expiry: 2nd Thursday of each month

    Args:
        contract_code: Contract code (e.g., 'A05601' for front month)
        reference_date: Reference date (default: today)

    Returns:
        Days until expiry
    """
    from datetime import timedelta
    import calendar

    ref = reference_date or date.today()

    # A05601 = front month, A05602 = next month
    # Determine expiry month
    if contract_code == "A05601":
        # Front month - current or next month's 2nd Thursday
        expiry = _get_second_thursday(ref.year, ref.month)
        if ref >= expiry:
            # Already past this month's expiry, use next month
            next_month = ref.month + 1
            next_year = ref.year
            if next_month > 12:
                next_month = 1
                next_year += 1
            expiry = _get_second_thursday(next_year, next_month)
    elif contract_code == "A05602":
        # Next month
        next_month = ref.month + 1
        next_year = ref.year
        if next_month > 12:
            next_month = 1
            next_year += 1
        expiry = _get_second_thursday(next_year, next_month)
    else:
        # Default to 30 days if unknown
        logger.warning(f"Unknown contract code: {contract_code}, using 30 days default")
        return 30

    days = (expiry - ref).days
    return max(1, days)  # At least 1 day


def _get_second_thursday(year: int, month: int) -> date:
    """Get the 2nd Thursday of a given month."""
    import calendar

    # Find first day of month
    first_day = date(year, month, 1)

    # Find first Thursday (weekday 3)
    days_until_thursday = (3 - first_day.weekday()) % 7
    first_thursday = first_day + timedelta(days=days_until_thursday)

    # Second Thursday is 7 days later
    second_thursday = first_thursday + timedelta(days=7)

    return second_thursday


# Import timedelta at module level for _get_second_thursday
from datetime import timedelta
