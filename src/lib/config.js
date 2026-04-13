const DEFAULT_API_BASE_URL = "http://127.0.0.1:7200/api";

const rawBaseUrl = String(import.meta.env.VITE_API_BASE_URL || "").trim();

export const API_BASE_URL = rawBaseUrl || DEFAULT_API_BASE_URL;
export const API_ROOT_URL = API_BASE_URL.replace(/\/api\/?$/, "");
