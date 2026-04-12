#!/bin/bash
# FreeHive v0.5.3 start script

cd "$(dirname "$0")"

# Launch Chrome with CDP enabled (for Arena adapter)
# Skip if already running with debugging port
if ! curl -s http://localhost:9222/json > /dev/null 2>&1; then
    echo "[FreeHive] Launching Chrome with CDP on port 9222..."
    /usr/bin/google-chrome \
        --remote-debugging-port=9222 \
        --user-data-dir=/home/nazmoney/.config/google-chrome \
        --no-first-run \
        --no-default-browser-check \
        &
    sleep 2
    echo "[FreeHive] Chrome ready"
else
    echo "[FreeHive] Chrome CDP already running on port 9222"
fi

# Start backend
echo "[FreeHive] Starting backend on port 7200..."
source venv/bin/activate
uvicorn backend.main:app --host 127.0.0.1 --port 7200 --reload &
BACKEND_PID=$!

# Start frontend
echo "[FreeHive] Starting frontend on port 1420..."
npm run dev &
FRONTEND_PID=$!

echo "[FreeHive] All services started"
echo "  Frontend: http://localhost:1420"
echo "  Backend:  http://localhost:7200"
echo ""
echo "Press Ctrl+C to stop all services"

wait $BACKEND_PID $FRONTEND_PID