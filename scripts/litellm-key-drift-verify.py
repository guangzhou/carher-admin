#!/usr/bin/env python3
"""Compare a pre-write key snapshot against the live DB: did we change ONLY what we meant to?

Why this script exists
----------------------
`/key/update` replaces `models` and `aliases` wholesale, so a read-merge-write
bug -- or a concurrent session doing its own read-merge-write -- silently
reverts other people's entries while returning 200 for every call. The batch
script's own `applied_ok` counter cannot see this: it reports what the API
accepted, not what survived. 2026-09-08 on 198, `applied_ok=632/632` left only
115 keys actually changed.

So the acceptance ruler has to be a DIFFERENT one: the snapshot the batch
script wrote before touching anything, versus rows read straight out of
Postgres. This script is that ruler.

It asserts, per key, exactly:
    live.models  == snapshot.models  | --add-model...   (minus --rm-model...)
    live.aliases == snapshot.aliases | --alias...       (minus --rm-alias...)
and classifies every key as ok / missing (our change didn't land) /
drift (something ELSE changed -- almost always a concurrent writer).

`missing` and `drift` mean different things and must not be merged into one
"bad" count: missing = re-run to convergence; drift = stop and find the other
writer before writing again.

Footprint: read-only. Reads the snapshot file and runs one SELECT.

Examples
--------
    # 226, Aliyun cluster
    ./litellm-key-drift-verify.py \\
        --snapshot /root/herflash/full-20260917T1712.json \\
        --add-model her-flash --alias her-flash=openrouter-deepseek-v4.1-flash

    # a rename verified against the snapshot taken BEFORE the whole rename
    ./litellm-key-drift-verify.py --snapshot /root/herpro/pre.json \\
        --add-model her-pro --alias her-pro=grok-4.6 \\
        --rm-model auto --rm-alias auto

    # 198 instead
    ./litellm-key-drift-verify.py --ns litellm-product --db-pod litellm-db-0 --snapshot ...

The snapshot format is whatever `litellm-198-key-allowlist.py --backup` wrote:
a list of objects (or a dict keyed by token) each carrying `token`, `models`,
`aliases`, and usually `key_alias`.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

# psql literals go in $$...$$ dollar quotes: double quotes are identifiers in
# psql, and \x27 escapes get mangled on the way through kubectl exec.
SQL = """
select coalesce(json_agg(json_build_object(
    'token', token, 'key_alias', key_alias,
    'models', models, 'aliases', aliases, 'blocked', blocked)), $$[]$$::json)
