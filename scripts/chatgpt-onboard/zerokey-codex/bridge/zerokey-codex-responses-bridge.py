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
import hashlib, json, os, re, sys, time, threading, urllib.request, itertools
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = os.environ.get("BRIDGE_LISTEN", "127.0.0.1:8788")
UPSTREAMS = [u.strip().rstrip("/") for u in os.environ.get(
    "BRIDGE_UPSTREAMS", "http://10.68.13.188:8124/v1").split(",") if u.strip()]
UP_AUTH = os.environ.get("BRIDGE_UPSTREAM_AUTH", "vscode")
UP_MODEL = os.environ.get("BRIDGE_MODEL", "gpt-5-5")
LOGFILE = os.environ.get("BRIDGE_LOG", "/tmp/zk_bridge.log")
DEBUG = os.environ.get("BRIDGE_DEBUG", "0") == "1"

_rr = itertools.cycle(UPSTREAMS)
_rr_int = itertools.count()   # tier-internal round-robin
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


# ── pod health tracking ──────────────────────────────────────────────
# Some zerokey pods are chronically unhealthy (web session stale → never emit
# tool_calls). Track a rolling score per upstream: a tool_call HIT bumps it up,
# a miss/error drops it. next_healthy_upstream() prefers high-score pods and
# skips ones that recently kept failing, so fanout=1 lands on a good pod.
_health = {u: 0.0 for u in UPSTREAMS}   # score, clamped [-3, 3]
_health_seen = {}                       # upstream -> last time we scored it
_cooldown = {}                          # upstream -> don't touch until ts
_health_lock = threading.Lock()
HEALTH_DECAY_S = float(os.environ.get("BRIDGE_HEALTH_DECAY_S", 180))
COOLDOWN_S = float(os.environ.get("BRIDGE_COOLDOWN_S", 60))
# Much shorter than a rate-limit cooldown: a 502 is usually a blip, so we only
# want to stop the CURRENT burst of requests from re-picking that pod.
ERR_COOLDOWN_S = float(os.environ.get("BRIDGE_ERR_COOLDOWN_S", 10))


def cool_down(u, seconds=None):
    """Take an upstream out of rotation for a while.

    A rate limit is categorically different from a transient failure: 502 means
    "try again", 429 means "this account is capped, stop asking". Scoring them
    alike (both just -1) let a 429'd pod stay in rotation and keep burning
    retries. sub2api models the same idea per-account (rate_limited_until /
    quota-schedulable gating); this is the small version of it.
    """
    with _health_lock:
        _cooldown[u] = time.time() + (COOLDOWN_S if seconds is None else seconds)


def mark_health(u, hit, error=False):
    """Score an upstream. Three outcomes, deliberately NOT equal weight:

      hit   (+1)   emitted a tool_call
      error (-1)   transport/HTTP failure — a real defect
      miss  (-0.34) returned text but no tool_call

    A miss used to cost a full point, but per the GUIDE plain text is the CORRECT
    answer to a greeting or an explanation — so a few chat turns drove EVERY pod
    to the -3 floor and the ranking became meaningless. A miss is now a weak
    signal: only a sustained run of them moves a pod down a tier.
    """
    with _health_lock:
        s = _health.get(u, 0.0) + (1.0 if hit else (-1.0 if error else -0.34))
        _health[u] = max(-3.0, min(3.0, s))
        _health_seen[u] = time.time()


def _tier(u, now):
    """Health rounded to a tier, decayed toward 0 the longer we haven't used the
    pod. Without decay a pod that hit -3 on one bad streak (a transient DNS blip
    on zero-91.svc) was blacklisted forever and never retried."""
    s = _health.get(u, 0.0)
    if s < 0:
        idle = now - _health_seen.get(u, 0)
        if idle > HEALTH_DECAY_S:
            s += (idle / HEALTH_DECAY_S)   # creep back toward 0
            s = min(0.0, s)
    return int(round(s))


HEALTH_BAD = float(os.environ.get("BRIDGE_HEALTH_BAD", -2))


def next_healthy_upstream(n=1):
    """Round-robin over all upstreams that aren't currently BAD.

    Health is an EXCLUSION filter, not a ranking key. Two earlier versions both
    starved the pool by ranking:
      - sort-by-score: 400 requests over 4 pods went 291/109/0/0
      - best-tier-then-rotate: the winner climbed to score 3, sat alone in the
        top tier, and took 400/400
    Both defeat the point of having ~19 accounts: quota should spread evenly and
    only genuinely broken pods should be skipped. So: filter out the bad ones,
    then plain round-robin the rest.
    """
    now = time.time()
    with _health_lock:
        good = [u for u in UPSTREAMS
                if _tier(u, now) > HEALTH_BAD and _cooldown.get(u, 0) <= now]
        if not good:                        # everything cooling/bad -> least-bad
            good = [u for u in UPSTREAMS if _cooldown.get(u, 0) <= now]
        pool = good or list(UPSTREAMS)      # truly nothing left -> try anyway
        if not good:
            pool.sort(key=lambda u: -_tier(u, now))
    if len(pool) > 1:
        with _rr_lock:
            k = next(_rr_int) % len(pool)
        pool = pool[k:] + pool[:k]
    return pool[:max(1, n)]


