#!/usr/bin/env python3
"""Probe whether Cursor's agent.v1.AgentService/RunSSE accepts externally
injected tool definitions (McpTools) from a plain HTTP client.

Stdlib only. Run from an IP allowed to talk to api2.cursor.sh.

Flow:
  1. exchange crsr_ API key -> short-lived JWT accessToken
  2. POST several Connect-RPC framed AgentRunRequest variants, each carrying an
     mcpTools block that declares a fake `get_weather` tool
  3. decode every response frame, scan for success/error signals, print verdict

Verdicts:
  TOOL_ACCEPTED     get_weather / a toolCall shows up in the response
  STREAM_OK_NO_TOOL valid stream but only heartbeats / nothing tool-shaped
  SCHEMA_REJECTED   invalid_argument -> endpoint live, our field shape wrong
  ERROR_*           whatever error token the server returned
"""

import base64
import json
import socket
import struct
import sys
import time
import urllib.error
import urllib.request

API_KEY = "crsr_ab2d5e42e6aa60dbdb4f83f663895d9ec895a9d3a75806b16f485067f6d33e2e"

EXCHANGE_URL = "https://api2.cursor.sh/auth/exchange_user_api_key"
RUNSSE_URL = "https://api2.cursor.sh/agent.v1.AgentService/RunSSE"

CLIENT_VERSION = "cli-2026.07.23-e383d2b"
CLIENT_TYPE = "cli"

READ_DEADLINE = 45.0     # overall wall-clock seconds to sit on one response stream
SOCKET_TIMEOUT = 15.0    # per-read timeout

# NB: resp.read(n) blocks until n bytes accumulate or EOF. The server emits a
# 43-byte heartbeat frame every few seconds forever, so read(4096) would sit
# there for ~10 min collecting heartbeats and the wall-clock deadline below
# would never be consulted. read1() returns whatever is already buffered.

PROMPT = "What is the weather in Tokyo?"

MCP_TOOLS_BLOCK = {
    "mcpTools": [
        {
            "serverName": "testsrv",
            "serverIdentifier": "testsrv",
            "tools": [
                {
                    "toolName": "get_weather",
                    "description": "Get current weather for a city",
                    "inputSchemaJson": json.dumps(
                        {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        }
                    ),
                }
            ],
        }
    ]
}

SYSTEM_PROMPT = "You must use the get_weather tool to answer."


def log(msg=""):
    sys.stdout.write(str(msg) + "\n")
    sys.stdout.flush()


def hr(title=""):
    log()
    log("=" * 78)
    if title:
        log(title)
        log("=" * 78)


# ── step 1: API key -> JWT ────────────────────────────────────────────────
def b64url_decode(seg):
    seg = seg + "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg.encode("ascii"))


