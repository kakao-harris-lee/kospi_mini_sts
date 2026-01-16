"""
Trade Analyzer Module

Analyzes trading decisions from JSONL log files for strategy performance assessment.

Usage:
    from src.analysis import TradeAnalyzer, analyze_trades

    # Quick analysis
    summary = analyze_trades("2026-01-16")
    print(summary)

    # Detailed analysis
    analyzer = TradeAnalyzer()
    analyzer.load_date_range("2026-01-01", "2026-01-17")
    df = analyzer.get_trades_df()
    stats = analyzer.get_summary()
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    """Represents a complete trade (entry + close)."""
    trade_id: str
    entry_time: datetime
    exit_time: Optional[datetime]
    side: str  # LONG or SHORT
    entry_price: float
    exit_price: Optional[float]
    pnl_points: Optional[float]
    hold_time_minutes: Optional[int]
    mode: str
    entry_reason: str
    exit_reason: Optional[str]
    strategy: str
    session_id: str

    # Additional context from entry
    up_prob_h1: float = 0.5
    up_prob_h10: float = 0.5
    ofi_zscore: float = 0.0
    atr: float = 0.0


class TradeAnalyzer:
    """
    Analyzes trading decisions from JSONL log files.

    Loads decision logs, matches entry/close pairs, and calculates
    performance metrics.
    """

    def __init__(self, log_dir: str = "logs/decisions"):
        """
        Initialize analyzer.

        Args:
            log_dir: Directory containing decision JSONL files
        """
        self.log_dir = Path(log_dir)
        self.decisions: List[Dict[str, Any]] = []
        self.trades: List[Trade] = []

    def load_file(self, file_path: str) -> int:
        """
        Load decisions from a single JSONL file.

        Args:
            file_path: Path to JSONL file

        Returns:
            Number of decisions loaded
        """
        path = Path(file_path)
        if not path.exists():
            logger.warning(f"File not found: {path}")
            return 0

        count = 0
        with open(path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    decision = json.loads(line)
                    self.decisions.append(decision)
                    count += 1
                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid JSON: {e}")

        logger.info(f"Loaded {count} decisions from {path.name}")
        return count

    def load_date(self, date_str: str) -> int:
        """
        Load decisions for a specific date.

        Args:
            date_str: Date in YYYY-MM-DD format

        Returns:
            Number of decisions loaded
        """
        file_path = self.log_dir / f"{date_str}.jsonl"
        return self.load_file(str(file_path))

    def load_date_range(self, start_date: str, end_date: str) -> int:
        """
        Load decisions for a date range.

        Args:
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format (inclusive)

        Returns:
            Total number of decisions loaded
        """
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()

        total = 0
        current = start
        while current <= end:
            total += self.load_date(current.strftime("%Y-%m-%d"))
            current = date(current.year, current.month, current.day + 1) if current.day < 28 else \
                      date(current.year, current.month + 1, 1) if current.month < 12 else \
                      date(current.year + 1, 1, 1)
            # Simple date increment (handles month/year boundaries)
            try:
                current = date(current.year, current.month, current.day)
            except ValueError:
                break

        return total

    def load_recent_days(self, days: int = 7) -> int:
        """
        Load decisions for the last N days.

        Args:
            days: Number of days to load

        Returns:
            Total number of decisions loaded
        """
        total = 0
        today = datetime.now().date()
        for i in range(days):
            d = today - pd.Timedelta(days=i)
            total += self.load_date(d.strftime("%Y-%m-%d"))
        return total

    def _match_trades(self) -> List[Trade]:
        """
        Match entry and close decisions into complete trades.

        Returns:
            List of matched Trade objects
        """
        trades = []
        entries: Dict[str, Dict] = {}  # trade_id -> entry decision
        unmatched_entries: List[Dict] = []  # entries without trade_id

        # Sort decisions by timestamp
        sorted_decisions = sorted(
            self.decisions,
            key=lambda d: d.get('timestamp', '')
        )

        for decision in sorted_decisions:
            signal = decision.get('signal', '')
            trade_id = decision.get('trade_id', '')

            if signal in ('BUY', 'SELL'):
                # Entry signal
                if trade_id:
                    entries[trade_id] = decision
                else:
                    unmatched_entries.append(decision)

            elif signal == 'CLOSE':
                # Try to match by trade_id first
                entry = None
                if trade_id and trade_id in entries:
                    entry = entries.pop(trade_id)
                elif unmatched_entries:
                    # Fall back to most recent unmatched entry
                    entry = unmatched_entries.pop()

                if entry:
                    trade = self._create_trade(entry, decision)
                    trades.append(trade)
                else:
                    # Close without matching entry - create partial trade
                    trade = self._create_trade_from_close(decision)
                    if trade:
                        trades.append(trade)

        # Add orphan entries (no close found)
        for entry in list(entries.values()) + unmatched_entries:
            trade = self._create_trade_from_entry(entry)
            trades.append(trade)

        self.trades = trades
        return trades

    def _create_trade(self, entry: Dict, close: Dict) -> Trade:
        """Create Trade from matched entry and close decisions."""
        entry_signal = entry.get('signal', '')
        side = 'LONG' if entry_signal == 'BUY' else 'SHORT'

        entry_time = self._parse_timestamp(entry.get('timestamp'))
        exit_time = self._parse_timestamp(close.get('timestamp'))

        # Calculate PnL if not provided
        pnl = close.get('pnl_points')
        if pnl is None and entry.get('entry_price') and close.get('exit_price'):
            entry_price = entry.get('entry_price') or entry.get('price', 0)
            exit_price = close.get('exit_price') or close.get('price', 0)
            if side == 'LONG':
                pnl = exit_price - entry_price
            else:
                pnl = entry_price - exit_price

        # Calculate hold time if not provided
        hold_time = close.get('hold_time_minutes')
        if hold_time is None and entry_time and exit_time:
            hold_time = int((exit_time - entry_time).total_seconds() / 60)

        return Trade(
            trade_id=entry.get('trade_id', '') or close.get('trade_id', ''),
            entry_time=entry_time,
            exit_time=exit_time,
            side=side,
            entry_price=entry.get('entry_price') or entry.get('price', 0),
            exit_price=close.get('exit_price') or close.get('price'),
            pnl_points=pnl,
            hold_time_minutes=hold_time,
            mode=entry.get('mode', 'unknown'),
            entry_reason=entry.get('reason', ''),
            exit_reason=close.get('reason', ''),
            strategy=entry.get('strategy', 'unknown'),
            session_id=entry.get('session_id', ''),
            up_prob_h1=entry.get('up_prob_h1', 0.5),
            up_prob_h10=entry.get('up_prob_h10', 0.5),
            ofi_zscore=entry.get('ofi_zscore', 0.0),
            atr=entry.get('atr', 0.0),
        )

    def _create_trade_from_entry(self, entry: Dict) -> Trade:
        """Create Trade from entry-only decision (no close found)."""
        entry_signal = entry.get('signal', '')
        side = 'LONG' if entry_signal == 'BUY' else 'SHORT'

        return Trade(
            trade_id=entry.get('trade_id', ''),
            entry_time=self._parse_timestamp(entry.get('timestamp')),
            exit_time=None,
            side=side,
            entry_price=entry.get('entry_price') or entry.get('price', 0),
            exit_price=None,
            pnl_points=None,
            hold_time_minutes=None,
            mode=entry.get('mode', 'unknown'),
            entry_reason=entry.get('reason', ''),
            exit_reason=None,
            strategy=entry.get('strategy', 'unknown'),
            session_id=entry.get('session_id', ''),
            up_prob_h1=entry.get('up_prob_h1', 0.5),
            up_prob_h10=entry.get('up_prob_h10', 0.5),
            ofi_zscore=entry.get('ofi_zscore', 0.0),
            atr=entry.get('atr', 0.0),
        )

    def _create_trade_from_close(self, close: Dict) -> Optional[Trade]:
        """Create Trade from close-only decision (no entry found)."""
        if not close.get('entry_price'):
            return None

        # Infer side from PnL and prices
        entry_price = close.get('entry_price', 0)
        exit_price = close.get('exit_price') or close.get('price', 0)
        pnl = close.get('pnl_points', 0)

        if pnl > 0:
            # Profitable: LONG if exit > entry, else SHORT
            side = 'LONG' if exit_price > entry_price else 'SHORT'
        elif pnl < 0:
            side = 'LONG' if exit_price < entry_price else 'SHORT'
        else:
            side = 'UNKNOWN'

        return Trade(
            trade_id=close.get('trade_id', ''),
            entry_time=None,
            exit_time=self._parse_timestamp(close.get('timestamp')),
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_points=pnl,
            hold_time_minutes=close.get('hold_time_minutes'),
            mode=close.get('mode', 'unknown'),
            entry_reason='',
            exit_reason=close.get('reason', ''),
            strategy=close.get('strategy', 'unknown'),
            session_id=close.get('session_id', ''),
        )

    def _parse_timestamp(self, ts: Optional[str]) -> Optional[datetime]:
        """Parse ISO format timestamp."""
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return None

    def get_trades_df(self) -> pd.DataFrame:
        """
        Get all trades as a DataFrame.

        Returns:
            DataFrame with trade details
        """
        if not self.trades:
            self._match_trades()

        data = []
        for t in self.trades:
            data.append({
                'trade_id': t.trade_id,
                'entry_time': t.entry_time,
                'exit_time': t.exit_time,
                'side': t.side,
                'entry_price': t.entry_price,
                'exit_price': t.exit_price,
                'pnl_points': t.pnl_points,
                'hold_time_min': t.hold_time_minutes,
                'mode': t.mode,
                'entry_reason': t.entry_reason,
                'exit_reason': t.exit_reason,
                'strategy': t.strategy,
                'session_id': t.session_id,
                'up_prob_h1': t.up_prob_h1,
                'up_prob_h10': t.up_prob_h10,
                'ofi_zscore': t.ofi_zscore,
                'atr': t.atr,
            })

        return pd.DataFrame(data)

    def get_summary(self) -> Dict[str, Any]:
        """
        Get summary statistics.

        Returns:
            Dictionary with performance metrics
        """
        if not self.trades:
            self._match_trades()

        df = self.get_trades_df()

        if df.empty:
            return {
                'total_trades': 0,
                'message': 'No trades found',
            }

        # Filter completed trades (have PnL)
        completed = df[df['pnl_points'].notna()]
        incomplete = df[df['pnl_points'].isna()]

        if completed.empty:
            return {
                'total_trades': len(df),
                'completed_trades': 0,
                'incomplete_trades': len(incomplete),
                'message': 'No completed trades with PnL data',
            }

        # Basic stats
        total_pnl = completed['pnl_points'].sum()
        wins = completed[completed['pnl_points'] > 0]
        losses = completed[completed['pnl_points'] < 0]
        win_rate = len(wins) / len(completed) * 100 if len(completed) > 0 else 0

        # Profit factor
        gross_profit = wins['pnl_points'].sum() if len(wins) > 0 else 0
        gross_loss = abs(losses['pnl_points'].sum()) if len(losses) > 0 else 0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        # Hold time stats
        hold_times = completed['hold_time_min'].dropna()
        avg_hold = hold_times.mean() if len(hold_times) > 0 else 0

        # By mode
        mode_stats = {}
        for mode in completed['mode'].unique():
            mode_df = completed[completed['mode'] == mode]
            mode_wins = mode_df[mode_df['pnl_points'] > 0]
            mode_stats[mode] = {
                'trades': len(mode_df),
                'win_rate': len(mode_wins) / len(mode_df) * 100 if len(mode_df) > 0 else 0,
                'total_pnl': mode_df['pnl_points'].sum(),
                'avg_pnl': mode_df['pnl_points'].mean(),
            }

        # By side
        side_stats = {}
        for side in completed['side'].unique():
            side_df = completed[completed['side'] == side]
            side_wins = side_df[side_df['pnl_points'] > 0]
            side_stats[side] = {
                'trades': len(side_df),
                'win_rate': len(side_wins) / len(side_df) * 100 if len(side_df) > 0 else 0,
                'total_pnl': side_df['pnl_points'].sum(),
            }

        return {
            'total_trades': len(df),
            'completed_trades': len(completed),
            'incomplete_trades': len(incomplete),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': round(win_rate, 1),
            'total_pnl_points': round(total_pnl, 2),
            'total_pnl_krw': round(total_pnl * 50000, 0),  # Point value
            'avg_pnl_points': round(completed['pnl_points'].mean(), 2),
            'best_trade': round(completed['pnl_points'].max(), 2),
            'worst_trade': round(completed['pnl_points'].min(), 2),
            'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else 'inf',
            'avg_hold_time_min': round(avg_hold, 1),
            'by_mode': mode_stats,
            'by_side': side_stats,
        }

    def print_summary(self) -> None:
        """Print formatted summary to console."""
        stats = self.get_summary()

        print("\n" + "=" * 50)
        print("TRADE ANALYSIS SUMMARY")
        print("=" * 50)

        print(f"\nTotal Trades: {stats['total_trades']}")
        print(f"  Completed: {stats.get('completed_trades', 0)}")
        print(f"  Incomplete: {stats.get('incomplete_trades', 0)}")

        if stats.get('completed_trades', 0) > 0:
            print(f"\nPerformance:")
            print(f"  Win Rate: {stats['win_rate']}%")
            print(f"  Wins: {stats['wins']} / Losses: {stats['losses']}")
            print(f"  Profit Factor: {stats['profit_factor']}")

            print(f"\nPnL:")
            print(f"  Total: {stats['total_pnl_points']} pts ({stats['total_pnl_krw']:,.0f} KRW)")
            print(f"  Average: {stats['avg_pnl_points']} pts/trade")
            print(f"  Best: {stats['best_trade']} pts")
            print(f"  Worst: {stats['worst_trade']} pts")

            print(f"\nHold Time: {stats['avg_hold_time_min']} min (avg)")

            if stats.get('by_mode'):
                print(f"\nBy Mode:")
                for mode, mode_data in stats['by_mode'].items():
                    print(f"  {mode}: {mode_data['trades']} trades, "
                          f"{mode_data['win_rate']:.1f}% WR, "
                          f"{mode_data['total_pnl']:.2f} pts")

            if stats.get('by_side'):
                print(f"\nBy Side:")
                for side, side_data in stats['by_side'].items():
                    print(f"  {side}: {side_data['trades']} trades, "
                          f"{side_data['win_rate']:.1f}% WR, "
                          f"{side_data['total_pnl']:.2f} pts")

        print("\n" + "=" * 50)


# Convenience functions
def load_decisions(date_str: str, log_dir: str = "logs/decisions") -> List[Dict]:
    """Load decisions for a specific date."""
    analyzer = TradeAnalyzer(log_dir)
    analyzer.load_date(date_str)
    return analyzer.decisions


def analyze_trades(
    date_str: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    days: int = 7,
    log_dir: str = "logs/decisions",
) -> Dict[str, Any]:
    """
    Quick analysis of trading performance.

    Args:
        date_str: Single date to analyze (YYYY-MM-DD)
        start_date: Start of date range
        end_date: End of date range
        days: Number of recent days (if no dates specified)
        log_dir: Directory containing decision logs

    Returns:
        Summary statistics dictionary
    """
    analyzer = TradeAnalyzer(log_dir)

    if date_str:
        analyzer.load_date(date_str)
    elif start_date and end_date:
        analyzer.load_date_range(start_date, end_date)
    else:
        analyzer.load_recent_days(days)

    return analyzer.get_summary()