# ----------------------------------------------------------------------------
# Codex input[] -> chat messages
# ----------------------------------------------------------------------------
def _json_unquote(inner):
    """Decode the inside of a JSON string literal (no surrounding quotes).

    Single source of truth for build_messages() and _history_commands(): they
    used two different decoders and disagreed on non-ASCII, so the loop brake
    compared a mojibaked label against a correct command and never matched.
    """
    try:
        return json.loads('"' + inner + '"')
    except Exception:
        return inner


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
    "You produce tool calls for an external executor. When a task needs to run a "
    "command, read/inspect files, or create/edit a file, reply by CALLING the "
    "matching tool — run_in_terminal (any shell command incl. cat/ls/grep and "
    "running CLIs), create_file, or replace_string_in_file. A separate executor "
    "runs the call and returns the result to you next turn; you never run "
    "anything yourself and never see the result this turn.\n"
    "Rules:\n"
    "  - The executor HAS full access to the command line and filesystem. Never "
    "say you lack a terminal, cannot access files/systems, or ask the user to run "
    "a command and paste the output — instead CALL run_in_terminal with that "
    "command.\n"
    "  - Assume the user wants the action carried out. A plain-text answer or a "
    "refusal INSTEAD OF a tool call is a failure.\n"
    "  - Never emit a canvas / textdoc / document object as text; use create_file."
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
    # ONLY the explicit canvas marker. The old permissive fallback matched any
    # object carrying "content" + "name", so ordinary prose like
    #   Here is an example manifest:
    #   {"name": "demo", "content": "hello"}
    # became a create_file — and _infer_filename pulls the name from the USER's
    # question, so an explanation about package.json turned into a request to
    # overwrite the real package.json with a 5-byte example. A leaked textdoc
    # always carries text_document_type; prose doesn't.
    m = re.search(r'\{[^{}]*"text_document_type"[^{}]*\}', text, re.S)
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
    # A SHORT, permissive guide (not the old forcing job-queue framing which made
    # pods refuse). It only tells the model WHEN to use a tool vs reply in text —
    # fixing "hi" triggering a pointless `echo hi` loop because the model saw an
    # exec tool and felt compelled to call it. Chat/greetings → plain text.
    GUIDE = (
        "You are a coding agent with a real shell tool that runs on the user's "
        "machine. If the user asks to run a command, read/list files, or execute "
        "anything (e.g. `lark-cli ...`, `ls`, `cat`), you MUST call the tool to "
        "actually run it — NEVER invent, guess, or simulate the command's output "
        "in text (fabricating a result like 'Output: hi' is wrong). "
        "Only reply in plain text for pure conversation (greetings, small talk, "
        "explanations) where no command needs to run. After a tool result comes "
        "back, give a short final answer instead of re-running the same command. "
        # The web model's default self-image is a sandboxed chatbot with no shell,
        # so on anything involving a URL or external resource it used to refuse
        # ("I can't run lark-cli, so I can't fabricate the result") and hand the
        # command back for the user to run. It DOES have a shell here — say so.
        "IMPORTANT: never claim you cannot run commands or lack tool access, and "
        "never ask the user to run a command and paste the output back. The tool "
        "call is your only way to act, and the shell has network access plus "
        "authenticated CLIs installed. A URL in the request is not a blocker: "
        "reach it with the appropriate CLI (e.g. a Feishu/Lark docx link → "
        "`lark-cli api GET /open-apis/docx/v1/documents/<DOC_ID>/raw_content`, "
        "where <DOC_ID> is the path segment after /docx/). Call the tool first, "
        "then answer from its real output.")
    msgs = [{"role": "system", "content": GUIDE}]
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
                # Codex's developer prompts ("You are Codex… operating on a REAL
                # local filesystem", multi_agent_mode, personality) make the web
                # model refuse (it knows it has no filesystem). Drop them; our
                # SYS_PREAMBLE + tools[] injection already frame the task
                # correctly for the web-injection pods.
                continue
            elif role == "assistant":
                add("assistant", t)
            else:
                # Drop Codex's <environment_context> turn: it declares
                # <filesystem><workspace_roots>… which tells the web model it has
                # a real filesystem — conflicting with the web pod's "no
                # filesystem" self-image and triggering a refusal. The tools[] +
                # SYS_PREAMBLE already frame execution correctly.
                if "<environment_context>" in t or "<workspace_roots>" in t:
                    continue
                add("user", t)
        elif typ == "function_call":
            cid = it.get("call_id") or it.get("id") or ""
            nm = it.get("name") or "tool"
            call_names[cid] = nm
            args = it.get("arguments") or ""
            add("assistant", f"[invoked {nm} {args}]")
        elif typ in ("function_call_output", "custom_tool_call_output"):
            cid = it.get("call_id") or ""
            nm = call_names.get(cid, "command")
            out = it.get("output")
            # Codex custom_tool_call_output is a list of {type:input_text,text:..}
            if isinstance(out, list):
                out = "".join(seg.get("text", "") if isinstance(seg, dict) else str(seg)
                              for seg in out)
            elif isinstance(out, (dict,)):
                out = json.dumps(out)
            add("user", f"[result of {nm}]:\n{out}")
        elif typ == "custom_tool_call":
            # exec code_mode: input is JS `await tools.exec_command({"cmd":".."})`.
            # Extract the real command so the web model sees a human-readable
            # history ("already ran X") instead of raw JS it can't interpret.
            cid = it.get("call_id") or it.get("id") or ""
            js = it.get("input", "") or ""
            m = re.search(r'"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"', js)
            # Decode as JSON, not unicode_escape: the latter mojibakes UTF-8
            # (`测试` -> `æµè¯`), and real clients send raw UTF-8 here. A garbled
            # history label meant the model couldn't match its own prior command,
            # defeating the loop brake on exactly the lark-cli-with-Chinese
            # workload. Must agree with _history_commands().
            cmd = _json_unquote(m.group(1)) if m else js
            call_names[cid] = cmd[:40]
            add("assistant", f"[already ran]: {cmd}")
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
CALL_BUDGET = int(os.environ.get("BRIDGE_CALL_BUDGET", 60))
BODY_DUMP = os.environ.get("BRIDGE_BODY_DUMP", "/tmp/codex_bridge_body.json")


