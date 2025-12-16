"""
Phase 7 테스트 - CLI, Result DB, Paper Trading, Reporting
"""
import pytest
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import os


# ========== Database Tests ==========

class TestResultDB:
    """Result DB 테스트"""

    def test_repository_creation(self):
        """Repository 생성 테스트"""
        from src.database import ResultRepository

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            repo = ResultRepository(db_path)
            assert repo is not None
            assert db_path.exists()

    def test_create_run(self):
        """Run 생성 테스트"""
        from src.database import ResultRepository

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            repo = ResultRepository(db_path)

            run = repo.create_run(
                strategy="hybrid",
                mode="backtest",
                start_date=datetime(2024, 1, 1),
                end_date=datetime(2024, 12, 31),
                config={"capital": 10_000_000},
                initial_capital=10_000_000,
            )

            assert run is not None
            assert run.strategy == "hybrid"
            assert run.mode == "backtest"
            assert run.initial_capital == 10_000_000

    def test_update_run_results(self):
        """Run 결과 업데이트 테스트"""
        from src.database import ResultRepository

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            repo = ResultRepository(db_path)

            run = repo.create_run(
                strategy="hybrid",
                mode="backtest",
                start_date=datetime(2024, 1, 1),
                end_date=datetime(2024, 12, 31),
            )

            repo.update_run_results(
                run_id=run.id,
                final_capital=11_000_000,
                total_return=10.0,
                total_pnl=1_000_000,
                total_trades=50,
                winning_trades=30,
                losing_trades=20,
                win_rate=60.0,
                profit_factor=1.5,
                max_drawdown=5.0,
            )

            updated = repo.get_run(run.id)
            assert updated.final_capital == 11_000_000
            assert updated.total_return == 10.0
            assert updated.total_trades == 50
            assert updated.win_rate == 60.0

    def test_add_trades(self):
        """거래 기록 추가 테스트"""
        from src.database import ResultRepository

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            repo = ResultRepository(db_path)

            run = repo.create_run(
                strategy="hybrid",
                mode="backtest",
                start_date=datetime(2024, 1, 1),
                end_date=datetime(2024, 12, 31),
            )

            trade = repo.add_trade(
                run_id=run.id,
                entry_time=datetime(2024, 1, 15, 10, 0),
                side="LONG",
                entry_price=350.0,
                exit_time=datetime(2024, 1, 15, 14, 0),
                exit_price=352.0,
                pnl=2.0,
                pnl_amount=500_000,
            )

            assert trade is not None
            assert trade.side == "LONG"
            assert trade.pnl_amount == 500_000

            trades = repo.get_trades(run.id)
            assert len(trades) == 1

    def test_add_trades_bulk(self):
        """거래 기록 일괄 추가 테스트"""
        from src.database import ResultRepository

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            repo = ResultRepository(db_path)

            run = repo.create_run(
                strategy="hybrid",
                mode="backtest",
                start_date=datetime(2024, 1, 1),
                end_date=datetime(2024, 12, 31),
            )

            trades_data = [
                {
                    "entry_time": datetime(2024, 1, i, 10, 0),
                    "side": "LONG" if i % 2 == 0 else "SHORT",
                    "entry_price": 350.0 + i,
                    "exit_time": datetime(2024, 1, i, 14, 0),
                    "exit_price": 351.0 + i,
                    "pnl": 1.0,
                    "pnl_amount": 250_000,
                }
                for i in range(1, 11)
            ]

            repo.add_trades_bulk(run.id, trades_data)

            trades = repo.get_trades(run.id)
            assert len(trades) == 10

    def test_get_runs_with_filter(self):
        """Run 목록 필터링 테스트"""
        from src.database import ResultRepository

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            repo = ResultRepository(db_path)

            # 여러 전략으로 Run 생성
            for strategy in ["hybrid", "hybrid", "mean_reversion"]:
                repo.create_run(
                    strategy=strategy,
                    mode="backtest",
                    start_date=datetime(2024, 1, 1),
                    end_date=datetime(2024, 12, 31),
                )

            all_runs = repo.get_runs()
            assert len(all_runs) == 3

            hybrid_runs = repo.get_runs(strategy="hybrid")
            assert len(hybrid_runs) == 2

    def test_strategy_stats(self):
        """전략 통계 테스트"""
        from src.database import ResultRepository

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            repo = ResultRepository(db_path)

            # Run 생성 및 결과 업데이트
            for i in range(3):
                run = repo.create_run(
                    strategy="hybrid",
                    mode="backtest",
                    start_date=datetime(2024, 1, 1),
                    end_date=datetime(2024, 12, 31),
                )
                repo.update_run_results(
                    run_id=run.id,
                    final_capital=11_000_000 + i * 500_000,
                    total_return=10.0 + i * 5,
                    total_pnl=1_000_000 + i * 500_000,
                    total_trades=50,
                    winning_trades=30,
                    losing_trades=20,
                    win_rate=60.0,
                    profit_factor=1.5,
                    max_drawdown=5.0 + i,
                )

            stats = repo.get_strategy_stats("hybrid")
            assert stats["total_runs"] == 3
            assert stats["avg_return"] == 15.0  # (10 + 15 + 20) / 3


