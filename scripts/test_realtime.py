"""
실시간 데이터 수신 테스트 스크립트
한투 WebSocket을 통해 KOSPI Mini 호가/체결 데이터 수신
"""
import os
import sys
import time
import json
import signal
from datetime import datetime
from pathlib import Path

# 프로젝트 경로 추가
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

import redis
from config.settings import settings
from src.common import setup_logging, StreamPublisher
from src.collector.kis_websocket import KISWebSocketAdapter, KISConfig, KISMarket
from src.collector.data_collector import TickData

logger = setup_logging("test_realtime")


class RealtimeTest:
    """실시간 데이터 수신 테스트"""

    def __init__(self):
        self.redis = redis.Redis(
            host=settings.redis.host,
            port=settings.redis.port,
            decode_responses=True
        )
        self.publisher = StreamPublisher(settings.redis.raw_stream)
        self.tick_count = 0
        self.start_time = None
        self.running = False

    def on_tick(self, tick: TickData):
        """틱 데이터 수신 콜백"""
        self.tick_count += 1

        # Redis Stream에 발행
        data = {
            'symbol': tick.symbol,
            'timestamp': str(tick.timestamp),
            'bid_price_1': str(tick.bid_price_1),
            'bid_qty_1': str(tick.bid_qty_1),
            'ask_price_1': str(tick.ask_price_1),
            'ask_qty_1': str(tick.ask_qty_1),
        }

        # 추가 호가 데이터
        for i in range(2, 6):
            bid_p = getattr(tick, f'bid_price_{i}', None)
            bid_q = getattr(tick, f'bid_qty_{i}', None)
            ask_p = getattr(tick, f'ask_price_{i}', None)
            ask_q = getattr(tick, f'ask_qty_{i}', None)

            if bid_p is not None:
                data[f'bid_price_{i}'] = str(bid_p)
            if bid_q is not None:
                data[f'bid_qty_{i}'] = str(bid_q)
            if ask_p is not None:
                data[f'ask_price_{i}'] = str(ask_p)
            if ask_q is not None:
                data[f'ask_qty_{i}'] = str(ask_q)

        if tick.tick_volume:
            data['tick_volume'] = str(tick.tick_volume)

        self.publisher.publish(data)

        # 로그 출력 (10개마다)
        if self.tick_count % 10 == 0:
            elapsed = time.time() - self.start_time
            rate = self.tick_count / elapsed if elapsed > 0 else 0
            mid = (tick.bid_price_1 + tick.ask_price_1) / 2
            spread = tick.ask_price_1 - tick.bid_price_1

            logger.info(
                f"Ticks: {self.tick_count} | "
                f"Rate: {rate:.1f}/s | "
                f"{tick.symbol} Mid={mid:.2f} Spread={spread:.2f}"
            )

    def run(self, symbols: list, duration: int = 60):
        """
        실시간 테스트 실행

        Args:
            symbols: 종목코드 리스트
            duration: 테스트 지속 시간 (초)
        """
        print("\n" + "=" * 60)
        print("  KOSPI Mini 실시간 데이터 수신 테스트")
        print("=" * 60)

        # API 키 확인
        app_key = os.getenv("KIS_APP_KEY", "")
        app_secret = os.getenv("KIS_APP_SECRET", "")

        if not app_key or not app_secret:
            print("\n[ERROR] KIS_APP_KEY, KIS_APP_SECRET 환경변수를 설정하세요.")
            print("  .env 파일에 다음을 추가:")
            print("    KIS_APP_KEY=your_app_key")
            print("    KIS_APP_SECRET=your_app_secret")
            return False

        print(f"\n[1] 설정 확인")
        print(f"  - APP_KEY: {app_key[:8]}...")
        print(f"  - 종목: {symbols}")
        print(f"  - 테스트 시간: {duration}초")

        # WebSocket 어댑터 생성
        market = KISMarket.MOCK if os.getenv("KIS_MARKET") == "mock" else KISMarket.REAL
        config = KISConfig(
            app_key=app_key,
            app_secret=app_secret,
            market=market
        )
        adapter = KISWebSocketAdapter(config)

        print(f"\n[2] WebSocket 연결 중...")
        print(f"  - URL: {config.ws_url}")
        print(f"  - 시장: {market.value}")

        try:
            # approval_key 발급
            adapter.connect()
            print("  - approval_key 발급 완료")

            self.start_time = time.time()
            self.running = True

            # 타임아웃 설정
            def timeout_handler(signum, frame):
                print(f"\n\n[테스트 완료] {duration}초 경과")
                adapter.disconnect()
                self.running = False

            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(duration)

            print(f"\n[3] 데이터 수신 시작...")
            print(f"  (Ctrl+C로 중단 가능)\n")

            # 구독 시작 (블로킹)
            adapter.subscribe(symbols, self.on_tick)

        except KeyboardInterrupt:
            print("\n\n[중단됨] 사용자에 의해 중단")
            adapter.disconnect()

        except Exception as e:
            logger.error(f"테스트 실패: {e}", exc_info=True)
            return False

        finally:
            signal.alarm(0)  # 타이머 해제

        # 결과 출력
        print("\n" + "-" * 40)
        print("  테스트 결과")
        print("-" * 40)
        elapsed = time.time() - self.start_time if self.start_time else 0
        print(f"  - 수신 틱: {self.tick_count}건")
        print(f"  - 경과 시간: {elapsed:.1f}초")
        print(f"  - 평균 수신율: {self.tick_count / elapsed:.1f}건/초" if elapsed > 0 else "")

        # Redis Stream 확인
        stream_len = self.redis.xlen(settings.redis.raw_stream)
        print(f"  - RAW_DATA_STREAM: {stream_len}건")
        print("-" * 40 + "\n")

        return self.tick_count > 0


def get_current_futures_code() -> str:
    """현재 근월물 종목코드 반환"""
    now = datetime.now()
    year = now.year % 100  # 2025 -> 25

    # 월 코드
    month_codes = {
        1: 'F', 2: 'G', 3: 'H', 4: 'J', 5: 'K', 6: 'M',
        7: 'N', 8: 'Q', 9: 'U', 10: 'V', 11: 'X', 12: 'Z'
    }

    # 현재 월의 둘째 주 목요일(만기일) 이전이면 현재 월물, 이후면 다음 월물
    month = now.month
    # 간단하게 현재 월물 사용 (실제로는 만기일 계산 필요)

    month_code = month_codes[month]
    return f"101{month_code}{year}"


def main():
    import argparse

    parser = argparse.ArgumentParser(description="실시간 데이터 수신 테스트")
    parser.add_argument("--symbol", type=str, help="종목코드 (예: 101Z25)")
    parser.add_argument("--duration", type=int, default=30, help="테스트 시간 (초)")

    args = parser.parse_args()

    # 종목코드 결정
    if args.symbol:
        symbols = [args.symbol]
    else:
        symbols = [get_current_futures_code()]
        print(f"근월물 자동 선택: {symbols[0]}")

    test = RealtimeTest()
    success = test.run(symbols=symbols, duration=args.duration)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
