"""
adapter_cascade.py — FreeHive Resilience

Wraps provider adapters in an ordered fallback chain. When strategy N fails with
a classified error, the cascade picks the appropriate next strategy.

Error-to-action mapping:
  AUTH_EXPIRED       → retry with refreshed token (same strategy), then advance
  AUTH_REVOKED       → skip to next strategy immediately
  BLOCKED_FINGERPRINT→ try updating UA/headers, then next strategy
  RATE_LIMITED       → honour retry-after within same strategy, do NOT cascade
  QUOTA_EXHAUSTED    → terminal — no strategy can help
  FORMAT_CHANGED     → next strategy
  ENDPOINT_GONE      → next strategy
  CONTENT_BLOCKED    → try extended scrub, then next strategy
  CONNECTION_LOST    → retry once, then next strategy
"""

import asyncio
import logging
import time

from backend.resilience.error_classifier import ErrorCategory, ClassifiedError

logger = logging.getLogger(__name__)


class AdapterStrategy:
    """Wraps a single adapter with its name and priority."""

    __slots__ = ("name", "adapter", "priority", "is_healthy", "_last_failure", "_failure_count")

    def __init__(self, name: str, adapter, priority: int = 0):
        self.name = name
        self.adapter = adapter
        self.priority = priority
        self.is_healthy = True
        self._last_failure: float = 0
        self._failure_count: int = 0

    def mark_failed(self, error: ClassifiedError):
        self._last_failure = time.time()
        self._failure_count += 1
        if error.should_cascade:
            self.is_healthy = False
            logger.warning(
                "[cascade] Strategy '%s' marked unhealthy: %s",
                self.name, error.message,
            )

    def mark_success(self):
        self._failure_count = 0
        self.is_healthy = True

    def should_retry_after_cooldown(self) -> bool:
        """After 5 min cooldown, try unhealthy strategies again."""
        if self.is_healthy:
            return True
        return (time.time() - self._last_failure) > 300

    @property
    def status(self) -> dict:
        return {
            "name": self.name,
            "healthy": self.is_healthy,
            "failure_count": self._failure_count,
            "last_failure": self._last_failure,
        }


class AdapterCascade:
    """
    Ordered list of adapter strategies. Tries each in order until one succeeds.

    Usage:
        cascade = AdapterCascade("claude", [
            AdapterStrategy("direct_oauth", ClaudeDirectAdapter(model)),
            AdapterStrategy("subprocess_cli", ClaudeSubprocessAdapter()),
            AdapterStrategy("user_api_key", ClaudeApiKeyAdapter()),
        ])
        result = await cascade.raw_request(messages, tools=tools)
    """

    def __init__(self, provider: str, strategies: list[AdapterStrategy]):
        self.provider = provider
        self.strategies = strategies
        self._active_index = 0
        self._model: str | None = None

    @property
    def active_strategy(self) -> AdapterStrategy | None:
        if 0 <= self._active_index < len(self.strategies):
            return self.strategies[self._active_index]
        return None

    @property
    def conversation_history(self):
        """Proxy to active adapter's history."""
        strategy = self.active_strategy
        if strategy and hasattr(strategy.adapter, "conversation_history"):
            return strategy.adapter.conversation_history
        return []

    @conversation_history.setter
    def conversation_history(self, value):
        strategy = self.active_strategy
        if strategy and hasattr(strategy.adapter, "conversation_history"):
            strategy.adapter.conversation_history = value

    def load_history(self, messages: list[dict]):
        """Load history into all strategies that support it."""
        for s in self.strategies:
            if hasattr(s.adapter, "load_history"):
                try:
                    s.adapter.load_history(messages)
                except Exception:
                    pass

    def clear_history(self, **kwargs):
        for s in self.strategies:
            if hasattr(s.adapter, "clear_history"):
                try:
                    s.adapter.clear_history(**kwargs)
                except Exception:
                    pass

    def is_authenticated(self) -> bool:
        """True if any strategy can authenticate."""
        for s in self.strategies:
            if hasattr(s.adapter, "is_authenticated"):
                try:
                    if s.adapter.is_authenticated():
                        return True
                except Exception:
                    continue
        return False

    async def send_message(self, *args, **kwargs) -> str:
        """Try each strategy in order for send_message."""
        return await self._try_strategies("send_message", args, kwargs)

    async def raw_request(self, *args, **kwargs) -> dict:
        """Try each strategy in order for raw_request."""
        return await self._try_strategies("raw_request", args, kwargs)

    async def close(self):
        """Close all strategies."""
        for s in self.strategies:
            if hasattr(s.adapter, "close"):
                try:
                    await s.adapter.close()
                except Exception:
                    pass

    async def _try_strategies(self, method: str, args: tuple, kwargs: dict):
        """Try each healthy strategy in order. Cascade on classified errors."""
        last_error = None

        for i, strategy in enumerate(self.strategies):
            if not strategy.is_healthy and not strategy.should_retry_after_cooldown():
                continue

            adapter = strategy.adapter
            fn = getattr(adapter, method, None)
            if fn is None:
                continue

            try:
                result = await fn(*args, **kwargs)
                strategy.mark_success()
                self._active_index = i
                return result
            except Exception as exc:
                classified = self._classify_error(exc)
                strategy.mark_failed(classified)
                last_error = classified

                if classified.is_terminal:
                    raise RuntimeError(classified.message) from exc

                if classified.is_retryable and not classified.should_cascade:
                    # Rate limit / transient — don't cascade, just fail
                    # The adapter's own retry logic should have handled this
                    raise

                logger.info(
                    "[cascade] %s strategy '%s' failed (%s), trying next...",
                    self.provider, strategy.name, classified.category.value,
                )
                continue

        # All strategies exhausted
        msg = (
            f"All {self.provider} strategies exhausted. "
            f"Last error: {last_error.message if last_error else 'unknown'}"
        )
        raise RuntimeError(msg)

    def _classify_error(self, exc: Exception) -> ClassifiedError:
        """Classify an exception from an adapter."""
        msg = str(exc)

        # Import here to avoid circular imports
        from backend.resilience.error_classifier import (
            classify_claude_error,
            classify_chatgpt_error,
            classify_gemini_error,
        )

        # Try to extract status code from RuntimeError messages
        import re
        status_match = re.search(r'(?:error|status)\s*(\d{3})', msg, re.IGNORECASE)
        status_code = int(status_match.group(1)) if status_match else 0

        if self.provider == "claude":
            return classify_claude_error(status_code, msg)
        elif self.provider == "chatgpt":
            if "websocket" in msg.lower() or "connection" in msg.lower():
                return classify_chatgpt_error(is_ws_close=True)
            return classify_chatgpt_error(body=msg, status_code=status_code)
        elif self.provider == "gemini":
            return classify_gemini_error(status_code, msg)

        return ClassifiedError(
            ErrorCategory.UNKNOWN,
            msg,
            original=exc,
        )

    @property
    def health_status(self) -> dict:
        return {
            "provider": self.provider,
            "active_strategy": self.active_strategy.name if self.active_strategy else None,
            "strategies": [s.status for s in self.strategies],
        }
