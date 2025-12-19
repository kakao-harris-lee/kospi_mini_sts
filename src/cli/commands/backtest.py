"""
백테스트 CLI 명령어

사용법:
    sts backtest run --strategy hybrid --start 2024-01-01 --end 2024-12-31
    sts backtest run --strategy mean_reversion --capital 5000000
"""
from datetime import datetime, date
from typing import Optional
import typer
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.panel import Panel

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
}


def parse_date(date_str: str) -> date:
    """날짜 문자열 파싱"""
    return datetime.strptime(date_str, "%Y-%m-%d").date()


@app.command("run")
def run_backtest(
    strategy: str = typer.Option(
        "hybrid",
        "--strategy", "-s",
        help="전략 이름 (mean_reversion, breakout, ofi_momentum, hybrid)",
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
        help="선물 코드 (예: 101M25). 미지정 시 자동 선택",
    ),
    save: bool = typer.Option(
        True,
        "--save/--no-save",
        help="결과를 DB에 저장",
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
        from src.backtest import BacktestEngine, BacktestConfig
        from src.strategy import (
            MeanReversionStrategy,
            BreakoutStrategy,
            OFIMomentumStrategy,
            HybridStrategy,
            PureMicrostructureStrategy,
            AdaptiveMicrostructureStrategy,
        )
        from src.database import ResultRepository

        # 전략 인스턴스 생성
        strategy_classes = {
            "mean_reversion": MeanReversionStrategy,
            "breakout": BreakoutStrategy,
            "ofi_momentum": OFIMomentumStrategy,
            "hybrid": HybridStrategy,
            "pure_micro": PureMicrostructureStrategy,
            "adaptive_micro": AdaptiveMicrostructureStrategy,
        }
        strategy_instance = strategy_classes[strategy]()

        # 백테스트 설정
        config = BacktestConfig(
            initial_capital=capital,
            commission_rate=0.00015,
            slippage_ticks=1,
            tick_value=250_000,
        )

        engine = BacktestEngine(config)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task("Running backtest...", total=None)
            result = engine.run(
                strategy=strategy_instance,
                start_date=start_date,
                end_date=end_date,
                code=code,
            )

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
                    "commission_rate": config.commission_rate,
                    "slippage_ticks": config.slippage_ticks,
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
            trades_data = [
                {
                    'entry_time': t.entry_time,
                    'exit_time': t.exit_time,
                    'side': t.side.name,
                    'entry_price': t.entry_price,
                    'exit_price': t.exit_price,
                    'quantity': t.quantity,
                    'pnl': t.pnl,
                    'pnl_amount': t.pnl_amount,
                    'commission': t.commission,
                    'exit_reason': t.exit_reason,
                }
                for t in result.trades
            ]
            repo.add_trades_bulk(run.id, trades_data)

            # 일별 지표 저장
            if hasattr(result, 'daily_metrics'):
                repo.add_daily_metrics_bulk(run.id, result.daily_metrics)

            console.print(f"\n[green]Saved to DB:[/green] Run ID = {run.id[:8]}...")

    except ImportError as e:
        console.print(f"[red]Import Error:[/red] {e}")
        console.print("Make sure all dependencies are installed: pip install -e '.[all]'")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


def _display_result(result):
    """백테스트 결과 출력"""
    # 성과 테이블
    table = Table(title="Backtest Result")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green", justify="right")

    metrics = [
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
        "hybrid",
        "--strategy", "-s",
        help="전략 이름",
    ),
):
    """최근 N일 빠른 백테스트"""
    from datetime import timedelta

    end_date = date.today()
    start_date = end_date - timedelta(days=days)

    run_backtest(
        strategy=strategy,
        start=start_date.strftime("%Y-%m-%d"),
        end=end_date.strftime("%Y-%m-%d"),
        capital=10_000_000,
        code=None,
        save=True,
    )
