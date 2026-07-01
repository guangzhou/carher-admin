#!/usr/bin/env python3
"""zerokey-codex-responses-bridge — Codex `/v1/responses` <-> zerokey
`/v1/chat/completions` translator with full Agent loop support.

WHY (see docs/zerokey-codex-agent-bridge-plan.md):
  Codex CLI (>=0.134) only speaks the Responses protocol and drives a local
  agent loop via the `exec_command` tool (unified exec: shell + apply_patch).
  zerokey (Bearer vscode) speaks Chat Completions and emits structured
  tool_calls in VS Code grammar (`run_in_terminal`, `create_file`,
  `replace_string_in_file`, ...) via its ToolCompiler. Routing zerokey through
  LiteLLM drops these tools, so we need a dedicated bridge that:

    1. accepts POST /v1/responses (stream or JSON)
    2. flattens Codex `input[]` (incl. prior function_call / function_call_output)
       into chat messages
    3. forwards to zerokey (Bearer vscode), aggregating the SSE tool_calls
    4. maps each zerokey tool_call -> a Codex `exec_command` function_call:
         run_in_terminal             -> exec_command{cmd}
         create_file / write         -> exec_command{cmd: apply_patch Add File}
         replace_string_in_file/...  -> exec_command{cmd: apply_patch Update File}
         read_file / list_dir / grep -> exec_command{cmd: cat/ls/grep}
    5. streams the proper Responses events back to Codex, which executes the
       command locally (real sandbox / diff) and sends results next turn.

ENV:
  BRIDGE_LISTEN        host:port           (default 127.0.0.1:8788)
  BRIDGE_UPSTREAMS     comma list base /v1 (default http://10.68.13.188:8124/v1)
  BRIDGE_UPSTREAM_AUTH bearer value        (default vscode)
  BRIDGE_MODEL         upstream model      (default gpt-5-5)
  BRIDGE_LOG           debug log file      (default /tmp/zk_bridge.log)
  BRIDGE_DEBUG         "1" to log bodies   (default 0)
"""
import json, os, re, sys, time, threading, urllib.request, itertools
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = os.environ.get("BRIDGE_LISTEN", "127.0.0.1:8788")
UPSTREAMS = [u.strip().rstrip("/") for u in os.environ.get(
    "BRIDGE_UPSTREAMS", "http://10.68.13.188:8124/v1").split(",") if u.strip()]
UP_AUTH = os.environ.get("BRIDGE_UPSTREAM_AUTH", "vscode")
UP_MODEL = os.environ.get("BRIDGE_MODEL", "gpt-5-5")
LOGFILE = os.environ.get("BRIDGE_LOG", "/tmp/zk_bridge.log")
DEBUG = os.environ.get("BRIDGE_DEBUG", "0") == "1"

_rr = itertools.cycle(UPSTREAMS)
_rr_lock = threading.Lock()


def _log(*a):
    line = "[%s] %s\n" % (time.strftime("%H:%M:%S"), " ".join(str(x) for x in a))
    try:
        with open(LOGFILE, "a") as f:
            f.write(line)
    except Exception:
        pass
    sys.stderr.write(line)


def next_upstream():
    with _rr_lock:
        return next(_rr)


# ----------------------------------------------------------------------------
# Codex input[] -> chat messages
# ----------------------------------------------------------------------------
def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for seg in content:
            if isinstance(seg, dict):
                out.append(seg.get("text") or seg.get("input_text")
                           or seg.get("output_text") or "")
            else:
                out.append(str(seg))
        return "".join(out)
    return ""


SYS_PREAMBLE = (
    "You are a coding agent operating directly on a REAL local filesystem in the "
    "current working directory. You MUST act using only these tools:\n"
    "  - run_in_terminal: to run any shell command (build, test, run scripts, "
    "inspect files with cat/ls/grep, create or modify files with shell when "
    "convenient).\n"
    "  - create_file: to create a new file with full content.\n"
    "  - replace_string_in_file: to edit an existing file.\n"
    "Hard rules:\n"
    "  - NEVER use the canvas / textdoc / document / 'text_document_type' tool. "
    "Those do not touch the filesystem and are forbidden here.\n"
    "  - To create a file, ALWAYS call create_file or run_in_terminal; never "
    "just describe the file or emit a JSON document object as text.\n"
    "  - After each tool result, keep going until the task is fully done, then "
    "give one short final summary."
)


