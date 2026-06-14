#!/usr/bin/env python3
"""
Claude Max Anthropic-Messages transparent proxy (v3).

Bypasses Anthropic's OAuth /v1/messages model allowlist by impersonating the
official `claude` CLI's request shape:

  POST https://api.anthropic.com/v1/messages?beta=true
  Headers:
    Authorization: Bearer <oauth>
    anthropic-beta: ...,claude-code-20250219
    anthropic-dangerous-direct-browser-access: true
    anthropic-version: 2023-06-01
    user-agent: claude-cli/...
    x-app: cli
  Body must have system[0] = "x-anthropic-billing-header: cc_version=...;
                              cc_entrypoint=sdk-cli; cch=XXX;"
  And system[1] = "You are a Claude agent, built on Anthropic's Claude Agent SDK."

We prepend these two system blocks to whatever the client sends, leaving the
rest of the request *untouched* — so tool_use, cache_control, vision, thinking,
max_tokens, etc. all pass through.

Endpoints:
  POST /v1/messages   (only)
  GET  /v1/models
  GET  /health

Multi-account: sticky-by-conversation-hash, round-robin fallback.

Env:
  ACCT_TOKENS    "label1::sk-ant-oat01-...,label2::..."
  PORT           default 3456
  API_KEYS       optional bearer/x-api-key allowlist
  CC_VERSION     billing header version string (default 2.1.148.0b7)
  UPSTREAM       default https://api.anthropic.com
"""
import hashlib, http.client, json, os, ssl, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", "3456"))
API_KEYS = set(filter(None, os.environ.get("API_KEYS", "").split(",")))
CC_VERSION = os.environ.get("CC_VERSION", "2.1.148.0b7")
UPSTREAM = os.environ.get("UPSTREAM", "https://api.anthropic.com")

RAW = os.environ.get("ACCT_TOKENS", "").strip()
ACCOUNTS = []
for entry in RAW.split(","):
    entry = entry.strip()
    if not entry:
        continue
    if "::" in entry:
        label, tok = entry.split("::", 1)
    else:
        label, tok = f"acct-{len(ACCOUNTS)+1}", entry
    ACCOUNTS.append((label, tok))
if not ACCOUNTS and "ANTHROPIC_AUTH_TOKEN" in os.environ:
    ACCOUNTS.append(("acct-default", os.environ["ANTHROPIC_AUTH_TOKEN"]))
if not ACCOUNTS:
    sys.exit("no ACCT_TOKENS or ANTHROPIC_AUTH_TOKEN set")

_rr_lock = threading.Lock()
_rr_idx = 0


def pick_account(req_hash=None):
    global _rr_idx
    if req_hash:
        return ACCOUNTS[int(req_hash, 16) % len(ACCOUNTS)]
    with _rr_lock:
        a = ACCOUNTS[_rr_idx % len(ACCOUNTS)]
        _rr_idx += 1
        return a


# Models we expose via /v1/models (cosmetic; upstream accepts any valid id).
ADVERTISED = ("claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5")


def billing_header(body_bytes):
    """Generate the `x-anthropic-billing-header` system block.
    `cch` is a short hash of body content (mirrors CLI behavior; server appears
    to ignore the actual value, but we keep it varying to match real traffic)."""
    cch = hashlib.md5(body_bytes).hexdigest()[:5]
    return (f"x-anthropic-billing-header: cc_version={CC_VERSION}; "
            f"cc_entrypoint=sdk-cli; cch={cch};")


def conversation_hash(req):
    blob = req.get("model", "") + "\n"
    for m in req.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            blob += m.get("role", "") + ":" + c + "\n"
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    blob += m.get("role", "") + ":" + b.get("text", "") + "\n"
    return hashlib.md5(blob.encode("utf-8")).hexdigest()[:8]


def inject_identity(req):
    """Prepend the Claude-Code identification system blocks; preserve client's
    own system content."""
    cli_system = [
        {"type": "text", "text": billing_header(json.dumps(req).encode("utf-8"))},
        {"type": "text",
         "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK."},
    ]
    s = req.get("system")
    if s is None:
        new_system = cli_system
    elif isinstance(s, str):
        new_system = cli_system + [{"type": "text", "text": s}]
    elif isinstance(s, list):
        new_system = cli_system + s
    else:
        new_system = cli_system
    req["system"] = new_system
    return req


