#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NUM_PROCESSES="${CONSUMER_PROCESSES:-1}"
PID_DIR="${T2AV_PID_DIR:-/tmp}"
SHUTDOWN_GRACE="${T2AV_SHUTDOWN_GRACE:-30}"

echo "=== T2AV Consumer Supervisor ==="
echo "Launching ${NUM_PROCESSES} consumer process(es)..."
echo "Workers per process: ${CONSUMER_WORKERS:-15}"
echo "Max retries: ${CONSUMER_MAX_RETRIES:-5}"
echo "Retry backoff base: ${CONSUMER_RETRY_BACKOFF:-30}s"
echo "XML-RPC timeout: ${XMLRPC_TIMEOUT:-2700}s"
echo "Pipeline wall-clock cap: ${T2AV_PIPELINE_WALL_CLOCK:-1800}s"
echo ""

PIDS=()

cleanup() {
    echo ""
    echo "Sending SIGTERM to all consumer processes..."
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null || true
            echo "  Sent SIGTERM to PID $pid"
        fi
    done

    echo "Waiting up to ${SHUTDOWN_GRACE}s for graceful drain..."
    for ((i=0; i<SHUTDOWN_GRACE; i++)); do
        local alive=0
        for pid in "${PIDS[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                alive=$((alive+1))
            fi
        done
        if [ "$alive" -eq 0 ]; then
            break
        fi
        sleep 1
    done

    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "  Force-killing PID $pid"
            kill -KILL "$pid" 2>/dev/null || true
        fi
    done
    rm -f "$PID_DIR"/t2av_consumer_*.pid
    echo "All consumers stopped."
}

trap cleanup EXIT INT TERM

cd "$SCRIPT_DIR"

for i in $(seq 1 "$NUM_PROCESSES"); do
    python3 consumer.py &
    pid=$!
    PIDS+=("$pid")
    echo "$pid" > "$PID_DIR/t2av_consumer_${i}.pid"
    echo "  [Process $i] PID=$pid"
done

echo ""
echo "All ${NUM_PROCESSES} consumer process(es) running. Press Ctrl+C to stop."
echo ""

wait
