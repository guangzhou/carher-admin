#!/usr/bin/env python3
"""A/B the upstream tool NAME, measured end-to-end on real pods.

Why: the pod's responses.js picks one of two injection modes by matching the
tool name (detectShellTool matches shell/terminal/bash/exec):

  * MATCH   -> "exec-harvest": the model is told "use your shell", uses its own
               code interpreter, and the pod HARVESTS the container.exec call
               and returns it as a tool_call.
  * NO MATCH -> "job-queue": the model is told a separate worker will run the
               command, and is expected to emit a function_call itself.

We currently use `enqueue_job` (job-queue) because it measured 83% vs 48% for
`run_in_terminal`. But that old measurement counted only emitted function_calls.
The new evidence is that a chunk of the job-queue failures are the model reaching
for its OWN interpreter anyway and reporting `status 127` -- willing, but with no
harvest safety net to catch it. exec-harvest mode would catch exactly those.

So re-measure both, same task, same n, counting ANY returned tool call.
"""
import json, re, sys, urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

TASK = ("帮我看看这个飞书文档 https://t83dfrspj4.feishu.cn/docx/Cj7OdSqgjoV5Ldxr9dKcKK6BnOa "
        "用 lark-cli 搞定")

GUIDE = (
    "You are a coding agent with a real shell tool that runs on the user's "
    "machine. If the user asks to run a command, read/list files, or execute "
    "anything, you MUST call the tool to actually run it — NEVER invent, guess, "
    "or simulate output. IMPORTANT: never claim you cannot run commands or lack "
    "tool access, and never ask the user to run a command and paste the output "
    "back. The shell has network access plus authenticated CLIs installed. "
    "For `lark-cli`: start with `lark-cli <domain> --help`, and for docs work "
    "read the skill first (`lark-cli skills read lark-doc`); prefer a documented "
    "`+shortcut` such as `lark-cli docs +fetch --doc <URL>`."
)

CANDIDATES = {
    # job-queue mode (current production choice)
    "enqueue_job": "Enqueue a shell command for the worker to run; its output is returned to you next turn.",
    # exec-harvest mode -- name contains "terminal"
    "run_in_terminal": "Run a shell command on the user's machine and return its output.",
    # exec-harvest mode -- name contains "shell"
    "run_shell": "Run a shell command on the user's machine and return its output.",
    # exec-harvest mode, but described with the job-queue framing that measured
    # well. Tests whether NAME (routing) and DESCRIPTION (framing) can be
    # decoupled to get harvest + high compliance at once.
    "shell_enqueue_job": "Enqueue a shell command for the worker to run; its output is returned to you next turn.",
}


def probe(pod, name, desc, timeout=110):
    body = {
        "model": "gpt-5.6-sol",
        "stream": False,
        "input": [{"role": "system", "content": GUIDE},
                  {"role": "user", "content":
                   "Execution environment — OS: macOS 25.2.0; shell: zsh; "
                   "cwd: /Users/Liuguoxian/codes/carher-admin. Use native POSIX commands."},
                  {"role": "user", "content": TASK}],
        "tools": [{"type": "function", "name": name, "description": desc,
                   "parameters": {"type": "object", "properties": {
                       "command": {"type": "string"}}, "required": ["command"]}}],
    }
    req = urllib.request.Request(
        "http://%s.litellm-product.svc.cluster.local:8200/v1/responses" % pod,
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer raw", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        return ("ERR", type(e).__name__)
    text = ""
    for o in d.get("output") or []:
        if o.get("type") in ("function_call", "custom_tool_call"):
            a = o.get("arguments") or o.get("input") or ""
            return ("CALL", str(a)[:80])
        for c in o.get("content") or []:
            if isinstance(c, dict) and c.get("text"):
                text += c["text"]
    # Did the model run it in its OWN sandbox and report failure? That is
    # "willing but misdirected" -- the case harvest is supposed to rescue.
    if re.search(r"status 127|ENOENT|不存在|未能启动|命令没有成功", text[:400]):
        return ("SELFRUN", text[:70].replace("\n", " "))
    if re.search(r"没有可用|没有可调用|不能直接|无法直接|贴给我|你可以在", text[:400]):
        return ("REFUSE", text[:70].replace("\n", " "))
    return ("TEXT", text[:70].replace("\n", " "))


def run(pods, name, desc):
    with ThreadPoolExecutor(max_workers=len(pods)) as ex:
        res = list(ex.map(lambda p: probe(p, name, desc), pods))
    c = Counter(k for k, _ in res)
    n = len(pods)
    print("  %-18s CALL=%-2d SELFRUN=%-2d REFUSE=%-2d TEXT=%-2d ERR=%-2d  -> %d%% call"
          % (name, c["CALL"], c["SELFRUN"], c["REFUSE"], c["TEXT"], c["ERR"],
             round(100.0 * c["CALL"] / max(1, n - c["ERR"]))))
    return c


if __name__ == "__main__":
    pods = sys.argv[1].split(",")
    only = sys.argv[2] if len(sys.argv) > 2 else None
    for nm, desc in CANDIDATES.items():
        if only and nm != only:
            continue
        run(pods, nm, desc)
