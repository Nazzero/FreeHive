"""
chrome_launcher.py — Launch Chrome with the Arena extension auto-loaded.

Handles:
  - Finding Chrome binary across platforms
  - Loading the extension via --load-extension
  - Using the user's default Chrome profile (keeps cookies, history, reCAPTCHA score)
  - Detecting if Chrome is already running and injecting via chrome:// navigation
  - Opening arena.ai tab if not already open
"""

import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Extension dir — handle both dev and PyInstaller-bundled layouts
if getattr(sys, "frozen", False):
    # PyInstaller onedir: data lives under sys._MEIPASS
    _BUNDLE_ROOT = Path(sys._MEIPASS)
    _PROJECT_ROOT = _BUNDLE_ROOT
    EXTENSION_DIR = _BUNDLE_ROOT / "arena_extension"
else:
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    EXTENSION_DIR = _PROJECT_ROOT / "arena_extension"

# Where native host manifest should live
NATIVE_HOST_NAME = "com.freehive.arena_bridge"


def _find_chrome_binary() -> str | None:
    """Find Chrome/Chromium binary on the system."""
    system = platform.system()

    if system == "Linux":
        candidates = [
            "google-chrome", "google-chrome-stable", "google-chrome-beta",
            "chromium-browser", "chromium",
        ]
        for name in candidates:
            path = shutil.which(name)
            if path:
                return path
        # Direct paths
        for p in ["/usr/bin/google-chrome", "/opt/google/chrome/chrome"]:
            if os.path.isfile(p):
                return p

    elif system == "Darwin":
        mac_paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
        for p in mac_paths:
            if os.path.isfile(p):
                return p
        path = shutil.which("google-chrome")
        if path:
            return path

    elif system == "Windows":
        win_paths = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
        for p in win_paths:
            if os.path.isfile(p):
                return p
        path = shutil.which("chrome")
        if path:
            return path

    return None


