#!/usr/bin/env python3
"""zk_loop.py — zerokey/acct 闭环驱动器：像真 Codex 客户端那样跑完整 agent loop。
发任务 -> 收工具调用 -> 沙箱真执行 -> 回喂 -> 循环。跑完用 VERIFY 命令验真产物。

关键：real_success = 模型说完成 **且** 沙箱里真有能跑通的产物。
"说完成但 0 工具调用 / 产物缺失" = 假完成 = 失败（不再算成功）。

用法:
  POD=zk-115-gpt-5.6-sol VERIFY='python3 test_add.py' python3 zk_loop.py "任务文本"
  POD=chatgpt-acct-86-gpt-5.6-sol TOOLS=1 UA=desktop VERIFY='...' python3 zk_loop.py "任务"
环境变量:
  POD    目标 deployment（决定打 zerokey 还是 acct）
  TOOLS  =1 则注入真实 Codex additional_tools（打 acct 必须，否则它当纯聊天）
  UA     =desktop 用 Codex Desktop UA（acct 路由）；默认 codex-tui（zerokey 路由）
  VERIFY 沙箱内跑的验证命令，exit 0 才算真成功
  SKILLCAT =1 注入 skill 目录（测 skill 层用）
沙箱固定 /tmp/zk_harness/sbx。"""
import json, urllib.request, re, subprocess, os, shutil, sys

KEY = "sk-R0YepgJaLzqm7TbFyJhZGQ"
POD = os.environ.get("POD", "zk-115-gpt-5.6-sol")
URL = "https://cc.auto-link.com.cn/pro/v1/responses" + (f"?model={POD}" if POD else "")
UA_DESKTOP = "Codex Desktop/0.147.0 (Mac OS 26.2.0; arm64) unknown (Codex Desktop; 1)"
UA_CLI = "codex-tui/0.146.1 (Mac OS 26.2.0; arm64) unknown"
UA = UA_DESKTOP if os.environ.get("UA") == "desktop" else UA_CLI
SBX = "/tmp/zk_harness/sbx"
VERIFY = os.environ.get("VERIFY", "")
WANT_TOOLS = os.environ.get("TOOLS") == "1"
MAX_TURNS = 12

# 真实 Codex additional_tools（从抓到的载荷取），打 acct 时必须带
def load_tools_item():
    try:
        p = json.load(open("/tmp/agents_payload.json"))
        for it in p.get("input", []):
            if it.get("type") == "additional_tools":
                return it
    except Exception:
        pass
    return None

def post(inp):
    b = {"model": "gpt-5.6-sol", "instructions": "", "stream": False, "input": inp}
    r = urllib.request.urlopen(urllib.request.Request(URL, data=json.dumps(b).encode(),
        headers={"Authorization": "Bearer "+KEY, "Content-Type": "application/json",
                 "User-Agent": UA}), timeout=180)
    return json.loads(r.read())

def run_cmd(cmd):
    if re.search(r'rm\s+-rf\s+(/|~|\$HOME)', cmd) or ':(){' in cmd:
        return "REFUSED unsafe command"
    try:
        p = subprocess.run(cmd, shell=True, cwd=SBX, capture_output=True, text=True, timeout=30)
        return (p.stdout + p.stderr)[:8000] or f"(exit {p.returncode}, no output)"
    except Exception as e:
        return f"ERROR: {e}"