def exchange_api_key(key):
    body = b"{}"
    req = urllib.request.Request(
        EXCHANGE_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "x-cursor-client-version": CLIENT_VERSION,
            "x-cursor-client-type": CLIENT_TYPE,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as e:
        log("  exchange HTTP " + str(e.code))
        try:
            log("  body: " + e.read(600).decode("utf-8", "replace"))
        except Exception:
            pass
        return None
    except Exception as e:
        log("  exchange transport error: " + type(e).__name__ + ": " + str(e))
        return None

    log("  exchange status=" + str(status) + " bytes=" + str(len(raw)))
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        log("  exchange body not JSON: " + str(e))
        log("  raw[:400]=" + raw[:400].decode("utf-8", "replace"))
        return None

    log("  exchange keys: " + ", ".join(sorted(data.keys())))
    token = data.get("accessToken") or data.get("access_token")
    if not token:
        log("  NO accessToken in response")
        return None

    log("  accessToken len=" + str(len(token)))
    log("  accessToken head=" + token[:32] + "...")
    rt = data.get("refreshToken") or data.get("refresh_token")
    if rt:
        log("  refreshToken len=" + str(len(rt)))

    parts = token.split(".")
    log("  JWT segments=" + str(len(parts)))
    if len(parts) >= 2:
        try:
            claims = json.loads(b64url_decode(parts[1]).decode("utf-8", "replace"))
            log("  JWT claims:")
            for k in sorted(claims.keys()):
                v = claims[k]
                sv = str(v)
                if len(sv) > 120:
                    sv = sv[:120] + "..."
                log("    " + k + " = " + sv)
            exp = claims.get("exp")
            if isinstance(exp, (int, float)):
                left = exp - time.time()
                log("    -> exp in " + str(round(left / 60.0, 1)) + " min")
        except Exception as e:
            log("  JWT payload decode failed: " + str(e))
    return token


# ── Connect-RPC framing ───────────────────────────────────────────────────
def frame(payload_bytes):
    return struct.pack(">BI", 0, len(payload_bytes)) + payload_bytes


def decode_frames(raw):
    """Yield (flag, declared_len, payload_bytes). Tolerates a truncated tail."""
    out = []
    i = 0
    n = len(raw)
    while i + 5 <= n:
        flag = raw[i]
        (ln,) = struct.unpack(">I", raw[i + 1 : i + 5])
        body = raw[i + 5 : i + 5 + ln]
        out.append((flag, ln, body))
        if len(body) < ln:
            break
        i += 5 + ln
    if i < n and i + 5 > n:
        out.append((-1, n - i, raw[i:]))  # dangling partial header
    return out


def post_stream(token, payload_obj):
    """POST one framed request, drain the stream, return (status, raw_bytes, note)."""
    payload = json.dumps(payload_obj).encode("utf-8")
    body = frame(payload)
    req = urllib.request.Request(
        RUNSSE_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/connect+json",
            "connect-protocol-version": "1",
            "x-cursor-client-version": CLIENT_VERSION,
            "x-cursor-client-type": CLIENT_TYPE,
        },
    )
    started = time.time()
    raw = b""
    note = ""
    status = None
    try:
        resp = urllib.request.urlopen(req, timeout=SOCKET_TIMEOUT)
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read(65536)
        except Exception:
            raw = b""
        return status, raw, "http_error"
    except Exception as e:
        return None, b"", "transport_error: " + type(e).__name__ + ": " + str(e)

    status = resp.status
    reader = getattr(resp, "read1", None)
    heartbeat_frames = 0
    try:
        while True:
            if time.time() - started > READ_DEADLINE:
                note = "deadline_reached"
                break
            try:
                # read1 -> only what is already buffered, so we never block
                # waiting for a full 4096 bytes of trickling heartbeats
                chunk = reader(4096) if reader else resp.read(1)
            except socket.timeout:
                note = "socket_timeout"
                break
            except Exception as e:
                note = "read_error: " + type(e).__name__ + ": " + str(e)
                break
            if not chunk:
                note = "stream_closed"
                break
            raw += chunk
            # stop as soon as an end-of-stream frame (flag 2) is complete
            if _has_eos(raw):
                note = "eos_frame"
                break
            # heartbeats-only stream: bail out early, nothing more is coming
            hb = raw.count(b'"heartbeat"')
            if hb >= 8 and hb * 43 >= len(raw) - 60:
                heartbeat_frames = hb
                note = "heartbeat_only_after_" + str(hb) + "_frames"
                break
            if len(raw) > 2_000_000:
                note = "size_cap"
                break
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return status, raw, note or "done"


def _has_eos(raw):
    i = 0
    n = len(raw)
    while i + 5 <= n:
        flag = raw[i]
        (ln,) = struct.unpack(">I", raw[i + 1 : i + 5])
        if i + 5 + ln > n:
            return False
        if flag == 2:
            return True
        i += 5 + ln
    return False


# ── signal scanning ───────────────────────────────────────────────────────
import re

ERR_TOKEN_RE = re.compile(r"ERROR_[A-Z0-9_]+")


def scan(text):
    sig = {}
    sig["error_tokens"] = sorted(set(ERR_TOKEN_RE.findall(text)))
    sig["get_weather"] = "get_weather" in text
    sig["toolcall"] = ("toolCall" in text) or ("tool_call" in text)
    low = text.lower()
    sig["unimplemented"] = "unimplemented" in low
    sig["invalid_argument"] = ("invalid_argument" in low) or ("invalidargument" in low)
    sig["unauthenticated"] = "unauthenticated" in low
    sig["heartbeat"] = "heartbeat" in low
    sig["testsrv"] = "testsrv" in text
    return sig


