#!/usr/bin/env python3
"""Expose the subscription-backed grok models to colleague LiteLLM keys by adding
them to each key's `models` allowlist. Three client protocols, three key groups:

  * cursor-*   (Cursor / Xcode, /v1/chat/completions + /v1/models discovery)
  * codex-*    (Codex IDE, /v1/responses)
        -> add  grok-4.5, grok-4.6
  * claude-code-*  (Claude Code, /v1/messages; CC only lists claude-* names)
        -> add  claude-grok-4.5, claude-grok-4.6

Unlike glm-5.3 (litellm-cursor-glm53.py), grok needs NO aliases: the four model
names are REAL deployments in config.yaml (model_name grok-4.5 / grok-4.6 /
claude-grok-4.5 / claude-grok-4.6, all openai/grok-4.x -> grok-proxy). A client
types the exact name, it matches the deployment. So this is a pure allowlist
union — `models` is a whole-field REPLACE in LiteLLM, so we read-merge-write.

There is no fallback target: the grok subscription is a single upstream account,
nothing to fall back to. (Cf. project_grok_subscription_litellm_live_2026_08_15.)

Usage (run on 198 host; NodePort base + master key from litellm-secrets):
    LITELLM_BASE=http://127.0.0.1:30402 LITELLM_MASTER_KEY=... \
        python3 litellm-grok-key-allowlist.py                      # dry-run all groups
    ... --only-prefix cursor- --limit 1 --backup /root/grok-canary.json --apply
    ... --backup /root/grok-key-full.json --apply                  # full apply
    ... --restore /root/grok-key-full.json --apply                 # rollback
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:30402").rstrip("/")
MASTER = os.environ["LITELLM_MASTER_KEY"]

# key_alias prefix -> model names to add to that group's allowlist
GROUPS = {
    "cursor-": ["grok-4.5", "grok-4.6"],
    "codex-": ["grok-4.5", "grok-4.6"],
    "claude-code-": ["claude-grok-4.5", "claude-grok-4.6"],
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


def group_for(alias: str) -> str | None:
    for pref in GROUPS:
        if alias.startswith(pref):
            return pref
    return None


def plan_key(k: dict, add: list[str]) -> dict | None:
    """Return {models} if the key needs the grok names, else None. Skip keys with
    an empty models list — [] means 'unrestricted' in LiteLLM, and writing a list
    would silently narrow an all-access key into a whitelist."""
    cur = k.get("models") or []
    if not cur:
        return None
    models = list(cur)
    for name in add:
        if name not in models:
            models.append(name)
    if models == cur:
        return None
    return {"models": models}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default dry-run)")
    ap.add_argument("--backup", help="pre-change snapshot path (required with --apply)")
    ap.add_argument("--only-prefix", help="restrict to one group prefix (e.g. cursor-)")
    ap.add_argument("--only", action="append", default=[], help="restrict to these key_alias(es)")
    ap.add_argument("--limit", type=int, help="canary: only first N planned keys")
    ap.add_argument("--restore", help="restore models from a backup json")
    args = ap.parse_args()

    if args.restore:
        snap = json.load(open(args.restore))
        print(f"restoring {len(snap)} keys from {args.restore}")
        if not args.apply:
            print("(dry-run; pass --apply to write)")
            return
        fails = ok = 0
        for token, saved in snap.items():
            try:
                api("POST", "/key/update", {"key": token, "models": saved["models"]})
                ok += 1
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  RESTORE FAIL {saved.get('key_alias')}: {e}")
                if fails >= 2:
                    sys.exit("aborting: 2 consecutive restore failures")
        print(f"restored ok={ok} fail={fails}")
        return

    groups = dict(GROUPS)
    if args.only_prefix:
        if args.only_prefix not in groups:
            sys.exit(f"--only-prefix must be one of {list(groups)}")
        groups = {args.only_prefix: groups[args.only_prefix]}

    keys = list_all_keys()
    planned: list[tuple[dict, dict, list[str]]] = []
    counts: dict[str, list[int]] = {p: [0, 0] for p in groups}  # [in_scope, planned]
    for k in keys:
        alias = str(k.get("key_alias") or "")
        pref = group_for(alias)
        if pref is None or pref not in groups:
            continue
        if args.only and alias not in set(args.only):
            continue
        counts[pref][0] += 1
        p = plan_key(k, groups[pref])
        if p is not None:
            counts[pref][1] += 1
            planned.append((k, p, groups[pref]))

    for pref, (scope, need) in counts.items():
        print(f"{pref:14s} in_scope={scope:4d} need_update={need:4d}  add={groups[pref]}")
    if args.limit:
        planned = planned[:args.limit]
    print(f"TOTAL planned this run: {len(planned)}")
    for k, p, add in planned[:5]:
        print(f"  e.g. {k.get('key_alias')}: +{add}  models {len(k.get('models') or [])} -> {len(p['models'])}")

    if not args.apply:
        print("\n(dry-run; pass --apply with --backup to write)")
        return
    if not args.backup:
        sys.exit("--apply requires --backup")

    snap = {k["token"]: {"key_alias": k.get("key_alias"),
                         "models": list(k.get("models") or [])}
            for k, _, _ in planned}
    json.dump(snap, open(args.backup, "w"), ensure_ascii=False, indent=2)
    print(f"backup -> {args.backup} ({len(snap)} keys)")

    fails = ok = 0
    for k, p, _ in planned:
        try:
            api("POST", "/key/update", {"key": k["token"], **p})
            ok += 1
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  FAIL {k.get('key_alias')}: {e}")
            if fails >= 2:
                sys.exit("aborting: 2 consecutive write failures (backup is safe)")
    print(f"applied ok={ok} fail={fails}")

    # readback verification
    after = {k["token"]: k for k in list_all_keys()}
    bad = 0
    for k, p, add in planned:
        ml = set((after.get(k["token"], {}) or {}).get("models") or [])
        if not set(add) <= ml:
            bad += 1
            print(f"  READBACK MISMATCH {k.get('key_alias')}")
    print(f"readback: {len(planned) - bad}/{len(planned)} verified, {bad} mismatch")


if __name__ == "__main__":
    main()
