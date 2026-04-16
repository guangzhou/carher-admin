---
name: litellm-ops
description: >-
  LiteLLM Proxy 运维：升级镜像、Prisma DB 迁移、故障排查、性能调优。
  Use when the user mentions "litellm" + 升级/部署/502/挂了/故障/重启/schema/prisma/
  探针/OOM/性能/日志级别, or when litellm.carher.net returns 502/503.
---

# LiteLLM Proxy 运维

## 架构概览

| 组件 | K8s 资源 | 镜像 | 端口 |
|------|---------|------|------|
| LiteLLM Proxy | `deploy/litellm-proxy` | `ghcr.io/berriai/litellm` | 4000 |
| PostgreSQL | `sts/litellm-db` | `docker.io/library/postgres` | 5432 |

外部访问：`https://litellm.carher.net` → Cloudflare Tunnel → `svc/litellm-proxy:4000`

清单文件：`k8s/litellm-proxy.yaml`（ConfigMap + Deployment + Service）、`k8s/litellm-postgres.yaml`

## 连接集群

```bash
SSHPASS='5ip0krF>qazQjcvnqc' sshpass -e ssh \
  -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
  -p 1023 -L 16443:172.16.1.163:6443 -N root@47.84.112.136 &
kubectl get nodes  # 验证
```

## DB 凭证

```bash
kubectl get secret litellm-secrets -n carher \
  -o jsonpath='{.data.DATABASE_URL}' | base64 -d
# postgresql://litellm:<password>@litellm-db.carher.svc:5432/litellm
```

---

## 升级 LiteLLM 镜像

### 关键：Prisma Schema 必须同步

LiteLLM 用 Prisma ORM。新版镜像可能新增 DB 列，如果不执行迁移会导致：
`column XXX does not exist` → Prisma 连接池崩溃 → 所有请求 401 → Liveness 探针超时 → CrashLoop → 502

当前部署已包含 initContainer `prisma-migrate`，每次 Pod 启动前自动执行 `prisma db push`。

### 升级流程

1. **在构建服务器拉取新镜像**：
```bash
SSHPASS='5ip0krF>qazQjcvnqc' sshpass -e ssh -p 1023 root@47.84.112.136 \
  "nerdctl pull ghcr.io/berriai/litellm:<new-tag>"
```

2. **推到 ACR**（构建服务器对 `her/litellm-proxy` 无 push 权限，需用 Kaniko Job）：
```bash
# 更新 k8s/litellm-build-job.yaml 中的 --destination tag
# 然后 kubectl apply -f k8s/litellm-build-job.yaml
```

3. **更新 Deployment 镜像**：
```bash
# 修改 k8s/litellm-proxy.yaml 中 initContainers 和 containers 的 image
# 两处必须同步更新为同一个镜像
kubectl apply -f k8s/litellm-proxy.yaml
# 让 K8s 滚动更新，不要手动 delete pod
kubectl rollout status deploy/litellm-proxy -n carher --timeout=300s
```

4. **验证**：
```bash
kubectl get pods -n carher | grep litellm-proxy   # 1/1 Running
kubectl logs <pod> -c prisma-migrate -n carher     # 应显示 "Your database is now in sync"
curl -s -o /dev/null -w "%{http_code}" https://litellm.carher.net/health  # 应返回 401（非 502）
kubectl logs litellm-db-0 -n carher --since=2m | grep ERROR  # 不应有 "column XXX does not exist"
```

---

## 故障排查：502 Bad Gateway

### 快速诊断

```bash
# 1. Pod 状态
kubectl get pods -n carher | grep litellm-proxy

# 2. 看 Events（探针失败、OOM、镜像拉取问题）
kubectl describe pod <pod> -n carher | grep -A15 "Events:"

# 3. Proxy 日志（看 Prisma 错误）
kubectl logs <pod> -n carher --tail=50 | grep -iE "error|column|does not exist|ClientNotConnected"

# 4. DB 日志（看 schema 错误）
kubectl logs litellm-db-0 -n carher --tail=30 | grep ERROR
```

