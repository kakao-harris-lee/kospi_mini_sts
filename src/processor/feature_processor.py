"""
Feature Processor 모듈
RAW_DATA_STREAM → FEATURE_STREAM
OFI, 유동성 점수, 1분봉 Feature 계산
"""
import time
import json
import logging
import numpy as np
from typing import Dict, Any, Optional, List, Deque
from collections import deque
from dataclasses import dataclass, field

import sys
from config.settings import settings
from src.common import StreamConsumer, StreamPublisher, StreamMessage, setup_logging, RedisClient, init_metrics, get_metrics, ClickHouseClient, trading_logger

logger = setup_logging("processor")


@dataclass
class OrderBookSnapshot:
    """호가 스냅샷"""
    timestamp: float
    bid_price: float
    bid_qty: float
    ask_price: float
    ask_qty: float


class OFICalculator:
    """
    Order Flow Imbalance (OFI) 계산기
    호가 변화를 기반으로 매수/매도 압력 측정
    """
    
    def __init__(self, lookback: int = 100):
        self.prev_snapshot: Optional[OrderBookSnapshot] = None
        self.ofi_history: Deque[float] = deque(maxlen=lookback)
        self.lookback = lookback
    
    def calculate_tick_ofi(
        self, 
        bid_price: float, 
        bid_qty: float, 
        ask_price: float, 
        ask_qty: float,
        timestamp: float = None
    ) -> float:
        """
        틱 단위 OFI 계산
        
        Returns:
            OFI 값 (양수: 매수 압력, 음수: 매도 압력)
        """
        current = OrderBookSnapshot(
            timestamp=timestamp or time.time(),
            bid_price=bid_price,
            bid_qty=bid_qty,
            ask_price=ask_price,
            ask_qty=ask_qty
        )
        
        ofi = 0.0
        
        if self.prev_snapshot:
            # Bid side contribution
            if current.bid_price > self.prev_snapshot.bid_price:
                ofi += current.bid_qty
            elif current.bid_price == self.prev_snapshot.bid_price:
                ofi += (current.bid_qty - self.prev_snapshot.bid_qty)
            else:
                ofi -= self.prev_snapshot.bid_qty
            
            # Ask side contribution
            if current.ask_price < self.prev_snapshot.ask_price:
                ofi -= current.ask_qty
            elif current.ask_price == self.prev_snapshot.ask_price:
                ofi -= (current.ask_qty - self.prev_snapshot.ask_qty)
            else:
                ofi += self.prev_snapshot.ask_qty
        
        self.prev_snapshot = current
        self.ofi_history.append(ofi)
        
        return ofi
    
    def get_z_score(self) -> float:
        """
        OFI의 Z-Score 계산 (정규화)
        """
        if len(self.ofi_history) < 2:
            return 0.0
        
        arr = np.array(self.ofi_history)
        mean = np.mean(arr)
        std = np.std(arr)
        
        if std == 0:
            return 0.0
        
        current_ofi = self.ofi_history[-1]
        return (current_ofi - mean) / std
    
    def get_cumulative_ofi(self, window: int = 20) -> float:
        """최근 N틱의 누적 OFI"""
        if not self.ofi_history:
            return 0.0

        recent = list(self.ofi_history)[-window:]
        return sum(recent)

    def warm_up(self, symbol: str) -> int:
        """
        ClickHouse에서 최근 호가 데이터를 로드하여 OFI 히스토리 초기화 (v0.0.2)

        Args:
            symbol: 종목 코드

        Returns:
            로드된 레코드 수
        """
        try:
            client = ClickHouseClient.get_client()

            query = """
                SELECT timestamp, bid_price_1, bid_qty_1, ask_price_1, ask_qty_1
                FROM orderbook_snapshots
                WHERE symbol = %(symbol)s
                ORDER BY timestamp DESC
                LIMIT %(limit)s
            """

            result = client.query(query, parameters={
                'symbol': symbol,
                'limit': self.lookback
            })

            rows = result.result_rows
            if not rows:
                logger.warning(f"Warm-up: No data found for {symbol}")
                return 0

            # 역순으로 정렬 (oldest first)
            rows = list(reversed(rows))

            # 각 행에 대해 OFI 계산
            for row in rows:
                timestamp, bid_p1, bid_q1, ask_p1, ask_q1 = row
                ts = timestamp.timestamp() if hasattr(timestamp, 'timestamp') else float(timestamp)
                self.calculate_tick_ofi(
                    bid_price=float(bid_p1 or 0),
                    bid_qty=float(bid_q1 or 0),
                    ask_price=float(ask_p1 or 0),
                    ask_qty=float(ask_q1 or 0),
                    timestamp=ts
                )

            logger.info(f"Warm-up: Loaded {len(rows)} OFI records for {symbol}")
            return len(rows)

        except Exception as e:
            logger.error(f"Warm-up failed for {symbol}: {e}")
            return 0


