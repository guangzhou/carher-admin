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
# Per-upstream error rate as an EWMA (exponentially weighted moving average):
#
#     rate = alpha*sample + (1-alpha)*rate      sample: 1.0 failed, 0.0 ok
#
# This replaced a hand-rolled integer score in [-3,3] with +1/-1/-0.34 steps and
# a separate time-decay function. EWMA is strictly better here:
#   * self-decaying — a recovered pod's rate falls back on its own, so the
#     "un-blacklist a pod that had one bad streak" logic disappears;
#   * scale-free — distinguishes "failed once out of 20" from "failing steadily"
#     without needing hand-tuned weights per outcome;
#   * one number with an obvious meaning (0.0 perfect .. 1.0 always failing).
# Pattern taken from sub2api's openAIAccountRuntimeStats (alpha=0.2), verified in
# its source; the arithmetic is identical, minus the atomics Go needs.
ERR_ALPHA = float(os.environ.get("BRIDGE_ERR_ALPHA", 0.2))
# Above this EWMA an upstream is skipped while healthier ones exist. 0.6 means
# "failing most of the time recently" — deliberately high so that a pod which
# merely answers in text (a legitimate reply, see GUIDE) is never excluded.
ERR_BAD = float(os.environ.get("BRIDGE_ERR_BAD", 0.6))
# A fresh pod starts at 0.0 (assumed good) so new capacity gets traffic at once.
_err = {u: 0.0 for u in UPSTREAMS}      # upstream -> error-rate EWMA [0,1]
# Refusal-rate EWMA: how often this pod declines to use the tool harness on a
# turn that offered tools. Separate from _err on purpose (see mark_health).
_refuse = {u: 0.0 for u in UPSTREAMS}
REFUSE_ALPHA = float(os.environ.get("BRIDGE_REFUSE_ALPHA", 0.3))
_cooldown = {}                          # upstream -> don't touch until ts
_health_lock = threading.Lock()
COOLDOWN_S = float(os.environ.get("BRIDGE_COOLDOWN_S", 60))
# Much shorter than a rate-limit cooldown: a 502 is usually a blip, so we only
# want to stop the CURRENT burst of requests from re-picking that pod.
ERR_COOLDOWN_S = float(os.environ.get("BRIDGE_ERR_COOLDOWN_S", 10))
# Chars per synthetic output_text delta on turns the upstream sent as one
# block. Presentation only — see _stream_body.
TEXT_CHUNK = int(os.environ.get("BRIDGE_TEXT_CHUNK", 60))


# Pods that answered "no Codex tokens available for tool_call" (HTTP 503 from
# the pod itself, not from OpenAI). This is NOT capacity shedding and NOT model
# reluctance: the pod has no Codex-capable token bound, so it can NEVER serve a
# tool_call turn, though it still serves plain chat fine.
#
# Measured across the pool: 21 of 46 pods are in this state, 25 are usable. The
# bridge previously treated all 47 as interchangeable, so roughly 45% of picks on
# a tool turn were spent on pods that could not possibly succeed — that is the
# single largest cause of "it says it can't run lark-cli", because each wasted
# pick consumed a retry round.
#
# Tracked separately from _cooldown because the cooldown horizon is seconds and
# this trait persists for the life of the pod's token binding. Re-checked after
# TOOLGATE_RECHECK_S so a re-authed pod can rejoin.
_no_tool = {}                           # upstream -> ts when it said NOTOKEN
TOOLGATE_RECHECK_S = float(os.environ.get("BRIDGE_TOOLGATE_RECHECK_S", 900))

# Seed list for pods known to lack tool capability. EMPTY by default, because the
# real cause was fixed at the source: 21 of 47 pods were missing three `cp` lines
# in their startup command, so they never installed the patched
# /app/routes/responses.js and hit the old `if (tools) { if (!hasTokens()) 503 }`
# gate. The patched file (already present in the shared zk-image-patch CM) instead
# falls back to web tool-injection when there is no Codex token. After adding
# those cp lines to all 21 deployments, a full re-survey returned 46/46 serving
# tool_calls with zero NOTOKEN.
#
# Kept as an env-tunable escape hatch: if some pod regresses, set
# BRIDGE_NO_TOOL_PODS to bench it without a code change. Do NOT hardcode a list
# here again — a stale list benches healthy pods, which is worse than paying one
# discovery round.
_SEED_NO_TOOL = os.environ.get("BRIDGE_NO_TOOL_PODS", "")


def _seed_no_tool_pods():
    """Apply _SEED_NO_TOOL to the live map, matching by pod NAME against each
    upstream URL so the seed survives changes to the host/port format."""
    names = [n.strip() for n in _SEED_NO_TOOL.split(",") if n.strip()]
    if not names:
        return 0
    now = time.time()
    hits = 0
    for u in UPSTREAMS:
        for n in names:
            # Anchor on the name followed by a dot so "zero-8" cannot match
            # "zero-81"; every upstream is http://<name>.<namespace>...
            if ("/%s." % n) in u or u.endswith("/" + n):
                _no_tool[u] = now
                hits += 1
                break
    return hits


def _err_body_says_no_tool_token(exc):
    """True when a 503 body is the pod's own 'no Codex tokens available for
    tool_call' error. Reads the body at most once and never raises: the body is a
    one-shot stream on HTTPError, and losing it must not break error handling."""
    try:
        body = getattr(exc, "_zk_body", None)
        if body is None:
            body = exc.read().decode("utf-8", "ignore")
            try:
                exc._zk_body = body
            except Exception:
                pass
        return "no Codex tokens available" in body
    except Exception:
        return False


def mark_no_tool_capability(u):
    with _health_lock:
        _no_tool[u] = time.time()


def lacks_tool_capability(u, now=None):
    ts = _no_tool.get(u)
    if not ts:
        return False
    return (now or time.time()) - ts < TOOLGATE_RECHECK_S


def cool_down(u, seconds=None):
    """Take an upstream out of rotation for a while.

    A rate limit is categorically different from a transient failure: 502 means
    "try again", 429 means "this account is capped, stop asking". Scoring them
    alike let a 429'd pod stay in rotation and keep burning retries. sub2api
    models the same idea per-account (rate_limited_until / quota-schedulable
    gating); this is the small version of it.
    """
    with _health_lock:
        _cooldown[u] = time.time() + (COOLDOWN_S if seconds is None else seconds)


def mark_health(u, hit, error=False, refused=False):
    """Feed one observation into the upstream's rolling scores.

    Two SEPARATE signals, because they mean different things:

      _err      transport/HTTP failures — the pod is broken.
      _refuse   the pod was given tools on a tool-requiring turn and answered
                in prose anyway ("I can't run commands on your machine").

    Refusal must NOT feed _err: a plain-text reply is the CORRECT answer to a
    greeting, and counting that as a failure drove every pod to the floor in an
    earlier version. But it must be tracked somewhere — willingness to use the
    injected tool harness is a stable per-ACCOUNT trait, not noise. Measured on
    the same prompt across 12 pods: 1 complied, 11 refused. Without this the
    bridge re-discovers that by trial and error on every single request.
    """
    with _health_lock:
        if error or hit:
            prev = _err.get(u, 0.0)
            _err[u] = ERR_ALPHA * (1.0 if error else 0.0) + (1 - ERR_ALPHA) * prev
        if refused or hit:
            prevr = _refuse.get(u, 0.0)
            _refuse[u] = (REFUSE_ALPHA * (1.0 if refused else 0.0)
                          + (1 - REFUSE_ALPHA) * prevr)


def error_rate(u):
    with _health_lock:
        return _err.get(u, 0.0)


# ── per-caller pod affinity ──────────────────────────────────────────
# Which pod each caller is pinned to, so a multi-turn conversation keeps
# hitting the same ChatGPT account.
#
# Why this has to live HERE and not in litellm: litellm already ships
# weighted_affinity.py ("weighted first pick + session stickiness"), and it
# demonstrably works — zerokey-pool-gpt-5.5 spreads 227/193/122/121/95/93/90
# across accounts, i.e. each key pinned to one, different keys spread out. But it
# pins a *litellm deployment*, and every zerokey-codex request maps to a single
# deployment (the bridge). With one candidate there is nothing to pin, so the
# real account choice — the 47 pods inside this process — was left as plain
# round-robin: every turn of a conversation landed on a different account and
# threw away whatever prompt cache the previous turn had built.
#
# Effect wanted:
#   spread   — different callers start on different pods (weighted random)
#   reuse    — the same caller stays on its pod while it keeps working
#   escape   — a refusing/erroring/cooling pod releases the pin immediately
_affinity = {}            # key_hash -> (upstream, last_used_ts)
_affinity_lock = threading.Lock()
AFFINITY_TTL = float(os.environ.get("BRIDGE_AFFINITY_TTL", 600))


