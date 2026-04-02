---
name: carher-admin-deploy
description: >-
  Deploy carher-admin and carher-operator to Alibaba Cloud K8s.
  Use when deploying admin panel or operator code changes.
  Does NOT touch bot instances (carher main program).
---

# CarHer Admin + Operator 部署

部署 admin 管理后台和 operator 控制器。**不涉及 bot 实例（carher 主程序）**。

| Component | Image | K8s Resource | Dockerfile |
|-----------|-------|-------------|------------|
| **carher-admin** | `her/carher-admin` | `deploy/carher-admin` | `./Dockerfile` |
| **carher-operator** | `her/carher-operator` | `deploy/carher-operator` | `./operator-go/Dockerfile` |

## Container Registries

| Endpoint | Usage |
|----------|-------|
| `cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com` | Public (CI push, local push) |
| `cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com` | VPC (K8s pod pull) |

Push 用 Public，`kubectl set image` 用 **VPC**。

## kubectl 隧道

本地 kubectl 通过 SSH 隧道连接阿里云 K8s API Server：

```bash
SSHPASS='5ip0krF>qazQjcvnqc' sshpass -e ssh \
  -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
  -p 1023 -L 16443:172.16.1.163:6443 -N root@47.84.112.136 &
```

验证连通性：`kubectl get nodes`

如果 kubectl 报 `connection refused`，重新执行上面的命令重建隧道。

---

## 方式 1：手动 kubectl（默认）

CI 会自动构建镜像并推送到 ACR，但 **不会** 自动部署 admin/operator。
推送代码到 main 后，等 CI 构建完成，再手动 `kubectl set image`。

### 标准流程

```bash
# 1. 推代码到 main（触发 CI 构建镜像）
git push origin main

# 2. 等 CI 构建完成（约 3-5 分钟），确认 tag
TAG="v$(date +%Y%m%d)-$(git rev-parse --short HEAD)"

# 3. 部署 admin
kubectl set image deploy/carher-admin \
  admin=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher-admin:$TAG \
  -n carher
kubectl rollout status deploy/carher-admin -n carher --timeout=120s

# 4. 部署 operator（如果有 operator 代码变更）
kubectl set image deploy/carher-operator \
  operator=cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher-operator:$TAG \
  -n carher
kubectl rollout status deploy/carher-operator -n carher --timeout=120s
```

### 验证

```bash
kubectl get pods -n carher -l app=carher-admin -o wide
kubectl get pods -n carher -l app=carher-operator -o wide
kubectl get deploy carher-admin -n carher \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl logs -n carher deploy/carher-admin --tail=30
kubectl logs -n carher deploy/carher-operator --tail=30
```

---

## 方式 2：本地构建 + 手动部署

CI 挂了或需要紧急部署时使用。

```bash
TAG="v$(date +%Y%m%d)-$(git rev-parse --short HEAD)"
ACR_PUB="cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com/her"
ACR_VPC="cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her"

# CRITICAL: Must specify --platform linux/amd64 (Mac 默认构建 ARM64)
docker build --platform linux/amd64 -t $ACR_PUB/carher-admin:$TAG .
docker push $ACR_PUB/carher-admin:$TAG

kubectl set image deploy/carher-admin \
  admin=$ACR_VPC/carher-admin:$TAG -n carher
kubectl rollout status deploy/carher-admin -n carher --timeout=120s

# Operator（如需）
docker build --platform linux/amd64 -t $ACR_PUB/carher-operator:$TAG ./operator-go
docker push $ACR_PUB/carher-operator:$TAG

kubectl set image deploy/carher-operator \
  operator=$ACR_VPC/carher-operator:$TAG -n carher
kubectl rollout status deploy/carher-operator -n carher --timeout=120s
```

---

## 方式 3：GitHub Actions 手动触发

通过 `workflow_dispatch` 只构建 admin 或 operator，不触发 bot 实例部署：

1. GitHub Actions → Build & Deploy → Run workflow
2. `deploy_mode` 选 `build-only`
3. `components` 选 `admin` 或 `operator`
4. CI 构建并推送镜像后，手动 `kubectl set image`

---

## Apply K8s Manifests（RBAC, CRD 等）

```bash
kubectl apply -f k8s/crd.yaml
kubectl apply -f k8s/operator-rbac.yaml
kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/operator-deployment.yaml
kubectl apply -f k8s/deployment.yaml
```

## K8s Resources

| Resource | File | Notes |
|----------|------|-------|
| Admin Deployment | `k8s/deployment.yaml` | 1 replica, pinned to node `ap-southeast-1.172.16.0.226` (SQLite hostPath), probe TCP:8900 |
| Operator Deployment | `k8s/operator-deployment.yaml` | 2 replicas, Prometheus metrics, probe HTTP:8081 `/healthz` `/readyz` |
| CRD | `k8s/crd.yaml` | HerInstance custom resource (`herinstances.carher.io`) |
| Operator RBAC | `k8s/operator-rbac.yaml` | ClusterRole: herinstances, deployments, services, pods, configmaps, PVCs, secrets, events, namespaces, leases |
| Admin RBAC | `k8s/rbac.yaml` | SA + Role + ClusterRole (`carher-admin-cluster`): pods, configmaps, PVCs, secrets, events, herinstances, nodes, metrics |

## Pitfalls

### 1. Platform Mismatch (ImagePullBackOff)

**Symptom**: `no match for platform in manifest: not found`
**Fix**: Always use `--platform linux/amd64` when building locally.

### 2. Public vs VPC Registry

**Symptom**: Image push succeeds but pod can't pull.
**Fix**: Push to public endpoint, set pod image to VPC endpoint. They share the same underlying storage.

### 3. rollout restart Doesn't Update Code

**Symptom**: `kubectl rollout restart` but pod still runs old code.
**Fix**: Deployment uses a fixed image tag. Must `kubectl set image` with new tag.

### 4. Admin Pod Pinned to Node

Admin uses `nodeSelector` pinned to `ap-southeast-1.172.16.0.226` (SQLite hostPath).
If this node is unavailable, admin pod cannot be scheduled.

### 5. DB Schema Version Mismatch After Rollback

**Symptom**: Logs show `schema v7` after a revert, but the previous deploy was `v8`.
**Fix**: Always deploy forward. SQLite data on hostPath persists across deploys.

## Troubleshooting

| Symptom | Check | Fix |
|---------|-------|-----|
| ImagePullBackOff | `kubectl describe pod` → image arch | Rebuild with `--platform linux/amd64` |
| CrashLoopBackOff | `kubectl logs <pod>` | Fix config, rebuild |
| Connection refused | SSH tunnel down | Re-run SSH tunnel command |
| Pod stuck Pending | `kubectl describe pod` → node affinity | Check nodeSelector and node status |
| Operator not reconciling | `kubectl logs deploy/carher-operator` | Check RBAC, CRD version |
| OOMKilled | `kubectl describe pod` → Last State | Admin: 256Mi-512Mi, Operator: 128Mi-512Mi |
| Probe failed restart | `kubectl describe pod` → Events | Admin: TCP 8900, Operator: HTTP 8081 /healthz |
