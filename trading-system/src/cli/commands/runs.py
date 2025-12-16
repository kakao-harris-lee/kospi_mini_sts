"""
실행 기록 관리 CLI 명령어

사용법:
    sts runs list --limit 10
    sts runs show <run_id>
    sts runs compare <run_id1> <run_id2>
    sts runs delete <run_id>
"""
from typing import Optional, List
import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command("list")
def list_runs(
    strategy: Optional[str] = typer.Option(
        None,
        "--strategy", "-s",
        help="전략으로 필터링",
    ),
    mode: Optional[str] = typer.Option(
        None,
        "--mode", "-m",
        help="모드로 필터링 (backtest, paper, live)",
    ),
    limit: int = typer.Option(
        20,
        "--limit", "-n",
        help="최대 출력 개수",
    ),
):
    """실행 기록 목록 조회"""
    try:
        from src.database import ResultRepository

        repo = ResultRepository()
        runs = repo.get_runs(strategy=strategy, mode=mode, limit=limit)

        if not runs:
            console.print("[yellow]No runs found[/yellow]")
            return

        table = Table(title=f"Recent Runs (Total: {len(runs)})")
        table.add_column("ID", style="cyan", width=8)
        table.add_column("Strategy", style="green")
        table.add_column("Mode", style="yellow")
        table.add_column("Return", justify="right")
        table.add_column("Win Rate", justify="right")
        table.add_column("Trades", justify="right")
        table.add_column("MDD", justify="right")
        table.add_column("Date", style="dim")

        for run in runs:
            return_color = "green" if run.total_return >= 0 else "red"
            table.add_row(
                run.id[:8],
                run.strategy,
                run.mode,
                f"[{return_color}]{run.total_return:+.2f}%[/{return_color}]",
                f"{run.win_rate:.1f}%",
                str(run.total_trades),
                f"{run.max_drawdown:.2f}%",
                run.created_at.strftime("%m/%d %H:%M") if run.created_at else "-",
            )

        console.print(table)

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("show")
def show_run(
    run_id: str = typer.Argument(..., help="실행 ID (앞 8자리도 가능)"),
    trades: bool = typer.Option(
        False,
        "--trades", "-t",
        help="거래 내역 포함",
    ),
):
    """실행 기록 상세 조회"""
    try:
        from src.database import ResultRepository

        repo = ResultRepository()

        # ID가 8자리면 전체 ID 검색
        runs = repo.get_runs(limit=100)
        matching_run = None
        for run in runs:
            if run.id.startswith(run_id):
                matching_run = run
                break

        if not matching_run:
            console.print(f"[red]Error:[/red] Run not found: {run_id}")
            raise typer.Exit(1)

        run = matching_run

        # 기본 정보
        console.print(Panel(
            f"[bold]ID:[/bold] {run.id}\n"
            f"[bold]Strategy:[/bold] {run.strategy}\n"
            f"[bold]Mode:[/bold] {run.mode}\n"
            f"[bold]Period:[/bold] {run.start_date} ~ {run.end_date}\n"
            f"[bold]Created:[/bold] {run.created_at}",
            title="Run Info",
        ))

        # 성과 테이블
        table = Table(title="Performance")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green", justify="right")

        pnl_color = "green" if run.total_pnl >= 0 else "red"
        ret_color = "green" if run.total_return >= 0 else "red"

        metrics = [
            ("Initial Capital", f"{run.initial_capital:,.0f} KRW"),
            ("Final Capital", f"{run.final_capital:,.0f} KRW"),
            ("Total Return", f"[{ret_color}]{run.total_return:+.2f}%[/{ret_color}]"),
            ("Total PnL", f"[{pnl_color}]{run.total_pnl:+,.0f} KRW[/{pnl_color}]"),
            ("Total Trades", str(run.total_trades)),
            ("Win Rate", f"{run.win_rate:.1f}%"),
            ("Profit Factor", f"{run.profit_factor:.2f}"),
            ("Max Drawdown", f"{run.max_drawdown:.2f}%"),
            ("Sharpe Ratio", f"{run.sharpe_ratio:.2f}"),
        ]

        for name, value in metrics:
            table.add_row(name, value)

        console.print(table)

        # 거래 내역
        if trades:
            trade_list = repo.get_trades(run.id)
            if trade_list:
                trade_table = Table(title=f"Trades ({len(trade_list)})")
                trade_table.add_column("#", style="dim")
                trade_table.add_column("Entry Time")
                trade_table.add_column("Side", style="cyan")
                trade_table.add_column("Entry", justify="right")
                trade_table.add_column("Exit", justify="right")
                trade_table.add_column("PnL", justify="right")
                trade_table.add_column("Reason")

                for i, t in enumerate(trade_list, 1):
                    pnl_color = "green" if t.pnl_amount >= 0 else "red"
                    trade_table.add_row(
                        str(i),
                        t.entry_time.strftime("%m/%d %H:%M") if t.entry_time else "-",
                        t.side,
                        f"{t.entry_price:,.2f}",
                        f"{t.exit_price:,.2f}" if t.exit_price else "-",
                        f"[{pnl_color}]{t.pnl_amount:+,.0f}[/{pnl_color}]",
                        t.exit_reason or "-",
                    )

                console.print(trade_table)
            else:
                console.print("[yellow]No trades recorded[/yellow]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("compare")
def compare_runs(
    run_ids: List[str] = typer.Argument(..., help="비교할 실행 ID들 (공백으로 구분)"),
):
    """실행 기록 비교"""
    if len(run_ids) < 2:
        console.print("[red]Error:[/red] At least 2 run IDs required")
        raise typer.Exit(1)

    try:
        from src.database import ResultRepository

        repo = ResultRepository()
        all_runs = repo.get_runs(limit=200)

        # ID 매칭
        matched_runs = []
        for run_id in run_ids:
            for run in all_runs:
                if run.id.startswith(run_id):
                    matched_runs.append(run)
                    break

        if len(matched_runs) != len(run_ids):
            console.print("[red]Error:[/red] Some runs not found")
            raise typer.Exit(1)

        # 비교 테이블
        table = Table(title="Run Comparison")
        table.add_column("Metric", style="cyan")
        for run in matched_runs:
            table.add_column(f"{run.id[:8]}\n({run.strategy})", justify="right")

        metrics = [
            ("Return", lambda r: f"{r.total_return:+.2f}%"),
            ("PnL", lambda r: f"{r.total_pnl:+,.0f}"),
            ("Trades", lambda r: str(r.total_trades)),
            ("Win Rate", lambda r: f"{r.win_rate:.1f}%"),
            ("Profit Factor", lambda r: f"{r.profit_factor:.2f}"),
            ("Max DD", lambda r: f"{r.max_drawdown:.2f}%"),
            ("Sharpe", lambda r: f"{r.sharpe_ratio:.2f}"),
        ]

        for name, formatter in metrics:
            row = [name]
            for run in matched_runs:
                value = formatter(run)
                # 수익률/PnL 색상
                if name in ("Return", "PnL"):
                    val = run.total_return if name == "Return" else run.total_pnl
                    color = "green" if val >= 0 else "red"
                    value = f"[{color}]{value}[/{color}]"
                row.append(value)
            table.add_row(*row)

        console.print(table)

        # Best performer
        best_return = max(matched_runs, key=lambda r: r.total_return)
        console.print(f"\n[bold]Best Return:[/bold] {best_return.id[:8]} ({best_return.strategy})")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("stats")
def strategy_stats(
    strategy: str = typer.Argument(..., help="전략 이름"),
    mode: Optional[str] = typer.Option(
        None,
        "--mode", "-m",
        help="모드로 필터링",
    ),
):
    """전략별 통계 요약"""
    try:
        from src.database import ResultRepository

        repo = ResultRepository()
        stats = repo.get_strategy_stats(strategy, mode)

        if not stats:
            console.print(f"[yellow]No data for strategy: {strategy}[/yellow]")
            return

        console.print(Panel(
            f"[bold]Strategy:[/bold] {stats['strategy']}\n"
            f"[bold]Total Runs:[/bold] {stats['total_runs']}\n"
            f"[bold]Avg Return:[/bold] {stats['avg_return']:.2f}%\n"
            f"[bold]Best Return:[/bold] {stats['best_return']:.2f}%\n"
            f"[bold]Worst Return:[/bold] {stats['worst_return']:.2f}%\n"
            f"[bold]Avg Win Rate:[/bold] {stats['avg_win_rate']:.1f}%\n"
            f"[bold]Avg Max DD:[/bold] {stats['avg_max_drawdown']:.2f}%",
            title="Strategy Statistics",
        ))

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("delete")
def delete_run(
    run_id: str = typer.Argument(..., help="삭제할 실행 ID"),
    force: bool = typer.Option(
        False,
        "--force", "-f",
        help="확인 없이 삭제",
    ),
):
    """실행 기록 삭제"""
    try:
        from src.database import ResultRepository

        repo = ResultRepository()

        # ID 매칭
        runs = repo.get_runs(limit=100)
        matching_run = None
        for run in runs:
            if run.id.startswith(run_id):
                matching_run = run
                break

        if not matching_run:
            console.print(f"[red]Error:[/red] Run not found: {run_id}")
            raise typer.Exit(1)

        if not force:
            console.print(f"[yellow]Warning:[/yellow] This will delete run {matching_run.id[:8]} ({matching_run.strategy})")
            confirm = typer.confirm("Are you sure?")
            if not confirm:
                console.print("Cancelled")
                return

        repo.delete_run(matching_run.id)
        console.print(f"[green]Deleted:[/green] {matching_run.id[:8]}")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
