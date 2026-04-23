#!/bin/bash
# install_host.sh
# Installs the FreeHive Arena Bridge Native Messaging Host for Chrome on Linux.

HOST_NAME="com.freehive.arena_bridge"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
MANIFEST_SRC="${SCRIPT_DIR}/${HOST_NAME}.json"
TARGET_DIR="${HOME}/.config/google-chrome/NativeMessagingHosts"
TARGET_MANIFEST="${TARGET_DIR}/${HOST_NAME}.json"
HOST_PATH="${SCRIPT_DIR}/host.py"

# 1. Make host.py executable
chmod +x "${HOST_PATH}"

# 2. Create target directory and write target manifest with absolute host path
mkdir -p "${TARGET_DIR}"
python3 - "${MANIFEST_SRC}" "${TARGET_MANIFEST}" "${HOST_PATH}" <<'PY'
import json
import sys

manifest_src, target_manifest, host_path = sys.argv[1:]
with open(manifest_src, "r", encoding="utf-8") as f:
    data = json.load(f)
data["path"] = host_path
with open(target_manifest, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY

echo "Native messaging host '${HOST_NAME}' installed to ${TARGET_DIR}"
echo "Extension ID: jkclihigpeefogblifghhpojgkbheked (hardcoded)"
