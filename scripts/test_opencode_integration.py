#!/usr/bin/env python3
"""
Test harness for POST /api/integrations/opencode ("Add to OpenCode" button).

Runs three scenarios:
  1. FIRST-TIME     — no existing opencode.json. Endpoint must create the file
                     with 3 FreeHive providers and set default model.
  2. MERGE          — existing opencode.json with an unrelated provider. Endpoint
                     must merge in the 3 FreeHive providers WITHOUT removing
                     the existing one.
  3. PRESERVE MODEL — existing opencode.json that already has a "model" key.
                     Endpoint must NOT overwrite the user's default model.

The user's real ~/.config/opencode/opencode.json is backed up to
~/.config/opencode/opencode.json.testbackup before the run and restored when
the run finishes (even on failure). Safe to run on a machine with a live
OpenCode config.

Run from repo root:
    python3 scripts/test_opencode_integration.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "opencode" / "opencode.json"
BACKUP_PATH = CONFIG_PATH.with_suffix(".json.testbackup")
PORT = int(os.getenv("FREEHIVE_BACKEND_PORT", "7200"))
ENDPOINT = f"http://127.0.0.1:{PORT}/api/integrations/opencode"

EXPECTED_PROVIDER_KEYS = {"freehive-claude", "freehive-chatgpt", "freehive-gemini"}
EXPECTED_DEFAULT_MODEL = "freehive-claude/claude-sonnet-4-6"


# ──────────────────────────────── helpers ──────────────────────────────── #

def call_endpoint() -> dict:
    req = urllib.request.Request(ENDPOINT, method="POST", headers={"Content-Type": "application/json"}, data=b"{}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"status": resp.status, "body": json.loads(resp.read().decode())}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read().decode(errors="replace")}
    except urllib.error.URLError as e:
        print(f"  [FATAL] Could not reach {ENDPOINT}: {e.reason}")
        print(f"          Is the FreeHive backend running on port {PORT}?")
        sys.exit(2)


def read_config() -> dict | None:
    if not CONFIG_PATH.exists():
        return None
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception as e:
        print(f"  [WARN] existing opencode.json is invalid JSON: {e}")
        return None


def write_config(data: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2))


def delete_config() -> None:
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()


def ok(msg: str) -> None:
    print(f"  [PASS] {msg}")


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")
    FAILURES.append(msg)


def show(label: str, obj) -> None:
    print(f"  --- {label} ---")
    if isinstance(obj, (dict, list)):
        print("  " + json.dumps(obj, indent=2).replace("\n", "\n  "))
    else:
        print(f"  {obj}")


FAILURES: list[str] = []


# ──────────────────────────────── scenarios ──────────────────────────────── #

def scenario_first_time() -> None:
    print("\n━━━ Scenario 1: FIRST-TIME (no opencode.json exists) ━━━")
    delete_config()
    assert not CONFIG_PATH.exists(), "precondition: config file should not exist"

    resp = call_endpoint()
    show("HTTP response", resp)

    if resp["status"] != 200:
        fail(f"expected HTTP 200, got {resp['status']}")
        return

    body = resp["body"]
    if not body.get("success"):
        fail(f"response.success != True: {body}")
        return
    ok("endpoint returned success=true")

    if set(body.get("providers", [])) != EXPECTED_PROVIDER_KEYS:
        fail(f"providers mismatch: {body.get('providers')}")
    else:
        ok(f"response lists all 3 providers: {sorted(body['providers'])}")

    if not CONFIG_PATH.exists():
        fail("opencode.json was NOT created")
        return
    ok(f"opencode.json created at {CONFIG_PATH}")

    cfg = read_config()
    if cfg is None:
        fail("written file is not valid JSON")
        return

    provider_block = cfg.get("provider", {})
    missing = EXPECTED_PROVIDER_KEYS - set(provider_block.keys())
    if missing:
        fail(f"missing providers in written config: {missing}")
    else:
        ok("all 3 FreeHive providers present in written config")

    fh_claude = provider_block.get("freehive-claude", {})
    opts = fh_claude.get("options", {})
    if opts.get("baseURL") != f"http://127.0.0.1:{PORT}/v1":
        fail(f"freehive-claude baseURL wrong: {opts.get('baseURL')}")
    else:
        ok(f"baseURL correctly set to {opts['baseURL']}")
    if opts.get("apiKey") != "freehive-claude":
        fail(f"freehive-claude apiKey wrong: {opts.get('apiKey')}")
    else:
        ok("apiKey correctly set to 'freehive-claude'")

    claude_models = list(fh_claude.get("models", {}).keys())
    if not claude_models:
        fail("freehive-claude has no models")
    else:
        ok(f"freehive-claude has {len(claude_models)} models: {claude_models}")

    if cfg.get("model") != EXPECTED_DEFAULT_MODEL:
        fail(f"default model != {EXPECTED_DEFAULT_MODEL}, got {cfg.get('model')}")
    else:
        ok(f"default model set to {EXPECTED_DEFAULT_MODEL}")

    show("written opencode.json", cfg)


def scenario_merge_with_existing() -> None:
    print("\n━━━ Scenario 2: MERGE (existing config has unrelated provider) ━━━")
    seed = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "my-custom-provider": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "My Custom",
                "options": {"baseURL": "http://example.test/v1", "apiKey": "xxx"},
                "models": {"my-model": {"name": "my-model"}},
            }
        },
        "model": "my-custom-provider/my-model",
    }
    write_config(seed)
    ok("seeded opencode.json with my-custom-provider")

    resp = call_endpoint()
    if resp["status"] != 200:
        fail(f"expected HTTP 200, got {resp['status']}: {resp['body']}")
        return
    ok("endpoint returned HTTP 200")

    cfg = read_config() or {}
    provider_block = cfg.get("provider", {})

    if "my-custom-provider" not in provider_block:
        fail("existing my-custom-provider was REMOVED (data loss!)")
    else:
        ok("existing my-custom-provider preserved")

    missing = EXPECTED_PROVIDER_KEYS - set(provider_block.keys())
    if missing:
        fail(f"FreeHive providers not merged in: {missing}")
    else:
        ok("all 3 FreeHive providers merged in alongside existing one")

    if cfg.get("model") != "my-custom-provider/my-model":
        fail(f"existing default model was overwritten! got {cfg.get('model')}")
    else:
        ok("existing default model preserved (not overwritten)")

    print(f"  final provider keys: {sorted(provider_block.keys())}")


def scenario_rerun_idempotent() -> None:
    print("\n━━━ Scenario 3: RE-RUN (endpoint called twice) ━━━")
    delete_config()
    r1 = call_endpoint()
    if r1["status"] != 200:
        fail(f"first call failed: {r1}")
        return
    cfg1 = read_config() or {}
    r2 = call_endpoint()
    if r2["status"] != 200:
        fail(f"second call failed: {r2}")
        return
    cfg2 = read_config() or {}

    if cfg1 == cfg2:
        ok("endpoint is idempotent (two consecutive calls produce identical config)")
    else:
        fail("config changed between two consecutive calls")
        show("diff (cfg1)", cfg1)
        show("diff (cfg2)", cfg2)


# ──────────────────────────────── main ──────────────────────────────── #

def main() -> int:
    print(f"Testing {ENDPOINT}")
    print(f"Config path: {CONFIG_PATH}")

    # Backup real config if present
    had_real_config = CONFIG_PATH.exists()
    if had_real_config:
        shutil.copy2(CONFIG_PATH, BACKUP_PATH)
        print(f"Backed up real config → {BACKUP_PATH}")
    else:
        print("No existing config to back up (true first-time state on this machine)")

    try:
        scenario_first_time()
        scenario_merge_with_existing()
        scenario_rerun_idempotent()
    finally:
        # Restore real config (or clean up test file if there was no real one)
        if had_real_config:
            shutil.copy2(BACKUP_PATH, CONFIG_PATH)
            BACKUP_PATH.unlink()
            print(f"\nRestored real config from backup.")
        else:
            delete_config()
            print("\nCleaned up test config (none existed before).")

    print()
    if FAILURES:
        print(f"✗ {len(FAILURES)} failure(s):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("✓ All scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
