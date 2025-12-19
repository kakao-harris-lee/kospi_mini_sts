#!/bin/bash
# =============================================================================
# KOSPI Trading System - 배포 스크립트
# systemd 기반 프로세스 관리
# =============================================================================

set -e

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 설정
DEPLOY_HOST="${DEPLOY_HOST:-deploy@49.247.171.64}"
DEPLOY_PATH="${DEPLOY_PATH:-/home/deploy/trading-system}"
LOCAL_PATH="$(cd "$(dirname "$0")/.." && pwd)"

# 서비스 목록
SERVICES=(
    "trading-tick-collector"
    "trading-feature-processor"
    "trading-strategy-manager"
    "trading-db-logger"
)

# =============================================================================
# 헬퍼 함수
# =============================================================================

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

show_help() {
    cat << EOF
사용법: $0 [명령] [옵션]

명령:
    deploy          전체 배포 (코드 동기화 + 서비스 재시작)
    sync            코드만 동기화 (서비스 재시작 없음)
    start           모든 서비스 시작
    stop            모든 서비스 중지
    restart         모든 서비스 재시작
    status          서비스 상태 확인
    logs [service]  로그 확인 (서비스 지정 가능)
    setup           초기 설정 (venv, systemd 등록)
    install-deps    의존성 설치

옵션:
    --dry-run       실제 실행 없이 명령만 출력
    -h, --help      도움말 출력

예시:
    $0 deploy                   # 전체 배포
    $0 status                   # 상태 확인
    $0 logs trading-tick-collector  # 특정 서비스 로그
    $0 restart                  # 서비스 재시작
EOF
}

# =============================================================================
# 원격 실행 함수
# =============================================================================

remote_exec() {
    ssh "$DEPLOY_HOST" "$@"
}

# =============================================================================
# 배포 함수
# =============================================================================

sync_code() {
    log_info "코드 동기화 중..."

    rsync -avz --delete \
        --exclude '.git' \
        --exclude '__pycache__' \
        --exclude '*.pyc' \
        --exclude '.pytest_cache' \
        --exclude 'venv' \
        --exclude '.env' \
        --exclude 'logs' \
        --exclude '*.egg-info' \
        --exclude '.mypy_cache' \
        --exclude '.ruff_cache' \
        "$LOCAL_PATH/" "$DEPLOY_HOST:$DEPLOY_PATH/"

    log_info "코드 동기화 완료"
}

create_env_file() {
    log_info ".env 파일 생성 중..."

    # .env 파일이 없으면 생성
    remote_exec "cat > $DEPLOY_PATH/.env << 'EOF'
# =============================================================================
# KOSPI Trading System - 환경 설정
# =============================================================================

# 한국투자증권 API
KIS_APP_KEY=${KIS_APP_KEY:-PSGnWoWbUwphmwiyt1UBAZVHAfnB0sdB9LT5}
KIS_APP_SECRET=${KIS_APP_SECRET:-LTEpng3OhKWHW4TapFaxG81DpD9uNXNEtF5f29Uii4U8IrlT2YC7I5ZmKhh2XCFwa5WOPLwx5W0fgoU0ldhWWavYuXYqMi+bQ3Ssj2SBG2q4kQZTF4OtsAFzkgkjYY3L8P/BpeSgr+VH0gFfnJDTpA7+8RsFQXXeT6fGxGZ7dc1wYHJrrSM=}
KIS_ACCOUNT_NO=${KIS_ACCOUNT_NO:-}
KIS_MARKET=real

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=@1tidh6ls6ls

# ClickHouse
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8123
CLICKHOUSE_DATABASE=kospi
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=@1tidh6ls6ls

# 전략 설정
DRY_RUN=true
STRATEGY=pure_micro

# 로깅
LOG_LEVEL=INFO
EOF"

    log_info ".env 파일 생성 완료"
}

setup_venv() {
    log_info "Python 가상환경 설정 중..."

    remote_exec "cd $DEPLOY_PATH && \
        python3 -m venv venv && \
        source venv/bin/activate && \
        pip install --upgrade pip && \
        pip install -e '.[all]'"

    log_info "가상환경 설정 완료"
}

