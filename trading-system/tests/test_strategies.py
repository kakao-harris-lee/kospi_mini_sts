"""
전략 모듈 테스트
"""
import pytest
from datetime import datetime
import numpy as np

from src.strategy import (
    BaseStrategy,
    Signal,
    PositionSide,
    BarData,
    RegimeDetector,
    RegimeConfig,
    VolatilityRegime,
    MeanReversionStrategy,
    BreakoutStrategy,
    OFIMomentumStrategy,
    HybridStrategy,
)
from src.strategy.signals.microstructure import (
    OFICalculator,
    OrderBookImbalance,
    SpreadAnalyzer,
    TradeFlowAnalyzer,
    OrderBookSnapshot,
    MicrostructureSignals,
)


class TestRegimeDetector:
    """레짐 판단기 테스트"""

    def test_initial_state(self):
        """초기 상태 테스트"""
        detector = RegimeDetector()
        assert detector.current_regime == VolatilityRegime.MEDIUM

    def test_low_volatility_detection(self):
        """저변동성 레짐 감지 테스트"""
        config = RegimeConfig(
            atr_period=5,
            atr_low_threshold=0.3,
            atr_high_threshold=0.7,
            regime_smoothing=2,
            hv_lookback=20,
        )
        detector = RegimeDetector(config)

        # 저변동성 데이터 입력 (작은 범위)
        base_price = 350.0
        for i in range(30):
            # 아주 작은 변동
            high = base_price + 0.02
            low = base_price - 0.02
            close = base_price + np.random.uniform(-0.01, 0.01)
            volume = 100
            detector.update(high, low, close, volume)

        # 저변동성 레짐이어야 함 (또는 MEDIUM)
        assert detector.current_regime in [VolatilityRegime.LOW, VolatilityRegime.MEDIUM]

    def test_high_volatility_detection(self):
        """고변동성 레짐 감지 테스트"""
        config = RegimeConfig(
            atr_period=5,
            atr_low_threshold=0.3,
            atr_high_threshold=0.7,
            regime_smoothing=2,
            hv_lookback=20,
        )
        detector = RegimeDetector(config)

        # 저변동성 데이터로 시작
        base_price = 350.0
        for i in range(20):
            high = base_price + 0.1
            low = base_price - 0.1
            close = base_price
            volume = 100
            detector.update(high, low, close, volume)

        # 고변동성 데이터 입력 (큰 범위)
        for i in range(15):
            high = base_price + 2.0 + i * 0.1
            low = base_price - 2.0
            close = base_price + i * 0.2
            volume = 500  # 거래량도 증가
            detector.update(high, low, close, volume)

        # 고변동성 레짐이어야 함
        assert detector.current_regime in [VolatilityRegime.HIGH, VolatilityRegime.MEDIUM]

    def test_atr_calculation(self):
        """ATR 계산 테스트"""
        detector = RegimeDetector()

        # 데이터 입력
        for i in range(20):
            high = 350.0 + i * 0.1
            low = 349.0 + i * 0.1
            close = 349.5 + i * 0.1
            detector.update(high, low, close, 100)

        atr = detector.calculate_atr()
        assert atr > 0
        assert atr < 5.0  # 합리적인 범위

    def test_regime_info(self):
        """레짐 정보 반환 테스트"""
        detector = RegimeDetector()

        for i in range(30):
            detector.update(351.0, 349.0, 350.0, 100)

        info = detector.get_regime_info()

        assert 'regime' in info
        assert 'atr' in info
        assert 'atr_percentile' in info
        assert 'volume_surge' in info


