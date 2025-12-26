#!/bin/bash
# =============================================================================
# KOSPI Trading System - 배포 스크립트
# Crontab 기반 프로세스 관리
# =============================================================================

set -e

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 설정
DEPLOY_HOST="${DEPLOY_HOST:-deploy@chsvr.duckdns.org}"
DEPLOY_PATH="${DEPLOY_PATH:-/home/deploy/project/kospi_mini_sts}"
LOCAL_PATH="$(cd "$(dirname "$0")/.." && pwd)"

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
    deploy          전체 배포 (코드 동기화 + 의존성 업데이트)
    sync            코드만 동기화
    setup           초기 설정 (venv, crontab 등록)
    install-deps    의존성 설치
    status          상태 확인 (cron, 프로세스)
    logs            최근 로그 확인

옵션:
    -h, --help      도움말 출력

예시:
    $0 deploy       # 전체 배포
    $0 status       # 상태 확인
    $0 logs         # 로그 확인
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
        --exclude 'pids' \
        --exclude '*.egg-info' \
        --exclude '.mypy_cache' \
        --exclude '.ruff_cache' \
        --exclude '.claude' \
        "$LOCAL_PATH/" "$DEPLOY_HOST:$DEPLOY_PATH/"

    log_info "코드 동기화 완료"
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

setup_dirs() {
    log_info "디렉토리 생성 중..."
    remote_exec "mkdir -p $DEPLOY_PATH/logs $DEPLOY_PATH/pids"
    log_info "디렉토리 생성 완료"
}

install_crontab() {
    log_info "Crontab 등록 중..."

    # 기존 crontab 백업 및 kospi 관련 항목 제거 후 추가
    remote_exec "
        crontab -l 2>/dev/null | grep -v 'kospi_mini_sts' > /tmp/cron_backup || true

        # kospi_mini_sts cron 추가
        cat >> /tmp/cron_backup << 'CRON_EOF'

# ============================================================
# KOSPI Mini Futures Trading System
# ============================================================
# Paper Trading (08:50 on weekdays)
50 8 * * 1-5 /home/deploy/project/kospi_mini_sts/scripts/cron_paper_trading.sh

# Backfill (16:00 on weekdays)
0 16 * * 1-5 /home/deploy/project/kospi_mini_sts/scripts/cron_backfill.sh
CRON_EOF

        crontab /tmp/cron_backup
        rm /tmp/cron_backup
    "

    log_info "Crontab 등록 완료"
}

show_status() {
    log_info "=== 상태 확인 ==="
    echo ""

    # Crontab 확인
    log_info "Crontab (kospi_mini_sts):"
    remote_exec "crontab -l 2>/dev/null | grep -A1 'kospi_mini_sts' | grep -v '^#' | grep -v '^--$' || echo '  (등록 안됨)'"
    echo ""

    # 실행 중인 프로세스
    log_info "실행 중인 프로세스:"
    procs=$(remote_exec "ps aux | grep -E 'paper_trading|tick_collector|feature_processor' | grep -v grep" 2>/dev/null || echo "")
    if [ -z "$procs" ]; then
        echo "  (실행 중인 프로세스 없음)"
    else
        echo "$procs" | while read line; do
            echo "  $line"
        done
    fi
    echo ""

    # 인프라 상태
    log_info "인프라 상태:"
    redis_status=$(remote_exec "systemctl is-active redis-server.service" 2>/dev/null || echo "inactive")
    ch_status=$(remote_exec "systemctl is-active clickhouse-server.service" 2>/dev/null || echo "inactive")

    if [ "$redis_status" = "active" ]; then
        echo -e "  Redis: ${GREEN}${redis_status}${NC}"
    else
        echo -e "  Redis: ${RED}${redis_status}${NC}"
    fi

    if [ "$ch_status" = "active" ]; then
        echo -e "  ClickHouse: ${GREEN}${ch_status}${NC}"
    else
        echo -e "  ClickHouse: ${RED}${ch_status}${NC}"
    fi
    echo ""

    # 오늘 로그
    log_info "오늘 로그:"
    today=$(date +%Y%m%d)
    remote_exec "ls -la $DEPLOY_PATH/logs/*${today}* 2>/dev/null || echo '  (오늘 로그 없음)'"
}

show_logs() {
    log_info "최근 로그:"
    remote_exec "
        echo '=== Paper Trading ==='
        tail -30 $DEPLOY_PATH/logs/paper_trading_\$(date +%Y%m%d).log 2>/dev/null || echo '(로그 없음)'
        echo ''
        echo '=== Backfill ==='
        tail -20 $DEPLOY_PATH/logs/backfill_\$(date +%Y%m%d).log 2>/dev/null || echo '(로그 없음)'
    "
}

# =============================================================================
# 메인 명령어 처리
# =============================================================================

case "${1:-deploy}" in
    deploy)
        log_info "=== 전체 배포 시작 ==="
        sync_code
        setup_dirs

        # venv가 없으면 생성, 있으면 의존성만 업데이트
        if ! remote_exec "test -d $DEPLOY_PATH/venv"; then
            setup_venv
        else
            log_info "의존성 업데이트 중..."
            remote_exec "cd $DEPLOY_PATH && source venv/bin/activate && pip install -e '.[all]' -q"
        fi

        log_info "=== 배포 완료 ==="
        show_status
        ;;

    sync)
        sync_code
        ;;

    setup)
        log_info "=== 초기 설정 시작 ==="
        sync_code
        setup_dirs
        setup_venv
        install_crontab

        # 스크립트 실행 권한 부여
        remote_exec "chmod +x $DEPLOY_PATH/scripts/cron_*.sh"

        log_info "=== 초기 설정 완료 ==="
        show_status
        ;;

    install-deps)
        setup_venv
        ;;

    status)
        show_status
        ;;

    logs)
        show_logs
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
