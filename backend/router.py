"""
router.py — FreeHive v0.5.1
"""

import asyncio
import json
import os
import platform
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import logging

from backend.feature_flags import is_arena_enabled

logger = logging.getLogger(__name__)

router = APIRouter()  # NO prefix here — main.py adds /api


class ChatRequest(BaseModel):
    model: str
    message: str
    session_id: str


class SessionCreateRequest(BaseModel):
    model: str


class ArenaStartRequest(BaseModel):
    force_login: bool = False


def _is_arena_model(model: str | None) -> bool:
    return str(model or "").strip().lower().startswith("arena/")


def _require_arena_enabled() -> None:
    if not is_arena_enabled():
        raise HTTPException(status_code=404, detail="Not found")


@router.post("/sessions")
async def create_session(body: SessionCreateRequest, request: Request):
    if _is_arena_model(body.model) and not is_arena_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    sm = request.app.state.session_manager
    cm = request.app.state.conversation_manager
    try:
        db_session = cm.create_session(body.model)
        session_id = db_session["id"]
        sm.create_session(session_id, body.model)
        messages = cm.get_messages(session_id)
        if messages:
            await sm.load_history(session_id, messages)
        return {"id": session_id, "model": body.model}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception(f"[sessions] Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/sessions")
async def list_sessions(request: Request, model: str | None = None, source: str | None = None):
    if _is_arena_model(model) and not is_arena_enabled():
        return []
    sessions = request.app.state.conversation_manager.list_sessions(model=model, source=source)
    if not is_arena_enabled():
        sessions = [row for row in sessions if not _is_arena_model(row.get("model"))]
    return sessions


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str, request: Request):
    cm = request.app.state.conversation_manager
    session = cm.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if _is_arena_model(session.get("model")) and not is_arena_enabled():
        raise HTTPException(status_code=404, detail="Session not found")
    messages = cm.get_messages(session_id)
    return messages


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request):
    request.app.state.session_manager.delete_session(session_id)
    request.app.state.conversation_manager.delete_session(session_id)
    return {"status": "deleted"}


@router.post("/chat")
async def chat(body: ChatRequest, request: Request):
    if _is_arena_model(body.model) and not is_arena_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    sm = request.app.state.session_manager
    cm = request.app.state.conversation_manager
    try:
        # If this session exists in DB but not memory (e.g., app restart or user loaded
        # an older chat), recreate adapter and replay stored messages.
        if sm.get_session(body.session_id) is None:
            db_session = cm.get_session(body.session_id)
            if not db_session:
                raise ValueError(f"Session {body.session_id} not found")
            if _is_arena_model(db_session.get("model")) and not is_arena_enabled():
                raise ValueError(f"Session {body.session_id} not found")
            sm.create_session(body.session_id, db_session["model"])
            history = cm.get_messages(body.session_id)
            if history:
                await sm.load_history(body.session_id, history)

        response = await sm.send_message(
            session_id=body.session_id,
            message=body.message,
            conversation_manager=cm,
        )

        # Auto-title on first message
        db_session = cm.get_session(body.session_id)
        if db_session and not db_session.get("title"):
            title = body.message.strip()[:50]
            if title:
                cm.update_session_title(body.session_id, title)

        transport = None
        session = sm.get_session(body.session_id)
        if session:
            adapter = session.get("adapter")
            if adapter and hasattr(adapter, "get_last_transport"):
                try:
                    transport = adapter.get_last_transport()
                except Exception:
                    transport = None
        return {"response": response, "transport": transport}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception(f"[chat] Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/chat/clear")
async def clear_chat(request: Request, model: str | None = None):
    sm = request.app.state.session_manager
    cm = request.app.state.conversation_manager
    try:
        if model:
            cleared_mem = sm.clear_model_sessions(model)
            db_sessions = cm.list_sessions(model=model)
            for row in db_sessions:
                sid = str(row.get("id", "")).strip()
                if sid:
                    cm.delete_session(sid)
            return {
                "status": "cleared",
                "model": model,
                "cleared_memory_sessions": cleared_mem,
                "cleared_db_sessions": len(db_sessions),
            }

        cleared_mem = sm.clear_all_sessions()
        db_sessions = cm.list_sessions()
        for row in db_sessions:
            sid = str(row.get("id", "")).strip()
            if sid:
                cm.delete_session(sid)
        return {
            "status": "cleared",
            "model": None,
            "cleared_memory_sessions": cleared_mem,
            "cleared_db_sessions": len(db_sessions),
        }
    except Exception as e:
        logger.exception(f"[chat/clear] Failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to clear chat history")


