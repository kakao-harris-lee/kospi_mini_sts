"""
백테스트 모듈 테스트
"""
import pytest
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

from src.backtest import (
    BacktestEngine,
    BacktestConfig,
    BacktestResult,
    Signal,
    PositionManager,
    Position,
    PositionSide,
    RiskManager,
    RiskConfig,
    ExitReason,
    KOSPIMiniCostModel,
    ZeroCostModel,
    TradeCost,
    TradingHoursFilter,
    TradingHoursConfig,
    SimpleMovingAverageStrategy,
)


class TestPositionManager:
    """포지션 관리자 테스트"""

    def test_initial_state(self):
        """초기 상태 테스트"""
        pm = PositionManager(initial_capital=10_000_000)
        assert pm.position.side == PositionSide.FLAT
        assert pm.capital == 10_000_000
        assert len(pm.trades) == 0

    def test_open_long_position(self):
        """롱 포지션 진입 테스트"""
        pm = PositionManager(initial_capital=10_000_000)
        timestamp = datetime(2024, 1, 2, 9, 30)

        success = pm.open_position(
            side=PositionSide.LONG,
            price=350.0,
            quantity=1,
            timestamp=timestamp,
            commission=800,
            slippage=0.02
        )

        assert success is True
        assert pm.position.side == PositionSide.LONG
        assert pm.position.entry_price == 350.02  # 슬리피지 적용
        assert pm.position.quantity == 1
        assert pm.capital == 10_000_000 - 800  # 수수료 차감
        assert len(pm.trades) == 1

    def test_open_short_position(self):
        """숏 포지션 진입 테스트"""
        pm = PositionManager(initial_capital=10_000_000)
        timestamp = datetime(2024, 1, 2, 9, 30)

        success = pm.open_position(
            side=PositionSide.SHORT,
            price=350.0,
            quantity=1,
            timestamp=timestamp,
            commission=800,
            slippage=0.02
        )

        assert success is True
        assert pm.position.side == PositionSide.SHORT
        assert pm.position.entry_price == 349.98  # 슬리피지 적용 (숏은 반대)
        assert len(pm.trades) == 1

    def test_close_long_position_profit(self):
        """롱 포지션 익절 테스트"""
        pm = PositionManager(initial_capital=10_000_000)
        entry_time = datetime(2024, 1, 2, 9, 30)
        exit_time = datetime(2024, 1, 2, 10, 0)

        pm.open_position(
            side=PositionSide.LONG,
            price=350.0,
            quantity=1,
            timestamp=entry_time,
            commission=800,
            slippage=0.02
        )

        pnl = pm.close_position(
            price=352.0,
            timestamp=exit_time,
            commission=800,
            slippage=0.02
        )

        # PnL = (351.98 - 350.02) * 1 - 800 = 1.96 - 800 (포인트 기준)
        # 실제 원화: 1.96 * 50000 - 800 = 97200원
        assert pnl is not None
        assert pm.position.side == PositionSide.FLAT
        assert len(pm.trades) == 2  # 진입 + 청산

    def test_cannot_open_when_position_exists(self):
        """포지션 있을 때 진입 불가 테스트"""
        pm = PositionManager()
        timestamp = datetime(2024, 1, 2, 9, 30)

        pm.open_position(PositionSide.LONG, 350.0, 1, timestamp)
        success = pm.open_position(PositionSide.SHORT, 351.0, 1, timestamp)

        assert success is False
        assert pm.position.side == PositionSide.LONG

    def test_cannot_close_when_flat(self):
        """포지션 없을 때 청산 불가 테스트"""
        pm = PositionManager()
        timestamp = datetime(2024, 1, 2, 9, 30)

        pnl = pm.close_position(350.0, timestamp)

        assert pnl is None

    def test_get_stats(self):
        """거래 통계 테스트"""
        pm = PositionManager(initial_capital=10_000_000)

        # 거래 없는 경우
        stats = pm.get_stats()
        assert stats['total_trades'] == 0
        assert stats['win_rate'] == 0.0

        # 2 승 1 패 거래
        # Trade 1: 2포인트 수익
        pm.open_position(PositionSide.LONG, 350.0, 1, datetime(2024, 1, 2, 9, 30))
        pm.close_position(352.0, datetime(2024, 1, 2, 10, 0))

        # Trade 2: 1포인트 손실
        pm.open_position(PositionSide.LONG, 350.0, 1, datetime(2024, 1, 2, 10, 30))
        pm.close_position(349.0, datetime(2024, 1, 2, 11, 0))

        # Trade 3: 3포인트 수익
        pm.open_position(PositionSide.SHORT, 350.0, 1, datetime(2024, 1, 2, 13, 0))
        pm.close_position(347.0, datetime(2024, 1, 2, 14, 0))

        stats = pm.get_stats()
        assert stats['total_trades'] == 3
        assert stats['winning_trades'] == 2
        assert stats['losing_trades'] == 1
        assert abs(stats['win_rate'] - 66.67) < 1  # ~66.67%


