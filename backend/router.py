from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List
from backend.session_manager import SessionManager
from backend.account_store import add_account, get_accounts, remove_account

## Router.py

router = APIRouter()
session_manager = SessionManager()

class ChatRequest(BaseModel):
    model: str
    message: str

class ChatResponse(BaseModel):
    model: str
    response: str

class CompareRequest(BaseModel):
    models: List[str]
    message: str

@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        response = await session_manager.send_message(
            request.model,
            request.message
        )
        return ChatResponse(model=request.model, response=response)
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
            {"name": "gemini", "status": "coming_soon"}
        ]
    }

@router.post("/compare")
async def compare(request: CompareRequest):
    import asyncio

    async def fetch(model):
        try:
            response = await session_manager.send_message(model, request.message)
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
async def list_accounts(model: str = None):
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