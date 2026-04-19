<script>
    import { createEventDispatcher } from 'svelte';
    import { API_BASE_URL } from '$lib/config.js';

    const dispatch = createEventDispatcher();
    const BASE_URL = API_BASE_URL;
    const APP_VERSION = '0.6.0';

    /** When true, don't auto-dispatch 'ready' on mount (used when opened from Accounts). */
    export let skipAutoReady = false;

    // ── Provider definitions ──────────────────────────────────────────────
    /** @type {Record<string, any>} */
    const PROVIDERS = {
        openclaude: {
            name: 'OpenClaude',
            provider: 'Claude',
            logo: '/logos/claude.png',
            tag: 'Free access',
            tagColor: 'green',
            headline: 'Use any claude.ai account',
            desc: 'Open-source fork of Claude Code CLI. Works with free or Pro accounts.',
            warn: null,
        },
        claude_code: {
            name: 'Claude Code',
            provider: 'Claude',
            logo: '/logos/claude.png',
            tag: 'Pro required',
            tagColor: 'yellow',
            headline: 'Official Anthropic CLI',
            desc: 'Maintained directly by Anthropic. Requires Claude Pro ($20/mo).',
            warn: 'Free accounts will fail at auth.',
        },
        gemini_cli: {
            name: 'Gemini CLI',
            provider: 'Gemini',
            logo: '/logos/gemini.png',
            tag: 'Free access',
            tagColor: 'green',
            headline: 'Google Gemini CLI',
            desc: 'Free Google account. 1M token context, fast and reliable.',
            warn: null,
        },
        chatgpt_cli: {
            name: 'ChatGPT (Codex CLI)',
            provider: 'ChatGPT',
            logo: '/logos/chatgpt.png',
            tag: 'Free access',
            tagColor: 'green',
            headline: 'OpenAI Codex CLI',
            desc: 'Free ChatGPT account. Access GPT-5, Codex, and more.',
            warn: null,
        },
    };

    // ── Status ────────────────────────────────────────────────────────────
    /** @type {any} */
    let status = {
        prerequisites: { node: false, npm: false, ripgrep: false },
        openclaude: { installed: false, authenticated: false, tier: null, account_label: null },
        claude_code: { installed: false, authenticated: false, tier: null, account_label: null },
        gemini_cli: { installed: false, authenticated: false, account_label: null },
        chatgpt_cli: { installed: false, authenticated: false, tier: null, account_label: null },
        selected_tool: null,
        ready: false,
    };

    let loading = true;
    let backendError = '';
    let backendAttempt = 0;

    /** @type {Record<string, any>} */
    let toolState = {
        openclaude: { installing: false, authing: false, log: [], failed: false },
        claude_code: { installing: false, authing: false, log: [], failed: false },
        gemini_cli: { installing: false, authing: false, log: [], failed: false },
        chatgpt_cli: { installing: false, authing: false, log: [], failed: false },
    };

    /** @type {string | null} */
    let expandedTool = null;

    async function fetchStatus() {
        loading = true;
        backendError = '';
        backendAttempt = 0;
        const maxAttempts = 30;
        const delayMs = 1000;
        for (let attempt = 1; attempt <= maxAttempts; attempt++) {
            backendAttempt = attempt;
            try {
                const res = await fetch(`${BASE_URL}/setup/status`);
                status = await res.json();

                if (status.ready && !skipAutoReady) {
                    dispatch('ready', { tool: status.selected_tool });
                }
                loading = false;
                return;
            } catch {
                if (attempt < maxAttempts) {
                    await new Promise((r) => setTimeout(r, delayMs));
                }
            }
        }
        backendError = 'Cannot reach FreeHive backend — make sure it is running (see start.sh).';
        loading = false;
    }

    fetchStatus();

    // ── Computed ──────────────────────────────────────────────────────────
    $: connectedCount = ['openclaude', 'claude_code', 'gemini_cli', 'chatgpt_cli']
        .filter((k) => status[k]?.authenticated).length;
    // Claude shares auth — count as one provider
    $: connectedProviders = (() => {
        const set = new Set();
        if (status.openclaude?.authenticated || status.claude_code?.authenticated) set.add('claude');
        if (status.gemini_cli?.authenticated) set.add('gemini');
        if (status.chatgpt_cli?.authenticated) set.add('chatgpt');
        return set.size;
    })();
    $: anyConnected = connectedProviders > 0;
    $: canInstall = status.prerequisites?.npm && status.prerequisites?.node;

    // ── SSE stream helpers ────────────────────────────────────────────────

    /**
     * @param {Response} res
     * @param {string} tool
     * @param {Function} onDone
     */
    async function streamSSE(res, tool, onDone) {
        if (!res.body) return;
        const reader = res.body.getReader();
        const decoder = new TextDecoder();

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;

            const chunk = decoder.decode(value);
            for (const line of chunk.split('\n')) {
                if (!line.startsWith('data:')) continue;
                let data;
                try { data = JSON.parse(line.slice(5).trim()); } catch { continue; }

                if (data.msg && data.status !== 'waiting') {
                    toolState[tool].log = [...toolState[tool].log, data.msg];
                }

                if (data.status === 'done' || data.status === 'success' ||
                    data.status === 'failed' || data.status === 'timeout' ||
                    data.status === 'error') {
                    onDone(data);
                }
            }
        }
    }

    // ── Install ───────────────────────────────────────────────────────────

    /** @param {string} tool */
    async function install(tool) {
        toolState[tool] = { installing: true, authing: false, log: ['Starting install...'], failed: false };
        expandedTool = tool;

        const res = await fetch(`${BASE_URL}/setup/install`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ tool }),
        });

        await streamSSE(res, tool, async (/** @type {any} */ data) => {
            toolState[tool].installing = false;
            if (data.success) {
                toolState[tool].log = [...toolState[tool].log, 'Installed successfully.'];
                toolState[tool].failed = false;
            } else {
                toolState[tool].log = [...toolState[tool].log, 'Install failed — see log above.'];
                toolState[tool].failed = true;
            }
            await fetchStatus();
        });
    }

    // ── Auth ──────────────────────────────────────────────────────────────

    /** @param {string} tool */
    async function startAuth(tool) {
        toolState[tool] = { installing: false, authing: true, log: ['Launching authentication...'], failed: false };
        expandedTool = tool;

        // Persist selected tool so backend tracks it
        try {
            await fetch(`${BASE_URL}/setup/select-tool`, {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify({ tool }),
            });
        } catch { /* best effort */ }

        const res = await fetch(`${BASE_URL}/setup/auth/${tool}`);

        await streamSSE(res, tool, async (/** @type {any} */ data) => {
            toolState[tool].authing = false;
            if (data.status === 'success') {
                toolState[tool].log = [...toolState[tool].log, 'Authenticated successfully.'];
                toolState[tool].failed = false;
                await fetchStatus();
            } else {
                const msg = data.msg || data.status;
                toolState[tool].log = [...toolState[tool].log, `Failed: ${msg}`];
                toolState[tool].failed = true;
            }
        });
    }

    // ── Logout ────────────────────────────────────────────────────────────

    /** @param {string} tool */
    async function logout(tool) {
        try {
            await fetch(`${BASE_URL}/setup/logout/${tool}`, { method: 'POST' });
            toolState[tool] = { installing: false, authing: false, log: [], failed: false };
            await fetchStatus();
        } catch { /* best effort */ }
    }

    function proceed() {
        dispatch('ready', { tool: status.selected_tool });
    }

    /** @param {string} tool */
    function toggleExpanded(tool) {
        expandedTool = expandedTool === tool ? null : tool;
    }

    const _platform = (navigator.userAgentData?.platform ?? navigator.userAgent).toLowerCase();
    const isMac = _platform.includes('mac') || _platform.includes('apple');
    const isWin = _platform.includes('win');
    const rgHint = isMac ? 'brew install ripgrep' : isWin ? 'winget install BurntSushi.ripgrep' : 'sudo apt install ripgrep';
    const rgHintAlt = isMac ? 'Or: sudo port install ripgrep' : isWin ? 'Or: choco install ripgrep' : 'Or: sudo dnf install ripgrep / snap install ripgrep';
