<script>
    import { availableModels } from '$lib/store.js';
    import { API_ROOT_URL } from '$lib/config.js';

    let settingsTab = 'apikeys';
    /** @type {string | null} */
    let copiedKey = null;

    const BASE_URL = API_ROOT_URL;

    /** @type {Record<string, {name: string, color: string}>} */
    const PROVIDER_LABELS = {
        claude:  { name: 'Claude',  color: '#cc785c' },
        chatgpt: { name: 'ChatGPT', color: '#19c37d' },
        gemini:  { name: 'Gemini',  color: '#4285f4' },
    };

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

    // Flatten all models across providers for the key list
    $: allModels = Object.entries($availableModels).flatMap(([provider, data]) =>
        (data.models || []).map((/** @type {any} */ m) => ({ ...m, provider, tier: data.tier }))
    );
</script>

<div class="settings-page">
    <div class="settings-tabs">
        <button
            class="tab-btn {settingsTab === 'apikeys' ? 'active' : ''}"
            on:click={() => settingsTab = 'apikeys'}>
            API Keys
        </button>
        <button
            class="tab-btn {settingsTab === 'usage' ? 'active' : ''} disabled"
            disabled
            title="Coming soon">
            Usage
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

                {#each Object.entries($availableModels) as [provider, data]}
                    {#if data.models && data.models.length > 0}
                        <div class="provider-group">
                            <div class="provider-header">
                                <span class="provider-dot" style="background: {PROVIDER_LABELS[provider]?.color ?? 'var(--text-muted)'}"></span>
                                <span class="provider-name">{PROVIDER_LABELS[provider]?.name ?? provider}</span>
                                {#if data.tier && data.tier !== 'unknown'}
                                    <span class="tier-pill">{data.tier}</span>
                                {/if}
                            </div>

                            <div class="model-key-list">
                                {#each data.models as model}
                                    {@const key = makeKey(model.id)}
                                    <div class="model-key-row">
                                        <div class="model-info">
                                            <span class="model-name">{model.display_name}</span>
                                            {#if model.note}
                                                <span class="model-note">{model.note}</span>
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
                        </div>
                    {/if}
                {/each}
            </div>

            <div class="quickstart">
                <h3 class="keys-heading">Quick Setup</h3>
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
</div>

<style>
    .settings-page {
        display: flex;
        flex-direction: column;
        height: 100%;
        overflow: hidden;
    }

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
        border-bottom: 1px solid var(--border-medium);
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
        color: var(--text-secondary);
        background: var(--bg-tertiary);
        padding: 2px 8px;
        border-radius: 4px;
        white-space: nowrap;
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
</style>