# ========== Virtual Broker Tests ==========

class TestVirtualBroker:
    """Virtual Broker 테스트"""

    def test_broker_creation(self):
        """브로커 생성 테스트"""
        from src.paper_trading import VirtualBroker

        broker = VirtualBroker(initial_capital=10_000_000)
        assert broker.capital == 10_000_000
        assert broker.equity == 10_000_000
        assert not broker.position.is_open

    def test_market_order_buy(self):
        """시장가 매수 주문 테스트"""
        from src.paper_trading import VirtualBroker, OrderSide, OrderType

        broker = VirtualBroker(initial_capital=10_000_000)
        broker.update_price(350.0, bid=349.95, ask=350.05)

        order = broker.submit_order(
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
        )

        assert order.status.value == "FILLED"
        assert broker.position.is_open
        assert broker.position.side.value == "LONG"

    def test_market_order_sell(self):
        """시장가 매도 주문 테스트"""
        from src.paper_trading import VirtualBroker, OrderSide, OrderType

        broker = VirtualBroker(initial_capital=10_000_000)
        broker.update_price(350.0, bid=349.95, ask=350.05)

        order = broker.submit_order(
            side=OrderSide.SELL,
            quantity=1,
            order_type=OrderType.MARKET,
        )

        assert order.status.value == "FILLED"
        assert broker.position.is_open
        assert broker.position.side.value == "SHORT"

    def test_close_position(self):
        """포지션 청산 테스트"""
        from src.paper_trading import VirtualBroker, OrderSide

        broker = VirtualBroker(initial_capital=10_000_000)
        broker.update_price(350.0)

        # 진입
        broker.submit_order(OrderSide.BUY, quantity=1)
        assert broker.position.is_open

        # 가격 상승
        broker.update_price(352.0)

        # 청산
        broker.close_position(reason="TEST")
        assert not broker.position.is_open

        # PnL 확인 (2포인트 * 250,000 = 500,000 - 수수료)
        assert broker.total_pnl > 0
        assert len(broker.trades) == 1

    def test_pnl_calculation_long(self):
        """롱 포지션 PnL 계산 테스트"""
        from src.paper_trading import VirtualBroker, OrderSide

        broker = VirtualBroker(
            initial_capital=10_000_000,
            tick_value=250_000,
            tick_size=0.05,
            commission_rate=0,  # 수수료 없이 테스트
            slippage_ticks=0,   # 슬리피지 없이 테스트
        )
        broker.update_price(350.0)

        # 롱 진입 @ 350
        broker.submit_order(OrderSide.BUY, quantity=1)

        # 가격 상승 @ 352 (2포인트 = 40틱)
        broker.update_price(352.0)

        # 청산
        broker.close_position()

        # 40틱 * 250,000 = 10,000,000
        expected_pnl = (352.0 - 350.0) / 0.05 * 250_000
        assert abs(broker.trades[-1].pnl_amount - expected_pnl) < 1

    def test_pnl_calculation_short(self):
        """숏 포지션 PnL 계산 테스트"""
        from src.paper_trading import VirtualBroker, OrderSide

        broker = VirtualBroker(
            initial_capital=10_000_000,
            tick_value=250_000,
            tick_size=0.05,
            commission_rate=0,
            slippage_ticks=0,
        )
        broker.update_price(350.0)

        # 숏 진입 @ 350
        broker.submit_order(OrderSide.SELL, quantity=1)

        # 가격 하락 @ 348 (2포인트 = 40틱)
        broker.update_price(348.0)

        # 청산
        broker.close_position()

        expected_pnl = (350.0 - 348.0) / 0.05 * 250_000
        assert abs(broker.trades[-1].pnl_amount - expected_pnl) < 1

    def test_unrealized_pnl(self):
        """미실현 손익 테스트"""
        from src.paper_trading import VirtualBroker, OrderSide

        broker = VirtualBroker(
            initial_capital=10_000_000,
            tick_value=250_000,
            tick_size=0.05,
            commission_rate=0,
            slippage_ticks=0,
        )
        broker.update_price(350.0)

        # 롱 진입
        broker.submit_order(OrderSide.BUY, quantity=1)

        # 가격 상승
        broker.update_price(351.0)

        # 미실현 손익 확인
        expected_unrealized = (351.0 - 350.0) / 0.05 * 250_000
        assert abs(broker.position.unrealized_pnl - expected_unrealized) < 1


