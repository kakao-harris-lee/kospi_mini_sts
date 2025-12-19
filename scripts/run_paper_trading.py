#!/usr/bin/env python3
"""
실시간 Paper Trading 실행 스크립트

tick_collector와 feature_processor가 실행 중인지 확인하고,
필요시 시작한 후 Paper Trading을 실행합니다.

사용법:
    python run_paper_trading.py                          # 기본: pure_micro 전략, 1시간
    python run_paper_trading.py --strategy hybrid --duration 30m
    python run_paper_trading.py --simulation             # 시뮬레이션 모드
"""
import os
import sys
import time
import signal
import asyncio
import argparse
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, List

# 프로젝트 루트 추가
PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))


class Colors:
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    NC = '\033[0m'


def log_info(msg: str):
    print(f"{Colors.GREEN}[INFO]{Colors.NC} {datetime.now().strftime('%H:%M:%S')} {msg}")


def log_warn(msg: str):
    print(f"{Colors.YELLOW}[WARN]{Colors.NC} {datetime.now().strftime('%H:%M:%S')} {msg}")


def log_error(msg: str):
    print(f"{Colors.RED}[ERROR]{Colors.NC} {datetime.now().strftime('%H:%M:%S')} {msg}")


def log_step(msg: str):
    print(f"{Colors.BLUE}[STEP]{Colors.NC} {datetime.now().strftime('%H:%M:%S')} {msg}")


class ProcessManager:
    """백그라운드 프로세스 관리"""

    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.log_dir = project_dir / "logs"
        self.pid_dir = project_dir / "pids"
        self.processes: List[subprocess.Popen] = []

        # 디렉토리 생성
        self.log_dir.mkdir(exist_ok=True)
        self.pid_dir.mkdir(exist_ok=True)

    def is_running(self, name: str) -> tuple[bool, Optional[int]]:
        """프로세스 실행 중인지 확인"""
        pid_file = self.pid_dir / f"{name}.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                # 프로세스 존재 확인
                os.kill(pid, 0)
                return True, pid
            except (ValueError, ProcessLookupError, PermissionError):
                pid_file.unlink(missing_ok=True)
        return False, None

    def start_process(self, name: str, module: str) -> bool:
        """백그라운드 프로세스 시작"""
        running, pid = self.is_running(name)
        if running:
            log_info(f"{name} 이미 실행 중 (PID: {pid})")
            return True

        log_step(f"{name} 시작...")

        log_file = self.log_dir / f"{name}.log"
        pid_file = self.pid_dir / f"{name}.pid"

        with open(log_file, "w") as f:
            proc = subprocess.Popen(
                [sys.executable, "-m", module],
                cwd=str(self.project_dir),
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self.processes.append(proc)
            pid_file.write_text(str(proc.pid))

        # 시작 확인
        time.sleep(3)
        if proc.poll() is None:
            log_info(f"{name} 시작됨 (PID: {proc.pid})")
            return True
        else:
            log_error(f"{name} 시작 실패")
            # 로그 출력
            if log_file.exists():
                print(log_file.read_text()[-2000:])
            return False

    def stop_all(self):
        """모든 백그라운드 프로세스 종료"""
        log_step("백그라운드 프로세스 정리...")

        for name in ["tick_collector", "feature_processor"]:
            pid_file = self.pid_dir / f"{name}.pid"
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text().strip())
                    os.kill(pid, signal.SIGTERM)
                    log_info(f"{name} 종료됨 (PID: {pid})")
                except (ValueError, ProcessLookupError):
                    pass
                pid_file.unlink(missing_ok=True)

    def show_status(self):
        """프로세스 상태 출력"""
        print("\n========================================")
        print(" 프로세스 상태")
        print("========================================")

        for name in ["tick_collector", "feature_processor"]:
            running, pid = self.is_running(name)
            if running:
                print(f" {name:20s} {Colors.GREEN}실행 중{Colors.NC} (PID: {pid})")
            else:
                print(f" {name:20s} {Colors.RED}중지됨{Colors.NC}")

        print("========================================\n")


async def check_redis() -> bool:
    """Redis 연결 확인"""
    log_step("Redis 연결 확인...")

    try:
        import redis.asyncio as aioredis
        from config.settings import settings

        password = settings.redis.password
        client = aioredis.Redis(
            host=settings.redis.host,
            port=settings.redis.port,
            password=password,
            decode_responses=True,
        )
        await client.ping()
        await client.close()
        log_info("Redis 연결 성공")
        return True
    except Exception as e:
        log_error(f"Redis 연결 실패: {e}")
        return False


