"""
Strategy Manager 모듈
FEATURE_STREAM + PREDICTION_STREAM → ORDER_COMMAND_STREAM
이원화 전략 (Mode A/B) 및 주문 실행
"""
import time
import logging
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
    get_metrics
)

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
    - Mode A: 스나이퍼 차익거래 (유동성↑ + 괴리↑ + 예측↑)
    - Mode B: 딥러닝 추세 매매 (높은 신뢰도 예측)
    """
    
    def __init__(self, order_executor: BaseOrderExecutor = None):
        # Prediction Stream 소비
        super().__init__(
            stream_name=settings.redis.prediction_stream,
            group_name=settings.consumer.strategy_group,
            consumer_name="strategy_1"
        )
        
        self.order_publisher = StreamPublisher(settings.redis.order_stream)
        self.redis = RedisClient.get_client()
        
        # 주문 실행기
        self.executor = order_executor or DryRunOrderExecutor()
        
        # 전략 설정
        self.cfg = settings.strategy
        
        # 캐시된 Feature 데이터 (symbol -> latest feature)
        self._feature_cache: Dict[str, Dict] = {}
        
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
        up_prob: float, 
        down_prob: float,
        feature: Dict
    ) -> Optional[OrderCommand]:
        """
        Mode A: 스나이퍼 차익거래
        """
        if up_prob > self.cfg.mode_a_up_prob_threshold:
            return OrderCommand(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                size=self.cfg.mode_a_order_size,
                price=feature.get('mid_price'),
                strategy_id="ARBITRAGE_BUY",
                mode=TradingMode.MODE_A
            )
        elif down_prob > self.cfg.mode_a_up_prob_threshold:
            return OrderCommand(
                symbol=symbol,
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                size=self.cfg.mode_a_order_size,
                price=feature.get('mid_price'),
                strategy_id="ARBITRAGE_SELL",
                mode=TradingMode.MODE_A
            )
        return None
    
    def _execute_mode_b(
        self, 
        symbol: str, 
        up_prob: float, 
        down_prob: float,
        feature: Dict
    ) -> Optional[OrderCommand]:
        """
        Mode B: 딥러닝 추세 매매
        """
        if up_prob > self.cfg.mode_b_up_prob_buy:
            return OrderCommand(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                size=self.cfg.mode_b_order_size,
                price=feature.get('mid_price'),
                strategy_id="DIRECTIONAL_BUY",
                mode=TradingMode.MODE_B
            )
        elif down_prob > (1 - self.cfg.mode_b_down_prob_sell):
            return OrderCommand(
                symbol=symbol,
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                size=self.cfg.mode_b_order_size,
                price=feature.get('mid_price'),
                strategy_id="DIRECTIONAL_SELL",
                mode=TradingMode.MODE_B
            )
        return None
    
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
            
            # 4. 모드별 실행
            order: Optional[OrderCommand] = None
            
            if mode == TradingMode.AVOID:
                logger.debug(f"{symbol}: AVOID mode (liquidity={liquidity_score:.1f})")
                # TODO: 기존 포지션 청산 로직
                
            elif mode == TradingMode.MODE_A:
                order = self._execute_mode_a(symbol, up_prob, down_prob, feature)
                
            elif mode == TradingMode.MODE_B:
                order = self._execute_mode_b(symbol, up_prob, down_prob, feature)
            
            # 5. 주문 실행
            if order:
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
