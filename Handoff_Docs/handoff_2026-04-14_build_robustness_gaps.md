# 📋 FreeHive vs OmniRoute — Build Robustness Gap Analysis
**Date:** 2026-04-14  
**Scope:** Build infrastructure only — CI/CD, scripts, safety nets, tooling. NOT features.  
**Verdict:** OmniRoute has 12+ build robustness patterns FreeHive is missing entirely.

---

## SUMMARY TABLE

| Area | OmniRoute | FreeHive | Gap |
|------|-----------|----------|-----|
| GitHub Actions CI | ✅ Full pipeline (lint, test, coverage, security, E2E) | ❌ Zero workflows | Critical |
| Release automation | ✅ Tag-triggered multi-platform builds | ❌ Manual only | Critical |
| Pre-commit hooks | ✅ husky + lint-staged | ❌ None | High |
| Dependabot | ✅ Weekly auto-PRs for npm + Actions | ❌ None | High |
| Node engine constraint | ✅ `">=18 <24"` in package.json | ❌ None | High |
| Secret scanning in CI | ✅ TruffleHog + Snyk + npm audit | ❌ None | High |
| Health check script | ✅ `scripts/healthcheck.mjs` | ❌ None | Medium |
| System info / diagnostics | ✅ `scripts/system-info.mjs` | ❌ None | Medium |
| Build script error recovery | ✅ Signal forwarding, atomic moves, restore-on-fail | ❌ No safety net | Medium |
| Version sync enforcement | ✅ `check-docs-sync`, version validated in CI | ❌ Manual, easy to mismatch | Medium |
| Circular import detection | ✅ `check-cycles.mjs` runs in CI | ❌ None | Low-Medium |
| `.env.example` | ✅ Fully documented with comments | ❌ Not present | Low |
| SECURITY.md | ✅ Present with responsible disclosure policy | ❌ None | Low |
| Code coverage gating | ✅ 60% minimum enforced by `c8` | ❌ No tests exist | Low |

---

## 🔴 CRITICAL — DO THESE FIRST

---

### GAP 1 — No GitHub Actions CI whatsoever

FreeHive has **zero** `.github/workflows/` files. OmniRoute's `ci.yml` catches broken builds, regressions, and security issues on every push/PR.

**What to add — `.github/workflows/ci.yml`:**
```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  build-frontend:
    name: Build Frontend
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
      - run: npm ci
      - run: npm run build

  build-sidecar:
    name: Build Python Sidecar
    runs-on: windows-latest          # Must be Windows — PyInstaller output is platform-specific
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: python -m venv venv
      - run: venv\Scripts\pip install -r requirements.txt pyinstaller
        shell: cmd
      - run: npm run build:sidecar
      - uses: actions/upload-artifact@v4
        with:
          name: freehive-backend-win
          path: src-tauri/sidecar/freehive-backend.exe

  typecheck:
    name: Type Check
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
      - run: npm ci
      - run: npm run check

  security:
    name: Security Scan
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - name: TruffleHog Secret Scan
        uses: trufflesecurity/trufflehog@main
        with:
          path: ./
          base: ${{ github.event.repository.default_branch }}
          head: HEAD
          extra_args: --only-verified
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
      - run: npm ci
      - run: npm audit --audit-level=high --omit=dev || true
```

**Why:** Without this, every push to `main` is a leap of faith. The sidecar build-on-Windows job is especially important — it validates the PyInstaller pipeline doesn't silently break.

---

### GAP 2 — No release automation (Tauri build triggered by tags)

OmniRoute has `electron-release.yml` that triggers on `v*` tags, builds for Windows/macOS/Linux in parallel, and publishes to GitHub Releases. FreeHive's Tauri build is 100% manual.

