import os


def _is_truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_arena_enabled() -> bool:
    """
    Arena (extension bridge + CloakBrowser fallback) is ON by default.
    Set FREEHIVE_ENABLE_ARENA=0 to disable.
    """
    return _is_truthy(os.getenv("FREEHIVE_ENABLE_ARENA", "1"))
