#!/usr/bin/env python3
"""Repoint ONE 198 key's request name onto another model group AND give both
names a global fallback row -- the two writes that must happen together.

WHY BOTH, ALWAYS (the whole point of this script)
  LiteLLM's router resolves in this order (read from router.py in the running
  pod, not inferred):
      specific_deployment -> has_model_id -> _get_model_from_alias REWRITE
                          -> _get_all_deployments(model_name)
  but the *fallback* table is looked up with the **pre-rewrite** name. So a key
  aliased `A -> B` needs a fallback row for **A and for B**, both carrying the
  same target list. Write only B's row and the day B's upstream dies the user
  gets a hard 5xx with `No fallback model group found for original
  model_group=A` -- the fallback silently never existed.
  See feedback_litellm_alias_fallback_pre_rewrite_lookup.

SCOPE / ISOLATION
  * per-key `aliases` (and `models` allowlist if the target name is missing) on
    exactly ONE key, read-merge-written with a zero-deletion assertion.
  * global `router_settings.fallbacks` via surgical `jsonb_set` append. NEVER a
    full-field overwrite and never POST /config/update (which silently drops
    model_group_alias -- 10 aliases -> {} -- detonating on the next restart).
  * Nothing else: no ConfigMap, no ProxyModelTable, no other key, no HerInstance.

FALLBACKS ARE GLOBAL BY GROUP NAME. There is no per-key fallback granularity on
198 (per-key router_settings.fallbacks measured NOT effective here, 2026-06-23).
Adding a row for a shared group name hands that fallback to every consumer of
the name. That is additive, never a removal -- but say so in the report.

RESTART GATE
  A fallbacks write does NOT hot-load. Polling a pod for 7 minutes after a DB
  write shows nothing; the router reads router_settings at boot. The per-key
  alias, by contrast, IS live immediately. So `--apply` leaves you with the main
  path working and the fallback staged, and prints the rollout command instead of
  running it. Restarting the 4-replica production proxy is the user's call.
  Pass --restart only with an explicit go-ahead.

VERIFY
  `--probe` clones the target key's shape into a throwaway key and sends real
  inference. It does NOT read the key's plaintext: LiteLLM_VerificationToken.token
  is the **sha256 hash**, and bearer-ing it 401s on every model name -- including
  a nonexistent one, so a negative control cannot discriminate and the whole
  reading is void. See feedback_litellm_key_probe_clones_key_shape.
  Judges: HTTP 200 + unique nonce echoed back, a nonexistent name returning 403,
  and the SpendLogs row's model_id (the response header x-litellm-model-id is an
  internal hash on 198, not the declared id).

FOOTPRINT / BACKUP / ROLLBACK
  --apply writes backup/<key>-alias-fallback-<utc>.json holding the key's
  pre-change models+aliases and the full pre-change fallbacks array.
  Rollback: --rollback --from <that file> --apply
    restores the alias values and removes exactly the rows this run appended;
    removing fallback rows needs another rollout restart to take effect.

Usage
  # preview
  python3 scripts/litellm-198-key-alias-fallback-pair.py \
      --key carher-1 --map claude-fable-5.1=cursor-fc-fable-5.1 \
      --map claude-opus-5=cursor-fc-opus-5 \
      --fallback-to anthropic.wangsu5.claude-opus-5
  # apply (alias live, fallback staged), then probe
  ... --apply
  ... --probe
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import pathlib
import subprocess
import sys
import time
from typing import Any

NS = "litellm-product"
DEPLOY_LABEL = "app=litellm-proxy"
DB_POD = "litellm-db-0"
DB_USER = "litellm"          # NOT postgres -- that role does not exist here
DB_NAME = "litellm"
JMS_HOST = "10.68.13.198"    # IP, never an asset name: names route to the wrong box
BACKUP_DIR = pathlib.Path(__file__).resolve().parent.parent / "backup"


class PairError(RuntimeError):
    pass


def _run(args: list[str], input_text: str | None = None) -> str:
    """stderr is not failure: every hop through the 198 login shell prints a
    sitecustomize warning that an earlier helper read as a hard error."""
    p = subprocess.run(args, input=input_text, capture_output=True, text=True)
    if p.returncode and not p.stdout.strip():
        raise PairError(f"{' '.join(args[:4])}: {(p.stderr or p.stdout).strip()[:600]}")
    return p.stdout


def jms_bash(script: str) -> str:
    """bash over jms stdin. No --tty: the relay rejects a pty and `exit`
    teardown eats trailing sentinels."""
    return _run(["jms", "ssh", JMS_HOST, "bash -s"], input_text=script)


def _marker(out: str, what: str) -> Any:
    line = next((l for l in out.splitlines() if l.startswith("__R__")), None)
    if not line:
        raise PairError(f"no response marker for {what}; raw:\n{out[:900]}")
    return json.loads(line[len("__R__"):])


def psql(sql: str) -> str:
    """Run SQL in the DB pod via a base64-injected .sql file.

    NOT `psql -c "..."`: the ssh -> kubectl -> psql nesting strips the double
    quotes psql needs for CamelCase identifiers, so "LiteLLM_SpendLogs" arrives
    lowercased and errors as `relation does not exist`.
    """
    b64 = base64.b64encode(sql.encode()).decode()
    script = (
        "set -u\n"
        "F=/tmp/pair.$$.sql\n"
        f"sudo kubectl -n {NS} exec {DB_POD} -- sh -c \"echo '{b64}' | base64 -d > $F\"\n"
        f"sudo kubectl -n {NS} exec {DB_POD} -- sh -c "
        f"'psql -U {DB_USER} -d {DB_NAME} -t -A -f '$F\n"
        f"sudo kubectl -n {NS} exec {DB_POD} -- rm -f $F\n"
    )
    return jms_bash(script)


def proxy_api(path: str, body: Any = None, method: str = "GET",
              api_key_env: str = "LITELLM_MASTER_KEY",
              reduce_py: str | None = None) -> Any:
    """Call the admin API from inside the proxy pod using the pod's own master
    key -- never printed, never on a cmdline.

    `reduce_py` is evaluated pod-side against the parsed response bound to `R`.
    /model/info is ~10MB here (1597 deployments) and gets truncated mid-string
    on the way out, so any reader of it MUST reduce in the pod.
    """
    runner = (
        "import json,os,base64,urllib.request,urllib.error\n"
        "p=json.loads(base64.b64decode(os.environ['REQ_B64']).decode())\n"
        "data=json.dumps(p['body']).encode() if p['body'] is not None else None\n"
        "r=urllib.request.Request('http://127.0.0.1:4000'+p['path'],data=data,"
        "headers={'Authorization':'Bearer '+os.environ[p['keyenv']],"
        "'Content-Type':'application/json'},method=p['method'])\n"
        "try:\n"
        "    x=urllib.request.urlopen(r,timeout=170)\n"
        "    R=json.load(x)\n"
        "    red=p.get('reduce')\n"
        "    out=eval(red,{'json':json},{'R':R}) if red else R\n"
        "    print('__R__'+json.dumps({'status':x.status,'body':out}))\n"
        "except urllib.error.HTTPError as e:\n"
        "    print('__R__'+json.dumps({'status':e.code,"
        "'error':e.read().decode(errors='replace')[:600]}))\n"
    )
    req = json.dumps({"path": path, "body": body, "method": method,
                      "keyenv": api_key_env, "reduce": reduce_py})
    req_b64 = base64.b64encode(req.encode()).decode()
    run_b64 = base64.b64encode(runner.encode()).decode()
    # unique temp name: a shared /tmp path collided with a stale root-owned file
    # from another round and silently ran the wrong script three times.
    script = (
        f"LP=$(sudo kubectl -n {NS} get pods -l {DEPLOY_LABEL} "
        "-o jsonpath='{.items[0].metadata.name}' | awk '{print $1}')\n"
        f"sudo kubectl -n {NS} exec -i $LP -c litellm -- env REQ_B64={req_b64} "
        f"sh -c 'echo {run_b64} | base64 -d | python3 -' 2>/dev/null\n"
    )
    res = _marker(jms_bash(script), f"{method} {path}")
    if res.get("status") not in range(200, 300):
        raise PairError(f"{method} {path} -> HTTP {res.get('status')}: {res.get('error')}")
    return res["body"]


# ---------------------------------------------------------------- discovery


def find_key(alias: str) -> dict[str, Any]:
    """Locate the key by key_alias in the DB, then read its shape via /key/info.

    NOT /key/list: it is truncated to the caller's scope (returned 10 rows and
    no carher-1 on a master-key call), so "does key X exist" must go to
    LiteLLM_VerificationToken.
    """
    esc = alias.replace("'", "''")
    out = psql(
        "SELECT token FROM \"LiteLLM_VerificationToken\" "
        f"WHERE key_alias='{esc}';"
    )
    toks = [l.strip() for l in out.splitlines()
            if len(l.strip()) == 64 and all(c in "0123456789abcdef" for c in l.strip())]
    if len(toks) != 1:
        raise PairError(f"expected exactly 1 token for key_alias={alias!r}, got {len(toks)}")
    token = toks[0]
    info = proxy_api(f"/key/info?key={token}")
    d = info.get("info") or info.get("key") or {}
    return {
        "token": token,
        "alias": alias,
        "models": list(d.get("models") or []),
        "aliases": dict(d.get("aliases") or {}),
    }


def group_truth(names: list[str]) -> dict[str, list[dict[str, str]]]:
    """api_base / model / model_info.id per group name.

    Group names lie -- `claude-opus-5` is copilot2api, `anthropic.wangsu5.
    claude-opus-5` is Wangsu, `kiro-claude-opus-5` is kiro-rs. Judge a target by
    these three fields only, never by the name reading right.
    """
    want = json.dumps(names)
    reduce_py = (
        "[{'g':m.get('model_name'),"
        "'model':(m.get('litellm_params') or {}).get('model'),"
        "'api_base':(m.get('litellm_params') or {}).get('api_base'),"
        "'id':(m.get('model_info') or {}).get('id')}"
        f" for m in R['data'] if m.get('model_name') in {want}]"
    )
    rows = proxy_api("/model/info", reduce_py=reduce_py)
    out: dict[str, list[dict[str, str]]] = {n: [] for n in names}
    for r in rows:
        out.setdefault(r["g"], []).append(r)
    return out


def read_fallbacks() -> list[dict[str, list[str]]]:
    raw = psql(
        "SELECT param_value->'fallbacks' FROM \"LiteLLM_Config\" "
        "WHERE param_name='router_settings';"
    )
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("["):
            return json.loads(line)
    raise PairError(f"could not read router_settings.fallbacks; raw:\n{raw[:600]}")


# ---------------------------------------------------------------- planning


def plan(key: dict[str, Any], mapping: dict[str, str],
         fb_target: list[str]) -> dict[str, Any]:
    """Compute alias diff, models additions, and the fallback rows to append.

    Every request name AND every alias target gets its own fallback row (the
    pre-rewrite lookup rule). Rows that already exist are skipped, never
    duplicated -- a duplicated key in the array is not an error LiteLLM reports,
    it just shadows.
    """
    alias_changes: dict[str, tuple[str | None, str]] = {}
    for src, dst in mapping.items():
        cur = key["aliases"].get(src)
        if cur != dst:
            alias_changes[src] = (cur, dst)

    # A request name only resolves if it is ALSO in the models allowlist --
    # allowlist-without-alias 400s, and alias-without-allowlist 403s.
    models_add = [n for n in mapping if n not in key["models"]]

    existing = {k for row in read_fallbacks() for k in row}
    fb_names = list(dict.fromkeys(list(mapping) + list(mapping.values())))
    fb_add = [{n: fb_target} for n in fb_names if n not in existing]
    fb_skip = [n for n in fb_names if n in existing]

    return {
        "alias_changes": alias_changes,
        "models_add": models_add,
        "fb_add": fb_add,
        "fb_skip": fb_skip,
        "fb_names": fb_names,
    }


def print_plan(key: dict[str, Any], p: dict[str, Any], truth: dict[str, list[dict[str, str]]],
               fb_target: list[str]) -> None:
    print(f"KEY {key['alias']}  token={key['token'][:8]}…  "
          f"models_n={len(key['models'])} aliases_n={len(key['aliases'])}")
    print("\n-- alias --")
    if not p["alias_changes"]:
        print("  (already in target shape)")
    for src, (cur, dst) in p["alias_changes"].items():
        print(f"  {src}: {cur!r} -> {dst!r}")
    print("\n-- models allowlist --")
    print(f"  add: {p['models_add'] or '(none needed)'}")
    print("\n-- global fallbacks (append via jsonb_set) --")
    for row in p["fb_add"]:
        print(f"  + {json.dumps(row)}")
    if p["fb_skip"]:
        print(f"  already present, left alone: {p['fb_skip']}")
    print("\n-- target truth (api_base / model_info.id, NOT the group name) --")
    for name in dict.fromkeys(list(p["fb_names"]) + fb_target):
        legs = truth.get(name) or []
        if not legs:
            print(f"  {name}: NO DEPLOYMENT (alias target must exist or every call 400s)")
        for leg in legs:
            print(f"  {name}: id={leg['id']} model={leg['model']} api_base={leg['api_base']}")


# ---------------------------------------------------------------- writes


def snapshot(key: dict[str, Any], fallbacks: list[dict], appended: list[dict],
             mapping: dict[str, str]) -> pathlib.Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = BACKUP_DIR / f"{key['alias']}-alias-fallback-{ts}.json"
    path.write_text(json.dumps({
        "utc": ts,
        "key_alias": key["alias"],
        "token_prefix": key["token"][:8],
        "models_before": key["models"],
        "aliases_before": key["aliases"],
        "fallbacks_before": fallbacks,
        "fallbacks_appended": appended,
        "mapping_applied": mapping,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_key(key: dict[str, Any], p: dict[str, Any]) -> None:
    """Read-merge-write. `models` and `aliases` are whole-field replaces in
    LiteLLM, so a partial POST deletes everything it omits."""
    models = list(key["models"]) + p["models_add"]
    aliases = dict(key["aliases"])
    for src, (_cur, dst) in p["alias_changes"].items():
        aliases[src] = dst

    proxy_api("/key/update", {"key": key["token"], "models": models,
                              "aliases": aliases}, method="POST")

    info = proxy_api(f"/key/info?key={key['token']}")
    d = info.get("info") or info.get("key") or {}
    got_m, got_a = list(d.get("models") or []), dict(d.get("aliases") or {})

    lost_models = [m for m in key["models"] if m not in got_m]
    lost_aliases = [k for k in key["aliases"] if k not in got_a]
    changed_others = {k: (v, got_a.get(k)) for k, v in key["aliases"].items()
                      if k not in p["alias_changes"] and got_a.get(k) != v}
    wrong = {s: got_a.get(s) for s, (_c, dst) in p["alias_changes"].items()
             if got_a.get(s) != dst}
    if lost_models or lost_aliases or changed_others or wrong:
        raise PairError(
            "KEY READBACK FAILED -- restore from the backup file:\n"
            f"  models lost: {lost_models}\n  alias keys lost: {lost_aliases}\n"
            f"  unrelated aliases changed: {changed_others}\n  not applied: {wrong}"
        )
    print(f"KEY OK  models {len(key['models'])}->{len(got_m)}  "
          f"aliases {len(key['aliases'])}->{len(got_a)}  regressions: none")


def append_fallbacks(rows: list[dict], before: list[dict]) -> None:
    """Surgical jsonb_set on {fallbacks} only. A full param_value overwrite --
    or POST /config/update -- silently drops sibling router settings such as
    model_group_alias, and the loss only detonates on the next restart."""
    if not rows:
        print("FALLBACKS: nothing to append")
        return
    payload = json.dumps(rows).replace("'", "''")
    guard = " OR ".join(f"e ? '{list(r)[0]}'" for r in rows)
    psql(
        "UPDATE \"LiteLLM_Config\" SET param_value = jsonb_set(param_value, "
        f"'{{fallbacks}}', (param_value->'fallbacks') || '{payload}'::jsonb, true) "
        "WHERE param_name='router_settings' AND NOT EXISTS ("
        "SELECT 1 FROM jsonb_array_elements(param_value->'fallbacks') e "
        f"WHERE {guard});"
    )
    after = read_fallbacks()
    if after[:len(before)] != before:
        raise PairError(
            f"FALLBACK READBACK FAILED: the first {len(before)} entries are no longer "
            "byte-identical -- something rewrote the array. Restore from the backup."
        )
    if after[len(before):] != rows:
        raise PairError(f"FALLBACK READBACK FAILED: appended {after[len(before):]}, wanted {rows}")
    print(f"FALLBACKS OK  {len(before)} -> {len(after)}  "
          f"(prefix byte-identical, appended exactly {len(rows)})")


def remove_fallbacks(names: list[str], before: list[dict]) -> None:
    keep = [r for r in before if not (len(r) == 1 and list(r)[0] in names)]
    payload = json.dumps(keep).replace("'", "''")
    psql(
        "UPDATE \"LiteLLM_Config\" SET param_value = jsonb_set(param_value, "
        f"'{{fallbacks}}', '{payload}'::jsonb, true) "
        "WHERE param_name='router_settings';"
    )
    after = read_fallbacks()
    if after != keep:
        raise PairError(f"fallback removal readback mismatch: {len(after)} vs {len(keep)}")
    print(f"FALLBACKS REMOVED  {len(before)} -> {len(after)} for {names}")
    print("⚠️  removal needs another rollout restart to take effect")


# ---------------------------------------------------------------- probe


PROBE_PY = r"""
import json, os, base64, time, urllib.request, urllib.error
p = json.loads(base64.b64decode(os.environ['REQ_B64']).decode())
MK = os.environ['LITELLM_MASTER_KEY']
BASE = 'http://127.0.0.1:4000'

