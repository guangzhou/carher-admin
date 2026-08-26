"""cursor-web-fc-pool-* 组池 deployment 注册模板（2026-08-24 首次落地 6/6 OK）。

用法（新账号入池，第二步；第一步先 clone_web_fc_lane.py 克隆出线）：
1. LANES 加一行：'N': 'http://zero-cursor-bpi-N.litellm-product.svc.cluster.local:8201/v1'
2. base64 本文件，在 198 litellm-proxy pod 内跑（有 LITELLM_MASTER_KEY env）：
   base64 < pool_register.py | ssh cltx@10.68.13.198 'export KUBECONFIG=...; \
     kubectl -n litellm-product exec -i <proxy-pod> -- sh -c "base64 -d > /tmp/pr.py && python3 /tmp/pr.py"'
3. 已注册过的 (model_name, model_info.id) 会报错 —— 只保留新 lane 再跑，或忽略旧行报错。
4. 验收：临时 scoped key 打 pool 别名暗号 ≥4 次 → 200+回显 + grep 新 pod 日志确认流量归属。
注意：api_key 占位符必带（假绿①）；weight 可按账号质量调；回滚 /model/delete 对应 id。
详见 skill zk-cursor-web-fc-iterate「新账号入池清单」。
"""
import os, json, urllib.request

MK = os.environ["LITELLM_MASTER_KEY"]
BASE = "http://localhost:4000"

LANES = {
    "101": "http://zero-cursor-bpi.litellm-product.svc.cluster.local:8201/v1",
    "82":  "http://zero-cursor-bpi-82.litellm-product.svc.cluster.local:8201/v1",
}
TIERS = [("", None), ("-high", "high"), ("-max", "xhigh")]

COMMON = {
    "use_in_pass_through": False, "use_litellm_proxy": False,
    "use_chat_completions_api": False, "use_xai_oauth": False,
    "merge_reasoning_content_in_choices": False,
    "model": "openai/gpt-5.6-terra",
    "api_key": "sk-zerokey-web-noop",
    "weight": 1,
}

def post(path, payload):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + MK, "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        return {"HTTP_ERROR": e.code, "body": e.read().decode()[:300]}

results = []
for suffix, reff in TIERS:
    for lane, api_base in LANES.items():
        lp = dict(COMMON, api_base=api_base)
        if reff: lp["reasoning_effort"] = reff
        payload = {
            "model_name": f"cursor-web-fc-pool-terra{suffix}",
            "litellm_params": lp,
            "model_info": {"id": f"zerokey-cursor-web-fc-pool-{lane}-terra{suffix}", "mode": "chat"},
        }
        r = post("/model/new", payload)
        ok = "HTTP_ERROR" not in r
        results.append((payload["model_info"]["id"], "OK" if ok else r))
        print(payload["model_info"]["id"], "=>", "OK" if ok else r)

fails = [r for r in results if r[1] != "OK"]
print("SUMMARY:", f"{len(results)-len(fails)}/6 OK", "FAILS:", fails if fails else "none")
