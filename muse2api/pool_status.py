#!/usr/bin/env python3
"""Read-only health report for the 198 muse2api pool.

    python3 pool_status.py            # accounts, cookie expiry, use counts, concurrency
    python3 pool_status.py --quota    # also read each account's quota (opens a page per account, ~30s each)
    python3 pool_status.py --test     # also run /test per account (real login check)

Exit 1 if any enabled account is not ok, its cookie is expired, or concurrency != live accounts x slots.
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

K3S = "cltx@10.68.13.198"
POOL = "http://10.68.13.198:30610"


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout.strip()


def pool(key, method, path, timeout=200):
    req = urllib.request.Request(POOL + path, method=method, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def pool_safe(key, method, path, timeout=200):
    """Like pool(), but an HTTP error (e.g. 401 on a dead account) becomes {"detail": ...}."""
    try:
        return pool(key, method, path, timeout)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}") | {"http": e.code}
        except ValueError:
            return {"detail": f"HTTP {e.code}", "http": e.code}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quota", action="store_true")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()

    key = sh(["ssh", K3S, "sudo kubectl -n muse2api get secret muse2api-env -o jsonpath='{.data.MUSE2API_KEY}' | base64 -d"])
    conc = sh(["ssh", K3S, "sudo kubectl -n muse2api get deploy muse2api -o "
               "jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"MUSE2API_CONCURRENCY\")].value}'"])
    slots = sh(["ssh", K3S, "sudo kubectl -n muse2api get deploy muse2api -o "
                "jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"MUSE2API_PER_ACCOUNT_SLOTS\")].value}'"]) or "1"
    pod = sh(["ssh", K3S, "sudo kubectl -n muse2api get pod -l app=muse2api -o "
              "jsonpath='{.items[0].status.phase} restarts={.items[0].status.containerStatuses[0].restartCount}'"])
    d = pool(key, "GET", "/admin/accounts")
    accs, now, bad = d["accounts"], time.time(), []
    live = [x for x in accs if x.get("enabled") and x.get("ok") is not False]
    print(f"pod: {pod}   concurrency={conc}   slots/account={slots}   accounts={len(accs)} live={len(live)}")
    # harden #8 (h6): each account runs up to MUSE2API_PER_ACCOUNT_SLOTS tasks at once.
    if str(len(live) * int(slots)) != conc:
        bad.append(f"concurrency {conc} != live accounts {len(live)} x slots {slots}")
    for x in accs:
        days = ((x.get("expires_at") or 0) - now) / 86400
        line = (f"  {x['label']:<18} ok={x.get('ok')!s:<5} enabled={x.get('enabled')!s:<5} "
                f"uses={x.get('use_count', 0):<4} cookie_left={days:5.1f}d")
        if a.test and x.get("enabled"):
            t = pool_safe(key, "POST", f"/admin/accounts/{x['id']}/test")
            line += f" test={t.get('ok')}"
            if not t.get("ok"):
                bad.append(f"{x['label']} test failed: {t.get('message') or t.get('detail')}")
        if a.quota and x.get("enabled"):
            q = pool_safe(key, "POST", f"/admin/accounts/{x['id']}/quota")
            if q.get("found"):
                line += f" weekly={q.get('weekly_used_pct')}% reset={q.get('weekly_reset')} extra={q.get('extra_left')}"
            else:
                line += f" quota=ERR({q.get('detail') or q.get('message')})"
                bad.append(f"{x['label']} quota read failed: {q.get('detail') or q.get('message')}")
        print(line)
        if x.get("enabled") and x.get("ok") is False:
            bad.append(f"{x['label']} marked not ok: {x.get('note')}")
        if days < 0 and x.get("enabled"):
            bad.append(f"{x['label']} cookie expired")
    for b in bad:
        print("PROBLEM:", b)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
