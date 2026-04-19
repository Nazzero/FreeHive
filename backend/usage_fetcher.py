"""
usage_fetcher.py — Pull remaining usage from each provider.

Primary source: OmniRoute's quota_snapshots DB (~/.omniroute/storage.sqlite)
  which tracks remaining % + reset times for all providers.
Fallback: direct API calls to ChatGPT and Gemini if OmniRoute data is stale.
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

CLAUDE_CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"
GEMINI_CREDENTIALS_FILE = Path.home() / ".gemini" / "oauth_creds.json"
OMNIROUTE_DB = Path.home() / ".omniroute" / "storage.sqlite"
GEMINI_ACCOUNTS_FILE = Path.home() / ".gemini" / "google_accounts.json"

# Provider name mapping: OmniRoute uses "codex" for ChatGPT, "gemini-cli" for Gemini
_OR_PROVIDER_MAP = {"claude": "claude", "chatgpt": "codex", "gemini": "gemini-cli"}


def _make_result(provider: str) -> dict:
    return {
        "provider": provider,
        "status": "disconnected",
        "tier": "unknown",
        "email": None,
        "quotas": [],
        "error": None,
    }


def _get_email(provider: str) -> str | None:
    """Extract account email for a provider."""
    if provider == "chatgpt":
        try:
            import base64
            creds = json.loads(CODEX_AUTH_FILE.read_text())
            id_token = creds.get("tokens", {}).get("id_token", "")
            if id_token:
                payload = id_token.split(".")[1] + "=="
                data = json.loads(base64.urlsafe_b64decode(payload))
                return data.get("email")
        except Exception:
            pass
    elif provider == "gemini":
        try:
            accts = json.loads(GEMINI_ACCOUNTS_FILE.read_text())
            return accts.get("active")
        except Exception:
            pass
    elif provider == "claude":
        # Claude CLI doesn't store email in credentials.
        # Try reading from setup_router's auth check.
        try:
            creds = json.loads(CLAUDE_CREDENTIALS_FILE.read_text())
            oauth = creds.get("claudeAiOauth", {})
            return oauth.get("email") or oauth.get("accountEmail")
        except Exception:
            pass
    return None


def _reset_label(seconds: int) -> str:
    if seconds <= 0:
        return "Resetting..."
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h > 24:
        d = h // 24
        return f"Resets in {d}d {h % 24}h"
    if h > 0:
        return f"Resets in {h}h {m}m"
    return f"Resets in {m}m"


def _reset_label_from_iso(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        reset_dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        secs = int((reset_dt - datetime.now(timezone.utc)).total_seconds())
        return _reset_label(secs)
    except Exception:
        return ""


def _read_omniroute_snapshots(or_provider: str) -> list[dict]:
    """Read latest quota snapshots per window from OmniRoute DB."""
    if not OMNIROUTE_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(OMNIROUTE_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT window_key, remaining_percentage, is_exhausted, next_reset_at
            FROM quota_snapshots
            WHERE provider = ?
              AND id IN (SELECT MAX(id) FROM quota_snapshots WHERE provider = ? GROUP BY window_key)
            ORDER BY window_key
        """, (or_provider, or_provider)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning("[UsageFetcher] OmniRoute DB read failed: %s", exc)
        return []


def _get_tier(provider: str) -> str:
    """Read tier from credential files."""
    if provider == "claude":
        try:
            creds = json.loads(CLAUDE_CREDENTIALS_FILE.read_text())
            return creds.get("claudeAiOauth", {}).get("subscriptionType", "unknown")
        except Exception:
            return "unknown"
    if provider == "chatgpt":
        # Will be overwritten by live API if available
        return "unknown"
    if provider == "gemini":
        return "free"
    return "unknown"


def _get_status(provider: str) -> str:
    """Check if provider is connected."""
    if provider == "claude":
        if not CLAUDE_CREDENTIALS_FILE.exists():
            return "disconnected"
        try:
            creds = json.loads(CLAUDE_CREDENTIALS_FILE.read_text())
            oauth = creds.get("claudeAiOauth", {})
            expires_at = oauth.get("expiresAt")
            if expires_at and time.time() * 1000 >= expires_at:
                return "expired"
            return "connected" if oauth.get("accessToken") else "disconnected"
        except Exception:
            return "error"
    if provider == "chatgpt":
        if not CODEX_AUTH_FILE.exists():
            return "disconnected"
        try:
            creds = json.loads(CODEX_AUTH_FILE.read_text())
            return "connected" if creds.get("tokens", {}).get("access_token") else "disconnected"
        except Exception:
            return "error"
    if provider == "gemini":
        if not GEMINI_CREDENTIALS_FILE.exists():
            return "disconnected"
        try:
            creds = json.loads(GEMINI_CREDENTIALS_FILE.read_text())
            expiry_ms = creds.get("expiry_date", 0)
            if (time.time() * 1000) >= expiry_ms:
                return "expired"
            return "connected" if creds.get("access_token") else "disconnected"
        except Exception:
            return "error"
    return "disconnected"


