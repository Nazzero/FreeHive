"""
test_tool_use.py — FreeHive tool use validation

Tests all three providers (Claude, ChatGPT, Gemini) for tool use support
through the FreeHive compat layer.

Each test does a full round-trip:
  1. Send a message + tool definition asking the model to call the tool
  2. Verify the model returns a tool call (not just text)
  3. Send the tool result back
  4. Verify the model returns a final text answer

Usage:
  python scripts/test_tool_use.py

Requires FreeHive backend running at http://localhost:7200.
"""

import json
import sys
import httpx

BASE_URL = "http://localhost:7200"

# ── Simple tool definition ─────────────────────────────────────────────────
# get_weather is unambiguous — the model can't answer without calling it.

WEATHER_TOOL_OAI = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city. Always call this when asked about weather.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name, e.g. London"},
            },
            "required": ["city"],
        },
    },
}

WEATHER_TOOL_ANTHROPIC = {
    "name": "get_weather",
    "description": "Get the current weather for a city. Always call this when asked about weather.",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name, e.g. London"},
        },
        "required": ["city"],
    },
}

WEATHER_RESULT = json.dumps({"temperature": "22°C", "condition": "sunny", "humidity": "45%"})
USER_QUESTION  = "What is the weather like in Paris right now?"


# ── Helpers ────────────────────────────────────────────────────────────────

def hdr(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print('─'*60)

def ok(msg: str):  print(f"  ✓  {msg}")
def fail(msg: str): print(f"  ✗  {msg}")
def info(msg: str): print(f"     {msg}")


# ── Claude (/v1/messages, Anthropic format) ────────────────────────────────

def test_claude():
    hdr("Claude  ·  POST /v1/messages  (Anthropic format)")
    key = "freehive-claude-sonnet-4-6"

    with httpx.Client(base_url=BASE_URL, timeout=60) as client:

        # ── Turn 1: ask + tool definition ──────────────────────────────────
        resp = client.post("/v1/messages", headers={"x-api-key": key}, json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "tools": [WEATHER_TOOL_ANTHROPIC],
            "tool_choice": {"type": "auto"},
            "messages": [{"role": "user", "content": USER_QUESTION}],
        })

        if resp.status_code != 200:
            fail(f"Turn 1 HTTP {resp.status_code}: {resp.text[:200]}")
            return False

        body = resp.json()
        stop_reason = body.get("stop_reason")
        content     = body.get("content", [])

        info(f"stop_reason : {stop_reason}")
        info(f"content     : {json.dumps(content, indent=6)}")

        tool_use = next((b for b in content if b.get("type") == "tool_use"), None)
        if not tool_use:
            fail("No tool_use block in response — model did not call the tool")
            return False

        ok(f"Tool called: {tool_use['name']}({json.dumps(tool_use['input'])})")

        # ── Turn 2: send tool result ────────────────────────────────────────
        resp2 = client.post("/v1/messages", headers={"x-api-key": key}, json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "tools": [WEATHER_TOOL_ANTHROPIC],
            "messages": [
                {"role": "user",      "content": USER_QUESTION},
                {"role": "assistant", "content": content},
                {"role": "user",      "content": [{
                    "type":        "tool_result",
                    "tool_use_id": tool_use["id"],
                    "content":     WEATHER_RESULT,
                }]},
            ],
        })

        if resp2.status_code != 200:
            fail(f"Turn 2 HTTP {resp2.status_code}: {resp2.text[:200]}")
            return False

        body2   = resp2.json()
        content2 = body2.get("content", [])
        final   = next((b["text"] for b in content2 if b.get("type") == "text"), None)

        if not final:
            fail("Turn 2 returned no text — expected final answer")
            return False

        ok(f"Final answer: {final[:120]}")
        return True


# ── ChatGPT (/v1/chat/completions, OpenAI format) ─────────────────────────

