#!/usr/bin/env python3
"""在 198 LiteLLM（DB）加 / 删 `kiro-slots-*` 组：照抄生产 `kiro-*` 组，只改 api_base/api_key/model_info.id。
在生产 litellm pod 里跑（读 /app/config.yaml + env 的 master key，打 127.0.0.1:4000）。不重启 LiteLLM。

  python3 kiro-slots-groups.py plan   <slots_api_key>   # 只打印将加哪些
  python3 kiro-slots-groups.py apply  <slots_api_key>   # 加（已存在的跳过）
  python3 kiro-slots-groups.py remove                   # 回滚：删掉所有 kiro-slots-*

🔴 model_info 不带 mode（memory feedback_model_new_mode_poisons_shared_backend_key）
"""
import json, os, sys, urllib.request, yaml

BASE = "http://127.0.0.1:4000"
MK = os.environ["LITELLM_MASTER_KEY"]
SLOTS_BASE = "http://kiro-rs-slots.kiro-rs.svc.cluster.local:8990"
KEEP_INFO = ("input_cost_per_token", "output_cost_per_token", "cache_creation_input_token_cost",
             "cache_read_input_token_cost", "max_input_tokens", "max_output_tokens",
             "kiro_rate_multiplier", "kiro_upstream_model_id", "supports_vision",
             "supports_function_calling", "supports_reasoning", "supports_prompt_caching")

def api(path, body=None, method=None):
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                               method=method or ("POST" if body is not None else "GET"),
                               headers={"Authorization": f"Bearer {MK}", "content-type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=60))


def existing_slots():
    """现有 kiro-slots-* 部署：{model_name: [model_id...]}（/v2/model/info 分页）"""
    out, page = {}, 1
    while True:
        d = api(f"/v2/model/info?page={page}&size=100")
        for m in d.get("data", []):
            n = m.get("model_name", "")
            if n.startswith("kiro-slots-"):
                out.setdefault(n, []).append(m.get("model_info", {}).get("id"))
        if page >= d.get("total_pages", 1):
            return out
        page += 1

def planned(slots_key):
    c = yaml.safe_load(open("/app/config.yaml"))
    rows = []
    for m in c["model_list"]:
        n = m["model_name"]
        if not n.startswith("kiro-") or n.startswith("kiro-slots-"):
            continue
        p = dict(m["litellm_params"]); p["api_base"] = SLOTS_BASE; p["api_key"] = slots_key
        info = {k: v for k, v in (m.get("model_info") or {}).items() if k in KEEP_INFO}
        info["id"] = "kiro-slots/" + (m.get("model_info") or {}).get("id", n).split("/")[-1]
        rows.append({"model_name": "kiro-slots-" + n[len("kiro-"):], "litellm_params": p, "model_info": info})
    return rows

def main():
    cmd = sys.argv[1]
    have = existing_slots()
    if cmd == "remove":
        n = 0
        for name, ids in have.items():
            for i in ids:
                api("/model/delete", {"id": i}); n += 1
        print("OK removed", n, sorted(have))
        return
    rows = planned(sys.argv[2])
    only = [x for x in os.environ.get("ONLY_GROUPS", "").split(",") if x]
    if only:
        rows = [r for r in rows if r["model_name"] in only]
    for r in rows:
        assert "mode" not in r["model_info"], r["model_name"]
        if r["model_name"] in have:
            print("SKIP exists", r["model_name"]); continue
        if cmd == "apply":
            api("/model/new", r)
        print(("ADD " if cmd == "apply" else "PLAN"), r["model_name"], r["litellm_params"]["model"], r["model_info"]["id"])
    print("OK", cmd, len(rows), "groups")

main()