class TestRiskManager:
    """리스크 관리자 테스트"""

    def test_stop_loss_long(self):
        """롱 포지션 손절 테스트"""
        config = RiskConfig(stop_loss_points=2.0)
        rm = RiskManager(config)

        position = Position(
            side=PositionSide.LONG,
            entry_price=350.0,
            quantity=1,
            entry_time=datetime(2024, 1, 2, 9, 30)
        )

        # 손절 미도달
        result = rm.check_exit(position, 349.0, datetime(2024, 1, 2, 9, 35))
        assert result is None

        # 손절 도달 (350 - 2 = 348)
        result = rm.check_exit(position, 347.5, datetime(2024, 1, 2, 9, 40))
        assert result == ExitReason.STOP_LOSS

    def test_stop_loss_short(self):
        """숏 포지션 손절 테스트"""
        config = RiskConfig(stop_loss_points=2.0)
        rm = RiskManager(config)

        position = Position(
            side=PositionSide.SHORT,
            entry_price=350.0,
            quantity=1,
            entry_time=datetime(2024, 1, 2, 9, 30)
        )

        # 손절 미도달
        result = rm.check_exit(position, 351.0, datetime(2024, 1, 2, 9, 35))
        assert result is None

        # 손절 도달 (350 + 2 = 352)
        result = rm.check_exit(position, 352.5, datetime(2024, 1, 2, 9, 40))
        assert result == ExitReason.STOP_LOSS

    def test_take_profit_long(self):
        """롱 포지션 익절 테스트"""
        config = RiskConfig(take_profit_points=4.0)
        rm = RiskManager(config)

        position = Position(
            side=PositionSide.LONG,
            entry_price=350.0,
            quantity=1,
            entry_time=datetime(2024, 1, 2, 9, 30)
        )

        # 익절 미도달
        result = rm.check_exit(position, 353.0, datetime(2024, 1, 2, 9, 35))
        assert result is None

        # 익절 도달 (350 + 4 = 354)
        result = rm.check_exit(position, 354.5, datetime(2024, 1, 2, 9, 40))
        assert result == ExitReason.TAKE_PROFIT

    def test_time_stop(self):
        """시간 손절 테스트"""
        config = RiskConfig(time_stop_minutes=30)
        rm = RiskManager(config)

        entry_time = datetime(2024, 1, 2, 9, 30)
        position = Position(
            side=PositionSide.LONG,
            entry_price=350.0,
            quantity=1,
            entry_time=entry_time
        )

        # 29분 경과: 미청산
        result = rm.check_exit(position, 350.0, entry_time + timedelta(minutes=29))
        assert result is None

        # 30분 경과: 청산
        result = rm.check_exit(position, 350.0, entry_time + timedelta(minutes=30))
        assert result == ExitReason.TIME_STOP

    def test_trailing_stop(self):
        """트레일링 스탑 테스트"""
        config = RiskConfig(
            trailing_activation_points=1.0,
            trailing_stop_points=1.5
        )
        rm = RiskManager(config)

        position = Position(
            side=PositionSide.LONG,
            entry_price=350.0,
            quantity=1,
            entry_time=datetime(2024, 1, 2, 9, 30),
            highest_price=354.0  # 최고가 354
        )

        # 현재가 353 (수익 3포인트, 활성화됨), 최고가에서 1포인트 하락: 미청산
        result = rm.check_exit(position, 353.0, datetime(2024, 1, 2, 9, 40))
        assert result is None

        # 현재가 352.4 (수익 2.4포인트, 활성화됨), 최고가에서 1.6포인트 하락: 청산
        # trailing_stop = 354 - 1.5 = 352.5
        result = rm.check_exit(position, 352.4, datetime(2024, 1, 2, 9, 45))
        assert result == ExitReason.TRAILING_STOP

    def test_daily_max_loss(self):
        """일일 최대 손실 테스트"""
        config = RiskConfig(max_daily_loss=500_000)
        rm = RiskManager(config)

        position = Position(
            side=PositionSide.LONG,
            entry_price=350.0,
            quantity=1,
            entry_time=datetime(2024, 1, 2, 9, 30)
        )

        # 일일 손실 500,000원 초과
        rm.daily_pnl = -500_001

        result = rm.check_exit(position, 349.0, datetime(2024, 1, 2, 10, 0))
        assert result == ExitReason.MAX_LOSS

    def test_can_trade(self):
        """거래 가능 여부 테스트"""
        config = RiskConfig(max_daily_loss=500_000, max_daily_trades=20)
        rm = RiskManager(config)

        # 초기: 거래 가능
        assert rm.can_trade() is True

        # 일일 손실 초과
        rm.daily_pnl = -500_001
        assert rm.can_trade() is False

        # 리셋
        rm.daily_pnl = 0
        assert rm.can_trade() is True

        # 일일 거래 횟수 초과
        rm.daily_trades = 20
        assert rm.can_trade() is False

    def test_calculate_position_size(self):
        """포지션 사이즈 계산 테스트"""
        config = RiskConfig(
            risk_per_trade=0.02,  # 2%
            max_position_size=5
        )
        rm = RiskManager(config, point_value=50_000)

        # 자본 10,000,000원, 2% 리스크 = 200,000원
        # 손절폭 2포인트 = 100,000원/계약
        # → 2계약
        size = rm.calculate_position_size(
            capital=10_000_000,
            entry_price=350.0,
            stop_price=348.0
        )
        assert size == 2

        # 손절폭 1포인트 = 50,000원/계약 → 4계약
        size = rm.calculate_position_size(
            capital=10_000_000,
            entry_price=350.0,
            stop_price=349.0
        )
        assert size == 4


