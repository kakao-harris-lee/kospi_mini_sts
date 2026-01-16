#!/usr/bin/env python3
"""
Paper Trading 서비스

장 시간에 맞춰 자동으로 Paper Trading을 실행하고,
장 시작/종료 시 텔레그램 알림을 보냅니다.

기능:
- 장 시작 (09:00) 알림 및 거래 시작
- 장 종료 (15:45) 알림 및 일일 결과 요약
- 공휴일/주말 자동 감지 및 스킵
- tick_collector, feature_processor 자동 관리
- 시간대별 요약 알림 (점심, 오후)
- 선물 코드 자동 감지

사용법:
    python paper_trading_service.py                    # 데몬 모드
    python paper_trading_service.py --once             # 오늘만 실행
    python paper_trading_service.py --strategy hybrid  # 전략 지정
"""
import os
import sys
import time
import signal
import asyncio
import argparse
import subprocess
import logging
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any
from dataclasses import dataclass
from enum import Enum

# 프로젝트 루트 추가
PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from src.common.telegram import TelegramNotifier
from src.common.futures_code import get_front_month_code, get_kis_code, get_current_futures_info
from src.common.metrics import init_metrics

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


# =============================================================================
# 설정
# =============================================================================

@dataclass
class MarketSchedule:
    """장 시간 설정"""
    open_hour: int = 9
    open_minute: int = 0
    close_hour: int = 15
    close_minute: int = 45

    # 서비스 시작 시간 (장 시작 전)
    service_start_hour: int = 8
    service_start_minute: int = 55

    # 서비스 종료 시간 (장 종료 후)
    service_end_hour: int = 15
    service_end_minute: int = 50

    # 요약 알림 시간 (시:분)
    summary_times: tuple = ((12, 0), (14, 0))  # 점심, 오후 2시


# 한국 공휴일 (2024-2026)
KOREAN_HOLIDAYS = {
    # 2024
    date(2024, 1, 1), date(2024, 2, 9), date(2024, 2, 10), date(2024, 2, 11),
    date(2024, 2, 12), date(2024, 3, 1), date(2024, 4, 10), date(2024, 5, 5),
    date(2024, 5, 6), date(2024, 5, 15), date(2024, 6, 6), date(2024, 8, 15),
    date(2024, 9, 16), date(2024, 9, 17), date(2024, 9, 18), date(2024, 10, 3),
    date(2024, 10, 9), date(2024, 12, 25),
    # 2025
    date(2025, 1, 1), date(2025, 1, 28), date(2025, 1, 29), date(2025, 1, 30),
    date(2025, 3, 1), date(2025, 3, 3), date(2025, 5, 5), date(2025, 5, 6),
    date(2025, 6, 6), date(2025, 8, 15), date(2025, 10, 3), date(2025, 10, 5),
    date(2025, 10, 6), date(2025, 10, 7), date(2025, 10, 8), date(2025, 10, 9),
    date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
    date(2026, 3, 1), date(2026, 3, 2), date(2026, 5, 5), date(2026, 5, 24),
    date(2026, 6, 6), date(2026, 8, 15), date(2026, 9, 24), date(2026, 9, 25),
    date(2026, 9, 26), date(2026, 10, 3), date(2026, 10, 9), date(2026, 12, 25),
}


class ServiceState(Enum):
    IDLE = "idle"                    # 대기 중
    WAITING_MARKET_OPEN = "waiting"  # 장 시작 대기
    TRADING = "trading"              # 거래 중
    STOPPED = "stopped"              # 종료됨


# =============================================================================
# 유틸리티 함수
# =============================================================================

def is_trading_day(d: date = None) -> bool:
    """거래일인지 확인"""
    if d is None:
        d = date.today()

    # 주말 체크
    if d.weekday() >= 5:
        return False

    # 공휴일 체크
    if d in KOREAN_HOLIDAYS:
        return False

    return True


def get_kst_now() -> datetime:
    """현재 한국 시간 반환"""
    import pytz
    kst = pytz.timezone('Asia/Seoul')
    return datetime.now(kst)


def format_number(n: float) -> str:
    """숫자 포맷팅 (천 단위 콤마)"""
    if n >= 0:
        return f"+{n:,.0f}"
    return f"{n:,.0f}"


# =============================================================================
# 프로세스 관리
# =============================================================================