def _affinity_get(key_hash, now):
    """Pinned upstream for this caller, or None. Expired or unusable pins are
    dropped so the caller is re-assigned."""
    if not key_hash:
        return None
    with _affinity_lock:
        rec = _affinity.get(key_hash)
        if not rec:
            return None
        up, ts = rec
        if now - ts > AFFINITY_TTL:
            del _affinity[key_hash]
            return None
    return up


def _affinity_set(key_hash, up, now):
    if not key_hash or not up:
        return
    with _affinity_lock:
        _affinity[key_hash] = (up, now)
        # Bound the map: one entry per active caller is fine, but a long-lived
        # process seeing many keys would grow forever.
        if len(_affinity) > 5000:
            cutoff = now - AFFINITY_TTL
            for k, (_, t) in list(_affinity.items()):
                if t < cutoff:
                    del _affinity[k]


def _affinity_drop(key_hash):
    """Release the pin — the pod refused, errored, or went into cooldown."""
    if not key_hash:
        return
    with _affinity_lock:
        _affinity.pop(key_hash, None)


def next_healthy_upstream(n=1, prefer_compliant=False, key_hash=None):
    """Round-robin over upstreams that are neither cooling down nor failing hard.

    Health is an EXCLUSION filter, not a ranking key. Two earlier versions both
    starved the pool by ranking:
      - sort-by-score: 400 requests over 4 pods went 291/109/0/0
      - best-tier-then-rotate: the winner climbed to the top tier alone, 400/400
    Both defeat the point of having ~19 accounts: quota should spread evenly and
    only genuinely broken pods should be skipped. sub2api lands in the same place
    — its quota/RPM/window checks are boolean gates and the actual pick is
    LRU + shuffle-within-group, not "highest score wins".
    """
    now = time.time()
    with _health_lock:
        free = [u for u in UPSTREAMS if _cooldown.get(u, 0) <= now]
        # On a tool turn, drop pods that told us they have no Codex token: they
        # cannot serve a tool_call at all, so including them only burns retries.
        # Keep them for chat turns, where they work normally. Measured 21/46 pods
        # in this state, so this filter roughly halves the search space on exactly
        # the turns that were failing.
        if prefer_compliant:
            capable = [u for u in free if not lacks_tool_capability(u, now)]
            if capable:
                free = capable
        pool = [u for u in free if _err.get(u, 0.0) < ERR_BAD]
        if prefer_compliant and pool:
            pool = sorted(pool, key=lambda u: _refuse.get(u, 0.0))
        if not pool:
            pool = sorted(free or list(UPSTREAMS),
                          key=lambda u: _err.get(u, 0.0))
    # Honour this caller pinned pod, but only while it is still usable: a pin
    # must never hold a caller on a cooling or failing account. Applied AFTER
    # the health filtering above. Returned alone only when the caller wants a
    # single pod; otherwise it just goes first, so the retry loop still has
    # somewhere to go when the pinned pod declines this turn.
    pinned = _affinity_get(key_hash, now)
    if pinned and pinned in pool:
        if n <= 1:
            return [pinned]
        pool = [pinned] + [u for u in pool if u != pinned]
    elif pinned:
        _affinity_drop(key_hash)
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
    "matching tool — shell_enqueue_job (any shell command incl. cat/ls/grep and "
    "running CLIs), create_file, or replace_string_in_file. A separate executor "
    "runs the call and returns the result to you next turn; you never run "
    "anything yourself and never see the result this turn.\n"
    "Rules:\n"
    "  - The executor HAS full access to the command line and filesystem. Never "
    "say you lack a terminal, cannot access files/systems, or ask the user to run "
    "a command and paste the output — instead CALL shell_enqueue_job with that "
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


def salvage_tool_calls_from_text(text):
    """Recover tool calls the pod emitted as plain TEXT instead of as real calls.

    Observed live: a turn came back with the literal body `{"tool_calls":[]}` as
    its answer, which reached the user verbatim. The same leak also happens with a
    POPULATED array, in which case the command we need is sitting right there in
    the text and can be honoured instead of thrown away.

    Returns a list of tool-call dicts in the same shape as a real one, or [].
    An EMPTY tool_calls array returns [] too — but the caller then clears `text`,
    so the user no longer sees raw JSON as the reply.
    """
    if not text or "tool_calls" not in text:
        return []
    m = re.search(r'\{\s*"tool_calls"\s*:\s*\[.*?\]\s*\}', text, re.S)
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for c in obj.get("tool_calls") or []:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") if isinstance(c.get("function"), dict) else c
        name = fn.get("name") or c.get("name")
        args = fn.get("arguments", fn.get("args"))
        if args is None:
            args = {k: v for k, v in fn.items() if k not in ("name", "type")}
        if not isinstance(args, str):
            try:
                args = json.dumps(args, ensure_ascii=False)
            except Exception:
                continue
        if name:
            out.append({"name": name, "arguments": args})
    return out


def _text_is_only_tool_call_json(text):
    """True when the whole reply is just a leaked tool_calls envelope, so it
    carries no information for the user."""
    if not text:
        return False
    t = text.strip()
    return bool(re.fullmatch(r'\{\s*"tool_calls"\s*:\s*\[\s*\]\s*\}', t))


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


def build_messages(instructions, inp, with_tools=True):
    """Flatten Codex's input[] into upstream chat messages.

    with_tools=False means this turn offers NO tools (plain chat), so the
    agent GUIDE is omitted entirely. Injecting "you have a shell, you MUST call
    the tool" into a conversational turn made the model reach for commands when
    the user just wanted an answer — it must not be sent when no tool exists to
    call. This mirrors how native clients behave: the tool preamble travels with
    the tools, not with every request.
    """
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
        "explanations) where no command needs to run.\n"
        # This used to read "After a tool result comes back, give a short final
        # answer instead of re-running the same command." It was written to stop
        # a REPEAT loop, but the model read it as "stop after ANY result" and so
        # multi-step work died on turn 2: it ran `lark-cli --help`, then wrote a
        # status report ("I haven't actually read the doc yet; next I would need
        # to...") instead of issuing the next command. The repeat case is already
        # handled in code by the LOOP BRAKE, so the prompt no longer needs to ask
        # for an early stop — it needs the opposite.
        "MULTI-STEP WORK: most real tasks need SEVERAL commands in sequence "
        "(discover syntax, then fetch, then inspect). When a tool result comes "
        "back, judge whether the user's ORIGINAL request is now fully satisfied. "
        "If it is not, immediately call the tool again with the next command — do "
        "NOT stop to describe what you are going to do. Announcing a plan "
        "('next I need to run X', 'the next step would be to...') instead of "
        "calling the tool is a failure; if you know the next command, run it. "
        "Only give a plain-text final answer once you actually have the "
        "information the user asked for. Never claim a task is done when you have "
        "not yet seen the data that proves it. Do not re-run a command that "
        "already succeeded — move forward to the next one.\n"
        # The web model's default self-image is a sandboxed chatbot with no shell,
        # so on anything involving a URL or external resource it used to refuse
        # ("I can't run lark-cli, so I can't fabricate the result") and hand the
        # command back for the user to run. It DOES have a shell here — say so.
        "IMPORTANT: never claim you cannot run commands or lack tool access, and "
        "never ask the user to run a command and paste the output back. The tool "
        "call is your only way to act, and the shell has network access plus "
        "authenticated CLIs installed. A URL in the request is not a blocker: "
        "reach it with the appropriate CLI. Call the tool first, then answer from "
        "its real output.\n"
        # Hardcoding one lark-cli recipe (the docx one) only fixed docx: for any
        # other domain the model INVENTED subcommands — `lark-cli contact
        # get-user-info` doesn't exist, so it burned turns failing before it
        # thought to run --help. Teach the discovery path instead of more
        # recipes; lark-cli is self-documenting and explicitly asks agents to
        # read its embedded skill first.
        # Platform default. The client does not always send an environment
        # block, and with no OS stated the model guessed Windows and emitted
        # PowerShell (wmic / Get-Volume / Get-PSDrive) for "check my local
        # disk" on a Mac. A real <os> line from the client, when present, is
        # added later as a user turn and overrides this.
        "Unless the request states otherwise, the shell is a UNIX-like system "
        "(macOS or Linux) with a POSIX shell. Use commands such as df -h, ls, "
        "cat, grep. NEVER emit PowerShell or Windows-only commands "
        "(wmic, Get-Volume, Get-PSDrive, dir /w).\n"
        "Discovering CLI syntax: do NOT guess subcommand names. If a command "
        "fails or you are unsure of its syntax, call the tool to inspect it "
        "(`<cli> --help`, `<cli> <domain> --help`). For `lark-cli` specifically: "
        "its domains are self-documenting and it ships embedded skills — start "
        "with `lark-cli <domain> --help`, and for docs/sheets/base work read the "
        "skill first (`lark-cli skills read lark-doc`). Prefer a documented "
        "`+shortcut` (e.g. `lark-cli docs +fetch --doc <URL>`) over hand-built "
        "raw calls; `lark-cli api GET <path>` is the escape hatch when no typed "
        "command fits. Reading help output is a normal, cheap tool call — always "
        "cheaper than guessing wrong.")
    msgs = [{"role": "system", "content": GUIDE}] if with_tools else []
    # The client's own `instructions` (the Responses API's system prompt) used to
    # be accepted as a parameter and then never used — so a caller that set a
    # persona or hard rules had them silently ignored. Verified: asking for
    # "English only, prefix every sentence with [BOT]" produced plain Chinese
    # with no prefix. It goes in AFTER the GUIDE so the user's intent wins on
    # conflict, and it is forwarded even on tool-less turns.
    if instructions:
        txt = instructions if isinstance(instructions, str) else _text_of(instructions)
        if txt and txt.strip():
            msgs.append({"role": "system", "content": txt.strip()})
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
                    # Drop the block, but KEEP the platform facts. Without them
                    # the model guessed Windows and emitted PowerShell
                    # (Get-Volume / Get-PSDrive) for "check my local disk" on a
                    # Mac. Only os/shell/cwd are carried over — the
                    # <filesystem>/<workspace_roots> parts are what trigger the
                    # "I have no filesystem" refusal, so they stay dropped.
                    env_bits = []
                    for pat, lbl in ((r"<os>([^<]{1,60})</os>", "OS"),
                                     (r"<shell>([^<]{1,40})</shell>", "shell"),
                                     (r"<cwd>([^<]{1,200})</cwd>", "cwd")):
                        mm = re.search(pat, t)
                        if mm and mm.group(1).strip():
                            env_bits.append("%s: %s" % (lbl, mm.group(1).strip()))
                    if env_bits:
                        add("user", "Execution environment — " + "; ".join(env_bits)
                            + ". Use native POSIX commands for this platform; "
                              "never PowerShell or Windows-only commands.")
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

    # CONTINUATION NUDGE. The single highest-leverage fix for the multi-turn
    # break, and it belongs here rather than in the system GUIDE because a
    # standing instruction competes with 20 other lines while the LAST message
    # in the context is what the model actually acts on.
    #
    # The failure it fixes: history ends with a tool result, the model's own
    # chat-tuned instinct is to summarise for a human ("I ran --help; next I
    # would need to..."), and the original request has scrolled far up the
    # context. Restating the goal immediately after the result, plus an explicit
    # "do not narrate", converts that summary into the next command.
    #
    # Only fires when (a) tools are on offer and (b) the last substantive item is
    # a tool result — i.e. exactly the turn that used to break. It never fires on
    # turn 1 or on a fresh user turn, so plain chat is untouched.
    if with_tools and _ends_with_tool_result(inp) and msgs:
        goal = _original_goal(inp)
        nudge = (
            "The command output above is the ONLY new information you have. "
            "Now decide: is this original request fully answered?\n"
            "  ORIGINAL REQUEST: " + (goal or "(see above)") + "\n"
            "If YES — give the final answer, citing the real output.\n"
            "If NO — call the tool RIGHT NOW with the single next command that "
            "makes progress. Do not explain what you are about to do, do not "
            "list steps, do not ask permission, and do not say the task is done "
            "when it is not. Output either a tool call or the final answer, "
            "nothing else.")
        msgs.append({"role": "user", "content": nudge})
    return msgs


