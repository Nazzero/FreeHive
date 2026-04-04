import { writable } from 'svelte/store';

export const messages = writable([]);
export const isLoading = writable(false);
export const selectedModel = writable('claude');

export function addMessage(role, content, model = null) {
    messages.update(msgs => [...msgs, {
        id: Date.now(),
        role,
        content,
        model,
        timestamp: new Date().toLocaleTimeString()
    }]);
}

export function clearMessages() {
    messages.set([]);
}