async def fetch_claude_usage() -> dict:
    result = _make_result("claude")
    result["tier"] = _get_tier("claude")
    result["status"] = _get_status("claude")
    result["email"] = _get_email("claude")

    # Read from OmniRoute snapshots
    snapshots = _read_omniroute_snapshots("claude")
    for snap in snapshots:
        remaining = snap.get("remaining_percentage", 0)
        reset_at = snap.get("next_reset_at")
        result["quotas"].append({
            "label": snap.get("window_key", "unknown"),
            "remaining_pct": round(remaining, 1),
            "reset_label": _reset_label_from_iso(reset_at) if reset_at else "",
            "limit_reached": snap.get("is_exhausted", 0) == 1,
        })

    if not result["quotas"] and result["status"] == "disconnected":
        result["error"] = "Not authenticated"

    return result


async def fetch_chatgpt_usage() -> dict:
    result = _make_result("chatgpt")
    result["status"] = _get_status("chatgpt")
    result["email"] = _get_email("chatgpt")

    if result["status"] == "disconnected":
        result["error"] = "Not authenticated"
        return result

    # Try live API first for fresh data
    try:
        creds = json.loads(CODEX_AUTH_FILE.read_text())
        tokens = creds.get("tokens", {})
        token = tokens.get("access_token", "")
        account_id = tokens.get("account_id", "")

        headers = {"Authorization": f"Bearer {token}", "originator": "codex_cli_rs"}
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://chatgpt.com/backend-api/wham/usage", headers=headers)

        if resp.status_code == 200:
            data = resp.json()
            result["tier"] = data.get("plan_type", "unknown")

            rl = data.get("rate_limit", {})
            pw = rl.get("primary_window")
            if pw:
                used_pct = pw.get("used_percent", 0)
                reset_secs = pw.get("reset_after_seconds", 0)
                window_secs = pw.get("limit_window_seconds", 0)
                window_h = window_secs // 3600
                label = "Weekly" if window_h >= 168 else f"{window_h}h"
                result["quotas"].append({
                    "label": label,
                    "remaining_pct": round(100 - used_pct, 1),
                    "reset_label": _reset_label(reset_secs),
                    "limit_reached": rl.get("limit_reached", False),
                })

            sw = rl.get("secondary_window")
            if sw:
                used_pct = sw.get("used_percent", 0)
                reset_secs = sw.get("reset_after_seconds", 0)
                window_secs = sw.get("limit_window_seconds", 0)
                window_h = window_secs // 3600
                result["quotas"].append({
                    "label": f"{window_h}h" if window_h else "Burst",
                    "remaining_pct": round(100 - used_pct, 1),
                    "reset_label": _reset_label(reset_secs),
                    "limit_reached": False,
                })
            return result
        elif resp.status_code == 401:
            result["status"] = "expired"
            result["error"] = "Token expired"
    except Exception as exc:
        logger.warning("[UsageFetcher] ChatGPT live API failed, falling back to OmniRoute: %s", exc)

    # Fallback: OmniRoute snapshots
    if not result["quotas"]:
        snapshots = _read_omniroute_snapshots("codex")
        for snap in snapshots:
            remaining = snap.get("remaining_percentage", 0)
            reset_at = snap.get("next_reset_at")
            result["quotas"].append({
                "label": snap.get("window_key", "unknown"),
                "remaining_pct": round(remaining, 1),
                "reset_label": _reset_label_from_iso(reset_at) if reset_at else "",
                "limit_reached": snap.get("is_exhausted", 0) == 1,
            })

    return result


async def fetch_gemini_usage() -> dict:
    result = _make_result("gemini")
    result["status"] = _get_status("gemini")
    result["tier"] = _get_tier("gemini")
    result["email"] = _get_email("gemini")

    if result["status"] == "disconnected":
        result["error"] = "Not authenticated"
        return result

    # Try live API first
    try:
        creds = json.loads(GEMINI_CREDENTIALS_FILE.read_text())
        token = creds.get("access_token", "")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-goog-api-client": "gl-node/22.22.2",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
                headers=headers,
                json={},
            )
        if resp.status_code == 200:
            for bucket in resp.json().get("buckets", []):
                model_id = bucket.get("modelId", "")
                if not model_id:
                    continue
                remaining_frac = bucket.get("remainingFraction", 1)
                remaining_pct = round(remaining_frac * 100, 1)
                reset_time = bucket.get("resetTime")
                result["quotas"].append({
                    "label": model_id,
                    "remaining_pct": remaining_pct,
                    "reset_label": _reset_label_from_iso(reset_time) if reset_time else "",
                    "limit_reached": remaining_pct == 0,
                })
    except Exception as exc:
        logger.warning("[UsageFetcher] Gemini live API failed: %s", exc)

    # Supplement with OmniRoute data for models not in live response
    if OMNIROUTE_DB.exists():
        live_models = {q["label"] for q in result["quotas"]}
        snapshots = _read_omniroute_snapshots("gemini-cli")
        for snap in snapshots:
            window = snap.get("window_key", "")
            if window not in live_models:
                remaining = snap.get("remaining_percentage", 0)
                reset_at = snap.get("next_reset_at")
                result["quotas"].append({
                    "label": window,
                    "remaining_pct": round(remaining, 1),
                    "reset_label": _reset_label_from_iso(reset_at) if reset_at else "",
                    "limit_reached": snap.get("is_exhausted", 0) == 1,
                })

    return result


