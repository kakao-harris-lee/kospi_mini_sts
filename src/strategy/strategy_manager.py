"""
Strategy Manager 모듈
FEATURE_STREAM + PREDICTION_STREAM → ORDER_COMMAND_STREAM
"""
import time
import logging
from datetime import datetime, timezone
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

logger = setup_logging("strategy")


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
    mode: str = ""
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()
        # Validate minimum order quantity (1 contract for KOSPI Mini Futures)
        if self.size < 1:
            raise ValueError(f"Order size must be at least 1 contract, got {self.size}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol': self.symbol,
            'side': self.side.value,
            'order_type': self.order_type.value,
            'size': self.size,
            'price': self.price,
            'strategy_id': self.strategy_id,
            'mode': self.mode,
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
            f"@ {order.price or 'MARKET'} (Mode: {order.mode or 'N/A'})"
        )
        self.orders.append(order)
        return True


class StrategyManager(StreamConsumer):
    """
    Strategy Manager 메인 클래스

    두 개의 Stream을 소비:
    - FEATURE_STREAM: 유동성 정보
    - PREDICTION_STREAM: 모델 예측 결과

    설정된 전략을 사용하여 주문을 생성합니다.
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

        # 설정 로드
        self.cfg = settings.strategy
        self.trend_cfg = settings.trend

        # 전략 초기화 (settings.STRATEGY 기준)
        self.strategy_name = (self.cfg.strategy_name or "trend_confirmed").lower()
        self.strategy = self._build_strategy(self.strategy_name)
        self.strategy_id = getattr(self.strategy, "name", self.strategy.__class__.__name__)
        self.mode_name = self._resolve_mode_name(self.strategy) or self.strategy_id
        logger.info(f"Initialized strategy: {self.strategy_id} (mode={self.mode_name})")

        # 캐시된 Feature 데이터 (symbol -> latest feature)
        self._feature_cache: Dict[str, Dict] = {}

        # 통계
        self._order_count = 0
        self._processed_count = 0

        # Per-trade Telegram alerts (Strategy Manager)
        self._trade_alerts_enabled = settings.strategy.trade_alerts_enabled
        self._trade_notifier = None
        if self._trade_alerts_enabled:
            try:
                from src.common.telegram import TelegramNotifier
                self._trade_notifier = TelegramNotifier(
                    check_trading_day=settings.monitoring.telegram_check_trading_day
                )
                if not self._trade_notifier.bot_token or not self._trade_notifier.chat_id:
                    logger.warning(
                        "Trade alerts enabled but Telegram not configured; disabling trade alerts."
                    )
                    self._trade_alerts_enabled = False
                    self._trade_notifier = None
            except Exception as e:
                logger.warning(f"Failed to initialize trade alerts: {e}")
                self._trade_alerts_enabled = False
                self._trade_notifier = None

    def _build_strategy(self, name: str):
        """Instantiate strategy based on configured name."""
        key = (name or "").lower()

        if key in ("dual_mode", "dual-mode"):
            logger.warning("Strategy 'dual_mode' is deprecated; using 'trend_confirmed'.")
            key = "trend_confirmed"

        if key in ("mode_b", "modeb"):
            logger.warning("Strategy 'mode_b' is deprecated; using 'trend_confirmed'.")
            key = "trend_confirmed"

        if key in ("trend_confirmed", "trend-confirmed"):
            from src.strategy.strategies.trend_confirmed import (
                TrendConfirmedStrategy,
                TrendConfirmedConfig,
            )

            config = TrendConfirmedConfig(
                # Trend Confirmed: Triple Barrier & Trend Settings
                # (Triple Barrier 모델 경로는 기본값 사용)
                trend_dl_threshold=self.trend_cfg.dl_threshold,
                trend_ma_fast=self.trend_cfg.ma_fast_period,
                trend_ma_slow=self.trend_cfg.ma_slow_period,
                trend_atr_period=self.trend_cfg.atr_period,
                trend_atr_multiplier=self.trend_cfg.atr_stop_multiplier,
                trend_time_cut_minutes=self.trend_cfg.time_cut_minutes,
                trend_time_cut_atr_threshold=self.trend_cfg.time_cut_atr_threshold,

                # Common
                order_size=self.trend_cfg.order_size,
                enable_decision_logging=True,
                enable_telegram=True,
                enable_clickhouse=True
            )

            return TrendConfirmedStrategy(config=config)

        from src.strategy.strategies import (
            MeanReversionStrategy,
            BreakoutStrategy,
            OFIMomentumStrategy,
            HybridStrategy,
            PureMicrostructureStrategy,
            AdaptiveMicrostructureStrategy,
        )

        strategy_map = {
            "mean_reversion": MeanReversionStrategy,
            "breakout": BreakoutStrategy,
            "ofi_momentum": OFIMomentumStrategy,
            "hybrid": HybridStrategy,
            "pure_micro": PureMicrostructureStrategy,
            "adaptive_micro": AdaptiveMicrostructureStrategy,
        }

        if key in strategy_map:
            return strategy_map[key]()

        logger.warning(f"Unknown strategy '{name}'. Falling back to trend_confirmed.")
        return self._build_strategy("trend_confirmed")

    def _resolve_mode_name(self, strategy) -> Optional[str]:
        """Resolve mode name from strategy (if available)."""
        if hasattr(strategy, "get_mode_name"):
            try:
                return strategy.get_mode_name()
            except Exception:
                return None
        return getattr(strategy, "mode_name", None)

    def _send_trade_alert(self, order: OrderCommand, ref_price: Optional[float]) -> None:
        """Send per-trade alert via Telegram."""
        if not self._trade_alerts_enabled or not self._trade_notifier:
            return

        # Price display (market orders use reference price if provided)
        if order.order_type == OrderType.MARKET:
            price_str = f"{ref_price:.2f}" if ref_price is not None else "MKT"
        else:
            price_str = f"{order.price:.2f}" if order.price is not None else "N/A"

        run_mode = "DRY RUN" if settings.dry_run else "LIVE"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        mode_label = order.mode or "N/A"
        msg = (
            f"<b>📌 Trade {run_mode}</b>\n"
            f"<b>Strategy:</b> {order.strategy_id}\n"
            f"<b>Symbol:</b> {order.symbol}\n"
            f"<b>Side:</b> {order.side.value}\n"
            f"<b>Size:</b> {order.size}\n"
            f"<b>Type:</b> {order.order_type.value}\n"
            f"<b>Price:</b> {price_str}\n"
            f"<b>Mode:</b> {mode_label}\n"
            f"<i>{ts}</i>"
        )

        if not self._trade_notifier.send(msg):
            logger.warning(
                "Trade alert send failed: %s %s %s",
                order.side.value,
                order.symbol,
                price_str,
            )

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

    def process_message(self, message: StreamMessage) -> bool:
        """
        예측 메시지 처리 및 전략 실행
        """
        try:
            data = message.data
            symbol = data.get('symbol')

            if not symbol:
                return True

            # 1. 예측 데이터 파싱 (Legacy support & Fallback)
            up_prob = float(data.get('up_prob', 0.5))
            down_prob = float(data.get('down_prob', 0.5))

            # 2. Feature 데이터 조회
            feature = self._get_latest_feature(symbol)
            if not feature:
                logger.debug(f"No feature data for {symbol}")
                return True

            # 3. BarData 생성 및 데이터 주입
            # Feature 데이터를 BarData 객체로 변환
            bar = BarData.from_dict(feature)

            # 예측값 주입 (전략에서 사용)
            bar.up_prob = up_prob
            bar.down_prob = down_prob

            # Ensemble 확률이 있다면 주입 (Optional)
            if 'up_prob_h10' in data:
                bar.up_prob_h1 = float(data.get('up_prob_h1', 0.5))
                bar.up_prob_h3 = float(data.get('up_prob_h3', 0.5))
                bar.up_prob_h5 = float(data.get('up_prob_h5', 0.5))
                bar.up_prob_h10 = float(data.get('up_prob_h10', 0.5))
            else:
                # Single model일 경우 h10을 메인 확률로 매핑
                bar.up_prob_h10 = up_prob

            # 4. 전략에게 판단 위임
            from src.strategy.base import Signal
            signal = self.strategy.generate_signal(bar)

            # 처리 카운트 업데이트
            self._processed_count += 1

            # 5. 주문 실행 처리
            if signal == Signal.BUY or signal == Signal.SELL:
                side = OrderSide.BUY if signal == Signal.BUY else OrderSide.SELL

                # 주문 유형 결정 (기본: 시장가)
                order_type = OrderType.MARKET
                price = None
                order_size = getattr(getattr(self.strategy, "config", None), "order_size", 1.0)

                # 주문 명령 생성
                order = OrderCommand(
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    size=order_size,
                    price=price,
                    strategy_id=self.strategy_id,
                    mode=self.mode_name,
                    timestamp=time.time()
                )

                # 주문 실행
                success = self.executor.execute(order)

                if success:
                    self.order_publisher.publish(order.to_dict())
                    self._order_count += 1

                    logger.info(
                        f"Order Placed: {side.value} {order.size} {symbol} "
                        f"[{self.mode_name}] Price={price or 'MKT'}"
                    )

                    # Per-trade alert (Telegram)
                    self._send_trade_alert(order, ref_price=bar.close)

            # 6. 주기적 통계 로그
            if self._processed_count % 1000 == 0 and self._processed_count > 0:
                if hasattr(self.strategy, "get_stats"):
                    stats = self.strategy.get_stats()
                    logger.info(f"Strategy Stats: {stats}")

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
