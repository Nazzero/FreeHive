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
        const openclaude = current?.openclaude || {};
        const claudeCode = current?.claude_code || {};
        const gemini = current?.gemini_cli || {};
        const chatgpt = current?.chatgpt_cli || {};
        const selectedTool = current?.selected_tool || null;

        const activeClaudeTools = [];
        if (openclaude.authenticated) activeClaudeTools.push('OpenClaude CLI');
        if (claudeCode.authenticated) activeClaudeTools.push('Claude Code CLI');
        const claudeTier = openclaude.tier || claudeCode.tier || null;
        const claudeInstalled = Boolean(openclaude.installed || claudeCode.installed);
        const claudeLoginTool = (
            selectedTool === 'openclaude' || selectedTool === 'claude_code'
                ? selectedTool
                : openclaude.installed
                ? 'openclaude'
                : claudeCode.installed
                ? 'claude_code'
                : 'openclaude'
        );
        const claudeAccount = (
            openclaude.account_email
            || claudeCode.account_email
            || openclaude.account_label
            || claudeCode.account_label
            || openclaude.account_name
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
                authenticated: activeClaudeTools.length > 0,
                installed: claudeInstalled,
                tier: claudeTier,
                detail: activeClaudeTools.length > 0
                    ? (claudeAccount
                        ? `Connected as ${claudeAccount} via ${activeClaudeTools.join(' + ')}`
                        : `Connected via ${activeClaudeTools.join(' + ')} (account email unavailable token)`)
                    : (claudeInstalled ? 'CLI installed, not authenticated' : 'CLI not installed'),
                logoutTool: 'claude',
                loginTool: claudeLoginTool,
                loginOptions: [
                    { tool: 'openclaude', label: 'OpenClaude', installed: Boolean(openclaude.installed) },
                    { tool: 'claude_code', label: 'Claude Code', installed: Boolean(claudeCode.installed) },
                ],
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
            if (provider.loginTool === 'chatgpt_cli') return 'Install Codex CLI';
            if (provider.loginTool === 'gemini_cli') return 'Install Gemini CLI';
            return 'Install CLI';
        }
        if (provider.loginTool === 'openclaude') return 'Login via OpenClaude';
        if (provider.loginTool === 'claude_code') return 'Login via Claude Code';
        if (provider.loginTool === 'chatgpt_cli') return 'Login via Chatgpt';
        if (provider.loginTool === 'gemini_cli') return 'Login via Gemini';
        return 'Login via Provider';
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

        if (tool === 'claude' || tool === 'openclaude' || tool === 'claude_code') {
            return {
                ...current,
                openclaude: {
                    ...(current.openclaude || {}),
                    authenticated: false,
                    tier: null,
                },
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
        if (provider.id === 'claude') {
            const option = Array.isArray(provider.loginOptions)
                ? provider.loginOptions.find((/** @type {any} */ o) => o.tool === tool)
                : null;
            if (!option?.installed) {
                await handleInstallAndAuth(provider, tool);
                return;
            }
        } else if (!provider.installed) {
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
        <button class="back-btn" on:click={() => dispatch('close')}>← Back</button>
        <h2>Accounts</h2>
        <button class="refresh-btn" on:click={() => fetchStatus()} disabled={loading}>
            {loading ? 'Refreshing...' : 'Refresh'}
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
        <p class="empty">Loading account status...</p>
    {:else}
        <div class="provider-list">
            {#each providers as provider}
                <div class="provider-card {provider.authenticated ? 'connected' : ''}">
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
                    {:else if provider.id === 'claude'}
                        <div class="action-group">
                            {#each provider.loginOptions as option}
                                <button
                                    class="action-btn {option.installed ? '' : 'ghost'}"
                                    on:click={() => handleLogin(provider, option.tool)}
                                    disabled={!!busyLoginTool || !!busyLogoutTool || !!busyInstallTool}
                                >
                                    {#if busyInstallTool === option.tool}
                                        Installing...
                                    {:else if busyLoginTool === option.tool}
                                        Logging in...
                                    {:else if option.installed}
                                        Login via {option.label}
                                    {:else}
                                        Install {option.label}
                                    {/if}
                                </button>
                            {/each}
                        </div>
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
        font-size: 16px;
        font-weight: 600;
        color: var(--text-primary);
    }

    .back-btn {
        background: transparent;
        border: none;
        color: var(--text-secondary);
        font-size: 13px;
        cursor: pointer;
        padding: 4px 8px;
        border-radius: 4px;
    }
    .back-btn:hover { color: var(--text-primary); background: var(--bg-secondary); }

    .refresh-btn {
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        color: var(--text-primary);
        padding: 6px 12px;
        border-radius: 6px;
        font-size: 13px;
        cursor: pointer;
    }

    .refresh-btn:disabled {
        opacity: 0.6;
        cursor: not-allowed;
    }

    .provider-list {
        display: flex;
        flex-direction: column;
        gap: 10px;
    }

    .provider-card {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 14px 16px;
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        border-radius: 10px;
    }

    .provider-card.connected {
        border-color: var(--border-medium);
    }

    .provider-main {
        display: flex;
        flex-direction: column;
        gap: 6px;
        min-width: 0;
    }

    .provider-title {
        display: flex;
        align-items: center;
        gap: 10px;
    }

    h3 {
        font-size: 15px;
        font-weight: 600;
        color: var(--text-primary);
        margin: 0;
    }

    .provider-detail {
        font-size: 12px;
        color: var(--text-secondary);
        margin: 0;
    }

    .badge {
        font-size: 11px;
        padding: 3px 8px;
        border-radius: 999px;
        border: 1px solid var(--border-medium);
        background: var(--bg-tertiary);
        white-space: nowrap;
    }

    .badge.active {
        color: var(--accent-color);
    }

    .badge.inactive {
        color: var(--text-muted);
    }

    .action-btn {
        background: var(--text-primary);
        border: none;
        color: var(--bg-primary);
        padding: 8px 12px;
        border-radius: 6px;
        font-size: 12px;
        font-weight: 500;
        cursor: pointer;
        white-space: nowrap;
    }

    .action-btn:disabled {
        opacity: 0.6;
        cursor: not-allowed;
    }

    .action-btn.danger {
        background: var(--bg-tertiary);
        color: #ef4444;
        border: 1px solid var(--border-medium);
    }

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

    .alert {
        padding: 10px 12px;
        border-radius: 8px;
        font-size: 13px;
        border: 1px solid var(--border-medium);
        background: var(--bg-secondary);
    }

    .alert.error {
        color: #ef4444;
    }

    .alert.success {
        color: var(--accent-color);
    }

    .alert.info {
        color: var(--text-secondary);
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
    }
</style>
