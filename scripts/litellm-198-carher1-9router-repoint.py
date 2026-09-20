#!/usr/bin/env python3
"""Repoint ONE key's (carher-1) fable-5.1 + opus-5 aliases onto the 9router
Cursor provider on 198 (ns litellm-product). Additive registration + per-key
alias swap. Nothing else on the fleet is touched.

WHY (2026-09-17): carher-1 reached both models through the copilot2api groups
`claude-fable-5.1` / `claude-opus-5` (legs on copilot2api-4/-5 :7777). Those
copilot accounts are exhausted. 9router now serves the same two models from a
Cursor account that passed the full 4-scenario tool-call regression
(A text / B single tool / C tool result fed back / D multi-tool choice).

WHY NOT just repoint the groups: those two group names are NOT carher-1's
alone -- fleet-wide 15 keys alias onto `claude-fable-5.1` and 18 onto
`claude-opus-5`. Editing the group would drag all of them along. So we add two
NEW deployments and move only carher-1's two alias entries.

WHY NOT edit LiteLLM_ProxyModelTable directly: a direct write bumps updated_at
on an EXISTING row and the proxy hot-re-registers that deployment, which on
2026-08-30 rerouted a live lane into a different pod entrypoint and caused 11
consecutive InternalServerErrors. /model/new only INSERTS new rows; no existing
row's updated_at moves. See feedback_proxymodeltable_write_reroutes_deployment_needs_restart_regress.

ISOLATION GUARANTEES:
  * /pro/model/new for 2 NEW ids only (9router/claude-fable-5-1-medium,
    9router/claude-opus-5-medium); refuses to overwrite if an id already exists
    with a different body.
  * Touches exactly ONE key (carher-1). aliases/models are whole-field replace
    in LiteLLM, so both are read-merge-written and asserted for zero deletions.
  * Never edits the copilot2api groups, other keys, pods, or ConfigMaps.

FOOTPRINT / BACKUP / ROLLBACK:
  * Writes a pre-change snapshot of the key (models + aliases) to
    backup/carher1-9router-baseline-<utc>.json before applying.
  * Rollback (alias only, restores copilot2api routing):
      python3 scripts/litellm-198-carher1-9router-repoint.py --rollback --apply
    which restores the two alias values from that baseline file.
  * The two new deployments are additive and harmless if left in place. To also
    remove them: DELETE model ids 9router/claude-fable-5-1-medium and
    9router/claude-opus-5-medium via /model/delete.

Run:  --dry-run (default) prints the diff; --apply performs the writes.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import pathlib
import subprocess
import sys
from typing import Any

NS = "litellm-product"
LITELLM_DEPLOY = "litellm-proxy"
JMS_HOST = "AIYJY-litellm"
TARGET_ALIAS = "carher-1"

# 9router is a ClusterIP Service in the same namespace; litellm-proxy reaches it
# directly. Shape copied verbatim from the working cursor-fc-composer-2.5 entry
# (registered 2026-09-12): api_base must end in /v1 so LiteLLM appends
# /chat/completions.
ROUTER9_BASE = f"http://router9.{NS}.svc.cluster.local:20128/v1"

# public group name -> 9router upstream model id.
# medium tier chosen by the user; it is also the exact tier that passed the
# 4-scenario tool-call regression on 2026-09-17. Other tiers are untested.
NEW_MODELS = {
    "cursor-fc-fable-5.1": "cu/claude-fable-5-1-medium",
    "cursor-fc-opus-5": "cu/claude-opus-5-medium",
}

# the key's request name -> the new group it should resolve to.
ALIAS_SWAP = {
    "claude-fable-5.1": "cursor-fc-fable-5.1",
    "claude-opus-5": "cursor-fc-opus-5",
}

# what those aliases must currently point at; refuse to act on anything else so
# a second run after a partial change cannot silently rewrite the wrong value.
ALIAS_EXPECT_BEFORE = {
    "claude-fable-5.1": "claude-fable-5.1",
    "claude-opus-5": "claude-opus-5",
}

BACKUP_DIR = pathlib.Path(__file__).resolve().parent.parent / "backup"


class RepointError(RuntimeError):
    pass


def run(args: list[str], input_text: str | None = None) -> str:
    """Run a command. stderr is NOT treated as failure: the 198 login shell
    prints a sitecustomize import warning on every hop, which an earlier
    version of this helper read as a hard error."""
    p = subprocess.run(args, input=input_text, capture_output=True, text=True)
    if p.returncode and not p.stdout.strip():
        raise RepointError(
            f"command failed: {' '.join(args[:4])}: {(p.stderr or p.stdout).strip()[:600]}"
        )
    return p.stdout


def jms_bash(script: str) -> str:
    """Run bash on the 198 box over jms stdin (no --tty; the relay rejects pty
    and `exit` teardown eats trailing sentinels)."""
    return run(["jms", "ssh", JMS_HOST, "bash -s"], input_text=script)


def proxy_api(path: str, body: Any = None, method: str = "GET",
              reduce_py: str | None = None) -> Any:
    """Call the LiteLLM admin API from inside the litellm-proxy pod, using the
    pod's own LITELLM_MASTER_KEY (never printed, never passed on a cmdline).

    Request spec and runner are BOTH base64 so nothing is mangled across the
    jms -> bash -> kubectl exec -> python hops.

    `reduce_py` is python evaluated IN THE POD against the parsed response bound
    to `R`; only its result comes back. /model/info is ~10MB on 198 (1593
    deployments) and gets truncated mid-string on the way out, so anything that
    reads it MUST reduce pod-side rather than haul the whole document across.
    """
    runner = (
        "import json,os,base64,urllib.request,urllib.error\n"
        "p=json.loads(base64.b64decode(os.environ['REQ_B64']).decode())\n"
        "data=json.dumps(p['body']).encode() if p['body'] is not None else None\n"
        "r=urllib.request.Request('http://127.0.0.1:4000'+p['path'],data=data,"
        "headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],"
        "'Content-Type':'application/json'},method=p['method'])\n"
        "try:\n"
        "    x=urllib.request.urlopen(r,timeout=115)\n"
        "    R=json.load(x)\n"
        "    red=p.get('reduce')\n"
        "    out=eval(red,{'json':json},{'R':R}) if red else R\n"
        "    print('__R__'+json.dumps({'status':x.status,'body':out}))\n"
        "except urllib.error.HTTPError as e:\n"
        "    print('__R__'+json.dumps({'status':e.code,"
        "'error':e.read().decode(errors='replace')[:600]}))\n"
    )
    req_b64 = base64.b64encode(
        json.dumps({"path": path, "body": body, "method": method,
                    "reduce": reduce_py}).encode()
    ).decode()
    run_b64 = base64.b64encode(runner.encode()).decode()
    # unique filename: a shared /tmp name collided with a stale root-owned file
    # from another round and silently executed the wrong script.
    script = (
        f"LP=$(sudo kubectl -n {NS} get pods -l app={LITELLM_DEPLOY} "
        "-o jsonpath='{.items[0].metadata.name}' | awk '{print $1}')\n"
        f"sudo kubectl -n {NS} exec -i $LP -c litellm -- env REQ_B64={req_b64} "
        f"sh -c 'echo {run_b64} | base64 -d | python3 -' 2>/dev/null\n"
    )
    out = jms_bash(script)
    line = next((l for l in out.splitlines() if l.startswith("__R__")), None)
    if not line:
        raise RepointError(f"no API response marker; raw:\n{out[:800]}")
    result = json.loads(line[len("__R__"):])
    if result.get("status") not in range(200, 300):
        raise RepointError(
            f"LiteLLM {method} {path} -> HTTP {result.get('status')}: {result.get('error')}"
        )
    return result["body"]


_BRIDGE_KEY: str | None = None


def bridge_api_key() -> str:
    """Read 9router's own API key from 9router's SQLite, inside the 9router pod.

    WHY this is not a literal: 9router authenticates the bridge with a key from
    its `apiKeys` table, and LiteLLM's /model/info NEVER returns api_key -- so
    the credential cannot be cloned off the working cursor-fc-composer-2.5
    deployment the way the rest of the shape was. On 2026-09-17 I filled it with
    the guess "router9"; both models then 401'd, LiteLLM cooled the (single-leg,
    no-fallback) groups, and carher-1 saw intermittent 500s for ~20 minutes.
    Read it, never guess it.

    The value crosses only pod -> jms stdout -> this process; it is never
    printed, never put on a cmdline, and never written to the backup file.
    """
    global _BRIDGE_KEY
    if _BRIDGE_KEY:
        return _BRIDGE_KEY
    reader = (
        "const D=require('/app/node_modules/better-sqlite3');\n"
        "const db=new D('/app/data/db/data.sqlite',{readonly:true});\n"
        "const r=db.prepare(\"select key from apiKeys where isActive=1 \"+\n"
        "  \"and name='litellm-bridge'\").get();\n"
        "if(!r){process.stderr.write('no active litellm-bridge key\\n');"
        "process.exit(1);}\n"
        "process.stdout.write('__K__'+r.key);\n"
    )
    b64 = base64.b64encode(reader.encode()).decode()
    script = (
        f"RP=$(sudo kubectl -n {NS} get pods -l app=9router "
        "-o jsonpath='{.items[0].metadata.name}' | awk '{print $1}')\n"
        f"echo {b64} | base64 -d > /tmp/9r_rdkey_$$.js\n"
        f"sudo kubectl -n {NS} cp /tmp/9r_rdkey_$$.js "
        f"{NS}/$RP:/tmp/9r_rdkey_$$.js 2>/dev/null\n"
        f"sudo kubectl -n {NS} exec -i $RP -- node /tmp/9r_rdkey_$$.js "
        "2>/dev/null < /dev/null\n"
        "rm -f /tmp/9r_rdkey_$$.js\n"
    )
    out = jms_bash(script)
    marker = out.find("__K__")
    if marker < 0:
        raise RepointError(
            "could not read 9router's litellm-bridge api key from the 9router "
            f"pod; raw output:\n{out[:400]}"
        )
    key = out[marker + len("__K__"):].strip()
    if len(key) < 20:
        raise RepointError(f"bridge key looks wrong (len={len(key)})")
    print(f"[auth] read 9router litellm-bridge key: len={len(key)} "
          f"prefix={key[:5]}***")
    _BRIDGE_KEY = key
    return key


def entries(api_key: str = "__UNSET__") -> list[dict[str, Any]]:
    """`api_key` defaults to a placeholder so read-only callers (id lists, shape
    comparison) never need the credential -- /model/info omits api_key anyway,
    so it is excluded from every comparison. Only the actual POST passes the
    real value."""
    out = []
    for group, upstream in NEW_MODELS.items():
        out.append({
            "model_name": group,
            "litellm_params": {
                "model": f"openai/{upstream}",
                "api_base": ROUTER9_BASE,
                "api_key": api_key,
                # cost 0 mirrors the existing cursor-fc-* entries: this is a
                # subscription seat, not metered tokens.
                "input_cost_per_token": 0,
                "output_cost_per_token": 0,
            },
            "model_info": {"id": f"9router/{upstream.split('/')[-1]}"},
        })
    return out


def register_models(dry: bool) -> None:
    wanted_ids = [e["model_info"]["id"] for e in entries()]
    # reduce pod-side: only the rows whose id we are about to claim. Also carry
    # the model_name set so a name collision on a DIFFERENT id is still caught.
    reduce_py = (
        "{'rows':[{'model_name':x.get('model_name'),"
        "'litellm_params':{k:v for k,v in (x.get('litellm_params') or {}).items() "
        "if k in ('model','api_base')},"
        "'id':str((x.get('model_info') or {}).get('id'))} "
        "for x in R['data'] "
        "if str((x.get('model_info') or {}).get('id')) in %r "
        "or x.get('model_name') in %r]}" % (wanted_ids, list(NEW_MODELS))
    )
    rows = proxy_api("/v1/model/info", reduce_py=reduce_py).get("rows") or []
    by_id = {r["id"]: r for r in rows}
    for entry in entries():
        mid = entry["model_info"]["id"]
        # A name already used by some OTHER deployment id would make the group
        # a two-legged pool with a stranger in it -- refuse rather than pool.
        squatter = [r for r in rows
                    if r["model_name"] == entry["model_name"] and r["id"] != mid]
        if squatter:
            raise RepointError(
                f"model_name {entry['model_name']} is already taken by id "
                f"{squatter[0]['id']} -- registering would silently create a "
                "two-leg pool. Pick another name."
            )
        old = by_id.get(mid)
        if old:
            lp = old.get("litellm_params") or {}
            # api_key never comes back from /model/info, so it is not comparable
            same = (
                old.get("model_name") == entry["model_name"]
                and all(lp.get(k) == v
                        for k, v in entry["litellm_params"].items()
                        if k != "api_key")
            )
            if not same:
                raise RepointError(
                    f"existing model id {mid} differs from desired; refusing overwrite.\n"
                    f"  live: name={old.get('model_name')} model={lp.get('model')} base={lp.get('api_base')}\n"
                    f"  want: name={entry['model_name']} model={entry['litellm_params']['model']} "
                    f"base={entry['litellm_params']['api_base']}"
                )
            print(f"[models] {entry['model_name']} already registered (id {mid}) -- skip")
        else:
            print(f"[models] register {entry['model_name']} -> "
                  f"{entry['litellm_params']['model']} @ {ROUTER9_BASE} (id {mid})")
            if not dry:
                # fetched lazily and only on the write path, so --dry-run never
                # touches the credential at all
                entry["litellm_params"]["api_key"] = bridge_api_key()
                proxy_api("/pro/model/new", entry, "POST")


def find_key() -> dict[str, Any]:
    """Paginate defensively -- the alias is not guaranteed to be on page 1."""
    # `n` is the page's TOTAL row count, kept separate from the filtered hits:
    # after reducing pod-side an empty list means "not on this page", which is
    # indistinguishable from "no more pages" unless the raw count comes along.
    reduce_py = (
        "{'n':len(R.get('keys') or []),"
        "'keys':[{'key_alias':k.get('key_alias'),'token':k.get('token'),"
        "'blocked':k.get('blocked'),'models':k.get('models'),"
        "'aliases':k.get('aliases')} "
        "for k in (R.get('keys') or []) if k.get('key_alias')==%r]}" % TARGET_ALIAS
    )
    for page in range(1, 60):
        resp = proxy_api(
            f"/key/list?page={page}&size=100&return_full_object=true",
            reduce_py=reduce_py,
        )
        if not resp.get("n"):
            break
        m = resp.get("keys") or []
        if m:
            if len(m) != 1:
                raise RepointError(
                    f"expected exactly one {TARGET_ALIAS}, got {len(m)} on page {page}"
                )
            return m[0]
    raise RepointError(f"key alias {TARGET_ALIAS} not found in 60 pages")


def snapshot(key: dict[str, Any], tag: str) -> pathlib.Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = BACKUP_DIR / f"carher1-9router-{tag}-{stamp}.json"
    p.write_text(json.dumps({
        "key_alias": key.get("key_alias"),
        "blocked": key.get("blocked"),
        "models": list(key.get("models") or []),
        "aliases": dict(key.get("aliases") or {}),
    }, ensure_ascii=False, indent=1))
    return p


def swap_aliases(dry: bool, rollback_from: dict[str, str] | None = None) -> None:
    key = find_key()
    before_aliases = dict(key.get("aliases") or {})
    before_models = list(key.get("models") or [])

    if rollback_from is None:
        target = ALIAS_SWAP
        # Refuse to act unless the aliases are where we think they are. A second
        # run over an already-swapped key, or a key someone else moved in the
        # meantime, must stop rather than rewrite blind.
        for name, expect in ALIAS_EXPECT_BEFORE.items():
            live = before_aliases.get(name)
            if live == ALIAS_SWAP[name]:
                print(f"[alias] {name} already -> {live} (nothing to do)")
            elif live != expect:
                raise RepointError(
                    f"alias {name} is {live!r}, expected {expect!r} -- refusing to "
                    "rewrite. Someone else changed this key; re-read before acting."
                )
    else:
        target = rollback_from

    changes = {k: v for k, v in target.items() if before_aliases.get(k) != v}
    if not changes:
        print("[alias] nothing to change")
        return

    # aliases is a WHOLE-FIELD REPLACE in LiteLLM, not a merge: sending only the
    # two changed entries would delete the other 12. Read-merge-write.
    after_aliases = {**before_aliases, **changes}
    for k, v in changes.items():
        print(f"[alias] {k}: {before_aliases.get(k)!r} -> {v!r}")
    print(f"[alias] key {TARGET_ALIAS}: {len(before_aliases)} entries -> "
          f"{len(after_aliases)} (merged, {len(changes)} rewritten)")

    # Alias targets do not need to be in `models`, but say so explicitly rather
    # than leave the reader guessing why the allowlist is untouched.
    for v in changes.values():
        print(f"[alias] note: target {v} is not added to the allowlist "
              f"({len(before_models)} entries unchanged) -- alias targets are "
              "resolved after the allowlist check")

    if dry:
        return

    snap = snapshot(key, "rollback" if rollback_from else "baseline")
    print(f"[backup] pre-change snapshot: {snap}")

    token = key.get("token")
    if not token:
        raise RepointError("target key has no update token")
    proxy_api("/key/update", {"key": token, "aliases": after_aliases}, "POST")

    verified = find_key()
    v_aliases = dict(verified.get("aliases") or {})
    v_models = list(verified.get("models") or [])

    bad = {k: (v, v_aliases.get(k)) for k, v in after_aliases.items()
           if v_aliases.get(k) != v}
    if bad:
        raise RepointError(f"alias verification failed (want, got): {bad}")
    dropped_aliases = [k for k in before_aliases if k not in v_aliases]
    dropped_models = [m for m in before_models if m not in v_models]
    if dropped_aliases or dropped_models:
        raise RepointError(
            f"REGRESSION: dropped aliases={dropped_aliases} models={dropped_models}"
        )
    if verified.get("blocked") != key.get("blocked"):
        raise RepointError(
            f"blocked flipped {key.get('blocked')} -> {verified.get('blocked')}"
        )
    print(f"[alias] verified: {len(v_aliases)} aliases, {len(v_models)} models, "
          f"zero deletions, blocked unchanged ({verified.get('blocked')})")


def fleet_alias_census(label: str) -> dict[str, int]:
    """Count how many keys fleet-wide alias onto each group we care about.
    Proves only one key moved -- a per-key write that hit the wrong row would
    show up here as a count change on a group we never meant to touch."""
    watch = sorted(set(ALIAS_EXPECT_BEFORE.values()) | set(ALIAS_SWAP.values()))
    tally = {g: 0 for g in watch}
    # count pod-side; only the tally crosses the wire
    reduce_py = (
        "{'n':len(R.get('keys') or []),"
        "'t':{g:sum(1 for k in (R.get('keys') or []) "
        "if g in ((k.get('aliases') or {}).values())) for g in %r}}" % watch
    )
    for page in range(1, 60):
        resp = proxy_api(
            f"/key/list?page={page}&size=100&return_full_object=true",
            reduce_py=reduce_py,
        )
        if not resp.get("n"):
            break
        for g, n in (resp.get("t") or {}).items():
            tally[g] += n
    print(f"[census {label}] " + "  ".join(f"{g}={n}" for g, n in tally.items()))
    return tally


def load_rollback(path: str | None) -> dict[str, str]:
    if path:
        p = pathlib.Path(path)
    else:
        cands = sorted(BACKUP_DIR.glob("carher1-9router-baseline-*.json"))
        if not cands:
            raise RepointError(
                f"no baseline snapshot in {BACKUP_DIR}; pass --from <file>"
            )
        p = cands[-1]
    data = json.loads(p.read_text())
    aliases = data.get("aliases") or {}
    out = {k: aliases[k] for k in ALIAS_SWAP if k in aliases}
    if len(out) != len(ALIAS_SWAP):
        raise RepointError(
            f"baseline {p} does not carry both aliases; has {sorted(out)}"
        )
    print(f"[rollback] restoring from {p}: {out}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="perform writes (default: dry-run)")
    ap.add_argument("--rollback", action="store_true",
                    help="restore the two aliases from the newest baseline snapshot")
    ap.add_argument("--from", dest="from_file", default=None,
                    help="explicit baseline json for --rollback")
    ap.add_argument("--census", action="store_true",
                    help="only print the fleet-wide alias census and exit")
    args = ap.parse_args()

    dry = not args.apply
    mode = "DRY-RUN" if dry else "APPLY"

    if args.census:
        fleet_alias_census("now")
        return 0

    print(f"=== carher-1 -> 9router repoint [{mode}] ns={NS} key={TARGET_ALIAS} "
          f"rollback={args.rollback} ===")

    if args.rollback:
        before = fleet_alias_census("before")
        swap_aliases(dry, rollback_from=load_rollback(args.from_file))
        if not dry:
            fleet_alias_census("after")
        print("[done]" if args.apply else "[dry-run] no changes applied")
        return 0

    before = fleet_alias_census("before")
    register_models(dry)
    swap_aliases(dry)
    if not dry:
        after = fleet_alias_census("after")
        # every group we watch should move by exactly the one key, or not at all
        for g in before:
            d = after[g] - before[g]
            expected = 0
            if g in ALIAS_EXPECT_BEFORE.values():
                expected = -1
            elif g in ALIAS_SWAP.values():
                expected = +1
            if d != expected:
                raise RepointError(
                    f"census mismatch on {g}: {before[g]} -> {after[g]} "
                    f"(delta {d:+d}, expected {expected:+d}) -- more than one key moved"
                )
        print("[census] delta is exactly one key on each watched group")
    print("[done]" if args.apply else "[dry-run] no changes applied")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RepointError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
