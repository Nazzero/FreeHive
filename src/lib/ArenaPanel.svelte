<script>
    import { createEventDispatcher, onMount } from 'svelte';
    import { getArenaStatus, getArenaModels, getArenaHealth, probeArenaModels, startArena, logoutArena, showArenaBrowser, setupArena, getArenaChromeStatus, refreshArenaModels, getArenaExtensionPath, openArenaExtensionFolder, getArenaExtensionIds, setArenaExtensionId } from '$lib/api.js';
    import { availableModels, selectedModel } from '$lib/store.js';

    const dispatch = createEventDispatcher();

    /** @type {any} */
    let status = null;
    let models = [];
    let loading = true;
    let modelsLoading = false;
    let modelsRefreshing = false;
    let error = '';
    /** @type {Record<string, string[]>} */
    let modelCapabilities = {};
    /** @type {Record<string, string[]>} */
    let modelModes = {};
    let chatCount = 0;
    let codeCount = 0;
    let searchCount = 0;
    let loginLoading = false;
    let loginMessage = '';
    let logoutLoading = false;
    let showBrowserLoading = false;
    let showBrowserMessage = '';
    let setupLoading = false;
    let setupMessage = '';
    let extensionPath = '';
    let extensionExists = false;
    let openFolderLoading = false;
    let setupStep = 1;          // tracks which step user is on (1, 2, 3)
    let checkingConnection = false;
    let connectionMessage = '';
    let installMode = 'unpacked';       // 'webstore' | 'unpacked'
    let unpackedExtensionId = '';
    let settingExtensionId = false;
    let extensionIdMessage = '';
    let registeredUnpackedId = null;

    // Probe state
    let probing = false;
    let probeProgress = 0;      // 0-100
    let probeTotal = 0;
    let probeCurrent = '';
    let probeAborted = false;
    /** @type {Record<string, {health: string, error?: string, preview?: string}>} */
    let probeResults = {};       // model id → result
    /** @type {any} */
    let probeSummary = null;

    const HEALTH_LABEL = {
        working:     { text: 'Working',      color: '#22c55e' },
        unavailable: { text: 'Unavailable',  color: '#ef4444' },
        private:     { text: 'Private only', color: '#ef4444' },
        rate_limited:{ text: 'Rate limited', color: '#f59e0b' },
        recaptcha:   { text: 'reCAPTCHA',   color: '#f59e0b' },
        timeout:     { text: 'Timeout',      color: '#f59e0b' },
        error:       { text: 'Error',        color: '#ef4444' },
    };

    onMount(async () => {
        // Fetch extension path + registered IDs in parallel with status
        getArenaExtensionPath().then((data) => {
            extensionPath = data?.path || '';
            extensionExists = data?.exists || false;
        }).catch(() => {});
        getArenaExtensionIds().then((data) => {
            registeredUnpackedId = data?.unpacked_id || null;
            if (registeredUnpackedId) unpackedExtensionId = registeredUnpackedId;
        }).catch(() => {});
        await fetchStatus();
        if (status?.bridge_active || status?.browser_available || status?.steel_available) {
            await loadStoredHealth();
        }
    });

    async function fetchStatus() {
        loading = true;
        error = '';
        try {
            status = await getArenaStatus();
            const isAvailable = status?.bridge_active || status?.browser_available || status?.steel_available;
            const isAuth = status?.authenticated || status?.bridge_active; // Extension = already authed
            if (isAvailable && isAuth) {
                await fetchModels();
            }
        } catch (e) {
            error = /** @type {any} */ (e)?.message || 'Failed to check Arena status.';
        } finally {
            loading = false;
        }
    }

    async function fetchModels() {
        modelsLoading = true;
        error = '';
        try {
            const data = await getArenaModels();
            _applyModelData(data);
        } catch (e) {
            error = /** @type {any} */ (e)?.message || 'Failed to load Arena models.';
        } finally {
            modelsLoading = false;
        }
    }

    async function handleRefreshModels() {
        modelsRefreshing = true;
        error = '';
        try {
            const data = await refreshArenaModels();
            _applyModelData(data);
        } catch (e) {
            error = /** @type {any} */ (e)?.message || 'Failed to refresh models.';
        } finally {
            modelsRefreshing = false;
        }
    }

    /** @param {string} raw */
    function _ensureArenaPrefix(raw) {
        const s = String(raw || '').trim();
        return s.startsWith('arena/') ? s : `arena/${s}`;
    }

    /** @param {any} data */
    function _applyModelData(data) {
        // Handle both old format (models: [{id, display_name}]) and new cache format
        // Always ensure arena/ prefix on model IDs
        const allModels = data.all_models || data.chat_models || [];
        if (allModels.length > 0) {
            models = allModels.map((/** @type {string} */ m) => _ensureArenaPrefix(typeof m === 'string' ? m : (m.id || '')));
        } else if (Array.isArray(data.models)) {
            models = data.models.map((/** @type {any} */ m) => _ensureArenaPrefix(typeof m === 'string' ? m : (m.id || '')));
        }
        modelCapabilities = data.capabilities || {};
        modelModes = data.model_modes || {};
        chatCount = data.chat_count || 0;
        codeCount = data.code_count || 0;
        searchCount = data.search_count || 0;
        if (models.length > 0) {
            syncModelsToStore(models);
        }
    }

    /** @param {any[]} list */
    function syncModelsToStore(list) {
        availableModels.update((current) => {
            const formatted = list.map((m) => {
                const raw = typeof m === 'string' ? m : (m.id || '');
                const displayName = typeof m === 'object' && m.display_name
                    ? m.display_name
                    : raw.replace('arena/', '');
                return { id: raw, display_name: displayName, note: m.note || '' };
            });
            return { ...current, arena: { tier: 'free', models: formatted } };
        });
    }

    async function loadStoredHealth() {
        try {
            const data = await getArenaHealth();
            const stored = data?.models || {};
            /** @type {Record<string, any>} */
            const merged = {};
            for (const [slug, entry] of Object.entries(stored)) {
                if (!entry || typeof entry !== 'object') continue;
                const st = entry.status || 'unknown';
                if (st === 'unknown') continue;
                const modelId = `arena/${slug}`;
                merged[modelId] = {
                    health: st === 'verified' ? 'working' : st,
                    error: entry.last_error || '',
                    preview: '',
                };
            }
            probeResults = { ...merged, ...probeResults };
        } catch { /* non-fatal */ }
    }

    async function startProbe() {
        if (probing) return;
        probing = true;
        probeAborted = false;
        probeProgress = 0;
        probeTotal = 0;
        probeCurrent = '';
        probeSummary = null;
        error = '';

        try {
            await probeArenaModels((ev) => {
                if (probeAborted) return;
                if (ev.status === 'starting') {
                    probeTotal = ev.total || 0;
                } else if (ev.status === 'probing') {
                    probeCurrent = ev.model || '';
                    probeProgress = probeTotal > 0
                        ? Math.round(((ev.index || 0) / probeTotal) * 100)
                        : 0;
                } else if (ev.status === 'result') {
                    const id = ev.model || '';
                    probeResults = {
                        ...probeResults,
                        [id]: { health: ev.health || 'error', error: ev.error || '', preview: ev.preview || '' },
                    };
                    probeProgress = probeTotal > 0
                        ? Math.round((((ev.index || 0) + 1) / probeTotal) * 100)
                        : probeProgress;
                } else if (ev.status === 'done') {
                    probeSummary = ev.summary;
                    probeProgress = 100;
                    probeCurrent = '';
                    if (probeSummary?.working?.length > 0) {
                        const workingSet = new Set(probeSummary.working.map((/** @type {string} */ m) => m.toLowerCase()));
                        availableModels.update((current) => {
                            const arenaModels = current.arena?.models || [];
                            const filtered = arenaModels.filter((/** @type {any} */ m) => workingSet.has((m.id || '').toLowerCase()));
                            return { ...current, arena: { tier: 'free', models: filtered } };
                        });
                    }
                } else if (ev.status === 'aborted') {
                    const workingNow = Object.entries(probeResults)
                        .filter(([, r]) => r.health === 'working')
                        .map(([id]) => id);
                    probeSummary = { working: workingNow, unavailable: [], errored: [], aborted: true, reason: ev.reason };
                    probeProgress = probeTotal > 0 ? Math.round(((ev.index || 0) / probeTotal) * 100) : probeProgress;
                    probeCurrent = '';
                    error = ev.reason || 'Probe stopped early — session may be rate-limited.';
                } else if (ev.status === 'error') {
                    error = ev.error || 'Probe failed.';
                }
            });
        } catch (e) {
            if (!probeAborted) {
                error = /** @type {any} */ (e)?.message || 'Probe failed.';
            }
        } finally {
            probing = false;
        }
    }

    function stopProbe() {
        probeAborted = true;
        probing = false;
        probeCurrent = '';
    }

    async function handleSetup() {
        setupLoading = true;
        setupMessage = '';
        error = '';
        try {
            const result = await setupArena();
            if (result?.success) {
                setupMessage = 'Chrome opened — log in to arena.ai with Google.';
                setupStep = 2;
            } else {
                error = result?.chrome?.error || result?.message || 'Setup failed.';
            }
        } catch (e) {
            error = /** @type {any} */ (e)?.message || 'Setup failed.';
        } finally {
            setupLoading = false;
        }
    }

    async function handleCheckConnection() {
        checkingConnection = true;
        connectionMessage = 'Checking connection...';
        error = '';
        let attempts = 0;
        const maxAttempts = 20;
        const check = async () => {
            attempts++;
            try {
                const s = await getArenaStatus();
                const connected = s?.bridge_active || s?.browser_available || s?.steel_available;
                if (connected) {
                    connectionMessage = 'Connected! Loading models...';
                    checkingConnection = false;
                    await fetchStatus();
                    return;
                }
            } catch { /* ignore */ }
            if (attempts < maxAttempts) {
                connectionMessage = `Checking... (${attempts}/${maxAttempts})`;
                setTimeout(check, 2000);
            } else {
                // Fetch diagnostics to show what's missing
                checkingConnection = false;
                connectionMessage = '';
                try {
                    const cs = await getArenaChromeStatus();
                    const issues = [];
                    if (!cs.chrome_installed) issues.push('Chrome is not installed');
                    if (!cs.native_host_installed) issues.push('Native host not installed — click "Open Arena.ai" in Step 1 to install it');
                    if (!cs.extension_dir_exists) issues.push('Extension files not found');
                    if (!cs.bridge_connected) issues.push('Extension bridge not responding — refresh the arena.ai tab in Chrome');
                    if (issues.length > 0) {
                        error = 'Connection check failed:\n' + issues.map((s, i) => `${i + 1}. ${s}`).join('\n');
                    } else {
                        error = 'All components detected but bridge not active. Close Chrome completely, then click "Open Arena.ai" in Step 1 to relaunch.';
                    }
                } catch {
                    error = 'Could not detect extension bridge. Make sure Chrome is open with arena.ai and the extension is installed.';
                }
            }
        };
        await check();
    }

    async function handleOpenExtensionFolder() {
        openFolderLoading = true;
        try {
            await openArenaExtensionFolder();
        } catch { /* non-fatal */ }
        finally { openFolderLoading = false; }
    }

    async function handleSetExtensionId() {
        const id = unpackedExtensionId.trim().toLowerCase();
        if (!id || id.length !== 32 || !/^[a-z]+$/.test(id)) {
            extensionIdMessage = 'Invalid ID. Must be 32 lowercase letters (shown in chrome://extensions).';
            return;
        }
        settingExtensionId = true;
        extensionIdMessage = '';
        try {
            const result = await setArenaExtensionId(id);
            registeredUnpackedId = result.unpacked_id || id;
            extensionIdMessage = result.message || 'Extension ID registered.';
        } catch (e) {
            extensionIdMessage = e?.response?.data?.detail || e?.message || 'Failed to register extension ID.';
        } finally {
            settingExtensionId = false;
        }
    }

    async function handleLogin() {
        loginLoading = true;
        error = '';
        loginMessage = '';
        try {
            const result = await startArena(true);
            if (result?.message) {
                loginMessage = result.message;
            }
            if (result?.status === 'login_failed' || result?.status === 'browser_offline') {
                error = result.message || 'Could not open browser.';
                loginMessage = '';
            }
        } catch (e) {
            error = /** @type {any} */ (e)?.message || 'Login failed.';
        } finally {
            loginLoading = false;
        }
    }

    async function handleLogout() {
        logoutLoading = true;
        error = '';
        try {
            await logoutArena();
            status = null;
            models = [];
            loginMessage = '';
            await fetchStatus();
        } catch (e) {
            error = /** @type {any} */ (e)?.message || 'Logout failed.';
        } finally {
            logoutLoading = false;
        }
    }

    async function handleShowBrowser() {
        showBrowserLoading = true;
        showBrowserMessage = '';
        error = '';
        try {
            const result = await showArenaBrowser();
            showBrowserMessage = result?.message || 'Browser window opened.';
        } catch (e) {
            error = /** @type {any} */ (e)?.message || 'Failed to open browser window.';
        } finally {
            showBrowserLoading = false;
        }
    }

    /** @param {string} raw */
    function modelDisplayName(raw) {
        if (typeof raw !== 'string') return String(raw);
        return raw.replace('arena/', '');
    }

    /** @param {string} id */
    function healthOf(id) {
        return probeResults[id] || null;
    }

    // Search & filter state
    let searchQuery = '';
    let activeFilter = 'all'; // 'all' | 'code' | 'search'
    /** @type {string | null} */
    let expandedCard = null; // which family card is expanded

    // Family detection for arena models
    const FAMILY_MAP = /** @type {const} */ ({
        claude: 'Claude', gpt: 'OpenAI', o1: 'OpenAI', o3: 'OpenAI', o4: 'OpenAI',
        codex: 'OpenAI',
        gemini: 'Gemini', gemma: 'Gemini', grok: 'Grok', qwen: 'Qwen', qwq: 'Qwen',
        deepseek: 'DeepSeek', mistral: 'Mistral', kimi: 'Kimi', minimax: 'MiniMax',
        glm: 'GLM', mimo: 'GLM', llama: 'Meta', olmo: 'OLMo', step: 'Step',
        ring: 'Ring', ling: 'Ling', mercury: 'Mercury', trinity: 'Trinity',
        ernie: 'Ernie', hunyuan: 'Hunyuan', seed: 'Seed', dola: 'Dola',
        amazon: 'Amazon', global: 'Amazon',
    });

    const FAMILY_PREFIXES = Object.keys(FAMILY_MAP).sort((a, b) => b.length - a.length);

    // Families with logos — shown as prominent cards. Order = display order.
    /** @type {Array<{key: string, logo: string}>} */
    const POPULAR_FAMILIES = [
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

    const POPULAR_KEYS = new Set(POPULAR_FAMILIES.map(f => f.key));

    /** @param {string} family */
    function familyLogo(family) {
        return POPULAR_FAMILIES.find(f => f.key === family)?.logo || '';
    }

    /** @param {string} id */
    function detectFamily(id) {
        const slug = id.replace(/^arena\//, '').toLowerCase();
        for (const prefix of FAMILY_PREFIXES) {
            if (slug.startsWith(prefix)) return FAMILY_MAP[prefix];
        }
        const first = slug.split(/[-._]/)[0];
        return first.charAt(0).toUpperCase() + first.slice(1);
    }

    function toggleCard(/** @type {string} */ family) {
        expandedCard = expandedCard === family ? null : family;
    }

    /**
     * Higher score = stronger model. Models sorted descending by this.
     * Parses version numbers and model tier keywords to rank.
     * @param {string} id
     * @returns {number}
     */
    function modelStrength(id) {
        const slug = id.replace(/^arena\//, '').toLowerCase();
        let score = 0;

        // Tier keywords (strongest first)
        if (slug.includes('pro'))   score += 500;
        if (slug.includes('max'))   score += 450;
        if (slug.includes('high'))  score += 400;
        if (slug.includes('medium')) score += 200;
        if (slug.includes('ultra')) score += 480;

        // Weakness indicators
        if (slug.includes('mini'))  score -= 200;
        if (slug.includes('lite'))  score -= 150;
        if (slug.includes('nano'))  score -= 250;
        if (slug.includes('small')) score -= 100;
        if (slug.includes('fast'))  score -= 50;
        if (slug.includes('preview')) score -= 10;

        // Extract version number — higher = stronger
        // Matches patterns like: gpt-5.4, o4, gemini-3.1, claude-sonnet-4-6, deepseek-v3.2
        const verMatch = slug.match(/[\-_v]?(\d+)[\.\-]?(\d*)/);
        if (verMatch) {
            const major = parseInt(verMatch[1]) || 0;
            const minor = parseInt(verMatch[2]) || 0;
            score += major * 100 + minor * 10;
        }

        // Model family tier boosts
        if (slug.includes('opus'))   score += 600;
        if (slug.includes('sonnet')) score += 400;
        if (slug.includes('haiku')) score += 200;

        // O-series reasoning models rank high
        if (/^o[134]/.test(slug)) score += 550;

        // Codex models
        if (slug.includes('codex')) score += 300;

        return score;
    }

    $: arenaModelList = models
        .map((m) => ({
            id: typeof m === 'string' ? m : (m.id || ''),
            display: typeof m === 'object' && m.display_name
                ? m.display_name
                : modelDisplayName(typeof m === 'string' ? m : (m.id || '')),
        }))
        .sort((a, b) => a.id.replace('arena/', '').localeCompare(b.id.replace('arena/', '')));

    // Smart search: provider aliases map search terms to family names
    /** @type {Record<string, string[]>} */
    const SEARCH_ALIASES = {
        'google':    ['Gemini'],
        'anthropic': ['Claude'],
        'openai':    ['OpenAI'],
        'chatgpt':   ['OpenAI'],
        'meta':      ['Meta', 'Llama'],
        'facebook':  ['Meta', 'Llama'],
        'alibaba':   ['Qwen'],
        'aliyun':    ['Qwen'],
        'baidu':     ['Ernie'],
        'tencent':   ['Hunyuan'],
        'bytedance': ['Seed'],
        'zhipu':     ['GLM'],
        'moonshot':  ['Kimi'],
        'xai':       ['Grok'],
        'x.ai':      ['Grok'],
        'amazon':    ['Amazon'],
        'aws':       ['Amazon'],
        'reasoning': ['OpenAI'],  // o-series
        'vision':    [],  // handled by capability filter below
        'free':      [],  // all arena models are free
    };

    /**
     * Fuzzy match: checks if all chars of query appear in target in order.
     * "gsf" matches "gemini-3-flash", "dsk" matches "deepseek"
     * @param {string} query
     * @param {string} target
     * @returns {boolean}
     */
    function fuzzyMatch(query, target) {
        let qi = 0;
        for (let ti = 0; ti < target.length && qi < query.length; ti++) {
            if (target[ti] === query[qi]) qi++;
        }
        return qi === query.length;
    }

    /**
     * Smart search: exact substring, alias match, or fuzzy match.
     * @param {string} query - lowercase search term
     * @param {string} bare - model slug without arena/
     * @param {string} display - display name
     * @param {string} family - detected family name
     * @returns {boolean}
     */
    function smartMatch(query, bare, display, family) {
        const bareLower = bare.toLowerCase();
        const displayLower = display.toLowerCase();
        const familyLower = family.toLowerCase();

        // 1. Exact substring match on model name or display
        if (bareLower.includes(query) || displayLower.includes(query)) return true;

        // 2. Family name match
        if (familyLower.includes(query)) return true;

        // 3. Alias match — "google" finds Gemini+Gemma, "anthropic" finds Claude, etc.
        for (const [alias, families] of Object.entries(SEARCH_ALIASES)) {
            if (alias.includes(query) || query.includes(alias)) {
                if (families.some(f => f === family)) return true;
            }
        }

        // 4. Fuzzy character match (for short queries like "gsf" → gemini-3-flash)
        if (query.length >= 2 && fuzzyMatch(query, bareLower)) return true;

        return false;
    }

    // Filtered by search + capability filter
    $: filteredModels = arenaModelList.filter(m => {
        const bare = m.id.replace('arena/', '');
        const caps = modelCapabilities[bare] || [];
        const modes = modelModes[bare] || [];
        const family = detectFamily(m.id);
        // Search filter
        if (searchQuery) {
            const q = searchQuery.toLowerCase().trim();
            if (!smartMatch(q, bare, m.display, family)) return false;
        }
        // Capability filter
        if (activeFilter === 'code' && !modes.includes('code')) return false;
        if (activeFilter === 'search' && !modes.includes('search')) return false;
        return true;
    });

    // Group filtered models by family, sorted strongest-first within each group
    $: groupedByFamily = (() => {
        /** @type {Record<string, typeof filteredModels>} */
        const groups = {};
        for (const m of filteredModels) {
            const family = detectFamily(m.id);
            if (!groups[family]) groups[family] = [];
            groups[family].push(m);
        }
        // Sort each group: strongest model first
        for (const key of Object.keys(groups)) {
            groups[key].sort((a, b) => modelStrength(b.id) - modelStrength(a.id));
        }
        return groups;
    })();

    // Split into popular (with logos) and other families
    $: popularFamilies = POPULAR_FAMILIES
        .filter(f => groupedByFamily[f.key]?.length > 0)
        .map(f => ({ ...f, models: groupedByFamily[f.key] }));

    $: otherFamilies = Object.entries(groupedByFamily)
        .filter(([key]) => !POPULAR_KEYS.has(key))
        .sort((a, b) => a[0].localeCompare(b[0]))
        .map(([key, models]) => ({ key, models }));

    // Auto-expand card when search narrows to few results
    $: if (searchQuery) {
        const allKeys = [...popularFamilies.map(f => f.key), ...otherFamilies.map(f => f.key)];
        if (allKeys.length === 1) expandedCard = allKeys[0];
    }

    $: workingCount  = arenaModelList.filter(m => healthOf(m.id)?.health === 'working').length;
    $: brokenCount   = arenaModelList.filter(m => {
        const h = healthOf(m.id)?.health;
        return h && h !== 'working' && h !== 'rate_limited' && h !== 'timeout';
    }).length;
    $: testedCount   = Object.keys(probeResults).length;

    // Count models with each capability for filter badges
    $: codeFilterCount = arenaModelList.filter(m => (modelModes[m.id.replace('arena/', '')] || []).includes('code')).length;
    $: searchFilterCount = arenaModelList.filter(m => (modelModes[m.id.replace('arena/', '')] || []).includes('search')).length;
</script>

<div class="panel">
    <div class="panel-header">
        <button class="back-btn" on:click={() => dispatch('close')}>&larr; Back</button>
        <h2>Arena.ai</h2>
        <div class="header-actions">
            {#if status?.bridge_active || status?.browser_available || status?.steel_available}
                <button
                    class="icon-action-btn"
                    on:click={handleShowBrowser}
                    disabled={showBrowserLoading}
                    title="View Browser Window"
                >
                    {#if showBrowserLoading}
                        <svg class="spinning" viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
                    {:else}
                        <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
                    {/if}
                </button>
            {/if}
            <button class="refresh-btn" on:click={fetchStatus} disabled={loading || probing}>
                {loading ? 'Checking...' : 'Refresh'}
            </button>
        </div>
    </div>
    {#if showBrowserMessage}
        <div class="login-message">{showBrowserMessage}</div>
    {/if}

    <p class="panel-subtitle">Access arena.ai models via Chrome Extension bridge{status?.transport === 'cloakbrowser' ? ' (CloakBrowser fallback)' : ''}.</p>

    {#if error}
        <div class="alert error" style="white-space: pre-line;">{error}</div>
    {/if}

    {#if loading}
        <p class="empty">Checking browser status...</p>

    {:else if !(status?.bridge_active || (status?.browser_available ?? status?.steel_available))}
        <!-- State A: No transport available — 3-step progressive setup -->
        <div class="setup-card">
            <!-- Step 1: Login to Arena -->
            <div class="setup-step {setupStep > 1 ? 'done' : ''}">
                <div class="step-number {setupStep > 1 ? 'check' : ''}">{setupStep > 1 ? '\u2713' : '1'}</div>
                <div class="step-body">
                    <h3>Login to Arena.ai</h3>
                    <p>Opens Chrome and navigates to arena.ai — sign in with your Google account.</p>
                    <p class="hint">Arena.ai must remain open in Chrome for FreeHive to access models.</p>
                    {#if setupStep === 1}
                        <button class="action-btn primary" on:click={handleSetup} disabled={setupLoading}>
                            {setupLoading ? 'Opening Chrome...' : 'Open Arena.ai'}
                        </button>
                    {/if}
                    {#if setupMessage}
                        <div class="login-message">{setupMessage}</div>
                    {/if}
                </div>
            </div>

            <!-- Step 2: Install Extension -->
            <div class="setup-step {setupStep > 2 ? 'done' : ''} {setupStep < 2 ? 'muted' : ''}">
                <div class="step-number {setupStep > 2 ? 'check' : ''}">{setupStep > 2 ? '\u2713' : '2'}</div>
                <div class="step-body">
                    <h3>Install Extension</h3>
                    <p>Choose install method, then <strong>refresh the arena.ai tab</strong>.</p>
                    {#if setupStep >= 2}
                        <div class="install-mode-tabs">
                            <button class="install-tab {installMode === 'unpacked' ? 'active' : ''}" on:click={() => installMode = 'unpacked'}>
                                Load Unpacked
                                <span class="tab-version">v1.0.1</span>
                            </button>
                            <button class="install-tab {installMode === 'webstore' ? 'active' : ''}" on:click={() => installMode = 'webstore'}>
                                Chrome Web Store
                                <span class="tab-version">v1.0.1</span>
                            </button>
                        </div>

                        {#if installMode === 'unpacked'}
                            <div class="install-panel">
                                <ol class="install-steps">
                                    <li>Open <code class="inline-code">chrome://extensions</code> in Chrome</li>
                                    <li>Enable <strong>Developer mode</strong> (top-right toggle)</li>
                                    <li>Click <strong>Load unpacked</strong> and select the folder below:</li>
                                </ol>
                                {#if extensionPath}
                                    <div class="ext-path-box">
                                        <code class="ext-path">{extensionPath}</code>
                                        <button class="ext-open-btn" on:click={handleOpenExtensionFolder} disabled={openFolderLoading}>
                                            {openFolderLoading ? 'Opening...' : 'Open Folder'}
                                        </button>
                                    </div>
                                {/if}
                                <div class="ext-id-section">
                                    <p class="hint">After loading, copy the <strong>extension ID</strong> from <code class="inline-code">chrome://extensions</code> and paste below:</p>
                                    <div class="ext-id-input-row">
                                        <input type="text" class="ext-id-input"
                                            placeholder="32-character extension ID"
                                            bind:value={unpackedExtensionId}
                                            maxlength="32" />
                                        <button class="action-btn primary" on:click={handleSetExtensionId}
                                            disabled={settingExtensionId || !unpackedExtensionId.trim()}>
                                            {settingExtensionId ? 'Saving...' : 'Register ID'}
                                        </button>
                                    </div>
                                    {#if extensionIdMessage}
                                        <div class="ext-id-message">{extensionIdMessage}</div>
                                    {/if}
                                    {#if registeredUnpackedId}
                                        <p class="hint ext-id-registered">Registered: <code class="inline-code">{registeredUnpackedId}</code></p>
                                    {/if}
                                </div>
                            </div>
                        {:else}
                            <div class="install-panel">
                                <a class="action-btn primary"
                                   href="https://chromewebstore.google.com/detail/freehive-arena-bridge/jkclihigpeefogblifghhpojgkbheked"
                                   target="_blank" rel="noopener noreferrer"
                                   style="display: inline-block; text-decoration: none;">
                                    Install from Chrome Web Store
                                </a>
                                <p class="hint">Uses the published extension. No additional setup needed.</p>
                            </div>
                        {/if}
                        {#if setupStep === 2}
                            <button class="action-btn" on:click={() => setupStep = 3} style="margin-top: 8px;">
                                Done — Next Step
                            </button>
                        {/if}
                    {/if}
                </div>
            </div>

            <!-- Step 3: Check Connection -->
            <div class="setup-step {setupStep < 3 ? 'muted' : ''}">
                <div class="step-number">3</div>
                <div class="step-body">
                    <h3>Check Connection</h3>
                    <p>Verify the extension is connected and Arena.ai is reachable.</p>
                    {#if setupStep >= 3}
                        <button class="action-btn primary" on:click={handleCheckConnection} disabled={checkingConnection}>
                            {checkingConnection ? 'Checking...' : 'Check Connection'}
                        </button>
                        {#if connectionMessage}
                            <div class="login-message">{connectionMessage}</div>
                        {/if}
                    {/if}
                </div>
            </div>
        </div>

    {:else if !status?.authenticated}
        <!-- State B: CloakBrowser available, not logged in -->
        <div class="account-bar not-linked">
            <span class="account-label">Account:</span>
            <span class="account-email dim">No account linked</span>
        </div>

        <div class="setup-card">
            <div class="setup-step done">
                <div class="step-number check">&check;</div>
                <div class="step-body">
                    <h3>CloakBrowser Ready</h3>
                    <p>Stealth Chromium binary installed and available.</p>
                </div>
            </div>
            <div class="setup-step">
                <div class="step-number">2</div>
                <div class="step-body">
                    <h3>Log in to Arena.ai</h3>
                    <p>Opens a browser window for Google OAuth sign-in. Cookies persist across restarts.</p>
                    <button class="action-btn primary" on:click={handleLogin} disabled={loginLoading}>
                        {loginLoading ? 'Opening browser...' : 'Sign in to Arena.ai'}
                    </button>
                    {#if loginMessage}
                        <div class="login-message">{loginMessage}</div>
                    {/if}
                    <p class="hint">Opens a browser window for Google OAuth. After login, click <strong>Refresh</strong> above.</p>
                </div>
            </div>
            <div class="setup-step muted">
                <div class="step-number">3</div>
                <div class="step-body">
                    <h3>Done</h3>
                    <p>Models will load automatically once authenticated.</p>
                </div>
            </div>
        </div>

    {:else}
        <!-- State C: Connected -->
        <div class="status-bar">
            <span class="status-dot"></span>
            <span class="status-text">Connected to Arena.ai</span>
            <span class="status-badge">{#if status?.transport === 'extension'}Extension Bridge {registeredUnpackedId ? '(Unpacked)' : '(Web Store)'}{:else}CloakBrowser{/if}</span>
        </div>

        <div class="account-bar">
            {#if status?.account?.email}
                <span class="account-label">Account:</span>
                <span class="account-email">{status.account.email}</span>
                {#if status.account.name}
                    <span class="account-name">({status.account.name})</span>
                {/if}
            {:else}
                <span class="account-label">Account:</span>
                <span class="account-email dim">Authenticated (email not available)</span>
            {/if}
            <button class="small-btn danger logout-btn" on:click={handleLogout} disabled={logoutLoading}>
                {logoutLoading ? 'Logging out...' : 'Logout'}
            </button>
        </div>
        <!-- Probe section -->
        <div class="probe-section">
            <div class="probe-header">
                <div class="probe-title">
                    <span class="section-label">Model Health Check</span>
                    {#if testedCount > 0}
                        <span class="probe-summary-badges">
                            <span class="badge-pill green">{workingCount} working</span>
                            {#if brokenCount > 0}<span class="badge-pill red">{brokenCount} unavailable</span>{/if}
                        </span>
                    {/if}
                </div>
                {#if probing}
                    <button class="small-btn danger" on:click={stopProbe}>Stop</button>
                {:else}
                    <button class="small-btn" on:click={startProbe} disabled={arenaModelList.length === 0}>
                        {testedCount > 0 ? 'Re-test All' : 'Test All Models'}
                    </button>
                {/if}
            </div>

            {#if probing}
                <div class="probe-progress-wrap">
                    <div class="probe-bar-track">
                        <div class="probe-bar-fill" style="width: {probeProgress}%"></div>
                    </div>
                    <p class="probe-status-text">
                        {probeProgress}% &mdash; {probeCurrent ? `Testing ${probeCurrent.replace('arena/', '')}...` : 'Starting...'}
                    </p>
                </div>
            {:else if probeSummary}
                <p class="hint">
                    Probe complete: <strong>{probeSummary.working?.length ?? 0}</strong> working,
                    <strong>{(probeSummary.unavailable?.length ?? 0)}</strong> unavailable,
                    <strong>{probeSummary.errored?.length ?? 0}</strong> with other errors.
                    Working models updated in sidebar.
                </p>
            {:else if testedCount === 0}
                <p class="hint">Run the health check to find which models actually work in Direct chat mode.</p>
            {/if}
        </div>

        <!-- Model list -->
        <div class="models-section">
            <div class="models-header">
                <p class="section-label">
                    Models {#if arenaModelList.length > 0}({filteredModels.length}{#if filteredModels.length !== arenaModelList.length}/{arenaModelList.length}{/if}){/if}
                    {#if chatCount > 0 || codeCount > 0}
                        <span class="mode-counts">{chatCount} chat, {codeCount} code</span>
                    {/if}
                </p>
                <div class="models-header-btns">
                    <button class="small-btn" on:click={fetchModels} disabled={modelsLoading || modelsRefreshing}>
                        {modelsLoading ? 'Loading...' : 'Cached'}
                    </button>
                    <button class="small-btn primary-btn" on:click={handleRefreshModels} disabled={modelsRefreshing || modelsLoading}>
                        {modelsRefreshing ? 'Refreshing...' : 'Refresh Live'}
                    </button>
                </div>
            </div>

            {#if modelsLoading || modelsRefreshing}
                <p class="empty">{modelsRefreshing ? 'Fetching models from arena.ai chat + code (takes ~10s)...' : 'Loading cached models...'}</p>
            {:else if arenaModelList.length === 0}
                <div class="empty-models">
                    <p>No models found. This usually means:</p>
                    <ul>
                        <li>The page hasn't fully loaded yet &mdash; click <strong>Refresh</strong></li>
                        <li>You're not logged in to arena.ai &mdash; click Sign in above</li>
                    </ul>
                </div>
            {:else}
                <!-- Search + filters -->
                <div class="search-filter-bar">
                    <div class="search-box">
                        <svg class="search-icon" viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                        <input
                            type="text"
                            class="search-input"
                            placeholder="Search models..."
                            bind:value={searchQuery}
                        />
                        {#if searchQuery}
                            <button class="search-clear" on:click={() => searchQuery = ''}>
                                <svg viewBox="0 0 24 24" width="12" height="12" stroke="currentColor" stroke-width="2" fill="none"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                            </button>
                        {/if}
                    </div>
                    <div class="filter-pills">
                        <button class="filter-pill {activeFilter === 'all' ? 'active' : ''}" on:click={() => activeFilter = 'all'}>
                            All <span class="filter-count">{arenaModelList.length}</span>
                        </button>
                        <button class="filter-pill {activeFilter === 'code' ? 'active' : ''}" on:click={() => activeFilter = activeFilter === 'code' ? 'all' : 'code'}>
                            <span class="tag tag-code">code</span> <span class="filter-count">{codeFilterCount}</span>
                        </button>
                        <button class="filter-pill {activeFilter === 'search' ? 'active' : ''}" on:click={() => activeFilter = activeFilter === 'search' ? 'all' : 'search'}>
                            <span class="tag tag-search">search</span> <span class="filter-count">{searchFilterCount}</span>
                        </button>
                    </div>
                </div>

                <!-- Model cards -->
                {#if filteredModels.length === 0}
                    <p class="empty">No models match "{searchQuery}"</p>
                {:else}
                    <!-- Popular provider cards -->
                    {#if popularFamilies.length > 0}
                        <div class="card-grid">
                            {#each popularFamilies as fam}
                                <button
                                    class="provider-card {expandedCard === fam.key ? 'expanded' : ''}"
                                    on:click={() => toggleCard(fam.key)}
                                >
                                    <img class="card-logo" src={fam.logo} alt={fam.key} />
                                    <span class="card-name">{fam.key}</span>
                                    <span class="card-count">{fam.models.length}</span>
                                </button>
                            {/each}
                        </div>
                    {/if}

                    <!-- Expanded model list for selected card -->
                    {#if expandedCard && groupedByFamily[expandedCard]}
                        <div class="expanded-panel">
                            <div class="expanded-panel-header">
                                {#if familyLogo(expandedCard)}
                                    <img class="expanded-logo" src={familyLogo(expandedCard)} alt={expandedCard} />
                                {/if}
                                <span class="expanded-title">{expandedCard}</span>
                                <span class="expanded-count">{groupedByFamily[expandedCard].length} models</span>
                                <button class="expanded-close" on:click={() => expandedCard = null}>
                                    <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                                </button>
                            </div>
                            <div class="expanded-model-list">
                                {#each groupedByFamily[expandedCard] as m}
                                    {@const health = healthOf(m.id)}
                                    {@const hl = health ? (HEALTH_LABEL[health.health] || {text: health.health, color: '#6b7280'}) : null}
                                    {@const bare = m.id.replace('arena/', '')}
                                    {@const mCaps = modelCapabilities[bare] || []}
                                    {@const mModes = modelModes[bare] || []}
                                    <button
                                        class="model-row {$selectedModel === m.id ? 'active' : ''}"
                                        on:click={() => { selectedModel.set(m.id); dispatch('close'); }}
                                        title={[m.id, health?.error || ''].filter(Boolean).join(' | ')}
                                    >
                                        <span class="model-row-name">{m.display}</span>
                                        <span class="model-row-tags">
                                            {#if mModes.includes('code')}<span class="tag tag-code">code</span>{/if}
                                            {#if mCaps.includes('search')}<span class="tag tag-search">search</span>{/if}
                                                                                    </span>
                                        {#if hl}
                                            <span class="health-dot" style="background: {hl.color}" title={hl.text}></span>
                                        {:else if probing && probeCurrent === m.id}
                                            <span class="health-dot probing" title="Testing..."></span>
                                        {/if}
                                    </button>
                                {/each}
                            </div>
                        </div>
                    {/if}

                    <!-- Other / less common families -->
                    {#if otherFamilies.length > 0}
                        <div class="other-section">
                            <button class="other-header" on:click={() => toggleCard('__other__')}>
                                <svg class="family-chevron {expandedCard === '__other__' ? 'open' : ''}" viewBox="0 0 24 24" width="12" height="12" stroke="currentColor" stroke-width="2.5" fill="none"><polyline points="6 9 12 15 18 9"/></svg>
                                <span>Other Providers</span>
                                <span class="card-count">{otherFamilies.reduce((s, f) => s + f.models.length, 0)}</span>
                            </button>
                            {#if expandedCard === '__other__'}
                                <div class="other-families">
                                    {#each otherFamilies as fam}
                                        <div class="other-family-group">
                                            <button class="other-family-header" on:click|stopPropagation={() => toggleCard(`other:${fam.key}`)}>
                                                <span class="other-family-name">{fam.key}</span>
                                                <span class="family-count-badge">{fam.models.length}</span>
                                            </button>
                                        </div>
                                    {/each}
                                </div>
                            {/if}
                        </div>

                        <!-- Expanded other family models -->
                        {#each otherFamilies as fam}
                            {#if expandedCard === `other:${fam.key}`}
                                <div class="expanded-panel">
                                    <div class="expanded-panel-header">
                                        <span class="expanded-title">{fam.key}</span>
                                        <span class="expanded-count">{fam.models.length} models</span>
                                        <button class="expanded-close" on:click={() => expandedCard = '__other__'}>
                                            <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                                        </button>
                                    </div>
                                    <div class="expanded-model-list">
                                        {#each fam.models as m}
                                            {@const health = healthOf(m.id)}
                                            {@const hl = health ? (HEALTH_LABEL[health.health] || {text: health.health, color: '#6b7280'}) : null}
                                            {@const bare = m.id.replace('arena/', '')}
                                            {@const mCaps = modelCapabilities[bare] || []}
                                            {@const mModes = modelModes[bare] || []}
                                            <button
                                                class="model-row {$selectedModel === m.id ? 'active' : ''}"
                                                on:click={() => { selectedModel.set(m.id); dispatch('close'); }}
                                                title={[m.id, health?.error || ''].filter(Boolean).join(' | ')}
                                            >
                                                <span class="model-row-name">{m.display}</span>
                                                <span class="model-row-tags">
                                                    {#if mModes.includes('code')}<span class="tag tag-code">code</span>{/if}
                                                    {#if mCaps.includes('search')}<span class="tag tag-search">search</span>{/if}
                                                                                                    </span>
                                                {#if hl}
                                                    <span class="health-dot" style="background: {hl.color}" title={hl.text}></span>
                                                {:else if probing && probeCurrent === m.id}
                                                    <span class="health-dot probing" title="Testing..."></span>
                                                {/if}
                                            </button>
                                        {/each}
                                    </div>
                                </div>
                            {/if}
                        {/each}
                    {/if}
                {/if}
            {/if}
        </div>

        <div class="tool-note">
            <strong>Tool calling</strong> is supported for models that offer it &mdash; returned automatically in responses.
            Captcha challenges are forwarded to you for solving when they appear.
        </div>
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

    h2 { font-size: 16px; font-weight: 600; color: var(--text-primary); }

    .panel-subtitle { font-size: 13px; color: var(--text-secondary); margin-top: -6px; }

    .back-btn {
        background: transparent; border: none; color: var(--text-secondary);
        font-size: 13px; cursor: pointer; padding: 4px 8px; border-radius: 4px;
    }
    .back-btn:hover { color: var(--text-primary); background: var(--bg-secondary); }

    .header-actions { display: flex; align-items: center; gap: 6px; }

    .icon-action-btn {
        background: var(--bg-secondary); border: 1px solid var(--border-medium);
        color: var(--text-secondary); padding: 6px;
        border-radius: 6px; cursor: pointer;
        display: flex; align-items: center; justify-content: center;
        transition: color 0.15s, border-color 0.15s, background 0.15s;
    }
    .icon-action-btn:hover:not(:disabled) {
        color: var(--text-primary); border-color: var(--text-muted);
        background: var(--bg-tertiary);
    }
    .icon-action-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .icon-action-btn .spinning { animation: spin 0.8s linear infinite; }
    @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

    .refresh-btn {
        background: var(--bg-secondary); border: 1px solid var(--border-medium);
        color: var(--text-primary); padding: 6px 12px; border-radius: 6px;
        font-size: 13px; cursor: pointer;
    }
    .refresh-btn:disabled { opacity: 0.6; cursor: not-allowed; }

    .alert { padding: 10px 12px; border-radius: 8px; font-size: 13px; border: 1px solid var(--border-medium); background: var(--bg-secondary); }
    .alert.error { color: #ef4444; }

    .empty { font-size: 13px; color: var(--text-muted); margin-top: 8px; }

    /* Setup card */
    .setup-card { display: flex; flex-direction: column; background: var(--bg-secondary); border: 1px solid var(--border-medium); border-radius: 10px; overflow: hidden; }
    .setup-step { display: flex; gap: 16px; padding: 16px; border-bottom: 1px solid var(--border-light); }
    .setup-step:last-child { border-bottom: none; }
    .setup-step.muted { opacity: 0.45; }
    .setup-step.done .step-number { background: var(--accent-color); color: var(--bg-primary); border-color: var(--accent-color); }
    .step-number {
        width: 28px; height: 28px; border-radius: 50%; border: 1.5px solid var(--border-medium);
        display: flex; align-items: center; justify-content: center;
        font-size: 13px; font-weight: 600; flex-shrink: 0; color: var(--text-secondary);
    }
    .step-body { display: flex; flex-direction: column; gap: 8px; flex: 1; min-width: 0; }
    .step-body h3 { font-size: 14px; font-weight: 600; color: var(--text-primary); margin: 0; }
    .step-body p { font-size: 13px; color: var(--text-secondary); margin: 0; }

    .code-block { display: flex; align-items: flex-start; gap: 8px; background: var(--bg-primary); border: 1px solid var(--border-medium); border-radius: 6px; padding: 10px 12px; }
    .code-block code { flex: 1; font-family: ui-monospace, Menlo, Monaco, monospace; font-size: 11.5px; color: var(--text-primary); word-break: break-all; white-space: pre-wrap; }
    .inline-code { font-family: ui-monospace, Menlo, Monaco, monospace; font-size: 12px; background: var(--bg-tertiary); padding: 1px 5px; border-radius: 3px; }
    .install-mode-tabs {
        display: flex; gap: 0; border: 1px solid var(--border-medium);
        border-radius: 8px; overflow: hidden; margin-bottom: 8px;
    }
    .install-tab {
        flex: 1; padding: 10px 12px; background: var(--bg-primary);
        border: none; color: var(--text-secondary); font-size: 13px;
        font-weight: 500; cursor: pointer; text-align: center;
        transition: all 0.15s; display: flex; flex-direction: column;
        align-items: center; gap: 2px;
    }
    .install-tab:first-child { border-right: 1px solid var(--border-medium); }
    .install-tab:hover { background: var(--bg-tertiary); color: var(--text-primary); }
    .install-tab.active { background: var(--accent-muted, rgba(34,197,94,0.08)); color: var(--accent-color); font-weight: 600; }
    .tab-version { font-size: 10px; color: var(--text-muted); font-family: ui-monospace, monospace; }
    .install-tab.active .tab-version { color: var(--accent-color); opacity: 0.7; }
    .install-panel { margin-top: 4px; }
    .ext-id-section { margin-top: 8px; display: flex; flex-direction: column; gap: 6px; }
    .ext-id-input-row { display: flex; gap: 8px; align-items: center; }
    .ext-id-input {
        flex: 1; background: var(--bg-primary); border: 1px solid var(--border-medium);
        border-radius: 6px; padding: 8px 12px; font-size: 12px;
        font-family: ui-monospace, Menlo, Monaco, monospace;
        color: var(--text-primary); outline: none; transition: border-color 0.2s;
    }
    .ext-id-input:focus { border-color: var(--accent-color); }
    .ext-id-input::placeholder { color: var(--text-muted); }
    .ext-id-message {
        font-size: 12px; padding: 6px 10px;
        background: rgba(34, 197, 94, 0.08); border: 1px solid rgba(34, 197, 94, 0.2);
        border-radius: 6px; color: #22c55e;
    }
    .ext-id-registered { margin-top: 0; }
    .install-steps {
        padding-left: 20px; margin: 6px 0; display: flex; flex-direction: column; gap: 4px;
        font-size: 13px; color: var(--text-secondary);
    }
    .install-steps li { line-height: 1.5; }
    .install-steps li strong { color: var(--text-primary); }
    .ext-path-box {
        display: flex; align-items: center; gap: 8px;
        background: var(--bg-primary); border: 1px solid var(--border-medium);
        border-radius: 6px; padding: 8px 12px; margin-top: 4px;
    }
    .ext-path {
        flex: 1; font-family: ui-monospace, Menlo, Monaco, monospace;
        font-size: 11.5px; color: var(--text-primary);
        word-break: break-all; white-space: pre-wrap;
        user-select: all;
    }
    .ext-open-btn {
        flex-shrink: 0; background: var(--text-primary); color: var(--bg-primary);
        border: none; padding: 5px 12px; border-radius: 4px;
        font-size: 12px; font-weight: 500; cursor: pointer;
        transition: opacity 0.15s;
    }
    .ext-open-btn:hover:not(:disabled) { opacity: 0.85; }
    .ext-open-btn:disabled { opacity: 0.5; cursor: not-allowed; }

    .store-link { display: inline-block; padding: 4px 12px; background: var(--accent, #4285f4); color: #fff; border-radius: 4px; text-decoration: none; font-size: 13px; font-weight: 500; margin-top: 4px; }
    .store-link:hover { opacity: 0.9; }

    .hint { font-size: 12px; color: var(--text-muted); margin: 0; }

    .action-btn { align-self: flex-start; background: var(--bg-secondary); border: 1px solid var(--border-medium); color: var(--text-primary); padding: 8px 16px; border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer; }
    .action-btn.primary { background: var(--text-primary); color: var(--bg-primary); border-color: transparent; }
    .action-btn:hover:not(:disabled) { opacity: 0.85; }
    .action-btn:disabled { opacity: 0.5; cursor: not-allowed; }

    /* Status bar */
    .status-bar { display: flex; align-items: center; gap: 8px; padding: 10px 14px; background: var(--bg-secondary); border: 1px solid var(--border-medium); border-radius: 8px; }
    .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent-color); flex-shrink: 0; }
    .status-text { font-size: 13px; font-weight: 500; color: var(--accent-color); }
    .status-badge { font-size: 11px; color: var(--text-muted); margin-left: auto; background: var(--bg-tertiary); padding: 2px 8px; border-radius: 4px; font-family: ui-monospace, monospace; }

    /* Probe section */
    .probe-section { display: flex; flex-direction: column; gap: 8px; padding: 12px 14px; background: var(--bg-secondary); border: 1px solid var(--border-medium); border-radius: 10px; }
    .probe-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .probe-title { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
    .probe-summary-badges { display: flex; gap: 6px; }
    .badge-pill { font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 500; }
    .badge-pill.green { background: rgba(34,197,94,0.15); color: #22c55e; }
    .badge-pill.red   { background: rgba(239,68,68,0.15);  color: #ef4444; }

    .probe-progress-wrap { display: flex; flex-direction: column; gap: 6px; }
    .probe-bar-track { height: 6px; background: var(--bg-tertiary); border-radius: 3px; overflow: hidden; }
    .probe-bar-fill { height: 100%; background: var(--accent-color); border-radius: 3px; transition: width 0.3s ease; }
    .probe-status-text { font-size: 12px; color: var(--text-secondary); }

    /* Models */
    .models-section { display: flex; flex-direction: column; gap: 10px; }
    .models-header { display: flex; align-items: center; justify-content: space-between; }
    .section-label { font-size: 11px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }

    .small-btn { background: transparent; border: 1px solid var(--border-medium); color: var(--text-secondary); font-size: 11px; padding: 4px 10px; border-radius: 4px; cursor: pointer; }
    .small-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .small-btn:hover:not(:disabled) { color: var(--text-primary); }
    .small-btn.danger { color: #ef4444; border-color: rgba(239,68,68,0.4); }
    .small-btn.danger:hover { background: rgba(239,68,68,0.08); }

    /* Legacy chip styles removed — using family-grouped rows */

    .health-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
    .health-dot.probing { background: var(--accent-color); animation: blink 0.9s infinite; }
    @keyframes blink { 0%,100%{opacity:0.3} 50%{opacity:1} }

    .dot-legend { display: inline-block; width: 7px; height: 7px; border-radius: 50%; vertical-align: middle; }
    .dot-legend.green  { background: #22c55e; }
    .dot-legend.red    { background: #ef4444; }
    .dot-legend.yellow { background: #f59e0b; }

    .empty-models { font-size: 13px; color: var(--text-secondary); padding: 10px 14px; background: var(--bg-secondary); border: 1px solid var(--border-light); border-radius: 8px; display: flex; flex-direction: column; gap: 6px; }
    .empty-models ul { padding-left: 18px; display: flex; flex-direction: column; gap: 3px; }
    .empty-models li { font-size: 12px; color: var(--text-muted); }

    .tool-note { font-size: 12px; color: var(--text-secondary); padding: 10px 14px; background: var(--bg-secondary); border: 1px solid var(--border-light); border-radius: 8px; }

    .login-message { font-size: 13px; color: #22c55e; padding: 8px 12px; background: rgba(34,197,94,0.08); border: 1px solid rgba(34,197,94,0.2); border-radius: 6px; }

    .account-bar {
        display: flex; align-items: center; gap: 8px; padding: 10px 14px;
        background: var(--bg-secondary); border: 1px solid var(--border-medium); border-radius: 8px;
    }
    .account-bar.not-linked { opacity: 0.7; }
    .account-label { font-size: 12px; color: var(--text-muted); font-weight: 600; }
    .account-email { font-size: 13px; color: var(--text-primary); font-family: ui-monospace, monospace; }
    .account-email.dim { color: var(--text-muted); font-style: italic; font-family: inherit; }
    .account-name { font-size: 12px; color: var(--text-secondary); }
    .logout-btn { margin-left: auto; }
    /* browser-btn removed — now icon in header */

    .models-header-btns { display: flex; gap: 6px; }
    .primary-btn { background: var(--accent-muted); color: var(--accent-color); border-color: var(--accent-color); }
    .mode-counts { font-size: 10px; color: var(--text-muted); font-weight: 400; text-transform: none; letter-spacing: 0; }

    /* Search & Filter */
    .search-filter-bar { display: flex; flex-direction: column; gap: 8px; }
    .search-box {
        display: flex; align-items: center; gap: 8px;
        background: var(--bg-primary); border: 1px solid var(--border-medium);
        border-radius: 8px; padding: 7px 12px;
        transition: border-color 0.2s;
    }
    .search-box:focus-within { border-color: var(--text-muted); }
    .search-icon { color: var(--text-muted); flex-shrink: 0; }
    .search-input {
        flex: 1; background: none; border: none; outline: none;
        color: var(--text-primary); font-size: 13px; font-family: inherit;
    }
    .search-input::placeholder { color: var(--text-muted); }
    .search-clear {
        background: var(--bg-tertiary); border: none; color: var(--text-muted);
        width: 18px; height: 18px; border-radius: 50%; cursor: pointer;
        display: flex; align-items: center; justify-content: center;
        transition: color 0.15s;
    }
    .search-clear:hover { color: var(--text-primary); }

    .filter-pills { display: flex; gap: 4px; flex-wrap: wrap; }
    .filter-pill {
        display: flex; align-items: center; gap: 4px;
        background: transparent; border: 1px solid var(--border-medium);
        border-radius: 6px; padding: 4px 10px;
        font-size: 12px; color: var(--text-secondary);
        cursor: pointer; transition: all 0.15s;
    }
    .filter-pill:hover { border-color: var(--text-muted); color: var(--text-primary); }
    .filter-pill.active { background: var(--accent-muted); border-color: var(--accent-color); color: var(--accent-color); }
    .filter-count { font-size: 10px; color: var(--text-muted); }
    .filter-pill.active .filter-count { color: var(--accent-color); opacity: 0.7; }

    /* Provider card grid */
    .card-grid {
        display: flex;
        flex-wrap: wrap;
        justify-content: center;
        gap: 10px;
    }
    .provider-card {
        display: flex; flex-direction: column; align-items: center; gap: 10px;
        padding: 20px 12px 16px;
        width: 140px;
        background: var(--bg-secondary); border: 1px solid var(--border-light);
        border-radius: 12px; cursor: pointer;
        transition: all 0.18s ease;
    }
    .provider-card:hover {
        border-color: var(--text-muted);
        background: var(--bg-tertiary);
        transform: translateY(-2px);
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
    }
    .provider-card.expanded {
        border-color: var(--accent-color);
        background: var(--accent-muted);
        box-shadow: 0 0 0 1px var(--accent-color);
        transform: translateY(0);
    }
    .card-logo {
        width: 48px; height: 48px;
        border-radius: 10px; object-fit: contain;
        background: var(--bg-tertiary);
        padding: 4px;
    }
    .card-name {
        font-size: 14px; font-weight: 600;
        color: var(--text-primary);
        white-space: nowrap;
    }
    .provider-card.expanded .card-name { color: var(--accent-color); }
    .card-count {
        font-size: 11px; color: var(--text-muted);
        background: var(--bg-tertiary); padding: 2px 10px;
        border-radius: 10px;
    }
    .provider-card.expanded .card-count {
        background: rgba(62, 207, 142, 0.2); color: var(--accent-color);
    }

    /* Expanded model panel */
    .expanded-panel {
        background: var(--bg-secondary); border: 1px solid var(--border-medium);
        border-radius: 10px; overflow: hidden;
        animation: slideDown 0.15s ease;
    }
    @keyframes slideDown {
        from { opacity: 0; transform: translateY(-4px); }
        to { opacity: 1; transform: translateY(0); }
    }
    .expanded-panel-header {
        display: flex; align-items: center; gap: 8px;
        padding: 10px 14px;
        border-bottom: 1px solid var(--border-light);
    }
    .expanded-logo {
        width: 20px; height: 20px; border-radius: 5px;
        object-fit: contain; background: var(--bg-tertiary); padding: 1px;
    }
    .expanded-title { font-size: 13px; font-weight: 600; color: var(--text-primary); }
    .expanded-count { font-size: 11px; color: var(--text-muted); margin-right: auto; }
    .expanded-close {
        background: none; border: none; color: var(--text-muted);
        cursor: pointer; padding: 4px; border-radius: 4px;
        display: flex; align-items: center;
        transition: color 0.15s;
    }
    .expanded-close:hover { color: var(--text-primary); background: var(--bg-tertiary); }
    .expanded-model-list {
        max-height: 300px; overflow-y: auto;
    }
    .expanded-model-list::-webkit-scrollbar { width: 4px; }
    .expanded-model-list::-webkit-scrollbar-thumb { background: var(--border-medium); border-radius: 2px; }

    .model-row {
        display: flex; align-items: center; gap: 8px;
        padding: 7px 14px;
        background: transparent; border: none; border-bottom: 1px solid var(--border-light);
        color: var(--text-secondary); font-size: 12px;
        cursor: pointer; text-align: left; width: 100%;
        transition: background 0.12s, color 0.12s;
    }
    .model-row:last-child { border-bottom: none; }
    .model-row:hover { background: var(--bg-tertiary); color: var(--text-primary); }
    .model-row.active {
        background: var(--accent-muted); color: var(--accent-color);
        font-weight: 500;
    }
    .model-row-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .model-row-tags { display: flex; gap: 3px; flex-shrink: 0; }

    /* Other providers section */
    .other-section {
        border: 1px solid var(--border-light); border-radius: 8px;
        overflow: hidden; background: var(--bg-secondary);
    }
    .other-header {
        display: flex; align-items: center; gap: 8px;
        width: 100%; padding: 10px 14px;
        background: transparent; border: none;
        color: var(--text-secondary); font-size: 13px; font-weight: 500;
        cursor: pointer; transition: background 0.15s;
    }
    .other-header:hover { background: var(--bg-tertiary); }
    .family-chevron { transition: transform 0.2s; color: var(--text-muted); flex-shrink: 0; }
    .family-chevron.open { transform: rotate(180deg); }
    .other-families {
        display: flex; flex-wrap: wrap; gap: 8px;
        padding: 12px 14px 14px;
        border-top: 1px solid var(--border-light);
        justify-content: center;
    }
    .other-family-group { display: contents; }
    .other-family-header {
        display: flex; align-items: center; gap: 6px;
        background: var(--bg-primary); border: 1px solid var(--border-medium);
        border-radius: 8px; padding: 10px 16px;
        font-size: 13px; color: var(--text-secondary);
        cursor: pointer; transition: all 0.15s;
    }
    .other-family-header:hover { border-color: var(--text-muted); color: var(--text-primary); background: var(--bg-tertiary); }
    .other-family-name { white-space: nowrap; }
    .family-count-badge {
        font-size: 10px; color: var(--text-muted);
        background: var(--bg-tertiary); padding: 1px 7px;
        border-radius: 10px; flex-shrink: 0;
    }

    .tag { font-size: 9px; padding: 1px 5px; border-radius: 3px; font-weight: 500; line-height: 1.3; }
    .tag-code { background: rgba(99,102,241,0.15); color: #818cf8; }
    .tag-search { background: rgba(34,197,94,0.15); color: #22c55e; }
    .tag-img { background: rgba(245,158,11,0.15); color: #f59e0b; }
</style>
