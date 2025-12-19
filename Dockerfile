# KOSPI Mini Trading System Dockerfile
# Phase 8.3: 컨테이너 배포
FROM python:3.10-slim

# 환경 변수
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 시스템 의존성
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 작업 디렉토리
WORKDIR /app

# 의존성 먼저 복사 (캐시 활용)
COPY pyproject.toml setup.py ./
COPY src/__init__.py src/

# 의존성 설치
RUN pip install --upgrade pip && \
    pip install -e ".[all]"

# 소스 코드 복사
COPY . .

# 헬스체크용 포트 (메트릭)
EXPOSE 8080

# 기본 명령 (컬렉터)
CMD ["python", "-m", "src.collector.tick_collector"]
