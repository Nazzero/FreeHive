from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List
from backend.session_manager import SessionManager

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
            {"name": "chatgpt", "status": "coming_soon"},
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