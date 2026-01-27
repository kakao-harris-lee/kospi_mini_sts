"""
Report Generator

백테스트/모의투자 결과를 다양한 형식으로 출력
"""
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import List, Dict, Any, Optional
import json


class ReportFormat(str, Enum):
    TEXT = "text"
    HTML = "html"
    JSON = "json"
    MARKDOWN = "markdown"


class ReportGenerator:
    """리포트 생성기"""

    def __init__(self, repository=None):
        """
        :param repository: ResultRepository 인스턴스
        """
        self.repository = repository

    def generate(
        self,
        run_id: str = None,
        run=None,
        format: ReportFormat = ReportFormat.TEXT,
        include_trades: bool = True,
        include_daily: bool = True,
    ) -> str:
        """
        리포트 생성

        :param run_id: 실행 ID
        :param run: Run 객체 (run_id 대신 직접 전달)
        :param format: 출력 형식
        :param include_trades: 거래 내역 포함
        :param include_daily: 일별 지표 포함
        :return: 리포트 문자열
        """
        if run is None and run_id:
            if not self.repository:
                raise ValueError("Repository required when using run_id")
            run = self.repository.get_run(run_id)
            if not run:
                raise ValueError(f"Run not found: {run_id}")

        trades = []
        daily_metrics = []

        if self.repository:
            if include_trades:
                trades = self.repository.get_trades(run.id)
            if include_daily:
                daily_metrics = self.repository.get_daily_metrics(run.id)

        if format == ReportFormat.TEXT:
            return self._generate_text(run, trades, daily_metrics)
        elif format == ReportFormat.HTML:
            return self._generate_html(run, trades, daily_metrics)
        elif format == ReportFormat.JSON:
            return self._generate_json(run, trades, daily_metrics)
        elif format == ReportFormat.MARKDOWN:
            return self._generate_markdown(run, trades, daily_metrics)
        else:
            raise ValueError(f"Unknown format: {format}")

    def save(
        self,
        run_id: str,
        output_path: Path,
        format: ReportFormat = None,
        **kwargs,
    ):
        """
        리포트를 파일로 저장

        :param run_id: 실행 ID
        :param output_path: 출력 파일 경로
        :param format: 출력 형식 (None이면 확장자에서 추론)
        """
        # 확장자에서 형식 추론
        if format is None:
            ext = output_path.suffix.lower()
            format_map = {
                '.txt': ReportFormat.TEXT,
                '.html': ReportFormat.HTML,
                '.json': ReportFormat.JSON,
                '.md': ReportFormat.MARKDOWN,
            }
            format = format_map.get(ext, ReportFormat.TEXT)

        content = self.generate(run_id=run_id, format=format, **kwargs)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding='utf-8')

    def _generate_text(self, run, trades: List, daily_metrics: List) -> str:
        """텍스트 형식 리포트"""
        lines = []
        lines.append("=" * 60)
        lines.append("TRADING SYSTEM REPORT")
        lines.append("=" * 60)
        lines.append("")

        # 기본 정보
        lines.append("[ Run Info ]")
        lines.append(f"  ID:       {run.id}")
        lines.append(f"  Strategy: {run.strategy}")
        lines.append(f"  Mode:     {run.mode}")
        lines.append(f"  Period:   {run.start_date} ~ {run.end_date}")
        lines.append(f"  Created:  {run.created_at}")
        lines.append("")

        # 성과 지표
        lines.append("[ Performance ]")
        lines.append(f"  Initial Capital:  {run.initial_capital:>15,.0f} KRW")
        lines.append(f"  Final Capital:    {run.final_capital:>15,.0f} KRW")
        lines.append(f"  Total Return:     {run.total_return:>14.2f}%")
        lines.append(f"  Total PnL:        {run.total_pnl:>+15,.0f} KRW")
        lines.append("")
        lines.append(f"  Total Trades:     {run.total_trades:>15}")
        lines.append(f"  Winning Trades:   {run.winning_trades:>15}")
        lines.append(f"  Losing Trades:    {run.losing_trades:>15}")
        lines.append(f"  Win Rate:         {run.win_rate:>14.1f}%")
        lines.append("")
        lines.append(f"  Profit Factor:    {run.profit_factor:>15.2f}")
        lines.append(f"  Max Drawdown:     {run.max_drawdown:>14.2f}%")
        lines.append(f"  Sharpe Ratio:     {run.sharpe_ratio:>15.2f}")
        lines.append(f"  Sortino Ratio:    {run.sortino_ratio:>15.2f}")
        lines.append("")

        # 거래 내역
        if trades:
            lines.append("[ Trades ]")
            lines.append("-" * 60)
            lines.append(f"{'#':>3} {'Entry Time':>12} {'Side':>5} {'Entry':>8} {'Exit':>8} {'PnL':>12}")
            lines.append("-" * 60)

            for i, t in enumerate(trades[:20], 1):  # 최대 20개
                entry_time = t.entry_time.strftime("%m/%d %H:%M") if t.entry_time else "-"
                exit_price = f"{t.exit_price:.2f}" if t.exit_price else "-"
                pnl_str = f"{t.pnl_amount:+,.0f}"
                lines.append(
                    f"{i:>3} {entry_time:>12} {t.side:>5} {t.entry_price:>8.2f} {exit_price:>8} {pnl_str:>12}"
                )

            if len(trades) > 20:
                lines.append(f"  ... and {len(trades) - 20} more trades")
            lines.append("")

        # 일별 지표
        if daily_metrics:
            lines.append("[ Daily Summary ]")
            lines.append("-" * 60)
            lines.append(f"{'Date':>10} {'PnL':>12} {'Trades':>6} {'Win%':>6} {'DD':>8}")
            lines.append("-" * 60)

            for m in daily_metrics[-10:]:  # 최근 10일
                date_str = m.date.strftime("%Y-%m-%d") if m.date else "-"
                lines.append(
                    f"{date_str:>10} {m.pnl:>+12,.0f} {m.trades:>6} {m.win_rate:>5.1f}% {m.drawdown:>7.2f}%"
                )
            lines.append("")

        lines.append("=" * 60)
        lines.append(f"Generated at: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")

        return "\n".join(lines)

    def _generate_html(self, run, trades: List, daily_metrics: List) -> str:
        """HTML 형식 리포트"""
        pnl_color = "green" if run.total_pnl >= 0 else "red"
        ret_color = "green" if run.total_return >= 0 else "red"

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Trading Report - {run.id[:8]}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px; background: #f5f5f5; }}
        .container {{ max-width: 900px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        h1 {{ color: #333; border-bottom: 2px solid #007bff; padding-bottom: 10px; }}
        h2 {{ color: #555; margin-top: 30px; }}
        .info-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 15px; }}
        .metric {{ padding: 15px; background: #f8f9fa; border-radius: 4px; }}
        .metric-label {{ font-size: 12px; color: #666; text-transform: uppercase; }}
        .metric-value {{ font-size: 24px; font-weight: bold; margin-top: 5px; }}
        .positive {{ color: green; }}
        .negative {{ color: red; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
        th, td {{ padding: 10px; text-align: right; border-bottom: 1px solid #ddd; }}
        th {{ background: #f8f9fa; text-align: left; }}
        td:first-child, th:first-child {{ text-align: left; }}
        .footer {{ margin-top: 30px; padding-top: 15px; border-top: 1px solid #ddd; color: #999; font-size: 12px; }}
    </style>
</head>
<body>
<div class="container">
    <h1>Trading Report</h1>

    <div class="info-grid">
        <div class="metric">
            <div class="metric-label">Strategy</div>
            <div class="metric-value">{run.strategy}</div>
        </div>
        <div class="metric">
            <div class="metric-label">Mode</div>
            <div class="metric-value">{run.mode}</div>
        </div>
        <div class="metric">
            <div class="metric-label">Period</div>
            <div class="metric-value" style="font-size: 16px;">{run.start_date} ~ {run.end_date}</div>
        </div>
        <div class="metric">
            <div class="metric-label">Total Trades</div>
            <div class="metric-value">{run.total_trades}</div>
        </div>
    </div>

    <h2>Performance</h2>
    <div class="info-grid">
        <div class="metric">
            <div class="metric-label">Total Return</div>
            <div class="metric-value {ret_color}">{run.total_return:+.2f}%</div>
        </div>
        <div class="metric">
            <div class="metric-label">Total PnL</div>
            <div class="metric-value {pnl_color}">{run.total_pnl:+,.0f} KRW</div>
        </div>
        <div class="metric">
            <div class="metric-label">Win Rate</div>
            <div class="metric-value">{run.win_rate:.1f}%</div>
        </div>
        <div class="metric">
            <div class="metric-label">Max Drawdown</div>
            <div class="metric-value negative">{run.max_drawdown:.2f}%</div>
        </div>
        <div class="metric">
            <div class="metric-label">Profit Factor</div>
            <div class="metric-value">{run.profit_factor:.2f}</div>
        </div>
        <div class="metric">
            <div class="metric-label">Sharpe Ratio</div>
            <div class="metric-value">{run.sharpe_ratio:.2f}</div>
        </div>
    </div>
"""

        if trades:
            html += """
    <h2>Trade History</h2>
    <table>
        <tr><th>#</th><th>Entry Time</th><th>Side</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Reason</th></tr>
"""
            for i, t in enumerate(trades[:50], 1):
                entry_time = t.entry_time.strftime("%Y-%m-%d %H:%M") if t.entry_time else "-"
                exit_price = f"{t.exit_price:.2f}" if t.exit_price else "-"
                pnl_class = "positive" if t.pnl_amount >= 0 else "negative"
                html += f"""        <tr>
            <td>{i}</td>
            <td>{entry_time}</td>
            <td>{t.side}</td>
            <td>{t.entry_price:.2f}</td>
            <td>{exit_price}</td>
            <td class="{pnl_class}">{t.pnl_amount:+,.0f}</td>
            <td>{t.exit_reason or '-'}</td>
        </tr>
"""
            html += "    </table>\n"

        html += f"""
    <div class="footer">
        Run ID: {run.id}<br>
        Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}
    </div>
</div>
</body>
</html>"""

        return html

    def _generate_json(self, run, trades: List, daily_metrics: List) -> str:
        """JSON 형식 리포트"""
        data = {
            'run': run.to_dict(),
            'trades': [t.to_dict() for t in trades],
            'daily_metrics': [m.to_dict() for m in daily_metrics],
            'generated_at': datetime.now(timezone.utc).isoformat(),
        }
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)

    def _generate_markdown(self, run, trades: List, daily_metrics: List) -> str:
        """Markdown 형식 리포트"""
        lines = []
        lines.append(f"# Trading Report: {run.strategy}")
        lines.append("")
        lines.append(f"**Run ID:** `{run.id}`")
        lines.append(f"**Mode:** {run.mode}")
        lines.append(f"**Period:** {run.start_date} ~ {run.end_date}")
        lines.append("")

        lines.append("## Performance Summary")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Initial Capital | {run.initial_capital:,.0f} KRW |")
        lines.append(f"| Final Capital | {run.final_capital:,.0f} KRW |")
        lines.append(f"| Total Return | **{run.total_return:+.2f}%** |")
        lines.append(f"| Total PnL | **{run.total_pnl:+,.0f} KRW** |")
        lines.append(f"| Total Trades | {run.total_trades} |")
        lines.append(f"| Win Rate | {run.win_rate:.1f}% |")
        lines.append(f"| Profit Factor | {run.profit_factor:.2f} |")
        lines.append(f"| Max Drawdown | {run.max_drawdown:.2f}% |")
        lines.append(f"| Sharpe Ratio | {run.sharpe_ratio:.2f} |")
        lines.append("")

        if trades:
            lines.append("## Trade History")
            lines.append("")
            lines.append("| # | Entry Time | Side | Entry | Exit | PnL | Reason |")
            lines.append("|---|------------|------|-------|------|-----|--------|")

            for i, t in enumerate(trades[:30], 1):
                entry_time = t.entry_time.strftime("%m/%d %H:%M") if t.entry_time else "-"
                exit_price = f"{t.exit_price:.2f}" if t.exit_price else "-"
                pnl_str = f"{t.pnl_amount:+,.0f}"
                lines.append(
                    f"| {i} | {entry_time} | {t.side} | {t.entry_price:.2f} | {exit_price} | {pnl_str} | {t.exit_reason or '-'} |"
                )
            lines.append("")

        lines.append("---")
        lines.append(f"*Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}*")

        return "\n".join(lines)

    def compare_runs(self, run_ids: List[str], format: ReportFormat = ReportFormat.TEXT) -> str:
        """여러 실행 결과 비교"""
        if not self.repository:
            raise ValueError("Repository required for comparison")

        runs = [self.repository.get_run(rid) for rid in run_ids]
        runs = [r for r in runs if r]

        if not runs:
            return "No runs found"

        if format == ReportFormat.TEXT:
            return self._compare_text(runs)
        elif format == ReportFormat.MARKDOWN:
            return self._compare_markdown(runs)
        else:
            return self._compare_text(runs)

    def _compare_text(self, runs: List) -> str:
        """텍스트 형식 비교"""
        lines = []
        lines.append("=" * 80)
        lines.append("RUN COMPARISON")
        lines.append("=" * 80)
        lines.append("")

        # 헤더
        header = f"{'Metric':<20}"
        for run in runs:
            header += f"{run.id[:8]:>15}"
        lines.append(header)
        lines.append("-" * 80)

        # 메트릭
        metrics = [
            ("Strategy", lambda r: r.strategy),
            ("Mode", lambda r: r.mode),
            ("Return (%)", lambda r: f"{r.total_return:+.2f}"),
            ("PnL (KRW)", lambda r: f"{r.total_pnl:+,.0f}"),
            ("Trades", lambda r: str(r.total_trades)),
            ("Win Rate (%)", lambda r: f"{r.win_rate:.1f}"),
            ("Profit Factor", lambda r: f"{r.profit_factor:.2f}"),
            ("Max DD (%)", lambda r: f"{r.max_drawdown:.2f}"),
            ("Sharpe", lambda r: f"{r.sharpe_ratio:.2f}"),
        ]

        for name, func in metrics:
            row = f"{name:<20}"
            for run in runs:
                row += f"{func(run):>15}"
            lines.append(row)

        lines.append("")
        lines.append("=" * 80)

        return "\n".join(lines)

    def _compare_markdown(self, runs: List) -> str:
        """Markdown 형식 비교"""
        lines = []
        lines.append("# Run Comparison")
        lines.append("")

        # 테이블 헤더
        header = "| Metric |"
        separator = "|--------|"
        for run in runs:
            header += f" {run.id[:8]} |"
            separator += "--------|"

        lines.append(header)
        lines.append(separator)

        metrics = [
            ("Strategy", lambda r: r.strategy),
            ("Return", lambda r: f"{r.total_return:+.2f}%"),
            ("PnL", lambda r: f"{r.total_pnl:+,.0f}"),
            ("Trades", lambda r: str(r.total_trades)),
            ("Win Rate", lambda r: f"{r.win_rate:.1f}%"),
            ("Max DD", lambda r: f"{r.max_drawdown:.2f}%"),
            ("Sharpe", lambda r: f"{r.sharpe_ratio:.2f}"),
        ]

        for name, func in metrics:
            row = f"| {name} |"
            for run in runs:
                row += f" {func(run)} |"
            lines.append(row)

        return "\n".join(lines)