def _retry_after(exc):
    """Honour an upstream Retry-After when it gives one, else the default
    cooldown. Only accepts a plain delta-seconds value; HTTP-date form is rare
    here and not worth parsing wrong."""
    try:
        v = exc.headers.get("Retry-After")
        if v and str(v).strip().isdigit():
            return max(1.0, min(600.0, float(v)))
    except Exception:
        pass
    return None


def _call_timeout(deadline=None):
    """Seconds an upstream call may still take. Passed explicitly from
    call_zerokey — it must NOT be thread-local state, because _call_one runs in
    worker threads that would never see the handler thread's value."""
    if not deadline:
        return CALL_BUDGET
    left = deadline - time.time()
    if left <= 0:
        return 5
    return max(5, min(CALL_BUDGET, left))


def _call_one(base, messages, deadline=None):
    # Pass structured `tools` so the current zerokey web pods trigger their
    # tool-injection path (exec-harvest / envelope). The old ToolCompiler
    # inferred tools from the system prompt alone; the web-injection pods need
    # a real tools[] to switch out of plain-chat mode. run_in_terminal maps to
    # exec-harvest (shell), the file tools to envelope injection.
    tools_schema = [
        {"type": "function", "function": {
            "name": "run_in_terminal",
            "description": "Run a shell command in the user's terminal and return its output.",
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string", "description": "The shell command to run."}},
                "required": ["command"]}}},
        {"type": "function", "function": {
            "name": "create_file",
            "description": "Create a new file with full content.",
            "parameters": {"type": "object", "properties": {
                "filePath": {"type": "string"}, "content": {"type": "string"}},
                "required": ["filePath", "content"]}}},
        {"type": "function", "function": {
            "name": "replace_string_in_file",
            "description": "Edit an existing file by replacing a string.",
            "parameters": {"type": "object", "properties": {
                "filePath": {"type": "string"}, "oldString": {"type": "string"},
                "newString": {"type": "string"}},
                "required": ["filePath", "oldString", "newString"]}}},
    ]
    # Target the pod's /v1/responses web-injection path (Bearer raw), NOT
    # /chat/completions (Bearer vscode → old ToolCompiler, which 500s on these
    # tool schemas). responses.js runs the exec-harvest / job-queue injection
    # fixed 2026-07-24 and returns a native function_call. Tools use the
    # Responses shape (top-level name). Messages → a single flattened input turn.
    resp_tools = [
        {"type": "function", "name": t["function"]["name"],
         "description": t["function"].get("description", ""),
         "parameters": t["function"]["parameters"]}
        for t in tools_schema
    ]
    input_items = [{"role": ("assistant" if m["role"] == "assistant" else
                             ("system" if m["role"] == "system" else "user")),
                    "content": m["content"]}
                   for m in messages]
    body = json.dumps({"model": UP_MODEL, "input": input_items,
                       "tools": resp_tools, "tool_choice": "auto",
                       "stream": False}).encode()
    if DEBUG:
        # DEBUG-only: this is a full user prompt. It used to be written
        # unconditionally to a fixed world-readable /tmp path (symlink-followable,
        # and on the shared 188 host it persisted other users' prompts).
        try:
            fd = os.open(BODY_DUMP, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(body)
        except FileExistsError:
            pass
        except Exception:
            pass
    req = urllib.request.Request(
        f"{base}/responses", data=body, method="POST",
        headers={"Authorization": "Bearer raw",
                 "Content-Type": "application/json"})
    text_parts, tools = [], {}
    # Bounded by the caller's remaining deadline, not a flat 240s: the executor
    # is abandoned with shutdown(wait=False) and its workers are NON-daemon, so
    # every over-long urlopen stranded a thread + socket well past the 60s the
    # caller waited, delaying pod shutdown and piling up under load.
    with urllib.request.urlopen(req, timeout=_call_timeout(deadline)) as resp:
        d = json.loads(resp.read().decode("utf-8", "ignore"))
    idx = 0
    for o in d.get("output", []) or []:
        typ = o.get("type")
        if typ == "function_call":
            args = o.get("arguments")
            if not isinstance(args, str):
                args = json.dumps(args or {})
            tools[idx] = {"name": o.get("name", ""), "arguments": args}
            idx += 1
        elif typ == "message":
            for c in o.get("content", []) or []:
                if isinstance(c, dict) and c.get("text"):
                    text_parts.append(c["text"])
    return "".join(text_parts), [tools[k] for k in sorted(tools)]


def call_zerokey(messages, want_tools=True, max_rounds=2):
    """Fan out to several pods concurrently; return the FIRST usable result and
    abandon the rest (don't block on slow/miss pods). "Usable" = a tool_call, or
    (when the model legitimately answers in text) the first substantive text.
    Web-injection has per-account variance, so a pod that only refuses is skipped
    in favor of the next completer. Falls back to best text if a round yields
    nothing. Key perf fix: never wait for a whole round to finish once we have an
    answer, and cap total wall time so Codex's stream timeout isn't hit."""
    import concurrent.futures
    best_text = ""
    last_err = None
    fanout = int(os.environ.get('BRIDGE_FANOUT', '1'))
    deadline = time.time() + CALL_BUDGET  # hard wall-clock cap for the whole call
    # Transport errors (a burst of upstream 502s) are worth retrying on a
    # DIFFERENT pod, and with ~19 upstreams a flat 2 rounds threw the request
    # away whenever both picks landed in the bad batch — measured 3/8 failures
    # during a 502 burst even though healthy pods were available. Rounds are only
    # spent on hard errors; the deadline still bounds total time.
    max_err_rounds = max(max_rounds, min(6, len(UPSTREAMS)))
    tried = set()
    _round = -1
    while True:
        _round += 1
        if _round >= max_err_rounds or time.time() > deadline:
            break
        batch = [u for u in next_healthy_upstream(fanout + len(tried))
                 if u not in tried][:max(1, fanout)]
        if not batch:                      # exhausted the pool -> allow reuse
            tried.clear()
            batch = next_healthy_upstream(fanout)
        tried.update(batch)
        # NOT a daemon pool — ThreadPoolExecutor workers are non-daemon, so an
        # abandoned future keeps the interpreter (and pod shutdown) waiting. We
        # bound each urlopen by the remaining deadline instead, so workers can
        # only outlive us briefly.
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(batch)))
        futs = {ex.submit(_call_one, b, messages, deadline): b for b in batch}
        got_text_this_round = False
        try:
            remaining = max(1, deadline - time.time())
            for fut in concurrent.futures.as_completed(futs, timeout=remaining):
                b = futs[fut]
                try:
                    text, tcs = fut.result()
                except Exception as e:
                    last_err = e
                    mark_health(b, False, error=True)   # transport failure
                    code = getattr(e, "code", None)
                    if code in (429, 503):
                        # Rate limited / capacity-shed: asking again soon just
                        # burns retries. Park this pod and move on.
                        cool_down(b, _retry_after(e))
                        _log("upstream %s rate-limited (%s) -> cooling down"
                             % (b, code))
                    else:
                        # Brief parking for hard failures too. Upstream web
                        # sessions 502 in bursts: one request hit SIX 502 pods
                        # in a row and exhausted its rounds. A short cooldown
                        # steers CONCURRENT requests away from pods we just saw
                        # fail, instead of each one rediscovering them.
                        cool_down(b, ERR_COOLDOWN_S)
                        _log("upstream %s failed: %r" % (b, e))
                    continue
                if tcs:
                    mark_health(b, True)
                    _log("HIT %s r%d" % (b, _round))
                    ex.shutdown(wait=False)
                    return text, tcs
                mark_health(b, False)
                if text and text.strip():
                    best_text = text
                    got_text_this_round = True
                    # Chat/no-tool mode: first substantive text wins immediately.
                    if not want_tools:
                        ex.shutdown(wait=False)
                        return text, tcs
        except concurrent.futures.TimeoutError:
            _log("round %d timed out" % _round)
        finally:
            ex.shutdown(wait=False)
        if time.time() > deadline:
            break
        # A whole round finished with NO tool_call. Empirically the next round
        # rarely produces one either, and burning it doubles latency (24s vs
        # ~10s). If this round gave ANY text, return it now instead of retrying.
        if got_text_this_round and best_text:
            _log("round %d no tool_call, has text -> return (skip retry)" % _round)
            return best_text, []
        _log("round %d: no tool_call, no text -> retry" % _round)
    if best_text:
        return best_text, []
    raise last_err or RuntimeError("all upstreams failed")


