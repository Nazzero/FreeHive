<script>
    import { tick, onMount } from 'svelte';
    import { messages, isLoading, selectedModel, selectedProvider, availableModels, addMessage } from '$lib/store.js';
    import {
        sendChat,
        getSetupStatus,
        getAvailableModels,
        listChatSessions,
        getChatSessionMessages,
        setActiveSession,
        clearActiveSession,
        deleteChatSession,
    } from '$lib/api.js';
    import AccountPanel from '$lib/AccountPanel.svelte';
    import SetupScreen from '$lib/SetupScreen.svelte';
    import SettingsPage from '$lib/SettingsPage.svelte';
    import { marked } from 'marked';

    let input = '';
    /** @type {HTMLElement} */
    let chatContainer;
    let activeView = 'chat';
    let setupReady = false;
    let checkingSetup = true;

    // TODO (v2): Arena integration postponed.
    // Arena state
    /*
    let arenaExpanded = false;
    let arenaStatus = { running: true, bridge_active: false };
    let arenaModels = [];
    let arenaLoading = false;
    let arenaError = '';
    */
    let showPrivacyWarning = false;
    /** @type {any[]} */
    let chatSessions = [];
    let sessionsLoading = false;
    /** @type {string | null} */
    let activeChatSessionId = null;

    /** @type {string | null} */
    let expandedProvider = null;

    onMount(async () => {
        try {
            const status = await getSetupStatus();
            setupReady = status.ready;
            if (setupReady) {
                // TODO (v2): Arena integration postponed.
                // await refreshArenaStatus({ loadModels: false });
                getAvailableModels().then(data => {
                    if (data && Object.keys(data).length > 0) {
                        availableModels.set(data);
                        const provider = $selectedProvider;
                        const providerData = data[provider];
                        if (providerData?.models?.length > 0) {
                            const ids = providerData.models.map((/** @type {any} */ m) => m.id);
                            if (!ids.includes($selectedModel)) {
                                selectedModel.set(ids[0]);
                            }
                        }
                    }
                }).catch(() => {});
                await refreshSavedSessions();
                if (!activeChatSessionId && chatSessions.length > 0) {
                    await openSavedSession(chatSessions[0]);
                }
            }
        } catch (e) {
            setupReady = false;
        } finally {
            checkingSetup = false;
        }
    });

    async function onSetupReady() {
        setupReady = true;
        // TODO (v2): Arena integration postponed.
        /*
        try {
            await refreshArenaStatus({ loadModels: false });
        } catch (e) {}
        */
        await refreshSavedSessions();
        if (!activeChatSessionId && chatSessions.length > 0) {
            await openSavedSession(chatSessions[0]);
        }
    }

    /**
     * @param {string} path
     * @returns {string | null}
     */
    function formatTransportLabel(path) {
        const raw = String(path || '').trim();
        if (!raw) return null;
        if (raw === 'direct_api') return 'direct_api';
        if (raw === 'codex_cli_fallback') return 'codex_cli_fallback';
        return raw;
    }

    async function refreshSavedSessions() {
        sessionsLoading = true;
        try {
            chatSessions = await listChatSessions();
        } finally {
            sessionsLoading = false;
        }
    }

    /**
     * @param {string | null} isoOrText
     * @returns {string}
     */
    function formatMessageTime(isoOrText) {
        if (!isoOrText) return new Date().toLocaleTimeString();
        const parsed = new Date(isoOrText);
        if (Number.isNaN(parsed.getTime())) return String(isoOrText);
        return parsed.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    /**
     * @param {string} role
     * @returns {string}
     */
    function toUiRole(role) {
        return role === 'user' ? 'user' : 'assistant';
    }

    /**
     * @param {any} row
     * @returns {string}
     */
    function toUiContent(row) {
        const content = String(row?.content ?? '');
        const role = String(row?.role ?? '');
        if (role === 'tool') return `[tool]\n${content}`;
        if (role === 'system') return `[system]\n${content}`;
        return content;
    }

    /**
     * @param {any} session
     */
    async function openSavedSession(session) {
        if (!session?.id) return;
        const rows = await getChatSessionMessages(session.id);
        activeChatSessionId = session.id;
        setActiveSession(session.id, session.model);
        $selectedModel = session.model || $selectedModel;
        $messages = rows.map((row) => ({
            id: row.id || `${session.id}-${Math.random().toString(36).slice(2)}`,
            role: toUiRole(row.role),
            content: toUiContent(row),
            model: session.model || null,
            transport: null,
            timestamp: formatMessageTime(row.created_at),
        }));
        await tick();
        scrollToBottom();
    }

    /**
     * @param {string} id
     */
    async function handleDeleteSession(id) {
        if (confirm('Are you sure you want to delete this chat session?')) {
            try {
                await deleteChatSession(id);
                await refreshSavedSessions();
                if (activeChatSessionId === id) {
                    await handleNewChat();
                }
            } catch (err) {
                console.error('Failed to delete session:', err);
                alert('Failed to delete session.');
            }
        }
    }

    async function handleNewChat() {
        activeChatSessionId = null;
        clearActiveSession();
        input = '';
        $messages = [];
        activeView = 'chat';
    }

    async function handleSubmit() {
        if (!input.trim() || $isLoading) return;

        const userMessage = input.trim();
        input = '';

        addMessage('user', userMessage);
        $isLoading = true;

        await tick();
        scrollToBottom();

        try {
            const result = await sendChat($selectedModel, userMessage, activeChatSessionId);
            if (result?.session_id) {
                activeChatSessionId = result.session_id;
                setActiveSession(result.session_id, $selectedModel);
            }
            const response = typeof result === 'string' ? result : (result?.response ?? '');
            const transport = formatTransportLabel(result?.transport?.path);
            addMessage('assistant', response, $selectedModel, transport);
            await refreshSavedSessions();
        } catch (err) {
            const msg = String(/** @type {any} */ (err)?.message || 'Unknown error');
            // TODO (v2): Arena integration postponed.
            /*
            if ($selectedModel.startsWith('arena/') && /model not found|unavailable right now/i.test(msg)) {
                arenaError = 'Selected Arena model is unavailable right now. Click "Refresh Models" and choose another model.';
            }
            */
            addMessage('assistant', `Error: ${msg}`, $selectedModel);
        } finally {
            $isLoading = false;
            await tick();
            scrollToBottom();
        }
    }

    function scrollToBottom() {
        if (chatContainer) {
            chatContainer.scrollTop = chatContainer.scrollHeight;
        }
    }

    /**
     * @param {KeyboardEvent} e
     */
    function handleKeydown(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSubmit();
        }
    }
