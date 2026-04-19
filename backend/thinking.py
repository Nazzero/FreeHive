"""
thinking.py — Centralised thinking/reasoning effort logic for FreeHive.

Controls how much "thinking" (extended reasoning) each provider does before
answering.  Three access paths:

  1. FreeHive UI  → persists default in ~/.freehive/config.json
  2. Model suffix → claude-sonnet-4-6-think-high  (any OpenAI-compat client)
  3. Body param   → "thinking_effort": "high"      (SDK / curl users)

Priority: body param > model suffix > config default > "off"
"""

import json
import re
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Effort constants ──────────────────────────────────────────────────────── #

EFFORT_OFF = "off"
EFFORT_LOW = "low"
EFFORT_MEDIUM = "medium"
EFFORT_HIGH = "high"
VALID_EFFORTS = {EFFORT_OFF, EFFORT_LOW, EFFORT_MEDIUM, EFFORT_HIGH}

# ── Budget mapping ────────────────────────────────────────────────────────── #

_CLAUDE_BUDGETS = {
    EFFORT_LOW: 4096,
    EFFORT_MEDIUM: 10000,
    EFFORT_HIGH: 24000,
}

_GEMINI_BUDGETS = {
    EFFORT_LOW: 4096,
    EFFORT_MEDIUM: 10000,
    EFFORT_HIGH: 24000,
}

_CHATGPT_EFFORTS = {
    EFFORT_LOW: "low",
    EFFORT_MEDIUM: "medium",
    EFFORT_HIGH: "high",
}

# ── Model-name suffix parsing ─────────────────────────────────────────────── #

_THINK_SUFFIX_RE = re.compile(r"-think-(low|med|medium|high|off)$", re.IGNORECASE)
_SUFFIX_NORMALIZE = {"med": EFFORT_MEDIUM}


def parse_model_think_suffix(model: str) -> tuple[str, str | None]:
    """Strip ``-think-XXX`` suffix from *model*.

    Returns ``(clean_model, effort_or_None)``.  If no suffix found the model
    string is returned unchanged with ``None``.
    """
    m = _THINK_SUFFIX_RE.search(model)
    if not m:
        return model, None
    raw = m.group(1).lower()
    effort = _SUFFIX_NORMALIZE.get(raw, raw)
    clean = model[: m.start()]
    return clean, effort


# ── Effort resolution ─────────────────────────────────────────────────────── #

_FREEHIVE_CONFIG = Path.home() / ".freehive" / "config.json"


def get_default_effort() -> str:
    """Read ``thinking_effort`` from ``~/.freehive/config.json``."""
    try:
        data = json.loads(_FREEHIVE_CONFIG.read_text())
        effort = data.get("thinking_effort", EFFORT_OFF)
        return effort if effort in VALID_EFFORTS else EFFORT_OFF
    except Exception:
        return EFFORT_OFF


def resolve_effort(
    body_effort: str | None = None,
    suffix_effort: str | None = None,
) -> str:
    """Apply priority: body > suffix > config default > off."""
    if body_effort and body_effort in VALID_EFFORTS:
        return body_effort
    if suffix_effort and suffix_effort in VALID_EFFORTS:
        return suffix_effort
    return get_default_effort()


# ── Provider-specific param builders ──────────────────────────────────────── #

CLAUDE_THINKING_BETA = "interleaved-thinking-2025-04-14"


def claude_thinking_params(effort: str) -> tuple[str | None, dict | None]:
    """Return ``(beta_header_addition, thinking_body_dict)`` for Claude.

    Returns ``(None, None)`` when thinking is off.
    """
    budget = _CLAUDE_BUDGETS.get(effort)
    if not budget:
        return None, None
    return CLAUDE_THINKING_BETA, {"type": "enabled", "budget_tokens": budget}


def gemini_thinking_config(effort: str) -> dict | None:
    """Return ``thinkingConfig`` dict for Gemini, or ``None`` when off."""
    budget = _GEMINI_BUDGETS.get(effort)
    if not budget:
        return None
    return {"thinkingBudget": budget}


def chatgpt_reasoning_param(effort: str) -> dict | None:
    """Return ``reasoning`` dict for ChatGPT, or ``None`` when off."""
    level = _CHATGPT_EFFORTS.get(effort)
    if not level:
        return None
    return {"effort": level}


# ── Support detection ─────────────────────────────────────────────────────── #

_CLAUDE_THINKING_PREFIXES = ("claude-",)
_GEMINI_THINKING_PREFIXES = ("gemini-2.5-", "gemini-3", "gemini-3.")
_CHATGPT_THINKING_PREFIXES = ("o1", "o3", "o4", "gpt-5.3", "gpt-5.4")


def provider_supports_thinking(provider: str, model: str) -> bool:
    """Whether *model* on *provider* supports extended thinking."""
    model_lower = model.lower()
    if provider == "claude":
        return any(model_lower.startswith(p) for p in _CLAUDE_THINKING_PREFIXES)
    if provider == "gemini":
        return any(model_lower.startswith(p) for p in _GEMINI_THINKING_PREFIXES)
    if provider == "chatgpt":
        return any(model_lower.startswith(p) for p in _CHATGPT_THINKING_PREFIXES)
    return False
