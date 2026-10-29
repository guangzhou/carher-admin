#!/usr/bin/env python3
"""两条车道并排量「鉴权 + 网关自身」开销：用 N 把临时 key 轮流打 /v1/models（不打上游、不花钱），比延迟分位数。

198 root 跑：
    python3 litellm-198-auth-latency-ab.py --lane-a litellm-proxy-gray --lane-b litellm-proxy-gray-ref ... \
        [--keys 400] [--rounds 3] [--concurrency 16]

为什么用 /v1/models：它走完整鉴权（key → team → user → end user 缓存 / DB），但不经过路由和上游，
延迟里只剩「鉴权缓存命中率 × DB 往返」和网关本身。key 数故意 > 200（默认鉴权缓存大小），
第二轮起如果缓存装得下应全部命中、p50 明显下降；装不下则每轮都回源 DB。
key 全部 15 分钟过期、跑完逐把删除，明文只在内存里。
"""
import argparse
import base64
import concurrent.futures as cf
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
import uuid

NS = "litellm-product"
ENV = dict(os.environ, KUBECONFIG="/etc/rancher/k3s/k3s.yaml")


def kc(*a):
    r = subprocess.run(["kubectl", "-n", NS, *a], capture_output=True, text=True, env=ENV)
    if r.returncode:
        sys.exit(f"kubectl {' '.join(a[:3])}: {r.stderr[:300]}")
    return r.stdout


def master_key():
    raw = kc("get", "secret", "litellm-secrets-r2", "-o", "jsonpath={.data.LITELLM_MASTER_KEY}")
    return base64.b64decode(raw).decode()


def pod_ips(app):
    d = json.loads(kc("get", "po", "-l", f"app={app}", "-o", "json"))
    return [i["status"]["podIP"] for i in d["items"] if i["status"].get("phase") == "Running" and not i["metadata"].get("deletionTimestamp")]


def call(url, key, body=None, timeout=30):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(); st = r.status
    except urllib.error.HTTPError as e:
        st = e.code
    except Exception:
        st = 0
    return st, (time.perf_counter() - t0) * 1000


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane-a", required=True)
    ap.add_argument("--lane-b", required=True)
    ap.add_argument("--keys", type=int, default=400)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--model", default="deepseek-v4.1-flash", help="临时 key 白名单里放的模型名（不会被调用）")
    a = ap.parse_args()
    mk = master_key()
    admin = f"http://{pod_ips(a.lane_b)[0]}:4000"
    keys = []
    try:
        with cf.ThreadPoolExecutor(8) as ex:
            def gen(_):
                st, _ = 0, 0
                req = urllib.request.Request(f"{admin}/key/generate", method="POST", data=json.dumps(
                    {"models": [a.model], "duration": "15m", "max_budget": 0.01,
                     "key_alias": f"auth-ab-{uuid.uuid4().hex[:8]}", "metadata": {"purpose": "auth latency A/B probe"}}).encode(),
                    headers={"Authorization": f"Bearer {mk}", "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read())["key"]
            keys = list(ex.map(gen, range(a.keys)))
        print(f"keys generated: {len(keys)}")
        for lane in (a.lane_a, a.lane_b):
            ips = pod_ips(lane)
            for rnd in range(1, a.rounds + 1):
                lat, bad = [], 0
                jobs = [(f"http://{ips[i % len(ips)]}:4000/v1/models", k) for i, k in enumerate(keys)]
                with cf.ThreadPoolExecutor(a.concurrency) as ex:
                    for st, ms in ex.map(lambda j: call(*j), jobs):
                        lat.append(ms); bad += st != 200
                print(f"{lane:28s} round{rnd} n={len(lat)} non200={bad} p50={pct(lat, .5):6.1f}ms p90={pct(lat, .9):6.1f}ms "
                      f"p99={pct(lat, .99):6.1f}ms mean={statistics.mean(lat):6.1f}ms")
    finally:
        if keys:
            for i in range(0, len(keys), 50):
                try:
                    call(f"{admin}/key/delete", mk, {"keys": keys[i:i + 50]})
                except Exception as e:
                    print(f"WARN delete batch {i}: {type(e).__name__}", file=sys.stderr)
            print(f"keys deleted: {len(keys)}")


if __name__ == "__main__":
    sys.exit(main())
