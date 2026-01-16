"""
Strategy Manager 모듈
FEATURE_STREAM + PREDICTION_STREAM → ORDER_COMMAND_STREAM
이원화 전략 (Mode A/B) 및 주문 실행

Mode A: Pure Basis Arbitrage (ArbitrageEngine)
Mode B: Deep Learning Trend Following (TrendEngine)
"""
import time
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from enum import Enum
from abc import ABC, abstractmethod

import sys
from config.settings import settings
from src.common import (
    StreamConsumer,
    StreamPublisher,
    StreamMessage,
    RedisClient,
    setup_logging,
    init_metrics,
    get_metrics,
    trading_logger
)
from src.strategy.base import BarData
from src.strategy.arbitrage import ArbitrageEngine, ArbitrageSignal
from src.strategy.arbitrage.basis_calculator import calculate_days_to_expiry
from src.strategy.trend import TrendEngine, TrendSignal

logger = setup_logging("strategy")


class TradingMode(Enum):
    """트레이딩 모드"""
    AVOID = "AVOID"      # 회피 구간
    MODE_A = "MODE_A"    # 스나이퍼 차익거래
    MODE_B = "MODE_B"    # 딥러닝 추세 매매


class OrderSide(Enum):
    """주문 방향"""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    """주문 유형"""
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@dataclass
class OrderCommand:
    """주문 명령"""
    symbol: str
    side: OrderSide
    order_type: OrderType
    size: float
    price: Optional[float] = None
    strategy_id: str = ""
    mode: TradingMode = TradingMode.MODE_B
    timestamp: float = 0.0
    
    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'side': self.side.value,
            'order_type': self.order_type.value,
            'size': self.size,
            'price': self.price,
            'strategy_id': self.strategy_id,
            'mode': self.mode.value,
            'timestamp': self.timestamp
        }


class BaseOrderExecutor(ABC):
    """주문 실행기 베이스 클래스"""
    
    @abstractmethod
    def execute(self, order: OrderCommand) -> bool:
        """주문 실행"""
        pass


class DryRunOrderExecutor(BaseOrderExecutor):
    """테스트용 주문 실행기 (실제 주문 없음)"""
    
    def __init__(self):
        self.orders: List[OrderCommand] = []
    
    def execute(self, order: OrderCommand) -> bool:
        logger.info(
            f"[DRY RUN] {order.side.value} {order.size} {order.symbol} "
            f"@ {order.price or 'MARKET'} (Mode: {order.mode.value})"
        )
        self.orders.append(order)
        return True


