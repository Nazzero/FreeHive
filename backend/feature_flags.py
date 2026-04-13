import os


def _is_truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_arena_enabled() -> bool:
    """
    Arena is intentionally OFF by default until v2.
    Set FREEHIVE_ENABLE_ARENA=1 to re-enable during development.
    """
    return _is_truthy(os.getenv("FREEHIVE_ENABLE_ARENA", "0"))
