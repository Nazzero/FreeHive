<script>
    import { createEventDispatcher } from 'svelte';

    const dispatch = createEventDispatcher();
    const BASE_URL = 'http://localhost:8000/api';

    // ── Setup step ────────────────────────────────────────────────────────
    // 'choose'  → user picks openclaude or claude_code
    // 'setup'   → install + auth for the chosen tool
    let step = 'choose';
    let chosenTool = null; // 'openclaude' | 'claude_code'

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
    };

    // ── Status ────────────────────────────────────────────────────────────
    let status = {
        prerequisites: { node: false, npm: false, ripgrep: false },
        openclaude: { installed: false, authenticated: false, tier: null },
        claude_code: { installed: false, authenticated: false, tier: null },
        selected_tool: null,
        ready: false,
    };

    let loading = true;
    let backendError = '';

    let toolState = {
        openclaude: { installing: false, authing: false, log: [] },
        claude_code: { installing: false, authing: false, log: [] },
    };

    async function fetchStatus() {
        loading = true;
        backendError = '';
        try {
            const res = await fetch(`${BASE_URL}/setup/status`);
            status = await res.json();

            // If already configured from a previous run, skip straight to setup step
            if (status.selected_tool) {
                chosenTool = status.selected_tool;
                step = 'setup';
            }

            // If already fully ready, proceed immediately
            if (status.ready) {
                dispatch('ready', { tool: status.selected_tool });
            }
        } catch {
            backendError = 'Cannot reach FreeHive backend — make sure it is running (see start.sh).';
        } finally {
            loading = false;
        }
    }

    fetchStatus();

    // ── Tool selection ────────────────────────────────────────────────────

    async function selectTool(tool) {
        chosenTool = tool;
        // Persist choice to backend config
        try {
            await fetch(`${BASE_URL}/setup/select-tool`, {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify({ tool }),
            });
        } catch {
            // Non-fatal — choice still works in memory for this session
        }
        step = 'setup';
    }

    function backToChoose() {
        step = 'choose';
        chosenTool = null;
    }

    // ── SSE stream helpers ────────────────────────────────────────────────

    async function streamSSE(res, tool, onDone) {
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

    async function install(tool) {
        toolState[tool] = { installing: true, authing: false, log: ['Starting install…'] };

        const res = await fetch(`${BASE_URL}/setup/install`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ tool }),
        });

        await streamSSE(res, tool, async (data) => {
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

    async function startAuth(tool) {
        toolState[tool] = { installing: false, authing: true, log: ['Launching authentication…'] };

        const res = await fetch(`${BASE_URL}/setup/auth/${tool}`);

        await streamSSE(res, tool, async (data) => {
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
                {#if step === 'choose'}First, choose how to connect Claude.
                {:else}{TOOL_META[chosenTool].name} selected
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
            {@const s = status[chosenTool]}
            {@const ts = toolState[chosenTool]}
            {@const meta = TOOL_META[chosenTool]}
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
                            <span class="badge yellow">Installed · not authenticated</span>
                        {:else}
                            <span class="badge gray">Not installed</span>
                        {/if}
                    </div>
                </div>

                {#if !s.installed && !ts.installing}
                    <button
                        class="action-btn"
                        disabled={!canInstall || ts.authing}
                        on:click={() => install(chosenTool)}
                        title={!canInstall ? 'Install Node.js and npm first' : ''}
                    >
                        Install {meta.name}
                    </button>
                {/if}

                {#if s.installed && !s.authenticated && !ts.authing}
                    <button
                        class="action-btn"
                        disabled={ts.installing}
                        on:click={() => startAuth(chosenTool)}
                    >
                        Authenticate — opens browser
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
        background: #0a0a0a;
        padding: 32px 16px;
    }

    .setup-card {
        width: 580px;
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
    }

    .logo-hex {
        font-size: 20px;
        color: #6366f1;
        line-height: 1;
    }

    .setup-header h1 {
        font-size: 20px;
        font-weight: 600;
        color: #fff;
        letter-spacing: -0.3px;
    }

    .subtitle {
        font-size: 13px;
        color: #444;
        display: flex;
        align-items: center;
        gap: 8px;
    }

    .back-link {
        background: none;
        border: none;
        color: #5555aa;
        font-size: 12px;
        cursor: pointer;
        padding: 0;
        text-decoration: underline;
        text-underline-offset: 2px;
    }
    .back-link:hover { color: #8888ff; }

    /* Loading */
    .checking {
        font-size: 13px;
        color: #444;
        display: flex;
        align-items: center;
        gap: 8px;
    }

    .mini-spinner {
        width: 10px;
        height: 10px;
        border: 1.5px solid #2a2a2a;
        border-top-color: #6366f1;
        border-radius: 50%;
        animation: spin 0.7s linear infinite;
        flex-shrink: 0;
    }

    .alert-error {
        background: #2a1a1a;
        border: 1px solid #5a2a2a;
        color: #ff8888;
        padding: 12px 14px;
        border-radius: 8px;
        font-size: 13px;
        line-height: 1.5;
    }

    /* Step 1: Choose */
    .choose-label {
        font-size: 11px;
        color: #444;
        text-transform: uppercase;
        letter-spacing: 0.8px;
    }

    .choice-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 12px;
    }

    .choice-card {
        background: #111;
        border: 1px solid #1e1e1e;
        border-radius: 12px;
        padding: 18px;
        text-align: left;
        cursor: pointer;
        display: flex;
        flex-direction: column;
        gap: 10px;
        transition: border-color 0.15s, background 0.15s;
        color: inherit;
        font: inherit;
    }

    .choice-card:hover {
        border-color: #3333aa;
        background: #13131f;
    }

    .choice-top {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
    }

    .choice-name {
        font-size: 14px;
        font-weight: 600;
        color: #e0e0e0;
    }

    .choice-tag {
        font-size: 10px;
        font-weight: 500;
        padding: 2px 7px;
        border-radius: 4px;
        flex-shrink: 0;
    }

    .choice-tag.green  { background: #0d2a1a; color: #3ecf8e; border: 1px solid #1a4a2a; }
    .choice-tag.yellow { background: #2a2010; color: #f59e0b; border: 1px solid #4a3510; }

    .choice-headline {
        font-size: 12px;
        color: #666;
        margin: 0;
        line-height: 1.4;
    }

    .choice-bullets {
        list-style: none;
        padding: 0;
        margin: 0;
        display: flex;
        flex-direction: column;
        gap: 5px;
    }

    .choice-bullets li {
        font-size: 11.5px;
        color: #4a4a4a;
        padding-left: 14px;
        position: relative;
        line-height: 1.4;
    }

    .choice-bullets li::before {
        content: '·';
        position: absolute;
        left: 4px;
        color: #333;
    }

    .choice-warn {
        font-size: 11px;
        color: #a87a20;
        background: #1c1600;
        border: 1px solid #3a2e00;
        border-radius: 5px;
        padding: 6px 9px;
        margin: 0;
        line-height: 1.4;
    }

    .choice-cta {
        font-size: 12px;
        color: #4444aa;
        margin-top: 4px;
        transition: color 0.15s;
    }

    .choice-card:hover .choice-cta { color: #8888ff; }

    /* Prerequisites */
    .section-label {
        font-size: 11px;
        color: #444;
        text-transform: uppercase;
        letter-spacing: 0.8px;
        margin-bottom: 10px;
    }

    .prereq-section {
        display: flex;
        flex-direction: column;
        gap: 10px;
    }

    .prereq-grid {
        display: flex;
        flex-direction: column;
        gap: 6px;
        background: #111;
        border: 1px solid #1e1e1e;
        border-radius: 8px;
        padding: 12px 14px;
    }

    .prereq-row {
        display: flex;
        align-items: center;
        gap: 10px;
        font-size: 13px;
    }

    .check { font-size: 13px; font-weight: 600; width: 14px; flex-shrink: 0; }
    .check.ok   { color: #3ecf8e; }
    .check.fail { color: #ef4444; }

    .prereq-name { color: #aaa; flex-shrink: 0; }
    .prereq-hint { color: #444; font-size: 12px; }

    code.prereq-cmd {
        font-size: 11px;
        font-family: monospace;
        background: #1a1a1a;
        border: 1px solid #2a2a2a;
        color: #888;
        padding: 2px 6px;
        border-radius: 4px;
    }

    .prereq-warn {
        font-size: 12px;
        color: #f59e0b;
        background: #1a1400;
        border: 1px solid #3a2e00;
        border-radius: 6px;
        padding: 8px 12px;
    }

    /* Tool card */
    .tool-card {
        background: #111;
        border: 1px solid #1e1e1e;
        border-radius: 10px;
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 10px;
        transition: border-color 0.2s;
    }

    .tool-card.connected {
        border-color: #1a3a2a;
        background: #0d1712;
    }

    .tool-header {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
    }

    .tool-meta { display: flex; flex-direction: column; gap: 3px; }
    .tool-name { font-size: 14px; font-weight: 500; color: #e0e0e0; }
    .tool-desc { font-size: 12px; color: #444; }

    .badge-wrap { flex-shrink: 0; }

    .badge {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        font-size: 11px;
        padding: 3px 8px;
        border-radius: 4px;
    }

    .badge.green  { background: #0d2a1a; color: #3ecf8e; border: 1px solid #1a4a2a; }
    .badge.yellow { background: #2a2010; color: #f59e0b; border: 1px solid #4a3510; }
    .badge.gray   { background: #181818; color: #444;    border: 1px solid #222; }

    .dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: #3ecf8e;
        flex-shrink: 0;
    }

    .action-btn {
        background: #14142a;
        border: 1px solid #222255;
        color: #7777ee;
        padding: 8px 14px;
        border-radius: 7px;
        font-size: 13px;
        font-weight: 500;
        cursor: pointer;
        text-align: left;
        transition: background 0.15s;
        width: fit-content;
    }

    .action-btn:hover:not(:disabled) { background: #1c1c40; }
    .action-btn:disabled { opacity: 0.3; cursor: not-allowed; }

    /* Log */
    .log {
        background: #080808;
        border: 1px solid #181818;
        border-radius: 6px;
        padding: 10px 12px;
        max-height: 130px;
        overflow-y: auto;
        display: flex;
        flex-direction: column;
        gap: 2px;
    }

    .log-line {
        font-size: 11px;
        color: #444;
        font-family: monospace;
        white-space: pre-wrap;
        word-break: break-all;
        line-height: 1.5;
    }

    /* Spinner */
    .spinner-row {
        display: flex;
        align-items: center;
        gap: 8px;
    }

    .spinner {
        width: 12px;
        height: 12px;
        border: 2px solid #1e1e3a;
        border-top-color: #6366f1;
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
        flex-shrink: 0;
    }

    @keyframes spin { to { transform: rotate(360deg); } }

    .spinner-label { font-size: 12px; color: #444; }

    /* Continue / Refresh */
    .continue-btn {
        background: #14142a;
        border: 1px solid #222255;
        color: #7777ee;
        padding: 12px;
        border-radius: 8px;
        font-size: 14px;
        font-weight: 500;
        cursor: pointer;
        width: 100%;
        transition: all 0.15s;
    }

    .continue-btn:hover:not(:disabled) {
        background: #1c1c40;
        color: #aaaaff;
    }

    .continue-btn:disabled {
        opacity: 0.3;
        cursor: not-allowed;
        color: #444;
        background: #111;
        border-color: #1e1e1e;
    }

    .refresh-btn {
        background: transparent;
        border: none;
        color: #333;
        font-size: 12px;
        cursor: pointer;
        align-self: center;
        padding: 4px 8px;
    }

    .refresh-btn:hover { color: #555; }
</style>