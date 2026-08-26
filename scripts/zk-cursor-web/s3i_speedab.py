#!/usr/bin/env python3
"""s3i_speedab.py — 延迟净收益 A/B(198 跑):baseline(全关) vs fast(fast_sse+sent_cache+skip_prepare)
每臂 8 发 hi(独立 key,词不同防缓存),记 total + first_text。切 env 由外层脚本做。
"""
import importlib.util
import json
import time
import urllib.error

spec = importlib.util.spec_from_file_location("s3", "/home/cltx/s3_probe.py")
s3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s3)

import sys
arm = sys.argv[1] if len(sys.argv) > 1 else "arm"
base, mk = s3.proxy(), s3.master_key()
alias = f"s3i-{arm}-{int(time.time())}"
kr = json.loads(s3.post(base, mk, "/key/generate",
                        {"models": ["cursor-web-fc-82-terra"], "duration": "20m",
                         "key_alias": alias}).read())
key = kr["key"]
lats, firsts = [], []
try:
    for i in range(8):
        d = json.load(open("/home/cltx/cw-harness/cw/replay_hi.json"))
        d["model"] = "cursor-web-fc-82-terra"
        # 改 query 词防会话缓存
        for it in d["input"]:
            if isinstance(it, dict) and it.get("role") == "user":
                for c in (it.get("content") or []):
                    if isinstance(c, dict) and "text" in c:
                        c["text"] = f"<user_query>ab-{arm}-{i} 你好</user_query>"
        req = urllib.request.Request(base + "/v1/responses", data=json.dumps(d).encode(),
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json"})
        t0 = time.time()
        ft = None
        try:
            r = urllib.request.urlopen(req, timeout=120)
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    try:
                        ev = json.loads(line[5:])
                    except ValueError:
                        continue
                    if ev.get("type") == "response.output_text.delta" and ft is None:
                        ft = round(time.time() - t0, 1)
        except (urllib.error.URLError, OSError) as e:
            print(f"  #{i} ERR {e}", flush=True)
            continue
        tot = round(time.time() - t0, 1)
        lats.append(tot)
        if ft:
            firsts.append(ft)
        print(f"  {arm} #{i}: total={tot}s first_text={ft}s", flush=True)
        time.sleep(2)
finally:
    s3.post(base, mk, "/key/delete", {"keys": [key]}).read()

lats.sort()
firsts.sort()
med = lats[len(lats) // 2] if lats else None
fmed = firsts[len(firsts) // 2] if firsts else None
print(json.dumps({"arm": arm, "n": len(lats), "total_median": med,
                  "total_avg": round(sum(lats) / len(lats), 1) if lats else None,
                  "first_text_median": fmed, "lats": lats}, ensure_ascii=False), flush=True)
