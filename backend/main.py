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
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── Logging setup ──────────────────────────────────────────────────────────
# Logs go to BOTH stdout (for dev terminal) and ~/.freehive/backend.log
# (for the in-app Backend Logs viewer). The file rotates at 2 MB, keeps 3 backups.
_LOG_DIR = Path.home() / ".freehive"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = _LOG_DIR / "backend.log"

class _FlushingRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that flushes after every emit for real-time tailing."""
    def emit(self, record):
        super().emit(record)
        self.flush()

_log_fmt = logging.Formatter(
    "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
_file_handler = _FlushingRotatingFileHandler(
    LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
_file_handler.setFormatter(_log_fmt)

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_log_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_stream_handler, _file_handler])

# Force uvicorn loggers to also write to our file handler.
# Uvicorn creates its own loggers with propagate=False and separate handlers,
# so access logs (e.g. "GET /api/... 200 OK") would otherwise never reach
# backend.log — making the in-app log viewer miss all HTTP traffic.
for _uvi_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
    _uvi_logger = logging.getLogger(_uvi_name)
    _uvi_logger.addHandler(_file_handler)

app = FastAPI(title="FreeHive API", version="0.5.3")

# ⚠️ DO NOT TOUCH — CORS must use allow_origins=["*"].
# Tauri WebView2 sends varying origins (tauri://, https://tauri.localhost,
# http://localhost:*) and specific origins break it. Attempted specific origins
# in the past — failed on Windows WebView2. This is safe because the API only
# binds to 127.0.0.1 (unreachable from network). See handoff 2026-04-13.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # must be False when allow_origins=["*"]
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")
app.include_router(setup_router, prefix="/api")
app.include_router(compat_router)  # no prefix — /v1/messages, /v1/chat/completions

# --- Manager wiring (must be AFTER app is defined) ---
# init_db() is sync but fast (<50ms on warm starts). See conversation_manager.py.
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


# ⚠️ DO NOT TOUCH — PyInstaller frozen detection below is required.
# In frozen bundles, uvicorn cannot resolve "backend.main:app" by module string
# because the filesystem layout differs. Must pass the app object directly.
# Removing the sys.frozen check breaks the packaged .exe. See handoff 2026-04-13.
if __name__ == "__main__":
    import sys
    _host = os.getenv("FREEHIVE_BACKEND_HOST", "127.0.0.1")
    _port = int(os.getenv("FREEHIVE_BACKEND_PORT", "7200"))
    if getattr(sys, "frozen", False):
        uvicorn.run(app, host=_host, port=_port)
    else:
        uvicorn.run(
            "backend.main:app",
            host=_host,
            port=_port,
            reload=os.getenv("FREEHIVE_BACKEND_RELOAD", "0").strip() == "1",
        )
