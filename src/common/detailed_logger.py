"""
상세 트레이딩 데이터 로거
거래 시간 동안 수신/평가되는 모든 데이터를 상세히 로깅

환경변수 DETAILED_LOG=true 로 활성화
"""
import os
import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional, List
from dataclasses import asdict, is_dataclass

from config.settings import settings

# 상세 로깅 활성화 여부
DETAILED_LOG_ENABLED = os.getenv("DETAILED_LOG", "false").lower() == "true"

# 상세 로거 설정
detailed_logger = logging.getLogger("detailed_trading")
if DETAILED_LOG_ENABLED:
    detailed_logger.setLevel(logging.DEBUG)
else:
    detailed_logger.setLevel(logging.WARNING)


def _format_price(price: float) -> str:
    """가격 포맷팅 (소수점 2자리)"""
    return f"{price:,.2f}"


def _format_qty(qty: float) -> str:
    """수량 포맷팅"""
    return f"{qty:,.1f}"


def _format_timestamp(ts: float) -> str:
    """타임스탬프를 읽기 쉬운 형식으로 변환"""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


class DetailedTradingLogger:
    """
    트레이딩 시스템의 모든 데이터 흐름을 상세히 로깅하는 클래스

    사용법:
        from src.common.detailed_logger import trading_logger

        # Collector에서
        trading_logger.log_tick_received(tick_data)

        # Processor에서
        trading_logger.log_feature_calculated(symbol, ofi_z, liquidity, ...)

        # Prediction Engine에서
        trading_logger.log_prediction(symbol, sequence, probs)

        # Strategy에서
        trading_logger.log_strategy_decision(symbol, mode, scores, order)
    """

    def __init__(self):
        self.logger = detailed_logger
        self.enabled = DETAILED_LOG_ENABLED
        self._tick_count = 0
        self._feature_count = 0
        self._prediction_count = 0
        self._order_count = 0

    def is_enabled(self) -> bool:
        """상세 로깅 활성화 여부"""
        return self.enabled

    # ============================================================
    # Collector 관련 로깅
    # ============================================================

    def log_tick_received(self, tick_data: Any, tick_type: str = "orderbook"):
        """
        틱 데이터 수신 로깅

        Args:
            tick_data: TickData 객체 또는 dict
            tick_type: "orderbook" 또는 "trade"
        """
        if not self.enabled:
            return

        self._tick_count += 1

        # dataclass를 dict로 변환
        if is_dataclass(tick_data) and not isinstance(tick_data, dict):
            data = asdict(tick_data)
        elif hasattr(tick_data, 'to_dict'):
            data = tick_data.to_dict()
        else:
            data = dict(tick_data) if tick_data else {}

        symbol = data.get('symbol', 'UNKNOWN')
        ts = data.get('timestamp', 0)
        ts_str = _format_timestamp(ts) if ts else "N/A"

        if tick_type == "orderbook":
            bid_p1 = data.get('bid_price_1', 0)
            bid_q1 = data.get('bid_qty_1', 0)
            ask_p1 = data.get('ask_price_1', 0)
            ask_q1 = data.get('ask_qty_1', 0)
            spread = ask_p1 - bid_p1 if bid_p1 and ask_p1 else 0

            # L5 호가 정보
            bid_total = sum(data.get(f'bid_qty_{i}', 0) or 0 for i in range(1, 6))
            ask_total = sum(data.get(f'ask_qty_{i}', 0) or 0 for i in range(1, 6))

            self.logger.debug(
                f"[TICK #{self._tick_count}] {ts_str} {symbol} ORDERBOOK | "
                f"BID: {_format_price(bid_p1)} x {_format_qty(bid_q1)} (total: {_format_qty(bid_total)}) | "
                f"ASK: {_format_price(ask_p1)} x {_format_qty(ask_q1)} (total: {_format_qty(ask_total)}) | "
                f"SPREAD: {spread:.2f}"
            )

            # L5 상세 호가 (5틱마다)
            if self._tick_count % 5 == 0:
                self._log_l5_orderbook(data, ts_str, symbol)

        elif tick_type == "trade":
            tick_vol = data.get('tick_volume', 0)
            oi = data.get('open_interest', 0)
            bid_p1 = data.get('bid_price_1', 0)
            ask_p1 = data.get('ask_price_1', 0)

            self.logger.debug(
                f"[TICK #{self._tick_count}] {ts_str} {symbol} TRADE | "
                f"VOL: {_format_qty(tick_vol)} | "
                f"BID: {_format_price(bid_p1)} | "
                f"ASK: {_format_price(ask_p1)} | "
                f"OI: {_format_qty(oi) if oi else 'N/A'}"
            )

    def _log_l5_orderbook(self, data: Dict, ts_str: str, symbol: str):
        """L5 호가 상세 로깅"""
        bid_lines = []
        ask_lines = []

        for i in range(1, 6):
            bid_p = data.get(f'bid_price_{i}', 0) or 0
            bid_q = data.get(f'bid_qty_{i}', 0) or 0
            ask_p = data.get(f'ask_price_{i}', 0) or 0
            ask_q = data.get(f'ask_qty_{i}', 0) or 0

            if bid_p > 0:
                bid_lines.append(f"  L{i}: {_format_price(bid_p)} x {_format_qty(bid_q)}")
            if ask_p > 0:
                ask_lines.append(f"  L{i}: {_format_price(ask_p)} x {_format_qty(ask_q)}")

        if bid_lines or ask_lines:
            self.logger.debug(f"[L5 ORDERBOOK] {ts_str} {symbol}")
            self.logger.debug("  === BID (매수) ===")
            for line in bid_lines:
                self.logger.debug(line)
            self.logger.debug("  === ASK (매도) ===")
            for line in ask_lines:
                self.logger.debug(line)

    def log_websocket_raw(self, raw_message: str, parsed: bool = True):
        """WebSocket 원본 메시지 로깅"""
        if not self.enabled:
            return

        # 매우 긴 메시지는 잘라서 로깅
        if len(raw_message) > 500:
            msg = raw_message[:500] + "..."
        else:
            msg = raw_message

        status = "PARSED" if parsed else "FAILED"
        self.logger.debug(f"[WS RAW] [{status}] {msg}")

    # ============================================================
    # Processor 관련 로깅
    # ============================================================

    def log_feature_calculated(
        self,
        symbol: str,
        timestamp: float,
        ofi_tick: float,
        ofi_z_score: float,
        ofi_cumulative: float,
        liquidity_score: float,
        mid_price: float,
        spread: float,
        bid_total: float,
        ask_total: float,
        candle_completed: bool = False,
        candle: Optional[Dict] = None,
        tech_features: Optional[Dict] = None
    ):
        """
        Feature 계산 결과 로깅
        """
        if not self.enabled:
            return

        self._feature_count += 1
        ts_str = _format_timestamp(timestamp)

        # 기본 Feature 로깅
        self.logger.debug(
            f"[FEATURE #{self._feature_count}] {ts_str} {symbol} | "
            f"OFI_tick: {ofi_tick:+.2f} | "
            f"OFI_Z: {ofi_z_score:+.3f} | "
            f"OFI_cum: {ofi_cumulative:+.2f} | "
            f"LIQ: {liquidity_score:.1f} | "
            f"MID: {_format_price(mid_price)} | "
            f"SPREAD: {spread:.2f} | "
            f"BID_Q: {_format_qty(bid_total)} | "
            f"ASK_Q: {_format_qty(ask_total)}"
        )

        # OFI 강도 알림
        if abs(ofi_z_score) >= 2.0:
            direction = "BUY PRESSURE" if ofi_z_score > 0 else "SELL PRESSURE"
            self.logger.info(
                f"[OFI ALERT] {ts_str} {symbol} | "
                f"{direction} | Z-Score: {ofi_z_score:+.3f}"
            )

        # 1분봉 완성 시 상세 로깅
        if candle_completed and candle:
            self.logger.info(
                f"[CANDLE 1M] {ts_str} {symbol} | "
                f"O: {_format_price(candle.get('open', 0))} | "
                f"H: {_format_price(candle.get('high', 0))} | "
                f"L: {_format_price(candle.get('low', 0))} | "
                f"C: {_format_price(candle.get('close', 0))} | "
                f"V: {_format_qty(candle.get('volume', 0))} | "
                f"TICKS: {candle.get('tick_count', 0)}"
            )

            # 기술적 지표 로깅
            if tech_features:
                self.logger.info(
                    f"[TECH INDICATORS] {ts_str} {symbol} | "
                    f"Returns: {tech_features.get('returns', 0)*100:+.4f}% | "
                    f"RSI: {tech_features.get('rsi', 50):.1f} | "
                    f"BB_Pos: {tech_features.get('bb_position', 0.5):.3f} | "
                    f"MA5: {tech_features.get('ma_ratio_5', 1):.4f} | "
                    f"MA10: {tech_features.get('ma_ratio_10', 1):.4f} | "
                    f"MA20: {tech_features.get('ma_ratio_20', 1):.4f} | "
                    f"VolRatio: {tech_features.get('volume_ratio', 1):.2f} | "
                    f"Volatility: {tech_features.get('volatility', 0):.6f}"
                )

    def log_rolling_window_update(self, symbol: str, window_size: int, features_with_tech: int):
        """Rolling Window 업데이트 로깅"""
        if not self.enabled:
            return

        self.logger.debug(
            f"[ROLLING WINDOW] {symbol} | "
            f"Size: {window_size}/60 | "
            f"With Tech Indicators: {features_with_tech}"
        )

    # ============================================================
    # Prediction Engine 관련 로깅
    # ============================================================

    def log_prediction_input(
        self,
        symbol: str,
        sequence_shape: tuple,
        feature_summary: Dict[str, float]
    ):
        """
        예측 모델 입력 데이터 로깅

        Args:
            symbol: 종목 코드
            sequence_shape: 입력 시퀀스 형태 (seq_len, features)
            feature_summary: 마지막 타임스텝의 feature 요약
        """
        if not self.enabled:
            return

        self.logger.debug(
            f"[PRED INPUT] {symbol} | "
            f"Shape: {sequence_shape} | "
            f"Last Features: "
            f"ret={feature_summary.get('returns', 0)*100:+.4f}% "
            f"rsi={feature_summary.get('rsi', 50):.1f} "
            f"bb={feature_summary.get('bb_position', 0.5):.3f} "
            f"vol={feature_summary.get('volume_ratio', 1):.2f}"
        )

    def log_prediction_output(
        self,
        symbol: str,
        timestamp: float,
        up_prob: float,
        down_prob: float,
        hold_prob: float,
        inference_time_ms: float,
        model_version: str
    ):
        """
        예측 결과 로깅
        """
        if not self.enabled:
            return

        self._prediction_count += 1
        ts_str = _format_timestamp(timestamp)

        # 방향 결정
        max_prob = max(up_prob, down_prob, hold_prob)
        if max_prob == up_prob:
            direction = "UP"
            prob = up_prob
        elif max_prob == down_prob:
            direction = "DOWN"
            prob = down_prob
        else:
            direction = "HOLD"
            prob = hold_prob

        self.logger.debug(
            f"[PREDICTION #{self._prediction_count}] {ts_str} {symbol} | "
            f"UP: {up_prob:.3f} | DOWN: {down_prob:.3f} | HOLD: {hold_prob:.3f} | "
            f"=> {direction} ({prob:.3f}) | "
            f"Time: {inference_time_ms:.2f}ms | "
            f"Model: {model_version}"
        )

        # 강한 시그널 알림
        if up_prob > 0.7 or down_prob > 0.7:
            direction_str = "STRONG UP" if up_prob > 0.7 else "STRONG DOWN"
            prob_val = up_prob if up_prob > 0.7 else down_prob
            self.logger.info(
                f"[PREDICTION ALERT] {ts_str} {symbol} | "
                f"{direction_str} SIGNAL | Probability: {prob_val:.3f}"
            )

    # ============================================================
    # Strategy 관련 로깅
    # ============================================================

    def log_strategy_evaluation(
        self,
        symbol: str,
        timestamp: float,
        up_prob: float,
        down_prob: float,
        liquidity_score: float,
        basis_gap: float,
        ofi_z_score: float,
        mode: str,
        feature_data: Optional[Dict] = None
    ):
        """
        전략 평가 상세 로깅
        """
        if not self.enabled:
            return

        ts_str = _format_timestamp(timestamp)

        self.logger.debug(
            f"[STRATEGY EVAL] {ts_str} {symbol} | "
            f"Mode: {mode} | "
            f"UP: {up_prob:.3f} | DOWN: {down_prob:.3f} | "
            f"LIQ: {liquidity_score:.1f} | "
            f"BASIS: {basis_gap:.2f}σ | "
            f"OFI_Z: {ofi_z_score:+.3f}"
        )

        # Mode 전환 알림
        if mode == "AVOID":
            self.logger.info(
                f"[MODE AVOID] {ts_str} {symbol} | "
                f"Low Liquidity: {liquidity_score:.1f} < threshold"
            )

    def log_order_decision(
        self,
        symbol: str,
        timestamp: float,
        side: str,
        order_type: str,
        size: float,
        price: Optional[float],
        strategy_id: str,
        mode: str,
        reason: str,
        scores: Optional[Dict] = None
    ):
        """
        주문 결정 로깅
        """
        if not self.enabled:
            return

        self._order_count += 1
        ts_str = _format_timestamp(timestamp)

        price_str = _format_price(price) if price else "MARKET"

        self.logger.info(
            f"[ORDER #{self._order_count}] {ts_str} {symbol} | "
            f"{side} {size} @ {price_str} | "
            f"Type: {order_type} | Mode: {mode} | "
            f"Strategy: {strategy_id} | "
            f"Reason: {reason}"
        )

        if scores:
            self.logger.debug(
                f"[ORDER SCORES] {ts_str} {symbol} | "
                f"OFI: {scores.get('ofi_score', 0):.3f} | "
                f"Imbalance: {scores.get('imbalance_score', 0):.3f} | "
                f"Spread: {scores.get('spread_score', 0):.3f} | "
                f"Total: {scores.get('total_score', 0):.3f}"
            )

    def log_no_order(self, symbol: str, timestamp: float, reason: str):
        """주문 없음 사유 로깅"""
        if not self.enabled:
            return

        ts_str = _format_timestamp(timestamp)
        self.logger.debug(f"[NO ORDER] {ts_str} {symbol} | {reason}")

    # ============================================================
    # 요약 통계
    # ============================================================

    def log_summary(self):
        """현재까지의 요약 통계 로깅"""
        if not self.enabled:
            return

        self.logger.info(
            f"[SUMMARY] Ticks: {self._tick_count} | "
            f"Features: {self._feature_count} | "
            f"Predictions: {self._prediction_count} | "
            f"Orders: {self._order_count}"
        )

    def reset_counters(self):
        """카운터 리셋"""
        self._tick_count = 0
        self._feature_count = 0
        self._prediction_count = 0
        self._order_count = 0


# 싱글톤 인스턴스
trading_logger = DetailedTradingLogger()


def enable_detailed_logging():
    """상세 로깅 활성화 (런타임)"""
    global DETAILED_LOG_ENABLED
    DETAILED_LOG_ENABLED = True
    trading_logger.enabled = True
    detailed_logger.setLevel(logging.DEBUG)
    trading_logger.logger.info("[DETAILED LOG] Enabled")


def disable_detailed_logging():
    """상세 로깅 비활성화 (런타임)"""
    global DETAILED_LOG_ENABLED
    DETAILED_LOG_ENABLED = False
    trading_logger.enabled = False
    detailed_logger.setLevel(logging.WARNING)
