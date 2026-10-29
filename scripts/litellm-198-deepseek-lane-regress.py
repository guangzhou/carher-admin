#!/usr/bin/env python3
"""litellm-198-deepseek-lane-regress.py — 换 deepseek callback 前后，对一条车道跑 10 条回归。只发请求不改配置。

为什么有这个脚本（2026-10-25）
==============================
10-22 deepseek-v4.1-flash 报「Input items array must not be empty」：Codex ``view_image`` 把截图
base64 塞进 function_call_output（单张几 MB、token 才 ~1k），``deepseek_responses_adapt`` 按字节判
超 3MB 把整单删成 ``[]``。修完要证明两件事：截图那条好了、别的没坏。这里固化那轮的 10 条：

  截图 0.5MB（本来就好）/ 截图 3.5MB（复现那条）/ 4 张 9MB / 3.5MB 流式
  deepseek 普通文本 / 发起工具调用 / 并行工具多轮 / chat 路径
  对照：命令行追加的非 deepseek 模型（默认 gpt-5.5 sa-grok-4.7）

🔴 判据：deepseek 看完图**常常继续调工具**（再跑命令看像素）而不是直接回答 —— 「回答为空」
不等于失败，「有文字或有工具调用」都算过。10-25 第一版只认文字，新代码被判 6/10、旧代码 5/10，
两边都红 ⇒ 是尺子错了（对照组也红先疑量具）。

用法（198 上跑，用 master key；gray=30405 prod=30402 guarded-old=30406）
====
    KEY=$(sudo -n kubectl -n litellm-product get secret litellm-secrets-r2 \
          -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
    python3 litellm-198-deepseek-lane-regress.py http://127.0.0.1:30405 "$KEY" gpt-5.5 sa-grok-4.7

上线顺序：先 gray 跑（新）+ prod 跑（旧）并排 → 只有截图 3 条从 FAIL 变 PASS、其余逐条同结果 →
再上 prod 复跑必须 10/10。配合 ``scripts/litellm-198-callback-update.py``（skill
codex-deepseek-tool-triage「改完怎么上线」）。
"""
import base64, json, random, struct, sys, time, urllib.request, urllib.error, zlib
URL, KEY = sys.argv[1], sys.argv[2]

def png(w, h, seed=1):
    rnd = random.Random(seed)
    raw = b"".join(b"\x00" + bytes(rnd.getrandbits(8) for _ in range(w * 3)) for _ in range(h))
    ck = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + ck(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ck(b"IDAT", zlib.compress(raw, 1)) + ck(b"IEND", b"")

U = lambda t: {"type": "message", "role": "user", "content": [{"type": "input_text", "text": t}]}
R = {"type": "reasoning", "summary": [], "content": [{"type": "reasoning_text", "text": "想一下。"}]}
TOOLS = [{"type": "function", "name": "view_image", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}},
         {"type": "function", "name": "exec_command", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}]

def shot(w, h, n=1):
    items = [U("看一下截图，一句话说它是什么颜色风格。"), R]
    items += [{"type": "function_call", "call_id": f"v{k}", "name": "view_image", "arguments": json.dumps({"path": f"s{k}.png", "detail": "original"})} for k in range(n)]
    items += [{"type": "function_call_output", "call_id": f"v{k}", "output": [{"type": "input_image",
               "image_url": "data:image/png;base64," + base64.b64encode(png(w, h, k)).decode(), "detail": "original"}]} for k in range(n)]
    return {"model": "deepseek-v4.1-flash", "stream": False, "store": False, "input": items, "tools": TOOLS}

def post(path, body, stream=False):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    t = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=300)
        raw = r.read().decode("utf-8", "replace")
        return 200, raw, time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200], time.time() - t

def resp_text(raw):
    j = json.loads(raw)
    calls = [o.get("name") for o in j.get("output", []) if o.get("type") in ("function_call", "custom_tool_call")]
    txt = "".join(c.get("text", "") for o in j.get("output", []) if o.get("type") == "message" for c in o.get("content", []))
    return j.get("model"), txt.strip(), calls

cases = []
def case(name, fn):
    try:
        ok, msg = fn()
    except Exception as e:
        ok, msg = False, f"EXC {type(e).__name__}: {str(e)[:150]}"
    cases.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name:28s} {msg}", flush=True)

