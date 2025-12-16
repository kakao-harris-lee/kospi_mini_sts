"""
Paper Trading CLI 명령어

사용법:
    sts paper run --strategy hybrid --duration 1h
    sts paper run --strategy mean_reversion --capital 5000000
"""
import asyncio
from datetime import datetime
from typing import Optional
import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel

app = typer.Typer(no_args_is_help=True)
console = Console()

STRATEGY_MAP = {
    "mean_reversion": "MeanReversionStrategy",
    "breakout": "BreakoutStrategy",
    "ofi_momentum": "OFIMomentumStrategy",
    "hybrid": "HybridStrategy",
    "pure_micro": "PureMicrostructureStrategy",
    "adaptive_micro": "AdaptiveMicrostructureStrategy",
}


def parse_duration(duration_str: str) -> int:
    """Duration 문자열을 초로 변환 (예: 1h, 30m, 3600s)"""
    duration_str = duration_str.strip().lower()

    if duration_str.endswith('h'):
        return int(duration_str[:-1]) * 3600
    elif duration_str.endswith('m'):
        return int(duration_str[:-1]) * 60
    elif duration_str.endswith('s'):
        return int(duration_str[:-1])
    else:
        return int(duration_str)


@app.command("run")
def run_paper_trading(
    strategy: str = typer.Option(
        "hybrid",
        "--strategy", "-s",
        help="전략 이름 (mean_reversion, breakout, ofi_momentum, hybrid)",
    ),
    duration: str = typer.Option(
        "1h",
        "--duration", "-d",
        help="실행 시간 (예: 1h, 30m, 3600s)",
    ),
    capital: float = typer.Option(
        10_000_000,
        "--capital", "-c",
        help="초기 자본금 (KRW)",
    ),
    simulation: bool = typer.Option(
        False,
        "--simulation",
        help="시뮬레이션 모드 (Redis 없이 실행)",
    ),
    save: bool = typer.Option(
        True,
        "--save/--no-save",
        help="결과를 DB에 저장",
    ),
):
    """Paper Trading 실행"""
    if strategy not in STRATEGY_MAP:
        console.print(f"[red]Error:[/red] Unknown strategy '{strategy}'")
        console.print(f"Available: {', '.join(STRATEGY_MAP.keys())}")
        raise typer.Exit(1)

    try:
        duration_seconds = parse_duration(duration)
    except ValueError:
        console.print(f"[red]Error:[/red] Invalid duration format: {duration}")
        console.print("Examples: 1h, 30m, 3600s")
        raise typer.Exit(1)

    console.print(Panel(
        f"[bold]Strategy:[/bold] {strategy}\n"
        f"[bold]Duration:[/bold] {duration} ({duration_seconds}s)\n"
        f"[bold]Capital:[/bold] {capital:,.0f} KRW\n"
        f"[bold]Mode:[/bold] {'Simulation' if simulation else 'Live Redis'}",
        title="Paper Trading Configuration",
    ))

    console.print("\n[yellow]Press Ctrl+C to stop early[/yellow]\n")

    try:
        result = asyncio.run(_run_paper_trading_async(
            strategy_name=strategy,
            duration_seconds=duration_seconds,
            initial_capital=capital,
            simulation=simulation,
        ))

        # 결과 출력
        _display_result(result)

        # DB 저장
        if save:
            _save_to_db(result, strategy, capital)

    except KeyboardInterrupt:
        console.print("\n[yellow]Paper Trading stopped by user[/yellow]")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


async def _run_paper_trading_async(
    strategy_name: str,
    duration_seconds: int,
    initial_capital: float,
    simulation: bool = False,
):
    """비동기 Paper Trading 실행"""
    from src.strategy import (
        MeanReversionStrategy,
        BreakoutStrategy,
        OFIMomentumStrategy,
        HybridStrategy,
        PureMicrostructureStrategy,
        AdaptiveMicrostructureStrategy,
    )
    from src.paper_trading import PaperTradingEngine, VirtualBroker

    strategy_classes = {
        "mean_reversion": MeanReversionStrategy,
        "breakout": BreakoutStrategy,
        "ofi_momentum": OFIMomentumStrategy,
        "hybrid": HybridStrategy,
        "pure_micro": PureMicrostructureStrategy,
        "adaptive_micro": AdaptiveMicrostructureStrategy,
    }

    strategy = strategy_classes[strategy_name]()
    broker = VirtualBroker(initial_capital=initial_capital)

    # Redis 클라이언트
    redis_client = None
    if not simulation:
        try:
            import redis.asyncio as aioredis
            redis_client = await aioredis.from_url("redis://localhost:6379")
            await redis_client.ping()
            console.print("[green]Connected to Redis[/green]")
        except Exception as e:
            console.print(f"[yellow]Redis not available: {e}[/yellow]")
            console.print("[yellow]Falling back to simulation mode[/yellow]")
            redis_client = None

    engine = PaperTradingEngine(
        strategy=strategy,
        broker=broker,
        redis_client=redis_client,
    )

    try:
        await engine.start(duration_seconds=duration_seconds)
    finally:
        if redis_client:
            await redis_client.close()

    return engine.get_result()


