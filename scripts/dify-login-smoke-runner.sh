#!/usr/bin/env bash
set -euo pipefail

NS="${NS:-carher}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="${SCRIPT_PATH:-$SCRIPT_DIR/dify-login-entry-repair.py}"
PY_IMAGE="${PY_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:dify-python-3.12-slim-20260530}"
KUBECTL_IMAGE="${KUBECTL_IMAGE:-cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:dify-bitnami-kubectl-latest-20260530}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-carher-operator}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%d%H%M%S)}"
NAME="${NAME:-dify-login-smoke-$RUN_ID}"
RUN_DIR="${RUN_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)/runs/dify-login-smoke}"
LOG_PATH="$RUN_DIR/$NAME.log"

usage() {
  cat <<'USAGE'
Usage:
  scripts/dify-login-smoke-runner.sh --smoke-her 266 --smoke-her 268 [extra args...]

Notes:
  - Runs inside the carher namespace with the carher-operator ServiceAccount.
  - This runner only performs active Her issue-login smoke.
  - Dify infrastructure repair/verify remains in h75-upgrade-repair-suite.py.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -eq 0 ]]; then
  usage
  exit 0
fi

cleanup() {
  kubectl -n "$NS" delete pod "$NAME" --ignore-not-found=true --wait=false >/dev/null 2>&1 || true
  kubectl -n "$NS" delete configmap "$NAME-script" --ignore-not-found=true >/dev/null 2>&1 || true
}
trap cleanup EXIT

kubectl -n "$NS" create configmap "$NAME-script" \
  --from-file=dify-login-entry-repair.py="$SCRIPT_PATH" \
  --dry-run=client -o yaml | kubectl apply -f -

args_json="$(python3 - "$@" <<'PY'
import json
import sys
print(json.dumps(sys.argv[1:]))
PY
)"

python3 - "$NAME" "$SERVICE_ACCOUNT" "$PY_IMAGE" "$KUBECTL_IMAGE" "$args_json" <<'PY' | kubectl -n "$NS" apply -f -
import json
import sys

name, service_account, py_image, kubectl_image, args_json = sys.argv[1:]
args = json.loads(args_json)
pod = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {"name": name},
    "spec": {
        "restartPolicy": "Never",
        "serviceAccountName": service_account,
        "initContainers": [
            {
                "name": "kubectl-copy",
                "image": kubectl_image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/sh", "-lc"],
                "args": [
                    "cp /opt/bitnami/kubectl/bin/kubectl /tools/kubectl "
                    "|| cp /usr/local/bin/kubectl /tools/kubectl "
                    "|| cp /usr/bin/kubectl /tools/kubectl; chmod +x /tools/kubectl"
                ],
                "volumeMounts": [{"name": "tools", "mountPath": "/tools"}],
            }
        ],
        "containers": [
            {
                "name": "runner",
                "image": py_image,
                "imagePullPolicy": "IfNotPresent",
                "env": [
                    {
                        "name": "PATH",
                        "value": "/tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    },
                    {"name": "KUBECTL_BIN", "value": "/tools/kubectl"},
                    {"name": "PYTHONUNBUFFERED", "value": "1"},
                ],
                "command": ["python3", "/scripts/dify-login-entry-repair.py", *args],
                "volumeMounts": [
                    {"name": "script", "mountPath": "/scripts", "readOnly": True},
                    {"name": "tools", "mountPath": "/tools"},
                ],
            }
        ],
        "volumes": [
            {"name": "script", "configMap": {"name": f"{name}-script"}},
            {"name": "tools", "emptyDir": {}},
        ],
    },
}
print(json.dumps(pod))
PY

kubectl -n "$NS" wait --for=condition=Ready "pod/$NAME" --timeout=180s || true
mkdir -p "$RUN_DIR"
kubectl -n "$NS" logs -f "pod/$NAME" | tee "$LOG_PATH"
phase=""
for _ in $(seq 1 30); do
  phase="$(kubectl -n "$NS" get pod "$NAME" -o jsonpath='{.status.phase}' 2>/tmp/"$NAME".phase.err || true)"
  if [[ -z "$phase" ]]; then
    err="$(tr '\n' ' ' </tmp/"$NAME".phase.err 2>/dev/null || true)"
    echo "warn: runner phase poll failed: ${err:-empty phase}" >&2
    sleep 2
    continue
  fi
  [[ "$phase" != "Running" ]] && break
  sleep 1
done
case "$phase" in
  Succeeded)
    echo "log: $LOG_PATH"
    exit 0
    ;;
  *) echo "fatal: runner pod phase=$phase" >&2; exit 1 ;;
esac