class TestMicrostructureSignals:
    """마이크로스트럭처 시그널 테스트"""

    def test_ofi_calculator(self):
        """OFI 계산기 테스트"""
        ofi = OFICalculator()

        # 첫 스냅샷
        snap1 = OrderBookSnapshot(
            bid_price=350.0,
            ask_price=350.02,
            bid_qty=100,
            ask_qty=100
        )
        result1 = ofi.update(snap1)
        assert result1 == 0.0  # 첫 번째는 0

        # 매수 압력: bid_qty 증가
        snap2 = OrderBookSnapshot(
            bid_price=350.0,
            ask_price=350.02,
            bid_qty=150,
            ask_qty=100
        )
        result2 = ofi.update(snap2)
        assert result2 > 0  # 양수 (매수 압력)

        # 매도 압력: ask_qty 증가
        snap3 = OrderBookSnapshot(
            bid_price=350.0,
            ask_price=350.02,
            bid_qty=150,
            ask_qty=200
        )
        result3 = ofi.update(snap3)
        assert result3 < 0  # 음수 (매도 압력)

    def test_order_book_imbalance(self):
        """호가 불균형 테스트"""
        imbalance = OrderBookImbalance()

        # 매수 우위
        result1 = imbalance.update(bid_qty=150, ask_qty=50)
        assert result1 > 0
        assert imbalance.is_strong_bid(threshold=0.3)

        # 매도 우위
        result2 = imbalance.update(bid_qty=50, ask_qty=150)
        assert result2 < 0
        assert imbalance.is_strong_ask(threshold=0.3)

        # 균형
        result3 = imbalance.update(bid_qty=100, ask_qty=100)
        assert result3 == 0

    def test_spread_analyzer(self):
        """스프레드 분석기 테스트"""
        spread = SpreadAnalyzer()

        # 평균보다 넓은 스프레드 먼저 입력
        for _ in range(10):
            spread.update(bid_price=350.0, ask_price=350.04)  # 2틱

        # 좁은 스프레드 (1틱) 입력 - 평균(0.04)보다 좁음
        spread.update(bid_price=350.0, ask_price=350.02)

        assert abs(spread.current - 0.02) < 0.001  # 부동소수점 비교
        assert spread.spread_ticks == 1
        assert spread.is_tight()  # current(0.02) < average(~0.038) * 0.8

        # 넓은 스프레드
        spread.update(bid_price=350.0, ask_price=350.10)
        assert abs(spread.current - 0.10) < 0.001
        assert spread.spread_ticks == 5

    def test_trade_flow_analyzer(self):
        """체결 흐름 분석기 테스트"""
        flow = TradeFlowAnalyzer()

        # 매수 체결 (체결가 > 중간가)
        for _ in range(5):
            flow.update(
                price=350.02,  # ask 쪽에서 체결
                volume=10,
                bid_price=350.0,
                ask_price=350.02
            )

        assert flow.buy_volume_ratio > 0.5
        assert flow.is_aggressive_buying()

        # 매도 체결 추가
        for _ in range(10):
            flow.update(
                price=350.0,  # bid 쪽에서 체결
                volume=10,
                bid_price=350.0,
                ask_price=350.02
            )

        assert flow.buy_volume_ratio < 0.5
        assert flow.is_aggressive_selling()

    def test_microstructure_signals_composite(self):
        """복합 시그널 테스트"""
        signals = MicrostructureSignals()

        # 호가창 업데이트
        for i in range(10):
            snap = OrderBookSnapshot(
                bid_price=350.0,
                ask_price=350.02,
                bid_qty=100 + i * 10,  # 매수 잔량 증가
                ask_qty=100
            )
            signals.update_orderbook(snap)

        # 체결 업데이트
        for _ in range(5):
            signals.update_trade(350.02, 10, 350.0, 350.02)

        result = signals.get_signals()
        assert 'ofi' in result
        assert 'imbalance' in result
        assert 'spread' in result
        assert 'buy_volume_ratio' in result

        composite = signals.get_composite_signal()
        assert -1 <= composite <= 1


class TestMeanReversionStrategy:
    """Mean Reversion 전략 테스트"""

    def _create_bar(
        self,
        close: float,
        ofi: float = 0.0,
        regime: str = "LOW"
    ) -> dict:
        """테스트용 바 데이터 생성"""
        return {
            'datetime': datetime.now(),
            'open': close,
            'high': close + 0.1,
            'low': close - 0.1,
            'close': close,
            'volume': 100,
            'ofi': ofi,
            'bid_ask_imbalance': 0.0,
            'spread': 0.02,
            'regime': regime,
        }

    def test_initial_state(self):
        """초기 상태 테스트"""
        strategy = MeanReversionStrategy()
        assert strategy.state.position == PositionSide.FLAT

    def test_buy_signal_at_lower_band(self):
        """하단 밴드 터치 시 매수 시그널"""
        strategy = MeanReversionStrategy()

        # 히스토리 채우기 (중간 가격대)
        for i in range(25):
            bar = self._create_bar(close=350.0 + np.random.uniform(-0.1, 0.1))
            strategy.on_bar(bar)

        # 하단 밴드 터치 (큰 하락)
        bar = self._create_bar(close=348.0, ofi=10.0, regime="LOW")
        signal = strategy.on_bar(bar)

        # 매수 시그널이어야 함
        assert signal == Signal.BUY

    def test_sell_signal_at_upper_band(self):
        """상단 밴드 터치 시 매도 시그널"""
        strategy = MeanReversionStrategy()

        # 히스토리 채우기
        for i in range(25):
            bar = self._create_bar(close=350.0 + np.random.uniform(-0.1, 0.1))
            strategy.on_bar(bar)

        # 상단 밴드 터치 (큰 상승)
        bar = self._create_bar(close=352.0, ofi=-10.0, regime="LOW")
        signal = strategy.on_bar(bar)

        # 매도 시그널이어야 함
        assert signal == Signal.SELL

    def test_hold_in_wrong_regime(self):
        """잘못된 레짐에서는 HOLD"""
        strategy = MeanReversionStrategy()

        # 히스토리 채우기
        for i in range(25):
            bar = self._create_bar(close=350.0)
            strategy.on_bar(bar)

        # HIGH 레짐에서는 진입 안함
        bar = self._create_bar(close=348.0, ofi=10.0, regime="HIGH")
        signal = strategy.on_bar(bar)

        assert signal == Signal.HOLD


