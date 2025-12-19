"""
Result Repository

데이터베이스 접근 레이어
"""
from datetime import datetime, date
from typing import List, Optional, Dict, Any
from pathlib import Path
import os

from sqlalchemy import create_engine, desc, func
from sqlalchemy.orm import sessionmaker, Session

from .models import Base, Run, Trade, DailyMetric, RunMode


def get_db_path() -> Path:
    """데이터베이스 파일 경로 반환"""
    # 환경변수 또는 기본 경로
    db_path = os.getenv('RESULT_DB_PATH')
    if db_path:
        return Path(db_path)

    # 기본: trading-system/data/results.db
    base_dir = Path(__file__).parent.parent.parent
    data_dir = base_dir / 'data'
    data_dir.mkdir(exist_ok=True)
    return data_dir / 'results.db'


def get_engine(db_path: Optional[Path] = None):
    """SQLAlchemy 엔진 생성"""
    path = db_path or get_db_path()
    return create_engine(f'sqlite:///{path}', echo=False)


def init_db(db_path: Optional[Path] = None):
    """데이터베이스 초기화 (테이블 생성)"""
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    return engine


class ResultRepository:
    """
    Result Repository

    백테스트/모의투자 결과 저장 및 조회
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.engine = get_engine(db_path)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def _get_session(self) -> Session:
        """세션 생성"""
        return self.Session()

    # ========== Run ==========

    def create_run(
        self,
        strategy: str,
        mode: str,
        start_date: datetime,
        end_date: datetime,
        config: dict = None,
        initial_capital: float = 10_000_000,
    ) -> Run:
        """새 실행 기록 생성"""
        with self._get_session() as session:
            run = Run(
                strategy=strategy,
                mode=mode,
                start_date=start_date,
                end_date=end_date,
                initial_capital=initial_capital,
                final_capital=initial_capital,
            )
            if config:
                run.set_config(config)

            session.add(run)
            session.commit()
            session.refresh(run)

            # detach from session
            run_id = run.id
            return self.get_run(run_id)

    def update_run_results(
        self,
        run_id: str,
        final_capital: float,
        total_return: float,
        total_pnl: float,
        total_trades: int,
        winning_trades: int,
        losing_trades: int,
        win_rate: float,
        profit_factor: float,
        max_drawdown: float,
        sharpe_ratio: float = 0.0,
        sortino_ratio: float = 0.0,
    ):
        """실행 결과 업데이트"""
        with self._get_session() as session:
            run = session.query(Run).filter(Run.id == run_id).first()
            if run:
                run.final_capital = final_capital
                run.total_return = total_return
                run.total_pnl = total_pnl
                run.total_trades = total_trades
                run.winning_trades = winning_trades
                run.losing_trades = losing_trades
                run.win_rate = win_rate
                run.profit_factor = profit_factor
                run.max_drawdown = max_drawdown
                run.sharpe_ratio = sharpe_ratio
                run.sortino_ratio = sortino_ratio
                session.commit()

    def get_run(self, run_id: str) -> Optional[Run]:
        """ID로 실행 기록 조회"""
        with self._get_session() as session:
            run = session.query(Run).filter(Run.id == run_id).first()
            if run:
                # Detach and return copy
                session.expunge(run)
                return run
            return None

    def get_runs(
        self,
        strategy: str = None,
        mode: str = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Run]:
        """실행 기록 목록 조회"""
        with self._get_session() as session:
            query = session.query(Run)

            if strategy:
                query = query.filter(Run.strategy == strategy)
            if mode:
                query = query.filter(Run.mode == mode)

            runs = query.order_by(desc(Run.created_at)).offset(offset).limit(limit).all()

            # Detach
            for run in runs:
                session.expunge(run)

            return runs

    def get_latest_run(self, strategy: str, mode: str = None) -> Optional[Run]:
        """특정 전략의 최신 실행 기록"""
        with self._get_session() as session:
            query = session.query(Run).filter(Run.strategy == strategy)
            if mode:
                query = query.filter(Run.mode == mode)

            run = query.order_by(desc(Run.created_at)).first()
            if run:
                session.expunge(run)
                return run
            return None

    def delete_run(self, run_id: str):
        """실행 기록 삭제 (관련 거래/일별 지표도 삭제)"""
        with self._get_session() as session:
            run = session.query(Run).filter(Run.id == run_id).first()
            if run:
                session.delete(run)
                session.commit()

    # ========== Trade ==========

    def add_trade(
        self,
        run_id: str,
        entry_time: datetime,
        side: str,
        entry_price: float,
        quantity: int = 1,
        exit_time: datetime = None,
        exit_price: float = None,
        pnl: float = 0.0,
        pnl_amount: float = 0.0,
        commission: float = 0.0,
        exit_reason: str = None,
    ) -> Trade:
        """거래 기록 추가"""
        with self._get_session() as session:
            trade = Trade(
                run_id=run_id,
                entry_time=entry_time,
                exit_time=exit_time,
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                pnl=pnl,
                pnl_amount=pnl_amount,
                commission=commission,
                exit_reason=exit_reason,
            )
            session.add(trade)
            session.commit()
            session.refresh(trade)

            trade_id = trade.id
            session.expunge(trade)
            return trade

    def add_trades_bulk(self, run_id: str, trades_data: List[Dict[str, Any]]):
        """거래 기록 일괄 추가"""
        with self._get_session() as session:
            trades = [
                Trade(run_id=run_id, **data)
                for data in trades_data
            ]
            session.add_all(trades)
            session.commit()

    def get_trades(self, run_id: str) -> List[Trade]:
        """특정 실행의 모든 거래 조회"""
        with self._get_session() as session:
            trades = session.query(Trade).filter(
                Trade.run_id == run_id
            ).order_by(Trade.entry_time).all()

            for trade in trades:
                session.expunge(trade)

            return trades

    # ========== DailyMetric ==========

    def add_daily_metric(
        self,
        run_id: str,
        date: datetime,
        pnl: float = 0.0,
        cumulative_pnl: float = 0.0,
        trades: int = 0,
        wins: int = 0,
        losses: int = 0,
        win_rate: float = 0.0,
        equity: float = 0.0,
        drawdown: float = 0.0,
        max_drawdown: float = 0.0,
    ) -> DailyMetric:
        """일별 지표 추가"""
        with self._get_session() as session:
            metric = DailyMetric(
                run_id=run_id,
                date=date,
                pnl=pnl,
                cumulative_pnl=cumulative_pnl,
                trades=trades,
                wins=wins,
                losses=losses,
                win_rate=win_rate,
                equity=equity,
                drawdown=drawdown,
                max_drawdown=max_drawdown,
            )
            session.add(metric)
            session.commit()
            session.refresh(metric)

            session.expunge(metric)
            return metric

    def add_daily_metrics_bulk(self, run_id: str, metrics_data: List[Dict[str, Any]]):
        """일별 지표 일괄 추가"""
        with self._get_session() as session:
            metrics = [
                DailyMetric(run_id=run_id, **data)
                for data in metrics_data
            ]
            session.add_all(metrics)
            session.commit()

    def get_daily_metrics(self, run_id: str) -> List[DailyMetric]:
        """특정 실행의 일별 지표 조회"""
        with self._get_session() as session:
            metrics = session.query(DailyMetric).filter(
                DailyMetric.run_id == run_id
            ).order_by(DailyMetric.date).all()

            for metric in metrics:
                session.expunge(metric)

            return metrics

    # ========== Analytics ==========

    def get_strategy_stats(self, strategy: str, mode: str = None) -> Dict[str, Any]:
        """전략별 통계 요약"""
        with self._get_session() as session:
            query = session.query(Run).filter(Run.strategy == strategy)
            if mode:
                query = query.filter(Run.mode == mode)

            runs = query.all()

            if not runs:
                return {}

            total_returns = [r.total_return for r in runs]
            win_rates = [r.win_rate for r in runs if r.total_trades > 0]
            mdds = [r.max_drawdown for r in runs]

            return {
                'strategy': strategy,
                'total_runs': len(runs),
                'avg_return': sum(total_returns) / len(total_returns) if total_returns else 0,
                'best_return': max(total_returns) if total_returns else 0,
                'worst_return': min(total_returns) if total_returns else 0,
                'avg_win_rate': sum(win_rates) / len(win_rates) if win_rates else 0,
                'avg_max_drawdown': sum(mdds) / len(mdds) if mdds else 0,
            }

    def compare_strategies(self, strategies: List[str], mode: str = None) -> List[Dict[str, Any]]:
        """전략 간 비교"""
        return [
            self.get_strategy_stats(s, mode)
            for s in strategies
        ]

    def get_equity_curve(self, run_id: str) -> List[Dict[str, Any]]:
        """자산 곡선 데이터"""
        metrics = self.get_daily_metrics(run_id)
        return [
            {'date': m.date, 'equity': m.equity, 'drawdown': m.drawdown}
            for m in metrics
        ]
