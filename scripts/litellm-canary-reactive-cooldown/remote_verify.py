#!/usr/bin/env python3
# Runs on 198 (AIYJY-litellm). Driven by scripts/litellm-canary-reactive-cooldown-verify.py.
import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

NS = "litellm-product"
SVC = "litellm-proxy-canary"
CANARY_MASTER_SECRET = "litellm-canary-master-key"
CANARY_DEPLOY_IDS = ["chatgpt-acct-canary-49", "chatgpt-acct-canary-68"]
PROD_REAL_DEPLOY_IDS = ["chatgpt-acct-49", "chatgpt-acct-68"]
KEYS_FILE = "/root/litellm-canary/keys/sk-canary-rc-keys.txt"
LOG_DIR = "/root/litellm-canary/verify"


def sh(cmd, check=True):
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def sh_ok(cmd):
    return sh(cmd, check=False)


def stamp(msg):
    print("[" + time.strftime("%H:%M:%S") + "] " + msg, flush=True)


def get_canary_master_key():
    r = sh(["kubectl", "-n", NS, "get", "secret", CANARY_MASTER_SECRET,
            "-o", "jsonpath={.data.LITELLM_MASTER_KEY}"])
    return base64.b64decode(r.stdout).decode()


def load_test_keys():
    keys = {}
    with open(KEYS_FILE) as f:
        for line in f:
            line = line.strip()
            if "=" not in line:
                continue
            alias, key = line.split("=", 1)
            keys[alias] = key
    return keys


# All requests run *from inside* a prod proxy pod (it has python3 + cluster DNS).
def exec_in_proxy(script_path, *args):
    pod = sh(["kubectl", "-n", NS, "get", "pod", "-l", "app=litellm-proxy",
              "-o", "jsonpath={.items[0].metadata.name}"]).stdout.strip()
    return sh(["kubectl", "-n", NS, "exec", pod, "-c", "litellm", "--",
               "python3", script_path] + list(args))


def upload_helper(name, body):
    pod = sh(["kubectl", "-n", NS, "get", "pod", "-l", "app=litellm-proxy",
              "-o", "jsonpath={.items[0].metadata.name}"]).stdout.strip()
    local = "/tmp/" + name
    with open(local, "w") as f:
        f.write(body)
    sh(["kubectl", "-n", NS, "cp", local, pod + ":/tmp/" + name, "-c", "litellm"])
    return "/tmp/" + name


CALL_HELPER = r"""
import json, sys, urllib.request, urllib.error
svc, ns, key, dep = sys.argv[1:5]
body = {
    "model": "chatgpt-canary-gpt-5.5",
    "input": [{"role":"user","content":[{"type":"input_text","text":"ping"}]}],
    "max_output_tokens": 16,
    "store": False,
}
headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
if dep:
    headers["X-Litellm-Specific-Deployment"] = dep
req = urllib.request.Request(
    f"http://{svc}.{ns}.svc.cluster.local:4000/v1/responses",
    method="POST", headers=headers, data=json.dumps(body).encode(),
)
out = {"call_dep_pin": dep}
try:
    r = urllib.request.urlopen(req, timeout=90)
    out["http"] = r.status
    out["headers"] = {h: v for h, v in r.getheaders()
                      if h.lower().startswith("x-litellm") or h.lower() == "retry-after"}
    data = r.read().decode()
    try:
        d = json.loads(data)
        out["status"] = d.get("status")
        out["usage"] = d.get("usage")
        out["rate_limit"] = d.get("rate_limit") or d.get("response_metadata", {}).get("rate_limit")
    except Exception:
        out["body_head"] = data[:300]
except urllib.error.HTTPError as e:
    out["http"] = e.code
    out["headers"] = {h: v for h, v in e.getheaders()
                      if h.lower().startswith("x-litellm") or h.lower() == "retry-after"}
    out["err_body"] = e.read().decode()[:600]
except Exception as e:
    out["err"] = type(e).__name__ + ": " + str(e)[:300]
print(json.dumps(out))
"""


def make_call(key, dep=""):
    helper = upload_helper("_canary_call.py", CALL_HELPER)
    r = exec_in_proxy(helper, SVC, NS, key, dep)
    if r.returncode != 0:
        return {"err": "exec failed: " + r.stderr[-300:]}
    try:
        return json.loads(r.stdout.strip())
    except Exception:
        return {"err": "parse failed", "raw": r.stdout[-300:]}


def redis_cooldown_keys(pattern):
    r = sh_ok(["kubectl", "-n", NS, "exec", "litellm-redis-0", "--",
               "redis-cli", "--scan", "--pattern", pattern])
    return sorted(line.strip() for line in r.stdout.splitlines() if line.strip())


def assert_eq(label, got, want):
    ok = got == want
    mark = "PASS" if ok else "FAIL"
    print("  [" + mark + "] " + label + ": got=" + repr(got) + " want=" + repr(want))
    return ok