**What to add — `.github/workflows/tauri-release.yml`:**
```yaml
name: Build Tauri Installers

on:
  push:
    tags:
      - "v*"
  workflow_dispatch:
    inputs:
      version:
        description: "Version tag (e.g. v0.2.0)"
        required: true

permissions:
  contents: write

jobs:
  build:
    strategy:
      fail-fast: false
      matrix:
        include:
          - platform: windows-latest
            args: "--target x86_64-pc-windows-msvc"
          - platform: macos-latest
            args: "--target aarch64-apple-darwin"
          - platform: ubuntu-22.04
            args: ""

    runs-on: ${{ matrix.platform }}
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install Rust stable
        uses: dtolnay/rust-toolchain@stable

      - name: Install Linux deps
        if: matrix.platform == 'ubuntu-22.04'
        run: |
          sudo apt-get update
          sudo apt-get install -y libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf

      - name: Install frontend deps
        run: npm ci

      - name: Build Python sidecar
        run: |
          python -m venv venv
          venv/Scripts/pip install -r requirements.txt pyinstaller  # Windows
          npm run build:sidecar

      - uses: tauri-apps/tauri-action@v0
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        with:
          tagName: ${{ github.ref_name }}
          releaseName: "FreeHive ${{ github.ref_name }}"
          args: ${{ matrix.args }}
          includeUpdaterJson: true
```

**Why:** Without this, you have to manually build and upload installers. A tag push becomes a single command that produces MSI + NSIS on Windows, DMG on macOS, AppImage on Linux — all in parallel.

---

## 🟠 HIGH PRIORITY

---

### GAP 3 — No pre-commit hooks (husky + lint-staged)

OmniRoute runs prettier + eslint on every staged file before it can be committed. FreeHive has no such guard — malformatted or broken JS/TS goes straight to main.

**Steps to add:**

1. Install:
```bash
npm install --save-dev husky lint-staged prettier
npx husky init
```

2. Edit `.husky/pre-commit`:
```sh
npx lint-staged
```

3. Add to `package.json`:
```json
"lint-staged": {
    "*.{js,ts,svelte,mjs}": [
        "prettier --write",
        "eslint --fix --no-error-on-unmatched-pattern"
    ],
    "*.{json,css,md}": [
        "prettier --write"
    ]
},
"prepare": "husky"
```

4. Add prettier config — `prettier.config.mjs`:
```js
export default {
    printWidth: 100,
    singleQuote: true,
    trailingComma: 'all',
    semi: true,
    plugins: ['prettier-plugin-svelte'],
    overrides: [{ files: '*.svelte', options: { parser: 'svelte' } }]
};
```

**Why:** Prevents "fix formatting" commits that make `git blame` useless. Catches Svelte/TS syntax errors before they hit CI.

---

### GAP 4 — No Dependabot

OmniRoute gets weekly auto-PRs for outdated npm packages, GitHub Actions versions, and Docker base images. FreeHive deps will silently drift.

**Add — `.github/dependabot.yml`:**
```yaml
version: 2
updates:
  - package-ecosystem: npm
    directory: "/"
    schedule:
      interval: weekly
      day: monday
    commit-message:
      prefix: "deps"
    open-pull-requests-limit: 5
    ignore:
      - dependency-name: "@tauri-apps/*"
        update-types: ["version-update:semver-major"]

  - package-ecosystem: github-actions
    directory: "/"
    schedule:
      interval: weekly

  - package-ecosystem: cargo
    directory: "/src-tauri"
    schedule:
      interval: weekly
    commit-message:
      prefix: "deps(rust)"
```

**Note:** Add `cargo` ecosystem — OmniRoute doesn't need this (no Rust), but FreeHive does.

---

### GAP 5 — No Node.js engine constraint

FreeHive's `package.json` has no `"engines"` field. If someone runs `npm run build` with Node 16, they'll get cryptic errors instead of a clear "update your Node" message.

**Add to `package.json`:**
```json
"engines": {
    "node": ">=18.0.0",
    "npm": ">=9.0.0"
}
```

---

### GAP 6 — No secret scanning in CI

OmniRoute CI runs:
- **TruffleHog** — detects verified secrets (API keys, tokens) in git history  
- **Snyk** — vulnerability DB check against all dependencies  
- **`npm audit`** — built-in npm vulnerability check  
- **`is-my-node-vulnerable`** — checks Node.js version for known CVEs

