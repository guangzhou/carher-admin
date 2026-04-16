---
name: carher-admin-deploy
description: >-
  Deploy carher-admin and carher-operator to Alibaba Cloud K8s.
  Use when deploying admin panel or operator code changes, or asking about
  K8s image pull rules, ACR VPC registry, zero-downtime deployment strategy.
  Does NOT touch bot instances (carher main program).
  For LiteLLM proxy operations, see litellm-ops skill.
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
| `cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com` | **VPC 内网**（构建服务器 push + K8s pod pull，优先使用） |
| `cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com` | Public（仅当 VPC 不可达时 fallback） |

构建服务器（`47.84.112.136`）在 VPC 内，**push 和 pull 都走 VPC 内网**，速度更快、更稳定、不消耗公网带宽。

### K8s 镜像拉取规则（全局适用）

- K8s Pod 的镜像**必须通过 ACR VPC 内网**拉取（`cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com`）
- **禁止**在 Deployment/Job 中直接引用公网镜像仓库（如 `ghcr.io`、`docker.io`），公网拉取极慢且不可靠
- 第三方镜像（如 LiteLLM）需先推到 ACR 再引用 VPC 地址：
  1. 在构建服务器（`47.84.112.136:1023`）上 `nerdctl pull` 公网镜像
  2. `nerdctl tag` 为 ACR 格式
  3. 通过 Kaniko Job 或构建服务器 `nerdctl push` 推到 ACR
  4. 注意：构建服务器对 `her/litellm-proxy` 仓库无 push 权限，需用集群内 Kaniko Job 推送
- `imagePullPolicy` 推荐设为 `IfNotPresent`（tag 部署）或明确用 digest（不可变引用）

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

## 零中断部署规则（全局适用）

- **禁止手动 `kubectl delete pod` 正在服务的 Pod**，必须依赖 Deployment 的滚动更新机制
- 滚动更新流程：新 Pod 启动 → Readiness 探针通过 → 流量切到新 Pod → 旧 Pod 才被终止
- 操作变更时使用 `kubectl apply` 或 `kubectl set image`，让 K8s 自动完成滚动
- 如需确认新 Pod 正常后再继续，使用 `kubectl rollout status` 监控，而不是手动杀旧 Pod
- 对于启动慢的服务（如 LiteLLM 有 90s initialDelaySeconds），要有耐心等待

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
ACR_VPC='cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her'

# 构建 + 推送 admin（走 VPC 内网）
nerdctl build -t \$ACR_VPC/carher-admin:\$TAG . && \
nerdctl push \$ACR_VPC/carher-admin:\$TAG

# 构建 + 推送 operator（如果有 operator 代码变更）
nerdctl build -t \$ACR_VPC/carher-operator:\$TAG ./operator-go && \
nerdctl push \$ACR_VPC/carher-operator:\$TAG

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

### Step 3.5：确认 admin 带上 Cloudflare token

`carher-admin` 创建新实例时，需要用 `CLOUDFLARE_API_TOKEN` 去更新远程 tunnel ingress。
如果这个 token 缺失，新实例 callback URL 会 `404`，现在 API 会直接返回 `503`。

```bash
# secret 里必须有 cloudflare-api-token
kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.cloudflare-api-token}' | base64 -d

# admin Pod 里环境变量必须为 true
kubectl exec -n carher deploy/carher-admin -- \
  python -c "import os; print(bool(os.environ.get('CLOUDFLARE_API_TOKEN')))"
```

如果你刚修改了 `carher-admin-secrets`，必须再执行一次：

```bash
kubectl rollout restart deploy/carher-admin -n carher
kubectl rollout status deploy/carher-admin -n carher --timeout=120s
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
# VPC 内网（优先，构建服务器和 K8s 都在 VPC 内）
nerdctl login --username='liuguoxian@1989403661820148' \
  --password='cltx!@#456' \
  cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com

# Public（仅 fallback）
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

> **LiteLLM 的升级、故障排查、性能调优请使用 `litellm-ops` skill。**

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

### 2. ACR Registry Endpoint

构建服务器在 VPC 内，push 和 pull 统一用 VPC endpoint。
如果 VPC endpoint push 失败（`nerdctl login` 未配置 VPC），需先登录：
```bash
nerdctl login --username='liuguoxian@1989403661820148' \
  --password='cltx!@#456' \
  cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com
```

### 3. rollout restart Doesn't Update Code

**Symptom**: `kubectl rollout restart` but pod still runs old code.
**Fix**: Deployment uses a fixed image tag. Must `kubectl set image` with new tag.

### 4. Admin Pod Pinned to Node

Admin uses `nodeSelector` pinned to `ap-southeast-1.172.16.0.226` (SQLite hostPath).
If this node is unavailable, admin pod cannot be scheduled.

### 5. DB Schema Version Mismatch After Rollback

**Symptom**: Logs show `schema v7` after a revert, but the previous deploy was `v8`.
**Fix**: Always deploy forward. SQLite data on hostPath persists across deploys.

### 6. Cloudflare Token Missing on Admin

**Symptom**: `POST /api/instances` or `/api/instances/batch-import` returns `503` mentioning `CLOUDFLARE_API_TOKEN`.
**Root cause**: `carher-admin-secrets` lacks `cloudflare-api-token`, or the secret was updated but `carher-admin` was not restarted.
**Fix**: patch the secret, rollout restart `deploy/carher-admin`, then retry the create request.

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