def assert_true(label, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print("  [" + mark + "] " + label + (" — " + detail if detail else ""))
    return ok


def tc_a():
    stamp("=== TC-A: /v1/model/info on canary returns 2 chatgpt-canary entries ===")
    mk = get_canary_master_key()
    pod = sh(["kubectl", "-n", NS, "get", "pod", "-l", "app=litellm-proxy",
              "-o", "jsonpath={.items[0].metadata.name}"]).stdout.strip()
    h = upload_helper("_canary_info.py", r"""
import json, sys, urllib.request
svc, ns, mk = sys.argv[1:4]
req = urllib.request.Request(
    f"http://{svc}.{ns}.svc.cluster.local:4000/v1/model/info",
    headers={"Authorization": f"Bearer {mk}"})
d = json.load(urllib.request.urlopen(req, timeout=10))
print(json.dumps({"entries": [
    {"name": e.get("model_name"),
     "id": e.get("model_info", {}).get("id"),
     "model": e.get("litellm_params", {}).get("model"),
     "api_base": e.get("litellm_params", {}).get("api_base"),
    } for e in d.get("data", [])
]}))
""")
    r = exec_in_proxy(h, SVC, NS, mk)
    data = json.loads(r.stdout.strip())
    entries = data["entries"]
    ok = True
    ok &= assert_eq("entries count", len(entries), 2)
    names = sorted(e["name"] for e in entries)
    ok &= assert_eq("model_name set", names, ["chatgpt-canary-gpt-5.5"] * 2)
    ids = sorted(e["id"] for e in entries)
    ok &= assert_eq("model_info.id set", ids, sorted(CANARY_DEPLOY_IDS))
    models = sorted(e["model"] for e in entries)
    ok &= assert_eq("litellm_params.model uses openai/chatgpt-gpt-5.5",
                    models, ["openai/chatgpt-gpt-5.5"] * 2)
    return ok


def tc_h1():
    stamp("=== TC-H1: 5 happy-path calls (unpinned, sticky to whichever) ===")
    keys = load_test_keys()
    key = keys["sk-canary-rc-001"]
    ok_count = 0
    deps = []
    for i in range(5):
        r = make_call(key)
        if r.get("http") == 200:
            ok_count += 1
            deps.append(r["headers"].get("x-litellm-model-id"))
        else:
            print("    iter", i, "http=", r.get("http"), "err=", str(r)[:200])
    print("  routed deployments:", deps)
    return assert_eq("5/5 200", ok_count, 5)


def tc_e1_real():
    stamp("=== TC-E1-real: pin to each canary deploy id, capture response shape ===")
    keys = load_test_keys()
    key = keys["sk-canary-rc-001"]
    results = []
    for dep in CANARY_DEPLOY_IDS:
        # NOTE: X-Litellm-Specific-Deployment 在 v1.89.4 实测 unpinned 路由仍按 sticky
        # 选 deployment — Plan B 升级版用 model_info.id 隔离已经够；这里探测形态主要
        # 是为了看真撞顶 acct 上游返回是 200+空 / 200+used_percent>=100 / 429 三种之一
        r = make_call(key, dep=dep)
        results.append((dep, r))
        print("  pin=", dep, "→ http=", r.get("http"),
              "actual_id=", r.get("headers", {}).get("x-litellm-model-id"),
              "status=", r.get("status"),
              "usage_keys=", list((r.get("usage") or {}).keys())[:5],
              "rate_limit_keys=", list((r.get("rate_limit") or {}).keys())[:5])
    # Pass criterion: 不卡 5xx — 即上游能服务（happy 形态）
    ok = all(r.get("http") == 200 for _, r in results)
    return assert_true("both deps reachable (200)", ok)


def tc_prod_pollution():
    stamp("=== TC-prod-pollution: prod-shape Redis cooldown keys unchanged ===")
    # Before list
    pol_before = redis_cooldown_keys("deployment:chatgpt-acct-*:cooldown")
    canary_before = [k for k in pol_before if "-canary-" in k]
    real_before = [k for k in pol_before if "-canary-" not in k]
    print("  canary cd before:", canary_before)
    print("  real-acct cd before:", real_before)

    # Trigger some traffic
    keys = load_test_keys()
    key = keys["sk-canary-rc-002"]
    for _ in range(3):
        make_call(key)

    pol_after = redis_cooldown_keys("deployment:chatgpt-acct-*:cooldown")
    canary_after = [k for k in pol_after if "-canary-" in k]
    real_after = [k for k in pol_after if "-canary-" not in k]
    print("  canary cd after:", canary_after)
    print("  real-acct cd after:", real_after)

    return assert_eq("prod real-acct cooldown set unchanged",
                     real_after, real_before)


TC_REGISTRY = {
    "tc-a": tc_a,
    "tc-h1": tc_h1,
    "tc-e1-real": tc_e1_real,
    "tc-prod-pollution": tc_prod_pollution,
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tc", default="all")
    args = p.parse_args()
    os.makedirs(LOG_DIR, exist_ok=True)

    if args.tc == "all":
        names = list(TC_REGISTRY.keys())
    else:
        names = [n.strip() for n in args.tc.split(",") if n.strip()]

    results = {}
    for name in names:
        fn = TC_REGISTRY.get(name)
        if not fn:
            print("UNKNOWN TC:", name)
            results[name] = False
            continue
        try:
            results[name] = fn()
        except Exception as e:
            print("EXC in", name, ":", type(e).__name__, str(e)[:400])
            results[name] = False
        print()

    stamp("=== SUMMARY ===")
    for name, ok in results.items():
        print(" ", "PASS" if ok else "FAIL", name)
    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
