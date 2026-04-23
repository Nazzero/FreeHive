# FreeHive Arena Extension (MVP)

This extension is the primary Arena transport path for FreeHive.
It receives jobs from the native messaging host and executes direct
`/nextjs-api/stream/...` requests inside `arena.ai`.

## Files

- `manifest.json` - MV3 manifest
- `background.js` - native host connection + job dispatch
- `content.js` - bridge between extension and page context
- `page_bridge.js` - in-page stream fetch + SSE parsing

## Setup

Extension ID: `jkclihigpeefogblifghhpojgkbheked` (hardcoded, published on Chrome Web Store)

1. Install from Chrome Web Store: https://chromewebstore.google.com/detail/freehive-arena-bridge/jkclihigpeefogblifghhpojgkbheked
2. Install native host manifest:

```bash
cd native_host
./install_host.sh        # Linux
./install_host_macos.sh  # macOS
# Windows: run install_host_windows.ps1 in PowerShell
```

3. Start backend and native host, then run smoke tests.

## Notes

- This cycle intentionally has **no Playwright fallback**.
- Failures should return typed errors so backend can surface explicit `503`.
