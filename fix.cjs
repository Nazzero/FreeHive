const fs = require('fs');
const path = require('path');

function replaceFile(filepath, replaces) {
    let content = fs.readFileSync(filepath, 'utf-8');
    for (const [from, to] of replaces) {
        content = content.replace(from, to);
    }
    fs.writeFileSync(filepath, content);
}

// 1. src/lib/api.js
replaceFile('src/lib/api.js', [
    [`let activeSession = null; // { id, model }
const sessionModelById = {}; // session_id -> normalized model`,
`/**
 * @typedef {Object} Session
 * @property {string} id
 * @property {string} model
 */

/** @type {Session | null} */
let activeSession = null; // { id, model }

/** @type {Record<string, string>} */
const sessionModelById = {}; // session_id -> normalized model`],
    [`function normalizeModelId(model) {`, `/**\n * @param {string} model\n * @returns {string}\n */\nfunction normalizeModelId(model) {`],
    [`async function createSessionInternal(model) {`, `/**\n * @param {string} model\n * @returns {Promise<{id: string, model: string}>}\n */\nasync function createSessionInternal(model) {`],
    [`function getOrCreateSession(model, preferredSessionId = null) {`, `/**\n * @param {string} model\n * @param {string | null} [preferredSessionId]\n * @returns {Promise<string>}\n */\nfunction getOrCreateSession(model, preferredSessionId = null) {`],
    [`export function setActiveSession(sessionId, model) {`, `/**\n * @param {string} sessionId\n * @param {string} model\n */\nexport function setActiveSession(sessionId, model) {`],
    [`export async function createChatSession(model) {`, `/**\n * @param {string} model\n * @returns {Promise<{id: string, model: string}>}\n */\nexport async function createChatSession(model) {`],
    [`export async function sendChat(model, message, sessionId = null) {`, `/**\n * @param {string} model\n * @param {string} message\n * @param {string | null} [sessionId]\n * @returns {Promise<any>}\n */\nexport async function sendChat(model, message, sessionId = null) {`],
    [`export async function listChatSessions({ source = null, model = null } = {}) {
    const params = {};`, `/**\n * @param {{source?: string | null, model?: string | null}} [opts]\n * @returns {Promise<any[]>}\n */\nexport async function listChatSessions({ source = null, model = null } = {}) {\n    /** @type {Record<string, string>} */\n    const params = {};`],
    [`export async function getChatSessionMessages(sessionId) {`, `/**\n * @param {string} sessionId\n * @returns {Promise<any[]>}\n */\nexport async function getChatSessionMessages(sessionId) {`],
    [`export async function deleteChatSession(sessionId) {`, `/**\n * @param {string} sessionId\n * @returns {Promise<void>}\n */\nexport async function deleteChatSession(sessionId) {`],
    [`export async function getModels() {`, `/**\n * @returns {Promise<any[]>}\n */\nexport async function getModels() {`],
    [`export async function clearHistory(model = null) {`, `/**\n * @param {string | null} [model]\n * @returns {Promise<void>}\n */\nexport async function clearHistory(model = null) {`],
    [`export async function getSetupStatus() {`, `/**\n * @returns {Promise<any>}\n */\nexport async function getSetupStatus() {`],
    [`export async function getArenaStatus() {
    const res = await axios.get(\`\${BASE_URL}/arena/status\`);
    return res.data;
}

export async function startArena(forceLogin = false) {
    const res = await axios.post(\`\${BASE_URL}/arena/start\`, {
        force_login: forceLogin
    });
    return res.data;
}

export async function getArenaModels() {
    const res = await axios.get(\`\${BASE_URL}/arena/models\`);
    return res.data;
}`, `// TODO (v2): Arena integration postponed.
/*
export async function getArenaStatus() {
    const res = await axios.get(\`\${BASE_URL}/arena/status\`);
    return res.data;
}

export async function startArena(forceLogin = false) {
    const res = await axios.post(\`\${BASE_URL}/arena/start\`, {
        force_login: forceLogin
    });
    return res.data;
}

export async function getArenaModels() {
    const res = await axios.get(\`\${BASE_URL}/arena/models\`);
    return res.data;
}
*/`],
    [`export async function getAvailableModels(refresh = false) {`, `/**\n * @param {boolean} [refresh]\n * @returns {Promise<any>}\n */\nexport async function getAvailableModels(refresh = false) {`]
]);

