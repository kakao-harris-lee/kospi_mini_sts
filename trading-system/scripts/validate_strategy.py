#!/usr/bin/env python3
"""
전략 유효성 검증 스크립트

PureMicrostructureStrategy를 합성 마이크로스트럭처 데이터로 백테스트

사용법:
    python scripts/validate_strategy.py
    python scripts/validate_strategy.py --days 90
    python scripts/validate_strategy.py --strategy adaptive_micro
"""
import os
import sys
import argparse
from datetime import datetime, timedelta
import pandas as pd

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


def load_data_from_clickhouse(days: int = 90) -> pd.DataFrame:
    """ClickHouse에서 데이터 로드"""
    try:
        import clickhouse_connect

        # 환경변수에서 설정 로드
        from dotenv import load_dotenv
        load_dotenv()

        host = os.getenv('CLICKHOUSE_HOST', 'localhost')
        port = int(os.getenv('CLICKHOUSE_PORT', '8123'))
        database = os.getenv('CLICKHOUSE_DATABASE', 'kospi')
        user = os.getenv('CLICKHOUSE_USER', 'default')
        password = os.getenv('CLICKHOUSE_PASSWORD', '')

        client = clickhouse_connect.get_client(
            host=host,
            port=port,
            database=database,
            username=user,
            password=password or None,
        )

        # 최근 N일 데이터 조회
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        query = f"""
            SELECT
                code,
                datetime,
                open,
                high,
                low,
                close,
                volume
            FROM kospi_mini_1m
            WHERE datetime >= '{start_date.strftime('%Y-%m-%d')}'
              AND datetime <= '{end_date.strftime('%Y-%m-%d')}'
            ORDER BY datetime
        """

        result = client.query(query)

        df = pd.DataFrame(
            result.result_rows,
            columns=['code', 'datetime', 'open', 'high', 'low', 'close', 'volume']
        )

        print(f"Loaded {len(df):,} rows from ClickHouse")
        print(f"Date range: {df['datetime'].min()} ~ {df['datetime'].max()}")

        return df

    except Exception as e:
        print(f"Failed to load from ClickHouse: {e}")
        print("Using sample data instead...")
        return generate_sample_data(days)


def generate_sample_data(days: int = 30) -> pd.DataFrame:
    """샘플 데이터 생성 (ClickHouse 불가 시)"""
    import numpy as np

    np.random.seed(42)

    # 거래일 기준 분당 데이터
    trading_minutes_per_day = 375  # 09:00 ~ 15:15
    total_rows = days * trading_minutes_per_day

    # 기본 가격
    base_price = 350.0
    prices = [base_price]

    for _ in range(total_rows - 1):
        change = np.random.randn() * 0.1  # 변동성
        new_price = prices[-1] + change
        prices.append(max(300, min(400, new_price)))  # 범위 제한

    prices = np.array(prices)

    # OHLCV 생성
    data = []
    start_date = datetime.now() - timedelta(days=days)

    for i in range(total_rows):
        day_offset = i // trading_minutes_per_day
        minute_offset = i % trading_minutes_per_day

        dt = start_date + timedelta(days=day_offset, minutes=minute_offset + 540)  # 09:00 시작

        close = prices[i]
        volatility = np.random.uniform(0.05, 0.2)
        open_price = close * (1 + np.random.randn() * 0.001)
        high = max(open_price, close) * (1 + volatility * np.random.rand())
        low = min(open_price, close) * (1 - volatility * np.random.rand())
        volume = int(np.random.exponential(100) + 10)

        data.append({
            'code': '101F26',
            'datetime': dt,
            'open': open_price,
            'high': high,
            'low': low,
            'close': close,
            'volume': volume,
        })

    return pd.DataFrame(data)


