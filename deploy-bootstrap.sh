#!/bin/bash
# First-time bootstrap deployment of CarHer Admin platform to K8s.
# Run on K8s node 226 after cloning the repo.
#
# Usage:
#   ./deploy-bootstrap.sh                    # full: build + push + deploy
#   ./deploy-bootstrap.sh --images-only      # only build & push images
#   ./deploy-bootstrap.sh --apply-only       # only apply K8s manifests (images already pushed)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ACR="cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com"
ACR_VPC="cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com"
ACR_USER="liuguoxian@1989403661820148"
ACR_PASSWORD="${ACR_PASSWORD:-}"
CLOUDFLARE_API_TOKEN="${CLOUDFLARE_API_TOKEN:-}"
NS="carher"
TAG="v$(date +%Y%m%d)-$(git -C "$SCRIPT_DIR" rev-parse --short HEAD)"

BUILD="yes"
APPLY="yes"
for arg in "$@"; do
  case "$arg" in
    --images-only) APPLY="" ;;
    --apply-only)  BUILD="" ;;
  esac
done

echo "============================================"
echo "  CarHer Admin Bootstrap Deployment"
echo "  Tag: $TAG"
echo "  ACR: $ACR"
echo "============================================"

# --- Build & Push ---
if [ -n "$BUILD" ]; then
  if [ -z "$ACR_PASSWORD" ]; then
    echo "ERROR: ACR_PASSWORD is not set. Export the ACR password before building images." >&2
    exit 1
  fi

  echo ""
  echo "▶ [1/4] Starting Docker daemon (temporary)..."
  groupadd docker 2>/dev/null || true
  systemctl start docker.socket docker 2>/dev/null || true
  sleep 2

  echo "▶ [2/4] Logging into ACR..."
  printf '%s' "$ACR_PASSWORD" | docker login --username="$ACR_USER" \
    --password-stdin "$ACR"

  echo ""
  echo "▶ [3/4] Building carher-admin image..."
  docker build -t "${ACR}/her/carher-admin:${TAG}" "$SCRIPT_DIR"
  echo "  Pushing carher-admin..."
  docker push "${ACR}/her/carher-admin:${TAG}"
  echo "  ✓ carher-admin pushed"

  echo ""
  echo "▶ [4/4] Building carher-operator image..."
  docker build -t "${ACR}/her/carher-operator:${TAG}" "$SCRIPT_DIR/operator-go"
  echo "  Pushing carher-operator..."
  docker push "${ACR}/her/carher-operator:${TAG}"
  echo "  ✓ carher-operator pushed"

  echo ""
  echo "▶ Stopping Docker daemon..."
  systemctl stop docker docker.socket 2>/dev/null || true
  echo "  ✓ Docker stopped"
fi

# --- Apply K8s manifests ---
if [ -n "$APPLY" ]; then
  echo ""
  echo "▶ Applying K8s manifests..."

  echo "  [1/7] CRD..."
  kubectl apply -f "$SCRIPT_DIR/k8s/crd.yaml"

  echo "  [2/7] Shared NAS PVCs (skills + sessions)..."
  kubectl apply -f "$SCRIPT_DIR/k8s/shared-pvcs.yaml"

  echo "  [3/7] Operator RBAC..."
  kubectl apply -f "$SCRIPT_DIR/k8s/operator-rbac.yaml"

  echo "  [4/7] Operator Deployment (2 replicas)..."
  kubectl apply -f "$SCRIPT_DIR/k8s/operator-deployment.yaml"

  echo "  [5/7] Admin RBAC..."
  kubectl apply -f "$SCRIPT_DIR/k8s/rbac.yaml"

  # Create admin secrets if they don't exist
  if ! kubectl get secret carher-admin-secrets -n "$NS" &>/dev/null; then
    echo "  [5.5] Creating admin secrets..."
    if [ -z "$CLOUDFLARE_API_TOKEN" ]; then
      echo "ERROR: CLOUDFLARE_API_TOKEN is not set. Export it before bootstrap so admin can update Cloudflare tunnel ingress." >&2
      exit 1
    fi
    WEBHOOK_SECRET=$(openssl rand -hex 32)
    API_KEY=$(openssl rand -hex 32)
    kubectl create secret generic carher-admin-secrets -n "$NS" \
      --from-literal=deploy-webhook-secret="$WEBHOOK_SECRET" \
      --from-literal=admin-api-key="$API_KEY" \
      --from-literal=cloudflare-api-token="$CLOUDFLARE_API_TOKEN"
    echo ""
    echo "  ╔══════════════════════════════════════════════════════╗"
    echo "  ║  SAVE THESE KEYS (shown only once):                 ║"
    echo "  ║  DEPLOY_WEBHOOK_SECRET = $WEBHOOK_SECRET"
    echo "  ║  ADMIN_API_KEY         = $API_KEY"
    echo "  ╚══════════════════════════════════════════════════════╝"
    echo ""
  else
    echo "  [5.5] Secret carher-admin-secrets already exists, skipping"
  fi

  echo "  [6/7] Admin Deployment..."
  kubectl apply -f "$SCRIPT_DIR/k8s/deployment.yaml"

  if [ -n "$BUILD" ]; then
    echo "  [6.5/7] Pinning admin/operator Deployments to freshly built tag..."
    kubectl set image deployment/carher-admin \
      admin="${ACR_VPC}/her/carher-admin:${TAG}" -n "$NS"
    kubectl set image deployment/carher-operator \
      operator="${ACR_VPC}/her/carher-operator:${TAG}" -n "$NS"
  fi

  echo "  [7/7] ServiceMonitor + AlertRules..."
  kubectl apply -f "$SCRIPT_DIR/k8s/servicemonitor.yaml" 2>/dev/null || \
    echo "  ⚠ ServiceMonitor CRD not installed, skipping (monitoring can be added later)"

  echo ""
  echo "▶ Waiting for pods to be ready..."
  kubectl rollout status deployment/carher-operator -n "$NS" --timeout=120s || true
  kubectl rollout status deployment/carher-admin -n "$NS" --timeout=120s || true

  echo ""
  echo "============================================"
  echo "  Deployment Complete!"
  echo "============================================"
  echo ""
  kubectl get pods -n "$NS" -o wide
  echo ""
  echo "Verify:"
  echo "  kubectl get her -n $NS                    # should be empty (no CRDs yet)"
  echo "  kubectl logs -l app=carher-operator -n $NS --tail=20"
  echo "  kubectl logs -l app=carher-admin -n $NS --tail=20"
  echo ""
  echo "carher-14 bare Pod should be unaffected:"
  echo "  kubectl get pod carher-14 -n $NS -o wide"
fi
