#!/usr/bin/env python3
"""zerokey-responses-bridge (PoC) — minimal Codex `/v1/responses` ↔ zerokey
`/v1/chat/completions` translator.

PURPOSE (minimal closed-loop proof, see docs/zerokey-codex-agent-bridge-plan.md):
  Codex speaks the Responses protocol with an `apply_patch` custom tool. zerokey
  (Bearer vscode) speaks Chat Completions and emits structured `create_file` /
  `replace_string_in_file` tool_calls via its ToolCompiler. This bridge:
    1. accepts POST /v1/responses
    2. forwards as chat/completions to zerokey (Bearer vscode), aggregating SSE
    3. maps the returned file-edit tool_call -> Codex `apply_patch` (V4A diff)
    4. returns a Responses-shaped JSON with a `custom_tool_call` output item

SCOPE: non-streaming round-trip proof only. Production needs streaming
`response.custom_tool_call_input.delta` events (Phase 3 in the plan).

ENV:
  BRIDGE_LISTEN     host:port           (default 127.0.0.1:8788)
  BRIDGE_UPSTREAM   zerokey base /v1    (default http://127.0.0.1:8125/v1)
  BRIDGE_UPSTREAM_AUTH  bearer value    (default vscode)
  BRIDGE_MODEL      default model       (default gpt-5-5)
"""
import json, os, re, sys, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = os.environ.get("BRIDGE_LISTEN", "127.0.0.1:8788")
UPSTREAM = os.environ.get("BRIDGE_UPSTREAM", "http://127.0.0.1:8125/v1").rstrip("/")
UP_AUTH = os.environ.get("BRIDGE_UPSTREAM_AUTH", "vscode")
DEF_MODEL = os.environ.get("BRIDGE_MODEL", "gpt-5-5")


def to_messages(inp):
    """Normalize Codex Responses `input` into chat messages."""
    if isinstance(inp, str):
        return [{"role": "user", "content": inp}]
    msgs = []
    if isinstance(inp, list):
        for it in inp:
            if isinstance(it, str):
                msgs.append({"role": "user", "content": it})
                continue
            if not isinstance(it, dict):
                continue
            role = it.get("role", "user")
            c = it.get("content", "")
            if isinstance(c, list):
                parts = []
                for seg in c:
                    if isinstance(seg, dict):
                        parts.append(seg.get("text") or seg.get("input_text") or "")
                    else:
                        parts.append(str(seg))
                c = "".join(parts)
            msgs.append({"role": role, "content": c})
    return msgs or [{"role": "user", "content": ""}]


def call_zerokey(model, messages):
    """Call zerokey chat/completions (Bearer vscode), aggregate SSE → (text, tool_calls)."""
    body = json.dumps({"model": model, "messages": messages, "stream": True}).encode()
    req = urllib.request.Request(
        f"{UPSTREAM}/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {UP_AUTH}", "Content-Type": "application/json"},
    )
    text_parts, tools = [], {}
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except Exception:
                continue
            delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
            if delta.get("content"):
                text_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tools.setdefault(idx, {"name": "", "arguments": ""})
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
    return "".join(text_parts), [tools[k] for k in sorted(tools)]


def to_v4a(tool):
    """Map a zerokey file-edit tool_call → V4A apply_patch text."""
    name = tool.get("name", "")
    try:
        args = json.loads(tool.get("arguments") or "{}")
    except Exception:
        args = {}
    path = args.get("filePath") or args.get("path") or args.get("file") or "UNKNOWN"
    if name in ("create_file", "write"):
        content = args.get("content", "")
        lines = "".join(f"+{ln}\n" for ln in content.splitlines() or [""])
        return f"*** Begin Patch\n*** Add File: {path}\n{lines}*** End Patch"
    if name in ("replace_string_in_file", "edit_file"):
        old = args.get("oldString") or args.get("old") or ""
        new = args.get("newString") or args.get("new") or ""
        hunk = "".join(f"-{ln}\n" for ln in old.splitlines())
        hunk += "".join(f"+{ln}\n" for ln in new.splitlines())
        return f"*** Begin Patch\n*** Update File: {path}\n@@\n{hunk}*** End Patch"
    return None


def build_response(model, text, tools):
    out = []
    for i, t in enumerate(tools):
        v4a = to_v4a(t)
        if v4a is not None:
            out.append({
                "type": "custom_tool_call", "id": f"ctc_{i}",
                "name": "apply_patch", "input": v4a,
                "_source_tool": t.get("name"),
            })
    if text:
        out.append({"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": text}]})
    return {
        "id": "resp_zkbridge", "object": "response", "model": model,
        "status": "completed", "output": out,
        "output_text": text,
    }


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "healthy", "upstream": UPSTREAM})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/responses"):
            return self._send(404, {"error": "only /v1/responses"})
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"error": f"bad json: {e}"})
        model = req.get("model") or DEF_MODEL
        messages = to_messages(req.get("input", ""))
        try:
            text, tools = call_zerokey(model, messages)
        except Exception as e:
            return self._send(502, {"error": f"upstream: {e}"})
        self._send(200, build_response(model, text, tools))

    def log_message(self, *a):
        sys.stderr.write("[bridge] " + (a[0] % a[1:]) + "\n")


if __name__ == "__main__":
    host, _, port = LISTEN.partition(":")
    srv = ThreadingHTTPServer((host, int(port)), H)
    print(f"zerokey-responses-bridge on {LISTEN} → {UPSTREAM} (Bearer {UP_AUTH})", flush=True)
    srv.serve_forever()