### 常见原因及修复

| 现象 | 原因 | 修复 |
|------|------|------|
| `column XXX does not exist` | 镜像升级后未迁移 DB | 在 Pod 内执行 `prisma db push`（见下方） |
| `ClientNotConnectedError` | Prisma 连接池崩溃 | 修复 schema 后重启 Pod |
| Liveness probe timeout | DEBUG 日志导致 I/O 过高 | 改 `LITELLM_LOG=INFO` |
| initContainer OOMKilled | Prisma 引擎内存不足 | initContainer limits 至少 1536Mi |
| 镜像拉取超时 | 从 ghcr.io 公网拉取慢 | 推到 ACR，用 VPC 地址 |

### 手动执行 Prisma 迁移（紧急修复）

如果 initContainer 不存在或失败，在当前运行的容器内执行：

```bash
kubectl exec <proxy-pod> -n carher -- sh -c \
  'DATABASE_URL="postgresql://litellm:<password>@litellm-db.carher.svc:5432/litellm" \
   prisma db push --schema /app/litellm/proxy/schema.prisma --accept-data-loss'
```

注意：这只修复当前容器，Pod 重启后需要重新执行。应确保 initContainer 正常工作。

### 验证 DB Schema 同步

```bash
# 检查特定列是否存在
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm \
  -c "SELECT column_name FROM information_schema.columns WHERE table_name='LiteLLM_MCPServerTable';"
```

---

## 配置变更

### 模型路由配置

路由配置在 `litellm-config` ConfigMap 中（定义在 `k8s/litellm-proxy.yaml`）。
修改后 `kubectl apply` 并 rollout restart（ConfigMap 变更不会自动触发重启）：

```bash
kubectl apply -f k8s/litellm-proxy.yaml
kubectl rollout restart deploy/litellm-proxy -n carher
kubectl rollout status deploy/litellm-proxy -n carher --timeout=300s
```

### 日志级别

```bash
# 查看当前
kubectl get deploy litellm-proxy -n carher -o jsonpath='{.spec.template.spec.containers[0].env}' | python3 -m json.tool

# 修改（会触发滚动更新）
kubectl set env deploy/litellm-proxy -n carher LITELLM_LOG=INFO
```

生产环境禁止使用 DEBUG 级别（会导致大量日志 I/O，拖慢 health 端点响应）。

### 探针参数

当前配置（在 `k8s/litellm-proxy.yaml` 中）：

```yaml
livenessProbe:
  initialDelaySeconds: 90   # LiteLLM 启动慢，不能低于 90
  periodSeconds: 15
  failureThreshold: 5       # 5 次失败才杀，避免误杀
  timeoutSeconds: 15        # 高负载下 health 端点可能慢
readinessProbe:
  # 同上
```

---

## 监控

```bash
# Pod 资源使用
kubectl top pod -n carher | grep litellm

# DB 大小
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm \
  -c "SELECT pg_size_pretty(pg_database_size('litellm'));"

# 各表大小（SpendLogs 是最大的表）
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm \
  -c "SELECT tablename, pg_size_pretty(pg_total_relation_size('public.\"' || tablename || '\"')) AS size FROM pg_tables WHERE schemaname='public' ORDER BY pg_total_relation_size('public.\"' || tablename || '\"') DESC LIMIT 5;"
```

---

## 零中断操作原则

- **禁止手动 `kubectl delete pod`**，必须依赖 Deployment 滚动更新
- 变更通过 `kubectl apply` 或 `kubectl set image/env`，让 K8s 自动完成：新 Pod Ready → 流量切换 → 旧 Pod 终止
- LiteLLM 有 90s initialDelay，要有耐心等滚动更新完成
- 用 `kubectl rollout status` 监控进度