</script>

<div class="setup">
    <div class="setup-card">

        <div class="setup-header">
            <div class="logo-row">
                <span class="logo-hex">&#x2B21;</span>
                <h1>FreeHive</h1>
            </div>
            <span class="version-badge">v{APP_VERSION}</span>
            <p class="subtitle">Connect your AI providers to get started.</p>
            <p class="subtitle-hint">One app. Every frontier AI model. Zero API keys.</p>
        </div>

        {#if backendError}
            <div class="alert-error">{backendError}</div>

        {:else if loading}
            <div class="checking">
                <span class="mini-spinner"></span>
                Starting backend... (attempt {backendAttempt}/30)
            </div>

        {:else}

            <!-- ── Prerequisites ──────────────────────────────────────── -->
            {#if !status.prerequisites.node || !status.prerequisites.npm}
                <div class="prereq-section">
                    <p class="section-label">System Requirements</p>
                    <div class="prereq-grid">
                        <div class="prereq-row">
                            <span class="check {status.prerequisites.node ? 'ok' : 'fail'}">
                                {status.prerequisites.node ? '&#10003;' : '&#10007;'}
                            </span>
                            <span class="prereq-name">Node.js</span>
                            {#if !status.prerequisites.node}
                                <span class="prereq-hint">Install from nodejs.org or via nvm</span>
                            {/if}
                        </div>
                        <div class="prereq-row">
                            <span class="check {status.prerequisites.npm ? 'ok' : 'fail'}">
                                {status.prerequisites.npm ? '&#10003;' : '&#10007;'}
                            </span>
                            <span class="prereq-name">npm</span>
                            {#if !status.prerequisites.npm}
                                <span class="prereq-hint">Comes with Node.js</span>
                            {/if}
                        </div>
                    </div>
                    <div class="prereq-warn">
                        Node.js and npm are required before connecting any provider.
                    </div>
                </div>
            {/if}

            <!-- ── Provider Cards ─────────────────────────────────────── -->
            <p class="section-label">Providers</p>

            <div class="provider-list">
                {#each Object.entries(PROVIDERS) as [key, meta]}
                    {@const s = status[key] || {}}
                    {@const ts = toolState[key]}
                    {@const isExpanded = expandedTool === key}
                    {@const isBusy = ts.installing || ts.authing}
                    {@const needsRipgrep = key === 'openclaude' && !status.prerequisites.ripgrep}

                    <div class="provider-card {s.authenticated ? 'connected' : ''} {isExpanded ? 'expanded' : ''}">
                        <button class="provider-row" on:click={() => toggleExpanded(key)}>
                            <img class="provider-logo" src={meta.logo} alt={meta.provider} />
                            <div class="provider-info">
                                <div class="provider-name-row">
                                    <span class="provider-name">{meta.name}</span>
                                    <span class="provider-tag {meta.tagColor}">{meta.tag}</span>
                                </div>
                                <span class="provider-desc">{meta.desc}</span>
                                {#if s.authenticated && s.account_label}
                                    <span class="provider-account">{s.account_label}{s.tier ? ` (${s.tier})` : ''}</span>
                                {/if}
                            </div>
                            <div class="provider-status-area">
                                {#if s.authenticated}
                                    <span class="status-badge connected">
                                        <span class="status-dot"></span>
                                        Connected
                                    </span>
                                {:else if s.installed}
                                    <span class="status-badge installed">Installed</span>
                                {:else}
                                    <span class="status-badge none">Not installed</span>
                                {/if}
                                <svg class="chevron {isExpanded ? 'open' : ''}" viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><polyline points="6 9 12 15 18 9"></polyline></svg>
                            </div>
                        </button>

                        {#if isExpanded}
                            <div class="provider-detail">
                                {#if meta.warn && !s.authenticated}
                                    <div class="detail-warn">{meta.warn}</div>
                                {/if}

                                {#if needsRipgrep && !s.authenticated}
                                    <div class="ripgrep-warn">
                                        <span class="rg-label">ripgrep required</span>
                                        <div class="rg-commands">
                                            <code class="prereq-cmd">{rgHint}</code>
                                            <span class="rg-alt">{rgHintAlt}</span>
                                        </div>
                                        <span class="rg-note">Install ripgrep, then click Refresh below.</span>
                                    </div>
                                {/if}

                                <!-- Action buttons -->
                                <div class="detail-actions">
                                    {#if s.authenticated}
                                        <button class="action-btn outline danger" on:click|stopPropagation={() => logout(key)} disabled={isBusy}>
                                            Switch Account
                                        </button>
                                    {:else if !s.installed}
                                        <button
                                            class="action-btn primary"
                                            disabled={!canInstall || isBusy || (needsRipgrep)}
                                            on:click|stopPropagation={() => install(key)}
                                            title={!canInstall ? 'Install Node.js and npm first' : needsRipgrep ? 'Install ripgrep first' : ''}
                                        >
                                            {isBusy ? 'Installing...' : `Install ${meta.name}`}
                                        </button>
                                    {:else}
                                        <button
                                            class="action-btn primary"
                                            disabled={isBusy}
                                            on:click|stopPropagation={() => startAuth(key)}
                                        >
                                            {isBusy ? 'Authenticating...' : 'Authenticate'}
                                        </button>
                                    {/if}
                                </div>

                                <!-- Log -->
                                {#if ts.log.length > 0}
                                    <div class="log">
                                        {#each ts.log as line}
                                            <div class="log-line">{line}</div>
                                        {/each}
                                    </div>
                                {/if}

                                {#if isBusy}
                                    <div class="spinner-row">
                                        <span class="spinner"></span>
                                        <span class="spinner-label">
                                            {ts.installing ? 'Installing...' : 'Waiting for browser login...'}
                                        </span>
                                    </div>
                                {/if}

                                <!-- Retry on failure -->
                                {#if ts.failed && !isBusy}
                                    <div class="retry-section">
                                        <span class="retry-hint">Something went wrong. Check the log above.</span>
                                        <button class="action-btn outline" on:click|stopPropagation={() => {
                                            if (s.installed) startAuth(key);
                                            else install(key);
                                        }}>
                                            Try Again
                                        </button>
                                    </div>
                                {/if}
                            </div>
                        {/if}
                    </div>
                {/each}
            </div>

            <!-- ── Arena teaser ────────────────────────────────────────── -->
            <div class="arena-teaser">
                <img class="arena-teaser-logo" src="/logos/arena.png" alt="Arena" />
                <div class="arena-teaser-text">
                    <span class="arena-teaser-title">150+ Arena Models</span>
                    <span class="arena-teaser-desc">Access every model on LMSYS Chatbot Arena via Chrome extension bridge. Set up in the Arena panel after completing initial setup.</span>
                </div>
            </div>

            <!-- ── Footer ─────────────────────────────────────────────── -->
            <div class="setup-footer">
                <button class="continue-btn" disabled={!anyConnected} on:click={proceed}>
                    {#if anyConnected}
                        Continue to FreeHive ({connectedProviders} provider{connectedProviders !== 1 ? 's' : ''} connected)
                    {:else}
                        Connect at least one provider to continue
                    {/if}
                </button>

                <button class="refresh-btn" on:click={fetchStatus}>Refresh status</button>
            </div>

        {/if}

    </div>
</div>

<style>
    .setup {
        display: flex;
        align-items: flex-start;
        justify-content: center;
        min-height: 100vh;
        background: var(--bg-primary);
        padding: 48px 16px;
        color: var(--text-primary);
    }

    .setup-card {
        width: 100%;
        max-width: 640px;
        display: flex;
        flex-direction: column;
        gap: 20px;
    }

    /* Header */
    .setup-header {
        text-align: center;
        margin-bottom: 4px;
    }

    .logo-row {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 4px;
        justify-content: center;
    }

    .logo-hex {
        font-size: 24px;
        color: var(--text-primary);
        line-height: 1;
    }

    .setup-header h1 {
        font-size: 24px;
        font-weight: 600;
        color: var(--text-primary);
        letter-spacing: -0.5px;
    }

    .version-badge {
        display: inline-block;
        font-size: 11px;
        color: var(--text-muted);
        background: var(--bg-tertiary);
        padding: 2px 8px;
        border-radius: 10px;
        margin-top: 4px;
        letter-spacing: 0.3px;
    }

    .subtitle {
        font-size: 15px;
        color: var(--text-secondary);
        margin-top: 12px;
    }

    .subtitle-hint {
        font-size: 12px;
        color: var(--text-muted);
        margin-top: 4px;
    }

    /* Loading */
    .checking {
        font-size: 14px;
        color: var(--text-secondary);
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 12px;
    }

    .mini-spinner {
        width: 12px;
        height: 12px;
        border: 2px solid var(--border-medium);
        border-top-color: var(--text-primary);
        border-radius: 50%;
        animation: spin 0.7s linear infinite;
        flex-shrink: 0;
    }

    .alert-error {
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        color: #ef4444;
        padding: 12px 16px;
        border-radius: 8px;
        font-size: 14px;
        line-height: 1.5;
        text-align: center;
    }

    /* Section label */
    .section-label {
        font-size: 11px;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 0.8px;
        margin-bottom: 0;
    }

    /* Prerequisites */
    .prereq-section {
        display: flex;
        flex-direction: column;
        gap: 10px;
    }

    .prereq-grid {
        display: flex;
        flex-direction: column;
        gap: 8px;
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        border-radius: 10px;
        padding: 14px 16px;
    }

    .prereq-row {
        display: flex;
        align-items: center;
        gap: 10px;
        font-size: 14px;
    }

    .check { font-size: 14px; font-weight: 600; width: 16px; flex-shrink: 0; }
    .check.ok   { color: var(--accent-color); }
    .check.fail { color: #ef4444; }

    .prereq-name { color: var(--text-primary); flex-shrink: 0; }
    .prereq-hint { color: var(--text-secondary); font-size: 13px; }

    .prereq-warn {
        font-size: 13px;
        color: #f59e0b;
        background: var(--bg-tertiary);
        border-radius: 8px;
        padding: 10px 14px;
    }

    /* Provider list */
    .provider-list {
        display: flex;
        flex-direction: column;
        gap: 8px;
    }

    .provider-card {
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        border-radius: 12px;
        overflow: hidden;
        transition: border-color 0.2s, box-shadow 0.2s;
    }

    .provider-card.connected {
        border-color: color-mix(in srgb, var(--accent-color) 40%, var(--border-medium));
    }

    .provider-card.expanded {
        box-shadow: 0 2px 12px rgba(0,0,0,0.08);
    }

    .provider-row {
        display: flex;
        align-items: center;
        gap: 14px;
        padding: 14px 16px;
        width: 100%;
        background: transparent;
        border: none;
        cursor: pointer;
        text-align: left;
        color: inherit;
        font: inherit;
        transition: background 0.15s;
    }

    .provider-row:hover {
        background: var(--bg-tertiary);
    }

    .provider-logo {
        width: 36px;
        height: 36px;
        border-radius: 8px;
        object-fit: contain;
        flex-shrink: 0;
        border: 1px solid var(--border-light);
        background: var(--bg-primary);
        padding: 4px;
    }

    .provider-info {
        flex: 1;
        min-width: 0;
        display: flex;
        flex-direction: column;
        gap: 2px;
    }

    .provider-name-row {
        display: flex;
        align-items: center;
        gap: 8px;
    }

    .provider-name {
        font-size: 14px;
        font-weight: 600;
        color: var(--text-primary);
    }

    .provider-tag {
        font-size: 10px;
        font-weight: 500;
        padding: 1px 7px;
        border-radius: 10px;
        flex-shrink: 0;
    }

    .provider-tag.green  { background: var(--bg-tertiary); color: var(--accent-color); }
    .provider-tag.yellow { background: var(--bg-tertiary); color: #f59e0b; }

    .provider-desc {
        font-size: 12px;
        color: var(--text-secondary);
        line-height: 1.3;
    }

    .provider-account {
        font-size: 11px;
        color: var(--accent-color);
        margin-top: 2px;
    }

    .provider-status-area {
        display: flex;
        align-items: center;
        gap: 8px;
        flex-shrink: 0;
    }

    .status-badge {
        font-size: 11px;
        padding: 3px 8px;
        border-radius: 6px;
        background: var(--bg-tertiary);
        color: var(--text-muted);
        display: inline-flex;
        align-items: center;
        gap: 5px;
        white-space: nowrap;
    }

    .status-badge.connected { color: var(--accent-color); }
    .status-badge.installed { color: #f59e0b; }

    .status-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--accent-color);
        flex-shrink: 0;
    }

    .chevron {
        color: var(--text-muted);
        transition: transform 0.2s;
        flex-shrink: 0;
    }

    .chevron.open { transform: rotate(180deg); }

    /* Provider detail (expanded) */
    .provider-detail {
        padding: 0 16px 16px;
        display: flex;
        flex-direction: column;
        gap: 12px;
        border-top: 1px solid var(--border-light);
        margin-top: -1px;
        padding-top: 14px;
    }

    .detail-warn {
        font-size: 12px;
        color: #f59e0b;
        background: var(--bg-tertiary);
        border-radius: 6px;
        padding: 8px 12px;
        line-height: 1.4;
    }

    .detail-actions {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
    }

    /* Ripgrep warning */
    .ripgrep-warn {
        background: var(--bg-tertiary);
        border-radius: 8px;
        padding: 12px 14px;
        display: flex;
        flex-direction: column;
        gap: 6px;
    }

    .rg-label {
        font-size: 12px;
        font-weight: 600;
        color: #f59e0b;
    }

    .rg-commands {
        display: flex;
        flex-direction: column;
        gap: 4px;
    }

    code.prereq-cmd {
        font-size: 12px;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        background: var(--bg-primary);
        color: var(--text-primary);
        padding: 6px 10px;
        border-radius: 6px;
        display: inline-block;
    }

    .rg-alt {
        font-size: 11px;
        color: var(--text-muted);
    }

    .rg-note {
        font-size: 11px;
        color: var(--text-secondary);
    }

    /* Buttons */
    .action-btn {
        padding: 8px 16px;
        border-radius: 8px;
        font-size: 13px;
        font-weight: 500;
        cursor: pointer;
        transition: opacity 0.2s, background 0.15s;
        border: none;
    }

    .action-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .action-btn:hover:not(:disabled) { opacity: 0.85; }

    .action-btn.primary {
        background: var(--text-primary);
        color: var(--bg-primary);
    }

    .action-btn.outline {
        background: var(--bg-tertiary);
        color: var(--text-secondary);
        border: 1px solid var(--border-medium);
    }

    .action-btn.danger {
        color: #ef4444;
    }

    /* Log */
    .log {
        background: var(--bg-tertiary);
        border-radius: 8px;
        padding: 10px 14px;
        max-height: 140px;
        overflow-y: auto;
        display: flex;
        flex-direction: column;
        gap: 3px;
    }

    .log-line {
        font-size: 11px;
        color: var(--text-secondary);
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        white-space: pre-wrap;
        word-break: break-all;
        line-height: 1.5;
    }

    /* Spinner */
    .spinner-row {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 4px 0;
    }

    .spinner {
        width: 14px;
        height: 14px;
        border: 2px solid var(--border-medium);
        border-top-color: var(--text-primary);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
        flex-shrink: 0;
    }

    @keyframes spin { to { transform: rotate(360deg); } }

    .spinner-label { font-size: 12px; color: var(--text-secondary); }

    /* Retry */
    .retry-section {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 8px 12px;
        background: rgba(239, 68, 68, 0.06);
        border-radius: 8px;
    }

    .retry-hint {
        font-size: 12px;
        color: var(--text-secondary);
        flex: 1;
    }

    /* Arena teaser */
    .arena-teaser {
        display: flex;
        align-items: center;
        gap: 14px;
        padding: 14px 16px;
        background: var(--bg-secondary);
        border: 1px dashed var(--border-medium);
        border-radius: 12px;
    }

    .arena-teaser-logo {
        width: 32px;
        height: 32px;
        border-radius: 6px;
        object-fit: contain;
        flex-shrink: 0;
    }

    .arena-teaser-text {
        display: flex;
        flex-direction: column;
        gap: 2px;
    }

    .arena-teaser-title {
        font-size: 13px;
        font-weight: 600;
        color: var(--text-primary);
    }

    .arena-teaser-desc {
        font-size: 12px;
        color: var(--text-secondary);
        line-height: 1.4;
    }

    /* Footer */
    .setup-footer {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 8px;
        margin-top: 4px;
    }

    .continue-btn {
        background: var(--text-primary);
        border: none;
        color: var(--bg-primary);
        padding: 14px;
        border-radius: 12px;
        font-size: 15px;
        font-weight: 500;
        cursor: pointer;
        width: 100%;
        transition: opacity 0.2s;
    }

    .continue-btn:hover:not(:disabled) { opacity: 0.8; }

    .continue-btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
        background: var(--bg-tertiary);
        color: var(--text-muted);
    }

    .refresh-btn {
        background: transparent;
        border: none;
        color: var(--text-muted);
        font-size: 13px;
        cursor: pointer;
        padding: 8px 12px;
        transition: color 0.2s;
    }

    .refresh-btn:hover { color: var(--text-primary); }
</style>
