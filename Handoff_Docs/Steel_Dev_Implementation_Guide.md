# Implementation Plan: Steel.dev Migration for Arena.ai

**Version:** 1.0  
**Status:** Design Ready  
**Objective:** Replace the brittle Chrome Extension + Native Host bridge with a high-reliability **Steel.dev** integration.

---

## 1. Prerequisites & Dependencies

### **A. Backend Requirements**
- **Python Package:** `steel-sdk` (Official async-first client).
- **Environment Variable:** `STEEL_API_KEY` (Optional for cloud) or `STEEL_BASE_URL` (For self-hosting).

### **B. Infrastructure (Self-Hosted)**
The system will recommend users run a local Steel engine via Docker:
```bash
docker run -d --name steel-browser \
  -p 3000:3000 -p 9223:9223 \
  ghcr.io/steel-dev/steel-browser:latest
```

---

## 2. Architectural Changes

### **Decommissioned Components (To be deleted)**
- `Ilee_AI/arena_extension/` (Chrome Extension)
- `Ilee_AI/native_host/` (Python StdIO Bridge)
- `Ilee_AI/backend/services/arena_bridge_client.py` (Native Host Client)

### **New Components (To be added)**
- **`Ilee_AI/backend/adapters/arena_steel_adapter.py`**: The core integration logic using `steel-sdk`.
- **`Ilee_AI/backend/services/steel_orchestrator.py`**: Handles browser session lifecycle (Session creation, cleanup, and "Standby" management).

---

## 3. The "Injected Fetch" Porting Strategy

We will reuse the **Direct Fetch** logic from the existing `page_bridge.js`. Instead of being injected by an extension, it will be injected by Python via `page.evaluate()`.

### **Ported Logic Flow:**
1. **Initialize:** Start/Attach to a Steel session at `https://arena.ai/text/direct`.
2. **Inject:** Run a JS block that creates the `fetch()` request to `/nextjs-api/stream`.
3. **Listen:** Use Steel's **Network Interception** or `exposeBinding` to pipe SSE chunks back to Python in real-time.
4. **Tool Calls:** Extract `tool_calls` from the SSE stream metadata frames.

---

## 4. Step-by-Step Implementation Guide

### **Step 1: Environment Setup**
- Update `requirements.txt` with `steel-sdk`.
- Add `STEEL_BASE_URL=http://localhost:3000` to `.env`.

### **Step 2: Create the Steel Adapter**
The `ArenaSteelAdapter` will inherit from the same interface as the previous bridge but use `AsyncSteel`.
- **Session Persistence:** It will use Steel's `sessions.create(use_proxy=True)` to ensure high-trust IP reputation.
- **Model Health:** It will continue to use `ArenaModelHealthStore` to hide 404/422 models.

### **Step 3: Update ArenaManager**
- Modify `backend/arena_manager.py` to check for the health of the local Steel Docker container (port 3000) instead of the Native Host socket.

### **Step 4: Frontend UI Updates**
- **Setup Screen:** Replace "Install Extension" instructions with "Run Steel Docker Container" instructions.
- **Status Indicator:** Change "Bridge Connected" to "Steel Browser Ready."

---

## 5. Summary of Improvements
1. **Reliability:** 100% bypass of "403 Forbidden" due to Steel's hardened stealth patches.
2. **Simplification:** Removes the need for users to keep a Chrome tab open or manage extension permissions.
3. **Performance:** Sub-second startup compared to the previous native host "handshake" lag.
4. **Tool Calling:** Native support via direct SSE observation in the browser's network pipe.

---

## 6. Guidance for Next Agent
When implementing `arena_steel_adapter.py`, ensure you use **"Managed Sessions."** This prevents Arena from logging the user out. The user should log in once via the Steel UI (`http://localhost:3000/ui`) and the session should be persisted in a Docker volume.
