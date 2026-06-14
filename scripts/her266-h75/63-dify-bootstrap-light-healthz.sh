#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-dify}"
CM="${CM:-dify-bootstrap-code}"
DEPLOY="${DEPLOY:-dify-bootstrap}"
STATE_DIR="${STATE_DIR:-.dify-ha-state}"

usage() {
  cat <<EOF
Usage: $0 <apply|verify> [backup-file]

apply:
  - backs up ConfigMap $NS/$CM to $STATE_DIR
  - changes /healthz to a cheap process probe
  - keeps the old Dify login dependency check at /deep-healthz
  - rolls $NS/$DEPLOY with maxUnavailable=0 strategy already on the Deployment

verify:
  - checks /healthz from a live bootstrap pod
  - checks /deep-healthz from the same pod
EOF
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing command: $1" >&2
    exit 2
  }
}

timestamp() {
  date -u +%Y%m%dT%H%M%SZ
}

backup_configmap() {
  mkdir -p "$STATE_DIR"
  local backup="${1:-$STATE_DIR/dify-bootstrap-code-before-light-healthz-$(timestamp).yaml}"
  kubectl -n "$NS" get cm "$CM" -o yaml >"$backup"
  echo "$backup"
}

patch_configmap() {
  local patched_at="$1"
  local patch_file
  patch_file="$(mktemp)"
  kubectl -n "$NS" get cm "$CM" -o json | PATCHED_AT="$patched_at" python3 -c '
import json
import os
import sys

old_master = """def master_token():
    now = time.time()
    if _master_cache["token"] and _master_cache["expires_at"] > now + 60:
        return _master_cache["token"]
    env = _read_env(MASTER_ENV)
    r = requests.post(
        f"{DIFY_BASE}/console/api/login",
        json={
            "email": env["DIFY_MASTER_EMAIL"],
            "password": env["DIFY_MASTER_PASSWORD"],
            "language": "en-US",
        },
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()["data"]
    _master_cache["token"] = d["access_token"]
    _master_cache["expires_at"] = now + 3000
    return d["access_token"]
"""

new_master = """def master_token():
    now = time.time()
    if _master_cache["token"] and _master_cache["expires_at"] > now + 60:
        return _master_cache["token"]
    env = _read_env(MASTER_ENV)
    last_error = None
    for attempt in range(2):
        try:
            r = requests.post(
                f"{DIFY_BASE}/console/api/login",
                json={
                    "email": env["DIFY_MASTER_EMAIL"],
                    "password": env["DIFY_MASTER_PASSWORD"],
                    "language": "en-US",
                },
                timeout=30,
            )
            r.raise_for_status()
            d = r.json()["data"]
            _master_cache["token"] = d["access_token"]
            _master_cache["expires_at"] = time.time() + 3000
            return d["access_token"]
        except Exception as e:
            last_error = e
            _master_cache["token"] = None
            _master_cache["expires_at"] = 0
            if attempt == 0:
                time.sleep(0.5)
    raise last_error
"""

old = """@app.route("/healthz")
def healthz():
    try:
        master_token()
        return jsonify({"ok": True, "ts": int(time.time())})
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)}), 500
"""

new = """@app.route("/healthz")
def healthz():
    # Kubernetes probes must stay local-only; Dify login is checked below.
    return jsonify({"ok": True, "ts": int(time.time())})


@app.route("/deep-healthz")
def deep_healthz():
    try:
        master_token()
        return jsonify({"ok": True, "ts": int(time.time()), "dify_login": True})
    except Exception as e:
        return jsonify({"ok": False, "err": str(e), "dify_login": False}), 500
"""

doc = json.load(sys.stdin)
data = doc.setdefault("data", {})
source = data.get("bootstrap.py")
if not source:
    raise SystemExit("bootstrap.py missing from ConfigMap data")

changes = []
if new_master in source:
    pass
elif old_master in source:
    source = source.replace(old_master, new_master, 1)
    changes.append("master_token_retry")
else:
    raise SystemExit("expected master_token block not found; refusing to patch")

if new in source:
    pass
elif old in source:
    source = source.replace(old, new, 1)
    changes.append("light_healthz")
else:
    raise SystemExit("expected healthz block not found; refusing to patch")
data["bootstrap.py"] = source

patch = {
    "metadata": {
        "annotations": {
            "carher.io/light-healthz-patched-at": os.environ["PATCHED_AT"],
            "carher.io/light-healthz-changes": ",".join(changes) if changes else "none",
        }
    },
    "data": {"bootstrap.py": source},
}

print(json.dumps(patch))
' >"$patch_file"
  kubectl -n "$NS" patch cm "$CM" --type=merge --patch-file "$patch_file"
  rm -f "$patch_file"
}

first_bootstrap_pod() {
  kubectl -n "$NS" get pod -l app="$DEPLOY" \
    -o jsonpath='{range .items[?(@.status.phase=="Running")]}{.metadata.name}{"\n"}{end}' \
    | head -1
}

verify_endpoint() {
  local pod="$1"
  local path="$2"
  kubectl -n "$NS" exec -i "$pod" -- python - "$path" <<'PY'
import sys
import urllib.error
import urllib.request

path = sys.argv[1]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:5688{path}", timeout=10) as resp:
        body = resp.read().decode()
        print(f"{path} status={resp.status} body={body}")
        if resp.status != 200:
            raise SystemExit(1)
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"{path} status={e.code} body={body}")
    raise
PY
}

apply_fix() {
  local backup patched_at
  backup="$(backup_configmap "${1:-}")"
  patched_at="$(timestamp)"
  echo "[dify-bootstrap] backup: $backup"
  patch_configmap "$patched_at"
  kubectl -n "$NS" rollout restart "deploy/$DEPLOY"
  kubectl -n "$NS" rollout status "deploy/$DEPLOY" --timeout=300s
  verify_fix
}

verify_fix() {
  local pod
  pod="$(first_bootstrap_pod)"
  if [[ -z "$pod" ]]; then
    echo "no running $DEPLOY pod found" >&2
    exit 1
  fi
  echo "[dify-bootstrap] verifying pod: $pod"
  verify_endpoint "$pod" /healthz
  verify_endpoint "$pod" /deep-healthz
  kubectl -n "$NS" get deploy "$DEPLOY" \
    -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,AVAILABLE:.status.availableReplicas,UPDATED:.status.updatedReplicas'
}

need_cmd kubectl
need_cmd python3

action="${1:-}"
case "$action" in
  apply)
    apply_fix "${2:-}"
    ;;
  verify)
    verify_fix
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
