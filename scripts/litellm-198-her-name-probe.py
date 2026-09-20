#!/usr/bin/env python3
"""Probe the `her-pro` / `her-flash` public names on every serving 198 proxy pod.

Run on 198. Three shapes this encodes, each a ruler that has lied before:

* **Every serving pod, not the service VIP.** 198's `svc/litellm-proxy` selects
  `carher.net/litellm-production-route=enabled`, which is the 4-replica gray
  lane. Each replica caches key metadata independently and can lag a write by
  ~2 minutes, so a 403 on one pod right after a write is not a failed write --
  it is that pod's cache. Hitting the VIP would load-balance and hide which pod
  disagrees.
* **The verdict is the parsed `choices[0].message.content`.** Grepping the whole
  body reads the echoed prompt as success. `her-flash` is a reasoning model:
  with a small `max_tokens` it returns `finish_reason=length` and *empty*
  content, so the nonce check must survive that and report it as a miss.
* **A negative control.** An invented model name must come back 4xx. Without it
  an all-green run cannot tell "the alias resolves" from "this key is
  unrestricted".
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request


def call(pod_ip: str, key: str, model: str, nonce: str, timeout: int) -> dict:
    body = json.dumps({
        "model": model,
        "max_tokens": 512,
        "messages": [{
            "role": "user",
            "content": f"Reply with exactly this token and nothing else: {nonce}",
        }],
    }).encode()
    req = urllib.request.Request(
        f"http://{pod_ip}:4000/v1/chat/completions", data=body, method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {
                "status": resp.status,
                "model_id": resp.headers.get("x-litellm-model-id", ""),
                "body": json.loads(resp.read().decode()),
            }
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "model_id": "",
                "body": exc.read().decode(errors="replace")[:300]}
    except Exception as exc:  # noqa: BLE001
        return {"status": 0, "model_id": "", "body": f"{type(exc).__name__}: {exc}"}


def verdict(res: dict, nonce: str) -> str:
    if res["status"] != 200:
        return f"HTTP_{res['status']}"
    body = res["body"]
    if not isinstance(body, dict) or "choices" not in body:
        return "NO_CHOICES"
    choice = body["choices"][0]
    content = (choice.get("message", {}).get("content") or "")
    finish = choice.get("finish_reason")
    if nonce in content:
        return "NONCE_OK"
    return f"NONCE_MISS finish={finish} content={content.strip()[:60]!r}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--key", required=True)
    p.add_argument("--nonce", required=True)
    p.add_argument("--pod-ip", action="append", required=True)
    p.add_argument("--model", action="append", default=[])
    p.add_argument("--negative", default="her-nonexistent-control",
                   help="invented name that must NOT return 200")
    p.add_argument("--timeout", type=int, default=120)
    args = p.parse_args()

    models = args.model or ["her-pro", "her-flash"]
    bad = 0
    for pod_ip in args.pod_ip:
        for model in models:
            res = call(pod_ip, args.key, model, args.nonce, args.timeout)
            note = verdict(res, args.nonce)
            if note != "NONCE_OK":
                bad += 1
            print(f"{pod_ip:<14} {model:<12} {note:<40} mid={res['model_id']}")
        res = call(pod_ip, args.key, args.negative, args.nonce, args.timeout)
        ok = res["status"] >= 400
        if not ok:
            bad += 1
        print(f"{pod_ip:<14} {'[negative]':<12} "
              f"{('BLOCKED_' + str(res['status'])) if ok else 'LEAKED_200':<40}")

    print("PASS: all pods resolve both names, negative control blocked" if not bad
          else f"FAIL: {bad} bad results")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