class LiquidityCalculator:
    """
    유동성 점수 계산기
    """
    
    @staticmethod
    def calculate_score(
        bid_prices: List[float],
        bid_qtys: List[float],
        ask_prices: List[float],
        ask_qtys: List[float]
    ) -> float:
        """
        유동성 점수 계산 (0-100)
        
        기준:
        - 총 호가 잔량
        - 스프레드 (좁을수록 유동성 높음)
        - 호가 분포 균형
        """
        if not bid_prices or not ask_prices:
            return 0.0
        
        # 스프레드
        spread = ask_prices[0] - bid_prices[0]
        mid_price = (ask_prices[0] + bid_prices[0]) / 2
        spread_pct = (spread / mid_price) * 100 if mid_price > 0 else 100
        
        # 총 잔량
        total_bid_qty = sum(bid_qtys)
        total_ask_qty = sum(ask_qtys)
        total_qty = total_bid_qty + total_ask_qty
        
        # 균형 (0-1, 1이 완벽한 균형)
        balance = 1 - abs(total_bid_qty - total_ask_qty) / (total_qty + 1e-10)
        
        # 점수 계산 (가중 합)
        # 스프레드: 작을수록 좋음 (0.1% 이하 = 만점)
        spread_score = max(0, 100 - spread_pct * 100)
        
        # 잔량: 많을수록 좋음 (정규화 필요 - 종목별로 다름)
        qty_score = min(100, total_qty / 10)  # 임시 기준
        
        # 균형: 균형일수록 좋음
        balance_score = balance * 100
        
        # 가중 평균
        score = (spread_score * 0.4 + qty_score * 0.4 + balance_score * 0.2)
        
        return round(score, 2)


@dataclass
class CandleAggregator:
    """
    틱 데이터를 캔들(1분봉 등)로 집계
    """
    interval_sec: int = 60  # 1분

    # 현재 캔들 상태
    open_price: Optional[float] = None
    high_price: float = 0.0
    low_price: float = float('inf')
    close_price: float = 0.0
    volume: float = 0.0
    tick_count: int = 0
    candle_start: float = 0.0

    def update(self, price: float, volume: float, timestamp: float) -> Optional[Dict]:
        """
        틱으로 캔들 업데이트

        Returns:
            완성된 캔들 (interval 경과 시) 또는 None
        """
        # 첫 틱 또는 새 캔들 시작
        if self.open_price is None:
            self.candle_start = timestamp
            self.open_price = price
            self.high_price = price
            self.low_price = price

        # OHLCV 업데이트
        self.high_price = max(self.high_price, price)
        self.low_price = min(self.low_price, price)
        self.close_price = price
        self.volume += volume
        self.tick_count += 1

        # 인터벌 경과 체크
        if timestamp - self.candle_start >= self.interval_sec:
            candle = {
                'timestamp': self.candle_start,
                'open': self.open_price,
                'high': self.high_price,
                'low': self.low_price,
                'close': self.close_price,
                'volume': self.volume,
                'tick_count': self.tick_count
            }
            self._reset()
            return candle

        return None

    def _reset(self):
        self.open_price = None
        self.high_price = 0.0
        self.low_price = float('inf')
        self.close_price = 0.0
        self.volume = 0.0
        self.tick_count = 0


