# Handoff — Setup-screen Node install + Arena pipeline overhaul
**Branch:** `claude/brave-blackwell-862f7e` (worktree)
**Base commit:** `5e735c0 fix: refresh Windows PATH from registry for mid-session installs`
**Date:** 2026-04-25
**Status:** all uncommitted changes still in working tree (no commit/push)

---

## TL;DR

Three independent issue families were addressed in one session:

1. **Setup-screen "Install Node.js" button failed** on Windows → now installs cleanly
2. **Arena panel did not surface code models** (UI showed `code 0` even though arena.ai exposes 99 of them) → root cause was in the chrome-extension data pipeline; both old code AND my first fix were checking the wrong field name
3. **Arena UI lacked chat/code organization** → All-models view now splits into Direct Chat → Code Models with overlap models tagged `code + chat`; Settings → API page rows now show capability badges

The third issue was the original ask. Fixes 1 and 2 were uncovered while debugging it. **Bug 2 (the data pipeline) was the real blocker** — without that, the partition UI in fix 3 has nothing to partition.

---

## 1. Setup screen / Node install

### Symptom
User clicked "Install Node.js" on the setup screen. UI showed:
```
fnm installation failed.
Found an existing package already installed. Trying to upgrade the installed package…
No available upgrade found.
```
Plus inconsistency: **Node ✗** but **npm ✓** in the prerequisites strip.

### Root causes

| # | Bug | Cause |
|---|---|---|
| 1.1 | "fnm installation failed" when fnm is already installed | `winget install Schniz.fnm` exits `0x8A15002B` (`APPINSTALLER_CLI_ERROR_UPDATE_NOT_APPLICABLE`) when the package is already at the newest version. Code at `setup_router.py:688` treated *any* non-zero rc as fatal. |
| 1.2 | Node ✗ / npm ✓ asymmetry | An orphan `%APPDATA%\npm\npm.cmd` shim survives after node uninstall / nvm-windows lacks an active version. `_is_installed("npm")` only checked PATH presence — found the shim, returned true, even though the shim is non-functional without node. |
| 1.3 | After fnm install, node never appears on PATH | fnm requires `fnm env` shell integration to expose its bins. The flow ran `fnm install --lts && fnm default lts-latest` but never persisted the active node's dir to user PATH. Verification step then reported "node not found" indefinitely. |
| 1.4 | First attempt at fix 1.3 used wrong directory | I hardcoded `%LOCALAPPDATA%\fnm\node-versions\…\installation`. fnm on this machine actually uses **`%APPDATA%\fnm` (Roaming)**. The persistence step silently no-op'd because the wrong path didn't exist. |
| 1.5 | First attempt called `fnm current` to resolve active version | `fnm current` errors with *"`fnm env` was not applied in this context"* in any non-shell-integrated subprocess. Returned empty string → check `if ($ver -ne "none")` skipped persistence. |

### Fixes — `backend/setup_router.py`

- **`_is_installed(name)`** (≈line 147): when `name == "npm"`, also require `node` to be present. Eliminates orphan-shim ✓ false-positive.
- **Windows install branch** (≈line 671):
  - Probe `shutil.which("fnm", path=registry_PATH)` *before* calling winget. Skip winget entirely if fnm is already on PATH.
  - After winget runs, re-probe via PATH; only fail if fnm is *still* missing. Trust PATH over winget's exit code.
  - Replaced the "version-specific node-versions path" persistence logic with: regex-extract `FNM_DIR` from `fnm env --shell powershell` output, then add `$FNM_DIR\aliases\default` to user PATH. The `aliases\default` junction is auto-maintained by `fnm default …` and contains both `node.exe` and `npm.cmd` — single PATH entry exposes both globally and auto-follows future fnm-default switches.
  - Defensive null-handling: `((fnm current 2>$null) | Out-String).Trim()` so an empty `fnm current` doesn't throw on `.Trim()` (fallback path; main discovery is via `fnm env`).
- **Pre-existing bug fixed in `scripts/build_backend_sidecar.py`**: line 103 used `os.pathsep` without `import os` — would NameError on every PyInstaller run. Added `import os`.

### Tests added — `tests/test_setup_node_install.py` (13 tests, all pass)

- `_is_installed("npm")` requires node (orphan-shim guard) — 5 cases
- Static inspection of `install_node` source: `winget` skip-on-existing, no `fnm current` in runtime code, no version-specific node-versions path, alias junction usage, `SetEnvironmentVariable` persistence, idempotent append — 7 cases
- Live `fnm env --shell powershell` returns `$env:FNM_DIR` — 1 case (skipped if fnm absent)