install_systemd_services() {
    log_info "systemd 서비스 등록 중..."

    # systemd 파일 복사
    for service in "${SERVICES[@]}"; do
        remote_exec "sudo cp $DEPLOY_PATH/deploy/systemd/${service}.service /etc/systemd/system/"
    done
    remote_exec "sudo cp $DEPLOY_PATH/deploy/systemd/trading.target /etc/systemd/system/"

    # 데몬 리로드
    remote_exec "sudo systemctl daemon-reload"

    # 서비스 활성화
    for service in "${SERVICES[@]}"; do
        remote_exec "sudo systemctl enable ${service}.service"
    done

    log_info "systemd 서비스 등록 완료"
}

setup_logs_dir() {
    log_info "로그 디렉토리 생성 중..."
    remote_exec "mkdir -p $DEPLOY_PATH/logs"
    log_info "로그 디렉토리 생성 완료"
}

# =============================================================================
# 서비스 제어 함수
# =============================================================================

start_services() {
    log_info "서비스 시작 중..."
    for service in "${SERVICES[@]}"; do
        remote_exec "sudo systemctl start ${service}.service"
        log_info "  ${service} 시작됨"
    done
}

stop_services() {
    log_info "서비스 중지 중..."
    for service in "${SERVICES[@]}"; do
        remote_exec "sudo systemctl stop ${service}.service" || true
        log_info "  ${service} 중지됨"
    done
}

restart_services() {
    log_info "서비스 재시작 중..."
    for service in "${SERVICES[@]}"; do
        remote_exec "sudo systemctl restart ${service}.service"
        log_info "  ${service} 재시작됨"
    done
}

show_status() {
    log_info "서비스 상태:"
    echo ""
    for service in "${SERVICES[@]}"; do
        status=$(remote_exec "systemctl is-active ${service}.service" 2>/dev/null || echo "inactive")
        if [ "$status" = "active" ]; then
            echo -e "  ${GREEN}●${NC} ${service}: ${GREEN}${status}${NC}"
        else
            echo -e "  ${RED}●${NC} ${service}: ${RED}${status}${NC}"
        fi
    done
    echo ""

    # Redis & ClickHouse 상태
    log_info "인프라 상태:"
    redis_status=$(remote_exec "systemctl is-active redis-server.service" 2>/dev/null || echo "inactive")
    ch_status=$(remote_exec "systemctl is-active clickhouse-server.service" 2>/dev/null || echo "inactive")
    echo -e "  Redis: ${redis_status}"
    echo -e "  ClickHouse: ${ch_status}"
}

show_logs() {
    service="${1:-trading-tick-collector}"
    log_info "${service} 로그:"
    remote_exec "sudo journalctl -u ${service}.service -f --no-pager -n 50"
}

# =============================================================================
# 메인 명령어 처리
# =============================================================================

case "${1:-deploy}" in
    deploy)
        log_info "=== 전체 배포 시작 ==="
        sync_code
        create_env_file
        setup_logs_dir

        # venv가 없으면 생성
        if ! remote_exec "test -d $DEPLOY_PATH/venv"; then
            setup_venv
            install_systemd_services
        else
            log_info "의존성 업데이트 중..."
            remote_exec "cd $DEPLOY_PATH && source venv/bin/activate && pip install -e '.[all]' -q"
        fi

        restart_services
        sleep 2
        show_status
        log_info "=== 배포 완료 ==="
        ;;

    sync)
        sync_code
        ;;

    setup)
        log_info "=== 초기 설정 시작 ==="
        sync_code
        create_env_file
        setup_logs_dir
        setup_venv
        install_systemd_services
        log_info "=== 초기 설정 완료 ==="
        log_info "서비스 시작: $0 start"
        ;;

    install-deps)
        setup_venv
        ;;

    start)
        start_services
        sleep 2
        show_status
        ;;

    stop)
        stop_services
        ;;

    restart)
        restart_services
        sleep 2
        show_status
        ;;

    status)
        show_status
        ;;

    logs)
        show_logs "$2"
        ;;

    -h|--help|help)
        show_help
        ;;

    *)
        log_error "알 수 없는 명령: $1"
        show_help
        exit 1
        ;;
esac
