"""
cli_introspection.py — FreeHive Resilience

Extracts hardcoded values (Client IDs, User-Agents, headers, billing markers)
from installed CLI binaries so FreeHive auto-updates when CLIs release new versions.

Strategy:
  1. Find CLI via npm root -g → node_modules/<package>
  2. Read package.json for version
  3. Regex-search dist/build JS for target patterns
  4. Cache results in ~/.freehive/cli_metadata.json (24h TTL)
  5. Fall back to hardcoded defaults on extraction failure
"""

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_FILE = Path.home() / ".freehive" / "cli_metadata.json"
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours

# Hardcoded defaults — used when CLI not installed or extraction fails.
# These must be kept manually updated as a last resort.
_DEFAULTS = {
    "claude": {
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "version": "2.1.92",
        "user_agent": "claude-cli/2.1.92 (external, cli)",
        "beta_header": "claude-code-20250219,oauth-2025-04-20",
        "billing_marker": "x-anthropic-billing-header: cc_version=2.1.92; cc_entrypoint=cli; cch=cli;",
        "identity": "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
        "token_url": "https://platform.claude.com/v1/oauth/token",
        "messages_url": "https://api.anthropic.com/v1/messages?beta=true",
    },
    "codex": {
        "originator": "codex_cli_rs",
        "ws_url": "wss://chatgpt.com/backend-api/codex/responses",
        "beta_header": "responses_websockets=2026-02-06",
        "version": "0.1.0",
    },
    "gemini": {
        "client_id": "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com",
        "client_secret": "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",
        "version": "0.38.0",
        "endpoint_base": "https://cloudcode-pa.googleapis.com/v1internal",
    },
}