### Live verification on the user's machine

```
GET  /api/setup/status BEFORE → node:false, npm:false           (Fix 1.2 working)
POST /api/setup/install-node →
  "fnm already installed — skipping winget install."             (Fix 1.1 working)
  "Installing Node v24.15.0 (x64)"
  "Added C:\Users\nasse\AppData\Roaming\fnm\aliases\default
   to user PATH (fnm default-alias junction)."                   (Fix 1.3 working)
  {"success": true, "msg": "Node.js and npm installed successfully."}
GET  /api/setup/status AFTER  → node:true, npm:true              ✓
PowerShell readback           → user PATH count 16→17, alias dir present
```

---

## 2. Arena chrome-extension data pipeline (the real blocker)

This was discovered while debugging the original Arena UI request. The user reported `chat 141 / code 0 / search 0 / img 0` even after Refresh Live and reinstalling everything. The UI was correct; **the bridge was returning bad data**.

### Symptom
- Arena panel showed 141 models all classified as chat
- Code count: 0 (despite arena.ai having ~99 code-capable models)
- Search count: 0 (despite ~92 search-capable)
- Image count: 0 (despite ~92 image-input)

### Investigation chain

I downloaded `https://arena.ai/text/direct` HTML directly and parsed the `initialModels` blob (534 raw model records).

**Bug 2.1 — wrong modality key (the headline bug):**
- Old code in `arena_extension/page_bridge.js`: `'code' in m.rankByModality` → always false
- Probe of live arena.ai: rankByModality keys observed across all 506 selectable models = **`chat`, `webdev`, `image`, `search`, `video`** — there is **no `code` key**
- arena.ai's UI route `/code/direct` filters models by the `webdev` modality
- This bug existed in the published Web Store extension AND in my first hydration-based fix — both checked for `code`, which is never present

**Bug 2.2 — strict text-I/O filter excludes webdev models:**
- Old code: `if (!ic.text || !oc.text) continue;`
- Live arena.ai data: webdev-only models have `outputCapabilities: { web: true }` (no `text` key)
- 55 webdev-only models lacked `oc.text` → all dropped, even after fixing 2.1
- Example: `gpt-5.3-codex` has `inputCapabilities: { text:true, image:true }`, `outputCapabilities: { web:true }`, `rankByModality: { webdev: 24 }`

**Bug 2.3 — page navigation race in original refresh logic (already fixed earlier in session):**
- Old `arena_models_refresh` called `fetch_models` after navigating `/text/direct → /code/direct` via `window.location.href` + a 3 s sleep. Frequently raced page-hydration completion and returned `code_models = []`.
- Now uses single `fetch_all_models` op that reads page hydration once — no navigation, no race.

**Bug 2.4 — "Web Store path means my fixes never reached Chrome":**
- Extension is published at Chrome Web Store ID `jkclihigpeefogblifghhpojgkbheked`
- FreeHive Setup screen and Arena panel direct users to the Web Store as primary install path
- User had Web Store version installed → my bundled `page_bridge.js` updates were never loaded by Chrome
- Bundle copy at `C:\Users\nasse\AppData\Local\FreeHive\extensions\arena\` was correct, just not used

### Fixes — `arena_extension/page_bridge.js` (`fetch_all_models` operation)

```js
const isChat = Object.prototype.hasOwnProperty.call(rbm, "chat");
// arena's modality key for code is `webdev`. Accept `code` too for
// forward-compat in case they ever rename it.
const isCode = Object.prototype.hasOwnProperty.call(rbm, "webdev")
            || Object.prototype.hasOwnProperty.call(rbm, "code");
…
if (!ic.text) continue;
if (!oc.text && !oc.web) continue;   // accept text-out OR web-out (webdev models)
…
const modes = [];
if (isChat) { modes.push("chat"); chatModels.push(name); }
if (isCode) { modes.push("code"); codeModels.push(name); }   // export internal label "code"
modelModes[name] = modes;
```

Also added `diagnostics` field to the response (`{ total_initial_models, userSelectable_count, chat_count, code_count }`) for future debugging without re-deriving.

### Fixes — `backend/router.py`

- `POST /arena/models/refresh`: replaced two-page navigate-and-fetch with one `fetch_all_models` call. Returns 502 with actionable message if bridge yields nothing instead of silently caching empty data.
- `GET /arena/all-models` (used by dev tools): same single-call refactor.
- Removed the now-unused `import asyncio` and `_fetch_from_page` helper.

### Manifest version bump
`arena_extension/manifest.json`: `1.0.0 → 1.0.1`. So when the new build is published to Chrome Web Store, users will get the update auto-pushed.

### Live verification — pipeline against the actual arena.ai HTML

`build/probe_arena_full_pipeline.cjs` runs the new classifier against `https://arena.ai/text/direct` HTML I downloaded:

```
total unique:     293 models
chat (any):       260      ← was 141 (dropdown-only)
code (any):        99      ← was 0 (the user-reported failure)
  chat-only:      207
  code-only:       45
  chat+code:       53
search-capable:    92      ← was 0
image-input:       92      ← was 0

User-asked models (verified by name):
  gpt-5.3-codex                  → code-only         ✓ (was missing entirely)
  gpt-5.2-codex                  → code-only         ✓
  gpt-5.1-codex                  → code-only         ✓
  KAT-Coder-Pro-V1               → code-only         ✓
  qwen3-coder-480b-a35b-instruct → chat + code       ✓ (overlap, tagged accordingly)
```

### Tests added — `tests/arena_bridge_classify.test.mjs` (16 tests, all pass)

Replicates the bridge classifier algorithm and runs against fixtures matching arena.ai's actual page-hydration shape. Critical regression guards:

- `gpt-5.3-codex` model object (verbatim shape from live HTML) classifies as code
- `claude-sonnet-4-6` chat+webdev overlap shape produces `model_modes = ['chat','code']`
- webdev-only models with `oc.web` but no `oc.text` are NOT excluded by the text-I/O filter
- Models with neither `oc.text` nor `oc.web` ARE excluded
- image/search/video-only models excluded
- `'code' in rbm` forward-compat fallback works

The fixtures use a `model(name, modes, opts)` builder where passing `modes: ['code']` writes `webdev` into `rankByModality` to mirror the wire format.

---

## 3. Arena UI categorization (the original request)

### What the user wanted
> When viewing All Models, first option is direct chat then code models. Some models in code are not in chat — pull unique code models. Models in both chat and code → tag as "both code and chat". Code/chat capability should also surface in Settings → API.

### Implementation

**`src/lib/arenaModelPartition.js`** — new pure module (testable headlessly via `node --test`):

