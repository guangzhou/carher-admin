#!/usr/bin/env python3
"""Probe public model names (``her-pro`` / ``her-flash`` ...) on every serving 198 proxy pod.

Run on 198. Shapes this encodes, each a ruler that has lied before:

* **Every serving pod, not the service VIP.** 198's `svc/litellm-proxy` selects
  `carher.net/litellm-production-route=enabled`. Each replica caches key
  metadata independently and can lag a write by ~2 minutes, so a 403 on one
  pod right after a write is not a failed write -- it is that pod's cache.
  Hitting the VIP would load-balance and hide which pod disagrees.
* **The verdict is the parsed `choices[0].message.content`.** Grepping the whole
  body reads the echoed prompt as success. `her-flash` is a reasoning model:
  with a small `max_tokens` it returns `finish_reason=length` and *empty*
  content, so the check must survive that and report it as a miss.
* **The default check is arithmetic, not "repeat this token".** 2026-09-29
  grok-4.7 refused every "Reply with exactly this token" prompt on all 4 pods
  ("I can't comply with requests to output an exact token") -- HTTP 200, right
  landing, and the nonce check read it as 8/8 FAIL. A random ``a*b`` whose
  product must appear in the reply proves a live model just as well and no
  model refuses it. ``--check nonce`` keeps the old behaviour.
* **A negative control.** An invented model name must come back 4xx. Without it
  an all-green run cannot tell "the alias resolves" from "this key is
  unrestricted".
* **``--clone-key``**: 198 stores only ``sha256(key)``, so a real carher key's
  plaintext is not retrievable there. Cloning the target key's ``models`` +
  ``aliases`` into a short-lived temp key probes *exactly* that key's routing;
  the temp key is deleted in ``finally`` and its absence is re-checked.

Landing is NOT proven here: ``x-litellm-model-id`` is a hash on 198 (e.g.
``4e698ca6f80f``), not ``sa/grok-4.7``. Confirm the landing from
``LiteLLM_SpendLogs`` (``model_group`` / ``model`` columns) for the temp key alias
this script prints.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import urllib.error
import urllib.parse
import urllib.request

ADMIN_BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:30402").rstrip("/")


def make_prompt(check: str, nonce: str, a: int, b: int) -> str:
    if check == "nonce":
        return f"Reply with exactly this token and nothing else: {nonce}"
    return f"What is {a}*{b}? Answer with just the number."


def call(pod_ip: str, key: str, model: str, prompt: str, timeout: int) -> dict:
    body = json.dumps({
        "model": model,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": prompt}],
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


def verdict(res: dict, expect: str) -> str:
    """``OK`` iff HTTP 200 and ``expect`` is in the parsed content.

    Thousands separators are stripped so ``71,883`` still matches ``71883``.
    """
    if res["status"] != 200:
        return f"HTTP_{res['status']}"
    body = res["body"]
    if not isinstance(body, dict) or not body.get("choices"):
        return "NO_CHOICES"
    choice = body["choices"][0]
    content = (choice.get("message", {}).get("content") or "")
    finish = choice.get("finish_reason")
    if expect in content.replace(",", ""):
        return "OK"
    return f"MISS finish={finish} content={content.strip()[:60]!r}"


def admin(method: str, path: str, master: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(ADMIN_BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {master}")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def clone_temp_key(alias: str, master: str, temp_alias: str) -> tuple[str, dict]:
    listing = admin("GET", "/key/list?return_full_object=true&key_alias="
                    + urllib.parse.quote(alias), master)
    rows = [k for k in listing.get("keys") or []
            if isinstance(k, dict) and k.get("key_alias") == alias]
    if len(rows) != 1:
        raise SystemExit(f"--clone-key {alias!r} matched {len(rows)} keys, need exactly 1")
    src = rows[0]
    made = admin("POST", "/key/generate", master, {
        "models": list(src.get("models") or []),
        "aliases": dict(src.get("aliases") or {}),
        "duration": "20m",
        "key_alias": temp_alias,
        "metadata": {"purpose": f"her-name-probe clone of {alias}"},
    })
    return made["key"], src


def delete_temp_key(temp_alias: str, master: str) -> bool:
    admin("POST", "/key/delete", master, {"key_aliases": [temp_alias]})
    left = admin("GET", "/key/list?return_full_object=true&key_alias="
                 + urllib.parse.quote(temp_alias), master)
    return not [k for k in left.get("keys") or []
                if isinstance(k, dict) and k.get("key_alias") == temp_alias]


def main() -> int:
    p = argparse.ArgumentParser()
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--key", help="plaintext key to probe with")
    who.add_argument("--clone-key", metavar="KEY_ALIAS",
                     help="clone this key's models+aliases into a 20m temp key "
                          "(needs LITELLM_MASTER_KEY); deleted afterwards")
    p.add_argument("--nonce", default="", help="only used by --check nonce")
    p.add_argument("--check", choices=("arith", "nonce"), default="arith")
    p.add_argument("--pod-ip", action="append", required=True)
    p.add_argument("--model", action="append", default=[])
    p.add_argument("--negative", default="her-nonexistent-control",
                   help="invented name that must NOT return 200")
    p.add_argument("--timeout", type=int, default=120)
    args = p.parse_args()
    if args.check == "nonce" and not args.nonce:
        p.error("--check nonce needs --nonce")

    master = os.environ.get("LITELLM_MASTER_KEY", "")
    temp_alias = ""
    key = args.key
    if args.clone_key:
        if not master:
            p.error("--clone-key needs LITELLM_MASTER_KEY")
        temp_alias = f"tmp-probe-{args.clone_key}-{os.getpid()}"
        key, src = clone_temp_key(args.clone_key, master, temp_alias)
        print(f"cloned {args.clone_key} -> {temp_alias} "
              f"models={len(src.get('models') or [])} "
              f"aliases={ {m: (src.get('aliases') or {}).get(m) for m in args.model} }")

    a, b = random.randint(100, 999), random.randint(100, 999)
    prompt = make_prompt(args.check, args.nonce, a, b)
    expect = args.nonce if args.check == "nonce" else str(a * b)
    models = args.model or ["her-pro", "her-flash"]
    bad = 0
    try:
        for pod_ip in args.pod_ip:
            for model in models:
                res = call(pod_ip, key, model, prompt, args.timeout)
                note = verdict(res, expect)
                if note != "OK":
                    bad += 1
                print(f"{pod_ip:<14} {model:<24} {note:<40} mid={res['model_id']}")
            res = call(pod_ip, key, args.negative, prompt, args.timeout)
            ok = res["status"] >= 400
            if not ok:
                bad += 1
            print(f"{pod_ip:<14} {'[negative]':<24} "
                  f"{('BLOCKED_' + str(res['status'])) if ok else 'LEAKED_200':<40}")
    finally:
        if temp_alias:
            gone = delete_temp_key(temp_alias, master)
            print(f"temp_key {temp_alias} deleted={gone}  "
                  f"(landing: SpendLogs where metadata->>'user_api_key_alias'='{temp_alias}')")
            if not gone:
                bad += 1

    print("PASS: every pod serves every name, negative control blocked" if not bad
          else f"FAIL: {bad} bad results")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
