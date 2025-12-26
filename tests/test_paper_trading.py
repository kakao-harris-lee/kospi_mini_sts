"""
Paper Trading 모듈 유닛 테스트

Coverage:
- src/paper_trading/virtual_broker.py
- src/paper_trading/paper_engine.py
"""
import pytest
import asyncio
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch, AsyncMock


# =============================================================================
# VirtualOrder Tests
# =============================================================================
class TestVirtualOrder:
    """VirtualOrder 데이터 클래스 테스트"""

    def test_order_creation(self):
        """주문 생성 테스트"""
        from src.paper_trading.virtual_broker import (
            VirtualOrder, OrderSide, OrderType, OrderStatus
        )

        order = VirtualOrder(
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=1
        )

        assert order.side == OrderSide.BUY
        assert order.order_type == OrderType.MARKET
        assert order.quantity == 1
        assert order.status == OrderStatus.PENDING
        assert len(order.id) == 8  # UUID 앞 8자

    def test_order_to_dict(self):
        """주문 딕셔너리 변환 테스트"""
        from src.paper_trading.virtual_broker import VirtualOrder, OrderSide

        order = VirtualOrder(side=OrderSide.BUY, quantity=2)
        d = order.to_dict()

        assert d['side'] == 'BUY'
        assert d['quantity'] == 2
        assert 'id' in d
        assert 'created_at' in d


# =============================================================================
# VirtualPosition Tests
# =============================================================================
class TestVirtualPosition:
    """VirtualPosition 데이터 클래스 테스트"""

    def test_flat_position(self):
        """평 포지션 테스트"""
        from src.paper_trading.virtual_broker import VirtualPosition, PositionSide

        pos = VirtualPosition()

        assert pos.side == PositionSide.FLAT
        assert pos.quantity == 0
        assert pos.is_open is False

    def test_long_position(self):
        """롱 포지션 테스트"""
        from src.paper_trading.virtual_broker import VirtualPosition, PositionSide

        pos = VirtualPosition(
            side=PositionSide.LONG,
            quantity=1,
            entry_price=350.0,
            entry_time=datetime.now()
        )

        assert pos.is_open is True
        assert pos.side == PositionSide.LONG

    def test_position_to_dict(self):
        """포지션 딕셔너리 변환 테스트"""
        from src.paper_trading.virtual_broker import VirtualPosition, PositionSide

        pos = VirtualPosition(
            side=PositionSide.LONG,
            quantity=1,
            entry_price=350.0
        )
        d = pos.to_dict()

        assert d['side'] == 'LONG'
        assert d['quantity'] == 1
        assert d['entry_price'] == 350.0


