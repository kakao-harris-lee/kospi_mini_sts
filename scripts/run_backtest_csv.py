#!/usr/bin/env python3
"""
CSV 데이터로 전략 백테스트 실행

원격 ClickHouse에서 추출한 CSV 데이터로 백테스트
"""
import os
import sys
import pandas as pd
from datetime import datetime

# 프로젝트 루트 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.backtest import (
    BacktestEngine,
    BacktestConfig,
    FeatureEngineer,
    FeatureConfig,
    StrategyAdapter,
    RiskConfig,
)
from src.strategy import (
    PureMicrostructureStrategy,
    AdaptiveMicrostructureStrategy,
    PureMicroConfig,
)


def load_csv_data(filepath: str, code_filter: str = None) -> pd.DataFrame:
    """CSV 파일에서 데이터 로드"""
    df = pd.read_csv(
        filepath,
        names=['code', 'datetime', 'open', 'high', 'low', 'close', 'volume'],
        parse_dates=['datetime']
    )

    if code_filter:
        df = df[df['code'] == code_filter].copy()

    df = df.sort_values('datetime').reset_index(drop=True)

    print(f"Loaded {len(df):,} rows")
    print(f"Code: {df['code'].iloc[0] if len(df) > 0 else 'N/A'}")
    print(f"Date range: {df['datetime'].min()} ~ {df['datetime'].max()}")
    print(f"Price range: {df['close'].min():.2f} ~ {df['close'].max():.2f}")

    return df


def run_backtest(df: pd.DataFrame, strategy_name: str = 'pure_micro') -> dict:
    """백테스트 실행"""

    # 피처 엔지니어링
    print("\n[1/3] Feature Engineering...")
    engineer = FeatureEngineer(FeatureConfig())
    df = engineer.transform(df)

    # 피처 통계 출력
    summary = engineer.get_feature_summary(df)
    print("\nFeature Summary:")
    print(f"  OFI Z-Score > 2σ: {summary['ofi_zscore']['pct_above_2']:.1f}%")
    print(f"  OFI Z-Score < -2σ: {summary['ofi_zscore']['pct_below_minus2']:.1f}%")
    print(f"  Imbalance > 0.5: {summary['bid_ask_imbalance']['pct_above_0.5']:.1f}%")
    print(f"  Imbalance < -0.5: {summary['bid_ask_imbalance']['pct_below_minus0.5']:.1f}%")
    print(f"  Regime: LOW={summary['regime']['LOW']:.1f}%, MEDIUM={summary['regime']['MEDIUM']:.1f}%, HIGH={summary['regime']['HIGH']:.1f}%")

    # 전략 설정 - 합성 데이터용 완화된 파라미터
    print(f"\n[2/3] Creating Strategy: {strategy_name}")

    config = PureMicroConfig(
        # 진입 조건 완화
        ofi_zscore_threshold=1.0,      # 2.0 → 1.0
        ofi_consecutive_bars=2,         # 3 → 2
        imbalance_threshold=0.3,        # 0.5 → 0.3
        entry_score_threshold=0.45,     # 0.6 → 0.45
        # 리스크 관리
        stop_loss_points=1.5,
        take_profit_points=3.0,
        trailing_stop_points=1.0,
        max_bars_in_position=30,
        cooldown_bars=3,
    )

    if strategy_name == 'adaptive_micro':
        strategy = AdaptiveMicrostructureStrategy(config=config)
    else:
        strategy = PureMicrostructureStrategy(config=config)

    adapter = StrategyAdapter(strategy)

    # 백테스트 설정
    # ETF/ETN 데이터의 경우 포인트 가치 조정 필요
    # 실제 KOSPI Mini: 1포인트 = 50,000원
    # ETF: 1주 단위 거래 (가정: 10,000원/단위)
    point_value = 10_000  # ETF 가정

    backtest_config = BacktestConfig(
        initial_capital=10_000_000,
        position_size=1,
        point_value=point_value,
        risk_config=RiskConfig(
            stop_loss_points=3.0,        # 변동성 대비 확대
            take_profit_points=6.0,
            time_stop_minutes=60,
            trailing_stop_points=2.0,
            max_daily_loss=50_000_000,   # 테스트용 확대
            max_daily_trades=1000,
        ),
        verbose=False,
    )

    engine = BacktestEngine(
        strategy=adapter,
        config=backtest_config,
    )

    # 백테스트 실행
    print("\n[3/3] Running Backtest...")
    result = engine.run(df)

    return result


