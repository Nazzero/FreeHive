<script>
    import { messages, isLoading, selectedModel, addMessage } from '$lib/store.js';
    import { sendChat } from '$lib/api.js';
    import { tick } from 'svelte';

    let input = '';
    let chatContainer;

    async function handleSubmit() {
        if (!input.trim() || $isLoading) return;

        const userMessage = input.trim();
        input = '';

        addMessage('user', userMessage);
        $isLoading = true;

        await tick();
        scrollToBottom();

        try {
            const response = await sendChat($selectedModel, userMessage);
            addMessage('assistant', response, $selectedModel);
        } catch (err) {
            addMessage('assistant', `Error: ${err.message}`, $selectedModel);
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

    function handleKeydown(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSubmit();
        }
    }
</script>

<div class="app">
    <aside class="sidebar">
        <div class="sidebar-header">
            <h1>FreeHive</h1>
            <span class="version">v0.1.0</span>
        </div>

        <div class="model-section">
            <p class="section-label">Model</p>
            <div class="model-list">
                <button
                    class="model-btn {$selectedModel === 'claude' ? 'active' : ''}"
                    on:click={() => $selectedModel = 'claude'}>
                    <span class="dot green"></span> Claude
                </button>
                <button class="model-btn disabled" disabled>
                    <span class="dot gray"></span> ChatGPT
                    <span class="soon">soon</span>
                </button>
                <button class="model-btn disabled" disabled>
                    <span class="dot gray"></span> Gemini
                    <span class="soon">soon</span>
                </button>
            </div>
        </div>
    </aside>

    <main class="chat-area">
        <div class="chat-header">
            <span class="active-model">{$selectedModel}</span>
            <span class="status-dot"></span>
            <span class="status-text">connected</span>
        </div>

        <div class="messages" bind:this={chatContainer}>
            {#if $messages.length === 0}
                <div class="empty-state">
                    <p>FreeHive is running.</p>
                    <p class="sub">Send a message to start.</p>
                </div>
            {/if}

            {#each $messages as msg (msg.id)}
                <div class="message {msg.role}">
                    <div class="bubble">
                        <p>{msg.content}</p>
                    </div>
                    <span class="meta">{msg.role === 'assistant' ? msg.model : 'you'} · {msg.timestamp}</span>
                </div>
            {/each}

            {#if $isLoading}
                <div class="message assistant">
                    <div class="bubble loading">
                        <span></span><span></span><span></span>
                    </div>
                </div>
            {/if}
        </div>

        <div class="input-area">
            <textarea
                bind:value={input}
                on:keydown={handleKeydown}
                placeholder="Message {$selectedModel}... (Enter to send)"
                rows="1"
                disabled={$isLoading}
            ></textarea>
            <button on:click={handleSubmit} disabled={$isLoading || !input.trim()}>
                Send
            </button>
        </div>
    </main>
</div>

<style>
    * { box-sizing: border-box; margin: 0; padding: 0; }

    .app {
        display: flex;
        height: 100vh;
        background: #0f0f0f;
        color: #e8e8e8;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }

    .sidebar {
        width: 220px;
        background: #161616;
        border-right: 1px solid #2a2a2a;
        display: flex;
        flex-direction: column;
        padding: 20px 14px;
        gap: 24px;
        flex-shrink: 0;
    }

    .sidebar-header h1 {
        font-size: 18px;
        font-weight: 600;
        color: #fff;
        letter-spacing: -0.3px;
    }

    .version {
        font-size: 11px;
        color: #555;
    }

    .section-label {
        font-size: 11px;
        color: #555;
        text-transform: uppercase;
        letter-spacing: 0.8px;
        margin-bottom: 8px;
    }

    .model-list {
        display: flex;
        flex-direction: column;
        gap: 4px;
    }

    .model-btn {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 8px 10px;
        border-radius: 6px;
        border: none;
        background: transparent;
        color: #aaa;
        font-size: 13px;
        cursor: pointer;
        text-align: left;
        transition: background 0.15s;
        position: relative;
    }

    .model-btn:hover:not(.disabled) { background: #1f1f1f; color: #fff; }
    .model-btn.active { background: #1f1f1f; color: #fff; }
    .model-btn.disabled { opacity: 0.4; cursor: not-allowed; }

    .dot {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        flex-shrink: 0;
    }
    .dot.green { background: #3ecf8e; }
    .dot.gray { background: #555; }

    .soon {
        margin-left: auto;
        font-size: 10px;
        color: #555;
        background: #222;
        padding: 2px 6px;
        border-radius: 4px;
    }

    .chat-area {
        flex: 1;
        display: flex;
        flex-direction: column;
        overflow: hidden;
    }

    .chat-header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 16px 24px;
        border-bottom: 1px solid #1f1f1f;
        font-size: 13px;
        color: #aaa;
    }

    .active-model {
        font-weight: 500;
        color: #fff;
        text-transform: capitalize;
    }

    .status-dot {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: #3ecf8e;
    }

    .status-text { color: #3ecf8e; font-size: 12px; }

    .messages {
        flex: 1;
        overflow-y: auto;
        padding: 24px;
        display: flex;
        flex-direction: column;
        gap: 16px;
    }

    .messages::-webkit-scrollbar { width: 4px; }
    .messages::-webkit-scrollbar-thumb { background: #2a2a2a; border-radius: 4px; }

    .empty-state {
        margin: auto;
        text-align: center;
        color: #444;
    }

    .empty-state p { font-size: 15px; }
    .empty-state .sub { font-size: 13px; margin-top: 6px; }

    .message {
        display: flex;
        flex-direction: column;
        gap: 4px;
        max-width: 75%;
    }

    .message.user { align-self: flex-end; align-items: flex-end; }
    .message.assistant { align-self: flex-start; align-items: flex-start; }

    .bubble {
        padding: 10px 14px;
        border-radius: 12px;
        font-size: 14px;
        line-height: 1.6;
        white-space: pre-wrap;
        word-break: break-word;
    }

    .message.user .bubble {
        background: #1a1a2e;
        border: 1px solid #2a2a4a;
        color: #e8e8ff;
    }

    .message.assistant .bubble {
        background: #1a1a1a;
        border: 1px solid #2a2a2a;
        color: #e8e8e8;
    }

    .bubble.loading {
        display: flex;
        gap: 5px;
        align-items: center;
        padding: 12px 16px;
    }

    .bubble.loading span {
        width: 6px;
        height: 6px;
        background: #555;
        border-radius: 50%;
        animation: pulse 1.2s infinite;
    }

    .bubble.loading span:nth-child(2) { animation-delay: 0.2s; }
    .bubble.loading span:nth-child(3) { animation-delay: 0.4s; }

    @keyframes pulse {
        0%, 100% { opacity: 0.3; transform: scale(0.8); }
        50% { opacity: 1; transform: scale(1); }
    }

    .meta {
        font-size: 11px;
        color: #444;
        padding: 0 4px;
    }

    .input-area {
        display: flex;
        gap: 10px;
        padding: 16px 24px;
        border-top: 1px solid #1f1f1f;
        background: #0f0f0f;
    }

    textarea {
        flex: 1;
        background: #161616;
        border: 1px solid #2a2a2a;
        border-radius: 8px;
        color: #e8e8e8;
        font-size: 14px;
        padding: 10px 14px;
        resize: none;
        outline: none;
        font-family: inherit;
        line-height: 1.5;
        transition: border 0.15s;
    }

    textarea:focus { border-color: #3a3a5a; }
    textarea:disabled { opacity: 0.5; }

    button {
        background: #1a1a3a;
        border: 1px solid #2a2a5a;
        color: #8888ff;
        padding: 10px 18px;
        border-radius: 8px;
        font-size: 13px;
        font-weight: 500;
        cursor: pointer;
        transition: all 0.15s;
        white-space: nowrap;
    }

    button:hover:not(:disabled) {
        background: #22224a;
        color: #aaaaff;
    }

    button:disabled { opacity: 0.4; cursor: not-allowed; }
</style>