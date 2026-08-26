#!/usr/bin/env python3
"""s3g_latency.py — sol vs instant 档 "hi" 延迟对照(各4轮,临时key用完即删)"""
import importlib.util
import json
import time

spec = importlib.util.spec_from_file_location("s3", "/home/cltx/s3_probe.py")
s3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s3)

base, mk = s3.proxy(), s3.master_key()
out = {}
for model in ("cursor-g-5.6-sol", "cursor-g-5.6-instant"):
    alias = f"s3g-{model.split('.')[-1]}-{int(time.time())}"
    r = json.loads(s3.post(base, mk, "/key/generate",
                           {"models": [model], "duration": "30m", "key_alias": alias}).read())
    key = r["key"]
    lats, forms = [], []
    try:
        for i in range(4):
            d = s3.load_fix("hi", "")
            d["model"] = model
            rec = s3.run_request(base, key, d)
            lats.append(rec["latency_s"])
            forms.append(f"{'ok' if rec['completed'] else 'X'}/{len((rec['text'] or '').strip())}ch")
            print(f"[{model}] hi #{i+1}: {rec['latency_s']}s {forms[-1]}", flush=True)
            time.sleep(2)
    finally:
        s3.post(base, mk, "/key/delete", {"keys": [key]}).read()
    out[model] = {"lats": lats, "forms": forms,
                  "avg": round(sum(lats) / len(lats), 1), "min": min(lats), "max": max(lats)}
print(json.dumps(out, ensure_ascii=False))
