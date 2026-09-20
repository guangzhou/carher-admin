#!/usr/bin/env python3
"""Repoint ONE key's (carher-1) `claude-fable-5.1` request name onto the Wangsu
opus-5 leg on 198 (ns litellm-product). Per-key alias only.

WHAT CHANGES
  carher-1 aliases['claude-fable-5.1']:
      'cursor-fc-fable-5.1'  (9router / Cursor seat, cost 0)
   -> 'anthropic.wangsu5.claude-opus-5'
      (id wangsu-direct5/claude-opus-5, api_base
       https://aigateway.edgecloudapp.com/v2/gws/sqix2pnh/anthropic, metered)

Exactly one alias entry on exactly one key. No model registration, no
ConfigMap, no rollout, no other key, no HerInstance / her config.

WHY THIS TARGET AND NOT A NAME THAT LOOKS RIGHT
  Group names on 198 lie. Measured 2026-09-16 via /v1/model/info:
    anthropic.wangsu5.claude-opus-5 -> wangsu-direct5/claude-opus-5 @ sqix2pnh  (Wangsu)
    claude-opus-5                   -> copilot2api-4/-5 :7777                   (copilot, exhausted)
    openrouter-claude-opus-5        -> openrouter                               (not Wangsu)
    kiro-claude-opus-5              -> kiro-rs 10.43.109.5:8990                 (not Wangsu)
  Judged by api_base + model + model_info.id, never by the group name.

LEG HEALTH (measured 2026-09-16, before writing anything)
  * Live probe through a throwaway key: HTTP 200, unique nonce echoed back,
    and SpendLogs confirms model_id=wangsu-direct5/claude-opus-5 with
    api_base .../gws/sqix2pnh/anthropic/v1/messages for that exact request.
  * The response header x-litellm-model-id returns an internal hash
    (e50faa178dff), NOT the declared id -- so the landing judge here is the
    SpendLogs row, not the header.
  * router_settings in LiteLLM_Config has ZERO fallback entries mentioning
    opus-5: this is a SINGLE-LEG group with NO fallback. If Wangsu 4xx/5xx's,
    carher-1's fable-5.1 requests fail rather than falling back.
  * Cost note: the previous target (9router Cursor seat) was registered at
    cost 0; Wangsu is metered (0.89 USD over 30 calls on 09-14).

FOOTPRINT / BACKUP / ROLLBACK
  * Pre-change snapshot (models + aliases) ->
    backup/carher1-fable51-wangsu-baseline-<utc>.json
  * Rollback restores the alias from that snapshot:
      python3 scripts/litellm-198-carher1-fable51-to-wangsu-opus5.py --rollback --apply
  * Nothing else to undo; no deployment was created or modified.

Run:  (default) dry-run diff | --probe live judge | --apply write | --rollback
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import importlib.util
import json
import pathlib
import secrets
import sys
from typing import Any

_HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "repoint", _HERE / "litellm-198-carher1-9router-repoint.py")
_repoint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_repoint)

_raw_jms_bash = _repoint.jms_bash
_raw_proxy_api = _repoint.proxy_api
find_key = _repoint.find_key


def _is_relay_misroute(err: str) -> bool:
    """The jms relay intermittently resolves the asset name to 10.68.13.189 --
    a host that never participates in this flow -- and returns
    `Permission denied (password,publickey)`. That reads like expired
    credentials but is the name->IP mapping landing on the wrong box, and a
    plain retry lands on the right one. Only THIS shape is retried; a real
    auth failure or a LiteLLM error is not."""
    return "10.68.13.189" in err and "Permission denied" in err


def _retry(fn, *a, **kw):
    last = None
    for attempt in range(1, 6):
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if not _is_relay_misroute(msg):
                raise
            last = e
            print(f"[jms] relay misrouted to 10.68.13.189 (attempt {attempt}/5), "
                  "retrying")
    raise last  # type: ignore[misc]


def jms_bash(script: str) -> str:
    return _retry(_raw_jms_bash, script)


def proxy_api(*a, **kw):
    return _retry(_raw_proxy_api, *a, **kw)


# the imported helpers close over the module's own jms_bash, so patch it there
# too or find_key()/proxy_api() inside repoint keep using the unretried version
_repoint.jms_bash = jms_bash
_repoint.proxy_api = proxy_api
RepointError = _repoint.RepointError
NS = _repoint.NS
LITELLM_DEPLOY = _repoint.LITELLM_DEPLOY

TARGET_ALIAS = "carher-1"
REQUEST_NAME = "claude-fable-5.1"
NEW_GROUP = "anthropic.wangsu5.claude-opus-5"
NEW_DEPLOY_ID = "wangsu-direct5/claude-opus-5"
NEW_API_BASE_PART = "gws/sqix2pnh/anthropic"
# Refuse to act on anything else: a second run over an already-swapped key, or a
# key another session moved meanwhile, must stop rather than rewrite blind.
EXPECT_BEFORE = "cursor-fc-fable-5.1"

BACKUP_DIR = _HERE.parent / "backup"


def psql(sql: str) -> str:
    """One SQL statement against the prod DB. The statement travels as an env
    var: passing it inline died on bash paren parsing, and a /tmp file died
    because `$$` differs between the jms shell and the exec'd sh."""
    b64 = base64.b64encode(sql.encode()).decode()
    return jms_bash(
        f"sudo kubectl -n {NS} exec -i litellm-db-0 -- env SQL_B64={b64} "
        "sh -c 'echo \"$SQL_B64\" | base64 -d | psql -U litellm -d litellm -f -' "
        "2>&1 < /dev/null\n"
    )