_FNAME_RE = re.compile(r'[\w./-]+\.[A-Za-z0-9]{1,8}')


def _infer_filename(stem, hint_text):
    """Pick the real filename (with extension) the user asked for, matching the
    leaked textdoc `name` (which usually lacks an extension)."""
    cands = [c for c in _FNAME_RE.findall(hint_text or "")
             if not c.endswith(".") and "/" not in c[:1]]
    base_stem = os.path.splitext(os.path.basename(str(stem)))[0].lower()
    for c in cands:
        if os.path.splitext(os.path.basename(c))[0].lower() == base_stem:
            return c
    if len(cands) == 1:
        return cands[0]
    if "." in os.path.basename(str(stem)):
        return stem
    return f"{stem}.md"


def salvage_tool_from_text(text, hint_text=""):
    """Some web responses leak a canvas/textdoc JSON as plain content instead of
    a real tool_call. Recover it into a create_file tool_call so the file is
    actually written, inferring the intended filename from the request."""
    if not text:
        return None
    m = re.search(r'\{[^{}]*"text_document_type"[^{}]*\}', text, re.S)
    if not m:
        m = re.search(r'\{[^{}]*"content"\s*:[^{}]*"name"\s*:[^{}]*\}'
                      r'|\{[^{}]*"name"\s*:[^{}]*"content"\s*:[^{}]*\}', text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    content = obj.get("content")
    name = obj.get("name") or obj.get("filename") or obj.get("path")
    if not name or content is None:
        return None
    name = _infer_filename(name, hint_text)
    return {"name": "create_file",
            "arguments": json.dumps({"filePath": name, "content": content})}


def build_messages(instructions, inp):
    msgs = [{"role": "system", "content": SYS_PREAMBLE}]
    call_names = {}  # call_id -> tool name (label outputs)

    def add(role, text):
        text = text or ""
        if text.strip():
            msgs.append({"role": role, "content": text})

    if isinstance(inp, str):
        add("user", inp)
        return msgs
    for it in inp or []:
        if not isinstance(it, dict):
            add("user", str(it))
            continue
        typ = it.get("type")
        role = it.get("role")
        if typ in (None, "message") and role:
            t = _text_of(it.get("content"))
            if role == "developer":
                add("system", t)
            elif role == "assistant":
                add("assistant", t)
            else:
                add("user", t)
        elif typ == "function_call":
            cid = it.get("call_id") or it.get("id") or ""
            nm = it.get("name") or "tool"
            call_names[cid] = nm
            args = it.get("arguments") or ""
            add("assistant", f"[invoked {nm} {args}]")
        elif typ in ("function_call_output", "custom_tool_call_output"):
            cid = it.get("call_id") or ""
            nm = call_names.get(cid, "tool")
            out = it.get("output")
            if isinstance(out, (dict, list)):
                out = json.dumps(out)
            add("user", f"[output of {nm}]:\n{out}")
        elif typ == "custom_tool_call":
            add("assistant", f"[applied {it.get('name')}]:\n{it.get('input','')}")
        elif typ == "reasoning":
            continue
        else:
            t = _text_of(it.get("content"))
            if t:
                add("user", t)
    return msgs


# ----------------------------------------------------------------------------
# zerokey chat/completions (stream) -> (text, tool_calls)
# ----------------------------------------------------------------------------
def _call_one(base, messages):
    body = json.dumps({"model": UP_MODEL, "messages": messages,
                       "stream": True}).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {UP_AUTH}",
                 "Content-Type": "application/json"})
    text_parts, tools = [], {}
    with urllib.request.urlopen(req, timeout=240) as resp:
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


def call_zerokey(messages):
    """Try accounts across the pool; on 5xx/empty/error, fail over to the next.
    Returns (text, tool_calls). Raises only if every account fails."""
    n = len(UPSTREAMS)
    start = next_upstream()
    order = UPSTREAMS[UPSTREAMS.index(start):] + UPSTREAMS[:UPSTREAMS.index(start)]
    last_err = None
    for base in order:
        try:
            text, tcs = _call_one(base, messages)
        except Exception as e:
            last_err = e
            _log("upstream %s failed: %r -> failover" % (base, e))
            continue
        if tcs or (text and text.strip()):
            if base != order[0]:
                _log("recovered on %s" % base)
            return text, tcs
        last_err = RuntimeError("empty response")
        _log("upstream %s returned empty -> failover" % base)
    raise last_err or RuntimeError("all upstreams failed")