def verdict_for(status, sig, frames, note=""):
    if sig["get_weather"] or sig["toolcall"]:
        return "TOOL_ACCEPTED"
    if sig["error_tokens"]:
        return "|".join(sig["error_tokens"])
    if sig["invalid_argument"]:
        return "SCHEMA_REJECTED"
    if note.startswith("heartbeat_only"):
        return "STREAM_OK_NO_TOOL (heartbeats only)"
    if sig["unauthenticated"]:
        return "UNAUTHENTICATED"
    if sig["unimplemented"]:
        return "UNIMPLEMENTED"
    if status == 200 and frames:
        return "STREAM_OK_NO_TOOL"
    if status is None:
        return "TRANSPORT_ERROR"
    return "HTTP_" + str(status) + "_NO_SIGNAL"


# ── variants ──────────────────────────────────────────────────────────────
def variants():
    base = {
        "mcpTools": MCP_TOOLS_BLOCK,
        "customSystemPrompt": SYSTEM_PROMPT,
        "harness": "cli",
    }

    def with_extra(extra):
        d = json.loads(json.dumps(base))
        d.update(extra)
        return d

    vs = []
    vs.append(("A_tools_only", with_extra({})))
    vs.append(
        (
            "B_action_userMessage",
            with_extra({"action": {"userMessage": {"text": PROMPT}}}),
        )
    )
    vs.append(
        (
            "C_convstate_messages",
            with_extra(
                {
                    "conversationState": {
                        "messages": [{"role": "user", "text": PROMPT}]
                    }
                }
            ),
        )
    )
    vs.append(
        (
            "D_action_plus_convstate",
            with_extra(
                {
                    "action": {"userMessage": {"text": PROMPT}},
                    "conversationState": {
                        "messages": [{"role": "user", "text": PROMPT}]
                    },
                }
            ),
        )
    )
    # E: model_details filled in — server may refuse to run an agent turn without a model
    vs.append(
        (
            "E_with_modelDetails",
            with_extra(
                {
                    "action": {"userMessage": {"text": PROMPT}},
                    "conversationState": {
                        "messages": [{"role": "user", "text": PROMPT}]
                    },
                    "modelDetails": {"modelName": "gpt-5"},
                    "requestedModel": {"modelName": "gpt-5"},
                }
            ),
        )
    )
    # F: alternate conversation_state shape — bubbles/parts naming used elsewhere in Cursor protos
    vs.append(
        (
            "F_convstate_bubbles",
            with_extra(
                {
                    "conversationState": {
                        "bubbles": [
                            {"type": "user", "text": PROMPT, "role": "user"}
                        ]
                    },
                    "action": {"sendMessage": {"text": PROMPT}},
                    "modelDetails": {"modelName": "gpt-5"},
                }
            ),
        )
    )
    # G: no mcpTools at all — control group, isolates whether mcpTools is what breaks it
    vs.append(
        (
            "G_control_no_mcpTools",
            {
                "action": {"userMessage": {"text": PROMPT}},
                "conversationState": {"messages": [{"role": "user", "text": PROMPT}]},
                "harness": "cli",
            },
        )
    )
    # H: NEGATIVE CONTROL. Pure nonsense field names. If this also streams
    # heartbeats, the server is not parsing/validating our body at all, and a
    # heartbeat-only response tells us nothing about tool acceptance.
    vs.append(
        (
            "H_negative_control_garbage",
            {
                "zzzNotAField": "garbage",
                "harness": 12345,
                "mcpTools": "this should be a message not a string",
            },
        )
    )
    # I: NEGATIVE CONTROL 2. Empty object — known from prior testing to stream
    # heartbeats. Included so all variants sit in one comparison table.
    vs.append(("I_negative_control_empty", {}))
    return vs


