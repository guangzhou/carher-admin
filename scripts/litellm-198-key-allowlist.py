#!/usr/bin/env python3
"""Bulk edit the ``models`` allowlist and per-key ``aliases`` of 198 LiteLLM keys.

Generalises the one-off scripts (``litellm-cursor-add-astra.py``,
``litellm-198-add-sa-kimi-k3.py``) into a single reusable tool.

Four invariants this encodes, each learned the hard way:

* ``/key/update`` replaces the whole ``models`` field *and* the whole
  ``aliases`` field, so every write is read-merge-write. Never send a bare
  list or a bare dict. (Fields you omit entirely are left alone -- only the
  ones you send get replaced.)
* ``models == []`` means *unrestricted*. Such keys never get a ``models``
  write: putting an allowlist on them would take access away, not grant it.
  They can still take an alias-only patch, which narrows nothing.
* An allowlist grants *access*, it does not route traffic. Putting a model here
  is safe for a scarce upstream; putting it in a fallback chain is not.
* Add and remove in one call. ``--add-model`` / ``--rm-model`` / ``--alias``
  all land in a single ``/key/update`` per key, so a rename never leaves a
  window where the user's old name is gone and the new one is not there yet.

The allowlist gate is checked against the name the *client* sent, before the
alias rewrite (measured 2026-09-08 with a throwaway key). So to expose one
public name backed by a private model group you put only the public name in
``models`` and map it with ``--alias public=private``.

Usage (run on 198, where 127.0.0.1:30402 is the proxy NodePort)::

    export LITELLM_MASTER_KEY=$(kubectl -n litellm-product get secret litellm-secrets \
      -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)

    # 1. dry-run (default): shows scope, never writes
    python3 litellm-198-key-allowlist.py --prefix cursor- \
        --rm-model sa-kimi-k3 --rm-model sa-kimi-k3-responses \
        --add-model kimi-k3 --alias kimi-k3=sa-kimi-k3-responses
    # 2. canary one key, then verify by probing it
    python3 litellm-198-key-allowlist.py ... --limit 1 \
        --apply --backup ~/k3-canary-$(date +%Y%m%dT%H%M%S).json
    # 3. full rollout
    python3 litellm-198-key-allowlist.py ... \
        --apply --backup ~/k3-full-$(date +%Y%m%dT%H%M%S).json
    # 4. rollback from any snapshot taken above (restores models AND aliases)
    python3 litellm-198-key-allowlist.py --restore ~/k3-full-<ts>.json --apply

``--model`` is a legacy spelling of ``--add-model``; with ``--remove`` it means
``--rm-model``.
"""
from __future__ import annotations

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


def list_all_keys(verbose: bool = False) -> list[dict]:
    """Every key, deduped by token.

    A single pass is not authoritative. Measured 2026-09-08: a run reporting
    ``planned=1235 applied_ok=1235 applied_fail=0`` left 218 keys unchanged in
    psql, and the built-in readback -- which walks the same listing -- only
    flagged 103 of them. Re-running converged in three passes.

    The established cause is a *concurrent* bulk writer: another session's
    read-merge-write, built on a snapshot taken before ours, wrote the old
    ``models`` back. Whether ``/key/list`` paging is itself unstable is NOT
    established -- a later run in a quiet window walked the full table with
    zero duplicates and zero loss, which argues against it.

    So the dedup below is a detector, not a fix: with ``verbose`` it prints a
    count if the same token ever comes back twice. As of 2026-09-09 it has
    never fired. Regardless of cause, callers must re-run to convergence and
    judge by psql, never by a single pass's ``applied_ok``.
    """
    seen: dict[str, dict] = {}
    raw = 0
    for page in range(1, 1000):
        data = api("GET", f"/key/list?page={page}&size=100&return_full_object=true")
        keys = data.get("keys") or []
        if not keys:
            break
        for key in keys:
            if isinstance(key, dict) and key.get("token"):
                raw += 1
                seen[key["token"]] = key
    if verbose and raw != len(seen):
        print(f"key_list_duplicates={raw - len(seen)} raw_rows={raw} unique={len(seen)}",
              file=sys.stderr)
    return list(seen.values())


