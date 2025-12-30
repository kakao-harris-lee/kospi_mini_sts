#!/bin/bash
# =============================================================================
# KOSPI Trading System - Direct Execution Script
#
# Usage:
#   ./run.sh paper                    # Paper trading (default: pure_micro, 1h)
#   ./run.sh paper -s breakout -d 2h  # Paper trading with options
#   ./run.sh backtest                 # Run backtest
#   ./run.sh backfill                 # Backfill today's data
#   ./run.sh status                   # Check system status
# =============================================================================

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Paths
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/venv"
LOG_DIR="${SCRIPT_DIR}/logs"

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step() { echo -e "${BLUE}[STEP]${NC} $1"; }

show_help() {
    cat << EOF
KOSPI Trading System - Direct Execution

Usage: ./run.sh <command> [options]

Commands:
    paper [options]     Run paper trading
                        -s, --strategy NAME   Strategy (default: pure_micro)
                        -d, --duration TIME   Duration (default: 1h)
                        --simulation          Simulation mode (no Redis)

    backtest [options]  Run backtest
                        --strategy NAME       Strategy (default: pure_micro)
                        --days N              Days to backtest (default: 30)

    backfill [days]     Backfill candle data (default: today)

    status              Show system status

    logs                Show recent logs

    help                Show this help

Examples:
    ./run.sh paper                           # Default paper trading
    ./run.sh paper -s breakout -d 2h         # Breakout strategy, 2 hours
    ./run.sh paper --simulation -d 30m       # Simulation mode
    ./run.sh backtest --days 14              # 14-day backtest
    ./run.sh backfill 7                      # Backfill last 7 days
    ./run.sh status                          # Check status
EOF
}

activate_venv() {
    if [[ ! -d "$VENV_DIR" ]]; then
        log_error "Virtual environment not found: $VENV_DIR"
        log_info "Run: python3 -m venv venv && pip install -e '.[all]'"
        exit 1
    fi
    source "$VENV_DIR/bin/activate"
}

check_infra() {
    # Check Redis
    REDIS_PASSWORD=""
    if [[ -f "${SCRIPT_DIR}/.env" ]]; then
        REDIS_PASSWORD=$(grep "^REDIS_PASSWORD=" "${SCRIPT_DIR}/.env" 2>/dev/null | cut -d'=' -f2 || echo "")
    fi

    if [[ -n "$REDIS_PASSWORD" ]]; then
        redis-cli -a "$REDIS_PASSWORD" ping &>/dev/null || { log_error "Redis connection failed"; return 1; }
    else
        redis-cli ping &>/dev/null || { log_error "Redis connection failed"; return 1; }
    fi
    log_info "Redis: OK"

    # Check ClickHouse
    curl -s "http://localhost:8123/ping" &>/dev/null || { log_error "ClickHouse connection failed"; return 1; }
    log_info "ClickHouse: OK"

    return 0
}

run_paper() {
    log_step "Starting Paper Trading..."
    activate_venv
    mkdir -p "$LOG_DIR"

    # Pass all arguments to the paper trading script
    cd "$SCRIPT_DIR"
    exec bash scripts/run_paper_trading.sh "$@"
}

run_backtest() {
    local strategy="pure_micro"
    local days="30"

    while [[ $# -gt 0 ]]; do
        case $1 in
            --strategy) strategy="$2"; shift 2 ;;
            --days) days="$2"; shift 2 ;;
            *) shift ;;
        esac
    done

    log_step "Running Backtest: strategy=$strategy, days=$days"
    activate_venv
    cd "$SCRIPT_DIR"
    sts backtest quick --days "$days" --strategy "$strategy"
}

run_backfill() {
    local days="${1:-0}"

    activate_venv
    cd "$SCRIPT_DIR"

    if [[ "$days" == "0" ]] || [[ -z "$days" ]]; then
        log_step "Backfilling today's data..."
        sts backfill today
    else
        log_step "Backfilling last $days days..."
        sts backfill run --days "$days"
    fi
}

show_status() {
    echo ""
    echo "========================================"
    echo " System Status"
    echo "========================================"
    echo ""

    # Git status
    log_info "Git:"
    cd "$SCRIPT_DIR"
    echo "  Branch: $(git branch --show-current)"
    echo "  Commit: $(git log -1 --format='%h %s')"
    echo ""

    # Infrastructure
    log_info "Infrastructure:"
    REDIS_PASSWORD=$(grep "^REDIS_PASSWORD=" "${SCRIPT_DIR}/.env" 2>/dev/null | cut -d'=' -f2 || echo "")
    if [[ -n "$REDIS_PASSWORD" ]]; then
        redis_ok=$(redis-cli -a "$REDIS_PASSWORD" ping 2>/dev/null | grep -c PONG || echo 0)
    else
        redis_ok=$(redis-cli ping 2>/dev/null | grep -c PONG || echo 0)
    fi
    ch_ok=$(curl -s "http://localhost:8123/ping" 2>/dev/null | grep -c Ok || echo 0)

    if [[ "$redis_ok" -gt 0 ]]; then
        echo -e "  Redis: ${GREEN}running${NC}"
    else
        echo -e "  Redis: ${RED}not available${NC}"
    fi

    if [[ "$ch_ok" -gt 0 ]]; then
        echo -e "  ClickHouse: ${GREEN}running${NC}"
    else
        echo -e "  ClickHouse: ${RED}not available${NC}"
    fi
    echo ""

    # Running processes
    log_info "Processes:"
    procs=$(ps aux | grep -E 'tick_collector|feature_processor|paper_trading' | grep -v grep || echo "")
    if [[ -z "$procs" ]]; then
        echo "  (no trading processes running)"
    else
        echo "$procs" | awk '{print "  " $11 " (PID: " $2 ")"}'
    fi
    echo ""

    # Today's logs
    log_info "Today's Logs:"
    today=$(date +%Y%m%d)
    ls -la "$LOG_DIR"/*"${today}"* 2>/dev/null || echo "  (no logs today)"
    echo ""
}

show_logs() {
    echo ""
    log_info "=== Recent Logs ==="
    echo ""

    today=$(date +%Y%m%d)

    echo "--- Paper Trading ---"
    tail -50 "$LOG_DIR/paper_trading_${today}.log" 2>/dev/null || echo "(no log)"
    echo ""

    echo "--- Backfill ---"
    tail -20 "$LOG_DIR/backfill_${today}.log" 2>/dev/null || echo "(no log)"
}

# =============================================================================
# Main
# =============================================================================

case "${1:-help}" in
    paper)
        shift
        run_paper "$@"
        ;;
    backtest)
        shift
        run_backtest "$@"
        ;;
    backfill)
        shift
        run_backfill "$@"
        ;;
    status)
        show_status
        ;;
    logs)
        show_logs
        ;;
    help|-h|--help)
        show_help
        ;;
    *)
        log_error "Unknown command: $1"
        echo ""
        show_help
        exit 1
        ;;
esac