class ProcessManager:
    """백그라운드 프로세스 관리"""

    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.log_dir = project_dir / "logs"
        self.pid_dir = project_dir / "pids"

        self.log_dir.mkdir(exist_ok=True)
        self.pid_dir.mkdir(exist_ok=True)

    def is_running(self, name: str) -> tuple[bool, Optional[int]]:
        """프로세스 실행 중인지 확인"""
        pid_file = self.pid_dir / f"{name}.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)
                return True, pid
            except (ValueError, ProcessLookupError, PermissionError):
                pid_file.unlink(missing_ok=True)
        return False, None

    def start_process(self, name: str, module: str) -> bool:
        """프로세스 시작"""
        running, pid = self.is_running(name)
        if running:
            logger.info(f"{name} 이미 실행 중 (PID: {pid})")
            return True

        logger.info(f"{name} 시작...")

        log_file = self.log_dir / f"{name}.log"
        pid_file = self.pid_dir / f"{name}.pid"

        with open(log_file, "a") as f:
            f.write(f"\n{'='*50}\n")
            f.write(f"Started at {datetime.now()}\n")
            f.write(f"{'='*50}\n")

            proc = subprocess.Popen(
                [sys.executable, "-m", module],
                cwd=str(self.project_dir),
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            pid_file.write_text(str(proc.pid))

        time.sleep(3)
        if self.is_running(name)[0]:
            logger.info(f"{name} 시작됨 (PID: {proc.pid})")
            return True
        else:
            logger.error(f"{name} 시작 실패")
            return False

    def stop_process(self, name: str):
        """프로세스 종료"""
        pid_file = self.pid_dir / f"{name}.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                logger.info(f"{name} 종료됨 (PID: {pid})")
            except (ValueError, ProcessLookupError):
                pass
            pid_file.unlink(missing_ok=True)

    def stop_all(self):
        """모든 프로세스 종료"""
        for name in ["tick_collector", "feature_processor"]:
            self.stop_process(name)


# =============================================================================
# Paper Trading 서비스
# =============================================================================

class PaperTradingService:
    """Paper Trading 서비스"""

    def __init__(
        self,
        strategy: str = "pure_micro",
        capital: float = 10_000_000,
        simulation: bool = False,
        use_kis_api: bool = True,
        enable_summary_alerts: bool = True,
    ):
        self.strategy = strategy
        self.capital = capital
        self.simulation = simulation
        self.use_kis_api = use_kis_api
        self.enable_summary_alerts = enable_summary_alerts

        self.schedule = MarketSchedule()
        self.state = ServiceState.IDLE
        self.telegram = TelegramNotifier()
        self.process_manager = ProcessManager(PROJECT_DIR)

        # 선물 코드 자동 감지
        self.futures_info = get_current_futures_info()
        self.futures_symbol = get_front_month_code()  # KRX 코드 (예: 101H25)

        # 거래 결과
        self.engine = None
        self.today_result: Optional[Dict[str, Any]] = None
        self.running = False
        self.summary_sent: set = set()  # 이미 보낸 요약 알림 시간

    def send_market_open_notification(self):
        """장 시작 알림"""
        now = get_kst_now()
        kis_status = "KIS 모의투자 API" if self.use_kis_api else "가상 브로커"
        front_info = self.futures_info['front_month']
        msg = (
            f"<b>📈 [Paper Trading] 장 시작</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 날짜: {now.strftime('%Y-%m-%d')} ({['월','화','수','목','금'][now.weekday()]})\n"
            f"⏰ 시간: {now.strftime('%H:%M:%S')}\n"
            f"📊 전략: {self.strategy}\n"
            f"💰 자본금: {self.capital:,.0f} KRW\n"
            f"🔗 주문: {kis_status}\n"
            f"📋 종목: {front_info['krx_code']} (D-{front_info['days_to_expiry']})\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"모의투자를 시작합니다."
        )
        self.telegram.send(msg)
        logger.info(f"장 시작 알림 전송 (종목: {front_info['krx_code']})")

    def send_market_close_notification(self, result: Dict[str, Any]):
        """장 종료 알림 (일일 결과 포함)"""
        now = get_kst_now()

        pnl = result.get('total_pnl', 0)
        total_return = result.get('total_return', 0)
        total_trades = result.get('total_trades', 0)
        winning_trades = result.get('winning_trades', 0)
        losing_trades = result.get('losing_trades', 0)
        win_rate = result.get('win_rate', 0)

        pnl_emoji = "🟢" if pnl >= 0 else "🔴"

        msg = (
            f"<b>📉 [Paper Trading] 장 종료</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 날짜: {now.strftime('%Y-%m-%d')}\n"
            f"⏰ 종료: {now.strftime('%H:%M:%S')}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>📊 일일 결과</b>\n"
            f"{pnl_emoji} 손익: {format_number(pnl)} KRW\n"
            f"📈 수익률: {total_return:+.2f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>📋 거래 통계</b>\n"
            f"총 거래: {total_trades}건\n"
            f"승리: {winning_trades}건 / 패배: {losing_trades}건\n"
            f"승률: {win_rate:.1f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"내일 다시 만나요! 🌙"
        )
        self.telegram.send(msg)
        logger.info("장 종료 알림 전송")

    def send_error_notification(self, error: str):
        """에러 알림"""
        msg = (
            f"<b>⚠️ [Paper Trading] 에러</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"{error}"
        )
        self.telegram.send(msg)
        logger.error(f"에러 알림: {error}")

    def send_holiday_notification(self, reason: str):
        """휴장일 알림"""
        now = get_kst_now()
        msg = (
            f"<b>🏖️ [Paper Trading] 휴장</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 날짜: {now.strftime('%Y-%m-%d')}\n"
            f"사유: {reason}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"오늘은 거래가 없습니다."
        )
        self.telegram.send(msg)
        logger.info(f"휴장 알림: {reason}")

    def send_interim_summary(self, period: str = ""):
        """시간대별 요약 알림"""
        if not self.engine or not self.enable_summary_alerts:
            return

        now = get_kst_now()
        result = self.engine.get_result()

        pnl = result.get('total_pnl', 0)
        total_return = result.get('total_return', 0)
        total_trades = result.get('total_trades', 0)
        position = "없음"

        if self.engine.broker.position.is_open:
            pos_side = self.engine.broker.position.side.value
            unrealized = self.engine.broker.position.unrealized_pnl
            position = f"{pos_side} ({format_number(unrealized)})"

        pnl_emoji = "🟢" if pnl >= 0 else "🔴"

        msg = (
            f"<b>📊 [Paper Trading] {period} 현황</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 시간: {now.strftime('%H:%M')}\n"
            f"{pnl_emoji} 손익: {format_number(pnl)} KRW ({total_return:+.2f}%)\n"
            f"📈 거래: {total_trades}건\n"
            f"📋 포지션: {position}\n"
            f"━━━━━━━━━━━━━━━━━━━━━"
        )
        self.telegram.send(msg)
        logger.info(f"{period} 요약 알림 전송")

    def check_and_send_summary(self):
        """요약 알림 시간 확인 및 전송"""
        if not self.enable_summary_alerts:
            return

        now = get_kst_now()
        current_time = (now.hour, now.minute)

        for summary_hour, summary_minute in self.schedule.summary_times:
            summary_key = f"{now.date()}_{summary_hour}_{summary_minute}"

            # 이미 보냈으면 스킵
            if summary_key in self.summary_sent:
                continue

            # 해당 시간이 되었는지 확인 (±2분 범위)
            if now.hour == summary_hour and abs(now.minute - summary_minute) <= 2:
                period = "점심" if summary_hour == 12 else "오후"
                self.send_interim_summary(period)
                self.summary_sent.add(summary_key)
                break

    async def start_trading(self):
        """Paper Trading 시작"""
        logger.info("Paper Trading 시작...")

        # 프로세스 시작 (시뮬레이션 모드가 아닐 때)
        if not self.simulation:
            if not self.process_manager.start_process("tick_collector", "src.collector.tick_collector"):
                self.send_error_notification("tick_collector 시작 실패")
                return

            if not self.process_manager.start_process("feature_processor", "src.processor.feature_processor"):
                self.send_error_notification("feature_processor 시작 실패")
                return

            # 데이터 수집 대기
            await asyncio.sleep(5)

        # 전략 및 엔진 생성
        from src.strategy import (
            MeanReversionStrategy,
            BreakoutStrategy,
            OFIMomentumStrategy,
            HybridStrategy,
            PureMicrostructureStrategy,
            AdaptiveMicrostructureStrategy,
            DualModeStrategy,
        )
        from src.paper_trading import PaperTradingEngine, VirtualBroker
        from src.collector.kis_order import KISOrderAPI, KISOrderConfig, KISOrderExecutor

        strategy_classes = {
            "mean_reversion": MeanReversionStrategy,
            "breakout": BreakoutStrategy,
            "ofi_momentum": OFIMomentumStrategy,
            "hybrid": HybridStrategy,
            "pure_micro": PureMicrostructureStrategy,
            "adaptive_micro": AdaptiveMicrostructureStrategy,
            "dual_mode": DualModeStrategy,  # MODE_A + MODE_B with state machine
        }

        strategy_instance = strategy_classes[self.strategy]()
        broker = VirtualBroker(
            initial_capital=self.capital,
            strategy_name=self.strategy,
            symbol=self.futures_symbol,
        )

        # Initialize Prometheus metrics server on port 8082
        init_metrics("strategy_manager", port=8082)

        # KIS 모의투자 API 설정
        kis_executor = None
        futures_symbol = self.futures_symbol  # 자동 감지된 근월물 코드
        logger.info(f"거래 종목: {futures_symbol} (만기: {self.futures_info['front_month']['expiry']})")

        if self.use_kis_api:
            try:
                from config.settings import settings
                kis_config = KISOrderConfig(
                    app_key=settings.kis.app_key,
                    app_secret=settings.kis.app_secret,
                    account_no=settings.kis.account_no,
                    is_mock=settings.kis.is_mock,
                )

                if kis_config.account_no:
                    kis_api = KISOrderAPI(kis_config)
                    kis_executor = KISOrderExecutor(api=kis_api, dry_run=False)
                    logger.info(f"KIS 모의투자 API 연동됨 (계좌: {kis_config.account_no})")
                else:
                    logger.warning("KIS 계좌번호가 설정되지 않음")
            except Exception as e:
                logger.error(f"KIS API 초기화 실패: {e}")

        # Redis 클라이언트
        redis_client = None
        if not self.simulation:
            try:
                import redis.asyncio as aioredis
                from config.settings import settings

                redis_client = aioredis.Redis(
                    host=settings.redis.host,
                    port=settings.redis.port,
                    db=settings.redis.db,  # Use DB2
                    password=settings.redis.password,
                    decode_responses=True,
                )
                await redis_client.ping()
                logger.info(f"Redis 연결됨 (DB{settings.redis.db})")
            except Exception as e:
                logger.warning(f"Redis 연결 실패: {e}, 시뮬레이션 모드로 전환")
                redis_client = None

        self.engine = PaperTradingEngine(
            strategy=strategy_instance,
            broker=broker,
            redis_client=redis_client,
            kis_executor=kis_executor,
            symbol=futures_symbol,
        )

        self.state = ServiceState.TRADING

        # 장 종료까지 남은 시간 계산
        now = get_kst_now()
        close_time = now.replace(
            hour=self.schedule.close_hour,
            minute=self.schedule.close_minute,
            second=0,
            microsecond=0
        )
        duration_seconds = int((close_time - now).total_seconds())

        if duration_seconds <= 0:
            logger.warning("이미 장이 종료되었습니다")
            return

        logger.info(f"거래 시간: {duration_seconds}초 ({duration_seconds // 3600}시간 {(duration_seconds % 3600) // 60}분)")

        # 요약 알림 체크 태스크
        async def summary_checker():
            """주기적으로 요약 알림 시간 체크"""
            while self.engine and self.engine.is_running:
                self.check_and_send_summary()
                await asyncio.sleep(60)  # 1분마다 체크

        try:
            # 거래 엔진과 요약 체크 태스크 동시 실행
            summary_task = asyncio.create_task(summary_checker())

            await self.engine.start(duration_seconds=duration_seconds)

            summary_task.cancel()
            try:
                await summary_task
            except asyncio.CancelledError:
                pass

        except Exception as e:
            self.send_error_notification(f"거래 중 에러: {e}")
            logger.error(f"거래 중 에러: {e}")
        finally:
            self.today_result = self.engine.get_result() if self.engine else {}

            if redis_client:
                await redis_client.close()

    def stop_trading(self):
        """Paper Trading 종료"""
        logger.info("Paper Trading 종료...")

        if self.engine:
            # 엔진 종료는 자동으로 처리됨
            pass

        # 백그라운드 프로세스 종료
        if not self.simulation:
            self.process_manager.stop_all()

        self.state = ServiceState.IDLE

    async def run_daily_session(self):
        """일일 세션 실행"""
        today = date.today()

        # 거래일 체크
        if not is_trading_day(today):
            reason = "주말" if today.weekday() >= 5 else "공휴일"
            self.send_holiday_notification(reason)
            return

        now = get_kst_now()

        # 장 시작 시간까지 대기
        open_time = now.replace(
            hour=self.schedule.open_hour,
            minute=self.schedule.open_minute,
            second=0,
            microsecond=0
        )

        if now < open_time:
            wait_seconds = (open_time - now).total_seconds()
            logger.info(f"장 시작까지 {wait_seconds:.0f}초 대기...")
            self.state = ServiceState.WAITING_MARKET_OPEN
            await asyncio.sleep(wait_seconds)

        # 장 시작 알림
        self.send_market_open_notification()

        # 거래 시작
        await self.start_trading()

        # 거래 종료
        self.stop_trading()

        # 장 종료 알림
        if self.today_result:
            self.send_market_close_notification(self.today_result)

    async def run_daemon(self):
        """데몬 모드 실행 (매일 반복)"""
        logger.info("Paper Trading 서비스 시작 (데몬 모드)")
        self.running = True

        kis_status = "KIS 모의투자 API" if self.use_kis_api else "가상 브로커"

        # 시작 알림
        self.telegram.send(
            f"<b>🚀 [Paper Trading] 서비스 시작</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"전략: {self.strategy}\n"
            f"자본금: {self.capital:,.0f} KRW\n"
            f"모드: {'시뮬레이션' if self.simulation else '실시간'}\n"
            f"주문: {kis_status}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"장 시간에 자동으로 거래합니다."
        )

        while self.running:
            try:
                await self.run_daily_session()

                # 다음 날 서비스 시작 시간까지 대기
                now = get_kst_now()
                tomorrow = (now + timedelta(days=1)).replace(
                    hour=self.schedule.service_start_hour,
                    minute=self.schedule.service_start_minute,
                    second=0,
                    microsecond=0
                )

                wait_seconds = (tomorrow - now).total_seconds()
                logger.info(f"다음 세션까지 {wait_seconds / 3600:.1f}시간 대기...")

                self.state = ServiceState.IDLE
                await asyncio.sleep(wait_seconds)

            except asyncio.CancelledError:
                logger.info("서비스 종료 요청")
                break
            except Exception as e:
                self.send_error_notification(f"서비스 에러: {e}")
                logger.error(f"서비스 에러: {e}")
                await asyncio.sleep(60)  # 1분 후 재시도

    def stop(self):
        """서비스 종료"""
        self.running = False
        self.stop_trading()

        self.telegram.send(
            f"<b>🛑 [Paper Trading] 서비스 종료</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"서비스가 종료되었습니다."
        )
        logger.info("서비스 종료됨")


# =============================================================================
# 메인
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Paper Trading 서비스",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "-s", "--strategy",
        default="pure_micro",
        help="전략 이름 (기본: pure_micro)"
    )
    parser.add_argument(
        "-c", "--capital",
        type=float,
        default=10_000_000,
        help="초기 자본금 (기본: 10000000)"
    )
    parser.add_argument(
        "--simulation",
        action="store_true",
        help="시뮬레이션 모드 (Redis 없이)"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="오늘만 실행 (데몬 모드 아님)"
    )
    parser.add_argument(
        "--test-notification",
        action="store_true",
        help="텔레그램 알림 테스트"
    )
    parser.add_argument(
        "--no-kis",
        action="store_true",
        help="KIS API 사용 안 함 (가상 브로커만 사용)"
    )
    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="시간대별 요약 알림 비활성화"
    )
    parser.add_argument(
        "--show-futures",
        action="store_true",
        help="현재 선물 정보 출력"
    )

    args = parser.parse_args()

    # 선물 정보 출력
    if args.show_futures:
        from src.common.futures_code import format_futures_info
        print(format_futures_info())
        return

    service = PaperTradingService(
        strategy=args.strategy,
        capital=args.capital,
        simulation=args.simulation,
        use_kis_api=not args.no_kis,
        enable_summary_alerts=not args.no_summary,
    )

    # 텔레그램 테스트
    if args.test_notification:
        logger.info("텔레그램 알림 테스트...")
        service.send_market_open_notification()
        time.sleep(2)
        service.send_market_close_notification({
            'total_pnl': 150000,
            'total_return': 1.5,
            'total_trades': 5,
            'winning_trades': 3,
            'losing_trades': 2,
            'win_rate': 60.0,
        })
        return

    # 시그널 핸들러
    def signal_handler(signum, frame):
        logger.info(f"시그널 수신: {signum}")
        service.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 실행
    try:
        if args.once:
            logger.info("오늘만 실행 모드")
            asyncio.run(service.run_daily_session())
        else:
            asyncio.run(service.run_daemon())
    except KeyboardInterrupt:
        service.stop()


if __name__ == "__main__":
    main()
