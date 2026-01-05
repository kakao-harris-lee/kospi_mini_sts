"""
백테스트 CLI 명령어

사용법:
    sts backtest run --strategy pure_micro --start 2024-01-01 --end 2024-12-31
    sts backtest quick --days 30 --strategy pure_micro
"""
from datetime import datetime, date, timedelta
from typing import Optional
import typer
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.panel import Panel
import pandas as pd

app = typer.Typer(no_args_is_help=True)
console = Console()

# 전략 매핑
STRATEGY_MAP = {
    "mean_reversion": "MeanReversionStrategy",
    "breakout": "BreakoutStrategy",
    "ofi_momentum": "OFIMomentumStrategy",
    "hybrid": "HybridStrategy",
    "pure_micro": "PureMicrostructureStrategy",
    "adaptive_micro": "AdaptiveMicrostructureStrategy",
    "dual_mode": "DualModeStrategy",
}


def parse_date(date_str: str) -> date:
    """날짜 문자열 파싱"""
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def load_data_from_clickhouse(start_date: date, end_date: date, code: Optional[str] = None) -> pd.DataFrame:
    """ClickHouse에서 데이터 로드"""
    import os
    import clickhouse_connect
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

    # 코드 필터
    code_filter = f"AND code = '{code}'" if code else ""

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
          AND datetime <= '{end_date.strftime('%Y-%m-%d')} 23:59:59'
          {code_filter}
        ORDER BY datetime
    """

    result = client.query(query)

    df = pd.DataFrame(
        result.result_rows,
        columns=['code', 'datetime', 'open', 'high', 'low', 'close', 'volume']
    )

    return df


@app.command("run")
def run_backtest(
    strategy: str = typer.Option(
        "pure_micro",
        "--strategy", "-s",
        help="전략 이름 (pure_micro, adaptive_micro, mean_reversion, breakout, ofi_momentum, hybrid)",
    ),
    start: str = typer.Option(
        ...,
        "--start",
        help="시작일 (YYYY-MM-DD)",
    ),
    end: str = typer.Option(
        ...,
        "--end",
        help="종료일 (YYYY-MM-DD)",
    ),
    capital: float = typer.Option(
        10_000_000,
        "--capital", "-c",
        help="초기 자본금 (KRW)",
    ),
    code: str = typer.Option(
        None,
        "--code",
        help="선물 코드 (예: A05601). 미지정 시 전체",
    ),
    save: bool = typer.Option(
        True,
        "--save/--no-save",
        help="결과를 DB에 저장",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose", "-v",
        help="상세 로그 출력",
    ),
):
    """백테스트 실행"""
    if strategy not in STRATEGY_MAP:
        console.print(f"[red]Error:[/red] Unknown strategy '{strategy}'")
        console.print(f"Available: {', '.join(STRATEGY_MAP.keys())}")
        raise typer.Exit(1)

    start_date = parse_date(start)
    end_date = parse_date(end)

    if start_date >= end_date:
        console.print("[red]Error:[/red] Start date must be before end date")
        raise typer.Exit(1)

    console.print(Panel(
        f"[bold]Strategy:[/bold] {strategy}\n"
        f"[bold]Period:[/bold] {start} ~ {end}\n"
        f"[bold]Capital:[/bold] {capital:,.0f} KRW",
        title="Backtest Configuration",
    ))

    try:
        # 데이터 로드
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task("Loading data from ClickHouse...", total=None)
            df = load_data_from_clickhouse(start_date, end_date, code)

        if df.empty:
            console.print("[red]Error:[/red] No data found for the specified period")
            raise typer.Exit(1)

        console.print(f"[green]Loaded {len(df):,} bars[/green]")
        console.print(f"Date range: {df['datetime'].min()} ~ {df['datetime'].max()}")

        # 임포트
        from src.backtest import (
            BacktestEngine,
            BacktestConfig,
            FeatureEngineer,
            FeatureConfig,
            StrategyAdapter,
            RiskConfig,
        )
        from src.strategy import (
            MeanReversionStrategy,
            BreakoutStrategy,
            OFIMomentumStrategy,
            HybridStrategy,
            PureMicrostructureStrategy,
            AdaptiveMicrostructureStrategy,
            DualModeStrategy,
        )
        from src.database import ResultRepository

        # 피처 엔지니어링
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task("Applying feature engineering...", total=None)
            engineer = FeatureEngineer(FeatureConfig())
            df = engineer.transform(df)

        # 전략 인스턴스 생성
        strategy_classes = {
            "mean_reversion": MeanReversionStrategy,
            "breakout": BreakoutStrategy,
            "ofi_momentum": OFIMomentumStrategy,
            "hybrid": HybridStrategy,
            "pure_micro": PureMicrostructureStrategy,
            "adaptive_micro": AdaptiveMicrostructureStrategy,
            "dual_mode": DualModeStrategy,
        }
        strategy_instance = strategy_classes[strategy]()
        adapter = StrategyAdapter(strategy_instance)

        # 백테스트 설정
        config = BacktestConfig(
            initial_capital=capital,
            position_size=1,
            point_value=50_000,  # KOSPI Mini
            risk_config=RiskConfig(
                stop_loss_points=1.5,
                take_profit_points=3.0,
                time_stop_minutes=30,
                trailing_stop_points=1.0,
                max_daily_loss=500_000,
                max_daily_trades=50,
            ),
            verbose=verbose,
        )

        engine = BacktestEngine(
            strategy=adapter,
            config=config,
        )

        # 백테스트 실행
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task("Running backtest...", total=None)
            result = engine.run(df)

        # 결과 출력
        _display_result(result)

        # DB 저장
        if save:
            repo = ResultRepository()
            run = repo.create_run(
                strategy=strategy,
                mode="backtest",
                start_date=datetime.combine(start_date, datetime.min.time()),
                end_date=datetime.combine(end_date, datetime.max.time()),
                config={
                    "capital": capital,
                    "code": code,
                    "position_size": config.position_size,
                    "point_value": config.point_value,
                },
                initial_capital=capital,
            )

            # 결과 업데이트
            repo.update_run_results(
                run_id=run.id,
                final_capital=result.final_capital,
                total_return=result.total_return,
                total_pnl=result.total_pnl,
                total_trades=result.total_trades,
                winning_trades=result.winning_trades,
                losing_trades=result.losing_trades,
                win_rate=result.win_rate,
                profit_factor=result.profit_factor,
                max_drawdown=result.max_drawdown,
                sharpe_ratio=getattr(result, 'sharpe_ratio', 0.0),
                sortino_ratio=getattr(result, 'sortino_ratio', 0.0),
            )

            # 거래 기록 저장
            if result.trades:
                trades_data = [
                    {
                        'entry_time': t.entry_time,
                        'exit_time': t.exit_time,
                        'side': t.side.name if hasattr(t.side, 'name') else str(t.side),
                        'entry_price': t.entry_price,
                        'exit_price': t.exit_price,
                        'quantity': t.quantity,
                        'pnl': t.pnl,
                        'pnl_amount': getattr(t, 'pnl_amount', t.pnl * config.point_value),
                        'commission': getattr(t, 'commission', 0.0),
                        'exit_reason': getattr(t, 'exit_reason', None),
                    }
                    for t in result.trades
                ]
                repo.add_trades_bulk(run.id, trades_data)

            console.print(f"\n[green]Saved to DB:[/green] Run ID = {run.id[:8]}...")

    except ImportError as e:
        console.print(f"[red]Import Error:[/red] {e}")
        console.print("Make sure all dependencies are installed: pip install -e '.[all]'")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        import traceback
        if verbose:
            traceback.print_exc()
        raise typer.Exit(1)


def _display_result(result):
    """백테스트 결과 출력"""
    # 성과 테이블
    table = Table(title="Backtest Result")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green", justify="right")

    metrics = [
        ("Period", f"{result.start_date.date()} ~ {result.end_date.date()}"),
        ("Total Bars", f"{result.total_bars:,}"),
        ("", ""),
        ("Initial Capital", f"{result.initial_capital:,.0f} KRW"),
        ("Final Capital", f"{result.final_capital:,.0f} KRW"),
        ("Total Return", f"{result.total_return:+.2f}%"),
        ("Total PnL", f"{result.total_pnl:+,.0f} KRW"),
        ("", ""),
        ("Total Trades", f"{result.total_trades}"),
        ("Winning Trades", f"{result.winning_trades}"),
        ("Losing Trades", f"{result.losing_trades}"),
        ("Win Rate", f"{result.win_rate:.1f}%"),
        ("", ""),
        ("Avg Win", f"{result.avg_win:+,.0f} KRW"),
        ("Avg Loss", f"{result.avg_loss:+,.0f} KRW"),
        ("Profit Factor", f"{result.profit_factor:.2f}"),
        ("Max Drawdown", f"{result.max_drawdown:.2f}%"),
    ]

    if hasattr(result, 'sharpe_ratio'):
        metrics.append(("Sharpe Ratio", f"{result.sharpe_ratio:.2f}"))
    if hasattr(result, 'sortino_ratio'):
        metrics.append(("Sortino Ratio", f"{result.sortino_ratio:.2f}"))

    for name, value in metrics:
        if name:
            table.add_row(name, value)
        else:
            table.add_row("─" * 15, "─" * 15)

    console.print(table)

    # 청산 사유
    if result.exit_reasons:
        console.print("\n[bold]Exit Reasons:[/bold]")
        for reason, count in result.exit_reasons.items():
            console.print(f"  {reason}: {count}")

    # PnL 색상
    pnl_color = "green" if result.total_pnl >= 0 else "red"
    console.print(f"\n[{pnl_color}]Net PnL: {result.total_pnl:+,.0f} KRW[/{pnl_color}]")


@app.command("quick")
def quick_backtest(
    days: int = typer.Option(
        30,
        "--days", "-d",
        help="최근 N일",
    ),
    strategy: str = typer.Option(
        "pure_micro",
        "--strategy", "-s",
        help="전략 이름",
    ),
    capital: float = typer.Option(
        10_000_000,
        "--capital", "-c",
        help="초기 자본금 (KRW)",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose", "-v",
        help="상세 로그 출력",
    ),
):
    """최근 N일 빠른 백테스트"""
    end_date = date.today()
    start_date = end_date - timedelta(days=days)

    run_backtest(
        strategy=strategy,
        start=start_date.strftime("%Y-%m-%d"),
        end=end_date.strftime("%Y-%m-%d"),
        capital=capital,
        code=None,
        save=True,
        verbose=verbose,
    )
