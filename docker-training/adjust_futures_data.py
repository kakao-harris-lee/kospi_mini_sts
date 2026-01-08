#!/usr/bin/env python3
"""
Backward Adjustment for KOSPI Mini Futures Data

Adjusts historical futures data to maintain price continuity across contract rollovers.
Uses backward adjustment method: all past data is lifted/lowered to match current price level.

For CNN-LSTM training, this ensures the model learns from continuous price patterns
rather than artificial gaps at contract rollovers.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import argparse
from pathlib import Path


def detect_rollover_gaps(df: pd.DataFrame, gap_threshold: float = 30.0) -> pd.DataFrame:
    """
    Detect potential rollover gaps at market open times.

    KOSPI Mini futures trade 08:45-15:45 (regular) and 17:00-05:00 (night).
    Rollovers typically happen at morning session start.

    Args:
        df: DataFrame with datetime, open, close columns
        gap_threshold: Minimum gap size to consider as rollover (points)

    Returns:
        DataFrame of detected gaps with adjustment values
    """
    df = df.copy()
    df['prev_close'] = df['close'].shift(1)
    df['gap'] = df['open'] - df['prev_close']

    # Filter for morning session starts (08:58 or similar times after overnight gap)
    df['hour'] = df['datetime'].dt.hour
    df['time_gap'] = (df['datetime'] - df['datetime'].shift(1)).dt.total_seconds() / 3600

    # Look for gaps at session start (> 2 hour gap from previous bar) with significant price gap
    session_gaps = df[
        (df['time_gap'] > 2) &  # Session gap (not intraday)
        (abs(df['gap']) > gap_threshold)
    ].copy()

    return session_gaps[['datetime', 'prev_close', 'open', 'gap']]


def backward_adjust(df: pd.DataFrame, rollover_gaps: pd.DataFrame) -> pd.DataFrame:
    """
    Apply backward adjustment to futures data.

    Working from most recent to oldest, at each rollover point:
    - All data BEFORE the rollover is adjusted by the gap amount
    - This creates a continuous price series

    Args:
        df: Original price data
        rollover_gaps: DataFrame of detected rollover points

    Returns:
        Adjusted DataFrame
    """
    df = df.copy()
    price_cols = ['open', 'high', 'low', 'close']

    # Sort gaps from most recent to oldest (we work backward)
    gaps_sorted = rollover_gaps.sort_values('datetime', ascending=False)

    cumulative_adjustment = 0.0
    adjustments_applied = []

    print("\n=== Applying Backward Adjustments ===")

    for idx, gap_row in gaps_sorted.iterrows():
        gap_dt = gap_row['datetime']
        gap_value = gap_row['gap']

        # Add this gap to cumulative adjustment
        cumulative_adjustment += gap_value

        # Adjust all data BEFORE this rollover point
        mask = df['datetime'] < gap_dt
        df.loc[mask, price_cols] += gap_value

        adjustments_applied.append({
            'datetime': gap_dt,
            'gap': gap_value,
            'cumulative': cumulative_adjustment,
            'rows_adjusted': mask.sum()
        })

        print(f"  {gap_dt}: gap={gap_value:+.2f}, cumulative={cumulative_adjustment:+.2f}, "
              f"adjusted {mask.sum()} rows")

    return df, adjustments_applied


def create_adjusted_data(
    input_file: str,
    output_file: str = None,
    gap_threshold: float = 30.0,
    manual_adjustments: list = None
):
    """
    Main function to create backward-adjusted futures data.

    Args:
        input_file: Path to input CSV
        output_file: Path for output CSV (auto-generated if None)
        gap_threshold: Minimum gap to detect as rollover
        manual_adjustments: List of (datetime_str, adjustment_value) tuples for manual override
    """
    print(f"Loading data from: {input_file}")
    df = pd.read_csv(input_file)
    df['datetime'] = pd.to_datetime(df['datetime'])

    print(f"\n=== Original Data Summary ===")
    print(f"Date range: {df['datetime'].min()} to {df['datetime'].max()}")
    print(f"Price range: {df['close'].min():.2f} to {df['close'].max():.2f}")
    print(f"Total bars: {len(df)}")

    # Detect rollover gaps
    detected_gaps = detect_rollover_gaps(df, gap_threshold)

    print(f"\n=== Detected Rollover Gaps (threshold > {gap_threshold} points) ===")
    if len(detected_gaps) > 0:
        print(detected_gaps.to_string())
    else:
        print("No significant gaps detected")

    # Apply manual adjustments if provided
    if manual_adjustments:
        manual_df = pd.DataFrame(manual_adjustments, columns=['datetime', 'gap'])
        manual_df['datetime'] = pd.to_datetime(manual_df['datetime'])
        manual_df['prev_close'] = np.nan
        manual_df['open'] = np.nan
        detected_gaps = manual_df
        print(f"\n=== Using Manual Adjustments ===")
        print(detected_gaps.to_string())

    # Apply backward adjustment
    df_adjusted, adjustments = backward_adjust(df, detected_gaps)

    # Summary
    print(f"\n=== Adjusted Data Summary ===")
    print(f"Date range: {df_adjusted['datetime'].min()} to {df_adjusted['datetime'].max()}")
    print(f"Price range: {df_adjusted['close'].min():.2f} to {df_adjusted['close'].max():.2f}")
    print(f"Total adjustments applied: {len(adjustments)}")

    total_adjustment = sum(a['gap'] for a in adjustments) if adjustments else 0
    print(f"Total price shift (oldest data): {total_adjustment:+.2f}")

    # Save output
    if output_file is None:
        stem = Path(input_file).stem
        output_file = str(Path(input_file).parent / f"{stem}_adjusted.csv")

    # Clean up helper columns if present
    cols_to_drop = ['prev_close', 'gap', 'hour', 'time_gap']
    for col in cols_to_drop:
        if col in df_adjusted.columns:
            df_adjusted = df_adjusted.drop(columns=[col])

    df_adjusted.to_csv(output_file, index=False)
    print(f"\nSaved adjusted data to: {output_file}")

    return df_adjusted, adjustments


def verify_continuity(df: pd.DataFrame, max_normal_gap: float = 20.0):
    """
    Verify there are no remaining large gaps after adjustment.
    """
    df = df.copy()
    df['prev_close'] = df['close'].shift(1)
    df['gap'] = df['open'] - df['prev_close']
    df['time_gap'] = (df['datetime'] - df['datetime'].shift(1)).dt.total_seconds() / 3600

    # Check for remaining large overnight gaps
    remaining_gaps = df[(df['time_gap'] > 2) & (abs(df['gap']) > max_normal_gap)]

    print(f"\n=== Continuity Check (overnight gaps > {max_normal_gap}) ===")
    if len(remaining_gaps) > 0:
        print(f"WARNING: {len(remaining_gaps)} large gaps remain:")
        print(remaining_gaps[['datetime', 'prev_close', 'open', 'gap']].to_string())
    else:
        print("PASS: No large overnight gaps detected")

    return remaining_gaps


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backward adjust futures data for continuous series")
    parser.add_argument("input", help="Input CSV file")
    parser.add_argument("-o", "--output", help="Output CSV file (default: auto-generated)")
    parser.add_argument("-t", "--threshold", type=float, default=30.0,
                       help="Gap threshold for rollover detection (default: 30)")
    parser.add_argument("--verify", action="store_true", help="Verify continuity after adjustment")

    args = parser.parse_args()

    df_adjusted, _ = create_adjusted_data(
        args.input,
        args.output,
        args.threshold
    )

    if args.verify:
        verify_continuity(df_adjusted)
