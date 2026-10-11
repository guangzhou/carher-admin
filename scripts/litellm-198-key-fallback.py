#!/usr/bin/env python3
"""Per-key fallback tables for 198 LiteLLM: prepend one target ahead of every
model group a key can reach.

This writes ``LiteLLM_VerificationToken.router_settings`` (native key-level
router settings, LiteLLM >= 1.90). It is a pure DB/API write: no callback
module, no ConfigMap edit, no proxy restart, no multi-lane rollout. That
matters because ``litellm-callbacks`` is shared by the production proxy lane
(select by route label ``carher.net/litellm-production-route=enabled``, never a
hardcoded name — it moved from ``litellm-proxy-gray`` back to ``litellm-proxy``
on 2026-09-24) and 13 ``chatgpt-acct-*`` deployments.

Five invariants this encodes, each measured on 198 (2026-09-17):

* **Per-request fallbacks REPLACE the global table, they do not merge**
  (``router.py:6858`` ``kwargs.pop("fallbacks", self.fallbacks)``). A key-level
  table must therefore enumerate *every* group that key can reach and carry
  that group's existing global targets along, or writing it silently strips
  the tail of every chain. ``--check`` asserts no global row was dropped.

* **The authoritative model-group list is the running router, not the DB.**
  ``LiteLLM_ProxyModelTable`` held 189 groups while ``/model_group/info``
  returned 300 -- groups defined in config.yaml's ``model_list`` are not in
  that table. Judging "this name is dead" from the DB marks live groups dead.

* **Fallback lookup uses the POST-rewrite name for per-key aliases.** The
  rewrite is at ``litellm_pre_call_utils.py:_update_model_if_key_alias_exists``,
  which runs before the router. This is the opposite of router-level
  ``model_group_alias``, whose fallback lookup uses the pre-rewrite name. So
  rows are keyed on alias *targets*; bare names that have no alias get their
  own row (they reach the router unrewritten).

* **Non-chat groups are excluded.** Routing an embedding or image_generation
  failure into a text model just swaps one error for another. Mode comes from
  ``/model_group/info``.

* **``mock_response`` / ``mock_testing_fallbacks`` are silently dropped on 198
  prod**, so they cannot serve as a ruler. ``probe`` instead clones the key's
  full shape (models + aliases + router_settings), injects a group that is
  known to fail as the main leg, and judges by the ``x-litellm-attempted-
  fallbacks`` header plus a nonce echoed in the body. Never by the body's
  ``model`` field -- that echoes the client's request name.

Usage (run on 198, where 127.0.0.1:30402 is the litellm-product NodePort)::

    export LITELLM_MASTER_KEY=$(sudo kubectl -n litellm-product get secret \
      litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)

    S=~/litellm-198-key-fallback-<sid>.py   # session-numbered: 198 runs parallel sessions

    # 1. dry-run (default): print the table that would be written, per key
    python3 $S plan --key carher-13 --key carher-14 --target openrouter-deepseek-v4.1-flash
    # 2. canary one key, then probe it
    python3 $S apply --key carher-13 --target openrouter-deepseek-v4.1-flash \
        --backup ~/fb-canary-$(date +%Y%m%dT%H%M%S).json
    python3 $S probe --key carher-13 --dead-group google/lyria-3-pro-preview \
        --expect-target openrouter-deepseek-v4.1-flash
    # 3. rest of the keys
    python3 $S apply --key carher-14 --key carher-75 --target ... --backup ~/fb-full-<ts>.json
    # 4. re-audit any time (no writes): drift, lost global rows, uncovered traffic
    python3 $S check --key carher-13 --key carher-14 --key carher-75 \
        --target openrouter-deepseek-v4.1-flash
    # 5. rollback
    python3 $S restore --backup ~/fb-full-<ts>.json --apply

Removing ONE group's fallback for ONE key (the inverse operation, added
2026-10-10 for "take astra's fallback off this person's cursor key")::

    python3 $S drop --key cursor-<name>-<sfx> --group gpt-6-astra
    python3 $S drop --key cursor-<name>-<sfx> --group gpt-6-astra \
        --apply --backup ~/<name>-astra-nofb-$(date -u +%Y%m%dT%H%M%SZ).json
    python3 $S drop-check --key cursor-<name>-<sfx> --group gpt-6-astra

Two things ``drop`` refuses to do, both learned the same day:

* **It will not write when the group has no reachable global row.** A key whose
  per-key table is already non-empty does not read the global table at all, so
  a group missing from that table ALREADY has no fallback. "Doing it anyway"
  would rewrite the same rows, report success, change nothing, and newly freeze
  the table against future global edits.
* **It will not write an empty table.** See ``key_info``: empty is not "no
  fallbacks", it hands the key back to the global table.

``plan``/``check``/``drop-check`` never write, and ``drop`` is dry-run until
``--apply``. ``apply`` requires either ``--backup`` or ``--no-backup``; there is
no unbacked default.
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

# Modes that must never be given a text fallback target.
NON_CHAT_MODES = {
    "embedding",
    "image_generation",
    "audio_transcription",
    "audio_speech",
    "rerank",
    "moderation",
}


def api(method: str, path: str, body: dict | None = None, key: str | None = None,
        timeout: int = 120) -> tuple[int, dict, bytes]:
    """Call the proxy. Returns (status, headers, raw_body) -- never raises on 4xx/5xx,
    because a fallback probe's interesting cases are the error ones."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key or MASTER}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def live_groups() -> dict[str, str | None]:
    """model_group -> mode, from the RUNNING router. The authoritative list."""
    st, _, raw = api("GET", "/model_group/info")
    if st != 200:
        die(f"/model_group/info returned {st}: {raw[:300]!r}")
    rows = json.loads(raw)
    rows = rows.get("data", rows) if isinstance(rows, dict) else rows
    return {r.get("model_group"): r.get("mode") for r in rows if r.get("model_group")}


