#!/usr/bin/env python3
"""Set per-deployment ``litellm_params.timeout`` on 198 (ns litellm-product).

Why this exists: the effective per-attempt timeout comes from DB
``router_settings.timeout`` = 300s, and a group with no
``model_group_retry_policy`` retries ``num_retries``=5 more times, so ONE hung
leg burns 300 x 6 = 1800s before the caller sees anything. Measured 2026-10-30:
``ag-gemini-3.8-flash`` had 7 requests wait 1830-1863s for a 408.

Why NOT lower the global ``router_settings.timeout``: in the same 24h, 396 slow
(>290s) rows were real long generations (avg 3857 completion tokens, no failure
status). A global 90-120s cut would have broken ~380 real requests/day. The
timeout belongs on the individual leg, sized from that leg's own p99.

Why ``jsonb_set`` and not ``POST /model/update``: ``litellm_params.model`` /
``api_base`` / ``api_key`` are stored ENCRYPTED, and /model/update wants the
whole params object back -- re-sending re-encrypts and can corrupt them (see
skill ``add-litellm-model`` SS "ProxyModelTable 的 model/api_base 是加密存的").
``jsonb_set(litellm_params,'{timeout}',...)`` rewrites exactly one key.

Propagation: every write to ``LiteLLM_ProxyModelTable`` publishes to
``litellm_proxy.config_change``; pods re-run ``add_deployment`` in ~1-6s, else
on the <=30s poll. No rollout needed.

Usage (on 198, needs kubectl)::

    python3 litellm-198-leg-timeout.py --spec legs.json              # dry-run
    python3 litellm-198-leg-timeout.py --spec legs.json --apply --backup b.json
    python3 litellm-198-leg-timeout.py --spec legs.json --verify     # DB + pods
    python3 litellm-198-leg-timeout.py --restore b.json --apply

Spec (JSON): ``{"<model_id>": <seconds>|null, ...}``; null removes the key so
the leg falls back to the global 300s. ``model_id`` is the deployment id as it
appears in ``LiteLLM_ProxyModelTable`` (e.g. ``cliproxy/ag-gemini-3.8-flash``),
NOT the group name -- a group can have several legs and they rarely want the
same number.
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


def read_legs() -> dict[str, dict]:
    """model_id -> {name, timeout}. Reads only what we touch, never the secrets."""
    raw = psql("select model_id || E'\\t' || model_name || E'\\t' || "
               "coalesce(litellm_params->>'timeout','') "
               "from \"LiteLLM_ProxyModelTable\"")
    out: dict[str, dict] = {}
    for line in raw.splitlines():
        if line.startswith("SET") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        mid, name, to = parts
        out[mid] = {"name": name, "timeout": float(to) if to else None}
    if not out:
        raise SystemExit("ProxyModelTable read as 0 rows -- that is a broken read, not an answer")
    return out


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
    # the litellm image has no curl; use the bundled python3
    code = ("import json,os,sys,urllib.request;"
            "q=urllib.request.Request('http://127.0.0.1:4000'+sys.argv[1],"
            "headers={'Authorization':'Bearer '+os.environ['MK']});"
            "print(urllib.request.urlopen(q,timeout=30).read().decode())")
    return json.loads(sh(["kubectl", "-n", NS, "exec", pod, "-c", "litellm", "--",
                          "env", f"MK={mk}", "python3", "-c", code, path]))


def pod_timeouts(pod: str, mk: str) -> dict[str, object]:
    """model_id -> timeout as the RUNNING router sees it (layer 2)."""
    d = pod_get(pod, mk, "/model/info")
    rows = d.get("data", d) if isinstance(d, dict) else d
    out: dict[str, object] = {}
    for r in rows:
        mid = (r.get("model_info") or {}).get("id")
        if mid:
            out[mid] = (r.get("litellm_params") or {}).get("timeout")
    if not out:
        raise SystemExit(f"{pod}: /model/info gave 0 ids -- broken read")
    return out


def check_spec(spec: dict, legs: dict[str, dict]) -> None:
    missing = sorted(k for k in spec if k not in legs)
    if missing:
        raise SystemExit("not deployment ids in ProxyModelTable (group name instead of "
                         f"model_id?): {missing}")
    for k, v in spec.items():
        if v is None:
            continue
        if not isinstance(v, (int, float)) or not (5 <= v <= 3600):
            raise SystemExit(f"{k}: timeout {v!r} out of sane range 5..3600")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", help='JSON {model_id: seconds|null}')
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup", help="required with --apply")
    ap.add_argument("--verify", action="store_true", help="read-only: DB + every prod pod")
    ap.add_argument("--restore", metavar="FILE")
    a = ap.parse_args()

    legs = read_legs()
    pod_list = pods()
    if not pod_list:
        raise SystemExit(f"no Running pods match {PROD_SELECTOR} -- refusing to guess the lane")
    mk = master_key()

    if a.restore:
        saved = json.loads(pathlib.Path(a.restore).read_text())
        spec = {k: v["timeout"] for k, v in saved.items()}
    elif a.spec:
        spec = json.loads(pathlib.Path(a.spec).read_text())
    else:
        raise SystemExit("--spec or --restore is required")
    check_spec(spec, legs)

    if a.verify:
        bad = [k for k, v in spec.items() if legs[k]["timeout"] != (float(v) if v is not None else None)]
        print(f"DB: {len(spec) - len(bad)}/{len(spec)} legs exact" + (f"  differ: {bad}" if bad else ""))
        rc = 1 if bad else 0
        for p in pod_list:
            live = pod_timeouts(p, mk)
            lag = [k for k, v in spec.items()
                   if (live.get(k) if live.get(k) is None else float(live[k]))
                   != (float(v) if v is not None else None)]
            print(f"  {'OK ' if not lag else 'NO '} {p}  ids={len(live)}" + (f"  stale: {lag}" if lag else ""))
            if lag:
                rc = 1
        print("\n  layers 1-2 only (written + loaded). Layer 3 = a real slow/hung call "
              "actually returning at the new deadline.")
        return rc

    changed = {k: v for k, v in spec.items()
               if legs[k]["timeout"] != (float(v) if v is not None else None)}
    for k, v in spec.items():
        cur = legs[k]["timeout"]
        tag = "SET   " if k in changed else "same  "
        print(f"  {tag} {k}  ({legs[k]['name']})  {cur if cur is not None else '(none->300)'} -> "
              f"{v if v is not None else '(none->300)'}")
    if not changed:
        print("nothing to do")
        return 0
    if not a.apply:
        print(f"\n{len(changed)} leg(s) would change; dry-run -- pass --apply --backup <file>")
        return 0
    if not a.backup:
        raise SystemExit("--apply requires --backup <file>")
    pathlib.Path(a.backup).write_text(json.dumps({k: legs[k] for k in spec},
                                                 ensure_ascii=False, indent=2))
    print(f"backup -> {a.backup}")

    for k, v in changed.items():
        if v is None:
            psql("update \"LiteLLM_ProxyModelTable\" set litellm_params = "
                 f"litellm_params - 'timeout' where model_id = $${k}$$")
        else:
            psql("update \"LiteLLM_ProxyModelTable\" set litellm_params = "
                 f"jsonb_set(litellm_params, '{{timeout}}', $${float(v)}$$::jsonb) "
                 f"where model_id = $${k}$$")

    back = read_legs()
    for k in spec:
        if back[k]["name"] != legs[k]["name"]:
            raise SystemExit(f"{k}: model_name changed {legs[k]['name']} -> {back[k]['name']} -- ROLL BACK")
        want = float(spec[k]) if spec[k] is not None else None
        if back[k]["timeout"] != want:
            raise SystemExit(f"{k}: reads back {back[k]['timeout']}, wanted {want} -- ROLL BACK")
    untouched = [k for k in legs if k not in spec and legs[k] != back.get(k)]
    if untouched:
        raise SystemExit(f"legs changed that we never named: {untouched} -- ROLL BACK")
    if len(back) != len(legs):
        raise SystemExit(f"row count {len(legs)} -> {len(back)} -- ROLL BACK")
    print(f"  DB readback OK: {len(changed)} changed, {len(legs) - len(spec)} untouched byte-identical")

    print("waiting for pods to pick it up (pub/sub ~1-6s, poll <=30s)")
    for attempt in range(12):
        time.sleep(5)
        lag = []
        for p in pod_list:
            live = pod_timeouts(p, mk)
            for k, v in spec.items():
                got = live.get(k)
                if (got if got is None else float(got)) != (float(v) if v is not None else None):
                    lag.append(f"{p}:{k}={got}")
        if not lag:
            print(f"  all {len(pod_list)} prod pods converged after {(attempt + 1) * 5}s")
            return 0
        print(f"  {(attempt + 1) * 5}s: {len(lag)} stale")
    print("NOT converged in 60s -- check with --verify", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