// 2. src/lib/store.js
replaceFile('src/lib/store.js', [
    [`export const messages = writable([]);
export const isLoading = writable(false);

export function addMessage(role, content, model = null, transport = null) {`, `/** @type {import('svelte/store').Writable<any[]>} */
export const messages = writable([]);
/** @type {import('svelte/store').Writable<boolean>} */
export const isLoading = writable(false);

/**
 * @param {string} role
 * @param {string} content
 * @param {string | null} [model]
 * @param {string | null} [transport]
 */
export function addMessage(role, content, model = null, transport = null) {`]
]);

// 3. src/lib/AccountPanel.svelte
replaceFile('src/lib/AccountPanel.svelte', [
    [`let accounts = [];`, `/** @type {any[]} */\n    let accounts = [];`],
    [`        } catch (e) {
            error = e.response?.data?.detail || 'Failed to add account';`, `        } catch (e) {
            error = /** @type {any} */ (e).response?.data?.detail || 'Failed to add account';`],
    [`async function deleteAccount(id) {`, `/** @param {string} id */\n    async function deleteAccount(id) {`],
    [`function groupByModel(accounts) {`, `/**\n     * @param {any[]} accounts\n     * @returns {Record<string, any[]>}\n     */\n    function groupByModel(accounts) {`]
]);

// 4. src/lib/SettingsPage.svelte
replaceFile('src/lib/SettingsPage.svelte', [
    [`let copiedKey = null;`, `/** @type {string | null} */\n    let copiedKey = null;`],
    [`const PROVIDER_LABELS = {`, `/** @type {Record<string, {name: string, color: string}>} */\n    const PROVIDER_LABELS = {`],
    [`function makeKey(modelId) {`, `/** @param {string} modelId */\n    function makeKey(modelId) {`],
    [`async function copyToClipboard(text, keyId) {`, `/**\n     * @param {string} text\n     * @param {string} keyId\n     */\n    async function copyToClipboard(text, keyId) {`],
    [`$: allModels = Object.entries($availableModels).flatMap(([provider, data]) =>
        (data.models || []).map(m => ({ ...m, provider, tier: data.tier }))
    );`, `$: allModels = Object.entries($availableModels).flatMap(([provider, data]) =>
        (data.models || []).map((/** @type {any} */ m) => ({ ...m, provider, tier: data.tier }))
    );`]
]);

// 5. src/lib/SetupScreen.svelte
replaceFile('src/lib/SetupScreen.svelte', [
    [`let chosenTool = null; // 'openclaude' | 'claude_code' | 'gemini_cli'`, `/** @type {string | null} */\n    let chosenTool = null; // 'openclaude' | 'claude_code' | 'gemini_cli'`],
    [`const TOOL_META = {`, `/** @type {Record<string, any>} */\n    const TOOL_META = {`],
    [`let status = {`, `/** @type {any} */\n    let status = {`],
    [`let toolState = {`, `/** @type {Record<string, any>} */\n    let toolState = {`],
    [`async function selectTool(tool) {`, `/** @param {string} tool */\n    async function selectTool(tool) {`],
    [`async function streamSSE(res, tool, onDone) {`, `/**\n     * @param {Response} res\n     * @param {string} tool\n     * @param {Function} onDone\n     */\n    async function streamSSE(res, tool, onDone) {`],
    [`async function install(tool) {`, `/** @param {string} tool */\n    async function install(tool) {`],
    [`async function startAuth(tool) {`, `/** @param {string} tool */\n    async function startAuth(tool) {`]
]);

// 6. src/routes/+page.svelte
let pageSvelte = fs.readFileSync('src/routes/+page.svelte', 'utf-8');