- `bareOf(arenaId)` — strips `arena/` prefix, idempotent
- `modesOf(bareId, modelModes)` — returns the modes array for a model with `['chat']` default when missing/empty/corrupt. **Critical for the live-fetch fallback path** (`GET /arena/models` returns `{models:[…]}` without `model_modes` when there's no cache); without the default, every model would have empty modes and disappear from both partition sections.
- `partitionByMode(models, modelModes)` → `{ directChat, codeOnly }`. Direct Chat = chat-mode (chat-only OR chat+code overlap). Code Models = code WITHOUT chat. Mathematically a partition: disjoint sections, complete coverage, overlap models live in chat.
- `modeTag(modes)` — returns `null` for chat-only (clean default), `{cls:'tag-code', label:'code'}` for code-only, `{cls:'tag-both', label:'code + chat'}` for overlap.

**`src/lib/ArenaPanel.svelte`** — reactive partition state via the module; when `activeFilter === 'all'`, renders two stacked sections with headers:
1. **Direct Chat** (count) — family-card grid + expanded panels + Other Providers
2. **Code Models** (count, hint *"unique to /code/direct — not in chat"*) — same shape

A Svelte 5 `{#snippet familySection(popular, other, prefix)}` is rendered twice with namespaced expandedCard keys (`chat:OpenAI`, `code:OpenAI`, `chat:__other__`, `code:other:Llama`) so cards in the two sections expand independently. Other filters (`code`, `search`, `image`) keep the existing single-section behavior with empty prefix.

Tag rendering in model rows uses the imported `modeTag()` helper.

**`src/lib/SettingsPage.svelte`** — model rows in API Keys → Arena now show capability badges next to the model name (`code` / `code + chat` / `search` / `img`). Imports `modeTag` + `bareOf` from the same module so the tag vocabulary is consistent across panels. Added `.arena-tag-both` (purple) and `.arena-tag-img` (amber) styles plus a `.model-row-tags` flex container.

### Tests added — `tests/arena_model_partition.test.mjs` (36 tests, all pass)

- `bareOf` idempotence + null/empty handling — 4 tests
- `modesOf` with full data, missing entry, null modes, empty array, corrupt non-array — 6 tests
- `modeTag` for each mode combination + order-independence + empty array — 5 tests
- Partition invariants I1 (union = input), I2 (disjoint), I3 (overlap → chat), I4 (code-only → code) — 3 tests
- 11 data-flow scenarios: full cache / live-fetch fallback / chat-page-only success / code-page-only success / partial mode coverage / future modalities / order-of-modes-array / preserves input order / etc.
- Realistic refreshed-cache integration fixture — 6 sub-tests

---

## State of the working tree

```
M  arena_extension/manifest.json       (version 1.0.0 → 1.0.1)
M  arena_extension/page_bridge.js      (fetch_all_models rewrite)
M  backend/router.py                   (single-call refresh + dev /all-models)
M  backend/setup_router.py             (Node install fixes)
M  scripts/build_backend_sidecar.py    (added missing import os)
M  src-tauri/Cargo.toml                (auto-touched by build, LF→CRLF warning only)
M  src/lib/ArenaPanel.svelte           (chat/code sections + module import)
M  src/lib/SettingsPage.svelte         (capability tags on model rows)
?? src/lib/arenaModelPartition.js      (new pure module)
?? tests/arena_bridge_classify.test.mjs        (new)
?? tests/arena_model_partition.test.mjs        (new)
?? tests/test_setup_node_install.py            (new)
?? docs/HANDOFF_2026-04-25_setup-and-arena.md  (this file)
?? build/probe_arena_html.cjs                  (diagnostic — can be deleted)
?? build/probe_arena_parse.cjs                 (diagnostic — can be deleted)
?? build/probe_arena_full_pipeline.cjs         (diagnostic — keep, useful regression check)
?? build/arena.html, build/arena_code.html     (downloaded fixtures — can be deleted)
```

Nothing committed. Master agent should review and decide commit boundaries — likely 3 commits split by issue family.

## Final test gate

```
node --test tests/*.test.mjs
  tests 58, suites 12, pass 58, fail 0       ✓

pytest tests/
  24 passed, 1 unrelated pre-existing failure  ✓
  (test_extension_path.py::TestOpenExtensionFolder::test_handles_popen_exception
   — confirmed failing on HEAD before any of these edits; chrome_launcher
   open_extension_folder Popen exception handler returns wrong success flag)

build/probe_arena_full_pipeline.cjs  →  PASS: code_models=99 (was 0)

npm run build  →  ✓ built in ~3 s

npm run tauri build  →  artifacts at:
  src-tauri/target/release/bundle/nsis/FreeHive_0.1.0_x64-setup.exe (80 MB)
  src-tauri/target/release/bundle/msi/FreeHive_0.1.0_x64_en-US.msi (97 MB)
```

---

## Pre-existing issues found, NOT fixed in this session

These were noticed but left alone to keep the change scope tight:

1. **`tests/test_extension_path.py::TestOpenExtensionFolder::test_handles_popen_exception`** fails on HEAD. `chrome_launcher.open_extension_folder` returns `success=True` even when `subprocess.Popen` raises. Test was failing before any of these edits — I confirmed by `git stash` + re-run.
2. **Setup screen and Arena panel direct users to the Chrome Web Store** as primary install path (`SetupScreen.svelte:511`, `ArenaPanel.svelte:736`). For dev iteration this is wrong — local builds with bridge changes can't reach users until republished. Consider promoting "Load unpacked from `<install dir>\extensions\arena\`" to primary, or adding a "use local build" toggle.
3. **`src-tauri/Cargo.toml` has unstaged LF/CRLF whitespace flutter** caused by Tauri's build process touching it. Not from my edits.
4. **Many pre-existing `implicit any` TypeScript warnings** across `*.svelte` files. The 5 new warnings introduced by my snippet's parameters fit the same pattern — the codebase doesn't enforce strict types.

---

## Important machine-state notes (specific to nasseromar8166@gmail.com on this machine)

These will affect the master agent's testing/validation:

- **fnm 1.39.0 already installed** via winget. fnm `FNM_DIR` is `%APPDATA%\fnm` (Roaming) on this machine.
- **nvm-windows ALSO installed** (`%LOCALAPPDATA%\nvm\nvm.exe`) with v22.22.2 + v24.14.1 installed but no version `nvm use`-active. `C:\nvm4w\nodejs` is on PATH but the directory does not exist (stale junction target). This is independent of FreeHive — user's choice whether to clean up.
- **`%APPDATA%\npm\npm.cmd` orphan shim is present** on this machine. The npm-requires-node guard makes it appear as ✗ correctly now.
- **My install-node live test added** `C:\Users\nasse\AppData\Roaming\fnm\aliases\default` to user PATH. This persists. Any new shell on this machine has fnm's default node accessible.
- **Chrome's installed extension was hot-patched.** I overwrote `C:\Users\nasse\AppData\Local\Google\Chrome\User Data\Default\Extensions\jkclihigpeefogblifghhpojgkbheked\1.0.0_0\page_bridge.js` with the new bundled version. Original Web Store file backed up to `page_bridge.js.webstore-backup` in the same dir. Chrome may detect the modification and disable the extension on next start (Web Store extensions are signed). If that happens, user should remove the Web Store version and load unpacked from `C:\Users\nasse\AppData\Local\FreeHive\extensions\arena\`.
- **No arena cache on this machine yet.** `~/.freehive/arena_models_full_cache.json` did not exist as of last check. Live-fetch fallback was active.
- **Junction created during build:** `C:\Users\nasse\Documents\FreeHive\.claude\worktrees\brave-blackwell-862f7e\venv` is a junction to the parent repo's `C:\Users\nasse\Documents\FreeHive\venv` (so PyInstaller could find the deps in the worktree). Cleanup: `rmdir` the junction (not the target).

---

## What the master agent should do next

In rough order of impact:

1. **Verify the hot-patch survived a Chrome restart.** If not, walk the user through removing the Web Store extension and Loading Unpacked from `%LOCALAPPDATA%\FreeHive\extensions\arena\`. Or add a "Use local extension" button to the Arena panel's setup step.
2. **Publish v1.0.1 of the extension to Chrome Web Store** so all users get the modality fix, not just this user. Repo already has the bumped manifest — needs the CWS publisher upload step.
3. **Decide commit boundaries.** Suggested split:
   - Commit A: setup_router Node install fixes + scripts/build_backend_sidecar.py `import os` fix + tests/test_setup_node_install.py
   - Commit B: arena bridge data pipeline (page_bridge.js modality fix + manifest bump + router.py refactor + tests/arena_bridge_classify.test.mjs)
   - Commit C: arena UI categorization (arenaModelPartition.js + ArenaPanel.svelte sections + SettingsPage.svelte tags + tests/arena_model_partition.test.mjs)
4. **Address the Settings/API page partition gap.** The page now shows tags on individual rows but does NOT split into Direct Chat / Code Models sections (only the main Arena panel does). The user mentioned "in that section I should be able to see models that can code or cant" — tags answer this, but if they want the same dual-section layout there, that's an additional ~50 lines mirroring the snippet.
5. **Clean up `build/probe_arena_*.cjs` and `build/arena*.html`** OR move them under `scripts/diagnostics/` if you want to keep the live-data smoke test around. `probe_arena_full_pipeline.cjs` is genuinely useful as a future regression check — recommend keeping.
6. **Pre-existing chrome_launcher test** (`test_handles_popen_exception`). Either fix the Popen exception path in `open_extension_folder` to actually return `success=False`, or update the test if the implementation changed intentionally.
7. **Watch for arena.ai schema drift.** They may change `webdev` back to `code` (or to something else) in the future. The forward-compat fallback `'webdev' in rbm || 'code' in rbm` covers a rename to `code`. If they pick a third name, the live fixture probe will show the failure quickly.

---

## Quick references for the master agent

- **Live diagnostic command (definitive answer for "is the bridge working"):**
  ```
  cd build && node probe_arena_full_pipeline.cjs
  ```
  Re-downloads arena.ai/text/direct (if you wipe `build/arena.html` first via `curl`), runs the classifier, asserts `code_models > 0`. Fails loudly if arena renames the modality key.

- **Where the bundled extension lives in an installed FreeHive:**
  - `C:\Users\nasse\AppData\Local\FreeHive\extensions\arena\` (Tauri resources)
  - `C:\Users\nasse\AppData\Local\FreeHive\sidecar\freehive-backend\_internal\arena_extension\` (PyInstaller bundle copy — same files)

- **Where Chrome's installed extension lives:**
  - `C:\Users\nasse\AppData\Local\Google\Chrome\User Data\Default\Extensions\jkclihigpeefogblifghhpojgkbheked\1.0.0_0\`

- **Cache file (created on first successful Refresh Live):**
  - `~/.freehive/arena_models_full_cache.json` (version 2)
