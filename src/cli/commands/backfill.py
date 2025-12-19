"""
Backfill CLI 명령어

과거 데이터 수집 명령어

사용법:
    sts backfill run --days 180
    sts backfill today
    sts backfill status
"""
import asyncio
from datetime import date, timedelta
import typer
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command("run")
def run_backfill(
    days: int = typer.Option(
        180,
        "--days", "-d",
        help="백필할 일수 (기본: 180일)",
    ),
    verbose: bool = typer.Option(
        True,
        "--verbose/--quiet",
        help="상세 출력 여부",
    ),
):
    """과거 데이터 백필 실행"""
    from src.collector.historical import backfill

    console.print(f"\n[bold]과거 {days}일 데이터 백필 시작[/bold]\n")

    try:
        rows = asyncio.run(backfill(days=days, verbose=verbose))
        console.print(f"\n[green]백필 완료![/green] 총 {rows:,}건 수집")
    except Exception as e:
        console.print(f"[red]에러:[/red] {e}")
        raise typer.Exit(1)


@app.command("today")
def collect_today_data(
    verbose: bool = typer.Option(True, "--verbose/--quiet"),
):
    """오늘 데이터 수집 (장 마감 후)"""
    from src.collector.historical import collect_today

    console.print("\n[bold]오늘 데이터 수집[/bold]\n")

    try:
        rows = asyncio.run(collect_today(verbose=verbose))
        if rows > 0:
            console.print(f"\n[green]수집 완료![/green] {rows:,}건")
        else:
            console.print("[yellow]수집된 데이터가 없습니다.[/yellow]")
    except Exception as e:
        console.print(f"[red]에러:[/red] {e}")
        raise typer.Exit(1)


@app.command("status")
def show_status():
    """현재 데이터 상태 확인"""
    try:
        import clickhouse_connect
        from config.settings import settings

        client = clickhouse_connect.get_client(
            host=settings.clickhouse.host,
            port=settings.clickhouse.port,
            database=settings.clickhouse.database,
            username=settings.clickhouse.user or None,
            password=settings.clickhouse.password or None,
        )

        # 전체 통계
        result = client.query("""
            SELECT
                count(*) as total_rows,
                countDistinct(code) as codes,
                min(datetime) as min_date,
                max(datetime) as max_date
            FROM kospi_mini_1m
        """)

        row = result.result_rows[0]
        total_rows, codes, min_date, max_date = row

        console.print("\n[bold]데이터 현황[/bold]\n")

        table = Table()
        table.add_column("항목", style="cyan")
        table.add_column("값", style="green", justify="right")

        table.add_row("총 레코드", f"{total_rows:,}")
        table.add_row("종목 수", str(codes))
        table.add_row("시작일", str(min_date))
        table.add_row("종료일", str(max_date))

        console.print(table)

        # 종목별 통계
        result = client.query("""
            SELECT
                code,
                count(*) as rows,
                min(datetime) as min_date,
                max(datetime) as max_date
            FROM kospi_mini_1m
            GROUP BY code
            ORDER BY code
        """)

        console.print("\n[bold]종목별 현황[/bold]\n")

        code_table = Table()
        code_table.add_column("코드", style="cyan")
        code_table.add_column("레코드", justify="right")
        code_table.add_column("시작", justify="center")
        code_table.add_column("종료", justify="center")

        for row in result.result_rows:
            code, rows, min_dt, max_dt = row
            code_table.add_row(
                code,
                f"{rows:,}",
                str(min_dt.date()) if min_dt else "-",
                str(max_dt.date()) if max_dt else "-",
            )

        console.print(code_table)

    except Exception as e:
        console.print(f"[red]에러:[/red] {e}")
        raise typer.Exit(1)


@app.command("codes")
def show_codes(
    target_date: str = typer.Option(
        None,
        "--date", "-d",
        help="조회할 날짜 (YYYY-MM-DD)",
    ),
):
    """특정 날짜의 활성 선물 코드 확인"""
    from src.collector.historical import get_active_codes_for_date

    if target_date:
        d = date.fromisoformat(target_date)
    else:
        d = date.today()

    codes = get_active_codes_for_date(d)

    console.print(f"\n[bold]{d} 활성 선물 코드[/bold]\n")

    for code in codes:
        console.print(f"  {code}")

    console.print(f"\n총 {len(codes)}개 코드")
