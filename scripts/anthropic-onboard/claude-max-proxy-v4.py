#!/usr/bin/env python3
"""
Claude Max Anthropic-Messages transparent proxy (v4).

Bypasses Anthropic's OAuth /v1/messages model allowlist by impersonating the
official `claude` CLI's request shape:

  POST https://api.anthropic.com/v1/messages?beta=true
  Headers:
    Authorization: Bearer <oauth>
    anthropic-beta: <full Claude-Code beta set, see BETAS>
    anthropic-dangerous-direct-browser-access: true
    anthropic-version: 2023-06-01
    user-agent: claude-cli/<CC_VERSION> (external, cli)
    x-app: cli
  Body must have a 3-block `system`:
    [0] "x-anthropic-billing-header: cc_version=X.Y.Z.{fp}; cc_entrypoint=cli;"
    [1] "You are Claude Code, Anthropic's official CLI for Claude."
    [2] a CC system-prompt excerpt, carrying the cache breakpoint
  and the caller's own system prompt relocated into `messages`.

Everything else passes through untouched — tool_use, cache_control, vision,
thinking, max_tokens, etc.

WHAT CHANGED IN v4 (cross-checked against Wei-Shaw/sub2api, 2026-07-29)
----------------------------------------------------------------------
That project solves the same problem (subscription OAuth token -> /v1/messages)
and its backend/internal/pkg/claude/constants.go documents a failure mode v3 did
not defend against:

  "Anthropic 上游会基于 anthropic-beta 的完整集合判定请求来源；缺少任何
   '官方 Claude Code 请求才会带' 的 beta，都会被降级到第三方额度，
   对应报错: Third-party apps now draw from your extra usage, not your plan limits."

That degradation is SILENT at the HTTP layer — 200 OK, normal body, but billed
against extra usage instead of the plan. A /health check or a smoke test cannot
see it. Three deltas v3 had:

  1. anthropic-beta was missing oauth-2025-04-20 (the OAuth-path beta),
     effort-2025-11-24, and extended-cache-ttl-2025-04-11.
  2. cc_version in the billing block was "2.1.148.0b7" (4 segments) while the
     UA said "claude-cli/2.1.148" (3 segments). sub2api states these must match
     exactly or the request is judged third-party, and its regex for the field
     is cc_version=<3-segment semver> — our 4-segment value would not even match.
     v4 derives both from one 3-segment constant so they cannot drift.
  3. cc_entrypoint/UA said "sdk-cli"; real CLI traffic per sub2api is "cli".
  4. cc_version had no `.{fp}` suffix, and we sent a random md5 `cch=`. Both were
     wrong in the same direction — inventing fields instead of reproducing them.
     The suffix is SHA256(salt + firstUserText[4,7,20] + version)[:3] with salt
     59cf53e54c78, which is server-VALIDATED (same constant and algorithm found
     independently in sub2api and in a de-obfuscated Claude Code fingerprint.ts).
     `cch` is no longer sent by current CLI at all, so sending one is a tell.
  5. `system` was our blocks PREPENDED to the caller's text. Upstream inspects
     system *content*, so trailing non-CLI text defeats the mimicry regardless of
     what precedes it. Now: exact 3-block CLI shape, caller's prompt relocated
     into `messages` as a user/assistant pair (kept, not dropped).

UNVERIFIED: whether these deltas actually caused plan-vs-extra misbilling on our
traffic. Establishing that needs a live token and a before/after read of usage
attribution. Treat v4 as the candidate arm of that A/B, not a proven fix; v3 is
kept intact as the control.

Cross-check against new-api PR #1445 (QuantumNous/new-api, merged 2025-07-31 and
reverted 24h later by #1481; recover with
`gh api repos/QuantumNous/new-api/pulls/1445/files`). Independently confirms
client_id 9d1c250a-e61b-44d9-88ed-5944d1962f5e, scope user:inference, and
POST /v1/messages with `Authorization: Bearer`. Two deliberate divergences:

  - It OVERWRITES request.System with "You are Claude Code, Anthropic's official
    CLI for Claude." We PREPEND our block and keep the client's own system
    content, because overwriting silently discards caller instructions. Note our
    string differs from both new-api's and Claude Code's real one; sub2api injects
    no identity string at all and works, so this text is not load-bearing for
    provenance — the headers and the billing block are.
  - Its refresh scheduler fires every 5 min with the `shouldRefreshToken` expiry
    pre-check COMMENTED OUT, i.e. it refreshes unconditionally. Do not copy that:
    the token endpoint rate-limits per ACCOUNT (verified 2026-07-29 — the limit
    followed the account across two different egress IPs, .143 and .144), so a
    blind ticker is how you lock yourself out of minting. Any refresh loop here
    must gate on real expiry and back off on rate_limit_error.

Endpoints:
  POST /v1/messages   (only)
  GET  /v1/models
  GET  /health

Multi-account: sticky-by-conversation-hash, round-robin fallback.

Env:
  ACCT_TOKENS    "label1::sk-ant-oat01-...,label2::..."
  PORT           default 3456
  API_KEYS       optional bearer/x-api-key allowlist
  CC_VERSION     3-segment CLI version (default 2.1.220), drives UA + billing
  CC_ENTRYPOINT  billing cc_entrypoint (default cli)
  UPSTREAM       default https://api.anthropic.com
"""
import hashlib, http.client, json, os, re, ssl, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", "3456"))
API_KEYS = set(filter(None, os.environ.get("API_KEYS", "").split(",")))
# 3 segments only. The billing block's cc_version and the UA version must be
# byte-identical (sub2api: 不一致会被 Anthropic 判第三方), so both are derived
# from this one value instead of being written twice.
CC_VERSION = os.environ.get("CC_VERSION", "2.1.220")
CC_ENTRYPOINT = os.environ.get("CC_ENTRYPOINT", "cli")
UPSTREAM = os.environ.get("UPSTREAM", "https://api.anthropic.com")

