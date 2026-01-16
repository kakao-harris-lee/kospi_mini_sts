"""
Apply ratio-based roll adjustment to futures data.

Detects contract roll points by identifying abnormal price gaps,
then applies a cumulative ratio adjustment to maintain return continuity.

This ensures:
- Percentage returns (log returns) are preserved
- No artificial jumps in price series
- ML models trained on returns see consistent data
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


def detect_roll_points(
    df: pd.DataFrame,
    gap_threshold: float = 0.02,
    min_gap_abs: float = 1.0,
) -> list[int]:
    """
    Detect contract roll points by finding abnormal price gaps.

    Roll points typically occur at market open when the front month
    contract changes. We detect these by looking for price gaps that:
    1. Exceed a percentage threshold (default 2%)
    2. Exceed a minimum absolute threshold

    Args:
        df: DataFrame with 'close' column
        gap_threshold: Minimum percentage gap to consider as roll (0.02 = 2%)
        min_gap_abs: Minimum absolute price gap

    Returns:
        List of indices where rolls were detected
    """
    if len(df) < 2:
        return []

    closes = df['close'].values
    roll_indices = []

    for i in range(1, len(closes)):
        prev_close = closes[i - 1]
        curr_close = closes[i]

        if prev_close == 0:
            continue

        # Calculate percentage gap
        pct_gap = abs(curr_close - prev_close) / prev_close
        abs_gap = abs(curr_close - prev_close)

        # Detect roll if gap exceeds thresholds
        if pct_gap >= gap_threshold and abs_gap >= min_gap_abs:
            roll_indices.append(i)
            print(f"  Roll detected at index {i}: "
                  f"{prev_close:.2f} -> {curr_close:.2f} "
                  f"(gap: {pct_gap*100:.2f}%, {abs_gap:.2f} pts)")

    return roll_indices


def apply_ratio_adjustment(
    df: pd.DataFrame,
    roll_indices: list[int],
) -> pd.DataFrame:
    """
    Apply ratio-based adjustment for detected rolls.

    For each roll point, we calculate:
        ratio = price_after_roll / price_before_roll

    Then multiply all SUBSEQUENT prices by the inverse ratio to
    bring the new contract's prices in line with the old contract.

    This preserves returns: log(P2/P1) remains the same.

    Args:
        df: DataFrame with OHLC columns
        roll_indices: List of indices where rolls were detected

    Returns:
        DataFrame with adjusted prices
    """
    df = df.copy()

    price_cols = ['open', 'high', 'low', 'close']
    adjustment_factor = 1.0

    # Process rolls in reverse order (newest to oldest)
    # This way we adjust all data after each roll point
    for roll_idx in sorted(roll_indices, reverse=True):
        if roll_idx < 1 or roll_idx >= len(df):
            continue

        # Price before roll (last bar of old contract)
        price_before = df.iloc[roll_idx - 1]['close']
        # Price after roll (first bar of new contract)
        price_after = df.iloc[roll_idx]['close']

        if price_before == 0:
            continue

        # Ratio to adjust old prices to match new contract level
        ratio = price_after / price_before

        print(f"  Applying ratio {ratio:.6f} at index {roll_idx}")

        # Adjust all prices BEFORE the roll point
        for col in price_cols:
            df.loc[df.index[:roll_idx], col] = df.loc[df.index[:roll_idx], col] * ratio

    return df


def validate_adjustment(
    df_raw: pd.DataFrame,
    df_adjusted: pd.DataFrame,
    roll_indices: list[int],
) -> dict:
    """
    Validate that the adjustment preserved returns correctly.

    Returns at roll points are expected to differ (the gap is intentionally removed),
    so we exclude those indices from validation.

    Args:
        df_raw: Original DataFrame
        df_adjusted: Adjusted DataFrame
        roll_indices: Indices where rolls were detected (to exclude from validation)

    Returns:
        Dictionary with validation metrics
    """
    # Calculate log returns
    raw_returns = np.log(df_raw['close'] / df_raw['close'].shift(1)).dropna()
    adj_returns = np.log(df_adjusted['close'] / df_adjusted['close'].shift(1)).dropna()

    # Create mask to exclude roll point returns from validation
    # Roll indices are in df index space; returns start at index 1 (shift drops first)
    # So roll_idx in returns corresponds to the return AT that index
    valid_mask = np.ones(len(raw_returns), dtype=bool)
    for roll_idx in roll_indices:
        # Return at roll_idx is log(close[roll_idx] / close[roll_idx-1])
        # This is at position (roll_idx - 1) in the returns array (due to dropna)
        return_pos = roll_idx - 1
        if 0 <= return_pos < len(valid_mask):
            valid_mask[return_pos] = False

    # Compare only non-roll returns (should be nearly identical)
    raw_valid = raw_returns.values[valid_mask]
    adj_valid = adj_returns.values[valid_mask]

    if len(raw_valid) == 0:
        return {
            'max_return_difference': 0.0,
            'mean_return_difference': 0.0,
            'returns_preserved': True,
            'excluded_roll_points': len(roll_indices),
        }

    return_diff = np.abs(raw_valid - adj_valid)
    max_diff = np.max(return_diff)
    mean_diff = np.mean(return_diff)

    return {
        'max_return_difference': float(max_diff),
        'mean_return_difference': float(mean_diff),
        'returns_preserved': bool(max_diff < 0.0001),  # Strict: only floating point error allowed
        'excluded_roll_points': len(roll_indices),
    }


def adjust_rolls(
    input_path: str,
    output_path: str,
    gap_threshold: float = 0.02,
    min_gap_abs: float = 1.0,
    save_metadata: bool = True,
) -> pd.DataFrame:
    """
    Main function to apply roll adjustment to futures data.

    Args:
        input_path: Path to input CSV file
        output_path: Path to output adjusted CSV file
        gap_threshold: Percentage threshold for roll detection
        min_gap_abs: Minimum absolute gap for roll detection
        save_metadata: Whether to save adjustment metadata JSON

    Returns:
        Adjusted DataFrame
    """
    print(f"\n=== Roll Adjustment ===")
    print(f"Input: {input_path}")
    print(f"Gap threshold: {gap_threshold*100:.1f}%")

    # Load data
    df = pd.read_csv(input_path, parse_dates=['datetime'])
    df = df.sort_values('datetime').reset_index(drop=True)
    print(f"Loaded {len(df)} rows")
    print(f"Date range: {df['datetime'].min()} to {df['datetime'].max()}")

    # Detect roll points
    print(f"\nDetecting roll points...")
    roll_indices = detect_roll_points(df, gap_threshold, min_gap_abs)
    print(f"Found {len(roll_indices)} roll point(s)")

    if not roll_indices:
        print("No rolls detected - copying data as-is")
        df_adjusted = df.copy()
    else:
        # Apply adjustment
        print(f"\nApplying ratio adjustment...")
        df_adjusted = apply_ratio_adjustment(df, roll_indices)

    # Validate (excluding roll point returns which are expected to differ)
    print(f"\nValidating adjustment...")
    validation = validate_adjustment(df, df_adjusted, roll_indices)
    print(f"Max return difference (excl. rolls): {validation['max_return_difference']:.8f}")
    print(f"Excluded roll points: {validation.get('excluded_roll_points', 0)}")
    print(f"Returns preserved: {validation['returns_preserved']}")

    if not validation['returns_preserved']:
        raise ValueError(
            f"Roll adjustment validation failed: returns not preserved. "
            f"Max return difference (excluding roll points): {validation['max_return_difference']:.8f}. "
            f"Expected < 0.0001 (floating point precision). "
            f"This may indicate a bug in the adjustment logic or corrupted data."
        )

    # Save adjusted data
    output_dir = Path(output_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)

    df_adjusted.to_csv(output_path, index=False)
    print(f"\nSaved adjusted data to: {output_path}")

    # Save metadata
    if save_metadata:
        meta_path = Path(output_path).with_suffix('.meta.json')
        metadata = {
            'input_file': str(input_path),
            'output_file': str(output_path),
            'rows': len(df_adjusted),
            'date_range': {
                'start': str(df_adjusted['datetime'].min()),
                'end': str(df_adjusted['datetime'].max()),
            },
            'rolls_detected': len(roll_indices),
            'roll_indices': roll_indices,
            'gap_threshold': gap_threshold,
            'min_gap_abs': min_gap_abs,
            'validation': validation,
            'created_at': datetime.now().isoformat(),
        }
        with open(meta_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        print(f"Saved metadata to: {meta_path}")

    return df_adjusted


def main():
    parser = argparse.ArgumentParser(
        description="Apply ratio-based roll adjustment to futures data"
    )
    parser.add_argument(
        "--input",
        type=str,
        required=True,
        help="Input CSV file path (raw data)"
    )
    parser.add_argument(
        "--output",
        type=str,
        required=True,
        help="Output CSV file path (adjusted data)"
    )
    parser.add_argument(
        "--gap-threshold",
        type=float,
        default=0.02,
        help="Percentage threshold for roll detection (default: 0.02 = 2%%)"
    )
    parser.add_argument(
        "--min-gap-abs",
        type=float,
        default=1.0,
        help="Minimum absolute price gap (default: 1.0)"
    )
    parser.add_argument(
        "--no-metadata",
        action="store_true",
        help="Don't save metadata JSON file"
    )

    args = parser.parse_args()

    df = adjust_rolls(
        input_path=args.input,
        output_path=args.output,
        gap_threshold=args.gap_threshold,
        min_gap_abs=args.min_gap_abs,
        save_metadata=not args.no_metadata,
    )

    print(f"\n=== Adjustment Complete ===")
    print(f"Output: {args.output}")
    print(f"Rows: {len(df)}")


if __name__ == "__main__":
    main()