// Replacements for +page.svelte
const pageReplaces = [
    [`let chatContainer;`, `/** @type {HTMLElement} */\n    let chatContainer;`],
    [`let chatSessions = [];`, `/** @type {any[]} */\n    let chatSessions = [];`],
    [`let activeChatSessionId = null;`, `/** @type {string | null} */\n    let activeChatSessionId = null;`],
    [`let expandedProvider = null;`, `/** @type {string | null} */\n    let expandedProvider = null;`],
    [`function formatTransportLabel(path) {`, `/**\n     * @param {string} path\n     * @returns {string | null}\n     */\n    function formatTransportLabel(path) {`],
    [`function formatMessageTime(isoOrText) {`, `/**\n     * @param {string | null} isoOrText\n     * @returns {string}\n     */\n    function formatMessageTime(isoOrText) {`],
    [`function toUiRole(role) {`, `/**\n     * @param {string} role\n     * @returns {string}\n     */\n    function toUiRole(role) {`],
    [`function toUiContent(row) {`, `/**\n     * @param {any} row\n     * @returns {string}\n     */\n    function toUiContent(row) {`],
    [`async function openSavedSession(session) {`, `/**\n     * @param {any} session\n     */\n    async function openSavedSession(session) {`],
    [`async function handleDeleteSession(id) {`, `/**\n     * @param {string} id\n     */\n    async function handleDeleteSession(id) {`],
    [`function handleKeydown(e) {`, `/**\n     * @param {KeyboardEvent} e\n     */\n    function handleKeydown(e) {`],
    [`function getErrMessage(e, fallback) {`, `/**\n     * @param {any} e\n     * @param {string} fallback\n     * @returns {string}\n     */\n    function getErrMessage(e, fallback) {`],
    [`async function selectModel(provider, modelId) {`, `/**\n     * @param {string} provider\n     * @param {string} modelId\n     */\n    async function selectModel(provider, modelId) {`]
];

for (const [from, to] of pageReplaces) {
    pageSvelte = pageSvelte.replace(from, to);
}

// Arena logic commenting in +page.svelte
pageSvelte = pageSvelte.replace(
`    // Arena state
    let arenaExpanded = false;
    let arenaStatus = { running: true, bridge_active: false };
    let arenaModels = [];
    let arenaLoading = false;
    let arenaError = '';`,
`    // TODO (v2): Arena integration postponed.
    // Arena state
    /*
    let arenaExpanded = false;
    let arenaStatus = { running: true, bridge_active: false };
    let arenaModels = [];
    let arenaLoading = false;
    let arenaError = '';
    */`
);

pageSvelte = pageSvelte.replace(
`$: isArenaModel = $selectedModel.startsWith('arena/');
    $: arenaModelDisplay = isArenaModel ? $selectedModel.replace('arena/', '') : '';`,
`// TODO (v2): Arena integration postponed.
    /*
    $: isArenaModel = $selectedModel.startsWith('arena/');
    $: arenaModelDisplay = isArenaModel ? $selectedModel.replace('arena/', '') : '';
    */`
);

pageSvelte = pageSvelte.replace(
`            if (setupReady) {
                await refreshArenaStatus({ loadModels: false });`,
`            if (setupReady) {
                // TODO (v2): Arena integration postponed.
                // await refreshArenaStatus({ loadModels: false });`
);

pageSvelte = pageSvelte.replace(
`        setupReady = true;
        try {
            await refreshArenaStatus({ loadModels: false });
        } catch (e) {}`,
`        setupReady = true;
        // TODO (v2): Arena integration postponed.
        /*
        try {
            await refreshArenaStatus({ loadModels: false });
        } catch (e) {}
        */`
);

pageSvelte = pageSvelte.replace(
`            if ($selectedModel.startsWith('arena/') && /model not found|unavailable right now/i.test(msg)) {
                arenaError = 'Selected Arena model is unavailable right now. Click "Refresh Models" and choose another model.';
            }`,
`            // TODO (v2): Arena integration postponed.
            /*
            if ($selectedModel.startsWith('arena/') && /model not found|unavailable right now/i.test(msg)) {
                arenaError = 'Selected Arena model is unavailable right now. Click "Refresh Models" and choose another model.';
            }
            */`
);

