#!/usr/bin/env python3
"""Make the compat_proxy admission wait configurable instead of an instant 429.

Run ON the GPU host (h100 / 192.168.3.205).

BEFORE: the 17th concurrent generation request hit
    await asyncio.wait_for(_inflight.acquire(), timeout=0.01)
and got an immediate HTTP 429. openclaw/LiteLLM treat that as a failed turn, so
a brief traffic burst surfaces to users as an error rather than a slow reply.

AFTER: the wait is DEEPSEEK_ADMIT_WAIT_S, default 0.01 so this patch alone
changes nothing. The systemd unit sets a real value (e.g. 20s), making bursts
queue briefly instead of failing.

⚠ DEEPSEEK_MAX_INFLIGHT is deliberately NOT raised. It looks like an obvious
knob (the engine allows --max-running-requests 64) but this workload has
p50 ~46k-token prompts: 16 concurrent already needs ~750k of the 1.63M-token
KV pool, and 48 concurrent would need ~2.2M and thrash it. Admission control at
16 is correctly sized here; the bug was the instant rejection, not the limit.

Usage:
  python3 patch_compat_proxy_admission.py --check
  python3 patch_compat_proxy_admission.py --apply
  python3 patch_compat_proxy_admission.py --revert
"""
import argparse
import glob
import shutil
import sys
import time

TARGET = "/home/cltx/deepseek-v4-flash/scripts/compat_proxy.py"
SUFFIX = ".bak-admitwait-" + time.strftime("%Y%m%dT%H%M%S")

ANCHOR_LIMIT = 'MAX_INFLIGHT = max(1, int(os.environ.get("DEEPSEEK_MAX_INFLIGHT", "16")))'
ANCHOR_WAIT = "await asyncio.wait_for(_inflight.acquire(), timeout=0.01)"

ADDED = '''
# How long to wait for an in-flight slot before returning 429. Historically a
# hardcoded 0.01s, i.e. an instant hard failure once MAX_INFLIGHT was reached.
# openclaw treats 429 as a failed turn, so a short queue is strictly better.
# Default stays 0.01 so importing this module changes nothing; the systemd unit
# supplies the real value.
ADMIT_WAIT_S = float(os.environ.get("DEEPSEEK_ADMIT_WAIT_S", "0.01"))'''

NEW_WAIT = "await asyncio.wait_for(_inflight.acquire(), timeout=ADMIT_WAIT_S)"


def read():
    with open(TARGET, encoding="utf-8") as fh:
        return fh.read()


def check(src):
    errs = []
    if "ADMIT_WAIT_S" in src:
        errs.append("already patched (ADMIT_WAIT_S present)")
    if src.count(ANCHOR_LIMIT) != 1:
        errs.append(f"MAX_INFLIGHT anchor count={src.count(ANCHOR_LIMIT)}, need 1")
    if src.count(ANCHOR_WAIT) != 1:
        errs.append(f"acquire-timeout anchor count={src.count(ANCHOR_WAIT)}, need 1")
    return errs


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--revert", action="store_true")
    args = ap.parse_args()

    if args.revert:
        baks = sorted(glob.glob(TARGET + ".bak-admitwait-*"))
        if not baks:
            print("no backup found")
            return 1
        shutil.copy2(baks[-1], TARGET)
        print(f"restored {TARGET} from {baks[-1]}")
        return 0

    src = read()
    errs = check(src)
    for e in errs:
        print(f"BLOCKED: {e}")
    if errs:
        return 1

    new = src.replace(ANCHOR_LIMIT, ANCHOR_LIMIT + ADDED, 1)
    new = new.replace(ANCHOR_WAIT, NEW_WAIT, 1)

    if args.check:
        print("checks PASS — would add ADMIT_WAIT_S and use it in the acquire()")
        return 0

    shutil.copy2(TARGET, TARGET + SUFFIX)
    print(f"backup: {TARGET}{SUFFIX}")
    with open(TARGET, "w", encoding="utf-8") as fh:
        fh.write(new)

    after = read()
    assert "ADMIT_WAIT_S = float(" in after
    assert NEW_WAIT in after
    assert "timeout=0.01)" not in after
    import py_compile
    py_compile.compile(TARGET, doraise=True)
    print("APPLIED and py_compile OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
