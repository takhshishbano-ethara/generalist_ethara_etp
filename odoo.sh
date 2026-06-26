#!/bin/bash
cd "$(dirname "$0")"

export OPENSSL_CONF="$(pwd)/openssl-legacy.cnf"

PIDFILE="$HOME/.odoo.pid"
LOGFILE="$HOME/odoo.log"

start() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "Odoo is already running (PID $(cat "$PIDFILE"))"
        echo "Access it at: http://localhost:8069"
        return
    fi

    echo "Starting Odoo 19..."
    source .venv/bin/activate
    nohup python src/odoo-bin -c odoo.conf >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 5

    if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "Odoo started successfully (PID $(cat "$PIDFILE"))"
        echo "Access it at: http://localhost:8069"
        echo "Logs: $LOGFILE"
    else
        echo "ERROR: Odoo failed to start. Check logs:"
        tail -20 "$LOGFILE"
        rm -f "$PIDFILE"
    fi
}

stop() {
    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "Stopping Odoo (PID $PID)..."
            kill "$PID"
            sleep 3
            kill -0 "$PID" 2>/dev/null && kill -9 "$PID"
            echo "Odoo stopped."
        else
            echo "Odoo is not running (stale PID file)."
        fi
        rm -f "$PIDFILE"
    else
        echo "No PID file found. Odoo is not running."
    fi
}

restart() {
    stop
    sleep 2
    start
}

status() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "Odoo is running (PID $(cat "$PIDFILE"))"
        echo "http://localhost:8069"
    else
        echo "Odoo is NOT running."
        rm -f "$PIDFILE" 2>/dev/null
    fi
}

logs() {
    tail -f "$LOGFILE"
}

case "${1:-start}" in
    start)   start   ;;
    stop)    stop    ;;
    restart) restart ;;
    status)  status  ;;
    logs)    logs    ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