pageSvelte = pageSvelte.replace(
`    async function toggleArena() {
        arenaExpanded = !arenaExpanded;
        if (arenaExpanded && arenaModels.length === 0) {
            await handleRefreshArenaModels();
        }
    }

    async function refreshArenaStatus({ loadModels = true } = {}) {
        try {
            arenaStatus = await getArenaStatus();
            if (loadModels && arenaStatus.bridge_active) {
                const data = await getArenaModels();
                const raw = Array.isArray(data.models) ? data.models : [];
                arenaModels = raw
                    .map((m) => {
                        const id = String(m || '').trim();
                        return id.startsWith('arena/') ? id : \`arena/\${id}\`;
                    })
                    .filter((id) => id !== 'arena/')
                    .sort((a, b) => a.localeCompare(b));
            }
        } catch (e) {
            arenaStatus = { running: true, bridge_active: false };
            throw e;
        }
    }

    function getErrMessage(e, fallback) {
        return e?.response?.data?.detail || e?.message || fallback;
    }

    async function handleStartArena(forceLogin = false) {
        arenaLoading = true;
        arenaError = '';
        try {
            await startArena(forceLogin);
            await refreshArenaStatus({ loadModels: true });
            if (!arenaStatus.bridge_active) {
                arenaError = 'Bridge not detected. Ensure the Native Host is installed and Chrome has the extension enabled.';
            }
        } catch (e) {
            arenaError = getErrMessage(e, 'Failed to connect to Arena Bridge.');
        } finally {
            arenaLoading = false;
        }
    }

    async function handleRefreshArenaModels() {
        arenaLoading = true;
        arenaError = '';
        try {
            await refreshArenaStatus({ loadModels: true });
            if (!arenaStatus.bridge_active) {
                arenaError = 'Bridge is not active.';
            } else if (arenaModels.length === 0) {
                arenaError = 'No models returned. Keep arena.ai open and logged in, then retry.';
            }
        } catch (e) {
            arenaError = getErrMessage(e, 'Failed to refresh Arena models.');
        } finally {
            arenaLoading = false;
        }
    }`,
`    // TODO (v2): Arena integration postponed.
    /*
    async function toggleArena() {
        arenaExpanded = !arenaExpanded;
        if (arenaExpanded && arenaModels.length === 0) {
            await handleRefreshArenaModels();
        }
    }

    async function refreshArenaStatus({ loadModels = true } = {}) {
        try {
            arenaStatus = await getArenaStatus();
            if (loadModels && arenaStatus.bridge_active) {
                const data = await getArenaModels();
                const raw = Array.isArray(data.models) ? data.models : [];
                arenaModels = raw
                    .map((m) => {
                        const id = String(m || '').trim();
                        return id.startsWith('arena/') ? id : \`arena/\${id}\`;
                    })
                    .filter((id) => id !== 'arena/')
                    .sort((a, b) => a.localeCompare(b));
            }
        } catch (e) {
            arenaStatus = { running: true, bridge_active: false };
            throw e;
        }
    }
    */

    /**
     * @param {any} e
     * @param {string} fallback
     * @returns {string}
     */
    function getErrMessage(e, fallback) {
        return e?.response?.data?.detail || e?.message || fallback;
    }

    /*
    async function handleStartArena(forceLogin = false) {
        arenaLoading = true;
        arenaError = '';
        try {
            await startArena(forceLogin);
            await refreshArenaStatus({ loadModels: true });
            if (!arenaStatus.bridge_active) {
                arenaError = 'Bridge not detected. Ensure the Native Host is installed and Chrome has the extension enabled.';
            }
        } catch (e) {
            arenaError = getErrMessage(e, 'Failed to connect to Arena Bridge.');
        } finally {
            arenaLoading = false;
        }
    }

    async function handleRefreshArenaModels() {
        arenaLoading = true;
        arenaError = '';
        try {
            await refreshArenaStatus({ loadModels: true });
            if (!arenaStatus.bridge_active) {
                arenaError = 'Bridge is not active.';
            } else if (arenaModels.length === 0) {
                arenaError = 'No models returned. Keep arena.ai open and logged in, then retry.';
            }
        } catch (e) {
            arenaError = getErrMessage(e, 'Failed to refresh Arena models.');
        } finally {
            arenaLoading = false;
        }
    }
    */`
);

pageSvelte = pageSvelte.replace(
`        if (modelId.startsWith('arena/')) {
            showPrivacyWarning = true;
            if (previousModel !== modelId) {
                input = '';
                $messages = [];
                activeChatSessionId = null;
                clearActiveSession();
            }
        }`,
`        // TODO (v2): Arena integration postponed.
        /*
        if (modelId.startsWith('arena/')) {
            showPrivacyWarning = true;
            if (previousModel !== modelId) {
                input = '';
                $messages = [];
                activeChatSessionId = null;
                clearActiveSession();
            }
        }
        */`
);

// We need to fix template usages of arena variables
pageSvelte = pageSvelte.replace(
`{isArenaModel ? arenaModelDisplay : $selectedModel}`,
`{$selectedModel}`
);

pageSvelte = pageSvelte.replace(
`placeholder="Message {isArenaModel ? arenaModelDisplay : $selectedModel}..."`,
`placeholder="Message {$selectedModel}..."`
);

