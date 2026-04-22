<script>
    import { createEventDispatcher, onMount } from 'svelte';
    import { installTool, authenticateTool, getSetupStatus, logoutTool } from '$lib/api.js';

    const dispatch = createEventDispatcher();

    /** @type {any} */
    let status = null;
    let loading = true;
    let error = '';
    let success = '';
    let busyLogoutTool = '';
    let busyLoginTool = '';
    let busyInstallTool = '';
    let loginProgress = '';
    let installProgress = '';
    let loginElapsed = 0;
    /** @type {ReturnType<typeof setInterval> | null} */
    let loginTimerInterval = null;
    /** @type {number | null} */
    let lastUpdatedAt = null;

    onMount(fetchStatus);

    /**
     * @param {{ silent?: boolean }} [opts]
     */
    async function fetchStatus(opts = {}) {
        const { silent = false } = opts;
        if (!silent) loading = true;
        error = '';
        try {
            status = await getSetupStatus();
            lastUpdatedAt = Date.now();
        } catch (e) {
            error = 'Failed to load account status.';
        } finally {
            if (!silent) loading = false;
        }
    }

    /**
     * @param {any} current
     * @returns {any[]}
     */
    function toProviders(current) {
        const claudeCode = current?.claude_code || {};
        const gemini = current?.gemini_cli || {};
        const chatgpt = current?.chatgpt_cli || {};

        const claudeInstalled = Boolean(claudeCode.installed);
        const claudeAccount = (
            claudeCode.account_email
            || claudeCode.account_label
            || claudeCode.account_name
            || null
        );
        const chatgptAccount = (
            chatgpt.account_email
            || chatgpt.account_label
            || chatgpt.account_name
            || null
        );
        const geminiAccount = (
            gemini.account_email
            || gemini.account_label
            || gemini.account_name
            || null
        );

        return [
            {
                id: 'claude',
                name: 'Claude',
                authenticated: Boolean(claudeCode.authenticated),
                installed: claudeInstalled,
                tier: claudeCode.tier || null,
                detail: claudeCode.authenticated
                    ? (claudeAccount
                        ? `Connected as ${claudeAccount}`
                        : 'Connected via Claude Code CLI')
                    : (claudeInstalled ? 'CLI installed, not authenticated' : 'CLI not installed — subscription required'),
                logoutTool: 'claude',
                loginTool: 'claude_code',
            },
            {
                id: 'chatgpt',
                name: 'ChatGPT',
                authenticated: Boolean(chatgpt.authenticated),
                installed: Boolean(chatgpt.installed),
                tier: chatgpt.tier || null,
                detail: chatgpt.authenticated
                    ? (chatgptAccount
                        ? `Connected as ${chatgptAccount} via Codex CLI auth`
                        : 'Connected via Codex CLI auth')
                    : (chatgpt.installed ? 'Codex CLI installed, not authenticated' : 'Codex CLI not installed'),
                logoutTool: 'chatgpt_cli',
                loginTool: 'chatgpt_cli',
            },
            {
                id: 'gemini',
                name: 'Gemini',
                authenticated: Boolean(gemini.authenticated),
                installed: Boolean(gemini.installed),
                tier: gemini.tier || null,
                detail: gemini.authenticated
                    ? (geminiAccount
                        ? `Connected as ${geminiAccount} via Gemini CLI auth`
                        : 'Connected via Gemini CLI auth')
                    : (gemini.installed ? 'Gemini CLI installed, not authenticated' : 'Gemini CLI not installed'),
                logoutTool: 'gemini_cli',
                loginTool: 'gemini_cli',
            },
        ];
    }

    /**
     * @param {any} provider
     * @returns {string}
     */
    function statusLabel(provider) {
        if (!provider.authenticated) return 'Disconnected';
        return provider.tier ? `Active · ${provider.tier}` : 'Active';
    }

    /**
     * @param {any} provider
     * @returns {string}
     */
    function loginButtonLabel(provider) {
        if (!provider.installed) {
            if (provider.loginTool === 'claude_code') return 'Install Claude CLI';
            if (provider.loginTool === 'chatgpt_cli') return 'Install Codex CLI';
            if (provider.loginTool === 'gemini_cli') return 'Install Gemini CLI';
            return 'Install CLI';
        }
        if (provider.loginTool === 'claude_code') return 'Login via Claude';
        if (provider.loginTool === 'chatgpt_cli') return 'Login via ChatGPT';
        if (provider.loginTool === 'gemini_cli') return 'Login via Gemini';
        return 'Login';
    }

    /**
     * @param {string} id
     * @returns {string}
     */
    function providerIcon(id) {
        if (id === 'claude') return '/logos/claude.png';
        if (id === 'chatgpt') return '/logos/chatgpt.png';
        if (id === 'gemini') return '/logos/gemini.png';
        return '';
    }

    /**
     * @param {any} value
     * @returns {any}
     */
    function clone(value) {
        return JSON.parse(JSON.stringify(value));
    }

    /**
     * @param {any} current
     * @param {string} tool
     * @returns {any}
     */
    function optimisticLogout(current, tool) {
        if (!current) return current;

        if (tool === 'claude' || tool === 'claude_code') {
            return {
                ...current,
                claude_code: {
                    ...(current.claude_code || {}),
                    authenticated: false,
                    tier: null,
                },
            };
        }

        if (tool === 'chatgpt' || tool === 'chatgpt_cli') {
            return {
                ...current,
                chatgpt_cli: {
                    ...(current.chatgpt_cli || {}),
                    authenticated: false,
                    tier: null,
                },
            };
        }

        if (tool === 'gemini' || tool === 'gemini_cli') {
            return {
                ...current,
                gemini_cli: {
                    ...(current.gemini_cli || {}),
                    authenticated: false,
                },
            };
        }

        return current;
    }

    /**
     * @param {number | null} ts
     * @returns {string}
     */
    function formatLastUpdated(ts) {
        if (!ts) return 'Never';
        const date = new Date(ts);
        return date.toLocaleString([], {
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
        });
    }

    /**
     * @param {any} provider
     */
    async function handleLogout(provider) {
        busyLogoutTool = provider.logoutTool;
        error = '';
        success = '';
        const previousStatus = status ? clone(status) : null;
        status = optimisticLogout(status, provider.logoutTool);
        lastUpdatedAt = Date.now();
        try {
            const result = await logoutTool(provider.logoutTool);
            if (!result?.success) {
                throw new Error(result?.error || 'Logout failed');
            }
            success = `${provider.name} logged out.`;
            await fetchStatus({ silent: true });
            dispatch('modelsChanged');
        } catch (e) {
            if (previousStatus) {
                status = previousStatus;
            }
            error = /** @type {any} */ (e)?.message || `Failed to log out ${provider.name}.`;
        } finally {
            busyLogoutTool = '';
        }
    }

    /**
     * Install a CLI tool inline, then auto-start authentication.
     * @param {any} provider
     * @param {string} tool
     */
    async function handleInstallAndAuth(provider, tool) {
        busyInstallTool = tool;
        installProgress = `Installing ${tool}...`;
        error = '';
        success = '';

        try {
            await installTool(tool, (event) => {
                if (event?.msg && event?.status !== 'waiting') {
                    installProgress = event.msg;
                } else if (event?.status === 'starting') {
                    installProgress = event.msg || 'Starting install...';
                }
            });

            installProgress = 'Installed. Starting authentication...';
            await fetchStatus({ silent: true });

            // Auto-start auth after successful install
            busyInstallTool = '';
            installProgress = '';
            await handleLogin(provider, tool);
        } catch (e) {
            error = /** @type {any} */ (e)?.message || `Failed to install CLI for ${provider.name}.`;
        } finally {
            busyInstallTool = '';
            installProgress = '';
        }
    }

    /**
     * @param {any} provider
     */
    async function handleLogin(provider, tool = provider.loginTool) {
        if (!provider.installed) {
            await handleInstallAndAuth(provider, tool);
            return;
        }

        busyLoginTool = tool;
        loginProgress = 'Launching authentication flow...';
        loginElapsed = 0;
        loginTimerInterval = setInterval(() => { loginElapsed += 1; }, 1000);
        error = '';
        success = '';

        try {
            await authenticateTool(tool, (event) => {
                if (event?.status === 'browser_opened') {
                    loginProgress = event.msg || 'Browser opened. Complete login there.';
                } else if (event?.status === 'waiting') {
                    loginProgress = event.msg || 'Waiting for login completion...';
                } else if (event?.status === 'output' && event?.msg) {
                    loginProgress = event.msg;
                } else if (event?.status === 'starting') {
                    loginProgress = event.msg || 'Starting authentication...';
                }
            });

            await fetchStatus({ silent: true });
            success = `${provider.name} login complete.`;
            dispatch('modelsChanged');
        } catch (e) {
            error = /** @type {any} */ (e)?.message || `Failed to log in to ${provider.name}.`;
        } finally {
            busyLoginTool = '';
            loginProgress = '';
            if (loginTimerInterval) { clearInterval(loginTimerInterval); loginTimerInterval = null; }
            loginElapsed = 0;
        }
    }

    $: providers = toProviders(status);
    $: lastUpdatedLabel = formatLastUpdated(lastUpdatedAt);
