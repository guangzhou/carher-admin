#!/usr/bin/env python3
"""kiro.rs 隔离 E2E 用例（由 scripts/kiro-rs-e2e.sh 拷到 198 上跑，直打隔离容器）。

用法: kiro_rs_e2e_cases.py <base_url> <config.json>

每例打 OK/STOP + 原因，分母 = CASES 全部；全过 rc=0。
判落点不认响应 model 字段（kiro 原样回显请求名），只认 HTTP 状态 + 内容形状；
映射落点由 cargo 单测管（sonnet-5 / opus-5 阳性对照都在单测里）。
"""
import base64
import json
import struct
import sys
import urllib.error
import urllib.request
import zlib

BASE = sys.argv[1].rstrip("/")
KEY = json.load(open(sys.argv[2]))["apiKey"]
# /v1/models 现行条数：上游 v2026.10.1 的 22 + 0001 的 9 透传 + sonnet-5.5 两条 + 0005 kimi-k3 = 34。改了补丁就改这里。
EXPECTED_MODELS = 34


def post(body, timeout=180):
    req = urllib.request.Request(
        BASE + "/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def text_of(raw):
    d = json.loads(raw)
    return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text"), d


def simple(model):
    def run():
        st, raw = post({"model": model, "max_tokens": 64,
                        "messages": [{"role": "user", "content": "Reply with exactly: PONG"}]})
        if st != 200:
            return False, f"HTTP {st} {raw[:160]}"
        t, _ = text_of(raw)
        return "PONG" in t.upper(), f"text={t[:40]!r}"
    return run


def models_list():
    req = urllib.request.Request(BASE + "/v1/models", headers={"x-api-key": KEY})
    ids = [m["id"] for m in json.load(urllib.request.urlopen(req, timeout=30))["data"]]
    need = ["claude-sonnet-5-5", "claude-opus-5-5", "gpt-5.6-sol", "glm-5", "auto", "kimi-k3"]
    miss = [n for n in need if n not in ids]
    return len(ids) == EXPECTED_MODELS and not miss, f"n={len(ids)} (期望 {EXPECTED_MODELS}) missing={miss}"


def stream():
    st, raw = post({"model": "claude-sonnet-5-5", "max_tokens": 64, "stream": True,
                    "messages": [{"role": "user", "content": "Reply with exactly: PONG"}]})
    if st != 200:
        return False, f"HTTP {st} {raw[:160]}"
    ev = [l[6:].strip() for l in raw.splitlines() if l.startswith("event: ")]
    txt = "".join(json.loads(l[5:]).get("delta", {}).get("text", "") for l in raw.splitlines()
                  if l.startswith("data:") and '"text_delta"' in l)
    ok = ev[:1] == ["message_start"] and "message_stop" in ev and "PONG" in txt.upper()
    return ok, f"events={len(ev)} last={ev[-1:]} text={txt[:30]!r}"


def tools():
    tool = {"name": "get_weather", "description": "Get weather for a city",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}
    msgs = [{"role": "user", "content": "What's the weather in Paris? Use the tool."}]
    st, raw = post({"model": "claude-opus-5-5", "max_tokens": 256, "tools": [tool], "messages": msgs})
    if st != 200:
        return False, f"HTTP {st} {raw[:160]}"
    _, d = text_of(raw)
    tu = [b for b in d["content"] if b["type"] == "tool_use"]
    if not tu or tu[0]["input"].get("city", "").lower() != "paris":
        return False, f"no tool_use / bad args: {d['content']}"
    msgs += [{"role": "assistant", "content": d["content"]},
             {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tu[0]["id"], "content": "Sunny, 21C"}]}]
    st, raw = post({"model": "claude-opus-5-5", "max_tokens": 128, "tools": [tool], "messages": msgs})
    if st != 200:
        return False, f"round2 HTTP {st} {raw[:160]}"
    t, _ = text_of(raw)
    return "21" in t, f"args={tu[0]['input']} round2={t[:50]!r}"


def _png(rgb, n=32):
    raw = b"".join(b"\x00" + bytes(rgb) * n for _ in range(n))
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))  # noqa: E731
    return (b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 2, 0, 0, 0))
            + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b""))


def tool_result_image():
    """上游 v2026.10.1 修的 bug：tool_result 里的 image 块旧版被静默丢掉。两种颜色都要答对，排除蒙中。"""
    tool = {"name": "screenshot", "description": "Take a screenshot", "input_schema": {"type": "object", "properties": {}}}
    got = []
    for color, rgb in (("green", (0, 200, 0)), ("blue", (0, 0, 230))):
        msgs = [
            {"role": "user", "content": "Take a screenshot, then tell me the single dominant color of it in one word."},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_01", "name": "screenshot", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01", "content": [
                {"type": "text", "text": "screenshot captured"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.b64encode(_png(rgb)).decode()}}]}]},
        ]
        st, raw = post({"model": "claude-sonnet-5-5", "max_tokens": 60, "tools": [tool], "messages": msgs})
        if st != 200:
            return False, f"{color}: HTTP {st} {raw[:120]}"
        t, _ = text_of(raw)
        got.append((color, t.strip()[:40]))
    return all(c in t.lower() for c, t in got), f"{got}"


def web_search():
    st, raw = post({"model": "claude-sonnet-5-5", "max_tokens": 1024,
                    "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
                    "messages": [{"role": "user", "content": "Search the web: what is the latest kiro.rs release version on GitHub (hank9999/kiro.rs)? Answer briefly."}]},
                   timeout=240)
    if st != 200:
        return False, f"HTTP {st} {raw[:160]}"
    _, d = text_of(raw)
    types = [b["type"] for b in d["content"]]
    return "server_tool_use" in types and "web_search_tool_result" in types, f"blocks={types}"


CASES = [
    ("models-list", models_list),
    ("sonnet-5-5 (0001)", simple("claude-sonnet-5-5")),
    ("sonnet-5 (对照)", simple("claude-sonnet-5")),
    ("opus-5-5", simple("claude-opus-5-5")),
    ("kiro-gpt-5.6-sol (0001 前缀)", simple("kiro-gpt-5.6-sol")),
    ("glm-5 (0001 透传)", simple("glm-5")),
    ("kimi-k3 (0005 透传)", simple("kimi-k3")),
    ("kiro-kimi-k3 (0005 前缀)", simple("kiro-kimi-k3")),
    ("haiku-4-5", simple("claude-haiku-4-5-20251001")),
    ("stream sonnet-5-5", stream),
    ("tool_use + tool_result 续答", tools),
    ("tool_result 夹图片 (上游 10-08 修)", tool_result_image),
    ("web_search 搜索回合 (0002)", web_search),
]

fails = 0
for name, fn in CASES:
    try:
        ok, why = fn()
    except Exception as e:  # noqa: BLE001 每例都要有出口
        ok, why = False, f"EXC {type(e).__name__}: {e}"
    fails += not ok
    print(f"{'OK  ' if ok else 'STOP'} {name}: {why}", flush=True)
print(f"== {len(CASES) - fails}/{len(CASES)} passed")
sys.exit(1 if fails else 0)
