#!/usr/bin/env python3
"""验收「6 个 grok 名写进 cursor key 白名单」这一步。

判据纪律（踩过的坑都在这）：
- **禁用 master key 打**：master 走的是另一条路径（没有 per-key 白名单闸门），
  必然假绿。正解是 `/key/generate` 复刻目标 key 的 `models`+`aliases` 形状，
  用复刻 key 打真流量，收尾删掉。
- **阴性对照必须能红得不一样**：编一个不存在的名字同发一遍。如果它和阳性给出
  同一个读数，这轮探针零分辨力、读数作废（不是"全挂了"）。
- **两个 image 名走 `/v1/images/generations`**，不能走 chat —— 它们是
  `mode=image_generation`，拿 chat 打出来的红和"白名单没生效"长得一模一样。
- **每发带唯一 nonce**：响应缓存是开的，字节相同的请求根本到不了上游。
- **逐 pod 打**：写完 key 另一个 proxy pod 约 2 分钟才认，当场 403 是传播延迟。

用法（在 198 上，proxy pod 内没有 curl，所以整个脚本用 urllib）：
    LITELLM_MASTER_KEY=... python3 litellm-198-grok6-cursor-probe.py \
        --base http://127.0.0.1:30402 --clone-from backups/<canary>.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

CHAT_NAMES = (
    "sa-composer-2.5-fast",
    "sa-grok-4.20-0309-reasoning",
    "sa-grok-4.6-latest",
    "sa-grok-4.5-latest",
)
IMAGE_NAMES = ("sa-grok-imagine", "sa-grok-imagine-image-2.0")


def call(base: str, path: str, payload, key: str, timeout: int = 240):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Authorization": "Bearer " + key}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.load(r), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf8", "replace")[:300], dict(e.headers or {})
    except Exception as e:  # noqa: BLE001 - 传输层断了也要看得见，禁 2>/dev/null
        return -1, "%s: %s" % (type(e).__name__, str(e)[:200]), {}


def clone_shape(path: str) -> tuple[str, list, dict]:
    """从 allowlist 脚本的 backup 快照里取目标 key 的形状。

    快照是**写之前**的 models，所以这里补上这 6 个名 —— 复刻的是"写完之后"
    应该长的样子。backup 里没有明文 key（库里只有 sha256），所以只能复刻形状。
    """
    snap = json.load(open(path))
    token, row = next(iter(snap.items()))
    models = list(row["models"])
    for n in CHAT_NAMES + IMAGE_NAMES:
        if n not in models:
            models.append(n)
    return row.get("key_alias", token[:12]), models, dict(row.get("aliases") or {})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:30402")
    ap.add_argument("--clone-from", required=True,
                    help="allowlist 脚本 --backup 产出的快照（取第一把 key 的形状）")
    ap.add_argument("--skip-image", action="store_true",
                    help="只打 chat（image 名慢，赶时间时用）")
    args = ap.parse_args()

    master = os.environ.get("LITELLM_MASTER_KEY")
    if not master:
        print("LITELLM_MASTER_KEY 未设置", file=sys.stderr)
        return 2

    nonce = "%d-%d" % (time.time(), random.randint(1000, 9999))
    alias, models, aliases = clone_shape(args.clone_from)
    print("clone_from=%s alias=%s models=%d aliases=%d nonce=%s"
          % (args.clone_from, alias, len(models), len(aliases), nonce))

    st, gen, _ = call(args.base, "/key/generate",
                      {"key_alias": "zz-probe-grok6-" + nonce, "models": models,
                       "aliases": aliases, "duration": "20m"}, master)
    if st != 200:
        print("key/generate 失败 %s %s" % (st, gen))
        return 1
    probe = gen["key"]

    bad = 0
    try:
        for n in CHAT_NAMES:
            st, body, h = call(args.base, "/v1/chat/completions", {
                "model": n,
                "messages": [{"role": "user", "content":
                              "nonce %s for %s: reply with the single word OK" % (nonce, n)}],
                "max_tokens": 16}, probe)
            ok = st == 200 and nonce not in json.dumps(body)  # 回显 nonce 不算内容
            txt = (body["choices"][0]["message"]["content"] if st == 200 else str(body))
            print("  CHAT %-32s %s mid=%s %r" % (n, st, h.get("x-litellm-model-id"), str(txt)[:40]))
            if st != 200:
                bad += 1
        if not args.skip_image:
            for n in IMAGE_NAMES:
                st, body, h = call(args.base, "/v1/images/generations", {
                    "model": n, "prompt": "a plain red square, token " + nonce, "n": 1}, probe)
                info = ("data=%d" % len(body.get("data", []))) if st == 200 else str(body)
                print("  IMG  %-32s %s mid=%s %s" % (n, st, h.get("x-litellm-model-id"), info[:120]))
                if st != 200:
                    bad += 1
        # 阴性对照：不存在的名字必须给出**和阳性不同**的读数
        st, body, _ = call(args.base, "/v1/chat/completions", {
            "model": "sa-does-not-exist-" + nonce,
            "messages": [{"role": "user", "content": "x"}], "max_tokens": 8}, probe)
        print("  NEG  %-32s %s %s" % ("(bogus name)", st, str(body)[:100]))
        if st == 200:
            print("RULER-BROKEN: 不存在的名字也 200，这轮读数作废")
            return 3
    finally:
        st, body, _ = call(args.base, "/key/delete", {"keys": [probe]}, master)
        print("  cleanup /key/delete -> %s %s" % (st, str(body)[:80]))

    print("PROBE %s  chat=%d image=%d fail=%d"
          % ("GREEN" if bad == 0 else "RED", len(CHAT_NAMES),
             0 if args.skip_image else len(IMAGE_NAMES), bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
