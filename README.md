# CarHer Admin — 企业级 Her 实例管理平台

管理 500+ CarHer (飞书 AI 助手) 实例的全生命周期：声明式管理、自动自愈、灰度部署、实时监控。

## 系统架构

```
                    ┌──────────────────────────────────────────────────────────┐
                    │                    GitHub                                │
                    │   push main ──→ Actions ──→ Build Docker ──→ Push ACR   │
                    │                                    │                     │
                    │                              webhook (secret 验证)       │
                    └──────────────────────────────────┬───────────────────────┘
                                                       │
                    ┌──────────────────────────────────▼───────────────────────┐
                    │              Alibaba Cloud ACK (K8s 集群)                │
                    │                                                          │
                    │  ┌──────────────────────────────────────────────────┐    │
                    │  │  carher-admin (Python FastAPI + React)           │    │
                    │  │  Web Dashboard + REST API + SQLite (审计)         │    │
                    │  │         │                                        │    │
                    │  │         │ 操作 CRD (声明式)                       │    │
                    │  │         ▼                                        │    │
                    │  │  ┌─────────────────────────────────────┐        │    │
                    │  │  │  HerInstance CRD (K8s etcd)         │        │    │
                    │  │  │  = source of truth                  │        │    │
                    │  │  └──────────────┬──────────────────────┘        │    │
                    │  └─────────────────┼──────────────────────────────┘    │
                    │                    │ watch                               │
                    │  ┌─────────────────▼──────────────────────────────┐     │
                    │  │  carher-operator (Go, controller-runtime)       │     │
                    │  │  ├── Reconciler (多 goroutine 并发)              │     │
                    │  │  ├── Health Checker (50 worker 并发池)           │     │
                    │  │  ├── KnownBots Manager (goroutine-safe)         │     │
                    │  │  └── Prometheus /metrics (8 指标)                │     │
                    │  └──────────────┬──────────────────────────────────┘     │
                    │                 │                                         │
                    │  ┌──────────────▼──────────────────────────────────┐     │
                    │  │  CarHer 实例 Pods (×500+)                       │     │
                    │  │  每个: Pod + ConfigMap + PVC + Secret            │     │
                    │  └──────────────────────────────────────────────────┘     │
                    │                                                          │
                    │  Prometheus → Grafana → AlertManager → 飞书群告警         │
                    │  Cloudflare Tunnel (*.carher.net → Pod)                  │
                    └──────────────────────────────────────────────────────────┘
```

### 数据流

| 数据 | 存储 | 写入方 | 读取方 |
|------|------|--------|--------|
| 实例配置 (name, model, appId…) | HerInstance CRD (etcd) | admin Dashboard | Operator |
| 实例状态 (phase, feishuWS…) | CRD status | Operator | admin Dashboard |
| appSecret | K8s Secret (加密) | admin / 迁移工具 | Operator |
| knownBots (全局 bot 注册表) | 共享 ConfigMap (1 份) | Operator | 各实例 Pod |
| 每实例运行配置 | per-user ConfigMap | Operator | 各实例 Pod |
| 用户数据 | PVC (NAS 5Gi) | Pod 运行时 | Pod 运行时 |
| 审计日志 + 部署历史 | SQLite (非关键路径) | admin API | admin Dashboard |
| 监控指标 | Prometheus | Operator /metrics | Grafana |

## 四大组件

### 1. carher-admin — Web Dashboard + API

**技术栈**: Python FastAPI + React + Vite + Tailwind CSS + SQLite

| 功能模块 | 功能 |
|----------|------|
| 仪表盘 | 集群概览、节点分布、Pod 统计 |
| 实例管理 | 列表、搜索、详情、配置编辑 |
| 新增/导入 | 表单创建、CSV 批量导入 |
| 生命周期 | 启动、停止、重启、删除 (单个/批量) |
| 部署管理 | 灰度 / 紧急全量 / 仅金丝雀，回滚、中止 |
| 分组管理 | canary / early / stable 拖拽分配 |
| 健康检查 | 飞书 WS、记忆库、模型加载 三项全检 |
| 日志 | 实时 Pod 日志查看 |
| 系统管理 | 强制同步、一致性检查、审计日志 |

