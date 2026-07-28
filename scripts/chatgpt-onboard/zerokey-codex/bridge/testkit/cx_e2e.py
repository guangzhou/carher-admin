#!/usr/bin/env python3
"""cx_e2e.py -- codex 侧端到端 harness(真 key / 真 model / 真 base_url)

与 agentloop.py 的区别:agentloop 用 ZK_KEY 直连 bridge;这个用**用户 codex 自己的
key 和 openai_base_url**,即 codex IDE 的真实请求路径,能验证 alias 改写、
LiteLLM 路由、bridge、pod 整条链。

⚠️ 回填工具结果必须原样 echo assistant 的 call item(type/call_id/name 都要),
output 用 [{"type":"input_text","text":...}] 列表形状。
我第一版把 type 硬写成 function_call、重写 arguments、output 用裸字符串,
结果模型认不出"这条命令已经跑过了",把同一条命令重复了 8 轮 —— 那是 harness 的
bug,不是 bridge 的。

用法: python3 testkit/cx_e2e.py "Run: echo hi"
      需要 /tmp/cxkey.txt 存放 codex 的 key(从 ~/.codex/auth.json 取)
"""
import json, os, re, subprocess, sys, time, urllib.request

KEY = open('/tmp/cxkey.txt').read().strip()
URL = "https://cc.auto-link.com.cn/pro/v1/responses"
MODEL = os.environ.get("CX_MODEL", "gpt-5.6-sol")

# codex 真实声明的 exec 工具:嵌在 input[] 的 additional_tools 项里(非顶层字段)
TOOLS_ITEM = {"type": "additional_tools", "tools": [
    {"type": "custom", "name": "exec",
     "description": "Runs shell commands via container.exec"}]}
ENV_CTX = {"role": "user", "content":
           "<environment_context>\n  <cwd>/tmp</cwd>\n  <os>macOS</os>\n"
           "  <shell>zsh</shell>\n</environment_context>"}


def post(inp, timeout=240):
    b = json.dumps({"model": MODEL, "stream": False, "input": inp,
                    "instructions": "You are a coding agent operating on the user's machine."}).encode()
    r = urllib.request.Request(URL, data=b, headers={
        "Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return json.loads(f.read())


def extract(d):
    """返回 (tool_calls, text)。tool_calls = [(call_id, name, cmd_str)]"""
    calls, text = [], ""
    for it in d.get("output") or []:
        t = it.get("type", "")
        if t in ("custom_tool_call", "function_call"):
            raw = it.get("input") or it.get("arguments") or ""
            cmd = raw
            # codex code_mode: JS 包装 await tools.exec_command({cmd:"..."})
            mm = re.search(r'exec_command\(\s*\{.*?["\'](?:cmd|command)["\']\s*:\s*'
                           r'((?:"(?:[^"\\]|\\.)*")|(?:\[[^\]]*\]))', raw, re.S)
            if mm:
                try: v = json.loads(mm.group(1))
                except Exception: v = mm.group(1)
                cmd = " ".join(v) if isinstance(v, list) else v
            else:
                try:
                    j = json.loads(raw)
                    v = j.get("command") or j.get("cmd") or raw
                    cmd = " ".join(v) if isinstance(v, list) else v
                except Exception: pass
            calls.append((it, cmd))
        elif t == "message":
            for c in it.get("content") or []:
                if c.get("type") in ("output_text", "text"):
                    text += c.get("text") or ""
    return calls, text


def sh(cmd):
    try:
        p = subprocess.run(["zsh", "-ic", cmd], capture_output=True, text=True, timeout=90)
        return ((p.stdout or "") + (p.stderr or "")).strip()[:4000] or "(no output)"
    except Exception as e:
        return "(runner error: %s)" % e


def loop(task, max_turns=8, verbose=False):
    inp = [TOOLS_ITEM, ENV_CTX, {"role": "user", "content": task}]
    cmds = []
    for turn in range(max_turns):
        t0 = time.time()
        try:
            d = post(inp)
        except Exception as e:
            return {"ok": False, "why": "http:%s" % str(e)[:60], "cmds": cmds, "text": ""}
        calls, text = extract(d)
        dt = time.time() - t0
        if not calls:
            return {"ok": True, "cmds": cmds, "text": text.strip(), "turns": turn + 1}
        for o, cmd in calls:
            out = sh(cmd)
            cmds.append(cmd)
            if verbose:
                print("    T%d %.0fs CMD %s -> %s"
                      % (turn + 1, dt, cmd[:70], out[:60].replace("\n", " ")))
            # 原样 echo assistant 的 call item,再跟一条对应的 *_output ——
            # 这是 codex 的真实行为。我第一版把 type 硬写成 function_call、
            # 重写 arguments、且 output 用裸字符串,结果模型认不出"这条命令已经跑过了",
            # 于是把同一条命令重复 8 轮。类型/字段/output 形状都必须匹配。
            typ = o.get("type")
            cid = o.get("call_id") or o.get("id") or ("c%d" % turn)
            inp.append({"type": typ, "call_id": cid, "name": o.get("name") or "exec",
                        **({"input": o.get("input")} if typ == "custom_tool_call"
                           else {"arguments": o.get("arguments")})})
            inp.append({"type": "custom_tool_call_output" if typ == "custom_tool_call"
                        else "function_call_output",
                        "call_id": cid,
                        "output": [{"type": "input_text", "text": out}]})
    return {"ok": True, "cmds": cmds, "text": "(max turns)", "turns": max_turns}


if __name__ == "__main__":
    r = loop(sys.argv[1], verbose=True)
    print(json.dumps(r, ensure_ascii=False)[:400])
