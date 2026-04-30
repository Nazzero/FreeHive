"""
model_discovery.py — FreeHive v0.5.3

Dynamically discovers available models and account tier for each provider.
Results are cached in ~/.freehive/config.json so the frontend loads instantly.

Each provider has a discover_* function that:
  1. Calls the provider's real API to get available models
  2. Detects account tier (free/plus/pro/max/etc.)
  3. Returns a structured result
  4. Falls back gracefully if the API call fails
"""

import base64
import json
import logging
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

FREEHIVE_CONFIG_FILE = Path.home() / ".freehive" / "config.json"
CLAUDE_CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
GEMINI_CREDENTIALS_FILE = Path.home() / ".gemini" / "oauth_creds.json"
CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _read_config() -> dict:
    try:
        return json.loads(FREEHIVE_CONFIG_FILE.read_text())
    except Exception:
        return {}


def _write_config(data: dict):
    FREEHIVE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    FREEHIVE_CONFIG_FILE.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

async def discover_claude_models() -> dict:
    """
    Fetch Claude models from the Anthropic API using the OAuth token.
    Also reads account tier from the credentials file.
    Returns: { tier, models: [{id, display_name, note}] }
    """
    result = {"provider": "claude", "tier": "unknown", "models": [], "error": None}

    # Read credentials
    if not CLAUDE_CREDENTIALS_FILE.exists():
        result["error"] = "Not authenticated"
        return result

    try:
        creds = json.loads(CLAUDE_CREDENTIALS_FILE.read_text())
        oauth = creds.get("claudeAiOauth", {})
        token = oauth.get("accessToken", "")
        tier = oauth.get("subscriptionType", "unknown")
        result["tier"] = tier
    except Exception as exc:
        result["error"] = f"Failed to read credentials: {exc}"
        return result

    if not token:
        result["error"] = "No access token"
        return result

    # Fetch models from API
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "oauth-2025-04-20",
                },
            )
        if resp.status_code == 200:
            data = resp.json()
            models = []
            for m in data.get("data", []):
                mid = m.get("id", "")
                if not mid.startswith("claude-"):
                    continue
                models.append({
                    "id": mid,
                    "display_name": _claude_display_name(mid),
                    "note": _claude_note(mid, tier),
                })
            # Sort: haiku first, then sonnet, then opus
            order = {"haiku": 0, "sonnet": 1, "opus": 2}
            models.sort(key=lambda m: next((v for k, v in order.items() if k in m["id"]), 9))
            result["models"] = models
            logger.info("[ModelDiscovery] Claude: found %d models, tier=%s", len(models), tier)
            return result
        else:
            result["error"] = f"API returned {resp.status_code}"
    except Exception as exc:
        result["error"] = str(exc)
        logger.warning("[ModelDiscovery] Claude model fetch failed: %s", exc)

    # Fallback: known models by tier
    result["models"] = _claude_fallback_models(tier)
    return result


def _claude_display_name(model_id: str) -> str:
    # claude-haiku-4-5-20251001 → Haiku 4.5
    # claude-sonnet-4-6         → Sonnet 4.6
    # claude-3-haiku-20240307   → Haiku 3
    stripped = model_id.replace("claude-", "")
    parts = stripped.split("-")

    # Find the model family name (skip numeric prefixes like "3")
    family_idx = 0
    if parts[0].isdigit():
        family_idx = 1
    name = parts[family_idx].capitalize()

    # Collect version numbers immediately after the family name (not date stamps)
    version_parts = []
    for p in parts[family_idx + 1:]:
        if p.isdigit() and len(p) <= 2:
            version_parts.append(p)
        else:
            break  # stop at date stamp (8 digits) or non-numeric

    version = ".".join(version_parts)
    return f"{name} {version}".strip() if version else name


def _claude_note(model_id: str, tier: str) -> str:
    if "haiku" in model_id:
        return "fast"
    if "sonnet" in model_id:
        return "balanced"
    if "opus" in model_id:
        return "most capable" if tier not in ("free", "unknown") else "pro only"
    return ""


def _claude_fallback_models(tier: str) -> list[dict]:
    models = [
        {"id": "claude-haiku-4-5",  "display_name": "Haiku 4.5",  "note": "fast"},
        {"id": "claude-sonnet-4-5", "display_name": "Sonnet 4.5", "note": "balanced"},
    ]
    if tier not in ("free", "unknown", ""):
        models.append({"id": "claude-opus-4-5", "display_name": "Opus 4.5", "note": "most capable"})
    return models


# ---------------------------------------------------------------------------
# ChatGPT
# ---------------------------------------------------------------------------

CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models"


