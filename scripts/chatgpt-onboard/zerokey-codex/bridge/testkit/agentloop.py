#!/usr/bin/env python3
"""Play Codex's half of the agent loop against the REMOTE bridge.

Why this exists: every previous test of the bridge was single-shot (one request,
look at the reply). The reported failure is multi-TURN -- the model runs one
command and then narrates instead of continuing. That can only be reproduced by
actually closing the loop: send -> receive custom_tool_call -> RUN IT LOCALLY ->
feed the output back as history -> send again.

Commands run on THIS machine (macOS, real lark-cli), which is exactly the
production topology: remote bridge, local execution.
"""
import json, subprocess, sys, time, urllib.request, re, os

BASE = os.environ.get("ZK_BASE", "https://cc.auto-link.com.cn/pro/v1")
KEY = os.environ.get("ZK_KEY", "sk-seGuPVWItZNvNFaLYQ3UrQ")
MODEL = os.environ.get("ZK_MODEL", "gpt-5.6-sol")

# The `exec` custom tool exactly as Codex declares it (code_mode). CRITICAL:
# Codex nests this in an INPUT ITEM of type "additional_tools", not a top-level
# request field. _req_uses_exec_tool() only scans input[], so putting it at top
# level makes the bridge see tools=False -> no GUIDE -> instant refusal. My
# first harness got this wrong and "reproduced" a bug that was my own doing.
ADDITIONAL_TOOLS_ITEM = {
    "type": "additional_tools",
    "tools": [{
        "type": "custom",
        "name": "exec",
        "description": "Runs shell commands via container.exec",
    }],
}


def post(payload, timeout=200):
    req = urllib.request.Request(
        BASE + "/responses",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + KEY,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def extract_cmd(js):
    """Pull the argv out of the JS body the bridge returns."""
    m = re.search(r'"cmd"\s*:\s*(\[[^\]]*\])', js)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    m = re.search(r'"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"', js)
    if m:
        try:
            return json.loads('"' + m.group(1) + '"')
        except Exception:
            return m.group(1)
    return None


def run_local(cmd, timeout=90):
    """Run on this Mac. Use an interactive zsh so PATH/env match the user's
    real shell (a login shell does NOT source .zshrc -- learned that the hard
    way earlier)."""
    if isinstance(cmd, list):
        cmd_s = " ".join(cmd)
    else:
        cmd_s = str(cmd)
    try:
        p = subprocess.run(["zsh", "-ic", cmd_s], capture_output=True,
                           text=True, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        return out.strip()[:6000] or "(no output)"
    except subprocess.TimeoutExpired:
        return "(command timed out)"
    except Exception as e:
        return "(runner error: %s)" % e


def loop(task, max_turns=8, verbose=True, env_ctx=True):
    inp = [ADDITIONAL_TOOLS_ITEM]
    if env_ctx:
        inp.append({"role": "user", "content":
                    "<environment_context>\n  <cwd>/Users/Liuguoxian/codes/carher-admin</cwd>\n"
                    "  <os>macOS 25.2.0</os>\n  <shell>zsh</shell>\n"
                    "  <filesystem><workspace_roots><root>/Users/Liuguoxian/codes/carher-admin</root>"
                    "</workspace_roots></filesystem>\n</environment_context>"})
    inp.append({"role": "user", "content": task})

    trace = []
    import os as _os
    dump = _os.environ.get("ZK_DUMP")
    for turn in range(1, max_turns + 1):
        if dump:
            with open(dump, "w") as fh:
                json.dump(inp, fh, ensure_ascii=False, indent=1)
        payload = {"model": MODEL, "stream": False, "input": inp,
                   "instructions": "You are a coding agent operating on the user's machine."}
        t0 = time.time()
        try:
            d = post(payload)
        except Exception as e:
            body = ""
            try:
                body = e.read().decode()[:300]
            except Exception:
                pass
            trace.append(("ERROR", "%s %s" % (type(e).__name__, body)))
            if verbose:
                print("  T%d ERROR %s %s" % (turn, type(e).__name__, body))
            break
        dt = time.time() - t0

        out = d.get("output") or []
        calls, text = [], ""
        for o in out:
            if o.get("type") == "custom_tool_call":
                calls.append(o)
            elif o.get("type") == "function_call":
                calls.append(o)
            else:
                for c in o.get("content") or []:
                    if isinstance(c, dict) and c.get("text"):
                        text += c["text"]

        if not calls:
            trace.append(("TEXT", text[:400]))
            if verbose:
                print("  T%d %.0fs TEXT: %s" % (turn, dt, text[:150].replace("\n", " ")))
            break

        for o in calls:
            if o.get("type") == "custom_tool_call":
                js = o.get("input") or ""
                cmd = extract_cmd(js)
            else:
                try:
                    a = json.loads(o.get("arguments") or "{}")
                except Exception:
                    a = {}
                cmd = a.get("cmd") or a.get("command")
            cmd_s = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
            res = run_local(cmd)
            trace.append(("CMD", cmd_s))
            if verbose:
                print("  T%d %.0fs CMD: %s" % (turn, dt, cmd_s[:120]))
                print("        -> %s" % res[:100].replace("\n", " "))
            # echo assistant call + user result back into history, exactly as
            # Codex would
            inp.append({"type": o.get("type"),
                        "call_id": o.get("call_id") or o.get("id") or "c%d" % turn,
                        "name": o.get("name") or "exec",
                        **({"input": o.get("input")} if o.get("type") == "custom_tool_call"
                           else {"arguments": o.get("arguments")})})
            inp.append({"type": "custom_tool_call_output" if o.get("type") == "custom_tool_call"
                        else "function_call_output",
                        "call_id": o.get("call_id") or o.get("id") or "c%d" % turn,
                        "output": [{"type": "input_text", "text": res}]})
    return trace


if __name__ == "__main__":
    task = sys.argv[1] if len(sys.argv) > 1 else "read the doc"
    mt = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    tr = loop(task, max_turns=mt)
    ncmd = sum(1 for k, _ in tr if k == "CMD")
    print("SUMMARY commands=%d ended=%s" % (ncmd, tr[-1][0] if tr else "none"))
