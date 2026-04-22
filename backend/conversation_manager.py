import sqlite3
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / ".freehive" / "conversations.db"


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names = set()
    for row in rows:
        if isinstance(row, sqlite3.Row):
            names.add(str(row["name"]))
        else:
            names.add(str(row[1]))
    return names


def init_db():
    """Create tables if they don't exist. Safe to call on every startup.

    ⚠️ NOTE: This is synchronous I/O (SQLite) called at app startup (main.py:35).
    It runs CREATE TABLE IF NOT EXISTS + lightweight schema migrations. On first
    run it creates the DB; on subsequent starts it's fast (schema already exists).
    Do NOT convert to async — SQLite doesn't benefit and it would complicate the
    startup sequence. The blocking time is <50ms on warm starts.
    """
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id                 TEXT PRIMARY KEY,
                model              TEXT NOT NULL,
                title              TEXT,
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL,
                codex_thread_uuid  TEXT,
                source             TEXT NOT NULL DEFAULT 'ui',
                provider           TEXT,
                external_key       TEXT,
                metadata_json      TEXT
            );

            CREATE TABLE IF NOT EXISTS messages (
                id          TEXT PRIMARY KEY,
                session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'text',
                meta_json    TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_sessions_updated
                ON sessions(updated_at DESC);
        """)

        # Lightweight schema migration for older local DBs.
        session_cols = _column_names(conn, "sessions")
        if "source" not in session_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN source TEXT NOT NULL DEFAULT 'ui'")
        if "provider" not in session_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN provider TEXT")
        if "external_key" not in session_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN external_key TEXT")
        if "metadata_json" not in session_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN metadata_json TEXT")

        message_cols = _column_names(conn, "messages")
        if "content_type" not in message_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text'")
        if "meta_json" not in message_cols:
            conn.execute("ALTER TABLE messages ADD COLUMN meta_json TEXT")

        # New indexes that rely on migrated columns.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_source_updated ON sessions(source, updated_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_external ON sessions(source, external_key)"
        )


def create_session(
    model: str,
    *,
    source: str = "ui",
    provider: str | None = None,
    external_key: str | None = None,
    title: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Create a new session and return it as a dict."""
    now = _now()
    session = {
        "id": str(uuid.uuid4()),
        "model": model,
        "title": title,
        "created_at": now,
        "updated_at": now,
        "codex_thread_uuid": None,
        "source": source,
        "provider": provider,
        "external_key": external_key,
        "metadata_json": json_dumps_or_none(metadata),
    }
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO sessions (
                id, model, title, created_at, updated_at, codex_thread_uuid,
                source, provider, external_key, metadata_json
            )
            VALUES (
                :id, :model, :title, :created_at, :updated_at, :codex_thread_uuid,
                :source, :provider, :external_key, :metadata_json
            )
        """, session)
    return session


def get_session(session_id: str) -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def list_sessions(model: str = None, source: str = None) -> list[dict]:
    with _get_conn() as conn:
        if model and source:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE model = ? AND source = ? ORDER BY updated_at DESC",
                (model, source)
            ).fetchall()
        elif model:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE model = ? ORDER BY updated_at DESC",
                (model,)
            ).fetchall()
        elif source:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE source = ? ORDER BY updated_at DESC",
                (source,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def update_session_title(session_id: str, title: str):
    with _get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, _now(), session_id)
        )


def update_codex_thread(session_id: str, thread_uuid: str):
    with _get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET codex_thread_uuid = ?, updated_at = ? WHERE id = ?",
            (thread_uuid, _now(), session_id)
        )


def touch_session(session_id: str):
    with _get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (_now(), session_id)
        )


def delete_session(session_id: str):
    with _get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


def get_session_char_count(session_id: str) -> tuple[int, int]:
    """Return (total_chars, message_count) for a session via SQL aggregate."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(content)), 0) AS total_chars, COUNT(*) AS msg_count "
            "FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return (row["total_chars"], row["msg_count"]) if row else (0, 0)


def list_arena_sessions(limit: int = 20) -> list[dict]:
    """Return recent arena sessions (model starts with 'arena/')."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE model LIKE 'arena/%' ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def reset_database():
    """Delete the DB file entirely and recreate with fresh schema."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()


def add_message(
    session_id: str,
    role: str,
    content: str,
    *,
    content_type: str = "text",
    meta: dict | list | None = None,
) -> dict:
    msg = {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "role": role,
        "content": content,
        "created_at": _now(),
        "content_type": content_type,
        "meta_json": json_dumps_or_none(meta),
    }
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO messages (id, session_id, role, content, created_at, content_type, meta_json)
            VALUES (:id, :session_id, :role, :content, :created_at, :content_type, :meta_json)
        """, msg)
    touch_session(session_id)
    return msg


def replace_messages(session_id: str, rows: list[dict]) -> None:
    """
    Replace all messages for a session with the given ordered rows.
    Each row accepts keys: role, content, content_type(optional), meta(optional/meta_json).
    """
    with _get_conn() as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        for row in rows:
            conn.execute(
                """
                INSERT INTO messages (id, session_id, role, content, created_at, content_type, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    session_id,
                    str(row.get("role") or "user"),
                    str(row.get("content") or ""),
                    _now(),
                    str(row.get("content_type") or "text"),
                    row.get("meta_json") if row.get("meta_json") is not None else json_dumps_or_none(row.get("meta")),
                ),
            )
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (_now(), session_id),
        )


def get_or_create_external_session(
    *,
    source: str,
    provider: str,
    model: str,
    external_key: str,
    title: str | None = None,
    metadata: dict | None = None,
) -> dict:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE source = ? AND external_key = ? ORDER BY updated_at DESC LIMIT 1",
            (source, external_key),
        ).fetchone()
        if row:
            session = dict(row)
            new_title = title or session.get("title")
            new_model = model or session.get("model")
            conn.execute(
                "UPDATE sessions SET model = ?, provider = ?, title = ?, metadata_json = ?, updated_at = ? WHERE id = ?",
                (
                    new_model,
                    provider,
                    new_title,
                    json_dumps_or_none(metadata) or session.get("metadata_json"),
                    _now(),
                    session["id"],
                ),
            )
            session["model"] = new_model
            session["provider"] = provider
            session["title"] = new_title
            session["metadata_json"] = json_dumps_or_none(metadata) or session.get("metadata_json")
            return session

    return create_session(
        model=model,
        source=source,
        provider=provider,
        external_key=external_key,
        title=title,
        metadata=metadata,
    )


def get_messages(session_id: str) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps_or_none(value) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return None