# =============================================================================
# VirtualBroker Tests
# =============================================================================
class TestVirtualBroker:
    """VirtualBroker 클래스 테스트"""

    @pytest.fixture
    def broker(self):
        """기본 브로커 인스턴스"""
        from src.paper_trading.virtual_broker import VirtualBroker

        return VirtualBroker(
            initial_capital=10_000_000,
            tick_value=250_000,
            tick_size=0.05,
            commission_rate=0.00015,
            slippage_ticks=1
        )

    def test_broker_initialization(self, broker):
        """브로커 초기화 테스트"""
        assert broker.initial_capital == 10_000_000
        assert broker.capital == 10_000_000
        assert broker.tick_value == 250_000
        assert broker.position.is_open is False

    def test_update_price(self, broker):
        """가격 업데이트 테스트"""
        broker.update_price(price=350.0, bid=349.95, ask=350.05)

        assert broker._current_price == 350.0
        assert broker._current_bid == 349.95
        assert broker._current_ask == 350.05

    def test_market_order_buy(self, broker):
        """시장가 매수 주문 테스트"""
        from src.paper_trading.virtual_broker import OrderSide, OrderType, OrderStatus

        broker.update_price(price=350.0, bid=349.95, ask=350.05)
        order = broker.submit_order(
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.MARKET
        )

        assert order.status == OrderStatus.FILLED
        assert order.filled_price > 350.0  # 슬리피지 적용
        assert broker.position.is_open is True

    def test_market_order_sell(self, broker):
        """시장가 매도 주문 테스트"""
        from src.paper_trading.virtual_broker import OrderSide, OrderType, PositionSide

        broker.update_price(price=350.0, bid=349.95, ask=350.05)
        order = broker.submit_order(
            side=OrderSide.SELL,
            quantity=1,
            order_type=OrderType.MARKET
        )

        assert broker.position.side == PositionSide.SHORT

    def test_limit_order_pending(self, broker):
        """지정가 주문 대기 테스트"""
        from src.paper_trading.virtual_broker import OrderSide, OrderType, OrderStatus

        broker.update_price(price=350.0)
        order = broker.submit_order(
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.LIMIT,
            price=349.0  # 현재가보다 낮은 가격
        )

        assert order.status == OrderStatus.PENDING
        assert len(broker.pending_orders) == 1

    def test_limit_order_fill_on_price_reach(self, broker):
        """지정가 주문 체결 테스트 (가격 도달)"""
        from src.paper_trading.virtual_broker import OrderSide, OrderType, OrderStatus

        broker.update_price(price=350.0)
        order = broker.submit_order(
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.LIMIT,
            price=349.0
        )

        # 가격이 지정가에 도달
        broker.update_price(price=349.0)

        assert order.status == OrderStatus.FILLED
        assert len(broker.pending_orders) == 0

    def test_cancel_order(self, broker):
        """주문 취소 테스트"""
        from src.paper_trading.virtual_broker import OrderSide, OrderType, OrderStatus

        broker.update_price(price=350.0)
        order = broker.submit_order(
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.LIMIT,
            price=340.0
        )

        result = broker.cancel_order(order.id)

        assert result is True
        assert order.status == OrderStatus.CANCELLED
        assert len(broker.pending_orders) == 0

    def test_close_position(self, broker):
        """포지션 청산 테스트"""
        from src.paper_trading.virtual_broker import OrderSide, OrderType

        broker.update_price(price=350.0, bid=349.95, ask=350.05)

        # 롱 포지션 진입
        broker.submit_order(OrderSide.BUY, quantity=1, order_type=OrderType.MARKET)
        assert broker.position.is_open is True

        # 가격 상승
        broker.update_price(price=352.0, bid=351.95, ask=352.05)

        # 청산
        trade = broker.close_position(reason="TAKE_PROFIT")

        assert broker.position.is_open is False
        assert trade is not None
        assert trade.pnl > 0  # 이익

    def test_position_pnl_calculation(self, broker):
        """포지션 평가손익 계산 테스트"""
        from src.paper_trading.virtual_broker import OrderSide, OrderType

        broker.update_price(price=350.0, bid=349.95, ask=350.05)
        broker.submit_order(OrderSide.BUY, quantity=1, order_type=OrderType.MARKET)

        # 가격 상승 시 미실현 이익
        broker.update_price(price=352.0)
        assert broker.position.unrealized_pnl > 0

        # 가격 하락 시 미실현 손실
        broker.update_price(price=348.0)
        assert broker.position.unrealized_pnl < 0

    def test_commission_calculation(self, broker):
        """수수료 계산 테스트"""
        from src.paper_trading.virtual_broker import OrderSide, OrderType

        initial_capital = broker.capital
        broker.update_price(price=350.0, bid=349.95, ask=350.05)

        order = broker.submit_order(OrderSide.BUY, quantity=1, order_type=OrderType.MARKET)

        assert order.commission > 0
        assert broker.capital < initial_capital  # 수수료 차감

    def test_equity_property(self, broker):
        """자본 + 평가손익 계산 테스트"""
        from src.paper_trading.virtual_broker import OrderSide, OrderType

        initial_equity = broker.equity
        assert initial_equity == broker.initial_capital

        broker.update_price(price=350.0, bid=349.95, ask=350.05)
        broker.submit_order(OrderSide.BUY, quantity=1, order_type=OrderType.MARKET)

        broker.update_price(price=352.0)

        # equity = capital + unrealized_pnl
        assert broker.equity == broker.capital + broker.position.unrealized_pnl

    def test_get_summary(self, broker):
        """거래 요약 테스트"""
        from src.paper_trading.virtual_broker import OrderSide, OrderType

        broker.update_price(price=350.0, bid=349.95, ask=350.05)
        broker.submit_order(OrderSide.BUY, quantity=1, order_type=OrderType.MARKET)

        broker.update_price(price=352.0, bid=351.95, ask=352.05)
        broker.close_position("PROFIT")

        summary = broker.get_summary()

        assert 'initial_capital' in summary
        assert 'total_trades' in summary
        assert 'win_rate' in summary
        assert summary['total_trades'] == 1

    def test_total_return(self, broker):
        """총 수익률 계산 테스트"""
        from src.paper_trading.virtual_broker import OrderSide, OrderType

        broker.update_price(price=350.0, bid=349.95, ask=350.05)
        broker.submit_order(OrderSide.BUY, quantity=1, order_type=OrderType.MARKET)

        broker.update_price(price=360.0, bid=359.95, ask=360.05)
        broker.close_position("PROFIT")

        assert broker.total_return != 0

    def test_multiple_trades(self, broker):
        """다수 거래 테스트"""
        from src.paper_trading.virtual_broker import OrderSide, OrderType

        # Trade 1: Long -> Close
        broker.update_price(price=350.0, bid=349.95, ask=350.05)
        broker.submit_order(OrderSide.BUY, quantity=1, order_type=OrderType.MARKET)
        broker.update_price(price=352.0, bid=351.95, ask=352.05)
        broker.close_position("PROFIT")

        # Trade 2: Short -> Close
        broker.update_price(price=352.0, bid=351.95, ask=352.05)
        broker.submit_order(OrderSide.SELL, quantity=1, order_type=OrderType.MARKET)
        broker.update_price(price=350.0, bid=349.95, ask=350.05)
        broker.close_position("PROFIT")

        summary = broker.get_summary()
        assert summary['total_trades'] == 2