def select_keys(
    keys: list[dict],
    prefixes: tuple[str, ...],
    include_blocked: bool = False,
) -> list[dict]:
    """Keys whose alias starts with one of ``prefixes``.

    Blocked keys are skipped by default: they cannot authenticate, so writing
    to them is noise during a normal rollout. ``include_blocked`` is for the
    one case where they matter -- *revoking* a model. A blocked key keeps its
    allowlist, so unblocking it later silently restores whatever was left
    behind.
    """
    return [
        key for key in keys
        if str(key.get("key_alias") or "").startswith(prefixes)
        and (include_blocked or not key.get("blocked"))
    ]


def plan_key(
    key: dict,
    add_models: list[str] | tuple[str, ...] = (),
    rm_models: list[str] | tuple[str, ...] = (),
    set_aliases: dict[str, str] | None = None,
    rm_aliases: list[str] | tuple[str, ...] = (),
) -> dict | None:
    """Return the single ``/key/update`` patch for one key, or None if it is
    already in the desired state.

    ``models`` is only ever included for restricted keys -- an unrestricted key
    (``models == []``) is left unrestricted, see module docstring.
    """
    patch: dict = {}

    current = list(key.get("models") or [])
    if current:
        drop = set(rm_models)
        wanted = [item for item in current if item not in drop]
        wanted = list(dict.fromkeys(wanted + list(add_models)))
        if wanted != current:
            patch["models"] = wanted

    current_aliases = dict(key.get("aliases") or {})
    wanted_aliases = dict(current_aliases)
    for name in rm_aliases:
        wanted_aliases.pop(name, None)
    wanted_aliases.update(set_aliases or {})
    if wanted_aliases != current_aliases:
        patch["aliases"] = wanted_aliases

    return patch or None


def restore(path: str, do_apply: bool) -> int:
    with open(path, encoding="utf-8") as handle:
        snapshot = json.load(handle)
    print(f"restore_from={path} keys={len(snapshot)}")
    if not do_apply:
        print("dry-run: no writes")
        return 0
    failures = 0
    for token, saved in snapshot.items():
        body = {"key": token}
        # Only restore what the snapshot actually captured, so a models-only
        # snapshot never clobbers aliases (and vice versa).
        if "models" in saved:
            body["models"] = saved["models"]
        if "aliases" in saved:
            body["aliases"] = saved["aliases"]
        try:
            api("POST", "/key/update", body)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {saved.get('key_alias')}: {exc}", file=sys.stderr)
            if failures >= 2:
                print("aborting after two write failures", file=sys.stderr)
                return 3
    print(f"restored_ok={len(snapshot) - failures} restored_fail={failures}")
    return 0 if not failures else 3


