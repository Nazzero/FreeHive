import { writable, derived } from 'svelte/store';
import { marked } from 'marked';

// Full model string — e.g. 'claude-haiku-4-5', 'gpt-5.2', 'gemini-3-flash-preview'
export const selectedModel = writable('claude-haiku-4-5');

// Derived: which provider does the current model belong to
export const selectedProvider = derived(selectedModel, ($m) => {
    const m = ($m || '').toLowerCase();
    if (m === 'claude' || m.startsWith('claude-')) return 'claude';
    if (m === 'chatgpt' || m.startsWith('gpt-') || m.startsWith('o1-') ||
        m.startsWith('o3-') || m.startsWith('o4-') || m.startsWith('codex-') ||
        m.startsWith('gpt5') || m.startsWith('gpt-5')) return 'chatgpt';
    if (m === 'gemini' || m.startsWith('gemini-')) return 'gemini';
    if (m.startsWith('arena/')) return 'arena';
    return 'claude';
});

/** @type {import('svelte/store').Writable<any[]>} */
export const messages = writable([]);
/** @type {import('svelte/store').Writable<boolean>} */
export const isLoading = writable(false);

/**
 * Extract thinking/reasoning blocks from model response.
 * @param {string} text
 * @param {string|null} [model] - model id, used to scope untagged heuristic to arena only
 * Returns { thinking: string|null, answer: string }
 */
function extractThinking(text, model = null) {
    let thinking = null;
    let answer = text;

    // Pattern 1: <think>...</think> (DeepSeek, QwQ, etc.)
    const thinkMatch = answer.match(/^([\s\S]*?)<think>([\s\S]*?)<\/think>([\s\S]*)$/i);
    if (thinkMatch) {
        thinking = (thinkMatch[1] + thinkMatch[2]).trim();
        answer = thinkMatch[3].trim();
        return { thinking, answer };
    }

    // Pattern 2: <reasoning>...</reasoning>
    const reasonMatch = answer.match(/^([\s\S]*?)<reasoning>([\s\S]*?)<\/reasoning>([\s\S]*)$/i);
    if (reasonMatch) {
        thinking = (reasonMatch[1] + reasonMatch[2]).trim();
        answer = reasonMatch[3].trim();
        return { thinking, answer };
    }

    // Pattern 3: [thinking]...[/thinking]
    const bracketMatch = answer.match(/^([\s\S]*?)\[thinking\]([\s\S]*?)\[\/thinking\]([\s\S]*)$/i);
    if (bracketMatch) {
        thinking = (bracketMatch[1] + bracketMatch[2]).trim();
        answer = bracketMatch[3].trim();
        return { thinking, answer };
    }

    // Pattern 4: ```thinking ... ``` block
    const codeMatch = answer.match(/^([\s\S]*?)```(?:thinking|reasoning)\n([\s\S]*?)```([\s\S]*)$/i);
    if (codeMatch) {
        thinking = (codeMatch[1] + codeMatch[2]).trim();
        answer = codeMatch[3].trim();
        return { thinking, answer };
    }

    // Pattern 5: Untagged thinking (DeepSeek via arena.ai)
    // Only for arena models — their bridge doesn't strip thinking.
    // Claude/ChatGPT/Gemini adapters never leak raw thinking text.
    const isArena = model && model.startsWith('arena/');
    if (!isArena) return { thinking: null, answer };

    const THINK_MARKERS = /\b(?:the user|I should|I need to|I'll|I can|Let me think|my response|this is a|important to|maybe I|perhaps I|I want to make sure)\b/i;
    const lines = answer.split('\n');
    // Only attempt if first line looks like internal monologue
    if (lines.length > 2 && THINK_MARKERS.test(lines[0])) {
        // Walk paragraphs — thinking paragraphs have monologue markers
        let splitIdx = -1;
        let consecutiveMiss = 0;
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i].trim();
            if (!line) continue; // skip blank lines
            if (THINK_MARKERS.test(line)) {
                consecutiveMiss = 0;
            } else {
                consecutiveMiss++;
                // Two consecutive non-thinking paragraphs = answer started
                if (consecutiveMiss >= 1 && i > 2) {
                    splitIdx = i;
                    break;
                }
            }
        }
        // Also try concatenated split: "...what they need.Of course!" or "...keeps the interaction flowing.That's"
        if (splitIdx === -1) {
            for (let i = 0; i < lines.length; i++) {
                const concatMatch = lines[i].match(/[.!?]([A-Z][a-z])/);
                if (concatMatch && THINK_MARKERS.test(lines.slice(0, i + 1).join('\n'))) {
                    const pos = lines[i].indexOf(concatMatch[0]);
                    const before = lines.slice(0, i).join('\n') + '\n' + lines[i].slice(0, pos + 1);
                    const after = lines[i].slice(pos + 1) + '\n' + lines.slice(i + 1).join('\n');
                    if (before.trim().length > 30 && after.trim().length > 30) {
                        return { thinking: before.trim(), answer: after.trim() };
                    }
                }
            }
        }
        if (splitIdx > 2) {
            thinking = lines.slice(0, splitIdx).join('\n').trim();
            answer = lines.slice(splitIdx).join('\n').trim();
            if (thinking.length > 30 && answer.length > 30) {
                return { thinking, answer };
            }
        }
    }

    return { thinking: null, answer };
}