@router.post("/chat/reset-db")
async def reset_db(request: Request):
    """Nuke all history: clear in-memory sessions, delete DB, recreate fresh."""
    sm = request.app.state.session_manager
    cm = request.app.state.conversation_manager
    try:
        sm.clear_all_sessions()
        cm.reset_database()
        return {"status": "reset"}
    except Exception as e:
        logger.exception(f"[chat/reset-db] Failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to reset database")


@router.get("/arena/status")
async def arena_status(request: Request):
    _require_arena_enabled()
    am = request.app.state.arena_manager
    if am is None:
        raise HTTPException(status_code=503, detail="Arena manager unavailable")
    status = am.get_status()
    logged_in = await am.is_logged_in() if status["running"] else False
    return {**status, "logged_in": logged_in}


@router.post("/arena/start")
async def arena_start(request: Request, body: ArenaStartRequest = None):
    _require_arena_enabled()
    if body is None:
        body = ArenaStartRequest()
    am = request.app.state.arena_manager
    if am is None:
        raise HTTPException(status_code=503, detail="Arena manager unavailable")
    sm = request.app.state.session_manager
    try:
        result = await am.start(force_login=body.force_login)
        sm.set_arena_manager(am)
        return result
    except Exception as e:
        logger.exception(f"[arena/start] Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/arena/stop")
async def arena_stop(request: Request):
    _require_arena_enabled()
    am = request.app.state.arena_manager
    if am is None:
        raise HTTPException(status_code=503, detail="Arena manager unavailable")
    await am.stop()
    return {"status": "stopped"}


@router.get("/arena/models")
async def arena_models(request: Request):
    """Return cached model list. If no cache, triggers a refresh."""
    _require_arena_enabled()
    from backend.services.arena_model_cache import get_full_cache, is_cache_fresh
    cache = get_full_cache()
    if cache.get("total", 0) > 0:
        return cache
    # No cache — fall back to live fetch from current page
    am = request.app.state.arena_manager
    if am is None:
        raise HTTPException(status_code=503, detail="Arena manager unavailable")
    models = await am.get_models()
    return {"models": [{"id": m, "display_name": m.removeprefix("arena/")} for m in models]}


@router.post("/arena/models/refresh")
async def arena_models_refresh(request: Request):
    """Refresh model list from arena.ai — fetches both chat and code dropdowns, saves to cache."""
    _require_arena_enabled()
    from backend.services.arena_bridge_client import ArenaBridgeClient
    from backend.services.arena_model_cache import save_cache, get_full_cache
    from backend.services.arena_bridge_transport import is_bridge_available
    import asyncio

    if not is_bridge_available():
        raise HTTPException(status_code=503, detail="Extension bridge not connected. Open Chrome with the Arena extension.")

    client = ArenaBridgeClient()

    async def _fetch_from_page(url: str) -> tuple[list[str], dict]:
        """Navigate to url, wait for page load, read dropdown."""
        # Navigate
        async for update in client.send_chat(
            model="nav", message="__NAV__", session_id="nav",
            timeout_ms=10000, metadata={"operation": "navigate", "url": url},
        ):
            pass
        await asyncio.sleep(3)
        # Fetch models
        models = []
        caps = {}
        async for update in client.send_chat(
            model="fetch", message="__FETCH__", session_id="refresh",
            timeout_ms=15000, metadata={"operation": "fetch_models"},
        ):
            if update.get("type") == "JOB_COMPLETE":
                meta = update.get("metadata") or {}
                models = meta.get("models", [])
                caps = meta.get("capabilities", {})
        return models, caps

    # Fetch from both pages
    chat_models, chat_caps = await _fetch_from_page("https://arena.ai/text/direct")
    code_models, code_caps = await _fetch_from_page("https://arena.ai/code/direct")

    # Navigate back to text/direct
    async for update in client.send_chat(
        model="nav", message="__NAV__", session_id="nav-back",
        timeout_ms=10000, metadata={"operation": "navigate", "url": "https://arena.ai/text/direct"},
    ):
        pass

    # Merge capabilities
    all_caps = {**chat_caps, **code_caps}

    # Build mode map
    all_names = sorted(set(chat_models + code_models))
    chat_set = set(chat_models)
    code_set = set(code_models)
    model_modes = {}
    for name in all_names:
        modes = []
        if name in chat_set: modes.append("chat")
        if name in code_set: modes.append("code")
        model_modes[name] = modes

    # Save to disk
    save_cache(
        chat_models=chat_models,
        code_models=code_models,
        capabilities=all_caps,
        model_modes=model_modes,
    )

    return get_full_cache()