def global_fallbacks() -> dict[str, list[str]]:
    """The global ``router_settings.fallbacks`` table, flattened to group -> targets.

    🔴 ``router_settings`` here is a **dict**, not a list of
    ``{field_name, field_value}`` sections. This function used to assume the
    section shape; iterating a dict yields its KEYS (plain strings), so
    ``section.get`` raised ``AttributeError``, which was caught and swallowed,
    and the table came back EMPTY. It then fell through to ``/config/yaml`` --
    the one source that is authoritatively WRONG here, because
    ``STORE_MODEL_IN_DB=True`` makes Postgres the source of truth and the DB
    value overlays yaml entirely. Measured 2026-10-10: the real table is 109
    rows and this returned 0.

    An empty table is never a believable reading: every caller treats "no
    global row" as "this group has no fallback", so a blind read turns into
    "nothing to do here" or into a table with every chain stripped. Refuse.
    """
    st, _, raw = api("GET", "/get/config/callbacks")
    if st != 200:
        die(f"/get/config/callbacks returned {st}: {raw[:200]!r}")
    rs = json.loads(raw).get("router_settings")
    if not isinstance(rs, dict):
        die(f"router_settings came back as {type(rs).__name__}, not dict -- the "
            f"endpoint shape changed, so this table and everything judged from "
            f"it are suspect. Re-read the shape before trusting any result.")
    table: dict[str, list[str]] = {}
    for row in rs.get("fallbacks") or []:
        if not isinstance(row, dict):
            continue
        for k, v in row.items():
            if k in table:
                die(f"duplicate global fallback row for {k!r} -- ambiguous which "
                    f"chain is live; refusing to copy either")
            table[k] = list(v) if isinstance(v, list) else v
    if not table:
        die("the global fallback table read as EMPTY. That is almost certainly a "
            "broken read, not an empty table; every caller would mistake it for "
            "'no group has any fallback'.")
    return table


def model_group_aliases() -> dict:
    """The global ``router_settings.model_group_alias`` map.

    Needed because a key can reach a group through this map with no per-key
    alias at all, and then the chain is looked up under both spellings.
    """
    st, _, raw = api("GET", "/get/config/callbacks")
    if st != 200:
        die(f"/get/config/callbacks returned {st}")
    rs = json.loads(raw).get("router_settings")
    if not isinstance(rs, dict):
        die(f"router_settings came back as {type(rs).__name__}, not dict -- the "
            f"endpoint shape changed, so every reading below it is suspect")
    return rs.get("model_group_alias") or {}


