import axios from 'axios';

const BASE_URL = 'http://localhost:8000/api';

export async function sendChat(model, message) {
    const res = await axios.post(`${BASE_URL}/chat`, { model, message });
    return res.data.response;
}

export async function getModels() {
    const res = await axios.get(`${BASE_URL}/models`);
    return res.data.models;
}

export async function clearHistory(model = null) {
    const params = model ? `?model=${model}` : '';
    await axios.post(`${BASE_URL}/chat/clear${params}`);
}

export async function getSetupStatus() {
    const res = await axios.get(`${BASE_URL}/setup/status`);
    return res.data;
}