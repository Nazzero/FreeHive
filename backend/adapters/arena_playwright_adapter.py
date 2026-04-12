"""
arena_playwright_adapter.py — FreeHive v0.5.7
Arena adapter backed by CDP to a user-owned Chrome session.
"""

import asyncio
import json
import logging
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

ARENA_URL = "https://arena.ai/text/direct"
CDP_URL = "http://localhost:9222"
STREAM_ENDPOINT_FRAGMENT = "/nextjs-api/stream/"

LOGIN_SELECTORS = (
    'a[href*="sign-in"]',
    'a[href*="login"]',
    'button:has-text("Sign in")',
    'button:has-text("Log in")',
)

INPUT_SELECTORS = (
    'textarea[placeholder*="followup" i]',
    'textarea[placeholder*="ask" i]',
    'textarea[placeholder*="message" i]',
    'textarea[placeholder*="type" i]',
    'textarea[placeholder*="anything" i]',
    '[role="textbox"][contenteditable="true"]',
    'div[contenteditable="true"]',
    "textarea",
)

SEND_SELECTORS = (
    'button[type="submit"]',
    'button[aria-label*="send" i]',
    'button:has-text("Send")',
)

MODEL_PICKER_SELECTORS = (
    'button[aria-haspopup="dialog"]',
    '[role="combobox"]',
    'button[aria-haspopup="listbox"]',
    'button:has-text("Model")',
    'button:has-text("Choose model")',
)

