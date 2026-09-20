#!/usr/bin/env python3
"""Register 6 Cursor native-FC models (mode:responses) and grant them to the
Cursor key — additive only, touches nothing else in 198 litellm-product.

WHY: On 198, Cursor sends /v1/responses. The existing cursor-gpt-* models are
mode:chat, so LiteLLM's built-in responses->chat bridge downgrades the request
and zerokey serves it via lossy web-injection. A parallel mode:responses model
forwards the Responses body verbatim to zerokey's /v1/responses -> handleCodex,
which is native Codex function-calling (OAuth token pool on zero-cursor-101).

ISOLATION GUARANTEES (safety):
  * Only POSTs /pro/model/new for 6 NEW ids (zerokey-cursor-fc-101-*); refuses
    to overwrite if an id already exists with a different body.
  * Only appends the 6 new model_names to ONE key (cursor-liuguoxian03),
    preserving every existing model and the blocked flag; verifies after write.
  * Never deletes/edits existing models, other keys, pods, or ConfigMaps.
  * The carrier pod zero-cursor-101 and the existing cursor-gpt-* (mode:chat)
    are NOT touched — they remain the fallback/control path.

Run:  --dry-run (default) prints the diff; --apply performs the writes.
Rollback: DELETE the 6 zerokey-cursor-fc-101-* ids and restore the key's models
from backup/cursor-fc-baseline-*.json (the pre-change model list).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

NS = "litellm-product"
LITELLM_DEPLOY = "litellm-proxy"
JMS_HOST = "AIYJY-litellm"
TARGET_ALIAS = "cursor-liuguoxian03"  # default; override with --key-alias
CARRIER_SERVICE = "zero-cursor-101"
CARRIER_PORT = 8200
ID_PREFIX = "zerokey-cursor-fc-101"

# public Cursor model name -> verified ChatGPT web slug (same slugs as the
# proven mode:chat cursor-gpt-* deployment; only the mode differs).
FC_MODELS = {
    "cursor-fc-5.6-sol": "gpt-5.6-sol",
    "cursor-fc-5.6-terra": "gpt-5.6-terra",
    "cursor-fc-5.6-luna": "gpt-5.6-luna",
    "cursor-fc-5.5": "gpt-5-5",
    "cursor-fc-5.4": "gpt-5-4-thinking",
    "cursor-fc-5.3-codex": "gpt-5-3",
}


class RegError(RuntimeError):
    pass


def run(args: list[str], input_text: str | None = None, check: bool = True) -> str:
    p = subprocess.run(args, input=input_text, capture_output=True, text=True)
    if check and p.returncode:
        raise RegError(f"command failed: {' '.join(args[:4])}: {(p.stderr or p.stdout).strip()[:600]}")
    return p.stdout


def jms_bash(script: str) -> str:
    """Run a bash script on the 198 box via jms stdin (no --tty; relay rejects pty)."""
    return run(["jms", "ssh", JMS_HOST, "bash -s"], input_text=script)


def proxy_api(path: str, body: Any = None, method: str = "GET") -> Any:
    """Call the LiteLLM admin API from inside the litellm-proxy pod.

    The request spec (path/method/body) and the runner python are BOTH embedded
    as base64 to survive the jms -> bash -> kubectl exec -> python hop without
    any quoting/newline mangling.
    """
    import base64
    runner = (
        "import json,os,sys,base64,urllib.request,urllib.error\n"
        "p=json.loads(base64.b64decode(os.environ['REQ_B64']).decode())\n"
        "data=json.dumps(p['body']).encode() if p['body'] is not None else None\n"
        "r=urllib.request.Request('http://127.0.0.1:4000'+p['path'],data=data,"
        "headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],"
        "'Content-Type':'application/json'},method=p['method'])\n"
        "try:\n"
        "    x=urllib.request.urlopen(r,timeout=115)\n"
        "    print('__R__'+json.dumps({'status':x.status,'body':json.load(x)}))\n"
        "except urllib.error.HTTPError as e:\n"
        "    print('__R__'+json.dumps({'status':e.code,'error':e.read().decode(errors='replace')[:600]}))\n"
    )
    req_b64 = base64.b64encode(json.dumps({"path": path, "body": body, "method": method}).encode()).decode()
    run_b64 = base64.b64encode(runner.encode()).decode()
    script = (
        f"LP=$(sudo k3s kubectl -n {NS} get pods -l app={LITELLM_DEPLOY} "
        "-o jsonpath='{.items[0].metadata.name}' | awk '{print $1}')\n"
        f"sudo k3s kubectl -n {NS} exec -i $LP -c litellm -- env REQ_B64={req_b64} "
        f"sh -c 'echo {run_b64} | base64 -d | python3 -'\n"
    )
    out = jms_bash(script)
    line = next((l for l in out.splitlines() if l.startswith("__R__")), None)
    if not line:
        raise RegError(f"no API response marker; raw:\n{out[:800]}")
    result = json.loads(line[len("__R__"):])
    if result.get("status") not in range(200, 300):
        raise RegError(f"LiteLLM {method} {path} -> HTTP {result.get('status')}: {result.get('error')}")
    return result["body"]


def entries() -> list[dict[str, Any]]:
    base = f"http://{CARRIER_SERVICE}.{NS}.svc.cluster.local:{CARRIER_PORT}/v1"
    out = []
    for name, slug in FC_MODELS.items():
        out.append({
            "model_name": name,
            "litellm_params": {
                "model": f"openai/{slug}",
                "api_base": base,          # must end in /v1 -> LiteLLM appends /responses
                "api_key": "cursor",
                "rpm": 8,
                "input_cost_per_token": 5e-6,
                "output_cost_per_token": 3e-5,
            },
            # mode:responses => native responses->responses passthrough (NO chat downgrade)
            "model_info": {"id": f"{ID_PREFIX}-{name}", "mode": "responses"},
        })
    return out


def register_models(dry: bool) -> None:
    current = proxy_api("/v1/model/info").get("data") or []
    by_id = {str((x.get("model_info") or {}).get("id")): x for x in current}
    for entry in entries():
        mid = entry["model_info"]["id"]
        old = by_id.get(mid)
        if old:
            lp = old.get("litellm_params") or {}
            same = (old.get("model_name") == entry["model_name"]
                    and all(lp.get(k) == v for k, v in entry["litellm_params"].items())
                    and (old.get("model_info") or {}).get("mode") == "responses")
            if not same:
                raise RegError(f"existing model id {mid} differs from desired; refusing overwrite")
            print(f"[models] {entry['model_name']} already registered (id {mid})")
        else:
            print(f"[models] register {entry['model_name']} (id {mid}, mode:responses)")
            if not dry:
                proxy_api("/pro/model/new", entry, "POST")


def find_key() -> dict[str, Any]:
    # paginate defensively: the alias is not guaranteed on page 1
    for page in range(1, 60):
        rows = proxy_api(f"/key/list?page={page}&size=100&return_full_object=true").get("keys") or []
        if not rows:
            break
        m = [x for x in rows if x.get("key_alias") == TARGET_ALIAS]
        if m:
            if len(m) != 1:
                raise RegError(f"expected exactly one {TARGET_ALIAS}, got {len(m)} on page {page}")
            return m[0]
    raise RegError(f"key alias {TARGET_ALIAS} not found in 60 pages")


def grant_key(dry: bool) -> None:
    key = find_key()
    before = list(key.get("models") or [])
    add = [n for n in FC_MODELS if n not in before]
    after = before + add
    print(f"[key] {TARGET_ALIAS}: models {len(before)} -> {len(after)} "
          f"(adding {add}); blocked preserved ({key.get('blocked')})")
    if dry or not add:
        if not add:
            print("[key] nothing to add (already granted)")
        return
    token = key.get("token")
    if not token:
        raise RegError("target key has no update token")
    proxy_api("/key/update", {"key": token, "models": after}, "POST")
    verified = find_key()
    vmodels = verified.get("models") or []
    missing = [n for n in FC_MODELS if n not in vmodels]
    if missing:
        raise RegError(f"key verification failed; missing {missing}")
    # ensure we did not drop anything or flip blocked
    dropped = [n for n in before if n not in vmodels]
    if dropped or verified.get("blocked") != key.get("blocked"):
        raise RegError(f"key regression: dropped={dropped} blocked {key.get('blocked')}->{verified.get('blocked')}")
    print("[key] verified: additive, no drops, blocked unchanged")


def main() -> int:
    global TARGET_ALIAS
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="perform writes (default: dry-run)")
    ap.add_argument("--key-alias", default=TARGET_ALIAS,
                    help=f"target key alias to grant (default: {TARGET_ALIAS})")
    ap.add_argument("--grant-only", action="store_true",
                    help="skip model registration; only grant to the key (models must already exist)")
    args = ap.parse_args()
    TARGET_ALIAS = args.key_alias
    dry = not args.apply
    mode = "DRY-RUN" if dry else "APPLY"
    print(f"=== cursor-fc register [{mode}] ns={NS} carrier={CARRIER_SERVICE}:{CARRIER_PORT} "
          f"key={TARGET_ALIAS} grant_only={args.grant_only} ===")
    if not args.grant_only:
        register_models(dry)
    grant_key(dry)
    print("[done]" if args.apply else "[dry-run] no changes applied")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RegError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
