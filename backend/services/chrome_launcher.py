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
from pathlib import Path

logger = logging.getLogger(__name__)

# Extension dir relative to project root
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


def detect_extension_id() -> str | None:
    """Try to detect the FreeHive extension ID from Chrome's preferences."""
    prefs_file = Path.home() / ".config" / "google-chrome" / "Default" / "Preferences"
    if platform.system() == "Darwin":
        prefs_file = Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "Default" / "Preferences"
    if platform.system() == "Windows":
        prefs_file = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default" / "Preferences"

    if not prefs_file.exists():
        return None

    try:
        import json
        with open(prefs_file, "r") as f:
            prefs = json.load(f)
        exts = prefs.get("extensions", {}).get("settings", {})
        for ext_id, info in exts.items():
            path = str(info.get("path", ""))
            manifest = info.get("manifest", {})
            name = manifest.get("name", "")
            if "arena_extension" in path or "FreeHive Arena" in name:
                return ext_id
    except Exception as exc:
        logger.debug("[ChromeLauncher] Failed to read Chrome prefs: %s", exc)
    return None


def patch_native_host_manifest(extension_id: str) -> bool:
    """Update the native host manifest with the correct extension ID."""
    manifest_path = _get_native_host_manifest_path()
    if not manifest_path or not manifest_path.exists():
        return False
    try:
        import json
        with open(manifest_path, "r") as f:
            data = json.load(f)
        origin = f"chrome-extension://{extension_id}/"
        if data.get("allowed_origins") == [origin]:
            return True  # Already correct
        data["allowed_origins"] = [origin]
        with open(manifest_path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        logger.info("[ChromeLauncher] Patched native host manifest with ID: %s", extension_id)
        return True
    except Exception as exc:
        logger.warning("[ChromeLauncher] Failed to patch manifest: %s", exc)
        return False


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


def get_chrome_status() -> dict:
    """Return current state of Chrome + extension setup."""
    chrome_binary = _find_chrome_binary()
    return {
        "chrome_installed": chrome_binary is not None,
        "chrome_path": chrome_binary,
        "chrome_running": _is_chrome_running() if chrome_binary else False,
        "extension_dir_exists": EXTENSION_DIR.exists(),
        "native_host_installed": _is_native_host_installed(),
    }