def _get_codex_client_version() -> str:
    """Read the installed Codex CLI version from its package.json."""
    try:
        import subprocess
        import os
        from pathlib import Path
        cmd = ["cmd", "/c", "npm root -g"] if os.name == "nt" else ["bash", "-l", "-c", "npm root -g"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            npm_root = result.stdout.strip()
            pkg_path = Path(npm_root) / "@openai" / "codex" / "package.json"
            if pkg_path.exists():
                data = json.loads(pkg_path.read_text(encoding="utf-8"))
                return data.get("version", "0.118.0")
    except Exception:
        pass
    return "0.118.0"


async def discover_chatgpt_models() -> dict:
    """
    Fetch ChatGPT / Codex models from chatgpt.com/backend-api/codex/models.
    This is the same endpoint the Codex CLI uses for its /model command.
    Returns all models where visibility != 'hide' (matching what the CLI shows).
    Falls back to a minimal known-good list on failure.
    """
    result = {"provider": "chatgpt", "tier": "unknown", "models": [], "error": None}

    if not CODEX_AUTH_FILE.exists():
        result["error"] = "Not authenticated"
        return result

    try:
        auth = json.loads(CODEX_AUTH_FILE.read_text())
        tokens = auth.get("tokens") or {}
        token = tokens.get("access_token", "")
        account_id = tokens.get("account_id", "")
    except Exception as exc:
        result["error"] = f"Failed to read auth: {exc}"
        return result

    if not token:
        result["error"] = "No access token"
        return result

    # Decode JWT for tier info (informational only)
    tier = _decode_chatgpt_tier(token)
    result["tier"] = tier

    # Fetch model list from the Codex CLI endpoint
    client_version = _get_codex_client_version()
    headers = {
        "Authorization": f"Bearer {token}",
        "originator": "codex_cli_rs",
    }
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                CODEX_MODELS_URL,
                params={"client_version": client_version},
                headers=headers,
            )

        if resp.status_code == 200:
            data = resp.json()
            models = []
            for m in data.get("models", []):
                slug = m.get("slug", "")
                if not slug:
                    continue
                visibility = m.get("visibility", "list")
                # Only show models the CLI shows (visibility=list); hide deprecated/hidden ones
                if visibility == "hide":
                    continue
                reasoning = [
                    lv["effort"]
                    for lv in m.get("supported_reasoning_levels", [])
                    if isinstance(lv, dict) and "effort" in lv
                ]
                models.append({
                    "id": slug,
                    "display_name": m.get("display_name") or slug,
                    "note": _chatgpt_model_note(m, tier),
                    "description": m.get("description", ""),
                    "context_window": m.get("context_window"),
                    "supported_in_api": m.get("supported_in_api"),
                    "priority": m.get("priority"),
                    "reasoning_levels": reasoning,
                    "default_reasoning_level": m.get("default_reasoning_level"),
                })
            result["models"] = models
            logger.info("[ModelDiscovery] ChatGPT: found %d visible models (client_version=%s)", len(models), client_version)
            return result
        else:
            result["error"] = f"API returned {resp.status_code}"
            logger.warning("[ModelDiscovery] ChatGPT codex/models returned %s", resp.status_code)
    except Exception as exc:
        result["error"] = str(exc)
        logger.warning("[ModelDiscovery] ChatGPT model fetch failed: %s", exc)

    # Fallback: known working models
    result["models"] = _chatgpt_fallback_models(tier)
    return result


def _decode_chatgpt_tier(token: str) -> str:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.b64decode(payload_b64))
        tier = payload.get("chatgpt_plan_type")
        if not tier:
            auth_claim = payload.get("https://api.openai.com/auth", {})
            tier = auth_claim.get("chatgpt_plan_type")
        return tier or "unknown"
    except Exception:
        return "unknown"


def _chatgpt_model_note(m: dict, tier: str = "unknown") -> str:
    slug = m.get("slug", "").lower()
    desc = m.get("description", "").lower()

    if "mini" in slug:
        return "fast"
    elif "5.4" in slug:
        return "most capable"
    elif "5.3" in slug:
        return "best quality"
    elif "5.2" in slug:
        return "balanced"
    elif "reasoning" in desc or m.get("default_reasoning_level"):
        return "most capable"
    else:
        return ""


def _chatgpt_fallback_models(tier: str = "unknown") -> list[dict]:
    return [
        {"id": "gpt-5.4",      "display_name": "gpt-5.4",      "note": "most capable"},
        {"id": "gpt-5.4-mini", "display_name": "GPT-5.4-Mini", "note": "fast"},
        {"id": "gpt-5.3-codex","display_name": "gpt-5.3-codex","note": "best quality"},
        {"id": "gpt-5.2",      "display_name": "gpt-5.2",      "note": "balanced"},
    ]


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

