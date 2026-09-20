#!/usr/bin/env python3
"""Reset spend for every carher-* LiteLLM key in the Aliyun carher namespace."""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import time
import urllib.request


def call(base: str, master: str, path: str, method: str = "GET", payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={"Authorization": "Bearer " + master, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode() or "{}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="reset spend to zero")
    ap.add_argument("--port", type=int, default=14000)
    args = ap.parse_args()
    ns = "carher"
    pf = subprocess.Popen(
        ["kubectl", "-n", ns, "port-forward", "svc/litellm-proxy", f"{args.port}:4000"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        base = f"http://127.0.0.1:{args.port}"
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/health/liveliness", timeout=2).close()
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise SystemExit("LiteLLM port-forward did not become ready")
        raw = subprocess.check_output(
            ["kubectl", "-n", ns, "get", "secret", "litellm-secrets", "-o",
             "jsonpath={.data.LITELLM_MASTER_KEY}"], text=True).strip()
        master = base64.b64decode(raw).decode()
        rows = call(base, master, "/spend/keys?limit=1000")
        keys = [r for r in rows if (r.get("key_alias") or "").startswith("carher-")]
        positive = [r for r in keys if float(r.get("spend") or 0) > 0]
        print(f"carher-* keys: {len(keys)}; spend>0: {len(positive)}")
        print(f"before spend total: {sum(float(r.get('spend') or 0) for r in keys):.6f}")
        if not args.apply:
            print("DRY-RUN: no changes")
            return 0
        failed = []
        for i, row in enumerate(positive, 1):
            try:
                call(base, master, "/key/update", "POST", {"key": row["token"], "spend": 0.0})
            except Exception as exc:
                failed.append((row.get("key_alias"), str(exc)))
            if i % 50 == 0:
                print(f"progress: {i}/{len(positive)}", flush=True)
        print(f"updated: {len(positive) - len(failed)}/{len(positive)}")
        if failed:
            for alias, error in failed[:20]:
                print(f"FAIL {alias}: {error}")
            return 1
        after = call(base, master, "/spend/keys?limit=1000")
        keys_after = [r for r in after if (r.get("key_alias") or "").startswith("carher-")]
        positive_after = [r for r in keys_after if float(r.get("spend") or 0) > 0]
        print(f"after spend>0: {len(positive_after)}")
        print(f"after spend total: {sum(float(r.get('spend') or 0) for r in keys_after):.6f}")
        return 0
    finally:
        pf.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
