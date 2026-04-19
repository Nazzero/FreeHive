#!/bin/bash
# setup_arena_bridge.sh — Arena Extension Bridge setup
# Installs native host and configures extension ID.
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
HOST_NAME="com.freehive.arena_bridge"

echo "=== FreeHive Arena Extension Bridge Setup ==="
echo ""

# 1. Install native messaging host
echo "[1/4] Installing native messaging host..."
cd "${PROJECT_DIR}/native_host" && bash install_host.sh
echo ""

# 2. Detect extension ID
HOST_MANIFEST="${HOME}/.config/google-chrome/NativeMessagingHosts/${HOST_NAME}.json"
EXT_ID=""
STORE_EXT_ID="jkclihigpeefogblifghhpojgkbheked"

# Check if extension ID was passed as argument
if [ -n "$1" ]; then
    EXT_ID="$1"
    echo "[2/4] Using provided extension ID: ${EXT_ID}"
else
    # Try to find it from Chrome's Extensions Preferences
    PREFS_FILE="${HOME}/.config/google-chrome/Default/Preferences"
    if [ -f "$PREFS_FILE" ]; then
        EXT_ID=$(python3 -c "
import json, sys
try:
    with open('$PREFS_FILE', 'r') as f:
        prefs = json.load(f)
    exts = prefs.get('extensions', {}).get('settings', {})
    for ext_id, info in exts.items():
        path = info.get('path', '')
        if 'arena_extension' in path or 'freehive' in path.lower():
            print(ext_id)
            sys.exit(0)
except Exception:
    pass
" 2>/dev/null)
    fi

    if [ -n "$EXT_ID" ]; then
        echo "[2/4] Detected extension ID from Chrome: ${EXT_ID}"
    else
        EXT_ID="$STORE_EXT_ID"
        echo "[2/4] Using Chrome Web Store extension ID: ${EXT_ID}"
    fi
fi

# 3. Update host manifest with extension ID
if [ -n "$EXT_ID" ] && [ -f "$HOST_MANIFEST" ]; then
    echo "[3/4] Updating native host manifest with extension ID..."
    python3 -c "
import json
with open('$HOST_MANIFEST', 'r') as f:
    data = json.load(f)
data['allowed_origins'] = ['chrome-extension://$EXT_ID/']
with open('$HOST_MANIFEST', 'w') as f:
    json.dump(data, f, indent=2)
    f.write('\n')
print('  Manifest updated.')
"
else
    echo "[3/4] Skipping manifest update (no extension ID yet)."
fi
echo ""

# 4. Instructions
echo "[4/4] Next steps:"
echo ""
echo "  1. Install extension (pick one):"
echo "     A) Chrome Web Store: https://chromewebstore.google.com/detail/freehive-arena-bridge/jkclihigpeefogblifghhpojgkbheked"
echo "     B) Load unpacked: chrome://extensions → Developer mode → Load unpacked → ${PROJECT_DIR}/arena_extension"
echo "  2. Open https://arena.ai/text/direct in Chrome"
echo "  3. Log in with Google if needed"
echo "  4. Pin the arena.ai tab"
echo ""
echo "=== Setup complete. Start backend with: ./start.sh ==="