/**
 * Render assistant content as HTML, collapsing thinking blocks.
 * @param {string} content
 * @param {string|null} [model]
 */
export function renderAssistantHtml(content, model = null) {
    const { thinking, answer } = extractThinking(content, model);
    let html = '';
    if (thinking) {
        const thinkHtml = marked(thinking);
        html += `<details class="thinking-block"><summary class="thinking-summary"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg> Thought process</summary><div class="thinking-content">${thinkHtml}</div></details>`;
    }
    html += marked(answer);
    return html;
}

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
        contentHtml: role === 'assistant' ? renderAssistantHtml(content, model) : null,
        model,
        transport,
        timestamp: new Date().toLocaleTimeString()
    }]);
}

export function clearMessages() {
    messages.set([]);
}

// Thinking effort: "off" | "low" | "medium" | "high"
export const thinkingEffort = writable('off');

// Whether the currently selected model supports extended thinking.
// Mirrors backend/thinking.py prefix lists.
export const modelSupportsThinking = derived(
    [selectedModel, selectedProvider],
    ([$model, $provider]) => {
        const m = ($model || '').toLowerCase();
        if ($provider === 'claude')  return m.startsWith('claude-');
        if ($provider === 'gemini')  return m.startsWith('gemini-2.5-') || m.startsWith('gemini-3');
        if ($provider === 'chatgpt') return m.startsWith('o1') || m.startsWith('o3') || m.startsWith('o4') || m.startsWith('gpt-5.3') || m.startsWith('gpt-5.4');
        return false;
    }
);

// ── Model family grouping ────────────────────────────────────────────────── //

const ARENA_PREFIX_MAP = {
    claude: 'Claude', gpt: 'GPT', o1: 'GPT', o3: 'GPT', o4: 'GPT', codex: 'GPT',
    gemini: 'Gemini', gemma: 'Gemini', grok: 'Grok', qwen: 'Qwen', qwq: 'Qwen',
    deepseek: 'DeepSeek', mistral: 'Mistral', kimi: 'Kimi', minimax: 'MiniMax',
    glm: 'GLM', mimo: 'Mimo', llama: 'Llama', olmo: 'OLMo', step: 'Step',
    ring: 'Ring', ling: 'Ling', mercury: 'Mercury', trinity: 'Trinity',
    ernie: 'Ernie', hunyuan: 'Hunyuan', seed: 'Seed', dola: 'Dola',
};

