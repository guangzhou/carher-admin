#!/usr/bin/env python3
"""Prepend one fallback target to ONE existing 198 fallback row, surgically.

First use (2026-09-20): ``sa-grok-4.6`` fell back to
``['deepseek-v4-flash-responses']``; ``openrouter-deepseek-v4.1-flash`` went to
the head of that chain so a grok outage lands on OpenRouter DeepSeek v4.1 Flash
first and the official DeepSeek responses group stays as the second leg.
Defaults still encode that case; ``--group``/``--target`` generalise it.

That change is also the worked example for why ``--regress`` exists: the leg it
displaced was landing **983 failure / 959 success over 7 days (49%)**, and
**409/38 (8.5%) in the final 12 hours** -- every failure with
``completion_tokens=0``. A fallback row that exists and is even *reached* can
still be a leg that never answers. Config convergence does not measure that;
only per-status SpendLogs on the landing model does.

Why this shape and not something else
-------------------------------------
* **Global by group name is the only granularity that reaches cursor here.**
  676 ``cursor-*`` keys reach grok through a per-key ``aliases`` entry
  (``grok-4.6``/``gpt-5.2``/``gpt-5.4``/``chatgpt-gpt-5.4`` -> ``sa-grok-4.6``).
  Per-key alias rewrite happens in
  ``litellm_pre_call_utils._update_model_if_key_alias_exists``, which mutates
  ``data["model"]`` *before* the router -- so the fallback lookup sees the
  POST-rewrite name ``sa-grok-4.6``. One global row covers all of them.
  (Router-level ``model_group_alias`` is the opposite: pre-rewrite name. Do not
  mix the two rules up.)
* **``sa-grok-4.6`` is NOT cursor-exclusive.** 14-day landings: cursor ~157.5k
  (74%), ``aliyun-carher-pro198`` 51.0k, ``carher-75`` 1.3k. A global row is
  additive for them too -- they gain a first leg they did not have. That is a
  change in their behaviour and must be reported, not glossed over.
* **``jsonb_set(param_value,'{fallbacks}', ...)`` only.** Never
  ``POST /config/update``: a partial ``router_settings`` there silently wipes
  ``model_group_alias`` (24 aliases -> {}), detonating on the next restart.
  Never ``kubectl apply`` on 198 either -- the repo manifests are stale.
* **``router_settings`` DOES hot-load, every 30s.** Measured 2026-09-20 on the
  gray lane: all 4 replicas picked the new row up within ~40s with
  ``restartCount=0`` and a ``startTime`` 12h older than the write -- no rollout.
  The mechanism is in the image:
  ``constants.py`` ``PROXY_CONFIG_RELOAD_INTERVAL_SECONDS = get_env_int(..., 30)``
  drives ``proxy_server.py::_add_router_settings_from_db_config``, which calls
  ``llm_router.update_settings(**combined)``. Boot is not the only read point.
  So ``--apply`` polls instead of restarting, and only prints a restart command
  if a pod is still behind after the poll window. This supersedes the older
  "fallbacks never hot-load, must rollout" note -- but it is a property of THIS
  image, so re-measure after an upgrade or downgrade.

Verification this script performs on readback (all must hold):
  * top-level key count unchanged, every non-``fallbacks`` key byte-identical;
  * ``len(fallbacks)`` unchanged (a prepend never adds a row);
  * exactly ONE row differs, it is the requested group, and its only delta is
    the target inserted at index 0;
  * the target is a live model group on the running router.

Three layers, and they are NOT interchangeable -- ``--verify`` proves the first
two, only ``--regress`` touches the third:

  1. config    the DB row says what you meant           (readback assertions)
  2. loaded    every serving pod holds it in memory     (per-pod /get/config/callbacks)
  3. **held**  traffic that lands there gets an answer  (per-status SpendLogs)

Usage (run on 198, where kubectl -n litellm-product works)::

    python3 litellm-198-fallback-prepend.py                  # dry-run
    python3 litellm-198-fallback-prepend.py --apply \
        --backup /root/rs-bak/router_settings.<utc>.json
    python3 litellm-198-fallback-prepend.py --verify         # read-only, layers 1-2
    python3 litellm-198-fallback-prepend.py --regress --since '2026-09-20 01:59:00'
    python3 litellm-198-fallback-prepend.py --restore <file> --apply
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

NS = "litellm-product"
DB_POD = "litellm-db-0"
DB_USER = "litellm"
DB_NAME = "litellm"
PROD_SELECTOR = "carher.net/litellm-production-route=enabled"
SECRET = "litellm-secrets"

DEFAULT_GROUP = "sa-grok-4.6"
DEFAULT_TARGET = "openrouter-deepseek-v4.1-flash"


def sh(args: list[str]) -> str:
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"FAILED: {' '.join(args)}\n{p.stderr.strip()}")
    return p.stdout


def psql(sql: str) -> str:
    return sh(["kubectl", "-n", NS, "exec", DB_POD, "--",
               "psql", "-U", DB_USER, "-d", DB_NAME, "-tAc", sql])


def read_rs() -> dict:
    raw = psql("select param_value::text from \"LiteLLM_Config\" "
               "where param_name='router_settings'").strip()
    if not raw:
        raise SystemExit("router_settings row is empty -- refusing to touch it")
    return json.loads(raw)


def rows_of(fallbacks: list[dict]) -> dict[str, list]:
    return {k: v for row in fallbacks for k, v in row.items()}


def prod_pods() -> list[tuple[str, str]]:
    out = sh(["kubectl", "-n", NS, "get", "pods", "-l", PROD_SELECTOR,
              "-o", "jsonpath={range .items[*]}{.metadata.name} {.status.podIP}\n{end}"])
    return [(l.split()[0], l.split()[1]) for l in out.splitlines() if len(l.split()) == 2]


def master_key() -> str:
    import base64
    b64 = sh(["kubectl", "-n", NS, "get", "secret", SECRET,
              "-o", "jsonpath={.data.LITELLM_MASTER_KEY}"])
    return base64.b64decode(b64).decode()


def pod_live(ip: str, mk: str) -> tuple[int, int, list | None]:
    """(fallback row count, alias count, sa-grok row) as the pod holds it in memory."""
    raw = sh(["curl", "-s", "-m", "25", f"http://{ip}:4000/get/config/callbacks",
              "-H", f"Authorization: Bearer {mk}"])
    rs = json.loads(raw)["router_settings"]
    fb = rs.get("fallbacks") or []
    return len(fb), len(rs.get("model_group_alias") or {}), rows_of(fb).get(DEFAULT_GROUP)


def live_group_exists(ip: str, mk: str, name: str) -> bool:
    raw = sh(["curl", "-s", "-m", "30", f"http://{ip}:4000/model_group/info",
              "-H", f"Authorization: Bearer {mk}"])
    d = json.loads(raw)
    rows = d.get("data", d) if isinstance(d, dict) else d
    return any(r.get("model_group") == name for r in rows)


def _norm(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def landing_of(group: str, target: str, since: str) -> tuple[str | None, list[str]]:
    """Resolve the SpendLogs `model` value that `target` lands on.

    SpendLogs stores the LANDING model (`openrouter/deepseek/deepseek-v4.1-flash`),
    not the request-side group name (`openrouter-deepseek-v4.1-flash`), so a query
    keyed on the group name silently returns zero rows -- which reads as "no
    traffic" when it actually means "wrong column value".

    Neither available metadata source can give this mapping:
      * ``/model_group/info`` carries no ``litellm_params`` (only ``providers``);
      * ``LiteLLM_ProxyModelTable.litellm_params->>'model'`` is ENCRYPTED at rest.
    So derive it from observed rows: take the group's distinct landings and pick
    the one whose normalised form contains the target minus its provider prefix.
    Ambiguity is returned to the caller rather than guessed.
    """
    raw = psql("select distinct model from \"LiteLLM_SpendLogs\" "
               f"where model_group = $${group}$$ "
               f"  and \"startTime\" > timestamp $${since}$$ and model is not null")
    seen = [l.strip() for l in raw.splitlines() if l.strip()]
    provider = target.split("-", 1)[0]
    tail = _norm(target[len(provider):]) or _norm(target)
    hits = [m for m in seen if _norm(m).startswith(_norm(provider)) and tail in _norm(m)]
    return (hits[0] if len(hits) == 1 else None), seen


def regress(group: str, target: str, since: str, landing: str | None = None) -> int:
    """Layer 3: did traffic that landed on the new head actually get answered?

    Splits by cursor vs non-cursor because the global row is not cursor-exclusive
    -- lumping them together hides which population a failure belongs to.
    """
    if not landing:
        landing, seen = landing_of(group, target, since)
        if not landing:
            print(f"cannot resolve the landing model for {target!r} unambiguously.")
            print(f"  landings seen for {group} since {since}: {seen or '(none)'}")
            print("  pass --landing <value>. This is NOT evidence either way --\n"
                  "  layer 3 is simply still unmeasured.")
            return 2
    print(f"group={group}  target={target}  landing={landing}  since={since} (UTC)\n")

    rows = psql(
        "select case when coalesce(t.key_alias,'') like 'cursor-%' then 'cursor' "
        "  else coalesce(t.key_alias,'(unknown)') end, s.status, count(*), "
        "  round(avg(s.prompt_tokens)), round(avg(s.completion_tokens)), max(s.\"startTime\") "
        "from \"LiteLLM_SpendLogs\" s "
        "left join \"LiteLLM_VerificationToken\" t on t.token = s.api_key "
        f"where s.\"startTime\" > timestamp $${since}$$ "
        f"  and s.model_group = $${group}$$ and s.model = $${landing}$$ "
        "group by 1, 2 order by 1, 3 desc").strip()
    if not rows:
        print("NO ROWS: the new head has taken no traffic yet. That is not a pass --\n"
              "  it means layer 3 is still unmeasured. Re-run after real traffic,\n"
              "  or check that the main leg simply has not failed in this window.")
        return 2

    ok = bad = 0
    print(f"{'population':<24} {'status':<9} {'n':>6} {'avg_pt':>8} {'avg_ct':>8}  last")
    for line in rows.splitlines():
        f = line.split("|")
        if len(f) < 6:
            continue
        grp, status, n, pt, ct, last = (x.strip() for x in f[:6])
        print(f"{grp:<24} {status:<9} {n:>6} {pt:>8} {ct:>8}  {last}")
        if status == "success":
            ok += int(n)
        else:
            bad += int(n)

    tot = ok + bad
    print(f"\n  success {ok}/{tot} = {100 * ok // tot if tot else 0}%")
    print("  ⚠ completion_tokens=0 on a 'success' row is an empty shell -- not held.")
    print("  ⚠ failures inside one narrow window with one error_class are usually a\n"
          "    transient; prove it by re-running later and showing they do NOT recur.")
    return 0 if bad == 0 else 1


def build(rs: dict, group: str, target: str) -> tuple[list[dict], list, list]:
    """Return (new fallbacks, before targets, after targets). Prepend, idempotent."""
    fb = rs.get("fallbacks") or []
    hit = [i for i, row in enumerate(fb) if group in row]
    if len(hit) != 1:
        raise SystemExit(f"expected exactly 1 fallback row for {group!r}, found {len(hit)}")
    idx = hit[0]
    before = list(fb[idx][group])
    if before and before[0] == target:
        return fb, before, before                      # already done
    after = [target] + [t for t in before if t != target]
    new = [dict(r) for r in fb]
    new[idx] = {group: after}
    return new, before, after


def assert_readback(old: dict, new_rs: dict, group: str,
                    expect_after: list, expect_before: list) -> None:
    if sorted(old) != sorted(new_rs):
        raise SystemExit(f"top-level keys changed: {sorted(old)} -> {sorted(new_rs)}")
    for k in old:
        if k == "fallbacks":
            continue
        if json.dumps(old[k], sort_keys=True) != json.dumps(new_rs[k], sort_keys=True):
            raise SystemExit(f"non-fallbacks key {k!r} changed -- ROLL BACK")
    o, n = old["fallbacks"], new_rs["fallbacks"]
    if len(o) != len(n):
        raise SystemExit(f"fallback row count changed {len(o)} -> {len(n)}")
    diffs = [(i, a, b) for i, (a, b) in enumerate(zip(o, n))
             if json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True)]
    if len(diffs) != 1:
        raise SystemExit(f"expected exactly 1 changed row, got {len(diffs)}: {diffs}")
    i, a, b = diffs[0]
    if list(a) != [group] or list(b) != [group]:
        raise SystemExit(f"the changed row is not {group!r}: {a} -> {b}")
    if a[group] != expect_before or b[group] != expect_after:
        raise SystemExit(f"row delta unexpected: {a[group]} -> {b[group]}")
    print(f"  readback OK: 13 top keys intact, {len(n)} rows, single delta on {group}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default=DEFAULT_GROUP)
    ap.add_argument("--target", default=DEFAULT_TARGET)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup", help="required with --apply: pre-change router_settings json")
    ap.add_argument("--verify", action="store_true",
                    help="read-only layers 1-2: DB row + every prod pod's memory")
    ap.add_argument("--regress", action="store_true",
                    help="read-only layer 3: per-status SpendLogs on the new head")
    ap.add_argument("--since", metavar="'YYYY-MM-DD HH:MM:SS'",
                    help="--regress window start, UTC; default = 1 hour ago")
    ap.add_argument("--landing", metavar="MODEL",
                    help="--regress: SpendLogs `model` value, if auto-resolve is ambiguous")
    ap.add_argument("--restore", metavar="FILE",
                    help="write this file's fallbacks array back (needs --apply)")
    a = ap.parse_args()

    rs = read_rs()
    mk = master_key()
    pods = prod_pods()
    if not pods:
        raise SystemExit(f"no pods match {PROD_SELECTOR} -- refusing to guess the lane")

    if a.verify:
        print(f"DB  {a.group} = {rows_of(rs['fallbacks']).get(a.group)}   "
              f"rows={len(rs['fallbacks'])} alias={len(rs['model_group_alias'])}")
        ok = True
        for name, ip in pods:
            fbn, aln, row = pod_live(ip, mk)
            good = bool(row) and row[0] == a.target
            ok &= good
            print(f"  {'OK ' if good else 'NO '} {name} {ip}  fb={fbn} alias={aln}  {a.group}={row}")
        print("\n  layers 1-2 only. This says the row is loaded, NOT that the leg answers.")
        print(f"  for layer 3: --regress --since '<utc of the write>'")
        return 0 if ok else 1

    if a.regress:
        since = a.since or psql(
            "select (now() - interval '1 hour')::timestamp(0)::text").strip()
        return regress(a.group, a.target, since, a.landing)

    if a.restore:
        saved = json.loads(pathlib.Path(a.restore).read_text())
        target_fb = saved["fallbacks"]
        print(f"restore: {len(target_fb)} rows from {a.restore}, "
              f"{a.group} -> {rows_of(target_fb).get(a.group)}")
        if not a.apply:
            print("dry-run; pass --apply")
            return 0
        psql("update \"LiteLLM_Config\" set param_value = jsonb_set(param_value, '{fallbacks}', "
             f"$${json.dumps(target_fb)}$$::jsonb) where param_name='router_settings'")
        back = read_rs()
        if json.dumps(back["fallbacks"], sort_keys=True) != json.dumps(target_fb, sort_keys=True):
            raise SystemExit("restore readback mismatch")
        print("restored. router_settings hot-loads every ~30s; confirm with --verify")
        print("  before considering any restart (a restart is NOT normally needed).")
        return 0

    new_fb, before, after = build(rs, a.group, a.target)
    print(f"group  : {a.group}")
    print(f"before : {before}")
    print(f"after  : {after}")
    if before == after:
        print("already at the head -- nothing to do")
        return 0
    if not live_group_exists(pods[0][1], mk, a.target):
        raise SystemExit(f"{a.target!r} is not a live model group on the running router")
    print(f"target {a.target!r} confirmed live on {pods[0][0]}")
    print(f"rows   : {len(rs['fallbacks'])} -> {len(new_fb)} (must be equal)")

    if not a.apply:
        print("\ndry-run; pass --apply --backup <file>")
        return 0
    if not a.backup:
        raise SystemExit("--apply requires --backup <file>")
    pathlib.Path(a.backup).write_text(json.dumps(rs, ensure_ascii=False, indent=2))
    print(f"backup -> {a.backup}")

    psql("update \"LiteLLM_Config\" set param_value = jsonb_set(param_value, '{fallbacks}', "
         f"$${json.dumps(new_fb)}$$::jsonb) where param_name='router_settings'")
    assert_readback(rs, read_rs(), a.group, after, before)

    since = psql("select now()::timestamp(0)::text").strip()
    print(f"\nDB written at {since} UTC. router_settings hot-loads every ~30s; polling.")

    import time
    deadline = time.time() + 150
    lagging = [n for n, _ in pods]
    while time.time() < deadline and lagging:
        time.sleep(10)
        lagging = []
        for name, ip in pods:
            _, _, row = pod_live(ip, mk)
            if not (row and row[0] == a.target):
                lagging.append(name)
        print(f"  ok={len(pods) - len(lagging)}/{len(pods)}"
              + (f"  waiting: {', '.join(lagging)}" if lagging else ""))

    if lagging:
        print(f"\n{len(lagging)} pod(s) still behind after 150s. Only NOW is a restart"
              f" worth considering (zero-interruption rolling update, user's call):")
        print(f"  kubectl -n {NS} rollout restart deploy/litellm-proxy-gray")
        print(f"  kubectl -n {NS} rollout status  deploy/litellm-proxy-gray --timeout=15m")
        return 1

    print(f"\nlayers 1-2 done: {len(pods)}/{len(pods)} pods lead with {a.target}, no restart.")
    print("layer 3 is NOT done. The leg is loaded; nothing yet shows it ANSWERS.")
    print(f"  python3 {pathlib.Path(__file__).name} --group {a.group} "
          f"--target {a.target} --regress --since '{since}'")
    print("Run it once real traffic has failed over, then again later to show the\n"
          "failures you saw do not recur. Report layers separately, never merged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
