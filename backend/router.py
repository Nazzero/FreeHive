"""
router.py — FreeHive v0.5.1
"""

from fastapi import APIRouter, HTTPException, Request
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
    _require_arena_enabled()
    am = request.app.state.arena_manager
    if am is None:
        raise HTTPException(status_code=503, detail="Arena manager unavailable")
    models = await am.get_models()
    return {"models": models}


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