/* TODO (v2): Arena integration postponed.

    async function toggleArena() {
        arenaExpanded = !arenaExpanded;
        if (arenaExpanded && arenaModels.length === 0) {
            await handleRefreshArenaModels();
        }
    }
*/
/* TODO (v2): Arena integration postponed.

    async function refreshArenaStatus({ loadModels = true } = {}) {
        try {
            arenaStatus = await getArenaStatus();
            if (loadModels && arenaStatus.bridge_active) {
                const data = await getArenaModels();
                const raw = Array.isArray(data.models) ? data.models : [];
                arenaModels = raw
                    .map((m) => {
                        const id = String(m || '').trim();
                        return id.startsWith('arena/') ? id : `arena/${id}`;
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
/* TODO (v2): Arena integration postponed.

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
*/
/* TODO (v2): Arena integration postponed.

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
*/

    /**
     * @param {string} provider
     * @param {string} modelId
     */
    async function selectModel(provider, modelId) {
        const previousModel = $selectedModel;
        const isModelSwitch = previousModel !== modelId;
        const hasActiveChat = $messages.length > 0 || !!activeChatSessionId;

        if (isModelSwitch && hasActiveChat) {
            await handleNewChat();
        }

        $selectedModel = modelId;
        showPrivacyWarning = false;

        // TODO (v2): Arena integration postponed.
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
        */
    }

    function dismissPrivacyWarning() {
        showPrivacyWarning = false;
    }

    // TODO (v2): Arena integration postponed.
    /*
    $: isArenaModel = $selectedModel.startsWith('arena/');
    $: arenaModelDisplay = isArenaModel ? $selectedModel.replace('arena/', '') : '';
    */