def assert_target_is_wangsu() -> None:
    """The group name is not evidence. Re-verify the upstream every run."""
    reduce_py = (
        "[{'model':(x.get('litellm_params') or {}).get('model'),"
        "'api_base':(x.get('litellm_params') or {}).get('api_base'),"
        "'id':str((x.get('model_info') or {}).get('id'))} "
        "for x in R['data'] if x.get('model_name')==%r]" % NEW_GROUP
    )
    rows = proxy_api("/v1/model/info", reduce_py=reduce_py)
    if len(rows) != 1:
        raise RepointError(
            f"{NEW_GROUP} has {len(rows)} deployments, expected 1 -- a pooled "
            "group means the landing judge below is not decisive")
    r = rows[0]
    if r["id"] != NEW_DEPLOY_ID or NEW_API_BASE_PART not in (r["api_base"] or ""):
        raise RepointError(
            f"{NEW_GROUP} no longer points at Wangsu: id={r['id']} "
            f"base={r['api_base']}")
    print(f"[target] {NEW_GROUP} -> {r['id']} @ {r['api_base']}  (Wangsu, 1 leg)")


def assert_no_group_alias() -> None:
    """A global model_group_alias would unconditionally beat the per-key alias,
    so the plan is only valid if there is no entry for either name."""
    out = psql(
        "SELECT param_value->'model_group_alias' FROM \"LiteLLM_Config\" "
        "WHERE param_name='router_settings';")
    blob = out.lower()
    for name in (REQUEST_NAME.lower(), NEW_GROUP.lower()):
        if name in blob:
            raise RepointError(
                f"router_settings.model_group_alias mentions {name!r}; a global "
                "alias outranks the per-key alias. Read it before writing.\n" + out)
    print("[guard] router_settings.model_group_alias does not mention either name")


def report_fallbacks() -> None:
    out = psql(
        "SELECT jsonb_pretty(jsonb_build_object("
        "'fallbacks', param_value->'fallbacks',"
        "'ctx', param_value->'context_window_fallbacks')) "
        "FROM \"LiteLLM_Config\" WHERE param_name='router_settings';")
    hits = [l.strip() for l in out.splitlines()
            if "opus-5" in l or NEW_GROUP in l]
    if hits:
        print(f"[fallback] {len(hits)} entries mention opus-5:")
        for h in hits[:10]:
            print("   ", h)
    else:
        print("[fallback] NONE mention opus-5 -- single upstream, no fallback. "
              "If Wangsu errors, this request name fails.")


def snapshot(key: dict[str, Any], tag: str) -> pathlib.Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = BACKUP_DIR / f"carher1-fable51-wangsu-{tag}-{stamp}.json"
    p.write_text(json.dumps({
        "key_alias": key.get("key_alias"),
        "blocked": key.get("blocked"),
        "models": list(key.get("models") or []),
        "aliases": dict(key.get("aliases") or {}),
    }, ensure_ascii=False, indent=1))
    return p


