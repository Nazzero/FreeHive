<script>
    import { createEventDispatcher, onDestroy } from 'svelte';
    import { availableModels } from '$lib/store.js';
    import { API_ROOT_URL, API_BASE_URL } from '$lib/config.js';
    import { resetDatabase, getUsageStats, getArenaModels } from '$lib/api.js';

    const dispatch = createEventDispatcher();

    let settingsTab = 'apikeys';
    let showClearConfirm = false;
    let clearingHistory = false;

    /** @type {Object|null} */
    let usageData = null;
    let usageLoading = false;
    let usageError = null;
    /** @type {Array<any>} */
    let arenaUsageSessions = [];

    // ── Backend Logs state ────────────────────────────────────────────────
    /** @type {string[]} */
    let logLines = [];
    let logConnected = false;
    let autoScroll = true;
    /** @type {AbortController|null} */
    let logAbort = null;
    /** @type {HTMLPreElement|null} */
    let logContainer = null;
    const MAX_LOG_LINES = 1000;

    async function loadUsage() {
        usageLoading = true;
        usageError = null;
        try {
            const data = await getUsageStats();
            usageData = data.providers || {};
            arenaUsageSessions = data.arena_sessions || [];
        } catch (e) {
            usageError = e.message || 'Failed to load usage';
        } finally {
            usageLoading = false;
        }
    }

    /** @param {number} tokens */
    function formatTokens(tokens) {
        if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`;
        if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(0)}K`;
        return String(tokens);
    }

    $: if (settingsTab === 'usage') loadUsage();

    // Connect/disconnect log stream when tab changes
    $: if (settingsTab === 'logs') {
        connectLogStream();
    } else {
        disconnectLogStream();
    }

    async function connectLogStream() {
        disconnectLogStream();
        logAbort = new AbortController();
        logConnected = false;
        try {
            const res = await fetch(`${API_BASE_URL}/backend/logs?lines=200`, {
                signal: logAbort.signal,
            });
            if (!res.ok || !res.body) return;
            logConnected = true;
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buf = '';
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buf += decoder.decode(value, { stream: true });
                const parts = buf.split('\n\n');
                buf = parts.pop() || '';
                for (const part of parts) {
                    if (!part.startsWith('data: ')) continue;
                    try {
                        const payload = JSON.parse(part.slice(6));
                        if (payload.line != null) {
                            logLines = [...logLines.slice(-(MAX_LOG_LINES - 1)), payload.line];
                            if (autoScroll && logContainer) {
                                requestAnimationFrame(() => {
                                    if (logContainer) logContainer.scrollTop = logContainer.scrollHeight;
                                });
                            }
                        }
                    } catch { /* skip malformed */ }
                }
            }
        } catch (e) {
            if (e?.name !== 'AbortError') {
                logLines = [...logLines, `[Connection error: ${e?.message || 'unknown'}]`];
            }
        } finally {
            logConnected = false;
        }
    }

    function disconnectLogStream() {
        if (logAbort) {
            logAbort.abort();
            logAbort = null;
        }
        logConnected = false;
    }

    function clearLogView() {
        logLines = [];
    }

    onDestroy(() => disconnectLogStream());

    /** @type {string | null} */
    let copiedKey = null;

    let searchQuery = '';

    /** @type {Record<string, boolean>} */
    let collapsed = { claude: true, chatgpt: true, gemini: true, arena: true };

    /** @type {string | null} */
    let activeNoteFilter = null;

    /** @type {'idle' | 'loading' | 'success' | 'error'} */
    let opencodeState = 'idle';
    let opencodeMsg = '';
    let opencodeExpanded = false;
    /** @type {Record<string, boolean>} */
    let opencodeProviderOn = {};
    /** @type {Record<string, Set<string>>} */
    let opencodeSelected = {};
    let opencodeInitDone = false;
    let opencodeArenaSearch = '';

    // Initialize selections from available models
    $: if ($availableModels && !opencodeInitDone) {
        for (const [prov, data] of Object.entries($availableModels)) {
            if (!opencodeProviderOn.hasOwnProperty(prov)) {
                opencodeProviderOn[prov] = prov !== 'arena';
                // Non-arena providers start fully selected; arena starts empty so user picks
                if (prov !== 'arena') {
                    opencodeSelected[prov] = new Set((data.models || []).map((/** @type {any} */ m) => m.id));
                } else {
                    opencodeSelected[prov] = new Set();
                }
            }
        }
        if (Object.keys(opencodeProviderOn).length > 0) opencodeInitDone = true;
    }

    /** @param {string} prov */
    function ocToggleProvider(prov) {
        const wasOn = opencodeProviderOn[prov] ?? false;
        opencodeProviderOn = { ...opencodeProviderOn, [prov]: !wasOn };
        if (wasOn) {
            // Closing: clear selections
            opencodeSelected[prov] = new Set();
            opencodeSelected = { ...opencodeSelected };
        }
        // Opening: keep current selection (empty by default), user picks via All or individual checkboxes
    }

    /**
     * @param {string} prov
     * @param {string} modelId
     */
    function ocToggleModel(prov, modelId) {
        const s = new Set(opencodeSelected[prov] || []);
        if (s.has(modelId)) s.delete(modelId); else s.add(modelId);
        opencodeSelected = { ...opencodeSelected, [prov]: s };
        // Don't auto-close provider when 0 selected — user may still be picking
    }

    /** @param {string} prov @param {boolean} all */
    function ocSelectAll(prov, all) {
        if (all) {
            const models = $availableModels[prov]?.models || [];
            opencodeSelected[prov] = new Set(models.map((/** @type {any} */ m) => m.id));
        } else {
            opencodeSelected[prov] = new Set();
        }
        opencodeSelected = { ...opencodeSelected };
        // Keep provider list open — "None" just deselects, doesn't collapse
    }

    $: ocTotalSelected = Object.entries(opencodeSelected)
        .filter(([p]) => opencodeProviderOn[p])
        .reduce((sum, [, s]) => sum + s.size, 0);

    $: ocProviderCount = Object.values(opencodeProviderOn).filter(Boolean).length;

    $: ocArenaSearchLower = opencodeArenaSearch.trim().toLowerCase();

    async function addToOpenCode() {
        opencodeState = 'loading';
        opencodeMsg = '';
        try {
            /** @type {Record<string, string[]>} */
            const selections = {};
            for (const [prov, enabled] of Object.entries(opencodeProviderOn)) {
                if (enabled && opencodeSelected[prov]?.size > 0) {
                    selections[prov] = [...opencodeSelected[prov]];
                }
            }
            const res = await fetch(`${API_BASE_URL}/integrations/opencode`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ selections }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Unknown error');
            opencodeState = 'success';
            opencodeMsg = data.message;
        } catch (e) {
            opencodeState = 'error';
            opencodeMsg = e instanceof Error ? e.message : String(e);
        }
    }

    const BASE_URL = API_ROOT_URL;

    /** @type {Record<string, {name: string, color: string, logo: string}>} */
    const PROVIDER_LABELS = {
        claude:  { name: 'Claude',  color: '#cc785c', logo: '/logos/claude.png' },
        chatgpt: { name: 'ChatGPT', color: '#19c37d', logo: '/logos/chatgpt.png' },
        gemini:  { name: 'Gemini',  color: '#4285f4', logo: '/logos/gemini.png' },
        qwen:    { name: 'Qwen',    color: '#6366f1', logo: '/logos/qwen.png' },
        arena:   { name: 'Arena.ai', color: '#f97316', logo: '/logos/arena_header.png' },
    };

    /** @type {Record<string, number>} */
    const NOTE_ORDER = {
        'best quality': 0,
        'most capable': 0,
        'balanced':     1,
        'fast':         2,
        'most quota':   3,
    };

    /** @type {Record<string, {bg: string, fg: string}>} */
    const NOTE_COLORS = {
        'best quality': { bg: 'rgba(62, 207, 142, 0.15)',  fg: '#3ecf8e' },
        'most capable': { bg: 'rgba(168, 130, 255, 0.15)', fg: '#a882ff' },
        'balanced':     { bg: 'rgba(66, 133, 244, 0.15)',  fg: '#4285f4' },
        'fast':         { bg: 'rgba(251, 188, 4, 0.15)',   fg: '#fbc004' },
        'most quota':   { bg: 'rgba(160, 160, 160, 0.15)', fg: '#a0a0a0' },
    };

    /** @param {string} provider */
    function toggleCollapse(provider) {
        collapsed = { ...collapsed, [provider]: !collapsed[provider] };
    }

    /** @param {string} note */
    function toggleNoteFilter(note) {
        activeNoteFilter = activeNoteFilter === note ? null : note;
    }

    /** @param {string} modelId */
    function makeKey(modelId) {
        return `freehive-${modelId}`;
    }

    /**
     * @param {string} text
     * @param {string} keyId
     */
    async function copyToClipboard(text, keyId) {
        try {
            await navigator.clipboard.writeText(text);
            copiedKey = keyId;
            setTimeout(() => { copiedKey = null; }, 2000);
        } catch (e) {
            // fallback
            const el = document.createElement('textarea');
            el.value = text;
            document.body.appendChild(el);
            el.select();
            document.execCommand('copy');
            document.body.removeChild(el);
            copiedKey = keyId;
            setTimeout(() => { copiedKey = null; }, 2000);
        }
    }

    $: allNotes = [...new Set(
        Object.values($availableModels)
            .flatMap(d => (d.models || []).map((/** @type {any} */ m) => m.note))
            .filter(Boolean)
    )].sort((a, b) => (NOTE_ORDER[a] ?? 99) - (NOTE_ORDER[b] ?? 99));

    $: searchLower = searchQuery.trim().toLowerCase();

    $: hasAnyFilter = searchQuery !== '' || activeNoteFilter !== null || arenaSearch !== '' || arenaCapFilter !== 'all';

    function clearAllFilters() {
        searchQuery = '';
        activeNoteFilter = null;
        arenaSearch = '';
        arenaCapFilter = 'all';
        expandedArenaCard = null;
    }

    $: filteredProviders = Object.entries($availableModels)
        .map(([provider, data]) => {
            const models = (data.models || [])
                .filter((/** @type {any} */ m) => {
                    if (activeNoteFilter && m.note !== activeNoteFilter) return false;
                    if (searchLower && !m.display_name.toLowerCase().includes(searchLower) && !m.id.toLowerCase().includes(searchLower)) return false;
                    return true;
                })
                .sort((a, b) => (NOTE_ORDER[a.note] ?? 99) - (NOTE_ORDER[b.note] ?? 99));
            return { provider, tier: data.tier, models };
        })
        .filter(g => g.models.length > 0 && g.provider !== 'arena');

    // ─── Arena card grid state ───
    let arenaSearch = '';
    /** @type {string | null} */
    let expandedArenaCard = null;
    let arenaCapFilter = 'all'; // 'all' | 'code' | 'search' | 'chat'
    /** @type {Record<string, string[]>} */
    let arenaCapabilities = {};
    /** @type {Record<string, string[]>} */
    let arenaModes = {};
    let arenaCapsFetched = false;

    async function fetchArenaCaps() {
        if (arenaCapsFetched) return;
        try {
            const data = await getArenaModels();
            arenaCapabilities = data.capabilities || {};
            arenaModes = data.model_modes || {};
            arenaCapsFetched = true;
        } catch { /* non-fatal */ }
    }

    // Fetch capabilities when arena uncollapsed
    $: if (!collapsed.arena) fetchArenaCaps();

    const ARENA_FAMILY_MAP = /** @type {const} */ ({
        claude: 'Claude', gpt: 'OpenAI', o1: 'OpenAI', o3: 'OpenAI', o4: 'OpenAI',
        codex: 'OpenAI',
        gemini: 'Gemini', gemma: 'Gemini', grok: 'Grok', qwen: 'Qwen', qwq: 'Qwen',
        deepseek: 'DeepSeek', mistral: 'Mistral', kimi: 'Kimi', minimax: 'MiniMax',
        glm: 'GLM', mimo: 'GLM', llama: 'Meta', olmo: 'OLMo', step: 'Step',
        ring: 'Ring', ling: 'Ling', mercury: 'Mercury', trinity: 'Trinity',
        ernie: 'Ernie', hunyuan: 'Hunyuan', seed: 'Seed', dola: 'Dola',
        amazon: 'Amazon', global: 'Amazon',
    });

    const ARENA_FAMILY_PREFIXES = Object.keys(ARENA_FAMILY_MAP).sort((a, b) => b.length - a.length);

    /** @type {Array<{key: string, logo: string}>} */
    const ARENA_POPULAR_FAMILIES = [
        { key: 'OpenAI',    logo: '/logos/arena/openai.png' },
        { key: 'Claude',    logo: '/logos/arena/claude.png' },
        { key: 'Gemini',    logo: '/logos/arena/gemini.png' },
        { key: 'Grok',      logo: '/logos/arena/grok.png' },
        { key: 'DeepSeek',  logo: '/logos/arena/deepseek.png' },
        { key: 'Qwen',      logo: '/logos/arena/qwen.png' },
        { key: 'Mistral',   logo: '/logos/arena/mistral.png' },
        { key: 'GLM',       logo: '/logos/arena/glm.png' },
        { key: 'Kimi',      logo: '/logos/arena/kimi.png' },
        { key: 'MiniMax',   logo: '/logos/arena/minimax.png' },
    ];

    const ARENA_POPULAR_KEYS = new Set(ARENA_POPULAR_FAMILIES.map(f => f.key));

    /** @type {Record<string, string[]>} */
    const ARENA_SEARCH_ALIASES = {
        'google': ['Gemini'], 'anthropic': ['Claude'], 'openai': ['OpenAI'],
        'chatgpt': ['OpenAI'], 'meta': ['Meta', 'Llama'], 'facebook': ['Meta', 'Llama'],
        'alibaba': ['Qwen'], 'aliyun': ['Qwen'], 'baidu': ['Ernie'],
        'tencent': ['Hunyuan'], 'bytedance': ['Seed'], 'zhipu': ['GLM'],
        'moonshot': ['Kimi'], 'xai': ['Grok'], 'x.ai': ['Grok'],
        'amazon': ['Amazon'], 'aws': ['Amazon'],
    };

    /** @param {string} id */
    function arenaDetectFamily(id) {
        const slug = id.replace(/^arena\//, '').toLowerCase();
        for (const prefix of ARENA_FAMILY_PREFIXES) {
            if (slug.startsWith(prefix)) return ARENA_FAMILY_MAP[prefix];
        }
        const first = slug.split(/[-._]/)[0];
        return first.charAt(0).toUpperCase() + first.slice(1);
    }

    /** @param {string} family */
    function arenaFamilyLogo(family) {
        return ARENA_POPULAR_FAMILIES.find(f => f.key === family)?.logo || '';
    }

    /** @param {string} family */
    function toggleArenaCard(family) {
        expandedArenaCard = expandedArenaCard === family ? null : family;
    }

    /** @param {string} id @returns {number} */
    function arenaModelStrength(id) {
        const slug = id.replace(/^arena\//, '').toLowerCase();
        let score = 0;
        if (slug.includes('pro'))    score += 500;
        if (slug.includes('max'))    score += 450;
        if (slug.includes('high'))   score += 400;
        if (slug.includes('ultra'))  score += 480;
        if (slug.includes('medium')) score += 200;
        if (slug.includes('mini'))   score -= 200;
        if (slug.includes('lite'))   score -= 150;
        if (slug.includes('nano'))   score -= 250;
        if (slug.includes('small'))  score -= 100;
        if (slug.includes('fast'))   score -= 50;
        if (slug.includes('preview')) score -= 10;
        const verMatch = slug.match(/[\-_v]?(\d+)[\.\-]?(\d*)/);
        if (verMatch) {
            score += (parseInt(verMatch[1]) || 0) * 100 + (parseInt(verMatch[2]) || 0) * 10;
        }
        if (slug.includes('opus'))   score += 600;
        if (slug.includes('sonnet')) score += 400;
        if (slug.includes('haiku')) score += 200;
        if (/^o[134]/.test(slug)) score += 550;
        if (slug.includes('codex')) score += 300;
        return score;
    }

    /**
     * @param {string} query @param {string} bare @param {string} display @param {string} family
     * @returns {boolean}
     */
    function arenaSmartMatch(query, bare, display, family) {
        const bareLower = bare.toLowerCase();
        const displayLower = display.toLowerCase();
        const familyLower = family.toLowerCase();
        if (bareLower.includes(query) || displayLower.includes(query)) return true;
        if (familyLower.includes(query)) return true;
        for (const [alias, families] of Object.entries(ARENA_SEARCH_ALIASES)) {
            if (alias.includes(query) || query.includes(alias)) {
                if (families.some(f => f === family)) return true;
            }
        }
        if (query.length >= 2) {
            let qi = 0;
            for (let ti = 0; ti < bareLower.length && qi < query.length; ti++) {
                if (bareLower[ti] === query[qi]) qi++;
            }
            if (qi === query.length) return true;
        }
        return false;
    }

    // Reactive arena model pipeline
    $: arenaModels = ($availableModels.arena?.models || []).map((/** @type {any} */ m) => ({
        id: m.id,
        display: m.display_name || m.id.replace('arena/', ''),
    }));

    $: arenaSearchLower = arenaSearch.trim().toLowerCase();

    $: arenaFilteredModels = arenaModels.filter((/** @type {any} */ m) => {
        const bare = m.id.replace('arena/', '');
        const family = arenaDetectFamily(m.id);
        // Global search from toolbar
        if (searchLower && !arenaSmartMatch(searchLower, bare, m.display, family)) return false;
        // Arena-specific search
        if (arenaSearchLower && !arenaSmartMatch(arenaSearchLower, bare, m.display, family)) return false;
        // Capability filter
        const caps = arenaCapabilities[bare] || [];
        const modes = arenaModes[bare] || [];
        if (arenaCapFilter === 'code' && !modes.includes('code')) return false;
        if (arenaCapFilter === 'search' && !caps.includes('search')) return false;
        if (arenaCapFilter === 'chat' && !modes.includes('chat') && modes.length > 0) return false;
        return true;
    });

    // Capability counts for filter badges
    $: arenaCodeCount = arenaModels.filter(m => (arenaModes[m.id.replace('arena/', '')] || []).includes('code')).length;
    $: arenaSearchCount = arenaModels.filter(m => (arenaCapabilities[m.id.replace('arena/', '')] || []).includes('search')).length;
    $: arenaChatCount = arenaModels.filter(m => { const modes = arenaModes[m.id.replace('arena/', '')] || []; return modes.includes('chat') || modes.length === 0; }).length;

    $: arenaGrouped = (() => {
        /** @type {Record<string, Array<{id: string, display: string}>>} */
        const groups = {};
        for (const m of arenaFilteredModels) {
            const family = arenaDetectFamily(m.id);
            if (!groups[family]) groups[family] = [];
            groups[family].push(m);
        }
        for (const key of Object.keys(groups)) {
            groups[key].sort((a, b) => arenaModelStrength(b.id) - arenaModelStrength(a.id));
        }
        return groups;
    })();

    $: arenaPopularFamilies = ARENA_POPULAR_FAMILIES
        .filter(f => arenaGrouped[f.key]?.length > 0)
        .map(f => ({ ...f, models: arenaGrouped[f.key] }));

    $: arenaOtherFamilies = Object.entries(arenaGrouped)
        .filter(([key]) => !ARENA_POPULAR_KEYS.has(key))
        .sort((a, b) => a[0].localeCompare(b[0]))
        .map(([key, models]) => ({ key, models }));

    $: if (arenaSearch) {
        const allKeys = [...arenaPopularFamilies.map(f => f.key), ...arenaOtherFamilies.map(f => f.key)];
        if (allKeys.length === 1) expandedArenaCard = allKeys[0];
    }
</script>

<div class="settings-page">
    <div class="settings-header">
        <button class="back-btn" on:click={() => dispatch('close')}>← Back</button>
        <h2>Settings</h2>
    </div>
    <div class="settings-tabs">
        <button
            class="tab-btn {settingsTab === 'apikeys' ? 'active' : ''}"
            on:click={() => settingsTab = 'apikeys'}>
            API Keys
        </button>
        <button
            class="tab-btn {settingsTab === 'data' ? 'active' : ''}"
            on:click={() => settingsTab = 'data'}>
            Data
        </button>
        <button
            class="tab-btn {settingsTab === 'usage' ? 'active' : ''}"
            on:click={() => settingsTab = 'usage'}>
            Usage
        </button>
        <button
            class="tab-btn {settingsTab === 'logs' ? 'active' : ''}"
            on:click={() => settingsTab = 'logs'}>
            Backend Logs
        </button>
    </div>

    {#if settingsTab === 'apikeys'}
        <div class="apikeys-page">

            <div class="info-block">
                <div class="info-row">
                    <span class="info-label">Base URL</span>
                    <code class="info-value">{BASE_URL}</code>
                    <button class="copy-btn small" on:click={() => copyToClipboard(BASE_URL, 'baseurl')}>
                        {copiedKey === 'baseurl' ? '✓' : 'Copy'}
                    </button>
                </div>
                <p class="info-hint">
                    Point any Anthropic or OpenAI compatible tool at this URL.
                    Each API key below routes to a specific model — no model config needed in your tool.
                </p>
            </div>

            <div class="keys-section">
                <h3 class="keys-heading">API Keys by Model</h3>
                <p class="keys-subheading">Select a model to use its key. The key locks the request to that exact model regardless of what model name your tool sends.</p>

                <div class="keys-toolbar">
                    <div class="search-box">
                        <svg class="search-icon" viewBox="0 0 20 20" fill="currentColor" width="16" height="16">
                            <path fill-rule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8zM2 8a6 6 0 1110.89 3.476l4.817 4.817a1 1 0 01-1.414 1.414l-4.816-4.816A6 6 0 012 8z" clip-rule="evenodd" />
                        </svg>
                        <input
                            type="text"
                            class="search-input"
                            placeholder="Filter models..."
                            bind:value={searchQuery}
                        />
                        {#if searchQuery}
                            <button class="search-clear" on:click={() => searchQuery = ''}>&#215;</button>
                        {/if}
                    </div>
                    <div class="filter-chips">
                        {#each allNotes as note}
                            <button
                                class="filter-chip {activeNoteFilter === note ? 'active' : ''}"
                                style="--chip-bg: {NOTE_COLORS[note]?.bg ?? 'var(--bg-tertiary)'}; --chip-fg: {NOTE_COLORS[note]?.fg ?? 'var(--text-secondary)'}"
                                on:click={() => toggleNoteFilter(note)}
                            >
                                {note}
                            </button>
                        {/each}
                        {#if hasAnyFilter}
                            <button class="filter-chip clear-all-chip" on:click={clearAllFilters}>
                                &#215; Clear all
                            </button>
                        {/if}
                    </div>
                </div>

                {#each filteredProviders as { provider, tier, models } (provider)}
                    <div class="provider-group">
                        <button class="provider-header" on:click={() => toggleCollapse(provider)}>
                            {#if PROVIDER_LABELS[provider]?.logo}
                                <img class="provider-logo" src={PROVIDER_LABELS[provider].logo} alt={PROVIDER_LABELS[provider].name} />
                            {:else}
                                <span class="provider-dot" style="background: {PROVIDER_LABELS[provider]?.color ?? 'var(--text-muted)'}"></span>
                            {/if}
                            <span class="provider-name">{PROVIDER_LABELS[provider]?.name ?? provider}</span>
                            {#if tier && tier !== 'unknown'}
                                <span class="tier-pill">{tier}</span>
                            {/if}
                            <span class="model-count">{models.length} model{models.length !== 1 ? 's' : ''}</span>
                            <span class="collapse-arrow {collapsed[provider] ? 'collapsed' : ''}">&#9662;</span>
                        </button>

                        {#if !collapsed[provider]}
                            <div class="model-key-list">
                                {#each models as model (model.id)}
                                    {@const key = makeKey(model.id)}
                                    <div class="model-key-row">
                                        <div class="model-info">
                                            <span class="model-name">{model.display_name}</span>
                                            {#if model.note}
                                                <span
                                                    class="model-note"
                                                    style="background: {NOTE_COLORS[model.note]?.bg ?? 'var(--bg-tertiary)'}; color: {NOTE_COLORS[model.note]?.fg ?? 'var(--text-secondary)'}"
                                                >
                                                    {model.note}
                                                </span>
                                            {/if}
                                        </div>
                                        <div class="key-area">
                                            <code class="key-value">{key}</code>
                                            <button
                                                class="copy-btn {copiedKey === key ? 'copied' : ''}"
                                                on:click={() => copyToClipboard(key, key)}>
                                                {copiedKey === key ? '✓ Copied' : 'Copy'}
                                            </button>
                                        </div>
                                    </div>
                                {/each}
                            </div>
                        {/if}
                    </div>
                {:else}
                    <p class="no-results">No models match your search.</p>
                {/each}

                <!-- Arena section -->
                {#if arenaModels.length > 0}
                    <div class="provider-group arena-group">
                        <button class="provider-header" on:click={() => toggleCollapse('arena')}>
                            <img class="provider-logo" src={PROVIDER_LABELS.arena.logo} alt="Arena.ai" />
                            <span class="provider-name">{PROVIDER_LABELS.arena.name}</span>
                            <span class="tier-pill">free</span>
                            <span class="model-count">{arenaModels.length} model{arenaModels.length !== 1 ? 's' : ''}</span>
                            <span class="collapse-arrow {collapsed.arena ? 'collapsed' : ''}">&#9662;</span>
                        </button>

                        {#if !collapsed.arena}
                            <div class="arena-content">
                                <!-- Arena search -->
                                <div class="arena-search-bar">
                                    <div class="search-box">
                                        <svg class="search-icon" viewBox="0 0 20 20" fill="currentColor" width="16" height="16">
                                            <path fill-rule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8zM2 8a6 6 0 1110.89 3.476l4.817 4.817a1 1 0 01-1.414 1.414l-4.816-4.816A6 6 0 012 8z" clip-rule="evenodd" />
                                        </svg>
                                        <input
                                            type="text"
                                            class="search-input"
                                            placeholder="Search arena models... (try 'google', 'gpt', 'gsf')"
                                            bind:value={arenaSearch}
                                        />
                                        {#if arenaSearch}
                                            <button class="search-clear" on:click={() => { arenaSearch = ''; expandedArenaCard = null; }}>&#215;</button>
                                        {/if}
                                    </div>
                                    {#if arenaSearch}
                                        <span class="arena-result-count">{arenaFilteredModels.length} result{arenaFilteredModels.length !== 1 ? 's' : ''}</span>
                                    {/if}
                                </div>

                                <!-- Capability filter pills -->
                                <div class="arena-filter-pills">
                                    <button class="arena-filter-pill {arenaCapFilter === 'all' ? 'active' : ''}" on:click={() => arenaCapFilter = 'all'}>
                                        All <span class="arena-filter-count">{arenaModels.length}</span>
                                    </button>
                                    <button class="arena-filter-pill {arenaCapFilter === 'chat' ? 'active' : ''}" on:click={() => arenaCapFilter = arenaCapFilter === 'chat' ? 'all' : 'chat'}>
                                        <span class="arena-tag arena-tag-chat">chat</span> <span class="arena-filter-count">{arenaChatCount}</span>
                                    </button>
                                    <button class="arena-filter-pill {arenaCapFilter === 'code' ? 'active' : ''}" on:click={() => arenaCapFilter = arenaCapFilter === 'code' ? 'all' : 'code'}>
                                        <span class="arena-tag arena-tag-code">code</span> <span class="arena-filter-count">{arenaCodeCount}</span>
                                    </button>
                                    <button class="arena-filter-pill {arenaCapFilter === 'search' ? 'active' : ''}" on:click={() => arenaCapFilter = arenaCapFilter === 'search' ? 'all' : 'search'}>
                                        <span class="arena-tag arena-tag-search">search</span> <span class="arena-filter-count">{arenaSearchCount}</span>
                                    </button>
                                </div>

                                <!-- Provider card grid -->
                                {#if arenaFilteredModels.length === 0}
                                    <p class="no-results">No arena models match "{arenaSearch}"</p>
                                {:else}
                                    {#if arenaPopularFamilies.length > 0}
                                        <div class="arena-card-grid">
                                            {#each arenaPopularFamilies as fam}
                                                <button
                                                    class="arena-provider-card {expandedArenaCard === fam.key ? 'expanded' : ''}"
                                                    on:click={() => toggleArenaCard(fam.key)}
                                                >
                                                    <img class="arena-card-logo" src={fam.logo} alt={fam.key} />
                                                    <span class="arena-card-name">{fam.key}</span>
                                                    <span class="arena-card-count">{fam.models.length}</span>
                                                </button>
                                            {/each}
                                        </div>
                                    {/if}

                                    <!-- Expanded model list with API keys -->
                                    {#if expandedArenaCard && arenaGrouped[expandedArenaCard]}
                                        <div class="arena-expanded-panel">
                                            <div class="arena-expanded-header">
                                                {#if arenaFamilyLogo(expandedArenaCard)}
                                                    <img class="arena-expanded-logo" src={arenaFamilyLogo(expandedArenaCard)} alt={expandedArenaCard} />
                                                {/if}
                                                <span class="arena-expanded-title">{expandedArenaCard}</span>
                                                <span class="arena-expanded-count">{arenaGrouped[expandedArenaCard].length} models</span>
                                                <button class="arena-expanded-close" on:click={() => expandedArenaCard = null}>&#215;</button>
                                            </div>
                                            <div class="arena-expanded-list">
                                                {#each arenaGrouped[expandedArenaCard] as m (m.id)}
                                                    {@const key = makeKey(m.id)}
                                                    <div class="model-key-row">
                                                        <div class="model-info">
                                                            <span class="model-name">{m.display}</span>
                                                        </div>
                                                        <div class="key-area">
                                                            <code class="key-value">{key}</code>
                                                            <button
                                                                class="copy-btn {copiedKey === key ? 'copied' : ''}"
                                                                on:click={() => copyToClipboard(key, key)}>
                                                                {copiedKey === key ? '✓ Copied' : 'Copy'}
                                                            </button>
                                                        </div>
                                                    </div>
                                                {/each}
                                            </div>
                                        </div>
                                    {/if}

                                    <!-- Other providers -->
                                    {#if arenaOtherFamilies.length > 0}
                                        <div class="arena-other-section">
                                            <button class="arena-other-header" on:click={() => toggleArenaCard('__arena_other__')}>
                                                <span class="arena-other-chevron {expandedArenaCard === '__arena_other__' ? 'open' : ''}">&#9662;</span>
                                                <span>Other Providers</span>
                                                <span class="arena-card-count">{arenaOtherFamilies.reduce((s, f) => s + f.models.length, 0)}</span>
                                            </button>
                                            {#if expandedArenaCard === '__arena_other__'}
                                                <div class="arena-other-chips">
                                                    {#each arenaOtherFamilies as fam}
                                                        <button
                                                            class="arena-other-chip"
                                                            on:click|stopPropagation={() => toggleArenaCard(`other:${fam.key}`)}
                                                        >
                                                            {fam.key}
                                                            <span class="arena-chip-count">{fam.models.length}</span>
                                                        </button>
                                                    {/each}
                                                </div>
                                            {/if}
                                        </div>

                                        <!-- Expanded other family -->
                                        {#each arenaOtherFamilies as fam}
                                            {#if expandedArenaCard === `other:${fam.key}`}
                                                <div class="arena-expanded-panel">
                                                    <div class="arena-expanded-header">
                                                        <span class="arena-expanded-title">{fam.key}</span>
                                                        <span class="arena-expanded-count">{fam.models.length} models</span>
                                                        <button class="arena-expanded-close" on:click={() => expandedArenaCard = '__arena_other__'}>&#215;</button>
                                                    </div>
                                                    <div class="arena-expanded-list">
                                                        {#each fam.models as m (m.id)}
                                                            {@const key = makeKey(m.id)}
                                                            <div class="model-key-row">
                                                                <div class="model-info">
                                                                    <span class="model-name">{m.display}</span>
                                                                </div>
                                                                <div class="key-area">
                                                                    <code class="key-value">{key}</code>
                                                                    <button
                                                                        class="copy-btn {copiedKey === key ? 'copied' : ''}"
                                                                        on:click={() => copyToClipboard(key, key)}>
                                                                        {copiedKey === key ? '✓ Copied' : 'Copy'}
                                                                    </button>
                                                                </div>
                                                            </div>
                                                        {/each}
                                                    </div>
                                                </div>
                                            {/if}
                                        {/each}
                                    {/if}
                                {/if}
                            </div>
                        {/if}
                    </div>
                {/if}
            </div>

            <div class="quickstart">
                <h3 class="keys-heading">Quick Setup</h3>

                <div class="opencode-card">
                    <div class="opencode-card-header" on:click={() => opencodeExpanded = !opencodeExpanded}>
                        <div class="opencode-left">
                            <span class="opencode-title">OpenCode</span>
                            <span class="opencode-desc">Select providers and models to import into <code>~/.config/opencode/opencode.json</code></span>
                        </div>
                        <div class="opencode-right">
                            {#if opencodeState === 'success'}
                                <span class="opencode-feedback success">{opencodeMsg}</span>
                            {:else if opencodeState === 'error'}
                                <span class="opencode-feedback error">{opencodeMsg}</span>
                            {/if}
                            <span class="collapse-arrow {opencodeExpanded ? '' : 'collapsed'}">&#9662;</span>
                        </div>
                    </div>

                    {#if opencodeExpanded}
                        <div class="opencode-panel">
                            {#each Object.entries($availableModels) as [prov, data] (prov)}
                                {@const models = data.models || []}
                                {@const selected = opencodeSelected[prov] || new Set()}
                                {@const enabled = opencodeProviderOn[prov] ?? false}
                                {@const isArena = prov === 'arena'}
                                {@const provLabel = PROVIDER_LABELS[prov]}
                                <div class="oc-provider">
                                    <div class="oc-provider-row">
                                        <label class="oc-provider-check">
                                            <input type="checkbox" checked={enabled} on:change={() => ocToggleProvider(prov)} />
                                            {#if provLabel?.logo}
                                                <img class="oc-provider-logo" src={provLabel.logo} alt={provLabel.name} />
                                            {/if}
                                            <span class="oc-provider-name" style="color: {provLabel?.color ?? 'var(--text-primary)'}">{provLabel?.name ?? prov}</span>
                                            <span class="oc-provider-count">{selected.size}/{models.length}</span>
                                        </label>
                                        <div class="oc-provider-actions">
                                            <button class="oc-link-btn" on:click={() => ocSelectAll(prov, true)}>All</button>
                                            <span class="oc-separator">|</span>
                                            <button class="oc-link-btn" on:click={() => ocSelectAll(prov, false)}>None</button>
                                        </div>
                                    </div>

                                    {#if enabled}
                                        {#if isArena && models.length > 20}
                                            <div class="oc-arena-search">
                                                <input
                                                    type="text"
                                                    class="oc-arena-input"
                                                    placeholder="Search arena models..."
                                                    bind:value={opencodeArenaSearch}
                                                />
                                                {#if opencodeArenaSearch}
                                                    <button class="search-clear" on:click={() => opencodeArenaSearch = ''}>&#215;</button>
                                                {/if}
                                            </div>
                                        {/if}
                                        <div class="oc-model-list">
                                            {#each models as m (m.id)}
                                                {@const show = !isArena || !ocArenaSearchLower || m.display_name?.toLowerCase().includes(ocArenaSearchLower) || m.id.toLowerCase().includes(ocArenaSearchLower)}
                                                {#if show}
                                                    <label class="oc-model-item">
                                                        <input type="checkbox" checked={selected.has(m.id)} on:change={() => ocToggleModel(prov, m.id)} />
                                                        <span class="oc-model-name">{m.display_name || m.id}</span>
                                                        {#if m.note}
                                                            <span
                                                                class="model-note"
                                                                style="background: {NOTE_COLORS[m.note]?.bg ?? 'var(--bg-tertiary)'}; color: {NOTE_COLORS[m.note]?.fg ?? 'var(--text-secondary)'}"
                                                            >{m.note}</span>
                                                        {/if}
                                                    </label>
                                                {/if}
                                            {/each}
                                        </div>
                                    {/if}
                                </div>
                            {/each}

                            <div class="oc-footer">
                                <button
                                    class="opencode-btn import {opencodeState}"
                                    on:click={addToOpenCode}
                                    disabled={opencodeState === 'loading' || ocTotalSelected === 0}>
                                    {#if opencodeState === 'loading'}Importing…
                                    {:else if opencodeState === 'success'}✓ Imported
                                    {:else}Import {ocTotalSelected} model{ocTotalSelected !== 1 ? 's' : ''} to OpenCode{/if}
                                </button>
                            </div>
                        </div>
                    {/if}
                </div>

                <div class="quickstart-grid">
                    <div class="qs-card">
                        <div class="qs-title">Claude Code / Claw Code</div>
                        <pre class="qs-code">export ANTHROPIC_BASE_URL={BASE_URL}
export ANTHROPIC_API_KEY=freehive-&lt;model-id&gt;</pre>
                    </div>
                    <div class="qs-card">
                        <div class="qs-title">Cursor / Continue.dev (OpenAI format)</div>
                        <pre class="qs-code">Base URL: {BASE_URL}/v1
API Key:  freehive-&lt;model-id&gt;
Model:    (any — key determines the model)</pre>
                    </div>
                    <div class="qs-card">
                        <div class="qs-title">Python — Anthropic SDK</div>
                        <pre class="qs-code">import anthropic
client = anthropic.Anthropic(
    base_url="{BASE_URL}",
    api_key="freehive-claude-sonnet-4-6",
)</pre>
                    </div>
                    <div class="qs-card">
                        <div class="qs-title">Python — OpenAI SDK</div>
                        <pre class="qs-code">from openai import OpenAI
client = OpenAI(
    base_url="{BASE_URL}/v1",
    api_key="freehive-gpt-5.2",
)</pre>
                    </div>
                </div>
            </div>

        </div>
    {/if}

    {#if settingsTab === 'usage'}
        <div class="usage-page">
            {#if usageLoading}
                <div class="usage-loading">
                    <span class="usage-spinner"></span> Loading usage data...
                </div>
            {:else if usageError}
                <div class="usage-error">{usageError}</div>
            {:else if usageData}
                <h3 class="usage-section-header">Provider Quotas</h3>

                {#each [['claude', 'Claude'], ['chatgpt', 'ChatGPT'], ['gemini', 'Gemini']] as [key, label]}
                    {@const p = usageData[key]}
                    {#if p}
                        <div class="usage-card" style="border-left: 3px solid {PROVIDER_LABELS[key]?.color || 'var(--border-medium)'}">
                            <div class="usage-card-header">
                                <div class="usage-provider-info">
                                    <img class="usage-provider-logo" src="/logos/{key}.png" alt="{key}" />
                                    <div class="usage-provider-meta">
                                        <span class="usage-provider-name">{label}</span>
                                        {#if p.email}
                                            <span class="usage-email">{p.email}</span>
                                        {/if}
                                    </div>
                                </div>
                                <div class="usage-header-right">
                                    <span class="usage-tier-badge" style="background: {PROVIDER_LABELS[key]?.color}20; color: {PROVIDER_LABELS[key]?.color}">{p.tier}</span>
                                    <div class="usage-status-indicator">
                                        <span class="usage-status-dot {p.status}"></span>
                                        <span class="usage-status-text {p.status}">{p.status === 'connected' ? 'Connected' : p.status === 'expired' ? 'Expired' : p.status === 'error' ? 'Error' : 'Disconnected'}</span>
                                    </div>
                                </div>
                            </div>

                            {#if p.error && p.quotas.length === 0}
                                <div class="usage-card-error">{p.error}</div>
                            {:else if p.quotas.length > 0}
                                <div class="usage-quotas">
                                    {#each p.quotas as q}
                                        <div class="usage-quota-row">
                                            <div class="usage-quota-label">
                                                <span class="usage-quota-window">{q.label}</span>
                                                <span class="usage-quota-pct {q.remaining_pct < 15 ? 'low' : q.remaining_pct < 40 ? 'med' : ''}">{q.remaining_pct}% remaining</span>
                                            </div>
                                            <div class="usage-bar-track">
                                                <div class="usage-bar-fill {q.remaining_pct < 15 ? 'low' : q.remaining_pct < 40 ? 'med' : ''}"
                                                     style="width: {Math.max(2, 100 - q.remaining_pct)}%"></div>
                                            </div>
                                            {#if q.reset_label}
                                                <div class="usage-quota-detail">
                                                    <span class="usage-reset">{q.reset_label}</span>
                                                </div>
                                            {/if}
                                        </div>
                                    {/each}
                                </div>
                            {:else}
                                <div class="usage-card-empty">No remaining usage data available</div>
                            {/if}
                        </div>
                    {/if}
                {/each}

                <!-- Arena Context Windows -->
                {#if arenaUsageSessions.length > 0}
                    <div class="usage-section-divider"></div>
                    <h3 class="usage-section-header">
                        <img class="usage-section-icon" src="/logos/arena_header.png" alt="Arena" />
                        Arena Context Windows
                    </h3>

                    {#each arenaUsageSessions as session}
                        <div class="arena-session-card" class:inactive={!session.is_active}
                             style="border-left: 3px solid {session.is_active ? '#f97316' : 'var(--border-medium)'}">
                            <div class="arena-session-header">
                                <span class="arena-session-model">{session.model.replace('arena/', '')}</span>
                                <span class="arena-session-badge" class:active={session.is_active}>
                                    {session.is_active ? 'Active' : 'Ended'}
                                </span>
                            </div>
                            {#if session.title && session.title !== 'Untitled'}
                                <div class="arena-session-title">{session.title}</div>
                            {/if}
                            <div class="arena-context-gauge">
                                <div class="arena-gauge-labels">
                                    <span class="arena-gauge-tokens">
                                        {session.estimated_tokens.toLocaleString()} / {session.max_tokens.toLocaleString()} tokens
                                    </span>
                                    <span class="arena-gauge-pct" class:critical={session.usage_pct > 80} class:warning={session.usage_pct > 60 && session.usage_pct <= 80}>
                                        {session.usage_pct}%
                                    </span>
                                </div>
                                <div class="arena-gauge-track">
                                    <div class="arena-gauge-fill" class:critical={session.usage_pct > 80} class:warning={session.usage_pct > 60 && session.usage_pct <= 80}
                                         style="width: {Math.max(1, session.usage_pct)}%"></div>
                                </div>
                            </div>
                            <div class="arena-session-meta">
                                <span>{session.message_count} messages</span>
                                <span>{session.total_chars.toLocaleString()} chars</span>
                                <span>{formatTokens(session.max_tokens)} context</span>
                            </div>
                        </div>
                    {/each}
                {/if}
            {/if}

            <button class="usage-refresh-btn" on:click={loadUsage} disabled={usageLoading}>
                {usageLoading ? 'Refreshing...' : 'Refresh Usage'}
            </button>
        </div>
    {/if}

    {#if settingsTab === 'data'}
        <div class="data-page">

            <div class="data-section">
                <h3 class="data-heading">Export</h3>
                <div class="data-card">
                    <div class="data-info">
                        <span class="data-title">Export Conversations</span>
                        <span class="data-desc">Download all chat sessions and messages as a JSON file.</span>
                    </div>
                    <button class="data-btn" on:click={async () => {
                        try {
                            const { listChatSessions } = await import('$lib/api.js');
                            const sessions = await listChatSessions();
                            const blob = new Blob([JSON.stringify(sessions, null, 2)], { type: 'application/json' });
                            const url = URL.createObjectURL(blob);
                            const a = document.createElement('a');
                            a.href = url;
                            a.download = `freehive-export-${new Date().toISOString().slice(0, 10)}.json`;
                            a.click();
                            URL.revokeObjectURL(url);
                        } catch (e) {
                            alert('Export failed: ' + (e.message || e));
                        }
                    }}>
                        Export JSON
                    </button>
                </div>
            </div>

            <div class="data-section">
                <h3 class="danger-heading">Danger Zone</h3>
                <div class="danger-card">
                    <div class="data-info">
                        <span class="data-title">Clear All History</span>
                        <span class="data-desc">Delete all chat sessions and messages. This cannot be undone.</span>
                    </div>
                    <button class="danger-btn" on:click={() => showClearConfirm = true} disabled={clearingHistory}>
                        {clearingHistory ? 'Clearing...' : 'Clear History'}
                    </button>
                </div>
            </div>

        </div>
    {/if}

    {#if settingsTab === 'logs'}
        <div class="logs-page">
            <div class="logs-toolbar">
                <div class="logs-toolbar-left">
                    <span class="log-status" class:live={logConnected}>
                        {logConnected ? 'Live' : 'Disconnected'}
                    </span>
                    <span class="log-count">{logLines.length} lines</span>
                </div>
                <div class="logs-toolbar-right">
                    <label class="auto-scroll-label">
                        <input type="checkbox" bind:checked={autoScroll} />
                        Auto-scroll
                    </label>
                    <button class="logs-btn" on:click={clearLogView}>Clear View</button>
                    <button class="logs-btn" on:click={connectLogStream}>Reconnect</button>
                </div>
            </div>
            <pre class="log-viewer" bind:this={logContainer}>{logLines.join('\n')}</pre>
        </div>
    {/if}
</div>

{#if showClearConfirm}
    <div class="confirm-overlay" on:click|self={() => showClearConfirm = false}>
        <div class="confirm-modal">
            <h3 class="confirm-title">Clear all history?</h3>
            <p class="confirm-text">This will permanently delete all chat sessions, messages, and recreate a fresh database. This action cannot be undone.</p>
            <div class="confirm-actions">
                <button class="confirm-cancel" on:click={() => showClearConfirm = false}>Cancel</button>
                <button class="confirm-delete" on:click={async () => {
                    clearingHistory = true;
                    try {
                        await resetDatabase();
                        showClearConfirm = false;
                        dispatch('history-cleared');
                    } catch (e) {
                        alert('Failed to clear history: ' + (e.message || e));
                    } finally {
                        clearingHistory = false;
                    }
                }}>
                    {clearingHistory ? 'Deleting...' : 'Delete Everything'}
                </button>
            </div>
        </div>
    </div>
{/if}

<style>
    .settings-page {
        display: flex;
        flex-direction: column;
        height: 100%;
        overflow: hidden;
    }

    .settings-header {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 12px 16px 0;
    }
    .settings-header h2 { margin: 0; font-size: 16px; }

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

    .settings-tabs {
        display: flex;
        gap: 12px;
        padding: 16px 24px 0;
        border-bottom: 1px solid var(--border-light);
    }

    .tab-btn {
        padding: 10px 16px;
        border: none;
        background: transparent;
        color: var(--text-secondary);
        font-size: 14px;
        font-weight: 500;
        cursor: pointer;
        border-bottom: 2px solid transparent;
        margin-bottom: -1px;
        transition: color 0.15s, border-color 0.15s;
    }

    .tab-btn:hover:not(:disabled) { color: var(--text-primary); }
    .tab-btn.active { color: var(--text-primary); border-bottom-color: var(--text-primary); }
    .tab-btn.disabled { opacity: 0.4; cursor: not-allowed; }

    .apikeys-page {
        flex: 1;
        overflow-y: auto;
        padding: 24px;
        display: flex;
        flex-direction: column;
        gap: 32px;
    }

    /* Info block */
    .info-block {
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        border-radius: 12px;
        padding: 20px;
        display: flex;
        flex-direction: column;
        gap: 12px;
    }

    .info-row {
        display: flex;
        align-items: center;
        gap: 12px;
    }

    .info-label {
        font-size: 13px;
        font-weight: 500;
        color: var(--text-secondary);
        min-width: 70px;
    }

    .info-value {
        font-size: 14px;
        color: var(--accent-color);
        background: var(--bg-tertiary);
        padding: 4px 10px;
        border-radius: 6px;
    }

    .info-hint {
        font-size: 13px;
        color: var(--text-muted);
        margin: 0;
        line-height: 1.5;
    }

    /* Keys section */
    .keys-section {
        display: flex;
        flex-direction: column;
        gap: 16px;
    }

    .keys-heading {
        font-size: 14px;
        font-weight: 600;
        color: var(--text-primary);
        margin: 0;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }

    .keys-subheading {
        font-size: 13px;
        color: var(--text-secondary);
        margin: -8px 0 0;
        line-height: 1.5;
    }

    /* Search & filter toolbar */
    .keys-toolbar {
        display: flex;
        flex-direction: column;
        gap: 10px;
    }

    .search-box {
        display: flex;
        align-items: center;
        gap: 8px;
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        border-radius: 8px;
        padding: 8px 12px;
        transition: border-color 0.15s;
    }

    .search-box:focus-within {
        border-color: var(--accent-color);
    }

    .search-icon {
        color: var(--text-muted);
        flex-shrink: 0;
    }

    .search-input {
        flex: 1;
        background: transparent;
        border: none;
        outline: none;
        color: var(--text-primary);
        font-size: 14px;
        font-family: inherit;
    }

    .search-input::placeholder {
        color: var(--text-muted);
    }

    .search-clear {
        background: var(--bg-tertiary);
        border: none;
        color: var(--text-secondary);
        font-size: 14px;
        cursor: pointer;
        padding: 2px 6px;
        border-radius: 4px;
        line-height: 1;
    }

    .search-clear:hover {
        color: var(--text-primary);
    }

    .filter-chips {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
    }

    .filter-chip {
        padding: 4px 10px;
        border-radius: 12px;
        border: 1px solid var(--border-medium);
        background: var(--bg-secondary);
        color: var(--text-secondary);
        font-size: 12px;
        font-weight: 500;
        cursor: pointer;
        transition: all 0.15s;
        white-space: nowrap;
    }

    .filter-chip:hover {
        border-color: var(--chip-fg, var(--text-muted));
        color: var(--chip-fg, var(--text-primary));
    }

    .filter-chip.active {
        background: var(--chip-bg, var(--bg-tertiary));
        color: var(--chip-fg, var(--text-primary));
        border-color: var(--chip-fg, var(--accent-color));
    }

    .clear-all-chip {
        color: #ef4444;
        border-color: rgba(239, 68, 68, 0.3);
    }

    .clear-all-chip:hover {
        color: #ef4444;
        border-color: #ef4444;
        background: rgba(239, 68, 68, 0.08);
    }

    .no-results {
        font-size: 14px;
        color: var(--text-muted);
        text-align: center;
        padding: 24px 0;
    }

    .provider-group {
        border: 1px solid var(--border-medium);
        border-radius: 12px;
        overflow: hidden;
    }

    .provider-header {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 12px 16px;
        background: var(--bg-secondary);
        border: none;
        border-bottom: 1px solid var(--border-medium);
        width: 100%;
        text-align: left;
        font-family: inherit;
        cursor: pointer;
        transition: background 0.15s;
    }

    .provider-header:hover {
        background: var(--bg-tertiary);
    }

    .model-count {
        font-size: 12px;
        color: var(--text-muted);
        margin-left: auto;
    }

    .collapse-arrow {
        font-size: 12px;
        color: var(--text-muted);
        transition: transform 0.2s;
        margin-left: 4px;
    }

    .collapse-arrow.collapsed {
        transform: rotate(-90deg);
    }

    .provider-logo {
        width: 22px;
        height: 22px;
        border-radius: 5px;
        object-fit: contain;
        flex-shrink: 0;
    }

    .provider-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex-shrink: 0;
    }

    .provider-name {
        font-size: 14px;
        font-weight: 600;
        color: var(--text-primary);
    }

    .tier-pill {
        font-size: 11px;
        color: var(--accent-color);
        background: var(--bg-tertiary);
        padding: 2px 8px;
        border-radius: 12px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        font-weight: 500;
    }

    .model-key-list {
        display: flex;
        flex-direction: column;
    }

    .model-key-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 12px 16px;
        border-bottom: 1px solid var(--border-light);
        transition: background 0.15s;
    }

    .model-key-row:last-child { border-bottom: none; }
    .model-key-row:hover { background: var(--bg-tertiary); }

    .model-info {
        display: flex;
        align-items: center;
        gap: 10px;
        min-width: 0;
    }

    .model-name {
        font-size: 14px;
        color: var(--text-primary);
        white-space: nowrap;
    }

    .model-note {
        font-size: 12px;
        padding: 2px 8px;
        border-radius: 4px;
        white-space: nowrap;
        font-weight: 500;
    }

    .key-area {
        display: flex;
        align-items: center;
        gap: 10px;
        flex-shrink: 0;
    }

    .key-value {
        font-size: 13px;
        color: var(--text-secondary);
        background: var(--bg-secondary);
        padding: 6px 10px;
        border-radius: 6px;
        border: 1px solid var(--border-medium);
        white-space: nowrap;
        max-width: 340px;
        overflow: hidden;
        text-overflow: ellipsis;
    }

    /* Copy buttons */
    .copy-btn {
        padding: 6px 12px;
        border-radius: 6px;
        border: 1px solid var(--border-medium);
        background: var(--bg-secondary);
        color: var(--text-secondary);
        font-size: 13px;
        font-weight: 500;
        cursor: pointer;
        white-space: nowrap;
        transition: all 0.2s;
    }

    .copy-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); border-color: var(--text-muted); }
    .copy-btn.copied { background: var(--bg-tertiary); color: var(--accent-color); border-color: var(--accent-color); }
    .copy-btn.small { padding: 4px 10px; font-size: 12px; }

    /* Quickstart */
    .quickstart {
        display: flex;
        flex-direction: column;
        gap: 16px;
        padding-bottom: 24px;
    }

    .opencode-card {
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        border-radius: 12px;
        overflow: hidden;
    }

    .opencode-card-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        padding: 16px 20px;
        cursor: pointer;
        transition: background 0.15s;
    }

    .opencode-card-header:hover { background: var(--bg-tertiary); }

    .opencode-left {
        display: flex;
        flex-direction: column;
        gap: 4px;
    }

    .opencode-title {
        font-size: 14px;
        font-weight: 600;
        color: var(--text-primary);
    }

    .opencode-desc {
        font-size: 13px;
        color: var(--text-secondary);
        line-height: 1.5;
    }

    .opencode-desc code {
        font-size: 12px;
        color: var(--accent-color);
        background: var(--bg-tertiary);
        padding: 1px 5px;
        border-radius: 4px;
    }

    .opencode-right {
        display: flex;
        align-items: center;
        gap: 12px;
        flex-shrink: 0;
    }

    .opencode-feedback { font-size: 13px; }
    .opencode-feedback.success { color: var(--accent-color); }
    .opencode-feedback.error   { color: #e05c5c; }

    .opencode-panel {
        border-top: 1px solid var(--border-medium);
        padding: 16px 20px;
        display: flex;
        flex-direction: column;
        gap: 12px;
    }

    .oc-provider {
        border: 1px solid var(--border-medium);
        border-radius: 10px;
        overflow: hidden;
    }

    .oc-provider-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 10px 14px;
        background: var(--bg-tertiary);
    }

    .oc-provider-check {
        display: flex;
        align-items: center;
        gap: 10px;
        cursor: pointer;
        font-size: 13px;
        font-weight: 600;
    }

    .oc-provider-check input[type="checkbox"] {
        width: 16px;
        height: 16px;
        accent-color: var(--accent-color);
        cursor: pointer;
    }

    .oc-provider-logo {
        width: 18px;
        height: 18px;
        border-radius: 4px;
        object-fit: contain;
    }

    .oc-provider-name { font-size: 13px; }

    .oc-provider-count {
        font-size: 12px;
        color: var(--text-muted);
        font-weight: 400;
        margin-left: 4px;
    }

    .oc-provider-actions {
        display: flex;
        align-items: center;
        gap: 6px;
    }

    .oc-link-btn {
        background: none;
        border: none;
        color: var(--text-secondary);
        font-size: 12px;
        cursor: pointer;
        padding: 2px 4px;
    }
    .oc-link-btn:hover { color: var(--accent-color); }

    .oc-separator { color: var(--text-muted); font-size: 12px; }

    .oc-arena-search {
        position: relative;
        padding: 8px 14px 0;
    }

    .oc-arena-input {
        width: 100%;
        padding: 6px 28px 6px 10px;
        background: var(--bg-primary);
        border: 1px solid var(--border-medium);
        border-radius: 6px;
        color: var(--text-primary);
        font-size: 12px;
        outline: none;
    }
    .oc-arena-input:focus { border-color: var(--accent-color); }

    .oc-arena-search .search-clear {
        position: absolute;
        right: 20px;
        top: 50%;
        transform: translateY(-25%);
    }

    .oc-model-list {
        display: flex;
        flex-direction: column;
        max-height: 220px;
        overflow-y: auto;
        padding: 6px 14px 8px;
    }

    .oc-model-item {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 4px 0;
        cursor: pointer;
        font-size: 12px;
        color: var(--text-primary);
    }

    .oc-model-item input[type="checkbox"] {
        width: 14px;
        height: 14px;
        accent-color: var(--accent-color);
        cursor: pointer;
        flex-shrink: 0;
    }

    .oc-model-name {
        flex: 1;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .oc-footer {
        display: flex;
        justify-content: flex-end;
        padding-top: 4px;
    }

    .opencode-btn {
        padding: 8px 18px;
        border-radius: 8px;
        border: 1px solid var(--border-medium);
        background: var(--bg-tertiary);
        color: var(--text-primary);
        font-size: 13px;
        font-weight: 500;
        cursor: pointer;
        white-space: nowrap;
        transition: all 0.2s;
    }

    .opencode-btn.import {
        background: var(--accent-color);
        border-color: var(--accent-color);
        color: #000;
    }
    .opencode-btn.import:hover:not(:disabled) { opacity: 0.9; }
    .opencode-btn:hover:not(:disabled) { border-color: var(--accent-color); color: var(--accent-color); }
    .opencode-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .opencode-btn.success { border-color: var(--accent-color); color: var(--accent-color); background: var(--bg-tertiary); }

    .quickstart-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 16px;
    }

    .qs-card {
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        border-radius: 12px;
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 10px;
    }

    .qs-title {
        font-size: 13px;
        font-weight: 600;
        color: var(--text-secondary);
    }

    .qs-code {
        font-size: 12px;
        color: var(--accent-color);
        background: var(--bg-primary);
        padding: 12px;
        border-radius: 8px;
        margin: 0;
        white-space: pre-wrap;
        line-height: 1.6;
        overflow-x: auto;
    }

    /* Usage tab */
    /* Usage tab */
    .usage-page {
        display: flex;
        flex-direction: column;
        gap: 16px;
        padding: 32px;
        overflow-y: auto;
        flex: 1;
    }
    .usage-section-header {
        font-size: 13px;
        font-weight: 600;
        color: var(--text-secondary);
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin: 0;
        display: flex;
        align-items: center;
        gap: 8px;
    }
    .usage-section-icon {
        width: 18px; height: 18px;
        border-radius: 4px;
        object-fit: cover;
    }
    .usage-section-divider {
        border: none;
        border-top: 1px solid var(--border-light);
        margin: 8px 0;
    }
    .usage-loading {
        display: flex;
        align-items: center;
        gap: 8px;
        color: var(--text-muted);
        font-size: 13px;
        padding: 24px 0;
    }
    .usage-spinner {
        width: 16px; height: 16px;
        border: 2px solid var(--border-medium);
        border-top-color: var(--accent-color);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
    }
    .usage-error {
        color: #ef4444;
        font-size: 13px;
        padding: 16px;
        background: #ef444410;
        border-radius: 8px;
    }
    .usage-card {
        border: 1px solid var(--border-medium);
        border-radius: 12px;
        padding: 16px;
        background: var(--bg-secondary);
        display: flex;
        flex-direction: column;
        gap: 12px;
    }
    .usage-card-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
    }
    .usage-provider-info {
        display: flex;
        align-items: center;
        gap: 10px;
    }
    .usage-provider-logo {
        width: 24px; height: 24px;
        border-radius: 50%;
        object-fit: cover;
        border: 1px solid var(--border-light);
    }
    .usage-provider-meta {
        display: flex;
        flex-direction: column;
        gap: 1px;
    }
    .usage-provider-name {
        font-size: 14px;
        font-weight: 600;
        color: var(--text-primary);
    }
    .usage-email {
        font-size: 11px;
        color: var(--text-muted);
    }
    .usage-header-right {
        display: flex;
        align-items: center;
        gap: 10px;
    }
    .usage-tier-badge {
        font-size: 10px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.3px;
        padding: 2px 8px;
        border-radius: 10px;
    }
    .usage-status-indicator {
        display: flex;
        align-items: center;
        gap: 5px;
    }
    .usage-status-dot {
        width: 7px; height: 7px;
        border-radius: 50%;
        flex-shrink: 0;
    }
    .usage-status-dot.connected { background: #22c55e; }
    .usage-status-dot.expired { background: #ef4444; }
    .usage-status-dot.disconnected { background: var(--text-muted); }
    .usage-status-dot.error { background: #f59e0b; }
    .usage-status-text {
        font-size: 10px;
        color: var(--text-muted);
    }
    .usage-status-text.connected { color: #22c55e; }
    .usage-status-text.expired { color: #ef4444; }
    .usage-card-error {
        font-size: 12px;
        color: var(--text-muted);
        font-style: italic;
    }
    .usage-card-empty {
        font-size: 12px;
        color: var(--text-muted);
    }
    .usage-quotas {
        display: flex;
        flex-direction: column;
        gap: 10px;
    }
    .usage-quota-row {
        display: flex;
        flex-direction: column;
        gap: 4px;
    }
    .usage-quota-label {
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
    .usage-quota-window {
        font-size: 12px;
        font-weight: 500;
        color: var(--text-secondary);
        text-transform: capitalize;
    }
    .usage-quota-pct {
        font-size: 11px;
        color: var(--text-muted);
    }
    .usage-quota-pct.low { color: #ef4444; }
    .usage-quota-pct.med { color: #f59e0b; }
    .usage-bar-track {
        height: 8px;
        border-radius: 4px;
        background: var(--bg-tertiary);
        overflow: hidden;
    }
    .usage-bar-fill {
        height: 100%;
        border-radius: 4px;
        background: var(--accent-color);
        transition: width 0.3s ease;
    }
    .usage-bar-fill.med { background: #f59e0b; }
    .usage-bar-fill.low { background: #ef4444; }
    .usage-quota-detail {
        display: flex;
        justify-content: space-between;
        font-size: 11px;
        color: var(--text-muted);
    }
    .usage-reset {
        font-style: italic;
    }
    .usage-refresh-btn {
        align-self: flex-start;
        padding: 8px 16px;
        border: 1px solid var(--border-medium);
        border-radius: 8px;
        background: transparent;
        color: var(--text-secondary);
        font-size: 12px;
        cursor: pointer;
        transition: background 0.15s;
    }
    .usage-refresh-btn:hover { background: var(--bg-tertiary); }
    .usage-refresh-btn:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Arena context window cards */
    .arena-session-card {
        border: 1px solid var(--border-medium);
        border-radius: 12px;
        padding: 14px 16px;
        background: var(--bg-secondary);
        display: flex;
        flex-direction: column;
        gap: 8px;
        transition: opacity 0.2s;
    }
    .arena-session-card.inactive {
        opacity: 0.6;
    }
    .arena-session-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
    }
    .arena-session-model {
        font-size: 13px;
        font-weight: 600;
        color: var(--text-primary);
        font-family: 'SF Mono', 'Fira Code', monospace;
    }
    .arena-session-badge {
        font-size: 9px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.4px;
        padding: 2px 8px;
        border-radius: 10px;
        background: var(--bg-tertiary);
        color: var(--text-muted);
    }
    .arena-session-badge.active {
        background: rgba(249, 115, 22, 0.15);
        color: #f97316;
    }
    .arena-session-title {
        font-size: 12px;
        color: var(--text-muted);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .arena-context-gauge {
        display: flex;
        flex-direction: column;
        gap: 4px;
    }
    .arena-gauge-labels {
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
    .arena-gauge-tokens {
        font-size: 12px;
        color: var(--text-secondary);
    }
    .arena-gauge-pct {
        font-size: 12px;
        font-weight: 600;
        color: var(--accent-color);
    }
    .arena-gauge-pct.warning { color: #f59e0b; }
    .arena-gauge-pct.critical { color: #ef4444; }
    .arena-gauge-track {
        height: 10px;
        border-radius: 5px;
        background: var(--bg-tertiary);
        overflow: hidden;
    }
    .arena-gauge-fill {
        height: 100%;
        border-radius: 5px;
        background: #f97316;
        transition: width 0.3s ease;
    }
    .arena-gauge-fill.warning { background: #f59e0b; }
    .arena-gauge-fill.critical { background: #ef4444; }
    .arena-session-meta {
        display: flex;
        gap: 12px;
        font-size: 11px;
        color: var(--text-muted);
    }

    /* Data tab */
    .data-page {
        display: flex;
        flex-direction: column;
        gap: 32px;
        padding: 32px;
        overflow-y: auto;
        flex: 1;
    }
    .data-section {
        display: flex;
        flex-direction: column;
        gap: 12px;
    }
    .data-heading {
        font-size: 13px;
        font-weight: 600;
        color: var(--text-primary);
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .data-card {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 16px;
        border: 1px solid var(--border-medium);
        border-radius: 10px;
        background: var(--bg-secondary);
    }
    .data-info {
        display: flex;
        flex-direction: column;
        gap: 4px;
    }
    .data-title {
        font-size: 13px;
        font-weight: 600;
        color: var(--text-primary);
    }
    .data-desc {
        font-size: 12px;
        color: var(--text-muted);
    }
    .data-btn {
        padding: 8px 16px;
        border: 1px solid var(--border-medium);
        border-radius: 8px;
        background: transparent;
        color: var(--text-primary);
        font-size: 12px;
        font-weight: 500;
        cursor: pointer;
        transition: background 0.15s;
        white-space: nowrap;
    }
    .data-btn:hover { background: var(--bg-tertiary); }

    .danger-heading {
        font-size: 13px;
        font-weight: 600;
        color: #ef4444;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-bottom: 12px;
    }
    .danger-card {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 16px;
        border: 1px solid #ef444433;
        border-radius: 10px;
        background: #ef444408;
    }
    .danger-info {
        display: flex;
        flex-direction: column;
        gap: 4px;
    }
    .danger-title {
        font-size: 13px;
        font-weight: 600;
        color: var(--text-primary);
    }
    .danger-desc {
        font-size: 12px;
        color: var(--text-muted);
    }
    .danger-btn {
        padding: 8px 16px;
        border: 1px solid #ef4444;
        border-radius: 8px;
        background: transparent;
        color: #ef4444;
        font-size: 12px;
        font-weight: 500;
        cursor: pointer;
        transition: background 0.15s, color 0.15s;
        white-space: nowrap;
    }
    .danger-btn:hover { background: #ef4444; color: #fff; }
    .danger-btn:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Confirmation modal */
    .confirm-overlay {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.5);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 1000;
    }
    .confirm-modal {
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        border-radius: 14px;
        padding: 24px;
        max-width: 400px;
        width: 90%;
        display: flex;
        flex-direction: column;
        gap: 12px;
    }
    .confirm-title {
        font-size: 16px;
        font-weight: 600;
        color: var(--text-primary);
        margin: 0;
    }
    .confirm-text {
        font-size: 13px;
        color: var(--text-secondary);
        line-height: 1.5;
        margin: 0;
    }
    .confirm-actions {
        display: flex;
        justify-content: flex-end;
        gap: 8px;
        margin-top: 8px;
    }
    .confirm-cancel {
        padding: 8px 16px;
        border: 1px solid var(--border-medium);
        border-radius: 8px;
        background: transparent;
        color: var(--text-secondary);
        font-size: 13px;
        cursor: pointer;
        transition: background 0.15s;
    }
    .confirm-cancel:hover { background: var(--bg-tertiary); }
    .confirm-delete {
        padding: 8px 16px;
        border: none;
        border-radius: 8px;
        background: #ef4444;
        color: #fff;
        font-size: 13px;
        font-weight: 500;
        cursor: pointer;
        transition: opacity 0.15s;
    }
    .confirm-delete:hover { opacity: 0.9; }

    /* ─── Arena card grid ─── */
    .arena-content {
        display: flex;
        flex-direction: column;
        gap: 14px;
        padding: 16px;
    }

    .arena-search-bar {
        display: flex;
        align-items: center;
        gap: 10px;
    }

    .arena-search-bar .search-box { flex: 1; }

    .arena-result-count {
        font-size: 12px;
        color: var(--text-muted);
        white-space: nowrap;
    }

    .arena-card-grid {
        display: flex;
        flex-wrap: wrap;
        justify-content: center;
        gap: 10px;
    }

    .arena-provider-card {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 8px;
        padding: 16px 10px 12px;
        width: 120px;
        background: var(--bg-primary);
        border: 1px solid var(--border-light);
        border-radius: 10px;
        cursor: pointer;
        transition: all 0.18s ease;
    }

    .arena-provider-card:hover {
        border-color: var(--text-muted);
        background: var(--bg-tertiary);
        transform: translateY(-2px);
        box-shadow: 0 4px 12px rgba(0,0,0,0.12);
    }

    .arena-provider-card.expanded {
        border-color: var(--accent-color);
        background: var(--accent-muted, rgba(62,207,142,0.06));
        box-shadow: 0 0 0 1px var(--accent-color);
        transform: translateY(0);
    }

    .arena-card-logo {
        width: 40px;
        height: 40px;
        border-radius: 8px;
        object-fit: contain;
        background: var(--bg-tertiary);
        padding: 3px;
    }

    .arena-card-name {
        font-size: 13px;
        font-weight: 600;
        color: var(--text-primary);
        white-space: nowrap;
    }

    .arena-provider-card.expanded .arena-card-name { color: var(--accent-color); }

    .arena-card-count {
        font-size: 10px;
        color: var(--text-muted);
        background: var(--bg-tertiary);
        padding: 2px 8px;
        border-radius: 8px;
    }

    .arena-provider-card.expanded .arena-card-count {
        background: rgba(62, 207, 142, 0.2);
        color: var(--accent-color);
    }

    /* Expanded panel */
    .arena-expanded-panel {
        background: var(--bg-primary);
        border: 1px solid var(--border-medium);
        border-radius: 10px;
        overflow: hidden;
        animation: arenaSlideDown 0.15s ease;
    }

    @keyframes arenaSlideDown {
        from { opacity: 0; transform: translateY(-4px); }
        to { opacity: 1; transform: translateY(0); }
    }

    .arena-expanded-header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 10px 14px;
        border-bottom: 1px solid var(--border-light);
    }

    .arena-expanded-logo {
        width: 20px;
        height: 20px;
        border-radius: 5px;
        object-fit: contain;
        background: var(--bg-tertiary);
        padding: 1px;
    }

    .arena-expanded-title {
        font-size: 13px;
        font-weight: 600;
        color: var(--text-primary);
    }

    .arena-expanded-count {
        font-size: 11px;
        color: var(--text-muted);
        margin-right: auto;
    }

    .arena-expanded-close {
        background: none;
        border: none;
        color: var(--text-muted);
        cursor: pointer;
        padding: 4px 8px;
        border-radius: 4px;
        font-size: 16px;
        line-height: 1;
        transition: color 0.15s;
    }

    .arena-expanded-close:hover {
        color: var(--text-primary);
        background: var(--bg-tertiary);
    }

    .arena-expanded-list {
        max-height: 350px;
        overflow-y: auto;
    }

    .arena-expanded-list::-webkit-scrollbar { width: 4px; }
    .arena-expanded-list::-webkit-scrollbar-thumb { background: var(--border-medium); border-radius: 2px; }

    /* Other providers */
    .arena-other-section {
        border: 1px solid var(--border-light);
        border-radius: 8px;
        overflow: hidden;
        background: var(--bg-primary);
    }

    .arena-other-header {
        display: flex;
        align-items: center;
        gap: 8px;
        width: 100%;
        padding: 10px 14px;
        background: transparent;
        border: none;
        color: var(--text-secondary);
        font-size: 13px;
        font-weight: 500;
        cursor: pointer;
        transition: background 0.15s;
    }

    .arena-other-header:hover { background: var(--bg-tertiary); }

    .arena-other-chevron {
        font-size: 10px;
        color: var(--text-muted);
        transition: transform 0.2s;
    }

    .arena-other-chevron.open { transform: rotate(180deg); }

    .arena-other-chips {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        padding: 12px 14px 14px;
        border-top: 1px solid var(--border-light);
        justify-content: center;
    }

    .arena-other-chip {
        display: flex;
        align-items: center;
        gap: 6px;
        background: var(--bg-secondary);
        border: 1px solid var(--border-medium);
        border-radius: 8px;
        padding: 8px 14px;
        font-size: 13px;
        color: var(--text-secondary);
        cursor: pointer;
        transition: all 0.15s;
    }

    .arena-other-chip:hover {
        border-color: var(--text-muted);
        color: var(--text-primary);
        background: var(--bg-tertiary);
    }

    .arena-chip-count {
        font-size: 10px;
        color: var(--text-muted);
        background: var(--bg-tertiary);
        padding: 1px 6px;
        border-radius: 8px;
    }

    /* Arena filter pills */
    .arena-filter-pills {
        display: flex;
        gap: 6px;
        flex-wrap: wrap;
    }

    .arena-filter-pill {
        display: flex;
        align-items: center;
        gap: 5px;
        background: transparent;
        border: 1px solid var(--border-medium);
        border-radius: 6px;
        padding: 4px 10px;
        font-size: 12px;
        color: var(--text-secondary);
        cursor: pointer;
        transition: all 0.15s;
    }

    .arena-filter-pill:hover {
        border-color: var(--text-muted);
        color: var(--text-primary);
    }

    .arena-filter-pill.active {
        background: var(--accent-muted, rgba(62,207,142,0.08));
        border-color: var(--accent-color);
        color: var(--accent-color);
    }

    .arena-filter-count {
        font-size: 10px;
        color: var(--text-muted);
    }

    .arena-filter-pill.active .arena-filter-count {
        color: var(--accent-color);
        opacity: 0.7;
    }

    .arena-tag {
        font-size: 10px;
        padding: 1px 5px;
        border-radius: 3px;
        font-weight: 500;
    }

    .arena-tag-chat { background: rgba(66,133,244,0.15); color: #4285f4; }
    .arena-tag-code { background: rgba(99,102,241,0.15); color: #818cf8; }
    .arena-tag-search { background: rgba(34,197,94,0.15); color: #22c55e; }

    /* ── Backend Logs tab ──────────────────────────────────────────────── */
    .logs-page {
        display: flex;
        flex-direction: column;
        flex: 1;
        min-height: 0;
        gap: 8px;
    }

    .logs-toolbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 8px 12px;
        background: var(--bg-secondary);
        border: 1px solid var(--border-light);
        border-radius: 8px;
        flex-shrink: 0;
    }

    .logs-toolbar-left, .logs-toolbar-right {
        display: flex;
        align-items: center;
        gap: 10px;
    }

    .log-status {
        font-size: 11px;
        font-weight: 600;
        padding: 2px 8px;
        border-radius: 10px;
        background: rgba(239, 68, 68, 0.15);
        color: #ef4444;
    }

    .log-status.live {
        background: rgba(34, 197, 94, 0.15);
        color: #22c55e;
    }

    .log-count {
        font-size: 11px;
        color: var(--text-muted);
    }

    .auto-scroll-label {
        font-size: 12px;
        color: var(--text-secondary);
        display: flex;
        align-items: center;
        gap: 4px;
        cursor: pointer;
    }

    .logs-btn {
        font-size: 11px;
        padding: 4px 10px;
        border: 1px solid var(--border-medium);
        border-radius: 6px;
        background: var(--bg-tertiary);
        color: var(--text-secondary);
        cursor: pointer;
    }

    .logs-btn:hover {
        background: var(--bg-hover);
        color: var(--text-primary);
    }

    .log-viewer {
        flex: 1;
        min-height: 0;
        background: #0d1117;
        color: #c9d1d9;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
        font-size: 11.5px;
        line-height: 1.5;
        padding: 12px 14px;
        border-radius: 8px;
        border: 1px solid var(--border-light);
        overflow-y: auto;
        white-space: pre-wrap;
        word-break: break-all;
        margin: 0;
    }
</style>