class ArenaPlaywrightAdapter:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._history: list[dict] = []
        self._current_model: Optional[str] = None
        self._ready = False
        self._send_lock = asyncio.Lock()

    async def initialize(self, headless: bool = True):
        # CDP controls an already-running browser; headless flag is ignored here.
        del headless
        from playwright.async_api import async_playwright

        logger.info(f"[Arena] Connecting to Chrome via CDP at {CDP_URL}")
        self._playwright = await async_playwright().start()

        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(CDP_URL)
            contexts = self._browser.contexts
            if contexts:
                self._context = contexts[0]
                logger.info("[Arena] Using existing Chrome context")
            else:
                self._context = await self._browser.new_context()
                logger.info("[Arena] Created new Chrome context")

            self._page = await self._get_or_open_arena_tab()
            await self._ensure_arena_page()
            self._ready = True
            logger.info("[Arena] Connected to Chrome via CDP, ready")
        except Exception as e:
            await self._safe_shutdown_connection()
            raise RuntimeError(
                f"Could not connect to Chrome at {CDP_URL}. "
                f"Start Chrome with --remote-debugging-port=9222. Error: {e}"
            ) from e

    async def send_message(self, message: str, model: str) -> str:
        if not self._ready:
            raise RuntimeError("Arena adapter is not initialized.")

        async with self._send_lock:
            await self._ensure_arena_page()

            if not await self.is_logged_in():
                raise RuntimeError(
                    "Arena login required. Sign in on arena.ai in the CDP-connected Chrome window."
                )

            await self._ensure_model_selected(model)
            response_text, capture = await self._send_once(message)

            # Arena occasionally returns "prompt failed" when a chat state is stale.
            # In that case, reset to New Chat and retry once.
            if not response_text and "prompt failed" in capture.get("raw_preview", "").lower():
                logger.warning("[Arena] prompt failed detected; resetting chat and retrying once.")
                await self._start_new_chat()
                await self._ensure_model_selected(model)
                response_text, capture = await self._send_once(message)

            if not response_text.strip():
                raise RuntimeError(
                    "Arena returned an empty response. Verify login and that the selected model is available."
                )

            self._history.append({"role": "user", "content": message})
            self._history.append({"role": "assistant", "content": response_text})
            return response_text

    async def _send_once(self, message: str) -> tuple[str, dict]:
        before_messages = await self._snapshot_assistant_messages()
        capture_task = asyncio.create_task(self._capture_next_stream_text(timeout_ms=130000))

        try:
            await self._type_and_send(message)
        except Exception:
            capture_task.cancel()
            await self._clear_stream_hook()
            raise

        response_text = ""
        capture: dict = {}
        try:
            capture = await capture_task
            if capture.get("ok") and capture.get("text", "").strip():
                response_text = capture["text"].strip()
            else:
                logger.warning(
                    "[Arena] Stream capture failed/empty. error=%s preview=%s",
                    capture.get("error", "no error detail"),
                    capture.get("raw_preview", "")[:200],
                )
        except Exception as e:
            logger.warning("[Arena] Stream capture raised %s; using DOM fallback", e)

        if not response_text and "prompt failed" in capture.get("raw_preview", "").lower():
            return "", capture

        if not response_text:
            response_text = await self._wait_for_response_dom(before_messages)

        return response_text, capture

    async def load_history(self, messages: list[dict]):
        self._history = [{"role": m["role"], "content": m["content"]} for m in messages]
        logger.info("[Arena] Loaded %s messages from DB", len(self._history))

    def clear_history(self):
        self._history = []
        self._current_model = None
        logger.info("[Arena] History cleared")

    async def fetch_models(self) -> list[str]:
        if not self._ready:
            return self._fallback_models()

        try:
            await self._ensure_arena_page()
            html = await self._page.content()
            models = _extract_models_from_html(html)
            if models:
                return models
        except Exception as e:
            logger.warning("[Arena] Failed to fetch models from page: %s", e)

        return self._fallback_models()

    async def is_logged_in(self) -> bool:
        if not self._ready:
            return False

        try:
            await self._ensure_arena_page()
        except Exception:
            return False

        url = (self._page.url or "").lower()
        if "sign-in" in url or "login" in url:
            return False

        for selector in LOGIN_SELECTORS:
            try:
                el = await self._page.query_selector(selector)
                if el and await el.is_visible():
                    return False
            except Exception:
                continue

        return await self._find_input_element(timeout_ms=3000) is not None

    async def open_login_tab(self):
        await self._ensure_arena_page()
        try:
            await self._page.bring_to_front()
        except Exception:
            pass

    async def shutdown(self):
        # Do not close the user's Chrome process. Just detach Playwright.
        await self._safe_shutdown_connection()
        logger.info("[Arena] Disconnected from Chrome")

    async def _safe_shutdown_connection(self):
        self._ready = False
        self._page = None
        self._context = None
        self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._playwright = None

    async def _ensure_arena_page(self):
        if not self._context:
            raise RuntimeError("Arena browser context is unavailable.")

        if self._page is None or self._page.is_closed():
            self._page = await self._get_or_open_arena_tab()

        url = self._page.url or ""
        if "arena.ai" not in url:
            await self._page.goto(ARENA_URL, wait_until="domcontentloaded", timeout=45000)
        elif "/text/direct" not in url:
            try:
                await self._page.goto(ARENA_URL, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass

        try:
            await self._page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass

    async def _get_or_open_arena_tab(self):
        for page in self._context.pages:
            if "arena.ai" in (page.url or ""):
                logger.info("[Arena] Reusing arena.ai tab: %s", page.url)
                return page

        logger.info("[Arena] Opening new arena.ai tab")
        page = await self._context.new_page()
        await page.goto(ARENA_URL, wait_until="domcontentloaded", timeout=45000)
        return page

    async def _ensure_model_selected(self, model: str):
        if not model:
            return
        if self._current_model == model:
            return

        model_name = model.replace("arena/", "", 1)
        clicked = await self._click_model_option(model_name)
        if not clicked:
            picker_opened = await self._open_model_picker()
            if picker_opened:
                clicked = await self._click_model_option(model_name)

        if clicked:
            self._current_model = model
            await self._page.wait_for_timeout(250)
            logger.info("[Arena] Selected model: %s", model_name)
        else:
            logger.info(
                "[Arena] Could not confirm model switch to '%s'; using current Arena selection.",
                model_name,
            )

    async def _open_model_picker(self) -> bool:
        for selector in MODEL_PICKER_SELECTORS:
            try:
                elements = await self._page.query_selector_all(selector)
                for el in elements:
                    if not await el.is_visible():
                        continue
                    label = (
                        await el.evaluate(
                            "node => String(node.innerText || node.textContent || '').trim()"
                        )
                    ).lower()

                    # Skip mode picker controls; we only want the model picker.
                    if label in {"direct", "battle", "battle mode"}:
                        continue

                    await el.click()
                    await self._page.wait_for_timeout(250)
                    return True
            except Exception:
                continue
        return False

    async def _click_model_option(self, model_name: str) -> bool:
        try:
            dialog = self._page.locator('[role="dialog"]')
            if await dialog.count() > 0:
                search_input = dialog.locator(
                    'input[placeholder*="Search" i], input[type="text"]'
                )
                if await search_input.count() > 0:
                    await search_input.first.fill(model_name)
                    await self._page.wait_for_timeout(220)

                exact_candidates = dialog.locator(
                    f'button:has-text("{model_name}"), '
                    f'[role="option"]:has-text("{model_name}"), '
                    f'[role="menuitem"]:has-text("{model_name}")'
                )
                candidate_count = await exact_candidates.count()
                for i in range(min(candidate_count, 8)):
                    item = exact_candidates.nth(i)
                    if await item.is_visible():
                        await item.click()
                        return True
        except Exception:
            pass

        try:
            return await self._page.evaluate(
                """(targetName) => {
                    const normalize = (s) => String(s || "").toLowerCase().replace(/\\s+/g, " ").trim();
                    const target = normalize(targetName);
                    if (!target) return false;

                    const isVisible = (el) => {
                      if (!el) return false;
                      const rect = el.getBoundingClientRect();
                      if (rect.width <= 0 || rect.height <= 0) return false;
                      const style = window.getComputedStyle(el);
                      return style.visibility !== "hidden" && style.display !== "none";
                    };

                    const roots = [];
                    const overlaySelectors = [
                      '[role="dialog"]',
                      '[role="listbox"]',
                      '[role="menu"]',
                      '[data-radix-popper-content-wrapper]',
                      '[class*="popover"]',
                      '[class*="dropdown"]',
                    ];
                    for (const sel of overlaySelectors) {
                      for (const node of document.querySelectorAll(sel)) roots.push(node);
                    }
                    if (!roots.length) roots.push(document);

                    const candidates = [];
                    for (const root of roots) {
                      for (const el of root.querySelectorAll('button, [role="option"], [role="menuitem"], li, div')) {
                        if (!isVisible(el)) continue;
                        const text = normalize(el.innerText || el.textContent);
                        if (!text) continue;
                        candidates.push({ el, text });
                      }
                    }

                    for (const c of candidates) {
                      if (c.text === target) {
                        c.el.click();
                        return true;
                      }
                    }
                    for (const c of candidates) {
                      if (c.text.includes(target)) {
                        c.el.click();
                        return true;
                      }
                    }
                    return false;
                }""",
                model_name,
            )
        except Exception:
            return False

    async def _find_input_element(self, timeout_ms: int = 15000):
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            for selector in INPUT_SELECTORS:
                try:
                    el = await self._page.query_selector(selector)
                    if el and await el.is_visible():
                        return el
                except Exception:
                    continue
            await asyncio.sleep(0.2)
        return None

    async def _type_and_send(self, message: str):
        await self._dismiss_blocking_dialogs()
        input_el = await self._find_input_element(timeout_ms=15000)
        if not input_el:
            raise RuntimeError("Could not find Arena input box.")

        try:
            await input_el.click(timeout=3500)
        except Exception:
            await self._dismiss_blocking_dialogs()
            await input_el.click(force=True, timeout=3500)
        tag = await input_el.evaluate("el => (el.tagName || '').toUpperCase()")
        is_content_editable = await input_el.evaluate("el => !!el.isContentEditable")

        if tag == "TEXTAREA":
            await input_el.fill(message)
        elif is_content_editable:
            await input_el.press("Control+A")
            await input_el.press("Backspace")
            await self._page.keyboard.type(message)
        else:
            await input_el.fill(message)

        await self._page.wait_for_timeout(120)

        send_clicked = False
        for selector in SEND_SELECTORS:
            try:
                btn = await self._page.query_selector(selector)
                if btn and await btn.is_visible():
                    await btn.click()
                    send_clicked = True
                    break
            except Exception:
                continue

        if not send_clicked:
            await input_el.press("Enter")

    async def _dismiss_blocking_dialogs(self):
        if not self._page:
            return
        try:
            await self._page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            await self._page.evaluate(
                """() => {
                    const isVisible = (el) => {
                      if (!el) return false;
                      const rect = el.getBoundingClientRect();
                      if (rect.width <= 0 || rect.height <= 0) return false;
                      const style = window.getComputedStyle(el);
                      return style.visibility !== "hidden" && style.display !== "none";
                    };
                    const closeWords = ["close", "cancel", "dismiss", "not now"];
                    for (const el of document.querySelectorAll('button, [role="button"]')) {
                      if (!isVisible(el)) continue;
                      const text = String(el.innerText || el.textContent || "").toLowerCase().trim();
                      if (!text) continue;
                      if (closeWords.includes(text)) {
                        el.click();
                        return;
                      }
                    }
                }"""
            )
        except Exception:
            pass

    async def _capture_next_stream_text(self, timeout_ms: int = 120000) -> dict:
        if not self._page:
            return {"ok": False, "error": "No arena page available", "text": ""}

        return await self._page.evaluate(
            """({ timeoutMs, endpointFragment }) => {
                const w = window;
                return new Promise((resolve) => {
                    const previousRestore = w.__freehiveArenaRestoreFetch;
                    if (typeof previousRestore === "function") {
                        try { previousRestore(); } catch (e) {}
                    }

                    const originalFetch = w.fetch.bind(w);
                    let finished = false;
                    let streamSeen = false;
                    let textOut = "";
                    let rawPreview = "";

                    const consumeLine = (line) => {
                        const original = String(line || "");
                        if (original.trim()) rawPreview += original + "\\n";

                        let normalized = original.trim();
                        if (!normalized) return;
                        if (normalized.startsWith("data:")) normalized = normalized.slice(5).trim();
                        if (!normalized || normalized === "[DONE]") return;

                        if (normalized.startsWith("a0:")) {
                            try {
                                textOut += JSON.parse(normalized.slice(3));
                            } catch (e) {}
                            return;
                        }

                        const prefixedString = normalized.match(/^[a-z]\\d:(.*)$/i);
                        if (prefixedString) {
                            try {
                                const parsed = JSON.parse(prefixedString[1]);
                                if (typeof parsed === "string") {
                                    textOut += parsed;
                                }
                            } catch (e) {}
                            return;
                        }

                        if (normalized.startsWith("{") && normalized.endsWith("}")) {
                            try {
                                const obj = JSON.parse(normalized);
                                const maybeText = obj.delta || obj.text || obj.content || "";
                                if (typeof maybeText === "string") {
                                    textOut += maybeText;
                                    return;
                                }
                            } catch (e) {}
                        }

                        if (!normalized.includes(":")) {
                            textOut += normalized + "\\n";
                        }
                    };

                    const finish = (payload) => {
                        if (finished) return;
                        finished = true;
                        try { w.fetch = originalFetch; } catch (e) {}
                        try { w.__freehiveArenaRestoreFetch = null; } catch (e) {}
                        clearTimeout(timer);
                        resolve({
                            ok: !!payload.ok,
                            text: String(payload.text || "").trim(),
                            error: payload.error ? String(payload.error) : "",
                            stream_seen: streamSeen,
                            raw_preview: String(rawPreview || "").slice(0, 4000),
                        });
                    };

                    w.__freehiveArenaRestoreFetch = () => {
                        try { w.fetch = originalFetch; } catch (e) {}
                    };

                    const timer = setTimeout(() => {
                        finish({
                            ok: String(textOut || "").trim().length > 0,
                            text: textOut,
                            error: "Timed out waiting for Arena stream",
                        });
                    }, timeoutMs || 120000);

                    w.fetch = async (...args) => {
                        const req = args[0];
                        const url = typeof req === "string" ? req : (req && req.url ? String(req.url) : "");
                        const response = await originalFetch(...args);

                        try {
                            if (!streamSeen && url.includes(endpointFragment)) {
                                streamSeen = true;
                                const clone = response.clone();
                                const reader = clone.body && clone.body.getReader ? clone.body.getReader() : null;

                                if (!reader) {
                                    const text = await clone.text();
                                    for (const line of String(text || "").split(/\\r?\\n/)) consumeLine(line);
                                    finish({
                                        ok: String(textOut || "").trim().length > 0,
                                        text: textOut || text,
                                        error: "",
                                    });
                                    return response;
                                }

                                const decoder = new TextDecoder();
                                let buffer = "";
                                while (true) {
                                    const { value, done } = await reader.read();
                                    if (value) buffer += decoder.decode(value, { stream: true });
                                    if (done) buffer += decoder.decode();

                                    const lines = buffer.split(/\\r?\\n/);
                                    buffer = lines.pop() || "";
                                    for (const line of lines) consumeLine(line);
                                    if (done) break;
                                }
                                if (buffer.trim()) consumeLine(buffer);

                                finish({
                                    ok: String(textOut || "").trim().length > 0,
                                    text: textOut,
                                    error: "",
                                });
                            }
                        } catch (e) {
                            finish({
                                ok: String(textOut || "").trim().length > 0,
                                text: textOut,
                                error: String(e),
                            });
                        }

                        return response;
                    };
                });
            }""",
            {
                "timeoutMs": timeout_ms,
                "endpointFragment": STREAM_ENDPOINT_FRAGMENT,
            },
        )

    async def _clear_stream_hook(self):
        if not self._page:
            return
        try:
            await self._page.evaluate(
                """() => {
                    const restore = window.__freehiveArenaRestoreFetch;
                    if (typeof restore === "function") {
                        try { restore(); } catch (e) {}
                    }
                    window.__freehiveArenaRestoreFetch = null;
                }"""
            )
        except Exception:
            pass

    async def _start_new_chat(self):
        if not self._page:
            return
        try:
            clicked = await self._page.evaluate(
                """() => {
                    const isVisible = (el) => {
                      if (!el) return false;
                      const rect = el.getBoundingClientRect();
                      if (rect.width <= 0 || rect.height <= 0) return false;
                      const style = window.getComputedStyle(el);
                      return style.visibility !== "hidden" && style.display !== "none";
                    };
                    const candidates = document.querySelectorAll('button, a, [role="button"]');
                    for (const el of candidates) {
                      if (!isVisible(el)) continue;
                      const text = String(el.innerText || el.textContent || "").replace(/\\s+/g, " ").trim();
                      if (text === "New Chat") {
                        el.click();
                        return true;
                      }
                    }
                    return false;
                }"""
            )
            if clicked:
                await self._page.wait_for_timeout(500)
        except Exception:
            pass

    async def _wait_for_response_dom(self, before_messages: list[str], timeout_s: int = 120) -> str:
        deadline = time.monotonic() + timeout_s
        last_text = ""
        stable_cycles = 0

        while time.monotonic() < deadline:
            current_messages = await self._snapshot_assistant_messages()
            candidate = _pick_latest_response_delta(before_messages, current_messages)

            if candidate:
                if candidate == last_text:
                    stable_cycles += 1
                else:
                    last_text = candidate
                    stable_cycles = 1

                if stable_cycles >= 3 and len(last_text.strip()) >= 3:
                    return last_text.strip()

            await asyncio.sleep(0.7)

        return last_text.strip()

    async def _snapshot_assistant_messages(self) -> list[str]:
        if not self._page:
            return []

        try:
            messages = await self._page.evaluate(
                """() => {
                    const selectors = [
                      '[data-message-author-role="assistant"]',
                      '[data-role="assistant"]',
                      '[data-testid*="assistant"]',
                      '[class*="assistant-message"]',
                      '[class*="assistant"]',
                      'main article',
                      'main [role="article"]',
                    ];

                    const skipMarkers = [
                      "inputs are processed",
                      "your conversations",
                      "follow us",
                      "product walkthroughs",
                      "terms of use",
                      "privacy policy",
                      "by clicking",
                      "agree to be bound",
                      "how likely are you",
                      "security check",
                    ];

                    const normalize = (s) => String(s || "").replace(/\\s+/g, " ").trim();
                    const out = [];
                    const seen = new Set();

                    const isVisible = (el) => {
                      if (!el) return false;
                      const rect = el.getBoundingClientRect();
                      if (rect.width <= 0 || rect.height <= 0) return false;
                      const style = window.getComputedStyle(el);
                      return style.visibility !== "hidden" && style.display !== "none";
                    };

                    const pushText = (text) => {
                      const normalized = normalize(text);
                      if (!normalized) return;
                      if (normalized.length < 3) return;
                      const lower = normalized.toLowerCase();
                      for (const marker of skipMarkers) {
                        if (lower.includes(marker)) return;
                      }
                      if (!seen.has(normalized)) {
                        seen.add(normalized);
                        out.push(normalized);
                      }
                    };

                    const candidates = [];
                    for (const sel of selectors) {
                      for (const el of document.querySelectorAll(sel)) candidates.push(el);
                    }

                    for (const el of candidates) {
                        if (!isVisible(el)) continue;
                        pushText(el.innerText || el.textContent || "");
                    }

                    const bodyText = String(document.body?.innerText || "");
                    const battleMatch = bodyText.match(
                      /Assistant A\\s*([\\s\\S]*?)\\s*Assistant B\\s*([\\s\\S]*?)\\s*(A is better|Both are good|Both are bad|B is better|Add files|Inputs are processed)/i
                    );
                    if (battleMatch) {
                      const aText = normalize(battleMatch[1]);
                      const bText = normalize(battleMatch[2]);
                      if (aText || bText) {
                        pushText(`Assistant A:\\n${aText}\\n\\nAssistant B:\\n${bText}`);
                      }
                    }

                    return out.slice(-40);
                }"""
            )
            if isinstance(messages, list):
                return [str(m) for m in messages if isinstance(m, str)]
            return []
        except Exception:
            return []

    def _fallback_models(self) -> list[str]:
        return [
            "arena/gpt-4o",
            "arena/gpt-4.5-preview",
            "arena/claude-3-7-sonnet-20250219",
            "arena/claude-3-5-sonnet-20241022",
            "arena/gemini-2.0-flash-001",
            "arena/gemini-2.5-pro-preview-03-25",
            "arena/grok-3-beta",
            "arena/grok-3-mini-beta",
            "arena/deepseek-v3-0324",
            "arena/deepseek-r1",
            "arena/llama-4-maverick",
            "arena/llama-4-scout",
            "arena/mistral-large-2411",
            "arena/qwen-max-2025-01-25",
        ]


def _pick_latest_response_delta(before_messages: list[str], current_messages: list[str]) -> str:
    if not current_messages:
        return ""
    if not before_messages:
        return current_messages[-1]

    if len(current_messages) > len(before_messages):
        return current_messages[-1]

    if len(current_messages) == len(before_messages) and current_messages[-1] != before_messages[-1]:
        return current_messages[-1]

    return ""


def _extract_models_from_html(html: str) -> list[str]:
    patterns = [
        r'"initialModels":(\[.*?\]),"initialModel[A-Za-z]+Id"',
        r'\\"initialModels\\":(\[.*?\]),\\"initialModel[A-Za-z]+Id',
    ]

    parsed_models: list[dict] = []
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.DOTALL)
        if not match:
            continue

        models_blob = match.group(1)
        if '\\"' in models_blob:
            try:
                models_blob = models_blob.encode().decode("unicode_escape")
            except Exception:
                pass

        try:
            data = json.loads(models_blob)
            if isinstance(data, list):
                parsed_models = data
                break
        except Exception:
            continue

    result: list[str] = []
    seen: set[str] = set()
    for model in parsed_models:
        if not isinstance(model, dict):
            continue
        name = (
            model.get("publicName")
            or model.get("id")
            or model.get("name")
            or ""
        )
        name = str(name).strip()
        if not name:
            continue
        model_id = f"arena/{name}" if not name.startswith("arena/") else name
        if model_id not in seen:
            seen.add(model_id)
            result.append(model_id)
    return result