@router.get("/arena/health")
async def arena_health(request: Request):
    """Return the full health-store state for all probed models."""
    _require_arena_enabled()
    am = request.app.state.arena_manager
    if am is None:
        raise HTTPException(status_code=503, detail="Arena manager unavailable")
    adapter = am.get_adapter()
    return {"models": adapter._health.get_all()}


@router.post("/arena/probe")
async def arena_probe_models(request: Request):
    """
    SSE stream that sends a minimal test message to every known arena.ai model.
    Results are written to ArenaModelHealthStore and streamed back to the caller.

    Events:
      {status:"starting", total:N}
      {status:"probing",  model:"arena/...", index:i, total:N}
      {status:"result",   model:"arena/...", health:"working"|"unavailable"|"private"|"rate_limited"|"error", error?:"...", index:i, total:N}
      {status:"done",     summary:{working:[...], unavailable:[...], errored:[...]}}
    """
    _require_arena_enabled()
    am = request.app.state.arena_manager
    if am is None:
        raise HTTPException(status_code=503, detail="Arena manager unavailable")

    def _sse(data: dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    async def probe_stream():
        adapter = am.get_adapter()

        # Fetch current model list
        try:
            models = await am.get_models()
        except Exception as exc:
            yield _sse({"status": "error", "error": f"Failed to fetch models: {exc}"})
            return

        if not models:
            yield _sse({"status": "error", "error": "No models found. Ensure the browser is running and you are logged in to arena.ai."})
            return

        yield _sse({"status": "starting", "total": len(models)})

        working: list[str] = []
        unavailable: list[str] = []
        errored: list[str] = []

        CIRCUIT_BREAKER_LIMIT = 10  # stop after this many consecutive non-working results
        consecutive_failures = 0

        for i, model in enumerate(models):
            if consecutive_failures >= CIRCUIT_BREAKER_LIMIT:
                yield _sse({
                    "status": "aborted",
                    "reason": f"Stopped after {CIRCUIT_BREAKER_LIMIT} consecutive failures — session likely rate-limited.",
                    "index": i,
                    "total": len(models),
                })
                break

            yield _sse({"status": "probing", "model": model, "index": i, "total": len(models)})

            # Skip models already known to be permanently broken
            block_reason = adapter._health.get_block_reason(model)
            if block_reason:
                unavailable.append(model)
                yield _sse({
                    "status": "result",
                    "model": model,
                    "health": "unavailable",
                    "error": block_reason,
                    "index": i,
                    "total": len(models),
                })
                continue

            try:
                probe_session = f"__probe_{int(time.time())}_{i}"
                # Minimal message — short enough to be cheap, long enough to get a real response
                resp = await asyncio.wait_for(
                    adapter.send_message("hi", model=model, session_id=probe_session),
                    timeout=45.0,
                )
                working.append(model)
                consecutive_failures = 0
                yield _sse({
                    "status": "result",
                    "model": model,
                    "health": "working",
                    "preview": (resp or "")[:100],
                    "index": i,
                    "total": len(models),
                })
            except asyncio.TimeoutError:
                consecutive_failures += 1
                errored.append(model)
                yield _sse({
                    "status": "result",
                    "model": model,
                    "health": "timeout",
                    "error": "No response within 45 seconds.",
                    "index": i,
                    "total": len(models),
                })
            except RuntimeError as exc:
                msg = str(exc)
                msg_l = msg.lower()
                if "not found" in msg_l or "unavailable" in msg_l:
                    health = "unavailable"
                    unavailable.append(model)
                    consecutive_failures = 0  # permanent block, not a rate-limit signal
                elif "private" in msg_l or "battle-only" in msg_l or "not available for user selection" in msg_l:
                    health = "private"
                    unavailable.append(model)
                    consecutive_failures = 0  # permanent block, not a rate-limit signal
                elif "rate" in msg_l or "rate-limited" in msg_l:
                    health = "rate_limited"
                    errored.append(model)
                    consecutive_failures += 1
                elif "recaptcha" in msg_l:
                    health = "recaptcha"
                    errored.append(model)
                    consecutive_failures += 1
                else:
                    health = "error"
                    errored.append(model)
                    consecutive_failures += 1
                yield _sse({
                    "status": "result",
                    "model": model,
                    "health": health,
                    "error": msg[:300],
                    "index": i,
                    "total": len(models),
                })
            except Exception as exc:
                consecutive_failures += 1
                errored.append(model)
                yield _sse({
                    "status": "result",
                    "model": model,
                    "health": "error",
                    "error": str(exc)[:300],
                    "index": i,
                    "total": len(models),
                })

            # Polite delay between probes to avoid triggering rate limits
            await asyncio.sleep(3.0)

        yield _sse({
            "status": "done",
            "summary": {
                "working": working,
                "unavailable": unavailable,
                "errored": errored,
                "total": len(models),
            },
        })

    return StreamingResponse(probe_stream(), media_type="text/event-stream")


@router.post("/integrations/opencode")
async def add_to_opencode(request: Request):
    """Write selected FreeHive providers/models into ~/.config/opencode/opencode.json."""
    from backend.model_discovery import _read_config as _read_freehive_config

    # Parse optional JSON body for selective import
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    # selections: { "claude": ["model-id", ...], "chatgpt": [...], ... }
    # None → import all providers with all models (backward compatible)
    selections = body.get("selections")

    port = int(os.getenv("FREEHIVE_BACKEND_PORT", "7200"))
    base_url = f"http://127.0.0.1:{port}/v1"

    # Load discovered models from cache
    fh_config = _read_freehive_config()
    discovery = fh_config.get("model_discovery", {})

    def _models_for(provider_key: str, fallback_ids: list[str]) -> dict:
        models = {}
        discovered = discovery.get(provider_key, {}).get("models", [])
        ids = [m["id"] for m in discovered] if discovered else fallback_ids
        for mid in ids:
            models[mid] = {"name": mid}
        return models

    def _filter_models(models: dict, selected_ids: list[str] | None) -> dict:
        """Keep only selected model IDs. None → keep all."""
        if selected_ids is None:
            return models
        allowed = set(selected_ids)
        return {mid: v for mid, v in models.items() if mid in allowed}

    claude_fallback  = ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5"]
    chatgpt_fallback = ["gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2"]
    gemini_fallback  = [
        "gemini-3.1-pro-preview", "gemini-3-pro-preview",
        "gemini-3.1-flash-lite-preview", "gemini-3-flash-preview",
        "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite",
    ]

    def _add_think_variants(models: dict, provider: str) -> dict:
        """Append -think-low/-think-med/-think-high variants for supported models."""
        from backend.thinking import provider_supports_thinking
        extended = dict(models)
        for mid in list(models.keys()):
            if provider_supports_thinking(provider, mid):
                for suffix, label in [("-think-low", " (Think Low)"), ("-think-med", " (Think Med)"), ("-think-high", " (Think High)")]:
                    extended[mid + suffix] = {"name": mid + label}
        return extended

    # Build provider configs — only for selected providers
    provider_defs = {
        "claude": {
            "key": "freehive-claude",
            "name": "FreeHive Claude",
            "apiKey": "freehive-claude",
            "models_fn": lambda: _models_for("claude", claude_fallback),
            "think": True,
        },
        "chatgpt": {
            "key": "freehive-chatgpt",
            "name": "FreeHive ChatGPT",
            "apiKey": "freehive-chatgpt",
            "models_fn": lambda: _models_for("chatgpt", chatgpt_fallback),
            "think": True,
        },
        "gemini": {
            "key": "freehive-gemini",
            "name": "FreeHive Gemini",
            "apiKey": "freehive-gemini",
            "models_fn": lambda: _models_for("gemini", gemini_fallback),
            "think": True,
        },
        "arena": {
            "key": "freehive-arena",
            "name": "FreeHive Arena",
            "apiKey": "freehive-arena",
            "models_fn": lambda: _load_arena_models(),
            "think": False,
        },
    }

    def _load_arena_models() -> dict:
        cache_path = Path.home() / ".freehive" / "arena_models_full_cache.json"
        try:
            raw = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        except Exception:
            raw = {}
        # Cache is { version, all_models: ["bare-model-id", ...], ... }
        # Frontend uses arena/ prefix, so store with prefix to match selections
        model_ids = raw.get("all_models", []) if isinstance(raw, dict) else []
        models = {}
        for mid in model_ids:
            if isinstance(mid, str) and mid:
                full_id = f"arena/{mid}" if not mid.startswith("arena/") else mid
                models[full_id] = {"name": mid}
        return models

    # Determine which providers to include
    providers_to_include = list(provider_defs.keys()) if selections is None else list(selections.keys())

    new_providers = {}
    total_models = 0
    for prov in providers_to_include:
        defn = provider_defs.get(prov)
        if not defn:
            continue
        all_models = defn["models_fn"]()
        selected_ids = selections.get(prov) if selections else None
        filtered = _filter_models(all_models, selected_ids)
        if not filtered:
            continue
        if defn["think"]:
            filtered = _add_think_variants(filtered, prov)
        new_providers[defn["key"]] = {
            "npm": "@ai-sdk/openai-compatible",
            "name": defn["name"],
            "options": {"baseURL": base_url, "apiKey": defn["apiKey"]},
            "models": filtered,
        }
        total_models += len(filtered)

    opencode_config_path = Path.home() / ".config" / "opencode" / "opencode.json"
    try:
        existing = json.loads(opencode_config_path.read_text()) if opencode_config_path.exists() else {}
    except Exception:
        existing = {}

    existing.setdefault("provider", {})
    existing["provider"].update(new_providers)

    if "model" not in existing:
        existing["model"] = "freehive-claude/claude-sonnet-4-6"

    try:
        opencode_config_path.parent.mkdir(parents=True, exist_ok=True)
        opencode_config_path.write_text(json.dumps(existing, indent=2))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write config: {e}")

    prov_count = len(new_providers)
    return {
        "success": True,
        "message": f"{prov_count} provider{'s' if prov_count != 1 else ''}, {total_models} models added to OpenCode",
        "providers": list(new_providers.keys()),
        "config_path": str(opencode_config_path),
    }


@router.post("/arena/login")
async def arena_force_login(request: Request):
    _require_arena_enabled()
    am = request.app.state.arena_manager
    if am is None:
        raise HTTPException(status_code=503, detail="Arena manager unavailable")
    sm = request.app.state.session_manager
    if am.get_status()["running"]:
        await am.stop()
    try:
        result = await am.start(force_login=True)
        sm.set_arena_manager(am)
        return result
    except Exception as e:
        logger.exception(f"[arena/login] Failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Arena captcha solver endpoints (CloakBrowser fallback only)
# Extension bridge: user solves captchas directly in their Chrome tab.
# ---------------------------------------------------------------------------

@router.get("/arena/captcha")
async def get_arena_captcha(request: Request):
    """Return pending captcha state. Extension bridge: always {pending: false}."""
    _require_arena_enabled()
    am = request.app.state.arena_manager
    if am is None:
        return {"pending": False}
    adapter = am.get_adapter()
    if adapter is None or not hasattr(adapter, "get_captcha_state"):
        return {"pending": False}
    return adapter.get_captcha_state()


class CaptchaSolveRequest(BaseModel):
    cells: list[int]


@router.post("/arena/captcha/solve")
async def solve_arena_captcha(body: CaptchaSolveRequest, request: Request):
    """Receive user's tile selections (CloakBrowser fallback only)."""
    _require_arena_enabled()
    am = request.app.state.arena_manager
    if am is None:
        raise HTTPException(status_code=503, detail="Arena manager unavailable")
    adapter = am.get_adapter()
    if adapter is None:
        raise HTTPException(status_code=503, detail="Arena adapter unavailable")
    if not hasattr(adapter, "_captcha_pending") or not adapter._captcha_pending:
        raise HTTPException(status_code=409, detail="No captcha pending")
    adapter.submit_captcha_solution(body.cells)
    return {"status": "submitted", "cells": body.cells}


@router.post("/arena/logout")
async def arena_logout(request: Request):
    """Clear arena session. CloakBrowser: clears cookies. Extension: no-op (user manages Chrome)."""
    _require_arena_enabled()
    am = request.app.state.arena_manager
    if am is None:
        raise HTTPException(status_code=503, detail="Arena manager unavailable")
    # Extension bridge: logout means clearing conversation state only
    if am.active_transport == "extension":
        adapter = am.get_adapter()
        if adapter and hasattr(adapter, "clear_history"):
            adapter.clear_history()
        return {"status": "logged_out", "transport": "extension",
                "message": "Conversation state cleared. To log out of arena.ai, do so in your Chrome tab."}
    # CloakBrowser fallback: clear cookies + close browser
    adapter = am.get_adapter()
    if adapter is None:
        raise HTTPException(status_code=503, detail="Arena adapter unavailable")
    if hasattr(adapter, "logout"):
        await adapter.logout()
    from pathlib import Path
    profile = Path.home() / ".freehive" / "arena_profile"
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (profile / lock).unlink(missing_ok=True)
    return {"status": "logged_out", "transport": "cloakbrowser"}


@router.get("/arena/account")
async def arena_account_info(request: Request):
    """Return current arena.ai account info."""
    _require_arena_enabled()
    am = request.app.state.arena_manager
    if am is None:
        return {"logged_in": False}
    # Extension bridge: user is logged in via their Chrome
    if am.active_transport == "extension":
        return {"logged_in": True, "transport": "extension",
                "message": "Managed by your Chrome browser session"}
    adapter = am.get_adapter()
    if adapter is None or not hasattr(adapter, "get_account_info"):
        return {"logged_in": False}
    return await adapter.get_account_info()


@router.post("/arena/show-browser")
async def arena_show_browser(request: Request):
    """Open browser for interaction. Extension: open arena.ai tab. CloakBrowser: relaunch headed."""
    _require_arena_enabled()
    am = request.app.state.arena_manager
    if am is None:
        raise HTTPException(status_code=503, detail="Arena manager unavailable")
    # Extension bridge: user already has Chrome open
    if am.active_transport == "extension":
        return {"status": "extension", "message": "Arena.ai is open in your Chrome browser. Switch to the pinned arena.ai tab."}
    # CloakBrowser fallback: relaunch in headed mode
    adapter = am.get_adapter()
    if adapter is None or not hasattr(adapter, "_orchestrator"):
        raise HTTPException(status_code=503, detail="CloakBrowser adapter unavailable")

    orchestrator = adapter._orchestrator
    await adapter.close()

    from pathlib import Path
    try:
        if platform.system() == "Windows":
            for proc in ("chromium.exe", "chrome.exe"):
                subprocess.run(["taskkill", "/F", "/IM", proc], capture_output=True, timeout=5)
        else:
            subprocess.run(["pkill", "-f", "chromium-146"], capture_output=True, timeout=3)
    except Exception:
        pass
    import asyncio
    await asyncio.sleep(0.5)
    profile = Path.home() / ".freehive" / "arena_profile"
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (profile / lock).unlink(missing_ok=True)

    orchestrator._headless = False
    ctx = await orchestrator.get_or_create_context()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    if "arena.ai" not in (page.url or ""):
        await page.goto("https://arena.ai/text/direct", wait_until="load", timeout=30000)
    am._login_session_active = True
    logger.info("[arena/show-browser] Opened headed CloakBrowser window")
    return {"status": "opened", "message": "CloakBrowser window opened."}


# ---------------------------------------------------------------------------
# Arena extension setup + Chrome auto-launch
# ---------------------------------------------------------------------------

@router.post("/arena/setup")
async def arena_setup(request: Request):
    """One-click Arena setup: install native host, detect extension ID, launch Chrome."""
    _require_arena_enabled()
    from backend.services.chrome_launcher import (
        install_native_host, launch_chrome_with_extension,
        detect_extension_id, patch_native_host_manifest,
    )

    # Step 1: Install native host
    host_result = install_native_host()

    # Step 2: Detect extension ID and patch manifest
    ext_id = detect_extension_id()
    patched = False
    if ext_id:
        patched = patch_native_host_manifest(ext_id)

    # Step 3: Launch Chrome with extension
    launch_result = launch_chrome_with_extension("https://arena.ai/text/direct")

    return {
        "native_host": host_result,
        "extension_id": ext_id,
        "manifest_patched": patched,
        "chrome": launch_result,
        "message": launch_result.get("message", ""),
        "success": launch_result.get("success", False),
    }


@router.get("/arena/chrome-status")
async def arena_chrome_status(request: Request):
    """Check Chrome + extension + native host installation state."""
    _require_arena_enabled()
    from backend.services.chrome_launcher import get_chrome_status
    from backend.services.arena_bridge_transport import is_bridge_available

    status = get_chrome_status()
    status["bridge_connected"] = is_bridge_available()
    return status


@router.post("/arena/launch-chrome")
async def arena_launch_chrome(request: Request):
    """Launch Chrome with extension loaded, navigate to arena.ai."""
    _require_arena_enabled()
    from backend.services.chrome_launcher import launch_chrome_with_extension
    return launch_chrome_with_extension("https://arena.ai/text/direct")


@router.get("/arena/extension-path")
async def arena_extension_path(request: Request):
    """Return the resolved path to the bundled Arena Chrome extension."""
    _require_arena_enabled()
    from backend.services.chrome_launcher import get_extension_path
    return get_extension_path()


@router.post("/arena/open-extension-folder")
async def arena_open_extension_folder(request: Request):
    """Open the Arena extension folder in the OS file manager."""
    _require_arena_enabled()
    from backend.services.chrome_launcher import open_extension_folder
    return open_extension_folder()


@router.get("/arena/all-models")
async def arena_all_models(request: Request):
    """Fetch models from both text/direct and code/direct by navigating the tab."""
    _require_arena_enabled()
    from backend.services.arena_bridge_client import ArenaBridgeClient
    client = ArenaBridgeClient()

    async def _fetch_models_for_page(url: str) -> tuple[list[str], dict]:
        """Navigate tab to url, wait, then fetch models from dropdown."""
        # First navigate
        async for update in client.send_chat(
            model="nav", message="__NAV__", session_id="nav",
            timeout_ms=15000,
            metadata={"operation": "navigate", "url": url},
        ):
            pass  # Just wait for completion
        # Wait for page to load
        import asyncio
        await asyncio.sleep(3)
        # Now fetch models
        models = []
        caps = {}
        async for update in client.send_chat(
            model="fetch", message="__FETCH__", session_id="fetch-models",
            timeout_ms=15000, metadata={"operation": "fetch_models"},
        ):
            if update.get("type") == "JOB_COMPLETE":
                meta = update.get("metadata") or {}
                models = meta.get("models", [])
                caps = meta.get("capabilities", {})
            elif update.get("type") == "JOB_ERROR":
                break
        return models, caps

    # Get chat models (current page should be text/direct)
    chat_models, chat_caps = await _fetch_models_for_page("https://arena.ai/text/direct")

    # Get code models
    code_models, code_caps = await _fetch_models_for_page("https://arena.ai/code/direct")

    # Navigate back to text/direct
    async for update in client.send_chat(
        model="nav", message="__NAV__", session_id="nav-back",
        timeout_ms=10000,
        metadata={"operation": "navigate", "url": "https://arena.ai/text/direct"},
    ):
        pass

    # Merge
    all_names = sorted(set(chat_models + code_models))
    chat_set = set(chat_models)
    code_set = set(code_models)
    model_modes = {}
    for name in all_names:
        m = []
        if name in chat_set: m.append("chat")
        if name in code_set: m.append("code")
        model_modes[name] = m

    all_caps = {**chat_caps, **code_caps}

    return {
        "chat_models": sorted(chat_models),
        "code_models": sorted(code_models),
        "models": all_names,
        "capabilities": all_caps,
        "model_modes": model_modes,
        "chat_count": len(chat_models),
        "code_count": len(code_models),
        "total": len(all_names),
    }


@router.get("/arena/debug-models")
async def arena_debug_models(request: Request):
    """Return raw model objects from arena.ai to inspect field names."""
    _require_arena_enabled()
    am = request.app.state.arena_manager
    if am is None:
        raise HTTPException(status_code=503, detail="Arena manager unavailable")
    from backend.services.arena_bridge_client import ArenaBridgeClient
    client = ArenaBridgeClient()
    result_text = ""
    async for update in client.send_chat(
        model="debug", message="__DEBUG__", session_id="debug",
        timeout_ms=15000, metadata={"operation": "debug_models"},
    ):
        if update.get("type") == "JOB_COMPLETE":
            result_text = update.get("full_text", "")
        elif update.get("type") == "JOB_ERROR":
            raise HTTPException(status_code=503, detail=update.get("message", "Debug failed"))
    import json
    try:
        return json.loads(result_text)
    except Exception:
        return {"raw": result_text}


class SetExtensionIdRequest(BaseModel):
    extension_id: str


@router.get("/arena/extension-ids")
async def arena_extension_ids(request: Request):
    """Return which extension IDs are registered in the native host manifest."""
    _require_arena_enabled()
    from backend.services.chrome_launcher import get_active_extension_ids
    return get_active_extension_ids()


@router.post("/arena/set-extension-id")
async def arena_set_extension_id(body: SetExtensionIdRequest, request: Request):
    """Register an unpacked extension ID alongside the Web Store ID."""
    _require_arena_enabled()
    from backend.services.chrome_launcher import patch_native_host_manifest, get_active_extension_ids

    ext_id = body.extension_id.strip()
    if not ext_id or len(ext_id) != 32 or not ext_id.isalpha():
        raise HTTPException(status_code=400, detail="Invalid extension ID. Must be 32 lowercase letters.")
    patched = patch_native_host_manifest(ext_id)
    if patched:
        ids = get_active_extension_ids()
        return {"success": True, "extension_id": ext_id, **ids, "message": "Extension ID registered. Restart Chrome for changes to take effect."}
    raise HTTPException(status_code=500, detail="Failed to update native host manifest.")


# ---------------------------------------------------------------------------
# Provider health status + API key management (Resilience system)
# ---------------------------------------------------------------------------

@router.get("/provider-health")
async def provider_health():
    """Return per-provider health status for frontend degraded-mode banners."""
    from backend.resilience.health_status import health_monitor
    return health_monitor.get_all()


@router.get("/provider-health/{provider}")
async def provider_health_detail(provider: str):
    """Return health status for a specific provider."""
    from backend.resilience.health_status import health_monitor
    return health_monitor.get_status(provider)


@router.get("/cli-introspection")
async def cli_introspection_status():
    """Return CLI introspection cache status (for debugging)."""
    from backend.resilience.cli_introspection import get_all_configs
    configs = get_all_configs()
    # Strip secrets from output
    safe = {}
    for provider, cfg in configs.items():
        safe[provider] = {
            k: v for k, v in cfg.items()
            if k not in ("client_secret", "access_token", "refresh_token")
        }
    return safe


@router.post("/cli-introspection/refresh")
async def refresh_cli_introspection():
    """Force re-extraction of CLI metadata (clear cache)."""
    from backend.resilience.cli_introspection import invalidate_cache
    invalidate_cache()
    return {"status": "cache_cleared", "message": "CLI metadata will be re-extracted on next request."}


class ApiKeysRequest(BaseModel):
    anthropic: str | None = None
    openai: str | None = None
    google: str | None = None


@router.get("/settings/api-keys")
async def get_api_keys():
    """Check which API keys are configured (never return actual keys)."""
    api_key_file = Path.home() / ".freehive" / "api_keys.json"
    try:
        if api_key_file.exists():
            keys = json.loads(api_key_file.read_text())
            return {
                "anthropic": bool(keys.get("anthropic", "").strip()),
                "openai": bool(keys.get("openai", "").strip()),
                "google": bool(keys.get("google", "").strip()),
            }
    except Exception:
        pass
    return {"anthropic": False, "openai": False, "google": False}


@router.post("/settings/api-keys")
async def set_api_keys(body: ApiKeysRequest):
    """Save user-provided API keys (encrypted at rest)."""
    api_key_file = Path.home() / ".freehive" / "api_keys.json"
    try:
        existing = {}
        if api_key_file.exists():
            existing = json.loads(api_key_file.read_text())
    except Exception:
        existing = {}

    if body.anthropic is not None:
        existing["anthropic"] = body.anthropic.strip()
    if body.openai is not None:
        existing["openai"] = body.openai.strip()
    if body.google is not None:
        existing["google"] = body.google.strip()

    api_key_file.parent.mkdir(parents=True, exist_ok=True)
    api_key_file.write_text(json.dumps(existing, indent=2))
    # Restrict permissions
    api_key_file.chmod(0o600)

    return {
        "success": True,
        "anthropic": bool(existing.get("anthropic", "")),
        "openai": bool(existing.get("openai", "")),
        "google": bool(existing.get("google", "")),
    }


# ---------------------------------------------------------------------------
# Backend log streaming (for in-app debugger)
# ---------------------------------------------------------------------------

@router.get("/backend/logs")
async def stream_backend_logs(request: Request, lines: int = 200):
    """SSE endpoint that tails ~/.freehive/backend.log.

    Sends the last `lines` lines as backfill, then streams new lines every 1s.
    The frontend Settings → Backend Logs tab subscribes to this.
    """
    from backend.main import LOG_FILE

    async def _tail():
        # Backfill: read last N lines from file
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
                tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
                for line in tail:
                    stripped = line.rstrip("\n")
                    if stripped:
                        yield f"data: {json.dumps({'line': stripped})}\n\n"
                file_pos = f.tell()
        except FileNotFoundError:
            yield f"data: {json.dumps({'line': '[No log file yet — waiting for output...]'})}\n\n"
            file_pos = 0

        # Stream new lines as they appear
        while True:
            if await request.is_disconnected():
                break
            try:
                with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                    # Handle log rotation: if file shrank, reset to start
                    f.seek(0, 2)  # seek to end
                    current_size = f.tell()
                    if current_size < file_pos:
                        file_pos = 0
                    f.seek(file_pos)
                    new_lines = f.readlines()
                    if new_lines:
                        for line in new_lines:
                            stripped = line.rstrip("\n")
                            if stripped:
                                yield f"data: {json.dumps({'line': stripped})}\n\n"
                    file_pos = f.tell()
            except FileNotFoundError:
                pass
            await asyncio.sleep(1)

    return StreamingResponse(_tail(), media_type="text/event-stream")