class StrategyManager(StreamConsumer):
    """
    Strategy Manager 메인 클래스

    두 개의 Stream을 소비:
    - FEATURE_STREAM: 유동성 정보
    - PREDICTION_STREAM: 모델 예측 결과

    이원화 전략:
    - Mode A: Pure Basis Arbitrage (ArbitrageEngine) - No ML dependency
    - Mode B: 딥러닝 추세 매매 (높은 신뢰도 예측)
    """

    def __init__(self, order_executor: BaseOrderExecutor = None):
        # Prediction Stream 소비
        super().__init__(
            stream_name=settings.redis.prediction_stream,
            group_name=settings.consumer.strategy_group,
            consumer_name="strategy_1",
            component_name="strategy_manager"  # For health check heartbeat
        )

        self.order_publisher = StreamPublisher(settings.redis.order_stream)
        self.redis = RedisClient.get_client()

        # 주문 실행기
        self.executor = order_executor or DryRunOrderExecutor()

        # 전략 설정
        self.cfg = settings.strategy
        self.arb_cfg = settings.arbitrage

        # MODE_A: ArbitrageEngine (Pure Basis Arbitrage)
        self.arbitrage_engine = ArbitrageEngine(
            max_spread_ticks=self.arb_cfg.max_spread_ticks,
            depth_multiplier=self.arb_cfg.depth_multiplier,
            basis_threshold=self.arb_cfg.basis_threshold,
            order_size=self.arb_cfg.order_size,
            quarterly_blackout_days=self.arb_cfg.quarterly_blackout_days,
            risk_free_rate=self.arb_cfg.risk_free_rate,
            basis_rolling_window=self.arb_cfg.basis_rolling_window,
        )

        # MODE_B: TrendEngine (Deep Learning Trend Following)
        self.trend_cfg = settings.trend
        self.trend_engine = TrendEngine(
            dl_threshold=self.trend_cfg.dl_threshold,
            ma_fast_period=self.trend_cfg.ma_fast_period,
            ma_slow_period=self.trend_cfg.ma_slow_period,
            atr_period=self.trend_cfg.atr_period,
            atr_stop_multiplier=self.trend_cfg.atr_stop_multiplier,
            time_cut_minutes=self.trend_cfg.time_cut_minutes,
            time_cut_atr_threshold=self.trend_cfg.time_cut_atr_threshold,
            order_size=self.trend_cfg.order_size,
            min_bars_required=self.trend_cfg.min_bars_required,
        )

        # Index data cache (from INDEX_STREAM)
        self._index_value: Optional[float] = None
        self._index_update_time: float = 0.0

        # 캐시된 Feature 데이터 (symbol -> latest feature)
        self._feature_cache: Dict[str, Dict] = {}

        # Last bar timestamp for MODE_B bar updates
        self._last_bar_time: Dict[str, float] = {}

        # 통계
        self._order_count = 0
        self._mode_counts = {m: 0 for m in TradingMode}
    
    def _get_latest_feature(self, symbol: str) -> Optional[Dict]:
        """
        심볼의 최신 Feature 조회
        Redis에서 직접 읽거나 캐시 사용
        """
        # 간단한 구현: Feature Window에서 마지막 값
        key = f"feature_window:{symbol}"
        try:
            data = self.redis.get(key)
            if data:
                import json
                features = json.loads(data)
                if features:
                    return features[-1]
        except Exception as e:
            logger.error(f"Failed to get feature: {e}")
        return self._feature_cache.get(symbol)

    def _get_latest_index_value(self) -> Optional[float]:
        """
        Get latest KOSPI200 index value from Redis INDEX_STREAM.
        Returns cached value if fresh enough (within 5 seconds).
        """
        # Check if cached value is fresh enough
        if self._index_value and (time.time() - self._index_update_time) < 5.0:
            return self._index_value

        try:
            # Read latest from INDEX_STREAM
            stream_name = self.arb_cfg.index_stream
            result = self.redis.xrevrange(stream_name, count=1)
            if result:
                _, data = result[0]
                self._index_value = float(data.get(b'value', data.get('value', 0)))
                self._index_update_time = time.time()
                return self._index_value
        except Exception as e:
            logger.debug(f"Failed to get index value: {e}")

        return self._index_value
    
    def _determine_mode(
        self, 
        liquidity_score: float, 
        basis_gap: float, 
        up_prob: float
    ) -> TradingMode:
        """
        현재 시장 상태에 따른 모드 결정
        """
        # 1. 회피 구간 체크
        if liquidity_score < self.cfg.liquidity_avoid_threshold:
            return TradingMode.AVOID
        
        # 2. Mode A 조건 체크
        if (liquidity_score > self.cfg.mode_a_liquidity_threshold and
            basis_gap > self.cfg.mode_a_basis_gap_sigma):
            return TradingMode.MODE_A
        
        # 3. Mode B
        return TradingMode.MODE_B
    
    def _execute_mode_a(
        self,
        symbol: str,
        feature: Dict
    ) -> Optional[OrderCommand]:
        """
        Mode A: Pure Basis Arbitrage (ArbitrageEngine)

        Uses ArbitrageEngine to check entry conditions:
        1. Time filter (trading hours)
        2. Dividend blackout check
        3. Spread filter (≤2 ticks)
        4. Depth filter (≥5x order size)
        5. Basis signal (|z| > 2.5σ)

        No ML dependency - trades solely on basis deviation.
        """
        # Get KOSPI200 index value
        spot_index = self._get_latest_index_value()
        if not spot_index:
            logger.debug("No index value available for arbitrage")
            return None

        # Create BarData from feature dict
        bar = BarData.from_dict(feature)

        # Calculate days to expiry (default to front month A05601)
        days_to_expiry = calculate_days_to_expiry("A05601")

        # Check entry with ArbitrageEngine
        can_enter, signal, reason = self.arbitrage_engine.check_entry(
            bar=bar,
            spot_index=spot_index,
            days_to_expiry=days_to_expiry,
            current_time=datetime.now()
        )

        if not can_enter:
            logger.debug(f"MODE_A rejected: {reason}")
            return None

        # Generate order from signal
        if signal:
            side = OrderSide.BUY if signal.direction == "BUY" else OrderSide.SELL
            return OrderCommand(
                symbol=symbol,
                side=side,
                order_type=OrderType.LIMIT,
                size=signal.order_size,
                price=signal.entry_price,  # Maker price (best bid/ask)
                strategy_id=f"ARBITRAGE_{signal.direction}",
                mode=TradingMode.MODE_A
            )

        return None
    
    def _execute_mode_b(
        self,
        symbol: str,
        up_prob: float,
        feature: Dict
    ) -> Optional[OrderCommand]:
        """
        Mode B: Deep Learning Trend Following (TrendEngine)

        Uses TrendEngine with ensemble filter:
        1. DL probability > 85%
        2. MA(20) > MA(60) for long
        3. Price above Ichimoku cloud

        Plus ATR trailing stop and 30-min time cut.
        """
        # Update bar data for technical indicators (on new bar)
        self._update_trend_bar(symbol, feature)

        # Update current prices for order execution
        bid = float(feature.get('bid_price_1', feature.get('best_bid', 0)))
        ask = float(feature.get('ask_price_1', feature.get('best_ask', 0)))
        self.trend_engine.update_prices(bid, ask)

        # Current price
        current_price = float(feature.get('close', feature.get('mid_price', 0)))
        if current_price <= 0:
            current_price = (bid + ask) / 2 if bid > 0 and ask > 0 else 0

        # Check entry/exit with TrendEngine
        signal = self.trend_engine.check(
            up_prob=up_prob,
            current_price=current_price,
            symbol=symbol,
        )

        # Handle signal
        if signal.action == "HOLD":
            logger.debug(f"MODE_B: {signal.reason}")
            return None

        if signal.action.startswith("OPEN_"):
            # Entry signal
            side = OrderSide.BUY if signal.direction == "LONG" else OrderSide.SELL
            return OrderCommand(
                symbol=symbol,
                side=side,
                order_type=OrderType.MARKET,  # Aggressive taker
                size=signal.size,
                price=signal.price,
                strategy_id=f"TREND_{signal.direction}",
                mode=TradingMode.MODE_B
            )

        if signal.action == "CLOSE":
            # Exit signal
            side = OrderSide.SELL if signal.direction == "LONG" else OrderSide.BUY
            return OrderCommand(
                symbol=symbol,
                side=side,
                order_type=OrderType.MARKET,  # Immediate exit
                size=signal.size,
                price=signal.price,
                strategy_id=f"TREND_EXIT_{signal.exit_reason.value if signal.exit_reason else 'MANUAL'}",
                mode=TradingMode.MODE_B
            )

        return None

    def _update_trend_bar(self, symbol: str, feature: Dict):
        """Update TrendEngine with new bar data if bar timestamp changed."""
        bar_time = float(feature.get('timestamp', 0))
        last_time = self._last_bar_time.get(symbol, 0)

        # Check if this is a new bar (timestamp changed)
        if bar_time > last_time:
            high = float(feature.get('high', feature.get('close', 0)))
            low = float(feature.get('low', feature.get('close', 0)))
            close = float(feature.get('close', feature.get('mid_price', 0)))

            if high > 0 and low > 0 and close > 0:
                self.trend_engine.update_bar(high, low, close)
                self._last_bar_time[symbol] = bar_time
    
    def process_message(self, message: StreamMessage) -> bool:
        """
        예측 메시지 처리 및 전략 실행
        """
        try:
            data = message.data
            symbol = data.get('symbol')
            
            if not symbol:
                return True
            
            # 1. 예측 데이터 파싱
            up_prob = float(data.get('up_prob', 0.5))
            down_prob = float(data.get('down_prob', 0.5))
            
            # 2. Feature 데이터 조회
            feature = self._get_latest_feature(symbol)
            if not feature:
                logger.debug(f"No feature data for {symbol}")
                return True
            
            liquidity_score = float(feature.get('liquidity_score', 0))
            basis_gap = float(feature.get('basis_gap', 0))  # 현물-선물 괴리
            
            # 3. 모드 결정
            mode = self._determine_mode(liquidity_score, basis_gap, up_prob)
            self._mode_counts[mode] += 1

            # OFI Z-Score 조회 (상세 로깅용)
            ofi_z_score = float(feature.get('ofi_z_score', 0))

            # 상세 로깅: 전략 평가
            trading_logger.log_strategy_evaluation(
                symbol=symbol,
                timestamp=time.time(),
                up_prob=up_prob,
                down_prob=down_prob,
                liquidity_score=liquidity_score,
                basis_gap=basis_gap,
                ofi_z_score=ofi_z_score,
                mode=mode.value,
                feature_data=feature
            )

            # 4. 모드별 실행
            order: Optional[OrderCommand] = None

            if mode == TradingMode.AVOID:
                logger.debug(f"{symbol}: AVOID mode (liquidity={liquidity_score:.1f})")
                trading_logger.log_no_order(
                    symbol=symbol,
                    timestamp=time.time(),
                    reason=f"AVOID mode - Low liquidity: {liquidity_score:.1f}"
                )
                # TODO: 기존 포지션 청산 로직

            elif mode == TradingMode.MODE_A:
                order = self._execute_mode_a(symbol, feature)
                if not order:
                    # Get rejection reason from arbitrage engine stats
                    arb_stats = self.arbitrage_engine.get_stats()
                    trading_logger.log_no_order(
                        symbol=symbol,
                        timestamp=time.time(),
                        reason=f"MODE_A - Arbitrage filter rejected (pass_rate={arb_stats.get('pass_rate', 0):.1%})"
                    )

            elif mode == TradingMode.MODE_B:
                order = self._execute_mode_b(symbol, up_prob, feature)
                if not order:
                    # Get rejection reason from trend engine
                    trend_stats = self.trend_engine.get_stats()
                    filter_stats = trend_stats.get('filter', {})
                    trading_logger.log_no_order(
                        symbol=symbol,
                        timestamp=time.time(),
                        reason=f"MODE_B - Ensemble filter rejected (signal_rate={filter_stats.get('signal_rate', 0):.1%})"
                    )
            
            # 5. 주문 실행
            if order:
                # Build reason and scores based on mode
                if order.mode == TradingMode.MODE_A:
                    basis_data = self.arbitrage_engine.basis_calculator.get_current_data()
                    reason = f"Basis z={basis_data.basis_zscore:.2f}σ" if basis_data else "Arbitrage signal"
                    scores = {
                        'basis_zscore': basis_data.basis_zscore if basis_data else 0,
                        'basis': basis_data.basis if basis_data else 0,
                        'fair_value': basis_data.fair_value if basis_data else 0,
                        'liquidity_score': liquidity_score,
                    }
                elif order.mode == TradingMode.MODE_B:
                    tech = self.trend_engine.technical.get_current_data()
                    pos_info = self.trend_engine.position_manager.get_position_info()
                    if 'EXIT' in order.strategy_id:
                        reason = f"Exit: {order.strategy_id}"
                    else:
                        reason = f"Ensemble passed: DL={up_prob:.1%}, MA_bull={tech.is_bullish_ma if tech else 'N/A'}"
                    scores = {
                        'up_prob': up_prob,
                        'ma_fast': tech.ma_fast if tech else 0,
                        'ma_slow': tech.ma_slow if tech else 0,
                        'atr': tech.atr if tech else 0,
                        'stop_price': pos_info.get('current_stop', 0),
                    }
                else:
                    reason = f"Up={up_prob:.3f}, Down={down_prob:.3f}, LIQ={liquidity_score:.1f}"
                    scores = {
                        'ofi_score': ofi_z_score,
                        'liquidity_score': liquidity_score,
                        'up_prob': up_prob,
                        'down_prob': down_prob
                    }

                # 상세 로깅: 주문 결정
                trading_logger.log_order_decision(
                    symbol=order.symbol,
                    timestamp=order.timestamp,
                    side=order.side.value,
                    order_type=order.order_type.value,
                    size=order.size,
                    price=order.price,
                    strategy_id=order.strategy_id,
                    mode=order.mode.value,
                    reason=reason,
                    scores=scores
                )

                success = self.executor.execute(order)
                metrics = get_metrics()

                # 시그널 메트릭 기록
                metrics.record_signal(
                    strategy=order.strategy_id,
                    signal_type=order.mode.value,
                    direction=order.side.value
                )

                if success:
                    self.order_publisher.publish(order.to_dict())
                    self._order_count += 1

                    # 주문 성공 메트릭
                    metrics.record_order(
                        strategy=order.strategy_id,
                        order_type=order.order_type.value,
                        side=order.side.value,
                        status="SUCCESS"
                    )

                    logger.info(
                        f"Order: {order.side.value} {order.size} {symbol} "
                        f"[{order.mode.value}] Up={up_prob:.2f}"
                    )
                else:
                    # 주문 실패 메트릭
                    metrics.record_order(
                        strategy=order.strategy_id,
                        order_type=order.order_type.value,
                        side=order.side.value,
                        status="FAILED"
                    )
            
            # 6. 주기적 통계
            total_processed = sum(self._mode_counts.values())
            if total_processed % 1000 == 0 and total_processed > 0:
                logger.info(
                    f"Stats - Orders: {self._order_count}, "
                    f"Modes: {dict(self._mode_counts)}"
                )
            
            return True
            
        except Exception as e:
            logger.error(f"Strategy error: {e}", exc_info=True)
            return False


