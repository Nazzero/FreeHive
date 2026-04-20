"""
model_router.py — FreeHive Resilience

Cross-provider model routing. When an entire provider is down (all strategies
exhausted), route to an equivalent model on a surviving provider.

Arena is the ultimate fallback — it can access Claude, GPT, and Gemini models
through arena.ai's interface.
"""

import logging

logger = logging.getLogger(__name__)

# Equivalence tiers — models within each tier are rough substitutes
_EQUIVALENCE_MAP = {
    "flagship": {
        "claude": "claude-opus-4-6",
        "chatgpt": "gpt-5.4",
        "gemini": "gemini-3.1-pro-preview",
    },
    "standard": {
        "claude": "claude-sonnet-4-6",
        "chatgpt": "gpt-5.4",
        "gemini": "gemini-3.1-pro-preview",
    },
    "fast": {
        "claude": "claude-haiku-4-5",
        "chatgpt": "gpt-5.4-mini",
        "gemini": "gemini-3-flash-preview",
    },
}

# Model → tier mapping
_MODEL_TIER = {}
for tier_name, models in _EQUIVALENCE_MAP.items():
    for provider, model_id in models.items():
        _MODEL_TIER[model_id] = (tier_name, provider)

# Provider detection from model name
_PROVIDER_PREFIXES = {
    "claude": "claude",
    "gpt-": "chatgpt",
    "o1-": "chatgpt",
    "o3-": "chatgpt",
    "o4-": "chatgpt",
    "codex-": "chatgpt",
    "gemini": "gemini",
}


def detect_provider(model: str) -> str | None:
    """Detect which provider a model belongs to."""
    m = model.lower()
    for prefix, provider in _PROVIDER_PREFIXES.items():
        if m.startswith(prefix):
            return provider
    return None


def detect_tier(model: str) -> str:
    """Detect the quality tier of a model."""
    m = model.lower()

    # Exact match
    if model in _MODEL_TIER:
        return _MODEL_TIER[model][0]

    # Heuristic
    if "opus" in m or "5.4" in m and "mini" not in m:
        return "flagship"
    if "haiku" in m or "mini" in m or "flash" in m:
        return "fast"
    return "standard"


def find_equivalent(
    model: str,
    failed_provider: str,
    available_providers: list[str],
) -> str | None:
    """
    Find an equivalent model on a different provider.

    Args:
        model: The original model that failed
        failed_provider: The provider that's down
        available_providers: Providers that are still healthy

    Returns:
        Alternative model ID, or None if no equivalent available
    """
    tier = detect_tier(model)
    equivalents = _EQUIVALENCE_MAP.get(tier, _EQUIVALENCE_MAP["standard"])

    # Try each available provider in order
    for provider in available_providers:
        if provider == failed_provider:
            continue
        alt_model = equivalents.get(provider)
        if alt_model:
            logger.info(
                "[model_router] Routing %s → %s (provider %s → %s)",
                model, alt_model, failed_provider, provider,
            )
            return alt_model

    # Last resort: Arena can access any model
    if "arena" in available_providers:
        arena_model = f"arena/{model}"
        logger.info(
            "[model_router] Routing %s → %s (via Arena fallback)",
            model, arena_model,
        )
        return arena_model

    return None


def get_routing_headers(original_model: str, routed_model: str) -> dict:
    """Return headers indicating cross-provider routing occurred."""
    original_provider = detect_provider(original_model) or "unknown"
    routed_provider = detect_provider(routed_model) or "unknown"
    return {
        "X-FreeHive-Routed-From": f"{original_provider}/{original_model}",
        "X-FreeHive-Routed-To": f"{routed_provider}/{routed_model}",
    }
