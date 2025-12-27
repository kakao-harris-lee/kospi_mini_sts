#!/bin/bash
# 매일 장 마감 후 1분봉 데이터 수집
# crontab: 0 16 * * 1-5 /home/deploy/project/kospi_mini_sts/scripts/cron_backfill.sh

set -e

PROJECT_DIR="/home/deploy/project/kospi_mini_sts"
LOG_FILE="$PROJECT_DIR/logs/backfill_$(date +%Y%m%d).log"
VENV="$PROJECT_DIR/venv/bin/activate"

mkdir -p "$PROJECT_DIR/logs"

cd "$PROJECT_DIR"
source "$VENV"

# 거래일 확인 (공휴일/주말 체크)
IS_TRADING_DAY=$(python3 -c "
from datetime import date
from src.collector.historical.calendar import is_trading_day
print('1' if is_trading_day(date.today()) else '0')
" 2>/dev/null || echo "0")

if [ "$IS_TRADING_DAY" != "1" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 오늘은 휴장일입니다. 백필 스킵." >> "$LOG_FILE"
    exit 0
fi

{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') 백필 시작 ==="

    # 오늘 데이터 수집
    sts backfill today

    # 데이터 현황
    sts backfill status

    echo "=== $(date '+%Y-%m-%d %H:%M:%S') 백필 완료 ==="
} >> "$LOG_FILE" 2>&1