def test_chatgpt():
    hdr("ChatGPT  ·  POST /v1/chat/completions  (OpenAI format)")
    key = "freehive-gpt-5.2"

    with httpx.Client(base_url=BASE_URL, timeout=60) as client:

        # ── Turn 1 ──────────────────────────────────────────────────────────
        resp = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={
            "model": "gpt-5.2",
            "tools": [WEATHER_TOOL_OAI],
            "tool_choice": "auto",
            "messages": [{"role": "user", "content": USER_QUESTION}],
        })

        if resp.status_code != 200:
            fail(f"Turn 1 HTTP {resp.status_code}: {resp.text[:200]}")
            return False

        body    = resp.json()
        choice  = (body.get("choices") or [{}])[0]
        message = choice.get("message", {})
        finish  = choice.get("finish_reason")

        info(f"finish_reason : {finish}")
        info(f"message       : {json.dumps(message, indent=6)}")

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            fail("No tool_calls in response — model did not call the tool")
            return False

        tc = tool_calls[0]
        ok(f"Tool called: {tc['function']['name']}({tc['function']['arguments']})")

        # ── Turn 2: send tool result ─────────────────────────────────────────
        resp2 = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={
            "model": "gpt-5.2",
            "tools": [WEATHER_TOOL_OAI],
            "messages": [
                {"role": "user",      "content": USER_QUESTION},
                message,  # assistant message with tool_calls
                {
                    "role":         "tool",
                    "tool_call_id": tc["id"],
                    "content":      WEATHER_RESULT,
                },
            ],
        })

        if resp2.status_code != 200:
            fail(f"Turn 2 HTTP {resp2.status_code}: {resp2.text[:200]}")
            return False

        body2   = resp2.json()
        choice2 = (body2.get("choices") or [{}])[0]
        final   = choice2.get("message", {}).get("content")

        if not final:
            fail("Turn 2 returned no text — expected final answer")
            return False

        ok(f"Final answer: {final[:120]}")
        return True


# ── Gemini (/v1/chat/completions, OpenAI format) ──────────────────────────

def test_gemini():
    hdr("Gemini  ·  POST /v1/chat/completions  (OpenAI format)")
    key = "freehive-gemini-3-flash-preview"

    with httpx.Client(base_url=BASE_URL, timeout=60) as client:

        # ── Turn 1 ──────────────────────────────────────────────────────────
        resp = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={
            "model": "gemini-3-flash-preview",
            "tools": [WEATHER_TOOL_OAI],
            "tool_choice": "auto",
            "messages": [{"role": "user", "content": USER_QUESTION}],
        })

        if resp.status_code != 200:
            fail(f"Turn 1 HTTP {resp.status_code}: {resp.text[:200]}")
            return False

        body    = resp.json()
        choice  = (body.get("choices") or [{}])[0]
        message = choice.get("message", {})
        finish  = choice.get("finish_reason")

        info(f"finish_reason : {finish}")
        info(f"message       : {json.dumps(message, indent=6)}")

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            # Gemini via Code Assist may silently ignore tools — flag it clearly
            text = message.get("content", "")
            if text:
                fail(
                    "No tool_calls — Code Assist endpoint likely does not support function calling.\n"
                    f"     Model responded with text instead: {text[:120]}"
                )
            else:
                fail("No tool_calls and no text — unexpected empty response")
            return False

        tc = tool_calls[0]
        ok(f"Tool called: {tc['function']['name']}({tc['function']['arguments']})")

        # ── Turn 2: send tool result ─────────────────────────────────────────
        resp2 = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={
            "model": "gemini-3-flash-preview",
            "tools": [WEATHER_TOOL_OAI],
            "messages": [
                {"role": "user",      "content": USER_QUESTION},
                message,
                {
                    "role":         "tool",
                    "tool_call_id": tc["id"],
                    "content":      WEATHER_RESULT,
                },
            ],
        })

        if resp2.status_code != 200:
            fail(f"Turn 2 HTTP {resp2.status_code}: {resp2.text[:200]}")
            return False

        body2   = resp2.json()
        choice2 = (body2.get("choices") or [{}])[0]
        final   = choice2.get("message", {}).get("content")

        if not final:
            fail("Turn 2 returned no text — expected final answer")
            return False

        ok(f"Final answer: {final[:120]}")
        return True


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print("\nFreeHive Tool Use Test")
    print(f"Backend: {BASE_URL}")

    # Quick connectivity check
    try:
        httpx.get(f"{BASE_URL}/health", timeout=3)
    except Exception:
        try:
            httpx.get(f"{BASE_URL}/v1/models", timeout=3, headers={"x-api-key": "freehive-claude"})
        except Exception as e:
            print(f"\nERROR: Cannot reach {BASE_URL} — is the backend running?\n{e}")
            sys.exit(1)

    results = {
        "Claude":  test_claude(),
        "ChatGPT": test_chatgpt(),
        "Gemini":  test_gemini(),
    }

    hdr("Summary")
    all_pass = True
    for provider, passed in results.items():
        status = "PASS" if passed else "FAIL"
        mark   = "✓" if passed else "✗"
        print(f"  {mark}  {provider:<10} {status}")
        if not passed:
            all_pass = False

    print()
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