</script>

{#if checkingSetup}
    <div class="splash">
        <p>Starting FreeHive...</p>
    </div>
{:else if !setupReady}
    <SetupScreen on:ready={onSetupReady} />
{:else}

<!-- TODO (v2): Arena integration postponed.
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
-->

<div class="app">
    <aside class="sidebar">
        <div class="sidebar-top">
            <div class="sidebar-header">
                <h1>FreeHive</h1>
            </div>

            <button class="new-chat-btn" on:click={handleNewChat}>
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>
                New Chat
            </button>

            <div class="providers-wrapper">
                <p class="section-label">Providers</p>
                <div class="providers-list">
                    {#each Object.entries($availableModels) as [provider, data]}
                        {#if data.models && data.models.length > 0}
                            <div class="provider-group">
                                <button class="provider-btn {expandedProvider === provider ? 'open' : ''}" on:click={() => expandedProvider = expandedProvider === provider ? null : provider}>
                                    <div style="display: flex; align-items: center; gap: 6px;">
                                        <span class="status-dot" style="width: 6px; height: 6px; border-radius: 50%; background-color: var(--accent-color);"></span>
                                        <span style="text-transform: capitalize;">{provider}</span>
                                    </div>
                                    <svg class="chevron {expandedProvider === provider ? 'open' : ''}" viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><polyline points="6 9 12 15 18 9"></polyline></svg>
                                </button>
                                {#if expandedProvider === provider}
                                    <div class="provider-models">
                                        {#each data.models as m}
                                            <button class="model-btn {$selectedModel === m.id ? 'active' : ''}" on:click={() => selectModel(provider, m.id)}>
                                                {m.display_name}
                                                {#if m.note}<span class="model-note">{m.note}</span>{/if}
                                            </button>
                                        {/each}
                                    </div>
                                {/if}
                            </div>
                        {/if}
                    {/each}

                    <!-- TODO (v2): Arena integration postponed.
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
                    -->
                </div>
            </div>

            <div class="saved-chats-wrapper">
                <p class="section-label">Recent Chats</p>
                <div class="saved-chats">
                    {#if sessionsLoading}
                        <p class="saved-empty">Loading...</p>
                    {:else if chatSessions.length === 0}
                        <p class="saved-empty">No chats yet.</p>
                    {:else}
                        {#each chatSessions as session}
                            <div class="saved-chat-item {activeChatSessionId === session.id && activeView === 'chat' ? 'active' : ''}">
                                <button
                                    class="saved-chat-btn"
                                    on:click={() => { activeView = 'chat'; openSavedSession(session); }}
                                    title={session.title || session.id}>
                                    <span class="saved-chat-title">{session.title || 'Untitled chat'}</span>
                                </button>
                                <button
                                    class="delete-chat-btn"
                                    on:click|stopPropagation={() => handleDeleteSession(session.id)}
                                    title="Delete Chat">
                                    <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                                </button>
                            </div>
                        {/each}
                    {/if}
                </div>
            </div>
        </div>

        <div class="sidebar-bottom">
            <button class="icon-btn {activeView === 'accounts' ? 'active' : ''}" on:click={() => activeView = 'accounts'} title="Accounts">
                <svg viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" stroke-width="2" fill="none"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>
                <span>Accounts</span>
            </button>
            <button class="icon-btn {activeView === 'settings' ? 'active' : ''}" on:click={() => activeView = 'settings'} title="Settings">
                <svg viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" stroke-width="2" fill="none"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
                <span>Settings</span>
            </button>
        </div>
    </aside>

    <main class="chat-area">
        {#if activeView === 'chat'}
            <div class="chat-header">
                <div class="active-model-display">
                    {$selectedModel}
                </div>
            </div>

            <div class="messages" bind:this={chatContainer}>
                {#if $messages.length === 0}
                    <div class="empty-state">
                        <div class="empty-logo">⬡</div>
                        <h2>How can I help you today?</h2>
                    </div>
                {/if}

                {#each $messages as msg (msg.id)}
                    <div class="message-band {msg.role}">
                        <div class="message-content-wrapper">
                            <div class="role-indicator">{msg.role === 'user' ? 'You' : 'FreeHive'}</div>
                            <div class="bubble">
                                {#if msg.role === 'assistant'}
                                    {@html marked(msg.content)}
                                {:else}
                                    {msg.content}
                                {/if}
                            </div>
                        </div>
                    </div>
                {/each}

                {#if $isLoading}
                    <div class="message-band assistant">
                        <div class="message-content-wrapper">
                            <div class="role-indicator">FreeHive</div>
                            <div class="bubble loading">
                                <span></span><span></span><span></span>
                            </div>
                        </div>
                    </div>
                {/if}
            </div>

            <div class="input-area-wrapper">
                <div class="input-area">
                    <textarea
                        bind:value={input}
                        on:keydown={handleKeydown}
                        placeholder="Message {$selectedModel}..."
                        rows="1"
                        disabled={$isLoading}
                    ></textarea>
                    <button
                        class="send-btn"
                        aria-label="Send message"
                        on:click={handleSubmit}
                        disabled={$isLoading || !input.trim()}
                    >
                        <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>
                    </button>
                </div>
                <div class="footer-note">FreeHive can make mistakes. Check important info.</div>
            </div>

        {:else if activeView === 'accounts'}
            <AccountPanel on:openSettings={() => activeView = 'settings'} />
        {:else if activeView === 'settings'}
            <SettingsPage />
        {/if}
    </main>
</div>
{/if}

<style>
    * { box-sizing: border-box; margin: 0; padding: 0; }

    .splash {
        display: flex;
        align-items: center;
        justify-content: center;
        height: 100vh;
        background: var(--bg-primary);
        color: var(--text-primary);
        font-size: 13px;
    }

    .app {
        display: flex;
        height: 100vh;
        background: var(--bg-primary);
        color: var(--text-primary);
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }

    /* Sidebar */
    .sidebar {
        width: 260px;
        background: var(--bg-secondary);
        border-right: 1px solid var(--border-light);
        display: flex;
        flex-direction: column;
        justify-content: space-between;
        flex-shrink: 0;
    }

    .sidebar-top {
        display: flex;
        flex-direction: column;
        padding: 16px;
        gap: 16px;
        overflow-y: hidden;
    }

    .sidebar-header h1 {
        font-size: 16px;
        font-weight: 600;
        color: var(--text-primary);
        padding-left: 8px;
    }

    .new-chat-btn {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 10px 14px;
        background: var(--bg-primary);
        border: 1px solid var(--border-medium);
        border-radius: 8px;
        color: var(--text-primary);
        font-size: 13px;
        font-weight: 500;
        cursor: pointer;
        transition: background 0.2s, border-color 0.2s;
    }
    .new-chat-btn:hover { background: var(--bg-tertiary); border-color: var(--text-muted); }

    .saved-chats-wrapper {
        display: flex;
        flex-direction: column;
        gap: 8px;
        overflow-y: auto;
    }

    .section-label {
        font-size: 11px;
        font-weight: 600;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 0.5px;
        padding-left: 8px;
        margin-top: 8px;
    }

    .saved-chats {
        display: flex;
        flex-direction: column;
        gap: 2px;
    }

    .saved-chat-item {
        display: flex;
        align-items: center;
        border-radius: 6px;
        transition: background 0.15s;
    }
    .saved-chat-item:hover { background: var(--bg-tertiary); }
    .saved-chat-item.active { background: var(--bg-tertiary); }

    .saved-chat-btn {
        flex: 1;
        text-align: left;
        padding: 8px 8px 8px 12px;
        background: transparent;
        border: none;
        color: var(--text-secondary);
        font-size: 13px;
        cursor: pointer;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        transition: color 0.15s;
    }
    .saved-chat-item:hover .saved-chat-btn { color: var(--text-primary); }
    .saved-chat-item.active .saved-chat-btn { color: var(--text-primary); font-weight: 500; }

    .delete-chat-btn {
        background: transparent;
        border: none;
        color: var(--text-muted);
        cursor: pointer;
        padding: 8px;
        display: flex;
        align-items: center;
        justify-content: center;
        opacity: 0;
        transition: opacity 0.15s, color 0.15s;
    }
    .saved-chat-item:hover .delete-chat-btn { opacity: 1; }
    .delete-chat-btn:hover { color: #ef4444; }
    
    .saved-empty {
        font-size: 12px;
        color: var(--text-muted);
        padding: 8px 12px;
    }

    .sidebar-bottom {
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 4px;
        border-top: 1px solid var(--border-light);
    }

    .icon-btn {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 10px 12px;
        background: transparent;
        border: none;
        border-radius: 6px;
        color: var(--text-secondary);
        font-size: 13px;
        font-weight: 500;
        cursor: pointer;
        transition: background 0.15s, color 0.15s;
    }
    .icon-btn:hover, .icon-btn.active {
        background: var(--bg-tertiary);
        color: var(--text-primary);
    }

    /* Main Chat Area */
    .chat-area {
        flex: 1;
        display: flex;
        flex-direction: column;
        overflow: hidden;
        position: relative;
    }

    .providers-wrapper {
        display: flex;
        flex-direction: column;
        gap: 8px;
        margin-bottom: 8px;
    }
    
    .providers-list {
        display: flex;
        flex-direction: column;
        gap: 2px;
    }
    
    .provider-group {
        display: flex;
        flex-direction: column;
    }

    .provider-btn {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 12px;
        background: transparent;
        border: none;
        border-radius: 6px;
        color: var(--text-primary);
        font-size: 13px;
        font-weight: 500;
        cursor: pointer;
        transition: background 0.15s;
    }
    .provider-btn:hover { background: var(--bg-tertiary); }
    .provider-btn .chevron { transition: transform 0.2s; }
    .provider-btn .chevron.open { transform: rotate(180deg); }

    .provider-models {
        display: flex;
        flex-direction: column;
        gap: 2px;
        padding-left: 12px;
        margin-top: 2px;
        margin-bottom: 4px;
        border-left: 1px solid var(--border-light);
        margin-left: 12px;
    }

    .model-btn {
        text-align: left;
        padding: 6px 12px;
        background: transparent;
        border: none;
        border-radius: 6px;
        color: var(--text-secondary);
        font-size: 12px;
        cursor: pointer;
        display: flex;
        justify-content: space-between;
        align-items: center;
        transition: background 0.15s, color 0.15s;
    }
    .model-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); }
    .model-btn.active { background: var(--accent-muted); color: var(--accent-color); font-weight: 500; }
    
    .model-note {
        font-size: 10px;
        color: var(--text-muted);
    }
    
    .arena-controls { padding: 4px 12px; display: flex; flex-direction: column; gap: 4px;}
    .arena-action-btn { align-self: flex-start; font-size: 11px; padding: 4px 8px; border-radius: 4px; border: 1px solid var(--border-medium); background: var(--bg-tertiary); color: var(--text-secondary); cursor: pointer;}
    .arena-error { font-size: 11px; color: #ef4444;}

    .chat-header {
        padding: 16px 24px;
        display: flex;
        align-items: center;
        z-index: 100;
    }

    .active-model-display {
        font-size: 15px;
        font-weight: 500;
        color: var(--text-secondary);
    }

    /* Messages */
    .messages {
        flex: 1;
        overflow-y: auto;
        padding: 0 0 24px;
        display: flex;
        flex-direction: column;
        scroll-behavior: smooth;
    }
    .messages::-webkit-scrollbar { width: 6px; }
    .messages::-webkit-scrollbar-thumb { background: var(--border-medium); border-radius: 4px; }

    .empty-state {
        margin: auto;
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 16px;
        color: var(--text-primary);
    }
    .empty-logo {
        font-size: 32px;
        color: var(--text-muted);
    }
    .empty-state h2 { font-weight: 500; font-size: 20px; }

    .message-band {
        width: 100%;
        padding: 24px 0;
    }

    .message-content-wrapper {
        max-width: 800px;
        margin: 0 auto;
        padding: 0 24px;
        display: flex;
        flex-direction: column;
        gap: 6px;
    }

    .role-indicator {
        font-size: 13px;
        font-weight: 600;
        color: var(--text-primary);
    }

    .bubble {
        color: var(--text-primary);
        font-size: 15px;
        line-height: 1.6;
        background: transparent;
        border: none;
    }

    .bubble :global(p) { margin-bottom: 1em; }
    .bubble :global(p:last-child) { margin-bottom: 0; }
    .bubble :global(pre) {
        background: var(--bg-secondary);
        border: 1px solid var(--border-light);
        border-radius: 8px;
        padding: 16px;
        overflow-x: auto;
        margin: 12px 0;
    }
    .bubble :global(code) {
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        font-size: 13.5px;
        background: var(--bg-secondary);
        padding: 2px 6px;
        border-radius: 4px;
    }
    .bubble :global(pre code) { background: none; padding: 0; }
    .bubble :global(ul), .bubble :global(ol) { padding-left: 24px; margin: 12px 0; }
    .bubble :global(li) { margin-bottom: 4px; }
    .bubble :global(blockquote) {
        border-left: 3px solid var(--border-medium);
        padding-left: 16px;
        color: var(--text-secondary);
        margin: 12px 0;
    }

    .bubble.loading {
        display: flex;
        gap: 6px;
        align-items: center;
        padding: 8px 0;
    }
    .bubble.loading span {
        width: 6px;
        height: 6px;
        background: var(--text-muted);
        border-radius: 50%;
        animation: pulse 1.2s infinite;
    }
    .bubble.loading span:nth-child(2) { animation-delay: 0.2s; }
    .bubble.loading span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes pulse {
        0%, 100% { opacity: 0.3; transform: scale(0.8); }
        50% { opacity: 1; transform: scale(1); }
    }

    /* Input Area */
    .input-area-wrapper {
        padding: 16px 24px;
        background: linear-gradient(to top, var(--bg-primary) 80%, transparent);
        max-width: 800px;
        margin: 0 auto;
        width: 100%;
        display: flex;
        flex-direction: column;
        align-items: center;
    }

    .input-area {
        width: 100%;
        display: flex;
        align-items: flex-end;
        gap: 12px;
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        border-radius: 16px;
        padding: 12px 16px;
        box-shadow: var(--shadow-subtle);
        transition: border-color 0.2s, box-shadow 0.2s;
    }
    .input-area:focus-within {
        border-color: var(--text-muted);
        box-shadow: 0 4px 16px rgba(0,0,0,0.1);
    }

    textarea {
        flex: 1;
        background: transparent;
        border: none;
        color: var(--text-primary);
        font-size: 15px;
        resize: none;
        outline: none;
        font-family: inherit;
        line-height: 1.5;
        max-height: 200px;
        padding: 2px 0;
    }
    textarea::placeholder { color: var(--text-muted); }
    textarea:disabled { opacity: 0.5; }

    .send-btn {
        display: flex;
        align-items: center;
        justify-content: center;
        background: var(--text-primary);
        color: var(--bg-primary);
        border: none;
        width: 32px;
        height: 32px;
        border-radius: 8px;
        cursor: pointer;
        transition: opacity 0.2s;
        flex-shrink: 0;
    }
    .send-btn:hover:not(:disabled) { opacity: 0.8; }
    .send-btn:disabled { background: var(--border-medium); color: var(--text-muted); cursor: not-allowed; }

    .footer-note {
        font-size: 11px;
        color: var(--text-muted);
        margin-top: 12px;
    }

    /* Modal */
    .modal-overlay {
        position: fixed;
        inset: 0;
        background: rgba(0,0,0,0.5);
        backdrop-filter: blur(2px);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 1000;
    }
    .modal-box {
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        border-radius: 12px;
        padding: 24px;
        max-width: 400px;
        width: 90%;
        display: flex;
        flex-direction: column;
        gap: 16px;
        box-shadow: 0 8px 32px rgba(0,0,0,0.2);
    }
    .modal-icon { font-size: 24px; text-align: center; }
    
    
    .modal-btn { background: var(--text-primary); color: var(--bg-primary); border: none; padding: 10px; border-radius: 8px; font-size: 14px; font-weight: 500; cursor: pointer;}
</style>
