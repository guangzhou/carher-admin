"""cursor-web-fc-pool-* 入池验收 harness（2026-08-24 首次组池验收 4/4 PASS 的固化版）。

在 198 litellm-proxy pod 内跑（依赖 LITELLM_MASTER_KEY env）：
  base64 < pool_accept.py | ssh cltx@10.68.13.198 'export KUBECONFIG=/home/cltx/.kube/config; \
    kubectl -n litellm-product exec -i <proxy-pod> -- sh -c "base64 -d > /tmp/pa.py && python3 /tmp/pa.py [model] [n]"'

做什么：/key/generate 临时 scoped key（1h 过期兜底）→ 打 N 次（默认 4）带唯一暗号的流式
/v1/responses → 断言 200 + 暗号逐字回显 → /key/delete 自动清理。
验收纪律（假绿③）：必须走这种临时真 key，master key 绕过 per-key gate 必假绿。
跑完还差两步人工（脚本替不了）：
  1. proxy 日志 grep -i "weightedaffinity.*pool" —— 看 MISS→HIT 钉同一 deployment（key 级黏性）；
  2. 各 lane pod 日志 grep 暗号（--tail=-1！）——确认流量真落到目标 pod（防"注册了没人路由到"）。
"""
import os, json, sys, time, urllib.request, urllib.error

MK = os.environ["LITELLM_MASTER_KEY"]
BASE = "http://localhost:4000"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "cursor-web-fc-pool-terra"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 4
RUN = time.strftime("%H%M%S")

def post(path, payload, key=None, raw=False, timeout=180):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + (key or MK), "Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    return resp if raw else json.load(resp)

tk = post("/key/generate", {"models": [MODEL], "key_alias": f"tmp-poolchk-{RUN}", "duration": "1h"})["key"]
print(f"TEMPKEY {tk[:12]}... model={MODEL} n={N}")

passed = 0
try:
    for i in range(1, N + 1):
        marker = f"POOLCHK{RUN}-{i}"
        body = {"model": MODEL, "stream": True,
                "input": f"Reply with exactly this string and nothing else: {marker}"}
        t0 = time.time()
        try:
            resp = post("/v1/responses", body, key=tk, raw=True)
            text = ""
            for line in resp:
                line = line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"): continue
                try: ev = json.loads(line[5:].strip())
                except Exception: continue
                if ev.get("type") == "response.output_text.delta":
                    text += ev.get("delta", "")
                if ev.get("type") in ("response.completed", "response.failed"):
                    break
            ok = marker in text
            passed += ok
            print(f"req{i}: HTTP {resp.status} {time.time()-t0:.1f}s echo={'YES' if ok else 'NO'} text={text[:80]!r}")
        except Exception as e:
            print(f"req{i}: FAIL {e}")
finally:
    r = post("/key/delete", {"keys": [tk]})
    print("TEMPKEY deleted:", "OK" if r else r)

print(f"RESULT: {passed}/{N} PASS — 别忘了两步人工：proxy WA 日志黏性链 + lane pod grep POOLCHK{RUN}")
sys.exit(0 if passed == N else 1)
