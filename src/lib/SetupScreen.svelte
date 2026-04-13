<script>
    import { createEventDispatcher } from 'svelte';
    import { API_BASE_URL } from '$lib/config.js';

    const dispatch = createEventDispatcher();
    const BASE_URL = API_BASE_URL;

    // ── Setup step ────────────────────────────────────────────────────────
    // 'choose'  → user picks openclaude, claude_code, or gemini_cli
    // 'setup'   → install + auth for the chosen tool
    let step = 'choose';
    /** @type {string | null} */
    let chosenTool = null; // 'openclaude' | 'claude_code' | 'gemini_cli'

    /** @type {Record<string, any>} */
    const TOOL_META = {
        openclaude: {
            name: 'OpenClaude',
            tag: 'Free access',
            tagColor: 'green',
            headline: 'Use any claude.ai account',
            bullets: [
                'Works with a free or Pro claude.ai account',
                'Open-source fork of Claude Code CLI',
                'Auth handled silently — FreeHive controls everything',
                'Best choice if you just want to get started',
            ],
            warn: null,
        },
        claude_code: {
            name: 'Claude Code',
            tag: 'Pro required',
            tagColor: 'yellow',
            headline: 'Official Anthropic CLI',
            bullets: [
                'Requires an active Claude Pro subscription',
                'Maintained directly by Anthropic — most stable',
                'Auth handled silently — FreeHive controls everything',
                'Best if you already have a Pro account',
            ],
            warn: 'Requires Claude Pro ($20/mo). Free accounts will fail at auth.',
        },
        gemini_cli: {
            name: 'Gemini CLI',
            tag: 'Free access',
            tagColor: 'green',
            headline: 'Official Google Gemini CLI',
            bullets: [
                'Works with a free Google account',
                '1 million token context window',
                'Direct API access — fast and reliable',
                'Best for large documents and complex tasks',
            ],
            warn: null,
        },
    };

    // ── Status ────────────────────────────────────────────────────────────
    /** @type {any} */
    let status = {
        prerequisites: { node: false, npm: false, ripgrep: false },
        openclaude: { installed: false, authenticated: false, tier: null },
        claude_code: { installed: false, authenticated: false, tier: null },
        gemini_cli: { installed: false, authenticated: false },
        selected_tool: null,
        ready: false,
    };

    let loading = true;
    let backendError = '';

    /** @type {Record<string, any>} */
    let toolState = {
        openclaude: { installing: false, authing: false, log: [] },
        claude_code: { installing: false, authing: false, log: [] },
        gemini_cli: { installing: false, authing: false, log: [] },
    };

    async function fetchStatus() {
        loading = true;
        backendError = '';
        const maxAttempts = 15;
        const delayMs = 1000;
        for (let attempt = 1; attempt <= maxAttempts; attempt++) {
            try {
                const res = await fetch(`${BASE_URL}/setup/status`);
                status = await res.json();

                if (status.selected_tool) {
                    chosenTool = status.selected_tool;
                    step = 'setup';
                }

                if (status.ready) {
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

    // ── Tool selection ────────────────────────────────────────────────────

    /** @param {string} tool */
    async function selectTool(tool) {
        chosenTool = tool;
        try {
            await fetch(`${BASE_URL}/setup/select-tool`, {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify({ tool }),
            });
        } catch {
        }
        step = 'setup';
    }

    function backToChoose() {
        step = 'choose';
        chosenTool = null;
    }

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
        toolState[tool] = { installing: true, authing: false, log: ['Starting install…'] };

        const res = await fetch(`${BASE_URL}/setup/install`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ tool }),
        });

        await streamSSE(res, tool, async (/** @type {any} */ data) => {
            toolState[tool].installing = false;
            if (data.success) {
                toolState[tool].log = [...toolState[tool].log, '✓ Installed successfully.'];
            } else {
                toolState[tool].log = [...toolState[tool].log, '✗ Install failed — see log above.'];
            }
            await fetchStatus();
        });
    }

    // ── Auth ──────────────────────────────────────────────────────────────

    /** @param {string} tool */
    async function startAuth(tool) {
        toolState[tool] = { installing: false, authing: true, log: ['Launching authentication…'] };

        const res = await fetch(`${BASE_URL}/setup/auth/${tool}`);

        await streamSSE(res, tool, async (/** @type {any} */ data) => {
            toolState[tool].authing = false;
            if (data.status === 'success') {
                toolState[tool].log = [...toolState[tool].log, '✓ Authenticated.'];
                await fetchStatus();
            } else {
                const msg = data.msg || data.status;
                toolState[tool].log = [...toolState[tool].log, `✗ ${msg}`];
            }
        });
    }

    function proceed() {
        dispatch('ready', { tool: chosenTool });
    }

    const rgHint = navigator.platform?.toLowerCase().includes('mac')
        ? 'brew install ripgrep'
        : 'sudo apt install ripgrep';
