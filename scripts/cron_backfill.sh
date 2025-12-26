#!/bin/bash
# 매일 장 마감 후 1분봉 데이터 수집
# crontab: 0 16 * * 1-5 /home/deploy/project/kospi_mini_sts/scripts/cron_backfill.sh

set -e

PROJECT_DIR="/home/deploy/project/kospi_mini_sts"
LOG_FILE="$PROJECT_DIR/logs/backfill_$(date +%Y%m%d).log"
VENV="$PROJECT_DIR/venv/bin/activate"

mkdir -p "$PROJECT_DIR/logs"

{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') 백필 시작 ==="

    cd "$PROJECT_DIR"
    source "$VENV"

    # 오늘 데이터 수집
    sts backfill today

    # 데이터 현황
    sts backfill status

    echo "=== $(date '+%Y-%m-%d %H:%M:%S') 백필 완료 ==="
} >> "$LOG_FILE" 2>&1