### 2. carher-operator (Go) — 核心引擎

**技术栈**: Go + controller-runtime + Prometheus client

| 功能 | 说明 | 性能 |
|------|------|------|
| Reconcile | CRD spec → ConfigMap + PVC + Pod | 多 goroutine 并发 |
| 自愈 | Pod 消失 → 30s 内自动重建 | 无需人工 |
| 健康检查 | 飞书 WS、CrashLoop、重启次数 | 50 worker，500 实例 10s/轮 |
| knownBots | 共享 ConfigMap，自动计算 | 消除 O(N²) |
| Config Hash | 只在配置变更时重建 Pod | 避免无谓重启 |
| Leader Election | 多副本 HA | 内置 |
| /metrics | 8 个 Prometheus 指标 | 15s 采集 |

### 3. CI/CD — GitHub Actions

| 功能 | 说明 |
|------|------|
| 自动构建 | push main → 构建 admin + operator 镜像 → 推送 ACR |
| 自动部署 | webhook 触发灰度部署 |
| 手动触发 | workflow_dispatch：选组件 + 部署模式 |
| 4 种模式 | normal (灰度) / fast (全量) / canary-only / build-only |
| 幂等安全 | 相同 tag 不重复部署，secret 验证 |

### 4. 监控告警 — Prometheus + AlertManager

| 指标 | 说明 |
|------|------|
| `carher_instances_total` | 按 phase + deploy_group 统计 |
| `carher_feishu_ws_connected` | 每实例飞书 WS 状态 |
| `carher_pod_restarts` | 每实例重启次数 |
| `carher_reconcile_duration_seconds` | reconcile 耗时 |
| `carher_health_check_duration_seconds` | 全量健康检查耗时 |
| `carher_self_heal_total` | 自愈次数累计 |

| 告警 | 条件 | 严重性 |
|------|------|--------|
| FeishuDisconnected | 单实例断开 5min | warning |
| MassDisconnect | >10 实例断开 2min | critical |
| HighRestarts | 重启 >5 次 | warning |
| SelfHealSpike | 自愈率 >0.1/s | critical |

## 项目结构

```
├── backend/                   # Python FastAPI 后端
│   ├── main.py               # API 路由 (40+ endpoints)
│   ├── database.py           # SQLite (审计/部署历史)
│   ├── deployer.py           # 灰度部署编排器
│   ├── crd_ops.py            # CRD 操作 (admin → K8s API)
│   ├── k8s_ops.py            # 直接 K8s 操作 (legacy 兼容)
│   ├── config_gen.py         # 配置生成
│   ├── sync_worker.py        # 后台同步
│   └── models.py             # Pydantic 数据模型
├── frontend/                  # React + Vite + Tailwind
│   └── src/components/
│       ├── Dashboard.jsx      # 仪表盘
│       ├── InstanceList.jsx   # 实例列表
│       ├── DeployPage.jsx     # 部署管理
│       ├── AddInstance.jsx    # 新增实例
│       ├── BatchImport.jsx    # 批量导入
│       ├── HealthCheck.jsx    # 健康检查
│       └── AdminPanel.jsx     # 系统管理
├── operator-go/               # Go operator (500+ 规模)
│   ├── api/v1alpha1/         # CRD 类型定义
│   ├── internal/
│   │   ├── controller/       # reconciler + health + knownBots + config
│   │   └── metrics/          # Prometheus 指标定义
│   ├── cmd/main.go           # 入口
│   └── Dockerfile            # 多阶段构建 (Go → Alpine)
├── operator/                  # Python kopf (旧版, 兼容保留)
├── k8s/                       # K8s 部署清单
│   ├── crd.yaml              # HerInstance CRD
│   ├── rbac.yaml             # admin RBAC
│   ├── deployment.yaml       # admin Deployment + Service
│   ├── operator-rbac.yaml    # operator RBAC
│   ├── operator-deployment.yaml  # operator Deployment + metrics Service
│   └── servicemonitor.yaml   # Prometheus 采集规则 + 告警规则
├── .github/workflows/
│   └── build-deploy.yml      # CI/CD
├── Dockerfile                 # admin 多阶段构建
└── deploy.sh                  # 一键部署脚本
```

