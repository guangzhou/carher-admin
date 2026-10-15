#!/usr/bin/env python3
"""量具：Kiro MCP web_search 调用是否消耗账号 credit（skill litellm-198-web-search §3）。

只读 + 小量 MCP 调用，用一个**闲置号**（successCount=0、不在役）做，避免生产流量混入。
复用 scripts/kiro-probe.py 的凭据/出口/UA 逻辑（同一出口、同一设备指纹）。

⛔ 不刷新 token：social 刷新会轮转 refreshToken，线上那份当场变死（kiro-probe.py 注释）。
   盘上 accessToken 过期就直接退出，不重试。

用法（本机跑，经 ssh 198 读线上凭据与出口）：
  python3 scripts/kiro-mcp-search-quota-probe.py read  <id> [<id>...]   # getUsageLimits 现读（不走 admin 的 300s 缓存）
  python3 scripts/kiro-mcp-search-quota-probe.py search <id> <n>        # 对该号打 n 次 MCP web_search

判据（10-15 跑过一次）：挑 successCount=0 的闲置号 A 搜 n 次、闲置号 B 不动当对照，
立刻读一次、等 ≥10min 再读一次（Kiro 计量滞后几分钟）。A 不涨 ⇒ 不扣。
⚠️ 还缺阳性对照：在 A 上打一发普通对话，证明这个读数确实会涨，才算定论。
"""
import importlib.util
import json
import pathlib
import random
import string
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kp", ROOT / "scripts" / "kiro-probe.py")
kp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kp)

REGION = "us-east-1"
QUERIES = ["2026 Nobel Prize in Physics winner", "IceCube Neutrino Observatory news",
           "Francis Halzen biography", "2026 Nobel Prize in Chemistry", "Kiro IDE release notes",
           "AWS us-east-1 status", "Rust reqwest 0.12 changelog", "LiteLLM latest release",
           "Bitcoin price today", "Tokyo weather this week"]


def get_cred(pool, cid):
    hit = [c for c in pool if c.get("id") == cid]
    if len(hit) != 1:
        sys.exit(f"id={cid} 命中 {len(hit)} 条")
    return hit[0]


def no_refresh(*_a, **_k):
    raise SystemExit("🔴 accessToken 已失效需要刷新 —— 本探针拒绝刷新（social 会轮转 refreshToken），中止")


def read(pool, opener, cid):
    c = get_cred(pool, cid)
    r = kp.call("getUsageLimits", "origin=AI_EDITOR&resourceType=AGENTIC_REQUEST", c, REGION, opener)
    b = (r.get("usageBreakdownList") or [{}])[0]
    return b.get("currentUsageWithPrecision"), b.get("usageLimitWithPrecision")


def mcp_search(c, opener, query):
    mid = kp.machine_id_of(c)
    ua = f"KiroIDE-{kp.KIRO_IDE_VER}-{mid}"
    rid = "web_search_tooluse_%s_%d_%s" % (
        "".join(random.choices(string.ascii_letters + string.digits, k=22)),
        int(time.time() * 1000), "".join(random.choices(string.ascii_lowercase + string.digits, k=8)))
    body = json.dumps({"id": rid, "jsonrpc": "2.0", "method": "tools/call",
                       "params": {"name": "web_search", "arguments": {"query": query}}}).encode()
    h = {"content-type": "application/json", "Connection": "close",
         "Authorization": "Bearer " + c["accessToken"],
         "x-amz-user-agent": f"aws-sdk-js/1.0.34 {ua}",
         "user-agent": f"aws-sdk-js/1.0.34 ua/2.1 os/darwin#24.6.0 lang/js md/nodejs#22.22.0 "
                       f"api/codewhispererstreaming#1.0.34 m/E {ua}",
         "host": f"q.{REGION}.amazonaws.com",
         "amz-sdk-invocation-id": str(uuid.uuid4()), "amz-sdk-request": "attempt=1; max=3"}
    if c.get("profileArn"):
        h["x-amzn-kiro-profile-arn"] = c["profileArn"]
    req = urllib.request.Request(f"https://q.{REGION}.amazonaws.com/mcp", data=body, headers=h)
    t = time.time()
    try:
        raw = opener.open(req, timeout=60).read().decode()
    except urllib.error.HTTPError as e:
        return e.code, time.time() - t, e.read().decode("utf8", "replace")[:200]
    d = json.loads(raw)
    n = None
    try:
        n = len(json.loads(d["result"]["content"][0]["text"]).get("results", []))
    except Exception:
        pass
    return 200, time.time() - t, f"results={n}"


def main():
    kp.refresh_token = no_refresh          # 硬禁刷新
    pool = kp.load_pool(None)
    opener = kp.build_opener(kp.pod_proxy())
    cmd = sys.argv[1]
    if cmd == "read":
        for cid in map(int, sys.argv[2:]):
            used, cap = read(pool, opener, cid)
            print(f"{time.strftime('%H:%M:%S')} id={cid} used={used} cap={cap}")
    elif cmd == "search":
        cid, n = int(sys.argv[2]), int(sys.argv[3])
        c = get_cred(pool, cid)
        for i in range(n):
            code, dt, info = mcp_search(c, opener, QUERIES[i % len(QUERIES)])
            print(f"{time.strftime('%H:%M:%S')} #{i+1} http={code} {dt:.2f}s {info}")
            if code != 200:
                sys.exit("非 200，中止")


if __name__ == "__main__":
    main()
