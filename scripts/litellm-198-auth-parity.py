#!/usr/bin/env python3
"""两条车道鉴权并排：真 key 的每种 (models, aliases) 形状各克隆一把短命 key，对白名单里每个名字 + 一个白名单外名字，
两边各打一发 1-token 请求，比「403 与否」。

为什么（2026-10-17 v1.105.1 灰度）：上游 #43049 让 key 别名收窄鉴权，1323/2116 把 key 会对白名单内的名字 403；
24 组×3 协议用 master key 测，完全看不出来，只有克隆真 key 形状才抓得到。

用法（198 上）：LITELLM_MASTER_KEY=... python3 litellm-198-auth-parity.py <A_pod_ip> <B_pod_ip> [--max-shapes 40]
临时 key 15 分钟过期、预算 $0.05，跑完逐把删除；退出码 0 = 0 单边。
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
import os
import random
import subprocess
import sys
import urllib.error
import urllib.request

MK = os.environ["LITELLM_MASTER_KEY"]
ADMIN = os.environ.get("LITELLM_BASE", "http://127.0.0.1:30402")


def call(base, path, body=None, key=MK, timeout=20):
    r = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                               headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                               method="POST" if body is not None else "GET")
    try:
        x = urllib.request.urlopen(r, timeout=timeout)
        return x.status, x.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return -1, repr(e)


def shapes():
    sql = ("select distinct on (array_to_string(models,','), aliases::text) split_part(key_alias,'-',1), "
           "array_to_string(models,','), coalesce(aliases::text,'{}') from \"LiteLLM_VerificationToken\" "
           "where (blocked is null or blocked=false) and (expires is null or expires>now()) and key_alias is not null "
           "and team_id is null order by array_to_string(models,','), aliases::text, key_alias;")
    out = subprocess.run(["kubectl", "-n", "litellm-product", "exec", "-i", "litellm-db-0", "--", "psql", "-U", "litellm",
                          "-d", "litellm", "-At", "-F", "|"], input=sql, capture_output=True, text=True, timeout=120).stdout
    res = []
    for line in out.splitlines():
        p = line.split("|")
        if len(p) < 3:
            continue
        m = [x for x in p[1].split(",") if x]
        if not m or "all-team-models" in m:
            continue
        res.append((p[0], m, json.loads(p[2] or "{}")))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a"); ap.add_argument("b")
    ap.add_argument("--max-shapes", type=int, default=40)
    ap.add_argument("--max-names", type=int, default=30)
    a = ap.parse_args()
    A, B = f"http://{a.a}:4000", f"http://{a.b}:4000"
    sh = shapes()
    random.seed(7)
    if len(sh) > a.max_shapes:
        sh = random.sample(sh, a.max_shapes)
    print(f"shapes {len(sh)}", flush=True)
    diff = checked = 0
    cnt = collections.Counter()
    for pref, m, al in sh:
        st, b = call(ADMIN, "/key/generate", {"key_alias": f"tmp-authparity-{random.randint(1, 10**9)}", "models": m,
                                              "aliases": al, "duration": "15m", "max_budget": 0.05}, timeout=60)
        if st != 200:
            print(f"  ERROR generate {st}"); continue
        k = json.loads(b)["key"]
        try:
            names = list(dict.fromkeys(m + list(al)))[:a.max_names] + ["o3-pro-not-allowed"]

            def both(n):
                body = {"model": n, "max_tokens": 1, "messages": [{"role": "user", "content": "1"}]}
                return n, call(A, "/v1/chat/completions", body, key=k)[0], call(B, "/v1/chat/completions", body, key=k)[0]
            with cf.ThreadPoolExecutor(6) as ex:
                for n, x, y in ex.map(both, names):
                    checked += 1
                    cnt[(x == 403, y == 403)] += 1
                    if (x == 403) != (y == 403):
                        diff += 1
                        print(f"  AUTH DIFF {pref} {n} A={x} B={y}")
        finally:
            call(ADMIN, "/key/delete", {"keys": [k]}, timeout=60)
    print(f"names checked {checked} auth one-sided {diff} (A403,B403) {dict(cnt)}")
    sys.exit(0 if diff == 0 and checked else 1)


if __name__ == "__main__":
    main()