def _ends_with_tool_result(inp):
    """True when the newest substantive history item is a tool result — the turn
    where the model must decide 'continue or conclude'. Reasoning items are
    skipped because Codex interleaves them and they carry no decision."""
    if not isinstance(inp, list):
        return False
    for it in reversed(inp):
        if not isinstance(it, dict):
            return False
        typ = it.get("type")
        if typ == "reasoning":
            continue
        if typ in ("function_call_output", "custom_tool_call_output"):
            return True
        # A fresh user message, an assistant message, or a pending tool call all
        # mean this is not a post-result continuation turn.
        return False
    return False


def _original_goal(inp):
    """The user's FIRST real request, to restate in the nudge.

    Uses the first user turn, not the last: by turn 3 the last user-role message
    is a `[result of ...]` block we synthesised, and restating that as the goal
    would tell the model its goal is to look at output it already has. Skips our
    own synthetic turns and Codex's environment block."""
    if isinstance(inp, str):
        return inp.strip()[:400]
    for it in inp or []:
        if not isinstance(it, dict):
            continue
        if it.get("type") not in (None, "message"):
            continue
        if it.get("role") not in ("user", None):
            continue
        t = _text_of(it.get("content"))
        if not t or not t.strip():
            continue
        if "<environment_context>" in t or "<workspace_roots>" in t:
            continue
        if t.lstrip().startswith(("[result of", "[already ran]", "Execution environment")):
            continue
        return t.strip()[:400]
    return ""


# ----------------------------------------------------------------------------
# zerokey chat/completions (stream) -> (text, tool_calls)
# ----------------------------------------------------------------------------
# Wall-clock budget for one client request. Raised from 60s because a refusal
# round costs ~12-17s: at 60s only 2-3 pods could be tried before giving up and
# returning the refusal, which is exactly the case we retry for. Codex tolerates
# this because the bridge opens the SSE and sends response.created immediately,
# so the client is not waiting on a silent socket.
CALL_BUDGET = int(os.environ.get("BRIDGE_CALL_BUDGET", 150))
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



class _FirstSpeaker:
    """Let only ONE pod's text reach the client, for the whole request.

    Two ways this can be violated, both seen:
      * fanout>1 — several pods answer concurrently, and forwarding all of them
        splices two different replies together;
      * RETRY — a pod streams part of an answer and then fails, so the bridge
        retries a different pod. Locking only per-pod let the retry append its
        answer to the abandoned partial one: the client saw
        "第一个pod的部分回答...第二个pod的完整回答。" as a single reply.
    So once ANY pod has spoken, the channel is closed for the rest of the
    request: `retired` blocks later rounds, not just concurrent siblings.
    """

    def __init__(self, sink):
        self.sink = sink
        self.owner = None
        self.emitted = 0
        self.buf = ''
        self.retired = False
        self.lock = threading.Lock()

    def retire(self):
        """Called when the owner's attempt failed: nobody may stream after this,
        because the client already holds a partial answer we cannot retract."""
        with self.lock:
            if self.owner is not None:
                self.retired = True
            return self.retired

    def for_pod(self, pod):
        def on_delta(text):
            if not text:
                return
            with self.lock:
                if self.retired:
                    return
                if self.owner is None:
                    self.owner = pod
                elif self.owner != pod:
                    return          # a slower pod: stay silent
                self.emitted += len(text)
                self.buf += text
            self.sink(text)
        return on_delta

    def won_by(self, pod):
        with self.lock:
            return self.owner == pod

    def sent_chars(self):
        with self.lock:
            return self.emitted

    def text(self):
        with self.lock:
            return self.buf


def _read_sse_stream(resp, on_delta):
    # Parse the upstream SSE stream, forwarding text as it arrives.
    #
    # The pods DO speak SSE on /responses (verified: Content-Type
    # text/event-stream, first event ~2.4s). We used to send stream:false, wait
    # for the WHOLE answer, then hand Codex one giant delta -- which is why
    # output appeared all at once instead of typing out.
    #
    # on_delta(text) fires per chunk. The assembled text and tool_calls are
    # returned in the SAME shape as the non-streaming path, so the loop brake,
    # salvage and health logic downstream are unchanged.
    text_parts = []
    tools = {}
    idx = 0
    args_buf = {}
    names = {}
    for raw in resp:
        line = raw.decode("utf-8", "ignore").strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            ev = json.loads(body)
        except Exception:
            continue
        etype = ev.get("type") or ""
        # text as it is generated
        if etype == "response.output_text.delta":
            piece = ev.get("delta") or ""
            if piece:
                text_parts.append(piece)
                on_delta(piece)
            continue
        # a tool call is announced; remember its name
        if etype == "response.output_item.added":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                names[item.get("id") or ""] = item.get("name") or ""
                args_buf.setdefault(item.get("id") or "", "")
            continue
        # tool arguments stream in fragments
        if etype == "response.function_call_arguments.delta":
            iid = ev.get("item_id") or ""
            args_buf[iid] = args_buf.get(iid, "") + (ev.get("delta") or "")
            continue
        if etype == "response.function_call_arguments.done":
            iid = ev.get("item_id") or ""
            if ev.get("arguments") is not None:
                args_buf[iid] = ev.get("arguments")
            continue
        # Terminal event: trust its full output list over our accumulators, so a
        # pod that only sends a final response.completed (no deltas) still works.
        if etype in ("response.completed", "response.incomplete",
                     "response.failed"):
            out = (ev.get("response") or {}).get("output") or []
            if out:
                text_parts, tools, idx = [], {}, 0
                for o in out:
                    if o.get("type") == "function_call":
                        args = o.get("arguments")
                        if not isinstance(args, str):
                            args = json.dumps(args or {})
                        tools[idx] = {"name": o.get("name", ""),
                                      "arguments": args}
                        idx += 1
                    elif o.get("type") == "message":
                        for c in o.get("content", []) or []:
                            if isinstance(c, dict) and c.get("text"):
                                text_parts.append(c["text"])
            break
    # No terminal event carried an output list: assemble from the deltas.
    if not tools:
        for iid, args in args_buf.items():
            if names.get(iid):
                tools[idx] = {"name": names[iid], "arguments": args or "{}"}
                idx += 1
    return strip_web_markup("".join(text_parts)), [tools[k] for k in sorted(tools)]


