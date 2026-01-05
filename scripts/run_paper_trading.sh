#!/bin/bash
# =============================================================================
# 실시간 Paper Trading 실행 스크립트
#
# tick_collector와 feature_processor가 실행 중인지 확인하고,
# 필요시 시작한 후 Paper Trading을 실행합니다.
#
# 사용법:
#   ./run_paper_trading.sh                    # 기본: pure_micro 전략, 1시간
#   ./run_paper_trading.sh --strategy hybrid --duration 30m
#   ./run_paper_trading.sh --simulation       # 시뮬레이션 모드 (Redis 없이)
# =============================================================================

set -e

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 기본 설정
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="${PROJECT_DIR}/venv"
LOG_DIR="${PROJECT_DIR}/logs"
PID_DIR="${PROJECT_DIR}/pids"

# 기본값
STRATEGY="pure_micro"
DURATION="1h"
CAPITAL="10000000"
SIMULATION=false
CLEANUP_ON_EXIT=true

# =============================================================================
# 함수 정의
# =============================================================================

log_info() {
    echo -e "${GREEN}[INFO]${NC} $(date '+%H:%M:%S') $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $(date '+%H:%M:%S') $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $(date '+%H:%M:%S') $1"
}

log_step() {
    echo -e "${BLUE}[STEP]${NC} $(date '+%H:%M:%S') $1"
}

show_help() {
    cat << EOF
사용법: $0 [옵션]

실시간 Paper Trading을 실행합니다.
tick_collector와 feature_processor를 자동으로 시작합니다.

옵션:
    -s, --strategy NAME     전략 이름 (기본: pure_micro)
                            사용 가능: pure_micro, adaptive_micro, dual_mode,
                                      mean_reversion, breakout, ofi_momentum, hybrid
    -d, --duration TIME     실행 시간 (기본: 1h)
                            예: 30m, 1h, 2h, 3600s
    -c, --capital AMOUNT    초기 자본금 (기본: 10000000)
    --simulation            시뮬레이션 모드 (Redis 없이 가상 데이터로 테스트)
    --no-cleanup            종료 시 백그라운드 프로세스 유지
    -h, --help              도움말 출력

예시:
    $0                                    # 기본 설정으로 실행
    $0 -s pure_micro -d 2h               # pure_micro 전략, 2시간
    $0 --simulation -d 10m               # 시뮬레이션 모드, 10분
    $0 -s breakout -c 50000000 -d 30m    # breakout 전략, 5천만원, 30분

EOF
}

# 명령줄 인수 파싱
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            -s|--strategy)
                STRATEGY="$2"
                shift 2
                ;;
            -d|--duration)
                DURATION="$2"
                shift 2
                ;;
            -c|--capital)
                CAPITAL="$2"
                shift 2
                ;;
            --simulation)
                SIMULATION=true
                shift
                ;;
            --no-cleanup)
                CLEANUP_ON_EXIT=false
                shift
                ;;
            -h|--help)
                show_help
                exit 0
                ;;
            *)
                log_error "알 수 없는 옵션: $1"
                show_help
                exit 1
                ;;
        esac
    done
}

# 디렉토리 생성
setup_dirs() {
    mkdir -p "$LOG_DIR"
    mkdir -p "$PID_DIR"
}

# 가상환경 활성화
activate_venv() {
    if [[ ! -d "$VENV_DIR" ]]; then
        log_error "가상환경이 없습니다: $VENV_DIR"
        log_info "먼저 'python -m venv venv && pip install -e .[all]' 실행하세요"
        exit 1
    fi
    source "$VENV_DIR/bin/activate"
}

# Redis 연결 확인
check_redis() {
    if $SIMULATION; then
        log_info "시뮬레이션 모드 - Redis 확인 건너뜀"
        return 0
    fi

    log_step "Redis 연결 확인..."

    # Redis 비밀번호 가져오기 (.env에서)
    REDIS_PASSWORD=""
    if [[ -f "${PROJECT_DIR}/.env" ]]; then
        REDIS_PASSWORD=$(grep "^REDIS_PASSWORD=" "${PROJECT_DIR}/.env" | cut -d'=' -f2)
    fi

    if [[ -n "$REDIS_PASSWORD" ]]; then
        if redis-cli -a "$REDIS_PASSWORD" ping 2>/dev/null | grep -q "PONG"; then
            log_info "Redis 연결 성공"
            return 0
        fi
    else
        if redis-cli ping 2>/dev/null | grep -q "PONG"; then
            log_info "Redis 연결 성공"
            return 0
        fi
    fi

    log_error "Redis에 연결할 수 없습니다"
    log_info "시뮬레이션 모드로 실행하려면 --simulation 옵션을 사용하세요"
    exit 1
}

# 프로세스 실행 중인지 확인
is_process_running() {
    local pid_file="$1"
    if [[ -f "$pid_file" ]]; then
        local pid=$(cat "$pid_file")
        if ps -p "$pid" > /dev/null 2>&1; then
            return 0
        fi
    fi
    return 1
}

