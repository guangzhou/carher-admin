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
  → 创建 PVC + Pod
  → 更新 CRD status
```

### Self-Healing

每 30 秒检查所有实例：
- Pod 消失 → 自动重建
- CrashLoopBackOff → 标记 Failed + 告警
- 飞书 WS 断开 → 标记 Disconnected

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
│   │   ├── reconciler.go     # 主 reconciler
│   │   ├── health.go         # 50-worker 并发健康检查
│   │   ├── known_bots.go     # goroutine-safe knownBots 管理
│   │   ├── config_gen.go     # openclaw.json 配置生成
│   │   └── config_gen_test.go # 单元测试
│   └── metrics/
│       └── metrics.go        # Prometheus 指标定义
├── cmd/
│   └── main.go               # 入口 (manager setup)
├── Dockerfile                 # 多阶段: golang:1.23 → alpine:3.21
├── go.mod
└── README.md
```

## 构建

```bash
# Docker (推荐, 无需本地 Go)
docker build -t carher-operator:latest .

# 本地 (需要 Go 1.23+)
go mod tidy
go build -o operator ./cmd/main.go
```

## 部署

```bash
# 1. 安装 CRD
kubectl apply -f ../k8s/crd.yaml

# 2. 部署 operator
kubectl apply -f ../k8s/operator-rbac.yaml
kubectl apply -f ../k8s/operator-deployment.yaml

# 3. 安装 Prometheus 监控
kubectl apply -f ../k8s/servicemonitor.yaml

# 验证
kubectl get pods -n carher -l app=carher-operator
kubectl logs -n carher -l app=carher-operator -f
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
  model: gpt              # gpt | sonnet | opus
  appId: cli_xxx
  appSecretRef: carher-14-secret   # K8s Secret name
  prefix: s3
  owner: "ou_abc|ou_def"
  provider: openrouter     # openrouter | anthropic
  botOpenId: ou_bot123
  deployGroup: canary      # canary | early | stable
  image: v20260328
  paused: false
status:
  phase: Running           # Pending | Running | Failed | Stopped | Paused
  podIP: "10.0.1.50"
  node: "cn-hongkong.10.0.1.226"
  restarts: 0
  feishuWS: Connected      # Connected | Disconnected | Unknown
  lastHealthCheck: "2026-03-28T16:00:00Z"
  configHash: "a1b2c3d4e5f6"
```

```bash
# 常用命令
kubectl get her -n carher                     # 列出所有
kubectl describe her her-14 -n carher         # 详情
kubectl patch her her-14 -n carher --type merge -p '{"spec":{"image":"v20260329"}}'  # 更新镜像
kubectl patch her her-14 -n carher --type merge -p '{"spec":{"paused":true}}'        # 暂停
kubectl delete her her-14 -n carher           # 删除 (PVC 保留)
```
