#!/bin/bash
#
# Daily Training Pipeline - Cron Wrapper
#
# Schedule: 0 17 * * 1-5 (17:00 weekdays, after 16:00 backfill)
#
# This script:
#   1. Checks if today is a trading day
#   2. Sets up the environment
#   3. Runs the training pipeline with logging
#   4. Reports success/failure
#

set -euo pipefail

# Paths
PROJECT_DIR="/home/deploy/project/kospi_mini_sts/docker-training"
MAIN_PROJECT_DIR="/home/deploy/project/kospi_mini_sts"
LOG_DIR="$PROJECT_DIR/logs"
VENV_PATH="$MAIN_PROJECT_DIR/.venv"

# Create log directory if needed
mkdir -p "$LOG_DIR"

# Log file for today
LOG_FILE="$LOG_DIR/daily_training_$(date +%Y%m%d).log"

# Function to log with timestamp
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Function to send notification (optional - implement as needed)
notify() {
    local status="$1"
    local message="$2"

    # Uncomment to enable Telegram notification:
    # if command -v python &> /dev/null; then
    #     cd "$MAIN_PROJECT_DIR"
    #     source "$VENV_PATH/bin/activate"
    #     python -c "from src.common.telegram import send_message; send_message('[$status] $message')" 2>/dev/null || true
    # fi

    log "Notification: [$status] $message"
}

# Start logging
log "========================================"
log "Daily Training Cron Job Started"
log "========================================"

# Check if weekend (cron should handle this, but double-check)
DOW=$(date +%u)
if [[ "$DOW" -gt 5 ]]; then
    log "Weekend (day $DOW) - skipping"
    exit 0
fi

# Check if trading day (basic check - could add Korean holiday calendar)
# For now, rely on cron schedule (weekdays only)

# Activate virtual environment
if [[ -f "$VENV_PATH/bin/activate" ]]; then
    log "Activating virtual environment: $VENV_PATH"
    source "$VENV_PATH/bin/activate"
else
    log "ERROR: Virtual environment not found at $VENV_PATH"
    log "Please create venv with: python -m venv $VENV_PATH && $VENV_PATH/bin/pip install -e '.[all]'"
    notify "FAILED" "Virtual environment not found at $VENV_PATH"
    exit 1
fi

# Change to project directory
cd "$PROJECT_DIR"
log "Working directory: $(pwd)"

# Check Python and dependencies
log "Python: $(which python)"
log "Python version: $(python --version 2>&1)"

# Run the pipeline
log ""
log "Starting training pipeline..."
log ""

if "$PROJECT_DIR/scripts/daily_pipeline.sh" >> "$LOG_FILE" 2>&1; then
    EXIT_CODE=0
    log ""
    log "========================================"
    log "Pipeline completed successfully"
    log "========================================"
    notify "SUCCESS" "Daily training completed"
else
    EXIT_CODE=$?
    log ""
    log "========================================"
    log "Pipeline FAILED with exit code $EXIT_CODE"
    log "========================================"
    notify "FAILED" "Daily training failed with exit code $EXIT_CODE"
fi

# Log file size info
log ""
log "Log file: $LOG_FILE"
log "Log size: $(du -h "$LOG_FILE" | cut -f1)"

exit $EXIT_CODE
