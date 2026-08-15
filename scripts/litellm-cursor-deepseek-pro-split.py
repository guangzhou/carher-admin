#!/usr/bin/env python3
"""Split Cursor DeepSeek aliases: pro goes to real Pro, flash stays Flash.

Background (2026-08-15). The 2026-08-12 batch pinned BOTH client product
names (``deepseek-v4-pro`` and ``claude-deepseek-v4-pro``) to the official
Flash group because that was the only official-direct deployment at the
time. Official Pro (``deepseek-official/deepseek-v4-pro``) is now registered
on 198, and users want Pro and Flash as two distinct models in Cursor /
Xcode / Codex. This script flips the two aliases on every ``cursor-*`` key:

  * ``deepseek-v4-pro`` -> alias REMOVED (the bare name then resolves to the
    real ``deepseek-v4-pro`` group; verified 2026-08-15 that the global
    router model_group_alias has zero deepseek entries, so nothing else
    redirects it)
  * ``claude-deepseek-v4-pro`` -> ``deepseek-v4-pro`` (Cursor's Java client
    requests the claude- prefixed name; without the alias it would land on
    the wangsu ``claude/deepseek-v4-pro`` deployment)
  * models allowlist: union ``deepseek-v4-pro``

Safety rails (same as litellm-cursor-deepseek-official.py):
  * only rewrites alias entries whose CURRENT value is ``deepseek-v4-flash``
    — hand-customized keys are left untouched
  * keys with ``models == []`` (unrestricted) are skipped, never narrowed
  * default dry-run; --apply requires --backup; two consecutive write
    failures abort the batch; full readback verification afterwards

Codex note: with the bare alias removed, a Codex ``/v1/responses`` request
for ``deepseek-v4-pro`` now reaches the ``deepseek_responses_adapt`` rewrite
un-aliased and lands on ``deepseek-v4-pro-responses`` (real Pro) instead of
the Flash group — consistent with the split.

Examples:
    python3 scripts/litellm-cursor-deepseek-pro-split.py
    python3 scripts/litellm-cursor-deepseek-pro-split.py --only cursor-zhoujinman-sram \
        --backup ~/cursor-pro-split-canary.json --apply
    python3 scripts/litellm-cursor-deepseek-pro-split.py \
        --backup ~/cursor-pro-split.json --apply
    python3 scripts/litellm-cursor-deepseek-pro-split.py \
        --restore ~/cursor-pro-split.json --apply

Environment: LITELLM_BASE (default http://127.0.0.1:30402), LITELLM_MASTER_KEY.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
import time
from typing import Any

_SIB = pathlib.Path(__file__).with_name("zerokey-pioneer-key-sync.py")
_SPEC = importlib.util.spec_from_file_location("cursor_key_sync", _SIB)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"cannot load {_SIB}")
_SYNC = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SYNC)
api = _SYNC.api
list_all_keys = _SYNC.list_all_keys

BARE_ALIAS = "deepseek-v4-pro"
PREFIXED_ALIAS = "claude-deepseek-v4-pro"
OLD_TARGET = "deepseek-v4-flash"
PRO_GROUP = "deepseek-v4-pro"


def _selected(value: str | None) -> set[str]:
    return {item.strip() for item in (value or "").split(",") if item.strip()}


def plan_key(key: dict[str, Any]) -> dict[str, Any] | None:
    """Return merged aliases/models for /key/update, or None when no-op/unsafe."""
    current_models = list(key.get("models") or [])
    if not current_models:
        # [] means unrestricted in LiteLLM; adding a whitelist would narrow it.
        return None

    current_aliases = dict(key.get("aliases") or {})
    aliases = dict(current_aliases)
    if aliases.get(BARE_ALIAS) == OLD_TARGET:
        del aliases[BARE_ALIAS]
    if aliases.get(PREFIXED_ALIAS) == OLD_TARGET:
        aliases[PREFIXED_ALIAS] = PRO_GROUP
    models = sorted(set(current_models) | {PRO_GROUP})

    if aliases == current_aliases and models == sorted(current_models):
        return None
    return {"aliases": aliases, "models": models}


def _key_snapshot(key: dict[str, Any]) -> dict[str, Any]:
    return {
        "key_alias": key.get("key_alias"),
        "token": key.get("token"),
        "aliases": dict(key.get("aliases") or {}),
        "models": sorted(key.get("models") or []),
    }


def _assert_unique(keys: list[dict[str, Any]]) -> None:
    aliases = [str(key.get("key_alias") or "") for key in keys]
    tokens = [str(key.get("token") or "") for key in keys]
    if len(aliases) != len(set(aliases)):
        raise RuntimeError("duplicate key_alias in /key/list; refusing batch")
    if len(tokens) != len(set(tokens)):
        raise RuntimeError("duplicate token in /key/list; refusing batch")
    if any(not alias or not token or token == "None" for alias, token in zip(aliases, tokens)):
        raise RuntimeError("key_alias/token missing in /key/list; refusing batch")


def verify_keys(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> list[tuple[str, str]]:
    """Concrete invariant violations after batch readback (planned keys only)."""
    before_by_alias = {key["key_alias"]: key for key in before}
    after_by_alias = {key["key_alias"]: key for key in after}
    problems: list[tuple[str, str]] = []
    for alias, old in before_by_alias.items():
        new = after_by_alias.get(alias)
        if new is None:
            problems.append((alias, "missing"))
            continue
        old_aliases = dict(old.get("aliases") or {})
        new_aliases = dict(new.get("aliases") or {})
        if old_aliases.get(BARE_ALIAS) == OLD_TARGET and BARE_ALIAS in new_aliases:
            problems.append((alias, f"{BARE_ALIAS} alias still present"))
        if old_aliases.get(PREFIXED_ALIAS) == OLD_TARGET and new_aliases.get(PREFIXED_ALIAS) != PRO_GROUP:
            problems.append((alias, f"{PREFIXED_ALIAS} not mapped to {PRO_GROUP}"))
        touched = {BARE_ALIAS, PREFIXED_ALIAS}
        if {k: v for k, v in new_aliases.items() if k not in touched} != {
            k: v for k, v in old_aliases.items() if k not in touched
        }:
            problems.append((alias, "unrelated alias changed"))
        old_models = set(old.get("models") or [])
        new_models = set(new.get("models") or [])
        expected_add = {PRO_GROUP} if PRO_GROUP not in old_models else set()
        if new_models - old_models != expected_add or old_models - new_models:
            problems.append((alias, "unexpected model change"))
        if new.get("token") != old.get("token"):
            problems.append((alias, "token changed"))
    if set(after_by_alias) - set(before_by_alias):
        problems.extend((alias, "unexpected new key") for alias in sorted(set(after_by_alias) - set(before_by_alias)))
    return problems


def _write_backup(path: pathlib.Path, keys: list[dict[str, Any]]) -> None:
    payload = {
        "operation": {"kind": "deepseek-pro-split", "bare": BARE_ALIAS,
                      "prefixed": PREFIXED_ALIAS, "old_target": OLD_TARGET,
                      "pro_group": PRO_GROUP},
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "keys": [_key_snapshot(key) for key in keys],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _apply_plans(plans: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
    failures: list[tuple[str, str]] = []
    consecutive = 0
    for index, (key, change) in enumerate(plans, 1):
        try:
            api("POST", "/key/update", {"key": key["token"], **change})
            consecutive = 0
        except Exception as exc:  # noqa: BLE001
            failures.append((key["key_alias"], str(exc)))
            consecutive += 1
            print(f"FAIL {key['key_alias']}: {exc}", file=sys.stderr)
            if consecutive >= 2:
                raise RuntimeError("two consecutive writes failed; stopped") from exc
        if index % 50 == 0 or index == len(plans):
            print(f"progress={index}/{len(plans)} failures={len(failures)}")
    if failures:
        raise RuntimeError(f"{len(failures)} writes failed: {failures[:3]}")


def sync(args: argparse.Namespace) -> int:
    all_keys = [key for key in list_all_keys() if str(key.get("key_alias") or "").startswith(args.prefix)]
    _assert_unique(all_keys)
    only = _selected(args.only)
    exclude = _selected(args.exclude)
    selected = [key for key in all_keys if (not only or key["key_alias"] in only) and key["key_alias"] not in exclude]
    skipped_unrestricted = [key["key_alias"] for key in selected if not (key.get("models") or [])]
    writable = [key for key in selected if key["key_alias"] not in set(skipped_unrestricted)]
    plans = [(key, plan_key(key)) for key in writable]
    plans = [(key, change) for key, change in plans if change is not None]
    if args.limit:
        plans = plans[: args.limit]

    print(f"{args.prefix}* total={len(all_keys)} selected={len(selected)} "
          f"skipped_unrestricted={len(skipped_unrestricted)} to_update={len(plans)}")
    if skipped_unrestricted:
        print("skipped models==[]: " + ", ".join(skipped_unrestricted))

    planned_aliases = {key["key_alias"] for key, _ in plans}
    backup_keys = [key for key in selected if key["key_alias"] in planned_aliases]
    if args.backup:
        _write_backup(pathlib.Path(args.backup), backup_keys)
        print(f"backup={args.backup}")
    if not args.apply:
        for key, change in plans[:15]:
            print(f"[dry] {key['key_alias']}: aliases {len(key.get('aliases') or {})}->"
                  f"{len(change['aliases'])}, models {len(key.get('models') or [])}->{len(change['models'])}")
        if len(plans) > 15:
            print(f"[dry] ... {len(plans) - 15} more")
        return 0
    if not args.backup:
        raise SystemExit("--apply requires --backup <path>")

    _apply_plans(plans)
    fresh = [key for key in list_all_keys() if str(key.get("key_alias") or "").startswith(args.prefix)]
    writable_aliases = {key["key_alias"] for key, _ in plans}
    problems = verify_keys(
        [key for key, _ in plans],
        [key for key in fresh if key["key_alias"] in writable_aliases],
    )
    print(f"verify_total={len(fresh)} problems={len(problems)}")
    if problems:
        print(json.dumps(problems, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


def restore(args: argparse.Namespace) -> int:
    payload = json.loads(pathlib.Path(args.restore).read_text())
    keys = payload.get("keys") or []
    print(f"restore_count={len(keys)} source={args.restore}")
    if not args.apply:
        print("dry-run restore; add --apply to write")
        return 0
    if not keys:
        raise SystemExit("backup contains no keys")
    _apply_plans([(key, {"aliases": key["aliases"], "models": key["models"]}) for key in keys])
    fresh = list_all_keys()
    by_alias = {key["key_alias"]: key for key in fresh}
    problems = []
    for old in keys:
        new = by_alias.get(old["key_alias"])
        if not new or dict(new.get("aliases") or {}) != dict(old.get("aliases") or {}) or sorted(new.get("models") or []) != sorted(old.get("models") or []):
            problems.append(old["key_alias"])
    print(f"restore_verify_problems={len(problems)}")
    if problems:
        print(json.dumps(problems, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Split Cursor DeepSeek aliases: pro->real Pro, flash stays Flash")
    parser.add_argument("--prefix", default="cursor-")
    parser.add_argument("--only")
    parser.add_argument("--exclude")
    parser.add_argument("--backup")
    parser.add_argument("--restore")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.restore:
        if args.only or args.exclude or args.limit:
            raise SystemExit("--restore cannot combine with --only/--exclude/--limit")
        return restore(args)
    return sync(args)


if __name__ == "__main__":
    raise SystemExit(main())