UPSTREAM_HOST = urlparse(UPSTREAM).netloc
UPSTREAM_SCHEME = urlparse(UPSTREAM).scheme


def upstream_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": ("interleaved-thinking-2025-05-14,"
                           "context-management-2025-06-27,"
                           "prompt-caching-scope-2026-01-05,"
                           "claude-code-20250219"),
        "anthropic-dangerous-direct-browser-access": "true",
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-app": "cli",
        "user-agent": f"claude-cli/{CC_VERSION.split('.0b')[0]} (external, sdk-cli)",
        "x-stainless-arch": "x64",
        "x-stainless-lang": "js",
        "x-stainless-os": "Linux",
        "x-stainless-package-version": "0.94.0",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": "v22.14.0",
        "x-stainless-retry-count": "0",
        "x-stainless-timeout": "600",
        "x-claude-code-session-id": str(uuid.uuid4()),
        "x-client-request-id": str(uuid.uuid4()),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}", flush=True)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self):
        if not API_KEYS:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Bearer ") and h[7:] in API_KEYS:
            return True
        xak = self.headers.get("x-api-key", "")
        return bool(xak and xak in API_KEYS)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {"ok": True, "accounts": [a for a, _ in ACCOUNTS], "mode": "transparent"})
        elif path == "/v1/models":
            self._json(200, {"data": [
                {"id": m, "type": "model", "display_name": m, "created_at": "2025-01-01T00:00:00Z"}
                for m in ADVERTISED
            ]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._check_auth():
            return self._json(401, {"type": "error",
                                    "error": {"type": "authentication_error", "message": "unauthorized"}})
        if urlparse(self.path).path != "/v1/messages":
            return self._json(404, {"type": "error",
                                    "error": {"type": "invalid_request_error", "message": "only /v1/messages supported"}})

        try:
            n = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(n)
            req = json.loads(raw_body)
        except Exception as e:
            return self._json(400, {"type": "error",
                                    "error": {"type": "invalid_request_error", "message": f"bad json: {e}"}})

        model = req.get("model", "claude-opus-4-7")
        stream = bool(req.get("stream", False))
        n_msgs = len(req.get("messages", []))
        n_tools = len(req.get("tools", []))

        sticky = conversation_hash(req)
        label, token = pick_account(sticky)
        print(f"  → {label} model={model} stream={stream} msgs={n_msgs} tools={n_tools} sticky={sticky}",
              flush=True)

        # Inject identification system blocks (preserve client's own system).
        inject_identity(req)
        upstream_body = json.dumps(req, ensure_ascii=False).encode("utf-8")

        # Connect to upstream.
        try:
            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(UPSTREAM_HOST, timeout=600, context=ctx)
            conn.request("POST", "/v1/messages?beta=true", body=upstream_body,
                         headers=upstream_headers(token))
            resp = conn.getresponse()
        except Exception as e:
            return self._json(502, {"type": "error",
                                    "error": {"type": "api_error", "message": f"upstream connect failed: {e}"}})

        # Pass through status, content-type, and body verbatim (incl. SSE).
        self.send_response(resp.status)
        ct = resp.getheader("Content-Type", "application/json")
        self.send_header("Content-Type", ct)
        # Forward Anthropic ratelimit headers + request id (useful for clients).
        for h, v in resp.getheaders():
            if h.lower().startswith("anthropic-") or h.lower() == "request-id":
                self.send_header(h, v)
        if "text/event-stream" in ct:
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try: conn.close()
                except: pass
        else:
            data = resp.read()
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            try: conn.close()
            except: pass


if __name__ == "__main__":
    print(f"Claude Max Anthropic-transparent proxy v3 on :{PORT}", flush=True)
    print(f"  upstream:  {UPSTREAM}/v1/messages?beta=true", flush=True)
    print(f"  cc_version: {CC_VERSION}", flush=True)
    print(f"  accounts:  {[a for a, _ in ACCOUNTS]}", flush=True)
    print(f"  API_KEYS:  {'enabled' if API_KEYS else 'disabled (open access)'}", flush=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.serve_forever()