# ----------------------------------------------------------------------------
# zerokey tool_call -> Codex exec_command{cmd}
# ----------------------------------------------------------------------------
def _heredoc(patch):
    return "apply_patch <<'CODEX_PATCH_EOF'\n" + patch + "\nCODEX_PATCH_EOF"


def _add_file(path, content):
    lines = content.splitlines() or [""]
    body = "".join(f"+{ln}\n" for ln in lines)
    return _heredoc(f"*** Begin Patch\n*** Add File: {path}\n{body}*** End Patch")


def _update_file(path, old, new):
    hunk = "".join(f"-{ln}\n" for ln in old.splitlines())
    hunk += "".join(f"+{ln}\n" for ln in new.splitlines())
    return _heredoc(f"*** Begin Patch\n*** Update File: {path}\n@@\n{hunk}*** End Patch")


def _shq(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def tool_to_cmd(tc):
    name = (tc.get("name") or "").strip()
    try:
        a = json.loads(tc.get("arguments") or "{}")
    except Exception:
        a = {}
    path = a.get("filePath") or a.get("path") or a.get("file") or a.get("filename")
    if name in ("run_in_terminal", "run_in_terminal2", "terminal", "bash", "shell"):
        return a.get("command") or a.get("cmd") or ""
    if name in ("create_file", "write", "write_file", "new_file"):
        return _add_file(path or "UNKNOWN", a.get("content") or a.get("contents") or "")
    if name in ("replace_string_in_file", "edit_file", "insert_edit_into_file",
                "apply_patch", "str_replace"):
        old = a.get("oldString") or a.get("old_str") or a.get("old") or ""
        new = a.get("newString") or a.get("new_str") or a.get("new") or a.get("content") or ""
        if old == "" and new and path:
            return _add_file(path, new)
        return _update_file(path or "UNKNOWN", old, new)
    if name in ("read_file", "cat"):
        if path:
            sl = a.get("startLine") or a.get("start_line")
            el = a.get("endLine") or a.get("end_line")
            if sl and el:
                return f"sed -n {int(sl)},{int(el)}p {_shq(path)}"
            return f"cat {_shq(path)}"
    if name in ("list_dir", "list_directory", "ls"):
        return f"ls -la {_shq(path or '.')}"
    if name in ("grep_search", "search", "ripgrep", "file_search"):
        q = a.get("query") or a.get("pattern") or a.get("regex") or ""
        return f"grep -rn {_shq(q)} ."
    # unknown tool with an embedded command
    if a.get("command"):
        return a["command"]
    return None


def make_function_item(tc, rid, i):
    cmd = tool_to_cmd(tc)
    if cmd is None:
        return None
    return {
        "type": "function_call",
        "id": f"fc_{rid}_{i}",
        "call_id": f"call_{rid}_{i}",
        "name": "exec_command",
        "arguments": json.dumps({"cmd": cmd}),
        "status": "completed",
    }


def make_message_item(rid, text):
    return {
        "type": "message", "id": f"msg_{rid}", "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.rstrip("/")
        if p.endswith("/models"):
            return self._json(200, {"object": "list", "data": [
                {"id": "zerokey-codex", "object": "model", "owned_by": "zerokey"},
                {"id": UP_MODEL, "object": "model", "owned_by": "zerokey"}]})
        return self._json(200, {"status": "healthy", "upstreams": UPSTREAMS})

    def _sse_open(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

    def _ev(self, t, d):
        d = dict(d)
        d["type"] = t
        self.wfile.write(("event: %s\ndata: %s\n\n" % (t, json.dumps(d))).encode())
        self.wfile.flush()

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/responses"):
            return self._json(404, {"error": "only /v1/responses supported"})
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._json(400, {"error": f"bad json: {e}"})
        stream = bool(req.get("stream"))
        messages = build_messages(req.get("instructions"), req.get("input"))
        if DEBUG:
            _log("REQ messages:", json.dumps(messages)[:2000])
        else:
            _log("REQ items=%d stream=%s" % (len(req.get("input") or []), stream))
        try:
            text, tcs = call_zerokey(messages)
        except Exception as e:
            _log("UPSTREAM ERROR:", repr(e))
            return self._fail(stream, req, f"upstream error: {e}")
        rid = "zk_%d" % int(time.time() * 1000)
        # recover a leaked canvas/textdoc JSON into a real create_file call
        if not tcs:
            hint = " ".join(m.get("content", "") for m in messages
                            if m.get("role") == "user")
            salvaged = salvage_tool_from_text(text, hint)
            if salvaged:
                _log("SALVAGE textdoc -> create_file")
                tcs = [salvaged]
        items = []
        for i, tc in enumerate(tcs):
            it = make_function_item(tc, rid, i)
            if it:
                items.append(it)
        # text only (no actionable tools) -> assistant message
        if not items:
            items.append(make_message_item(rid, text or "(no output)"))
        _log("RESP rid=%s items=%s" % (
            rid, [it.get("name") or it["type"] for it in items]))
        if stream:
            return self._stream(rid, req, items)
        return self._json(200, self._final_obj(rid, req, items, "completed"))

    def _final_obj(self, rid, req, items, status):
        return {
            "id": "resp_" + rid, "object": "response",
            "created_at": int(time.time()), "status": status,
            "model": req.get("model") or UP_MODEL, "output": items,
            "parallel_tool_calls": False, "tool_choice": "auto",
            "tools": req.get("tools") or [],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }

    def _stream(self, rid, req, items):
        self._sse_open()
        base = {"id": "resp_" + rid, "object": "response",
                "created_at": int(time.time()), "status": "in_progress",
                "model": req.get("model") or UP_MODEL, "output": []}
        self._ev("response.created", {"response": base})
        self._ev("response.in_progress", {"response": base})
        for oi, item in enumerate(items):
            added = {k: item[k] for k in ("type", "id", "role", "call_id", "name")
                     if k in item}
            if item["type"] == "function_call":
                added["arguments"] = ""
                added["status"] = "in_progress"
                self._ev("response.output_item.added",
                         {"output_index": oi, "item": added})
                args = item["arguments"]
                self._ev("response.function_call_arguments.delta",
                         {"item_id": item["id"], "output_index": oi, "delta": args})
                self._ev("response.function_call_arguments.done",
                         {"item_id": item["id"], "output_index": oi, "arguments": args})
            else:  # message
                added["content"] = []
                added["status"] = "in_progress"
                self._ev("response.output_item.added",
                         {"output_index": oi, "item": added})
                txt = item["content"][0]["text"]
                self._ev("response.content_part.added",
                         {"item_id": item["id"], "output_index": oi,
                          "content_index": 0,
                          "part": {"type": "output_text", "text": ""}})
                self._ev("response.output_text.delta",
                         {"item_id": item["id"], "output_index": oi,
                          "content_index": 0, "delta": txt})
                self._ev("response.output_text.done",
                         {"item_id": item["id"], "output_index": oi,
                          "content_index": 0, "text": txt})
                self._ev("response.content_part.done",
                         {"item_id": item["id"], "output_index": oi,
                          "content_index": 0,
                          "part": {"type": "output_text", "text": txt}})
            self._ev("response.output_item.done",
                     {"output_index": oi, "item": item})
        done = self._final_obj(rid, req, items, "completed")
        self._ev("response.completed", {"response": done})

    def _fail(self, stream, req, msg):
        rid = "zk_err_%d" % int(time.time() * 1000)
        items = [make_message_item(rid, f"[bridge] {msg}")]
        if stream:
            return self._stream(rid, req, items)
        return self._json(200, self._final_obj(rid, req, items, "completed"))


if __name__ == "__main__":
    host, _, port = LISTEN.partition(":")
    srv = ThreadingHTTPServer((host, int(port)), H)
    _log("zerokey-codex-responses-bridge on %s -> %s (Bearer %s, model %s)" % (
        LISTEN, UPSTREAMS, UP_AUTH, UP_MODEL))
    print("zerokey-codex-responses-bridge on %s -> %s (Bearer %s, model %s)" % (
        LISTEN, UPSTREAMS, UP_AUTH, UP_MODEL), flush=True)
    srv.serve_forever()
