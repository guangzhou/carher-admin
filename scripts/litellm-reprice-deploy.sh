#!/usr/bin/env bash
# litellm-reprice-deploy: scp updated yaml to build node, kubectl apply,
# rollout restart, wait for both replicas. Idempotent.
#
# Pre-flight checks:
#   - target yaml exists locally
#   - yaml syntactically valid (python yaml.safe_load)
#   - jms wrapper exists
#
# Usage: scripts/litellm-reprice-deploy.sh [yaml-path]
#        default yaml-path = k8s/litellm-proxy.yaml
set -euo pipefail

TARGET="${1:-k8s/litellm-proxy.yaml}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
JMS="$REPO_ROOT/scripts/jms"

if [ ! -f "$TARGET" ]; then
  echo "not found: $TARGET" >&2
  exit 1
fi
if [ ! -x "$JMS" ]; then
  echo "jms wrapper not found: $JMS" >&2
  exit 1
fi

echo "=== syntax check ==="
python3 -c "import yaml,sys; list(yaml.safe_load_all(open('$TARGET'))); print('  ok')"

echo
echo "=== local md5 ==="
md5_local=$(md5sum "$TARGET" 2>/dev/null | awk '{print $1}' || md5 -q "$TARGET")
echo "  $md5_local"

echo
echo "=== upload to k8s-work-226 ==="
"$JMS" scp "$TARGET" k8s-work-226:/tmp/litellm-proxy.yaml

echo
echo "=== remote md5 verify + diff + apply + rollout ==="
"$JMS" ssh k8s-work-226 bash << REMOTE_EOF
set -e
REMOTE_MD5=\$(md5sum /tmp/litellm-proxy.yaml | awk '{print \$1}')
echo "  remote md5: \$REMOTE_MD5"
if [ "\$REMOTE_MD5" != "$md5_local" ]; then
  echo "MD5 MISMATCH — upload corrupted" >&2
  exit 1
fi

echo
echo "=== kubectl diff (shows actual ConfigMap delta) ==="
kubectl diff -f /tmp/litellm-proxy.yaml 2>&1 | grep -E '^[+-]' | head -40 || true

echo
echo "=== kubectl apply ==="
kubectl apply -f /tmp/litellm-proxy.yaml

echo
echo "=== rollout restart litellm-proxy (zero-downtime, 2 replicas) ==="
kubectl rollout restart deployment/litellm-proxy -n carher
kubectl rollout status deployment/litellm-proxy -n carher --timeout=300s
kubectl get pods -n carher -l app=litellm-proxy
REMOTE_EOF

echo
echo "DONE. Verify with:"
echo "  scripts/litellm-reprice-verify.sh '<pattern>' '$(date '+%Y-%m-%d %H:%M')'"