function detectArenaFamily(id) {
    const slug = id.replace(/^arena\//, '').toLowerCase();
    // try longest prefix first (e.g. "qwen3" before "qwen")
    for (const [prefix, label] of Object.entries(ARENA_PREFIX_MAP).sort((a, b) => b[0].length - a[0].length)) {
        if (slug.startsWith(prefix)) return label;
    }
    // fallback: capitalize first token
    const first = slug.split(/[-._]/)[0];
    return first.charAt(0).toUpperCase() + first.slice(1);
}

function detectClaudeFamily(id) {
    const m = id.toLowerCase();
    if (m.includes('haiku'))  return 'Haiku';
    if (m.includes('sonnet')) return 'Sonnet';
    if (m.includes('opus'))   return 'Opus';
    return 'Other';
}

function detectChatgptFamily(id) {
    const m = id.toLowerCase();
    if (m.startsWith('o4'))    return 'O4';
    if (m.startsWith('o3'))    return 'O3';
    if (m.startsWith('o1'))    return 'O1';
    if (m.startsWith('codex')) return 'Codex';
    if (m.startsWith('gpt-5')) return 'GPT-5';
    if (m.startsWith('gpt-4')) return 'GPT-4';
    return 'Other';
}

function detectGeminiFamily(id) {
    const m = id.toLowerCase();
    if (m.startsWith('gemini-3'))   return 'Gemini 3';
    if (m.startsWith('gemini-2.5')) return 'Gemini 2.5';
    if (m.startsWith('gemini-2'))   return 'Gemini 2';
    return 'Other';
}

/**
 * Group models into families/subcategories.
 * Returns array of [familyName, models[]] sorted by family name.
 * @param {string} provider
 * @param {Array<{id: string, display_name: string, note?: string}>} models
 * @returns {[string, Array][]}
 */
/** Popular arena families — shown first in this order, rest sorted by model count */
const ARENA_POPULAR_ORDER = [
    'GPT', 'Claude', 'Gemini', 'Grok', 'DeepSeek',
    'Qwen', 'Mistral', 'GLM', 'Kimi', 'MiniMax',
];

export function groupModelsByFamily(provider, models) {
    const detectFn =
        provider === 'arena'   ? (m) => detectArenaFamily(m.id) :
        provider === 'claude'  ? (m) => detectClaudeFamily(m.id) :
        provider === 'chatgpt' ? (m) => detectChatgptFamily(m.id) :
        provider === 'gemini'  ? (m) => detectGeminiFamily(m.id) :
        () => 'All';

    const groups = new Map();
    for (const m of models) {
        const family = detectFn(m);
        if (!groups.has(family)) groups.set(family, []);
        groups.get(family).push(m);
    }

    if (provider !== 'arena') {
        return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
    }

    // Arena: popular families first, then by count, small families merged into "Other"
    const popular = [];
    const rest = [];
    const other = [];

    for (const [family, familyModels] of groups.entries()) {
        const idx = ARENA_POPULAR_ORDER.indexOf(family);
        if (idx !== -1) {
            popular.push({ family, models: familyModels, order: idx });
        } else if (familyModels.length >= 3) {
            rest.push({ family, models: familyModels });
        } else {
            other.push(...familyModels);
        }
    }

    popular.sort((a, b) => a.order - b.order);
    rest.sort((a, b) => b.models.length - a.models.length);

    /** @type {[string, Array][]} */
    const result = [];
    for (const p of popular) result.push([p.family, p.models]);
    for (const r of rest) result.push([r.family, r.models]);
    if (other.length > 0) result.push(['Other', other]);

    return result;
}

// Dynamically populated from /api/setup/models on app load.
// Shape: { claude: { tier, models: [{id, display_name, note}] }, chatgpt: {...}, gemini: {...} }
export const availableModels = writable({
    claude: {
        tier: 'unknown',
        models: [
            { id: 'claude-sonnet-4-6',        display_name: 'Claude Sonnet 4.6',       note: 'best quality' },
            { id: 'claude-opus-4-6',          display_name: 'Claude Opus 4.6',         note: 'most capable' },
            { id: 'claude-sonnet-4-5',        display_name: 'Claude Sonnet 4.5',       note: 'balanced' },
            { id: 'claude-haiku-4-5',         display_name: 'Claude Haiku 4.5',        note: 'fast' },
        ],
    },
    chatgpt: {
        tier: 'unknown',
        models: [
            { id: 'gpt-5.4',          display_name: 'GPT-5.4',          note: 'best quality' },
            { id: 'gpt-5.3-codex',    display_name: 'GPT-5.3 Codex',    note: 'balanced' },
            { id: 'gpt-5.2',          display_name: 'GPT-5.2',          note: 'fast' },
            { id: 'gpt-5.1-codex-mini', display_name: 'GPT-5.1 Codex Mini', note: 'most quota' },
        ],
    },
    gemini: {
        tier: 'unknown',
        models: [
            { id: 'gemini-3.1-pro-preview',        display_name: 'Gemini 3.1 Pro Preview',        note: 'best quality' },
            { id: 'gemini-3-pro-preview',           display_name: 'Gemini 3 Pro Preview',          note: 'best quality' },
            { id: 'gemini-3.1-flash-lite-preview',  display_name: 'Gemini 3.1 Flash Lite Preview', note: 'fast' },
            { id: 'gemini-3-flash-preview',         display_name: 'Gemini 3 Flash Preview',        note: 'fast' },
            { id: 'gemini-2.5-pro',                 display_name: 'Gemini 2.5 Pro',                note: 'balanced' },
            { id: 'gemini-2.5-flash',               display_name: 'Gemini 2.5 Flash',              note: 'balanced' },
            { id: 'gemini-2.5-flash-lite',          display_name: 'Gemini 2.5 Flash Lite',         note: 'most quota' },
        ],
    },
    arena: {
        tier: 'unknown',
        models: [],
    },
});

// Pre-computed model family groupings — recalculates only when availableModels changes.
export const groupedModels = derived(availableModels, ($models) => {
    const result = {};
    for (const [provider, data] of Object.entries($models)) {
        if (data.models?.length) {
            result[provider] = groupModelsByFamily(provider, data.models);
        }
    }
    return result;
});

// O(1) display name lookup — rebuilt only when availableModels changes.
function formatModelId(id) {
    return id
        .replace(/^arena\//, '')
        .split(/[-_]/)
        .map(t => /^\d/.test(t) ? t.replace(/-/g, '.') : t.charAt(0).toUpperCase() + t.slice(1))
        .join(' ')
        .replace(/(\d) (\d)/g, '$1.$2');
}

const modelDisplayMap = derived(availableModels, ($models) => {
    const map = new Map();
    for (const data of Object.values($models)) {
        if (!data.models) continue;
        for (const m of data.models) map.set(m.id, m.display_name);
    }
    return map;
});

export const selectedModelDisplay = derived(
    [selectedModel, modelDisplayMap],
    ([$model, $map]) => $map.get($model) || formatModelId($model)
);