def call(path, body=None, method='GET', key=MK, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        x = urllib.request.urlopen(r, timeout=timeout)
        return x.status, json.load(x), dict(x.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors='replace')[:400], dict(e.headers)

out = {'rows': []}
# Clone the key's SHAPE into a throwaway key. We cannot bearer the real key:
# LiteLLM_VerificationToken.token is a sha256 hash, and using it 401s on EVERY
# model name -- including a nonexistent one -- so the negative control cannot
# discriminate and the whole reading is void.
st, gen, _ = call('/key/generate', {
    'models': p['models'], 'aliases': p['aliases'],
    'duration': '20m', 'key_alias': p['probe_alias'],
    'metadata': {'purpose': 'alias-fallback-pair probe, auto-deleted'},
}, 'POST')
if st != 200:
    print('__R__' + json.dumps({'fatal': f'key/generate -> {st}: {gen}'}))
    raise SystemExit(0)
pk = gen['key']
out['probe_key_alias'] = p['probe_alias']
try:
    for name in p['names'] + [p['negative']]:
        nonce = 'NONCE-%s-%d' % (name.replace('.', '')[:14], int(time.time() * 1000) % 10**7)
        t0 = time.time()
        # unique nonce per shot: LiteLLM response caching is ON, and a
        # byte-identical body never reaches the upstream -- that is how
        # "all green while the upstream counter stays 0" happens.
        st, body, hdr = call('/v1/chat/completions', {
            'model': name,
            'messages': [{'role': 'user', 'content':
                          'Reply with exactly this token and nothing else: ' + nonce}],
            'max_tokens': 512,   # not 4: a small cap truncates reasoning models
        }, 'POST', key=pk)       # to empty content, which reads as broken
        txt = ''
        if st == 200 and isinstance(body, dict):
            txt = ((body.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        out['rows'].append({
            'model': name, 'http': st, 'secs': round(time.time() - t0, 1),
            'echo': nonce in txt,
            # attempted-fallbacks=0 on a 200 is CORRECT: the main path answered.
            # It is only a red flag paired with a 5xx.
            'fb_hdr': hdr.get('x-litellm-attempted-fallbacks'),
            'negative': name == p['negative'],
            'err': body if st != 200 else None,
        })
finally:
    dst, dbody, _ = call('/key/delete', {'keys': [pk]}, 'POST')
    out['probe_key_deleted_http'] = dst
print('__R__' + json.dumps(out))
"""


def probe(key: dict[str, Any], names: list[str]) -> list[dict[str, Any]]:
    """Real inference through a clone of the key's shape, then SpendLogs.

    Judges, in order of authority:
      1. HTTP 200 **and the unique nonce echoed back** (caching is on).
      2. A nonexistent model name returning 403 -- without this negative
         control the greens above prove nothing.
      3. The SpendLogs row's `model_id` = where it actually landed. The
         response header `x-litellm-model-id` is an internal hash on 198
         (e.g. e50faa178dff), NOT the declared id, so it cannot judge.
    """
    negative = "no-such-model-zzz-" + dt.datetime.now(dt.timezone.utc).strftime("%H%M%S")
    req = json.dumps({
        "models": key["models"], "aliases": key["aliases"],
        "names": names, "negative": negative,
        "probe_alias": f"probe-pair-{int(time.time())}",
    })
    req_b64 = base64.b64encode(req.encode()).decode()
    run_b64 = base64.b64encode(PROBE_PY.encode()).decode()
    script = (
        f"LP=$(sudo kubectl -n {NS} get pods -l {DEPLOY_LABEL} "
        "-o jsonpath='{.items[0].metadata.name}' | awk '{print $1}')\n"
        f"sudo kubectl -n {NS} exec -i $LP -c litellm -- env REQ_B64={req_b64} "
        f"sh -c 'echo {run_b64} | base64 -d | python3 -' 2>/dev/null\n"
    )
    res = _marker(jms_bash(script), "probe")
    if res.get("fatal"):
        raise PairError(res["fatal"])

    ok = True
    for r in res["rows"]:
        tag = "NEG" if r["negative"] else "   "
        print(f"{tag} {r['model']:<34} HTTP={r['http']} {r['secs']}s "
              f"ECHO={r['echo']} fb_hdr={r['fb_hdr']}"
              + (f"  err={str(r['err'])[:160]}" if r["err"] else ""))
        if r["negative"]:
            if r["http"] != 403:
                ok = False
                print("    ✗ negative control did not 403 -- the greens above are "
                      "NOT trustworthy (this is exactly the hashed-token trap)")
        elif not (r["http"] == 200 and r["echo"]):
            ok = False
    print(f"PROBE KEY DELETED http={res.get('probe_key_deleted_http')}")
    if res.get("probe_key_deleted_http") != 200:
        print("⚠️  throwaway key may still exist -- delete it by hand")

    print("\n-- SpendLogs (model_group=request name, model=landing, model_id=deployment) --")
    print(spendlogs(names))
    if not ok:
        raise PairError("PROBE FAILED -- see rows above")
    print("PROBE OK")
    return res["rows"]


def spendlogs(names: list[str], minutes: int = 15) -> str:
    """The landing judge. Also proves the callback layer was not swallowed:
    completion_tokens must be non-zero."""
    want = ",".join("'" + n.replace("'", "''") + "'" for n in names)
    return psql(
        "SELECT to_char(\"startTime\",'HH24:MI:SS'), model_group, model, model_id, "
        "prompt_tokens, completion_tokens "
        "FROM \"LiteLLM_SpendLogs\" "
        f"WHERE model_group IN ({want}) "
        f"AND \"startTime\" > NOW() - INTERVAL '{minutes} minutes' "
        "ORDER BY \"startTime\" DESC LIMIT 20;"
    )


# ---------------------------------------------------------------- restart


def print_restart(do_it: bool) -> None:
    cmd = f"sudo kubectl -n {NS} rollout restart deployment/litellm-proxy"
    if not do_it:
        print("\nRESTART GATE -- fallbacks are written but NOT loaded.")
        print("  The per-key alias is live already; only failover is staged.")
        print("  Restarting the 4-replica production proxy is the user's call:")
        print(f"    {cmd}")
        return
    print(f"\nrestarting: {cmd}")
    print(jms_bash(
        cmd + "\n"
        f"sudo kubectl -n {NS} rollout status deployment/litellm-proxy --timeout=600s || true\n"
    ))
    print("⚠️  `rollout status` timing out is NOT failure here -- judge by "
          "ready>=1 throughout, then re-probe.")


# ---------------------------------------------------------------- main


def parse_map(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"--map wants REQUEST_NAME=TARGET_GROUP, got {p!r}")
        k, v = p.split("=", 1)
        if not v:
            raise SystemExit(f"--map {p!r}: empty target. This script only sets "
                             "aliases; to REMOVE one use litellm-aliyun-key-repoint.py.")
        out[k.strip()] = v.strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="repoint one 198 key's aliases AND give both pre/post-rewrite "
                    "names a global fallback row",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", required=True, help="key_alias, e.g. carher-1")
    ap.add_argument("--map", action="append", default=[], metavar="NAME=GROUP",
                    help="request name -> target model group (repeatable)")
    ap.add_argument("--fallback-to", action="append", default=[], metavar="GROUP",
                    help="fallback target group (repeatable, ordered)")
    ap.add_argument("--apply", action="store_true", help="write (default: preview only)")
    ap.add_argument("--probe", action="store_true", help="clone-key real-inference probe")
    ap.add_argument("--restart", action="store_true",
                    help="actually roll the proxy so fallbacks load (needs a go-ahead)")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--from", dest="from_file", help="backup json for --rollback")
    a = ap.parse_args()

    if a.rollback:
        if not a.from_file:
            raise SystemExit("--rollback needs --from <backup json>")
        bak = json.loads(pathlib.Path(a.from_file).read_text())
        key = find_key(bak["key_alias"])
        print(f"ROLLBACK from {a.from_file} (taken {bak['utc']})")
        if not a.apply:
            print("  would restore aliases:", json.dumps(bak["aliases_before"], ensure_ascii=False))
            print("  would remove fallback rows:",
                  [list(r)[0] for r in bak["fallbacks_appended"]])
            print("  (dry run -- add --apply)")
            return 0
        proxy_api("/key/update", {"key": key["token"],
                                  "models": bak["models_before"],
                                  "aliases": bak["aliases_before"]}, method="POST")
        print("KEY restored")
        if bak["fallbacks_appended"]:
            remove_fallbacks([list(r)[0] for r in bak["fallbacks_appended"]],
                             read_fallbacks())
        print_restart(a.restart)
        return 0

    if not a.map:
        raise SystemExit("--map is required")
    mapping = parse_map(a.map)
    fb_target = a.fallback_to

    key = find_key(a.key)

    if a.probe and not a.apply:
        # probe the key AS IT IS RIGHT NOW
        probe(key, list(mapping))
        return 0

    p = plan(key, mapping, fb_target)
    truth = group_truth(list(dict.fromkeys(list(mapping) + list(mapping.values()) + fb_target)))
    print_plan(key, p, truth, fb_target)

    # alias target must be a real group IN THIS CLUSTER. An upstream's own name,
    # or a group that only exists on another cluster, sails past the allowlist
    # gate and then 400s on every call.
    missing = [g for g in list(mapping.values()) + fb_target if not truth.get(g)]
    if missing:
        raise SystemExit(f"\nABORT: alias/fallback targets with no deployment here: {missing}")

    if not a.apply:
        print("\n(dry run -- add --apply)")
        return 0

    before_fb = read_fallbacks()
    bak = snapshot(key, before_fb, p["fb_add"], mapping)
    print(f"\nBACKUP {bak}")
    write_key(key, p)
    append_fallbacks(p["fb_add"], before_fb)
    print_restart(a.restart)

    if a.probe:
        # A key write takes ~2 min to be seen by the OTHER proxy pod. A 403
        # right after writing is propagation lag, not a failed write.
        print("\nwaiting 120s for the key write to reach every proxy pod…")
        time.sleep(120)
        key = find_key(a.key)
        probe(key, list(mapping))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PairError as e:
        print(f"\nFATAL: {e}", file=sys.stderr)
        sys.exit(2)