pageSvelte = pageSvelte.replace(
`<div class="provider-group">
                        <button class="provider-btn {expandedProvider === 'arena' ? 'open' : ''}" on:click={() => { expandedProvider = expandedProvider === 'arena' ? null : 'arena'; if (expandedProvider === 'arena' && arenaModels.length === 0) handleRefreshArenaModels(); }}>
                            <span>Arena Models</span>
                            <svg class="chevron {expandedProvider === 'arena' ? 'open' : ''}" viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><polyline points="6 9 12 15 18 9"></polyline></svg>
                        </button>
                        {#if expandedProvider === 'arena'}
                            <div class="provider-models">
                                <div class="arena-controls">
                                    {#if !arenaStatus.bridge_active}
                                        <button class="arena-action-btn" on:click|stopPropagation={() => handleStartArena(false)}>{arenaLoading ? 'Checking...' : 'Check Bridge'}</button>
                                        {#if arenaError}<p class="arena-error">{arenaError}</p>{/if}
                                    {:else}
                                        <button class="arena-action-btn" on:click|stopPropagation={handleRefreshArenaModels}>{arenaLoading ? 'Refreshing...' : 'Refresh Models'}</button>
                                        {#if arenaError}<p class="arena-error">{arenaError}</p>{/if}
                                    {/if}
                                </div>
                                {#each arenaModels as am}
                                    <button class="model-btn {$selectedModel === am ? 'active' : ''}" on:click={() => selectModel('arena', am)}>
                                        {am.replace('arena/', '')}
                                    </button>
                                {/each}
                            </div>
                        {/if}
                    </div>`,
`<!-- TODO (v2): Arena integration postponed.
                    <div class="provider-group">
                        <button class="provider-btn {expandedProvider === 'arena' ? 'open' : ''}" on:click={() => { expandedProvider = expandedProvider === 'arena' ? null : 'arena'; if (expandedProvider === 'arena' && arenaModels.length === 0) handleRefreshArenaModels(); }}>
                            <span>Arena Models</span>
                            <svg class="chevron {expandedProvider === 'arena' ? 'open' : ''}" viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><polyline points="6 9 12 15 18 9"></polyline></svg>
                        </button>
                        {#if expandedProvider === 'arena'}
                            <div class="provider-models">
                                <div class="arena-controls">
                                    {#if !arenaStatus.bridge_active}
                                        <button class="arena-action-btn" on:click|stopPropagation={() => handleStartArena(false)}>{arenaLoading ? 'Checking...' : 'Check Bridge'}</button>
                                        {#if arenaError}<p class="arena-error">{arenaError}</p>{/if}
                                    {:else}
                                        <button class="arena-action-btn" on:click|stopPropagation={handleRefreshArenaModels}>{arenaLoading ? 'Refreshing...' : 'Refresh Models'}</button>
                                        {#if arenaError}<p class="arena-error">{arenaError}</p>{/if}
                                    {/if}
                                </div>
                                {#each arenaModels as am}
                                    <button class="model-btn {$selectedModel === am ? 'active' : ''}" on:click={() => selectModel('arena', am)}>
                                        {am.replace('arena/', '')}
                                    </button>
                                {/each}
                            </div>
                        {/if}
                    </div>
                    -->`
);

pageSvelte = pageSvelte.replace(
`{#if showPrivacyWarning}
    <div
        class="modal-overlay"
        role="button"
        tabindex="0"
        on:click|self={dismissPrivacyWarning}
        on:keydown={(e) => e.key === 'Escape' && dismissPrivacyWarning()}
    >
        <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="privacy-warning-title">
            <div class="modal-icon">⚠️</div>
            <h2 id="privacy-warning-title">Privacy Warning</h2>
            <p>
                Arena.ai collects and <strong>may publicly publish</strong> your conversation data.
            </p>
            <button class="modal-btn" on:click={dismissPrivacyWarning}>I Understand</button>
        </div>
    </div>
{/if}`,
`<!-- TODO (v2): Arena integration postponed.
{#if showPrivacyWarning}
    <div
        class="modal-overlay"
        role="button"
        tabindex="0"
        on:click|self={dismissPrivacyWarning}
        on:keydown={(e) => e.key === 'Escape' && dismissPrivacyWarning()}
    >
        <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="privacy-warning-title">
            <div class="modal-icon">⚠️</div>
            <h2 id="privacy-warning-title">Privacy Warning</h2>
            <p>
                Arena.ai collects and <strong>may publicly publish</strong> your conversation data.
            </p>
            <button class="modal-btn" on:click={dismissPrivacyWarning}>I Understand</button>
        </div>
    </div>
{/if}
-->`
);

fs.writeFileSync('src/routes/+page.svelte', pageSvelte);

console.log('Done');