# =============================================================================
# TradeRecord Tests
# =============================================================================
class TestTradeRecord:
    """TradeRecord 데이터 클래스 테스트"""

    def test_trade_record_creation(self):
        """거래 기록 생성 테스트"""
        from src.paper_trading.virtual_broker import TradeRecord, PositionSide

        trade = TradeRecord(
            side=PositionSide.LONG,
            entry_price=350.0,
            quantity=1
        )

        assert trade.side == PositionSide.LONG
        assert trade.entry_price == 350.0
        assert trade.exit_time is None
        assert len(trade.id) == 8


# =============================================================================
# KISOrderBridge Tests
# =============================================================================
class TestKISOrderBridge:
    """KISOrderBridge 클래스 테스트"""

    def test_bridge_without_executor(self):
        """Executor 없이 브릿지 테스트"""
        from src.paper_trading.paper_engine import KISOrderBridge

        bridge = KISOrderBridge(kis_executor=None)
        result = bridge.place_order("BUY", 1, 350.0)

        assert result is None

    def test_bridge_with_mock_executor(self):
        """Mock Executor로 브릿지 테스트"""
        from src.paper_trading.paper_engine import KISOrderBridge

        mock_executor = MagicMock()
        mock_executor.execute.return_value = "ORDER12345"

        bridge = KISOrderBridge(kis_executor=mock_executor, symbol="101F26")
        result = bridge.place_order("BUY", 1, 350.0)

        assert result == "ORDER12345"
        mock_executor.execute.assert_called_once()
        assert len(bridge.orders) == 1

    def test_bridge_order_failure(self):
        """주문 실패 시 처리 테스트"""
        from src.paper_trading.paper_engine import KISOrderBridge

        mock_executor = MagicMock()
        mock_executor.execute.side_effect = Exception("API Error")

        bridge = KISOrderBridge(kis_executor=mock_executor)
        result = bridge.place_order("BUY", 1, 350.0)

        assert result is None