def parse_alias(spec: str) -> tuple[str, str]:
    if "=" not in spec:
        raise argparse.ArgumentTypeError(f"--alias wants src=dst, got {spec!r}")
    src, dst = spec.split("=", 1)
    if not src or not dst:
        raise argparse.ArgumentTypeError(f"--alias wants src=dst, got {spec!r}")
    return src, dst


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="append", default=[],
                        help="legacy spelling of --add-model (--rm-model with --remove)")
    parser.add_argument("--add-model", action="append", default=[],
                        help="model name to add to the allowlist (repeatable)")
    parser.add_argument("--rm-model", action="append", default=[],
                        help="model name to remove from the allowlist (repeatable)")
    parser.add_argument("--alias", action="append", default=[], type=parse_alias,
                        help="per-key alias src=dst (repeatable)")
    parser.add_argument("--rm-alias", action="append", default=[],
                        help="per-key alias source name to drop (repeatable)")
    parser.add_argument("--remove", action="store_true",
                        help="legacy: treat --model as --rm-model")
    parser.add_argument("--prefix", action="append", default=[],
                        help=f"key_alias prefix to scope to (default: {' '.join(DEFAULT_PREFIXES)})")
    parser.add_argument("--include-blocked", action="store_true",
                        help="also write blocked keys (they keep their allowlist, so a "
                             "revoked model comes back if the key is ever unblocked)")
    parser.add_argument("--only", action="append", default=[],
                        help="restrict to exact key_alias values")
    parser.add_argument("--limit", type=int, help="canary: write at most N keys")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", help="required with --apply")
    parser.add_argument("--restore", help="restore models+aliases from a backup snapshot")
    args = parser.parse_args()

    if args.restore:
        return restore(args.restore, args.apply)

    add_models = list(args.add_model)
    rm_models = list(args.rm_model)
    (rm_models if args.remove else add_models).extend(args.model)
    set_aliases = dict(args.alias)

    if not (add_models or rm_models or set_aliases or args.rm_alias):
        print("nothing to do: pass --add-model/--rm-model/--alias/--rm-alias (or --restore)",
              file=sys.stderr)
        return 2
    overlap = set(add_models) & set(rm_models)
    if overlap:
        print(f"same model both added and removed: {sorted(overlap)}", file=sys.stderr)
        return 2

    prefixes = tuple(args.prefix) if args.prefix else DEFAULT_PREFIXES
    keys = select_keys(list_all_keys(verbose=True), prefixes, args.include_blocked)
    if args.only:
        wanted = set(args.only)
        keys = [key for key in keys if key.get("key_alias") in wanted]

    planned = [
        (key, plan_key(key, add_models, rm_models, set_aliases, args.rm_alias))
        for key in keys
    ]
    planned = [(key, plan) for key, plan in planned if plan is not None]
    unrestricted = sum(not (key.get("models") or []) for key in keys)
    blocked_in_scope = sum(bool(key.get("blocked")) for key in keys)
    # Counted before --limit truncation, otherwise the skipped tail would be
    # misreported as "already in the desired state".
    no_change = len(keys) - len(planned)
    if args.limit:
        planned = planned[:args.limit]

    print(f"scope_prefixes={list(prefixes)} scoped_keys={len(keys)} planned={len(planned)} "
          f"no_change={no_change} unrestricted_in_scope={unrestricted} "
          f"include_blocked={args.include_blocked} blocked_in_scope={blocked_in_scope}")
    print(f"add_models={add_models} rm_models={rm_models} "
          f"set_aliases={set_aliases} rm_aliases={list(args.rm_alias)}")
    if not args.apply:
        for key, plan in planned[:3]:
            print(f"  sample {key.get('key_alias')}: "
                  f"models {len(key.get('models') or [])}->{len(plan.get('models', key.get('models') or []))} "
                  f"aliases {len(key.get('aliases') or {})}->{len(plan.get('aliases', key.get('aliases') or {}))}")
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

    after = {key.get("token"): key for key in list_all_keys(verbose=True)}
    mismatches = []
    for key, _plan in planned:
        fresh = after.get(key.get("token"), {})
        models = set(fresh.get("models") or [])
        aliases = dict(fresh.get("aliases") or {})
        ok = True
        if fresh.get("models"):  # unrestricted keys have nothing to assert here
            ok &= set(add_models) <= models
            ok &= models.isdisjoint(rm_models)
        ok &= all(aliases.get(src) == dst for src, dst in set_aliases.items())
        ok &= all(src not in aliases for src in args.rm_alias)
        if not ok:
            mismatches.append(key.get("key_alias"))
    print(f"readback_ok={len(planned) - len(mismatches)}/{len(planned)} mismatches={len(mismatches)}")
    if mismatches:
        print("mismatch_aliases=" + ",".join(str(item) for item in mismatches[:20]), file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