def _load_cache() -> dict:
    try:
        if CACHE_FILE.exists():
            return json.loads(CACHE_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_cache(data: dict):
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        logger.debug("[cli_introspection] Cache write failed: %s", exc)


def _is_fresh(cache: dict, provider: str) -> bool:
    ts = cache.get(provider, {}).get("_extracted_at", 0)
    return (time.time() - ts) < CACHE_TTL_SECONDS


def _find_npm_package(package_name: str) -> Path | None:
    """Find an npm global package directory."""
    try:
        result = subprocess.run(
            ["npm", "root", "-g"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            root = Path(result.stdout.strip())
            pkg_dir = root / package_name
            if pkg_dir.is_dir():
                return pkg_dir
    except Exception:
        pass

    # Fallback: common locations
    home = Path.home()
    for candidate in [
        home / ".nvm" / "versions" / "node",  # nvm installs
        Path("/usr/lib/node_modules"),
        Path("/usr/local/lib/node_modules"),
        home / ".npm-global" / "lib" / "node_modules",
    ]:
        if candidate.name == "node":
            # nvm: search latest version dir
            try:
                versions = sorted(candidate.iterdir(), reverse=True)
                for v in versions:
                    pkg = v / "lib" / "node_modules" / package_name
                    if pkg.is_dir():
                        return pkg
            except Exception:
                continue
        else:
            pkg = candidate / package_name
            if pkg.is_dir():
                return pkg
    return None


def _read_package_version(pkg_dir: Path) -> str | None:
    try:
        pjson = json.loads((pkg_dir / "package.json").read_text())
        return pjson.get("version")
    except Exception:
        return None


def _search_js_files(pkg_dir: Path, patterns: list[tuple[str, str]], max_files: int = 50) -> dict:
    """Search JS bundle files for regex patterns. Returns {key: first_match}."""
    results = {}
    js_files = []

    for subdir in ["dist", "build", "lib", "out", "."]:
        d = pkg_dir / subdir
        if d.is_dir():
            for f in d.rglob("*.js"):
                js_files.append(f)
                if len(js_files) >= max_files:
                    break
        if len(js_files) >= max_files:
            break

    # Also check .cjs files
    for subdir in ["dist", "build", "lib", "out"]:
        d = pkg_dir / subdir
        if d.is_dir():
            for f in d.rglob("*.cjs"):
                js_files.append(f)
                if len(js_files) >= max_files * 2:
                    break

    for f in js_files:
        try:
            content = f.read_text(errors="ignore")
        except Exception:
            continue
        for key, pattern in patterns:
            if key in results:
                continue
            match = re.search(pattern, content)
            if match:
                results[key] = match.group(1)

    return results


# ---------------------------------------------------------------------------
# Claude CLI extraction
# ---------------------------------------------------------------------------

def _extract_claude() -> dict:
    """Extract values from installed Claude Code CLI."""
    for pkg_name in ["@anthropic-ai/claude-code", "@gitlawb/openclaude"]:
        pkg_dir = _find_npm_package(pkg_name)
        if pkg_dir:
            break
    else:
        logger.info("[cli_introspection] No Claude CLI found, using defaults")
        return dict(_DEFAULTS["claude"])

    version = _read_package_version(pkg_dir) or _DEFAULTS["claude"]["version"]

    # Search JS bundles for key values
    patterns = [
        ("client_id", r'client_id["\s:=]+["\']([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})["\']'),
        ("beta_header", r'anthropic-beta["\s:=]+["\'](claude-code-\d{8}[^"\']*)["\']'),
        ("identity", r'(You are a Claude agent[^"\'\\]{0,100})'),
        ("token_url", r'(https://platform\.claude\.com/v1/oauth/token)'),
        ("messages_url", r'(https://api\.anthropic\.com/v1/messages[^"\'\\]*)'),
        ("billing_prefix", r'(x-anthropic-billing-header:\s*cc_version=[^;"]+;[^"\'\\]*)'),
    ]
    extracted = _search_js_files(pkg_dir, patterns)

    result = dict(_DEFAULTS["claude"])
    result["version"] = version
    result["user_agent"] = f"claude-cli/{version} (external, cli)"

    if "client_id" in extracted:
        result["client_id"] = extracted["client_id"]
    if "beta_header" in extracted:
        result["beta_header"] = extracted["beta_header"]
    if "identity" in extracted:
        result["identity"] = extracted["identity"]
    if "token_url" in extracted:
        result["token_url"] = extracted["token_url"]
    if "messages_url" in extracted:
        result["messages_url"] = extracted["messages_url"]
    if "billing_prefix" in extracted:
        # Reconstruct billing marker with extracted version
        result["billing_marker"] = f"x-anthropic-billing-header: cc_version={version}; cc_entrypoint=cli; cch=cli;"

    # Always update version in billing marker
    result["billing_marker"] = result["billing_marker"].replace(
        f"cc_version={_DEFAULTS['claude']['version']}",
        f"cc_version={version}",
    )

    logger.info(
        "[cli_introspection] Claude CLI v%s extracted (client_id=%s...)",
        version, result["client_id"][:12],
    )
    return result


# ---------------------------------------------------------------------------
# Codex CLI extraction
# ---------------------------------------------------------------------------

def _extract_codex() -> dict:
    """Extract values from installed Codex CLI."""
    pkg_dir = _find_npm_package("@openai/codex")
    if not pkg_dir:
        logger.info("[cli_introspection] No Codex CLI found, using defaults")
        return dict(_DEFAULTS["codex"])

    version = _read_package_version(pkg_dir) or _DEFAULTS["codex"]["version"]

    patterns = [
        ("originator", r'originator["\s:=]+["\']([a-z_]+)["\']'),
        ("ws_url", r'(wss://[a-z0-9.-]+/backend-api/codex/[^"\'\\]+)'),
        ("beta_header", r'OpenAI-Beta["\s:=]+["\'](responses_websockets=[^"\']+)["\']'),
    ]
    extracted = _search_js_files(pkg_dir, patterns)

    result = dict(_DEFAULTS["codex"])
    result["version"] = version

    if "originator" in extracted:
        result["originator"] = extracted["originator"]
    if "ws_url" in extracted:
        result["ws_url"] = extracted["ws_url"]
    if "beta_header" in extracted:
        result["beta_header"] = extracted["beta_header"]

    logger.info(
        "[cli_introspection] Codex CLI v%s extracted (originator=%s)",
        version, result["originator"],
    )
    return result


# ---------------------------------------------------------------------------
# Gemini CLI extraction
# ---------------------------------------------------------------------------

def _extract_gemini() -> dict:
    """Extract values from installed Gemini CLI."""
    pkg_dir = _find_npm_package("@google/gemini-cli")
    if not pkg_dir:
        logger.info("[cli_introspection] No Gemini CLI found, using defaults")
        return dict(_DEFAULTS["gemini"])

    version = _read_package_version(pkg_dir) or _DEFAULTS["gemini"]["version"]

    patterns = [
        ("client_id", r'(\d{12}-[a-z0-9]+\.apps\.googleusercontent\.com)'),
        ("client_secret", r'(GOCSPX-[A-Za-z0-9_-]{20,})'),
        ("endpoint_base", r'(https://cloudcode-pa\.googleapis\.com/v1[a-z]*)'),
    ]
    extracted = _search_js_files(pkg_dir, patterns)

    result = dict(_DEFAULTS["gemini"])
    result["version"] = version

    if "client_id" in extracted:
        result["client_id"] = extracted["client_id"]
    if "client_secret" in extracted:
        result["client_secret"] = extracted["client_secret"]
    if "endpoint_base" in extracted:
        result["endpoint_base"] = extracted["endpoint_base"]

    logger.info(
        "[cli_introspection] Gemini CLI v%s extracted (client_id=%s...)",
        version, result["client_id"][:12],
    )
    return result


# ---------------------------------------------------------------------------
# Public API — cached getters
# ---------------------------------------------------------------------------

def get_claude_config() -> dict:
    """Get Claude CLI config values. Cached with 24h TTL."""
    cache = _load_cache()
    if _is_fresh(cache, "claude"):
        return cache["claude"]

    extracted = _extract_claude()
    extracted["_extracted_at"] = time.time()
    cache["claude"] = extracted
    _save_cache(cache)
    return extracted


def get_codex_config() -> dict:
    """Get Codex CLI config values. Cached with 24h TTL."""
    cache = _load_cache()
    if _is_fresh(cache, "codex"):
        return cache["codex"]

    extracted = _extract_codex()
    extracted["_extracted_at"] = time.time()
    cache["codex"] = extracted
    _save_cache(cache)
    return extracted


def get_gemini_config() -> dict:
    """Get Gemini CLI config values. Cached with 24h TTL."""
    cache = _load_cache()
    if _is_fresh(cache, "gemini"):
        return cache["gemini"]

    extracted = _extract_gemini()
    extracted["_extracted_at"] = time.time()
    cache["gemini"] = extracted
    _save_cache(cache)
    return extracted


def invalidate_cache(provider: str | None = None):
    """Force re-extraction on next call."""
    cache = _load_cache()
    if provider:
        cache.pop(provider, None)
    else:
        cache.clear()
    _save_cache(cache)


def get_all_configs() -> dict:
    """Get all provider configs (for health status display)."""
    return {
        "claude": get_claude_config(),
        "codex": get_codex_config(),
        "gemini": get_gemini_config(),
    }