def key_info(alias: str) -> dict:
    """Read one key by alias, via /key/list.

    🔴 NOT ``/key/info?key_alias=``. On 198 (measured 2026-10-10) that endpoint
    answers **HTTP 200 with every field null** -- key_alias, token, models,
    aliases, router_settings. Nothing raises, because the failure is a 200.
    ``reachable()`` then turns ``models: None`` into ``[]`` via ``or []``,
    ``build_table()`` produces **zero rows**, and ``lost_global_rows()`` -- the
    guard meant to catch exactly this -- also computes from the same empty
    ``info``, so it returns empty and waves the write through. The result is
    ``{"key_alias": null, "router_settings": {"fallbacks": []}}``: the script
    prints success, and an EMPTY per-key table is not "no fallbacks", it makes
    the key fall back to the global table. Symptom: "ran fine, nothing changed."

    ``/key/info?key=<token sha256>`` does return models/aliases but still nulls
    ``token``; ``/key/list&return_full_object=true`` is the only shape that
    returns the whole row, so that is what this uses.
    """
    st, _, raw = api(
        "GET",
        "/key/list?page=1&size=100&return_full_object=true"
        f"&key_alias={urllib.parse.quote(alias)}",
    )
    if st != 200:
        die(f"/key/list for {alias} returned {st}: {raw[:300]!r}")
    rows = json.loads(raw).get("keys") or []
    rows = [r for r in rows if isinstance(r, dict) and r.get("key_alias") == alias]
    if len(rows) != 1:
        die(f"/key/list for {alias} matched {len(rows)} rows, expected exactly 1")
    info = rows[0]
    # A key with no models AND no aliases reaches nothing, so every table built
    # from it is empty -- indistinguishable from a read that silently failed.
    # Refuse rather than write a table computed from a blind read.
    if not (info.get("models") or info.get("aliases")):
        die(f"{alias}: read back with neither models nor aliases -- refusing to "
            f"build a fallback table from a read that returned nothing usable")
    return info


def die(msg: str) -> None:
    print(f"FATAL: {msg}", file=sys.stderr)
    raise SystemExit(2)


def reachable(info: dict, groups: dict[str, str | None]) -> tuple[list[str], list[str], list[str]]:
    """Split what a key can reach into (usable_chat, non_chat, absent_from_router).

    Reachable = models allowlist UNION alias targets. Alias *targets* are what
    the router sees, because the rewrite happens before it. An alias source
    that is also a bare group name is covered by its target's row.
    """
    models = [m for m in (info.get("models") or []) if m]
    aliases = info.get("aliases") or {}
    names = set(models) | {str(v) for v in aliases.values()}
    # An alias source never reaches the router under its own name.
    names -= set(aliases.keys()) - {str(v) for v in aliases.values()}
    absent = sorted(n for n in names if n not in groups)
    non_chat = sorted(n for n in names if n in groups and groups[n] in NON_CHAT_MODES)
    usable = sorted(
        n for n in names if n in groups and groups[n] not in NON_CHAT_MODES
    )
    return usable, non_chat, absent


def lookup_names(info: dict, group: str, mga: dict) -> set[str]:
    """Every name under which ``group``'s chain can be looked up for this key.

    ``fallback_lookup_groups()`` tries several names for one request (the tier a
    pre-routing hook picked, the routed group from metadata, then the requested
    group), so dropping a chain means dropping it under ALL of them. The three
    sources of a second name, each measured on 198:

    * global ``model_group_alias`` -- ``gpt-6-astra`` -> ``chatgpt-gpt-6-astra``.
      Both rows exist in the global table and both must go.
    * per-key ``aliases`` -- the rewrite happens before the router, so the
      POST-rewrite name is what the table is keyed on.
    * the bare name itself, for a key that reaches the group unrewritten.

    Dropping only the name the user said is the silent-failure shape: the chain
    survives under the other spelling, with no symptom.
    """
    names = {group}
    for src, dst in (info.get("aliases") or {}).items():
        if src == group or str(dst) == group:
            names.add(src)
            names.add(str(dst))
    for src, dst in (mga or {}).items():
        tgt = dst.get("model") if isinstance(dst, dict) else dst
        if src == group or str(tgt) == group:
            names.add(src)
            names.add(str(tgt))
    return {n for n in names if isinstance(n, str) and n}


