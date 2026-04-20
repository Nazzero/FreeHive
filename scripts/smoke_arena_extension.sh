#!/bin/bash
# scripts/smoke_arena_extension.sh
# Smoke tests for Arena Extension Bridge + CloakBrowser fallback.
# Tests: status, models, chat, conversation continuity, OpenAI compat.
set -euo pipefail

BASE="http://127.0.0.1:7200"
ARENA_MODEL="${ARENA_MODEL:-arena/gemini-2.5-flash}"
PASS=0; FAIL=0

ok()   { echo "  [PASS] $1"; PASS=$((PASS+1)); }
fail() { echo "  [FAIL] $1: $2"; FAIL=$((FAIL+1)); }

echo "=== FreeHive Arena Smoke Test ==="
echo "Model: $ARENA_MODEL"
echo ""

# 1. Status
echo "1. Arena status"
STATUS=$(curl -sf "$BASE/api/setup/arena/status" 2>/dev/null || echo '{}')
TRANSPORT=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transport','offline'))" 2>/dev/null || echo "offline")
BRIDGE=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('bridge_active',False))" 2>/dev/null || echo "False")
echo "  Transport: $TRANSPORT | Bridge: $BRIDGE"
if [ "$TRANSPORT" != "offline" ]; then ok "transport=$TRANSPORT"; else fail "status" "transport offline"; fi

# 2. Models
echo "2. Model list"
MODELS=$(curl -sf "$BASE/api/arena/models" 2>/dev/null || echo '{"models":[]}')
COUNT=$(echo "$MODELS" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('models',[])))" 2>/dev/null || echo "0")
echo "  Found: $COUNT models"
if [ "$COUNT" -gt 0 ]; then ok "models=$COUNT"; else fail "models" "none found"; fi

# 3. Create session + chat
echo "3. Chat test"
SID=$(curl -sf -X POST "$BASE/api/sessions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$ARENA_MODEL\"}" 2>/dev/null | \
  python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")

if [ -z "$SID" ]; then
    fail "session" "creation failed"
else
    ok "session=$SID"
    RESP=$(curl -sf --max-time 120 -X POST "$BASE/api/chat" \
      -H 'Content-Type: application/json' \
      -d "{\"model\":\"$ARENA_MODEL\",\"message\":\"Reply with exactly one word: PONG\",\"session_id\":\"$SID\"}" 2>/dev/null || echo "")
    if [ -n "$RESP" ] && ! echo "$RESP" | grep -qi "error"; then
        ok "chat response received"
        echo "  Preview: $(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print((d.get('response','') or d.get('text',''))[:100])" 2>/dev/null || echo "$RESP" | head -c 100)"
    else
        fail "chat" "error or empty response"
    fi
fi

# 4. OpenAI compat endpoint
echo "4. OpenAI /v1/chat/completions"
COMPAT=$(curl -sf --max-time 120 -X POST "$BASE/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer freehive-test' \
  -d "{\"model\":\"$ARENA_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello in one word\"}]}" 2>/dev/null || echo "")
if echo "$COMPAT" | python3 -c "import sys,json; d=json.load(sys.stdin); c=d['choices'][0]['message']['content']; assert c" 2>/dev/null; then
    ok "OpenAI compat"
    echo "  Content: $(echo "$COMPAT" | python3 -c "import sys,json; print(json.load(sys.stdin)['choices'][0]['message']['content'][:80])" 2>/dev/null)"
else
    fail "compat" "invalid response"
fi

# Summary
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ $FAIL -eq 0 ] && echo "All tests passed!" || echo "Some tests failed."
exit $FAIL