FreeHive has credentials files (`~/.claude/.credentials.json`, `~/.gemini/oauth_creds.json`, `~/.codex/auth.json`) referenced in backend code. If a real token ever gets committed accidentally, there's no automated detection.

**Minimum addition** — add to the CI workflow above (security job):
```yaml
- run: npm audit --audit-level=high --omit=dev || true
- name: Check Node.js vulnerabilities
  run: npx is-my-node-vulnerable || true
```

---

## 🟡 MEDIUM PRIORITY

---

### GAP 7 — No health check script

OmniRoute has `scripts/healthcheck.mjs` — a simple GET to `/api/monitoring/health` that exits non-zero on failure. Used by Docker HEALTHCHECK and can be called from shell scripts.

FreeHive's only "health check" is the 15-retry loop inside `SetupScreen.svelte` — it runs in the browser only, after the app is already up.

**Add — `scripts/healthcheck.mjs`:**
```javascript
#!/usr/bin/env node
/**
 * FreeHive backend health check.
 * Usage: node scripts/healthcheck.mjs
 * Exit 0 = healthy, Exit 1 = not reachable
 */
const port = process.env.FREEHIVE_BACKEND_PORT || "7200";
const host = process.env.FREEHIVE_BACKEND_HOST || "127.0.0.1";

fetch(`http://${host}:${port}/api/setup/status`, { signal: AbortSignal.timeout(5000) })
    .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    })
    .then((data) => {
        console.log(`✅ Backend healthy — ready: ${data.ready}`);
        process.exit(0);
    })
    .catch((err) => {
        console.error(`❌ Backend unreachable: ${err.message}`);
        process.exit(1);
    });
```

**Add to `package.json` scripts:**
```json
"health": "node scripts/healthcheck.mjs"
```

**Why:** You can call `npm run health` from a terminal, a shell script, or a CI step to verify the backend is up before running tests. Much better than manually opening the app and waiting 15 seconds.

---

### GAP 8 — No system info / diagnostics script

OmniRoute's `scripts/system-info.mjs` generates a report with Node version, OS, installed CLIs, RAM, and env vars. When a user reports a bug, they run `npm run system-info` and paste the output.

FreeHive users reporting issues have no easy way to give you their environment info.

**Add — `scripts/system-info.mjs`:**
```javascript
#!/usr/bin/env node
import { execSync } from 'child_process';
import os from 'os';
import { readFileSync } from 'fs';

function run(cmd) {
    try { return execSync(cmd, { encoding: 'utf-8', stdio: 'pipe' }).trim(); }
    catch { return 'not found'; }
}

const pkg = JSON.parse(readFileSync('package.json', 'utf-8'));
const lines = [
    `FreeHive v${pkg.version} — System Info`,
    `Generated: ${new Date().toISOString()}`,
    ``,
    `Node.js:  ${process.version}`,
    `npm:      v${run('npm --version')}`,
    `Platform: ${process.platform} (${process.arch})`,
    `OS:       ${os.type()} ${os.release()}`,
    `RAM:      ${Math.round(os.totalmem() / 1024 / 1024)} MB total`,
    ``,
    `CLI Tools:`,
    `  openclaude: ${run('openclaude --version')}`,
    `  claude:     ${run('claude --version')}`,
    `  gemini:     ${run('gemini --version')}`,
    `  codex:      ${run('codex --version')}`,
    `  python:     ${run('python --version')}`,
    `  cargo:      ${run('cargo --version')}`,
    ``,
    `Python venv: ${run('venv/Scripts/python --version') || run('venv/bin/python --version')}`,
    `PyInstaller: ${run('venv/Scripts/pyinstaller --version') || run('venv/bin/pyinstaller --version')}`,
    ``,
    `Sidecar binary exists: ${require('fs').existsSync('src-tauri/sidecar/freehive-backend.exe') ? 'YES' : 'NO'}`,
];

const report = lines.join('\n');
console.log(report);