</script>

<div class="setup">
    <div class="setup-card">

        <div class="setup-header">
            <div class="logo-row">
                <span class="logo-hex">⬡</span>
                <h1>FreeHive</h1>
            </div>
            <p class="subtitle">
                {#if step === 'choose'}First, choose how to connect.
                {:else}{TOOL_META[chosenTool || 'openclaude'].name} selected
                    <button class="back-link" on:click={backToChoose}>← change</button>
                {/if}
            </p>
        </div>

        {#if backendError}
            <div class="alert-error">{backendError}</div>

        {:else if loading}
            <div class="checking">
                <span class="mini-spinner"></span>
                Checking your environment…
            </div>

        {:else if step === 'choose'}

            <!-- ── Step 1: Choose tool ─────────────────────────────────── -->
            <p class="choose-label">How are you connecting?</p>

            <div class="choice-grid">
                {#each Object.entries(TOOL_META) as [key, meta]}
                    <button class="choice-card" on:click={() => selectTool(key)}>
                        <div class="choice-top">
                            <span class="choice-name">{meta.name}</span>
                            <span class="choice-tag {meta.tagColor}">{meta.tag}</span>
                        </div>
                        <p class="choice-headline">{meta.headline}</p>
                        <ul class="choice-bullets">
                            {#each meta.bullets as b}
                                <li>{b}</li>
                            {/each}
                        </ul>
                        {#if meta.warn}
                            <p class="choice-warn">⚠ {meta.warn}</p>
                        {/if}
                        <span class="choice-cta">Select →</span>
                    </button>
                {/each}
            </div>

        {:else if step === 'setup'}

            <!-- ── Step 2: Install + Auth for chosen tool ─────────────── -->

            <!-- Prerequisites -->
            <div class="prereq-section">
                <p class="section-label">System Requirements</p>
                <div class="prereq-grid">
                    <div class="prereq-row">
                        <span class="check {status.prerequisites.node ? 'ok' : 'fail'}">
                            {status.prerequisites.node ? '✓' : '✗'}
                        </span>
                        <span class="prereq-name">Node.js</span>
                        {#if !status.prerequisites.node}
                            <span class="prereq-hint">Install from nodejs.org or via nvm</span>
                        {/if}
                    </div>
                    <div class="prereq-row">
                        <span class="check {status.prerequisites.npm ? 'ok' : 'fail'}">
                            {status.prerequisites.npm ? '✓' : '✗'}
                        </span>
                        <span class="prereq-name">npm</span>
                        {#if !status.prerequisites.npm}
                            <span class="prereq-hint">Comes with Node.js</span>
                        {/if}
                    </div>
                    {#if chosenTool === 'openclaude'}
                        <div class="prereq-row">
                            <span class="check {status.prerequisites.ripgrep ? 'ok' : 'fail'}">
                                {status.prerequisites.ripgrep ? '✓' : '✗'}
                            </span>
                            <span class="prereq-name">ripgrep</span>
                            {#if !status.prerequisites.ripgrep}
                                <code class="prereq-cmd">{rgHint}</code>
                            {/if}
                        </div>
                    {/if}
                </div>

                {#if !status.prerequisites.node || !status.prerequisites.npm}
                    <div class="prereq-warn">
                        Node.js and npm are required before installing the tool below.
                    </div>
                {/if}
                {#if chosenTool === 'openclaude' && !status.prerequisites.ripgrep}
                    <div class="prereq-warn">
                        ripgrep is required by OpenClaude. Install it then refresh.
                    </div>
                {/if}
            </div>

            <!-- Single tool card for the chosen tool -->
            {@const s = status[chosenTool || 'openclaude']}
            {@const ts = toolState[chosenTool || 'openclaude']}
            {@const meta = TOOL_META[chosenTool || 'openclaude']}
            {@const canInstall = status.prerequisites.npm && status.prerequisites.node}

            <div class="tool-card {s.authenticated ? 'connected' : ''}">
                <div class="tool-header">
                    <div class="tool-meta">
                        <span class="tool-name">{meta.name}</span>
                        <span class="tool-desc">{meta.headline}</span>
                    </div>
                    <div class="badge-wrap">
                        {#if s.authenticated}
                            <span class="badge green">
                                <span class="dot"></span>
                                Connected{s.tier ? ` · ${s.tier}` : ''}
                            </span>
                        {:else if s.installed}
                            <span class="badge yellow">Installed</span>
                        {:else}
                            <span class="badge gray">Not installed</span>
                        {/if}
                    </div>
                </div>

                {#if !s.installed && !ts.installing}
                    <button
                        class="action-btn"
                        disabled={!canInstall || ts.authing}
                        on:click={() => install(chosenTool || 'openclaude')}
                        title={!canInstall ? 'Install Node.js and npm first' : ''}
                    >
                        Install {meta.name}
                    </button>
                {/if}

                {#if s.installed && !s.authenticated && !ts.authing}
                    <button
                        class="action-btn"
                        disabled={ts.installing}
                        on:click={() => startAuth(chosenTool || 'openclaude')}
                    >
                        Authenticate
                    </button>
                {/if}

                {#if ts.log.length > 0}
                    <div class="log">
                        {#each ts.log as line}
                            <div class="log-line">{line}</div>
                        {/each}
                    </div>
                {/if}

                {#if ts.installing || ts.authing}
                    <div class="spinner-row">
                        <span class="spinner"></span>
                        <span class="spinner-label">
                            {ts.installing ? 'Installing…' : 'Waiting for browser login…'}
                        </span>
                    </div>
                {/if}
            </div>

            <button class="continue-btn" disabled={!status.ready} on:click={proceed}>
                {status.ready ? 'Continue to FreeHive →' : 'Connect your account above to continue'}
            </button>

            <button class="refresh-btn" on:click={fetchStatus}>↻ Refresh status</button>

        {/if}

    </div>
</div>

<style>
    .setup {
        display: flex;
        align-items: center;
        justify-content: center;
        min-height: 100vh;
        background: var(--bg-primary);
        padding: 32px 16px;
        color: var(--text-primary);
    }

    .setup-card {
        width: 100%;
        max-width: 600px;
        display: flex;
        flex-direction: column;
        gap: 24px;
    }

    /* Header */
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

    .setup-header {
        text-align: center;
        margin-bottom: 8px;
    }

    .setup-header h1 {
        font-size: 24px;
        font-weight: 600;
        color: var(--text-primary);
        letter-spacing: -0.5px;
    }

    .subtitle {
        font-size: 14px;
        color: var(--text-secondary);
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        margin-top: 8px;
    }

    .back-link {
        background: none;
        border: none;
        color: var(--text-muted);
        font-size: 13px;
        cursor: pointer;
        padding: 0;
        text-decoration: underline;
        text-underline-offset: 2px;
    }
    .back-link:hover { color: var(--text-primary); }

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

    /* Step 1: Choose */
    .choose-label {
        font-size: 12px;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 0.8px;
        text-align: center;
    }

    .choice-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
        gap: 16px;
    }

    .choice-card {
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        border-radius: 12px;
        padding: 20px;
        text-align: left;
        cursor: pointer;
        display: flex;
        flex-direction: column;
        gap: 12px;
        transition: border-color 0.2s, box-shadow 0.2s, transform 0.2s;
        color: inherit;
        font: inherit;
        min-height: 220px;
    }

    .choice-card:hover {
        border-color: var(--text-muted);
        box-shadow: var(--shadow-subtle);
        transform: translateY(-2px);
    }

    .choice-top {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
    }

    .choice-name {
        font-size: 16px;
        font-weight: 600;
        color: var(--text-primary);
    }

    .choice-tag {
        font-size: 11px;
        font-weight: 500;
        padding: 2px 8px;
        border-radius: 12px;
        flex-shrink: 0;
    }

    .choice-tag.green  { background: var(--bg-tertiary); color: var(--accent-color); }
    .choice-tag.yellow { background: var(--bg-tertiary); color: #f59e0b; }

    .choice-headline {
        font-size: 13px;
        color: var(--text-secondary);
        margin: 0;
        line-height: 1.4;
    }

    .choice-bullets {
        list-style: none;
        padding: 0;
        margin: 0;
        display: flex;
        flex-direction: column;
        gap: 6px;
    }

    .choice-bullets li {
        font-size: 12px;
        color: var(--text-secondary);
        padding-left: 14px;
        position: relative;
        line-height: 1.4;
    }

    .choice-bullets li::before {
        content: '·';
        position: absolute;
        left: 4px;
        color: var(--text-muted);
    }

    .choice-warn {
        font-size: 12px;
        color: #f59e0b;
        background: var(--bg-tertiary);
        border-radius: 6px;
        padding: 8px 12px;
        margin: 0;
        line-height: 1.4;
    }

    .choice-cta {
        font-size: 13px;
        font-weight: 500;
        color: var(--text-primary);
        margin-top: auto;
        opacity: 0;
        transform: translateX(-4px);
        transition: opacity 0.2s, transform 0.2s;
    }

    .choice-card:hover .choice-cta { opacity: 1; transform: translateX(0); }

    /* Prerequisites */
    .section-label {
        font-size: 12px;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 0.8px;
        margin-bottom: 12px;
    }

    .prereq-section {
        display: flex;
        flex-direction: column;
        gap: 12px;
    }

    .prereq-grid {
        display: flex;
        flex-direction: column;
        gap: 8px;
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        border-radius: 12px;
        padding: 16px;
    }

    .prereq-row {
        display: flex;
        align-items: center;
        gap: 12px;
        font-size: 14px;
    }

    .check { font-size: 14px; font-weight: 600; width: 16px; flex-shrink: 0; }
    .check.ok   { color: var(--accent-color); }
    .check.fail { color: #ef4444; }

    .prereq-name { color: var(--text-primary); flex-shrink: 0; }
    .prereq-hint { color: var(--text-secondary); font-size: 13px; }

    code.prereq-cmd {
        font-size: 12px;
        font-family: monospace;
        background: var(--bg-tertiary);
        color: var(--text-primary);
        padding: 4px 8px;
        border-radius: 6px;
    }

    .prereq-warn {
        font-size: 13px;
        color: #f59e0b;
        background: var(--bg-tertiary);
        border-radius: 8px;
        padding: 12px 16px;
    }

    /* Tool card */
    .tool-card {
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        border-radius: 12px;
        padding: 20px;
        display: flex;
        flex-direction: column;
        gap: 16px;
        transition: border-color 0.2s;
    }

    .tool-card.connected {
        border-color: var(--border-medium);
        background: var(--bg-secondary);
    }

    .tool-header {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 16px;
    }

    .tool-meta { display: flex; flex-direction: column; gap: 4px; }
    .tool-name { font-size: 16px; font-weight: 600; color: var(--text-primary); }
    .tool-desc { font-size: 13px; color: var(--text-secondary); }

    .badge-wrap { flex-shrink: 0; }

    .badge {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        padding: 4px 10px;
        border-radius: 6px;
        background: var(--bg-tertiary);
        color: var(--text-secondary);
    }

    .badge.green  { color: var(--accent-color); }
    .badge.yellow { color: #f59e0b; }

    .dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--accent-color);
        flex-shrink: 0;
    }

    .action-btn {
        background: var(--text-primary);
        border: none;
        color: var(--bg-primary);
        padding: 10px 16px;
        border-radius: 8px;
        font-size: 14px;
        font-weight: 500;
        cursor: pointer;
        text-align: center;
        transition: opacity 0.2s;
        width: fit-content;
    }

    .action-btn:hover:not(:disabled) { opacity: 0.8; }
    .action-btn:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Log */
    .log {
        background: var(--bg-tertiary);
        border-radius: 8px;
        padding: 12px 16px;
        max-height: 160px;
        overflow-y: auto;
        display: flex;
        flex-direction: column;
        gap: 4px;
    }

    .log-line {
        font-size: 12px;
        color: var(--text-secondary);
        font-family: monospace;
        white-space: pre-wrap;
        word-break: break-all;
        line-height: 1.5;
    }

    /* Spinner */
    .spinner-row {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 8px 0;
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

    .spinner-label { font-size: 13px; color: var(--text-secondary); }

    /* Continue / Refresh */
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
        margin-top: 8px;
    }

    .continue-btn:hover:not(:disabled) {
        opacity: 0.8;
    }

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
        align-self: center;
        padding: 8px 12px;
        transition: color 0.2s;
    }

    .refresh-btn:hover { color: var(--text-primary); }
</style>
