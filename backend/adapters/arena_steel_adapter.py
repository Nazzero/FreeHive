"""
arena_steel_adapter.py — FreeHive v0.7.0

Arena.ai integration via CloakBrowser stealth Chromium.

Architecture:
  - CloakBrowser (patched Chromium with 49 C++ fingerprint patches) runs as a
    local process with a persistent profile (~/.freehive/arena_profile/).
  - Playwright BrowserContext is provided by StealthOrchestrator.
  - Requests are made by interacting with arena.ai's UI via Playwright.
  - Reasoning tokens (<think>…</think> etc.) are stripped before returning.
  - Tool calls accumulated from stream delta frames are returned alongside text.
  - When reCAPTCHA image challenge appears, captcha is screenshotted and sent
    to frontend for user to solve via /api/arena/captcha endpoints.

Setup:
  1. pip install cloakbrowser>=0.3.25  (binary auto-downloads on first use)
  2. Start backend → CloakBrowser Chrome window appears.
  3. Sign in to arena.ai via Google OAuth in the browser window.
  4. Session cookies persist in ~/.freehive/arena_profile/ across restarts.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# UUID pattern (v4 and arena.ai's v7-like IDs)
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

ARENA_MODEL_CACHE_PATH = Path.home() / ".freehive" / "arena_models_cache.json"


def _save_model_cache(models: list[str]) -> None:
    try:
        ARENA_MODEL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ARENA_MODEL_CACHE_PATH.write_text(
            json.dumps({"models": models, "saved_at": time.time()}, ensure_ascii=True),
            encoding="utf-8",
        )
    except Exception:
        pass


def _load_model_cache() -> list[str]:
    try:
        if ARENA_MODEL_CACHE_PATH.exists():
            data = json.loads(ARENA_MODEL_CACHE_PATH.read_text(encoding="utf-8"))
            return [str(m) for m in (data.get("models") or []) if m]
    except Exception:
        pass
    return []

from backend.services.arena_model_health import ArenaModelHealthStore
from backend.services.stealth_orchestrator import StealthOrchestrator

logger = logging.getLogger(__name__)

ARENA_DIRECT_URL = "https://arena.ai/text/direct"

# ---------------------------------------------------------------------------
# JS: click the model picker and harvest {id, name} pairs from the dropdown DOM.
# Arena.ai may not expose models via a dedicated API call (browser-cache / bundle).
# ---------------------------------------------------------------------------
_ARENA_JS_PICKER_EXTRACT = r"""
async () => {
    const seen = new Set();
    const results = [];

    function addText(t, source) {
        const v = String(t || '').trim();
        if (!v || v.length < 2 || v.length > 120) return;
        const k = v.toLowerCase();
        if (seen.has(k)) return;
        seen.add(k);
        results.push({id: v, name: v, source});
    }

    // ── Step 1: click the model picker ───────────────────────────────────
    const pickerSels = [
        '[cmdk-input]',
        'button[aria-haspopup="listbox"]',
        'button[aria-haspopup="dialog"]',
        'button[aria-haspopup]',
        '[role="combobox"]',
        '[data-testid*="model"]',
    ];
    let pickerEl = null;
    for (const sel of pickerSels) {
        const el = document.querySelector(sel);
        if (el) { pickerEl = el; break; }
    }
    if (pickerEl) {
        pickerEl.click();
        await new Promise(r => setTimeout(r, 2000));
    }

    // ── Step 2: scan entire DOM for model-name text ───────────────────────
    // Match provider prefixes common in arena.ai model names
    const PROVIDERS = /^(claude|gpt|o1|o3|o4|gemini|llama|mistral|qwen|deepseek|phi|grok|yi|solar|orca|wizard|nous|falcon|vicuna|command|titan|nova|haiku|sonnet|opus|flash|pro|ultra|mini|nano|turbo|mixtral|codex|palm|bard|copilot|ernie|spark|hunyuan|codegemma|codellama|stablelm|starcoder|replit)/i;
    const allItems = [...document.querySelectorAll('[cmdk-item], li, [role="option"], [role="menuitem"], button')]
        .filter(el => {
            const attr = el.getAttribute('data-value') || el.getAttribute('value');
            if (attr) return true;
            const t = (el.textContent || '').trim().toLowerCase();
            return t.length > 2 && t.length < 80 && PROVIDERS.test(t);
        });
        
    for (const el of allItems) {
        // data attribute takes priority (likely machine ID)
        const attr = el.getAttribute('data-value') || el.getAttribute('data-model-id') ||
                     el.getAttribute('data-id') || el.getAttribute('value') || '';
        if (attr) { addText(attr, el.tagName.toLowerCase() + '[attr]'); continue; }
        // fall back to visible text if it looks like a model name
        const txt = (el.childElementCount === 0 ? el.textContent : el.innerText || '').trim();
        if (txt) addText(txt, el.tagName.toLowerCase() + '[text]');
    }

    // ── Step 3: close dropdown ────────────────────────────────────────────
    document.dispatchEvent(new KeyboardEvent('keydown',
        {key: 'Escape', keyCode: 27, bubbles: true, cancelable: true}));

    // ── Step 4: scan performance timeline for non-ingest arena.ai URLs ────
    const timeline = performance.getEntriesByType('resource')
        .filter(e => e.name.includes('arena.ai') && !e.name.includes('/ingest/')
                  && !e.name.includes('google') && !e.name.includes('cdn-cgi'))
        .map(e => e.name);

    // ── Step 5: fetch leaderboard HTML (has a complete public model list) ──
    let leaderboardHtml = '';
    try {
        const lr = await Promise.race([
            fetch('https://arena.ai/leaderboard/text', {credentials: 'include'}),
            new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), 8000)),
        ]);
        if (lr.ok) leaderboardHtml = await lr.text();
    } catch(_) {}

    return {models: results, timeline, leaderboardHtml: leaderboardHtml.slice(0, 80000)};
}
"""

# ---------------------------------------------------------------------------
# Seed model list — only used when live extraction from arena.ai returns nothing.
# These slugs are populated at runtime by fetch_models() from the live page.
# Empty by default: arena.ai's model roster changes frequently and hardcoded
# slugs cause "not available for user selection" 400 errors.
# ---------------------------------------------------------------------------
ARENA_KNOWN_MODELS: list[str] = []

# ---------------------------------------------------------------------------
# JavaScript injected into the arena.ai page to perform the API fetch.
# Ported from arena_extension/page_bridge.js — same logic, no postMessage,
# returns a Promise that Playwright can await via page.evaluate().
# ---------------------------------------------------------------------------
_ARENA_JS_RUNNER = r"""
async ({model, message, conversationId, jobId}) => {
    const DEFAULT_SITEKEY_V3 = "6Led_uYrAAAAAKjxDIF58fgFtX3t8loNAK85bW9I";

    // ── SSE parsing ──────────────────────────────────────────────────────
    function extractSsePayload(line) {
        const t = line.trim();
        if (!t) return null;
        if (t.startsWith("data:")) return t.slice(5).trim();
        const i = t.indexOf(":");
        if (i > 0 && i < 8) return t.slice(i + 1).trim();
        return t;
    }

    function extractTextChunk(frame) {
        if (typeof frame === "string") return frame;
        if (!frame || typeof frame !== "object") return "";
        if (typeof frame.delta === "string") return frame.delta;
        if (typeof frame.text === "string") return frame.text;
        if (typeof frame.content === "string") return frame.content;
        if (Array.isArray(frame.choices) && frame.choices.length) {
            const c = frame.choices[0];
            if (c && typeof c === "object") {
                if (typeof c.delta === "string") return c.delta;
                if (c.delta && typeof c.delta.content === "string") return c.delta.content;
                if (typeof c.text === "string") return c.text;
            }
        }
        return "";
    }

    function extractToolCalls(frame) {
        if (!frame || typeof frame !== "object") return null;
        if (Array.isArray(frame.choices) && frame.choices.length) {
            const c = frame.choices[0];
            if (c && c.delta && Array.isArray(c.delta.tool_calls) && c.delta.tool_calls.length)
                return c.delta.tool_calls;
        }
        return null;
    }

    // ── Reasoning artifact removal ────────────────────────────────────────
    function stripReasoning(text) {
        let o = String(text || "");
        o = o.replace(/<think[\s\S]*?<\/think>/gi, "");
        o = o.replace(/<reasoning[\s\S]*?<\/reasoning>/gi, "");
        o = o.replace(/\[thinking\][\s\S]*?\[\/thinking\]/gi, "");
        o = o.replace(/```(?:thinking|reasoning)[\s\S]*?```/gi, "");
        const fm = o.match(/(?:^|\n)\s*(?:final answer|answer)\s*:\s*/i);
        if (fm && typeof fm.index === "number" && fm.index > 0 && fm.index < o.length * 0.8)
            o = o.slice(fm.index).replace(/^\s*(?:final answer|answer)\s*:\s*/i, "");
        o = o.replace(/^\s*(?:chain of thought|thought process|reasoning)\s*:.*$/gim, "");
        o = o.replace(/\n{3,}/g, "\n\n");
        return o.trim();
    }

    // ── Conversation ID extraction ────────────────────────────────────────
    function extractConvId(frame) {
        const keys = ["conversation_id", "evaluation_id", "evaluationId", "conversationId"];
        const uuidRe = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
        const arenaRe = /0[0-9a-z]{7}-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{12}/i;
        function walk(node, d) {
            if (d > 4 || node == null) return null;
            if (typeof node === "string") {
                const t = node.trim(); if (!t) return null;
                if (arenaRe.test(t) || uuidRe.test(t)) { const m = t.match(arenaRe)||t.match(uuidRe); return m?m[0]:t; }
                return null;
            }
            if (typeof node !== "object") return null;
            for (const k of keys) { if (typeof node[k] === "string" && node[k]) return node[k]; }
            if (typeof node.id === "string" && node.id) {
                const hint=[node.kind,node.type,node.object,node.entity].map(v=>String(v||"").toLowerCase()).join(" ");
                if (hint.includes("evaluation")||hint.includes("conversation")) return node.id;
            }
            if (Array.isArray(node)) { for (const v of node) { const r=walk(v,d+1); if(r) return r; } }
            for (const v of Object.values(node)) { const r=walk(v,d+1); if(r) return r; }
            return null;
        }
        return walk(frame, 0);
    }

    // ── Error detection ───────────────────────────────────────────────────
    function extractError(frame) {
        if (!frame) return null;
        if (typeof frame === "string") {
            if (frame.includes("prompt failed")) return {code:"prompt_failed",message:"Arena returned prompt failed"};
            return null;
        }
        if (typeof frame.error === "string" && frame.error)
            return {code:frame.error==="prompt failed"?"prompt_failed":"stream_error",message:frame.error};
        if (frame.error && typeof frame.error === "object")
            return {code:typeof frame.error.code==="string"?frame.error.code:"stream_error",
                    message:typeof frame.error.message==="string"?frame.error.message:"Unknown stream error",
                    details:frame.error};
        return null;
    }

    // ── UUID v7 ───────────────────────────────────────────────────────────
    function uuidV7() {
        const b=new Uint8Array(16); crypto.getRandomValues(b);
        const ts=BigInt(Date.now());
        b[0]=Number((ts>>40n)&0xffn);b[1]=Number((ts>>32n)&0xffn);b[2]=Number((ts>>24n)&0xffn);
        b[3]=Number((ts>>16n)&0xffn);b[4]=Number((ts>>8n)&0xffn);b[5]=Number(ts&0xffn);
        b[6]=(b[6]&0x0f)|0x70; b[8]=(b[8]&0x3f)|0x80;
        const h=Array.from(b,x=>x.toString(16).padStart(2,"0")).join("");
        return h.slice(0,8)+"-"+h.slice(8,12)+"-"+h.slice(12,16)+"-"+h.slice(16,20)+"-"+h.slice(20);
    }

    // ── Model ID resolution ───────────────────────────────────────────────
    function resolveModelId(slug) {
        const cleaned = String(slug||"").replace(/^arena\//, "").trim();
        // Hardcoded fallback map for well-known models
        const FALLBACK = {
            "gpt-5.2-chat-latest":          "019c5826-23b2-7ea7-be9a-b4557462fe19",
            "gemini-3.1-pro":               "019cc544-4848-771d-947a-1121fad1acb4",
            "claude-opus-4-6":              "019c2fac-13de-7550-a751-f5f593c77c72",
            "claude-opus-4-6-thinking":     "019c2f86-74db-7cc3-baa5-6891bebb5999",
        };
        // Try __NEXT_DATA__ first (most reliable)
        const nd = document.getElementById("__NEXT_DATA__");
        if (nd) {
            try {
                const map = new Map();
                function walkND(n, d) {
                    if (!n || d > 10) return;
                    if (Array.isArray(n)) { n.forEach(i=>walkND(i,d+1)); return; }
                    if (typeof n !== "object") return;
                    const id = String(n.id||n.modelId||n.model_id||"").trim();
                    if (id) { [n.publicName,n.name,n.displayName,n.slug,n.modelName]
                        .filter(v=>typeof v==="string"&&v.trim())
                        .forEach(nm=>map.set(nm.trim().toLowerCase(), id)); }
                    Object.values(n).forEach(v=>walkND(v,d+1));
                }
                walkND(JSON.parse(nd.textContent||""), 0);
                const found = map.get(cleaned.toLowerCase());
                if (found) return found;
            } catch(_) {}
        }
        return FALLBACK[cleaned.toLowerCase()] || cleaned;
    }

    // ── reCAPTCHA v3 ─────────────────────────────────────────────────────
    async function getRecaptchaToken() {
        try {
            const scripts = Array.from(document.scripts||[]).map(s=>String(s.src||""));
            let sitekey = DEFAULT_SITEKEY_V3;
            for (const src of scripts) {
                const m = src.match(/[?&]render=([^&]+)/);
                if (m && m[1]) { sitekey = decodeURIComponent(m[1]); break; }
            }
            const g = (window.grecaptcha&&window.grecaptcha.enterprise)||window.grecaptcha;
            if (!g || typeof g.execute !== "function") return "";
            if (typeof g.ready === "function") {
                await new Promise(r=>{ try{g.ready(r);}catch(_){r();} setTimeout(r,3000); });
            }
            const t = await Promise.race([
                Promise.resolve(g.execute(sitekey,{action:"chat_submit"})),
                new Promise(r=>setTimeout(()=>r(""),8000))
            ]);
            return typeof t==="string"?t:"";
        } catch(_) { return ""; }
    }

    // ── Model list — async so it can hit arena.ai's own API ──────────────
    async function getAvailableModels() {
        const names = new Set();
        const BLOCKED = ["image","vision","video","audio","speech","transcrib","tts","asr",
                         "embedding","rerank","ocr","diffusion","sdxl","midjourney","dall-e",
                         "paint","whisper","stable-diffusion","flux","sora"];
        function isChat(n) {
            const t = String(n||"").toLowerCase();
            return !BLOCKED.some(w => t.includes(w));
        }
        function tryAdd(val) {
            if (!val || typeof val !== "string") return;
            const v = val.trim();
            // Slug-like: no spaces, reasonable length, contains at least one hyphen or dot
            if (v.length < 3 || v.length > 120 || /\s/.test(v)) return;
            if (!isChat(v)) return;
            names.add(v);
        }
        function extractFromList(list) {
            if (!Array.isArray(list)) return 0;
            let added = 0;
            for (const m of list) {
                if (typeof m === "string") { tryAdd(m); added++; continue; }
                if (m && typeof m === "object") {
                    const n = m.slug || m.publicName || m.id || m.name || m.modelName || m.displayName || "";
                    if (n) { tryAdd(String(n)); added++; }
                }
            }
            return added;
        }

        // ── Strategy 1: arena.ai REST API (most reliable when logged in) ──
        const API_PATHS = [
            "/nextjs-api/models",
            "/nextjs-api/direct-chat/models",
            "/nextjs-api/available-models",
            "/nextjs-api/arena/models",
            "/api/v1/models",
            "/api/models",
        ];
        for (const path of API_PATHS) {
            try {
                const r = await Promise.race([
                    fetch(path, {credentials: "include", method: "GET"}),
                    new Promise((_,rej) => setTimeout(() => rej(new Error("timeout")), 4000)),
                ]);
                if (!r.ok) continue;
                const d = await r.json();
                const list = Array.isArray(d) ? d : (d.models || d.data || d.result || []);
                if (extractFromList(list) > 3) break;
            } catch(_) {}
        }

        // ── Strategy 2: __NEXT_DATA__ deep walk ──────────────────────────
        const nd = document.getElementById("__NEXT_DATA__");
        if (nd) {
            try {
                function walkND(n, d) {
                    if (!n || d > 12) return;
                    if (Array.isArray(n)) {
                        // If it looks like a list of model slugs, grab them all
                        if (n.length > 2 && typeof n[0] === "string" && n[0].includes("-"))
                            n.forEach(v => tryAdd(v));
                        n.forEach(i => walkND(i, d+1));
                        return;
                    }
                    if (typeof n !== "object") return;
                    tryAdd(n.slug); tryAdd(n.publicName); tryAdd(n.modelName);
                    // Grab arrays-of-strings that look like model slug lists
                    for (const v of Object.values(n)) {
                        if (Array.isArray(v) && v.length > 2 && typeof v[0] === "string" && v[0].includes("-"))
                            v.forEach(s => tryAdd(s));
                    }
                    Object.values(n).forEach(v => walkND(v, d+1));
                }
                walkND(JSON.parse(nd.textContent || ""), 0);
            } catch(_) {}
        }

        // ── Strategy 3: React fiber (client-side state) ───────────────────
        try {
            const root = document.getElementById("__next") || document.body;
            const fKey = Object.keys(root || {}).find(k => k.startsWith("__reactFiber"));
            if (fKey) {
                let visited = 0;
                function walkFiber(node, d) {
                    if (!node || d > 40 || visited++ > 5000) return;
                    try {
                        const p = node.memoizedProps || {};
                        // Combobox / select item props
                        if (p.value && typeof p.value === "string") tryAdd(p.value);
                        if (p["data-value"] && typeof p["data-value"] === "string") tryAdd(p["data-value"]);
                        // Props that look like model arrays
                        for (const val of Object.values(p)) {
                            if (Array.isArray(val) && val.length > 2)
                                extractFromList(val);
                        }
                        walkFiber(node.child, d+1);
                        walkFiber(node.sibling, d+1);
                    } catch(_) {}
                }
                walkFiber(root[fKey], 0);
            }
        } catch(_) {}

        // ── Strategy 4: DOM selector scan ────────────────────────────────
        try {
            const sels = ['[data-value]','option[value]','[role="option"]','[data-model-slug]','[data-model-id]','[data-testid*="model"]'];
            for (const sel of sels) {
                document.querySelectorAll(sel).forEach(el => {
                    tryAdd(el.getAttribute("data-value") || el.getAttribute("data-model-slug") ||
                           el.getAttribute("data-model-id") || el.getAttribute("value") || "");
                });
            }
        } catch(_) {}

        return Array.from(names).sort();
    }

    // ── Main fetch + SSE loop ─────────────────────────────────────────────
    if (model === "__FETCH_MODELS__") {
        return {models: await getAvailableModels(), text: "", conversationId: null, toolCalls: null};
    }

    const evalId  = conversationId || uuidV7();
    // Use pre-resolved UUID if provided, otherwise try to resolve
    const modelId = model.includes("-") && model.length > 30 ? model : resolveModelId(model);
    const endpoint = conversationId
        ? "/nextjs-api/stream/post-to-evaluation/" + encodeURIComponent(conversationId)
        : "/nextjs-api/stream/create-evaluation";

    // Small delay to ensure message UUIDs have later timestamps than eval ID
    await new Promise(r => setTimeout(r, 10));

    const recaptchaToken = await getRecaptchaToken();
    const payload = {
        id: evalId,
        mode: "direct-battle",
        modelAId: modelId,
        userMessageId:   uuidV7(),
        modelAMessageId: uuidV7(),
        userMessage: {content: String(message||""), experimental_attachments: [], metadata: {}},
        modality: "chat",
        recaptchaV3Token: recaptchaToken || ""
    };

    const makeHeaders = (token) => ({
        "Content-Type": "text/plain;charset=UTF-8",
        ...(token ? {"X-Recaptcha-Token": token, "X-Recaptcha-Action": "chat_submit"} : {})
    });

    const postPayload = (token) => fetch(endpoint, {
        method: "POST", credentials: "include",
        headers: makeHeaders(token), body: JSON.stringify(payload)
    });

    let resp = await postPayload(recaptchaToken);

    // 403 recaptcha retry — try once more with a fresh token.
    // Don't retry aggressively to avoid triggering 429 rate limits.
    // Python-side UI fallback handles persistent recaptcha failures.
    if (!resp.ok && resp.status === 403) {
        await new Promise(r => setTimeout(r, 2000));
        const fresh = await getRecaptchaToken();
        if (fresh) {
            payload.recaptchaV3Token = fresh;
            resp = await postPayload(fresh);
        }
    }
    // 404 model retry with DOM-detected ID
    if (!resp.ok && resp.status === 404) {
        const domEl = document.querySelector("[data-model-id],[data-selected-model-id]");
        const domId = domEl ? (domEl.getAttribute("data-model-id")||domEl.getAttribute("data-selected-model-id")||"").trim() : "";
        if (domId && domId !== payload.modelAId) {
            payload.modelAId = domId;
            resp = await postPayload(recaptchaToken);
        }
    }

    if (!resp.ok) {
        const body = await resp.text().catch(()=>"");
        return {
            error: {code:"http_error", message:`Arena ${endpoint} returned ${resp.status}${body?": "+body.slice(0,300):""}`,
                    details:{status:resp.status, body_preview:body.slice(0,300)}},
            text:"", conversationId:null, toolCalls:null
        };
    }
    if (!resp.body) {
        return {error:{code:"empty_stream",message:"Arena stream had no body"},text:"",conversationId:null,toolCalls:null};
    }

    const reader  = resp.body.getReader();
    const dec     = new TextDecoder();
    let buf = "", finalText = "", convId = conversationId || null, fatalErr = null;
    const toolCallMap = {};

    while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        buf += dec.decode(value, {stream: true});
        const lines = buf.split(/\r?\n/); buf = lines.pop()||"";
        for (const line of lines) {
            const raw = extractSsePayload(line); if (!raw) continue;
            let frame = raw;
            try { frame = JSON.parse(raw); } catch(_) {}
            finalText += extractTextChunk(frame);
            const err = extractError(frame); if (err) fatalErr = err;
            const cid = extractConvId(frame);  if (cid) convId = cid;
            const tcs = extractToolCalls(frame);
            if (tcs) {
                for (const tc of tcs) {
                    const idx = tc.index ?? 0;
                    if (!toolCallMap[idx]) toolCallMap[idx]={id:tc.id||uuidV7(),type:"function",function:{name:"",arguments:""}};
                    if (tc.function) {
                        if (tc.function.name) toolCallMap[idx].function.name += tc.function.name;
                        if (tc.function.arguments) toolCallMap[idx].function.arguments += tc.function.arguments;
                    }
                }
            }
        }
    }

    if (fatalErr) return {error:fatalErr, text:"", conversationId:convId, toolCalls:null};

    if (!convId && typeof payload.id==="string") convId = payload.id;
    if (!convId) {
        const pm = String(window.location.pathname||"").match(/\/c\/([0-9a-z-]{20,})/i);
        if (pm && pm[1]) convId = pm[1];
    }

    const toolCalls = Object.keys(toolCallMap).length>0
        ? Object.values(toolCallMap).filter(t=>t.function.name)
        : null;

    return {
        text: stripReasoning(finalText.trim()),
        conversationId: convId,
        toolCalls,
        error: null
    };
}
"""


class ArenaSteelAdapter:
    """
    Adapter that communicates with arena.ai through a Steel-managed browser session.

    A single Playwright page is kept alive and reused across requests.
    model format expected: "arena/<slug>" or bare "<slug>".
    """

    def __init__(self):
        self._orchestrator = StealthOrchestrator()
        self._health = ArenaModelHealthStore()
        self._conversation_ids: dict[str, Optional[str]] = {}
        self._history: dict[str, list[dict]] = {}
        self._page = None
        self._context = None
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()  # serialize send_message calls (one page, one request at a time)
        self._captured_models: list[str] = []  # intercepted from page network responses
        self._listener_pages: set[int] = set()  # id() of pages with response listener attached
        self._model_names: dict[str, str] = {}  # model_id → display name
        self._model_uuid_map: dict[str, str] = {}  # slug → UUID
        self._model_capabilities: dict[str, dict] = {}  # model_id → capability flags
        self._current_ui_model: str | None = None  # currently selected model in UI picker
        # Captcha solver state — shared between adapter and API endpoints
        self._captcha_pending = False
        self._captcha_image: bytes | None = None  # PNG screenshot of captcha bframe
        self._captcha_instruction: str = ""  # e.g. "Select all images with traffic lights"
        self._captcha_grid_size: int = 3  # 3x3 or 4x4
        self._captcha_event: asyncio.Event = asyncio.Event()  # signaled when user submits solution
        self._captcha_cells: list[int] = []  # cell numbers user clicked
        self._captcha_page = None  # page reference for clicking tiles
        self._captcha_bframe = None  # bframe reference for clicking tiles

    # ------------------------------------------------------------------
    # Network interception helpers
    # ------------------------------------------------------------------

    _MODEL_BLOCKED_KEYWORDS = frozenset([
        "image", "vision", "video", "audio", "speech", "transcrib", "tts", "asr",
        "embedding", "rerank", "ocr", "diffusion", "sdxl", "midjourney", "dall-e",
        "paint", "whisper", "stable-diffusion", "flux", "sora",
    ])

    def _is_chat_model(self, name: str) -> bool:
        n = str(name).lower()
        return not any(kw in n for kw in self._MODEL_BLOCKED_KEYWORDS)

    @staticmethod
    def _parse_capabilities(caps: dict, slug: str) -> dict:
        """Convert arena.ai capabilities object into flat boolean flags."""
        inp = caps.get("inputCapabilities", {})
        out = caps.get("outputCapabilities", {})
        slug_l = slug.lower()
        return {
            "vision": bool(inp.get("image")),
            "file_upload": bool(inp.get("file")),
            "web_browsing": bool(out.get("web")),
            "search": bool(out.get("search")),
            "reasoning": (
                slug_l.startswith("o3") or slug_l.startswith("o4")
                or slug_l.startswith("qwq")
                or (
                    ("think" in slug_l or "reason" in slug_l)
                    and "no-think" not in slug_l
                    and "non-think" not in slug_l
                    and "no-reason" not in slug_l
                    and "non-reason" not in slug_l
                )
            ),
        }

    def get_model_capabilities(self, model_id: str) -> dict:
        """Return capability flags for a model, or empty dict if unknown."""
        return self._model_capabilities.get(model_id, {})

    def _extract_models_from_json(self, data: object, seen: set[str] | None = None) -> list[str]:
        """Recursively pull model slugs from an arbitrary JSON structure."""
        if seen is None:
            seen = set()
        models: list[str] = []
        SLUG_KEYS = ("slug", "publicName", "id", "name", "modelName", "displayName", "model_id", "modelId")

        def try_add(val: object) -> None:
            if not isinstance(val, str):
                return
            v = val.strip()
            # Basic length + whitespace check
            if len(v) < 3 or len(v) > 120 or " " in v:
                return
            # Must look like a slug: hyphen or dot, no spaces
            if not ("-" in v or "." in v):
                return
            # Reject UUIDs (both v4 and arena.ai v7-like)
            if _UUID_RE.match(v):
                return
            # Must be lowercase (real model slugs are: gpt-4o, claude-sonnet-4-5, gemini-2.5-pro)
            if v != v.lower():
                return
            # Must contain at least one letter and one digit (filters out 'Text-to-Text' etc.)
            if not (any(c.isalpha() for c in v) and any(c.isdigit() for c in v)):
                return
            if not self._is_chat_model(v):
                return
            k = v.lower()
            if k not in seen:
                seen.add(k)
                models.append(v)

        def walk(node: object, depth: int = 0) -> None:
            if depth > 12 or node is None:
                return
            if isinstance(node, list):
                for item in node:
                    if isinstance(item, str):
                        try_add(item)
                    elif isinstance(item, dict):
                        for key in SLUG_KEYS:
                            try_add(item.get(key))
                        walk(item, depth + 1)
            elif isinstance(node, dict):
                for key in SLUG_KEYS:
                    try_add(node.get(key))
                for v in node.values():
                    if isinstance(v, (list, dict)):
                        walk(v, depth + 1)

        walk(data)
        return models

    async def _on_arena_response(self, response) -> None:
        """Playwright response handler — captures model list data from arena.ai API responses."""
        try:
            url = str(response.url or "")
            if "arena.ai" not in url:
                return
            if response.status != 200:
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            parsed = urlparse(url)
            path_only = parsed.path.lower()
            data = await response.json()
            extracted = self._extract_models_from_json(data)
            if extracted:
                logger.info(
                    "[ArenaSteelAdapter] Captured %d model slugs from: %s",
                    len(extracted), path_only,
                )
                # Merge into captured set (preserve ordering, no duplicates)
                existing_keys = {m.lower() for m in self._captured_models}
                for m in extracted:
                    if m.lower() not in existing_keys:
                        existing_keys.add(m.lower())
                        self._captured_models.append(m)
            else:
                # Log all arena.ai JSON endpoints so we can discover the model list endpoint
                logger.info("[ArenaSteelAdapter] JSON (no slugs extracted): %s", path_only)
        except Exception:
            pass  # never let interception errors bubble up

    def _extract_models_from_html_text(self, text: str) -> list[str]:
        """
        Extract model slugs from raw HTML/RSC/text by regex.
        Matches patterns like "claude-opus-4-6", "gpt-4o", "gemini-2.5-pro".
        """
        _PROVIDER_SLUG_RE = re.compile(
            r'\b((?:claude|gpt|o1|o3|o4|gemini|llama|mistral|qwen|deepseek|grok|phi|falcon|yi|'
            r'command|nova|haiku|sonnet|opus|flash|pro|turbo|mini|nano|mixtral|codex|'
            r'starcoder|replit|palm|ernie|spark|hunyuan)[-0-9][a-z0-9-._]{0,59})',
            re.IGNORECASE,
        )
        seen: set[str] = set()
        models: list[str] = []
        for m in _PROVIDER_SLUG_RE.finditer(text):
            slug = m.group(1).lower().rstrip('.-_')
            if not slug or _UUID_RE.match(slug):
                continue
            if not self._is_chat_model(slug):
                continue
            if slug not in seen:
                seen.add(slug)
                models.append(slug)
        return models

    async def _extract_rsc_models(self) -> list[str]:
        """Extract model list with UUID mappings from Next.js RSC payload (window.__next_f)."""
        try:
            page = await self._get_page()
            # Ensure page is on arena.ai direct chat (RSC data only loads there)
            if "text/direct" not in (page.url or ""):
                await page.goto("https://arena.ai/text/direct", wait_until="load", timeout=45000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
            rsc_models = await page.evaluate("""
                () => {
                    if (!window.__next_f) return [];
                    const models = [];
                    for (const chunk of window.__next_f) {
                        if (!Array.isArray(chunk) || typeof chunk[1] !== 'string') continue;
                        const text = chunk[1];
                        // Look for initialModels JSON arrays
                        let idx = 0;
                        while (true) {
                            const pos = text.indexOf('"id":', idx);
                            if (pos < 0) break;
                            // Walk backward to find the start of this object
                            let braceStart = text.lastIndexOf('{', pos);
                            if (braceStart < 0) { idx = pos + 5; continue; }
                            // Try to parse the object
                            let depth = 0;
                            let end = braceStart;
                            for (let i = braceStart; i < text.length && i < braceStart + 2000; i++) {
                                if (text[i] === '{') depth++;
                                else if (text[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
                            }
                            if (depth !== 0) { idx = pos + 5; continue; }
                            try {
                                const obj = JSON.parse(text.substring(braceStart, end));
                                if (obj.id && obj.userSelectable !== undefined &&
                                    (obj.publicName || obj.name || obj.displayName)) {
                                    models.push({
                                        id: obj.id,
                                        name: obj.name || '',
                                        publicName: obj.publicName || '',
                                        displayName: obj.displayName || '',
                                        organization: obj.organization || '',
                                        userSelectable: obj.userSelectable,
                                        capabilities: obj.capabilities || {},
                                        rankByModality: obj.rankByModality || {},
                                    });
                                }
                            } catch(_) {}
                            idx = end;
                        }
                    }
                    return models;
                }
            """)
            if rsc_models:
                slugs = []
                for m in rsc_models:
                    uuid_id = str(m.get("id", ""))
                    name = str(m.get("name", ""))
                    public_name = str(m.get("publicName", ""))
                    display_name = str(m.get("displayName", ""))
                    selectable = m.get("userSelectable", False)

                    if not uuid_id or not selectable:
                        continue

                    # Only include models with chat modality (Text tab on arena.ai)
                    modalities = m.get("rankByModality", {})
                    if "chat" not in modalities:
                        continue

                    # Skip internal/anonymous models (no organization = arena.ai test model)
                    org = str(m.get("organization", "")).strip()
                    if not org:
                        continue

                    # Build slug → UUID mapping (multiple keys for the same UUID)
                    if name:
                        self._model_uuid_map[name.lower()] = uuid_id
                    if public_name:
                        self._model_uuid_map[public_name.lower()] = uuid_id
                    if display_name:
                        self._model_uuid_map[display_name.lower()] = uuid_id

                    # Use 'name' as the primary slug
                    slug = name or public_name or display_name
                    if slug and self._is_chat_model(slug):
                        slugs.append(slug)
                        # Store display name
                        self._model_names[f"arena/{slug}"] = display_name or public_name or slug
                        # Extract capability flags
                        self._model_capabilities[f"arena/{slug}"] = self._parse_capabilities(
                            m.get("capabilities", {}), slug
                        )

                logger.info(
                    "[ArenaSteelAdapter] RSC extracted %d models, %d UUID mappings",
                    len(slugs), len(self._model_uuid_map),
                )
                return slugs
        except Exception as exc:
            logger.warning("[ArenaSteelAdapter] RSC model extraction failed: %s", exc)
        return []

    async def _fetch_models_via_rsc(self) -> list[str]:
        try:
            page = await self._get_page()
            rsc_text = await page.evaluate("""
                async () => {
                    const r = await fetch('/text/direct?_rsc=17dbu', {credentials: 'include'});
                    return r.ok ? await r.text() : '';
                }
            """)
            if rsc_text:
                slugs = self._extract_models_from_html_text(rsc_text)
                if slugs:
                    logger.info("[ArenaSteelAdapter] RSC fetch extracted %d model slugs", len(slugs))
                return slugs
        except Exception as exc:
            logger.warning("[ArenaSteelAdapter] RSC fetch failed: %s", exc)
        return []

    async def _fetch_models_via_picker(self) -> list[str]:
        """
        Click the model picker dropdown, read {id, name} pairs from the DOM,
        and re-fetch any non-ingest arena.ai API endpoints found in the
        performance timeline.  Returns a list of raw model slugs/IDs.
        """
        try:
            page = await self._get_page()
            logger.info("[ArenaSteelAdapter] fetch_models: running picker DOM extraction")
            result = await page.evaluate(_ARENA_JS_PICKER_EXTRACT)
        except Exception as exc:
            logger.warning("[ArenaSteelAdapter] picker extraction JS failed: %s", exc)
            return []

        picker_items: list[dict] = result.get("models") or []
        timeline_urls: list[str] = result.get("timeline") or []

        # Log timeline so we can see what endpoints arena.ai actually uses
        if timeline_urls:
            logger.info("[ArenaSteelAdapter] Performance timeline API calls (%d):", len(timeline_urls))
            for u in timeline_urls:
                logger.info("  %s", u)
        else:
            logger.info("[ArenaSteelAdapter] Performance timeline: no non-ingest arena.ai API calls found")

        # Process picker items from DOM scan
        raw: list[str] = []
        seen: set[str] = set()

        def _slug_from_name(name: str) -> str:
            s = name.lower().strip()
            s = re.sub(r'[^a-z0-9]+', '-', s)
            return s.strip('-')

        for item in picker_items:
            raw_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or raw_id).strip()
            if not raw_id:
                continue
            if _UUID_RE.match(raw_id):
                slug = raw_id
            elif " " in raw_id:
                slug = _slug_from_name(raw_id)
            else:
                slug = raw_id.lower()
            if slug and slug not in seen:
                seen.add(slug)
                raw.append(slug)
                if name and name.lower() != slug:
                    self._model_names[f"arena/{slug}"] = name

        if raw:
            logger.info("[ArenaSteelAdapter] picker DOM yielded %d models", len(raw))

        # We intentionally skip parsing the leaderboard HTML here because it contains
        # all historical/offline models that are no longer available for selection
        # and would pollute the active list and trigger 400 errors.

        # Re-fetch timeline endpoints for JSON model data (catches browser-cached model list)
        try:
            page = await self._get_page()
            for turl in timeline_urls:
                if len(raw) > 5:
                    break  # already have enough
                try:
                    tresult = await page.evaluate("""
                        async (url) => {
                            try {
                                const r = await fetch(url, {credentials: 'include'});
                                if (!r.ok) return null;
                                const ct = r.headers.get('content-type') || '';
                                if (!ct.includes('json')) return null;
                                return await r.json();
                            } catch(e) { return null; }
                        }
                    """, turl)
                    if tresult:
                        found = self._extract_models_from_json(tresult)
                        if found:
                            logger.info("[ArenaSteelAdapter] timeline refetch found %d models at %s", len(found), turl)
                            for m in found:
                                if m not in seen:
                                    seen.add(m)
                                    raw.append(m)
                except Exception:
                    pass
        except Exception:
            pass

        return raw

    # ------------------------------------------------------------------
    # Public interface (matches existing arena adapter API)
    # ------------------------------------------------------------------

    async def send_message(self, message: str, model: str, session_id: str = "default") -> str:
        """Send a message to arena.ai and return the text response."""
        blocked = self._health.get_block_reason(model)
        if blocked:
            raise RuntimeError(blocked)

        # Serialize all sends — single page can only handle one at a time
        async with self._send_lock:
            return await self._send_message_impl(message, model, session_id)

    async def raw_request(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        thinking_effort: str | None = None,
        session_id: str = "raw_default",
    ) -> dict:
        """Send message and return full Chat Completions-format response.

        Supports tool_calls if the arena.ai model returns them.
        Matches the raw_request() interface of Claude/ChatGPT/Gemini adapters.
        Conversation state is tracked by session_id — follow-up messages
        continue the same arena.ai conversation.
        """
        # Extract last user message
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, list):
                    last_user = " ".join(
                        p.get("text", "") for p in content if p.get("type") == "text"
                    )
                else:
                    last_user = str(content)
                break

        if not last_user:
            raise RuntimeError("No user message found in messages list.")

        model_id = self._current_ui_model or "arena/unknown"
        conv_id = self._conversation_ids.get(session_id)

        async with self._send_lock:
            slug = model_id.removeprefix("arena/")
            page = await self._get_page()
            result = await self._send_via_ui(page, slug, last_user, conv_id=conv_id)

        if not result:
            raise RuntimeError("Arena send failed — no response from UI.")

        if result.get("error"):
            err = result["error"]
            msg = str(err.get("message") or "Arena error")
            raise RuntimeError(msg)

        # Track conversation for continuity
        new_conv = result.get("conversationId")
        if new_conv:
            self._conversation_ids[session_id] = new_conv

        text = str(result.get("text") or "").strip()
        tool_calls_raw = result.get("toolCalls")

        # Build Chat Completions response
        assistant_msg: dict = {"role": "assistant", "content": text}
        finish_reason = "stop"

        if tool_calls_raw:
            assistant_msg["tool_calls"] = tool_calls_raw
            finish_reason = "tool_calls"

        return {
            "id": f"chatcmpl-arena-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_id,
            "choices": [{"index": 0, "message": assistant_msg, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    async def _send_message_impl(self, message: str, model: str, session_id: str) -> str:
        slug = model.removeprefix("arena/")
        conv_id = self._conversation_ids.get(session_id)

        # Resolve slug to UUID if we have the mapping
        resolved_model = slug
        if self._model_uuid_map:
            uuid_id = self._model_uuid_map.get(slug.lower())
            if uuid_id:
                resolved_model = uuid_id
                logger.info("[ArenaSteelAdapter] Resolved %s → %s", slug, uuid_id)
        else:
            # Try to load mapping on first use
            await self._extract_rsc_models()
            uuid_id = self._model_uuid_map.get(slug.lower())
            if uuid_id:
                resolved_model = uuid_id
                logger.info("[ArenaSteelAdapter] Resolved %s → %s (first load)", slug, uuid_id)

        page = await self._get_page()

        # Use UI path — send via Playwright interaction with arena.ai's React UI.
        # Pass conv_id so follow-up messages continue in the same conversation.
        logger.info("[ArenaAdapter] Sending via UI for %s (conv=%s)", slug, (conv_id or "new")[:12])
        result = await self._send_via_ui(page, slug, message, conv_id=conv_id)
        if not result:
            raise RuntimeError(
                f"Arena send failed for {slug}. UI fallback returned no response."
            )

        if result.get("error"):
            err = result["error"]
            if isinstance(err, dict):
                status = (err.get("details") or {}).get("status")
                msg = str(err.get("message") or "Arena error")
            else:
                status = None
                msg = str(err)
            logger.warning("[ArenaSteelAdapter] Error for %s: %s (status=%s)", slug, msg, status)
            self._health.mark_error(model, status_code=status, message=msg)
            raise RuntimeError(f"Arena error ({slug}): {msg}")

        text = str(result.get("text") or "").strip()
        if not text:
            raise RuntimeError("Arena returned an empty response.")

        new_conv = result.get("conversationId") or result.get("conversation_id")
        if new_conv:
            self._conversation_ids[session_id] = new_conv

        self._health.mark_success(model)

        # Store in local history
        self._history.setdefault(session_id, [])
        self._history[session_id].append({"role": "user", "content": message})
        self._history[session_id].append({"role": "assistant", "content": text})

        logger.info(
            "[ArenaSteelAdapter] session=%s model=%s conv=%s len=%d",
            session_id, slug, new_conv, len(text),
        )
        return text

    def _parse_sse_body(self, body: str) -> str:
        """Parse arena.ai SSE response body (a0: text chunks, ad: completion)."""
        text_parts = []
        for line in body.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Arena.ai SSE format: a0:"text chunk" for content
            if line.startswith("a0:"):
                payload = line[3:]
                try:
                    chunk = json.loads(payload)
                    if isinstance(chunk, str):
                        text_parts.append(chunk)
                except Exception:
                    pass
            # Also handle standard SSE data: prefix
            elif line.startswith("data:"):
                payload = line[5:].strip()
                try:
                    frame = json.loads(payload)
                    if isinstance(frame, str):
                        text_parts.append(frame)
                    elif isinstance(frame, dict):
                        text_parts.append(
                            frame.get("delta", "")
                            or frame.get("text", "")
                            or frame.get("content", "")
                        )
                except Exception:
                    pass
        return "".join(text_parts).strip()

    async def _read_response_from_dom(
        self, page, timeout: float = 45, pre_count: int = 0
    ) -> str | None:
        """Wait for an assistant response to appear in arena.ai's DOM.

        Arena.ai renders messages as .prose elements. User messages have a parent
        with 'self-end' class; assistant messages don't. We look for .prose elements
        that are NOT inside a self-end container.
        """
        _JS_FIND_ASSISTANT = """
            (preCount) => {
                const body = document.body?.innerText || '';

                // Check for arena.ai error messages
                if (body.includes('Something went wrong'))
                    return {error: 'Something went wrong while generating the response'};
                // Model-specific errors
                const errorPatterns = [
                    'unable to answer', 'cannot answer', 'cannot respond',
                    'not available', 'model is unavailable', 'capacity',
                    'too many requests', 'rate limit', 'try again later',
                    'refused to answer', 'content policy', 'safety filter',
                    'This model', 'Error generating',
                ];
                for (const pat of errorPatterns) {
                    if (body.toLowerCase().includes(pat.toLowerCase())) {
                        // Only treat as error if it appears in a toast/alert/error element, not in regular content
                        const errEls = document.querySelectorAll('[class*="error"], [class*="toast"], [role="alert"], .text-interactive-negative, [class*="warning"]');
                        for (const el of errEls) {
                            const t = (el.innerText || '').trim();
                            if (t && t.toLowerCase().includes(pat.toLowerCase())) {
                                return {error: t.substring(0, 300)};
                            }
                        }
                    }
                }

                // --- Battle Mode detection ---
                // Arena.ai sometimes shows "Response A" / "Response B" side-by-side comparison
                // with "Continue with A" / "Continue with B" / "Skip" buttons.
                const continueA = [...document.querySelectorAll('button')].find(
                    b => (b.textContent||'').trim().includes('Continue with A')
                );
                if (continueA) {
                    // Battle mode — read Response A's content
                    // Response A is typically the first .prose in the comparison layout
                    const proses = document.querySelectorAll('.prose');
                    if (proses.length > 0) {
                        const responseA = proses[0];
                        const t = (responseA.innerText || '').trim();
                        if (t) {
                            // Auto-click "Continue with A" to dismiss comparison
                            continueA.click();
                            return {text: t, count: 1, battleMode: true};
                        }
                    }
                }

                // --- Normal single-response mode ---
                const proses = document.querySelectorAll('.prose');
                const assistantMsgs = [...proses].filter(p => {
                    let el = p;
                    for (let i = 0; i < 6 && el; i++) {
                        const cls = el.className || '';
                        if (cls.includes('self-end') || cls.includes('bg-surface-raised'))
                            return false;
                        el = el.parentElement;
                    }
                    return true;
                });
                // For follow-up messages, wait until a NEW assistant message appears
                if (preCount > 0 && assistantMsgs.length <= preCount)
                    return null;
                if (!assistantMsgs.length) return null;
                const last = assistantMsgs[assistantMsgs.length - 1];
                const t = (last.innerText || '').trim();
                if (!t) return null;
                return {text: t, count: assistantMsgs.length};
            }
        """
        errors = 0
        last_text = ""
        stable_count = 0
        conv_url = page.url  # expected to be /c/{id}
        for tick in range(int(timeout * 2)):
            # Detect if page navigated away from conversation (arena.ai error → redirect)
            if "/c/" in conv_url and "/c/" not in (page.url or ""):
                logger.warning("[ArenaSteelAdapter] Page left conversation (now %s)", page.url)
                return None
            try:
                result = await page.evaluate(_JS_FIND_ASSISTANT, pre_count)
            except Exception as exc:
                errors += 1
                if errors <= 3 or errors % 20 == 0:
                    logger.info("[ArenaSteelAdapter] DOM poll #%d exception: %s", tick, str(exc)[:120])
                await asyncio.sleep(1)
                continue
            if result and result.get("error"):
                logger.warning("[ArenaSteelAdapter] Arena error in DOM: %s", result["error"])
                return None
            # If no response yet, check for reCAPTCHA blocking the response
            if (not result or not result.get("text")) and tick > 0 and tick % 6 == 0:
                captcha_solved = await self._handle_recaptcha(page, timeout=15)
                if captcha_solved:
                    logger.info("[ArenaSteelAdapter] reCAPTCHA solved mid-stream, continuing...")
                    await asyncio.sleep(2)
                    continue
            if result and result.get("battleMode"):
                logger.info("[ArenaSteelAdapter] Battle mode detected — using Response A, clicked Continue with A")
            if result and result.get("text"):
                text = result["text"]
                if text == last_text:
                    stable_count += 1
                    # Text unchanged for 3 ticks (1.5s) — done streaming
                    if stable_count >= 3:
                        logger.info("[ArenaSteelAdapter] DOM response stable after %d ticks", tick)
                        return text
                else:
                    last_text = text
                    stable_count = 0
                await asyncio.sleep(0.5)
                continue
            await asyncio.sleep(0.5)
        # If we have text but never stabilized, return what we have
        if last_text:
            logger.info("[ArenaSteelAdapter] DOM reader returning last text (%d chars)", len(last_text))
            return last_text
        logger.warning("[ArenaSteelAdapter] DOM reader exhausted %d ticks, %d errors", int(timeout * 2), errors)
        return None

    async def _handle_recaptcha(self, page, timeout: float = 30) -> bool:
        """Detect and solve reCAPTCHA checkbox challenge if present.

        Arena.ai shows a 'Security Verification' dialog with a reCAPTCHA
        'I'm not a robot' checkbox.  Click it and wait for the dialog to
        close.  Returns True if a captcha was found and handled.
        """
        try:
            # Check for the Security Verification dialog (not just the always-present badge).
            # Arena.ai has a reCAPTCHA badge (256x60) at bottom-right that's ALWAYS present.
            # Only trigger when the actual "Security Verification" dialog appears.
            has_captcha = await page.evaluate("""() => {
                var body = document.body ? document.body.innerText : '';
                if (!body.includes('Security Verification')) return false;
                // Double-check: look for the dialog's recaptcha container
                var container = document.querySelector('.recaptcha-v2-container');
                if (container) return true;
                // Or look for a second recaptcha anchor iframe (not the badge)
                var iframes = document.querySelectorAll('iframe[src*="recaptcha"][src*="anchor"]');
                return iframes.length >= 2;
            }""")
            if not has_captcha:
                return False

            logger.info("[ArenaSteelAdapter] reCAPTCHA detected, attempting to solve...")

            # Debug: capture all recaptcha-related iframes and their positions
            debug_info = await page.evaluate("""() => {
                var iframes = document.querySelectorAll('iframe[src*="recaptcha"]');
                var info = [];
                for (var f of iframes) {
                    var rect = f.getBoundingClientRect();
                    info.push({src: f.src.substring(0, 80), x: rect.x, y: rect.y, w: rect.width, h: rect.height, title: f.title});
                }
                // Check for Security Verification text
                var secVerif = document.body.innerText.includes('Security Verification');
                // Check all visible dialogs/modals
                var overlays = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="overlay"], [class*="captcha"]');
                var overlayInfo = [];
                for (var o of overlays) {
                    var rect = o.getBoundingClientRect();
                    overlayInfo.push({tag: o.tagName, cls: (o.className || '').substring(0, 60), text: o.innerText.substring(0, 100), w: rect.width, h: rect.height});
                }
                return {iframes: info, securityVerification: secVerif, overlays: overlayInfo, frameCount: window.frames.length};
            }""")
            logger.info("[ArenaSteelAdapter] reCAPTCHA debug: %s", debug_info)

            # There are TWO recaptcha anchor iframes:
            #   1. Badge at bottom-right (~1850, ~1006) — 256x60 — DO NOT CLICK
            #   2. Checkbox in Security Verification dialog (~808, ~571) — 302x76 — CLICK THIS
            # Find the checkbox iframe (the one NOT at bottom-right, or the one
            # inside the recaptcha-v2-container / Security Verification dialog).
            clicked = False

            # Strategy 1: Find the dialog's recaptcha iframe by position
            # (the one centered on page, not the badge at bottom-right)
            try:
                iframe_box = await page.evaluate("""() => {
                    var iframes = document.querySelectorAll('iframe[src*="recaptcha"][src*="anchor"]');
                    for (var f of iframes) {
                        var rect = f.getBoundingClientRect();
                        // The dialog checkbox is centered (~800, ~570), NOT at bottom-right (~1850, ~1000)
                        if (rect.x < 1500 && rect.y < 900 && rect.width > 200) {
                            return {x: rect.x + 30, y: rect.y + 30, w: rect.width, h: rect.height};
                        }
                    }
                    return null;
                }""")
                if iframe_box and iframe_box.get("w", 0) > 0:
                    await page.mouse.click(iframe_box["x"], iframe_box["y"])
                    clicked = True
                    logger.info("[ArenaSteelAdapter] Clicked dialog reCAPTCHA checkbox at (%d, %d)",
                                iframe_box["x"], iframe_box["y"])
            except Exception as e1:
                logger.info("[ArenaSteelAdapter] Dialog checkbox click failed: %s", str(e1)[:100])

            # Strategy 2: Use frame_locator targeting the dialog's recaptcha key
            if not clicked:
                try:
                    # The dialog uses a different key (6Ld7ePYr) from the badge (6Led_uYr)
                    fl = page.frame_locator('iframe[src*="recaptcha"][src*="6Ld7"]')
                    await fl.locator("#recaptcha-anchor").click(timeout=5000)
                    clicked = True
                    logger.info("[ArenaSteelAdapter] Clicked reCAPTCHA via key-specific frame_locator")
                except Exception as e2:
                    logger.info("[ArenaSteelAdapter] Key-specific frame_locator failed: %s", str(e2)[:100])

            # Strategy 3: Click inside the recaptcha-v2-container div
            if not clicked:
                try:
                    container_box = await page.evaluate("""() => {
                        var container = document.querySelector('.recaptcha-v2-container');
                        if (!container) return null;
                        var iframe = container.querySelector('iframe');
                        if (!iframe) return null;
                        var rect = iframe.getBoundingClientRect();
                        return {x: rect.x + 30, y: rect.y + 30, w: rect.width, h: rect.height};
                    }""")
                    if container_box and container_box.get("w", 0) > 0:
                        await page.mouse.click(container_box["x"], container_box["y"])
                        clicked = True
                        logger.info("[ArenaSteelAdapter] Clicked reCAPTCHA via container at (%d, %d)",
                                    container_box["x"], container_box["y"])
                except Exception as e3:
                    logger.info("[ArenaSteelAdapter] Container click failed: %s", str(e3)[:100])

            if not clicked:
                logger.warning("[ArenaSteelAdapter] All reCAPTCHA click strategies failed")
                return False

            # Wait for captcha to resolve
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(1.5)

                # Check if image challenge appeared
                bframe = None
                for frame in page.frames:
                    if "recaptcha" in (frame.url or "") and "bframe" in (frame.url or ""):
                        bframe = frame
                        break

                if bframe:
                    # Screenshot bframe and notify frontend for user to solve
                    logger.info("[ArenaAdapter] reCAPTCHA image challenge detected — requesting user solve...")
                    solved = await self._request_user_captcha_solve(page, bframe, timeout=120)
                    if solved:
                        logger.info("[ArenaAdapter] reCAPTCHA image challenge solved by user")
                        await asyncio.sleep(1)
                        return True
                    else:
                        logger.warning("[ArenaAdapter] reCAPTCHA image challenge not solved (timeout or error)")
                        return False

                # Check if captcha dialog closed (checkbox was enough)
                still_visible = await page.evaluate("""() => {
                    var iframe = document.querySelector('iframe[src*="recaptcha"]');
                    if (!iframe) return false;
                    var rect = iframe.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                }""")
                if not still_visible:
                    logger.info("[ArenaSteelAdapter] reCAPTCHA solved, dialog closed")
                    await asyncio.sleep(1)
                    return True

            logger.warning("[ArenaSteelAdapter] reCAPTCHA solve timed out")
            return False
        except Exception as exc:
            logger.warning("[ArenaSteelAdapter] reCAPTCHA handling error: %s", exc)
            return False

    async def _request_user_captcha_solve(self, page, bframe, timeout: float = 120) -> bool:
        """Screenshot captcha bframe and wait for user to solve via frontend.

        Sets _captcha_pending state, waits for _captcha_event to be signaled
        by the POST /api/arena/captcha/solve endpoint, then clicks the
        user-selected tiles and verifies.
        """
        max_rounds = 5  # reCAPTCHA sometimes gives multiple rounds
        for round_num in range(max_rounds):
            try:
                # Extract instruction text from bframe
                instruction = ""
                try:
                    instruction = await bframe.evaluate("""() => {
                        const el = document.querySelector('.rc-imageselect-desc-wrapper, .rc-imageselect-desc, .rc-imageselect-instructions');
                        return el ? el.innerText.trim() : '';
                    }""")
                except Exception:
                    instruction = "Select the matching images"

                # Detect grid size (3x3 or 4x4)
                grid_size = 3
                try:
                    grid_size = await bframe.evaluate("""() => {
                        const table = document.querySelector('table.rc-imageselect-table-44, table.rc-imageselect-table-33');
                        if (table && table.className.includes('44')) return 4;
                        const cells = document.querySelectorAll('td.rc-imageselect-tile');
                        if (cells.length === 16) return 4;
                        return 3;
                    }""")
                except Exception:
                    pass

                # Screenshot the bframe body
                screenshot = await bframe.locator("body").screenshot(type="png")

                # Set captcha state for frontend polling
                self._captcha_pending = True
                self._captcha_image = screenshot
                self._captcha_instruction = instruction
                self._captcha_grid_size = grid_size
                self._captcha_page = page
                self._captcha_bframe = bframe
                self._captcha_event.clear()
                self._captcha_cells = []

                logger.info(
                    "[ArenaAdapter] Captcha round %d: grid=%dx%d instruction='%s' — waiting for user solve...",
                    round_num + 1, grid_size, grid_size, instruction[:60],
                )

                # Wait for user to submit solution (or timeout)
                try:
                    await asyncio.wait_for(self._captcha_event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning("[ArenaAdapter] Captcha solve timed out waiting for user")
                    self._clear_captcha_state()
                    return False

                cells = self._captcha_cells
                if not cells:
                    logger.warning("[ArenaAdapter] User submitted empty captcha solution")
                    self._clear_captcha_state()
                    return False

                # Click the selected tiles in bframe
                await self._click_captcha_cells(bframe, cells, grid_size)

                # Click verify button
                try:
                    await bframe.locator("#recaptcha-verify-button").click(timeout=5000)
                except Exception:
                    logger.warning("[ArenaAdapter] Could not click verify button")

                self._clear_captcha_state()
                await asyncio.sleep(2)

                # Check if challenge is still visible (might need another round)
                still_challenge = False
                for frame in page.frames:
                    if "recaptcha" in (frame.url or "") and "bframe" in (frame.url or ""):
                        bframe = frame
                        still_challenge = True
                        break

                if not still_challenge:
                    return True  # Solved!

                # Check if we got an error message (wrong selection)
                try:
                    error_msg = await bframe.evaluate("""() => {
                        const err = document.querySelector('.rc-imageselect-incorrect-response, .rc-imageselect-error-select-more');
                        return err && err.offsetHeight > 0 ? err.innerText : '';
                    }""")
                    if error_msg:
                        logger.info("[ArenaAdapter] Captcha wrong answer: %s — requesting new solve", error_msg)
                        continue  # next round
                except Exception:
                    pass

                # New tiles appeared — go for another round
                logger.info("[ArenaAdapter] New captcha tiles appeared, round %d", round_num + 2)
                continue

            except Exception as exc:
                logger.warning("[ArenaAdapter] Captcha solve error round %d: %s", round_num + 1, exc)
                self._clear_captcha_state()
                return False

        self._clear_captcha_state()
        return False

    async def _click_captcha_cells(self, bframe, cells: list[int], grid_size: int = 3):
        """Click specific cells in the reCAPTCHA image grid."""
        # reCAPTCHA grid tile dimensions (approximate, within the bframe)
        tile_w = 130 if grid_size == 3 else 100
        tile_h = tile_w
        grid_left = 14   # left padding in bframe
        grid_top = 80    # top offset (below instruction text)

        for cell in cells:
            row = (cell - 1) // grid_size
            col = (cell - 1) % grid_size
            x = grid_left + col * tile_w + tile_w // 2
            y = grid_top + row * tile_h + tile_h // 2
            try:
                await bframe.locator("body").click(position={"x": x, "y": y})
            except Exception as exc:
                logger.warning("[ArenaAdapter] Failed to click cell %d: %s", cell, exc)
            await asyncio.sleep(random.uniform(0.3, 0.7))

    async def _save_debug_screenshot(self, page, label: str = "debug"):
        """Save a page screenshot for debugging failed interactions."""
        try:
            debug_dir = Path.home() / ".freehive" / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            path = debug_dir / f"arena_{label}_{ts}.png"
            await page.screenshot(path=str(path), full_page=True)
            logger.info("[ArenaAdapter] Debug screenshot saved: %s", path)

            # Also log page state
            page_info = await page.evaluate("""() => {
                const ta = document.querySelector('textarea');
                const dialogs = document.querySelectorAll('[role="dialog"][data-state="open"]');
                const captcha = document.body.innerText.includes('Security Verification');
                const buttons = [...document.querySelectorAll('button')].slice(0, 10).map(b => ({
                    text: (b.textContent||'').trim().substring(0, 40),
                    disabled: b.disabled,
                    type: b.type
                }));
                return {
                    url: location.href,
                    title: document.title,
                    hasTextarea: !!ta,
                    textareaValue: ta ? ta.value.substring(0, 100) : null,
                    textareaDisabled: ta ? ta.disabled : null,
                    openDialogs: dialogs.length,
                    hasCaptcha: captcha,
                    bodyTextPreview: document.body.innerText.substring(0, 300),
                    buttons: buttons
                };
            }""")
            logger.info("[ArenaAdapter] Page state at %s: %s", label, page_info)
        except Exception as exc:
            logger.warning("[ArenaAdapter] Failed to save debug screenshot: %s", exc)

    def _clear_captcha_state(self):
        """Reset captcha state after solve attempt."""
        self._captcha_pending = False
        self._captcha_image = None
        self._captcha_instruction = ""
        self._captcha_grid_size = 3
        self._captcha_cells = []
        self._captcha_page = None
        self._captcha_bframe = None
        self._captcha_event.clear()

    def get_captcha_state(self) -> dict:
        """Return current captcha state for API endpoint."""
        if not self._captcha_pending or not self._captcha_image:
            return {"pending": False}
        return {
            "pending": True,
            "image": base64.b64encode(self._captcha_image).decode("ascii"),
            "instruction": self._captcha_instruction,
            "grid_size": self._captcha_grid_size,
        }

    def submit_captcha_solution(self, cells: list[int]):
        """Receive solution from frontend and unblock the waiting adapter."""
        self._captcha_cells = cells
        self._captcha_event.set()

    async def _send_via_ui(self, page, slug: str, message: str, conv_id: str | None = None) -> dict | None:
        """Send a message by interacting with arena.ai's UI directly via Playwright.

        If conv_id is provided, continue an existing conversation at /c/{conv_id}.
        Otherwise start a new conversation from /text/direct.
        """
        try:
            if conv_id:
                # Continue existing conversation — navigate to /c/{conv_id}
                target_url = f"https://arena.ai/c/{conv_id}"
                current_url = page.url or ""
                if f"/c/{conv_id}" not in current_url:
                    logger.info("[ArenaAdapter] Continuing conversation %s...", conv_id[:12])
                    await page.goto(target_url, wait_until="load", timeout=30000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                # Model already selected in existing conversation — skip picker
            else:
                # New conversation — navigate to /text/direct
                logger.info("[ArenaAdapter] Starting new conversation...")
                await page.goto("https://arena.ai/text/direct", wait_until="load", timeout=20000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                self._current_ui_model = None  # Force model re-selection on fresh page

            # Select model in picker if new conversation (no conv_id)
            if not conv_id and self._current_ui_model != slug:
                selected = await page.evaluate(
                    """
                    async (targetSlug) => {
                        const picker = document.querySelector('button[aria-haspopup="dialog"]')
                                    || document.querySelector('button[aria-haspopup]');
                        if (!picker) return {error: "no picker"};
                        picker.click();
                        await new Promise(r => setTimeout(r, 600));

                        const search = document.querySelector('[cmdk-input]');
                        if (search) {
                            search.value = targetSlug;
                            search.dispatchEvent(new Event('input', {bubbles: true}));
                            await new Promise(r => setTimeout(r, 400));
                        }

                        const items = [...document.querySelectorAll('[cmdk-item]')];
                        const target = targetSlug.toLowerCase();
                        // Pass 1: exact match on data-value
                        for (const item of items) {
                            const val = (item.getAttribute('data-value') || '').toLowerCase();
                            if (val === target) {
                                item.click();
                                await new Promise(r => setTimeout(r, 200));
                                return {selected: val};
                            }
                        }
                        // Pass 2: partial match (fallback)
                        for (const item of items) {
                            const val = (item.getAttribute('data-value') || '').toLowerCase();
                            if (val.includes(target)) {
                                item.click();
                                await new Promise(r => setTimeout(r, 200));
                                return {selected: val};
                            }
                        }
                        document.dispatchEvent(new KeyboardEvent('keydown',
                            {key: 'Escape', keyCode: 27, bubbles: true}));
                        return {error: "model not found in picker", available: items.slice(0, 10).map(i => i.getAttribute('data-value'))};
                    }
                    """,
                    slug,
                )
                if selected.get("error"):
                    logger.warning("[ArenaSteelAdapter] UI model selection failed: %s", selected["error"])
                    return None
                self._current_ui_model = slug
                logger.info("[ArenaSteelAdapter] UI selected model: %s", selected.get("selected"))

            # Dismiss any blocking dialogs (terms, privacy, consent, etc.)
            dismissed = await page.evaluate("""
                () => {
                    const dialogs = document.querySelectorAll('[role="dialog"][data-state="open"], [role="alertdialog"]');
                    let dismissed = 0;
                    for (const dialog of dialogs) {
                        let found = false;
                        const btns = [...dialog.querySelectorAll('button')];
                        // Pass 1: keyword match
                        for (const b of btns) {
                            const t = (b.textContent||'').trim().toLowerCase();
                            if (t && (t.includes('accept')||t.includes('agree')||t.includes('continue')||
                                t.includes('ok')||t.includes('i understand')||t.includes('got it')||
                                t.includes('dismiss')||t.includes('close')||t.includes('confirm'))) {
                                b.click(); found = true; dismissed++; break;
                            }
                        }
                        if (found) continue;
                        // Pass 2: look for X/close icon buttons (aria-label or small icon buttons)
                        for (const b of btns) {
                            const label = (b.getAttribute('aria-label')||'').toLowerCase();
                            const rect = b.getBoundingClientRect();
                            if (label.includes('close') || label.includes('dismiss') ||
                                (rect.width < 50 && rect.height < 50 && !b.textContent.trim())) {
                                b.click(); found = true; dismissed++; break;
                            }
                        }
                        if (found) continue;
                        // Pass 3: click last button as fallback
                        if (btns.length > 0) {
                            btns[btns.length - 1].click(); dismissed++;
                        }
                    }
                    return dismissed;
                }
            """)
            if dismissed:
                logger.info("[ArenaAdapter] Dismissed %d blocking dialog(s)", dismissed)
                await asyncio.sleep(0.3)
            else:
                # Fallback: press Escape to close any modal
                try:
                    has_dialog = await page.evaluate("() => !!document.querySelector('[role=\"dialog\"][data-state=\"open\"]')")
                    if has_dialog:
                        await page.keyboard.press("Escape")
                        logger.info("[ArenaAdapter] Pressed Escape to dismiss dialog")
                        await asyncio.sleep(0.3)
                except Exception:
                    pass

            # Count existing assistant messages before submitting
            # Must match the same filter logic used in _read_response_from_dom
            pre_count = await page.evaluate("""
                () => {
                    const proses = document.querySelectorAll('.prose');
                    return [...proses].filter(p => {
                        let el = p;
                        for (let i = 0; i < 6 && el; i++) {
                            const cls = el.className || '';
                            if (cls.includes('self-end') || cls.includes('bg-surface-raised'))
                                return false;
                            el = el.parentElement;
                        }
                        return true;
                    }).length;
                }
            """)

            # Check for reCAPTCHA before attempting to type (may block page on load)
            await self._handle_recaptcha(page, timeout=5)

            textarea = page.locator("textarea").first
            try:
                await textarea.click(timeout=3000)
            except Exception:
                await self._save_debug_screenshot(page, "no_textarea")
                logger.warning("[ArenaAdapter] UI submit: no textarea found")
                return None

            await textarea.fill(message)
            await textarea.press("Enter")

            if conv_id:
                await asyncio.sleep(1)
            else:
                # First message — arena.ai navigates from /text/direct to /c/{id}.
                navigated = False
                try:
                    await page.wait_for_url("**/c/**", timeout=10000)
                    navigated = True
                except Exception:
                    # Enter didn't trigger submit — try clicking the send button
                    logger.info("[ArenaAdapter] Enter didn't navigate, trying submit button...")
                    clicked = await page.evaluate("""
                        () => {
                            const btns = document.querySelectorAll('button[type="submit"], button[aria-label*="send" i], button[aria-label*="Send"], form button:last-of-type');
                            for (const b of btns) {
                                if (!b.disabled) { b.click(); return 'clicked'; }
                            }
                            const form = document.querySelector('form');
                            if (form) {
                                form.requestSubmit();
                                return 'form-submitted';
                            }
                            return 'no-button';
                        }
                    """)
                    logger.info("[ArenaAdapter] Submit button attempt: %s", clicked)
                    try:
                        await page.wait_for_url("**/c/**", timeout=15000)
                        navigated = True
                    except Exception:
                        logger.warning("[ArenaAdapter] No navigation after Enter+button (URL: %s)", page.url)
                        await self._save_debug_screenshot(page, "no_navigation")

                if navigated:
                    logger.info("[ArenaAdapter] Navigated to conversation: %s", page.url)
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception:
                        pass
                    await asyncio.sleep(1)
                else:
                    await asyncio.sleep(1)

            # Handle reCAPTCHA if it appears
            await self._handle_recaptcha(page)

            # Extract conversation ID from URL (/c/{uuid})
            extracted_conv_id = None
            import re as _re
            url_match = _re.search(r'/c/([0-9a-zA-Z-]{20,})', page.url or "")
            if url_match:
                extracted_conv_id = url_match.group(1)

            # Wait for assistant response in DOM (up to 90 seconds)
            dom_text = await self._read_response_from_dom(page, timeout=90, pre_count=pre_count)
            if dom_text:
                logger.info("[ArenaAdapter] UI got response (%d chars, conv=%s)",
                            len(dom_text), (extracted_conv_id or "?")[:12])
                return {"text": dom_text, "error": None, "conversationId": extracted_conv_id, "toolCalls": None}

            # Check if arena.ai showed an error message
            error_text = await page.evaluate("""
                () => {
                    const errorEls = document.querySelectorAll(
                        '[class*="error"], [class*="toast"], [role="alert"], ' +
                        '.text-interactive-negative'
                    );
                    for (const el of errorEls) {
                        const t = (el.innerText || '').trim();
                        if (t) return t.substring(0, 200);
                    }
                    return null;
                }
            """)
            if error_text:
                logger.warning("[ArenaAdapter] UI error: %s", error_text)
                return {"text": None, "error": error_text, "conversationId": extracted_conv_id, "toolCalls": None}
            else:
                logger.warning("[ArenaAdapter] UI send timed out (no DOM response)")
                await self._save_debug_screenshot(page, "dom_timeout")
                return {"text": None, "error": "No response from arena.ai (timed out)", "conversationId": extracted_conv_id, "toolCalls": None}

        except Exception as exc:
            logger.warning("[ArenaSteelAdapter] UI send failed with exception: %s", exc)
            return {"text": None, "error": str(exc), "conversationId": None, "toolCalls": None}

    async def fetch_models(self) -> list[str]:
        """Return arena.ai models: live page extraction merged with known list, sorted alphabetically."""
        # Strategy 0 (most reliable): Extract from Next.js RSC payload (window.__next_f).
        # This contains initialModels with UUID, slug, name, and userSelectable flag.
        raw_models: list[str] = await self._extract_rsc_models()

        # Strategy 1: Playwright network interception
        if not raw_models:
            raw_models = list(self._captured_models)

        # Strategy 2: JS injection into the live page (fallback).
        if not raw_models:
            try:
                page = await self._get_page()
                result = await page.evaluate(
                    _ARENA_JS_RUNNER,
                    {"model": "__FETCH_MODELS__", "message": "", "conversationId": None, "jobId": ""},
                )
                raw_models = result.get("models") or []
                if raw_models:
                    logger.info("[ArenaSteelAdapter] fetch_models: JS injection found %d models", len(raw_models))
            except Exception as exc:
                logger.warning("[ArenaSteelAdapter] fetch_models JS fallback failed: %s", exc)
                raw_models = []
            if not raw_models and self._captured_models:
                raw_models = list(self._captured_models)
                logger.info("[ArenaSteelAdapter] fetch_models: interceptor populated %d models via JS fetch calls", len(raw_models))

        # Strategy 3: Click the model picker dropdown and read model names from the DOM.
        if not raw_models:
            raw_models = await self._fetch_models_via_picker()

        seen: set[str] = set()
        merged: list[str] = []

        # Absorb live-extracted models first
        for name in raw_models:
            name = str(name).strip()
            if not name:
                continue
            model_id = name if name.startswith("arena/") else f"arena/{name}"
            key = model_id.lower()
            if key not in seen:
                seen.add(key)
                merged.append(model_id)

        # Always supplement with the known baseline list
        for model_id in ARENA_KNOWN_MODELS:
            key = model_id.lower()
            if key not in seen:
                seen.add(key)
                merged.append(model_id)

        # Sort alphabetically by slug (strip prefix for comparison key)
        merged.sort(key=lambda m: m.removeprefix("arena/").lower())

        # If live extraction returned nothing, fall back to disk cache
        if not merged:
            cached = _load_model_cache()
            if cached:
                logger.info("[ArenaSteelAdapter] fetch_models: using disk cache (%d models)", len(cached))
                merged = cached
            else:
                logger.warning("[ArenaSteelAdapter] fetch_models: no models from page and no cache available")

        final = self._health.filter_and_rank(merged, unknown_cap=None)

        # Persist a successful extraction so future failures can fall back
        if final:
            _save_model_cache(final)

        return final

    def load_history(self, messages: list[dict], session_id: str = "default"):
        self._history[session_id] = [
            {"role": m["role"], "content": m["content"]} for m in messages
        ]

    def clear_history(self, session_id: str = "default"):
        self._history.pop(session_id, None)
        self._conversation_ids.pop(session_id, None)

    async def is_authenticated(self) -> bool:
        """Check if arena.ai cookies indicate a logged-in user."""
        try:
            info = await self.get_account_info()
            return info.get("logged_in", False)
        except Exception:
            return False

    async def get_account_info(self) -> dict:
        """Detect login state and extract account info.

        Primary: check arena.ai page DOM for logged-in indicators.
        Fallback: decode JWT from auth cookie.
        Returns {"logged_in": False} if not authenticated.
        """
        try:
            page = await self._get_page()

            # Ensure we're on an arena.ai page so DOM reflects login state
            current_url = page.url or ""
            if "arena.ai" not in current_url:
                await page.goto("https://arena.ai/text/direct", wait_until="load", timeout=20000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass

            # --- Method 1: DOM-based detection ---
            dom_info = await page.evaluate("""() => {
                const body = document.body ? document.body.innerText : '';

                // If "Login" or "Sign in" button is prominent and no user email, not logged in
                const loginBtn = document.querySelector('button');
                const hasLoginBtn = [...document.querySelectorAll('button')].some(
                    b => (b.textContent||'').trim() === 'Login' || (b.textContent||'').trim() === 'Sign in'
                );

                // Look for email pattern anywhere in the page text
                const emailMatch = body.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}/);
                const email = emailMatch ? emailMatch[0] : '';

                // Check for user avatar / profile indicators
                const hasAvatar = !!document.querySelector('img[alt*="avatar" i], img[alt*="profile" i], img[alt*="user" i]');

                // Check sidebar for conversation history (only visible when logged in)
                const hasHistory = body.includes('Today') || body.includes('Yesterday') ||
                    document.querySelectorAll('a[href*="/c/"]').length > 0;

                // Check for "New Chat" which appears when logged in
                const hasNewChat = body.includes('New Chat');

                return {
                    hasLoginBtn,
                    email,
                    hasAvatar,
                    hasHistory,
                    hasNewChat,
                    urlPath: location.pathname,
                    title: document.title,
                };
            }""")

            logger.info("[ArenaAdapter] DOM auth check: %s", dom_info)

            # Determine login state from DOM signals
            email = dom_info.get("email", "")
            has_history = dom_info.get("hasHistory", False)
            has_new_chat = dom_info.get("hasNewChat", False)
            has_login_btn = dom_info.get("hasLoginBtn", False)

            # Logged in if: has email OR has conversation history/New Chat AND no Login button
            is_logged_in = bool(email) or ((has_history or has_new_chat) and not has_login_btn)

            if is_logged_in:
                result = {
                    "logged_in": True,
                    "email": email,
                    "name": "",
                    "detection": "dom",
                }
                # Try to also get name from JWT cookie
                try:
                    ctx = page.context
                    for c in await ctx.cookies("https://arena.ai"):
                        if "auth" in c.get("name", "").lower() and len(c.get("value", "")) > 20:
                            parts = c["value"].split(".")
                            if len(parts) >= 2:
                                payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
                                decoded = json.loads(base64.urlsafe_b64decode(payload))
                                result["name"] = decoded.get("name", decoded.get("user_name", ""))
                                result["picture"] = decoded.get("picture", "")
                                if not result["email"]:
                                    result["email"] = decoded.get("email", "")
                            break
                except Exception:
                    pass
                return result

            # --- Method 2: Cookie fallback ---
            try:
                ctx = page.context
                all_cookies = await ctx.cookies()
                logger.info(
                    "[ArenaAdapter] Cookie fallback: %d total cookies, domains: %s",
                    len(all_cookies),
                    list(set(c.get("domain", "") for c in all_cookies))[:10],
                )
                for c in await ctx.cookies("https://arena.ai"):
                    name = c.get("name", "")
                    value = c.get("value", "")
                    if "auth" in name.lower() and len(value) > 20:
                        try:
                            parts = value.split(".")
                            if len(parts) >= 2:
                                payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
                                decoded = json.loads(base64.urlsafe_b64decode(payload))
                                return {
                                    "logged_in": True,
                                    "email": decoded.get("email", ""),
                                    "name": decoded.get("name", ""),
                                    "detection": "cookie",
                                    "cookie_name": name,
                                }
                        except Exception:
                            return {"logged_in": True, "email": "", "name": "", "detection": "cookie", "cookie_name": name}
            except Exception:
                pass

            return {"logged_in": False}
        except Exception as exc:
            logger.warning("[ArenaAdapter] get_account_info error: %s", exc)
            return {"logged_in": False}

    async def logout(self):
        """Clear arena.ai cookies and close browser to log out."""
        try:
            page = await self._get_page()
            ctx = page.context
            cookies = await ctx.cookies("https://arena.ai")
            # Clear all arena.ai cookies
            if cookies:
                await ctx.clear_cookies(domain="arena.ai")
                logger.info("[ArenaAdapter] Cleared %d arena.ai cookies", len(cookies))
        except Exception as exc:
            logger.debug("[ArenaAdapter] Cookie clear failed (context may be dead): %s", exc)
        # Close browser context
        await self.close()

    async def is_available(self) -> bool:
        return await self._orchestrator.is_available()

    # Backward compat alias
    is_steel_available = is_available

    def get_viewer_url(self) -> str | None:
        """CloakBrowser runs locally — no remote viewer URL."""
        return None

    async def close(self):
        """Gracefully close the CloakBrowser context."""
        try:
            await self._orchestrator.close()
        except Exception:
            pass
        self._page = None
        self._context = None

    # ------------------------------------------------------------------
    # Internal: Playwright page management
    # ------------------------------------------------------------------

    async def _get_page(self):
        """Return the live arena.ai page, reconnecting if necessary."""
        # If a send is in progress, return cached page without liveness check.
        # The send navigates the page, so a liveness check would see "stale" and
        # reconnect — creating a second CDP connection that fights with the send.
        if self._send_lock.locked() and self._page is not None:
            return self._page

        async with self._lock:
            if self._page is not None:
                try:
                    await self._page.evaluate("() => document.title")
                    return self._page
                except Exception:
                    logger.warning("[ArenaSteelAdapter] Page went stale, reconnecting")
                    self._listener_pages.discard(id(self._page))
                    self._page = None
                    self._context = None

            await self._connect()
            return self._page

    async def _connect(self):
        """Connect to arena.ai via CloakBrowser stealth browser."""
        ctx = await self._orchestrator.get_or_create_context()
        self._context = ctx

        # Find an existing arena.ai page or open one
        page = None
        for p in ctx.pages:
            if "arena.ai" in (p.url or ""):
                page = p
                break

        # Attach response interceptor before navigation so we capture model-list API calls
        new_page = page is None
        if new_page:
            page = await ctx.new_page()

        if id(page) not in self._listener_pages:
            page.on("response", self._on_arena_response)
            self._listener_pages.add(id(page))

        # No stealth patches needed — CloakBrowser handles at C++ binary level

        if new_page:
            await page.goto(ARENA_DIRECT_URL, wait_until="load", timeout=45000)
            # Let client-side JS finish loading model data
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
        elif "text/direct" not in (page.url or ""):
            # Already on arena.ai but not on the direct chat page — navigate there
            try:
                await page.goto(ARENA_DIRECT_URL, wait_until="load", timeout=45000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
            except Exception:
                pass

        self._page = page
        logger.info("[ArenaAdapter] Connected via CloakBrowser, page URL: %s", page.url)

        # Dismiss cookie consent / onboarding dialogs that block interaction
        try:
            await page.evaluate("""
                () => {
                    const dialog = document.querySelector('[role="dialog"][data-state="open"]');
                    if (!dialog) return;
                    const buttons = dialog.querySelectorAll('button');
                    for (const btn of buttons) {
                        const t = (btn.textContent || '').trim().toLowerCase();
                        if (t.includes('accept') || t.includes('agree') || t.includes('continue') ||
                            t.includes('got it') || t.includes('ok')) {
                            btn.click(); return;
                        }
                    }
                    if (buttons.length > 0) buttons[buttons.length - 1].click();
                }
            """)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal: error classification
    # ------------------------------------------------------------------

    def _classify_and_raise(self, model: str, status: int | None, msg: str):
        msg_l = str(msg).lower()
        if status == 404 and "model not found" in msg_l:
            raise RuntimeError(
                f"Arena model '{model}' is unavailable right now (Model not found). "
                "Refresh models and choose another model."
            )
        if status == 422 and ("not permitted" in msg_l or "choose another model" in msg_l):
            raise RuntimeError(
                f"Arena model '{model}' cannot be used for conversational chat. "
                "Refresh models and choose another model."
            )
        if status == 400 and "not available for user selection" in msg_l:
            raise RuntimeError(
                f"Arena model '{model}' is not available for user selection. "
                "Refresh models and choose another model."
            )
        if status == 400 and "private models" in msg_l:
            raise RuntimeError(
                f"Arena model '{model}' is private/battle-only and cannot be used in Direct mode."
            )
        if status == 429:
            raise RuntimeError(
                f"Arena rate limited. Wait a moment and try again."
            )
        raise RuntimeError(msg or "Arena returned an error.")
