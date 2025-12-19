"""
SQLite 데이터베이스 모델 (SQLAlchemy ORM)

백테스트/모의투자 실행 결과 저장
"""
from datetime import datetime
from enum import Enum
from typing import Optional, List
import json
import uuid

from sqlalchemy import (
    Column, String, Integer, Float, Text, DateTime, ForeignKey,
    create_engine, Index
)
from sqlalchemy.orm import (
    declarative_base, relationship, Session
)


Base = declarative_base()


class RunMode(str, Enum):
    """실행 모드"""
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class Run(Base):
    """
    실행 기록

    백테스트, 모의투자, 실거래 실행마다 하나의 Run 생성
    """
    __tablename__ = 'runs'

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    strategy = Column(String(100), nullable=False, index=True)
    mode = Column(String(20), nullable=False, index=True)  # backtest, paper, live
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)

    # 설정 (JSON)
    config = Column(Text, default='{}')

    # 요약 성과 지표
    initial_capital = Column(Float, default=10_000_000)
    final_capital = Column(Float, default=10_000_000)
    total_return = Column(Float, default=0.0)  # 퍼센트
    total_pnl = Column(Float, default=0.0)     # 원화
    total_trades = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    losing_trades = Column(Integer, default=0)
    win_rate = Column(Float, default=0.0)
    profit_factor = Column(Float, default=0.0)
    max_drawdown = Column(Float, default=0.0)
    sharpe_ratio = Column(Float, default=0.0)
    sortino_ratio = Column(Float, default=0.0)

    # 메타데이터
    created_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text, default='')

    # 관계
    trades = relationship("Trade", back_populates="run", cascade="all, delete-orphan")
    daily_metrics = relationship("DailyMetric", back_populates="run", cascade="all, delete-orphan")

    def set_config(self, config_dict: dict):
        """설정 딕셔너리 저장"""
        self.config = json.dumps(config_dict, ensure_ascii=False)

    def get_config(self) -> dict:
        """설정 딕셔너리 로드"""
        return json.loads(self.config) if self.config else {}

    def to_dict(self) -> dict:
        """딕셔너리 변환"""
        return {
            'id': self.id,
            'strategy': self.strategy,
            'mode': self.mode,
            'start_date': self.start_date.isoformat() if self.start_date else None,
            'end_date': self.end_date.isoformat() if self.end_date else None,
            'initial_capital': self.initial_capital,
            'final_capital': self.final_capital,
            'total_return': round(self.total_return, 2),
            'total_pnl': round(self.total_pnl, 0),
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': round(self.win_rate, 2),
            'profit_factor': round(self.profit_factor, 2),
            'max_drawdown': round(self.max_drawdown, 2),
            'sharpe_ratio': round(self.sharpe_ratio, 2),
            'sortino_ratio': round(self.sortino_ratio, 2),
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return (
            f"<Run(id={self.id[:8]}, strategy={self.strategy}, "
            f"mode={self.mode}, return={self.total_return:.2f}%)>"
        )


class Trade(Base):
    """
    거래 기록

    개별 거래 (진입 + 청산 쌍)
    """
    __tablename__ = 'trades'

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), ForeignKey('runs.id'), nullable=False, index=True)

    # 거래 정보
    entry_time = Column(DateTime, nullable=False)
    exit_time = Column(DateTime)
    side = Column(String(10), nullable=False)  # LONG, SHORT
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float)
    quantity = Column(Integer, default=1)

    # 손익
    pnl = Column(Float, default=0.0)          # 포인트
    pnl_amount = Column(Float, default=0.0)   # 원화
    commission = Column(Float, default=0.0)

    # 청산 사유
    exit_reason = Column(String(50))  # SIGNAL, STOP_LOSS, TAKE_PROFIT, TIME_STOP, etc.

    # 관계
    run = relationship("Run", back_populates="trades")

    # 인덱스
    __table_args__ = (
        Index('ix_trades_run_entry', 'run_id', 'entry_time'),
    )

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'run_id': self.run_id,
            'entry_time': self.entry_time.isoformat() if self.entry_time else None,
            'exit_time': self.exit_time.isoformat() if self.exit_time else None,
            'side': self.side,
            'entry_price': self.entry_price,
            'exit_price': self.exit_price,
            'quantity': self.quantity,
            'pnl': self.pnl,
            'pnl_amount': self.pnl_amount,
            'commission': self.commission,
            'exit_reason': self.exit_reason,
        }

    def __repr__(self):
        return (
            f"<Trade(id={self.id}, side={self.side}, "
            f"pnl={self.pnl_amount:+,.0f})>"
        )


class DailyMetric(Base):
    """
    일별 성과 지표

    일별 집계 데이터
    """
    __tablename__ = 'daily_metrics'

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), ForeignKey('runs.id'), nullable=False, index=True)

    # 날짜
    date = Column(DateTime, nullable=False)

    # 성과 지표
    pnl = Column(Float, default=0.0)              # 당일 손익 (원화)
    cumulative_pnl = Column(Float, default=0.0)   # 누적 손익
    trades = Column(Integer, default=0)           # 당일 거래 수
    wins = Column(Integer, default=0)
    losses = Column(Integer, default=0)
    win_rate = Column(Float, default=0.0)

    # 리스크 지표
    equity = Column(Float, default=0.0)           # 당일 종료 자산
    drawdown = Column(Float, default=0.0)         # 당일 낙폭
    max_drawdown = Column(Float, default=0.0)     # 누적 MDD

    # 관계
    run = relationship("Run", back_populates="daily_metrics")

    # 인덱스
    __table_args__ = (
        Index('ix_daily_run_date', 'run_id', 'date'),
    )

    def to_dict(self) -> dict:
        return {
            'date': self.date.strftime('%Y-%m-%d') if self.date else None,
            'pnl': round(self.pnl, 0),
            'cumulative_pnl': round(self.cumulative_pnl, 0),
            'trades': self.trades,
            'wins': self.wins,
            'losses': self.losses,
            'win_rate': round(self.win_rate, 2),
            'equity': round(self.equity, 0),
            'drawdown': round(self.drawdown, 2),
            'max_drawdown': round(self.max_drawdown, 2),
        }

    def __repr__(self):
        return (
            f"<DailyMetric(date={self.date}, pnl={self.pnl:+,.0f})>"
        )
