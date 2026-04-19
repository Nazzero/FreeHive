<script>
    import { tick, onMount } from 'svelte';
    import { messages, isLoading, selectedModel, selectedProvider, selectedModelDisplay, availableModels, groupedModels, addMessage, renderAssistantHtml, thinkingEffort, modelSupportsThinking } from '$lib/store.js';
    import {
        sendChat,
        getSetupStatus,
        getAvailableModels,
        listChatSessions,
        getChatSessionMessages,
        setActiveSession,
        clearActiveSession,
        deleteChatSession,
        getThinkingEffort,
        setThinkingEffort,
    } from '$lib/api.js';
    import AccountPanel from '$lib/AccountPanel.svelte';
    import ArenaPanel from '$lib/ArenaPanel.svelte';
    import CaptchaPopup from '$lib/CaptchaPopup.svelte';
    import SetupScreen from '$lib/SetupScreen.svelte';
    import SettingsPage from '$lib/SettingsPage.svelte';

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
    let expandedFamily = null;

    // ── Arena sidebar search + chip state ──
    let arenaSidebarSearch = '';
    /** @type {string | null} */
    let expandedArenaChip = null;

    const ARENA_CHIP_LOGOS = {
        GPT: '/logos/arena/openai.png',
        Claude: '/logos/arena/claude.png',
        Gemini: '/logos/arena/gemini.png',
        Grok: '/logos/arena/grok.png',
        DeepSeek: '/logos/arena/deepseek.png',
        Qwen: '/logos/arena/qwen.png',
        Mistral: '/logos/arena/mistral.png',
        GLM: '/logos/arena/glm.png',
        Kimi: '/logos/arena/kimi.png',
        MiniMax: '/logos/arena/minimax.png',
    };

    const ARENA_SIDEBAR_ALIASES = {
        google: ['Gemini'], anthropic: ['Claude'], openai: ['GPT'], chatgpt: ['GPT'],
        meta: ['Llama'], alibaba: ['Qwen'], xai: ['Grok'], 'x.ai': ['Grok'],
        zhipu: ['GLM'], moonshot: ['Kimi'],
    };

    function arenaFuzzyMatch(query, target) {
        let qi = 0;
        for (let ti = 0; ti < target.length && qi < query.length; ti++) {
            if (target[ti] === query[qi]) qi++;
        }
        return qi === query.length;
    }

    function arenaSmartSearch(query, modelId, displayName, family) {
        const q = query.toLowerCase().trim();
        if (!q) return true;
        const bare = modelId.replace('arena/', '').toLowerCase();
        const disp = displayName.toLowerCase();
        const fam = family.toLowerCase();
        if (bare.includes(q) || disp.includes(q)) return true;
        if (fam.includes(q)) return true;
        for (const [alias, families] of Object.entries(ARENA_SIDEBAR_ALIASES)) {
            if (alias.includes(q) || q.includes(alias)) {
                if (families.some(f => f === family)) return true;
            }
        }
        if (q.length >= 2 && arenaFuzzyMatch(q, bare)) return true;
        return false;
    }

    // Filtered arena families based on search
    $: arenaFilteredFamilies = (() => {
        const families = $groupedModels['arena'] || [];
        if (!arenaSidebarSearch) return families;
        return families.map(([family, models]) => {
            const filtered = models.filter(m => arenaSmartSearch(arenaSidebarSearch, m.id, m.display_name, family));
            return /** @type {[string, any[]]} */ ([family, filtered]);
        }).filter(([, models]) => models.length > 0);
    })();

    /** @type {number | null} */
    let copiedMsgId = null;
    function copyMessage(id, text) {
        navigator.clipboard.writeText(text);
        copiedMsgId = id;
        setTimeout(() => { if (copiedMsgId === id) copiedMsgId = null; }, 1500);
    }

    async function handleThinkingChange(value) {
        thinkingEffort.set(value);
        try { await setThinkingEffort(value); } catch (e) { /* config save best-effort */ }
    }

    let refreshingModels = false;
    async function refreshModels() {
        if (refreshingModels) return;
        refreshingModels = true;
        try {
            const fresh = await getAvailableModels(true);
            if (fresh && Object.keys(fresh).length > 0) {
                availableModels.set(fresh);
            }
        } catch (e) { /* best-effort */ }
        refreshingModels = false;
    }

    onMount(async () => {
        try {
            const [status, modelsData, effort] = await Promise.all([
                getSetupStatus(),
                getAvailableModels().catch(() => null),
                getThinkingEffort().catch(() => 'off'),
            ]);
            setupReady = status.ready;
            if (setupReady) {
                thinkingEffort.set(effort);
                if (modelsData && Object.keys(modelsData).length > 0) {
                    availableModels.set(modelsData);
                    const provider = $selectedProvider;
                    const providerData = modelsData[provider];
                    if (providerData?.models?.length > 0) {
                        const ids = providerData.models.map((/** @type {any} */ m) => m.id);
                        if (!ids.includes($selectedModel)) {
                            selectedModel.set(ids[0]);
                        }
                    }
                }
                // Load sessions in background — don't block first paint
                refreshSavedSessions().then(() => {
                    if (!activeChatSessionId && chatSessions.length > 0) {
                        openSavedSession(chatSessions[0]);
                    }
                });
            }
        } catch (e) {
            setupReady = false;
        } finally {
            checkingSetup = false;
        }
    });

    async function onSetupReady(event) {
        setupReady = true;
        // Guard against null tool (e.g. chatgpt_cli ready but selected_tool not yet persisted)
        const tool = event?.detail?.tool ?? null;

        // Refresh models now that auth is complete
        try {
            const fresh = await getAvailableModels(true);
            if (fresh && Object.keys(fresh).length > 0) {
                availableModels.set(fresh);
                // Auto-select first model for the chosen provider, or first available
                const providerKey = tool === 'chatgpt_cli' ? 'chatgpt'
                    : (tool === 'gemini_cli' ? 'gemini' : 'claude');
                const providerData = fresh[providerKey];
                if (providerData?.models?.length > 0) {
                    selectedModel.set(providerData.models[0].id);
                } else {
                    // Fallback: pick first model from any connected provider
                    for (const [, pdata] of Object.entries(fresh)) {
                        if (/** @type {any} */ (pdata)?.models?.length > 0) {
                            selectedModel.set(/** @type {any} */ (pdata).models[0].id);
                            break;
                        }
                    }
                }
            }
        } catch (e) { /* non-fatal */ }

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
        $messages = rows.map((row) => {
            const role = toUiRole(row.role);
            const content = toUiContent(row);
            return {
                id: row.id || `${session.id}-${Math.random().toString(36).slice(2)}`,
                role,
                content,
                contentHtml: role === 'assistant' ? renderAssistantHtml(content, session.model || null) : null,
                model: session.model || null,
                transport: null,
                timestamp: formatMessageTime(row.created_at),
            };
        });
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

    // ── Scroll-to-bottom button with exponential acceleration ──
    let showScrollBtn = false;
    let scrollAnimId = null;

    function checkScrollPosition() {
        if (!chatContainer) return;
        const distFromBottom = chatContainer.scrollHeight - chatContainer.scrollTop - chatContainer.clientHeight;
        showScrollBtn = distFromBottom > 150;
    }

    let animatingScroll = false;

    function handleChatScroll() {
        checkScrollPosition();
        // If user scrolls during animation (e.g. mousewheel), cancel it
        if (scrollAnimId && !animatingScroll) {
            cancelAnimationFrame(scrollAnimId);
            scrollAnimId = null;
            if (chatContainer) chatContainer.style.scrollBehavior = 'smooth';
        }
    }

    function smoothScrollToBottom() {
        if (!chatContainer) return;
        // Cancel any existing animation
        if (scrollAnimId) { cancelAnimationFrame(scrollAnimId); scrollAnimId = null; }
        chatContainer.style.scrollBehavior = 'auto';
        const startTime = performance.now();
        const baseSpeed = 2;

        function step(now) {
            if (!chatContainer) { scrollAnimId = null; animatingScroll = false; return; }
            const dist = chatContainer.scrollHeight - chatContainer.scrollTop - chatContainer.clientHeight;
            if (dist <= 1) {
                chatContainer.scrollTop = chatContainer.scrollHeight;
                chatContainer.style.scrollBehavior = 'smooth';
                scrollAnimId = null;
                animatingScroll = false;
                showScrollBtn = false;
                return;
            }
            const elapsed = now - startTime;
            const speed = baseSpeed * Math.pow(2, elapsed / 300);
            animatingScroll = true;
            chatContainer.scrollTop += Math.min(speed, dist);
            // Reset flag after browser processes the scroll event
            requestAnimationFrame(() => { animatingScroll = false; });
            scrollAnimId = requestAnimationFrame(step);
        }
        scrollAnimId = requestAnimationFrame(step);
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

        if (modelId.startsWith('arena/')) {
            showPrivacyWarning = true;
        }
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
                <div class="section-header">
                    <p class="section-label">Providers</p>
                    <button
                        class="refresh-models-btn {refreshingModels ? 'spinning' : ''}"
                        on:click={refreshModels}
                        disabled={refreshingModels}
                        title="Refresh models">
                        <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
                    </button>
                </div>
                <div class="providers-list">
                    {#each Object.entries($availableModels) as [provider, data]}
                        {#if data.models && data.models.length > 0 && provider !== 'arena'}
                            {@const families = $groupedModels[provider] || []}
                            {@const needsSub = families.length > 1 || data.models.length > 3}
                            <div class="provider-group">
                                <button class="provider-btn {expandedProvider === provider ? 'open' : ''}" on:click={() => { expandedProvider = expandedProvider === provider ? null : provider; expandedFamily = null; }}>
                                    <div style="display: flex; align-items: center; gap: 6px;">
                                        {#if ['claude', 'chatgpt', 'gemini'].includes(provider)}
                                            <img class="provider-logo" src="/logos/{provider}.png" alt="{provider}" />
                                        {:else}
                                            <span class="provider-badge provider-badge--{provider}">{provider.slice(0, 2).toUpperCase()}</span>
                                        {/if}
                                        <span style="text-transform: capitalize;">{provider}</span>
                                        <span class="provider-count">{data.models.length}</span>
                                    </div>
                                    <svg class="chevron {expandedProvider === provider ? 'open' : ''}" viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><polyline points="6 9 12 15 18 9"></polyline></svg>
                                </button>
                                {#if expandedProvider === provider}
                                    <div class="provider-models">
                                        {#if needsSub}
                                            {#each families as [family, familyModels]}
                                                <button class="family-btn {expandedFamily === `${provider}/${family}` ? 'open' : ''}"
                                                    on:click={() => expandedFamily = expandedFamily === `${provider}/${family}` ? null : `${provider}/${family}`}>
                                                    <span class="family-name">{family}</span>
                                                    <span class="family-count">{familyModels.length}</span>
                                                </button>
                                                {#if expandedFamily === `${provider}/${family}`}
                                                    <div class="family-models">
                                                        {#each familyModels as m}
                                                            <button class="model-btn {$selectedModel === m.id ? 'active' : ''}" on:click={() => selectModel(provider, m.id)}>
                                                                {m.display_name}
                                                                {#if m.note}<span class="model-note">{m.note}</span>{/if}
                                                            </button>
                                                        {/each}
                                                    </div>
                                                {/if}
                                            {/each}
                                        {:else}
                                            {#each data.models as m}
                                                <button class="model-btn {$selectedModel === m.id ? 'active' : ''}" on:click={() => selectModel(provider, m.id)}>
                                                    {m.display_name}
                                                    {#if m.note}<span class="model-note">{m.note}</span>{/if}
                                                </button>
                                            {/each}
                                        {/if}
                                    </div>
                                {/if}
                            </div>
                        {/if}
                    {/each}

                    <!-- Arena: chip-based layout with search -->
                    {#if $availableModels.arena?.models?.length > 0}
                        <div class="provider-group">
                            <button class="provider-btn {expandedProvider === 'arena' ? 'open' : ''}" on:click={() => { expandedProvider = expandedProvider === 'arena' ? null : 'arena'; expandedArenaChip = null; arenaSidebarSearch = ''; }}>
                                <div style="display: flex; align-items: center; gap: 6px;">
                                    <img class="provider-logo" src="/logos/arena_header.png" alt="arena" />
                                    <span>Arena</span>
                                    <span class="provider-count">{$availableModels.arena.models.length}</span>
                                </div>
                                <svg class="chevron {expandedProvider === 'arena' ? 'open' : ''}" viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><polyline points="6 9 12 15 18 9"></polyline></svg>
                            </button>
                            {#if expandedProvider === 'arena'}
                                <div class="arena-sidebar-content">
                                    <!-- Search -->
                                    <div class="arena-sidebar-search">
                                        <svg viewBox="0 0 24 24" width="12" height="12" stroke="currentColor" stroke-width="2" fill="none"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                                        <input type="text" placeholder="Search models..." bind:value={arenaSidebarSearch} />
                                        {#if arenaSidebarSearch}
                                            <button class="arena-search-clear" on:click={() => arenaSidebarSearch = ''}>×</button>
                                        {/if}
                                    </div>

                                    <!-- Provider chips -->
                                    <div class="arena-chip-grid">
                                        {#each arenaFilteredFamilies as [family, familyModels]}
                                            <button
                                                class="arena-chip {expandedArenaChip === family ? 'active' : ''}"
                                                on:click={() => expandedArenaChip = expandedArenaChip === family ? null : family}>
                                                {#if ARENA_CHIP_LOGOS[family]}
                                                    <img class="arena-chip-logo" src={ARENA_CHIP_LOGOS[family]} alt={family} />
                                                {/if}
                                                <span class="arena-chip-name">{family}</span>
                                                <span class="arena-chip-count">{familyModels.length}</span>
                                            </button>
                                        {/each}
                                    </div>

                                    <!-- Expanded model list -->
                                    {#if expandedArenaChip}
                                        {@const chipModels = arenaFilteredFamilies.find(([f]) => f === expandedArenaChip)?.[1] || []}
                                        <div class="arena-chip-models">
                                            {#each chipModels as m}
                                                <button class="model-btn {$selectedModel === m.id ? 'active' : ''}" on:click={() => selectModel('arena', m.id)}>
                                                    {m.display_name}
                                                    {#if m.note}<span class="model-note">{m.note}</span>{/if}
                                                </button>
                                            {/each}
                                        </div>
                                    {/if}
                                </div>
                            {/if}
                        </div>
                    {/if}
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
            <button class="icon-btn {activeView === 'arena' ? 'active' : ''}" on:click={() => activeView = 'arena'} title="Arena.ai">
                <svg viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" stroke-width="2" fill="none"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon></svg>
                <span>Arena</span>
            </button>
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
                    {$selectedModelDisplay}
                </div>
                {#if $modelSupportsThinking}
                <div class="thinking-control">
                    <span class="thinking-label">Thinking</span>
                    <div class="thinking-buttons">
                        {#each [['off', 'Off'], ['low', 'Lo'], ['medium', 'Med'], ['high', 'Hi']] as [value, label]}
                            <button
                                class="think-btn {$thinkingEffort === value ? 'active' : ''}"
                                on:click={() => handleThinkingChange(value)}>
                                {label}
                            </button>
                        {/each}
                    </div>
                </div>
                {/if}
            </div>

            <div class="messages" bind:this={chatContainer} on:scroll={handleChatScroll}>
                {#if $messages.length === 0}
                    <div class="empty-state">
                        <div class="empty-logo">⬡</div>
                        <h2>How can I help you today?</h2>
                    </div>
                {/if}

                {#each $messages as msg (msg.id)}
                    <div class="message-row {msg.role}">
                        <div class="message-content-wrapper">
                            <div class="role-indicator">{msg.role === 'user' ? 'You' : 'FreeHive'}</div>
                            <div class="bubble">
                                {#if msg.contentHtml}
                                    {@html msg.contentHtml}
                                {:else}
                                    {msg.content}
                                {/if}
                            </div>
                            <button class="copy-msg-btn" on:click={() => copyMessage(msg.id, msg.content)} title="Copy message">
                                {#if copiedMsgId === msg.id}
                                    <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><polyline points="20 6 9 17 4 12"></polyline></svg>
                                {:else}
                                    <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                                {/if}
                            </button>
                        </div>
                    </div>
                {/each}

                {#if $isLoading}
                    <div class="message-row assistant">
                        <div class="message-content-wrapper">
                            <div class="role-indicator">FreeHive</div>
                            <div class="bubble loading">
                                <span></span><span></span><span></span>
                            </div>
                        </div>
                    </div>
                {/if}
            </div>

            {#if showScrollBtn}
                <button class="scroll-bottom-btn" on:click={smoothScrollToBottom} title="Scroll to bottom">
                    <svg viewBox="0 0 24 24" width="18" height="18" stroke="currentColor" stroke-width="2" fill="none"><polyline points="6 9 12 15 18 9"></polyline></svg>
                </button>
            {/if}

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

        {:else if activeView === 'arena'}
            <ArenaPanel on:close={() => activeView = 'chat'} />
        {:else if activeView === 'accounts'}
            <AccountPanel
                on:openSettings={() => activeView = 'settings'}
                on:openSetup={() => activeView = 'setup'}
                on:modelsChanged={refreshModels}
                on:close={() => activeView = 'chat'} />
        {:else if activeView === 'setup'}
            <SetupScreen on:ready={(e) => { onSetupReady(e); activeView = 'chat'; }} />
        {:else if activeView === 'settings'}
            <SettingsPage on:close={() => activeView = 'chat'} on:history-cleared={async () => { await refreshSavedSessions(); handleNewChat(); activeView = 'chat'; }} />
        {/if}
    </main>
</div>
{/if}

<CaptchaPopup />

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
        overflow-y: auto;
        min-height: 0;
        flex: 1;
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

    .section-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding-right: 8px;
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
    .refresh-models-btn {
        background: transparent;
        border: none;
        color: var(--text-muted);
        cursor: pointer;
        padding: 4px;
        border-radius: 4px;
        display: flex;
        align-items: center;
        transition: color 0.15s;
    }
    .refresh-models-btn:hover {
        color: var(--text-primary);
        background: var(--bg-tertiary);
    }
    .refresh-models-btn:disabled {
        cursor: not-allowed;
        opacity: 0.5;
    }
    .refresh-models-btn.spinning svg {
        animation: spin 0.8s linear infinite;
    }
    @keyframes spin {
        from { transform: rotate(0deg); }
        to { transform: rotate(360deg); }
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

    /* Provider logo */
    .provider-logo {
        width: 20px; height: 20px;
        border-radius: 50%;
        object-fit: cover;
        flex-shrink: 0;
        border: 1px solid var(--border-light);
    }

    /* Provider badge (fallback for arena + others) */
    .provider-badge {
        width: 20px; height: 20px;
        border-radius: 5px;
        display: flex; align-items: center; justify-content: center;
        font-size: 9px; font-weight: 700; color: #fff;
        flex-shrink: 0;
        background: var(--text-muted);
    }
    .provider-badge--claude  { background: #d97706; }
    .provider-badge--chatgpt { background: #10a37f; }
    .provider-badge--gemini  { background: #4285f4; }
    .provider-badge--arena   { background: #8b5cf6; }

    .provider-count {
        font-size: 10px;
        color: var(--text-muted);
        margin-left: auto;
        padding-right: 4px;
    }

    /* Family subcategory level */
    .family-btn {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 5px 10px;
        background: transparent;
        border: none;
        border-radius: 5px;
        color: var(--text-secondary);
        font-size: 12px;
        font-weight: 500;
        cursor: pointer;
        transition: background 0.15s, color 0.15s;
    }
    .family-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); }
    .family-btn.open { color: var(--text-primary); }

    .family-name { flex: 1; text-align: left; }

    .family-count {
        font-size: 10px;
        color: var(--text-muted);
        background: var(--bg-tertiary);
        padding: 1px 6px;
        border-radius: 8px;
    }

    .family-models {
        display: flex;
        flex-direction: column;
        gap: 1px;
        padding-left: 10px;
        margin-bottom: 2px;
        border-left: 1px solid var(--border-light);
        margin-left: 10px;
    }

    /* Arena sidebar: chip layout + search */
    .arena-sidebar-content {
        padding: 6px 10px 8px;
        display: flex;
        flex-direction: column;
        gap: 8px;
        margin-left: 12px;
        border-left: 1px solid var(--border-light);
    }
    .arena-sidebar-search {
        display: flex;
        align-items: center;
        gap: 6px;
        background: var(--bg-tertiary);
        border-radius: 6px;
        padding: 5px 8px;
        color: var(--text-muted);
    }
    .arena-sidebar-search input {
        flex: 1;
        background: transparent;
        border: none;
        outline: none;
        color: var(--text-primary);
        font-size: 12px;
        font-family: inherit;
    }
    .arena-sidebar-search input::placeholder { color: var(--text-muted); }
    .arena-search-clear {
        background: none;
        border: none;
        color: var(--text-muted);
        font-size: 14px;
        cursor: pointer;
        padding: 0 2px;
        line-height: 1;
    }
    .arena-search-clear:hover { color: var(--text-primary); }
    .arena-chip-grid {
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
    }
    .arena-chip {
        display: flex;
        align-items: center;
        gap: 4px;
        padding: 4px 8px;
        border-radius: 6px;
        border: 1px solid var(--border-light);
        background: var(--bg-secondary);
        color: var(--text-secondary);
        font-size: 11px;
        cursor: pointer;
        transition: all 0.15s;
    }
    .arena-chip:hover {
        border-color: var(--text-muted);
        background: var(--bg-tertiary);
        color: var(--text-primary);
    }
    .arena-chip.active {
        border-color: #f97316;
        background: rgba(249, 115, 22, 0.1);
        color: #f97316;
    }
    .arena-chip-logo {
        width: 14px; height: 14px;
        border-radius: 3px;
        object-fit: cover;
    }
    .arena-chip-name {
        font-weight: 500;
    }
    .arena-chip-count {
        font-size: 9px;
        color: var(--text-muted);
        background: var(--bg-tertiary);
        padding: 0 4px;
        border-radius: 6px;
        min-width: 14px;
        text-align: center;
    }
    .arena-chip.active .arena-chip-count {
        background: rgba(249, 115, 22, 0.2);
        color: #f97316;
    }
    .arena-chip-models {
        display: flex;
        flex-direction: column;
        gap: 1px;
        padding-left: 4px;
        border-left: 2px solid #f97316;
        margin-left: 4px;
        max-height: 240px;
        overflow-y: auto;
    }

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

    .thinking-control {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-left: auto;
    }
    .thinking-label {
        font-size: 12px;
        color: var(--text-muted);
        white-space: nowrap;
    }
    .thinking-buttons {
        display: flex;
        gap: 0;
        border: 1px solid var(--border-medium);
        border-radius: 6px;
        overflow: hidden;
    }
    .think-btn {
        padding: 4px 10px;
        font-size: 12px;
        background: transparent;
        color: var(--text-secondary);
        border: none;
        border-right: 1px solid var(--border-medium);
        cursor: pointer;
        transition: background 0.15s, color 0.15s;
    }
    .think-btn:last-child {
        border-right: none;
    }
    .think-btn:hover {
        background: var(--bg-tertiary);
    }
    .think-btn.active {
        background: var(--accent-muted);
        color: var(--accent-color);
        font-weight: 600;
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

    .message-row {
        width: 100%;
        padding: 8px 24px;
        display: flex;
    }
    .message-row.user {
        justify-content: flex-end;
    }
    .message-row.assistant {
        justify-content: flex-start;
    }

    .message-content-wrapper {
        max-width: 75%;
        display: flex;
        flex-direction: column;
        gap: 4px;
    }

    .role-indicator {
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.3px;
    }
    .message-row.user .role-indicator {
        color: var(--accent-color);
        text-align: right;
    }
    .message-row.assistant .role-indicator {
        color: var(--text-muted);
    }

    .bubble {
        color: var(--text-primary);
        font-size: 15px;
        line-height: 1.6;
        padding: 12px 16px;
        border-radius: 14px;
    }
    .message-row.user .bubble {
        background: var(--accent-muted, rgba(99, 102, 241, 0.1));
        border-bottom-right-radius: 4px;
    }
    .message-row.assistant .bubble {
        background: var(--bg-secondary);
        border: 1px solid var(--border-light);
        border-bottom-left-radius: 4px;
    }

    .copy-msg-btn {
        background: transparent;
        border: none;
        color: var(--text-muted);
        cursor: pointer;
        padding: 2px 4px;
        border-radius: 4px;
        opacity: 0;
        transition: opacity 0.15s, color 0.15s;
        align-self: flex-end;
    }
    .message-row.user .copy-msg-btn { align-self: flex-end; }
    .message-row.assistant .copy-msg-btn { align-self: flex-start; }
    .message-content-wrapper:hover .copy-msg-btn { opacity: 1; }
    .copy-msg-btn:hover { color: var(--text-primary); background: var(--bg-tertiary); }

    /* ── Markdown rendering inside bubbles ── */
    .bubble :global(p) { margin: 0 0 0.75em; }
    .bubble :global(p:last-child) { margin-bottom: 0; }
    .bubble :global(p:first-child) { margin-top: 0; }

    /* Headings */
    .bubble :global(h1) { font-size: 1.4em; font-weight: 700; margin: 1.2em 0 0.5em; color: var(--text-primary); }
    .bubble :global(h2) { font-size: 1.2em; font-weight: 700; margin: 1em 0 0.4em; color: var(--text-primary); }
    .bubble :global(h3) { font-size: 1.05em; font-weight: 600; margin: 0.8em 0 0.3em; color: var(--text-primary); }
    .bubble :global(h4) { font-size: 1em; font-weight: 600; margin: 0.6em 0 0.2em; color: var(--text-secondary); }
    .bubble :global(h1:first-child), .bubble :global(h2:first-child), .bubble :global(h3:first-child) { margin-top: 0; }

    /* Code blocks */
    .bubble :global(pre) {
        background: #1a1b26;
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-radius: 10px;
        padding: 14px 16px;
        overflow-x: auto;
        margin: 10px 0;
        position: relative;
    }
    .bubble :global(pre::-webkit-scrollbar) { height: 5px; }
    .bubble :global(pre::-webkit-scrollbar-thumb) { background: rgba(255, 255, 255, 0.15); border-radius: 3px; }
    .bubble :global(pre code) {
        background: none;
        padding: 0;
        color: #c0caf5;
        font-size: 13px;
        line-height: 1.6;
    }

    /* Inline code */
    .bubble :global(code) {
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        font-size: 0.88em;
        background: rgba(127, 127, 127, 0.12);
        padding: 2px 6px;
        border-radius: 5px;
        color: var(--accent-color, #818cf8);
    }

    /* Lists */
    .bubble :global(ul), .bubble :global(ol) { padding-left: 22px; margin: 8px 0; }
    .bubble :global(li) { margin-bottom: 4px; line-height: 1.55; }
    .bubble :global(li > p) { margin-bottom: 0.3em; }
    .bubble :global(li::marker) { color: var(--text-muted); }

    /* Blockquotes */
    .bubble :global(blockquote) {
        border-left: 3px solid var(--accent-color, #818cf8);
        padding: 4px 0 4px 14px;
        color: var(--text-secondary);
        margin: 10px 0;
        font-style: italic;
    }
    .bubble :global(blockquote p:last-child) { margin-bottom: 0; }

    /* Horizontal rule */
    .bubble :global(hr) {
        border: none;
        border-top: 1px solid var(--border-light);
        margin: 16px 0;
    }

    /* Links */
    .bubble :global(a) {
        color: var(--accent-color, #818cf8);
        text-decoration: none;
        border-bottom: 1px solid transparent;
        transition: border-color 0.15s;
    }
    .bubble :global(a:hover) { border-bottom-color: var(--accent-color, #818cf8); }

    /* Tables */
    .bubble :global(table) {
        width: 100%;
        border-collapse: collapse;
        margin: 10px 0;
        font-size: 0.9em;
    }
    .bubble :global(th) {
        background: rgba(127, 127, 127, 0.08);
        font-weight: 600;
        text-align: left;
        padding: 8px 12px;
        border-bottom: 2px solid var(--border-medium);
        color: var(--text-primary);
    }
    .bubble :global(td) {
        padding: 6px 12px;
        border-bottom: 1px solid var(--border-light);
        color: var(--text-secondary);
    }
    .bubble :global(tr:last-child td) { border-bottom: none; }

    /* Thinking / reasoning collapsible block */
    .bubble :global(.thinking-block) {
        margin: 0 0 12px;
        border: 1px solid var(--border-light);
        border-radius: 10px;
        overflow: hidden;
        background: rgba(127, 127, 127, 0.04);
    }
    .bubble :global(.thinking-summary) {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 8px 14px;
        font-size: 12px;
        font-weight: 600;
        color: var(--text-muted);
        cursor: pointer;
        user-select: none;
        list-style: none;
        transition: color 0.15s;
    }
    .bubble :global(.thinking-summary::-webkit-details-marker) { display: none; }
    .bubble :global(.thinking-summary::marker) { display: none; content: ''; }
    .bubble :global(.thinking-summary:hover) { color: var(--text-secondary); }
    .bubble :global(.thinking-summary svg) { flex-shrink: 0; opacity: 0.5; }
    .bubble :global(.thinking-block[open] .thinking-summary) {
        color: var(--text-secondary);
        border-bottom: 1px solid var(--border-light);
    }
    .bubble :global(.thinking-content) {
        padding: 12px 14px;
        font-size: 13px;
        line-height: 1.55;
        color: var(--text-muted);
        max-height: 300px;
        overflow-y: auto;
    }
    .bubble :global(.thinking-content p) { margin-bottom: 0.6em; }
    .bubble :global(.thinking-content p:last-child) { margin-bottom: 0; }
    .bubble :global(.thinking-content::-webkit-scrollbar) { width: 4px; }
    .bubble :global(.thinking-content::-webkit-scrollbar-thumb) { background: var(--border-medium); border-radius: 2px; }

    /* Strong / Em */
    .bubble :global(strong) { font-weight: 600; color: var(--text-primary); }
    .bubble :global(em) { font-style: italic; }

    /* Images */
    .bubble :global(img) {
        max-width: 100%;
        border-radius: 8px;
        margin: 8px 0;
    }

    .bubble.loading {
        display: flex;
        gap: 6px;
        align-items: center;
        justify-content: center;
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
    .scroll-bottom-btn {
        position: absolute;
        bottom: 100px;
        left: 50%;
        transform: translateX(-50%);
        z-index: 50;
        width: 36px; height: 36px;
        border-radius: 50%;
        border: 1px solid var(--border-medium);
        background: var(--bg-secondary);
        color: var(--text-secondary);
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15);
        transition: background 0.15s, color 0.15s, transform 0.15s;
        animation: scrollBtnFadeIn 0.2s ease-out;
    }
    .scroll-bottom-btn:hover {
        background: var(--bg-tertiary);
        color: var(--text-primary);
        transform: translateX(-50%) scale(1.1);
    }
    @keyframes scrollBtnFadeIn {
        from { opacity: 0; transform: translateX(-50%) translateY(8px); }
        to { opacity: 1; transform: translateX(-50%) translateY(0); }
    }

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