def apply_patch(payload):
    for hdr, kind in ((r'\*\*\* Add File: (.+)', "add"), (r'\*\*\* Update File: (.+)', "upd")):
        m = re.search(hdr, payload)
        if m:
            path = m.group(1).strip()
            body = "\n".join(l[1:] for l in payload.split("\n")
                             if l.startswith("+") and not l.startswith("+++"))
            full = path if path.startswith(SBX) else os.path.join(SBX, path.lstrip("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            open(full, "w").write(body)
            return f"{kind} {path} ok"
    return "apply_patch: unsupported header"

def exec_tool_input(js):
    out = []
    for m in re.finditer(r'exec_command\(\{cmd:\s*("(?:[^"\\]|\\.)*")\}\)', js):
        cmd = json.loads(m.group(1)); out.append(f"$ {cmd}\n{run_cmd(cmd)}")
    for m in re.finditer(r'apply_patch\(("(?:[^"\\]|\\.)*")\)', js):
        out.append(apply_patch(json.loads(m.group(1))))
    return "\n".join(out) or "(no executable tool call parsed)"

def verify_real():
    """跑 VERIFY 命令，exit 0 = 真成功。没设 VERIFY 则退化成'沙箱非空'。"""
    if not VERIFY:
        return bool(os.path.isdir(SBX) and os.listdir(SBX))
    p = subprocess.run(VERIFY, shell=True, cwd=SBX, capture_output=True, text=True, timeout=30)
    return p.returncode == 0

def run_task(task):
    if os.path.exists(SBX): shutil.rmtree(SBX)
    os.makedirs(SBX)
    inp = []
    if WANT_TOOLS:
        t = load_tools_item()
        if t: inp.append(t)
    inp.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": task}]})
    turns = tool_calls = refusals = empties = asks = 0
    skill_read = False; claimed_done = False
    for turn in range(MAX_TURNS):
        turns += 1
        d = post(inp)
        outs = d.get("output", [])
        tcs = [o for o in outs if o.get("type") == "custom_tool_call"]
        # acct 走原生 function_call(exec/apply_patch)；zerokey 走 custom_tool_call
        native = [o for o in outs if o.get("type") == "function_call"
                  and o.get("name") in ("exec", "apply_patch", "shell", "exec_command")]
        asks_fc = [o for o in outs if o.get("type") == "function_call" and o.get("name") == "request_user_input"]
        txt = "".join(c.get("text","") for o in outs for c in (o.get("content") or []))
        if asks_fc:
            fc = asks_fc[0]
            print(f"  t{turn}: ASK => 答沙箱路径")
            inp.append({"type": "function_call", "call_id": fc.get("call_id","f"),
                        "name": "request_user_input", "arguments": fc.get("arguments","")})
            inp.append({"type": "function_call_output", "call_id": fc.get("call_id","f"),
                        "output": json.dumps({"answers":[{"id":"bpi_ask","value":SBX}]})})
            asks += 1; continue
        if tcs or native:
            tool_calls += 1
            if tcs:
                js = tcs[0].get("input", ""); cid = tcs[0].get("call_id","c")
                if "SKILL.md" in js: skill_read = True
                result = exec_tool_input(js)
                inp.append({"type": "custom_tool_call", "call_id": cid, "name": "exec", "input": js})
                inp.append({"type": "custom_tool_call_output", "call_id": cid,
                            "output": [{"type": "input_text", "text": result}]})
                print(f"  t{turn}: TOOL {js[:55].strip()}... => {result[:45].strip()}")
            else:
                fc = native[0]; cid = fc.get("call_id","c"); args = fc.get("arguments","")
                if "SKILL.md" in args: skill_read = True
                # acct 原生 exec：arguments 是 JSON，取 cmd/input 执行
                result = exec_native(fc.get("name"), args)
                inp.append({"type": "function_call", "call_id": cid, "name": fc.get("name"), "arguments": args})
                inp.append({"type": "function_call_output", "call_id": cid, "output": result})
                print(f"  t{turn}: NATIVE {fc.get('name')} {args[:45].strip()}... => {result[:40].strip()}")
        else:
            low = txt.lower()
            if not txt.strip(): empties += 1; print(f"  t{turn}: EMPTY")
            elif any(k in low for k in ["没有","无法","权限","cannot","can't","don't have","unable"]):
                refusals += 1; print(f"  t{turn}: REFUSE {txt[:50].strip()}")
            else:
                claimed_done = True; print(f"  t{turn}: CLAIM-DONE {txt[:55].strip()}")
                break
    real = verify_real()
    return dict(turns=turns, tool_calls=tool_calls, refusals=refusals, empties=empties,
                asks=asks, skill_read=skill_read, claimed_done=claimed_done, real_success=real)

def exec_native(name, args):
    try:
        a = json.loads(args)
    except Exception:
        a = {}
    if name in ("apply_patch",) or "input" in a and "*** Begin Patch" in str(a.get("input","")):
        return apply_patch(a.get("input",""))
    cmd = a.get("cmd") or a.get("command") or ""
    if isinstance(cmd, list): cmd = " ".join(cmd)
    return run_cmd(cmd) if cmd else "(no cmd in native call)"

if __name__ == "__main__":
    task = sys.argv[1] if len(sys.argv) > 1 else \
        f"在 {SBX} 里创建 add.py（含 add(a,b)）和 test_add.py（测试），运行确认通过"
    r = run_task(task)
    verdict = "真成功" if r["real_success"] else ("假完成" if r["claimed_done"] else "未完成")
    print(f"\n判定: {verdict}  轮次={r['turns']} 工具={r['tool_calls']} 问={r['asks']} "
          f"拒答={r['refusals']} 空={r['empties']} 说完成={r['claimed_done']} 真产物={r['real_success']}")