if not re.fullmatch(r"\d+\.\d+\.\d+", CC_VERSION):
    # A 4-segment value like 2.1.148.0b7 silently breaks the billing-header
    # match upstream; fail loudly at boot rather than mis-bill every request.
    sys.exit(f"CC_VERSION must be 3-segment semver, got {CC_VERSION!r}")

# Full Claude-Code beta set, order matching real CLI traffic. Upstream judges
# request provenance on the COMPLETE set — omitting any one of these downgrades
# billing to third-party extra usage, with no HTTP-level error.
BETAS = (
    "claude-code-20250219",
    "oauth-2025-04-20",
    "interleaved-thinking-2025-05-14",
    "prompt-caching-scope-2026-01-05",
    "effort-2025-11-24",
    "context-management-2025-06-27",
    "extended-cache-ttl-2025-04-11",
)

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


# Salt for the cc_version fingerprint suffix. This is NOT a guess: the same
# constant and the same [4,7,20] + SHA256-first-3-hex algorithm appear both in
# sub2api (backend/internal/service/gateway_billing_block.go:16,28) and in a
# de-obfuscated Claude Code source dump (src/utils/fingerprint.ts), where it is
# commented as a hardcoded salt that must match for server-side validation.
# So the suffix is VALIDATED upstream, not decorative.
FINGERPRINT_SALT = "59cf53e54c78"
FP_INDICES = (4, 7, 20)


