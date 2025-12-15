#!/bin/bash
#
# KOSPI Mini 데이터 수집기 서버 배포 스크립트
#
# 사용법:
#   ./deploy_to_server.sh              # 전체 배포
#   ./deploy_to_server.sh --sync-only  # 파일만 동기화
#   ./deploy_to_server.sh --setup-cron # cron 설정만
#

set -e

# ============================================================
# 설정
# ============================================================
SERVER="deploy@49.247.171.64"
REMOTE_DIR="/home/deploy/kospi-mini-collector"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

# 색상 출력
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# ============================================================
# 파일 동기화
# ============================================================
sync_files() {
    log_info "파일 동기화 중..."

    # 원격 디렉토리 생성
    ssh "$SERVER" "mkdir -p $REMOTE_DIR"

    # rsync로 파일 동기화 (불필요한 파일 제외)
    rsync -avz --progress \
        --exclude '__pycache__' \
        --exclude '*.pyc' \
        --exclude '.env' \
        --exclude 'venv/' \
        --exclude '.venv/' \
        --exclude 'keggle/' \
        --exclude '*.log' \
        --exclude '.git/' \
        "$LOCAL_DIR/" "$SERVER:$REMOTE_DIR/"

    log_info "파일 동기화 완료"
}

# ============================================================
# Python 환경 설정
# ============================================================
setup_python_env() {
    log_info "Python 가상환경 설정 중..."

    ssh "$SERVER" << 'ENDSSH'
        cd /home/deploy/kospi-mini-collector

        # 가상환경 생성 (없으면)
        if [ ! -d "venv" ]; then
            python3 -m venv venv
            echo "가상환경 생성됨"
        fi

        # 의존성 설치
        source venv/bin/activate
        pip install --upgrade pip
        pip install -r requirements.txt

        echo "의존성 설치 완료"
ENDSSH

    log_info "Python 환경 설정 완료"
}

# ============================================================
# 서버 설정 파일 생성
# ============================================================
setup_config() {
    log_info "서버 설정 확인 중..."

    # 서버에 .env 파일이 있는지 확인
    if ssh "$SERVER" "[ -f $REMOTE_DIR/.env ]"; then
        log_info ".env 파일이 이미 존재합니다"
    else
        log_warn ".env 파일이 없습니다. 생성해주세요:"
        echo ""
        echo "  ssh $SERVER"
        echo "  cd $REMOTE_DIR"
        echo "  cat > .env << 'EOF'"
        echo "  # 한국투자증권 API 설정"
        echo "  KIS_APP_KEY=your_app_key"
        echo "  KIS_APP_SECRET=your_app_secret"
        echo "  KIS_ACCOUNT_NO=your_account_no"
        echo "  "
        echo "  # ClickHouse 설정 (로컬호스트)"
        echo "  CLICKHOUSE_HOST=localhost"
        echo "  CLICKHOUSE_PORT=8123"
        echo "  CLICKHOUSE_DATABASE=kospi"
        echo "  EOF"
        echo ""
    fi
}

# ============================================================
# Cron 작업 설정
# ============================================================
setup_cron() {
    log_info "Cron 작업 설정 중..."

    ssh "$SERVER" << 'ENDSSH'
        CRON_CMD="0 16 * * 1-5 cd /home/deploy/kospi-mini-collector && ./venv/bin/python collector.py --today >> /home/deploy/kospi-mini-collector/logs/cron.log 2>&1"

        # logs 디렉토리 생성
        mkdir -p /home/deploy/kospi-mini-collector/logs

        # 기존 cron 작업 확인
        if crontab -l 2>/dev/null | grep -q "kospi-mini-collector"; then
            echo "Cron 작업이 이미 존재합니다"
        else
            # cron 작업 추가
            (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
            echo "Cron 작업 추가됨: 평일 16:00 (장 마감 후)"
        fi

        # 현재 cron 작업 출력
        echo ""
        echo "현재 cron 작업:"
        crontab -l | grep kospi || echo "(없음)"
ENDSSH

    log_info "Cron 설정 완료"
}

# ============================================================
# 테스트 실행
# ============================================================
test_connection() {
    log_info "연결 및 설정 테스트 중..."

    ssh "$SERVER" << 'ENDSSH'
        cd /home/deploy/kospi-mini-collector
        source venv/bin/activate

        echo "Python 버전: $(python --version)"
        echo "작업 디렉토리: $(pwd)"
        echo ""

        # ClickHouse 연결 테스트
        python -c "
from app import db, config
try:
    client = db.get_client()
    print('ClickHouse 연결: OK')
except Exception as e:
    print(f'ClickHouse 연결 실패: {e}')
"

        # 상태 확인
        echo ""
        echo "수집 상태:"
        python collector.py --status 2>/dev/null || echo "상태 확인 실패 (테이블이 아직 없을 수 있음)"
ENDSSH

    log_info "테스트 완료"
}

# ============================================================
# 전체 배포
# ============================================================
full_deploy() {
    log_info "=== KOSPI Mini 데이터 수집기 배포 시작 ==="
    echo ""

    sync_files
    setup_python_env
    setup_config
    setup_cron
    test_connection

    echo ""
    log_info "=== 배포 완료 ==="
    echo ""
    echo "다음 단계:"
    echo "  1. .env 파일에 KIS API 키 설정 (아직 안했다면)"
    echo "     ssh $SERVER 'cd $REMOTE_DIR && nano .env'"
    echo ""
    echo "  2. 백필 실행 (과거 데이터 수집)"
    echo "     ssh $SERVER 'cd $REMOTE_DIR && source venv/bin/activate && python collector.py --backfill'"
    echo ""
    echo "  3. 로그 확인"
    echo "     ssh $SERVER 'tail -f $REMOTE_DIR/logs/cron.log'"
}

# ============================================================
# 메인
# ============================================================
case "${1:-}" in
    --sync-only)
        sync_files
        ;;
    --setup-cron)
        setup_cron
        ;;
    --test)
        test_connection
        ;;
    --help|-h)
        echo "사용법: $0 [옵션]"
        echo ""
        echo "옵션:"
        echo "  (없음)        전체 배포"
        echo "  --sync-only   파일만 동기화"
        echo "  --setup-cron  cron 설정만"
        echo "  --test        연결 테스트"
        echo "  --help        도움말"
        ;;
    *)
        full_deploy
        ;;
esac