# ---------------------------------------------------------------------------
# Arena — Context Window Tracking
# ---------------------------------------------------------------------------

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
CONTEXT_CACHE_PATH = Path.home() / ".freehive" / "litellm_context_windows.json"
CONTEXT_CACHE_MAX_AGE = 86400  # 24h

CHARS_PER_TOKEN = 4

FAMILY_CONTEXT_FALLBACK = {
    "gpt": 128_000,
    "o1": 200_000, "o3": 200_000, "o4": 200_000,
    "claude": 200_000,
    "gemini": 1_048_576,
    "gemma": 131_072,
    "deepseek": 128_000,
    "grok": 131_072,
    "qwen": 131_072,
    "mistral": 128_000,
    "glm": 128_000,
    "kimi": 128_000,
    "minimax": 128_000,
    "llama": 131_072,
    "codex": 272_000,
}
DEFAULT_CONTEXT = 128_000

_context_map: dict[str, int] | None = None
_context_map_loaded_at: float = 0


def _load_context_map() -> dict[str, int]:
    """Build bare-model-name -> max_input_tokens map from LiteLLM data."""
    global _context_map, _context_map_loaded_at

    now = time.time()
    if _context_map is not None and (now - _context_map_loaded_at) < CONTEXT_CACHE_MAX_AGE:
        return _context_map

    raw = None

    # Try local cache first
    if CONTEXT_CACHE_PATH.exists():
        try:
            cache_stat = CONTEXT_CACHE_PATH.stat()
            if (now - cache_stat.st_mtime) < CONTEXT_CACHE_MAX_AGE:
                raw = json.loads(CONTEXT_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Fetch from GitHub if no fresh cache
    if raw is None:
        try:
            import urllib.request
            req = urllib.request.Request(LITELLM_URL, headers={"User-Agent": "freehive/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            # Save to cache
            CONTEXT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = CONTEXT_CACHE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(raw), encoding="utf-8")
            tmp.replace(CONTEXT_CACHE_PATH)
            logger.info("[ContextWindow] Fetched %d models from LiteLLM", len(raw))
        except Exception as exc:
            logger.warning("[ContextWindow] GitHub fetch failed: %s", exc)

    # Try stale cache as last resort
    if raw is None and CONTEXT_CACHE_PATH.exists():
        try:
            raw = json.loads(CONTEXT_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    if not raw:
        _context_map = {}
        _context_map_loaded_at = now
        return _context_map

    # Build bare-name map, keep highest context for each
    result_map: dict[str, int] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        ctx = entry.get("max_input_tokens") or entry.get("max_tokens")
        if not ctx or not isinstance(ctx, (int, float)):
            continue
        ctx = int(ctx)
        bare = key.split("/")[-1] if "/" in key else key
        if bare not in result_map or ctx > result_map[bare]:
            result_map[bare] = ctx

    _context_map = result_map
    _context_map_loaded_at = now
    logger.info("[ContextWindow] Built context map: %d bare models", len(result_map))
    return _context_map


def get_model_context_window(model_id: str) -> int:
    """Look up max input tokens for a model. Tries exact, prefix, family fallback."""
    cmap = _load_context_map()
    bare = model_id.removeprefix("arena/")

    # 1. Exact match
    if bare in cmap:
        return cmap[bare]

    # 2. Prefix match (longest wins)
    best_key, best_ctx = "", 0
    for k, v in cmap.items():
        if bare.startswith(k) and len(k) > len(best_key):
            best_key, best_ctx = k, v
    if best_ctx:
        return best_ctx

    # 3. Family fallback
    first_word = bare.split("-")[0].lower()
    if first_word in FAMILY_CONTEXT_FALLBACK:
        return FAMILY_CONTEXT_FALLBACK[first_word]

    return DEFAULT_CONTEXT


async def fetch_arena_context_usage(session_manager, conversation_manager) -> list[dict]:
    """Return context window usage for recent arena sessions."""
    from backend.conversation_manager import list_arena_sessions, get_session_char_count

    sessions = list_arena_sessions(limit=20)
    results = []
    for sess in sessions:
        sid = sess["id"]
        model = sess.get("model", "")
        total_chars, msg_count = get_session_char_count(sid)
        estimated_tokens = total_chars // CHARS_PER_TOKEN
        max_tokens = get_model_context_window(model)
        usage_pct = round(min(estimated_tokens / max_tokens * 100, 100), 1) if max_tokens else 0

        is_active = session_manager.get_session(sid) is not None if session_manager else False

        results.append({
            "session_id": sid,
            "model": model,
            "title": sess.get("title") or "Untitled",
            "message_count": msg_count,
            "total_chars": total_chars,
            "estimated_tokens": estimated_tokens,
            "max_tokens": max_tokens,
            "usage_pct": usage_pct,
            "is_active": is_active,
            "updated_at": sess.get("updated_at"),
        })

    return results