def build_drop_table(info: dict, drop: set[str], glob: dict[str, list[str]]) -> list[dict]:
    """The key's own copy of the global table, MINUS the dropped groups' rows.

    Removing one group's fallback for one key cannot be written as a deletion:
    a per-key table REPLACES the global one rather than merging with it
    (``route_llm_request.py`` copies ``fallbacks`` into the request body and
    ``router.py`` does ``kwargs.get("fallbacks", self.fallbacks)``). So "drop
    one row" has to be written as "every row this key needs, except that one".

    Cost this encodes: the written table is a SNAPSHOT of the global one. Later
    edits to the global table stop reaching this key. ``check`` re-diffs the two
    so the drift is visible instead of silent.
    """
    models = {m for m in (info.get("models") or []) if m}
    al = info.get("aliases") or {}
    # Union of allowlist, alias sources and alias targets: lookup can present
    # either spelling, so copying a row for either is the conservative choice.
    reach = models | set(al.keys()) | {str(v) for v in al.values()}
    keep = sorted(n for n in reach if n in glob and n not in drop)
    return [{n: list(glob[n])} for n in keep]


def build_table(usable: list[str], target: str, glob: dict[str, list[str]]) -> list[dict]:
    """One row per group: [target, *that group's existing global targets].

    Prepend, never replace: the tail is what already worked. The target is
    de-duplicated out of the tail so it cannot appear twice in one chain, and a
    group that IS the target gets no row (a self-referential row is skipped by
    ``run_async_fallback`` anyway, but writing it is noise).
    """
    rows = []
    for group in usable:
        if group == target:
            continue
        tail = [t for t in glob.get(group, []) if t != target]
        rows.append({group: [target] + tail})
    return rows


def lost_global_rows(info: dict, table: list[dict], glob: dict[str, list[str]]) -> list[str]:
    """Groups this key could reach that HAD a global fallback row but got no
    key-level row -- i.e. groups whose tail we silently deleted."""
    written = {k for row in table for k in row}
    models = set(m for m in (info.get("models") or []) if m)
    aliases = info.get("aliases") or {}
    reach = models | {str(v) for v in aliases.values()}
    return sorted(g for g in reach if g in glob and g not in written)


def cmd_plan(args) -> int:
    groups, glob = live_groups(), global_fallbacks()
    print(f"live model groups: {len(groups)}   global fallback rows: {len(glob)}")
    for alias in args.key:
        info = key_info(alias)
        usable, non_chat, absent = reachable(info, groups)
        table = build_table(usable, args.target, glob)
        lost = lost_global_rows(info, table, glob)
        current = info.get("router_settings") or {}
        cur_rows = len((current or {}).get("fallbacks") or [])
        print(f"\n=== {alias}")
        print(f"    current router_settings rows : {cur_rows}"
              f"{'  (empty/inert)' if not cur_rows else ''}")
        print(f"    rows to write                : {len(table)}")
        print(f"    target in models allowlist   : "
              f"{args.target in (info.get('models') or [])}")
        print(f"    excluded, non-chat mode      : {non_chat}")
        print(f"    excluded, absent from router : {absent}")
        if lost:
            print(f"    !! WOULD DROP global rows    : {lost}")
        if args.verbose:
            for row in table:
                for g, targets in row.items():
                    print(f"      {g}")
                    for i, t in enumerate(targets, 1):
                        print(f"          {i}. {t}")
    return 0


