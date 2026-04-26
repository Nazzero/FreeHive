#!/usr/bin/env python3
"""
Build a one-file backend executable for Tauri packaging.

Output:
  src-tauri/sidecar/freehive-backend(.exe)
"""

from __future__ import annotations

import os
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
        "--onedir",
        "--name",
        APP_NAME,
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR),
        "--specpath",
        str(SPEC_DIR),
        # -- backend core modules --
        "--hidden-import", "backend.router",
        "--hidden-import", "backend.setup_router",
        "--hidden-import", "backend.compat_router",
        "--hidden-import", "backend.session_manager",
        "--hidden-import", "backend.arena_manager",
        "--hidden-import", "backend.model_discovery",
        "--hidden-import", "backend.conversation_manager",
        "--hidden-import", "backend.account_store",
        "--hidden-import", "backend.feature_flags",
        "--hidden-import", "backend.thinking",
        "--hidden-import", "backend.usage_fetcher",
        # -- adapters --
        "--hidden-import", "backend.adapters.claude_direct_adapter",
        "--hidden-import", "backend.adapters.claude_adapter",
        "--hidden-import", "backend.adapters.chatgpt_direct_adapter",
        "--hidden-import", "backend.adapters.chatgpt_adapter",
        "--hidden-import", "backend.adapters.gemini_direct_adapter",
        "--hidden-import", "backend.adapters.arena_bridge_adapter",
        "--hidden-import", "backend.adapters.arena_steel_adapter",
        "--hidden-import", "backend.adapters.arena_playwright_adapter",
        # -- services --
        "--hidden-import", "backend.services.arena_bridge_client",
        "--hidden-import", "backend.services.arena_model_health",
        "--hidden-import", "backend.services.arena_model_cache",
        "--hidden-import", "backend.services.arena_bridge_transport",
        "--hidden-import", "backend.services.steel_orchestrator",
        "--hidden-import", "backend.services.stealth_orchestrator",
        "--hidden-import", "backend.services.chrome_launcher",
        # -- shared --
        "--hidden-import", "shared.arena_bridge_protocol",
        # -- third-party (runtime-resolved) --
        "--hidden-import", "playwright",
        "--hidden-import", "playwright.async_api",
        "--hidden-import", "playwright._impl._api_types",
        "--hidden-import", "cloakbrowser",
        "--collect-submodules", "playwright",
        "--collect-submodules", "cloakbrowser",
        # -- bundle arena extension + native host so chrome_launcher.py can find them --
        "--add-data", f"{REPO_ROOT / 'arena_extension'}{os.pathsep}arena_extension",
        "--add-data", f"{REPO_ROOT / 'native_host'}{os.pathsep}native_host",
        str(ENTRYPOINT),
    ]
    run(cmd)

    exe_name = f"{APP_NAME}.exe" if sys.platform.startswith("win") else APP_NAME
    built_dir = DIST_DIR / APP_NAME
    if not built_dir.exists() or not (built_dir / exe_name).exists():
        print(f"Build succeeded but output not found at: {built_dir / exe_name}", file=sys.stderr)
        return 1

    # Remove stale onefile binary or old onedir output
    for stale in [SIDECAR_DIR / exe_name, SIDECAR_DIR / APP_NAME]:
        if stale.exists():
            if stale.is_dir():
                shutil.rmtree(stale)
            else:
                stale.unlink()

    output_dir = SIDECAR_DIR / APP_NAME
    shutil.copytree(built_dir, output_dir)
    print(f"Sidecar ready: {output_dir}")

    # --- Copy Arena Chrome extension into Tauri resources ---
    ext_src = REPO_ROOT / "arena_extension"
    ext_dst = REPO_ROOT / "src-tauri" / "extensions" / "arena"
    if ext_src.exists():
        if ext_dst.exists():
            shutil.rmtree(ext_dst)
        shutil.copytree(ext_src, ext_dst)
        print(f"Arena extension bundled: {ext_dst}")
    else:
        print("Warning: arena_extension/ not found — extension not bundled", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