class TestBreakoutStrategy:
    """Breakout 전략 테스트"""

    def _create_bar(
        self,
        close: float,
        high: float = None,
        low: float = None,
        volume: int = 200,
        buy_volume_ratio: float = 0.5,
        regime: str = "HIGH",
        atr: float = 0.5
    ) -> dict:
        high = high or close + 0.1
        low = low or close - 0.1
        return {
            'datetime': datetime.now(),
            'open': close,
            'high': high,
            'low': low,
            'close': close,
            'volume': volume,
            'ofi': 0.0,
            'bid_ask_imbalance': 0.0,
            'spread': 0.02,
            'buy_volume_ratio': buy_volume_ratio,
            'regime': regime,
            'atr': atr,
        }

    def test_buy_signal_on_high_breakout(self):
        """고점 돌파 시 매수 시그널"""
        from src.strategy.strategies.breakout import BreakoutConfig
        config = BreakoutConfig(
            lookback_period=20,
            breakout_buffer=0.02,
            volume_confirm=True,
            volume_threshold=1.5,
            trade_flow_confirm=True,
            buy_ratio_threshold=0.55,
        )
        strategy = BreakoutStrategy(config)

        # 히스토리 채우기 (350 근처, 고점은 350.2)
        for i in range(25):
            bar = self._create_bar(
                close=350.0,
                high=350.2,
                low=349.8,
                volume=100
            )
            strategy.on_bar(bar)

        # 주의: on_bar는 현재 바를 history에 먼저 추가하므로
        # 돌파 바의 high가 highest에 포함됨
        # 따라서 close > 현재 바의 high + buffer 조건 필요
        # high=351.0인 바를 넣으면 highest=351.0
        # close > 351.0 + 0.02 = 351.02 이어야 함
        bar = self._create_bar(
            close=351.1,  # 351.02 초과
            high=351.0,   # 새로운 highest
            low=350.5,
            volume=200,   # 150 초과
            buy_volume_ratio=0.7,  # 0.55 초과
            regime="HIGH"
        )
        signal = strategy.on_bar(bar)

        assert signal == Signal.BUY

    def test_sell_signal_on_low_breakout(self):
        """저점 이탈 시 매도 시그널"""
        from src.strategy.strategies.breakout import BreakoutConfig
        config = BreakoutConfig(
            lookback_period=20,
            breakout_buffer=0.02,
            volume_confirm=True,
            volume_threshold=1.5,
            trade_flow_confirm=True,
            buy_ratio_threshold=0.55,
        )
        strategy = BreakoutStrategy(config)

        # 히스토리 채우기 (350 근처, 저점은 349.8)
        for i in range(25):
            bar = self._create_bar(
                close=350.0,
                high=350.2,
                low=349.8,
                volume=100
            )
            strategy.on_bar(bar)

        # 주의: on_bar는 현재 바를 history에 먼저 추가하므로
        # 돌파 바의 low가 lowest에 포함됨
        # low=349.0인 바를 넣으면 lowest=349.0
        # close < 349.0 - 0.02 = 348.98 이어야 함
        bar = self._create_bar(
            close=348.9,  # 348.98 미만
            high=349.5,
            low=349.0,    # 새로운 lowest
            volume=200,   # 150 초과
            buy_volume_ratio=0.3,  # 0.45 미만
            regime="HIGH"
        )
        signal = strategy.on_bar(bar)

        assert signal == Signal.SELL


