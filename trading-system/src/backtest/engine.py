"""
백테스트 엔진
1분 단위 이벤트 루프 기반 시뮬레이션
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any, Protocol
from enum import Enum
import logging
import pandas as pd
import numpy as np

from .position import PositionManager, Position, PositionSide, Trade
from .risk import RiskManager, RiskConfig, ExitReason
from .cost import CostModel, KOSPIMiniCostModel, TradeCost
from .filters import TradingHoursFilter, TradingHoursConfig

logger = logging.getLogger(__name__)


class Signal(Enum):
    """거래 시그널"""
    HOLD = 0
    BUY = 1
    SELL = -1


class Strategy(Protocol):
    """전략 프로토콜 (인터페이스)"""

    def on_bar(self, bar: Dict[str, Any]) -> Signal:
        """
        1분봉 데이터에 대한 시그널 생성

        Args:
            bar: OHLCV + features 데이터
                - datetime: 시간
                - open, high, low, close: 가격
                - volume: 거래량
                - 기타 피처들...

        Returns:
            거래 시그널 (BUY, SELL, HOLD)
        """
        ...


@dataclass
class BacktestConfig:
    """백테스트 설정"""
    # 자본 설정
    initial_capital: float = 10_000_000  # 1천만원

    # 포지션 설정
    position_size: int = 1  # 계약 수

    # 포인트 가치
    point_value: float = 50_000  # KOSPI Mini 1포인트 = 5만원

    # 리스크 설정
    risk_config: RiskConfig = field(default_factory=RiskConfig)

    # 거래 시간 설정
    trading_hours_config: TradingHoursConfig = field(default_factory=TradingHoursConfig)

    # 디버그 모드
    verbose: bool = False


@dataclass
class BacktestResult:
    """백테스트 결과"""
    # 기본 정보
    start_date: datetime
    end_date: datetime
    total_bars: int

    # 수익률
    initial_capital: float
    final_capital: float
    total_return: float  # 총 수익률 (%)
    total_pnl: float     # 총 손익 (원)

    # 거래 통계
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float      # 승률 (%)

    # 수익 분석
    avg_win: float       # 평균 수익 (원)
    avg_loss: float      # 평균 손실 (원)
    profit_factor: float # 수익/손실 비율
    max_win: float       # 최대 수익
    max_loss: float      # 최대 손실

    # 리스크 지표
    max_drawdown: float  # 최대 낙폭 (%)
    sharpe_ratio: float  # 샤프 비율
    sortino_ratio: float # 소르티노 비율

    # 청산 사유별 통계
    exit_reasons: Dict[str, int]

    # 시계열 데이터
    equity_curve: List[float]
    trades: List[Trade]

    def to_dict(self) -> Dict[str, Any]:
        """딕셔너리로 변환"""
        return {
            'start_date': self.start_date.isoformat(),
            'end_date': self.end_date.isoformat(),
            'total_bars': self.total_bars,
            'initial_capital': self.initial_capital,
            'final_capital': self.final_capital,
            'total_return': round(self.total_return, 2),
            'total_pnl': round(self.total_pnl, 0),
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': round(self.win_rate, 2),
            'avg_win': round(self.avg_win, 0),
            'avg_loss': round(self.avg_loss, 0),
            'profit_factor': round(self.profit_factor, 2),
            'max_win': round(self.max_win, 0),
            'max_loss': round(self.max_loss, 0),
            'max_drawdown': round(self.max_drawdown, 2),
            'sharpe_ratio': round(self.sharpe_ratio, 2),
            'sortino_ratio': round(self.sortino_ratio, 2),
            'exit_reasons': self.exit_reasons,
        }

    def print_summary(self):
        """결과 요약 출력"""
        print("\n" + "=" * 50)
        print("백테스트 결과 요약")
        print("=" * 50)
        print(f"기간: {self.start_date.date()} ~ {self.end_date.date()}")
        print(f"총 바 수: {self.total_bars:,}")
        print("-" * 50)
        print(f"초기 자본: {self.initial_capital:,.0f}원")
        print(f"최종 자본: {self.final_capital:,.0f}원")
        print(f"총 수익률: {self.total_return:+.2f}%")
        print(f"총 손익: {self.total_pnl:+,.0f}원")
        print("-" * 50)
        print(f"총 거래: {self.total_trades}회")
        print(f"승리: {self.winning_trades}회 / 패배: {self.losing_trades}회")
        print(f"승률: {self.win_rate:.1f}%")
        print("-" * 50)
        print(f"평균 수익: {self.avg_win:+,.0f}원")
        print(f"평균 손실: {self.avg_loss:+,.0f}원")
        print(f"Profit Factor: {self.profit_factor:.2f}")
        print(f"최대 수익: {self.max_win:+,.0f}원")
        print(f"최대 손실: {self.max_loss:+,.0f}원")
        print("-" * 50)
        print(f"최대 낙폭: {self.max_drawdown:.2f}%")
        print(f"Sharpe Ratio: {self.sharpe_ratio:.2f}")
        print(f"Sortino Ratio: {self.sortino_ratio:.2f}")
        print("-" * 50)
        print("청산 사유:")
        for reason, count in self.exit_reasons.items():
            print(f"  {reason}: {count}회")
        print("=" * 50)


class BacktestEngine:
    """
    백테스트 엔진

    1분 단위 이벤트 루프:
    1. 거래 시간 필터 체크
    2. 리스크 관리 (손절/익절/시간손절) 체크
    3. 전략 시그널 생성
    4. 포지션 진입/청산 실행
    5. 통계 업데이트
    """

    def __init__(
        self,
        strategy: Strategy,
        config: BacktestConfig = None,
        cost_model: CostModel = None
    ):
        self.strategy = strategy
        self.config = config or BacktestConfig()

        # 비용 모델
        self.cost_model = cost_model or KOSPIMiniCostModel(
            point_value=self.config.point_value
        )

        # 포지션 관리자
        self.position_manager = PositionManager(
            initial_capital=self.config.initial_capital
        )

        # 리스크 관리자
        self.risk_manager = RiskManager(
            config=self.config.risk_config,
            point_value=self.config.point_value
        )

        # 거래 시간 필터
        self.time_filter = TradingHoursFilter(
            config=self.config.trading_hours_config
        )

        # 청산 사유 통계
        self.exit_reasons: Dict[str, int] = {}

        # 일별 손익 (Sharpe/Sortino 계산용)
        self.daily_returns: List[float] = []
        self.current_day_pnl: float = 0.0
        self.last_date: Optional[datetime] = None

    def run(self, data: pd.DataFrame) -> BacktestResult:
        """
        백테스트 실행

        Args:
            data: 1분봉 데이터 DataFrame
                필수 컬럼: datetime, open, high, low, close, volume

        Returns:
            백테스트 결과
        """
        if data.empty:
            raise ValueError("Empty data provided")

        # 데이터 정렬
        data = data.sort_values('datetime').reset_index(drop=True)

        logger.info(f"Starting backtest: {len(data)} bars")
        logger.info(f"Period: {data['datetime'].iloc[0]} ~ {data['datetime'].iloc[-1]}")

        # 메인 루프
        for idx, row in data.iterrows():
            self._process_bar(row.to_dict())

        # 마지막 포지션 강제 청산
        if self.position_manager.position.side != PositionSide.FLAT:
            last_bar = data.iloc[-1]
            self._close_position(
                price=last_bar['close'],
                timestamp=last_bar['datetime'],
                reason=ExitReason.FORCED
            )

        # 마지막 일 수익률 기록
        if self.current_day_pnl != 0:
            self.daily_returns.append(self.current_day_pnl)

        # 결과 생성
        return self._generate_result(data)

    def _process_bar(self, bar: Dict[str, Any]):
        """1분봉 처리"""
        timestamp = bar['datetime']
        current_price = bar['close']

        # 일자 변경 시 일별 수익률 기록
        if self.last_date and timestamp.date() != self.last_date.date():
            if self.current_day_pnl != 0:
                self.daily_returns.append(self.current_day_pnl)
            self.current_day_pnl = 0.0
            self.risk_manager.reset_daily(timestamp)

        self.last_date = timestamp

        # 포지션 가격 업데이트
        self.position_manager.update(current_price)

        position = self.position_manager.position

        # 1. 포지션이 있으면 리스크 체크
        if position.side != PositionSide.FLAT:
            # 강제 청산 시간 체크
            if self.time_filter.should_force_close(timestamp):
                self._close_position(current_price, timestamp, ExitReason.FORCED)
                return

            # 리스크 관리자 체크 (손절/익절/시간손절/트레일링)
            exit_reason = self.risk_manager.check_exit(
                position, current_price, timestamp
            )
            if exit_reason:
                self._close_position(current_price, timestamp, exit_reason)
                return

        # 2. 거래 가능 시간 및 리스크 한도 체크
        if not self.time_filter.can_open_position(timestamp):
            if self.config.verbose:
                reason = self.time_filter.get_exclusion_reason(timestamp)
                logger.debug(f"[{timestamp}] Trading disabled: {reason}")
            return

        if not self.risk_manager.can_trade():
            return

        # 3. 전략 시그널 생성
        signal = self.strategy.on_bar(bar)

        # 4. 시그널에 따른 액션
        if position.side == PositionSide.FLAT:
            # 포지션 없음 → 진입
            if signal == Signal.BUY:
                self._open_position(PositionSide.LONG, current_price, timestamp)
            elif signal == Signal.SELL:
                self._open_position(PositionSide.SHORT, current_price, timestamp)

        elif position.side == PositionSide.LONG:
            # 롱 포지션 → 시그널 청산
            if signal == Signal.SELL:
                self._close_position(current_price, timestamp, ExitReason.SIGNAL)

        elif position.side == PositionSide.SHORT:
            # 숏 포지션 → 시그널 청산
            if signal == Signal.BUY:
                self._close_position(current_price, timestamp, ExitReason.SIGNAL)

    def _open_position(
        self,
        side: PositionSide,
        price: float,
        timestamp: datetime
    ):
        """포지션 진입"""
        # 비용 계산
        cost = self.cost_model.calculate(
            price=price,
            quantity=self.config.position_size,
            is_entry=True
        )

        # commission은 원화이므로 포인트로 변환
        commission_in_points = cost.commission / self.config.point_value

        success = self.position_manager.open_position(
            side=side,
            price=price,
            quantity=self.config.position_size,
            timestamp=timestamp,
            commission=commission_in_points,
            slippage=cost.slippage
        )

        if success and self.config.verbose:
            logger.info(
                f"[{timestamp}] OPEN {side.value}: "
                f"{self.config.position_size} @ {price:.2f} "
                f"(cost: {cost.total:.0f})"
            )

    def _close_position(
        self,
        price: float,
        timestamp: datetime,
        reason: ExitReason
    ):
        """포지션 청산"""
        # 비용 계산
        cost = self.cost_model.calculate(
            price=price,
            quantity=self.position_manager.position.quantity,
            is_entry=False
        )

        # commission은 원화이므로 포인트로 변환
        commission_in_points = cost.commission / self.config.point_value

        pnl = self.position_manager.close_position(
            price=price,
            timestamp=timestamp,
            commission=commission_in_points,
            slippage=cost.slippage
        )

        if pnl is not None:
            # 손익을 포인트 가치로 환산
            pnl_points = pnl * self.config.point_value

            # 일별 손익 및 리스크 통계 업데이트
            self.current_day_pnl += pnl_points
            self.risk_manager.update_daily_stats(pnl_points)

            # 청산 사유 기록
            reason_name = reason.value
            self.exit_reasons[reason_name] = self.exit_reasons.get(reason_name, 0) + 1

            if self.config.verbose:
                logger.info(
                    f"[{timestamp}] CLOSE ({reason_name}): "
                    f"PnL = {pnl_points:+,.0f}원"
                )

    def _generate_result(self, data: pd.DataFrame) -> BacktestResult:
        """백테스트 결과 생성"""
        stats = self.position_manager.get_stats()

        # 수익률 계산
        initial_capital = self.config.initial_capital
        final_capital = self.position_manager.capital
        total_return = (final_capital - initial_capital) / initial_capital * 100

        # 평균 수익/손실
        trades = self.position_manager.trades
        closed_trades = [t for t in trades if t.pnl != 0]

        wins = [t.pnl * self.config.point_value for t in closed_trades if t.pnl > 0]
        losses = [t.pnl * self.config.point_value for t in closed_trades if t.pnl < 0]

        avg_win = np.mean(wins) if wins else 0
        avg_loss = np.mean(losses) if losses else 0

        # 최대 낙폭 계산
        equity_curve = [e for e in self.position_manager.equity_curve]
        max_drawdown = self._calculate_max_drawdown(equity_curve)

        # Sharpe/Sortino 계산
        sharpe_ratio = self._calculate_sharpe_ratio(self.daily_returns)
        sortino_ratio = self._calculate_sortino_ratio(self.daily_returns)

        return BacktestResult(
            start_date=data['datetime'].iloc[0],
            end_date=data['datetime'].iloc[-1],
            total_bars=len(data),
            initial_capital=initial_capital,
            final_capital=final_capital,
            total_return=total_return,
            total_pnl=final_capital - initial_capital,
            total_trades=stats['total_trades'],
            winning_trades=stats['winning_trades'],
            losing_trades=stats['losing_trades'],
            win_rate=stats['win_rate'],
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=stats['profit_factor'],
            max_win=stats['max_win'] * self.config.point_value if stats['max_win'] else 0,
            max_loss=stats['max_loss'] * self.config.point_value if stats['max_loss'] else 0,
            max_drawdown=max_drawdown,
            sharpe_ratio=sharpe_ratio,
            sortino_ratio=sortino_ratio,
            exit_reasons=self.exit_reasons,
            equity_curve=equity_curve,
            trades=trades
        )

    def _calculate_max_drawdown(self, equity_curve: List[float]) -> float:
        """최대 낙폭 계산"""
        if not equity_curve:
            return 0.0

        peak = equity_curve[0]
        max_dd = 0.0

        for equity in equity_curve:
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100
            if dd > max_dd:
                max_dd = dd

        return max_dd

    def _calculate_sharpe_ratio(
        self,
        daily_returns: List[float],
        risk_free_rate: float = 0.03
    ) -> float:
        """
        샤프 비율 계산

        Sharpe = (mean(returns) - risk_free) / std(returns)
        연환산 (sqrt(252))
        """
        if len(daily_returns) < 2:
            return 0.0

        returns = np.array(daily_returns)
        mean_return = np.mean(returns)
        std_return = np.std(returns, ddof=1)

        if std_return == 0:
            return 0.0

        # 일별 risk-free rate
        daily_rf = risk_free_rate / 252

        sharpe = (mean_return - daily_rf) / std_return
        # 연환산
        return sharpe * np.sqrt(252)

    def _calculate_sortino_ratio(
        self,
        daily_returns: List[float],
        risk_free_rate: float = 0.03
    ) -> float:
        """
        소르티노 비율 계산

        Sortino = (mean(returns) - risk_free) / downside_std
        하방 변동성만 고려
        """
        if len(daily_returns) < 2:
            return 0.0

        returns = np.array(daily_returns)
        mean_return = np.mean(returns)

        # 하방 수익률만
        negative_returns = returns[returns < 0]
        if len(negative_returns) < 2:
            return float('inf') if mean_return > 0 else 0.0

        downside_std = np.std(negative_returns, ddof=1)

        if downside_std == 0:
            return 0.0

        daily_rf = risk_free_rate / 252
        sortino = (mean_return - daily_rf) / downside_std
        return sortino * np.sqrt(252)


# 간단한 테스트용 전략
class SimpleMovingAverageStrategy:
    """
    간단한 이동평균 교차 전략 (테스트용)

    - 단기 MA > 장기 MA: 매수
    - 단기 MA < 장기 MA: 매도
    """

    def __init__(self, short_period: int = 5, long_period: int = 20):
        self.short_period = short_period
        self.long_period = long_period
        self.prices: List[float] = []

    def on_bar(self, bar: Dict[str, Any]) -> Signal:
        self.prices.append(bar['close'])

        if len(self.prices) < self.long_period:
            return Signal.HOLD

        short_ma = np.mean(self.prices[-self.short_period:])
        long_ma = np.mean(self.prices[-self.long_period:])

        if short_ma > long_ma:
            return Signal.BUY
        elif short_ma < long_ma:
            return Signal.SELL
        else:
            return Signal.HOLD
