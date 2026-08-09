#!/usr/bin/env python3
"""198 prod: 幂等清扫 —— 给取不到价的 entry 补 model_info.base_model。
base_model 取值全部来自实测(SpendLogs.metadata.model_map_information 里已计费行命中的键),
不是猜的。池每次轮换都会引入一批新的无 base_model entry, 轮换后必跑。
用法: litellm-acct-base-model-fix.py [--apply]
"""
import json, re, subprocess, sys, urllib.request

# 后缀 -> 价表键。codex-auto-review 的值来自实测: 已计费行 map_key = gpt-5.6-luna
# (08-07 旧池 188/188, 08-09 新池 1837 行 / 57 个 id, 两轮都是同一个键)
SUFFIX_BASE = [
    (r"-codex-auto-review$", "gpt-5.6-luna"),
    (r"-gpt-5\.3-codex$",    "gpt-5.3-codex"),
    (r"-gpt-5\.4$",          "gpt-5.4"),
    (r"-gpt-5\.5$",          "gpt-5.5"),
    (r"-gpt-5\.6-luna$",     "gpt-5.6-luna"),
    (r"-gpt-5\.6-sol$",      "gpt-5.6-sol"),
    (r"-gpt-5\.6-terra$",    "gpt-5.6-terra"),
]
# zerokey bridge / cursor 直连类: id 不含变体后缀, 单独钉。值 = 各自解密后的上游 model 名
# (去 provider 前缀), 该键在内置价表有价。实测 zerokey-codex-bridge 出现过
# map_key=自己的 id / in=0 的行, 所以不能只靠上游名兜着。
EXACT_BASE = {
    "zerokey-codex-bridge":       "gpt-5.6-terra",
    "zerokey-codex-terra-bridge": "gpt-5.6-terra",
    "zerokey-codex-sol-bridge":   "gpt-5.6-sol",
    "zerokey-codex-luna-bridge":  "gpt-5.6-luna",
    "zerokey-codex-55-bridge":    "gpt-5.5",
    "zerokey-codex-54-bridge":    "gpt-5.4",
    "cursor/claude-opus-5":       "claude-opus-5",
}
PREFIXES = ("chatgpt-acct-", "uasplit-", "zerokey-codex", "cursor/")
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
    if not mid.startswith(PREFIXES):
        continue
    if mi.get("base_model") or lp.get("input_cost_per_token"):
        continue                                    # 已有价源, 幂等跳过
    bm = EXACT_BASE.get(mid) or next((v for pat, v in SUFFIX_BASE if re.search(pat, mid)), None)
    (todo if bm else skipped).append((mid, bm, mi.get("mode")))

print(f"apply={apply_} 待补={len(todo)} 无法映射={len(skipped)}")
for mid, bm, mode in todo:
    if not apply_:
        print(f"  DRY {mid} -> base_model={bm} (mode={mode})"); continue
    # model_id 带斜杠时 REST 路径进不去(404) —— 那几条只能走 DB jsonb_set 直写
    if "/" in mid:
        print(f"  MANUAL {mid} -> base_model={bm} (id 含斜杠, PATCH 404, 需 DB jsonb_set)"); continue
    req = urllib.request.Request(f"{EP}/model/{mid}/update",
        data=json.dumps({"model_info": {"id": mid, "mode": mode or "responses",
                                        "db_model": True, "base_model": bm}}).encode(),
        headers={"Authorization":"Bearer "+MK, "Content-Type":"application/json"}, method="PATCH")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp: print(f"  OK {mid} -> {bm} http={resp.status}")
    except Exception as e: print(f"  FAIL {mid} {e}")
for mid, _, _ in skipped: print(f"  SKIP(无映射) {mid}")