## API 参考

### 实例管理

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/instances` | 列出所有实例 |
| GET | `/api/instances/:id` | 实例详情 |
| POST | `/api/instances` | 创建实例 |
| PUT | `/api/instances/:id` | 修改配置 |
| DELETE | `/api/instances/:id` | 删除实例 |
| POST | `/api/instances/:id/stop` | 停止 |
| POST | `/api/instances/:id/start` | 启动 |
| POST | `/api/instances/:id/restart` | 重启 |
| GET | `/api/instances/:id/logs` | 查看日志 |
| POST | `/api/instances/batch` | 批量操作 |
| POST | `/api/instances/batch-import` | 批量导入 |
| PUT | `/api/instances/:id/deploy-group` | 设置部署分组 |

### 部署

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/deploy` | 启动部署 (body: `{image_tag, mode}`) |
| GET | `/api/deploy/status` | 当前部署状态 |
| POST | `/api/deploy/continue` | 继续暂停的部署 |
| POST | `/api/deploy/rollback` | 回滚到上一版本 |
| POST | `/api/deploy/abort` | 中止部署 |
| GET | `/api/deploy/history` | 部署历史 |
| POST | `/api/deploy/webhook` | GitHub Actions 自动触发 |

### 系统

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | 集群状态 |
| GET | `/api/health` | 全量健康检查 |
| GET | `/api/next-id` | 下一个可用 ID |
| POST | `/api/sync/force` | 强制全量同步 |
| GET | `/api/sync/check` | 一致性检查 |
| GET | `/api/audit` | 审计日志 |
| POST | `/api/import-from-k8s` | 从 K8s 导入 |

## 本地开发

```bash
# Backend
cd backend
pip install -r requirements.txt
CARHER_ADMIN_DB_DIR=/tmp/carher-admin CARHER_ADMIN_BACKUP_DIR=/tmp/carher-admin-bak \
  uvicorn backend.main:app --reload --port 8900

# Frontend (另一个终端)
cd frontend
npm install
npm run dev
```

## 部署到 K8s

```bash
# 一键部署 admin + operator
./deploy.sh

# 或分步:
kubectl apply -f k8s/crd.yaml              # 安装 CRD
kubectl apply -f k8s/operator-rbac.yaml     # operator 权限
kubectl apply -f k8s/operator-deployment.yaml  # 部署 operator
kubectl apply -f k8s/rbac.yaml              # admin 权限
kubectl apply -f k8s/deployment.yaml        # 部署 admin
kubectl apply -f k8s/servicemonitor.yaml    # Prometheus 监控

# 迁移现有实例到 CRD
python -m operator.migrate --dry-run
python -m operator.migrate
```

## 使用 HerInstance CRD

```bash
# 查看所有实例
kubectl get her -n carher
# NAME     USER   NAME   MODEL   PHASE     FEISHU      GROUP    IMAGE
# her-14   14     张三    gpt     Running   Connected   stable   v20260328

# 新增实例
kubectl apply -f - <<EOF
apiVersion: carher.io/v1alpha1
kind: HerInstance
metadata:
  name: her-301
  namespace: carher
spec:
  userId: 301
  name: "新用户"
  model: gpt
  appId: cli_xxx
  prefix: s1
  deployGroup: canary
EOF

# 更新镜像 (operator 自动重建 Pod)
kubectl patch her her-14 -n carher --type merge -p '{"spec":{"image":"v20260329"}}'

# 暂停实例 (删除 Pod, 保留数据)
kubectl patch her her-14 -n carher --type merge -p '{"spec":{"paused":true}}'
```

## 环境变量

| 变量 | 组件 | 说明 |
|------|------|------|
| `CARHER_ADMIN_DB_DIR` | admin | SQLite 存储路径 |
| `CARHER_ADMIN_BACKUP_DIR` | admin | NAS 备份路径 |
| `DEPLOY_WEBHOOK_SECRET` | admin | GitHub webhook 验证密钥 |
| `FEISHU_DEPLOY_WEBHOOK` | admin | 飞书群 webhook URL (部署通知) |
