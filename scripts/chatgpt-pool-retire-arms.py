#!/usr/bin/env python3
"""从 198 母 router 摘掉已确认死号的 arms(只动 router 成员关系)。

    python3 chatgpt-pool-retire-arms.py 89 93 95            # dry-run
    python3 chatgpt-pool-retire-arms.py --apply 89 93 95

**不碰** pod / PVC / auth.json / 账号本身 —— 号重烧 OAuth 后可 `/model/new`
加回来。这是刻意保留的可逆步骤。

## 三条纪律(2026-09-06/07 两轮摘号实证)

1. **只能走 `POST /model/delete`**。`LiteLLM_ProxyModelTable` 的 api_base 是
   加密存的,拿 acct 名去 DB 里 regex 查必定 0 行,别据此以为"表里没有"。
2. **`/model/delete` 可能返回 400 但其实已经成功** —— 判据是 readback,
   永远不是状态码。
3. **删完靠各 pod 轮询收敛**,中间态读到残留是收敛滞后不是失败;要轮询到稳定。
   收敛后必须**逐 pod + NodePort 复核**(共用 CM/DB 的多副本会藏分歧)。

## 归因只能按 unique `model_info.id`

`/model/info` 是 alias 展开过的,行数远多于真实 deployment 数(198 上 26 行
对应 8 个 deployment)。按行数算"每号几条"会算错,必须按 `model_info.id` 去重。

## ⚠️ 这个脚本只对 198 有效

阿里云(ns `carher`)的 arms 来自 **ConfigMap 不是 DB**,`/model/delete` 会返
400 `Model with id=... not found in db` 且什么都没删 —— 那边用
`aliyun-cm-drop-acct.py` 改 CM + rollout。
"""
import argparse
import base64
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

NS = "litellm-product"
K = ["sudo", "k3s", "kubectl"]
PRD = "http://127.0.0.1:30402/pro"


def sh(argv):
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode:
        sys.exit("FATAL " + r.stderr)
    return r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--poll-rounds", type=int, default=20)
    ap.add_argument("accts", nargs="+", type=int)
    a = ap.parse_args()
    targets = sorted(set(a.accts))

    mk = base64.b64decode(sh(K + ["-n", NS, "get", "secret", "litellm-secrets", "-o",
                                  "jsonpath={.data.LITELLM_MASTER_KEY}"])).decode().strip()
    hdr = {"Authorization": "Bearer " + mk, "Content-Type": "application/json"}

    def arms():
        """acct -> {unique model_info.id} 取自一次新鲜 /model/info。"""
        req = urllib.request.Request(PRD + "/model/info", headers=hdr)
        data = json.loads(urllib.request.urlopen(req, timeout=60).read())["data"]
        out = {}
        for e in data:
            b = (e.get("litellm_params") or {}).get("api_base") or ""
            m = re.search(r"chatgpt-acct-(\d+)", b)
            mid = (e.get("model_info") or {}).get("id")
            if m and mid:
                out.setdefault(int(m.group(1)), set()).add(mid)
        return out

    before = arms()
    print("router now holds %d accts / %d unique arms" % (
        len(before), sum(len(v) for v in before.values())))

    plan = {n: sorted(before.get(n, set())) for n in targets}
    total = sum(len(v) for v in plan.values())
    for n in targets:
        print("  acct-%-5s %2d arms  %s" % (n, len(plan[n]), ",".join(plan[n])[:150]))
    print("TOTAL ARMS TO DELETE: %d" % total)
    missing = [n for n in targets if not plan[n]]
    if missing:
        print("!! 不在 router 里(已经没了): %s" % missing)
    if not a.apply:
        print("\nDRY-RUN。确认判死证据已收敛(真实推理 + SpendLogs 真流量)再加 --apply。")
        return

    print("\n--- deleting ---")
    for n in targets:
        for mid in plan[n]:
            body = json.dumps({"id": mid}).encode()
            req = urllib.request.Request(PRD + "/model/delete", data=body, headers=hdr)
            try:
                with urllib.request.urlopen(req, timeout=60) as x:
                    code = x.status
            except urllib.error.HTTPError as e:
                code = e.code          # 400 在这里不代表失败
            except Exception as e:
                code = "ERR %s" % e
            print("  acct-%-5s %s -> http %s" % (n, mid, code))

    print("\n--- readback(轮询到收敛;状态码不是证据) ---")
    stable, last = 0, None
    for i in range(a.poll_rounds):
        time.sleep(6)
        cur = arms()
        left = {n: len(cur.get(n, set())) for n in targets if cur.get(n)}
        print("  t+%3ds  remaining target arms: %s  (accts in router: %d)" % (
            (i + 1) * 6, left or "none", len(cur)))
        stable = stable + 1 if left == last else 0
        last = left
        if not left and stable >= 1:
            print("\nCONVERGED: 目标 arms 全部消失。")
            break
    else:
        print("\n!! 未收敛到空; remaining=%s" % last)

    after = arms()
    print("\nrouter after: %d accts / %d unique arms" % (
        len(after), sum(len(v) for v in after.values())))
    print("accts remaining: %s" % sorted(after))
    print("\n下一步必做:①逐 pod + NodePort 复核零分歧 "
          "②30min SpendLogs 真流量回归 ③确认 188 governor 没把号加回来"
          "(它只补 ONLINE 号,paused/manual_offline 的不会回)。")


if __name__ == "__main__":
    main()
