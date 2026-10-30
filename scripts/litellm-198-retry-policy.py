#!/usr/bin/env python3
"""Set ``router_settings.model_group_retry_policy`` rows on 198 (ns litellm-product).

Why: DB ``router_settings.num_retries`` = 5, so a group with no retry-policy row
retries a FAILED attempt 5 more times on the same group before any fallback is
tried. Combined with a 300s per-attempt timeout that is 300 x 6 = 1800s of dead
wait. Measured 2026-10-30: ``ag-gemini-3.8-flash`` (ONE leg, no policy row) had
7 requests wait 1830-1863s before a 408 -- retrying a single hung leg 5 times
cannot succeed, it only delays the fallback.

A policy row with ``TimeoutErrorRetries: 0`` + ``DefaultRetries: 0`` makes a
timeout/error on such a group fall straight through to its fallback chain.
Do NOT use this on multi-leg groups where a retry lands on a DIFFERENT healthy
account (there a retry is the point) -- the script prints the leg count so the
decision is visible, and refuses >1 leg unless --allow-multi-leg.

Mechanism (v1.105.1 ``router.py`` ``async_function_with_retries`` ->
``_get_num_retries_from_retry_policy``): the policy is looked up by MODEL GROUP
name (post-``model_group_alias``), and a row's per-error field wins over
``num_retries``. Missing error types in a row fall back to ``DefaultRetries``,
and if that is absent too, to the global ``num_retries`` -- so a row must spell
out ``DefaultRetries`` or it leaks back to 5.

Write path is ``jsonb_set(param_value,'{model_group_retry_policy}',...)`` on the
``LiteLLM_Config`` row -- never ``POST /config/update`` (wipes
``model_group_alias``). ``router_settings`` hot-loads every ~30s.

Usage (on 198)::

    python3 litellm-198-retry-policy.py --spec pol.json                # dry-run
    python3 litellm-198-retry-policy.py --spec pol.json --apply --backup b.json
    python3 litellm-198-retry-policy.py --spec pol.json --verify
    python3 litellm-198-retry-policy.py --restore b.json --apply

Spec (JSON): ``{"<group>": {"DefaultRetries":0, "TimeoutErrorRetries":0, ...}}``
or ``{"<group>": null}`` to delete the row (back to global num_retries).
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
SECRET = "litellm-secrets-r2"
KEY = "model_group_retry_policy"
VALID = {"DefaultRetries", "TimeoutErrorRetries", "RateLimitErrorRetries",
         "ContentPolicyViolationErrorRetries", "InternalServerErrorRetries",
         "BadRequestErrorRetries", "AuthenticationErrorRetries",
         "ServiceUnavailableErrorRetries"}


def sh(args: list[str]) -> str:
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"FAILED: {' '.join(args)}\n{p.stderr.strip()}")
    return p.stdout


def psql(sql: str) -> str:
    out = sh(["kubectl", "-n", NS, "exec", DB_POD, "--", "psql", "-U", "litellm",
              "-d", "litellm", "-tAc", f"set statement_timeout='120s'; {sql}"])
    if "ERROR:" in out:
        raise SystemExit(f"psql error: {out.strip()}")
    return out


def read_rs() -> dict:
    raw = "".join(l for l in psql(
        "select param_value::text from \"LiteLLM_Config\" "
        "where param_name='router_settings'").splitlines()
        if not l.startswith("SET")).strip()
    if not raw:
        raise SystemExit("router_settings row is empty -- refusing to touch it")
    rs = json.loads(raw)
    if not isinstance(rs, dict) or not rs.get("fallbacks"):
        raise SystemExit("fallbacks read as empty -- broken read, not an answer")
    return rs


def master_key() -> str:
    b64 = sh(["kubectl", "-n", NS, "get", "secret", SECRET,
              "-o", "jsonpath={.data.LITELLM_MASTER_KEY}"])
    return base64.b64decode(b64).decode()


def pods() -> list[str]:
    out = sh(["kubectl", "-n", NS, "get", "pods", "-l", PROD_SELECTOR,
              "--field-selector=status.phase=Running",
              "-o", "jsonpath={range .items[*]}{.metadata.name}\n{end}"])
    return [l.strip() for l in out.splitlines() if l.strip()]


def pod_get(pod: str, mk: str, path: str) -> object:
    code = ("import json,os,sys,urllib.request;"
            "q=urllib.request.Request('http://127.0.0.1:4000'+sys.argv[1],"
            "headers={'Authorization':'Bearer '+os.environ['MK']});"
            "print(urllib.request.urlopen(q,timeout=30).read().decode())")
    return json.loads(sh(["kubectl", "-n", NS, "exec", pod, "-c", "litellm", "--",
                          "env", f"MK={mk}", "python3", "-c", code, path]))


def pod_policy(pod: str, mk: str) -> dict:
    rs = pod_get(pod, mk, "/get/config/callbacks")["router_settings"]
    if not isinstance(rs, dict):
        raise SystemExit(f"{pod}: router_settings is {type(rs).__name__}")
    return rs.get(KEY) or {}


def leg_count(mk: str, pod: str) -> dict[str, int]:
    d = pod_get(pod, mk, "/model_group/info")
    rows = d.get("data", d) if isinstance(d, dict) else d
    out = {}
    for r in rows:
        g = r.get("model_group")
        if g:
            ids = r.get("providers") or []
            out[g] = len(r.get("model_ids") or []) or len(ids) or 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", help='JSON {group: {…}|null}')
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--restore", metavar="FILE")
    ap.add_argument("--allow-multi-leg", action="store_true",
                    help="permit a 0-retry row on a group with >1 leg (a retry there "
                         "would have landed on a different account)")
    a = ap.parse_args()

    rs = read_rs()
    cur = rs.get(KEY) or {}
    pod_list = pods()
    if not pod_list:
        raise SystemExit(f"no Running pods match {PROD_SELECTOR}")
    mk = master_key()

    if a.restore:
        spec = json.loads(pathlib.Path(a.restore).read_text())[KEY]
        full_restore = True
    elif a.spec:
        spec = json.loads(pathlib.Path(a.spec).read_text())
        full_restore = False
    else:
        raise SystemExit("--spec or --restore is required")

    for g, v in spec.items():
        if v is None:
            continue
        if not isinstance(v, dict) or not v:
            raise SystemExit(f"{g}: policy must be a non-empty object or null")
        bad = sorted(set(v) - VALID)
        if bad:
            raise SystemExit(f"{g}: unknown retry-policy fields {bad} (litellm ignores "
                             f"these silently -- that is a no-op, not a fix)")
        if "DefaultRetries" not in v:
            raise SystemExit(f"{g}: row without DefaultRetries leaks back to the global "
                             "num_retries for every error type you did not list")
        for k, n in v.items():
            if not isinstance(n, int) or not (0 <= n <= 10):
                raise SystemExit(f"{g}.{k} = {n!r} out of range 0..10")

    if a.verify:
        bad = [g for g, v in spec.items()
               if (cur.get(g) if v is not None else cur.get(g, None)) != v]
        print(f"DB: {len(spec) - len(bad)}/{len(spec)} rows exact" + (f"  differ: {bad}" if bad else ""))
        rc = 1 if bad else 0
        for p in pod_list:
            live = pod_policy(p, mk)
            lag = [g for g, v in spec.items() if live.get(g) != v]
            print(f"  {'OK ' if not lag else 'NO '} {p}  rows={len(live)}" + (f"  stale: {lag}" if lag else ""))
            if lag:
                rc = 1
        print("\n  layers 1-2 only (written + loaded). Layer 3 = a hung leg actually "
              "falling through at ~1x timeout instead of 6x.")
        return rc

    if not full_restore:
        groups = leg_count(mk, pod_list[0])
        unknown = sorted(g for g in spec if g not in groups)
        if unknown:
            raise SystemExit(f"not live model groups on {pod_list[0]}: {unknown}")
        multi = sorted(g for g, v in spec.items()
                       if v and max(v.values()) == 0 and groups.get(g, 1) > 1)
        if multi and not a.allow_multi_leg:
            raise SystemExit(f"0-retry row on multi-leg group(s) {multi} -- a retry there "
                             "lands on another account and may be exactly what you want. "
                             "Pass --allow-multi-leg if you meant it.")
        for g, v in spec.items():
            print(f"  {g}  legs={groups.get(g)}  {json.dumps(cur.get(g), sort_keys=True)} -> "
                  f"{json.dumps(v, sort_keys=True)}")

    # --restore 必须整表替换：合并会留下备份之后新加的行，等于没撤（10-30 Lane A 读源码发现）。
    # 注意整表替换也会撤掉备份之后**别人**加的行 —— 只想撤自己那一行时用 spec `{"<group>": null}`。
    new = {} if full_restore else {k: v for k, v in cur.items()}
    for g, v in spec.items():
        if v is None:
            new.pop(g, None)
        else:
            new[g] = v
    if json.dumps(new, sort_keys=True) == json.dumps(cur, sort_keys=True):
        print("nothing to do")
        return 0
    print(f"rows: {len(cur)} -> {len(new)}")
    if not a.apply:
        print("\ndry-run; pass --apply --backup <file>")
        return 0
    if not a.backup:
        raise SystemExit("--apply requires --backup <file>")
    pathlib.Path(a.backup).write_text(json.dumps(rs, ensure_ascii=False, indent=2))
    print(f"backup -> {a.backup}  (full router_settings)")

    psql(f"update \"LiteLLM_Config\" set param_value = jsonb_set(param_value, '{{{KEY}}}', "
         f"$${json.dumps(new)}$$::jsonb) where param_name='router_settings'")

    back = read_rs()
    if sorted(back) != sorted(rs):
        raise SystemExit(f"top-level keys changed {sorted(rs)} -> {sorted(back)} -- ROLL BACK")
    for k in rs:
        if k != KEY and json.dumps(rs[k], sort_keys=True) != json.dumps(back[k], sort_keys=True):
            raise SystemExit(f"non-{KEY} key {k!r} changed -- ROLL BACK")
    if json.dumps(back.get(KEY), sort_keys=True) != json.dumps(new, sort_keys=True):
        raise SystemExit(f"{KEY} reads back differently -- ROLL BACK")
    print(f"  DB readback OK: {len(rs)} top keys intact, "
          f"{len(rs.get('fallbacks', []))} fallback rows untouched")

    print("waiting for pods (router_settings hot-load ~30s)")
    for attempt in range(12):
        time.sleep(5)
        lag = [f"{p}:{g}" for p in pod_list for g, v in spec.items()
               if pod_policy(p, mk).get(g) != v]
        if not lag:
            print(f"  all {len(pod_list)} prod pods converged after {(attempt + 1) * 5}s")
            return 0
        print(f"  {(attempt + 1) * 5}s: {len(lag)} stale")
    print("NOT converged in 60s -- check with --verify", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