def _display_result(result: dict):
    """Paper Trading 결과 출력"""
    table = Table(title="Paper Trading Result")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green", justify="right")

    pnl_color = "green" if result['total_pnl'] >= 0 else "red"
    ret_color = "green" if result['total_return'] >= 0 else "red"

    metrics = [
        ("Duration", f"{result['duration_seconds']:.0f} seconds"),
        ("Bars Processed", str(result['bars_processed'])),
        ("Signals Generated", str(result['signals_generated'])),
        ("", ""),
        ("Initial Capital", f"{result['initial_capital']:,.0f} KRW"),
        ("Final Equity", f"{result['equity']:,.0f} KRW"),
        ("Total Return", f"[{ret_color}]{result['total_return']:+.2f}%[/{ret_color}]"),
        ("Total PnL", f"[{pnl_color}]{result['total_pnl']:+,.0f} KRW[/{pnl_color}]"),
        ("", ""),
        ("Total Trades", str(result['total_trades'])),
        ("Winning Trades", str(result['winning_trades'])),
        ("Losing Trades", str(result['losing_trades'])),
        ("Win Rate", f"{result['win_rate']:.1f}%"),
    ]

    for name, value in metrics:
        if name:
            table.add_row(name, value)
        else:
            table.add_row("─" * 15, "─" * 15)

    console.print(table)


def _save_to_db(result: dict, strategy: str, capital: float):
    """결과를 DB에 저장"""
    try:
        from src.database import ResultRepository
        from datetime import datetime

        repo = ResultRepository()

        # Run 생성
        run = repo.create_run(
            strategy=strategy,
            mode="paper",
            start_date=datetime.fromisoformat(result['start_time']) if result['start_time'] else datetime.now(),
            end_date=datetime.fromisoformat(result['end_time']) if result['end_time'] else datetime.now(),
            config={
                'duration_seconds': result['duration_seconds'],
                'bars_processed': result['bars_processed'],
            },
            initial_capital=capital,
        )

        # 결과 업데이트
        repo.update_run_results(
            run_id=run.id,
            final_capital=result['equity'],
            total_return=result['total_return'],
            total_pnl=result['total_pnl'],
            total_trades=result['total_trades'],
            winning_trades=result['winning_trades'],
            losing_trades=result['losing_trades'],
            win_rate=result['win_rate'],
            profit_factor=0.0,  # Paper Trading에서는 계산 안함
            max_drawdown=0.0,
        )

        # 거래 기록 저장
        trades_data = [
            {
                'entry_time': datetime.fromisoformat(t['entry_time']),
                'exit_time': datetime.fromisoformat(t['exit_time']) if t['exit_time'] else None,
                'side': t['side'],
                'entry_price': t['entry_price'],
                'exit_price': t['exit_price'],
                'quantity': 1,
                'pnl': t['pnl'],
                'pnl_amount': t['pnl_amount'],
                'commission': 0,
                'exit_reason': t['exit_reason'],
            }
            for t in result['trades']
        ]
        if trades_data:
            repo.add_trades_bulk(run.id, trades_data)

        console.print(f"\n[green]Saved to DB:[/green] Run ID = {run.id[:8]}...")

    except Exception as e:
        console.print(f"[yellow]Warning: Could not save to DB: {e}[/yellow]")


@app.command("status")
def paper_status():
    """현재 Paper Trading 상태 확인"""
    console.print("[yellow]Paper Trading status check not implemented yet[/yellow]")
    console.print("Use 'sts runs list --mode paper' to see past paper trading runs")