def c_shot(w, h, n):
    def f():
        st, raw, dt = post("/v1/responses", shot(w, h, n))
        if st != 200: return False, f"{st} {raw}"
        m, txt, calls = resp_text(raw)
        # 模型看完图继续调工具（再跑命令看图）也是正常的 agent 行为
        return bool(txt or calls), f"200 {dt:.1f}s model={m} 答={txt[:30]!r} 工具={calls}"
    return f

def c_text_small():
    b = {"model": "deepseek-v4.1-flash", "stream": False, "store": False, "input": [U("1+1 等于几？只回数字。")]}
    st, raw, dt = post("/v1/responses", b)
    if st != 200: return False, f"{st} {raw}"
    m, txt, _ = resp_text(raw); return "2" in txt, f"200 {dt:.1f}s 答={txt[:20]!r}"

def c_tool_call():
    b = {"model": "deepseek-v4.1-flash", "stream": False, "store": False, "tools": TOOLS, "tool_choice": "auto",
         "input": [U("用 exec_command 运行 `ls` 看当前目录，必须调用工具。")]}
    st, raw, dt = post("/v1/responses", b)
    if st != 200: return False, f"{st} {raw}"
    m, txt, calls = resp_text(raw); return "exec_command" in calls, f"200 {dt:.1f}s 工具调用={calls}"

def c_tool_multiturn():
    b = {"model": "deepseek-v4.1-flash", "stream": False, "store": False, "tools": TOOLS,
         "input": [U("运行 ls 看看有什么文件，然后告诉我文件个数。"), R,
                   {"type": "function_call", "call_id": "e1", "name": "exec_command", "arguments": json.dumps({"cmd": "ls"})},
                   {"type": "function_call", "call_id": "e2", "name": "exec_command", "arguments": json.dumps({"cmd": "pwd"})},
                   {"type": "function_call_output", "call_id": "e1", "output": "a.txt\nb.txt\nc.txt\n"},
                   {"type": "function_call_output", "call_id": "e2", "output": "/work\n"}]}
    st, raw, dt = post("/v1/responses", b)
    if st != 200: return False, f"{st} {raw}"
    m, txt, calls = resp_text(raw); return bool(txt or calls), f"200 {dt:.1f}s 答={txt[:30]!r} 工具={calls}"

def c_stream_shot():
    b = shot(1100, 1000, 1); b["stream"] = True
    st, raw, dt = post("/v1/responses", b)
    if st != 200: return False, f"{st} {raw}"
    done = "response.completed" in raw; deltas = raw.count("output_text.delta"); fc = raw.count('"type":"function_call"') + raw.count('"type": "function_call"')
    return done and (deltas > 0 or fc > 0), f"200 {dt:.1f}s completed={done} text_delta={deltas} function_call={fc}"

def c_chat():
    b = {"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "1+1 等于几？只回数字。"}]}
    st, raw, dt = post("/v1/chat/completions", b)
    if st != 200: return False, f"{st} {raw}"
    txt = json.loads(raw)["choices"][0]["message"]["content"]; return "2" in txt, f"200 {dt:.1f}s 答={txt.strip()[:20]!r}"

def c_other(model):
    def f():
        b = {"model": model, "stream": False, "store": False, "input": [U("1+1 等于几？只回数字。")]}
        st, raw, dt = post("/v1/responses", b)
        if st != 200: return False, f"{st} {raw}"
        m, txt, _ = resp_text(raw); return "2" in txt, f"200 {dt:.1f}s model={m} 答={txt[:20]!r}"
    return f

case("截图 0.5MB（原本就正常）", c_shot(400, 400, 1))
case("截图 3.5MB（复现那条）", c_shot(1100, 1000, 1))
case("截图 4张 9MB", c_shot(800, 700, 4))
case("截图 3.5MB 流式", c_stream_shot)
case("deepseek 普通文本", c_text_small)
case("deepseek 发起工具调用", c_tool_call)
case("deepseek 并行工具多轮", c_tool_multiturn)
case("deepseek chat 路径", c_chat)
for m in sys.argv[3:]:
    case(f"对照 {m}", c_other(m))
print(f"\n合计 {sum(cases)}/{len(cases)} PASS", flush=True)
