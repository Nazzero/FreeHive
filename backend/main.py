import os

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.router import router
from backend.setup_router import setup_router
from backend.compat_router import compat_router
from backend.session_manager import SessionManager
from backend.feature_flags import is_arena_enabled
import backend.conversation_manager as conversation_manager

import logging
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="FreeHive API", version="0.5.3")

DEFAULT_CORS_ORIGINS = [
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "tauri://localhost",
    "https://tauri.localhost",
]


def _cors_origins() -> list[str]:
    raw = os.getenv("FREEHIVE_CORS_ORIGINS", "").strip()
    if not raw:
        return DEFAULT_CORS_ORIGINS
    parsed = [item.strip() for item in raw.split(",") if item.strip()]
    return parsed or DEFAULT_CORS_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")
app.include_router(setup_router, prefix="/api")
app.include_router(compat_router)  # no prefix — /v1/messages, /v1/chat/completions

# --- Manager wiring (must be AFTER app is defined) ---
conversation_manager.init_db()

arena_enabled = is_arena_enabled()
session_manager = SessionManager(arena_enabled=arena_enabled)
arena_manager = None

if arena_enabled:
    from backend.arena_manager import ArenaManager
    arena_manager = ArenaManager()
    session_manager.set_arena_manager(arena_manager)

app.state.conversation_manager = conversation_manager
app.state.session_manager = session_manager
app.state.arena_manager = arena_manager
app.state.arena_enabled = arena_enabled
# -----------------------------------------------------


@app.get("/")
async def root():
    return {"status": "FreeHive API running", "version": "0.5.3"}


if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host=os.getenv("FREEHIVE_BACKEND_HOST", "127.0.0.1"),
        port=int(os.getenv("FREEHIVE_BACKEND_PORT", "7200")),
        reload=os.getenv("FREEHIVE_BACKEND_RELOAD", "0").strip() == "1",
    )