from "LiteLLM_VerificationToken"
where key_alias like $${prefix}%$$
"""


def load_snapshot(path: str) -> dict[str, dict]:
    """Rows keyed by token, from either snapshot shape.

    ``litellm-198-key-allowlist.py --backup`` writes ``{token: {key_alias,
    models, aliases}}`` -- the token is the *mapping key* and is absent from the
    row itself. Reading only ``row["token"]`` silently dropped every row and the
    run reported ``snapshot=0 keys`` / FAIL zero-verified, which looks exactly
    like a wrong ``--prefix``. A list of rows carrying their own ``token`` is
    also accepted.
    """
    raw = json.load(open(path))
    if isinstance(raw, dict):
        out = {}
        for token, row in raw.items():
            if not isinstance(row, dict):
                continue
            out[row.get("token") or token] = {**row, "token": row.get("token") or token}
        return out
    return {r["token"]: r for r in raw if r.get("token")}


def diff_keys(
    snapshot: dict[str, dict],
    live: dict[str, dict],
    add_models: list[str],
    rm_models: list[str],
    add_aliases: dict[str, str],
    rm_aliases: list[str],
) -> dict[str, list]:
    """Classify each snapshot key as ok / missing / drift / gone. Pure."""
    out: dict[str, list] = {"ok": [], "missing": [], "drift": [], "gone": [], "unrestricted": []}
    for token, old in snapshot.items():
        cur = live.get(token)
        label = old.get("key_alias") or token[:12]
        if cur is None:
            out["gone"].append(label)
            continue
        om, cm = list(old.get("models") or []), list(cur.get("models") or [])
        oa = dict(old.get("aliases") or {})
        ca = dict(cur.get("aliases") or {})

        # models == [] means "unrestricted"; the batch script never writes models
        # for those, so expecting our additions there would be a false red.
        if not om:
            want_models = []
            out["unrestricted"].append(label)
        else:
            want_models = [m for m in om if m not in rm_models]
            want_models += [m for m in add_models if m not in want_models]

        want_aliases = {k: v for k, v in oa.items() if k not in rm_aliases}
        want_aliases.update(add_aliases)

        models_ok = sorted(cm) == sorted(want_models)
        aliases_ok = ca == want_aliases
        if models_ok and aliases_ok:
            out["ok"].append(label)
            continue

        # Did OUR change land? If the only discrepancy is our own additions
        # being absent, that is "missing" (re-run). Anything else is drift.
        ours_absent = (
            any(m in want_models and m not in cm for m in add_models)
            or any(m in cm for m in rm_models)
            or any(k not in ca or ca.get(k) != v for k, v in add_aliases.items())
            or any(k in ca for k in rm_aliases)
        )
        untouched_models_ok = sorted(m for m in cm if m not in add_models) == sorted(
            m for m in want_models if m not in add_models
        )
        untouched_aliases_ok = {k: v for k, v in ca.items() if k not in add_aliases} == {
            k: v for k, v in want_aliases.items() if k not in add_aliases
        }
        if ours_absent and untouched_models_ok and untouched_aliases_ok:
            out["missing"].append(label)
        else:
            out["drift"].append(
                {
                    "key": label,
                    "models_only_live": sorted(set(cm) - set(want_models)),
                    "models_only_want": sorted(set(want_models) - set(cm)),
                    "aliases_diff": {
                        k: [want_aliases.get(k), ca.get(k)]
                        for k in set(want_aliases) | set(ca)
                        if want_aliases.get(k) != ca.get(k)
                    },
                }
            )
    return out


def fetch_live(ns: str, db_pod: str, db_user: str, db_name: str, prefix: str) -> dict[str, dict]:
    sql = SQL.format(prefix=prefix)
    res = subprocess.run(
        ["kubectl", "-n", ns, "exec", db_pod, "--", "psql", "-U", db_user, "-d", db_name, "-tAc", sql],
        capture_output=True,
        text=True,
    )
    if res.returncode or not res.stdout.strip():
        sys.exit(f"psql failed rc={res.returncode}: {(res.stderr or res.stdout).strip()[:400]}")
    return {r["token"]: r for r in json.loads(res.stdout)}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--snapshot", required=True, help="--backup file written BEFORE the write")
    p.add_argument("--prefix", default="carher-")
    p.add_argument("--ns", default="carher")
    p.add_argument("--db-pod", default="litellm-db-0")
    p.add_argument("--db-user", default="litellm", help="NOT llmproxy -- that role does not exist")
    p.add_argument("--db-name", default="litellm")
    p.add_argument("--add-model", action="append", default=[])
    p.add_argument("--rm-model", action="append", default=[])
    p.add_argument("--alias", action="append", default=[], metavar="SRC=DST")
    p.add_argument("--rm-alias", action="append", default=[])
    p.add_argument("--live-json", help="skip psql, read the live rows from this file (for testing)")
    args = p.parse_args()

    add_aliases = dict(a.split("=", 1) for a in args.alias)
    snapshot = load_snapshot(args.snapshot)
    live = (
        {r["token"]: r for r in json.load(open(args.live_json))}
        if args.live_json
        else fetch_live(args.ns, args.db_pod, args.db_user, args.db_name, args.prefix)
    )
    print(f"snapshot={len(snapshot)} keys  live={len(live)} keys ({args.prefix}*)")

    res = diff_keys(snapshot, live, args.add_model, args.rm_model, add_aliases, args.rm_alias)
    for bucket in ("ok", "missing", "drift", "gone", "unrestricted"):
        print(f"  {bucket:12} {len(res[bucket])}")
    for label in res["missing"][:20]:
        print(f"  MISSING {label}")
    for d in res["drift"][:20]:
        print(f"  DRIFT   {json.dumps(d, ensure_ascii=False)}")

    if res["drift"]:
        print("\nFAIL: drift -- someone else's read-merge-write overwrote rows. Find the other")
        print("      writer (ListAgents / ls -lt ~/ / updated_at histogram) BEFORE writing again.")
        return 2
    if res["missing"]:
        print("\nFAIL: our change did not land on every key -- re-run the batch to convergence")
        print("      (planned=0), then verify again.")
        return 1
    if not res["ok"]:
        print("\nFAIL: zero keys verified. An empty sample is a failure, not a pass"
              " (wrong --prefix or wrong cluster?).")
        return 3
    print("\nPASS: every snapshot key differs from the baseline by exactly the requested change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
