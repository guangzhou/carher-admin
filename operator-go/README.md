# CarHer Operator (Go)

Kubernetes Operator for managing 500+ CarHer instances with self-healing, concurrent health checks, and Prometheus metrics.

## Why Go (not Python kopf)

| | kopf (Python) | Go (controller-runtime) |
|--|---------------|------------------------|
| 500 实例健康检查 | 250 min/轮 (串行) | **10 sec/轮 (50 并发)** |
| 内存 | ~200 MB | ~30 MB |
| 并发 reconcile | 单线程 | 多 goroutine |
| Leader election | 无 | 内置 |
| 类型安全 | dict | struct |
| 社区 | 小 | CNCF 标准 |

## 功能

### Reconciler

监听 `HerInstance` CRD 变化，自动管理子资源：

```
CRD spec 变化
  → 读取 appSecret (K8s Secret)
  → 生成 openclaw.json (含全局 knownBots)
  → apply ConfigMap
  → 比较 image / configHash → 决定是否重建 Pod
  → 确保 PVC 存在 + 创建 Pod (挂载 7 个 volume)
  → 更新 CRD status
```

### Self-Healing

每 30 秒检查所有实例：
- Pod 消失 → 自动重建（所有 volume 完整恢复）
- CrashLoopBackOff → 标记 Failed + 告警
- 飞书 WS 断开 → 标记 Disconnected

### Pod Volume 架构

每个 Pod 挂载 7 个 volume，确保自愈后数据完整：

| Volume | 类型 | 说明 |
|--------|------|------|
| `user-data` | PVC `carher-{uid}-data` (NAS) | 用户私有数据 (记忆、OAuth token 等) |
| `user-config` | ConfigMap | openclaw.json (Operator 从 CRD 生成) |
| `base-config` | ConfigMap | 共享基础配置 |
| `gcloud-adc` | Secret | GCloud 认证 |
| `shared-skills` | PVC `carher-shared-skills` (NAS, ReadWriteMany) | 全员共享 skills |
| `dept-skills` | PVC `carher-dept-skills` (NAS, ReadWriteMany) | 部门共享 skills |
| `user-sessions` | PVC `carher-shared-sessions` (NAS, ReadWriteMany) | Session 日志 (按 uid 子目录隔离) |

Skills 和 sessions 使用 NAS PVC (ReadWriteMany) 而非 hostPath，确保 Pod 无论调度到哪个节点都能读到完整数据。

### knownBots 中心化

所有 bot ID 存在一个共享 ConfigMap `carher-known-bots`：
- 新增/删除 bot → 自动重建共享 ConfigMap
- 各实例 ConfigMap 从缓存获取 knownBots
- 消除 O(N²) 问题

### Prometheus Metrics

| 指标 | 说明 |
|------|------|
| `carher_instances_total` | 按 phase + deploy_group |
| `carher_feishu_ws_connected` | 飞书 WS 状态 (0/1) |
| `carher_pod_restarts` | Pod 重启次数 |
| `carher_reconcile_duration_seconds` | reconcile 耗时 |
| `carher_health_check_duration_seconds` | 健康检查周期耗时 |
| `carher_known_bots_total` | knownBots 总数 |
| `carher_deploy_active` | 是否有活跃部署 |
| `carher_self_heal_total` | 自愈次数 |

## 项目结构

```
operator-go/
├── api/v1alpha1/
│   └── types.go              # HerInstance CRD 类型定义
├── internal/
│   ├── controller/
│   │   ├── reconciler.go     # 主 reconciler (CRD → ConfigMap + PVC + Pod)
│   │   ├── health.go         # 50-worker 并发健康检查 + 自愈
│   │   ├── known_bots.go     # goroutine-safe knownBots 管理
│   │   ├── config_gen.go     # openclaw.json 配置生成
│   │   └── config_gen_test.go # 单元测试
│   └── metrics/
│       └── metrics.go        # Prometheus 指标定义 + 注册
├── cmd/
│   └── main.go               # 入口 (controller-runtime manager setup)
├── Dockerfile                 # 多阶段: golang:1.23-alpine → alpine:3.21
├── go.mod / go.sum
└── README.md
```

