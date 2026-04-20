"""
cascade_factory.py — FreeHive Resilience

Factory functions that build AdapterCascade instances for each provider.
Used by compat_router and session_manager to wrap adapters in fallback chains.
"""

import logging

from backend.resilience.adapter_cascade import AdapterCascade, AdapterStrategy
from backend.resilience.health_status import health_monitor, ProviderStatus

logger = logging.getLogger(__name__)


def build_claude_cascade(model: str | None = None) -> AdapterCascade:
    """
    Build Claude adapter cascade:
      1. Direct OAuth (dynamic CLI credentials)
      2. Subprocess CLI (claude / openclaude)
      3. User-provided API key
    """
    strategies = []

    # Strategy 1: Direct OAuth — primary path
    try:
        from backend.adapters.claude_direct_adapter import ClaudeDirectAdapter
        strategies.append(
            AdapterStrategy("direct_oauth", ClaudeDirectAdapter(model=model), priority=0)
        )
    except Exception as exc:
        logger.debug("[cascade_factory] Claude direct adapter unavailable: %s", exc)

    # Strategy 2: Subprocess CLI
    try:
        from backend.adapters.claude_adapter import ClaudeAdapter
        strategies.append(
            AdapterStrategy("subprocess_cli", ClaudeAdapter(), priority=1)
        )
    except Exception as exc:
        logger.debug("[cascade_factory] Claude subprocess adapter unavailable: %s", exc)

    # Strategy 3: User API key
    try:
        from backend.adapters.claude_apikey_adapter import ClaudeApiKeyAdapter
        adapter = ClaudeApiKeyAdapter(model=model)
        if adapter.is_authenticated():
            strategies.append(
                AdapterStrategy("user_api_key", adapter, priority=2)
            )
    except Exception as exc:
        logger.debug("[cascade_factory] Claude API key adapter unavailable: %s", exc)

    if not strategies:
        raise RuntimeError("No Claude adapters available. Install Claude CLI or add API key.")

    cascade = AdapterCascade("claude", strategies)
    health_monitor.mark_healthy("claude", strategies[0].name)
    return cascade


def build_chatgpt_cascade(model: str | None = None) -> AdapterCascade:
    """
    Build ChatGPT adapter cascade:
      1. Direct WebSocket (dynamic CLI headers)
      2. REST API fallback (same token, HTTP)
      3. User-provided OpenAI API key
    """
    strategies = []

    # Strategy 1: Direct WebSocket — primary
    try:
        from backend.adapters.chatgpt_direct_adapter import ChatGPTDirectAdapter
        strategies.append(
            AdapterStrategy("direct_ws", ChatGPTDirectAdapter(model=model), priority=0)
        )
    except Exception as exc:
        logger.debug("[cascade_factory] ChatGPT direct adapter unavailable: %s", exc)

    # Strategy 2: REST fallback
    try:
        from backend.adapters.chatgpt_rest_adapter import ChatGPTRestAdapter
        strategies.append(
            AdapterStrategy("rest_fallback", ChatGPTRestAdapter(model=model), priority=1)
        )
    except Exception as exc:
        logger.debug("[cascade_factory] ChatGPT REST adapter unavailable: %s", exc)

    # Strategy 3: User API key
    try:
        from backend.adapters.chatgpt_apikey_adapter import ChatGPTApiKeyAdapter
        adapter = ChatGPTApiKeyAdapter(model=model)
        if adapter.is_authenticated():
            strategies.append(
                AdapterStrategy("user_api_key", adapter, priority=2)
            )
    except Exception as exc:
        logger.debug("[cascade_factory] ChatGPT API key adapter unavailable: %s", exc)

    if not strategies:
        raise RuntimeError("No ChatGPT adapters available. Install Codex CLI or add API key.")

    cascade = AdapterCascade("chatgpt", strategies)
    health_monitor.mark_healthy("chatgpt", strategies[0].name)
    return cascade


def build_gemini_cascade(model: str | None = None) -> AdapterCascade:
    """
    Build Gemini adapter cascade:
      1. Direct Code Assist (dynamic CLI credentials)
      2. Subprocess CLI (gemini)
      3. User-provided Google AI API key
    """
    strategies = []

    # Strategy 1: Direct Code Assist — primary
    try:
        from backend.adapters.gemini_direct_adapter import GeminiDirectAdapter
        strategies.append(
            AdapterStrategy("direct_codeassist", GeminiDirectAdapter(model=model), priority=0)
        )
    except Exception as exc:
        logger.debug("[cascade_factory] Gemini direct adapter unavailable: %s", exc)

    # Strategy 2: Subprocess CLI
    try:
        from backend.adapters.gemini_subprocess_adapter import GeminiSubprocessAdapter
        adapter = GeminiSubprocessAdapter(model=model)
        if adapter.is_authenticated():
            strategies.append(
                AdapterStrategy("subprocess_cli", adapter, priority=1)
            )
    except Exception as exc:
        logger.debug("[cascade_factory] Gemini subprocess adapter unavailable: %s", exc)

    # Strategy 3: User API key
    try:
        from backend.adapters.gemini_apikey_adapter import GeminiApiKeyAdapter
        adapter = GeminiApiKeyAdapter(model=model)
        if adapter.is_authenticated():
            strategies.append(
                AdapterStrategy("user_api_key", adapter, priority=2)
            )
    except Exception as exc:
        logger.debug("[cascade_factory] Gemini API key adapter unavailable: %s", exc)

    if not strategies:
        raise RuntimeError("No Gemini adapters available. Install Gemini CLI or add API key.")

    cascade = AdapterCascade("gemini", strategies)
    health_monitor.mark_healthy("gemini", strategies[0].name)
    return cascade
