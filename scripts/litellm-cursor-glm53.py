#!/usr/bin/env python3
"""Expose glm-5.3 to Cursor / Xcode users by adding per-key aliases + allowlist
entries on every cursor-* LiteLLM key.

Background (2026-08-14/15). glm-5.3 was registered on 198 as five z.ai-direct
deployments (see project_198_glm53_added_zai_direct_2026_08_14). Probe result,
same ZAI_API_KEY:
  * /api/paas/v4        glm-5.2 -> 200 , glm-5.3 -> 429 throttling
  * /api/coding/paas/v4 glm-5.3 -> 200  (zai-coding-glm-5.3, openai-compatible)
  * /api/anthropic      glm-5.3 -> 200  (zai-claude / zai-max)
So this account's glm-5.3 access is gated to the coding-plan endpoints. We must
route users at a WORKING group, not the throttled paas/v4 one.

Cursor and Xcode both resolve models through the cursor key:
  * Xcode 26 Intelligence lists the key's /v1/models == the `models` allowlist
  * Cursor calls the typed name, rewritten by the per-key `aliases` pre-routing
So exposure = alias (bare -> working group) + allowlist union.

Mirrors litellm-cursor-glm52-zhipu.py exactly (read-merge-write; `models` and
`aliases` are whole-field REPLACE in LiteLLM, so we must client-side merge or we
wipe kimi/deepseek/glm-5.2 economy aliases). Differences from 5.2:
  * target is zai-coding-glm-5.3 (5.2 used zai-glm-5.2 on paas/v4, which works
    for 5.2 but 429s for 5.3)
  * claude-glm-5.3 is aliased too (5.2 left claude-glm-5.2 as a real group, but
    that group is paas/v4 and 429s for 5.3; Cursor's Java client sends the
    claude- prefix, so we redirect it to the working coding group)

Usage:
    # preview (default dry-run)
    python3 scripts/litellm-cursor-glm53.py

    # canary one key
    python3 scripts/litellm-cursor-glm53.py --only cursor-zhoujinman-sram \
        --backup ~/cursor-glm53-canary.json --apply

    # full
    python3 scripts/litellm-cursor-glm53.py --backup ~/cursor-glm53-full.json --apply

    # rollback
    python3 scripts/litellm-cursor-glm53.py --restore ~/cursor-glm53-full.json --apply

Environment: LITELLM_BASE (default http://127.0.0.1:30402), LITELLM_MASTER_KEY.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys

_SIB = pathlib.Path(__file__).with_name("zerokey-pioneer-key-sync.py")
_spec = importlib.util.spec_from_file_location("zk_key_sync", _SIB)
_zk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_zk)
api, list_all_keys = _zk.api, _zk.list_all_keys

# Target group. We route to the ANTHROPIC-format glm-5.3 group, not the
# openai-format coding group. Measured 2026-08-15 (10 samples each via /chat):
#   zai-coding-glm-5.3 (openai @ /api/coding/paas/v4)  -> 3/10 200, 7/10 500
#   zai-claude-glm-5.3 (anthropic @ /api/anthropic)    -> 10/10 200
#   zai-max-glm-5.3    (anthropic @ /api/anthropic)    -> 10/10 200
# The z.ai coding endpoint is flaky (intermittent 500) AND, being openai-shaped,
# it 500s/404s when reached via the Anthropic /v1/messages path that Claude Code
# uses. The anthropic-format group is stable on BOTH entrypoints (litellm
# converts openai /chat -> anthropic and anthropic /v1/messages -> anthropic),
# so a single target serves Cursor (/chat), Xcode, and Claude Code (/v1/messages).
TARGET = "zai-claude-glm-5.3"
FALLBACK_TARGET = "openrouter-glm-5.2"
# names that get an alias -> TARGET (bare + reasoning variants + claude- prefix)
ALIAS_NAMES = ["glm-5.3", "glm-5.3-high", "glm-5.3-medium", "glm-5.3-low",
               "claude-glm-5.3"]
# allowlist must contain every alias name, the alias target, and the fallback
# target (v1.89 allowlist is strict; fallback targets are NOT auto-permitted)
MODELS_UNION = ALIAS_NAMES + [TARGET, FALLBACK_TARGET]


def plan_key(k: dict) -> dict | None:
    """Return {aliases, models} for this key, or None if nothing to change."""
    cur_models = k.get("models") or []
    if not cur_models:
        # [] means "unrestricted" in LiteLLM — writing a list here would silently
        # convert an all-access key into a narrow whitelist
        return None

    cur_aliases = dict(k.get("aliases") or {})
    aliases = dict(cur_aliases)
    for name in ALIAS_NAMES:
        aliases[name] = TARGET

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
    ap.add_argument("--prefix", default="cursor-",
                    help="key_alias prefix to target (cursor- for Cursor/Xcode, "
                         "claude-code- for Claude Code; CC only lists claude-* names "
                         "so the claude-glm-5.3 alias is the one that matters there)")
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
    cursor = [k for k in keys if str(k.get("key_alias") or "").startswith(args.prefix)]
    if args.only:
        want = set(args.only)
        cursor = [k for k in cursor if k.get("key_alias") in want]
    print(f"{len(cursor)} {args.prefix}* keys in scope")

    planned = []
    for k in cursor:
        p = plan_key(k)
        if p is not None:
            planned.append((k, p))
    if args.limit:
        planned = planned[:args.limit]
    print(f"{len(planned)} keys need update (target alias -> {TARGET})")
    for k, p in planned[:5]:
        print(f"  e.g. {k.get('key_alias')}: +alias {ALIAS_NAMES} -> {TARGET}; "
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
        if any(al.get(n) != TARGET for n in ALIAS_NAMES) or not set(MODELS_UNION) <= ml:
            bad += 1
            print(f"  READBACK MISMATCH {k.get('key_alias')}")
    print(f"readback: {len(planned) - bad}/{len(planned)} verified, {bad} mismatch")


if __name__ == "__main__":
    main()
