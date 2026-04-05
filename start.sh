#!/usr/bin/env bash
# FreeHive launcher — starts both the Python backend and the frontend dev server.
set -e

cd "$(dirname "$0")"

# ── Backend ───────────────────────────────────────────────────────────────────
if [ ! -d "venv" ]; then
    echo "ERROR: Python venv not found. Run: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

source venv/bin/activate
echo "Starting backend  →  http://localhost:8000"
uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload &
BACKEND_PID=$!

# ── Frontend ──────────────────────────────────────────────────────────────────
if [ ! -d "node_modules" ]; then
    echo "node_modules not found — running npm install first..."
    npm install
fi

echo "Starting frontend →  http://localhost:5173"
npm run dev &
FRONTEND_PID=$!

# ── Info ──────────────────────────────────────────────────────────────────────
echo ""
echo "FreeHive is running."
echo "  Open http://localhost:5173 in your browser."
echo ""
echo "Press Ctrl+C to stop everything."

# ── Cleanup on exit ───────────────────────────────────────────────────────────
cleanup() {
    echo ""
    echo "Stopping FreeHive..."
    kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
    echo "Done."
}

trap cleanup INT TERM
wait
