#!/usr/bin/env python3
"""198 prod: 幂等清扫 —— 给取不到价的 responses 类 entry 补 model_info.base_model。
base_model 取值全部来自实测(SpendLogs.metadata.model_map_information 里已计费行命中的键),
不是猜的。用法: base_model_sweep.py [--apply]
"""
import json, re, subprocess, sys, urllib.request

# 后缀 -> 价表键。codex-auto-review 的值来自实测: 已计费行 map_key = gpt-5.6-luna (188/188)
SUFFIX_BASE = [
    (r"-codex-auto-review$", "gpt-5.6-luna"),
    (r"-gpt-5\.3-codex$",    "gpt-5.3-codex"),
    (r"-gpt-5\.4$",          "gpt-5.4"),
    (r"-gpt-5\.5$",          "gpt-5.5"),
    (r"-gpt-5\.6-luna$",     "gpt-5.6-luna"),
    (r"-gpt-5\.6-sol$",      "gpt-5.6-sol"),
    (r"-gpt-5\.6-terra$",    "gpt-5.6-terra"),
]
def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
MK = sh("kubectl -n litellm-product get secret litellm-secrets -o jsonpath={.data.LITELLM_MASTER_KEY} | base64 -d")
EP = "http://127.0.0.1:30402"

def get(path):
    r = urllib.request.Request(EP+path, headers={"Authorization":"Bearer "+MK})
    return json.loads(urllib.request.urlopen(r, timeout=60).read())

rows = get("/model/info")["data"]
best = {}
for r in rows:
    mi = r.get("model_info") or {}; mid = mi.get("id")
    if not mid: continue
    if mid not in best or (mi.get("base_model") and not best[mid][0].get("base_model")):
        best[mid] = (mi, r.get("litellm_params") or {})

apply_ = "--apply" in sys.argv
todo, skipped = [], []
for mid, (mi, lp) in sorted(best.items()):
    if not (mid.startswith("chatgpt-acct-") or mid.startswith("uasplit-")):
        continue
    if mi.get("base_model") or lp.get("input_cost_per_token"):
        continue                                    # 已有价源, 幂等跳过
    bm = next((v for pat, v in SUFFIX_BASE if re.search(pat, mid)), None)
    (todo if bm else skipped).append((mid, bm, mi.get("mode")))

print(f"apply={apply_} 待补={len(todo)} 无法映射={len(skipped)}")
for mid, bm, mode in todo:
    if not apply_:
        print(f"  DRY {mid} -> base_model={bm} (mode={mode})"); continue
    req = urllib.request.Request(f"{EP}/model/{mid}/update",
        data=json.dumps({"model_info": {"id": mid, "mode": mode or "responses",
                                        "db_model": True, "base_model": bm}}).encode(),
        headers={"Authorization":"Bearer "+MK, "Content-Type":"application/json"}, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp: print(f"  OK {mid} -> {bm} http={resp.status}")
    except Exception as e: print(f"  FAIL {mid} {e}")
for mid, _, _ in skipped: print(f"  SKIP(无映射) {mid}")