import { writeFileSync } from 'fs';
writeFileSync('system-info.txt', report);
console.log('\n✅ Saved to system-info.txt');
```

**Add to `package.json` scripts:**
```json
"system-info": "node scripts/system-info.mjs"
```

---

### GAP 9 — Build script has no error recovery or signal handling

OmniRoute's `build-next-isolated.mjs` does:
1. **Signal forwarding** — if you Ctrl+C during build, the child process is also killed properly
2. **Atomic file moves with EXDEV fallback** — cross-device rename (e.g., Docker tmpfs) handled gracefully
3. **Restore on failure** — if build fails mid-way, temporary state is restored so the next run isn't broken

FreeHive's `scripts/build_sidecar.cjs` is a bare `execSync()` call with no error recovery. If the build is interrupted mid-run:
- The `src-tauri/sidecar/` dir may be in a half-written state
- The next `npm run build` will either fail silently or use the stale binary

**Add to `scripts/build_sidecar.cjs`** — at minimum, wrap with a try/catch that removes partial output:
```javascript
const SIDECAR_DIR = path.join(__dirname, '..', 'src-tauri', 'sidecar');
const OUTPUT_EXE = path.join(SIDECAR_DIR, 'freehive-backend.exe');

// Remove stale binary before building to avoid shipping old artifact on failure
if (fs.existsSync(OUTPUT_EXE)) {
    fs.unlinkSync(OUTPUT_EXE);
    console.log('[build:sidecar] Removed stale binary before rebuild');
}

try {
    execSync(buildCmd, { stdio: 'inherit', cwd: ROOT });
    if (!fs.existsSync(OUTPUT_EXE)) {
        throw new Error('PyInstaller finished but output binary not found at expected path');
    }
    console.log('[build:sidecar] ✅ Sidecar built successfully');
} catch (err) {
    // Clean up any partial output so next run starts fresh
    if (fs.existsSync(OUTPUT_EXE)) fs.unlinkSync(OUTPUT_EXE);
    console.error('[build:sidecar] ❌ Build failed:', err.message);
    process.exit(1);
}
```

**Also add signal forwarding** (same pattern as OmniRoute) so Ctrl+C during `npm run build:sidecar` kills Python/PyInstaller, not just Node.

---

### GAP 10 — No version sync enforcement

FreeHive has 3 places that must all match for a Tauri build to succeed:
- `package.json` → `"version": "0.1.0"`
- `src-tauri/tauri.conf.json` → `"version": "0.1.0"`
- `src-tauri/Cargo.toml` → `version = "0.1.0"`

If they ever diverge, `cargo tauri build` fails with a confusing version mismatch error. OmniRoute checks this kind of consistency in CI (`check-docs-sync`).

**Add — `scripts/check-versions.mjs`:**
```javascript
#!/usr/bin/env node
import { readFileSync } from 'fs';

const pkg = JSON.parse(readFileSync('package.json', 'utf-8')).version;
const tauri = JSON.parse(readFileSync('src-tauri/tauri.conf.json', 'utf-8')).version;
const cargo = readFileSync('src-tauri/Cargo.toml', 'utf-8')
    .match(/^version\s*=\s*"(.+?)"/m)?.[1];

console.log(`package.json:       ${pkg}`);
console.log(`tauri.conf.json:    ${tauri}`);
console.log(`Cargo.toml:         ${cargo}`);