async def run_paper_trading(
    strategy: str,
    duration: str,
    capital: float,
    simulation: bool,
):
    """Paper Trading 실행"""
    from src.paper_trading import PaperTradingEngine, VirtualBroker
    from src.strategy import (
        MeanReversionStrategy,
        BreakoutStrategy,
        OFIMomentumStrategy,
        HybridStrategy,
        PureMicrostructureStrategy,
        AdaptiveMicrostructureStrategy,
    )

    strategy_classes = {
        "mean_reversion": MeanReversionStrategy,
        "breakout": BreakoutStrategy,
        "ofi_momentum": OFIMomentumStrategy,
        "hybrid": HybridStrategy,
        "pure_micro": PureMicrostructureStrategy,
        "adaptive_micro": AdaptiveMicrostructureStrategy,
    }

    if strategy not in strategy_classes:
        log_error(f"알 수 없는 전략: {strategy}")
        log_info(f"사용 가능: {', '.join(strategy_classes.keys())}")
        return

    # Duration 파싱
    duration_seconds = parse_duration(duration)

    print("\n========================================")
    print(" Paper Trading 설정")
    print("========================================")
    print(f" 전략: {strategy}")
    print(f" 시간: {duration} ({duration_seconds}초)")
    print(f" 자본: {capital:,.0f} KRW")
    print(f" 모드: {'시뮬레이션' if simulation else '실시간'}")
    print("========================================\n")

    # 전략 및 브로커 생성
    strategy_instance = strategy_classes[strategy]()
    broker = VirtualBroker(initial_capital=capital)

    # Redis 클라이언트
    redis_client = None
    if not simulation:
        try:
            import redis.asyncio as aioredis
            from config.settings import settings

            redis_client = aioredis.Redis(
                host=settings.redis.host,
                port=settings.redis.port,
                password=settings.redis.password,
                decode_responses=True,
            )
            await redis_client.ping()
            log_info("Redis 연결됨")
        except Exception as e:
            log_warn(f"Redis 연결 실패: {e}")
            log_warn("시뮬레이션 모드로 전환")
            redis_client = None

    # 엔진 생성 및 실행
    engine = PaperTradingEngine(
        strategy=strategy_instance,
        broker=broker,
        redis_client=redis_client,
    )

    log_step("Paper Trading 시작...")
    try:
        await engine.start(duration_seconds=duration_seconds)
    except KeyboardInterrupt:
        log_warn("사용자에 의해 중단됨")
    finally:
        if redis_client:
            await redis_client.close()

    engine.print_summary()
    return engine.get_result()


def parse_duration(duration_str: str) -> int:
    """Duration 문자열을 초로 변환"""
    duration_str = duration_str.strip().lower()

    if duration_str.endswith('h'):
        return int(duration_str[:-1]) * 3600
    elif duration_str.endswith('m'):
        return int(duration_str[:-1]) * 60
    elif duration_str.endswith('s'):
        return int(duration_str[:-1])
    else:
        return int(duration_str)


def main():
    parser = argparse.ArgumentParser(
        description="실시간 Paper Trading 실행",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python run_paper_trading.py                          # 기본 설정
  python run_paper_trading.py -s pure_micro -d 2h     # pure_micro 전략, 2시간
  python run_paper_trading.py --simulation -d 10m     # 시뮬레이션 모드, 10분
        """
    )

    parser.add_argument(
        "-s", "--strategy",
        default="pure_micro",
        help="전략 이름 (기본: pure_micro)"
    )
    parser.add_argument(
        "-d", "--duration",
        default="1h",
        help="실행 시간 (예: 30m, 1h, 2h)"
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
        "--no-cleanup",
        action="store_true",
        help="종료 시 백그라운드 프로세스 유지"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="프로세스 상태만 확인"
    )

    args = parser.parse_args()

    print("\n========================================")
    print(" 실시간 Paper Trading")
    print("========================================\n")

    pm = ProcessManager(PROJECT_DIR)

    # 상태만 확인
    if args.status:
        pm.show_status()
        return

    # 시뮬레이션 모드가 아니면 프로세스 시작
    if not args.simulation:
        # Redis 확인
        if not asyncio.run(check_redis()):
            log_error("Redis에 연결할 수 없습니다")
            log_info("시뮬레이션 모드로 실행하려면 --simulation 옵션을 사용하세요")
            sys.exit(1)

        # 프로세스 시작
        if not pm.start_process("tick_collector", "src.collector.tick_collector"):
            sys.exit(1)

        if not pm.start_process("feature_processor", "src.processor.feature_processor"):
            sys.exit(1)

        # 데이터 수집 대기
        log_info("데이터 수집 시작 대기 (5초)...")
        time.sleep(5)

        pm.show_status()

    # 종료 시 정리
    if not args.no_cleanup and not args.simulation:
        def cleanup_handler(signum, frame):
            print()
            pm.stop_all()
            sys.exit(0)

        signal.signal(signal.SIGINT, cleanup_handler)
        signal.signal(signal.SIGTERM, cleanup_handler)

    try:
        # Paper Trading 실행
        asyncio.run(run_paper_trading(
            strategy=args.strategy,
            duration=args.duration,
            capital=args.capital,
            simulation=args.simulation,
        ))
    finally:
        if not args.no_cleanup and not args.simulation:
            pm.stop_all()


if __name__ == "__main__":
    main()
