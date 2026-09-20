#!/usr/bin/env python3
"""判 chatgpt-acct 叶子死活的**决定性**判据:真实推理走一遍生产路径。

    # 在 198 上
    python3 chatgpt-acct-leaf-real-inference.py 89 93 155
    # 在阿里云 226 上
    python3 chatgpt-acct-leaf-real-inference.py --site aliyun 122 124 125

判决三档,永不两档:
    SERVES   = HTTP 200,上游真出字
    DEAD     = 上游 auth/账号级拒绝(token_revoked / account_deactivated / ...)
    UNKNOWN  = 其余一切(含 token_expired、429、超时、网络抖动)

## 为什么必须是这把尺(2026-09-07 摘号时另外两把当场碎掉)

碎尺 1 —— **叶子 `/v1/models` 数 model 数**。63 个号全返 "12 models",里面
包括 09-06 已实证 `account_deactivated` 的号。12 models 只证明叶子把 config
读进内存了,与上游账号活不活零相关。

碎尺 2 —— **读 pod 内 `/chatgpt-auth/auth.json` 的 access_token 打
`/accounts/check/v4`,把 401 判死**。对 117/145/147/156/158 全部假阳:这五个
号同期 SpendLogs 里有 6k~9k 次成功。机制是**盘上 auth.json 是签发时刻的快照,
pod 早就用内存里 refresh 过的 token 在服务**,两者不同步。
⇒ 单凭 `token_revoked` 不足以判死;`token_expired` 更只能记 UNKNOWN。

## 四条不能少的实现细节(少一条就退化成假绿/假红)

1. 走 `/v1/responses` **不走** `/v1/chat/completions` —— 这台 router 上两条路
   对死腿行为不同(见 skill litellm-198-router-patch),生产走前者。
2. **每发 body 带唯一 nonce** —— 否则 LiteLLM 响应缓存直接回绿,不碰上游。
3. 带 **pool master key**(secret `chatgpt-pool-master-key`),不是母 router 的
   key —— 拿错 key,健康叶子也回 400 `No connected db.`。
4. `input` 必须是 list + `input_text`;给字符串上游回 400 `Input must be a
   list`。(副产品:上游**先判 auth 再判 payload**。)

READ-ONLY:只发推理请求,不改任何配置。
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
from concurrent.futures import ThreadPoolExecutor

SITES = {
    # site   -> (namespace, kubectl argv)
    "198": ("litellm-product", ["sudo", "k3s", "kubectl"]),
    "aliyun": ("carher", ["kubectl"]),
}

DEAD_MARKERS = (
    "token_revoked",
    "account_deactivated",
    "invalidated oauth",
    "unauthorized",
    "no healthy deployments",
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="198", choices=sorted(SITES))
    ap.add_argument("--model-hint", default="5.5",
                    help="挑哪个 model 打;默认挑名字含 5.5 的")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("accts", nargs="+", type=int)
    a = ap.parse_args()
    ns, kubectl = SITES[a.site]

    def sh(argv):
        r = subprocess.run(argv, capture_output=True, text=True)
        if r.returncode:
            sys.exit("FATAL " + r.stderr)
        return r.stdout

    pool_key = base64.b64decode(sh(
        kubectl + ["-n", ns, "get", "secret", "chatgpt-pool-master-key", "-o",
                   "jsonpath={.data.LITELLM_MASTER_KEY}"])).decode().strip()
    if not pool_key:
        sys.exit("FATAL: pool master key 空 —— 先 `get secret ... -o jsonpath={.data}` "
                 "确认字段名,别猜(实测字段是 LITELLM_MASTER_KEY 不是 master-key)")

    svc_ip = {}
    for s in json.loads(sh(kubectl + ["-n", ns, "get", "svc", "-o", "json"]))["items"]:
        m = re.fullmatch(r"chatgpt-acct-(\d+)", s["metadata"]["name"])
        if m:
            svc_ip[int(m.group(1))] = s["spec"]["clusterIP"]

    def leaf_models(n):
        r = urllib.request.Request("http://%s:4000/v1/models" % svc_ip[n],
                                   headers={"Authorization": "Bearer " + pool_key})
        with urllib.request.urlopen(r, timeout=20) as x:
            return [m["id"] for m in json.loads(x.read())["data"]]

    def probe(n):
        if n not in svc_ip:
            return n, "UNKNOWN", "no svc chatgpt-acct-%d in ns %s" % (n, ns)
        try:
            models = leaf_models(n)
        except Exception as e:
            return n, "UNKNOWN", "models: %s" % e
        pick = next((m for m in models if a.model_hint in m),
                    models[0] if models else None)
        if not pick:
            return n, "DEAD", "leaf loaded 0 models"
        nonce = "%d-%.6f" % (n, time.time())      # 唯一 body -> 绕开响应缓存
        body = json.dumps({
            "model": pick,
            "input": [{"role": "user", "content": [
                {"type": "input_text",
                 "text": "reply with the single word ok. probe id %s" % nonce}]}],
            "max_output_tokens": 16,
            "stream": False,
        }).encode()
        r = urllib.request.Request(
            "http://%s:4000/v1/responses" % svc_ip[n], data=body,
            headers={"Authorization": "Bearer " + pool_key,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=90) as x:
                j = json.loads(x.read())
            return n, "SERVES", "%s status=%s" % (pick, j.get("status"))
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf8", "replace")
            except Exception:
                pass
            flat = " ".join(raw.split())[:300]
            low = flat.lower()
            if any(s in low for s in DEAD_MARKERS):
                return n, "DEAD", "HTTP %s %s" % (e.code, flat)
            # 429 usage_limit_reached = 活着但配额耗尽,绝不摘号
            return n, "UNKNOWN", "HTTP %s %s" % (e.code, flat)
        except Exception as e:
            return n, "UNKNOWN", "%s %s" % (type(e).__name__, e)

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        res = sorted(ex.map(probe, a.accts))
    for n, v, d in res:
        print("acct-%-5s %-8s %s" % (n, v, d))
    print()
    for v in ("SERVES", "DEAD", "UNKNOWN"):
        print("%s=%s" % (v, ",".join(str(n) for n, vv, _ in res if vv == v)))
    print("\n摘号前:DEAD 还需 SpendLogs 真流量(按 model_id 归因,不是按 model)交叉确认;"
          "UNKNOWN 一律不动手。")


if __name__ == "__main__":
    main()
