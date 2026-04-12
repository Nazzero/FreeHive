"""
Persistent model-health registry for Arena model selection quality.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HEALTH_PATH = Path(
    os.getenv(
        "FREEHIVE_ARENA_MODEL_HEALTH",
        str(Path.home() / ".freehive" / "arena_model_health.json"),
    )
)

BLOCKING_STATUSES = {
    "invalid_not_found",
    "invalid_not_permitted",
    "invalid_private_direct",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


class ArenaModelHealthStore:
    def __init__(self, path: Path = HEALTH_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"version": 1, "models": {}}
        self._load()

    def normalize_model(self, model: str) -> str:
        text = str(model or "").strip().lower()
        if not text:
            return ""
        return text[6:] if text.startswith("arena/") else text

    def mark_success(self, model: str) -> None:
        key = self.normalize_model(model)
        if not key:
            return
        with self._lock:
            entry = self._entry(key)
            entry["status"] = "verified"
            entry["last_success_at"] = _iso_now()
            entry["cooldown_until"] = ""
            entry["last_error"] = ""
            self._save_locked()

    def mark_error(
        self,
        model: str,
        *,
        status_code: int | None,
        message: str,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        key = self.normalize_model(model)
        if not key:
            return
        diagnostics = diagnostics or {}
        classified = self._classify(status_code=status_code, message=message, diagnostics=diagnostics)

        with self._lock:
            entry = self._entry(key)
            entry["status"] = classified["status"]
            entry["last_error"] = str(message or "")[:600]
            entry["last_error_at"] = _iso_now()
            entry["error_count"] = int(entry.get("error_count", 0)) + 1
            cooldown_until = classified.get("cooldown_until", "")
            entry["cooldown_until"] = str(cooldown_until or "")
            self._save_locked()

    def status(self, model: str) -> str:
        key = self.normalize_model(model)
        if not key:
            return "unknown"
        with self._lock:
            models = self._state.get("models", {})
            entry = models.get(key, {})
            return str(entry.get("status", "unknown"))

    def get_block_reason(self, model: str) -> str:
        key = self.normalize_model(model)
        if not key:
            return ""
        with self._lock:
            entry = self._state.get("models", {}).get(key, {})
            if not isinstance(entry, dict):
                return ""
            status = str(entry.get("status", "unknown"))
            if status in BLOCKING_STATUSES:
                if status == "invalid_not_found":
                    return f"Arena model '{key}' is unavailable right now (Model not found). Refresh models and choose another model."
                if status == "invalid_not_permitted":
                    return f"Arena model '{key}' cannot be used for conversational chat right now. Refresh models and choose another model."
                if status == "invalid_private_direct":
                    return f"Arena model '{key}' is private/battle-only and cannot be used in Direct mode. Choose another model."
            if status == "cooldown":
                cooldown_until = self._parse_iso(entry.get("cooldown_until"))
                if cooldown_until and cooldown_until > _utc_now():
                    remaining = int((cooldown_until - _utc_now()).total_seconds())
                    return f"Arena model '{key}' is rate-limited. Try again in about {max(remaining, 1)} seconds."
            return ""

    def filter_and_rank(self, models: list[str], unknown_cap: int | None = None) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for model in models:
            raw = str(model or "").strip()
            if not raw:
                continue
            normalized = raw if raw.startswith("arena/") else f"arena/{raw}"
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(normalized)

        with self._lock:
            verified: list[str] = []
            soft: list[str] = []
            unknown: list[str] = []
            blocked: list[str] = []

            for model in cleaned:
                key = self.normalize_model(model)
                entry = self._state.get("models", {}).get(key, {})
                status = str(entry.get("status", "unknown"))

                if status in BLOCKING_STATUSES:
                    blocked.append(model)
                    continue

                if status == "cooldown":
                    cooldown_until = self._parse_iso(entry.get("cooldown_until"))
                    if cooldown_until and cooldown_until > _utc_now():
                        blocked.append(model)
                        continue
                    # cooldown expired, treat as soft candidate
                    soft.append(model)
                    continue

                if status == "verified":
                    verified.append(model)
                elif status in {"rate_limited", "recaptcha_failed", "transient_server"}:
                    soft.append(model)
                else:
                    unknown.append(model)

            if unknown_cap is None:
                unknown_kept = unknown
            else:
                unknown_kept = unknown[: max(int(unknown_cap), 0)]

            if verified:
                result = verified + soft + unknown_kept
            else:
                result = soft + unknown_kept

            return result

    def _classify(self, *, status_code: int | None, message: str, diagnostics: dict[str, Any]) -> dict[str, str]:
        text = str(message or "").lower()
        retry_after_raw = str(diagnostics.get("retry_after_header", "") or "").strip()
        retry_after = self._parse_retry_after_seconds(retry_after_raw)

        if status_code == 404 and "model not found" in text:
            return {"status": "invalid_not_found", "cooldown_until": ""}
        if status_code == 422 and ("not permitted" in text or "choose another model" in text):
            return {"status": "invalid_not_permitted", "cooldown_until": ""}
        if status_code == 400 and "private models" in text:
            return {"status": "invalid_private_direct", "cooldown_until": ""}
        if status_code == 429:
            if retry_after is not None and retry_after >= 120:
                return {
                    "status": "cooldown",
                    "cooldown_until": (_utc_now() + timedelta(seconds=retry_after)).isoformat(),
                }
            return {"status": "rate_limited", "cooldown_until": ""}
        if "recaptcha validation failed" in text:
            return {"status": "recaptcha_failed", "cooldown_until": ""}
        if status_code in {500, 502, 503, 504}:
            return {"status": "transient_server", "cooldown_until": ""}
        return {"status": "unknown", "cooldown_until": ""}

    def _entry(self, key: str) -> dict[str, Any]:
        models = self._state.setdefault("models", {})
        entry = models.get(key)
        if not isinstance(entry, dict):
            entry = {}
            models[key] = entry
        return entry

    def _parse_retry_after_seconds(self, raw: str) -> int | None:
        text = str(raw or "").strip()
        if not text:
            return None
        if text.isdigit():
            return int(text)
        parsed_dt = self._parse_iso(text)
        if parsed_dt is None:
            return None
        diff = int((parsed_dt - _utc_now()).total_seconds())
        return diff if diff > 0 else None

    def _parse_iso(self, value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _load(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                models = payload.get("models")
                if isinstance(models, dict):
                    self._state = {"version": 1, "models": models}
        except Exception:
            # Keep default empty state.
            return

    def _save_locked(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=True, indent=2), encoding="utf-8")
        tmp.replace(self._path)
