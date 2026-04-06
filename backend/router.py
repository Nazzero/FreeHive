from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from backend.session_manager import SessionManager
from backend.account_store import add_account, get_accounts, remove_account
from backend import conversation_manager as cm

router = APIRouter()
session_manager = SessionManager()


# ── Request / Response models ─────────────────────────────────────────────────

class ChatRequest(BaseModel):
    model: str
    message: str
    session_id: str  # Required — create via POST /sessions first

class ChatResponse(BaseModel):
    model: str
    response: str
    session_id: str

class CreateSessionRequest(BaseModel):
    model: str

class CompareRequest(BaseModel):
    models: List[str]
    message: str
    session_ids: dict  # {"claude": "uuid", "chatgpt": "uuid"}


# ── Sessions ──────────────────────────────────────────────────────────────────

@router.post("/sessions")
async def create_session(request: CreateSessionRequest):
    """Create a new conversation session. Returns session_id to use in /chat."""
    valid = ["claude", "chatgpt", "gemini"]
    if request.model not in valid:
        raise HTTPException(status_code=400, detail=f"Model must be one of {valid}")
    session = cm.create_session(request.model)
    return session

@router.get("/sessions")
async def list_sessions(model: Optional[str] = None):
    """List all sessions, optionally filtered by model."""
    sessions = cm.list_sessions(model)
    return {"sessions": sessions}

@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    session = cm.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session

@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str):
    """Get full message history for a session."""
    session = cm.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    messages = cm.get_messages(session_id)
    return {"session_id": session_id, "messages": messages}

@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    session = cm.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    cm.delete_session(session_id)
    return {"deleted": True}


# ── Chat ──────────────────────────────────────────────────────────────────────

@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        response = await session_manager.send_message(
            request.model,
            request.message,
            request.session_id,
        )
        return ChatResponse(
            model=request.model,
            response=response,
            session_id=request.session_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/models")
async def get_models():
    return {
        "models": [
            {"name": "claude", "status": "active"},
            {"name": "chatgpt", "status": "active"},
            {"name": "gemini", "status": "active"},
        ]
    }

@router.post("/compare")
async def compare(request: CompareRequest):
    import asyncio

    async def fetch(model):
        session_id = request.session_ids.get(model)
        if not session_id:
            return model, f"Error: no session_id provided for {model}"
        try:
            response = await session_manager.send_message(model, request.message, session_id)
            return model, response
        except Exception as e:
            return model, f"Error: {str(e)}"

    results = await asyncio.gather(*[fetch(m) for m in request.models])
    return {"results": dict(results)}


@router.post("/chat/clear")
async def clear_history(model: str = None):
    if model:
        session_manager.clear_history(model)
    else:
        session_manager.clear_all_history()
    return {"cleared": True}


# ── Accounts ──────────────────────────────────────────────────────────────────

class AddAccountRequest(BaseModel):
    model: str
    username: str
    password: str

@router.post("/accounts")
async def create_account(request: AddAccountRequest):
    try:
        account = add_account(request.model, request.username, request.password)
        return account
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/accounts")
async def list_accounts(model: Optional[str] = None):
    try:
        accounts = get_accounts(model)
        return {"accounts": accounts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str):
    try:
        success = remove_account(account_id)
        if not success:
            raise HTTPException(status_code=404, detail="Account not found")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))