# =============================================================================
# PaperTradingEngine Tests
# =============================================================================
class TestPaperTradingEngine:
    """PaperTradingEngine 클래스 테스트"""

    @pytest.fixture
    def mock_strategy(self):
        """Mock 전략"""
        strategy = MagicMock()
        strategy.__class__.__name__ = "MockStrategy"
        strategy.on_bar.return_value = None
        return strategy

    @pytest.fixture
    def engine(self, mock_strategy):
        """Paper Trading 엔진"""
        from src.paper_trading.paper_engine import PaperTradingEngine
        from src.paper_trading.virtual_broker import VirtualBroker

        broker = VirtualBroker(initial_capital=10_000_000)

        return PaperTradingEngine(
            strategy=mock_strategy,
            broker=broker,
            redis_client=None,  # 시뮬레이션 모드
            run_id="test_run_001"
        )

    def test_engine_initialization(self, engine, mock_strategy):
        """엔진 초기화 테스트"""
        assert engine.strategy == mock_strategy
        assert engine.run_id == "test_run_001"
        assert engine.is_running is False
        assert engine.bars_processed == 0

    def test_parse_bar_data(self, engine):
        """바 데이터 파싱 테스트"""
        data = {
            "timestamp": "2025-01-15T10:00:00",
            "code": "101M25",
            "close": "350.0",
            "volume": "1000"
        }

        bar = engine._parse_bar_data(data)

        assert bar['close'] == 350.0
        assert bar['volume'] == 1000
        assert bar['code'] == "101M25"

    def test_parse_bar_data_bytes(self, engine):
        """bytes 타입 데이터 파싱 테스트"""
        data = {
            "timestamp": "2025-01-15T10:00:00",
            "code": b"101M25",
            "close": b"350.0",
            "volume": b"1000"
        }

        bar = engine._parse_bar_data(data)

        assert bar['close'] == 350.0
        assert bar['code'] == "101M25"

    def test_get_result(self, engine):
        """결과 조회 테스트"""
        engine.start_time = datetime.now()
        engine.end_time = datetime.now()
        engine.bars_processed = 100
        engine.signals_generated = 5

        result = engine.get_result()

        assert result['run_id'] == "test_run_001"
        assert result['strategy'] == "MockStrategy"
        assert result['bars_processed'] == 100
        assert result['signals_generated'] == 5

    @pytest.mark.asyncio
    async def test_stop_engine(self, engine):
        """엔진 중지 테스트"""
        engine.is_running = True
        await engine.stop()

        assert engine.is_running is False


# =============================================================================
# Signal Handling Tests
# =============================================================================
class TestSignalHandling:
    """시그널 처리 테스트"""

    @pytest.fixture
    def engine_with_strategy(self):
        """전략이 있는 엔진"""
        from src.paper_trading.paper_engine import PaperTradingEngine
        from src.paper_trading.virtual_broker import VirtualBroker

        mock_strategy = MagicMock()
        mock_strategy.__class__.__name__ = "TestStrategy"

        broker = VirtualBroker(initial_capital=10_000_000)
        broker.update_price(350.0, 349.95, 350.05)

        return PaperTradingEngine(
            strategy=mock_strategy,
            broker=broker,
            redis_client=None
        )

    @pytest.mark.asyncio
    async def test_buy_signal_opens_long(self, engine_with_strategy):
        """BUY 시그널 롱 포지션 진입 테스트"""
        from src.strategy import Signal
        from src.paper_trading.virtual_broker import OrderSide

        engine = engine_with_strategy
        bar_data = {'datetime': datetime.now().isoformat(), 'close': 350.0}

        # Directly test broker order submission
        engine.broker.submit_order(OrderSide.BUY, quantity=1)

        assert engine.broker.position.is_open is True

    @pytest.mark.asyncio
    async def test_sell_signal_opens_short(self, engine_with_strategy):
        """SELL 시그널 숏 포지션 진입 테스트"""
        from src.paper_trading.virtual_broker import OrderSide, PositionSide

        engine = engine_with_strategy

        # Directly test broker order submission
        engine.broker.submit_order(OrderSide.SELL, quantity=1)

        assert engine.broker.position.side == PositionSide.SHORT


# =============================================================================
# Order Enums Tests
# =============================================================================
class TestOrderEnums:
    """주문 관련 Enum 테스트"""

    def test_order_side_values(self):
        """OrderSide 값 테스트"""
        from src.paper_trading.virtual_broker import OrderSide

        assert OrderSide.BUY.value == "BUY"
        assert OrderSide.SELL.value == "SELL"

    def test_order_type_values(self):
        """OrderType 값 테스트"""
        from src.paper_trading.virtual_broker import OrderType

        assert OrderType.MARKET.value == "MARKET"
        assert OrderType.LIMIT.value == "LIMIT"

    def test_order_status_values(self):
        """OrderStatus 값 테스트"""
        from src.paper_trading.virtual_broker import OrderStatus

        assert OrderStatus.PENDING.value == "PENDING"
        assert OrderStatus.FILLED.value == "FILLED"
        assert OrderStatus.CANCELLED.value == "CANCELLED"
        assert OrderStatus.REJECTED.value == "REJECTED"

    def test_position_side_values(self):
        """PositionSide 값 테스트"""
        from src.paper_trading.virtual_broker import PositionSide

        assert PositionSide.LONG.value == "LONG"
        assert PositionSide.SHORT.value == "SHORT"
        assert PositionSide.FLAT.value == "FLAT"
