#!/usr/bin/env python3
"""P-c 补充探针:P-a 契约能否治 ll 型短指令答非所问(10 轮,钉 82,临时 key)"""
import importlib.util
import json
import time

spec = importlib.util.spec_from_file_location("s3", "/home/cltx/s3_probe.py")
s3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s3)

base, mk = s3.proxy(), s3.master_key()
key = None
for attempt in range(5):
    alias = f"s3c-ll-{int(time.time())}"
    r = json.loads(s3.post(base, mk, "/key/generate",
                           {"models": [s3.MODEL], "duration": "1h", "key_alias": alias}).read())
    k = r["key"]
    s3.run_request(base, k, s3.load_fix("hi", ""))
    time.sleep(4)
    lane = "?"
    for ln, dep in (("82", "zero-cursor-bpi-82"), ("101", "zero-cursor-bpi")):
        c = s3.sh(f"kubectl -n {s3.NS} logs deploy/{dep} --since=60s 2>/dev/null | grep -c REQ || true")
        if c and int(c or 0) > 0:
            lane = ln
            break
    print(f"[pin] {attempt+1}: lane={lane}", flush=True)
    if lane == "82":
        key = k
        break
    s3.post(base, mk, "/key/delete", {"keys": [k]}).read()
if not key:
    raise SystemExit("no 82 pin")

res = []
try:
    for i in range(10):
        rec = s3.run_request(base, key, s3.load_fix("ll", s3.PA_CONTRACT))
        v = s3.pa_verdict(rec)
        row = {"n": i + 1, "verdict": v, "calls": len(rec["calls"]),
               "text_head": rec["text"][:60], "lat": rec["latency_s"]}
        res.append(row)
        print(f"[P-c {i+1:02d}] ll -> {v} calls={row['calls']} {row['lat']}s", flush=True)
        time.sleep(3)
finally:
    s3.post(base, mk, "/key/delete", {"keys": [key]}).read()
    print("[key] deleted", flush=True)

agg = {}
for r in res:
    agg[r["verdict"]] = agg.get(r["verdict"], 0) + 1
print(json.dumps({"P-c_ll_with_contract": agg}, ensure_ascii=False))
with open("/home/cltx/s3c_ll.json", "w") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
