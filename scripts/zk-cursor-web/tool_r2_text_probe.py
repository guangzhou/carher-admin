#!/usr/bin/env python3
"""tool_r2_text_probe.py — 只重放 tool 两轮,打印 r2 wrapup 完整正文(裁定 hollow 是严判还是真空)。"""
import importlib.util
import json
import random
import sys
import time

spec = importlib.util.spec_from_file_location("s3", "/home/cltx/s3_probe.py")
s3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s3)

random.seed(7)
NAMES = []
for i in range(200):
    kind = random.choice(["src", "docs", "scripts", "tests", "k8s"])
    NAMES.append(f"{kind}/file_{i:03d}.{random.choice(['py', 'md', 'sh', 'yaml', 'js'])}")
BIG_LS = "total 480\n" + "\n".join(
    f"-rw-r--r--  1 dev dev {random.randint(200, 99999):6d} Aug {random.randint(1, 26):2d} 0{random.randint(1, 9)}:{random.randint(10, 59)} {n}"
    for n in NAMES)


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
                         "key_alias": f"r2text-{lane}-{int(time.time())}"}).read())
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
    d2["input"] = d["input"] + [call_item,
                                {"type": out_type, "call_id": c.get("call_id"), "output": BIG_LS}]
    r2 = drain(s3.post(base, key, "/v1/responses", d2))
    hits = sum(1 for n in NAMES if n.split("/", 1)[1] in r2["text"] or n in r2["text"])
    print(f"[r2] completed={r2['completed']} calls={len(r2['calls'])} len={len(r2['text'].strip())} hits={hits}", flush=True)
    print("[r2-TEXT-BEGIN]")
    print(r2["text"])
    print("[r2-TEXT-END]")
    if r2["calls"]:
        print("[r2-CALLS]", json.dumps(r2["calls"], ensure_ascii=False)[:600])
s3.post(base, mk, "/key/delete", {"keys": [key]}).read()
print("key deleted", flush=True)