# ----------------------------------------------------------------------------
# zerokey tool_call -> Codex exec_command{cmd}
# ----------------------------------------------------------------------------
class UnsafeToolArgs(Exception):
    """Model-supplied tool arguments that can't be turned into a safe command."""


def _safe_path(path):
    """Validate a model-chosen file path before it goes into a patch header.

    The path is picked by a REMOTE web ChatGPT session whose context can be
    poisoned by anything it reads (e.g. a Lark doc fetched via lark-cli), and the
    string we build is executed by Codex through `zsh -lc`. A newline in the path
    used to break out of the heredoc entirely:
        filePath = 'a.txt\\n*** End Patch\\nCODEX_PATCH_EOF\\nrm -rf ~/x'
    closed the sentinel and left `rm -rf ~/x` as a top-level command.
    """
    p = str(path or "")
    if not p:
        raise UnsafeToolArgs("empty path")
    if any(c in p for c in "\n\r\x00"):
        raise UnsafeToolArgs("path contains newline/NUL: %r" % p[:80])
    return p


def _heredoc(patch):
    # Randomized sentinel: a fixed one can be reproduced inside model-supplied
    # content to terminate the heredoc early. Also assert the body can't contain
    # it, so we never emit a command that closes itself.
    eof = "CODEX_PATCH_EOF_%s" % hashlib.sha256(
        (patch + str(next(_rid_seq))).encode()).hexdigest()[:16]
    if eof in patch:  # astronomically unlikely; refuse rather than emit
        raise UnsafeToolArgs("patch body collides with sentinel")
    return "apply_patch <<'%s'\n%s\n%s" % (eof, patch, eof)


