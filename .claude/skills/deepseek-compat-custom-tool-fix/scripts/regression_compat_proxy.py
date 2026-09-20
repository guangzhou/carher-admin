#!/usr/bin/env python3
"""Regression suite for deepseek-v4-flash compat_proxy (8000) + vLLM (8001).

Covers BOTH endpoints — Chat Completions and Responses — because the compat_proxy
handles them differently since 2026-07-20 (path-aware rewrite gating):

  /v1/chat/completions  — nested tools passthrough, NO rewrite
  /v1/responses         — flat tools + custom-tool conversion + additional_tools extract

Run on the GPU box:
    scripts/jms ssh local-gpu -- 'python3 /tmp/regression_compat_proxy.py'

Or upload via jms:
    cat scripts/regression_compat_proxy.py | \
      scripts/jms ssh local-gpu -- 'cat > /tmp/rc.py && python3 /tmp/rc.py'

Exit code 0 = all pass; non-zero = at least one failure (with diff).
"""

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"  # compat_proxy under test
UPSTREAM = "http://127.0.0.1:8001"  # vLLM direct (used only for baseline)

failures: list = []


def _post(url: str, payload: dict, stream: bool = False):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _check(name: str, ok: bool, detail: str = ""):
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}  {detail[:200]}")
    if not ok:
        failures.append(name)


# ---------- Chat Completions (added 2026-07-20) ----------

# 1. Chat + nested function tools — the 2026-07-20 regression signature.
#    Before the path-aware fix: compat_proxy's _flatten_tools stripped
#    tools[].function -> vLLM 400 "body.tools[0].function: Field required".
status, body = _post(
    f"{BASE}/v1/chat/completions",
    {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "hi in one word"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "echo",
                "description": "echo x",
                "parameters": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                    "required": ["x"],
                },
            },
        }],
        "max_tokens": 16,
        "stream": False,
    },
)
_check(
    "chat-nested-tools-sync",
    status == 200 and "choices" in body and "Field required" not in body,
    f"status={status}",
)

# 2. Chat plain (no tools) — pre-existing baseline, must remain untouched.
status, body = _post(
    f"{BASE}/v1/chat/completions",
    {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 16,
        "stream": False,
    },
)
_check("chat-plain-sync", status == 200 and "choices" in body,
       f"status={status}")

# 3. Chat + tools + stream — Cursor/Codex's actual on-wire shape.
status, body = _post(
    f"{BASE}/v1/chat/completions",
    {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "say ok"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "echo",
                "parameters": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                },
            },
        }],
        "max_tokens": 8,
        "stream": True,
    },
)
_check(
    "chat-nested-tools-stream",
    status == 200 and "chat.completion.chunk" in body,
    f"status={status} bytes={len(body)}",
)

# ---------- Responses API (2026-07-17 fix — must remain green) ----------

CUSTOM_TOOL_INPUT = [
    {"role": "user", "content": "run a script"},
    {"type": "custom_tool_call", "call_id": "c1", "name": "exec",
     "input": "console.log('hi')"},
    {"type": "custom_tool_call_output", "call_id": "c1", "output": "hi"},
    {"role": "user", "content": "now say bye"},
]

# 4. Responses + custom_tool_call sync — the 2026-07-17 crash signature.
#    Must NOT surface `'ResponseCustomToolCall' object has no attribute 'get'`.
status, body = _post(
    f"{BASE}/v1/responses",
    {
        "model": "deepseek-v4-flash",
        "input": CUSTOM_TOOL_INPUT,
        "tools": [{"type": "custom", "name": "exec", "description": "Run JS"}],
        "max_output_tokens": 32,
        "stream": False,
    },
)
parsed = json.loads(body) if status == 200 else {}
_check(
    "responses-custom-tool-sync",
    status == 200
    and parsed.get("status") in ("completed", "incomplete")
    and "ResponseCustomToolCall" not in body,
    f"status={status} response.status={parsed.get('status')}",
)

# 5. Responses stream custom_tool_call — Cursor/Codex on the responses path.
status, body = _post(
    f"{BASE}/v1/responses",
    {
        "model": "deepseek-v4-flash",
        "input": CUSTOM_TOOL_INPUT,
        "tools": [{"type": "custom", "name": "exec"}],
        "max_output_tokens": 32,
        "stream": True,
    },
)
_check(
    "responses-custom-tool-stream",
    status == 200
    and ("response.completed" in body or "response.incomplete" in body)
    and "ResponseCustomToolCall" not in body,
    f"status={status} bytes={len(body)}",
)

# 6. Responses + plain input — regression on non-custom-tool path.
status, body = _post(
    f"{BASE}/v1/responses",
    {
        "model": "deepseek-v4-flash",
        "input": [{"role": "user", "content": "hello"}],
        "max_output_tokens": 16,
        "stream": False,
    },
)
_check("responses-plain-sync", status == 200,
       f"status={status}")

# 7. Responses + standard function tool — regression: must not be touched.
status, body = _post(
    f"{BASE}/v1/responses",
    {
        "model": "deepseek-v4-flash",
        "input": [{"role": "user", "content": "what is 2+2? use the tool"}],
        "tools": [{
            "type": "function", "name": "add", "description": "add",
            "parameters": {"type": "object",
                           "properties": {"a": {"type": "number"}}},
        }],
        "max_output_tokens": 32,
        "stream": False,
    },
)
_check("responses-std-function", status == 200, f"status={status}")

# ---------- Summary ----------

print()
print(f"total={7} pass={7 - len(failures)} fail={len(failures)}")
if failures:
    print("failed:", ", ".join(failures))
    sys.exit(1)
