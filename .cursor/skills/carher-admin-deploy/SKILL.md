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
| `cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com` | Public（push 用此地址） |
| `cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com` | VPC（K8s pod pull） |

Push 用 Public，`kubectl set image` 用 **VPC**。二者共享同一底层存储。

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

## 构建服务器

镜像构建在 K8s 跳板机上完成（`47.84.112.136:1023`），**不在本地 Mac 构建**。

| 项目 | 值 |
|------|---|
| SSH | `sshpass -e ssh -p 1023 root@47.84.112.136` |
| 构建工具 | `nerdctl` + `buildkitd`（已安装为 systemd service） |
| 仓库路径 | `/root/carher-admin` |
| ACR 凭证 | 已 `nerdctl login`（存于 `/root/.docker/config.json`） |

---

## 标准部署流程（默认方式）

Admin/Operator **不走 CI/CD**，GitHub Actions 不构建也不部署。

### Step 1：本地提交代码

```bash
git add -A && git commit -m "your message" && git push
```

### Step 2：SSH 到服务器构建并推送

```bash
export SSHPASS='5ip0krF>qazQjcvnqc'

sshpass -e ssh -o StrictHostKeyChecking=no -p 1023 root@47.84.112.136 "
cd /root/carher-admin && git pull

TAG=\"v\$(date +%Y%m%d)-\$(git rev-parse --short HEAD)\"
ACR_PUB='cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com/her'

# 构建 + 推送 admin
nerdctl build -t \$ACR_PUB/carher-admin:\$TAG . && \
nerdctl push \$ACR_PUB/carher-admin:\$TAG

# 构建 + 推送 operator（如果有 operator 代码变更）
nerdctl build -t \$ACR_PUB/carher-operator:\$TAG ./operator-go && \
nerdctl push \$ACR_PUB/carher-operator:\$TAG

echo \"TAG=\$TAG\"
"
```

### Step 3：kubectl 部署

```bash
TAG="v$(date +%Y%m%d)-$(git rev-parse --short HEAD)"
ACR_VPC="cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her"

# 部署 admin
kubectl set image deploy/carher-admin \
  admin=$ACR_VPC/carher-admin:$TAG -n carher
kubectl rollout status deploy/carher-admin -n carher --timeout=120s

# 部署 operator（如果有 operator 代码变更）
kubectl set image deploy/carher-operator \
  operator=$ACR_VPC/carher-operator:$TAG -n carher
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

## 构建服务器维护

### buildkitd

buildkitd 作为 systemd service 运行，开机自启：

```bash
systemctl status buildkit       # 检查状态
systemctl restart buildkit      # 重启
journalctl -u buildkit -n 20    # 查看日志
```

如果 `nerdctl build` 报 `buildctl not found` 或 socket 错误，重启 buildkit。

### ACR 登录过期

```bash
nerdctl login --username='liuguoxian@1989403661820148' \
  --password='cltx!@#456' \
  cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com
```

### 仓库更新

```bash
cd /root/carher-admin && git pull
```

---

## Apply K8s Manifests（RBAC, CRD 等）

```bash
kubectl apply -f k8s/crd.yaml
kubectl apply -f k8s/operator-rbac.yaml
kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/operator-deployment.yaml
kubectl apply -f k8s/deployment.yaml
```

### LiteLLM 相关资源

LiteLLM proxy 和 PostgreSQL 有独立的 K8s manifests：

```bash
# Secrets（immutable。先把模板里的 CHANGE_ME_* 替换成真实值，再 apply）
cp k8s/litellm-secrets.yaml /tmp/litellm-secrets.rendered.yaml
# 编辑 /tmp/litellm-secrets.rendered.yaml，把所有 CHANGE_ME_* 替换为真实值
kubectl apply -f /tmp/litellm-secrets.rendered.yaml

# Proxy + DB（可反复 apply）
kubectl apply -f k8s/litellm-proxy.yaml
kubectl apply -f k8s/litellm-postgres.yaml
```

> **Immutable Secret 策略**：`k8s/litellm-secrets.yaml` 中的 Secret 标记了 `immutable: true`。
> 首次创建前，必须先把模板中的 `CHANGE_ME_*` 替换成真实值。
> 创建后，后续 apply 不可修改 data 字段。
> 如需变更：`kubectl delete secret <name> -n carher`，重新渲染模板后再 apply。

## K8s Resources

| Resource | File | Notes |
|----------|------|-------|
| Admin Deployment | `k8s/deployment.yaml` | 1 replica, pinned to node `ap-southeast-1.172.16.0.226` (SQLite hostPath), probe TCP:8900 |
| Operator Deployment | `k8s/operator-deployment.yaml` | 2 replicas, Prometheus metrics, probe HTTP:8081 `/healthz` `/readyz` |
| CRD | `k8s/crd.yaml` | HerInstance custom resource (`herinstances.carher.io`) |
| Operator RBAC | `k8s/operator-rbac.yaml` | ClusterRole: herinstances, deployments, services, pods, configmaps, PVCs, secrets, events, namespaces, leases |
| Admin RBAC | `k8s/rbac.yaml` | SA + Role + ClusterRole (`carher-admin-cluster`): pods, configmaps, PVCs, secrets, events, herinstances, nodes, metrics |
| LiteLLM Secrets | `k8s/litellm-secrets.yaml` | `litellm-secrets` + `litellm-db-credentials`，`immutable: true` |
| LiteLLM Proxy | `k8s/litellm-proxy.yaml` | ConfigMap + Deployment + Service，端口 4000 |
| LiteLLM PostgreSQL | `k8s/litellm-postgres.yaml` | StatefulSet + Service，NAS PVC 持久化 |

## Pitfalls

### 1. 禁止本地 Mac 构建

**原因**：Mac 默认 ARM64 架构，构建的镜像在 K8s（amd64）上会 `ImagePullBackOff`。
**正确做法**：始终在构建服务器（`47.84.112.136`）上用 `nerdctl build` 构建。

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
| `buildctl` not found on server | `systemctl status buildkit` | `systemctl restart buildkit` |
| ACR push 401 | `nerdctl login` 过期 | 重新 `nerdctl login`（见上方凭证） |
| ImagePullBackOff | `kubectl describe pod` → image arch | 确认在服务器上构建，不是本地 Mac |
| CrashLoopBackOff | `kubectl logs <pod>` | Fix config, rebuild |
| Connection refused | SSH tunnel down | Re-run SSH tunnel command |
| Pod stuck Pending | `kubectl describe pod` → node affinity | Check nodeSelector and node status |
| Operator not reconciling | `kubectl logs deploy/carher-operator` | Check RBAC, CRD version |
| OOMKilled | `kubectl describe pod` → Last State | Admin: 256Mi-512Mi, Operator: 128Mi-512Mi |
| Probe failed restart | `kubectl describe pod` → Events | Admin: TCP 8900, Operator: HTTP 8081 /healthz |