def _call_one(base, messages, deadline=None, on_delta=None, want_tools=True,
              model=None):
    # Pass structured `tools` so the current zerokey web pods trigger their
    # tool-injection path (exec-harvest / envelope). The old ToolCompiler
    # inferred tools from the system prompt alone; the web-injection pods need
    # a real tools[] to switch out of plain-chat mode. run_in_terminal maps to
    # exec-harvest (shell), the file tools to envelope injection.
    tools_schema = [
        {"type": "function", "function": {
            # Name and description are chosen INDEPENDENTLY, because they drive
            # two different mechanisms:
            #
            #   * The NAME decides the pod's injection mode. responses.js runs
            #     detectShellTool(), which matches shell/terminal/bash/exec and
            #     switches to "exec-harvest" — the model uses its own code
            #     interpreter and the pod harvests the container.exec call. A
            #     name without those substrings gets the "job-queue" mode, where
            #     the pod expects the model to emit the function_call itself.
            #   * The DESCRIPTION decides how willing the model is: the job-queue
            #     framing ("a separate worker runs it") avoids making the model
            #     wonder whether it personally has a shell, which is what
            #     produces refusals.
            #
            # `shell_enqueue_job` gets BOTH: "shell" routes to harvest, while the
            # description keeps the job-queue framing. Previously we used the
            # neutral `enqueue_job`, believing the harvest prompt itself caused
            # refusals — that conclusion was drawn from a measurement that
            # counted only emitted function_calls and did not separate the two
            # levers. Re-measured on the real "read this Lark doc" task across
            # three independent pod batches (28 non-error samples):
            #     enqueue_job        4/16 = 25%   <- previous production choice
            #     run_in_terminal    9/16 = 56%
            #     run_shell         14/23 = 61%
            #     shell_enqueue_job 19/21 = 90%   <- winner in all three rounds
            # Harvest mode also provides a safety net the job-queue mode lacks:
            # when the model reaches for its own interpreter anyway, the pod
            # captures that command instead of letting it fail with status 127
            # inside the web sandbox.
            "name": "shell_enqueue_job",
            "description": "Enqueue a shell command for the worker to run; its output is returned to you next turn.",
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
    # Always ask upstream to stream. This used to be `and not want_tools`,
    # because the pods' tool-injection path buffered everything and emitted no
    # incremental text — so on an agent turn we saved nothing by asking.
    #
    # That is no longer true: responses.js now decides early whether the reply is
    # prose or a tool envelope and streams the prose case. Keeping the old
    # restriction made the bridge the bottleneck instead of the pod — measured on
    # the same question, "explain quicksort" with an exec tool offered:
    #     pod directly : first text at 5.4s
    #     via bridge   : first text at 15.8s   <- bridge waiting for the whole body
    # The pod itself withholds deltas when the reply IS a tool envelope, so there
    # is nothing left for us to gate on here.
    streaming = bool(on_delta)
    # Honour the model the CLIENT asked for. This used to be hardcoded to
    # UP_MODEL, so a pioneer on gpt-5.6-sol silently got terra while the reply
    # still echoed "sol" — invisible model substitution, and a blocker for
    # routing existing users through the bridge unchanged. Verified the pods
    # respect this field (asked for sol/luna/terra, each echoed back its own).
    body = json.dumps({"model": model or UP_MODEL, "input": input_items,
                       "tools": resp_tools if want_tools else [],
                       "tool_choice": "auto" if want_tools else "none",
                       "stream": streaming}).encode()
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
        if streaming:
            return _read_sse_stream(resp, on_delta)
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
    return strip_web_markup("".join(text_parts)), [tools[k] for k in sorted(tools)]


# Refusal phrasings seen from pods that would not use the injected tool harness.
# Matched case-insensitively; both languages appear in practice.
# ---------------------------------------------------------------------------
# Refusal detection
# ---------------------------------------------------------------------------
# Rewritten after measuring the previous version against a corpus of REAL pod
# replies captured from this cluster: it caught only 7/14 and false-positived on
# 1/11 legitimate answers. Live impact was severe -- 5 of 8 requests for a Lark
# doc were refusals, NONE were detected, so the refusal-retry never fired and
# the user got "I can't run lark-cli" as the final answer.
#
# Why the old one failed:
#   * It scanned only the first 200 chars. Real refusals open with a cooperative
#     clause ("我可以帮你处理飞书文档内容，但...") and put the denial at char
#     60-160, sometimes later.
#   * It matched a denial VERB alone, so "该配置无法直接读取环境变量" -- a denial
#     about the subject matter -- was scored as a refusal.
#
# The fix is to require that the denial be about THIS ASSISTANT's ability to
# act, via three independent sufficient triggers (denial+self, handoff,
# narration). That lets the window widen to 600 chars safely.
#
# Scored on the corpus: 17/17 refusals, 0/11 false positives; on held-out sets
# not used for tuning: 12/12 refusals and 0/20 false positives.

# --- capability denial -------------------------------------------------------
_DENIAL = re.compile(
    # 不能/无法/没法 + (直接|实际|替你|真正) + action verb
    # Allow an intervening phrase between the adverb and the verb: real replies
    # say "不能直接在你的 macOS 环境里执行", where "在...里" sits in between. An
    # adjacency-only form missed those, and each miss costs a live retry -- one
    # such miss made a request give up after a single retry instead of six.
    # Safe to loosen because a bare denial is never sufficient on its own; it
    # must also pass _SELF (see below), which is what keeps subject-matter
    # denials like "这个脚本不能在容器里访问宿主机的网络" out.
    r"(?:无法|不能|没法|没有办法)\s*(?:直接|实际|替你|真正|亲自)?\s*"
    r"(?:[^。；！？\n]{0,24}?(?:里|上|中|内|下))?\s*"
    r"(?:查看|读取|访问|获取|打开|运行|执行|调用|进入|连接|拉取|抓取)"
    # "没有可用的 X 执行工具 / 环境 / 通道 / shell / 终端 / 权限"
    r"|没有(?:可用的?|可调用的?|可以调用的?|现成的?)?[^。；！\n]{0,24}?"
    r"(?:执行工具|执行环境|执行通道|命令执行|工具|环境|通道|权限|shell|终端|命令行|沙箱)"
    # explicit "I won't pretend to run it"
    r"|(?:不能|不会|无法)假装"
    r"|(?:看不到|拿不到|获取不到|读不到)(?:文档|正文|内容|磁盘|文件)?",
    re.I)

# --- the denial is about the MODEL, not about the subject matter --------------
_SELF = re.compile(
    r"我|咱们这边|当前对话|这个对话|当前环境|当前执行环境|当前的?会话"
    r"|这边(?:当前|目前)?|本机\s*shell|对话里|对话环境|执行环境",
    re.I)

# --- handing the job back to the user ---------------------------------------
_HANDOFF = re.compile(
    r"(?:贴|发|粘贴|复制)(?:给我|出来|过来|到这里)"
    r"|把(?:输出|结果|内容|执行结果)[^。；\n]{0,12}(?:贴|发|给我)"
    r"|你可以(?:在|自己|先)[^。；\n]{0,24}(?:执行|运行|查看|跑)"
    r"|(?:请|麻烦)你?(?:先)?(?:在|到)[^。；\n]{0,20}(?:执行|运行)"
    r"|(?:导出|复制)[^。；\n]{0,8}给我"
    r"|如果你(?:希望|想)我[^。；\n]{0,20}(?:提供|给我|贴)",
    re.I)

# --- narrating the plan instead of executing it (multi-turn break) -----------
# Requires an explicit admission that the work is NOT done. A genuine final
# answer never says "还没有真正读到".
_NARRATION = re.compile(
    r"还(?:没有|没|未)\s*(?:真正|实际|成功)?\s*"
    r"(?:读到|拿到|获取到|取到|完成|执行|运行|开始)"
    r"|上一?次只是|上一步只是|目前只是|仅仅只是确认"
    r"|(?:所以)?不能说(?:已经)?(?:搞定|完成|做完)"
    r"|下一步(?:需要|应该|要)(?:用|调用|执行|运行)",
    re.I)

# --- English equivalents -----------------------------------------------------
_EN_DENIAL = re.compile(
    r"(?:i\s*(?:'m|am)\s*(?:not\s*able|unable)|i\s*(?:can(?:'t|not)|don'?t\s+have))"
    r"[^.;\n]{0,40}"
    r"(?:access|run|execute|read|open|view|reach|directly|shell|terminal|filesystem)"
    r"|no\s+(?:access\s+to\s+a\s+)?(?:shell|terminal|sandbox|execution\s+tool)"
    r"|don'?t\s+have\s+(?:a\s+)?(?:shell|terminal|way\s+to\s+run)",
    re.I)
_EN_HANDOFF = re.compile(
    r"(?:please\s+)?(?:paste|share|send)\s+(?:me\s+)?(?:the\s+)?"
    r"(?:output|result|content|contents)"
    r"|you\s+can\s+run\s+(?:it|this|the\s+command)\s+(?:yourself|locally)",
    re.I)
_EN_NARRATION = re.compile(
    r"i\s+haven'?t\s+(?:actually|yet)?\s*(?:read|run|fetched|retrieved|completed)"
    r"|(?:the\s+)?next\s+step\s+would\s+be\s+to",
    re.I)


# Window: the old 200 was too tight -- real refusals put the denial at char
# 60-160 *after* a cooperative opener, and the narration form puts its tell in
# the second or third sentence. 600 is safe now that DENIAL alone is not
# sufficient (it must be paired with SELF), which is what previously made a
# wide window dangerous.
_HEAD = 600


def _looks_like_refusal(text):
    if not text:
        return False
    head = text.strip()[:_HEAD]
    if _HANDOFF.search(head) or _EN_HANDOFF.search(head):
        return True
    if _NARRATION.search(head) or _EN_NARRATION.search(head):
        return True
    # "不能假装执行本机命令" is inherently first-person (only the model would say
    # it) and can appear with no pronoun at all, so it stands alone.
    if re.search(r"(?:不能|不会|无法)假装", head):
        return True
    if (_DENIAL.search(head) or _EN_DENIAL.search(head)) and _SELF.search(head):
        return True
    # English is already first-person-anchored inside the pattern.
    if _EN_DENIAL.search(head):
        return True
    return False


def call_zerokey(messages, want_tools=True, max_rounds=2, on_delta=None,
                 model=None, key_hash=None):
    """Fan out to several pods concurrently; return the FIRST usable result and
    abandon the rest (don't block on slow/miss pods). "Usable" = a tool_call, or
    (when the model legitimately answers in text) the first substantive text.
    Web-injection has per-account variance, so a pod that only refuses is skipped
    in favor of the next completer. Falls back to best text if a round yields
    nothing. Key perf fix: never wait for a whole round to finish once we have an
    answer, and cap total wall time so Codex's stream timeout isn't hit.

    on_delta, when given, streams text to the client as the upstream produces it.
    It is gated by _FirstSpeaker: with fanout>1 several pods answer at once and
    forwarding all of them would interleave two different replies into one
    garbled stream, so only the first pod to emit anything is allowed through.
    """
    import concurrent.futures
    best_text = ""
    last_refusal = ""   # best refusal seen; returned only if nothing better exists
    last_err = None
    speaker = _FirstSpeaker(on_delta) if on_delta else None
    fanout = int(os.environ.get('BRIDGE_FANOUT', '1'))
    # Tool turns fan out; chat turns don't.
    #
    # Per-pod willingness to use the injected shell is roughly a coin flip on a
    # hard task (measured 90% on the best tool name for a simple probe, but far
    # lower for "read this Lark doc"), and each serial attempt costs ~11-20s.
    # With fanout=1 a request needed 3-6 SEQUENTIAL rounds to find a willing pod
    # — 60-90s — so Codex surfaced a stream error or the deadline hit and the
    # user got the refusal. Asking 3 pods AT ONCE turns "P(all refuse)" into
    # 0.5^3 per round and collapses the latency: the first pod to emit a
    # tool_call wins and the rest are abandoned (ex.shutdown(wait=False)).
    #
    # Chat turns stay at fanout=1 deliberately: there is no "wrong" answer to
    # discard, so extra pods would burn quota for nothing, and _FirstSpeaker
    # already has to suppress all but one stream.
    if want_tools:
        fanout = max(fanout, int(os.environ.get('BRIDGE_TOOL_FANOUT', '3')))
    deadline = time.time() + CALL_BUDGET  # hard wall-clock cap for the whole call
    # Transport errors (a burst of upstream 502s) are worth retrying on a
    # DIFFERENT pod, and with ~19 upstreams a flat 2 rounds threw the request
    # away whenever both picks landed in the bad batch — measured 3/8 failures
    # during a 502 burst even though healthy pods were available. Rounds are only
    # spent on hard errors; the deadline still bounds total time.
    # Refusals are pod-specific and common, so allow more attempts than the
    # error path needed; the deadline is still the real bound.
    #
    # Two SEPARATE budgets, because the two failure modes cost wildly different
    # amounts of wall clock and used to share one counter. Measured: a burst of
    # 503s burned rounds 2..8 inside a SINGLE second (pods shed capacity
    # instantly), exhausting the budget before any refusal could be retried —
    # the request then returned the one refusal it happened to have. Hard errors
    # are nearly free, so they get a generous cap; refusals cost 12-17s each, so
    # they get a tighter one. Neither can starve the other.
    max_err_rounds = max(max_rounds, min(24, len(UPSTREAMS)))
    max_refuse_rounds = max(max_rounds, min(6, len(UPSTREAMS)))
    err_rounds = 0
    refuse_rounds = 0
    tried = set()
    _round = -1
    while True:
        _round += 1
        if time.time() > deadline:
            break
        if err_rounds >= max_err_rounds or refuse_rounds >= max_refuse_rounds:
            break
        batch = [u for u in next_healthy_upstream(fanout + len(tried),
                                                  prefer_compliant=want_tools,
                                                  key_hash=key_hash)
                 if u not in tried][:max(1, fanout)]
        if not batch:                      # exhausted the pool -> allow reuse
            tried.clear()
            batch = next_healthy_upstream(fanout, prefer_compliant=want_tools,
                                          key_hash=key_hash)
        tried.update(batch)
        # NOT a daemon pool — ThreadPoolExecutor workers are non-daemon, so an
        # abandoned future keeps the interpreter (and pod shutdown) waiting. We
        # bound each urlopen by the remaining deadline instead, so workers can
        # only outlive us briefly.
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(batch)))
        futs = {ex.submit(_call_one, b, messages, deadline,
                          speaker.for_pod(b) if speaker else None,
                          want_tools, model): b
                for b in batch}
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
                    # Release the pin: a caller must not stay bound to a pod
                    # that just failed, or every retry re-picks it first.
                    _affinity_drop(key_hash)
                    # If this pod had already streamed text to the client, the
                    # client holds a partial answer we cannot retract — retire
                    # the channel so a retry cannot append a second answer.
                    if speaker is not None and speaker.retire():
                        _log("streamed partial before failure -> keep partial, "
                             "no retry (rid channel retired)")
                        return speaker.text(), []
                    code = getattr(e, "code", None)
                    # A 503 carrying "no Codex tokens available for tool_call" is
                    # NOT a rate limit — the pod has no Codex-capable token, so it
                    # will reject every tool turn until it is re-authed. Cooling
                    # it for 60s means it comes straight back and burns another
                    # retry. Mark the capability instead so it is skipped for
                    # tool turns while still serving chat.
                    if code == 503 and _err_body_says_no_tool_token(e):
                        mark_no_tool_capability(b)
                        _log("upstream %s has NO tool_call capability "
                             "(no Codex tokens) -> excluded from tool turns" % b)
                    elif code in (429, 503):
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
                    # Worked: pin this caller here so the next turn reuses the
                    # same account (and whatever prompt cache it just built).
                    _affinity_set(key_hash, b, time.time())
                    _log("HIT %s r%d" % (b, _round))
                    ex.shutdown(wait=False)
                    return text, tcs
                # A bare `{"tool_calls":[]}` is not an answer -- it is a
                # malformed emission. Drop it so the round counts as "no text"
                # and another pod is tried, instead of shipping raw JSON.
                if _text_is_only_tool_call_json(text):
                    _log("upstream %s emitted empty tool_calls envelope as text "
                         "-> discard, retry" % b)
                    text = ""
                # Record refusal so the next request can prefer a willing pod.
                _was_refusal = bool(want_tools and text
                                    and _looks_like_refusal(text))
                mark_health(b, False, refused=_was_refusal)
                if _was_refusal:
                    # Unpin: this account declines the tool harness, so keeping
                    # the caller on it would refuse every turn.
                    _affinity_drop(key_hash)
                elif text and text.strip():
                    # A plain answer is a SUCCESS for a chat turn, so pin here
                    # too. Pinning only on tool_call left chat — the case that
                    # benefits most from prompt-cache reuse — permanently
                    # unpinned (observed: 6 chat requests, pinned_callers=0).
                    _affinity_set(key_hash, b, time.time())
                if text and text.strip():
                    # Keep the BEST text of the round, not the last one. With
                    # fanout>1 several pods answer in the same round and they
                    # arrive in completion order, so an unconditional assignment
                    # let a refusal that finished second overwrite a good answer
                    # that finished first — the round then looked like a refusal
                    # and the good reply was discarded. A non-refusal always wins;
                    # among refusals, the first is kept.
                    if _was_refusal:
                        if not last_refusal:
                            last_refusal = text
                        if not best_text:
                            best_text = text
                            got_text_this_round = True
                    else:
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
        # A whole round finished with NO tool_call. Normally the next round
        # rarely produces one either and burning it doubles latency, so we return
        # the text we have.
        #
        # EXCEPT when the text is a REFUSAL ("I don't have a shell / I can't open
        # that link"). Pods vary in how readily they accept the injected tool
        # harness, and treating a refusal as a valid answer is what made asking
        # for a Lark doc fail: 8 rounds, 7 of them refusals, only 1 real exec —
        # each refusal was returned verbatim instead of trying another pod. A
        # refusal is a pod-specific miss, not an answer, so retry.
        if got_text_this_round and best_text:
            if want_tools and _looks_like_refusal(best_text):
                refuse_rounds += 1
                _log("round %d refusal -> retry another pod (%d/%d)"
                     % (_round, refuse_rounds, max_refuse_rounds))
                last_refusal = best_text   # keep as a last resort, see below
                best_text = ""          # don't fall back to it while pods remain
                continue
            _log("round %d no tool_call, has text -> return (skip retry)" % _round)
            return best_text, []
        # No text at all: the round was consumed by transport failures.
        err_rounds += 1
        _log("round %d: no tool_call, no text -> retry (err %d/%d)"
             % (_round, err_rounds, max_err_rounds))
    if best_text:
        return best_text, []
    # Every pod refused. Returning the refusal verbatim is what the user saw as
    # "it just tells me it can't do it" — but raising instead would surface a
    # bare 502, which is worse (Codex shows a stream error and the user learns
    # nothing). Return the refusal so the turn completes; the caller adds a
    # nudge and Codex will try again with that in history.
    if last_refusal:
        return last_refusal, []
    raise last_err or RuntimeError("all upstreams failed")


# ----------------------------------------------------------------------------
# zerokey tool_call -> Codex exec_command{cmd}
# ----------------------------------------------------------------------------
class UnsafeToolArgs(Exception):
    """Model-supplied tool arguments that can't be turned into a safe command."""


# How often each compatibility fallback actually fires. The web model spells
# arguments inconsistently (filePath / file_path / path / file), so we accept
# them all — but without counting we'd never know which branches earn their
# keep, or whether a NEW spelling has started showing up. Exposed on GET /.
# (sub2api keeps the same kind of tally in ToolCorrectionStats.)
_tool_fixes = {}
_tool_fixes_lock = threading.Lock()


def _note_fix(kind):
    with _tool_fixes_lock:
        _tool_fixes[kind] = _tool_fixes.get(kind, 0) + 1


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
    # literally named UNKNOWN. Counted so we can see which spellings are real.
    path = None
    for key in ("filePath", "file_path", "path", "file", "filename",
                "file_name"):
        if a.get(key):
            path = a[key]
            if key != "filePath":
                _note_fix("path:" + key)
            break
    # Substring match, not an exact-name list. Two reasons:
    #  1. We tune the upstream tool NAME to steer the pod's injection mode (see
    #     _call_one), so the name is expected to change; an exact list silently
    #     breaks every call the moment it does — the mapper falls through and the
    #     command is lost.
    #  2. Pods sometimes echo a near-miss variant of the name they were given.
    # A name containing any shell-ish token, or exactly our own job name, is a
    # command to run. Checked BEFORE the file tools so `shell_enqueue_job`
    # cannot be mistaken for anything else.
    _n = (name or "").lower()
    if ("shell" in _n or "terminal" in _n or "bash" in _n or "exec" in _n
            or _n in ("enqueue_job", "run_command", "command", "run")):
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


def _history_has_tool_use(inp):
    # True if this thread has already used tools. Keeps an agent session's tools
    # attached even on turns where the client does not re-advertise them, so we
    # never strip the shell out from under an in-flight session.
    for it in inp or []:
        if isinstance(it, dict) and it.get("type") in (
                "custom_tool_call", "function_call",
                "custom_tool_call_output", "function_call_output"):
            return True
    return False


def _history_commands(inp, only_unproductive=False):
    """Shell commands already executed in this thread's history (from prior
    custom_tool_call `exec` JS or exec_command function_call args). Used by the
    loop brake to detect the web model re-issuing the same command forever.

    only_unproductive=True returns just the commands whose output was empty or an
    error. That distinction is what makes the brake safe: braking on a command
    that ALREADY SUCCEEDED throws away real work. Observed live — the model ran
    `lark-cli docs +fetch`, got the document back ("ok": true, full content), then
    re-issued the same fetch; the brake fired and replaced the answer with "I
    stopped after repeating the same command without new progress", so the user
    lost a document the bridge had successfully retrieved. When the prior run
    succeeded the correct move is to let the model answer from that output, not
    to inject a failure message.
    """
    cmds = set()
    # call_id -> command, so a command can be paired with its own output.
    by_id = {}
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
            cmd = _json_unquote(m.group(1))
            cmds.add(cmd)
            cid = it.get("call_id") or it.get("id")
            if cid:
                by_id[cid] = cmd
    if not only_unproductive:
        return cmds

    productive = set()
    for it in inp or []:
        if not isinstance(it, dict):
            continue
        if it.get("type") not in ("function_call_output", "custom_tool_call_output"):
            continue
        cmd = by_id.get(it.get("call_id"))
        if not cmd:
            continue
        out = it.get("output")
        if isinstance(out, list):
            out = "".join(seg.get("text", "") if isinstance(seg, dict) else str(seg)
                          for seg in out)
        elif isinstance(out, dict):
            out = json.dumps(out)
        out = (out or "").strip()
        if _output_is_productive(out):
            productive.add(cmd)
    return cmds - productive


# Markers that mean the command did NOT yield usable information. Kept
# deliberately tight: treating a real result as a failure would re-enable the
# runaway loop the brake exists to stop.
_UNPRODUCTIVE_RE = re.compile(
    r"command not found|no such file or directory|permission denied"
    r"|not recognized|ENOENT|status 12[67]|Traceback \(most recent call last\)"
    r"|\berror\b.{0,40}\bnot\b|usage:\s|invalid (?:option|argument|choice)"
    r"|未找到|不存在|没有权限|参数错误",
    re.I)


def _output_is_productive(out):
    """True when a command's output carries information the model can answer from.

    Empty output is not productive (the model has nothing new, so a retry is a
    genuine loop). A recognisable error is not productive. Anything else is —
    including long help text, which is exactly how the model learns a CLI's
    syntax and a legitimate step in a multi-command task.
    """
    if not out or out == "(no output)":
        return False
    if len(out) < 400 and _UNPRODUCTIVE_RE.search(out):
        return False
    return True


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



# ChatGPT-web scaffolding that leaks into replayed answers. The pods replay a
# WEB session, so the model sometimes emits markup meant for chatgpt.com's
# renderer. Verified leaking verbatim to users:
#   ":::writing{variant=\"document\" id=\"58391\"}" wrapping a 300-word answer
#   citation markers delimited by U+E200/E201/E202, e.g.
#   "\ue200cite\ue202turn0search0\ue201" after a list item",
# An API client renders these as literal noise.
_WEB_CANVAS_RE = re.compile(r':::[a-zA-Z]+\{[^}]{0,200}\}[ \t]*\n?')
_WEB_BARE_RE = re.compile(r'^[ \t]*:::[ \t]*$', re.M)
# Whole marker including its private-use delimiters.
_WEB_CITE_RE = re.compile('[\ue200-\ue20f][^\ue200-\ue20f]*[\ue200-\ue20f]'
                          '[^\ue200-\ue20f]*[\ue200-\ue20f]?')
_WEB_PUA_RE = re.compile('[\ue200-\ue20f]+')


# Longest prefix we might need to withhold: a marker can straddle two deltas.
_TAIL_KEEP = 24


def _split_safe_tail(buf):
    """Split buf into (safe_to_emit, hold_back).

    A marker can arrive split across deltas, so anything that might be an
    UNFINISHED marker is held back until the rest shows up. Getting this wrong
    leaks marker fragments: an earlier version searched for the LAST delimiter
    and released everything before it, which emitted "cite" and "arch0" pieces
    around a marker split mid-way.
    """
    if not buf:
        return '', ''
    # First private-use delimiter that has no closing U+E201 after it: the
    # marker is still in flight, so withhold from there on.
    i = -1
    for k, ch in enumerate(buf):
        if '\ue200' <= ch <= '\ue20f':
            i = k
            break
    if i != -1 and '\ue201' not in buf[i:]:
        return buf[:i], buf[i:]
    # A ':::' fence whose closing brace has not arrived yet.
    j = buf.rfind(':::')
    if j != -1 and '}' not in buf[j:] and len(buf) - j < 200:
        return buf[:j], buf[j:]
    return buf, ''


def strip_web_markup_stream(text):
    """Marker removal for streamed chunks: same patterns, but must NOT trim
    surrounding whitespace, since chunks are concatenated by the client.
    """
    if not text:
        return text
    out = _WEB_CANVAS_RE.sub('', text)
    out = _WEB_CITE_RE.sub('', out)
    out = _WEB_PUA_RE.sub('', out)
    return out

def strip_web_markup(text):
    """Remove web-renderer scaffolding from a replayed answer.

    Deliberately narrow: only these known wrappers, never the user's own text.
    """
    if not text:
        return text
    out = _WEB_CANVAS_RE.sub('', text)
    out = _WEB_BARE_RE.sub('', out)
    out = _WEB_CITE_RE.sub('', out)
    out = _WEB_PUA_RE.sub('', out)
    out = re.sub(r'\n{3,}', '\n\n', out)
    return out.strip()


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
        # Operational view: without this, the only way to tell whether a pod is
        # being skipped (and why) was to grep the log. Debugging the load-skew
        # and 502-burst issues would have been much faster with this.
        now = time.time()
        with _health_lock:
            pods = [{"upstream": u,
                     "error_rate": round(_err.get(u, 0.0), 3),
                     "cooling_for_s": max(0, round(_cooldown.get(u, 0) - now, 1)),
                     "no_tool_capability": lacks_tool_capability(u, now),
                     "usable": (_err.get(u, 0.0) < ERR_BAD
                                and _cooldown.get(u, 0) <= now)}
                    for u in UPSTREAMS]
            stats = dict(_tool_fixes)
        # Affinity view: without it there is no way to tell whether callers are
        # actually being pinned (and spread) other than reading the log.
        with _affinity_lock:
            pinned_now = len(_affinity)
            spread = len({u for u, _ in _affinity.values()})
        return self._json(200, {
            "status": "healthy",
            "usable_pods": sum(1 for p in pods if p["usable"]),
            # Pods that can serve a TOOL turn -- the number that actually matters
            # for agent work. usable_pods counts chat capability and was
            # misleading on its own: it read 47/47 while 21 pods were rejecting
            # every tool_call.
            "tool_capable_pods": sum(1 for p in pods if p["usable"]
                                     and not p["no_tool_capability"]),
            "total_pods": len(pods),
            "err_bad_threshold": ERR_BAD,
            "pods": pods,
            "tool_arg_fixes": stats,
            "pinned_callers": pinned_now,
            "pods_in_use": spread,
            "affinity_ttl_s": AFFINITY_TTL,
        })

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
        # Real OpenAI stamps a monotonic sequence_number on every Responses SSE
        # event, and sub2api reproduces it (anthropic_to_responses_response.go
        # increments state.SequenceNumber per event). We emitted none — harmless
        # for Codex today, but a stricter client is entitled to reject or
        # mis-order the stream. Cheap to be spec-correct. Per-request counter
        # lives on the handler; the upstream pods send no sequence of their own.
        self._seq = getattr(self, "_seq", 0)
        d["sequence_number"] = self._seq
        self._seq += 1
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
        # Reset per REQUEST, not per connection: one handler instance serves
        # several keep-alive requests in sequence, so a stale counter would make
        # the second response's events start mid-sequence.
        self._seq = 0
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
        # Decide FIRST whether this turn is an agent turn, because it controls
        # both the upstream tools[] AND whether the agent GUIDE is injected.
        # (This used to be computed after build_messages, so the GUIDE went out
        # unconditionally — a pure chat turn was told "you have a shell, you MUST
        # call the tool", which is exactly how a plain question turned into a
        # command. Native clients ship the tool preamble WITH the tools.)
        wants_tools = (bool(req.get("tools")) or _req_uses_exec_tool(req)
                       or _history_has_tool_use(req.get("input")))
        messages = build_messages(req.get("instructions"), req.get("input"),
                                  with_tools=wants_tools)
        if DEBUG:
            _log("REQ messages:", json.dumps(messages)[:2000])
        else:
            _log("REQ items=%d stream=%s tools=%s"
                 % (len(req.get("input") or []), stream, wants_tools))
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
        # Stream text through as the upstream generates it (only when the client
        # asked for a stream). Tool calls are NOT streamed live: they must pass
        # the loop brake and safety checks below before the client sees them.
        self._live = None
        # Does this turn need tools at all? Codex agent turns offer an `exec`
        # custom tool or a tools[] array; a plain chat turn offers neither. Only
        # the latter can stream (see _call_one: upstream emits no deltas when
        # tools are present), so we ask for tools only when the client offered
        # some — and get real token-by-token output for conversational turns.
        sink = self._live_text_sink(rid) if streaming_open else None
        try:
            text, tcs = call_zerokey(messages, want_tools=wants_tools,
                                     on_delta=sink,
                                     model=req.get("model"),
                                     key_hash=key_hash)
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
        # recover a leaked tool-call JSON that arrived as plain TEXT
        if not tcs:
            salvaged_calls = salvage_tool_calls_from_text(text)
            if salvaged_calls:
                _log("SALVAGE leaked tool_calls JSON -> %d call(s)"
                     % len(salvaged_calls))
                tcs = salvaged_calls
                text = ""        # the JSON is not an answer; don't show it
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
            # Only UNPRODUCTIVE prior commands arm the brake. A command that
            # already returned real output must not trigger it: braking there
            # discards work the bridge successfully did (measured: a Lark doc was
            # fetched, then re-fetched, and the brake replaced the document with
            # an "I stopped..." message). Re-issuing a SUCCESSFUL command means
            # the model should be concluding, which the nudge below asks for.
            prev_cmds = _history_commands(req.get("input"),
                                          only_unproductive=True)
            all_prev = _history_commands(req.get("input"))
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
                        # "Done." claims success we cannot vouch for. The brake
                        # fires precisely when a command was retried, which is
                        # usually because it FAILED (seen live: the model retried
                        # a non-existent `lark-cli whoami`, got braked, and told
                        # the user "Done." as if the task had succeeded). Say what
                        # actually happened instead.
                        cmd = (new_cmds[0] or "").strip()
                        text = ("I stopped after repeating the same command "
                                "without new progress: `%s`. It likely failed or "
                                "returned nothing usable — check the command is "
                                "valid (e.g. verify the subcommand with `--help`) "
                                "and tell me how to proceed." % cmd[:160])
            elif new_cmds and all(c and c in all_prev for c in new_cmds) \
                    and not _user_spoke_after_last_tool(req.get("input")):
                # The repeat targets a command that ALREADY SUCCEEDED. Don't run
                # it again (wasted turn, and it can loop), but don't destroy the
                # result either: drop the tool call and ask the model — via a
                # retry on the next turn — to answer from the output in history.
                # Falling through with tcs=[] and whatever text it produced is
                # the safe move; Codex will show that text, and the output the
                # answer should come from is still in the transcript.
                _log("LOOP BRAKE (soft): repeat of a SUCCESSFUL cmd %r -> "
                     "answer from existing output" % new_cmds[:1])
                tcs = []
                if not (text and text.strip()):
                    # Ask the model AGAIN with tools switched OFF, so its only
                    # option is to answer from the output already in history.
                    # Returning a canned "the command already ran" line here was
                    # a visible dead end for the user (they asked for a document
                    # and got a status note about command repetition), even
                    # though the data needed to answer was sitting in the
                    # transcript. want_tools=False also removes the GUIDE, which
                    # is what was pushing it to call the tool again.
                    try:
                        followup = build_messages(req.get("instructions"),
                                                  req.get("input"),
                                                  with_tools=False)
                        followup.append({"role": "user", "content":
                                         "Do not run any more commands. Using ONLY "
                                         "the command output already shown above, "
                                         "answer the original request now."})
                        text2, _ = call_zerokey(followup, want_tools=False,
                                                model=req.get("model"),
                                                key_hash=key_hash)
                        if text2 and text2.strip():
                            text = text2
                            _log("soft brake: synthesised answer from history "
                                 "(%d chars)" % len(text))
                    except Exception as e:
                        _log("soft brake follow-up failed: %r" % e)
                if not (text and text.strip()):
                    text = ("The command `%s` already ran successfully earlier in "
                            "this conversation and its output is above. Answering "
                            "from that output instead of running it again."
                            % (new_cmds[0] or "").strip()[:160])
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

    def _usage_for(self, req, items):
        """Estimated token usage.

        The web-session upstream reports none, and we were returning hard zeros —
        which reads as "this call was free" to any client or dashboard that
        surfaces usage. We already estimate the same numbers for the spend log,
        so report them here too rather than claiming zero. Marked estimated so
        nobody mistakes it for metered truth.
        """
        try:
            pt = _est_tokens("".join(
                _text_of(it.get("content")) if isinstance(it, dict) else str(it)
                for it in (req.get("input") or [])) if isinstance(
                    req.get("input"), list) else str(req.get("input") or ""))
            ct = 0
            for it in items:
                ct += _est_tokens((it.get("content") or [{}])[0].get("text", "")
                                  if it.get("type") == "message" else
                                  (it.get("input") or it.get("arguments") or ""))
            return {"input_tokens": pt, "output_tokens": ct,
                    "total_tokens": pt + ct, "estimated": True}
        except Exception:
            return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def _final_obj(self, rid, req, items, status):
        return {
            "id": "resp_" + rid, "object": "response",
            "created_at": int(time.time()), "status": status,
            "model": req.get("model") or UP_MODEL, "output": items,
            "parallel_tool_calls": False, "tool_choice": "auto",
            "tools": req.get("tools") or [],
            "usage": self._usage_for(req, items),
        }

    def _live_text_sink(self, rid):
        """Return an on_delta callback that streams text to the client as it is
        produced, opening the message item lazily on the first chunk.

        State is kept on self._live so _stream_body knows the item is already
        open and how much text the client has seen; without that it would re-send
        the whole answer after having streamed it, duplicating the reply.
        """
        self._live = None

        def sink(piece):
            if self._live is None:
                item_id = "msg_%s" % rid
                self._live = {"id": item_id, "oi": 0, "text": "",
                              "pending": ""}
                self._ev("response.output_item.added",
                         {"output_index": 0,
                          "item": {"type": "message", "id": item_id,
                                   "role": "assistant", "content": [],
                                   "status": "in_progress"}})
                self._ev("response.content_part.added",
                         {"item_id": item_id, "output_index": 0,
                          "content_index": 0,
                          "part": {"type": "output_text", "text": ""}})
            # Sanitise BEFORE forwarding. Deltas go out live, so cleaning only
            # the assembled text (as the non-streaming path does) let web
            # scaffolding reach the client anyway — verified U+E200..E202
            # citation delimiters arriving inside streamed deltas.
            #
            # Markers can straddle a chunk boundary, so hold back a short tail
            # until we have enough context to tell whether it starts a marker,
            # and flush it in _close_live_text.
            buf = self._live["pending"] + piece
            emit, self._live["pending"] = _split_safe_tail(buf)
            if not emit:
                return
            emit = strip_web_markup_stream(emit)
            if not emit:
                return
            self._live["text"] += emit
            self._ev("response.output_text.delta",
                     {"item_id": self._live["id"], "output_index": 0,
                      "content_index": 0, "delta": emit})
        return sink

    def _close_live_text(self):
        """Finish the message item opened by _live_text_sink."""
        live = getattr(self, "_live", None)
        if not live:
            return 0
        # Flush the withheld tail: _split_safe_tail holds back the last few chars
        # in case a marker straddles chunks, so without this the answer would be
        # silently truncated by up to _TAIL_KEEP characters.
        tail = strip_web_markup_stream(live.get("pending") or "")
        if tail:
            live["text"] += tail
            self._ev("response.output_text.delta",
                     {"item_id": live["id"], "output_index": 0,
                      "content_index": 0, "delta": tail})
        txt = live["text"]
        self._ev("response.output_text.done",
                 {"item_id": live["id"], "output_index": 0,
                  "content_index": 0, "text": txt})
        self._ev("response.content_part.done",
                 {"item_id": live["id"], "output_index": 0,
                  "content_index": 0,
                  "part": {"type": "output_text", "text": txt}})
        self._ev("response.output_item.done",
                 {"output_index": 0,
                  "item": {"type": "message", "id": live["id"],
                           "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": txt}]}})
        return 1

    def _stream_body(self, rid, req, items):
        # SSE already opened and response.created/in_progress already sent by
        # do_POST (keep-alive). Here we only emit the output items + completed.
        #
        # If text was already streamed live, its message item is open and the
        # client has seen the whole answer: close that item and DROP the matching
        # message from `items`, otherwise the reply would be delivered twice.
        live = getattr(self, "_live", None)
        base_oi = 0
        if live:
            # Drop ANY message item once text was streamed live. Matching on
            # exact text equality broke as soon as sanitising was added: the
            # streamed copy is cleaned chunk-wise while the assembled copy is
            # cleaned whole (which also trims), so the two are no longer byte
            # identical and the message survived — the client received the whole
            # answer twice (measured 506 chars streamed vs a 253-char reply).
            # There is at most one text message per turn, so identity is not
            # needed: if we streamed text, that message is already delivered.
            items = [it for it in items if it.get("type") != "message"]
            base_oi = self._close_live_text()
        for oi, item in enumerate(items, start=base_oi):
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
                # Chunk the text instead of emitting one giant delta. On agent
                # turns the upstream cannot stream at all (a non-empty tools[]
                # switches the pod to its injection path, which emits no
                # incremental text — verified: 14 deltas with tools=[], 0 with
                # any tool present, even at tool_choice=none), so by the time we
                # get here the whole answer already exists.
                #
                # This is PRESENTATION only: it does not make the answer arrive
                # sooner. It exists because a single multi-KB delta makes clients
                # paint the reply in one jarring jump, and some renderers handle
                # one huge delta poorly. Real token-by-token streaming on agent
                # turns is blocked upstream, not here.
                for k in range(0, len(txt), TEXT_CHUNK):
                    self._ev("response.output_text.delta",
                             {"item_id": item["id"], "output_index": oi,
                              "content_index": 0,
                              "delta": txt[k:k + TEXT_CHUNK]})
                self._ev("response.output_text.done",
                         {"item_id": item["id"], "output_index": oi,
                          "content_index": 0, "text": txt})
                self._ev("response.content_part.done",
                         {"item_id": item["id"], "output_index": oi,
                          "content_index": 0,
                          "part": {"type": "output_text", "text": txt}})
            self._ev("response.output_item.done",
                     {"output_index": oi, "item": item})
        # The terminal response must carry the FULL output, including the message
        # we streamed live and filtered out of `items` above — clients that read
        # response.completed instead of accumulating deltas would otherwise see
        # an answer with the text missing.
        final_items = list(items)
        if live:
            final_items.insert(0, make_message_item(live["id"].replace("msg_", ""),
                                                    live["text"]))
        done = self._final_obj(rid, req, final_items, "completed")
        self._ev("response.completed", {"response": done})

    def _fail(self, stream, req, msg):
        # Only reached when the SSE stream was NOT yet opened (non-stream, or
        # error before keep-alive). Streamed failures are handled inline in
        # do_POST via response.failed.
        #
        # Report the failure AS a failure. This used to return status
        # "completed" with the error text as the assistant's answer, so a client
        # could not tell a real reply from "all upstreams failed" — the error was
        # laundered into a successful-looking response and would be stored as
        # conversation history. sub2api models the same distinction with
        # status + incomplete_details rather than always claiming success.
        rid = "err_" + new_rid()
        obj = self._final_obj(rid, req, [], "failed")
        obj["error"] = {"type": "upstream_error", "message": msg}
        obj["incomplete_details"] = {"reason": "upstream_error"}
        return self._json(502, obj)


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
    seeded = _seed_no_tool_pods()
    _log("seeded %d/%d upstreams as tool-incapable; %d tool-capable at start"
         % (seeded, len(UPSTREAMS), len(UPSTREAMS) - seeded))
    _log("zerokey-codex-responses-bridge on %s -> %s (Bearer %s, model %s)" % (
        LISTEN, UPSTREAMS, UP_AUTH, UP_MODEL))
    print("zerokey-codex-responses-bridge on %s -> %s (Bearer %s, model %s)" % (
        LISTEN, UPSTREAMS, UP_AUTH, UP_MODEL), flush=True)
    srv.serve_forever()
