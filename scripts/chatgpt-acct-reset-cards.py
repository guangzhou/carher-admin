#!/usr/bin/env python3
"""chatgpt-acct-reset-cards.py — 轮询 198 POOL 账号的官方 reset 卡数量(banked
rate-limit reset credits），输出 JSON {acct: {credits, plan, 5h, 7d, allowed, source}}。

数据源：live `chatgpt.com/backend-api/codex/usage` 的 rate_limit_reset_credits
.available_count（state.json 不含此字段）。

探测策略（每个 POOL 账号）：
  1. 有运行 pod → in-pod exec 探
  2. 无 pod → scale=1 wait_ready → 探 → 探完若原本 scale=0 则缩回 0
  内存守门：198 avail<MIN_AVAIL_GB 跳过 scale-up（标 source=skip_mem）

必须在 198 (AIYJY-litellm) 上跑（需 sudo k3s kubectl）。wrapper 走 jms。
用法：sudo python3 chatgpt-acct-reset-cards.py [--pool acct-1,acct-2] > cards.json
"""
from __future__ import annotations
import argparse, json, re, subprocess, sys, time

NS = "litellm-product"
MIN_AVAIL_GB = 3
WAIT_READY = 150

PROBE = r'''
import json, urllib.request, urllib.error
try:
    a=json.load(open("/chatgpt-auth/auth.json"))
    req=urllib.request.Request("https://chatgpt.com/backend-api/codex/usage",headers={
        "Authorization":"Bearer "+a["access_token"],
        "chatgpt-account-id":a.get("account_id",""),
        "Originator":"codex_cli_rs","OpenAI-Beta":"codex-1",
        "User-Agent":"codex_cli_rs/0.41.0 (Linux; x86_64)"})
    r=json.loads(urllib.request.urlopen(req,timeout=15).read())
    rl=r.get("rate_limit") or {}; pw=rl.get("primary_window") or {}
    c=(r.get("rate_limit_reset_credits") or {}).get("available_count")
    print(json.dumps({"s":"OK","credits":c,"plan":r.get("plan_type"),
        "p5h":pw.get("used_percent"),"allowed":rl.get("allowed")}))
except urllib.error.HTTPError as e:
    print(json.dumps({"s":"HTTP_%d"%e.code}))
except Exception as e:
    print(json.dumps({"s":"ERR","e":str(e)[:60]}))
'''


def k(*args, timeout=60):
    return subprocess.run(["sudo", "k3s", "kubectl", "-n", NS, *args],
                          capture_output=True, text=True, timeout=timeout)


def mem_avail_gb() -> int:
    try:
        out = subprocess.run(["free", "-g"], capture_output=True, text=True).stdout
        for ln in out.splitlines():
            if ln.startswith("Mem:"):
                return int(ln.split()[6])  # available
    except Exception:
        pass
    return 0


def pod_of(n: str) -> str | None:
    r = k("get", "pod", "-l", f"app=chatgpt-acct-{n}",
          "-o", "jsonpath={.items[0].metadata.name}")
    return r.stdout.strip() or None


def pod_ready(pod: str) -> bool:
    r = k("get", "pod", pod, "-o",
          "jsonpath={.status.containerStatuses[0].ready}")
    return r.stdout.strip() == "true"


def probe_pod(pod: str) -> dict:
    r = k("exec", "-i", pod, "--", "python3", "-c", PROBE, timeout=40)
    try:
        return json.loads(r.stdout.strip())
    except Exception:
        return {"s": "EXEC_ERR", "raw": (r.stdout or r.stderr)[:80]}


def replicas(n: str) -> int:
    r = k("get", "deploy", f"chatgpt-acct-{n}", "-o", "jsonpath={.spec.replicas}")
    try:
        return int(r.stdout.strip())
    except Exception:
        return -1


def scale(n: str, rep: int):
    k("scale", "deploy", f"chatgpt-acct-{n}", f"--replicas={rep}")


def wait_ready(n: str, secs: int) -> str | None:
    deadline = time.time() + secs
    while time.time() < deadline:
        pod = pod_of(n)
        if pod and pod_ready(pod):
            return pod
        time.sleep(5)
    return None


def collect(accts: list[str]) -> dict:
    out = {}
    for acct in accts:
        n = acct.split("-", 1)[1]
        pod = pod_of(n)
        if pod and pod_ready(pod):
            r = probe_pod(pod)
            out[acct] = {**r, "source": "pod"}
            print(f"{acct}: {out[acct]}", file=sys.stderr)
            continue
        # 无 pod / 未 ready → scale-up 探
        if mem_avail_gb() < MIN_AVAIL_GB:
            out[acct] = {"s": "SKIP_MEM", "source": "skip_mem"}
            print(f"{acct}: SKIP mem<{MIN_AVAIL_GB}G", file=sys.stderr)
            continue
        was = replicas(n)
        if was < 0:
            out[acct] = {"s": "NO_DEPLOY", "source": "no_deploy"}
            print(f"{acct}: NO_DEPLOY", file=sys.stderr)
            continue
        scale(n, 1)
        pod = wait_ready(n, WAIT_READY)
        if not pod:
            out[acct] = {"s": "NOT_READY", "source": "scale_up"}
            print(f"{acct}: NOT_READY", file=sys.stderr)
            if was == 0:
                scale(n, 0)
            continue
        r = probe_pod(pod)
        out[acct] = {**r, "source": "scale_up"}
        print(f"{acct}: {out[acct]}", file=sys.stderr)
        if was == 0:
            scale(n, 0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default="", help="逗号分隔 acct 名单;缺省读 stdin JSON list")
    args = ap.parse_args()
    if args.pool:
        accts = [a.strip() for a in args.pool.split(",") if a.strip()]
    else:
        accts = json.load(sys.stdin)
    accts = sorted(set(accts), key=lambda a: int(a.split("-")[1]))
    res = collect(accts)
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