def _is_chrome_running() -> bool:
    """Check if any Chrome process is running."""
    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe"],
                capture_output=True, text=True, timeout=5,
            )
            return "chrome.exe" in result.stdout.lower()
        else:
            result = subprocess.run(
                ["pgrep", "-f", "chrome"],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
    except Exception:
        return False


def _is_native_host_installed() -> bool:
    """Check if the native messaging host manifest is installed."""
    system = platform.system()
    if system == "Linux":
        manifest = Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts" / f"{NATIVE_HOST_NAME}.json"
    elif system == "Darwin":
        manifest = Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts" / f"{NATIVE_HOST_NAME}.json"
    elif system == "Windows":
        # On Windows, check registry instead. For now check common path.
        manifest = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "NativeMessagingHosts" / f"{NATIVE_HOST_NAME}.json"
    else:
        return False
    return manifest.exists()


def _get_native_host_manifest_path() -> Path | None:
    """Return the native host manifest path for this platform."""
    system = platform.system()
    if system == "Linux":
        return Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts" / f"{NATIVE_HOST_NAME}.json"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts" / f"{NATIVE_HOST_NAME}.json"
    if system == "Windows":
        return Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "NativeMessagingHosts" / f"{NATIVE_HOST_NAME}.json"
    return None


KNOWN_EXTENSION_ID = "jkclihigpeefogblifghhpojgkbheked"


def detect_extension_id() -> str | None:
    """Return the published Chrome Web Store extension ID."""
    return KNOWN_EXTENSION_ID


def patch_native_host_manifest(extension_id: str) -> bool:
    """Add extension_id to native host manifest alongside the Web Store ID."""
    manifest_path = _get_native_host_manifest_path()
    if not manifest_path or not manifest_path.exists():
        return False
    try:
        import json
        with open(manifest_path, "r") as f:
            data = json.load(f)
        origin = f"chrome-extension://{extension_id}/"
        known_origin = f"chrome-extension://{KNOWN_EXTENSION_ID}/"
        desired = {known_origin, origin}
        current = set(data.get("allowed_origins", []))
        if desired == current:
            return True
        data["allowed_origins"] = sorted(desired)
        with open(manifest_path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        logger.info("[ChromeLauncher] Patched native host manifest with IDs: %s", sorted(desired))
        return True
    except Exception as exc:
        logger.warning("[ChromeLauncher] Failed to patch manifest: %s", exc)
        return False


def get_active_extension_ids() -> dict:
    """Return which extension IDs are registered in the native host manifest."""
    manifest_path = _get_native_host_manifest_path()
    if not manifest_path or not manifest_path.exists():
        return {"web_store_id": KNOWN_EXTENSION_ID, "unpacked_id": None, "allowed_origins": []}
    try:
        import json
        with open(manifest_path, "r") as f:
            data = json.load(f)
        origins = data.get("allowed_origins", [])
        ids = [o.replace("chrome-extension://", "").rstrip("/") for o in origins]
        unpacked = None
        for ext_id in ids:
            if ext_id != KNOWN_EXTENSION_ID:
                unpacked = ext_id
                break
        return {"web_store_id": KNOWN_EXTENSION_ID, "unpacked_id": unpacked, "allowed_origins": origins}
    except Exception:
        return {"web_store_id": KNOWN_EXTENSION_ID, "unpacked_id": None, "allowed_origins": []}


def install_native_host() -> dict:
    """Install the native messaging host manifest."""
    script = _PROJECT_ROOT / "native_host" / "install_host.sh"
    if platform.system() == "Windows":
        script = _PROJECT_ROOT / "native_host" / "install_host_windows.ps1"
    elif platform.system() == "Darwin":
        script = _PROJECT_ROOT / "native_host" / "install_host_macos.sh"

    if not script.exists():
        return {"success": False, "error": f"Install script not found: {script}"}

    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                capture_output=True, text=True, timeout=30,
            )
        else:
            result = subprocess.run(
                ["bash", str(script)],
                capture_output=True, text=True, timeout=30,
                cwd=str(script.parent),
            )
        if result.returncode == 0:
            return {"success": True, "output": result.stdout.strip()}
        return {"success": False, "error": result.stderr.strip() or result.stdout.strip()}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def launch_chrome_with_extension(url: str = "https://arena.ai/text/direct") -> dict:
    """
    Launch Chrome with the FreeHive Arena extension loaded.

    If Chrome is already running: opens arena.ai in a new tab (extension
    must already be loaded — can't inject into running Chrome).

    If Chrome is not running: launches with --load-extension flag so extension
    auto-loads on startup.

    Returns status dict with what happened.
    """
    chrome = _find_chrome_binary()
    if not chrome:
        return {
            "success": False,
            "error": "Chrome not found. Install Google Chrome and try again.",
        }

    ext_dir = str(EXTENSION_DIR.resolve())
    if not EXTENSION_DIR.exists() or not (EXTENSION_DIR / "manifest.json").exists():
        return {
            "success": False,
            "error": f"Extension not found at {ext_dir}",
        }

    # Ensure native host is installed
    if not _is_native_host_installed():
        logger.info("[ChromeLauncher] Native host not installed, installing now...")
        host_result = install_native_host()
        if not host_result["success"]:
            logger.warning("[ChromeLauncher] Native host install failed: %s", host_result.get("error"))

    chrome_running = _is_chrome_running()

    if chrome_running:
        # Chrome already running — can't add --load-extension to existing process.
        # Open arena.ai tab. If extension was previously loaded, it will inject.
        # If not, user needs to load extension once manually or restart Chrome.
        try:
            subprocess.Popen(
                [chrome, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {
                "success": True,
                "action": "opened_tab",
                "chrome_was_running": True,
                "message": (
                    "Opened arena.ai in Chrome. "
                    "If the extension isn't loaded yet, go to chrome://extensions, "
                    "enable Developer mode, and click 'Load unpacked' → select arena_extension/. "
                    "This is a one-time step."
                ),
            }
        except Exception as exc:
            return {"success": False, "error": f"Failed to open tab: {exc}"}

    # Chrome not running — launch fresh with extension pre-loaded
    args = [
        chrome,
        f"--load-extension={ext_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]

    try:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("[ChromeLauncher] Launched Chrome with extension: %s", ext_dir)
        return {
            "success": True,
            "action": "launched_chrome",
            "chrome_was_running": False,
            "message": (
                "Chrome launched with Arena extension loaded. "
                "Log in to arena.ai if needed, then pin the tab."
            ),
        }
    except Exception as exc:
        return {"success": False, "error": f"Failed to launch Chrome: {exc}"}


def get_extension_path() -> dict:
    """Return the resolved path to the bundled Arena Chrome extension folder."""
    ext_dir = EXTENSION_DIR.resolve()
    exists = ext_dir.exists() and (ext_dir / "manifest.json").exists()
    return {
        "path": str(ext_dir),
        "exists": exists,
    }


def open_extension_folder() -> dict:
    """Open the Arena extension folder in the OS file manager."""
    ext_dir = EXTENSION_DIR.resolve()
    if not ext_dir.exists():
        return {"success": False, "error": f"Extension folder not found: {ext_dir}"}
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(str(ext_dir))
        elif system == "Darwin":
            subprocess.Popen(["open", str(ext_dir)])
        else:
            subprocess.Popen(["xdg-open", str(ext_dir)])
        return {"success": True, "path": str(ext_dir)}
    except Exception as exc:
        return {"success": False, "error": str(exc), "path": str(ext_dir)}


def get_chrome_status() -> dict:
    """Return current state of Chrome + extension setup."""
    from backend.services.arena_bridge_transport import is_bridge_available

    chrome_binary = _find_chrome_binary()
    ids = get_active_extension_ids()
    return {
        "chrome_installed": chrome_binary is not None,
        "chrome_path": chrome_binary,
        "chrome_running": _is_chrome_running() if chrome_binary else False,
        "extension_dir_exists": EXTENSION_DIR.exists(),
        "extension_path": str(EXTENSION_DIR.resolve()),
        "native_host_installed": _is_native_host_installed(),
        "bridge_connected": is_bridge_available(timeout_s=1.0),
        "web_store_id": ids["web_store_id"],
        "unpacked_id": ids["unpacked_id"],
    }
