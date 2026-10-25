#!/usr/bin/env python3
"""给 198 LiteLLM 加 haiku-5.5 两个组（老实例 kiro-* 一个、槽位实例 kiro-slots-* 一个）。
照抄生产 kiro-claude-haiku-4-5 的形状，只改模型 / 窗口 / 价格。在生产 litellm pod 里跑。

价格 = Anthropic 官方（docs.anthropic.com/en/docs/about-claude/pricing，2026-10-25 读）：
  ≤100K 提示：输入 $0.10 / 输出 $0.50 / 缓存写 5m $0.125 / 1h $0.20 / 缓存读 $0.01 （每 MTok）
  >100K 提示：输入 $0.50 / 输出 $2.50 / 缓存写 5m $0.625 / 1h $1.00 / 缓存读 $0.05
🔴 不传 model_info.mode（memory feedback_model_new_mode_poisons_shared_backend_key）

  python3 kiro-haiku55-groups.py plan|apply|remove
"""
import json, os, sys, urllib.request, yaml

BASE = "http://127.0.0.1:4000"
MK = os.environ["LITELLM_MASTER_KEY"]
M = 1e-6
PRICE = {
    "input_cost_per_token": 0.10 * M,
    "output_cost_per_token": 0.50 * M,
    "cache_creation_input_token_cost": 0.125 * M,
    "cache_creation_input_token_cost_above_1hr": 0.20 * M,
    "cache_read_input_token_cost": 0.01 * M,
    "input_cost_per_token_above_100k_tokens": 0.50 * M,
    "output_cost_per_token_above_100k_tokens": 2.50 * M,
    "cache_creation_input_token_cost_above_100k_tokens": 0.625 * M,
    "cache_creation_input_token_cost_above_1hr_above_100k_tokens": 1.00 * M,
    "cache_read_input_token_cost_above_100k_tokens": 0.05 * M,
}


def api(path, body=None):
    r = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                               method="POST" if body is not None else "GET",
                               headers={"Authorization": "Bearer " + MK, "content-type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=60))


def existing():
    out, page = {}, 1
    while True:
        d = api(f"/v2/model/info?page={page}&size=100")
        for m in d.get("data", []):
            if m.get("model_name") in NAMES:
                out.setdefault(m["model_name"], []).append(m.get("model_info", {}).get("id"))
        if page >= d.get("total_pages", 1):
            return out
        page += 1


NAMES = ("kiro-claude-haiku-5-5", "kiro-slots-claude-haiku-5-5")


def planned():
    c = yaml.safe_load(open("/app/config.yaml"))
    tmpl = [m for m in c["model_list"] if m["model_name"] == "kiro-claude-haiku-4-5"]
    assert len(tmpl) == 1, "模板 kiro-claude-haiku-4-5 不唯一"
    old = dict(tmpl[0]["litellm_params"])
    slots_key = os.environ["SLOTS_KEY"]
    rows = []
    for name, base, key, idp in (("kiro-claude-haiku-5-5", old["api_base"], old["api_key"], "kiro-rs"),
                                 ("kiro-slots-claude-haiku-5-5", "http://kiro-rs-slots.kiro-rs.svc.cluster.local:8990", slots_key, "kiro-slots")):
        p = {"model": "anthropic/claude-haiku-5-5", "api_base": base, "api_key": key,
             "input_cost_per_token": PRICE["input_cost_per_token"], "output_cost_per_token": PRICE["output_cost_per_token"]}
        info = dict(PRICE)
        info.update({"id": f"{idp}/claude-haiku-5-5", "max_input_tokens": 1000000, "max_output_tokens": 128000,
                     "kiro_upstream_model_id": "claude-haiku-5.5", "kiro_rate_multiplier": 0.06})
        assert "mode" not in info
        rows.append({"model_name": name, "litellm_params": p, "model_info": info})
    return rows


def main():
    cmd = sys.argv[1]
    have = existing()
    if cmd == "remove":
        for n, ids in have.items():
            for i in ids:
                api("/model/delete", {"id": i})
        print("OK removed", have)
        return
    for r in planned():
        if r["model_name"] in have:
            print("SKIP exists", r["model_name"]); continue
        if cmd == "apply":
            api("/model/new", r)
        print("ADD " if cmd == "apply" else "PLAN", r["model_name"], r["litellm_params"]["api_base"],
              {k: v for k, v in r["model_info"].items() if "cost" in k and "above" not in k})
    print("OK", cmd)


main()