def _drop_plan(alias: str, group: str) -> tuple[dict, dict, set[str], list[dict]]:
    """Shared read+compute for ``drop``/``drop-check`` so both judge identically."""
    info = key_info(alias)
    glob, mga = global_fallbacks(), model_group_aliases()
    drop = lookup_names(info, group, mga)
    table = build_drop_table(info, drop, glob)
    return info, glob, drop, table


def _drop_report(alias: str, info: dict, glob: dict, drop: set[str],
                 table: list[dict]) -> list[str]:
    cur = (info.get("router_settings") or {}).get("fallbacks") or []
    print(f"=== {alias}")
    print(f"    token                  : {info.get('token')}")
    print(f"    global rows            : {len(glob)}")
    print(f"    current per-key rows   : {len(cur)}"
          f"{'   (empty => this key currently uses the global table)' if not cur else ''}")
    print(f"    names the chain is looked up under : {sorted(drop)}")
    for n in sorted(drop):
        print(f"      {n:<28} global chain = {glob.get(n, '(no global row)')}")
    hit = sorted(n for n in drop if n in glob)
    print(f"    rows actually dropped  : {hit}")
    print(f"    rows to write          : {len(table)}")
    return hit


def cmd_drop(args) -> int:
    """Remove ONE group's fallback chain for ONE key, leaving every other chain."""
    info, glob, drop, table = _drop_plan(args.key[0], args.group)
    hit = _drop_report(args.key[0], info, glob, drop, table)
    # 🔴 The no-op judgement is "does THIS KEY currently have a chain for this
    # group", NOT "is there a global row". A global row always exists for a
    # live group, so testing `glob` has no discriminating power -- it waves
    # through both the already-done key and the key that never had the chain.
    # What is live for this key depends on which table it reads:
    #   * per-key table non-empty -> it REPLACES the global table, so only the
    #     per-key rows count (this is the claude-key case: 31 rows, no opus-5.5
    #     row => opus-5.5 already has no fallback).
    #   * per-key table empty     -> the key reads the global table.
    cur_rows = (info.get("router_settings") or {}).get("fallbacks") or []
    if cur_rows:
        live = {k for row in cur_rows if isinstance(row, dict) for k in row}
        source = f"its own {len(cur_rows)}-row per-key table"
    else:
        live = set(glob)
        source = f"the global {len(glob)}-row table"
    if not (drop & live):
        die(f"{args.group} already has NO fallback on this key: none of "
            f"{sorted(drop)} appears in {source}, which is what this key reads. "
            f"Writing anyway would rewrite the same rows, report success, change "
            f"nothing, and newly freeze the table against future global edits.")
    if not hit:
        die(f"{args.group} is live for this key but has no global row to copy "
            f"from, so the resulting table could not preserve it correctly.")
    if not table:
        die("the table came out EMPTY -- see cmd_apply for why that is never a "
            "legitimate write")
    print("\nrows written (group -> chain):")
    for row in table:
        for g, v in row.items():
            print(f"  {g:<28} {v}")
    if not args.apply:
        print("\nDRY-RUN. pass --apply (with --backup) to write.")
        return 0
    if not args.backup:
        die("--apply needs --backup <path>")
    with open(args.backup, "w") as fh:
        json.dump({"key_alias": info.get("key_alias"), "token": info.get("token"),
                   "group": args.group,
                   "router_settings": info.get("router_settings") or {},
                   "models": info.get("models") or [],
                   "aliases": info.get("aliases") or {}}, fh,
                  ensure_ascii=False, indent=2)
    print(f"\nsnapshot -> {args.backup}")
    # key_alias addresses the row; models/aliases are deliberately NOT sent, so
    # /key/update cannot clobber the allowlist or the alias map.
    st, _, raw = api("POST", "/key/update",
                     {"key_alias": info.get("key_alias"),
                      "router_settings": {"fallbacks": table}})
    print(f"/key/update -> {st}" + ("" if st == 200 else f"  {raw[:400]!r}"))
    return 0 if st == 200 else 1