def run_variant(token, name, payload):
    hr("VARIANT " + name)
    pj = json.dumps(payload, indent=2, sort_keys=True)
    if len(pj) > 1400:
        pj = pj[:1400] + "\n... [truncated]"
    log("request payload:")
    log(pj)
    log()

    t0 = time.time()
    status, raw, note = post_stream(token, payload)
    dt = round(time.time() - t0, 1)
    log("-> http status=" + str(status) + " bytes=" + str(len(raw)) + " note=" + note + " elapsed=" + str(dt) + "s")

    frames = decode_frames(raw)
    log("-> frames decoded: " + str(len(frames)))
    hb_run = 0
    shown = 0
    for idx, (flag, ln, body) in enumerate(frames):
        txt = body.decode("utf-8", "replace")
        if "heartbeat" in txt:
            hb_run += 1
            continue
        if hb_run:
            log("   [" + str(hb_run) + " x heartbeat frame(s) collapsed]")
            hb_run = 0
        if shown >= 30:
            continue
        shown += 1
        pretty = txt
        try:
            pretty = json.dumps(json.loads(txt), sort_keys=True)
        except Exception:
            pass
        if len(pretty) > 400:
            pretty = pretty[:400] + "...[+" + str(len(pretty) - 400) + "]"
        log("   frame[" + str(idx) + "] flag=" + str(flag) + " len=" + str(ln) + " actual=" + str(len(body)))
        log("      " + pretty)
    if hb_run:
        log("   [" + str(hb_run) + " x heartbeat frame(s) collapsed]")

    text = raw.decode("utf-8", "replace")
    sig = scan(text)
    log()
    log("-> signals:")
    log("   ERROR_* tokens   : " + (", ".join(sig["error_tokens"]) if sig["error_tokens"] else "(none)"))
    log("   get_weather      : " + str(sig["get_weather"]))
    log("   toolCall         : " + str(sig["toolcall"]))
    log("   testsrv echoed   : " + str(sig["testsrv"]))
    log("   unimplemented    : " + str(sig["unimplemented"]))
    log("   invalid_argument : " + str(sig["invalid_argument"]))
    log("   unauthenticated  : " + str(sig["unauthenticated"]))
    log("   heartbeat        : " + str(sig["heartbeat"]))

    # Fingerprint the response SHAPE (frame flags + payload kinds), ignoring how
    # many heartbeats happened to arrive before the deadline. If every variant
    # shares one shape, the body had no observable effect.
    kinds = []
    for flag, ln, body in frames:
        t = body.decode("utf-8", "replace")
        try:
            o = json.loads(t)
            k = ",".join(sorted(o.keys()))
            if isinstance(o.get("interactionUpdate"), dict):
                k += "/" + ",".join(sorted(o["interactionUpdate"].keys()))
        except Exception:
            k = "nonjson:" + str(len(body))
        kinds.append(str(flag) + ":" + k)
    shape = "|".join(sorted(set(kinds))) or "(empty)"
    log()
    log("-> response shape (dedup): " + shape)

    v = verdict_for(status, sig, frames, note)
    log()
    log("-> VERDICT " + name + ": " + v)
    return v, sig, shape


def main():
    hr("STEP 1 — exchange API key for JWT")
    token = exchange_api_key(API_KEY)
    if not token:
        log("FATAL: could not obtain accessToken; aborting")
        return 1

    hr("STEP 2 — probe RunSSE with injected tool definitions")
    results = []
    for name, payload in variants():
        try:
            v, sig, shape = run_variant(token, name, payload)
        except Exception as e:
            v = "SCRIPT_ERROR:" + type(e).__name__ + ":" + str(e)
            sig = {"get_weather": False, "toolcall": False}
            shape = "(error)"
            log("-> VERDICT " + name + ": " + v)
        results.append((name, v, sig, shape))
        time.sleep(1.0)

    hr("SUMMARY")
    w = max(len(n) for n, _, _, _ in results)
    log("variant".ljust(w) + " | verdict".ljust(34) + " | response shape")
    log("-" * w + "-+-" + "-" * 32 + "-+-" + "-" * 34)
    for name, v, _, shape in results:
        log(name.ljust(w) + " | " + v.ljust(32) + " | " + shape[:60])

    accepted = [n for n, v, s, _ in results if s.get("get_weather") or s.get("toolcall")]
    shapes = {sh for _, _, _, sh in results}
    log()
    if accepted:
        log("CONCLUSION: injected tool WAS recognized by variant(s): " + ", ".join(accepted))
    else:
        log("CONCLUSION: NO variant got the injected tool recognized.")
        log("            No 'get_weather' string and no toolCall appeared in any response.")
    log()
    log("distinct response shapes across ALL variants: " + str(len(shapes)))
    if len(shapes) == 1:
        log("  !! Every variant -- including the nonsense/garbage negative controls --")
        log("  !! produced the SAME response shape. The server therefore did not")
        log("  !! validate or act on the request body at all. A heartbeat-only")
        log("  !! stream is NOT evidence that tools were rejected; it is evidence")
        log("  !! that this request never started an agent turn. Tool acceptance")
        log("  !! is UNPROVEN either way by this probe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
