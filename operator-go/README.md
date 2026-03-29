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
  → 更新 CRD status (Patch, 减少 conflict)
```

**关键改进 (R1–R6)**:
- `Owns(&corev1.Pod{})`: Pod 被删除/驱逐时立即触发 reconcile，不再等 30s 健康检查
- `ownerReferences`: Pod 关联到 CRD，K8s GC 自动清理孤儿资源
- ConfigMap hash 跳过: `configHash` 相同时不写 ConfigMap，500 实例省 500 次 API 调用/轮
- knownBots 按需重建: 仅 `configHash` 变更时 `MarkDirty()`，不每次 reconcile 重建
- `resolveImage` / `resolvePrefix` helper: 统一默认值处理，消除空值比较导致的无限 Pod 重建循环
- Pod 重建不再使用 `time.Sleep(2s)` 阻塞 worker，改为 `AlreadyExists` 时 `RequeueAfter: 3s`
- `deletePod`/`deleteConfigMap` 返回 error 并由调用方处理
- Status 更新使用 `Patch` 替代 `Update`，减少高并发下的 conflict
- 实例删除时清理 Prometheus label (`FeishuWSConnected`/`PodRestarts`)，防止指标基数膨胀
- `DeepCopy` 方法定义在 `api/v1alpha1/types.go`（与 CRD 类型同包，确保编译通过）

### Self-Healing

双重机制确保秒级自愈：

1. **事件驱动** (`Owns(&Pod{})`): Pod 被删除/驱逐 → 立即触发 reconcile → 重建 Pod
2. **定期巡检** (健康检查): 每 30 秒检查所有实例，兜底发现异常

- Pod 消失 → 自动重建（所有 NAS volume 完整恢复）
- CrashLoopBackOff → 标记 Failed + 告警
- Container not Ready → 标记 Disconnected
- `SelfHealTotal` 仅在 Phase 从非 Pending 转换时计数，不每轮重复递增

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

所有共享 PVC 统一使用 `alibabacloud-cnfs-nas` StorageClass，确保 Pod 无论调度到哪个节点都能读到完整数据。

### Health Checker

- **可配 worker 并发池**: 默认 50 worker，可通过 `HEALTH_CHECK_WORKERS` 环境变量调整
- **Container Ready Status**: 用容器 ready 状态判断 Feishu WS 连接性
- **Status Patch**: 使用 `client.MergeFrom` + `Status().Patch()` 替代 `Status().Update()`，减少 conflict
- **Metrics 准确性**: 每轮 atomic 收集后一次性设置 `InstancesTotal`，不再 `Reset()` 造成 Prometheus 零值间隙

### knownBots 中心化

所有 bot ID 存在一个共享 ConfigMap `carher-known-bots`：
- **按需重建**: 仅当 `configHash` 变更时 `MarkDirty()`，不每次 reconcile 重建
- **错误处理**: ConfigMap Create/Update 失败时记录日志并标记重试
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
| `carher_self_heal_total` | 自愈次数 (仅状态转换时计数) |

## 项目结构

```
operator-go/
├── api/v1alpha1/
│   └── types.go              # CRD 类型 + DeepCopy (spec/status 不冗余赋值)
├── internal/
│   ├── controller/
│   │   ├── reconciler.go     # Owns(Pod) + ownerRef + hash 跳过 + resolveImage
│   │   ├── health.go         # 可配 worker 池 + SelfHeal 去重 + atomic 指标
│   │   ├── known_bots.go     # goroutine-safe + 错误处理 + 按需重建
│   │   ├── config_gen.go     # openclaw.json 生成 + resolvePrefix 统一
│   │   └── config_gen_test.go # 单元测试
│   └── metrics/
│       └── metrics.go        # 7 个 Prometheus 指标 (已移除 DeployActive)
├── cmd/
│   └── main.go               # metricsserver.Options + leader election
├── Dockerfile                 # 缓存优化 + -ldflags -s -w + alpine:3.21
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