def cmd_drop_check(args) -> int:
    """Audit a dropped chain: absent under every name, nothing else lost, no drift."""
    info, glob, drop, expect_rows = _drop_plan(args.key[0], args.group)
    written = {}
    for row in (info.get("router_settings") or {}).get("fallbacks") or []:
        written.update(row)
    expect = {g: v for row in expect_rows for g, v in row.items()}
    bad = []
    for n in sorted(drop):
        if n in written:
            bad.append(f"chain for {n} is STILL PRESENT: {written[n]}")
    for g, v in expect.items():
        if g not in written:
            bad.append(f"missing row {g} (its chain {v} was lost)")
        elif written[g] != v:
            bad.append(f"row {g} has DRIFTED from the global table: "
                       f"key={written[g]} global={v}")
    extra = sorted(set(written) - set(expect))
    if extra:
        bad.append(f"rows that are not in the planned set: {extra}")
    print(f"{args.key[0]}: per-key rows={len(written)} planned={len(expect)} "
          f"models={len(info.get('models') or [])} "
          f"aliases={len(info.get('aliases') or {})}")
    print(f"  {args.group} absent under every lookup name: "
          f"{all(n not in written for n in drop)}  (names: {sorted(drop)})")
    for b in bad:
        print(f"  !! {b}")
    print("\nOK" if not bad else f"\n{len(bad)} problem(s)")
    return 0 if not bad else 1


def cmd_apply(args) -> int:
    if not args.backup and not args.no_backup:
        die("apply needs --backup <path> (or an explicit --no-backup)")
    groups, glob = live_groups(), global_fallbacks()
    snapshot, writes = {}, []
    for alias in args.key:
        info = key_info(alias)
        usable, _, _ = reachable(info, groups)
        table = build_table(usable, args.target, glob)
        # An empty table is never a legitimate write. Writing it does NOT mean
        # "this key has no fallbacks" -- a per-key table replaces the global one
        # only when non-empty, so an empty table silently hands the key back to
        # the global table while the script reports success. ``lost_global_rows``
        # cannot catch this: it is computed from the same ``info``, so a read
        # that came back empty makes the guard itself return empty.
        if not table:
            die(f"{alias}: the table came out EMPTY (reachable chat groups: "
                f"{len(usable)}). Refusing to write -- an empty per-key table "
                f"makes the key fall back to the global table, so this would "
                f"look like success and change nothing. Check that the key read "
                f"actually returned models/aliases.")
        lost = lost_global_rows(info, table, glob)
        if lost and not args.allow_dropping_global_rows:
            die(f"{alias}: writing this table would drop the global fallback tail of "
                f"{lost}. Fix the row set, or pass --allow-dropping-global-rows if "
                f"that is genuinely intended.")
        snapshot[alias] = {
            "models": info.get("models") or [],
            "router_settings": info.get("router_settings") or {},
        }
        writes.append((alias, info, table))

    if args.backup:
        with open(args.backup, "w") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=2)
        print(f"snapshot -> {args.backup}  ({len(snapshot)} keys)")

    for alias, info, table in writes:
        body: dict = {"key_alias": alias, "router_settings": {"fallbacks": table}}
        # The target must be reachable in its own right for the chain to be
        # usable when a client selects it directly. Read-merge-write: /key/update
        # replaces the whole models field.
        models = list(info.get("models") or [])
        if models and args.target not in models:
            body["models"] = models + [args.target]
        st, _, raw = api("POST", "/key/update", body)
        print(f"{alias}: /key/update -> {st}  rows={len(table)}"
              f"{'  +allowlist' if 'models' in body else ''}")
        if st != 200:
            print(f"   {raw[:300]!r}")
    return 0


