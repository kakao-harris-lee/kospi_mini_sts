#!/bin/bash
# =============================================================================
# KOSPI Trading System - 배포 스크립트
# Git 기반 배포 (push/pull 방식)
# =============================================================================

set -e

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 설정
DEPLOY_HOST="${DEPLOY_HOST:-deploy@chsvr.duckdns.org}"
DEPLOY_PATH="${DEPLOY_PATH:-/home/deploy/project/kospi_mini_sts}"
LOCAL_PATH="$(cd "$(dirname "$0")/.." && pwd)"
GIT_BRANCH="${GIT_BRANCH:-main}"

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

log_step() {
    echo -e "${BLUE}[STEP]${NC} $1"
}

show_help() {
    cat << EOF
사용법: $0 [명령] [옵션]

명령:
    deploy          전체 배포 (git push + remote pull + 의존성 업데이트)
    push            로컬 변경사항 push (commit은 별도로 해야 함)
    pull            서버에서 git pull 실행
    setup           초기 설정 (git clone, venv, crontab 등록)
    install-deps    의존성 설치
    status          상태 확인 (git, cron, 프로세스)
    logs            최근 로그 확인

옵션:
    -h, --help      도움말 출력

예시:
    $0 deploy       # 전체 배포 (push → pull → deps)
    $0 push         # 로컬에서 push만
    $0 pull         # 서버에서 pull만
    $0 status       # 상태 확인

배포 워크플로우:
    1. 로컬에서 변경사항 커밋: git add . && git commit -m "message"
    2. 배포 실행: ./deploy/deploy.sh deploy
       - 또는 수동: ./deploy/deploy.sh push && ./deploy/deploy.sh pull
EOF
}

# =============================================================================
# 원격 실행 함수
# =============================================================================

remote_exec() {
    ssh "$DEPLOY_HOST" "$@"
}

# =============================================================================
# Git 관련 함수
# =============================================================================

check_git_status() {
    log_step "로컬 Git 상태 확인..."

    cd "$LOCAL_PATH"

    # uncommitted 변경사항 확인
    if ! git diff --quiet || ! git diff --cached --quiet; then
        log_warn "커밋되지 않은 변경사항이 있습니다:"
        git status --short
        echo ""
        read -p "계속 진행하시겠습니까? (y/N): " confirm
        if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
            log_error "배포 취소됨"
            exit 1
        fi
    fi

    # 현재 브랜치 확인
    current_branch=$(git branch --show-current)
    if [ "$current_branch" != "$GIT_BRANCH" ]; then
        log_warn "현재 브랜치: $current_branch (배포 브랜치: $GIT_BRANCH)"
    fi
}

git_push() {
    log_step "Git push 실행 중..."

    cd "$LOCAL_PATH"

    # remote 확인
    if ! git remote | grep -q origin; then
        log_error "Git remote 'origin'이 설정되지 않았습니다"
        exit 1
    fi

    # push
    git push origin "$GIT_BRANCH"

    log_info "Push 완료: $GIT_BRANCH"
}

git_pull_remote() {
    log_step "서버에서 Git pull 실행 중..."

    remote_exec "cd $DEPLOY_PATH && git pull origin $GIT_BRANCH"

    log_info "서버 Pull 완료"
}

# =============================================================================
# 설정 함수
# =============================================================================

setup_venv() {
    log_step "Python 가상환경 설정 중..."

    remote_exec "cd $DEPLOY_PATH && \
        python3 -m venv venv && \
        source venv/bin/activate && \
        pip install --upgrade pip && \
        pip install -e '.[all]'"

    log_info "가상환경 설정 완료"
}

setup_dirs() {
    log_step "디렉토리 생성 중..."
    remote_exec "mkdir -p $DEPLOY_PATH/logs $DEPLOY_PATH/pids"
    log_info "디렉토리 생성 완료"
}

install_crontab() {
    log_step "Crontab 등록 중..."

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

install_deps() {
    log_step "의존성 업데이트 중..."
    remote_exec "cd $DEPLOY_PATH && source venv/bin/activate && pip install -e '.[all]' -q"
    log_info "의존성 업데이트 완료"
}

# =============================================================================
# 상태 확인 함수
# =============================================================================

show_status() {
    log_info "=== 상태 확인 ==="
    echo ""

    # 로컬 Git 상태
    log_info "로컬 Git 상태:"
    cd "$LOCAL_PATH"
    echo "  브랜치: $(git branch --show-current)"
    echo "  최근 커밋: $(git log -1 --format='%h %s' 2>/dev/null)"
    local_changes=$(git status --porcelain | wc -l | tr -d ' ')
    if [ "$local_changes" -gt 0 ]; then
        echo -e "  변경사항: ${YELLOW}${local_changes}개 파일${NC}"
    else
        echo -e "  변경사항: ${GREEN}없음${NC}"
    fi
    echo ""

    # 서버 Git 상태
    log_info "서버 Git 상태:"
    remote_exec "cd $DEPLOY_PATH && \
        echo \"  브랜치: \$(git branch --show-current)\" && \
        echo \"  최근 커밋: \$(git log -1 --format='%h %s')\" && \
        echo \"  변경사항: \$(git status --porcelain | wc -l | tr -d ' ')개 파일\""
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
# 초기 설정 (git clone)
# =============================================================================

setup_git_clone() {
    log_step "서버에 Git repository 설정 중..."

    # repository URL 가져오기
    cd "$LOCAL_PATH"
    repo_url=$(git remote get-url origin)

    remote_exec "
        if [ -d $DEPLOY_PATH/.git ]; then
            echo 'Git repository가 이미 존재합니다'
        else
            mkdir -p $(dirname $DEPLOY_PATH)
            git clone $repo_url $DEPLOY_PATH
        fi
    "

    log_info "Git repository 설정 완료"
}

# =============================================================================
# 메인 명령어 처리
# =============================================================================

case "${1:-}" in
    deploy)
        log_info "=== 전체 배포 시작 ==="
        check_git_status
        git_push
        git_pull_remote
        setup_dirs

        # venv가 없으면 생성, 있으면 의존성만 업데이트
        if ! remote_exec "test -d $DEPLOY_PATH/venv"; then
            setup_venv
        else
            install_deps
        fi

        log_info "=== 배포 완료 ==="
        show_status
        ;;

    push)
        check_git_status
        git_push
        ;;

    pull)
        git_pull_remote
        ;;

    setup)
        log_info "=== 초기 설정 시작 ==="
        check_git_status
        git_push
        setup_git_clone
        git_pull_remote
        setup_dirs
        setup_venv
        install_crontab

        # 스크립트 실행 권한 부여
        remote_exec "chmod +x $DEPLOY_PATH/scripts/cron_*.sh"

        log_info "=== 초기 설정 완료 ==="
        show_status
        ;;

    install-deps)
        install_deps
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

    "")
        log_error "명령을 지정해주세요"
        echo ""
        show_help
        exit 1
        ;;

    *)
        log_error "알 수 없는 명령: $1"
        show_help
        exit 1
        ;;
esac