class TechnicalIndicators:
    """
    기술적 지표 계산기
    학습 모델과 동일한 10개 feature 생성
    """

    def __init__(self, lookback: int = 60):
        self.lookback = lookback
        self.candle_history: Deque[Dict] = deque(maxlen=lookback)
        self.close_prices: Deque[float] = deque(maxlen=lookback)
        self.volumes: Deque[float] = deque(maxlen=lookback)

    def add_candle(self, candle: Dict):
        """새 캔들 추가"""
        self.candle_history.append(candle)
        self.close_prices.append(candle['close'])
        self.volumes.append(candle['volume'])

    def calculate_features(self) -> Optional[Dict]:
        """
        기술적 지표 계산 (학습 모델과 동일한 10개 feature)
        Returns None if insufficient data
        """
        if len(self.close_prices) < 20:
            return None

        closes = np.array(list(self.close_prices))
        volumes = np.array(list(self.volumes))
        candles = list(self.candle_history)

        # 1. returns (수익률)
        returns = (closes[-1] - closes[-2]) / closes[-2] if closes[-2] > 0 else 0.0

        # 2-4. MA ratios (이동평균 비율)
        ma_5 = np.mean(closes[-5:]) if len(closes) >= 5 else closes[-1]
        ma_10 = np.mean(closes[-10:]) if len(closes) >= 10 else closes[-1]
        ma_20 = np.mean(closes[-20:]) if len(closes) >= 20 else closes[-1]

        ma_ratio_5 = closes[-1] / ma_5 if ma_5 > 0 else 1.0
        ma_ratio_10 = closes[-1] / ma_10 if ma_10 > 0 else 1.0
        ma_ratio_20 = closes[-1] / ma_20 if ma_20 > 0 else 1.0

        # 5. RSI
        rsi = self._calculate_rsi(closes)

        # 6. Bollinger Band position
        bb_position = self._calculate_bb_position(closes)

        # 7. Volume ratio
        avg_volume = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
        volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else 1.0

        # 8. Volatility (표준편차)
        if len(closes) >= 20:
            returns_arr = np.diff(closes[-21:]) / closes[-21:-1]
            volatility = np.std(returns_arr) if len(returns_arr) > 0 else 0.0
        else:
            volatility = 0.0

        # 9. HL range (고가-저가 범위)
        if candles:
            latest = candles[-1]
            hl_range = (latest['high'] - latest['low']) / latest['close'] if latest['close'] > 0 else 0.0
        else:
            hl_range = 0.0

        # 10. Candle body (캔들 바디 크기)
        if candles:
            latest = candles[-1]
            candle_body = (latest['close'] - latest['open']) / latest['open'] if latest['open'] > 0 else 0.0
        else:
            candle_body = 0.0

        return {
            'returns': float(returns),
            'ma_ratio_5': float(ma_ratio_5),
            'ma_ratio_10': float(ma_ratio_10),
            'ma_ratio_20': float(ma_ratio_20),
            'rsi': float(rsi),
            'bb_position': float(bb_position),
            'volume_ratio': float(volume_ratio),
            'volatility': float(volatility),
            'hl_range': float(hl_range),
            'candle_body': float(candle_body)
        }

    def _calculate_rsi(self, closes: np.ndarray, period: int = 14) -> float:
        """RSI 계산"""
        if len(closes) < period + 1:
            return 50.0  # 기본값

        deltas = np.diff(closes[-(period + 1):])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def _calculate_bb_position(self, closes: np.ndarray, period: int = 20) -> float:
        """Bollinger Band position 계산 (0-1 사이)"""
        if len(closes) < period:
            return 0.5  # 기본값

        window = closes[-period:]
        middle = np.mean(window)
        std = np.std(window)

        if std == 0:
            return 0.5

        upper = middle + 2 * std
        lower = middle - 2 * std

        position = (closes[-1] - lower) / (upper - lower) if upper != lower else 0.5
        return np.clip(position, 0, 1)


@dataclass
class SymbolState:
    """심볼별 상태 관리"""
    ofi_calc: OFICalculator = field(default_factory=OFICalculator)
    candle_1m: CandleAggregator = field(default_factory=CandleAggregator)
    candle_5m: CandleAggregator = field(default_factory=lambda: CandleAggregator(interval_sec=300))
    tech_indicators: TechnicalIndicators = field(default_factory=TechnicalIndicators)

    # Rolling Window for Prediction Engine (최근 60개 Feature)
    feature_window: Deque = field(default_factory=lambda: deque(maxlen=60))


