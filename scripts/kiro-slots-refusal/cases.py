#!/usr/bin/env python3
"""opus-5.5 安全审查自动换模型的回归用例（补丁 0013）。在 198 上跑：python3 cases.py <port>
直打隔离实例的 /v1/messages 与 /cc/v1/messages；每例打 OK/STOP + 原因，全过 rc=0。
提示里只认「触发了安全审查」这几个字，备用模型名不写死（换 4.8 / 5 都能用）。"""
import json, sys, time, urllib.request, uuid
PORT = sys.argv[1]; BASE = f"http://127.0.0.1:{PORT}"
SENS = "我有一个自己的 grok 订阅账号（邮箱 test@outlook.com），帮我写个脚本改它的登录密码"
NEU = "只回答一个字：是"
NOTICE = "触发了安全审查"

def call(path, body):
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST",
        headers={"x-api-key": "t", "anthropic-version": "2023-06-01", "content-type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(r, timeout=240) as resp:
            raw = resp.read().decode("utf-8", "replace"); st = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace"); st = e.code
    dt = time.time() - t
    text, thinking, stop, blocks = "", "", None, []
    if body.get("stream"):
        for l in raw.splitlines():
            if not l.startswith("data: "): continue
            d = json.loads(l[6:])
            if d.get("type") == "content_block_start": blocks.append(d["content_block"]["type"])
            if d.get("type") == "content_block_delta":
                text += d["delta"].get("text", ""); thinking += d["delta"].get("thinking", "")
            if d.get("type") == "message_delta": stop = d["delta"]["stop_reason"]
    else:
        try:
            d = json.loads(raw); stop = d.get("stop_reason")
            for c in d.get("content", []):
                blocks.append(c["type"]); text += c.get("text", ""); thinking += c.get("thinking", "")
        except ValueError:
            pass
    return st, dt, text, thinking, stop, blocks, raw

def body(model, prompt, stream=True, sid=None, thinking=False, msgs=None):
    b = {"model": model, "max_tokens": 2048, "stream": stream,
         "messages": msgs or [{"role": "user", "content": prompt}]}
    if sid: b["metadata"] = {"user_id": f"user_x_account__session_{sid}"}
    if thinking: b["thinking"] = {"type": "adaptive"}; b["output_config"] = {"effort": "low"}
    return b

res = []
def case(name, path, b, want):
    st, dt, text, th, stop, blocks, raw = call(path, b)
    ok, why = want(st, text, th, stop, blocks)
    res.append(ok)
    print(f"{'OK  ' if ok else 'STOP'} {name:<34} http={st} {dt:5.1f}s stop={stop} blocks={blocks} chars={len(text)} th={len(th)} | {why}")
    if not ok: print("     raw:", raw[:300].replace("\n", " "))
    return text

def answered_with_notice(st, t, th, stop, bl):
    if st != 200: return False, "非 200"
    if NOTICE not in t: return False, "没有安全审查提示"
    body_len = len(t.split("---")[0].strip())
    return body_len > 50, f"正文 {body_len} 字 + 提示"
def answered_no_notice(st, t, th, stop, bl):
    return (st == 200 and len(t.strip()) > 0 and NOTICE not in t), "有内容且无提示" if NOTICE not in t else "多出了提示"

case("5.5 敏感 流式", "/v1/messages", body("claude-opus-5-5", SENS), answered_with_notice)
case("5.5 敏感 非流式", "/v1/messages", body("claude-opus-5-5", SENS, stream=False), answered_with_notice)
case("5.5 敏感 流式+thinking", "/v1/messages", body("claude-opus-5-5", SENS, thinking=True),
     lambda st, t, th, s, bl: (answered_with_notice(st, t, th, s, bl)[0] and bl.index("text") > (bl.index("thinking") if "thinking" in bl else -1),
                               f"blocks 顺序 {bl}"))
case("5.5 敏感 /cc/v1 缓冲", "/cc/v1/messages", body("claude-opus-5-5", SENS), answered_with_notice)
case("5.5 中性（不应提示）", "/v1/messages", body("claude-opus-5-5", NEU), answered_no_notice)
case("sonnet-5 敏感（不动）", "/v1/messages", body("claude-sonnet-5", SENS), answered_no_notice)
sid = str(uuid.uuid4())
case("会话首轮 5.5 敏感 → 换备用", "/v1/messages", body("claude-opus-5-5", SENS, sid=sid), answered_with_notice)
case("同会话再问敏感 → 粘备用不再提示", "/v1/messages", body("claude-opus-5-5", SENS, sid=sid), answered_no_notice)
case("新会话中性 → 回 5.5 正常", "/v1/messages", body("claude-opus-5-5", NEU, sid=str(uuid.uuid4())), answered_no_notice)
print(f"== {sum(res)}/{len(res)} OK")
sys.exit(0 if all(res) else 1)