if (pkg !== tauri || pkg !== cargo) {
    console.error('\n❌ Version mismatch! All three must be identical before building.');
    process.exit(1);
}
console.log('\n✅ All versions match.');
```

**Add to `package.json` scripts:**
```json
"check:versions": "node scripts/check-versions.mjs"
```

**Add to CI** and to `build` as a precondition:
```json
"build": "node scripts/check-versions.mjs && vite build"
```

---

## 🟢 LOW PRIORITY (Polish)

---

### GAP 11 — No circular import detection

OmniRoute runs `scripts/check-cycles.mjs` in CI to catch circular imports in critical directories (`src/shared/components`, `src/lib/db`). Circular imports in Svelte cause silent runtime issues.

**Easiest fix — use `madge`:**
```bash
npm install --save-dev madge
```
Add to `package.json`:
```json
"check:cycles": "madge --circular --extensions js,ts,svelte src/"
```
Add to CI lint job:
```yaml
- run: npm run check:cycles
```

---

### GAP 12 — No `.env.example`

OmniRoute's `.env.example` documents every environment variable with comments explaining what it does, how to generate the value, and which file reads it.

FreeHive has no `.env.example`. The only env vars are the ones passed in `lib.rs` (`FREEHIVE_BACKEND_HOST`, `FREEHIVE_BACKEND_PORT`, `FREEHIVE_BACKEND_RELOAD`) — but a new developer has no idea these exist.

**Add — `.env.example`:**
```bash
# FreeHive Backend Configuration
# Copy this to .env for local development

# Host and port the FastAPI backend binds to.
# DO NOT change these if using the Tauri sidecar — lib.rs sets them before spawning.
# Only change for standalone backend development.
FREEHIVE_BACKEND_HOST=127.0.0.1
FREEHIVE_BACKEND_PORT=7200

# Set to 1 to enable uvicorn auto-reload during backend development (NOT for production).
FREEHIVE_BACKEND_RELOAD=0

# Feature flags
# Set to "1" to enable Arena mode (v2, currently disabled)
FREEHIVE_ARENA_ENABLED=0
```

---

### GAP 13 — No SECURITY.md

OmniRoute has a `SECURITY.md` with responsible disclosure instructions. GitHub shows this automatically when someone tries to open a security issue.

**Add — `SECURITY.md`** (2 minutes):
```markdown
# Security Policy

## Reporting Vulnerabilities

Do NOT open public GitHub issues for security vulnerabilities.

Use [GitHub Security Advisories](https://github.com/NazzWay/FreeHive/security/advisories/new)
or email nasseromar81666@gmail.com.

Include: description, reproduction steps, and potential impact.

## Response Timeline
- Acknowledgment: 48 hours
- Patch release (critical): 14 business days
```

---

## 🏗️ IMPLEMENTATION ORDER (Recommended)

Execute in this order — each step provides immediate value:

```
Week 1:
  1. Add engines field to package.json        (5 min)
  2. Add .env.example                         (10 min)  
  3. Add scripts/check-versions.mjs           (15 min)
  4. Add .github/dependabot.yml               (10 min)
  5. Add .github/workflows/ci.yml (basic)     (30 min)

Week 2:
  6. Add husky + lint-staged                  (20 min)
  7. Harden scripts/build_sidecar.cjs         (20 min)
  8. Add scripts/healthcheck.mjs              (15 min)
  9. Add scripts/system-info.mjs              (15 min)

Week 3:
  10. Add .github/workflows/tauri-release.yml (60 min — test carefully)
  11. Add check:cycles via madge              (10 min)
  12. Add SECURITY.md                         (5 min)
```

---

## 📁 FILES TO CREATE (Summary)

| File | Purpose |
|------|---------|
| `.github/workflows/ci.yml` | Build + typecheck + secret scan on every push |
| `.github/workflows/tauri-release.yml` | Multi-platform installer build on `v*` tags |
| `.github/dependabot.yml` | Weekly automated dependency update PRs |
| `.husky/pre-commit` | Run lint-staged before every commit |
| `prettier.config.mjs` | Consistent code formatting config |
| `scripts/healthcheck.mjs` | CLI backend health check |
| `scripts/system-info.mjs` | Debug info collector for bug reports |
| `scripts/check-versions.mjs` | Enforce package.json == tauri.conf.json == Cargo.toml |
| `.env.example` | Document all environment variables |
| `SECURITY.md` | Responsible disclosure policy |

## 📁 FILES TO MODIFY (Summary)

| File | Change |
|------|--------|
| `package.json` | Add `engines`, `lint-staged`, `prepare`, new script entries |
| `scripts/build_sidecar.cjs` | Add stale binary cleanup, error recovery, signal forwarding |
| `.gitignore` | Add `system-info.txt`, `.husky/_/`, `coverage/` |