async def discover_gemini_models() -> dict:
    """
    Discover Gemini models via retrieveUserQuota on the Code Assist endpoint.
    Falls back to known model list.
    Returns: { tier, models: [{id, display_name, note}] }
    """
    result = {"provider": "gemini", "tier": "unknown", "models": [], "error": None}

    if not GEMINI_CREDENTIALS_FILE.exists():
        result["error"] = "Not authenticated"
        return result

    try:
        creds = json.loads(GEMINI_CREDENTIALS_FILE.read_text())
        token = creds.get("access_token", "")
        expiry_ms = creds.get("expiry_date", 0)
        if (time.time() * 1000) >= expiry_ms:
            result["error"] = "Token expired — re-authenticate with: gemini auth login"
            result["models"] = _gemini_fallback_models("free")
            return result
    except Exception as exc:
        result["error"] = f"Failed to read credentials: {exc}"
        return result

    if not token:
        result["error"] = "No access token"
        return result

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "x-goog-api-client": "gl-node/22.22.2",
    }

    # Step 1: get project ID from loadCodeAssist (also tells us account info)
    project_id = None
    tier = "free"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
                headers=headers,
                json={"metadata": {
                    "ideType": "IDE_UNSPECIFIED",
                    "platform": "PLATFORM_UNSPECIFIED",
                    "pluginType": "GEMINI",
                }},
            )
        if resp.status_code == 200:
            data = resp.json()
            project_id = data.get("cloudaicompanionProject")

            # Detect subscription tier from API response
            paid_tier = data.get("paidTier", {})
            current_tier = data.get("currentTier", {})
            paid_id = paid_tier.get("id", "")

            if "pro" in paid_id.lower():
                tier = "pro"
            elif paid_id:
                tier = paid_tier.get("name", paid_id)
            elif data.get("enterpriseDataAccessEnabled") or data.get("workspaceEnabled"):
                tier = "workspace"
            elif current_tier.get("id") == "standard-tier":
                tier = "standard"

            result["tier"] = tier
    except Exception as exc:
        logger.warning("[ModelDiscovery] Gemini loadCodeAssist failed: %s", exc)

    # Step 2: fetch per-model quota to see which models are available
    if project_id:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
                    headers=headers,
                    json={
                        "metadata": {
                            "ideType": "IDE_UNSPECIFIED",
                            "platform": "PLATFORM_UNSPECIFIED",
                            "pluginType": "GEMINI",
                        },
                        "cloudaicompanionProject": project_id,
                    },
                )
            if resp.status_code == 200:
                quota_data = resp.json()
                models = _parse_gemini_quota(quota_data)
                if models:
                    result["models"] = models
                    logger.info("[ModelDiscovery] Gemini: found %d models from quota, tier=%s", len(models), tier)
                    return result
        except Exception as exc:
            logger.warning("[ModelDiscovery] Gemini retrieveUserQuota failed: %s", exc)

    # Fallback
    result["models"] = _gemini_fallback_models(tier)
    return result


def _parse_gemini_quota(quota_data: dict) -> list[dict]:
    """Parse the retrieveUserQuota response and merge with known model list."""
    quota_by_id: dict = {}

    for quota in quota_data.get("quotas", []):
        model_id = quota.get("model") or quota.get("modelId") or ""
        if not model_id or model_id in quota_by_id:
            continue
        remaining = quota.get("remaining", quota.get("remainingCount", 1))
        limit = quota.get("limit", quota.get("dailyLimit", 1))
        quota_by_id[model_id] = {"remaining": remaining, "limit": limit}

    # Start from the full known model list, enrich with quota data where available
    models = []
    seen = set()
    for base in _gemini_fallback_models("free"):
        mid = base["id"]
        seen.add(mid)
        q = quota_by_id.get(mid)
        exhausted = q is not None and q["remaining"] == 0
        models.append({
            **base,
            "note": "exhausted today" if exhausted else base["note"],
            **({"quota_remaining": q["remaining"], "quota_limit": q["limit"]} if q else {}),
        })

    # Also include any quota models not already in the known list
    for model_id, q in quota_by_id.items():
        if model_id in seen:
            continue
        exhausted = q["remaining"] == 0
        models.append({
            "id": model_id,
            "display_name": _gemini_display_name(model_id),
            "note": "exhausted today" if exhausted else _gemini_note(model_id),
            "quota_remaining": q["remaining"],
            "quota_limit": q["limit"],
        })

    return models


def _gemini_display_name(model_id: str) -> str:
    # gemini-3-flash-preview → Gemini 3 Flash Preview
    return " ".join(p.capitalize() for p in model_id.replace("-", " ").split())


