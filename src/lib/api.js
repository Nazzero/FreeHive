import axios from 'axios';
import { API_BASE_URL } from '$lib/config.js';

const BASE_URL = API_BASE_URL;

// Extract a readable message from axios errors (pulls FastAPI `detail` field)
function extractErrorMessage(err) {
    const detail = err?.response?.data?.detail;
    if (detail) return String(detail);
    return err?.message || 'Unknown error';
}

axios.interceptors.response.use(
    (res) => res,
    (err) => {
        const msg = extractErrorMessage(err);
        err.message = msg;
        return Promise.reject(err);
    }
);

// Active chat session for the current browser runtime.
/**
 * @typedef {Object} Session
 * @property {string} id
 * @property {string} model
 */

/** @type {Session | null} */
let activeSession = null; // { id, model }

/** @type {Record<string, string>} */
const sessionModelById = {}; // session_id -> normalized model

/**
 * @param {string} model
 * @returns {string}
 */
function normalizeModelId(model) {
    const id = String(model || '').trim();
    if (!id) return id;
    if (id.startsWith('arena/')) return id;
    // Known provider short names
    if (id === 'claude' || id === 'chatgpt' || id === 'gemini') return id;
    // Full model IDs for known providers — pass through as-is
    if (id.startsWith('claude-') || id.startsWith('gpt-') || id.startsWith('o1-') ||
        id.startsWith('o3-') || id.startsWith('o4-') || id.startsWith('codex-') ||
        id.startsWith('gemini-')) return id;
    // Unknown IDs pass through; backend validates.
    if (id.includes('-')) return id;
    return id;
}

/**
 * @param {string} model
 * @returns {Promise<{id: string, model: string}>}
 */
async function createSessionInternal(model) {
    const normalizedModel = normalizeModelId(model);
    const res = await axios.post(`${BASE_URL}/sessions`, { model: normalizedModel });
    const id = res.data.id;
    sessionModelById[id] = normalizedModel;
    activeSession = { id, model: normalizedModel };
    return { id, model: normalizedModel };
}

/**
 * @param {string} model
 * @param {string | null} [preferredSessionId]
 * @returns {Promise<string>}
 */
function getOrCreateSession(model, preferredSessionId = null) {
    const normalizedModel = normalizeModelId(model);
    if (preferredSessionId) {
        sessionModelById[preferredSessionId] = normalizedModel;
        activeSession = { id: preferredSessionId, model: normalizedModel };
        return Promise.resolve(preferredSessionId);
    }
    if (activeSession?.id && activeSession.model === normalizedModel) {
        return Promise.resolve(activeSession.id);
    }
    return createSessionInternal(normalizedModel).then((s) => s.id);
}

/**
 * @param {string} sessionId
 * @param {string} model
 */
export function setActiveSession(sessionId, model) {
    const normalizedModel = normalizeModelId(model);
    if (!sessionId || !normalizedModel) return;
    sessionModelById[sessionId] = normalizedModel;
    activeSession = { id: sessionId, model: normalizedModel };
}

export function clearActiveSession() {
    activeSession = null;
}

/**
 * @param {string} model
 * @returns {Promise<{id: string, model: string}>}
 */
export async function createChatSession(model) {
    return createSessionInternal(model);
}

/**
 * @param {string} model
 * @param {string} message
 * @param {string | null} [sessionId]
 * @returns {Promise<any>}
 */
export async function sendChat(model, message, sessionId = null) {
    const normalizedModel = normalizeModelId(model);
    const session_id = await getOrCreateSession(normalizedModel, sessionId);
    const res = await axios.post(`${BASE_URL}/chat`, { model: normalizedModel, message, session_id });
    if (res.data && typeof res.data === 'object') {
        return { ...res.data, session_id };
    }
    return { response: String(res.data ?? ''), session_id };
}

/**
 * @param {{source?: string | null, model?: string | null}} [opts]
 * @returns {Promise<any[]>}
 */
export async function listChatSessions({ source = null, model = null } = {}) {
    /** @type {Record<string, string>} */
    const params = {};
    if (source) params.source = source;
    if (model) params.model = normalizeModelId(model);
    const res = await axios.get(`${BASE_URL}/sessions`, { params });
    return Array.isArray(res.data) ? res.data : [];
}

/**
 * @param {string} sessionId
 * @returns {Promise<any[]>}
 */
export async function getChatSessionMessages(sessionId) {
    const res = await axios.get(`${BASE_URL}/sessions/${sessionId}/messages`);
    return Array.isArray(res.data) ? res.data : [];
}

/**
 * @param {string} sessionId
 * @returns {Promise<void>}
 */
export async function deleteChatSession(sessionId) {
    await axios.delete(`${BASE_URL}/sessions/${sessionId}`);
    delete sessionModelById[sessionId];
    if (activeSession?.id === sessionId) {
        activeSession = null;
    }
}

/**
 * @returns {Promise<any[]>}
 */
export async function getModels() {
    const res = await axios.get(`${BASE_URL}/models`);
    return res.data.models;
}

