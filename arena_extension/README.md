# FreeHive Arena Extension (MVP)

This extension is the primary Arena transport path for FreeHive.
It receives jobs from the native messaging host and executes direct
`/nextjs-api/stream/...` requests inside `arena.ai`.

## Files

- `manifest.json` - MV3 manifest
- `background.js` - native host connection + job dispatch
- `content.js` - bridge between extension and page context
- `page_bridge.js` - in-page stream fetch + SSE parsing

## Local setup

1. Load this folder as an unpacked extension in `chrome://extensions`.
2. Get the extension ID from Chrome.
3. Install native host manifest:

```bash
cd native_host
./install_host.sh
```

4. Reload the extension after manifest install and replace `<EXTENSION_ID_PLACEHOLDER>`
   in `~/.config/google-chrome/NativeMessagingHosts/com.freehive.arena_bridge.json`
   with your real extension id.
5. Start backend and native host, then run smoke tests.

## Notes

- This cycle intentionally has **no Playwright fallback**.
- Failures should return typed errors so backend can surface explicit `503`.
