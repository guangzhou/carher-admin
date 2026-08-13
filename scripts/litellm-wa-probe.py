#!/usr/bin/env python3
"""litellm-wa-probe.py — WA 亲和/缓存/failover 探针（在 litellm-proxy pod 内跑）

沉淀自 2026-08-12/13 WA v3/v4 上线验证（memory:
project_198_wa_v3_session_affinity_2026_08_12 /
project_198_zerokey_session_affinity_hang_blackhole_2026_08_13）。

用法（先 kubectl cp 进 proxy pod，用 pod env 的 LITELLM_MASTER_KEY）:
  POD=$(kubectl -n litellm-product get pods -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')
  kubectl -n litellm-product cp scripts/litellm-wa-probe.py $POD:/tmp/wa-probe-$(date +%s).py -c litellm
  kubectl -n litellm-product exec $POD -c litellm -- python3 /tmp/wa-probe-<ts>.py <mode> <model> [args]
  # 用完删除（feedback_temp_probe_must_be_inside_model_gate_and_removed）

模式:
  session <model>            同 prompt_cache_key 两发 → 应同 model_id（session 钉台）
  spread  <model> [n=4]      n 个不同 pck → 应分散多台且各自稳定
  cache   <model>            ~1900 token 同长文同 pck 两发 → 看第二发 cached_tokens
                             （注意: 5.3-codex / codex-auto-review 类 workload 上游本身
                             只缓存固定前缀, cached≈0 不代表路由失效）
  pin     <model> <dep_id> <redis_host>
                             把新 session 的 pin 种到指定 deployment（可指死成员）
                             再发请求 → 验证 re-pick 迁移（黑洞回归测试）

判读: 配合 proxy 日志 grep "WeightedAffinityRouter: (HIT|MISS|fail-marked|re-picking)"。
"""
import hashlib
import json
import os
import sys
import time
import urllib.request

KEY = os.environ["LITELLM_MASTER_KEY"]
BASE = os.environ.get("WA_PROBE_BASE", "http://127.0.0.1:4000")


def call(model, pck, text="Reply with the single word: ok", timeout=180):
    body = {"model": model, "input": text, "stream": True,
            "max_output_tokens": 32, "prompt_cache_key": pck}
    req = urllib.request.Request(
        f"{BASE}/v1/responses", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json",
                 "Accept": "text/event-stream"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            mid = r.headers.get("x-litellm-model-id")
            usage = None
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                if obj.get("type") in ("response.completed", "response.incomplete"):
                    usage = (obj.get("response") or {}).get("usage")
            return {"status": r.status, "model_id": mid, "usage": usage,
                    "took": round(time.time() - t0, 1)}
    except Exception as e:
        return {"status": f"ERR:{type(e).__name__}", "model_id": None,
                "usage": None, "took": round(time.time() - t0, 1)}


def main():
    mode, model = sys.argv[1], sys.argv[2]
    nonce = str(time.time())
    if mode == "session":
        pck = f"wa-probe-sess-{nonce}"
        a = call(model, pck)
        time.sleep(3)
        b = call(model, pck)
        same = a["model_id"] == b["model_id"] and a["model_id"] is not None
        print(f"call1={a}\ncall2={b}\nSESSION_STICKY={'PASS' if same else 'FAIL'}")
    elif mode == "spread":
        n = int(sys.argv[3]) if len(sys.argv) > 3 else 4
        ids = []
        for i in range(n):
            r = call(model, f"wa-probe-spread-{i}-{nonce}")
            ids.append(r["model_id"])
            print(f"spread-{i}={r}")
            time.sleep(1)
        print(f"distinct={len(set(x for x in ids if x))}/{n}")
    elif mode == "cache":
        para = "CarHer infra cache probe text segment. " * 8 + "\n"
        text = f"probe nonce={nonce}\n" + para * 40 + "\nReply with the single word: ok"
        pck = f"wa-probe-cache-{nonce}"
        a = call(model, pck, text)
        time.sleep(8)
        b = call(model, pck, text)
        for tag, r in (("call1", a), ("call2", b)):
            det = ((r["usage"] or {}).get("input_tokens_details") or {})
            print(f"{tag} model_id={r['model_id']} cached_tokens={det.get('cached_tokens')} usage={json.dumps(r['usage'])}")
    elif mode == "pin":
        dep_id, redis_host = sys.argv[3], sys.argv[4]
        import redis
        r = redis.Redis(host=redis_host, port=6379, socket_timeout=5)
        user_hash = None
        for k in r.keys(f"weighted_affinity:v2:{model}:*"):
            ks = k.decode()
            if ":s:" in ks:
                user_hash = ks.split(":")[3]  # v2:{group}:{user}:s:{fp} → parts[3]
                break
        assert user_hash and user_hash != "s", "先跑一次 session 模式生成现有 pin 以提取 user hash"
        pck = f"wa-probe-pin-{nonce}"
        fp = hashlib.sha256(pck.encode()).hexdigest()[:32]
        pin_key = f"weighted_affinity:v2:{model}:{user_hash}:s:{fp}"
        r.set(pin_key, json.dumps({"model_id": dep_id}), ex=300)
        print(f"pin planted -> {dep_id}")
        res = call(model, pck)
        print(f"result={res}")
        print(f"final_pin={r.get(pin_key)}")
        print("PASS 判据: status=200 且 final_pin != 种下的 dep_id（已迁移）或 model_id != dep_id")
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
