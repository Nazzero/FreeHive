import { writable, derived } from 'svelte/store';

// Full model string — e.g. 'claude-haiku-4-5', 'gpt-5.2', 'gemini-3-flash-preview'
export const selectedModel = writable('claude-haiku-4-5');

// Derived: which provider does the current model belong to
export const selectedProvider = derived(selectedModel, ($m) => {
    const m = ($m || '').toLowerCase();
    if (m === 'claude' || m.startsWith('claude-')) return 'claude';
    if (m === 'chatgpt' || m.startsWith('gpt-') || m.startsWith('o1-') ||
        m.startsWith('o3-') || m.startsWith('o4-') || m.startsWith('codex-')) return 'chatgpt';
    if (m === 'gemini' || m.startsWith('gemini-')) return 'gemini';
    return 'claude';
});

/** @type {import('svelte/store').Writable<any[]>} */
export const messages = writable([]);
/** @type {import('svelte/store').Writable<boolean>} */
export const isLoading = writable(false);

/**
 * @param {string} role
 * @param {string} content
 * @param {string | null} [model]
 * @param {string | null} [transport]
 */
export function addMessage(role, content, model = null, transport = null) {
    messages.update(msgs => [...msgs, {
        id: Date.now(),
        role,
        content,
        model,
        transport,
        timestamp: new Date().toLocaleTimeString()
    }]);
}

export function clearMessages() {
    messages.set([]);
}

// Dynamically populated from /api/setup/models on app load.
// Shape: { claude: { tier, models: [{id, display_name, note}] }, chatgpt: {...}, gemini: {...} }
export const availableModels = writable({
    claude: {
        tier: 'unknown',
        models: [
            { id: 'claude-haiku-4-5',  display_name: 'Haiku 4.5',  note: 'fast' },
            { id: 'claude-sonnet-4-5', display_name: 'Sonnet 4.5', note: 'balanced' },
        ],
    },
    chatgpt: {
        tier: 'unknown',
        models: [
            { id: 'gpt-5.2', display_name: 'GPT-5.2', note: 'free tier' },
        ],
    },
    gemini: {
        tier: 'unknown',
        models: [
            { id: 'gemini-3-flash-preview', display_name: 'Gemini 3 Flash Preview', note: 'fast' },
            { id: 'gemini-2.5-flash-lite',  display_name: 'Gemini 2.5 Flash Lite',  note: 'more quota' },
        ],
    },
});
