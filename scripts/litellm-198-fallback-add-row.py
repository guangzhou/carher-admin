#!/usr/bin/env python3
"""Add ONE brand-new row to 198 ``router_settings.fallbacks``, surgically.

The existing three tools all operate on a row that already exists
(``fallback-prepend`` explicitly asserts ``len(fallbacks)`` is unchanged --
"a prepend never adds a row"). When a freshly registered group has no chain at
all there was no tool for it, and the gap invites a hand-written
``POST /config/update`` -- which silently wipes ``model_group_alias``.

First use (2026-09-22): ``sa-grok-4.7`` was registered that morning and had no
fallback row, while ``cursor-*`` keys were about to be aliased onto it. Mirrors
the ``sa-grok-4.6`` chain established 2026-09-20::

    sa-grok-4.7 -> ['openrouter-deepseek-v4.1-flash', 'deepseek-v4-flash-responses']

Invariants this encodes (each one is somebody's outage):

* **``jsonb_set(param_value,'{fallbacks}', ...)`` only.** Never
  ``POST /config/update`` -- a partial ``router_settings`` there wipes
  ``model_group_alias`` and detonates on the next restart. Never ``kubectl
  apply`` on 198: the repo manifests are stale.
* **Every target must be a live group on the running router.** A chain naming a
  group that does not exist is a row that reads fine and cannot hold. Checked
  against ``/model/info`` before the write, not after.
* **The group must not already have a row.** Adding a second row for the same
  key is not "merge"; whichever loses is silently dead. Refuse and point at
  ``fallback-prepend`` instead.
* **``router_settings`` hot-loads every 30s** (measured 2026-09-20, property of
  THIS image -- re-measure after an up/downgrade). So ``--apply`` polls the
  serving pods instead of restarting them.
* **Config convergence is layer 1 of 3.** This script proves "the DB row says
  what you meant" and "every serving pod holds it". It does NOT prove traffic
  landing there gets an answer -- that needs per-status SpendLogs on the
  landing group. A row that exists and is even *reached* can still be a leg
  that never answers.

Usage (on 198)::

    export LITELLM_MASTER_KEY=$(sudo kubectl -n litellm-product get secret \
      litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)

    python3 litellm-198-fallback-add-row.py --group sa-grok-4.7 \
        --target openrouter-deepseek-v4.1-flash --target deepseek-v4-flash-responses
    python3 litellm-198-fallback-add-row.py --group sa-grok-4.7 \
        --target openrouter-deepseek-v4.1-flash --target deepseek-v4-flash-responses \
        --apply --backup ~/fb-$(date -u +%Y%m%dT%H%M%S).json
    python3 litellm-198-fallback-add-row.py --restore ~/fb-<ts>.json --apply
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

NS = "litellm-product"
DB_POD = "litellm-db-0"
PROXY = os.environ.get("LITELLM_198", "http://127.0.0.1:30402")
ROUTE_LABEL = "carher.net/litellm-production-route=enabled"


def sh(cmd: str) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("cmd failed: %s\n%s" % (cmd, r.stderr[:500]))
    return r.stdout


def psql(sql: str) -> str:
    """Single statement via -tAc. Multi-line SQL through ssh gets eaten."""
    return sh("sudo kubectl -n %s exec %s -- psql -U litellm -d litellm -tAc %s"
              % (NS, DB_POD, json_sh_quote(sql)))


def json_sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def read_router_settings() -> dict:
    raw = psql("select param_value from \"LiteLLM_Config\" "
               "where param_name='router_settings';").strip()
    if not raw:
        sys.exit("no router_settings row in LiteLLM_Config")
    return json.loads(raw)


def write_fallbacks(fallbacks: list) -> None:
    """Surgical: replace ONLY the fallbacks key, leave the rest of the blob alone."""
    payload = json.dumps(fallbacks)
    sql = ("update \"LiteLLM_Config\" set param_value = "
           "jsonb_set(param_value,'{fallbacks}', $J$%s$J$::jsonb) "
           "where param_name='router_settings';" % payload)
    sh("sudo kubectl -n %s exec %s -- psql -U litellm -d litellm -v ON_ERROR_STOP=1 -c %s"
       % (NS, DB_POD, json_sh_quote(sql)))


def master_key() -> str:
    k = os.environ.get("LITELLM_MASTER_KEY")
    if not k:
        sys.exit("LITELLM_MASTER_KEY not set (read it from secret/litellm-secrets)")
    return k


def live_groups(key: str) -> set:
    req = urllib.request.Request(
        PROXY + "/model/info", headers={"Authorization": "Bearer " + key})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
    except urllib.error.HTTPError as e:
        sys.exit("GET /model/info -> %s %s" % (e.code, e.read().decode()[:300]))
    return {m.get("model_name") for m in data.get("data", [])}


def row_group(row: dict) -> str:
    return next(iter(row))


def serving_pods() -> list:
    out = sh("sudo kubectl -n %s get pods -l %s --field-selector=status.phase=Running "
             "-o jsonpath='{range .items[*]}{.metadata.name}{\"\\n\"}{end}'"
             % (NS, ROUTE_LABEL))
    return [p for p in out.split() if p]


def pod_has_row(pod: str, group: str, targets: list, key: str) -> bool:
    """Layer 2: does THIS pod hold the row in memory? Read its own config view."""
    cmd = ("sudo kubectl -n %s exec %s -- python3 -c %s" % (
        NS, pod, json_sh_quote(
            "import json,urllib.request,sys\n"
            "r=urllib.request.Request('http://127.0.0.1:4000/get/config/callbacks',"
            "headers={'Authorization':'Bearer '+sys.argv[1]})\n"
            "d=json.load(urllib.request.urlopen(r,timeout=30))\n"
            "print(json.dumps(d))")) + " " + key)
    try:
        blob = json.loads(sh(cmd))
    except SystemExit:
        return False
    text = json.dumps(blob)
    return ('"%s"' % group) in text and all(('"%s"' % t) in text for t in targets)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", help="model group that gets the new chain")
    ap.add_argument("--target", action="append", default=[],
                    help="fallback target, in order; repeat for multiple hops")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup", help="write the pre-change fallbacks array here (required with --apply)")
    ap.add_argument("--restore", help="restore the fallbacks array from a snapshot")
    ap.add_argument("--poll-seconds", type=int, default=90)
    a = ap.parse_args()

    key = master_key()
    rs = read_router_settings()
    fb = rs.get("fallbacks") or []

    if a.restore:
        snap = json.load(open(a.restore))
        want = snap["fallbacks"] if isinstance(snap, dict) else snap
        print("restore: fallbacks %d rows -> %d rows" % (len(fb), len(want)))
        if not a.apply:
            print("dry-run: no writes")
            return 0
        write_fallbacks(want)
        now = read_router_settings().get("fallbacks") or []
        ok = now == want
        print("readback identical to snapshot: %s" % ok)
        return 0 if ok else 1

    if not a.group or not a.target:
        sys.exit("--group and at least one --target are required")

    existing = {row_group(r) for r in fb if isinstance(r, dict) and r}
    if a.group in existing:
        sys.exit("%s already has a fallback row -- use litellm-198-fallback-prepend.py "
                 "(a second row for the same group is not a merge)" % a.group)

    groups = live_groups(key)
    missing = [t for t in a.target if t not in groups]
    if missing:
        sys.exit("target(s) not a live model group on this router: %s\n"
                 "(a chain naming a non-existent group reads fine and cannot hold)"
                 % missing)
    if a.group not in groups:
        sys.exit("--group %s is not a live model group on this router" % a.group)

    new_row = {a.group: list(a.target)}
    print("router_settings top-level keys : %d" % len(rs))
    print("fallbacks rows                 : %d -> %d" % (len(fb), len(fb) + 1))
    print("new row                        : %s -> %s" % (a.group, a.target))
    print("all targets are live groups    : yes")

    if not a.apply:
        print("dry-run: no writes")
        return 0
    if not a.backup:
        sys.exit("--apply requires --backup")

    json.dump({"fallbacks": fb}, open(a.backup, "w"), ensure_ascii=False, indent=1)
    print("backup -> %s (%d rows)" % (a.backup, len(fb)))

    write_fallbacks(fb + [new_row])

    # ---- layer 1: the DB row says what you meant --------------------------
    rs2 = read_router_settings()
    fb2 = rs2.get("fallbacks") or []
    bad = []
    if set(rs2) != set(rs):
        bad.append("top-level keys changed: %s" % (set(rs2) ^ set(rs)))
    for k in rs:
        if k != "fallbacks" and rs2.get(k) != rs[k]:
            bad.append("non-fallbacks key mutated: %s" % k)
    if len(fb2) != len(fb) + 1:
        bad.append("row count %d, want %d" % (len(fb2), len(fb) + 1))
    if fb2[:len(fb)] != fb:
        bad.append("a pre-existing row changed")
    if fb2[-1:] != [new_row]:
        bad.append("appended row is not what we sent: %s" % fb2[-1:])
    print("layer1 config : %s" % ("ok" if not bad else "FAIL " + "; ".join(bad)))
    if bad:
        return 1

    # ---- layer 2: every serving pod holds it ------------------------------
    pods = serving_pods()
    print("layer2 loaded : polling %d serving pod(s), hot-reload is ~30s" % len(pods))
    deadline = time.time() + a.poll_seconds
    lagging = list(pods)
    while lagging and time.time() < deadline:
        lagging = [p for p in lagging if not pod_has_row(p, a.group, a.target, key)]
        if lagging:
            time.sleep(10)
    for p in pods:
        print("   %-40s %s" % (p, "behind" if p in lagging else "holds it"))
    if lagging:
        print("   still behind after %ss. Only if this persists:\n"
              "     sudo kubectl -n %s rollout restart deploy/<the gray deploy>"
              % (a.poll_seconds, NS))
        return 1

    print("\nlayer3 held   : NOT proven here. A row that exists and is even reached\n"
          "                can still be a leg that never answers. Judge by per-status\n"
          "                SpendLogs on %s." % a.target[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
