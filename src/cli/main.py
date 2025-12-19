"""
STS CLI - KOSPI Mini 선물 트레이딩 시스템 CLI

사용법:
    sts backtest --strategy hybrid --start 2024-01-01 --end 2024-12-31
    sts paper --strategy hybrid --duration 1h
    sts runs list --limit 10
    sts report <run_id>
"""
import typer
from rich.console import Console
from rich.table import Table

from .commands import backtest, runs, paper, report, backfill

app = typer.Typer(
    name="sts",
    help="KOSPI Mini 선물 트레이딩 시스템 CLI",
    no_args_is_help=True,
)

console = Console()

# 서브 커맨드 등록
app.add_typer(backtest.app, name="backtest", help="백테스트 실행")
app.add_typer(backfill.app, name="backfill", help="과거 데이터 수집")
app.add_typer(runs.app, name="runs", help="실행 기록 관리")
app.add_typer(paper.app, name="paper", help="모의투자 실행")
app.add_typer(report.app, name="report", help="리포트 생성")


@app.command()
def version():
    """버전 정보 출력"""
    console.print("[bold blue]STS[/bold blue] - KOSPI Mini Trading System")
    console.print("Version: 0.1.0")


@app.command()
def strategies():
    """사용 가능한 전략 목록"""
    table = Table(title="Available Strategies")
    table.add_column("Name", style="cyan")
    table.add_column("Description", style="green")
    table.add_column("Regime", style="yellow")

    strategies_info = [
        ("mean_reversion", "볼린저 밴드 기반 평균회귀", "LOW"),
        ("breakout", "N기간 고가/저가 돌파", "HIGH"),
        ("ofi_momentum", "OFI 기반 모멘텀", "ALL"),
        ("hybrid", "LSTM + 로지컬 복합 전략 (deprecated)", "ALL"),
        ("pure_micro", "순수 마이크로스트럭처 (추천)", "ALL"),
        ("adaptive_micro", "적응형 마이크로스트럭처", "ALL"),
    ]

    for name, desc, regime in strategies_info:
        table.add_row(name, desc, regime)

    console.print(table)


if __name__ == "__main__":
    app()