# tick_collector 시작
start_tick_collector() {
    if $SIMULATION; then
        return 0
    fi

    local pid_file="${PID_DIR}/tick_collector.pid"
    local log_file="${LOG_DIR}/tick_collector.log"

    if is_process_running "$pid_file"; then
        log_info "tick_collector 이미 실행 중 (PID: $(cat $pid_file))"
        return 0
    fi

    log_step "tick_collector 시작..."

    cd "$PROJECT_DIR"
    nohup python -m src.collector.tick_collector > "$log_file" 2>&1 &
    local pid=$!
    echo $pid > "$pid_file"

    # 시작 확인 (3초 대기)
    sleep 3
    if ps -p $pid > /dev/null 2>&1; then
        log_info "tick_collector 시작됨 (PID: $pid)"
    else
        log_error "tick_collector 시작 실패"
        cat "$log_file" | tail -20
        exit 1
    fi
}

# feature_processor 시작
start_feature_processor() {
    if $SIMULATION; then
        return 0
    fi

    local pid_file="${PID_DIR}/feature_processor.pid"
    local log_file="${LOG_DIR}/feature_processor.log"

    if is_process_running "$pid_file"; then
        log_info "feature_processor 이미 실행 중 (PID: $(cat $pid_file))"
        return 0
    fi

    log_step "feature_processor 시작..."

    cd "$PROJECT_DIR"
    nohup python -m src.processor.feature_processor > "$log_file" 2>&1 &
    local pid=$!
    echo $pid > "$pid_file"

    # 시작 확인 (3초 대기)
    sleep 3
    if ps -p $pid > /dev/null 2>&1; then
        log_info "feature_processor 시작됨 (PID: $pid)"
    else
        log_error "feature_processor 시작 실패"
        cat "$log_file" | tail -20
        exit 1
    fi
}

# 백그라운드 프로세스 정리
cleanup() {
    if ! $CLEANUP_ON_EXIT; then
        log_info "백그라운드 프로세스 유지 (--no-cleanup)"
        return 0
    fi

    log_step "백그라운드 프로세스 정리..."

    # tick_collector 종료
    local tick_pid_file="${PID_DIR}/tick_collector.pid"
    if [[ -f "$tick_pid_file" ]]; then
        local pid=$(cat "$tick_pid_file")
        if ps -p $pid > /dev/null 2>&1; then
            kill $pid 2>/dev/null || true
            log_info "tick_collector 종료됨 (PID: $pid)"
        fi
        rm -f "$tick_pid_file"
    fi

    # feature_processor 종료
    local proc_pid_file="${PID_DIR}/feature_processor.pid"
    if [[ -f "$proc_pid_file" ]]; then
        local pid=$(cat "$proc_pid_file")
        if ps -p $pid > /dev/null 2>&1; then
            kill $pid 2>/dev/null || true
            log_info "feature_processor 종료됨 (PID: $pid)"
        fi
        rm -f "$proc_pid_file"
    fi
}

# Paper Trading 실행
run_paper_trading() {
    log_step "Paper Trading 시작..."
    echo ""
    echo "========================================"
    echo " Paper Trading 설정"
    echo "========================================"
    echo " 전략: $STRATEGY"
    echo " 시간: $DURATION"
    echo " 자본: $(printf "%'d" $CAPITAL) KRW"
    echo " 모드: $(if $SIMULATION; then echo '시뮬레이션'; else echo '실시간'; fi)"
    echo "========================================"
    echo ""

    cd "$PROJECT_DIR"

    local cmd="sts paper run --strategy $STRATEGY --duration $DURATION --capital $CAPITAL"
    if $SIMULATION; then
        cmd="$cmd --simulation"
    fi

    # Paper Trading 실행
    eval $cmd
}

# 상태 확인
check_status() {
    echo ""
    echo "========================================"
    echo " 프로세스 상태"
    echo "========================================"

    # tick_collector
    local tick_pid_file="${PID_DIR}/tick_collector.pid"
    if is_process_running "$tick_pid_file"; then
        echo -e " tick_collector:    ${GREEN}실행 중${NC} (PID: $(cat $tick_pid_file))"
    else
        echo -e " tick_collector:    ${RED}중지됨${NC}"
    fi

    # feature_processor
    local proc_pid_file="${PID_DIR}/feature_processor.pid"
    if is_process_running "$proc_pid_file"; then
        echo -e " feature_processor: ${GREEN}실행 중${NC} (PID: $(cat $proc_pid_file))"
    else
        echo -e " feature_processor: ${RED}중지됨${NC}"
    fi

    echo "========================================"
    echo ""
}

# =============================================================================
# 메인 실행
# =============================================================================

main() {
    parse_args "$@"
    setup_dirs

    echo ""
    echo "========================================"
    echo " 실시간 Paper Trading"
    echo "========================================"
    echo ""

    activate_venv
    check_redis

    # 종료 시 정리 (Ctrl+C 등)
    trap cleanup EXIT

    if ! $SIMULATION; then
        start_tick_collector
        start_feature_processor

        # 데이터 수집 시작 대기
        log_info "데이터 수집 시작 대기 (5초)..."
        sleep 5

        check_status
    fi

    run_paper_trading
}

main "$@"
