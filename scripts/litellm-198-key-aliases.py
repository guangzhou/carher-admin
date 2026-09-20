#!/usr/bin/env python3
"""Bulk add or remove entries in the ``aliases`` map of 198 LiteLLM keys.

Companion to ``litellm-198-key-allowlist.py``. That script edits ``models``
(the allowlist -- who is *allowed* to call a group); this one edits ``aliases``
(the per-key rewrite -- what a client-facing model name *resolves to*).

Discipline, same as the allowlist script:

* ``/key/update`` replaces whole fields, so every write is read-merge-write:
  fetch the key's current ``aliases``, apply the ops, send the merged map back.
* Keys are selected by ``key_alias`` prefix (default ``cursor-`` / ``claude-``).
* ``--apply`` requires ``--backup``; the snapshot holds each touched key's
  original ``aliases`` and feeds ``--restore``.
* Every write is read back and compared; mismatches are reported and counted.

Unlike the allowlist script there is **no unrestricted-key carve-out**: an empty
``aliases`` map means "no rewrites", not "all rewrites", so adding to it is safe.

Usage::

    export LITELLM_MASTER_KEY=...
    # add / repoint (repeatable NAME=TARGET)
    python3 litellm-198-key-aliases.py --set glm-5.3-flash=zai-coding-glm-5.3-flash
    # remove by exact alias name (repeatable)
    python3 litellm-198-key-aliases.py --unset glm-5.2 --unset glm-5.2-low
    # only rewrite when it currently points at a given target (safety net)
    python3 litellm-198-key-aliases.py --unset gpt-5.2 --only-if-target zai-coding-glm-5.3

``--set`` overwrites an existing mapping for that name; ``--unset`` drops it.
Both default to dry-run.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("LITELLM_BASE", "http://127.0.0.1:30402").rstrip("/")
MASTER = os.environ.get("LITELLM_MASTER_KEY", "")

DEFAULT_PREFIXES = ("cursor-", "claude-")


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


def select_keys(keys: list[dict], prefixes: tuple[str, ...]) -> list[dict]:
    """Non-blocked keys whose alias starts with one of ``prefixes``."""
    return [
        key for key in keys
        if str(key.get("key_alias") or "").startswith(prefixes)
        and not key.get("blocked")
    ]


def plan_key(
    key: dict,
    sets: dict[str, str],
    unsets: list[str],
    only_if_target: str | None = None,
) -> dict | None:
    """Return the ``aliases`` patch for one key, or None when nothing to do.

    ``only_if_target`` restricts both ops to names whose *current* value equals
    it -- used to repoint a stray mapping without touching same-named aliases
    that legitimately point elsewhere.
    """
    current = dict(key.get("aliases") or {})
    wanted = dict(current)

    for name, target in sets.items():
        if only_if_target is not None and current.get(name) != only_if_target:
            continue
        wanted[name] = target

    for name in unsets:
        if name not in wanted:
            continue
        if only_if_target is not None and current.get(name) != only_if_target:
            continue
        del wanted[name]

    if wanted == current:
        return None
    return {"aliases": wanted}


def restore(path: str, do_apply: bool) -> int:
    with open(path, encoding="utf-8") as handle:
        snapshot = json.load(handle)
    print(f"restore_from={path} keys={len(snapshot)}")
    if not do_apply:
        print("dry-run: no writes")
        return 0
    failures = 0
    for token, saved in snapshot.items():
        try:
            api("POST", "/key/update", {"key": token, "aliases": saved["aliases"]})
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {saved.get('key_alias')}: {exc}", file=sys.stderr)
            if failures >= 2:
                print("aborting after two write failures", file=sys.stderr)
                return 3
    print(f"restored_ok={len(snapshot) - failures} restored_fail={failures}")
    return 0 if not failures else 3


def parse_set(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"--set needs NAME=TARGET, got {raw!r}")
    name, target = raw.split("=", 1)
    name, target = name.strip(), target.strip()
    if not name or not target:
        raise argparse.ArgumentTypeError(f"--set needs NAME=TARGET, got {raw!r}")
    return name, target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--set", action="append", default=[], type=parse_set,
                        metavar="NAME=TARGET",
                        help="add or repoint an alias; repeatable")
    parser.add_argument("--unset", action="append", default=[], metavar="NAME",
                        help="drop an alias by exact name; repeatable")
    parser.add_argument("--only-if-target", metavar="GROUP",
                        help="only act on names currently pointing at GROUP")
    parser.add_argument("--prefix", action="append", default=[],
                        help=f"key_alias prefix scope (default {DEFAULT_PREFIXES})")
    parser.add_argument("--only", action="append", default=[],
                        help="operate on these key_alias values only")
    parser.add_argument("--limit", type=int, help="canary: write at most N keys")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", help="required with --apply")
    parser.add_argument("--restore", help="restore aliases from a backup snapshot")
    args = parser.parse_args()

    if not MASTER:
        print("LITELLM_MASTER_KEY not set", file=sys.stderr)
        return 2
    if args.restore:
        return restore(args.restore, args.apply)
    if not args.set and not args.unset:
        print("nothing to do: pass --set and/or --unset", file=sys.stderr)
        return 2
    if args.apply and not args.backup:
        print("--apply requires --backup", file=sys.stderr)
        return 2

    sets = dict(args.set)
    prefixes = tuple(args.prefix) if args.prefix else DEFAULT_PREFIXES

    keys = list_all_keys()
    if args.only:
        wanted = set(args.only)
        scoped = [k for k in keys if str(k.get("key_alias") or "") in wanted]
    else:
        scoped = select_keys(keys, prefixes)

    planned = [
        (key, patch)
        for key, patch in (
            (k, plan_key(k, sets, args.unset, args.only_if_target)) for k in scoped
        )
        if patch is not None
    ]
    no_change = len(scoped) - len(planned)

    if args.limit is not None:
        planned = planned[:args.limit]

    print(f"scope_prefixes={list(prefixes)} scoped_keys={len(scoped)} "
          f"planned={len(planned)} no_change={no_change}")
    print(f"set={sets} unset={args.unset} only_if_target={args.only_if_target}")

    if not args.apply:
        for key, patch in planned[:5]:
            before = dict(key.get("aliases") or {})
            after = patch["aliases"]
            added = {k: v for k, v in after.items() if before.get(k) != v}
            dropped = [k for k in before if k not in after]
            print(f"  e.g. {key.get('key_alias')}: +{added} -{dropped}")
        print("dry-run: no writes")
        return 0

    snapshot = {
        key["token"]: {"key_alias": key.get("key_alias"),
                       "aliases": dict(key.get("aliases") or {})}
        for key, _ in planned
    }
    with open(args.backup, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=1, sort_keys=True)
    print(f"backup={args.backup} keys={len(snapshot)}")

    failures = 0
    for key, patch in planned:
        try:
            api("POST", "/key/update", {"key": key["token"], **patch})
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {key.get('key_alias')}: {exc}", file=sys.stderr)
            if failures >= 2:
                print("aborting after two write failures", file=sys.stderr)
                return 3
    print(f"applied_ok={len(planned) - failures} applied_fail={failures}")

    fresh = {k["token"]: k for k in list_all_keys()}
    ok = mismatches = 0
    for key, patch in planned:
        live = dict((fresh.get(key["token"]) or {}).get("aliases") or {})
        if live == patch["aliases"]:
            ok += 1
        else:
            mismatches += 1
            if mismatches <= 5:
                print(f"MISMATCH {key.get('key_alias')}: live={live} "
                      f"wanted={patch['aliases']}", file=sys.stderr)
    print(f"readback_ok={ok}/{len(planned)} mismatches={mismatches}")
    return 0 if not failures and not mismatches else 3


if __name__ == "__main__":
    raise SystemExit(main())
