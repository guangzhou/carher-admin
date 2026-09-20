#!/usr/bin/env python3
"""Add GPT-6 Astra to every scoped 198 Cursor key.

The key API replaces whole ``models``/``aliases`` fields, so every update is
read-merge-write. Empty ``models`` means unrestricted and is intentionally
skipped. Cursor clients may send either the bare product name or the pool name;
the bare name is mapped to the ChatGPT Astra pool while both names are added to
the allowlist.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:4000").rstrip("/")
# Keep pure planning helpers importable by offline tests; CLI execution still
# requires the admin key before making any API request.
MASTER = os.environ.get("LITELLM_MASTER_KEY", "")

PREFIX = "cursor-"
MODEL_NAME = "chatgpt-gpt-6-astra"
MODEL_NAMES = ("gpt-6-astra", MODEL_NAME)
ALIASES = {"gpt-6-astra": MODEL_NAME}


def api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {MASTER}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def list_all_keys() -> list[dict]:
    result: list[dict] = []
    for page in range(1, 1000):
        data = api("GET", f"/key/list?page={page}&size=100&return_full_object=true")
        keys = data.get("keys") or []
        if not keys:
            break
        result.extend(key for key in keys if isinstance(key, dict))
    return result


def plan_key(key: dict) -> dict | None:
    current_models = list(key.get("models") or [])
    if not current_models:
        return None
    current_aliases = dict(key.get("aliases") or {})
    aliases = dict(current_aliases)
    aliases.update(ALIASES)
    models = list(dict.fromkeys(current_models + list(MODEL_NAMES)))
    if aliases == current_aliases and models == current_models:
        return None
    return {"aliases": aliases, "models": models}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", required=False)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    keys = [key for key in list_all_keys() if str(key.get("key_alias") or "").startswith(PREFIX)]
    if args.only:
        wanted = set(args.only)
        keys = [key for key in keys if key.get("key_alias") in wanted]
    planned = [(key, plan_key(key)) for key in keys]
    planned = [(key, plan) for key, plan in planned if plan is not None]
    if args.limit:
        planned = planned[:args.limit]
    unrestricted = sum(not (key.get("models") or []) for key in keys)
    print(f"cursor_keys={len(keys)} planned={len(planned)} unrestricted_skipped={unrestricted}")
    print(f"models_added={list(MODEL_NAMES)} aliases_added={ALIASES}")
    if not args.apply:
        print("dry-run: no writes")
        return 0
    if not args.backup:
        print("--apply requires --backup", file=sys.stderr)
        return 2

    snapshot = {
        key["token"]: {
            "key_alias": key.get("key_alias"),
            "models": list(key.get("models") or []),
            "aliases": dict(key.get("aliases") or {}),
        }
        for key, _ in planned
    }
    with open(args.backup, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
    print(f"backup={args.backup} keys={len(snapshot)}")

    success = failures = 0
    for key, plan in planned:
        try:
            api("POST", "/key/update", {"key": key["token"], **plan})
            success += 1
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {key.get('key_alias')}: {exc}", file=sys.stderr)
            if failures >= 2:
                print("aborting after two write failures; backup is available", file=sys.stderr)
                return 3
    print(f"applied_ok={success} applied_fail={failures}")

    after = {key.get("token"): key for key in list_all_keys()}
    mismatches = []
    for key, plan in planned:
        current = after.get(key.get("token"), {})
        aliases = current.get("aliases") or {}
        models = set(current.get("models") or [])
        if any(aliases.get(source) != target for source, target in ALIASES.items()) or not set(MODEL_NAMES) <= models:
            mismatches.append(key.get("key_alias"))
    print(f"readback_ok={len(planned) - len(mismatches)}/{len(planned)} mismatches={len(mismatches)}")
    if mismatches:
        print("mismatch_aliases=" + ",".join(str(item) for item in mismatches[:20]), file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