def _gemini_note(model_id: str) -> str:
    m = model_id.lower()
    if "lite" in m:
        return "most quota"
    if "flash" in m:
        return "fast"
    if "pro" in m:
        return "best quality"
    return ""


def _gemini_fallback_models(tier: str) -> list[dict]:
    return [
        {"id": "gemini-3.1-pro-preview",           "display_name": "Gemini 3.1 Pro Preview",           "note": "best quality"},
        {"id": "gemini-3-pro-preview",             "display_name": "Gemini 3 Pro Preview",             "note": "best quality"},
        {"id": "gemini-3.1-flash-lite-preview",    "display_name": "Gemini 3.1 Flash Lite Preview",    "note": "fast"},
        {"id": "gemini-3-flash-preview",           "display_name": "Gemini 3 Flash Preview",           "note": "fast"},
        {"id": "gemini-2.5-pro",                   "display_name": "Gemini 2.5 Pro",                   "note": "balanced"},
        {"id": "gemini-2.5-flash",                 "display_name": "Gemini 2.5 Flash",                 "note": "balanced"},
        {"id": "gemini-2.5-flash-lite",            "display_name": "Gemini 2.5 Flash Lite",            "note": "most quota"},
    ]


# ---------------------------------------------------------------------------
# Discover all and cache
# ---------------------------------------------------------------------------

async def _discover_arena_models() -> dict:
    """
    Discover arena.ai models via extension bridge or CloakBrowser fallback.
    """
    result = {"provider": "arena", "tier": "free", "models": [], "error": None}
    try:
        # Try extension bridge first
        from backend.services.arena_bridge_transport import is_bridge_available
        if is_bridge_available():
            from backend.adapters.arena_bridge_adapter import ArenaBridgeAdapter
            adapter = ArenaBridgeAdapter()
            raw_models = await adapter.fetch_models()
            result["models"] = [
                {"id": m, "display_name": m.removeprefix("arena/"), "note": ""}
                for m in raw_models
            ]
            return result

        # CloakBrowser fallback
        from backend.adapters.arena_steel_adapter import ArenaSteelAdapter
        adapter = ArenaSteelAdapter()
        if not await adapter.is_available():
            result["error"] = "No arena transport available (extension or CloakBrowser)"
            return result
        raw_models = await adapter.fetch_models()
        result["models"] = [
            {"id": m, "display_name": m.removeprefix("arena/"), "note": ""}
            for m in raw_models
        ]
    except Exception as exc:
        result["error"] = str(exc)
    return result


async def discover_qwen_models() -> dict:
    """Discover Qwen models from chat.qwen.ai public API (no auth needed)."""
    result = {"provider": "qwen", "tier": "free", "models": [], "error": None}
    try:
        from backend.adapters.qwen_direct_adapter import QwenDirectAdapter
        models = await QwenDirectAdapter.fetch_models()
        if models:
            result["models"] = [
                {
                    "id": m["id"],
                    "display_name": m["display_name"],
                    "note": "thinking" if m.get("thinking") else "",
                }
                for m in models
            ]
            logger.info("[ModelDiscovery] Qwen: found %d models", len(models))
        else:
            result["models"] = _qwen_fallback_models()
            result["error"] = "Could not fetch models from chat.qwen.ai"
    except Exception as exc:
        result["error"] = str(exc)
        result["models"] = _qwen_fallback_models()
    return result


def _qwen_fallback_models() -> list[dict]:
    return [
        {"id": "qwen3.6-plus", "display_name": "Qwen3.6-Plus", "note": "most capable"},
        {"id": "qwen3.6-max-preview", "display_name": "Qwen3.6-Max Preview", "note": "preview"},
        {"id": "qwen3.6-27b", "display_name": "Qwen3.6-27B", "note": "open-source"},
    ]


async def discover_all_models() -> dict:
    """
    Run discovery for all authenticated providers.
    Saves results to ~/.freehive/config.json under 'model_discovery'.
    Returns the full discovery result.
    """
    import asyncio

    results = await asyncio.gather(
        discover_claude_models(),
        discover_chatgpt_models(),
        discover_gemini_models(),
        discover_qwen_models(),
        _discover_arena_models(),
        return_exceptions=True,
    )

    discovery = {}
    for r in results:
        if isinstance(r, Exception):
            logger.warning("[ModelDiscovery] Provider discovery raised: %s", r)
            continue
        provider = r.get("provider")
        if provider:
            discovery[provider] = r

    # Persist to config
    config = _read_config()
    config["model_discovery"] = discovery
    config["model_discovery_at"] = int(time.time())
    _write_config(config)

    return discovery


def get_cached_discovery() -> dict:
    """Return the last discovery result from config, or empty dict."""
    config = _read_config()
    return config.get("model_discovery", {})