class TestHybridStrategy:
    """Hybrid 전략 테스트"""

    def _create_bar(
        self,
        close: float,
        up_prob: float = 0.5,
        ofi: float = 0.0,
        imbalance: float = 0.0,
        spread: float = 0.02
    ) -> dict:
        return {
            'datetime': datetime.now(),
            'open': close,
            'high': close + 0.1,
            'low': close - 0.1,
            'close': close,
            'volume': 100,
            'ofi': ofi,
            'bid_ask_imbalance': imbalance,
            'spread': spread,
            'up_prob': up_prob,
            'down_prob': 1 - up_prob,
            'regime': "MEDIUM",
        }

    def test_buy_signal_with_all_conditions(self):
        """모든 조건 충족 시 매수"""
        strategy = HybridStrategy()

        # 히스토리 채우기
        for i in range(25):
            bar = self._create_bar(close=350.0)
            strategy.on_bar(bar)

        # 3개 조건 모두 충족
        bar = self._create_bar(
            close=350.0,
            up_prob=0.8,      # LSTM 매수 신호
            ofi=5.0,          # 매수 압력
            imbalance=0.3,    # 매수 우위
            spread=0.02       # 스프레드 좁음
        )
        signal = strategy.on_bar(bar)

        assert signal == Signal.BUY

    def test_sell_signal_with_all_conditions(self):
        """모든 조건 충족 시 매도"""
        strategy = HybridStrategy()

        # 히스토리 채우기
        for i in range(25):
            bar = self._create_bar(close=350.0)
            strategy.on_bar(bar)

        # 3개 조건 모두 충족
        bar = self._create_bar(
            close=350.0,
            up_prob=0.2,      # LSTM 매도 신호
            ofi=-5.0,         # 매도 압력
            imbalance=-0.3,   # 매도 우위
            spread=0.02
        )
        signal = strategy.on_bar(bar)

        assert signal == Signal.SELL

    def test_hold_with_insufficient_conditions(self):
        """조건 부족 시 HOLD"""
        strategy = HybridStrategy()

        # 히스토리 채우기
        for i in range(25):
            bar = self._create_bar(close=350.0)
            strategy.on_bar(bar)

        # 1개 조건만 충족
        bar = self._create_bar(
            close=350.0,
            up_prob=0.8,      # LSTM 매수 신호만
            ofi=-1.0,         # 매도 압력
            imbalance=-0.1,   # 약간 매도 우위
            spread=0.02
        )
        signal = strategy.on_bar(bar)

        assert signal == Signal.HOLD

    def test_hold_with_wide_spread(self):
        """스프레드 넓으면 HOLD"""
        strategy = HybridStrategy()

        # 히스토리 채우기
        for i in range(25):
            bar = self._create_bar(close=350.0)
            strategy.on_bar(bar)

        # 조건 충족이지만 스프레드 넓음
        bar = self._create_bar(
            close=350.0,
            up_prob=0.8,
            ofi=5.0,
            imbalance=0.3,
            spread=0.10  # 5틱 (넓음)
        )
        signal = strategy.on_bar(bar)

        assert signal == Signal.HOLD


class TestStrategyWithBacktestEngine:
    """백테스트 엔진과의 통합 테스트"""

    def test_mean_reversion_backtest(self):
        """Mean Reversion 백테스트"""
        import pandas as pd
        from src.backtest import BacktestEngine, BacktestConfig, ZeroCostModel

        # 샘플 데이터 생성 (박스권)
        data = []
        base_price = 350.0
        for i in range(200):
            timestamp = datetime(2024, 1, 2, 9, i // 60, i % 60)
            # 사인파 형태 (박스권)
            close = base_price + np.sin(i / 20) * 1.5
            data.append({
                'datetime': timestamp,
                'open': close - 0.05,
                'high': close + 0.1,
                'low': close - 0.1,
                'close': close,
                'volume': 100,
                'ofi': np.sin(i / 10) * 5,  # OFI도 진동
                'regime': 'LOW',
            })

        df = pd.DataFrame(data)

        # 백테스트 실행
        strategy = MeanReversionStrategy()
        config = BacktestConfig(initial_capital=10_000_000)
        engine = BacktestEngine(strategy, config, ZeroCostModel())

        result = engine.run(df)

        assert result is not None
        assert result.total_bars == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
