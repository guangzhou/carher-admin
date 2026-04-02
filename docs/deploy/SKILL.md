---
name: carher-deploy
description: >-
  Deploy carher-admin and carher-operator to Alibaba Cloud K8s. Covers CI/CD
  pipeline, manual deployment, image building, and troubleshooting. Use when
  deploying code, building Docker images, updating K8s deployments, checking
  deploy status, or debugging deployment failures.
---

# CarHer Admin Deployment

## Architecture

```
GitHub (main push)
  → GitHub Actions (build-deploy.yml)
    → Build Docker images (admin + operator)
    → Push to Alibaba Cloud ACR
    → Call /api/deploy/webhook
      → Admin rolling update of CarHer user instances
```

Two deployable components:

| Component | Image | K8s Resource | Dockerfile |
|-----------|-------|-------------|------------|
| **carher-admin** | `her/carher-admin` | `deploy/carher-admin` | `./Dockerfile` |
| **carher-operator** | `her/carher-operator` | `deploy/carher-operator` | `./operator-go/Dockerfile` |

## Container Registries

| Endpoint | Usage |
|----------|-------|
| `cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com` | Public (CI push, local push) |
| `cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com` | VPC (K8s pod pull) |

When setting image on K8s Deployments, always use the **VPC** endpoint.

## Method 1: CI/CD (Preferred)

Push to `main` branch auto-triggers `.github/workflows/build-deploy.yml`:

1. Builds both `carher-admin` and `carher-operator` images
2. Tags: `v{YYYYMMDD}-{sha7}` + `latest`
3. Pushes to ACR
4. Calls deploy webhook → Admin orchestrates rolling update of user instances
5. Polls `/api/deploy/status` for 90s to verify

**Paths ignored** (won't trigger CI): `*.md`, `docs/**`

**Manual trigger**: GitHub Actions → `workflow_dispatch` with options for
`deploy_mode` (normal/fast/canary-only/build-only) and `components`
(all/admin/operator).

## Method 2: Manual Deployment

When CI is broken or you need immediate deployment.

### Build & Push Image

```bash
# CRITICAL: Must specify --platform linux/amd64
# K8s nodes are AMD64; Mac builds ARM64 by default
docker build --platform linux/amd64 \
  -t cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com/her/carher-admin:v20260402-desc \
  .

docker push cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com/her/carher-admin:v20260402-desc
```

### Update K8s Deployment

```bash
# Use VPC endpoint for the image reference
kubectl set image deploy/carher-admin \
  admin=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher-admin:v20260402-desc \
  -n carher

kubectl rollout status deploy/carher-admin -n carher --timeout=120s
```

### Operator Deployment

```bash
docker build --platform linux/amd64 \
  -t cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com/her/carher-operator:v20260402-desc \
  ./operator-go

docker push cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com/her/carher-operator:v20260402-desc

kubectl set image deploy/carher-operator \
  operator=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher-operator:v20260402-desc \
  -n carher

kubectl rollout status deploy/carher-operator -n carher --timeout=120s
```

### Apply K8s Manifests (RBAC, CRD, etc.)

```bash
kubectl apply -f k8s/crd.yaml
kubectl apply -f k8s/operator-rbac.yaml
kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/operator-deployment.yaml
kubectl apply -f k8s/deployment.yaml
```

## Verification

```bash
# Check pod status
kubectl get pods -n carher -l app=carher-admin -o wide
kubectl get pods -n carher -l app=carher-operator -o wide

# Check running image
kubectl get deploy carher-admin -n carher \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'

# Check logs
kubectl logs -n carher deploy/carher-admin --tail=30
kubectl logs -n carher deploy/carher-operator --tail=30

# Deploy status via API
curl -sH "X-API-Key: $ADMIN_API_KEY" https://admin.carher.net/api/deploy/status | jq
```

## K8s Resources Reference

| Resource | File | Notes |
|----------|------|-------|
| Admin Deployment | `k8s/deployment.yaml` | Pinned to node (SQLite hostPath) |
| Operator Deployment | `k8s/operator-deployment.yaml` | 2 replicas, Prometheus metrics |
| CRD | `k8s/crd.yaml` | HerInstance custom resource |
| Operator RBAC | `k8s/operator-rbac.yaml` | ClusterRole for operator |
| Admin RBAC | `k8s/rbac.yaml` | ServiceAccount for admin |

## Secrets

| Secret | Keys | Used By |
|--------|------|---------|
| `carher-admin-secrets` | `admin-api-key`, `admin-username`, `admin-password`, `deploy-webhook-secret` | Admin |
| `acr-secret` | Docker registry pull secret | All pods |
| `carher-env-keys` | Shared env vars | User instances |
| `carher-{uid}-secret` | `app_secret` (Feishu) | Per-user instance |

## Pitfalls

### 1. Platform Mismatch (ImagePullBackOff)

**Symptom**: `no match for platform in manifest: not found`

**Cause**: Built on Mac (ARM64), K8s runs AMD64.

**Fix**: Always use `--platform linux/amd64` when building locally.

### 2. Public vs VPC Registry

**Symptom**: Image push succeeds but pod can't pull.

**Cause**: Pushed to public endpoint but K8s uses VPC endpoint, or tag only
exists on one endpoint.

**Fix**: Push to public endpoint (both share same storage). Set pod image to
VPC endpoint. They resolve to the same underlying repository.

### 3. Deploy Webhook 403

**Symptom**: CI build succeeds but deploy fails with 403.

**Cause**: `DEPLOY_WEBHOOK_SECRET` in GitHub Secrets doesn't match K8s Secret.

**Fix**: Verify the secret matches:
```bash
kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.deploy-webhook-secret}' | base64 -d
```
Then update GitHub repo Settings → Secrets → `DEPLOY_WEBHOOK_SECRET`.

### 4. rollout restart Doesn't Update Code

**Symptom**: `kubectl rollout restart` but pod still runs old code.

**Cause**: Deployment uses a fixed image tag (not `latest`). Restart only
re-pulls the same tag.

**Fix**: Build new image, push, then `kubectl set image` with the new tag.

### 5. Admin Pod Pinned to Node

The admin deployment uses `nodeSelector` to pin to a specific node
(`ap-southeast-1.172.16.0.226`) because SQLite requires local disk (hostPath).
If this node is unavailable, the admin pod cannot be scheduled.
