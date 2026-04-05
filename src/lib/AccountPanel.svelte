<script>
    import { onMount } from 'svelte';
    import axios from 'axios';

    const BASE_URL = 'http://localhost:8000/api';

    let accounts = [];
    let showForm = false;
    let loading = false;
    let error = '';
    let success = '';

    let form = {
        model: 'chatgpt',
        username: '',
        password: ''
    };

    const models = [
        { value: 'gemini', label: 'Gemini' },
        { value: 'claude', label: 'Claude' }
    ];

    const statusColors = {
        active: '#3ecf8e',
        rate_limited: '#f59e0b',
        banned: '#ef4444'
    };

    onMount(fetchAccounts);

    async function fetchAccounts() {
        try {
            const res = await axios.get(`${BASE_URL}/accounts`);
            accounts = res.data.accounts;
        } catch (e) {
            error = 'Failed to load accounts';
        }
    }

    async function addAccount() {
        if (!form.username || !form.password) {
            error = 'Username and password required';
            return;
        }
        loading = true;
        error = '';
        try {
            await axios.post(`${BASE_URL}/accounts`, form);
            success = 'Account added successfully';
            form = { model: 'chatgpt', username: '', password: '' };
            showForm = false;
            await fetchAccounts();
            setTimeout(() => success = '', 3000);
        } catch (e) {
            error = e.response?.data?.detail || 'Failed to add account';
        } finally {
            loading = false;
        }
    }

    async function deleteAccount(id) {
        try {
            await axios.delete(`${BASE_URL}/accounts/${id}`);
            await fetchAccounts();
        } catch (e) {
            error = 'Failed to delete account';
        }
    }

    function groupByModel(accounts) {
        return accounts.reduce((groups, acc) => {
            if (!groups[acc.model]) groups[acc.model] = [];
            groups[acc.model].push(acc);
            return groups;
        }, {});
    }

    $: grouped = groupByModel(accounts);
</script>

<div class="panel">
    <div class="panel-header">
        <h2>Accounts</h2>
        <button class="add-btn" on:click={() => showForm = !showForm}>
            {showForm ? 'Cancel' : '+ Add'}
        </button>
    </div>

    {#if error}
        <div class="alert error">{error}</div>
    {/if}

    {#if success}
        <div class="alert success">{success}</div>
    {/if}

    {#if showForm}
        <div class="form">
            <select bind:value={form.model}>
                {#each models as m}
                    <option value={m.value}>{m.label}</option>
                {/each}
            </select>
            <input
                type="text"
                placeholder="Email or username"
                bind:value={form.username}
            />
            <input
                type="password"
                placeholder="Password"
                bind:value={form.password}
            />
            <button class="submit-btn" on:click={addAccount} disabled={loading}>
                {loading ? 'Adding...' : 'Add Account'}
            </button>
        </div>
    {/if}

<div class="claude-note">
    <span class="dot green"></span>
    <p>Claude is authenticated via OpenClaude CLI — no credentials needed.</p>
</div>

{#if accounts.length === 0}
    <p class="empty">No browser model accounts added yet.</p>
    {:else}
        {#each Object.entries(grouped) as [model, accs]}
            <div class="model-group">
                <p class="group-label">{model}</p>
                {#each accs as acc}
                    <div class="account-row">
                        <span
                            class="status-dot"
                            style="background: {statusColors[acc.status] || '#555'}"
                        ></span>
                        <div class="account-info">
                            <span class="username">{acc.username}</span>
                            <span class="status-text">{acc.status}</span>
                        </div>
                        <button
                            class="delete-btn"
                            on:click={() => deleteAccount(acc.id)}
                        >✕</button>
                    </div>
                {/each}
            </div>
        {/each}
    {/if}
</div>

<style>
    .panel {
        display: flex;
        flex-direction: column;
        gap: 12px;
        padding: 16px;
        height: 100%;
        overflow-y: auto;
    }

    .claude-note {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 10px 12px;
        background: #0f1f0f;
        border: 1px solid #1a3a1a;
        border-radius: 6px;
        font-size: 12px;
        color: #3ecf8e;
    }
    
    .dot {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        flex-shrink: 0;
    }
    
    .dot.green { background: #3ecf8e; }
        .panel-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

    h2 {
        font-size: 14px;
        font-weight: 500;
        color: #fff;
    }

    .add-btn {
        background: #1a1a3a;
        border: 1px solid #2a2a5a;
        color: #8888ff;
        padding: 4px 10px;
        border-radius: 6px;
        font-size: 12px;
        cursor: pointer;
    }

    .add-btn:hover { background: #22224a; }

    .form {
        display: flex;
        flex-direction: column;
        gap: 8px;
        padding: 12px;
        background: #161616;
        border: 1px solid #2a2a2a;
        border-radius: 8px;
    }

    .form select,
    .form input {
        background: #0f0f0f;
        border: 1px solid #2a2a2a;
        border-radius: 6px;
        color: #e8e8e8;
        font-size: 13px;
        padding: 8px 10px;
        outline: none;
        width: 100%;
    }

    .form select:focus,
    .form input:focus { border-color: #3a3a5a; }

    .submit-btn {
        background: #1a1a3a;
        border: 1px solid #2a2a5a;
        color: #8888ff;
        padding: 8px;
        border-radius: 6px;
        font-size: 13px;
        cursor: pointer;
        font-weight: 500;
    }

    .submit-btn:hover:not(:disabled) { background: #22224a; }
    .submit-btn:disabled { opacity: 0.5; cursor: not-allowed; }

    .alert {
        padding: 8px 12px;
        border-radius: 6px;
        font-size: 12px;
    }

    .alert.error { background: #2a1a1a; border: 1px solid #5a2a2a; color: #ff8888; }
    .alert.success { background: #1a2a1a; border: 1px solid #2a5a2a; color: #88ff88; }

    .empty {
        font-size: 13px;
        color: #444;
        text-align: center;
        margin-top: 20px;
    }

    .model-group { display: flex; flex-direction: column; gap: 6px; }

    .group-label {
        font-size: 11px;
        color: #555;
        text-transform: uppercase;
        letter-spacing: 0.8px;
    }

    .account-row {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 8px 10px;
        background: #161616;
        border: 1px solid #2a2a2a;
        border-radius: 6px;
    }

    .status-dot {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        flex-shrink: 0;
    }

    .account-info {
        display: flex;
        flex-direction: column;
        flex: 1;
        gap: 2px;
    }

    .username { font-size: 13px; color: #e8e8e8; }
    .status-text { font-size: 11px; color: #555; }

    .delete-btn {
        background: transparent;
        border: none;
        color: #555;
        font-size: 12px;
        cursor: pointer;
        padding: 2px 6px;
        border-radius: 4px;
    }

    .delete-btn:hover { color: #ff8888; background: #2a1a1a; }
</style>