/**
 * @param {string | null} [model]
 * @returns {Promise<void>}
 */
export async function clearHistory(model = null) {
    activeSession = null;
    Object.keys(sessionModelById).forEach((k) => delete sessionModelById[k]);
    const params = model ? `?model=${model}` : '';
    await axios.post(`${BASE_URL}/chat/clear${params}`);
}

/**
 * Delete the DB file and recreate fresh. Clears all history.
 * @returns {Promise<void>}
 */
export async function resetDatabase() {
    activeSession = null;
    Object.keys(sessionModelById).forEach((k) => delete sessionModelById[k]);
    await axios.post(`${BASE_URL}/chat/reset-db`);
}

/**
 * Fetch live usage/quota from all providers.
 * @returns {Promise<{providers: Object}>}
 */
export async function getUsageStats() {
    const res = await axios.get(`${BASE_URL}/setup/usage`);
    return res.data;
}

/**
 * @returns {Promise<any>}
 */
export async function getSetupStatus() {
    const res = await axios.get(`${BASE_URL}/setup/status`);
    return res.data;
}

/**
 * @param {string} tool
 * @returns {Promise<any>}
 */
export async function logoutTool(tool) {
    const res = await axios.post(`${BASE_URL}/setup/logout/${tool}`);
    return res.data;
}

/**
 * Install a CLI tool via SSE stream (mirrors SetupScreen's install flow).
 * @param {string} tool  "claude_code" | "gemini_cli" | "chatgpt_cli"
 * @param {(event: any) => void} [onEvent]
 * @returns {Promise<any>}
 */
export async function installTool(tool, onEvent = () => {}) {
    const res = await fetch(`${BASE_URL}/setup/install`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ tool }),
    });
    if (!res.ok) {
        throw new Error(`Install request failed (${res.status})`);
    }
    if (!res.body) {
        throw new Error('Backend did not provide an install stream.');
    }

    const terminal = new Set(['done', 'success', 'failed', 'timeout', 'error']);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    /** @type {any} */
    let finalEvent = null;
    let finished = false;

    while (!finished) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let newlineIdx = buffer.indexOf('\n');
        while (newlineIdx >= 0) {
            const line = buffer.slice(0, newlineIdx).trim();
            buffer = buffer.slice(newlineIdx + 1);
            newlineIdx = buffer.indexOf('\n');

            if (!line.startsWith('data:')) continue;
            /** @type {any} */
            let event = null;
            try {
                event = JSON.parse(line.slice(5).trim());
            } catch {
                continue;
            }
            onEvent(event);
            if (terminal.has(String(event?.status || ''))) {
                finalEvent = event;
                finished = true;
                break;
            }
        }
    }

    try {
        await reader.cancel();
    } catch {
    }

    if (!finalEvent) {
        throw new Error('Install stream ended unexpectedly.');
    }
    if (!finalEvent.success && finalEvent.status !== 'success') {
        throw new Error(finalEvent.msg || 'Installation failed.');
    }
    return finalEvent;
}

/**
 * @param {string} tool
 * @param {(event: any) => void} [onEvent]
 * @returns {Promise<any>}
 */
export async function authenticateTool(tool, onEvent = () => {}) {
    const res = await fetch(`${BASE_URL}/setup/auth/${encodeURIComponent(tool)}`);
    if (!res.ok) {
        throw new Error(`Auth request failed (${res.status})`);
    }
    if (!res.body) {
        throw new Error('Backend did not provide an auth stream.');
    }

    const terminal = new Set(['done', 'success', 'failed', 'timeout', 'error']);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    /** @type {any} */
    let finalEvent = null;
    let finished = false;

    while (!finished) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let newlineIdx = buffer.indexOf('\n');
        while (newlineIdx >= 0) {
            const line = buffer.slice(0, newlineIdx).trim();
            buffer = buffer.slice(newlineIdx + 1);
            newlineIdx = buffer.indexOf('\n');

            if (!line.startsWith('data:')) continue;
            /** @type {any} */
            let event = null;
            try {
                event = JSON.parse(line.slice(5).trim());
            } catch {
                continue;
            }
            onEvent(event);
            if (terminal.has(String(event?.status || ''))) {
                finalEvent = event;
                finished = true;
                break;
            }
        }
    }

    try {
        await reader.cancel();
    } catch {
    }

    if (!finalEvent) {
        throw new Error('Authentication stream ended unexpectedly.');
    }
    if (finalEvent.status !== 'success') {
        throw new Error(finalEvent.msg || `Authentication ${finalEvent.status || 'failed'}.`);
    }
    return finalEvent;
}

/**
 * @returns {Promise<any>}
 */
export async function getArenaStatus() {
    const res = await axios.get(`${BASE_URL}/setup/arena/status`);
    return res.data;
}

/**
 * @param {boolean} [forceLogin]
 * @returns {Promise<any>}
 */
export async function startArena(forceLogin = false) {
    const res = await axios.post(`${BASE_URL}/arena/start`, { force_login: forceLogin });
    return res.data;
}

/**
 * @returns {Promise<any>}
 */
