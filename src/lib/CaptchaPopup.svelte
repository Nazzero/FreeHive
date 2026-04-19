<script>
    import { onMount, onDestroy } from 'svelte';
    import { getArenaCaptcha, solveArenaCaptcha } from '$lib/api.js';

    /** @type {boolean} */
    export let visible = false;

    let pending = false;
    let imageBase64 = '';
    let instruction = '';
    let gridSize = 3;
    /** @type {Set<number>} */
    let selectedCells = new Set();
    let submitting = false;
    let error = '';
    /** @type {ReturnType<typeof setInterval> | null} */
    let pollTimer = null;

    onMount(() => {
        pollTimer = setInterval(pollCaptcha, 2000);
        pollCaptcha();
    });

    onDestroy(() => {
        if (pollTimer) clearInterval(pollTimer);
    });

    async function pollCaptcha() {
        try {
            const state = await getArenaCaptcha();
            if (state.pending && state.image) {
                pending = true;
                visible = true;
                imageBase64 = state.image;
                instruction = state.instruction || 'Select the matching images';
                gridSize = state.grid_size || 3;
                // Don't clear selections if image hasn't changed (same round)
            } else if (pending) {
                // Captcha resolved
                pending = false;
                visible = false;
                selectedCells = new Set();
                error = '';
            }
        } catch {
            // Backend not available — ignore
        }
    }

    function toggleCell(cellNum) {
        if (submitting) return;
        const next = new Set(selectedCells);
        if (next.has(cellNum)) {
            next.delete(cellNum);
        } else {
            next.add(cellNum);
        }
        selectedCells = next;
    }

    async function submit() {
        if (selectedCells.size === 0 || submitting) return;
        submitting = true;
        error = '';
        try {
            await solveArenaCaptcha([...selectedCells]);
            selectedCells = new Set();
            // Poll immediately to check if solved or new round
            await pollCaptcha();
        } catch (e) {
            error = e?.message || 'Failed to submit';
        } finally {
            submitting = false;
        }
    }

    function dismiss() {
        visible = false;
    }
</script>

{#if visible && pending}
    <div class="captcha-overlay" on:click|self={dismiss}>
        <div class="captcha-popup">
            <div class="captcha-header">
                <span class="captcha-title">Arena Captcha Required</span>
                <button class="captcha-close" on:click={dismiss}>&times;</button>
            </div>

            <div class="captcha-instruction">{instruction}</div>

            <div class="captcha-grid" style="--cols: {gridSize}">
                {#each Array(gridSize * gridSize) as _, i}
                    {@const cellNum = i + 1}
                    <button
                        class="captcha-cell"
                        class:selected={selectedCells.has(cellNum)}
                        on:click={() => toggleCell(cellNum)}
                        disabled={submitting}
                    >
                        <span class="cell-number">{cellNum}</span>
                        {#if selectedCells.has(cellNum)}
                            <span class="cell-check">&#10003;</span>
                        {/if}
                    </button>
                {/each}
            </div>

            <div class="captcha-image-container">
                <img
                    src="data:image/png;base64,{imageBase64}"
                    alt="Captcha challenge"
                    class="captcha-image"
                />
            </div>

            {#if error}
                <div class="captcha-error">{error}</div>
            {/if}

            <div class="captcha-actions">
                <button
                    class="captcha-submit"
                    on:click={submit}
                    disabled={selectedCells.size === 0 || submitting}
                >
                    {submitting ? 'Submitting...' : 'Verify'}
                </button>
                <span class="captcha-hint">Click tile numbers matching the image, then Verify</span>
            </div>
        </div>
    </div>
{/if}

<style>
    .captcha-overlay {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.6);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 9999;
        backdrop-filter: blur(2px);
    }

    .captcha-popup {
        background: var(--bg-primary, #1a1a2e);
        border: 1px solid var(--border-medium, #333);
        border-radius: 12px;
        padding: 20px;
        max-width: 480px;
        width: 90vw;
        box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
    }

    .captcha-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 12px;
    }

    .captcha-title {
        font-size: 15px;
        font-weight: 600;
        color: var(--text-primary, #e0e0e0);
    }

    .captcha-close {
        background: none;
        border: none;
        color: var(--text-secondary, #888);
        font-size: 20px;
        cursor: pointer;
        padding: 2px 6px;
        border-radius: 4px;
    }
    .captcha-close:hover {
        color: var(--text-primary, #e0e0e0);
        background: var(--bg-secondary, #2a2a3e);
    }

    .captcha-instruction {
        font-size: 13px;
        color: var(--text-secondary, #aaa);
        margin-bottom: 12px;
        text-align: center;
    }

    .captcha-image-container {
        margin-bottom: 12px;
        border-radius: 8px;
        overflow: hidden;
        border: 1px solid var(--border-medium, #333);
    }

    .captcha-image {
        width: 100%;
        display: block;
    }

    .captcha-grid {
        display: grid;
        grid-template-columns: repeat(var(--cols), 1fr);
        gap: 4px;
        margin-bottom: 12px;
    }

    .captcha-cell {
        position: relative;
        aspect-ratio: 1;
        background: var(--bg-secondary, #2a2a3e);
        border: 2px solid var(--border-medium, #444);
        border-radius: 6px;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 14px;
        color: var(--text-secondary, #888);
        transition: all 0.15s ease;
    }

    .captcha-cell:hover:not(:disabled) {
        border-color: var(--accent, #6366f1);
        background: var(--bg-tertiary, #333);
    }

    .captcha-cell.selected {
        border-color: #22c55e;
        background: rgba(34, 197, 94, 0.15);
    }

    .captcha-cell:disabled {
        opacity: 0.5;
        cursor: not-allowed;
    }

    .cell-number {
        font-weight: 500;
    }

    .cell-check {
        position: absolute;
        top: 2px;
        right: 4px;
        font-size: 12px;
        color: #22c55e;
        font-weight: bold;
    }

    .captcha-error {
        color: #ef4444;
        font-size: 12px;
        margin-bottom: 8px;
        text-align: center;
    }

    .captcha-actions {
        display: flex;
        align-items: center;
        gap: 12px;
    }

    .captcha-submit {
        background: #22c55e;
        color: #fff;
        border: none;
        padding: 8px 20px;
        border-radius: 6px;
        font-size: 14px;
        font-weight: 600;
        cursor: pointer;
        transition: background 0.15s;
    }
    .captcha-submit:hover:not(:disabled) {
        background: #16a34a;
    }
    .captcha-submit:disabled {
        opacity: 0.5;
        cursor: not-allowed;
    }

    .captcha-hint {
        font-size: 11px;
        color: var(--text-secondary, #888);
    }
</style>