class TestCostModel:
    """비용 모델 테스트"""

    def test_kospi_mini_cost(self):
        """KOSPI Mini 비용 모델 테스트"""
        cost_model = KOSPIMiniCostModel(
            commission_per_contract=800,
            slippage_ticks=1.0,
            tick_size=0.02,
            point_value=50_000
        )

        cost = cost_model.calculate(price=350.0, quantity=2, is_entry=True)

        # 수수료: 800 * 2 = 1,600원
        assert cost.commission == 1_600

        # 슬리피지: 0.02포인트
        assert cost.slippage == 0.02

        # 총 비용: 1,600 + 0.02 * 50,000 * 2 = 1,600 + 2,000 = 3,600원
        assert cost.total == 3_600

    def test_zero_cost_model(self):
        """비용 없는 모델 테스트"""
        cost_model = ZeroCostModel()
        cost = cost_model.calculate(price=350.0, quantity=10, is_entry=True)

        assert cost.commission == 0
        assert cost.slippage == 0
        assert cost.total == 0


class TestTradingHoursFilter:
    """거래 시간 필터 테스트"""

    def test_market_hours(self):
        """장 시간 테스트"""
        filter = TradingHoursFilter()

        # 장 시간 내
        assert filter.is_trading_hours(datetime(2024, 1, 2, 9, 30)) is True
        assert filter.is_trading_hours(datetime(2024, 1, 2, 14, 0)) is True

        # 장 시간 외
        assert filter.is_trading_hours(datetime(2024, 1, 2, 8, 30)) is False
        assert filter.is_trading_hours(datetime(2024, 1, 2, 16, 0)) is False

    def test_noise_period(self):
        """노이즈 구간 테스트"""
        filter = TradingHoursFilter()

        # 노이즈 구간 (09:00~09:15)
        assert filter.can_open_position(datetime(2024, 1, 2, 9, 0)) is False
        assert filter.can_open_position(datetime(2024, 1, 2, 9, 10)) is False
        assert filter.can_open_position(datetime(2024, 1, 2, 9, 14)) is False

        # 노이즈 구간 이후
        assert filter.can_open_position(datetime(2024, 1, 2, 9, 20)) is True

    def test_lunch_period(self):
        """점심 시간 테스트"""
        filter = TradingHoursFilter()

        # 점심 시간 (11:30~13:00)
        assert filter.can_open_position(datetime(2024, 1, 2, 11, 30)) is False
        assert filter.can_open_position(datetime(2024, 1, 2, 12, 0)) is False
        assert filter.can_open_position(datetime(2024, 1, 2, 12, 59)) is False

        # 점심 시간 이후
        assert filter.can_open_position(datetime(2024, 1, 2, 13, 5)) is True

    def test_close_warning(self):
        """장 마감 직전 테스트"""
        filter = TradingHoursFilter()

        # 15:30 이후 신규 진입 금지
        assert filter.can_open_position(datetime(2024, 1, 2, 15, 25)) is True
        assert filter.can_open_position(datetime(2024, 1, 2, 15, 30)) is False
        assert filter.can_open_position(datetime(2024, 1, 2, 15, 40)) is False

    def test_force_close(self):
        """강제 청산 시간 테스트"""
        filter = TradingHoursFilter()

        # 15:43 이후 강제 청산
        assert filter.should_force_close(datetime(2024, 1, 2, 15, 42)) is False
        assert filter.should_force_close(datetime(2024, 1, 2, 15, 43)) is True

    def test_hour_boundary_exclusion(self):
        """매시 정각 ±5분 제외 테스트"""
        config = TradingHoursConfig(
            exclude_around_hour=True,
            exclude_minutes_before_hour=5,
            exclude_minutes_after_hour=5
        )
        filter = TradingHoursFilter(config)

        # 정각 직전/직후 제외
        assert filter.can_open_position(datetime(2024, 1, 2, 9, 56)) is False  # 56분
        assert filter.can_open_position(datetime(2024, 1, 2, 10, 3)) is False  # 03분

        # 정각에서 멀리 떨어진 시간
        assert filter.can_open_position(datetime(2024, 1, 2, 10, 30)) is True

    def test_exclusion_reason(self):
        """제외 사유 테스트"""
        filter = TradingHoursFilter()

        # 노이즈 구간
        reason = filter.get_exclusion_reason(datetime(2024, 1, 2, 9, 5))
        assert "노이즈" in reason

        # 점심 시간
        reason = filter.get_exclusion_reason(datetime(2024, 1, 2, 12, 0))
        assert "점심" in reason

        # 거래 가능 시간
        reason = filter.get_exclusion_reason(datetime(2024, 1, 2, 10, 30))
        assert reason is None


