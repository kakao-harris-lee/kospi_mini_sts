"""
로깅 설정
"""
import logging
import sys
from datetime import datetime

import sys as _sys
_sys.path.insert(0, '/Users/harris/Development/trading-system')
from config.settings import settings


def setup_logging(module_name: str = None) -> logging.Logger:
    """
    표준화된 로깅 설정
    
    Args:
        module_name: 모듈 이름 (없으면 root logger)
    
    Returns:
        설정된 Logger 인스턴스
    """
    log_format = (
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    
    # 루트 로거 설정
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    # Redis, ClickHouse 등 외부 라이브러리 로그 레벨 조정
    logging.getLogger("redis").setLevel(logging.WARNING)
    logging.getLogger("clickhouse_connect").setLevel(logging.WARNING)
    
    logger = logging.getLogger(module_name) if module_name else logging.getLogger()
    return logger


class MetricsLogger:
    """
    성능 메트릭 로깅
    처리량, 지연시간 등 추적
    """
    
    def __init__(self, name: str):
        self.name = name
        self.logger = logging.getLogger(f"metrics.{name}")
        self._counts = {}
        self._timings = {}
    
    def increment(self, metric: str, value: int = 1):
        """카운터 증가"""
        self._counts[metric] = self._counts.get(metric, 0) + value
    
    def timing(self, metric: str, duration_ms: float):
        """타이밍 기록"""
        if metric not in self._timings:
            self._timings[metric] = []
        self._timings[metric].append(duration_ms)
    
    def report(self):
        """메트릭 리포트 출력"""
        self.logger.info(f"=== {self.name} Metrics ===")
        for metric, count in self._counts.items():
            self.logger.info(f"  {metric}: {count}")
        for metric, timings in self._timings.items():
            if timings:
                avg = sum(timings) / len(timings)
                self.logger.info(f"  {metric}_avg_ms: {avg:.2f}")
        self._counts.clear()
        self._timings.clear()