export async function getArenaModels() {
    const res = await axios.get(`${BASE_URL}/arena/models`);
    return res.data;
}

/**
 * Refresh arena model list from live dropdowns (chat + code).
 * Fetches from both arena.ai/text/direct and arena.ai/code/direct,
 * saves to disk cache. Takes ~10s (navigates the arena tab).
 * @returns {Promise<any>} Full model catalog with capabilities and modes
 */
export async function refreshArenaModels() {
    const res = await axios.post(`${BASE_URL}/arena/models/refresh`);
    return res.data;
}

/**
 * @returns {Promise<any>}
 */
export async function getArenaHealth() {
    const res = await axios.get(`${BASE_URL}/arena/health`);
    return res.data;
}

/**
 * Stream arena model probe results via SSE.
 * @param {(event: any) => void} onEvent
 * @returns {Promise<any>} resolves with the final summary event
 */
export async function probeArenaModels(onEvent = () => {}) {
    const res = await fetch(`${BASE_URL}/arena/probe`, { method: 'POST' });
    if (!res.ok) throw new Error(`Probe failed (${res.status})`);
    if (!res.body) throw new Error('No response stream');

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    /** @type {any} */
    let finalEvent = null;

    while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let nl = buffer.indexOf('\n');
        while (nl >= 0) {
            const line = buffer.slice(0, nl).trim();
            buffer = buffer.slice(nl + 1);
            nl = buffer.indexOf('\n');
            if (!line.startsWith('data:')) continue;
            /** @type {any} */
            let ev = null;
            try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
            onEvent(ev);
            if (ev?.status === 'done' || ev?.status === 'error') finalEvent = ev;
        }
    }
    try { await reader.cancel(); } catch { /* ignore */ }
    return finalEvent;
}

/**
 * @param {boolean} [refresh]
 * @returns {Promise<any>}
 */
export async function getAvailableModels(refresh = false) {
    const url = refresh
        ? `${BASE_URL}/setup/models/refresh`
        : `${BASE_URL}/setup/models`;
    const res = refresh
        ? await axios.post(url)
        : await axios.get(url);
    const data = (res.data && typeof res.data === 'object') ? { ...res.data } : {};
    return data; // { claude: { tier, models }, chatgpt: { tier, models }, gemini: { tier, models }, arena: { tier, models } }
}

/**
 * Get the default thinking effort level from backend config.
 * @returns {Promise<string>} "off"|"low"|"medium"|"high"
 */
export async function getThinkingEffort() {
    const res = await axios.get(`${BASE_URL}/setup/thinking-effort`);
    return res.data?.thinking_effort || 'off';
}

/**
 * Set the default thinking effort level in backend config.
 * @param {string} effort "off"|"low"|"medium"|"high"
 * @returns {Promise<any>}
 */
export async function setThinkingEffort(effort) {
    const res = await axios.post(`${BASE_URL}/setup/thinking-effort`, { thinking_effort: effort });
    return res.data;
}

/**
 * Check if a captcha is pending for Arena.
 * @returns {Promise<{pending: boolean, image?: string, instruction?: string, grid_size?: number}>}
 */
export async function getArenaCaptcha() {
    const res = await axios.get(`${BASE_URL}/arena/captcha`);
    return res.data;
}

/**
 * Submit captcha solution (selected tile numbers).
 * @param {number[]} cells
 * @returns {Promise<any>}
 */
export async function solveArenaCaptcha(cells) {
    const res = await axios.post(`${BASE_URL}/arena/captcha/solve`, { cells });
    return res.data;
}

/**
 * Log out of arena.ai — clears cookies and closes browser.
 * @returns {Promise<any>}
 */
export async function logoutArena() {
    const res = await axios.post(`${BASE_URL}/arena/logout`);
    return res.data;
}

/**
 * Get arena.ai account info (email, name).
 * @returns {Promise<any>}
 */
export async function getArenaAccount() {
    const res = await axios.get(`${BASE_URL}/arena/account`);
    return res.data;
}

/**
 * Open browser window for arena interaction.
 * Extension mode: tells user to switch to Chrome tab.
 * CloakBrowser fallback: relaunches in headed mode.
 * @returns {Promise<any>}
 */
export async function showArenaBrowser() {
    const res = await axios.post(`${BASE_URL}/arena/show-browser`);
    return res.data;
}

/**
 * One-click Arena setup: install native host + launch Chrome with extension.
 * @returns {Promise<any>}
 */
export async function setupArena() {
    const res = await axios.post(`${BASE_URL}/arena/setup`);
    return res.data;
}

/**
 * Check Chrome + extension + native host installation state.
 * @returns {Promise<any>}
 */
export async function getArenaChromeStatus() {
    const res = await axios.get(`${BASE_URL}/arena/chrome-status`);
    return res.data;
}

/**
 * Launch Chrome with extension loaded, navigate to arena.ai.
 * @returns {Promise<any>}
 */
export async function launchArenaChrome() {
    const res = await axios.post(`${BASE_URL}/arena/launch-chrome`);
    return res.data;
}
