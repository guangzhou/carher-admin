#!/usr/bin/env python3
"""url_toolfeed_probe.py — 复现"工具结果里带URL→模型收尾给链接"的交付形状(裁定 url 残缺根因)。

T1: ls fixture 起会话拿到 function_call。
T2: 回灌工具结果=伪造的"飞书文档已创建"输出(含 docx URL),追加 user 消息要求把链接给用户。
观察:交付正文里 URL 是完整、被换成字面"url"、还是残缺(url…https 形态);配合 pod 日志
[url-safe] refs/mods 计数,钉死帧形状是否被现有捕获路径覆盖。
用法: python3 url_toolfeed_probe.py [lane]
"""
import importlib.util
import json
import sys
import time

spec = importlib.util.spec_from_file_location("s3", "/home/cltx/s3_probe.py")
s3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s3)

TOOL_OUT = (
    "飞书文档创建成功。\n"
    "标题: 工程代码结构梳理\n"
    "文档链接: https://cltx.feishu.cn/docx/R0k7wB2LwhZ7NXbDLSScXnU2nLf\n"
    "画板已嵌入,预览导出 230508 bytes。\n"
)


def drain(resp):
    out = {"completed": 0, "text": "", "calls": []}
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        dd = line[5:].strip()
        if dd == "[DONE]":
            continue
        try:
            ev = json.loads(dd)
        except ValueError:
            continue
        t = ev.get("type", "")
        if t == "response.completed":
            out["completed"] += 1
        elif t == "response.output_item.done":
            it = ev.get("item") or {}
            if it.get("type") == "message":
                out["text"] += "".join(c.get("text", "") for c in (it.get("content") or []) if isinstance(c, dict))
            elif it.get("type") in ("function_call", "custom_tool_call"):
                out["calls"].append({k: it.get(k) for k in ("type", "id", "call_id", "name", "arguments", "input", "status")})
    return out


lane = sys.argv[1] if len(sys.argv) > 1 else "82"
MODEL = {"101": "cursor-web-fc-terra", "82": "cursor-web-fc-82-terra"}[lane]
base, mk = s3.proxy(), s3.master_key()
kr = json.loads(s3.post(base, mk, "/key/generate",
                        {"models": [MODEL], "duration": "1h",
                         "key_alias": f"urlfeed-{lane}-{int(time.time())}"}).read())
key = kr["key"]
d = s3.load_fix("ls", "")
d["model"] = MODEL
r1 = drain(s3.post(base, key, "/v1/responses", d))
print(f"[r1] completed={r1['completed']} calls={len(r1['calls'])}", flush=True)
if r1["calls"]:
    c = r1["calls"][0]
    call_item = {k: v for k, v in c.items() if v is not None}
    out_type = "custom_tool_call_output" if c["type"] == "custom_tool_call" else "function_call_output"
    d2 = s3.load_fix("ls", "")
    d2["model"] = MODEL
    d2["input"] = d["input"] + [
        call_item,
        {"type": out_type, "call_id": c.get("call_id"), "output": TOOL_OUT},
        {"type": "message", "role": "user",
         "content": [{"type": "input_text",
                      "text": "<user_query>\n把文档链接原样发给我,格式:文档地址:[打开飞书文档](链接)。\n</user_query>"}]},
    ]
    r2 = drain(s3.post(base, key, "/v1/responses", d2))
    print(f"[r2] completed={r2['completed']} len={len(r2['text'].strip())}", flush=True)
    print("[r2-TEXT-BEGIN]")
    print(r2["text"])
    print("[r2-TEXT-END]")
    ok = "https://cltx.feishu.cn/docx/R0k7wB2LwhZ7NXbDLSScXnU2nLf" in r2["text"]
    print(f"[VERDICT] full_url_present={ok}", flush=True)
s3.post(base, mk, "/key/delete", {"keys": [key]}).read()
print("key deleted", flush=True)