## 构建

```bash
# Docker (推荐, 无需本地 Go 环境)
docker build -t carher-operator:latest .

# 本地 (需要 Go 1.23+)
go mod tidy
go build -o operator ./cmd/main.go
```

## 部署

```bash
# 1. 安装 CRD + 共享 PVC
kubectl apply -f ../k8s/crd.yaml
kubectl apply -f ../k8s/shared-pvcs.yaml

# 2. 部署 operator
kubectl apply -f ../k8s/operator-rbac.yaml
kubectl apply -f ../k8s/operator-deployment.yaml

# 3. 安装 Prometheus 监控
kubectl apply -f ../k8s/servicemonitor.yaml

# 验证
kubectl get pods -n carher -l app=carher-operator
kubectl logs -n carher -l app=carher-operator -f

# 查看 metrics
kubectl port-forward -n carher svc/carher-operator-metrics 8080:8080
curl http://localhost:8080/metrics | grep carher_
```

## 测试

```bash
go test ./internal/controller/ -v
```

## HerInstance CRD

```yaml
apiVersion: carher.io/v1alpha1
kind: HerInstance
metadata:
  name: her-14
  namespace: carher
spec:
  userId: 14
  name: "张三"
  model: gpt                    # gpt | sonnet | opus
  appId: cli_xxx
  appSecretRef: carher-14-secret # K8s Secret name (存 app_secret)
  prefix: s3                     # 服务器前缀 (影响 OAuth URL)
  owner: "ou_abc|ou_def"         # 飞书 open_id (竖线分隔多个)
  provider: openrouter           # openrouter | anthropic
  botOpenId: ou_bot123
  deployGroup: canary            # 任意自定义分组名 (按 priority 排序部署)
  image: v20260328               # ACR 镜像 tag
  paused: false                  # true → Operator 不维护 Pod
status:
  phase: Running                 # Pending | Running | Failed | Stopped | Paused
  podIP: "10.0.1.50"
  node: "cn-hongkong.10.0.1.226"
  restarts: 0
  feishuWS: Connected            # Connected | Disconnected | Unknown
  memoryDB: true
  lastHealthCheck: "2026-03-28T16:00:00Z"
  configHash: "a1b2c3d4e5f6"    # ConfigMap 内容 hash, 变更时才重建 Pod
```

### 常用 kubectl 命令

```bash
# 列出所有实例
kubectl get her -n carher

# 查看详情
kubectl describe her her-14 -n carher

# 更新镜像 (operator 自动重建 Pod)
kubectl patch her her-14 -n carher --type merge -p '{"spec":{"image":"v20260329"}}'

# 暂停实例 (删除 Pod, 保留数据)
kubectl patch her her-14 -n carher --type merge -p '{"spec":{"paused":true}}'

# 恢复实例
kubectl patch her her-14 -n carher --type merge -p '{"spec":{"paused":false}}'

# 移动到自定义灰度组
kubectl patch her her-14 -n carher --type merge -p '{"spec":{"deployGroup":"vip"}}'

# 删除实例 (PVC 保留)
kubectl delete her her-14 -n carher

# 批量查看状态
kubectl get her -n carher -o wide
kubectl get her -n carher -o json | jq '.items[] | {name: .metadata.name, phase: .status.phase, ws: .status.feishuWS, group: .spec.deployGroup}'
```

### 灰度分组说明

`deployGroup` 字段不限于固定值，支持任意自定义名称（如 `vip`, `test`, `team-a`）。部署编排器按 priority 从小到大逐组部署：

```
vip(P5) → canary(P10) → early(P50) → stable(P100)
```

分组的 priority 由 admin Dashboard 的 `deploy_groups` 表管理，Operator 本身不关心分组语义，只负责维护 Pod 生命周期。
