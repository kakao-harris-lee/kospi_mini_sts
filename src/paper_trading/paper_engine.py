"""
Paper Trading Engine

실시간 데이터로 전략을 실행하고 가상 주문을 처리하는 모의투자 엔진
KIS 모의투자 API 연동 지원
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Callable

from config.settings import settings
from src.common.metrics import get_metrics
from src.common.decision_logger import DecisionLogger, DecisionLog

from .virtual_broker import (
    VirtualBroker,
    VirtualOrder,
    TradeRecord,
    OrderSide,
    OrderType,
    PositionSide,
)

logger = logging.getLogger(__name__)


class KISOrderBridge:
    """
    KIS 주문 API 브릿지

    VirtualBroker와 KISOrderExecutor를 연결하여
    가상 체결과 실제 주문을 동시에 처리
    """

    def __init__(self, kis_executor=None, symbol: str = "101F26"):
        """
        :param kis_executor: KISOrderExecutor 인스턴스
        :param symbol: 거래 종목코드 (KRX 형식)
        """
        self.executor = kis_executor
        self.symbol = symbol
        self.orders: List[Dict] = []  # 주문 이력

    def place_order(self, side: str, quantity: int, price: float) -> Optional[str]:
        """
        KIS API로 주문 전송

        :param side: "BUY" 또는 "SELL"
        :param quantity: 수량
        :param price: 가격
        :return: 주문번호 (실패 시 None)
        """
        if not self.executor:
            return None

        order_side = "buy" if side == "BUY" else "sell"

        try:
            order_no = self.executor.execute(
                symbol=self.symbol,
                side=order_side,
                size=quantity,
                price=price,
                order_type="market"  # 시장가 주문
            )

            self.orders.append({
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'side': side,
                'quantity': quantity,
                'price': price,
                'order_no': order_no,
            })

            logger.info(f"KIS Order sent: {side} {quantity} @ {price} -> {order_no}")
            return order_no

        except Exception as e:
            logger.error(f"KIS Order failed: {e}")
            return None


class PaperTradingEngine:
    """
    Paper Trading Engine

    실시간 Redis Streams 데이터를 사용하여 전략을 실행합니다.
    KIS 모의투자 API 연동 지원.
    """

    def __init__(
        self,
        strategy,
        broker: VirtualBroker = None,
        redis_client=None,
        feature_stream: str = "FEATURE_STREAM",
        prediction_stream: str = "PREDICTION_STREAM",
        run_id: str = None,
        kis_executor=None,
        symbol: str = "101F26",
    ):
        """
        :param strategy: BaseStrategy 인스턴스
        :param broker: VirtualBroker 인스턴스 (없으면 기본값으로 생성)
        :param redis_client: Redis 클라이언트
        :param feature_stream: 가격 업데이트용 스트림 이름
        :param prediction_stream: DL 예측 결과 스트림 이름
        :param run_id: 실행 ID (DB 저장용)
        :param kis_executor: KISOrderExecutor 인스턴스 (실제 주문 시)
        :param symbol: 거래 종목코드
        """
        self.strategy = strategy
        strategy_name = strategy.__class__.__name__
        self.broker = broker or VirtualBroker(
            strategy_name=strategy_name,
            symbol=symbol,
        )
        # Update broker with strategy info if broker was passed in
        if broker:
            broker.strategy_name = strategy_name
            broker.symbol = symbol
        self.redis = redis_client
        self.feature_stream = feature_stream
        self.prediction_stream = prediction_stream
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.symbol = symbol

        # KIS 주문 브릿지
        self.kis_bridge = KISOrderBridge(kis_executor, symbol) if kis_executor else None

        # 상태
        self.is_running = False
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self.bars_processed = 0
        self.signals_generated = 0

        # 콜백 설정
        self.broker.on_fill = self._on_order_fill
        self.broker.on_trade_close = self._on_trade_close

        # 히스토리
        self.equity_history: List[Dict[str, Any]] = []
        self.signal_history: List[Dict[str, Any]] = []
        self.kis_orders: List[Dict] = []  # KIS 주문 이력

        # Decision logger for trade close logging
        self._decision_logger = DecisionLogger(
            session_id=f"paper_{self.run_id}",
            enable_telegram=False,  # Paper engine handles its own notifications
        )

        # Heartbeat for health monitoring
        self._heartbeat_interval = 30.0  # seconds
        self._last_heartbeat_time = 0.0

    async def _publish_heartbeats(self):
        """Publish heartbeat for strategy_manager (paper engine acts as strategy manager)."""
        if not self.redis:
            return

        now = time.time()
        if now - self._last_heartbeat_time < self._heartbeat_interval:
            return

        try:
            # Paper engine acts as strategy_manager only;
            # prediction_engine subprocess publishes its own heartbeat
            heartbeat_key = "heartbeat:strategy_manager"
            await self.redis.set(heartbeat_key, str(now), ex=120)
            self._last_heartbeat_time = now
            logger.debug("Heartbeat published: strategy_manager")
        except Exception as e:
            logger.warning(f"Failed to publish heartbeat: {e}")

    async def start(self, duration_seconds: int = None):
        """
        Paper Trading 시작

        :param duration_seconds: 실행 시간 (초). None이면 무한 실행
        """
        if self.is_running:
            logger.warning("Engine already running")
            return

        self.is_running = True
        self.start_time = datetime.now(timezone.utc)
        logger.info(f"Paper Trading started at {self.start_time}")

        try:
            if duration_seconds:
                # 시간 제한 실행
                await asyncio.wait_for(
                    self._run_loop(),
                    timeout=duration_seconds,
                )
            else:
                # 무한 실행
                await self._run_loop()
        except asyncio.TimeoutError:
            logger.info(f"Paper Trading completed (duration: {duration_seconds}s)")
        except asyncio.CancelledError:
            logger.info("Paper Trading cancelled")
        finally:
            self.is_running = False
            self.end_time = datetime.now(timezone.utc)
            await self._cleanup()

    async def stop(self):
        """Paper Trading 중지"""
        self.is_running = False
        logger.info("Stop signal received")

    async def _run_loop(self):
        """메인 실행 루프 (Feature + Prediction 듀얼 스트림)

        FEATURE_STREAM: 가격 업데이트 (브로커 PnL 추적용)
        PREDICTION_STREAM: DL 예측값 + 피처 → 전략 실행 → 주문
        """
        if not self.redis:
            logger.warning("No Redis client - running in simulation mode")
            await self._simulation_loop()
            return

        group_name = f"paper_{self.run_id}"
        consumer_name = f"consumer_{self.run_id}"

        # 두 스트림 모두에 Consumer Group 생성
        import redis.exceptions
        for stream in [self.feature_stream, self.prediction_stream]:
            try:
                await self.redis.xgroup_create(
                    stream, group_name, mkstream=True, id="$"
                )
            except redis.exceptions.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise
            except Exception:
                raise

        logger.info(
            f"Listening to {self.feature_stream} + {self.prediction_stream}..."
        )

        await self._publish_heartbeats()

        while self.is_running:
            try:
                await self._publish_heartbeats()

                messages = await self.redis.xreadgroup(
                    groupname=group_name,
                    consumername=consumer_name,
                    streams={
                        self.feature_stream: ">",
                        self.prediction_stream: ">",
                    },
                    count=10,
                    block=1000,
                )

                if not messages:
                    continue

                for stream_name, stream_messages in messages:
                    for msg_id, data in stream_messages:
                        if stream_name == self.prediction_stream:
                            await self._process_prediction(data)
                        else:
                            await self._process_feature_update(data)
                        await self.redis.xack(
                            stream_name, group_name, msg_id
                        )

            except Exception as e:
                logger.error(f"Error in run loop: {e}")
                await asyncio.sleep(1)

    async def _simulation_loop(self):
        """시뮬레이션 모드 (Redis 없이)"""
        import random

        base_price = 350.0
        while self.is_running:
            # 가상 데이터 생성
            price = base_price + random.uniform(-2, 2)
            data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "code": "101M25",
                "close": str(price),
                "bid": str(price - 0.05),
                "ask": str(price + 0.05),
                "volume": str(random.randint(100, 1000)),
            }

            await self._process_message(data)
            await asyncio.sleep(1)

    async def _process_message(self, data: Dict[str, Any]):
        """시뮬레이션 모드 전용 메시지 처리 (가격 업데이트 + 시그널 생성 통합).
        Redis 연결 없을 때 _simulation_loop()에서만 호출."""
        try:
            # 데이터 파싱
            bar_data = self._parse_bar_data(data)
            if not bar_data:
                return

            # 가격 업데이트
            self.broker.update_price(
                price=bar_data['close'],
                bid=bar_data.get('bid'),
                ask=bar_data.get('ask'),
            )

            # 전략 시그널 생성
            signal = self.strategy.on_bar(bar_data)
            self.bars_processed += 1

            if signal:
                self.signals_generated += 1
                await self._handle_signal(signal, bar_data)

            # 히스토리 기록 (1분마다)
            if self.bars_processed % 60 == 0:
                self._record_equity()

        except Exception as e:
            logger.error(f"Error processing message: {e}")

    async def _process_feature_update(self, data: Dict[str, Any]):
        """Feature 메시지 처리: 가격 업데이트만 (시그널 생성 없음)."""
        try:
            bar_data = self._parse_bar_data(data)
            if not bar_data:
                return

            self.broker.update_price(
                price=bar_data['close'],
                bid=bar_data.get('bid'),
                ask=bar_data.get('ask'),
            )
            self.bars_processed += 1

            if self.bars_processed % 60 == 0:
                self._record_equity()

        except Exception as e:
            logger.error(f"Error processing feature update: {e}")

    async def _process_prediction(self, data: Dict[str, Any]):
        """Prediction 메시지 처리: 피처 조회 + 예측값 주입 + 전략 실행."""
        try:
            symbol = data.get('symbol')
            if not symbol:
                logger.warning(f"Prediction message missing 'symbol' field. Keys: {list(data.keys())}")
                return

            raw_up = data.get('up_prob')
            raw_down = data.get('down_prob')
            if raw_up is None or raw_down is None:
                logger.warning(
                    f"Prediction missing probabilities: up_prob={raw_up}, down_prob={raw_down}. "
                    f"Skipping for symbol={symbol}"
                )
                return

            up_prob = float(raw_up)
            down_prob = float(raw_down)

            # Redis에서 최신 피처 조회
            feature_data = await self._get_latest_feature(symbol)
            if not feature_data:
                logger.debug(f"No feature data for {symbol}, skipping prediction")
                return

            bar_data = self._parse_bar_data(feature_data)
            if not bar_data:
                return

            # 예측 확률 주입
            bar_data['up_prob'] = up_prob
            bar_data['down_prob'] = down_prob

            # Multi-horizon 확률 주입 (strategy_manager.py 패턴과 동일)
            if 'up_prob_h10' in data:
                bar_data['up_prob_h1'] = float(data.get('up_prob_h1', 0.5))
                bar_data['up_prob_h3'] = float(data.get('up_prob_h3', 0.5))
                bar_data['up_prob_h5'] = float(data.get('up_prob_h5', 0.5))
                bar_data['up_prob_h10'] = float(data.get('up_prob_h10', 0.5))
            else:
                # Single model: h10을 메인 확률로 매핑
                bar_data['up_prob_h10'] = up_prob

            # 가격 업데이트
            self.broker.update_price(
                price=bar_data['close'],
                bid=bar_data.get('bid'),
                ask=bar_data.get('ask'),
            )

            # 전략 실행
            signal = self.strategy.on_bar(bar_data)

            if signal:
                self.signals_generated += 1
                await self._handle_signal(signal, bar_data)

        except Exception as e:
            logger.error(f"Error processing prediction: {e}")

    async def _get_latest_feature(self, symbol: str) -> Optional[Dict]:
        """Redis feature_window에서 최신 피처 조회."""
        key = f"feature_window:{symbol}"
        try:
            data = await self.redis.get(key)
            if data:
                features = json.loads(data)
                if features:
                    return features[-1]
        except Exception as e:
            logger.error(f"Failed to get feature window: {e}")
        return None

    def _parse_bar_data(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """스트림 데이터를 BarData 형식으로 파싱"""
        try:
            # Helper to get value with both string and bytes keys
            def get(key: str, default=None):
                return data.get(key) or data.get(key.encode(), default)

            # 문자열 -> 숫자 변환
            def to_float(v):
                if isinstance(v, bytes):
                    v = v.decode()
                return float(v) if v else 0.0

            def to_int(v):
                if isinstance(v, bytes):
                    v = v.decode()
                return int(float(v)) if v else 0

            def to_datetime(v):
                if v is None:
                    return datetime.now(timezone.utc)
                if isinstance(v, datetime):
                    if v.tzinfo is None:
                        return v.replace(tzinfo=timezone.utc)
                    return v
                if isinstance(v, bytes):
                    v = v.decode()
                if isinstance(v, str):
                    # Handle ISO format with or without timezone
                    try:
                        parsed = datetime.fromisoformat(v.replace('Z', '+00:00'))
                        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                    except ValueError:
                        # Try parsing as Unix timestamp
                        try:
                            return datetime.fromtimestamp(float(v), tz=timezone.utc)
                        except ValueError:
                            return datetime.now(timezone.utc)
                if isinstance(v, (int, float)):
                    return datetime.fromtimestamp(v, tz=timezone.utc)
                return datetime.now(timezone.utc)

            # Price fallback chain: close → current_price → mid_price
            mid_price = to_float(get('mid_price', 0))
            current_price = to_float(get('current_price', 0))
            close = to_float(get('close')) or current_price or mid_price
            spread = to_float(get('spread', 0))
            half_spread = spread / 2

            # OHLC from 체결 data (day's OHLC) or fallback to close
            open_price = to_float(get('open')) or to_float(get('open_price')) or close
            high_price = to_float(get('high')) or to_float(get('high_price')) or close
            low_price = to_float(get('low')) or to_float(get('low_price')) or close

            # Volume: prefer tick volume, fallback to cumulative
            volume = to_int(get('volume')) or to_int(get('cumulative_volume', 0))

            code = get('code', b'')
            if isinstance(code, bytes):
                code = code.decode()

            return {
                'datetime': to_datetime(get('timestamp')),
                'code': code or '',
                'open': open_price,
                'high': high_price,
                'low': low_price,
                'close': close,
                'volume': volume,
                'bid': to_float(get('bid')) or (mid_price - half_spread if mid_price else close),
                'ask': to_float(get('ask')) or (mid_price + half_spread if mid_price else close),
                # Feature 데이터 (있는 경우)
                'ofi': to_float(get('ofi') or get('ofi_cumulative', 0)),
                'ofi_zscore': to_float(get('ofi_zscore') or get('ofi_z_score', 0)),
                'imbalance': to_float(get('imbalance', 0)),
                'spread': spread,
            }
        except Exception as e:
            logger.error(f"Error parsing bar data: {e}")
            return None

    async def _handle_signal(self, signal, bar_data: Dict[str, Any]):
        """시그널 처리 (VirtualBroker + KIS 실제 주문)"""
        from src.strategy import Signal, PositionSide as StrategyPositionSide

        signal_info = {
            'timestamp': bar_data['datetime'],
            'signal': signal.name if hasattr(signal, 'name') else str(signal),
            'price': bar_data['close'],
            'position': self.broker.position.side.value,
        }
        self.signal_history.append(signal_info)

        # Record signal to Prometheus metrics
        metrics = get_metrics()
        strategy_name = self.strategy.__class__.__name__
        signal_type = "ENTRY" if not self.broker.position.is_open else "EXIT"
        direction = signal.name if hasattr(signal, 'name') else str(signal)
        metrics.record_signal(strategy_name, signal_type, direction)

        logger.debug(f"Signal: {signal} at price {bar_data['close']:.2f}")

        price = bar_data['close']

        # 포지션이 없을 때
        if not self.broker.position.is_open:
            if signal == Signal.BUY:
                self.broker.submit_order(OrderSide.BUY, quantity=1)
                # KIS 실제 주문
                if self.kis_bridge:
                    self.kis_bridge.place_order("BUY", 1, price)
                logger.info(f"OPEN LONG at {price:.2f}")
            elif signal == Signal.SELL:
                self.broker.submit_order(OrderSide.SELL, quantity=1)
                # KIS 실제 주문
                if self.kis_bridge:
                    self.kis_bridge.place_order("SELL", 1, price)
                logger.info(f"OPEN SHORT at {price:.2f}")

        # 롱 포지션일 때
        elif self.broker.position.side == PositionSide.LONG:
            if signal == Signal.SELL:
                self.broker.close_position(reason="SIGNAL")
                # KIS 실제 주문 (롱 청산 = 매도)
                if self.kis_bridge:
                    self.kis_bridge.place_order("SELL", 1, price)
                logger.info(f"CLOSE LONG at {price:.2f}")

        # 숏 포지션일 때
        elif self.broker.position.side == PositionSide.SHORT:
            if signal == Signal.BUY:
                self.broker.close_position(reason="SIGNAL")
                # KIS 실제 주문 (숏 청산 = 매수)
                if self.kis_bridge:
                    self.kis_bridge.place_order("BUY", 1, price)
                logger.info(f"CLOSE SHORT at {price:.2f}")

    def _on_order_fill(self, order: VirtualOrder):
        """주문 체결 콜백"""
        logger.info(
            f"Order filled: {order.side.value} @ {order.filled_price:.2f} "
            f"(commission: {order.commission:.0f})"
        )

    def _on_trade_close(self, trade: TradeRecord):
        """거래 청산 콜백 - logs to console AND decision_logger"""
        pnl_color = "+" if trade.pnl_amount >= 0 else ""
        logger.info(
            f"Trade closed: {trade.side.value} "
            f"{trade.entry_price:.2f} -> {trade.exit_price:.2f} "
            f"PnL: {pnl_color}{trade.pnl_amount:,.0f} ({trade.exit_reason})"
        )

        # Log to decision_logger for analysis
        self._log_trade_close(trade)

    def _log_trade_close(self, trade: TradeRecord):
        """Log trade close to decision_logger for analysis."""
        try:
            # Calculate hold time in minutes
            hold_time_minutes = None
            if trade.entry_time and trade.exit_time:
                hold_delta = trade.exit_time - trade.entry_time
                hold_time_minutes = int(hold_delta.total_seconds() / 60)

            # Determine signal direction (entry was opposite of close)
            entry_signal = "SELL" if trade.side == PositionSide.LONG else "BUY"

            # Create decision log for close
            decision = DecisionLog(
                timestamp=trade.exit_time or datetime.now(timezone.utc),
                signal="CLOSE",
                price=trade.exit_price,
                mode=self.strategy.get_mode_name() if hasattr(self.strategy, 'get_mode_name') else "unknown",
                reason=trade.exit_reason or "unknown",
                trade_id=f"t_{trade.entry_time.strftime('%Y%m%d_%H%M%S')}_{id(trade) % 0xFFFFFF:06x}" if trade.entry_time else "",
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                pnl_points=trade.pnl,
                hold_time_minutes=hold_time_minutes,
                strategy=self.strategy.__class__.__name__,
            )

            self._decision_logger.log_close(decision, entry_price=trade.entry_price, entry_time=trade.entry_time)
        except Exception as e:
            logger.error(f"Failed to log trade close: {e}")

    def _record_equity(self):
        """자산 기록"""
        self.equity_history.append({
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'equity': self.broker.equity,
            'capital': self.broker.capital,
            'unrealized_pnl': self.broker.position.unrealized_pnl,
        })

    async def _cleanup(self):
        """종료 정리"""
        # 미청산 포지션 청산
        if self.broker.position.is_open:
            # 현재 포지션 방향에 따라 반대 주문
            if self.kis_bridge:
                side = "SELL" if self.broker.position.side == PositionSide.LONG else "BUY"
                # Use broker's current price (not position.current_price which doesn't exist)
                self.kis_bridge.place_order(side, 1, self.broker._current_price)
            self.broker.close_position(reason="SESSION_END")
            logger.info("Closed remaining position at session end")

    def get_result(self) -> Dict[str, Any]:
        """결과 반환"""
        duration = (
            (self.end_time - self.start_time).total_seconds()
            if self.end_time and self.start_time else 0
        )

        summary = self.broker.get_summary()

        return {
            'run_id': self.run_id,
            'strategy': self.strategy.__class__.__name__,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'duration_seconds': duration,
            'bars_processed': self.bars_processed,
            'signals_generated': self.signals_generated,
            'kis_enabled': self.kis_bridge is not None,
            'kis_orders': self.kis_bridge.orders if self.kis_bridge else [],
            **summary,
            'trades': [
                {
                    'entry_time': t.entry_time.isoformat(),
                    'exit_time': t.exit_time.isoformat() if t.exit_time else None,
                    'side': t.side.value,
                    'entry_price': t.entry_price,
                    'exit_price': t.exit_price,
                    'pnl': t.pnl,
                    'pnl_amount': t.pnl_amount,
                    'exit_reason': t.exit_reason,
                }
                for t in self.broker.trades
            ],
        }

    def print_summary(self):
        """결과 요약 출력"""
        result = self.get_result()

        print("\n" + "=" * 50)
        print("Paper Trading Result")
        print("=" * 50)
        print(f"Strategy: {result['strategy']}")
        print(f"Duration: {result['duration_seconds']:.0f} seconds")
        print(f"Bars Processed: {result['bars_processed']}")
        print(f"Signals Generated: {result['signals_generated']}")
        print("-" * 50)
        print(f"Initial Capital: {result['initial_capital']:,.0f} KRW")
        print(f"Final Equity: {result['equity']:,.0f} KRW")
        print(f"Total Return: {result['total_return']:+.2f}%")
        print(f"Total PnL: {result['total_pnl']:+,.0f} KRW")
        print("-" * 50)
        print(f"Total Trades: {result['total_trades']}")
        print(f"Winning: {result['winning_trades']}")
        print(f"Losing: {result['losing_trades']}")
        print(f"Win Rate: {result['win_rate']:.1f}%")
        print("=" * 50)


# CLI용 간단한 실행 함수
async def run_paper_trading(
    strategy_name: str = "hybrid",
    duration_seconds: int = 3600,
    initial_capital: float = 10_000_000,
) -> Dict[str, Any]:
    """
    Paper Trading 실행

    :param strategy_name: 전략 이름
    :param duration_seconds: 실행 시간 (초)
    :param initial_capital: 초기 자본
    :return: 결과 딕셔너리
    """
    # 전략 로드
    from src.strategy import (
        MeanReversionStrategy,
        BreakoutStrategy,
        OFIMomentumStrategy,
        HybridStrategy,
        PureMicrostructureStrategy,
        AdaptiveMicrostructureStrategy,
        TrendConfirmedStrategy,
    )

    strategy_classes = {
        "mean_reversion": MeanReversionStrategy,
        "breakout": BreakoutStrategy,
        "ofi_momentum": OFIMomentumStrategy,
        "hybrid": HybridStrategy,
        "pure_micro": PureMicrostructureStrategy,
        "adaptive_micro": AdaptiveMicrostructureStrategy,
        "trend_confirmed": TrendConfirmedStrategy,
    }

    if strategy_name in ("mode_b", "dual_mode"):
        logger.warning("Strategy '%s' is deprecated; using 'trend_confirmed'.", strategy_name)
        strategy_name = "trend_confirmed"

    if strategy_name not in strategy_classes:
        raise ValueError(f"Unknown strategy: {strategy_name}")

    strategy = strategy_classes[strategy_name]()

    # 브로커 생성
    broker = VirtualBroker(initial_capital=initial_capital)

    # Redis 클라이언트 (옵션)
    redis_client = None
    try:
        import redis.asyncio as aioredis
        redis_url = f"redis://{settings.redis.host}:{settings.redis.port}/{settings.redis.db}"
        redis_client = await aioredis.from_url(redis_url)
        await redis_client.ping()
    except Exception:
        logger.warning("Redis not available - running in simulation mode")
        redis_client = None

    # 엔진 생성 및 실행
    engine = PaperTradingEngine(
        strategy=strategy,
        broker=broker,
        redis_client=redis_client,
    )

    await engine.start(duration_seconds=duration_seconds)
    engine.print_summary()

    if redis_client:
        await redis_client.aclose()

    return engine.get_result()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_paper_trading(duration_seconds=60))