def cmd_check(args) -> int:
    """Read-only audit. Non-zero exit if anything is off, so it can gate a deploy."""
    groups, glob = live_groups(), global_fallbacks()
    bad = 0
    for alias in args.key:
        info = key_info(alias)
        rs = info.get("router_settings") or {}
        rows = rs.get("fallbacks") or []
        written = {k: v for row in rows for k, v in row.items()}
        head_ok = sum(1 for v in written.values() if v and v[0] == args.target)
        usable, non_chat, absent = reachable(info, groups)
        missing = sorted(set(usable) - set(written) - {args.target})
        lost = lost_global_rows(info, rows, glob)
        allow_ok = args.target in (info.get("models") or [])
        problems = []
        if head_ok != len(written):
            problems.append(f"{len(written) - head_ok} rows do not lead with target")
        if missing:
            problems.append(f"reachable chat groups with no row: {missing}")
        if lost:
            problems.append(f"dropped global fallback tails: {lost}")
        if not allow_ok:
            problems.append("target not in models allowlist")
        print(f"{alias}: rows={len(written)} head_ok={head_ok} "
              f"target_in_allowlist={allow_ok} excluded_non_chat={non_chat} "
              f"absent_from_router={absent}")
        for p in problems:
            print(f"   !! {p}")
        bad += len(problems)
    print("\nOK" if not bad else f"\n{bad} problem(s)")
    return 0 if not bad else 1


def cmd_probe(args) -> int:
    """Clone the key's full shape, force the main leg to fail, judge by header+nonce.

    The clone carries models + aliases + router_settings so it routes exactly as
    the real key does. ``--dead-group`` is injected as an extra row at the head
    so the real rows are left untouched -- we are testing the mechanism, not
    breaking a live group. Never point this at a group real traffic uses.
    """
    info = key_info(args.key[0])
    rs = json.loads(json.dumps(info.get("router_settings") or {"fallbacks": []}))
    rs.setdefault("fallbacks", [])
    rs["fallbacks"] = [{args.dead_group: [args.expect_target]}] + rs["fallbacks"]
    probe_alias = f"zz-fbprobe-{args.key[0]}"
    st, _, raw = api("POST", "/key/generate", {
        "key_alias": probe_alias,
        "models": list(info.get("models") or []) + [args.dead_group],
        "aliases": info.get("aliases") or {},
        "router_settings": rs,
        "duration": "20m",
    })
    if st != 200:
        die(f"clone key failed: {st} {raw[:300]!r}")
    clone = json.loads(raw)["key"]
    rc = 0
    try:
        nonce = f"NONCE-{args.key[0]}"
        # Leg 1 (positive control): main leg fails -> must land on the target.
        # max_tokens must be generous: the target is a reasoner and a small
        # budget gets spent entirely on reasoning tokens, returning empty
        # content -- a broken ruler, not a dead leg.
        st, h, raw = api("POST", "/v1/chat/completions", {
            "model": args.dead_group,
            "messages": [{"role": "user", "content": f"Reply exactly: {nonce}"}],
            "max_tokens": 2000,
        }, key=clone, timeout=180)
        fb = h.get("x-litellm-attempted-fallbacks")
        body = json.loads(raw) if st == 200 else {}
        content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
        ok1 = st == 200 and fb not in (None, "0") and nonce in (content or "")
        print(f"  fallback leg   : {st} attempted-fallbacks={fb} "
              f"model-id={h.get('x-litellm-model-id')} content={content!r} "
              f"-> {'PASS' if ok1 else 'FAIL'}")
        rc |= 0 if ok1 else 1
        # Leg 2 (control group): a group the key really uses must be untouched.
        real = args.control_group or next(
            (m for m in (info.get("models") or []) if m != args.dead_group), None)
        if real:
            st, h, raw = api("POST", "/v1/chat/completions", {
                "model": real,
                "messages": [{"role": "user", "content": "say ok"}],
                "max_tokens": 600,
            }, key=clone, timeout=180)
            fb2 = h.get("x-litellm-attempted-fallbacks")
            ok2 = st == 200 and fb2 in (None, "0")
            print(f"  control leg    : {real} {st} attempted-fallbacks={fb2} "
                  f"model-id={h.get('x-litellm-model-id')} -> "
                  f"{'PASS' if ok2 else 'FAIL (this group now falls back when it should not)'}")
            rc |= 0 if ok2 else 1
        # Leg 3 (falsification): same shape WITHOUT router_settings must NOT fall back.
        st, _, raw = api("POST", "/key/generate", {
            "key_alias": probe_alias + "-nofb",
            "models": [args.dead_group],
            "duration": "20m",
        })
        if st == 200:
            bare = json.loads(raw)["key"]
            st, h, raw = api("POST", "/v1/chat/completions", {
                "model": args.dead_group,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 2000,
            }, key=bare, timeout=180)
            fb3 = h.get("x-litellm-attempted-fallbacks")
            ok3 = st != 200 and fb3 in (None, "0")
            print(f"  falsify leg    : no router_settings -> {st} "
                  f"attempted-fallbacks={fb3} -> "
                  f"{'PASS' if ok3 else 'FAIL (dead-group is not actually failing; the ruler is broken)'}")
            rc |= 0 if ok3 else 1
    finally:
        for a in (probe_alias, probe_alias + "-nofb"):
            api("POST", "/key/delete", {"key_aliases": [a]})
        st, _, raw = api("GET", "/key/list?key_alias=zz-fbprobe")
        leftover = [k for k in json.dumps(json.loads(raw) if st == 200 else {})
                    .split('"') if k.startswith("zz-fbprobe")]
        print(f"  cleanup        : probe keys deleted"
              f"{'  !! LEFTOVER ' + str(set(leftover)) if leftover else ''}")
    return rc


