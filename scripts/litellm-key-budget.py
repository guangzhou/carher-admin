#!/usr/bin/env python3
"""
Set / inspect daily budget on aliyun carher LiteLLM virtual keys.

Default policy (2026-05-23): every `carher-*` key must have
`max_budget=100` + `budget_duration=1d` unless explicitly whitelisted.

USAGE
  # Inspect current state of all carher-* keys (no changes)
  scripts/litellm-key-budget.py --inspect

  # Apply $100/day to keys WITHOUT any budget set (idempotent default)
  scripts/litellm-key-budget.py --apply

  # Force $100/day on specific keys (overrides any existing budget)
  scripts/litellm-key-budget.py --apply --force --key carher-234 --key carher-235

  # Custom budget value
  scripts/litellm-key-budget.py --apply --budget 50

PORT-FORWARD
  Auto-managed: spawns `kubectl port-forward svc/litellm-proxy 4000` if 127.0.0.1:4000
  is not reachable, and tears it down on exit. Pre-requirement: jms tunnel active
  (see k8s-via-bastion skill).

EXIT
  0 = no errors; 1 = at least one update failed.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import json
import socket
import subprocess
import sys
import time
import urllib.request

LITELLM_URL = "http://127.0.0.1:4000"
NS = "carher"
DEFAULT_BUDGET = 100.0
DEFAULT_DURATION = "1d"

# Whitelisted special-budget keys (do NOT auto-overwrite)
WHITELIST = {"carher-2", "carher-11", "carher-94"}


def port_forward_alive() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", 4000))
        return True
    except Exception:
        return False
    finally:
        s.close()


_PF_PROC: subprocess.Popen | None = None


def ensure_port_forward() -> None:
    global _PF_PROC
    if port_forward_alive():
        return
    _PF_PROC = subprocess.Popen(
        ["kubectl", "port-forward", "-n", NS, "svc/litellm-proxy", "4000:4000"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    atexit.register(lambda: _PF_PROC and _PF_PROC.terminate())
    for _ in range(20):
        time.sleep(0.5)
        if port_forward_alive():
            return
    raise RuntimeError("port-forward to litellm-proxy did not come up in 10s")


def get_master_key() -> str:
    out = subprocess.run(
        ["kubectl", "get", "secret", "litellm-secrets", "-n", NS,
         "-o", "jsonpath={.data.LITELLM_MASTER_KEY}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return base64.b64decode(out).decode()


def fetch_keys(master: str) -> list[dict]:
    req = urllib.request.Request(
        f"{LITELLM_URL}/spend/keys?limit=600",
        headers={"Authorization": f"Bearer {master}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def update_key(master: str, token: str, max_budget: float, duration: str) -> None:
    payload = json.dumps({
        "key": token,
        "max_budget": max_budget,
        "budget_duration": duration,
    }).encode()
    req = urllib.request.Request(
        f"{LITELLM_URL}/key/update", data=payload,
        headers={"Authorization": f"Bearer {master}", "Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10).read()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect", action="store_true",
                      help="Print current budget state of all carher-* keys")
    mode.add_argument("--apply", action="store_true",
                      help="Set budget on keys without one (or specified --key)")

    ap.add_argument("--key", action="append", default=[],
                    help="Specific key_alias to target (repeatable). Default: all "
                         "carher-* keys with max_budget=None.")
    ap.add_argument("--force", action="store_true",
                    help="Apply even if a budget is already set (overrides existing)")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET,
                    help=f"Daily budget in USD (default: {DEFAULT_BUDGET})")
    ap.add_argument("--duration", default=DEFAULT_DURATION,
                    help=f"Budget reset window (default: {DEFAULT_DURATION})")
    args = ap.parse_args()

    ensure_port_forward()
    master = get_master_key()
    keys = fetch_keys(master)
    carhers = [r for r in keys if (r.get("key_alias") or "").startswith("carher-")]

    if args.inspect:
        no_budget = [r for r in carhers if r.get("max_budget") is None]
        has_budget = [r for r in carhers if r.get("max_budget") is not None]
        from collections import Counter
        dist = Counter((r.get("max_budget"), r.get("budget_duration")) for r in has_budget)
        print(f"=== {len(carhers)} carher-* keys ===")
        print(f"  无限额: {len(no_budget)}")
        for r in no_budget:
            print(f"    {r['key_alias']:<15} spend=${r.get('spend',0):.2f}")
        print(f"  已设限额分布:")
        for (mb, bd), n in sorted(dist.items(), key=lambda x: (x[0][0] or 0)):
            tag = "WHITELIST" if mb in (300.0, 200.0, 150.0) else ""
            print(f"    ${mb:>5}/{bd}: {n:>4}  {tag}")
        return 0

    # Apply mode
    if args.key:
        targets = [r for r in carhers if r["key_alias"] in args.key]
        if not targets:
            print(f"no carher-* keys match --key {args.key}", file=sys.stderr)
            return 1
    elif args.force:
        targets = [r for r in carhers if r["key_alias"] not in WHITELIST]
    else:
        targets = [r for r in carhers if r.get("max_budget") is None]

    if not targets:
        print("nothing to update (all carher-* keys already have a budget). "
              "Use --force to overwrite, or --key to target specific keys.")
        return 0

    print(f"updating {len(targets)} keys → max_budget=${args.budget}, "
          f"budget_duration={args.duration}{' [FORCE]' if args.force else ''}")
    failed = 0
    for r in targets:
        alias = r["key_alias"]
        old_b = r.get("max_budget")
        old_d = r.get("budget_duration")
        try:
            update_key(master, r["token"], args.budget, args.duration)
            tag = f"(was ${old_b}/{old_d})" if old_b is not None else "(was 无限额)"
            print(f"  ✓ {alias:<15} → ${args.budget}/{args.duration} {tag}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {alias}: {e}")
    print(f"done: {len(targets) - failed}/{len(targets)} ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