class KISOrderExecutorAdapter(BaseOrderExecutor):
    """한투 주문 실행기 어댑터"""

    def __init__(self):
        from src.collector.kis_order import KISOrderExecutor, create_order_api_from_env
        import os

        # DRY_RUN 설정에 따라 실제 주문 여부 결정
        dry_run = settings.dry_run

        if dry_run:
            self._executor = KISOrderExecutor(api=None, dry_run=True)
        else:
            try:
                api = create_order_api_from_env()
                self._executor = KISOrderExecutor(api=api, dry_run=False)
                logger.info("KIS Order API initialized for LIVE trading")
            except Exception as e:
                logger.error(f"Failed to initialize KIS API: {e}. Falling back to DRY RUN.")
                self._executor = KISOrderExecutor(api=None, dry_run=True)

    def execute(self, order: OrderCommand) -> bool:
        order_no = self._executor.execute(
            symbol=order.symbol,
            side=order.side.value.lower(),
            size=order.size,
            price=order.price or 0,
            order_type="market" if order.order_type == OrderType.MARKET else "limit"
        )
        return order_no is not None


def main():
    """Strategy Manager 실행"""
    logger.info("Starting Strategy Manager...")

    # 메트릭 서버 시작 (포트 8082)
    init_metrics("strategy_manager", port=8082)

    # Dry Run 모드 확인
    if settings.dry_run:
        logger.warning("Running in DRY RUN mode. No real orders will be placed.")
        executor = DryRunOrderExecutor()
    else:
        logger.warning("Running in LIVE mode. Real orders will be placed!")
        executor = KISOrderExecutorAdapter()

    manager = StrategyManager(order_executor=executor)

    try:
        manager.run()
    except KeyboardInterrupt:
        manager.stop()


if __name__ == "__main__":
    main()
