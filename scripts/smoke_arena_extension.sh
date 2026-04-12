#!/bin/bash
# scripts/smoke_arena_extension.sh
# Verifies the Arena Extension Bridge flow via the backend API.

API_URL="http://127.0.0.1:7200/api"
ARENA_MODEL="${ARENA_MODEL:-arena/gpt-5.2-chat-latest}"

echo "=== FreeHive Arena Bridge Smoke Test ==="

# 1. Check Status
echo -n "Checking Arena status... "
STATUS=$(curl -s "${API_URL}/arena/status")
if echo "$STATUS" | grep -q '"bridge_active":true'; then
    echo "PASS (Bridge active)"
else
    echo "FAIL (Bridge inactive)"
    echo "Response: $STATUS"
    exit 1
fi

# 2. List Models
echo -n "Fetching Arena models... "
MODELS=$(curl -s "${API_URL}/arena/models")
if echo "$MODELS" | grep -q '"models":\['; then
    echo "PASS"
else
    echo "FAIL"
    echo "Response: $MODELS"
    exit 1
fi

# 3. Create Session
echo -n "Creating Arena session... "
SESSION=$(curl -s -X POST -H "Content-Type: application/json" \
    -d "{\"model\":\"${ARENA_MODEL}\"}" \
    "${API_URL}/sessions")
SESSION_ID=$(echo "$SESSION" | grep -oP '"id":"\K[^"]+')

if [ -n "$SESSION_ID" ]; then
    echo "PASS (ID: $SESSION_ID)"
else
    echo "FAIL"
    echo "Response: $SESSION"
    exit 1
fi

# 4. Send Message (Multi-turn verification)
echo "Sending Turn 1 (Hello)..."
RES1=$(curl -s -X POST -H "Content-Type: application/json" \
    -d "{\"model\":\"${ARENA_MODEL}\",\"message\":\"Reply with exactly: HI-TURN-1\",\"session_id\":\"$SESSION_ID\"}" \
    "${API_URL}/chat")

if echo "$RES1" | grep -q "HI-TURN-1"; then
    echo "Turn 1: PASS"
else
    echo "Turn 1: FAIL"
    echo "Response: $RES1"
    exit 1
fi

echo "Sending Turn 2 (Check continuity)..."
RES2=$(curl -s -X POST -H "Content-Type: application/json" \
    -d "{\"model\":\"${ARENA_MODEL}\",\"message\":\"What was my previous message? Reply with exactly: YOU-SAID-HI\",\"session_id\":\"$SESSION_ID\"}" \
    "${API_URL}/chat")

if echo "$RES2" | grep -q "YOU-SAID-HI"; then
    echo "Turn 2: PASS (Continuity confirmed)"
else
    echo "Turn 2: FAIL"
    echo "Response: $RES2"
    exit 1
fi

echo "=== All Arena Bridge Smoke Tests Passed ==="