</script>

<div class="panel">
    <div class="panel-header">
        <button class="back-btn" on:click={() => dispatch('close')}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5"/><path d="M12 19l-7-7 7-7"/></svg>
            Back
        </button>
        <h2>Accounts</h2>
        <button class="refresh-btn" on:click={() => fetchStatus()} disabled={loading} class:spinning={loading}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"/></svg>
            {loading ? '' : 'Refresh'}
        </button>
    </div>

    <p class="panel-subtitle">Manage CLI-linked provider accounts.</p>
    <p class="updated-at">Last updated: {lastUpdatedLabel}</p>

    {#if error}
        <div class="alert error">{error}</div>
    {/if}

    {#if success}
        <div class="alert success">{success}</div>
    {/if}

    {#if busyInstallTool}
        <div class="alert info">
            {installProgress || 'Installing CLI...'}
        </div>
    {/if}

    {#if busyLoginTool}
        <div class="alert info">
            {loginProgress || 'Authenticating...'}
            {#if loginElapsed > 5}<span class="elapsed"> ({loginElapsed}s)</span>{/if}
        </div>
    {/if}

    {#if loading}
        <div class="loading-state">
            <span></span><span></span><span></span>
        </div>
    {:else}
        <div class="provider-list">
            {#each providers as provider}
                <div class="provider-card {provider.authenticated ? 'connected' : ''}">
                    <div class="provider-icon">
                        <img src={providerIcon(provider.id)} alt="{provider.name} logo" class:chatgpt-logo={provider.id === 'chatgpt'} />
                    </div>
                    <div class="provider-main">
                        <div class="provider-title">
                            <h3>{provider.name}</h3>
                            <span class="badge {provider.authenticated ? 'active' : 'inactive'}">
                                {statusLabel(provider)}
                            </span>
                        </div>
                        <p class="provider-detail">{provider.detail}</p>
                    </div>

                    {#if provider.authenticated}
                        <button
                            class="action-btn danger"
                            on:click={() => handleLogout(provider)}
                            disabled={busyLogoutTool === provider.logoutTool || !!busyLoginTool}
                        >
                            {busyLogoutTool === provider.logoutTool ? 'Logging out...' : 'Logout'}
                        </button>
                    {:else}
                        <button class="action-btn" on:click={() => handleLogin(provider)} disabled={!!busyLoginTool || !!busyLogoutTool || !!busyInstallTool}>
                            {#if busyInstallTool === provider.loginTool}
                                Installing...
                            {:else if busyLoginTool === provider.loginTool}
                                Logging in...
                            {:else}
                                {loginButtonLabel(provider)}
                            {/if}
                        </button>
                    {/if}
                </div>
            {/each}
        </div>

        <p class="hint">Logins run directly from this page. If a CLI is missing, the button opens Setup.</p>
    {/if}
</div>

<style>
    .panel {
        display: flex;
        flex-direction: column;
        gap: 16px;
        padding: 24px;
        height: 100%;
        overflow-y: auto;
    }

    .panel-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
    }

    .panel-subtitle {
        font-size: 13px;
        color: var(--text-secondary);
        margin-top: -6px;
    }

    .updated-at {
        font-size: 12px;
        color: var(--text-muted);
        margin-top: -12px;
    }

    h2 {
        font-size: 17px;
        font-weight: 600;
        color: var(--text-primary);
    }

    .back-btn {
        display: flex;
        align-items: center;
        gap: 6px;
        background: transparent;
        border: none;
        color: var(--text-secondary);
        font-size: 13px;
        cursor: pointer;
        padding: 6px 10px;
        border-radius: 6px;
        transition: all 0.15s;
    }
    .back-btn:hover { color: var(--text-primary); background: var(--bg-tertiary); }

    .refresh-btn {
        display: flex;
        align-items: center;
        gap: 6px;
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        color: var(--text-primary);
        padding: 6px 12px;
        border-radius: 8px;
        font-size: 13px;
        cursor: pointer;
        transition: all 0.15s;
    }
    .refresh-btn:hover { background: var(--bg-tertiary); }
    .refresh-btn:disabled { opacity: 0.6; cursor: not-allowed; }
    .refresh-btn.spinning svg {
        animation: spin 1s linear infinite;
    }
    @keyframes spin {
        from { transform: rotate(0deg); }
        to { transform: rotate(360deg); }
    }

    /* Loading state */
    .loading-state {
        display: flex;
        gap: 7px;
        align-items: center;
        justify-content: center;
        padding: 32px 0;
    }
    .loading-state span {
        width: 8px;
        height: 8px;
        background: var(--accent-color);
        border-radius: 50%;
        opacity: 0.4;
        animation: dotBounce 1.4s ease-in-out infinite;
    }
    .loading-state span:nth-child(2) { animation-delay: 0.2s; }
    .loading-state span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes dotBounce {
        0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
        30% { transform: translateY(-6px); opacity: 1; }
    }

    /* Provider cards */
    .provider-list {
        display: flex;
        flex-direction: column;
        gap: 10px;
    }

    .provider-card {
        display: flex;
        align-items: center;
        gap: 14px;
        padding: 16px 18px;
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        border-radius: 12px;
        transition: transform 0.15s, box-shadow 0.15s;
    }
    .provider-card:hover {
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    }

    .provider-card.connected {
        border-left: 3px solid var(--accent-color);
    }

    .provider-icon {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 36px;
        height: 36px;
        border-radius: 8px;
        background: var(--bg-tertiary);
        flex-shrink: 0;
        overflow: hidden;
    }
    .provider-icon img {
        width: 22px;
        height: 22px;
        object-fit: contain;
    }
    .provider-icon img.chatgpt-logo {
        width: 36px;
        height: 36px;
    }

    .provider-main {
        display: flex;
        flex-direction: column;
        gap: 4px;
        min-width: 0;
        flex: 1;
    }

    .provider-title {
        display: flex;
        align-items: center;
        gap: 10px;
    }

    h3 {
        font-size: 14px;
        font-weight: 600;
        color: var(--text-primary);
        margin: 0;
    }

    .provider-detail {
        font-size: 12px;
        color: var(--text-muted);
        margin: 0;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }

    .badge {
        font-size: 11px;
        padding: 3px 9px;
        border-radius: 999px;
        white-space: nowrap;
        font-weight: 500;
    }

    .badge.active {
        color: var(--accent-color);
        background: var(--accent-muted);
        border: 1px solid var(--accent-color);
    }

    .badge.inactive {
        color: var(--text-muted);
        background: var(--bg-tertiary);
        border: 1px solid var(--border-medium);
    }

    /* Action buttons */
    .action-btn {
        background: var(--accent-color);
        border: none;
        color: var(--bg-primary);
        padding: 8px 14px;
        border-radius: 8px;
        font-size: 12px;
        font-weight: 600;
        cursor: pointer;
        white-space: nowrap;
        transition: opacity 0.15s, transform 0.15s;
    }
    .action-btn:hover:not(:disabled) { opacity: 0.85; transform: scale(1.02); }
    .action-btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }

    .action-btn.danger {
        background: var(--bg-tertiary);
        color: #ef4444;
        border: 1px solid var(--border-medium);
    }
    .action-btn.danger:hover:not(:disabled) { background: rgba(239, 68, 68, 0.1); border-color: #ef4444; }

    .action-btn.ghost {
        background: var(--bg-secondary);
        color: var(--text-secondary);
        border: 1px solid var(--border-medium);
    }

    .action-group {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 8px;
        flex-wrap: wrap;
    }

    /* Alerts */
    .alert {
        padding: 10px 14px;
        border-radius: 8px;
        font-size: 13px;
        border: 1px solid var(--border-medium);
        background: var(--bg-secondary);
        border-left: 3px solid var(--border-medium);
    }

    .alert.error {
        color: #ef4444;
        border-left-color: #ef4444;
        background: rgba(239, 68, 68, 0.05);
    }

    .alert.success {
        color: var(--accent-color);
        border-left-color: var(--accent-color);
        background: var(--accent-muted);
    }

    .alert.info {
        color: var(--text-secondary);
        border-left-color: #3b82f6;
        background: rgba(59, 130, 246, 0.05);
    }

    .elapsed {
        color: var(--text-muted);
        font-size: 12px;
    }

    .empty {
        font-size: 14px;
        color: var(--text-muted);
        text-align: center;
        margin-top: 24px;
    }

    .hint {
        font-size: 12px;
        color: var(--text-muted);
        margin-top: 4px;
    }
</style>
