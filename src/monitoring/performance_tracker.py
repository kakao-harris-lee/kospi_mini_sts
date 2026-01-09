"""
Performance Tracker for strategy metrics.

This module provides the PerformanceTracker class that calculates and tracks
strategy performance metrics including P&L, win rate, Sharpe ratio, and drawdown.

User Story 3: Strategy performance dashboard with key metrics

Constitution Compliance:
- I: Async-first architecture (all methods are async)
- III: Batch-only database operations (ClickHouse writes are batched)
"""

import asyncio
import logging
import math
from collections import deque
from datetime import datetime, date
from typing import Optional, Dict, List, Deque

from src.common.metrics import get_metrics
from src.monitoring.models import PerformanceMetric, TradeExecution

logger = logging.getLogger(__name__)


# Constants
RISK_FREE_RATE = 0.035  # 3.5% annual risk-free rate (Korean treasury)
TRADING_DAYS_PER_YEAR = 252


class PerformanceTracker:
    """
    Strategy performance tracking service.

    Features:
    - Real-time P&L tracking
    - Win rate calculation
    - Sharpe ratio calculation
    - Maximum drawdown tracking
    - Prometheus metrics for Grafana dashboard
    - ClickHouse batch insertion for historical data

    Usage:
        tracker = PerformanceTracker(clickhouse_client)
        await tracker.start()

        # Record a trade
        await tracker.record_trade(trade_execution)

        # Get current metrics
        metrics = await tracker.get_current_metrics("pure_micro", "A05601")

        await tracker.stop()
    """

    # Aggregation interval (seconds)
    AGGREGATION_INTERVAL = 60.0

    # Batch size for ClickHouse (Constitution III: minimum 1000 records)
    BATCH_SIZE = 1000

    # Maximum buffer size to prevent unbounded growth on ClickHouse failures
    MAX_BUFFER_SIZE = 10000

    # Rolling window size for Sharpe calculation (number of trades)
    SHARPE_WINDOW = 20

    def __init__(
        self,
        clickhouse_client=None,
        redis_client=None,
    ):
        """
        Initialize performance tracker.

        Args:
            clickhouse_client: ClickHouse client for batch insertion
            redis_client: Redis client for caching
        """
        self.clickhouse = clickhouse_client
        self.redis = redis_client
        self.metrics = get_metrics()

        # Performance data per strategy
        self._performance: Dict[str, Dict[str, PerformanceMetric]] = {}

        # Trade history for calculations (strategy -> symbol -> trades)
        self._trade_history: Dict[str, Dict[str, Deque[TradeExecution]]] = {}

        # Daily returns for Sharpe calculation
        self._daily_returns: Dict[str, Deque[float]] = {}

        # Peak equity for drawdown calculation
        self._peak_equity: Dict[str, float] = {}

        # Batch buffer for ClickHouse
        self._batch_buffer: List[PerformanceMetric] = []
        self._batch_lock = asyncio.Lock()

        # Running state
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the aggregation loop."""
        if self._running:
            logger.warning("PerformanceTracker already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._aggregation_loop())
        logger.info("PerformanceTracker started")

    async def stop(self) -> None:
        """Stop the aggregation loop and flush buffers."""
        if not self._running:
            return

        self._running = False

        # Flush batch buffer
        await self._flush_batch_buffer()

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info("PerformanceTracker stopped")

    async def _aggregation_loop(self) -> None:
        """Main aggregation loop."""
        logger.info("Performance aggregation loop started")

        while self._running:
            try:
                await self._aggregate_all_metrics()
            except Exception as e:
                logger.error(f"Error in aggregation loop: {e}")

            await asyncio.sleep(self.AGGREGATION_INTERVAL)

        logger.info("Performance aggregation loop stopped")

    async def record_trade(self, trade: TradeExecution) -> None:
        """
        Record a trade execution for performance tracking.

        Args:
            trade: Completed trade execution
        """
        strategy = trade.strategy
        symbol = trade.symbol

        # Initialize data structures if needed
        if strategy not in self._trade_history:
            self._trade_history[strategy] = {}
            self._daily_returns[strategy] = deque(maxlen=TRADING_DAYS_PER_YEAR)
            self._peak_equity[strategy] = 0.0

        if symbol not in self._trade_history[strategy]:
            self._trade_history[strategy][symbol] = deque(maxlen=1000)

        # Add trade to history
        self._trade_history[strategy][symbol].append(trade)

        # Update metrics
        await self._update_metrics(strategy, symbol)

        logger.debug(f"Recorded trade for {strategy}/{symbol}: PnL={trade.realized_pnl}")

    async def _update_metrics(self, strategy: str, symbol: str) -> None:
        """Update all metrics for a strategy/symbol pair."""
        trades = list(self._trade_history[strategy][symbol])

        if not trades:
            return

        # Initialize performance metric if needed
        if strategy not in self._performance:
            self._performance[strategy] = {}

        if symbol not in self._performance[strategy]:
            self._performance[strategy][symbol] = PerformanceMetric(
                strategy=strategy,
                symbol=symbol,
                period="minute",
            )

        metric = self._performance[strategy][symbol]

        # Calculate basic stats
        metric.trade_count = len(trades)
        metric.total_pnl = sum(t.realized_pnl for t in trades)
        metric.realized_pnl = metric.total_pnl

        # Win/Loss counts
        metric.win_count = sum(1 for t in trades if t.realized_pnl > 0)
        metric.loss_count = sum(1 for t in trades if t.realized_pnl < 0)

        # Win rate
        metric.win_rate = self._calculate_win_rate(trades)

        # Average win/loss
        metric.avg_win, metric.avg_loss = self._calculate_avg_win_loss(trades)

        # Profit factor
        metric.profit_factor = self._calculate_profit_factor(trades)

        # Max drawdown
        metric.max_drawdown = self._calculate_max_drawdown(trades, strategy)

        # Sharpe ratio
        metric.sharpe_ratio = self._calculate_sharpe_ratio(trades, strategy)

        # Update timestamp
        metric.datetime = datetime.utcnow()

        # Update Prometheus metrics
        self._update_prometheus_metrics(metric)

    def _calculate_win_rate(self, trades: List[TradeExecution]) -> float:
        """Calculate win rate from trades."""
        if not trades:
            return 0.0

        wins = sum(1 for t in trades if t.realized_pnl > 0)
        return wins / len(trades)

    def _calculate_avg_win_loss(self, trades: List[TradeExecution]) -> tuple:
        """Calculate average winning and losing trade."""
        wins = [t.realized_pnl for t in trades if t.realized_pnl > 0]
        losses = [t.realized_pnl for t in trades if t.realized_pnl < 0]

        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0

        return avg_win, avg_loss

    # Maximum profit factor value (cap to avoid infinity)
    MAX_PROFIT_FACTOR = 999.99

    def _calculate_profit_factor(self, trades: List[TradeExecution]) -> float:
        """Calculate profit factor (gross profit / gross loss)."""
        gross_profit = sum(t.realized_pnl for t in trades if t.realized_pnl > 0)
        gross_loss = abs(sum(t.realized_pnl for t in trades if t.realized_pnl < 0))

        if gross_loss == 0:
            # Cap at MAX_PROFIT_FACTOR to avoid infinity issues in Prometheus
            return self.MAX_PROFIT_FACTOR if gross_profit > 0 else 0.0

        return min(gross_profit / gross_loss, self.MAX_PROFIT_FACTOR)

    def _calculate_max_drawdown(self, trades: List[TradeExecution], strategy: str) -> float:
        """Calculate maximum drawdown from peak equity."""
        if not trades:
            return 0.0

        # Calculate cumulative equity curve
        equity = 0.0
        peak = self._peak_equity.get(strategy, 0.0)
        max_dd = 0.0

        for trade in trades:
            equity += trade.realized_pnl

            if equity > peak:
                peak = equity
                self._peak_equity[strategy] = peak

            if peak > 0:
                dd = (peak - equity) / peak
                max_dd = max(max_dd, dd)

        return max_dd

    def _calculate_sharpe_ratio(self, trades: List[TradeExecution], strategy: str) -> float:
        """
        Calculate Sharpe ratio.

        Uses trade returns (not daily returns) for simplicity.
        Annualized using sqrt(252) approximation.
        """
        if len(trades) < self.SHARPE_WINDOW:
            return 0.0

        # Get recent trade returns
        returns = [t.realized_pnl for t in trades[-self.SHARPE_WINDOW:]]

        if not returns:
            return 0.0

        # Calculate mean and std
        mean_return = sum(returns) / len(returns)
        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
        std_return = math.sqrt(variance) if variance > 0 else 0.0

        if std_return == 0:
            return 0.0

        # Annualize (rough approximation)
        # Assuming ~20 trades per day on average
        trades_per_year = 20 * TRADING_DAYS_PER_YEAR
        annualized_return = mean_return * trades_per_year
        annualized_std = std_return * math.sqrt(trades_per_year)

        sharpe = (annualized_return - RISK_FREE_RATE) / annualized_std

        return sharpe

    def _update_prometheus_metrics(self, metric: PerformanceMetric) -> None:
        """Update Prometheus gauges with current metrics."""
        self.metrics.set_strategy_total_pnl(metric.strategy, metric.symbol, metric.total_pnl)
        self.metrics.set_strategy_win_rate(metric.strategy, metric.win_rate)
        self.metrics.set_strategy_sharpe_ratio(metric.strategy, metric.sharpe_ratio)
        self.metrics.set_strategy_max_drawdown(metric.strategy, metric.max_drawdown)
        self.metrics.set_strategy_trade_count_today(metric.strategy, metric.trade_count)

    async def _aggregate_all_metrics(self) -> None:
        """Aggregate and persist all metrics."""
        for strategy, symbols in self._performance.items():
            for symbol, metric in symbols.items():
                # Add to batch buffer
                await self._add_to_batch(metric)

    async def _add_to_batch(self, metric: PerformanceMetric) -> None:
        """Add metric to batch buffer for ClickHouse insertion."""
        async with self._batch_lock:
            # Create a copy with current timestamp
            metric_copy = PerformanceMetric(
                strategy=metric.strategy,
                symbol=metric.symbol,
                period=metric.period,
                datetime=datetime.utcnow(),
                total_pnl=metric.total_pnl,
                realized_pnl=metric.realized_pnl,
                unrealized_pnl=metric.unrealized_pnl,
                trade_count=metric.trade_count,
                win_count=metric.win_count,
                loss_count=metric.loss_count,
                win_rate=metric.win_rate,
                avg_win=metric.avg_win,
                avg_loss=metric.avg_loss,
                profit_factor=min(metric.profit_factor, 999.99),  # Cap for storage
                max_drawdown=metric.max_drawdown,
                sharpe_ratio=metric.sharpe_ratio,
            )
            self._batch_buffer.append(metric_copy)

            if len(self._batch_buffer) >= self.BATCH_SIZE:
                await self._flush_batch_buffer()

    async def _flush_batch_buffer(self) -> None:
        """Flush batch buffer to ClickHouse."""
        async with self._batch_lock:
            if not self._batch_buffer:
                return

            metrics_to_flush = self._batch_buffer.copy()
            self._batch_buffer.clear()

        if not self.clickhouse:
            logger.debug(f"Would flush {len(metrics_to_flush)} performance metrics to ClickHouse")
            return

        try:
            # Convert to rows
            rows = [m.to_clickhouse_row() for m in metrics_to_flush]

            # Batch insert (runs in executor to not block)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: self.clickhouse.insert(
                    "kospi.strategy_performance",
                    rows,
                    column_names=[
                        "datetime", "strategy", "symbol", "period",
                        "total_pnl", "realized_pnl", "unrealized_pnl",
                        "trade_count", "win_count", "loss_count", "win_rate",
                        "avg_win", "avg_loss", "profit_factor",
                        "max_drawdown", "sharpe_ratio"
                    ]
                )
            )
            logger.debug(f"Flushed {len(metrics_to_flush)} performance metrics to ClickHouse")

        except Exception as e:
            logger.error(f"Failed to flush performance metrics to ClickHouse: {e}")
            # Re-add to buffer for retry, but respect MAX_BUFFER_SIZE to prevent unbounded growth
            async with self._batch_lock:
                if len(self._batch_buffer) + len(metrics_to_flush) <= self.MAX_BUFFER_SIZE:
                    self._batch_buffer.extend(metrics_to_flush)
                else:
                    # Buffer would exceed limit - discard oldest entries
                    available_space = self.MAX_BUFFER_SIZE - len(self._batch_buffer)
                    if available_space > 0:
                        self._batch_buffer.extend(metrics_to_flush[-available_space:])
                    logger.warning(
                        f"Discarded {len(metrics_to_flush) - available_space} metrics due to buffer limit"
                    )

    async def get_current_metrics(self, strategy: str, symbol: str) -> Optional[PerformanceMetric]:
        """Get current performance metrics for a strategy/symbol pair."""
        if strategy in self._performance and symbol in self._performance[strategy]:
            return self._performance[strategy][symbol]
        return None

    async def get_all_metrics(self) -> Dict[str, Dict[str, PerformanceMetric]]:
        """Get all current performance metrics."""
        return self._performance.copy()

    async def reset_daily_stats(self, strategy: str) -> None:
        """Reset daily statistics for a strategy."""
        if strategy in self._trade_history:
            for symbol in self._trade_history[strategy]:
                self._trade_history[strategy][symbol].clear()

        if strategy in self._performance:
            for symbol in self._performance[strategy]:
                metric = self._performance[strategy][symbol]
                metric.total_pnl = 0.0
                metric.realized_pnl = 0.0
                metric.trade_count = 0
                metric.win_count = 0
                metric.loss_count = 0
                metric.win_rate = 0.0

        self._peak_equity[strategy] = 0.0
        logger.info(f"Reset daily stats for strategy: {strategy}")