# ========== Report Generator Tests ==========

class TestReportGenerator:
    """리포트 생성기 테스트"""

    def test_text_report(self):
        """텍스트 리포트 생성 테스트"""
        from src.database import ResultRepository
        from src.reporting import ReportGenerator, ReportFormat

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            repo = ResultRepository(db_path)

            run = repo.create_run(
                strategy="hybrid",
                mode="backtest",
                start_date=datetime(2024, 1, 1),
                end_date=datetime(2024, 12, 31),
            )
            repo.update_run_results(
                run_id=run.id,
                final_capital=11_000_000,
                total_return=10.0,
                total_pnl=1_000_000,
                total_trades=50,
                winning_trades=30,
                losing_trades=20,
                win_rate=60.0,
                profit_factor=1.5,
                max_drawdown=5.0,
            )

            # DB에서 업데이트된 run 다시 로드
            updated_run = repo.get_run(run.id)

            generator = ReportGenerator(repository=repo)
            report = generator.generate(run=updated_run, format=ReportFormat.TEXT)

            assert "hybrid" in report
            assert "10.00%" in report or "+10.00%" in report
            assert "1,000,000" in report

    def test_html_report(self):
        """HTML 리포트 생성 테스트"""
        from src.database import ResultRepository
        from src.reporting import ReportGenerator, ReportFormat

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            repo = ResultRepository(db_path)

            run = repo.create_run(
                strategy="hybrid",
                mode="backtest",
                start_date=datetime(2024, 1, 1),
                end_date=datetime(2024, 12, 31),
            )

            generator = ReportGenerator(repository=repo)
            report = generator.generate(run=run, format=ReportFormat.HTML)

            assert "<!DOCTYPE html>" in report
            assert "hybrid" in report

    def test_json_report(self):
        """JSON 리포트 생성 테스트"""
        from src.database import ResultRepository
        from src.reporting import ReportGenerator, ReportFormat
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            repo = ResultRepository(db_path)

            run = repo.create_run(
                strategy="hybrid",
                mode="backtest",
                start_date=datetime(2024, 1, 1),
                end_date=datetime(2024, 12, 31),
            )

            generator = ReportGenerator(repository=repo)
            report = generator.generate(run=run, format=ReportFormat.JSON)

            data = json.loads(report)
            assert "run" in data
            assert data["run"]["strategy"] == "hybrid"

    def test_save_report(self):
        """리포트 파일 저장 테스트"""
        from src.database import ResultRepository
        from src.reporting import ReportGenerator, ReportFormat

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            repo = ResultRepository(db_path)

            run = repo.create_run(
                strategy="hybrid",
                mode="backtest",
                start_date=datetime(2024, 1, 1),
                end_date=datetime(2024, 12, 31),
            )

            generator = ReportGenerator(repository=repo)
            output_path = Path(tmpdir) / "report.html"

            generator.save(run.id, output_path, format=ReportFormat.HTML)

            assert output_path.exists()
            content = output_path.read_text()
            assert "<!DOCTYPE html>" in content


# ========== CLI Tests ==========

class TestCLI:
    """CLI 테스트"""

    def test_cli_import(self):
        """CLI 모듈 임포트 테스트"""
        from src.cli import app
        assert app is not None

    def test_strategies_command(self):
        """strategies 명령어 테스트"""
        from typer.testing import CliRunner
        from src.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["strategies"])

        assert result.exit_code == 0
        assert "hybrid" in result.stdout
        assert "mean_reversion" in result.stdout

    def test_version_command(self):
        """version 명령어 테스트"""
        from typer.testing import CliRunner
        from src.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["version"])

        assert result.exit_code == 0
        assert "STS" in result.stdout or "0.1.0" in result.stdout
