"""
CLI 모듈

Typer 기반 명령줄 인터페이스
- sts backtest: 백테스트 실행
- sts paper: 모의투자 실행
- sts report: 리포트 생성
- sts runs: 실행 기록 조회
"""
from .main import app

__all__ = ["app"]
