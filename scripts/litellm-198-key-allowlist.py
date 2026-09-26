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

To make keys *identical to an existing key* ("改成和 std 一样"), use
``--converge-to`` rather than hand-computing the difference into --rm-model /
--rm-alias flags. It reads the desired state off the reference, so a missing
name is not a possible failure mode, and it refuses to run when the reference is
inside the write scope (a no-op would pad ``applied_ok``) or is unrestricted
(that would grant everyone everything)::

    python3 litellm-198-key-allowlist.py --only cursor-alice-1 --only cursor-bob-2 \
        --converge-to cursor-liuguoxian-std \
        --apply --backup ~/recurate-$(date -u +%Y%m%dT%H%M%S).json

Converging a key that already carries a fuller alias map can silently move that
user to a different provider -- see ``upstream_changes``; those cases are
refused unless ``--allow-upstream-change`` is passed.
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
    exclude: tuple[str, ...] = (),
    exclude_prefixes: tuple[str, ...] = (),
) -> list[dict]:
    """Keys whose alias starts with one of ``prefixes``.

    Blocked keys are skipped by default: they cannot authenticate, so writing
    to them is noise during a normal rollout. ``include_blocked`` is for the
    one case where they matter -- *revoking* a model. A blocked key keeps its
    allowlist, so unblocking it later silently restores whatever was left
    behind.

    ``exclude`` (exact alias) and ``exclude_prefixes`` carve holes out of a
    prefix sweep -- "every cursor key **except** mine". Spelling that as 600+
    ``--only`` arguments is what it replaces: a list that long gets re-split by
    the remote shell, argparse keeps the first and silently turns the rest into
    positional junk, and the write lands on one key instead of 600.
    ⚠️ Carve by *prefix*, not by exact name, whenever the excluded keys may
    carry the random 4-char suffix the key issuer appends
    (``cursor-liuguoxian`` matches ``cursor-liuguoxian-l08v``): an exact-name
    exclusion list silently misses the suffixed twin and writes to it.
    """
    return [
        key for key in keys
        if str(key.get("key_alias") or "").startswith(prefixes)
        and (include_blocked or not key.get("blocked"))
        and str(key.get("key_alias") or "") not in set(exclude)
        and not (exclude_prefixes
                 and str(key.get("key_alias") or "").startswith(exclude_prefixes))
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


def plan_converge(
    key: dict,
    ref_models: list[str] | tuple[str, ...],
    ref_aliases: dict[str, str],
) -> dict | None:
    """Patch that makes one key's ``models`` + ``aliases`` identical to a reference key.

    This is the "把这把 key 改成和 <参考 key> 一样" operation. Doing it with
    ``--rm-model`` means hand-computing the difference (2026-09-21: 36 removals
    to get from the 68-name default template down to a curated 32), and a
    hand-computed list has to be proved complete with ``comm -23`` before every
    write. Reading the desired state off the reference key removes both.

    ``ref_models`` order is preserved verbatim, so a converged key is
    byte-identical to the reference and the acceptance criterion can be
    ``count(distinct md5(models::text)||md5(aliases::text)) = 1``.

    An unrestricted key (``models == []``) is still never narrowed; it only
    takes the alias half.
    """
    patch: dict = {}
    if key.get("models") or []:
        wanted = list(ref_models)
        if wanted != list(key["models"]):
            patch["models"] = wanted
    current_aliases = dict(key.get("aliases") or {})
    if dict(ref_aliases) != current_aliases:
        patch["aliases"] = dict(ref_aliases)
    return patch or None


def upstream_changes(
    key: dict,
    ref_models: list[str] | tuple[str, ...],
    ref_aliases: dict[str, str],
) -> list[tuple[str, str, str | None]]:
    """Alias entries this convergence drops or retargets *while the source name
    stays callable* -- the user keeps the name and silently lands elsewhere.

    Converging a key that already carries the 22-alias standard template onto a
    3-alias reference removes 19 aliases. Most are harmless: the source name
    leaves the allowlist too, so the call 403s instead of quietly changing
    upstream. The dangerous subset is the one where the name survives -- then
    there is no error at all, only a different provider. 2026-09-21 this was 3
    entries on one key and needed a human decision, not a write.

    Returns ``(src, old_dst, new_dst_or_None)`` tuples.
    """
    out: list[tuple[str, str, str | None]] = []
    final_models = set(ref_models) if (key.get("models") or []) else None
    for src, old_dst in sorted(dict(key.get("aliases") or {}).items()):
        new_dst = ref_aliases.get(src)
        if new_dst == old_dst:
            continue
        # An unrestricted key keeps every name callable, so it is always exposed.
        if final_models is None or src in final_models:
            out.append((src, old_dst, new_dst))
    return out


def dropped_names(
    key: dict,
    ref_models: list[str] | tuple[str, ...],
) -> list[str]:
    """Names this convergence removes from the allowlist -- the loud half.

    ``upstream_changes`` covers the *silent* failure mode (the name survives and
    quietly lands on another provider). This covers the other one: the name
    leaves ``models`` entirely, so the user's very next call gets a 403.

    Until 2026-09-26 nothing reported this, and it was the larger user-facing
    impact of that day's 688-key sweep: ``anthropic.claude-fable-5`` (1030 req /
    9 keys over 14d) left the allowlist with **no equivalent left on the
    reference** -- kiro carries no fable family at all. The silent-swap gate
    fired loudly about GPT-5.6 retargets while this went unmentioned.

    Loud is not the same as harmless. A 403 is visible to the user but invisible
    to the operator running the sweep, and "which of these names still has an
    equivalent under the new shape" is a judgement only a human can make. So
    this is *reported*, never gated: the point is that the operator measures the
    14d traffic on these names **before** writing, not that the script guesses.

    ⚠️ Measuring that traffic has its own trap: ``LiteLLM_SpendLogs`` records the
    **post-rewrite** group name, so when several alias sources point at one
    target you cannot tell which name the client actually typed. Per-name
    request counts reverse-mapped that way are an **upper bound**, not a count.

    An unrestricted key (``models == []``) is never narrowed, so it drops nothing.
    """
    current = list(key.get("models") or [])
    if not current:
        return []
    return sorted(set(current) - set(ref_models))


def convergence_residual(
    all_keys: list[dict],
    prefixes: tuple[str, ...],
    reference: str,
    ref_models: list[str] | tuple[str, ...],
    ref_aliases: dict[str, str],
    exclude: tuple[str, ...] = (),
    exclude_prefixes: tuple[str, ...] = (),
    include_blocked: bool = False,
) -> dict[str, list[str]]:
    """Every key in the *prefix* scope that is still not identical to the reference.

    ``applied_ok``/``readback_ok`` only ever speak about the keys the run
    *planned*. They cannot answer "是不是全部改完了", because the scope itself is
    narrowed first -- and a key silently outside the scope reads exactly like a
    key that needed no change.

    2026-09-26: a 688-key sweep printed ``applied_ok=668 readback_ok=668/668
    mismatches=0`` -- flawless, and wrong as an answer to "全部". 19 keys were
    ``blocked=t`` and therefore never in scope (``--include-blocked`` is off by
    default), and 4 were unrestricted and structurally unconvergeable. Only an
    independent full-table readback found them. This function is that readback,
    folded back into the tool so the next operator does not have to know to
    write it.

    Buckets are ordered by precedence, so each key appears exactly once:

    ``spared``        explicitly carved out by ``--exclude``/``--exclude-prefix``
                      (plus the reference itself) -- intended, no action.
    ``blocked``       skipped because ``--include-blocked`` was not passed. They
                      keep their old allowlist, so unblocking one later silently
                      restores the pre-sweep shape.
    ``unrestricted``  ``models == []``; never narrowed by design, so it takes the
                      alias half and stops there. It will never equal the
                      reference and that is correct, not a failure.
    ``mismatch``      in scope, writable, still different. **This is the only
                      bucket that means something went wrong.**

    A key that already equals the reference is in **no** bucket, whatever its
    flags -- the buckets and ``converged_identical`` partition the scope. The
    first revision bucketed by flag first, so the 19 blocked keys that had just
    been converged with ``--include-blocked`` were counted as identical *and*
    listed under ``blocked``: the same key reported twice, with opposite
    verdicts. The identity test has to come first.
    """
    spared_exact = set(exclude) | {reference}
    buckets: dict[str, list[str]] = {
        "spared": [], "blocked": [], "unrestricted": [], "mismatch": [],
    }
    for key in all_keys:
        alias = str(key.get("key_alias") or "")
        if not alias.startswith(prefixes):
            continue
        if (list(key.get("models") or []) == list(ref_models)
                and dict(key.get("aliases") or {}) == dict(ref_aliases)):
            continue
        if alias in spared_exact or (exclude_prefixes and alias.startswith(exclude_prefixes)):
            buckets["spared"].append(alias)
            continue
        if key.get("blocked") and not include_blocked:
            buckets["blocked"].append(alias)
            continue
        if not (key.get("models") or []):
            buckets["unrestricted"].append(alias)
            continue
        buckets["mismatch"].append(alias)
    return {name: sorted(values) for name, values in buckets.items()}


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
    parser.add_argument("--exclude", action="append", default=[],
                        help="carve an exact key_alias out of the prefix sweep (repeatable)")
    parser.add_argument("--exclude-prefix", action="append", default=[],
                        help="carve a key_alias prefix out of the prefix sweep (repeatable); "
                             "use this rather than --exclude when the keys to spare may "
                             "carry the issuer's random 4-char suffix")
    parser.add_argument("--converge-to", metavar="KEY_ALIAS",
                        help="make every selected key's models+aliases identical to this "
                             "reference key (instead of --add-model/--rm-model diffing). "
                             "Refuses to run if the reference is unrestricted or not found.")
    parser.add_argument("--allow-upstream-change", action="store_true",
                        help="with --converge-to: proceed even though some key keeps a name "
                             "whose alias target changes or disappears (silent provider swap)")
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

    if args.converge_to:
        if add_models or rm_models or set_aliases or args.rm_alias:
            print("--converge-to is exclusive with --add-model/--rm-model/--alias/--rm-alias",
                  file=sys.stderr)
            return 2
    elif not (add_models or rm_models or set_aliases or args.rm_alias):
        print("nothing to do: pass --add-model/--rm-model/--alias/--rm-alias "
              "(or --converge-to, or --restore)", file=sys.stderr)
        return 2
    overlap = set(add_models) & set(rm_models)
    if overlap:
        print(f"same model both added and removed: {sorted(overlap)}", file=sys.stderr)
        return 2

    prefixes = tuple(args.prefix) if args.prefix else DEFAULT_PREFIXES
    all_keys = list_all_keys(verbose=True)
    keys = select_keys(all_keys, prefixes, args.include_blocked,
                       tuple(args.exclude), tuple(args.exclude_prefix))
    # Report what the carve-out actually caught: an --exclude/--exclude-prefix
    # that matches nothing is a typo, and a typo here means writing to the very
    # keys the caller meant to spare.
    if args.exclude or args.exclude_prefix:
        spared = select_keys(all_keys, prefixes, args.include_blocked)
        spared_aliases = sorted(
            {str(k.get("key_alias")) for k in spared}
            - {str(k.get("key_alias")) for k in keys}
        )
        print(f"excluded={len(spared_aliases)} {spared_aliases}")
        if not spared_aliases:
            print("refusing: --exclude/--exclude-prefix matched 0 keys in scope -- "
                  "a typo here writes to the keys you meant to spare", file=sys.stderr)
            return 2
    if args.only:
        wanted = set(args.only)
        keys = [key for key in keys if key.get("key_alias") in wanted]

    ref_models: list[str] = []
    ref_aliases: dict[str, str] = {}
    if args.converge_to:
        # The reference is looked up across the whole table, not the selected
        # scope -- it is usually excluded by --only.
        refs = [k for k in all_keys if k.get("key_alias") == args.converge_to]
        if len(refs) != 1:
            print(f"--converge-to {args.converge_to!r} matched {len(refs)} keys, need exactly 1",
                  file=sys.stderr)
            return 2
        ref_models = list(refs[0].get("models") or [])
        ref_aliases = dict(refs[0].get("aliases") or {})
        if not ref_models:
            print(f"reference {args.converge_to!r} is unrestricted (models=[]); "
                  "converging to it would grant everyone everything", file=sys.stderr)
            return 2
        if args.converge_to in {k.get("key_alias") for k in keys}:
            print(f"reference {args.converge_to!r} is inside the write scope; "
                  "exclude it so a no-op cannot be reported as a write", file=sys.stderr)
            return 2
        print(f"reference={args.converge_to} ref_models={len(ref_models)} "
              f"ref_aliases={len(ref_aliases)}")
        # The loud half: names leaving the allowlist entirely -> the next call
        # 403s. Reported, never gated -- only a human can say whether a dropped
        # name still has an equivalent under the new shape. Go measure these in
        # SpendLogs before writing (and read the upper-bound caveat on
        # dropped_names before trusting per-name counts).
        # 🔴 Printed BEFORE the upstream-change gate, deliberately: that gate
        # exits 2, and the moment the operator most needs this list is exactly
        # when they are deciding whether to pass --allow-upstream-change. It sat
        # after the gate for one revision and never printed on the real run.
        drop_tally: dict[str, int] = {}
        for k in keys:
            for name in dropped_names(k, ref_models):
                drop_tally[name] = drop_tally.get(name, 0) + 1
        if drop_tally:
            print(f"NAMES DROPPED from the allowlist: {len(drop_tally)} distinct "
                  "-- every key below starts 403ing on them")
            for name, n in sorted(drop_tally.items(), key=lambda kv: (-kv[1], kv[0])):
                print(f"  {name}  ({n} key(s))")
            print("  ^ measure 14d traffic on these BEFORE --apply; a name with no "
                  "equivalent on the reference is a capability you are deleting.")
        exposed = [(k, upstream_changes(k, ref_models, ref_aliases)) for k in keys]
        exposed = [(k, ch) for k, ch in exposed if ch]
        if exposed:
            print(f"UPSTREAM CHANGE on {len(exposed)} key(s): a name stays callable but its "
                  "alias target changes/disappears")
            for k, changes in exposed:
                for src, old, new in changes:
                    print(f"  {k.get('key_alias')}: {src} => {old} -> "
                          f"{new or '(same-name real group)'}")
            if not args.allow_upstream_change:
                print("refusing: this silently moves users to another provider. Re-run with "
                      "--allow-upstream-change once that is the decision.", file=sys.stderr)
                return 2

    if args.converge_to:
        planned = [(key, plan_converge(key, ref_models, ref_aliases)) for key in keys]
    else:
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
    if args.converge_to:
        print(f"converge_to={args.converge_to} "
              f"allow_upstream_change={args.allow_upstream_change}")
    else:
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
        if args.converge_to:
            # Equality, not containment: the add/rm sets are empty here, so the
            # generic assertions below would all pass vacuously.
            if fresh.get("models"):
                ok &= list(fresh["models"]) == ref_models
            ok &= aliases == ref_aliases
        else:
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
    if args.converge_to:
        # readback_ok only speaks about the keys this run planned. Answer the
        # question the operator actually asked -- "全部改完了吗" -- over the whole
        # prefix scope, so a key that was never in scope cannot read as a no-op.
        residual = convergence_residual(
            list(after.values()), prefixes, args.converge_to, ref_models, ref_aliases,
            tuple(args.exclude), tuple(args.exclude_prefix), args.include_blocked,
        )
        converged = sum(
            1 for k in after.values()
            if str(k.get("key_alias") or "").startswith(prefixes)
            and list(k.get("models") or []) == ref_models
            and dict(k.get("aliases") or {}) == ref_aliases
        )
        print(f"converged_identical={converged} "
              + " ".join(f"{name}={len(v)}" for name, v in residual.items()))
        for name in ("spared", "blocked", "unrestricted", "mismatch"):
            if residual[name]:
                print(f"  {name}: {', '.join(residual[name][:20])}"
                      + (" ..." if len(residual[name]) > 20 else ""))
        if residual["blocked"]:
            print("  ^ blocked keys keep their OLD allowlist; unblocking one later "
                  "silently restores the pre-sweep shape. Re-run with --include-blocked.")
        if residual["mismatch"]:
            print("residual mismatch after a clean write -- investigate before "
                  "declaring the sweep done", file=sys.stderr)
            return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
