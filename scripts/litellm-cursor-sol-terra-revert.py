#!/usr/bin/env python3
"""Surgically revert the cursor-* sol->terra per-key aliases added on 2026-08-18
by litellm-cursor-sol-to-terra.py, restoring "you pick sol, you get sol".

Why NOT a blanket --restore from the 08-18 backup: a full restore writes back
the ENTIRE aliases+models snapshot, wiping anything changed since. Diff measured
2026-08-20: 571/575 keys are byte-identical to backup apart from my two aliases,
but 4 keys drifted (sa-* aliases + cursor-fc-*/local-deepseek-* allowlist edits
made after 08-18). A blanket restore would destroy those later edits.

Surgical rule, per key, per alias name in MY_MAP:
  * current value == the terra target I wrote  -> revert it:
      - backup had an ORIGINAL value for that name (2 keys: cursor-lianghaiqiang
        -> openrouter-gpt-5.6-sol, cursor-liuguoxian02 -> zerokey-pool-gpt-5.6-sol)
        -> write the original back
      - otherwise -> delete the alias entry
  * current value != my terra target (e.g. liuguoxian04's sa-* rewiring)
        -> LEAVE IT — someone deliberately changed it after 08-18
  * `models` is never touched (my union added terra names to a couple of
    allowlists; leaving them grants nothing hidden — picking terra explicitly
    should work anyway, which is exactly "改走什么走什么").

Keys not present in the 08-18 backup (created since) are still scanned: if one
carries my terra alias values it gets the same surgical delete, and is reported.

Usage (run on 198 host):
    LITELLM_BASE=http://127.0.0.1:30402 LITELLM_MASTER_KEY=... \
        python3 litellm-cursor-sol-terra-revert.py --backup-ref /root/sol-terra-full-20260818.json
    ... --only cursor-canary-xxx --apply          # canary
    ... --snapshot /root/sol-terra-revert-pre.json --apply   # full (snapshot = pre-revert state)

Verify after: SpendLogs two columns — cursor key sending gpt-5.6-sol must show
model=openai/chatgpt-gpt-5.6-sol and a chatgpt-acct-*-gpt-5.6-sol model_id again.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:30402").rstrip("/")
MASTER = os.environ["LITELLM_MASTER_KEY"]

PREFIX = "cursor-"
# alias name -> the terra target I wrote on 08-18 (only revert exact matches)
MY_MAP = {
    "gpt-5.6-sol": "gpt-5.6-terra",
    "chatgpt-gpt-5.6-sol": "chatgpt-gpt-5.6-terra",
}


def api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {MASTER}")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def list_all_keys() -> list[dict]:
    out: list[dict] = []
    for page in range(1, 1000):
        d = api("GET", f"/key/list?page={page}&size=100&return_full_object=true")
        ks = d.get("keys") or []
        if not ks:
            break
        out.extend(k for k in ks if isinstance(k, dict))
    return out


def plan_key(k: dict, backup: dict) -> tuple[dict, list[str]] | None:
    """Return ({aliases}, notes) if this key needs surgery, else None."""
    cur = dict(k.get("aliases") or {})
    new = dict(cur)
    notes: list[str] = []
    orig_aliases = (backup.get(k.get("token"), {}) or {}).get("aliases") or {}
    for src, my_tgt in MY_MAP.items():
        if new.get(src) != my_tgt:
            if src in new:
                notes.append(f"LEAVE {src}->{new[src]!r} (changed after 08-18, not mine)")
            continue
        orig = orig_aliases.get(src)
        if orig is not None and orig != my_tgt:
            new[src] = orig
            notes.append(f"RESTORE {src} -> {orig!r}")
        else:
            del new[src]
            notes.append(f"DELETE {src}")
    if new == cur:
        return None
    return {"aliases": new}, notes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup-ref", default="/root/sol-terra-full-20260818.json",
                    help="08-18 pre-change snapshot, used ONLY to look up original alias values")
    ap.add_argument("--apply", action="store_true", help="write changes (default dry-run)")
    ap.add_argument("--snapshot", help="write pre-revert state here (required with --apply, unless --only)")
    ap.add_argument("--only", action="append", default=[], help="restrict to these key_alias(es)")
    args = ap.parse_args()

    backup = json.load(open(args.backup_ref))
    keys = list_all_keys()
    cursor = [k for k in keys if str(k.get("key_alias") or "").startswith(PREFIX)]
    if args.only:
        want = set(args.only)
        cursor = [k for k in cursor if k.get("key_alias") in want]
    print(f"{len(cursor)} {PREFIX}* keys in scope; backup-ref has {len(backup)} keys")

    planned = []
    not_in_backup = []
    left_alone = []
    for k in cursor:
        if k.get("token") not in backup and any(
                (k.get("aliases") or {}).get(s) == t for s, t in MY_MAP.items()):
            not_in_backup.append(k.get("key_alias"))
        p = plan_key(k, backup)
        if p is not None:
            planned.append((k, *p))
        else:
            al = k.get("aliases") or {}
            leaves = [f"{s}->{al[s]!r}" for s in MY_MAP if s in al]
            if leaves:
                left_alone.append((k.get("key_alias"), leaves))

    print(f"{len(planned)} keys need surgery")
    if not_in_backup:
        print(f"NOTE: {len(not_in_backup)} keys carry my terra alias but are NOT in the 08-18 "
              f"backup (created since): {not_in_backup[:10]} — surgical delete applies")
    if left_alone:
        print(f"{len(left_alone)} keys keep a sol alias DELIBERATELY set after 08-18 (untouched):")
        for a, ls in left_alone[:10]:
            print(f"  {a}: {ls}")
    # show the interesting plans first (restores), then a couple of deletes
    restores = [(k, p, n) for k, p, n in planned if any(x.startswith("RESTORE") for x in n)]
    for k, p, n in restores:
        print(f"  {k.get('key_alias')}: {n}")
    for k, p, n in [x for x in planned if x not in restores][:3]:
        print(f"  e.g. {k.get('key_alias')}: {n}")

    if not args.apply:
        print("\n(dry-run; pass --apply to write)")
        return
    if not args.snapshot and not args.only:
        sys.exit("--apply on full scope requires --snapshot")

    if args.snapshot:
        snap = {k["token"]: {"key_alias": k.get("key_alias"),
                             "aliases": dict(k.get("aliases") or {})}
                for k, _, _ in planned}
        json.dump(snap, open(args.snapshot, "w"), ensure_ascii=False, indent=2)
        print(f"pre-revert snapshot -> {args.snapshot} ({len(snap)} keys)")

    ok = fails = 0
    for k, p, _ in planned:
        try:
            api("POST", "/key/update", {"key": k["token"], **p})
            ok += 1
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  FAIL {k.get('key_alias')}: {e}")
            if fails >= 2:
                sys.exit("aborting: 2 consecutive write failures")
    print(f"applied ok={ok} fail={fails}")

    # readback: planned keys must now match their planned aliases exactly
    after = {k["token"]: k for k in list_all_keys()}
    bad = 0
    for k, p, _ in planned:
        cur = after.get(k["token"], {})
        if dict(cur.get("aliases") or {}) != p["aliases"]:
            bad += 1
            print(f"  READBACK MISMATCH {k.get('key_alias')}")
    print(f"readback: {len(planned) - bad}/{len(planned)} verified, {bad} mismatch")


if __name__ == "__main__":
    main()
