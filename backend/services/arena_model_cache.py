"""
arena_model_cache.py — Persistent cache for arena.ai model catalog.

Stores:
  - Chat models (from /text/direct dropdown)
  - Code models (from /code/direct dropdown)
  - Per-model capabilities (search, image-input, file-input, code)
  - Per-model modes (chat, code, or both)
  - Timestamp of last refresh

Cached to ~/.freehive/arena_models_full_cache.json
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CACHE_PATH = Path.home() / ".freehive" / "arena_models_full_cache.json"
CACHE_MAX_AGE_S = 86400  # 24 hours — stale after this, but still usable


def _empty_cache() -> dict[str, Any]:
    return {
        "version": 2,
        "saved_at": 0,
        "chat_models": [],
        "code_models": [],
        "all_models": [],
        "capabilities": {},
        "model_modes": {},
    }


def load_cache() -> dict[str, Any]:
    """Load cached model data from disk. Returns empty cache if missing/corrupt."""
    try:
        if CACHE_PATH.exists():
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version") in (2, 3):
                return data
    except Exception as exc:
        logger.debug("[ModelCache] Failed to load: %s", exc)
    return _empty_cache()


def save_cache(
    chat_models: list[str],
    code_models: list[str],
    model_modes: dict[str, list[str]],
    search_models: list[str] | None = None,
    image_models: list[str] | None = None,
    capabilities: dict[str, list[str]] | None = None,
) -> None:
    """Save model catalog to disk."""
    search_models = search_models or []
    image_models = image_models or []
    all_models = sorted(set(chat_models + code_models + search_models + image_models))
    data = {
        "version": 3,
        "saved_at": time.time(),
        "chat_models": sorted(chat_models),
        "code_models": sorted(code_models),
        "search_models": sorted(search_models),
        "image_models": sorted(image_models),
        "all_models": all_models,
        "capabilities": capabilities or {},
        "model_modes": model_modes,
    }
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
        tmp.replace(CACHE_PATH)
        logger.info(
            "[ModelCache] Saved: %d chat, %d code, %d search, %d image, %d total",
            len(chat_models), len(code_models), len(search_models), len(image_models), len(all_models),
        )
    except Exception as exc:
        logger.warning("[ModelCache] Failed to save: %s", exc)


def is_cache_fresh() -> bool:
    """True if cache exists and is less than CACHE_MAX_AGE_S old."""
    cache = load_cache()
    if not cache.get("saved_at"):
        return False
    age = time.time() - cache["saved_at"]
    return age < CACHE_MAX_AGE_S


def get_cached_models(mode: str = "all") -> list[str]:
    """Get cached model names. mode: 'all', 'chat', or 'code'."""
    cache = load_cache()
    if mode == "chat":
        return cache.get("chat_models", [])
    if mode == "code":
        return cache.get("code_models", [])
    return cache.get("all_models", [])


def get_cached_capabilities() -> dict[str, list[str]]:
    """Get cached per-model capabilities."""
    return load_cache().get("capabilities", {})


def get_cached_model_modes() -> dict[str, list[str]]:
    """Get cached per-model modes (chat/code)."""
    return load_cache().get("model_modes", {})


def _prefix(name: str) -> str:
    """Ensure arena/ prefix on model name."""
    s = str(name or "").strip()
    return s if s.startswith("arena/") else f"arena/{s}"


def _prefix_list(names: list[str]) -> list[str]:
    return [_prefix(n) for n in names if n]


def get_full_cache() -> dict[str, Any]:
    """Return the full cache dict for API responses. All model names have arena/ prefix."""
    cache = load_cache()
    age = time.time() - cache.get("saved_at", 0) if cache.get("saved_at") else None
    chat = _prefix_list(cache.get("chat_models", []))
    code = _prefix_list(cache.get("code_models", []))
    search = _prefix_list(cache.get("search_models", []))
    image = _prefix_list(cache.get("image_models", []))
    all_m = _prefix_list(cache.get("all_models", []))
    return {
        "chat_models": chat,
        "code_models": code,
        "search_models": search,
        "image_models": image,
        "all_models": all_m,
        "capabilities": cache.get("capabilities", {}),
        "model_modes": cache.get("model_modes", {}),
        "chat_count": len(chat),
        "code_count": len(code),
        "search_count": len(search),
        "image_count": len(image),
        "total": len(all_m),
        "cached_at": cache.get("saved_at"),
        "cache_age_seconds": int(age) if age else None,
        "is_fresh": is_cache_fresh(),
    }