class FeatureProcessor(StreamConsumer):
    """
    Feature Processor 메인 클래스
    RAW_DATA_STREAM → FEATURE_STREAM
    """

    # 기본 워밍업 심볼 (근월물 코드)
    DEFAULT_WARMUP_SYMBOLS = ["A05601", "A05602"]

    def __init__(self, warmup_symbols: list = None):
        super().__init__(
            stream_name=settings.redis.raw_stream,
            group_name=settings.consumer.processor_group,
            consumer_name="processor_1"
        )
        self.publisher = StreamPublisher(settings.redis.feature_stream)
        self.symbol_states: Dict[str, SymbolState] = {}
        self.redis = RedisClient.get_client()
        self._warmup_symbols = warmup_symbols or self.DEFAULT_WARMUP_SYMBOLS
        self._warmed_up = False

    def _warm_up_symbols(self):
        """
        OFI 계산기 워밍업 (v0.0.2)
        ClickHouse에서 최근 호가 데이터를 로드하여 OFI 히스토리 초기화
        """
        if self._warmed_up:
            return

        logger.info(f"Warming up OFI calculators for symbols: {self._warmup_symbols}")

        for symbol in self._warmup_symbols:
            try:
                state = self._get_state(symbol)
                count = state.ofi_calc.warm_up(symbol)
                if count > 0:
                    logger.info(f"Warm-up complete: {symbol} with {count} records")
            except Exception as e:
                logger.warning(f"Warm-up skipped for {symbol}: {e}")

        self._warmed_up = True
    
    def _get_state(self, symbol: str) -> SymbolState:
        """심볼별 상태 가져오기 (없으면 생성)"""
        if symbol not in self.symbol_states:
            self.symbol_states[symbol] = SymbolState()
        return self.symbol_states[symbol]
    
    def process_message(self, message: StreamMessage) -> bool:
        """
        RAW 데이터 처리 및 Feature 계산
        """
        try:
            data = message.data
            
            # data_json 필드가 있으면 파싱
            if 'data' in data and isinstance(data['data'], dict):
                data = data['data']
            
            symbol = data.get('symbol')
            if not symbol:
                logger.warning(f"Missing symbol in message: {message.id}")
                return True  # ACK하고 스킵
            
            state = self._get_state(symbol)
            timestamp = float(data.get('timestamp', time.time()))
            
            # 1. OFI 계산
            bid_p1 = float(data.get('bid_price_1', 0))
            bid_q1 = float(data.get('bid_qty_1', 0))
            ask_p1 = float(data.get('ask_price_1', 0))
            ask_q1 = float(data.get('ask_qty_1', 0))
            
            ofi_tick = state.ofi_calc.calculate_tick_ofi(
                bid_p1, bid_q1, ask_p1, ask_q1, timestamp
            )
            ofi_z = state.ofi_calc.get_z_score()
            ofi_cumulative = state.ofi_calc.get_cumulative_ofi()
            
            # 2. 유동성 점수 계산
            bid_prices = [float(data.get(f'bid_price_{i}', 0)) for i in range(1, 6) if data.get(f'bid_price_{i}')]
            bid_qtys = [float(data.get(f'bid_qty_{i}', 0)) for i in range(1, 6) if data.get(f'bid_qty_{i}')]
            ask_prices = [float(data.get(f'ask_price_{i}', 0)) for i in range(1, 6) if data.get(f'ask_price_{i}')]
            ask_qtys = [float(data.get(f'ask_qty_{i}', 0)) for i in range(1, 6) if data.get(f'ask_qty_{i}')]
            
            if not bid_prices:
                bid_prices = [bid_p1]
                bid_qtys = [bid_q1]
            if not ask_prices:
                ask_prices = [ask_p1]
                ask_qtys = [ask_q1]
            
            liquidity_score = LiquidityCalculator.calculate_score(
                bid_prices, bid_qtys, ask_prices, ask_qtys
            )
            
            # 3. 캔들 집계
            mid_price = (bid_p1 + ask_p1) / 2
            tick_vol = float(data.get('tick_volume', 0))
            candle_1m = state.candle_1m.update(mid_price, tick_vol, timestamp)
            
            # 4. Feature 구성 (기본 호가 기반)
            feature = {
                'symbol': symbol,
                'timestamp': timestamp,
                'ofi_z_score': round(ofi_z, 4),
                'ofi_cumulative': round(ofi_cumulative, 4),
                'liquidity_score': liquidity_score,
                'mid_price': mid_price,
                'spread': ask_p1 - bid_p1,
                'bid_qty_total': sum(bid_qtys),
                'ask_qty_total': sum(ask_qtys),
            }

            # 상세 로깅: Feature 계산 결과
            trading_logger.log_feature_calculated(
                symbol=symbol,
                timestamp=timestamp,
                ofi_tick=ofi_tick,
                ofi_z_score=ofi_z,
                ofi_cumulative=ofi_cumulative,
                liquidity_score=liquidity_score,
                mid_price=mid_price,
                spread=ask_p1 - bid_p1,
                bid_total=sum(bid_qtys),
                ask_total=sum(ask_qtys),
                candle_completed=candle_1m is not None,
                candle=candle_1m,
                tech_features=None  # 아래에서 업데이트
            )

            # 1분봉 완성 시 기술적 지표 계산 (학습 모델용 10개 feature)
            tech_features = None
            if candle_1m:
                feature['candle_1m'] = candle_1m
                state.tech_indicators.add_candle(candle_1m)
                tech_features = state.tech_indicators.calculate_features()
                if tech_features:
                    feature.update(tech_features)
                    logger.debug(f"Tech features calculated: RSI={tech_features['rsi']:.1f}")

                    # 상세 로깅: 기술적 지표와 함께 다시 로깅
                    trading_logger.log_feature_calculated(
                        symbol=symbol,
                        timestamp=timestamp,
                        ofi_tick=ofi_tick,
                        ofi_z_score=ofi_z,
                        ofi_cumulative=ofi_cumulative,
                        liquidity_score=liquidity_score,
                        mid_price=mid_price,
                        spread=ask_p1 - bid_p1,
                        bid_total=sum(bid_qtys),
                        ask_total=sum(ask_qtys),
                        candle_completed=True,
                        candle=candle_1m,
                        tech_features=tech_features
                    )

                    # 5. Rolling Window 업데이트 (Prediction Engine용)
                    # 기술적 지표가 있는 feature만 저장 (1분봉 단위)
                    state.feature_window.append(feature)
                    self._update_rolling_window(symbol, state.feature_window)

                    # 상세 로깅: Rolling Window 업데이트
                    features_with_tech = sum(1 for f in state.feature_window if 'returns' in f)
                    trading_logger.log_rolling_window_update(
                        symbol=symbol,
                        window_size=len(state.feature_window),
                        features_with_tech=features_with_tech
                    )
            
            # 6. FEATURE_STREAM에 발행
            self.publisher.publish(feature)

            # 메트릭 기록
            metrics = get_metrics()
            metrics.record_feature_calculation("ofi")
            metrics.record_feature_calculation("liquidity")
            if candle_1m:
                metrics.record_feature_calculation("tech_indicators")
            metrics.record_redis_message(
                stream=settings.redis.raw_stream,
                consumer_group=settings.consumer.processor_group
            )

            logger.debug(f"Processed: {symbol} OFI_Z={ofi_z:.2f} LIQ={liquidity_score:.1f}")
            return True

        except Exception as e:
            logger.error(f"Error processing message {message.id}: {e}", exc_info=True)
            return False
    
    def _update_rolling_window(self, symbol: str, window: Deque):
        """
        Redis에 Rolling Window 저장
        Prediction Engine이 빠르게 조회할 수 있도록
        """
        key = f"feature_window:{symbol}"
        try:
            # 최근 60개 Feature를 JSON으로 저장
            self.redis.set(key, json.dumps(list(window)), ex=300)  # 5분 TTL
        except Exception as e:
            logger.error(f"Failed to update rolling window: {e}")


def main():
    """Feature Processor 실행"""
    logger.info("Starting Feature Processor...")

    # 메트릭 서버 시작 (포트 8081)
    init_metrics("feature_processor", port=8081)

    processor = FeatureProcessor()

    # v0.0.2: OFI 워밍업
    processor._warm_up_symbols()

    processor.run()


if __name__ == "__main__":
    main()