def probe(key: dict[str, Any], new_value: str) -> None:
    """Judgment-grade probe: a throwaway key that clones carher-1's shape (same
    `models` allowlist, same aliases WITH the swap applied), asked for
    REQUEST_NAME. This is the leg that answers a question the aliyun findings
    cannot answer for 198: the alias target is NOT in carher-1's 38-entry
    allowlist, so we measure whether 198 resolves it anyway.

    Landing judge = the SpendLogs row for this request's unique nonce-carrying
    call. The response body's `model` echoes the request name and the
    x-litellm-model-id header returns an internal hash, so neither decides.
    """
    aliases = {**(key.get("aliases") or {}), REQUEST_NAME: new_value}
    models = list(key.get("models") or [])
    tag = f"zz-probe-carher1-fable51-{secrets.token_hex(3)}"
    nonce = "fbprobe" + secrets.token_hex(4)
    made = proxy_api("/key/generate", {
        "key_alias": tag, "models": models, "aliases": aliases,
        "duration": "10m", "max_budget": 1,
    }, "POST")
    probe_key = made["key"]
    print(f"[probe] throwaway key {tag}: {len(models)} models, "
          f"{len(aliases)} aliases, {REQUEST_NAME} -> {new_value}")
    print(f"[probe] note: {new_value} is "
          f"{'IN' if new_value in models else 'NOT in'} the cloned allowlist")
    try:
        runner = (
            "import json,os,urllib.request,urllib.error\n"
            "body=json.dumps({'model':%r,'max_tokens':32,'messages':"
            "[{'role':'user','content':'Reply with exactly this token and "
            "nothing else: %s'}]}).encode()\n"
            "r=urllib.request.Request('http://127.0.0.1:4000/v1/chat/completions',"
            "data=body,headers={'Authorization':'Bearer '+os.environ['PK'],"
            "'Content-Type':'application/json'},method='POST')\n"
            "try:\n"
            "    x=urllib.request.urlopen(r,timeout=110)\n"
            "    d=json.load(x)\n"
            "    print('__P__'+json.dumps({'status':x.status,"
            "'text':(d['choices'][0]['message'].get('content') or '')[:200]}))\n"
            "except urllib.error.HTTPError as e:\n"
            "    print('__P__'+json.dumps({'status':e.code,"
            "'error':e.read().decode(errors='replace')[:500]}))\n"
        ) % (REQUEST_NAME, nonce)
        b64 = base64.b64encode(runner.encode()).decode()
        # every proxy pod: per-key config lives in the DB but each pod caches it
        pods = jms_bash(
            f"sudo kubectl -n {NS} get pods -l app={LITELLM_DEPLOY} "
            "-o jsonpath='{range .items[*]}{.metadata.name}{\"\\n\"}{end}'\n"
        ).split()
        pods = [p for p in pods if p.startswith(LITELLM_DEPLOY)]
        print(f"[probe] hitting {len(pods)} proxy pods")
        for pod in pods:
            out = jms_bash(
                f"sudo kubectl -n {NS} exec -i {pod} -c litellm -- "
                f"env PK={probe_key} sh -c 'echo {b64} | base64 -d | python3 -' "
                "2>/dev/null\n")
            line = next((l for l in out.splitlines() if l.startswith("__P__")), None)
            if not line:
                print(f"  {pod}: NO MARKER; raw {out[:300]!r}")
                continue
            res = json.loads(line[len("__P__"):])
            echoed = nonce in (res.get("text") or "")
            print(f"  {pod}: HTTP {res.get('status')} nonce_echoed={echoed}"
                  + (f" text={res.get('text')!r}" if not echoed else "")
                  + (f" err={res.get('error')[:200]}" if res.get("error") else ""))
        # SpendLogs is flushed asynchronously; querying immediately showed 2 of 4
        # calls and read like "half the requests vanished". Give the flush a
        # moment before treating a missing row as a missing request.
        jms_bash("sleep 20\n")
        # landing judge, DB side
        rows = psql(
            "SELECT \"startTime\", model_group, model_id, api_base "
            "FROM \"LiteLLM_SpendLogs\" WHERE api_key IN "
            "(SELECT token FROM \"LiteLLM_VerificationToken\" "
            f"WHERE key_alias='{tag}') ORDER BY \"startTime\" DESC LIMIT 10;")
        print("[probe] SpendLogs landing rows for this throwaway key:")
        print(rows.strip() or "  (none -- nothing was billed, so nothing landed)")
        landed_ok = rows.count(NEW_DEPLOY_ID)
        print(f"[probe] VERDICT: {landed_ok} row(s) landed on {NEW_DEPLOY_ID}")
    finally:
        proxy_api("/key/delete", {"keys": [probe_key]}, "POST")
        left = proxy_api(
            "/key/list?page=1&size=100&return_full_object=true",
            reduce_py="[k.get('key_alias') for k in (R.get('keys') or []) "
                      "if str(k.get('key_alias') or '').startswith('zz-probe')]")
        print(f"[cleanup] throwaway key deleted; remaining zz-probe*: {left}")


def fleet_census(label: str) -> dict[str, int]:
    """Prove only one key moved. A per-key write that hit the wrong row shows up
    here as a count change on a group we never meant to touch."""
    watch = sorted({EXPECT_BEFORE, NEW_GROUP})
    tally = {g: 0 for g in watch}
    reduce_py = (
        "{'n':len(R.get('keys') or []),"
        "'t':{g:sum(1 for k in (R.get('keys') or []) "
        "if g in ((k.get('aliases') or {}).values())) for g in %r}}" % watch
    )
    for page in range(1, 60):
        resp = proxy_api(
            f"/key/list?page={page}&size=100&return_full_object=true",
            reduce_py=reduce_py)
        if not resp.get("n"):
            break
        for g, n in (resp.get("t") or {}).items():
            tally[g] += n
    print(f"[census {label}] " + "  ".join(f"{g}={n}" for g, n in tally.items()))
    return tally


