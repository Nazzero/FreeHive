"""
arena_bridge_adapter.py — FreeHive v0.6.0
Arena adapter that talks to the Extension Bridge via Native Messaging.
"""

import logging
import asyncio
import random
from typing import Optional, List, Dict, Any
from backend.services.arena_bridge_client import ArenaBridgeClient
from backend.services.arena_model_health import ArenaModelHealthStore
from backend.services.arena_bridge_transport import is_bridge_available

logger = logging.getLogger(__name__)

class ArenaBridgeAdapter:
    def __init__(self):
        self._client = ArenaBridgeClient()
        self._history: Dict[str, List[Dict[str, str]]] = {} # session_id -> history
        self._conversation_ids: Dict[str, Optional[str]] = {} # session_id -> conversation_id
        self._invalid_models: set[str] = set()
        self._health = ArenaModelHealthStore()

    async def send_message(self, message: str, model: str, session_id: str = "default") -> str:
        """Sends a message via the bridge and returns the full response text."""
        logger.info(f"[ArenaBridge] Sending message for model '{model}' in session '{session_id}'")
        blocked_reason = self._health.get_block_reason(model)
        if blocked_reason:
            raise RuntimeError(blocked_reason)
        attempts = 3
        full_text = ""
        error_info = None
        for attempt in range(1, attempts + 1):
            full_text = ""
            error_info = None
            current_conv_id = self._conversation_ids.get(session_id)

            async for update in self._client.send_chat(
                model=model,
                message=message,
                conversation_id=current_conv_id,
                session_id=session_id,
            ):
                if update["type"] == "JOB_UPDATE":
                    chunk = update.get("chunk", "")
                    full_text += chunk

                elif update["type"] == "JOB_COMPLETE":
                    full_text = update.get("full_text", full_text)
                    self._conversation_ids[session_id] = update.get("conversation_id")
                    logger.info(f"[ArenaBridge] Job complete. Conv ID: {self._conversation_ids[session_id]}")

                elif update["type"] == "JOB_ERROR":
                    error_info = update
                    break

            if error_info:
                if attempt < attempts and self._should_retry_error(error_info):
                    delay_s = self._retry_delay_seconds(error_info, attempt)
                    logger.warning(
                        "[ArenaBridge] transient error, retrying (%s/%s) in %.1fs: %s",
                        attempt,
                        attempts,
                        delay_s,
                        error_info.get("message", "unknown"),
                    )
                    await asyncio.sleep(delay_s)
                    continue
                err_msg = error_info.get("message", "Unknown error in Arena Bridge")
                err_code = error_info.get("error_code", "503")
                details = error_info.get("details") if isinstance(error_info.get("details"), dict) else {}
                status = int(details.get("status")) if str(details.get("status", "")).isdigit() else None
                diagnostics = details.get("diagnostics") if isinstance(details.get("diagnostics"), dict) else {}
                self._health.mark_error(
                    model,
                    status_code=status,
                    message=err_msg,
                    diagnostics=diagnostics,
                )
                err_msg_l = err_msg.lower()
                if status == 404 and "model not found" in err_msg_l:
                    self._mark_model_invalid(model)
                    self._conversation_ids[session_id] = None
                    err_msg = (
                        f"Arena model '{model}' is unavailable right now (Model not found). "
                        "Refresh models and choose another model."
                    )
                elif status == 422 and (
                    "not permitted to handle this type of question" in err_msg_l
                    or "please choose another model" in err_msg_l
                ):
                    self._mark_model_invalid(model)
                    self._conversation_ids[session_id] = None
                    err_msg = (
                        f"Arena model '{model}' cannot be used for conversational chat right now. "
                        "Refresh models and choose another model."
                    )
                elif status == 400 and "private models" in err_msg_l:
                    self._mark_model_invalid(model)
                    self._conversation_ids[session_id] = None
                    err_msg = (
                        f"Arena model '{model}' is private/battle-only and cannot be used in Direct mode. "
                        "Refresh models and choose another model."
                    )
                logger.error(f"[ArenaBridge] Job error: {err_msg} (code: {err_code})")
                raise RuntimeError(f"{err_msg}")

            if full_text.strip():
                break

        if not full_text.strip():
            raise RuntimeError("Arena Bridge returned an empty response.")
        self._health.mark_success(model)

        if session_id not in self._history:
            self._history[session_id] = []
        self._history[session_id].append({"role": "user", "content": message})
        self._history[session_id].append({"role": "assistant", "content": full_text})
        
        return full_text

    def _should_retry_error(self, error_info: Dict[str, Any]) -> bool:
        message = str(error_info.get("message", "") or "")
        message_l = message.lower()
        error_code = str(error_info.get("error_code", "") or "").lower()
        details = error_info.get("details") if isinstance(error_info.get("details"), dict) else {}
        status = int(details.get("status")) if str(details.get("status", "")).isdigit() else None
        diagnostics = details.get("diagnostics") if isinstance(details.get("diagnostics"), dict) else {}
        retry_after_raw = str(diagnostics.get("retry_after_header", "") or "").strip()
        retry_after = self._parse_retry_after_seconds(retry_after_raw)

        if error_code == "job_timeout":
            return True
        if status == 429:
            if retry_after is not None and retry_after >= 120:
                return False
            return True
        if status in {500, 502, 503, 504}:
            return True
        if "prompt failed" in message_l:
            return True
        if "recaptcha validation failed" in message_l:
            return True
        return bool(error_info.get("retryable"))

    def _retry_delay_seconds(self, error_info: Dict[str, Any], attempt: int) -> float:
        details = error_info.get("details") if isinstance(error_info.get("details"), dict) else {}
        status = int(details.get("status")) if str(details.get("status", "")).isdigit() else None
        diagnostics = details.get("diagnostics") if isinstance(details.get("diagnostics"), dict) else {}
        retry_after_raw = str(diagnostics.get("retry_after_header", "") or "").strip()

        if status == 429:
            retry_after = self._parse_retry_after_seconds(retry_after_raw)
            if retry_after is not None:
                return min(max(float(retry_after), 1.0), 15.0)
            return min(2.0 + (attempt - 1) * 2.0 + random.uniform(0.2, 1.0), 12.0)

        if status in {500, 502, 503, 504}:
            return min(1.0 + attempt * 1.3 + random.uniform(0.1, 0.8), 8.0)

        message_l = str(error_info.get("message", "") or "").lower()
        if "timeout" in message_l:
            return min(1.5 + attempt * 1.2 + random.uniform(0.2, 0.8), 8.0)
        if "recaptcha validation failed" in message_l:
            return min(1.2 + attempt * 1.1 + random.uniform(0.1, 0.7), 8.0)

        return 1.5

    def _parse_retry_after_seconds(self, raw: str) -> float | None:
        text = str(raw or "").strip()
        if not text:
            return None
        if text.isdigit():
            return float(text)
        return None

    async def fetch_models(self) -> list[str]:
        """
        Fetch live model names from the extension/page bridge.
        Returns values normalized as `arena/<name>`.
        """
        models = await self._client.fetch_models()
        normalized: list[str] = []
        seen = set()
        for model in models:
            name = str(model).strip()
            if not name:
                continue
            model_id = name if name.startswith("arena/") else f"arena/{name}"
            if not self._is_likely_chat_model(model_id):
                continue
            if self._is_model_invalid(model_id):
                continue
            if model_id not in seen:
                seen.add(model_id)
                normalized.append(model_id)
        return self._health.filter_and_rank(normalized, unknown_cap=None)

    def _mark_model_invalid(self, model: str) -> None:
        key = str(model or "").strip().lower()
        if not key:
            return
        if key.startswith("arena/"):
            bare = key[len("arena/"):]
        else:
            bare = key
        self._invalid_models.add(key)
        self._invalid_models.add(f"arena/{bare}")
        self._invalid_models.add(bare)

    def _is_model_invalid(self, model: str) -> bool:
        key = str(model or "").strip().lower()
        if not key:
            return False
        if key.startswith("arena/"):
            bare = key[len("arena/"):]
        else:
            bare = key
        return key in self._invalid_models or bare in self._invalid_models or f"arena/{bare}" in self._invalid_models

    def _is_likely_chat_model(self, model: str) -> bool:
        text = str(model or "").strip().lower()
        if not text:
            return False
        bare = text[len("arena/"):] if text.startswith("arena/") else text

        blocked = [
            "image",
            "vision",
            "video",
            "audio",
            "speech",
            "transcrib",
            "tts",
            "asr",
            "embedding",
            "rerank",
            "ocr",
            "diffusion",
            "sdxl",
            "midjourney",
            "dall-e",
            "paint",
        ]
        allow_override = ["chat", "instruct", "text", "reason", "code"]
        has_blocked = any(word in bare for word in blocked)
        if has_blocked and not any(word in bare for word in allow_override):
            return False
        return True

    async def load_history(self, messages: List[Dict[str, str]], session_id: str = "default"):
        self._history[session_id] = [{"role": m["role"], "content": m["content"]} for m in messages]
        logger.info(f"[ArenaBridge] Loaded {len(self._history[session_id])} messages for session {session_id}")

    def clear_history(self, session_id: str = "default"):
        if session_id in self._history:
            self._history[session_id] = []
        if session_id in self._conversation_ids:
            self._conversation_ids[session_id] = None
        logger.info(f"[ArenaBridge] History cleared for session {session_id}")

    async def is_authenticated(self) -> bool:
        return is_bridge_available()
