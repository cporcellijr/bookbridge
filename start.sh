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
        echo "137" > /data/.last_exit_code
    else
        rm -f /data/.last_exit_code
    fi

    # If exit code is 0 (clean exit), maybe we should still restart?
    # Usually servers don't exit with 0 unless stopped.
    # But if we were killed by signal trapped above, the script exits in 'cleanup'.

    echo "⚠️  Application exited with code $EXIT_CODE. Restarting in 3 seconds..."
    sleep 3
done