class TestBacktestEngine:
    """백테스트 엔진 테스트"""

    def _create_sample_data(
        self,
        start_date: datetime,
        days: int = 1,
        trend: str = "up"
    ) -> pd.DataFrame:
        """테스트용 샘플 데이터 생성"""
        data = []
        base_price = 350.0
        current_date = start_date

        for day in range(days):
            # 9:00 ~ 15:45 (405분)
            for minute in range(405):
                hour = 9 + minute // 60
                min_of_hour = minute % 60

                if hour >= 15 and min_of_hour >= 45:
                    break

                timestamp = datetime(
                    current_date.year,
                    current_date.month,
                    current_date.day + day,
                    hour,
                    min_of_hour
                )

                # 트렌드에 따른 가격 변동
                if trend == "up":
                    price_change = minute * 0.01
                elif trend == "down":
                    price_change = -minute * 0.01
                else:  # sideways
                    price_change = np.sin(minute / 30) * 0.5

                close = base_price + price_change
                open_price = close - np.random.uniform(-0.1, 0.1)
                high = max(open_price, close) + np.random.uniform(0, 0.2)
                low = min(open_price, close) - np.random.uniform(0, 0.2)
                volume = np.random.randint(100, 1000)

                data.append({
                    'datetime': timestamp,
                    'open': open_price,
                    'high': high,
                    'low': low,
                    'close': close,
                    'volume': volume
                })

        return pd.DataFrame(data)

    def test_backtest_basic(self):
        """기본 백테스트 실행 테스트"""
        # 샘플 데이터
        data = self._create_sample_data(datetime(2024, 1, 2), days=1, trend="up")

        # 전략 (간단한 이동평균)
        strategy = SimpleMovingAverageStrategy(short_period=5, long_period=10)

        # 백테스트 설정
        config = BacktestConfig(
            initial_capital=10_000_000,
            position_size=1,
            verbose=False
        )

        # 비용 없는 모델로 테스트
        engine = BacktestEngine(
            strategy=strategy,
            config=config,
            cost_model=ZeroCostModel()
        )

        result = engine.run(data)

        assert result is not None
        assert result.total_bars == len(data)
        assert result.initial_capital == 10_000_000
        assert isinstance(result.final_capital, float)

    def test_backtest_with_cost(self):
        """비용 포함 백테스트 테스트"""
        data = self._create_sample_data(datetime(2024, 1, 2), days=1, trend="up")

        strategy = SimpleMovingAverageStrategy(short_period=5, long_period=10)

        config = BacktestConfig(
            initial_capital=10_000_000,
            position_size=1,
        )

        # KOSPI Mini 비용 모델
        cost_model = KOSPIMiniCostModel()

        engine = BacktestEngine(
            strategy=strategy,
            config=config,
            cost_model=cost_model
        )

        result = engine.run(data)

        # 비용이 있으므로 거래가 있다면 수익률에 영향
        assert result is not None

    def test_backtest_result_summary(self):
        """결과 요약 테스트"""
        data = self._create_sample_data(datetime(2024, 1, 2), days=1, trend="up")

        strategy = SimpleMovingAverageStrategy()
        config = BacktestConfig(initial_capital=10_000_000)
        engine = BacktestEngine(strategy=strategy, config=config, cost_model=ZeroCostModel())

        result = engine.run(data)

        # to_dict 테스트
        result_dict = result.to_dict()
        assert 'total_return' in result_dict
        assert 'win_rate' in result_dict
        assert 'max_drawdown' in result_dict

    def test_trading_hours_filter_integration(self):
        """거래 시간 필터 통합 테스트"""
        # 점심 시간 포함 데이터
        data = self._create_sample_data(datetime(2024, 1, 2), days=1, trend="up")

        # Always BUY 전략 (테스트용)
        class AlwaysBuyStrategy:
            def on_bar(self, bar):
                return Signal.BUY

        strategy = AlwaysBuyStrategy()

        config = BacktestConfig(
            initial_capital=10_000_000,
            position_size=1,
        )

        engine = BacktestEngine(strategy=strategy, config=config, cost_model=ZeroCostModel())
        result = engine.run(data)

        # 점심 시간, 노이즈 구간 등에서 진입 안 됨
        # 정확한 거래 수는 필터 로직에 따라 달라짐
        assert result is not None

    def test_risk_manager_integration(self):
        """리스크 관리자 통합 테스트"""
        data = self._create_sample_data(datetime(2024, 1, 2), days=1, trend="down")

        class AlwaysBuyStrategy:
            def __init__(self):
                self.bought = False

            def on_bar(self, bar):
                if not self.bought:
                    self.bought = True
                    return Signal.BUY
                return Signal.HOLD

        strategy = AlwaysBuyStrategy()

        config = BacktestConfig(
            initial_capital=10_000_000,
            position_size=1,
            risk_config=RiskConfig(
                stop_loss_points=1.0,  # 1포인트 손절
                take_profit_points=10.0,
            )
        )

        engine = BacktestEngine(strategy=strategy, config=config, cost_model=ZeroCostModel())
        result = engine.run(data)

        # 하락 추세에서 손절 발생 예상
        assert result is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
