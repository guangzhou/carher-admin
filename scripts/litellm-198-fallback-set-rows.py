#!/usr/bin/env python3
"""Set (add or replace) several whole rows of 198's global fallback table at once.

Why not ``litellm-198-fallback-prepend.py``: that one edits ONE row that must
already exist. A batch where most groups have no row yet (2026-10-12: 10 of 12)
needs add-or-replace semantics, and must still land as ONE write so a partial
run cannot leave half the groups changed.

Spec file (JSON): ``{"<group>": ["<target>", ...], ...}``. Each listed group's
row becomes exactly that list; every other row is left byte-identical.

Same rules as the prepend script (see skill ``litellm-198-fallback-chain``):
  * write only ``jsonb_set(param_value, '{fallbacks}', ...)`` on the DB row --
    never ``POST /config/update`` (wipes model_group_alias), never kubectl apply;
  * router_settings hot-loads every ~30s, so poll pods instead of restarting;
  * every group AND every target must be a live model group on the running router;
  * readback: top-level keys byte-identical, untouched rows byte-identical,
    row count == old + number of added groups, each spec row exact.

Usage (on 198)::

    python3 litellm-198-fallback-set-rows.py --spec rows.json             # dry-run
    python3 litellm-198-fallback-set-rows.py --spec rows.json --apply --backup <file>
    python3 litellm-198-fallback-set-rows.py --spec rows.json --verify    # layers 1-2
    python3 litellm-198-fallback-set-rows.py --restore <backup> --apply
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import subprocess
import sys
import time

NS = "litellm-product"
DB_POD = "litellm-db-0"
PROD_SELECTOR = "carher.net/litellm-production-route=enabled"


def sh(args: list[str]) -> str:
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"FAILED: {' '.join(args)}\n{p.stderr.strip()}")
    return p.stdout


def psql(sql: str) -> str:
    out = sh(["kubectl", "-n", NS, "exec", DB_POD, "--",
              "psql", "-U", "litellm", "-d", "litellm", "-tAc", sql])
    if "ERROR:" in out:
        raise SystemExit(f"psql error: {out.strip()}")
    return out


def read_rs() -> dict:
    raw = psql("select param_value::text from \"LiteLLM_Config\" "
               "where param_name='router_settings'").strip()
    if not raw:
        raise SystemExit("router_settings row is empty -- refusing to touch it")
    rs = json.loads(raw)
    if not isinstance(rs, dict) or not rs.get("fallbacks"):
        raise SystemExit("fallbacks read as empty -- that is a broken read, not an answer")
    return rs


def rows_of(fb: list[dict]) -> dict[str, list]:
    out: dict[str, list] = {}
    for row in fb:
        for k, v in row.items():
            if k in out:
                raise SystemExit(f"duplicate fallback row for {k!r} -- refusing")
            out[k] = v
    return out


def build(fb: list[dict], spec: dict[str, list]) -> tuple[list[dict], list[str], list[str]]:
    """Return (new fallbacks, replaced groups, added groups). Order preserved; adds appended."""
    rows_of(fb)  # duplicate guard
    new, replaced = [], []
    for row in fb:
        (k, v), = row.items()
        if k in spec:
            if v != spec[k]:
                replaced.append(k)
            new.append({k: list(spec[k])})
        else:
            new.append(dict(row))
    existing = {k for row in fb for k in row}
    added = [k for k in spec if k not in existing]
    new.extend({k: list(spec[k])} for k in added)
    return new, replaced, added


def assert_readback(old: dict, back: dict, spec: dict[str, list], added: list[str]) -> None:
    if sorted(old) != sorted(back):
        raise SystemExit(f"top-level keys changed: {sorted(old)} -> {sorted(back)}")
    for k in old:
        if k != "fallbacks" and json.dumps(old[k], sort_keys=True) != json.dumps(back[k], sort_keys=True):
            raise SystemExit(f"non-fallbacks key {k!r} changed -- ROLL BACK")
    o, n = rows_of(old["fallbacks"]), rows_of(back["fallbacks"])
    if len(n) != len(o) + len(added):
        raise SystemExit(f"row count {len(o)} -> {len(n)}, expected +{len(added)}")
    for k, v in o.items():
        if k not in spec and n.get(k) != v:
            raise SystemExit(f"untouched row {k!r} changed: {v} -> {n.get(k)}")
    for k, v in spec.items():
        if n.get(k) != v:
            raise SystemExit(f"spec row {k!r} reads back {n.get(k)}, wanted {v}")
    print(f"  readback OK: {len(old)} top keys intact, rows {len(o)} -> {len(n)}, "
          f"{len(spec)} spec rows exact, others byte-identical")


def pods() -> list[tuple[str, str]]:
    out = sh(["kubectl", "-n", NS, "get", "pods", "-l", PROD_SELECTOR, "-o",
              "jsonpath={range .items[*]}{.metadata.name} {.status.podIP}\n{end}"])
    return [tuple(l.split()) for l in out.splitlines() if len(l.split()) == 2]


def master_key() -> str:
    b64 = sh(["kubectl", "-n", NS, "get", "secret", "litellm-secrets-r2",
              "-o", "jsonpath={.data.LITELLM_MASTER_KEY}"])
    return base64.b64decode(b64).decode()


def pod_get(pod: str, mk: str, path: str) -> object:
    # the litellm image has no curl; use the bundled python3
    code = ("import json,os,sys,urllib.request;"
            "q=urllib.request.Request('http://127.0.0.1:4000'+sys.argv[1],"
            "headers={'Authorization':'Bearer '+os.environ['MK']});"
            "print(urllib.request.urlopen(q,timeout=30).read().decode())")
    return json.loads(sh(["kubectl", "-n", NS, "exec", pod, "-c", "litellm", "--",
                          "env", f"MK={mk}", "python3", "-c", code, path]))


def pod_rows(pod: str, mk: str) -> dict[str, list]:
    rs = pod_get(pod, mk, "/get/config/callbacks")["router_settings"]
    if not isinstance(rs, dict):
        raise SystemExit(f"{pod}: router_settings is {type(rs).__name__}, expected dict")
    return {k: v for row in (rs.get("fallbacks") or []) for k, v in row.items()}


def live_groups(pod: str, mk: str) -> set[str]:
    d = pod_get(pod, mk, "/model_group/info")
    rows = d.get("data", d) if isinstance(d, dict) else d
    return {r.get("model_group") for r in rows}


def converged(pod_list, mk, spec) -> list[str]:
    lag = []
    for name, _ in pod_list:
        r = pod_rows(name, mk)
        bad = [k for k, v in spec.items() if r.get(k) != v]
        print(f"  {'OK ' if not bad else 'NO '} {name}  rows={len(r)}"
              + (f"  missing/stale: {bad}" if bad else ""))
        if bad:
            lag.append(name)
    return lag


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", help="JSON {group: [targets...]}")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup", help="required with --apply: pre-change router_settings json")
    ap.add_argument("--verify", action="store_true", help="read-only: DB + every prod pod")
    ap.add_argument("--restore", metavar="FILE", help="write FILE's fallbacks back (needs --apply)")
    a = ap.parse_args()

    rs = read_rs()
    mk = master_key()
    pod_list = pods()
    if not pod_list:
        raise SystemExit(f"no pods match {PROD_SELECTOR} -- refusing to guess the lane")

    if a.restore:
        saved = json.loads(pathlib.Path(a.restore).read_text())["fallbacks"]
        print(f"restore: {len(rs['fallbacks'])} -> {len(saved)} rows from {a.restore}")
        if not a.apply:
            print("dry-run; pass --apply")
            return 0
        psql("update \"LiteLLM_Config\" set param_value = jsonb_set(param_value, '{fallbacks}', "
             f"$${json.dumps(saved)}$$::jsonb) where param_name='router_settings'")
        if json.dumps(read_rs()["fallbacks"], sort_keys=True) != json.dumps(saved, sort_keys=True):
            raise SystemExit("restore readback mismatch")
        print("restored; hot-loads in ~30s, confirm with --verify")
        return 0

    if not a.spec:
        raise SystemExit("--spec is required")
    spec = json.loads(pathlib.Path(a.spec).read_text())
    if not spec or not all(isinstance(v, list) and v for v in spec.values()):
        raise SystemExit("spec must map each group to a non-empty target list")

    if a.verify:
        db = rows_of(rs["fallbacks"])
        bad = [k for k, v in spec.items() if db.get(k) != v]
        print(f"DB rows={len(db)}  spec rows exact: {len(spec) - len(bad)}/{len(spec)}"
              + (f"  differ: {bad}" if bad else ""))
        lag = converged(pod_list, mk, spec)
        print("\n  layers 1-2 only: loaded, NOT proven to answer.")
        return 0 if not bad and not lag else 1

    live = live_groups(pod_list[0][0], mk)
    names = set(spec) | {t for v in spec.values() for t in v}
    dead = sorted(n for n in names if n not in live)
    if dead:
        raise SystemExit(f"not live model groups on {pod_list[0][0]}: {dead}")
    print(f"all {len(names)} groups/targets live on {pod_list[0][0]}")

    new_fb, replaced, added = build(rs["fallbacks"], spec)
    cur = rows_of(rs["fallbacks"])
    for k, v in spec.items():
        tag = "ADD    " if k in added else ("REPLACE" if k in replaced else "same   ")
        print(f"  {tag} {k}: {cur.get(k)}  ->  {v}")
    print(f"rows: {len(rs['fallbacks'])} -> {len(new_fb)}")
    if not replaced and not added:
        print("nothing to do")
        return 0
    if not a.apply:
        print("\ndry-run; pass --apply --backup <file>")
        return 0
    if not a.backup:
        raise SystemExit("--apply requires --backup <file>")
    pathlib.Path(a.backup).write_text(json.dumps(rs, ensure_ascii=False, indent=2))
    print(f"backup -> {a.backup}")

    psql("update \"LiteLLM_Config\" set param_value = jsonb_set(param_value, '{fallbacks}', "
         f"$${json.dumps(new_fb)}$$::jsonb) where param_name='router_settings'")
    assert_readback(rs, read_rs(), spec, added)
    print(f"DB written at {psql('select now()::timestamp(0)::text').strip()} UTC; polling pods")

    deadline = time.time() + 150
    lag = [n for n, _ in pod_list]
    while time.time() < deadline and lag:
        time.sleep(15)
        lag = converged(pod_list, mk, spec)
    if lag:
        print(f"\n{len(lag)} pod(s) behind after 150s -- investigate before any restart")
        return 1
    print(f"\nlayers 1-2 done: {len(pod_list)}/{len(pod_list)} pods hold all {len(spec)} rows.")
    print("layer 3 (does the leg answer) is NOT done by this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
