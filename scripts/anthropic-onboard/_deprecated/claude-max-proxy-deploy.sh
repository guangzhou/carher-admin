#!/usr/bin/env bash
# Deploy or update the claude-max-proxy Pod on the carher cluster.
#
# Usage:
#   ./claude-max-proxy-deploy.sh "acct-1::sk-ant-oat01-XXX,acct-2::sk-ant-oat01-YYY"
#
# Run on a host that has `scripts/jms ssh k8s-work-226` access.
#
# Idempotent: rewrites the ConfigMap (proxy code) and recreates the Pod.

set -euo pipefail

ACCT_TOKENS="${1:-}"
if [[ -z "$ACCT_TOKENS" ]]; then
  echo "usage: $0 \"label1::TOKEN1,label2::TOKEN2\"" >&2
  echo "  (or empty string to fall back to host credentials file)" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PROXY_PY="$REPO_ROOT/scripts/anthropic-onboard/claude-max-proxy.py"
POD_YAML="$REPO_ROOT/scripts/anthropic-onboard/claude-max-proxy-pod.yaml"
JMS="$REPO_ROOT/scripts/jms"

[[ -f "$PROXY_PY" ]] || { echo "missing $PROXY_PY"; exit 1; }
[[ -f "$POD_YAML" ]] || { echo "missing $POD_YAML"; exit 1; }

echo "[1/3] uploading ConfigMap with proxy code..."
"$JMS" ssh k8s-work-226 \
  "kubectl create configmap claude-proxy-script -n default \
     --from-file=proxy.py=/dev/stdin --dry-run=client -o yaml \
   | kubectl apply -f -" < "$PROXY_PY"

echo "[2/3] applying Pod (with ACCT_TOKENS env injected)..."
# Inject ACCT_TOKENS into the manifest before applying.
python3 - "$POD_YAML" "$ACCT_TOKENS" <<'PY' | "$JMS" ssh k8s-work-226 \
  "kubectl delete pod claude-max-proxy -n default --ignore-not-found 2>/dev/null; \
   kubectl apply -f -"
import sys, yaml
yaml_path, acct = sys.argv[1], sys.argv[2]
with open(yaml_path) as f:
    pod = yaml.safe_load(f)
for c in pod["spec"]["containers"]:
    for e in c.get("env", []):
        if e.get("name") == "ACCT_TOKENS":
            e["value"] = acct
sys.stdout.write(yaml.safe_dump(pod, sort_keys=False))
PY

echo "[3/3] waiting for Pod to be Ready..."
sleep 8
"$JMS" ssh k8s-work-226 \
  "kubectl get pod claude-max-proxy -n default; \
   echo ---; \
   kubectl logs claude-max-proxy -n default | tail -5"

echo
echo "✓ Proxy deployed at http://172.16.0.86:3456 (cluster-internal)"
echo "  health: curl http://172.16.0.86:3456/health  (from any pod)"
