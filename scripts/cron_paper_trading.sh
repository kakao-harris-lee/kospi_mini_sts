#!/bin/bash
# 모의투자 crontab 스크립트
# 장이 열리는 날만 실행하고, 장 종료 후 자동 종료
#
# crontab 등록:
# 50 8 * * 1-5 /home/deploy/project/kospi_mini_sts/scripts/cron_paper_trading.sh
#
# 기능:
# 1. 거래일인지 확인 (주말/공휴일 제외)
# 2. 거래일이면 paper_trading_service.py --once 실행
# 3. 장 종료 후 자동 종료 (--once 옵션)

set -e

PROJECT_DIR="/home/deploy/project/kospi_mini_sts"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/paper_trading_$(date +%Y%m%d).log"
VENV="$PROJECT_DIR/venv/bin/activate"
PID_FILE="$PROJECT_DIR/pids/paper_trading_service.pid"

mkdir -p "$LOG_DIR"
mkdir -p "$PROJECT_DIR/pids"

# 로그 함수
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# 이미 실행 중인지 확인
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        log "이미 실행 중 (PID: $OLD_PID)"
        exit 0
    else
        rm -f "$PID_FILE"
    fi
fi

log "=== Paper Trading 시작 스크립트 ==="

cd "$PROJECT_DIR"
source "$VENV"

# 거래일 확인 (Python 스크립트로 체크)
IS_TRADING_DAY=$(python3 -c "
from datetime import date
from scripts.paper_trading_service import is_trading_day
print('1' if is_trading_day(date.today()) else '0')
" 2>/dev/null || echo "0")

if [ "$IS_TRADING_DAY" != "1" ]; then
    log "오늘은 휴장일입니다. 종료합니다."
    exit 0
fi

log "거래일 확인됨. Paper Trading 시작..."

# Paper Trading 서비스 실행 (--once: 오늘만 실행 후 종료)
# trend_confirmed: Triple Barrier + Trend confirmation
nohup python scripts/paper_trading_service.py \
    --once \
    --strategy trend_confirmed \
    >> "$LOG_FILE" 2>&1 &

# PID 저장
echo $! > "$PID_FILE"
log "Paper Trading 시작됨 (PID: $!)"
