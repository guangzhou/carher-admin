#!/usr/bin/env python3
"""Redirect cursor-* LiteLLM keys' gpt-5.6-sol traffic to gpt-5.6-terra via
per-key aliases (client-transparent, cursor-only greyout).

Motivation (2026-08-18). gpt-5.6-sol burns far more tokens than terra; we want to
move Cursor users onto the cheaper terra reasoning tier WITHOUT asking anyone to
change the model they pick in Cursor. Terra and sol are the SAME 6-account pool
(chatgpt-acct 219/221/222/223/224/227) — the only difference is the `model`
parameter sent to each account (openai/chatgpt-gpt-5.6-sol vs -terra).

Why per-key aliases and NOT the config `model_group_alias`:
  * config.yaml HAS `model_group_alias: {gpt-5.6-sol: chatgpt-gpt-5.6-sol}` but it
    is DEAD — the DB holds 6 real deployments named bare `gpt-5.6-sol`, and a
    same-named model group silently shadows model_group_alias (measured
    2026-08-18; cf. litellm-per-key-model-alias skill). Editing that line is a
    no-op.
  * model_group_alias is GLOBAL — it would also move 3 production her instances
    (carher-*) + ai-usage + ceshi that call sol. Scope is cursor-only, so we act
    at the key layer, which only touches the keys we choose.

Consumers of gpt-5.6-sol over 3 days (measured 2026-08-18): 200 cursor keys via
bare `gpt-5.6-sol` (~175k reqs) + 36 cursor keys via pool name
`chatgpt-gpt-5.6-sol` (~3.7k). We alias BOTH names so neither cursor path leaks.
Non-cursor consumers (carher-*, ai-usage-*, ceshi-*) are left on sol.

Routing after this change:
    client types gpt-5.6-sol / chatgpt-gpt-5.6-sol
      -> per-key alias rewrites to gpt-5.6-terra / chatgpt-gpt-5.6-terra
      -> matches the terra DB deployment group (6 accounts)
      -> account receives openai/chatgpt-gpt-5.6-terra
    Fallback comes for free: the request now routes at the terra group, so it
    uses terra's existing fallback chain (chatgpt-gpt-5.6-luna, ...-sol,
    deepseek-v4-flash-responses) — exactly the ask (fallback follows terra).

`aliases` and `models` are WHOLE-FIELD REPLACE in LiteLLM /key/update, so we
read-merge-write; cursor keys already carry glm/kimi/deepseek economy aliases
that a blind write would wipe. terra names are already in the allowlist for most
keys, but we union them in defensively (skip keys with models==[] — that means
"unrestricted" and writing a list would narrow an all-access key).

Verify (must read TWO columns, per the skill): after canary, send the ORIGINAL
name gpt-5.6-sol on the canary key and check SpendLogs:
    select model, model_id from "LiteLLM_SpendLogs"
    where api_key='<sha256 of sk->' and "startTime" > now() - interval '5 min'
    order by "startTime" desc;
  expect model = openai/chatgpt-gpt-5.6-terra  (model rewritten to terra)
         model_id = chatgpt-acct-*-gpt-5.6-terra  (went to a terra deployment)

Usage (run on 198 host; NodePort base + master key from litellm-secrets):
    LITELLM_BASE=http://127.0.0.1:30402 LITELLM_MASTER_KEY=... \
        python3 litellm-cursor-sol-to-terra.py                       # dry-run all
    ... --only cursor-me-xxxx --backup /root/sol-terra-canary.json --apply
    ... --backup /root/sol-terra-full.json --apply                   # full apply
    ... --restore /root/sol-terra-full.json --apply                  # rollback
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
# sol name -> terra name to alias it to. Both cursor sol paths covered.
ALIAS_MAP = {
    "gpt-5.6-sol": "gpt-5.6-terra",
    "chatgpt-gpt-5.6-sol": "chatgpt-gpt-5.6-terra",
}
# every alias target must be in the allowlist (v1.89 strict); union them in.
MODELS_UNION = list(ALIAS_MAP.values())


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


def plan_key(k: dict) -> dict | None:
    """Return {aliases, models} for this key, or None if nothing to change.
    Skip keys with models==[] (unrestricted; writing a list would narrow it)."""
    cur_models = k.get("models") or []
    if not cur_models:
        return None

    cur_aliases = dict(k.get("aliases") or {})
    aliases = dict(cur_aliases)
    for src, dst in ALIAS_MAP.items():
        aliases[src] = dst

    models = list(cur_models)
    for name in MODELS_UNION:
        if name not in models:
            models.append(name)

    if aliases == cur_aliases and models == cur_models:
        return None
    return {"aliases": aliases, "models": models}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default dry-run)")
    ap.add_argument("--backup", help="write pre-change key snapshot here (required with --apply)")
    ap.add_argument("--only", action="append", default=[], help="restrict to these key_alias(es)")
    ap.add_argument("--limit", type=int, help="canary: only the first N planned keys")
    ap.add_argument("--restore", help="restore aliases/models from a backup json")
    args = ap.parse_args()

    if args.restore:
        snap = json.load(open(args.restore))
        print(f"restoring {len(snap)} keys from {args.restore}")
        if not args.apply:
            print("(dry-run; pass --apply to write)")
            return
        fails = 0
        for token, saved in snap.items():
            try:
                api("POST", "/key/update", {"key": token,
                                            "aliases": saved["aliases"],
                                            "models": saved["models"]})
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  RESTORE FAIL {saved.get('key_alias')}: {e}")
                if fails >= 2:
                    sys.exit("aborting: 2 consecutive restore failures")
        print(f"restored ok={len(snap) - fails} fail={fails}")
        return

    keys = list_all_keys()
    cursor = [k for k in keys if str(k.get("key_alias") or "").startswith(PREFIX)]
    if args.only:
        want = set(args.only)
        cursor = [k for k in cursor if k.get("key_alias") in want]
    print(f"{len(cursor)} {PREFIX}* keys in scope")

    planned = []
    skipped_unrestricted = 0
    for k in cursor:
        if not (k.get("models") or []):
            skipped_unrestricted += 1
        p = plan_key(k)
        if p is not None:
            planned.append((k, p))
    if args.limit:
        planned = planned[:args.limit]
    print(f"{len(planned)} keys need update; {skipped_unrestricted} skipped (models==[] unrestricted)")
    print(f"alias map: {ALIAS_MAP}")
    for k, p in planned[:5]:
        print(f"  e.g. {k.get('key_alias')}: +aliases {list(ALIAS_MAP)}; "
              f"models {len(k.get('models') or [])} -> {len(p['models'])}")

    if not args.apply:
        print("\n(dry-run; pass --apply with --backup to write)")
        return
    if not args.backup:
        sys.exit("--apply requires --backup")

    snap = {k["token"]: {"key_alias": k.get("key_alias"),
                         "aliases": dict(k.get("aliases") or {}),
                         "models": list(k.get("models") or [])}
            for k, _ in planned}
    json.dump(snap, open(args.backup, "w"), ensure_ascii=False, indent=2)
    print(f"backup -> {args.backup} ({len(snap)} keys)")

    fails = 0
    ok = 0
    for k, p in planned:
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
    for k, p in planned:
        cur = after.get(k["token"], {})
        al = cur.get("aliases") or {}
        ml = set(cur.get("models") or [])
        if any(al.get(s) != d for s, d in ALIAS_MAP.items()) or not set(MODELS_UNION) <= ml:
            bad += 1
            print(f"  READBACK MISMATCH {k.get('key_alias')}")
    print(f"readback: {len(planned) - bad}/{len(planned)} verified, {bad} mismatch")


if __name__ == "__main__":
    main()