def print_results(result):
    """결과 출력"""
    print("\n" + "=" * 60)
    print("전략 검증 결과")
    print("=" * 60)

    print(f"\n수익률")
    print(f"   초기 자본: {result.initial_capital:,.0f}원")
    print(f"   최종 자본: {result.final_capital:,.0f}원")
    print(f"   총 수익률: {result.total_return:+.2f}%")
    print(f"   총 손익: {result.total_pnl:+,.0f}원")

    print(f"\n거래 통계")
    print(f"   총 거래: {result.total_trades}회")
    print(f"   승리: {result.winning_trades}회")
    print(f"   패배: {result.losing_trades}회")
    print(f"   승률: {result.win_rate:.1f}%")

    print(f"\n수익 분석")
    print(f"   평균 수익: {result.avg_win:+,.0f}원")
    print(f"   평균 손실: {result.avg_loss:+,.0f}원")
    print(f"   Profit Factor: {result.profit_factor:.2f}")
    print(f"   최대 수익: {result.max_win:+,.0f}원")
    print(f"   최대 손실: {result.max_loss:+,.0f}원")

    print(f"\n리스크 지표")
    print(f"   최대 낙폭: {result.max_drawdown:.2f}%")
    print(f"   Sharpe Ratio: {result.sharpe_ratio:.2f}")
    print(f"   Sortino Ratio: {result.sortino_ratio:.2f}")

    print(f"\n청산 사유")
    for reason, count in result.exit_reasons.items():
        print(f"   {reason}: {count}회")

    # 성공 기준 판정
    print("\n" + "=" * 60)
    print("성공 기준 검증")
    print("=" * 60)

    criteria = [
        ("승률 > 45%", result.win_rate > 45),
        ("Profit Factor > 1.0", result.profit_factor > 1.0),
        ("Max Drawdown < 15%", result.max_drawdown < 15),
    ]

    all_passed = True
    for name, passed in criteria:
        status = "PASS" if passed else "FAIL"
        print(f"   {name}: {status}")
        if not passed:
            all_passed = False

    print("\n" + "-" * 60)
    if all_passed:
        print("최소 기준 통과! 전략이 유효할 가능성이 있습니다.")
    else:
        print("일부 기준 미달. 전략 개선이 필요합니다.")

    # 목표 기준
    print("\n목표 기준:")
    advanced = [
        ("승률 > 52%", result.win_rate > 52),
        ("Profit Factor > 1.5", result.profit_factor > 1.5),
        ("Sharpe Ratio > 1.0", result.sharpe_ratio > 1.0),
    ]

    for name, passed in advanced:
        status = "O" if passed else "X"
        print(f"   [{status}] {name}")

    print("=" * 60)

    return all_passed


def main():
    import argparse

    parser = argparse.ArgumentParser(description='CSV 데이터로 전략 검증')
    parser.add_argument('--csv', type=str, default='/tmp/kospi_data.csv',
                        help='CSV 파일 경로')
    parser.add_argument('--code', type=str, default='A05601',
                        help='종목 코드 필터')
    parser.add_argument('--strategy', type=str, default='pure_micro',
                        choices=['pure_micro', 'adaptive_micro'],
                        help='전략 선택')

    args = parser.parse_args()

    print("=" * 60)
    print("전략 유효성 검증 (CSV 데이터)")
    print("=" * 60)
    print(f"Data: {args.csv}")
    print(f"Code: {args.code}")
    print(f"Strategy: {args.strategy}")

    # 데이터 로드
    df = load_csv_data(args.csv, args.code)

    if df.empty:
        print("No data available!")
        return

    # 백테스트 실행
    result = run_backtest(df, args.strategy)

    # 결과 출력
    print_results(result)


if __name__ == "__main__":
    main()
