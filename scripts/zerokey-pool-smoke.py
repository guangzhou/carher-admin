#!/usr/bin/env python3
"""
zerokey-pool 存活探测：给每个 zerokey 端口发一条「随机数字」消息，间隔随机 5–10s。

为什么发随机数字：避免 ChatGPT 侧对固定 prompt 做缓存/去重；每次内容不同，最接近
真实单聊场景。只看「有没有回复内容」，不纠结回复质量。

数据源：scripts/zerokey_acct_port_map.py（live 采集端口↔acct 映射）。

用法（仓库根目录）：
    python3 scripts/zerokey-pool-smoke.py
    python3 scripts/zerokey-pool-smoke.py --min-wait 8 --max-wait 15
    python3 scripts/zerokey-pool-smoke.py --json
    python3 scripts/zerokey-pool-smoke.py --skip-198   # 不查 198 pool 状态

输出：每个端口一行（http / 有无回复 / 截断内容），末尾汇总。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request


def load_port_map() -> list[dict]:
    here = os.path.dirname(os.path.abspath(__file__))
    cmd = [sys.executable, os.path.join(here, "zerokey_acct_port_map.py"), "--json"]
    out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE)
    data = json.loads(out)
    return data.get("accounts", [])


def smoke_port(port: int, prompt: str, timeout: int = 60) -> dict:
    body = json.dumps({
        "model": "gpt-5-5",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 64,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=body,
        method="POST",
        headers={"Authorization": "Bearer raw", "Content-Type": "application/json"},
    )
    result = {"http": "?", "ok": False, "reply": ""}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            result["http"] = r.status
            data = json.loads(r.read().decode())
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        text = text.strip().replace("\n", " ")
        if text:
            result["ok"] = True
            result["reply"] = text[:60]
        else:
            result["reply"] = "(empty)"
    except urllib.error.HTTPError as e:
        result["http"] = e.code
        raw = e.read().decode(errors="replace")
        try:
            msg = json.loads(raw).get("error", {}).get("message", raw)
        except Exception:
            msg = raw
        result["reply"] = msg[:80].replace("\n", " ")
    except Exception as e:
        result["http"] = "ERR"
        result["reply"] = str(e)[:80]
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="zerokey-pool smoke: send random number to each port")
    ap.add_argument("--min-wait", type=float, default=5.0, help="min seconds between ports (default 5)")
    ap.add_argument("--max-wait", type=float, default=10.0, help="max seconds between ports (default 10)")
    ap.add_argument("--timeout", type=int, default=60, help="per-port timeout (default 60s)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--skip-198", action="store_true", help="skip 198 pool status check in map")
    args = ap.parse_args()

    if args.skip_198:
        os.environ["ZEROKEY_SKIP_198"] = "1"

    rows = load_port_map()
    if not rows:
        print("ERROR: no zerokey ports found", file=sys.stderr)
        return 1

    print("sending a random number to each zerokey port (interval %.0f-%.0fs)" % (
        args.min_wait, args.max_wait))
    print()
    print("%5s  %-10s %-10s %5s  %-5s  reply" % ("port", "zk_id", "acct", "http", "ok"))
    print("-" * 72)

    results = []
    ok = 0
    for i, r in enumerate(rows):
        port = int(r["port"])
        zk = r.get("zk_id", "?")
        acct = r.get("chatgpt_acct", "?")
        if i:
            wait = random.uniform(args.min_wait, args.max_wait)
            time.sleep(wait)
        prompt = str(random.randint(100000, 999999))
        res = smoke_port(port, prompt, timeout=args.timeout)
        res["port"] = port
        res["zk_id"] = zk
        res["acct"] = acct
        res["prompt"] = prompt
        if res["ok"]:
            ok += 1
        results.append(res)
        print("%5d  %-10s %-10s %5s  %-5s  %s" % (
            port, zk, acct, str(res["http"]), "Y" if res["ok"] else "N", res["reply"]))
        sys.stdout.flush()

    print("-" * 72)
    print("有回复: %d/%d" % (ok, len(results)))

    if args.json:
        print()
        print(json.dumps({"total": len(results), "ok": ok, "results": results},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