def swap(dry: bool, new_value: str, expect: str | None) -> None:
    key = find_key()
    before = dict(key.get("aliases") or {})
    before_models = list(key.get("models") or [])
    live = before.get(REQUEST_NAME)

    if live == new_value:
        print(f"[alias] {REQUEST_NAME} already -> {live} (nothing to do)")
        return
    if expect is not None and live != expect:
        raise RepointError(
            f"alias {REQUEST_NAME} is {live!r}, expected {expect!r} -- refusing "
            "to rewrite. Someone else changed this key; re-read before acting.")

    print(f"[alias] {REQUEST_NAME}: {live!r} -> {new_value!r}")
    # aliases is a WHOLE-FIELD REPLACE in LiteLLM, not a merge: sending only the
    # one changed entry would delete the other 13. Read-merge-write.
    after = {**before, REQUEST_NAME: new_value}
    print(f"[alias] key {TARGET_ALIAS}: {len(before)} entries -> {len(after)} "
          f"(merged, 1 rewritten); allowlist untouched ({len(before_models)})")
    if dry:
        print("[dry-run] no write performed")
        return

    snap = snapshot(key, "rollback" if expect is None else "baseline")
    print(f"[backup] pre-change snapshot: {snap}")

    token = key.get("token")
    if not token:
        raise RepointError("target key has no update token")
    proxy_api("/key/update", {"key": token, "aliases": after}, "POST")

    v = find_key()
    v_al = dict(v.get("aliases") or {})
    v_md = list(v.get("models") or [])
    bad = {k: (val, v_al.get(k)) for k, val in after.items() if v_al.get(k) != val}
    if bad:
        raise RepointError(f"alias verification failed (want, got): {bad}")
    dropped_al = [k for k in before if k not in v_al]
    dropped_md = [m for m in before_models if m not in v_md]
    if dropped_al or dropped_md:
        raise RepointError(
            f"REGRESSION: dropped aliases={dropped_al} models={dropped_md}")
    if v.get("blocked") != key.get("blocked"):
        raise RepointError(
            f"blocked flipped {key.get('blocked')} -> {v.get('blocked')}")
    print(f"[alias] verified: {len(v_al)} aliases, {len(v_md)} models, zero "
          f"deletions, blocked unchanged ({v.get('blocked')})")


def load_rollback(path: str | None) -> str:
    if path:
        p = pathlib.Path(path)
    else:
        cands = sorted(BACKUP_DIR.glob("carher1-fable51-wangsu-baseline-*.json"))
        if not cands:
            raise RepointError(f"no baseline snapshot in {BACKUP_DIR}; pass --from")
        p = cands[-1]
    data = json.loads(p.read_text())
    val = (data.get("aliases") or {}).get(REQUEST_NAME)
    if not val:
        raise RepointError(f"baseline {p} has no {REQUEST_NAME} alias")
    print(f"[rollback] restoring from {p}: {REQUEST_NAME} -> {val}")
    return val


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="perform the write")
    ap.add_argument("--probe", action="store_true",
                    help="live judgment probe with a cloned throwaway key")
    ap.add_argument("--rollback", action="store_true",
                    help="restore the alias from the newest baseline snapshot")
    ap.add_argument("--from", dest="from_file", default=None)
    ap.add_argument("--census", action="store_true")
    args = ap.parse_args()
    dry = not args.apply

    if args.census:
        fleet_census("now")
        return 0

    print(f"=== carher-1 {REQUEST_NAME} -> Wangsu opus-5 "
          f"[{'DRY-RUN' if dry else 'APPLY'}] ns={NS} rollback={args.rollback} ===")

    if args.rollback:
        val = load_rollback(args.from_file)
        fleet_census("before")
        swap(dry, val, expect=None)
        if not dry:
            fleet_census("after")
        print("[done]" if args.apply else "[dry-run] no changes applied")
        return 0

    assert_target_is_wangsu()
    assert_no_group_alias()
    report_fallbacks()

    if args.probe:
        probe(find_key(), NEW_GROUP)
        return 0

    before = fleet_census("before")
    swap(dry, NEW_GROUP, expect=EXPECT_BEFORE)
    if not dry:
        after = fleet_census("after")
        for g in before:
            d = after[g] - before[g]
            want = -1 if g == EXPECT_BEFORE else +1
            if d != want:
                raise RepointError(
                    f"census mismatch on {g}: {before[g]} -> {after[g]} "
                    f"(delta {d:+d}, expected {want:+d}) -- more than one key moved")
        print("[census] delta is exactly one key on each watched group")
    print("[done]" if args.apply else "[dry-run] no changes applied")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RepointError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
