#!/usr/bin/env python3
"""aws-* 组回归：每个组 × 三线型（chat 流式 / messages 流式 / responses 流式）+ chat 工具调用，
同时扫响应头和 body 里有没有 kiro 字样。在 198 上跑，打 127.0.0.1:30402。

  KEY=sk-... python3 aws-groups-regress.py <对外名>=<组> ...    # 用一把真 key（走它的 alias）
  KEY=sk-... python3 aws-groups-regress.py --names claude-opus-5-5 auto ...   # 只给对外名（看 key 自己的 alias）

判据：
- 文本线型：回复里含随机算术题答案（拒绝复述的模型也会算对）
- 工具：返回 tool_calls 且函数名 = add、参数 a/b 正确
- 泄漏：响应头 + body 全文不出现 kiro（大小写不敏感）；模型自称 "I'm Kiro" 是上游人设，单独计数不判失败
- SpendLogs 落点：脚本末尾打印本轮请求在 SpendLogs 的 model_group（由调用方去查）
"""
import json, os, random, sys, time, urllib.request, urllib.error

BASE = "http://127.0.0.1:30402"
KEY = os.environ["KEY"]
TOOLS = [{"type": "function", "function": {"name": "add", "description": "add two integers",
          "parameters": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                         "required": ["a", "b"]}}}]


def post(path, body, extra=None):
    h = {"Authorization": "Bearer " + KEY, "x-api-key": KEY, "content-type": "application/json",
         "anthropic-version": "2023-06-01"}
    h.update(extra or {})
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers=h)
    t = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=300)
        return r.status, dict(r.headers), r.read().decode(errors="replace"), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode(errors="replace"), time.time() - t
    except Exception as e:
        return 0, {}, repr(e), time.time() - t


def texts(raw):
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue

        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if k in ("content", "text", "delta", "output_text") and isinstance(v, str):
                        out.append(v)
                    else:
                        walk(v)
            elif isinstance(x, list):
                for i in x:
                    walk(i)
        walk(o)
    return "".join(out)


def leak(headers, raw):
    blob = (json.dumps(headers) + raw).lower()
    persona = blob.count("i'm kiro") + blob.count("i am kiro") + blob.count("我是 kiro") + blob.count("我是kiro")
    hits = blob.count("kiro") - persona
    return hits, persona


def case(name):
    a, b = random.randint(100, 999), random.randint(100, 999)
    q = f"{a}+{b}=? 只回答数字"
    want = str(a + b)
    res = []
    shapes = [
        ("chat", "/v1/chat/completions", {"model": name, "stream": True, "max_tokens": 400,
                                          "messages": [{"role": "user", "content": q}]}),
        ("msgs", "/v1/messages", {"model": name, "stream": True, "max_tokens": 400,
                                  "messages": [{"role": "user", "content": q}]}),
        ("resp", "/v1/responses", {"model": name, "stream": True, "max_output_tokens": 400, "input": q}),
    ]
    for tag, path, body in shapes:
        code, h, raw, dt = post(path, body)
        t = texts(raw)
        hits, persona = leak(h, raw)
        ok = code == 200 and want in t.replace(",", "")
        res.append((tag, ok, code, round(dt, 1), hits, persona, "" if ok else raw[:160].replace("\n", " ")))
    code, h, raw, dt = post("/v1/chat/completions", {
        "model": name, "max_tokens": 400, "tools": TOOLS, "tool_choice": "auto",
        "messages": [{"role": "user", "content": f"用 add 工具计算 {a} 加 {b}"}]})
    ok, why = False, raw[:160]
    try:
        tc = json.loads(raw)["choices"][0]["message"].get("tool_calls") or []
        args = json.loads(tc[0]["function"]["arguments"]) if tc else {}
        ok = bool(tc) and tc[0]["function"]["name"] == "add" and {int(args.get("a", 0)), int(args.get("b", 0))} == {a, b}
        why = "" if ok else json.dumps(tc)[:160]
    except Exception as e:
        why = repr(e)[:80] + " " + raw[:120]
    hits, persona = leak(h, raw)
    res.append(("tool", ok, code, round(dt, 1), hits, persona, why.replace("\n", " ")))
    return res


def main():
    names = [x for x in sys.argv[1:] if not x.startswith("--")]
    bad = 0
    for n in names:
        for tag, ok, code, dt, hits, persona, why in case(n):
            if not ok or hits:
                bad += 1
            print(f"{'OK ' if ok and not hits else 'BAD'} {n:22s} {tag:4s} {code} {dt:6.1f}s kiro={hits} persona={persona} {why}")
    print("RESULT", "PASS" if not bad else f"FAIL {bad}")
    sys.exit(1 if bad else 0)


main()