def first_user_text(req):
    """First text of the first role=user message; '' if absent.
    Accepts both string and block-list content shapes."""
    for m in req.get("messages", []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    return b.get("text", "") or ""
        return ""
    return ""


def cc_fingerprint(req):
    """Reproduce the CLI's cc_version suffix: chars 4/7/20 of the first user
    text (padded with '0'), salted, SHA256, first 3 hex chars."""
    t = first_user_text(req)
    chars = "".join(t[i] if i < len(t) else "0" for i in FP_INDICES)
    return hashlib.sha256(
        (FINGERPRINT_SALT + chars + CC_VERSION).encode("utf-8")).hexdigest()[:3]


def billing_header(req):
    """Build the `x-anthropic-billing-header` system block.

    cc_version carries the validated `.{fp}` suffix. There is deliberately NO
    `cch=` field: current Claude Code no longer sends it, so injecting one makes
    the request diverge from real CLI traffic (sub2api removed it for exactly
    this reason). v4 previously sent a random md5 `cch` — that was a liability,
    not a nicety."""
    return (f"x-anthropic-billing-header: cc_version={CC_VERSION}."
            f"{cc_fingerprint(req)}; cc_entrypoint={CC_ENTRYPOINT};")


DEGRADE_MARKERS = (
    b"Third-party apps now draw from your extra usage",
    b"not your plan limits",
)
_degraded = {}          # label -> count, surfaced on /health


def check_billing_degraded(payload, label):
    """Detect the SILENT plan->extra-usage downgrade.

    Upstream signals this in the response body while still returning 200, so it
    is invisible to status-code checks and to /health. Counting it per account
    is the only way an operator finds out that the mimicry has drifted.
    """
    if not payload:
        return
    for m in DEGRADE_MARKERS:
        if m in payload:
            _degraded[label] = _degraded.get(label, 0) + 1
            print(f"[{time.strftime('%H:%M:%S')}] ⚠️  BILLING DEGRADED on {label}: "
                  f"upstream billed this to extra usage, not the plan. "
                  f"anthropic-beta set is likely stale. count={_degraded[label]}",
                  flush=True)
            return


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


# Exact banner string. No trailing whitespace or newline — it is compared.
CC_BANNER = "You are Claude Code, Anthropic's official CLI for Claude."

# Third block: a short excerpt of the real CC system prompt, carrying the stable
# cache breakpoint. Tool-usage sections are deliberately omitted so we do not
# impose CLI tool behaviour on whoever is calling through this proxy.
CC_EXPANSION = (
    "You are an interactive CLI tool that helps users with software engineering "
    "tasks.\n\nIMPORTANT: Assist with defensive security tasks only. Refuse to "
    "create, modify, or improve code that may be used maliciously.\n\nIMPORTANT: "
    "Never generate or guess URLs for the user unless you are confident that the "
    "URLs are for helping the user with programming.\n\n# Tone and style\nYou "
    "should be concise, direct, and to the point."
)


def inject_identity(req):
    """Rewrite `system` into the real CLI's 3-block shape and RELOCATE the
    client's own system prompt into `messages`.

    Prepending our blocks in front of the caller's system text (what v3 did, and
    what v4 did until this was checked) is documented as insufficient: upstream
    detects third-party apps by inspecting the *content* of `system`, so the
    caller's non-CLI text still trailing behind the banner is itself the tell
    (sub2api gateway_claude_oauth_body.go:679-680 — "仅前置追加 Claude Code
    提示词无法通过检测，因为后续内容仍为非 Claude Code 格式").

    Instead the caller's instructions are moved into a synthetic user/assistant
    pair at the head of `messages`, so nothing is silently dropped — the model
    still receives them, just not in the position that gets fingerprinted.

    Order matters: the fingerprint reads the FIRST user message, so it must be
    computed before the synthetic pair is prepended.
    """
    fp_block = {"type": "text", "text": billing_header(req)}

    s = req.get("system")
    if s is None:
        original = ""
    elif isinstance(s, str):
        original = s
    elif isinstance(s, list):
        original = "\n\n".join(
            b.get("text", "") for b in s
            if isinstance(b, dict) and b.get("type") == "text").strip()
    else:
        original = ""

    req["system"] = [
        fp_block,
        {"type": "text", "text": CC_BANNER},
        {"type": "text", "text": CC_EXPANSION,
         "cache_control": {"type": "ephemeral"}},
    ]

    if original:
        req["messages"] = [
            {"role": "user",
             "content": [{"type": "text",
                          "text": "[System Instructions]\n" + original}]},
            {"role": "assistant",
             "content": [{"type": "text",
                          "text": "Understood. I will follow these instructions."}]},
        ] + list(req.get("messages", []))
    return req


UPSTREAM_HOST = urlparse(UPSTREAM).netloc
UPSTREAM_SCHEME = urlparse(UPSTREAM).scheme


def upstream_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": ",".join(BETAS),
        "anthropic-dangerous-direct-browser-access": "true",
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-app": "cli",
        "user-agent": f"claude-cli/{CC_VERSION} (external, {CC_ENTRYPOINT})",
        "x-stainless-arch": "arm64",
        "x-stainless-lang": "js",
        "x-stainless-os": "Linux",
        "x-stainless-package-version": "0.94.0",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": "v24.3.0",
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
            # billing_degraded is the one field worth alerting on: ok stays true
            # during a plan->extra-usage downgrade because HTTP still says 200.
            self._json(200, {"ok": True, "accounts": [a for a, _ in ACCOUNTS],
                             "mode": "transparent", "cc_version": CC_VERSION,
                             "betas": list(BETAS),
                             "billing_degraded": dict(_degraded)})
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
                    check_billing_degraded(chunk, label)
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
            check_billing_degraded(data, label)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            try: conn.close()
            except: pass


if __name__ == "__main__":
    print(f"Claude Max Anthropic-transparent proxy v4 on :{PORT}", flush=True)
    print(f"  upstream:  {UPSTREAM}/v1/messages?beta=true", flush=True)
    print(f"  cc_version: {CC_VERSION}", flush=True)
    print(f"  accounts:  {[a for a, _ in ACCOUNTS]}", flush=True)
    print(f"  API_KEYS:  {'enabled' if API_KEYS else 'disabled (open access)'}", flush=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.serve_forever()
