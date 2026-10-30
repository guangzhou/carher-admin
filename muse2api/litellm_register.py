#!/usr/bin/env python3
"""Register muse2api (ns muse2api on 198 k3s) as LiteLLM models on 198 litellm-product.

Groups (one leg each, all pointing at the in-cluster muse2api Service):
  muse-spark  chat               -> openai/muse-spark
  muse-image  image_generation   -> openai/muse-image

Runs ON 198 (needs sudo kubectl). Default is dry-run; --apply writes via /model/new.
Cost fields are 0: muse.ai web quota, no per-token price.

  scp muse2api/litellm_register.py cltx@10.68.13.198:~/muse-reg-<sid>.py
  python3 ~/muse-reg-<sid>.py                   # dry-run: plan + who can already reach muse-*
  python3 ~/muse-reg-<sid>.py --apply           # create (or recreate) entries
  python3 ~/muse-reg-<sid>.py --delete --apply  # rollback: remove both entries

Granting a key is a separate step (scripts/litellm-198-key-allowlist.py --only <alias>
--add-model muse-spark --add-model muse-image). Keys whose `models` is empty are
all-access and reach muse-* without any grant; dry-run lists them.

The upstream key (MUSE2API_KEY) is read from Secret muse2api/muse2api-env and
the master key from litellm-product/litellm-secrets; neither is printed.
"""
import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

PROXY = "http://127.0.0.1:30402"
UPSTREAM_BASE = "http://muse2api.muse2api.svc.cluster.local:18610/v1"
ENTRIES = [
    {"group": "muse-spark", "id": "muse2api-muse-spark", "upstream": "openai/muse-spark", "mode": "chat"},
    {"group": "muse-image", "id": "muse2api-muse-image", "upstream": "openai/muse-image", "mode": "image_generation"},
]


def secret(ns, name, key):
    out = subprocess.run(
        ["sudo", "kubectl", "-n", ns, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}"],
        capture_output=True, text=True, check=True).stdout
    val = base64.b64decode(out).decode().strip()
    if not val:
        sys.exit(f"empty secret {ns}/{name}.{key}")
    return val


def api(mk, method, path, body=None):
    req = urllib.request.Request(
        PROXY + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {mk}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def entry(e, upstream_key):
    return {
        "model_name": e["group"],
        "litellm_params": {
            "model": e["upstream"],
            "api_base": UPSTREAM_BASE,
            "api_key": upstream_key,
            "timeout": 300,
            "stream_timeout": 300,
            "num_retries": 0,
            "input_cost_per_token": 0,
            "output_cost_per_token": 0,
        },
        "model_info": {"id": e["id"], "mode": e["mode"], "base_model": e["upstream"].split("/", 1)[1]},
    }


def present_ids(mk):
    data = api(mk, "GET", "/model/info").get("data", [])
    return {(m.get("model_info") or {}).get("id"): m.get("model_name") for m in data}


def psql(sql):
    """Read-only query on the prod DB. SQL goes through stdin of `kubectl exec -i` (no shell quoting)."""
    return subprocess.run(
        ["sudo", "kubectl", "-n", "litellm-product", "exec", "-i", "litellm-db-0", "--",
         "psql", "-U", "litellm", "-d", "litellm", "-At", "-F", " | "],
        input=sql, capture_output=True, text=True, check=True).stdout.strip()


def who_can_reach():
    granted = psql("""select key_alias from "LiteLLM_VerificationToken"
        where ('muse-spark' = any(models) or 'muse-image' = any(models)) order by 1;""")
    open_keys = psql("""select coalesce(key_alias,'(no alias)') || ' expires=' || coalesce(expires::text,'never')
        from "LiteLLM_VerificationToken"
        where (models = '{}'::text[] or models is null) and not coalesce(blocked,false)
          and (expires is null or expires > now()) order by 1;""")
    print("keys granted muse-*:", granted.splitlines() or "none")
    print("all-access keys (empty models, also reach muse-*):")
    for line in open_keys.splitlines():
        print("   ", line)


def wait_state(mk, want_present, tries=18, gap=10):
    """Proxy has several replicas that refresh from DB on their own timer:
    require 3 consecutive reads that agree before calling it done."""
    ids = {e["id"] for e in ENTRIES}
    hit = 0
    for _ in range(tries):
        cur = set(present_ids(mk))
        ok = ids <= cur if want_present else not (ids & cur)
        hit = hit + 1 if ok else 0
        if hit >= 3:
            return True
        time.sleep(gap)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--delete", action="store_true", help="remove the entries (rollback)")
    a = ap.parse_args()

    mk = secret("litellm-product", "litellm-secrets", "LITELLM_MASTER_KEY")
    print(f"master key len={len(mk)}")
    cur = present_ids(mk)

    for e in ENTRIES:
        exists = e["id"] in cur
        clash = [i for i, g in cur.items() if g == e["group"] and i != e["id"]]
        if clash:
            sys.exit(f"group {e['group']} already has other members {clash}; refusing")
        if a.delete:
            act = "delete" if exists else "skip (absent)"
        else:
            act = "recreate" if exists else "create"
        print(f"  {e['group']:<11} id={e['id']:<22} mode={e['mode']:<17} -> {act}")

    if not a.apply:
        who_can_reach()
        print("dry-run; add --apply to write")
        return

    upstream_key = "" if a.delete else secret("muse2api", "muse2api-env", "MUSE2API_KEY")
    for e in ENTRIES:
        if e["id"] in cur:
            try:
                api(mk, "POST", "/model/delete", {"id": e["id"]})
            except urllib.error.HTTPError as err:
                print(f"  delete {e['id']} -> HTTP {err.code}")
        if not a.delete:
            api(mk, "POST", "/model/new", entry(e, upstream_key))
            print(f"  wrote {e['id']}")
        else:
            print(f"  deleted {e['id']}")

    ok = wait_state(mk, want_present=not a.delete)
    print(f"all replicas agree: {'yes' if ok else 'NO (timed out, do not treat as live)'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
