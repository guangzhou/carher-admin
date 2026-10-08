# 在 key-swap-proxy pod 里跑(它有 LITELLM_MASTER_KEY env 和集群网络)。只读。
# 白名单里的每个模型名,必须在**每一个** LiteLLM 副本上都注册了,并且真发一条能回 200。
# 只打 Service 会被负载均衡掩盖「某个副本还没同步」(10-07 /model/new 后真见过 1/4 副本落后)。
import os, urllib.request

H = {"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"], "Content-Type": "application/json"}
bad = 0
for ip in ARGS["ips"]:
    base = f"http://{ip}:4000"
    info = json.load(urllib.request.urlopen(urllib.request.Request(base + "/model/info", headers=H), timeout=60))
    names = {m["model_name"] for m in info["data"]}
    for m in ARGS["models"]:
        if m not in names:
            print(f"FAIL {ip} {m} 未注册"); bad += 1; continue
        body = {"model": m, "messages": [{"role": "user", "content": "reply exactly: nonce-6402"}], "max_tokens": 300}
        try:
            r = urllib.request.urlopen(urllib.request.Request(base + "/v1/chat/completions",
                                       data=json.dumps(body).encode(), headers=H), timeout=90)
            out = json.load(r)["choices"][0]["message"]["content"] or ""
            ok = "nonce-6402" in out
            print(("PASS" if ok else "FAIL"), ip, m, "->", r.headers.get("x-litellm-model-name"), repr(out[:40]))
            bad += not ok
        except Exception as e:
            print("FAIL", ip, m, e, getattr(e, "read", lambda: b"")()[:160]); bad += 1
print("PRECHECK", "PASS" if not bad else f"FAIL x{bad}")
