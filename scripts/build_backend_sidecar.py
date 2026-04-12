#!/usr/bin/env python3
"""
Build a one-file backend executable for Tauri packaging.

Output:
  src-tauri/sidecar/freehive-backend(.exe)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SIDECAR_DIR = REPO_ROOT / "src-tauri" / "sidecar"
ENTRYPOINT = REPO_ROOT / "backend" / "main.py"
DIST_DIR = REPO_ROOT / "dist"
BUILD_DIR = REPO_ROOT / "build" / "pyinstaller"
SPEC_DIR = REPO_ROOT / "build" / "pyinstaller-spec"
APP_NAME = "freehive-backend"


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)


def main() -> int:
    if importlib.util.find_spec("PyInstaller") is None:
        print(
            "PyInstaller is required for sidecar builds. Install it with "
            f"`{sys.executable} -m pip install pyinstaller`.",
            file=sys.stderr,
        )
        return 1

    if not ENTRYPOINT.exists():
        print(f"Entrypoint missing: {ENTRYPOINT}", file=sys.stderr)
        return 1

    SIDECAR_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    SPEC_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        APP_NAME,
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR),
        "--specpath",
        str(SPEC_DIR),
        "--hidden-import",
        "backend.router",
        "--hidden-import",
        "backend.setup_router",
        "--hidden-import",
        "backend.compat_router",
        "--hidden-import",
        "backend.session_manager",
        "--hidden-import",
        "backend.arena_manager",
        "--hidden-import",
        "backend.model_discovery",
        "--hidden-import",
        "backend.services.arena_bridge_client",
        "--hidden-import",
        "backend.services.arena_model_health",
        "--hidden-import",
        "backend.services.arena_bridge_transport",
        "--hidden-import",
        "backend.adapters.claude_direct_adapter",
        "--hidden-import",
        "backend.adapters.chatgpt_direct_adapter",
        "--hidden-import",
        "backend.adapters.gemini_direct_adapter",
        "--hidden-import",
        "backend.adapters.arena_bridge_adapter",
        str(ENTRYPOINT),
    ]
    run(cmd)

    exe_name = f"{APP_NAME}.exe" if sys.platform.startswith("win") else APP_NAME
    built = DIST_DIR / exe_name
    if not built.exists():
        print(f"Build succeeded but executable not found: {built}", file=sys.stderr)
        return 1

    output = SIDECAR_DIR / exe_name
    shutil.copy2(built, output)
    print(f"Sidecar ready: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
