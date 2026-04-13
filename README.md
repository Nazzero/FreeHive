# FreeHive

FreeHive is a local-first desktop app that works like a **universal remote for AI**.


Instead of managing separate apps and separate chat histories for Claude, ChatGPT, and Gemini, you use one interface and one local conversation store.

## What FreeHive Does

- Gives you one chat UI for multiple AI providers
- Keeps conversation history on your machine (SQLite)
- Lets external tools use FreeHive through API-compatible endpoints
- Routes requests by provider/model without changing your workflow

## Purpose and Benefits

FreeHive solves the “walled garden” problem:

- Without FreeHive: each provider has separate login, separate UI, separate chat context
- With FreeHive: one local hub, one workflow, provider switching with less friction

Benefits:

- Local-first: your chat/session data is stored locally
- Flexible: use desktop app or API from other tools
- Practical: point existing OpenAI/Anthropic-compatible clients at FreeHive

## How It Works (Simple)

FreeHive has 3 layers:
remove this!
1. Frontend (`Svelte + Tauri`)  
2. Backend (`FastAPI`)
3. Provider adapters (`backend/adapters/*`)

Flow:
remove this
1. You send a prompt from the app (or API client)
2. Backend picks the correct adapter (Claude/ChatGPT/Gemini)
3. Response comes back and is stored in local DB
4. UI shows the result

## Install and Use (Recommended for Most Users)

If you have a release installer:

- Windows: install `.exe`/MSI
- macOS: install `.app`/`.dmg`
- Linux: install `.deb`/`.rpm`

After install:

1. Launch FreeHive
2. Open **Accounts** and connect your provider(s)
3. Open **Settings → API Keys** to copy model keys for external tools

Notes:

- FreeHive backend runs locally at `http://127.0.0.1:7200`
- No FreeHive cloud backend is required for normal app usage

## Run From Source (Developer/Power User)

### Prerequisites

- Node.js + npm
- Python 3.10+
- Rust (for Tauri desktop builds)

### 1) Clone

```bash
git clone <your-repo-url>
cd Ilee_AI
```

### 2) Python env + dependencies

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 3) Frontend dependencies

```bash
npm install
```

### 4) Run backend + frontend

Terminal 1:

```bash
source venv/bin/activate
uvicorn backend.main:app --host 127.0.0.1 --port 7200 --reload
```

Terminal 2:

```bash
npm run dev
```

Open:

- Frontend: `http://localhost:1420`
- Backend: `http://127.0.0.1:7200`

## Build Desktop Installers

Build must run on the target OS:
remove
- Windows machine for Windows installer
- macOS machine for macOS installer
- Linux machine for Linux packages

### Build command

```bash
npm run tauri build
```

This build now includes:
remove
- Frontend build
- Backend sidecar build (`freehive-backend` / `freehive-backend.exe`)
- Tauri bundle creation

If you only want Linux `.deb` + `.rpm`:
remove
```bash
npm run tauri build -- --bundles deb,rpm
```

## Use FreeHive With Other Open-Source Projects (API)

FreeHive includes compatibility endpoints:

- Anthropic-style: `POST /v1/messages`
- OpenAI-style: `POST /v1/chat/completions`

### Base URLs

- Anthropic-compatible clients: `http://127.0.0.1:7200`
- OpenAI-compatible clients: `http://127.0.0.1:7200/v1`

### API Keys

In FreeHive app:
already said
1. Go to **Settings**
2. Open **API Keys**
3. Copy a key for the model you want

Recommended key format:
redundant
- `freehive-<model-id>`  
  Example: `freehive-gpt-5.2`, `freehive-claude-sonnet-4-5`

Provider shortcuts also work:

- `freehive-claude`
- `freehive-chatgpt`
- `freehive-gemini`

### Example: Cursor / Continue.dev (OpenAI format)

- Base URL: `http://127.0.0.1:7200/v1`
- API key: `freehive-<model-id>`
- Model field: any value (key decides routing)

### Example: Claude Code / Anthropic-compatible clients

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:7200
export ANTHROPIC_API_KEY=freehive-claude-sonnet-4-5
```

### Example: Python OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:7200/v1",
    api_key="freehive-gpt-5.2",
)
```

### Example: Python Anthropic SDK

```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://127.0.0.1:7200",
    api_key="freehive-claude-sonnet-4-5",
)
```

## Data, State, and Privacy

Local files used by FreeHive:

- Conversations DB: `~/.freehive/conversations.db`
- App config: `~/.freehive/config.json`
- Provider auth files (provider-owned CLIs), including:
  - `~/.claude/.credentials.json`
  - `~/.codex/auth.json`
  - `~/.gemini/oauth_creds.json`

## Current Scope Note (Arena)

Arena integration is currently disabled by default and hidden from normal usage.

- Flag: `FREEHIVE_ENABLE_ARENA=1` (development only)
- Default behavior: Arena endpoints/models are blocked

## Upcoming Implementations

Planned next additions:

- Arena v2 return:
  - Re-enable Arena behind the existing feature flag path
  - Keep Arena isolated from core Claude/ChatGPT/Gemini routing until fully validated
  - Add dedicated end-to-end tests before turning it on by default

- Qwen provider addition:
  - Add a dedicated Qwen adapter in `backend/adapters/`
  - Add setup/auth + model discovery wiring so Qwen appears in Accounts/Settings
  - Add API-key routing support (`freehive-qwen` and model-specific `freehive-qwen-*`)

## Troubleshooting
remove
- Backend unreachable: verify backend is running on `127.0.0.1:7200`
- No models in UI: authenticate at least one provider in **Accounts**
- Tauri build fails on sidecar: install PyInstaller in your Python environment
- Linux AppImage errors: use `--bundles deb,rpm` unless AppImage tooling is installed

## Key Project Paths
remove
- Backend app entry: `backend/main.py`
- Core API routes: `backend/router.py`
- Compat API routes: `backend/compat_router.py`
- Provider setup/auth routes: `backend/setup_router.py`
- Frontend API wrapper: `src/lib/api.js`
- Settings/API key UI: `src/lib/SettingsPage.svelte`
- Tauri sidecar management: `src-tauri/src/lib.rs`
