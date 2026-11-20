#!/bin/bash

# Graceful shutdown handler
cleanup() {
    echo "🛑 Shutting down services..."
    if [ -n "$WEB_PID" ]; then
        kill $WEB_PID 2>/dev/null
    fi
    exit 0
}

# Set up signal handlers for graceful shutdown
trap cleanup SIGTERM SIGINT

if [ "${BOOKBRIDGE_MODE:-}" = "worker" ]; then
    # Remote alignment worker: takes books from a main instance's alignment queue
    # (WORKER_MAIN_URL, WORKER_TOKEN). No web server, KoSync, sync cycle or sync
    # clients, and no migrations: it never opens the main instance's database.
    echo "🛠️ Starting BookBridge alignment worker..."
    while true; do
        python /app/src/alignment_worker.py &
        WEB_PID=$!
        wait $WEB_PID
        EXIT_CODE=$?
        if [ "$EXIT_CODE" -eq 2 ]; then
            echo "❌ Alignment worker is not configured (exit 2); not restarting."
            exit 2
        fi
        echo "⚠️  Alignment worker exited with code $EXIT_CODE. Restarting in 10 seconds..."
        sleep 10
    done
fi

echo "🚀 Starting ABS-KoSync Enhanced (Integrated Mode)..."
echo ""

echo "🔄 Running Database Migrations..."
if ! alembic upgrade head; then
    echo "❌ Database migration failed — refusing to start."
    echo "   Run 'alembic upgrade head' manually to diagnose."
    exit 1
fi
echo "✅ Database Migrations Completed"
echo ""

# Main Supervisor Loop
while true; do
    echo "  🌐 Starting unified service (web + sync daemon)..."
    # Start in background so we can trap signals
    python /app/src/web_server.py &
    WEB_PID=$!

    echo ""
    echo "✅ Service started successfully!"
    echo "   • Unified Service PID: $WEB_PID"
    echo "   • Web UI available at: http://localhost:5757"
    echo "   • Sync daemon running in background thread"
    echo ""

    # Wait for the process to exit
    # This will block until the python process ends (crashes or is killed)
    # If os.execv() is used, the PID stays the same and wait continues working.
    wait $WEB_PID
    EXIT_CODE=$?

    # If we get here, the app exited/crashed
    echo "Running cleanup..."

    # Record exit code so the next startup can report the real cause (e.g. OOM kill = 137)
    if [ "$EXIT_CODE" -eq 137 ]; then
        echo "💀 Process OOM-killed (exit 137). Writing sentinel..."
        echo "137" > "${DATA_DIR:-/data}/.last_exit_code"
    else
        rm -f "${DATA_DIR:-/data}/.last_exit_code"
    fi

    # If exit code is 0 (clean exit), maybe we should still restart?
    # Usually servers don't exit with 0 unless stopped.
    # But if we were killed by signal trapped above, the script exits in 'cleanup'.

    echo "⚠️  Application exited with code $EXIT_CODE. Restarting in 3 seconds..."
    sleep 3
done