def cmd_restore(args) -> int:
    with open(args.backup) as fh:
        snapshot = json.load(fh)
    print(f"restoring {len(snapshot)} keys from {args.backup}"
          f"{'' if args.apply else '  (dry-run; pass --apply)'}")
    for alias, saved in snapshot.items():
        body = {
            "key_alias": alias,
            "router_settings": saved.get("router_settings") or {},
            "models": saved.get("models") or [],
        }
        if not args.apply:
            print(f"  would restore {alias}: "
                  f"rows={len((body['router_settings'] or {}).get('fallbacks') or [])} "
                  f"models={len(body['models'])}")
            continue
        st, _, raw = api("POST", "/key/update", body)
        print(f"  {alias}: {st}{'' if st == 200 else '  ' + repr(raw[:200])}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, need_target=True):
        sp.add_argument("--key", action="append", default=[], required=True,
                        help="key_alias, repeatable")
        if need_target:
            sp.add_argument("--target", required=True,
                            help="model group to put at the head of every chain")

    sp = sub.add_parser("plan", help="print the table that would be written (no writes)")
    common(sp)
    sp.add_argument("--verbose", action="store_true", help="print every chain")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("apply", help="write router_settings (and extend the allowlist)")
    common(sp)
    sp.add_argument("--backup", help="write a rollback snapshot here first")
    sp.add_argument("--no-backup", action="store_true")
    sp.add_argument("--allow-dropping-global-rows", action="store_true",
                    help="permit a table that deletes some group's existing global tail")
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("check", help="read-only audit; exit 1 if anything is off")
    common(sp)
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("drop",
                        help="remove ONE group's fallback chain for ONE key "
                             "(every other chain is preserved)")
    sp.add_argument("--key", action="append", default=[], required=True)
    sp.add_argument("--group", required=True,
                    help="the model group whose chain to drop, as the client "
                         "requests it (e.g. gpt-6-astra); every alias spelling "
                         "is resolved and dropped too")
    sp.add_argument("--apply", action="store_true")
    sp.add_argument("--backup", help="required with --apply")
    sp.set_defaults(func=cmd_drop)

    sp = sub.add_parser("drop-check",
                        help="audit a dropped chain (absence, no collateral "
                             "loss, drift against the global table)")
    sp.add_argument("--key", action="append", default=[], required=True)
    sp.add_argument("--group", required=True)
    sp.set_defaults(func=cmd_drop_check)

    sp = sub.add_parser("probe", help="clone the key and prove the fallback really fires")
    common(sp, need_target=False)
    sp.add_argument("--dead-group", required=True,
                    help="a group that reliably fails, used as the main leg")
    sp.add_argument("--expect-target", required=True)
    sp.add_argument("--control-group",
                    help="a group that must NOT fall back (default: first allowlist entry)")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("restore", help="roll back from a snapshot")
    sp.add_argument("--backup", required=True)
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_restore)

    args = p.parse_args()
    if not MASTER:
        die("LITELLM_MASTER_KEY is not set")
    return args.func(args)


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (used by key_info)

    sys.exit(main())