def run_backtest(
    df: pd.DataFrame,
    strategy_name: str = 'pure_micro',
    initial_capital: float = 10_000_000,
) -> dict:
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

    # 전략 선택
    print(f"\n[2/3] Creating Strategy: {strategy_name}")

    # 합성 데이터용 완화된 파라미터
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
    config = BacktestConfig(
        initial_capital=initial_capital,
        position_size=1,
        point_value=50_000,  # KOSPI Mini
        risk_config=RiskConfig(
            stop_loss_points=1.5,
            take_profit_points=3.0,
            time_stop_minutes=30,
            trailing_stop_points=1.0,
            max_daily_loss=5_000_000,  # 일일 최대 손실 높임 (테스트용)
            max_daily_trades=100,
        ),
        verbose=False,
    )

    engine = BacktestEngine(
        strategy=adapter,
        config=config,
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

    print(f"\n📊 수익률")
    print(f"   초기 자본: {result.initial_capital:,.0f}원")
    print(f"   최종 자본: {result.final_capital:,.0f}원")
    print(f"   총 수익률: {result.total_return:+.2f}%")
    print(f"   총 손익: {result.total_pnl:+,.0f}원")

    print(f"\n📈 거래 통계")
    print(f"   총 거래: {result.total_trades}회")
    print(f"   승리: {result.winning_trades}회")
    print(f"   패배: {result.losing_trades}회")
    print(f"   승률: {result.win_rate:.1f}%")

    print(f"\n💰 수익 분석")
    print(f"   평균 수익: {result.avg_win:+,.0f}원")
    print(f"   평균 손실: {result.avg_loss:+,.0f}원")
    print(f"   Profit Factor: {result.profit_factor:.2f}")
    print(f"   최대 수익: {result.max_win:+,.0f}원")
    print(f"   최대 손실: {result.max_loss:+,.0f}원")

    print(f"\n⚠️ 리스크 지표")
    print(f"   최대 낙폭: {result.max_drawdown:.2f}%")
    print(f"   Sharpe Ratio: {result.sharpe_ratio:.2f}")
    print(f"   Sortino Ratio: {result.sortino_ratio:.2f}")

    print(f"\n🚪 청산 사유")
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
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"   {name}: {status}")
        if not passed:
            all_passed = False

    print("\n" + "-" * 60)
    if all_passed:
        print("🎉 최소 기준 통과! 전략이 유효할 가능성이 있습니다.")
    else:
        print("⚠️ 일부 기준 미달. 전략 개선이 필요합니다.")

    # 목표 기준
    print("\n목표 기준:")
    advanced = [
        ("승률 > 52%", result.win_rate > 52),
        ("Profit Factor > 1.5", result.profit_factor > 1.5),
        ("Sharpe Ratio > 1.0", result.sharpe_ratio > 1.0),
    ]

    for name, passed in advanced:
        status = "✅" if passed else "❌"
        print(f"   {status} {name}")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description='전략 유효성 검증')
    parser.add_argument('--days', type=int, default=90, help='백테스트 기간 (일)')
    parser.add_argument('--strategy', type=str, default='pure_micro',
                        choices=['pure_micro', 'adaptive_micro'],
                        help='전략 선택')
    parser.add_argument('--capital', type=float, default=10_000_000,
                        help='초기 자본금')
    parser.add_argument('--sample', action='store_true',
                        help='샘플 데이터 사용 (DB 연결 안함)')

    args = parser.parse_args()

    print("=" * 60)
    print("KOSPI Mini 전략 유효성 검증")
    print("=" * 60)
    print(f"Strategy: {args.strategy}")
    print(f"Period: {args.days} days")
    print(f"Capital: {args.capital:,.0f}원")

    # 데이터 로드
    if args.sample:
        df = generate_sample_data(args.days)
    else:
        df = load_data_from_clickhouse(args.days)

    if df.empty:
        print("No data available!")
        return

    # 백테스트 실행
    result = run_backtest(
        df=df,
        strategy_name=args.strategy,
        initial_capital=args.capital,
    )

    # 결과 출력
    print_results(result)


if __name__ == "__main__":
    main()
