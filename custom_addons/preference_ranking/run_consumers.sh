#!/usr/bin/env bash
# Launch N consumer processes as competing consumers against the same RabbitMQ queues.
#
# Usage:
#   ./run_consumers.sh          # launch $CONSUMER_PROCESSES (default 4) workers
#   CONSUMER_PROCESSES=8 ./run_consumers.sh
#
# Each process runs consumer.py with its own ThreadPoolExecutor (CONSUMER_WORKERS threads).
# All processes share the same RabbitMQ queues so work is distributed automatically.
#
# Stop all workers:  kill $(cat /tmp/preference_consumer_*.pid)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NUM_PROCESSES="${CONSUMER_PROCESSES:-2}"
PID_DIR="/tmp"

echo "=== Preference Ranking Consumer Supervisor ==="
echo "Launching $NUM_PROCESSES consumer processes..."
echo "Workers per process: ${CONSUMER_WORKERS:-5}"
echo "Max retries: ${CONSUMER_MAX_RETRIES:-5}"
echo "Retry backoff base: ${CONSUMER_RETRY_BACKOFF:-30}s"
echo "XMLRPC timeout: ${XMLRPC_TIMEOUT:-1800}s"
echo ""

PIDS=()

cleanup() {
    echo ""
    echo "Shutting down all consumer processes..."
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
            echo "  Stopped PID $pid"
        fi
    done
    rm -f "$PID_DIR"/preference_consumer_*.pid
    echo "All consumers stopped."
}

trap cleanup EXIT INT TERM

for i in $(seq 1 "$NUM_PROCESSES"); do
    python3 -m consumer &
    pid=$!
    PIDS+=("$pid")
    echo "$pid" > "$PID_DIR/preference_consumer_${i}.pid"
    echo "  [Process $i] PID=$pid"
done

echo ""
echo "All $NUM_PROCESSES consumers running. Press Ctrl+C to stop all."
echo ""

wait
