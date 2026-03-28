#!/bin/bash
# Build and deploy CarHer Admin Dashboard to K8s
#
# Usage:
#   ./deploy.sh                    # build + push + deploy
#   ./deploy.sh --build-only       # only build Docker image
#   ./deploy.sh --deploy-only      # only apply K8s manifests (image already pushed)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ACR="cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com"
REPO="her/carher-admin"
TAG="${1:-latest}"
NS="carher"

BUILD="yes"
DEPLOY="yes"
for arg in "$@"; do
  case "$arg" in
    --build-only)  DEPLOY="" ;;
    --deploy-only) BUILD="" ;;
  esac
done

if [ -n "$BUILD" ]; then
  echo "▶ Building Docker image..."
  docker build -t "${ACR}/${REPO}:${TAG}" "$SCRIPT_DIR"
  echo "✓ Build complete"

  echo "▶ Pushing to ACR..."
  docker push "${ACR}/${REPO}:${TAG}"
  echo "✓ Push complete"
fi

if [ -n "$DEPLOY" ]; then
  echo "▶ Applying K8s manifests..."
  kubectl apply -f "$SCRIPT_DIR/k8s/rbac.yaml"
  kubectl apply -f "$SCRIPT_DIR/k8s/deployment.yaml"

  echo "▶ Restarting deployment..."
  kubectl rollout restart deployment/carher-admin -n "$NS"
  kubectl rollout status deployment/carher-admin -n "$NS" --timeout=120s

  echo ""
  echo "✓ CarHer Admin deployed!"
  echo "  Pod: $(kubectl get pods -n $NS -l app=carher-admin -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
  echo "  IP:  $(kubectl get pods -n $NS -l app=carher-admin -o jsonpath='{.items[0].status.podIP}' 2>/dev/null)"
fi