def _add_file(path, content):
    path = _safe_path(path)
    lines = str(content or "").splitlines() or [""]
    body = "".join(f"+{ln}\n" for ln in lines)
    return _heredoc(f"*** Begin Patch\n*** Add File: {path}\n{body}*** End Patch")


def _update_file(path, old, new):
    path = _safe_path(path)
    hunk = "".join(f"-{ln}\n" for ln in str(old or "").splitlines())
    hunk += "".join(f"+{ln}\n" for ln in str(new or "").splitlines())
    return _heredoc(f"*** Begin Patch\n*** Update File: {path}\n@@\n{hunk}*** End Patch")


def _shq(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def _strip_shell_wrapper(cmd):
    """The web model often wraps the command as `bash -lc <cmd>` / `sh -lc '<cmd>'`.
    Codex's exec_command already runs cmd via `zsh -lc`, so a leftover inner
    `bash -lc lark-cli api GET "..."` becomes `zsh -lc 'bash -lc lark-cli ...'`
    where `-lc` only takes the FIRST word (`lark-cli`) and drops the rest → the
    CLI prints help. Strip one leading shell wrapper so Codex wraps the real
    command exactly once."""
    if not isinstance(cmd, str):
        return cmd
    s = cmd.strip()
    m = re.match(r"^(?:bash|sh|zsh)\s+-l?c\s+(.*)$", s, re.S)
    if not m:
        return s
    inner = m.group(1).strip()
    # unquote a single fully-quoted argument: '...'  or  "..."
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
        q = inner[0]
        body = inner[1:-1]
        if q == "'":
            body = body.replace("'\\''", "'")
        else:
            body = body.replace('\\"', '"').replace("\\\\", "\\")
        return body
    return inner


def tool_to_cmd(tc):
    name = (tc.get("name") or "").strip()
    try:
        a = json.loads(tc.get("arguments") or "{}")
    except Exception:
        a = {}
    # The web model emits camelCase or snake_case unpredictably; missing a
    # spelling used to silently drop the whole tool call (read_file with
    # file_path returned None → user saw "(no output)") or write to a file
    # literally named UNKNOWN.
    path = (a.get("filePath") or a.get("file_path") or a.get("path")
            or a.get("file") or a.get("filename") or a.get("file_name"))
    if name in ("run_in_terminal", "run_in_terminal2", "terminal", "bash", "shell"):
        return _strip_shell_wrapper(a.get("command") or a.get("cmd") or "")
    if name in ("create_file", "write", "write_file", "new_file"):
        if not path:
            raise UnsafeToolArgs("create_file without a path")
        return _add_file(path, a.get("content") or a.get("contents") or "")
    if name in ("replace_string_in_file", "edit_file", "insert_edit_into_file",
                "apply_patch", "str_replace"):
        old = a.get("oldString") or a.get("old_str") or a.get("old") or ""
        new = a.get("newString") or a.get("new_str") or a.get("new") or a.get("content") or ""
        if not path:
            raise UnsafeToolArgs("%s without a path" % name)
        if old == "" and new:
            return _add_file(path, new)
        return _update_file(path, old, new)
    if name in ("read_file", "cat"):
        if path:
            sl = a.get("startLine") or a.get("start_line")
            el = a.get("endLine") or a.get("end_line")
            # Model may send non-numeric bounds ("end", "EOF"). int() raised
            # mid-request and killed the handler thread after the SSE stream was
            # already open, so the client got no terminating event at all.
            try:
                if sl and el:
                    return "sed -n %d,%dp %s" % (int(sl), int(el), _shq(path))
            except (TypeError, ValueError):
                pass  # unusable range -> just cat the file
            return f"cat {_shq(path)}"
    if name in ("list_dir", "list_directory", "ls"):
        return f"ls -la {_shq(path or '.')}"
    if name in ("grep_search", "search", "ripgrep", "file_search"):
        q = a.get("query") or a.get("pattern") or a.get("regex") or ""
        # honour the requested scope instead of silently recursing everything
        scope = (a.get("includePattern") or a.get("include_pattern")
                 or a.get("include") or a.get("glob"))
        if scope:
            return "grep -rn --include=%s %s ." % (_shq(scope), _shq(q))
        return f"grep -rn {_shq(q)} ."
    # unknown tool with an embedded command
    if a.get("command"):
        return _strip_shell_wrapper(a["command"])
    return None


def _user_spoke_after_last_tool(inp):
    """True if a user message comes after the last tool call/result in history.

    Distinguishes "user asked me to run that again" (legitimate repeat) from the
    web model re-issuing a command on its own (runaway loop). In a loop, the tail
    of the history is tool_call → tool_output → tool_call …, with no new user
    turn; when the human asks for a re-run, their message is the last item."""
    last_tool = -1
    last_user = -1
    for i, it in enumerate(inp or []):
        if not isinstance(it, dict):
            continue
        typ = it.get("type")
        if typ in ("custom_tool_call", "function_call",
                   "custom_tool_call_output", "function_call_output"):
            last_tool = i
        # Codex sends plain user turns WITHOUT a "type" key ({"role":"user",...}),
        # which build_messages already tolerates. Requiring type=="message" here
        # made this guard a no-op against real clients — the brake still ate
        # legitimate "run it again" requests.
        elif typ in (None, "message") and it.get("role") == "user":
            last_user = i
    return last_user > last_tool


def _history_commands(inp):
    """Shell commands already executed in this thread's history (from prior
    custom_tool_call `exec` JS or exec_command function_call args). Used by the
    loop brake to detect the web model re-issuing the same command forever."""
    cmds = set()
    for it in inp or []:
        if not isinstance(it, dict):
            continue
        typ = it.get("type")
        if typ == "custom_tool_call":
            raw = it.get("input", "") or ""
        elif typ == "function_call":
            raw = it.get("arguments", "") or ""
        else:
            continue
        m = re.search(r'"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        if m:
            cmds.add(_json_unquote(m.group(1)))
    return cmds


def _req_uses_exec_tool(req):
    """Detect whether Codex offered the `exec` custom tool (code_mode / IDE
    app-server). If so, the client expects a `custom_tool_call` named `exec`
    whose input is JS `await tools.exec_command({...})`, NOT a plain
    `exec_command` function_call. (Blueprint captured from native acct 2026-07-24.)
    Codex puts `exec` inside an input item of type `additional_tools`."""
    for it in req.get("input") or []:
        if isinstance(it, dict) and it.get("type") == "additional_tools":
            for t in it.get("tools") or []:
                if isinstance(t, dict) and t.get("name") == "exec" \
                        and t.get("type") == "custom":
                    return True
    return False


def make_function_item(tc, rid, i, use_exec_custom=False):
    cmd = tool_to_cmd(tc)
    if cmd is None:
        return None
    if use_exec_custom:
        # Codex IDE / code_mode: return a `custom_tool_call` named `exec` whose
        # input is JS that calls the nested exec_command tool. Codex evaluates
        # this JS locally in its V8 isolate, runs the shell, and feeds the result
        # back as `custom_tool_call_output`. Matches the native acct blueprint.
        js = ('const result = await tools.exec_command('
              + json.dumps({"cmd": cmd}) + ');\ntext(result.output);')
        return {
            "type": "custom_tool_call",
            "id": f"ctc_{rid}_{i}",
            "call_id": f"call_{rid}_{i}",
            "name": "exec",
            "input": js,
            "status": "completed",
        }
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


SPENDLOG = os.environ.get("BRIDGE_SPENDLOG", "/tmp/zk_spendlog.jsonl")

class ClientGone(Exception):
    """Peer closed the connection mid-SSE (user cancelled the turn)."""


_rid_seq = itertools.count(1)
_rid_lock = threading.Lock()


def new_rid():
    """Unique per request. A bare ms timestamp collides under concurrency (this
    is a ThreadingHTTPServer): 8 parallel requests landed on 3 shared rids in
    testing. That's not just confusing logs — SpendLogs inserts use request_id
    with ON CONFLICT DO NOTHING, so a collision silently DROPS a user's billing
    row. Monotonic counter + pid keeps it unique across threads and across the
    replicas that share one spend log."""
    with _rid_lock:
        n = next(_rid_seq)
    return "zk_%d_%d_%d" % (int(time.time() * 1000), os.getpid() % 100000, n)


def _est_tokens(s):
    return max(1, len(s) // 4) if s else 0


def caller_key_hash(auth_header):
    """litellm stores LiteLLM_VerificationToken.token as sha256(plaintext key),
    so hashing the caller's Bearer gives the exact value SpendLogs.api_key needs
    for the row to show up under that user in the UI. Verified 2026-07-25 against
    cursor-liuguoxian-l08v. Returns "" when no usable key was sent (row then
    falls back to BRIDGE_DEFAULT_KEYHASH at flush time)."""
    if not auth_header:
        return ""
    tok = auth_header.strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    # placeholder used on the bridge→pod hop; never a real user key
    if not tok or tok == "raw":
        return ""
    return hashlib.sha256(tok.encode()).hexdigest()


def _spendlog(rid, req, messages, items, out_text, elapsed, key_hash=""):
    """Append one accounting row per request (codex→bridge→zerokey traffic that
    litellm can't see, since the bridge talks to pods directly). JSONL, one line
    per call — cheap, non-blocking. Fields mirror LiteLLM_SpendLogs so it can be
    ingested later. Never raises."""
    try:
        prompt_txt = "".join(m.get("content", "") for m in messages)
        pt = _est_tokens(prompt_txt)
        ct = _est_tokens(out_text or "") + sum(
            _est_tokens(it.get("input") or it.get("arguments") or "") for it in items)
        kind = "tool_call" if any(
            it.get("type") in ("custom_tool_call", "function_call") for it in items) else "message"
        row = {
            "request_id": "resp_" + rid,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "model": req.get("model") or UP_MODEL,
            "model_group": "zerokey-pool",
            "custom_llm_provider": "zerokey-web",
            "kind": kind,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
            "input_items": len(req.get("input") or []),
            "latency_s": round(elapsed, 2),
            # attribution: whose litellm key made this call (sha256 of the key)
            "api_key": key_hash or "",
        }
        with open(SPENDLOG, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


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

    MAX_BODY = int(os.environ.get("BRIDGE_MAX_BODY", 32 * 1024 * 1024))

    def _content_length(self):
        """Parsed Content-Length, or None if absent/garbage/oversized. A bare
        int() raised on 'abc' (empty reply, dropped connection), and an inflated
        length like 4000000000 parked the handler thread inside rfile.read() —
        a trivial thread-exhaustion vector on an endpoint with no key check."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return None
        if n < 0 or n > self.MAX_BODY:
            return None
        return n

    def _read_body(self):
        n = self._content_length()
        if n is None:
            return None
        return self.rfile.read(n) if n else b""

    def _drain_body(self):
        """Consume the request body so a keep-alive connection stays in sync."""
        try:
            n = self._content_length()
            if n:
                self.rfile.read(n)
        except Exception:
            pass

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
        """Emit one SSE event. Raises ClientGone if the peer hung up — cancelling
        a turn (Esc in Codex) is routine, and letting BrokenPipeError escape
        printed a full traceback per cancel and killed the handler thread
        mid-sequence. Spend is logged before streaming starts, so an abort here
        costs no billing."""
        d = dict(d)
        d["type"] = t
        try:
            self.wfile.write(
                ("event: %s\ndata: %s\n\n" % (t, json.dumps(d))).encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise ClientGone(str(e))

    def do_POST(self):
        """Outer guard: once the SSE stream is open, ANY escaping exception left
        the client with response.created and no terminator — socket held open,
        Codex hanging until its own timeout (seen for real via int('end') in a
        model-supplied line range). _do_POST does the work; here we guarantee a
        terminating event no matter what it raises."""
        try:
            return self._do_POST()
        except ClientGone as e:
            _log("client gone (outer) %s" % e)
        except Exception as e:
            _log("UNHANDLED in do_POST: %r" % (e,))
            if getattr(self, "_sse_started", False):
                try:
                    self._ev("response.failed", {"response": {
                        "id": "resp_" + getattr(self, "_rid", "unknown"),
                        "status": "failed",
                        "error": {"message": "bridge internal error: %s" % e}}})
                except Exception:
                    pass
            else:
                try:
                    self._json(500, {"error": "bridge internal error: %s" % e})
                except Exception:
                    pass

    def _do_POST(self):
        self._sse_started = False
        if not self.path.rstrip("/").endswith("/responses"):
            # Drain the body first: returning without reading it desyncs the
            # HTTP/1.1 keep-alive connection, and the leftover bytes get parsed
            # as the NEXT request line (a client probing /v1/chat/completions
            # then had its following real request destroyed).
            self._drain_body()
            return self._json(404, {"error": "only /v1/responses supported"})
        body = self._read_body()
        if body is None:
            return self._json(400, {"error": "bad or oversized Content-Length"})
        try:
            req = json.loads(body or b"{}")
        except Exception as e:
            return self._json(400, {"error": f"bad json: {e}"})
        stream = bool(req.get("stream"))
        # whose key called us — used for SpendLogs attribution (multi-user bridge)
        key_hash = caller_key_hash(self.headers.get("Authorization"))
        _t0 = time.time()
        messages = build_messages(req.get("instructions"), req.get("input"))
        if DEBUG:
            _log("REQ messages:", json.dumps(messages)[:2000])
        else:
            _log("REQ items=%d stream=%s" % (len(req.get("input") or []), stream))
        rid = new_rid()
        self._rid = rid
        # Keep-alive: in stream mode, open the SSE and emit response.created
        # BEFORE the (slow, multi-pod) upstream call, so Codex's ~30s client
        # stream timeout doesn't fire while we race the pool for a tool_call.
        streaming_open = False
        if stream:
            self._sse_open()
            self._sse_started = True
            base = {"id": "resp_" + rid, "object": "response",
                    "created_at": int(time.time()), "status": "in_progress",
                    "model": req.get("model") or UP_MODEL, "output": []}
            try:
                self._ev("response.created", {"response": base})
                self._ev("response.in_progress", {"response": base})
            except ClientGone:
                _log("client gone before upstream call rid=%s" % rid)
                return
            streaming_open = True
        try:
            text, tcs = call_zerokey(messages)
        except Exception as e:
            _log("UPSTREAM ERROR:", repr(e))
            if streaming_open:
                try:
                    self._ev("response.failed", {"response": {
                        "id": "resp_" + rid, "status": "failed",
                        "error": {"message": f"upstream error: {e}"}}})
                    self.wfile.flush()
                except (ClientGone, Exception):
                    pass
                return
            return self._fail(stream, req, f"upstream error: {e}")
        # recover a leaked canvas/textdoc JSON into a real create_file call
        if not tcs:
            hint = " ".join(m.get("content", "") for m in messages
                            if m.get("role") == "user")
            salvaged = salvage_tool_from_text(text, hint)
            if salvaged:
                _log("SALVAGE textdoc -> create_file")
                tcs = [salvaged]
        items = []
        use_exec_custom = _req_uses_exec_tool(req)
        # LOOP BRAKE: the web model doesn't "wrap up" like native Codex — after a
        # command succeeds it tends to re-issue the SAME command forever (e.g.
        # `echo hi` x27). If the command we're about to emit was already run in
        # this thread's history, stop calling tools and return a plain text
        # answer so the agent loop converges. (Native upstream self-terminates;
        # web injection can't, so we enforce it here.)
        if tcs and use_exec_custom:
            prev_cmds = _history_commands(req.get("input"))
            new_cmds = []
            for tc in tcs:
                try:
                    new_cmds.append(tool_to_cmd(tc))
                except UnsafeToolArgs as e:
                    _log("brake precheck: unusable tool args (%s)" % e)
                    new_cmds.append(None)
            if new_cmds and all(c and c in prev_cmds for c in new_cmds):
                # ...unless the user just explicitly asked for a re-run. Brake
                # was firing on "再跑一次一模一样的 echo N1" and answering
                # "Done." without executing anything. A fresh user turn after
                # the last tool result means the repeat was requested, not a
                # runaway loop (a loop has no new user turn between calls).
                if _user_spoke_after_last_tool(req.get("input")):
                    _log("LOOP BRAKE skipped: user asked to re-run %r" % new_cmds[:1])
                else:
                    _log("LOOP BRAKE: repeated cmd %r -> converge to text"
                         % new_cmds[:1])
                    tcs = []
                    if not (text and text.strip()):
                        text = "Done."
        for i, tc in enumerate(tcs):
            try:
                it = make_function_item(tc, rid, i, use_exec_custom)
            except UnsafeToolArgs as e:
                # Refusing one bad tool call must not kill the turn: fall through
                # and let the text answer (or "(no output)") be returned.
                _log("DROP unsafe tool call: %s" % e)
                continue
            if it:
                items.append(it)
        # text only (no actionable tools) -> assistant message
        if not items:
            items.append(make_message_item(rid, text or "(no output)"))
        _log("RESP rid=%s items=%s" % (
            rid, [it.get("name") or it["type"] for it in items]))
        _spendlog(rid, req, messages, items, text, time.time() - _t0, key_hash)
        if streaming_open:
            try:
                return self._stream_body(rid, req, items)
            except ClientGone as e:
                _log("client gone mid-stream rid=%s (%s)" % (rid, e))
                return
        try:
            return self._json(200, self._final_obj(rid, req, items, "completed"))
        except (BrokenPipeError, ConnectionResetError):
            _log("client gone before non-stream reply rid=%s" % rid)
            return

    def _final_obj(self, rid, req, items, status):
        return {
            "id": "resp_" + rid, "object": "response",
            "created_at": int(time.time()), "status": status,
            "model": req.get("model") or UP_MODEL, "output": items,
            "parallel_tool_calls": False, "tool_choice": "auto",
            "tools": req.get("tools") or [],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }

    def _stream_body(self, rid, req, items):
        # SSE already opened and response.created/in_progress already sent by
        # do_POST (keep-alive). Here we only emit the output items + completed.
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
            elif item["type"] == "custom_tool_call":
                # Codex code_mode/IDE: stream the JS input via
                # custom_tool_call_input.delta/done (matches native blueprint).
                added["input"] = ""
                added["status"] = "in_progress"
                self._ev("response.output_item.added",
                         {"output_index": oi, "item": added})
                js = item["input"]
                self._ev("response.custom_tool_call_input.delta",
                         {"item_id": item["id"], "output_index": oi, "delta": js})
                self._ev("response.custom_tool_call_input.done",
                         {"item_id": item["id"], "output_index": oi, "input": js})
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
        # Only reached when the SSE stream was NOT yet opened (non-stream, or
        # error before keep-alive). Streamed failures are handled inline in
        # do_POST via response.failed.
        rid = "err_" + new_rid()
        items = [make_message_item(rid, f"[bridge] {msg}")]
        return self._json(200, self._final_obj(rid, req, items, "completed"))


if __name__ == "__main__":
    host, _, port = LISTEN.partition(":")
    # A restart races the old process's TIME_WAIT socket; without SO_REUSEADDR
    # that surfaced as a bare "Address already in use" traceback that looked
    # like a crash when it was just a too-fast respawn.
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        srv = ThreadingHTTPServer((host, int(port)), H)
    except OSError as e:
        # Still occupied => a live bridge already owns the port. Say so plainly
        # instead of dumping a socket traceback.
        print("[bridge] cannot bind %s: %s\n"
              "         another bridge is probably already running "
              "(check: lsof -nP -iTCP:%s -sTCP:LISTEN)" % (LISTEN, e, port),
              file=sys.stderr, flush=True)
        sys.exit(1)
    _log("zerokey-codex-responses-bridge on %s -> %s (Bearer %s, model %s)" % (
        LISTEN, UPSTREAMS, UP_AUTH, UP_MODEL))
    print("zerokey-codex-responses-bridge on %s -> %s (Bearer %s, model %s)" % (
        LISTEN, UPSTREAMS, UP_AUTH, UP_MODEL), flush=True)
    srv.serve_forever()
