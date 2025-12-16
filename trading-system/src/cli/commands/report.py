"""
리포트 CLI 명령어

사용법:
    sts report show <run_id>
    sts report export <run_id> --format html --output report.html
    sts report compare <run_id1> <run_id2>
"""
from pathlib import Path
from typing import List, Optional
import typer
from rich.console import Console

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command("show")
def show_report(
    run_id: str = typer.Argument(..., help="실행 ID (앞 8자리도 가능)"),
    trades: bool = typer.Option(
        True,
        "--trades/--no-trades",
        help="거래 내역 포함",
    ),
    daily: bool = typer.Option(
        False,
        "--daily/--no-daily",
        help="일별 지표 포함",
    ),
):
    """실행 결과 리포트 출력"""
    try:
        from src.database import ResultRepository
        from src.reporting import ReportGenerator, ReportFormat

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

        generator = ReportGenerator(repository=repo)
        report = generator.generate(
            run=matching_run,
            format=ReportFormat.TEXT,
            include_trades=trades,
            include_daily=daily,
        )

        console.print(report)

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("export")
def export_report(
    run_id: str = typer.Argument(..., help="실행 ID"),
    output: Path = typer.Option(
        None,
        "--output", "-o",
        help="출력 파일 경로 (예: report.html)",
    ),
    format: str = typer.Option(
        "html",
        "--format", "-f",
        help="출력 형식 (text, html, json, markdown)",
    ),
    trades: bool = typer.Option(
        True,
        "--trades/--no-trades",
        help="거래 내역 포함",
    ),
):
    """리포트를 파일로 내보내기"""
    try:
        from src.database import ResultRepository
        from src.reporting import ReportGenerator, ReportFormat

        format_map = {
            "text": ReportFormat.TEXT,
            "html": ReportFormat.HTML,
            "json": ReportFormat.JSON,
            "markdown": ReportFormat.MARKDOWN,
            "md": ReportFormat.MARKDOWN,
        }

        if format.lower() not in format_map:
            console.print(f"[red]Error:[/red] Unknown format: {format}")
            console.print(f"Available: {', '.join(format_map.keys())}")
            raise typer.Exit(1)

        report_format = format_map[format.lower()]

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

        # 기본 출력 경로
        if output is None:
            ext_map = {
                ReportFormat.TEXT: ".txt",
                ReportFormat.HTML: ".html",
                ReportFormat.JSON: ".json",
                ReportFormat.MARKDOWN: ".md",
            }
            output = Path(f"report_{matching_run.id[:8]}{ext_map[report_format]}")

        generator = ReportGenerator(repository=repo)
        generator.save(
            run_id=matching_run.id,
            output_path=output,
            format=report_format,
            include_trades=trades,
        )

        console.print(f"[green]Report saved:[/green] {output}")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command("compare")
def compare_reports(
    run_ids: List[str] = typer.Argument(..., help="비교할 실행 ID들 (공백으로 구분)"),
    format: str = typer.Option(
        "text",
        "--format", "-f",
        help="출력 형식 (text, markdown)",
    ),
):
    """여러 실행 결과 비교"""
    if len(run_ids) < 2:
        console.print("[red]Error:[/red] At least 2 run IDs required")
        raise typer.Exit(1)

    try:
        from src.database import ResultRepository
        from src.reporting import ReportGenerator, ReportFormat

        format_map = {
            "text": ReportFormat.TEXT,
            "markdown": ReportFormat.MARKDOWN,
            "md": ReportFormat.MARKDOWN,
        }

        if format.lower() not in format_map:
            console.print(f"[red]Error:[/red] Unknown format: {format}")
            raise typer.Exit(1)

        repo = ResultRepository()
        all_runs = repo.get_runs(limit=200)

        # ID 매칭
        matched_ids = []
        for run_id in run_ids:
            for run in all_runs:
                if run.id.startswith(run_id):
                    matched_ids.append(run.id)
                    break

        if len(matched_ids) != len(run_ids):
            console.print("[red]Error:[/red] Some runs not found")
            raise typer.Exit(1)

        generator = ReportGenerator(repository=repo)
        report = generator.compare_runs(
            run_ids=matched_ids,
            format=format_map[format.lower()],
        )

        console.print(report)

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
