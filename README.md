# CarHer Admin — 企业级 Her 实例管理平台

管理 500+ CarHer (飞书 AI 助手) 实例的全生命周期：声明式管理、自动自愈、灰度部署、实时监控。

## 系统架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                         GitHub Actions                               │
│  main push ──→ build-deploy.yml ──→ Build Docker ──→ Push ACR       │
│  feature br ──→ feature-branch.yml ──→ Build ──→ canary-only deploy │
│                                          │                           │
│                                    webhook (secret 验证)             │
└──────────────────────────────────────┬───────────────────────────────┘
                                       │
┌──────────────────────────────────────▼───────────────────────────────┐
│                  Alibaba Cloud ACK (K8s 集群)                        │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────┐      │
│  │  carher-admin (Python FastAPI + React)                     │      │
│  │  ├── Web Dashboard + REST API (60+, OpenAPI 文档)           │      │
│  │  ├── SQLite (审计日志 / 部署历史 / 灰度分组)                   │      │
│  │  ├── AI Agent (自然语言运维, 中英文)                          │      │
│  │  ├── API Key 认证 (写操作需 X-API-Key)                      │      │
│  │  └── 灰度部署编排器 (动态 wave, 波次恢复)                     │      │
│  │         │                                                  │      │
│  │         │ CRUD HerInstance CRD (声明式)                     │      │
│  │         ▼                                                  │      │
│  │  ┌─────────────────────────────────────────────┐          │      │
│  │  │  HerInstance CRD (K8s etcd)                 │          │      │
│  │  │  = source of truth for runtime              │          │      │
│  │  └──────────────────┬──────────────────────────┘          │      │
│  └─────────────────────┼─────────────────────────────────────┘      │
│                        │ watch                                       │
│  ┌─────────────────────▼─────────────────────────────────────┐      │
│  │  carher-operator (Go, controller-runtime)                  │      │
│  │  ├── Reconciler: CRD → ConfigMap + PVC + Pod               │      │
│  │  │   ├── Owns(&Pod{}): Pod 删除/驱逐自动触发 reconcile     │      │
│  │  │   ├── ownerReferences: K8s GC 自动清理孤儿 Pod          │      │
│  │  │   └── ConfigMap hash 跳过: 无变更不写, 省 API 调用       │      │
│  │  ├── Health Checker: 可配 worker 池 (默认 50, env 可调)    │      │
│  │  │   └── SelfHealTotal 仅首次计数, 不每 30s 重复             │      │
│  │  ├── KnownBots Manager: goroutine-safe, 仅 config 变更重建  │      │
│  │  ├── Self-Heal: Pod 消失 → 立即 reconcile (NAS 完整恢复)   │      │
│  │  ├── Status Patch (减少 conflict) + 指标清理 (删除时)       │      │
│  │  └── Prometheus /metrics (7 指标)                           │      │
│  └─────────────────────┬─────────────────────────────────────┘      │
│                        │ manages                                     │
│  ┌─────────────────────▼─────────────────────────────────────┐      │
│  │  CarHer 实例 Pods (×500+)                                  │      │
│  │  每个: Pod + ConfigMap + PVC + Secret  (7 volume mounts)   │      │
│  │  共享: NAS PVC (skills) + NAS PVC (sessions)               │      │
│  └────────────────────────────────────────────────────────────┘      │
│                                                                      │
│  Prometheus → Grafana → AlertManager → 飞书群告警                     │
│  Cloudflare Tunnel (*.carher.net → Pod)                              │
└──────────────────────────────────────────────────────────────────────┘
```

### 数据流

| 数据 | 存储位置 | 写入方 | 读取方 |
|------|---------|--------|--------|
| 实例配置 (name, model, appId…) | HerInstance CRD (etcd) | admin Dashboard | Operator |
| 实例状态 (phase, feishuWS…) | CRD status | Operator | admin Dashboard |
| appSecret | K8s Secret (加密) | admin / 迁移工具 | Operator |
| knownBots (全局 bot 注册表) | 共享 ConfigMap (1 份) | Operator | 各实例 Pod |
| 每实例运行配置 | per-user ConfigMap | Operator | 各实例 Pod |
| 用户数据 (记忆/会话) | PVC `carher-{uid}-data` (NAS 5Gi) | Pod 运行时 | Pod 运行时 |
| Skills (全员共享) | NAS PVC `carher-shared-skills` (ReadWriteMany) | 管理员 | 各实例 Pod |
| Skills (部门共享) | NAS PVC `carher-dept-skills` (ReadWriteMany) | 管理员 | 各实例 Pod |
| Session 日志 | NAS PVC `carher-shared-sessions` (ReadWriteMany, 按 uid 子目录) | Pod 运行时 | Pod 运行时 |
| 审计日志 + 部署历史 | SQLite (非关键路径, hostPath + NAS 备份) | admin API | admin Dashboard |
| 监控指标 | Prometheus | Operator /metrics | Grafana |
| 灰度分组配置 | SQLite `deploy_groups` 表 | admin API | 部署编排器 |

## 六大功能模块

### 1. carher-admin — Web Dashboard + API

**技术栈**: Python 3.12 FastAPI + React + Vite + Tailwind CSS + SQLite

| 功能模块 | 功能 |
|----------|------|
| 仪表盘 | 集群概览、节点分布、Pod 统计 |
| 实例管理 | 列表、搜索、详情、配置编辑 |
| 新增/导入 | 表单创建、CSV 批量导入 |
| 生命周期 | 启动、停止、重启、删除 (单个/批量) |
| 部署管理 | 灰度 / 紧急全量 / 仅首组，回滚、中止、波次恢复 |
| 灰度分组 | 自定义分组 CRUD (名称 + 优先级)，拖拽分配实例，按 priority 排序部署 |
| 健康检查 | 飞书 WS、记忆库、模型加载 三项全检 |
| 日志 | 实时 Pod 日志查看 |
| 系统管理 | 强制同步、一致性检查、审计日志 |

### 2. carher-operator (Go) — 核心引擎

**技术栈**: Go 1.23 + controller-runtime + Prometheus client

| 功能 | 说明 | 性能 |
|------|------|------|
| Reconcile | CRD spec → ConfigMap + PVC + Pod，事件驱动 (无轮询) | 多 goroutine 并发 |
| Owns(&Pod{}) | Pod 被删除/驱逐 → 立即触发 reconcile 重建 | 秒级自愈 |
| ownerReferences | Pod 关联到 CRD，K8s GC 自动清理孤儿资源 | 无需手动 |
| ConfigMap 跳过 | hash 相同时不写 ConfigMap，省去 Get+Update API 调用 | 500 实例省 500 次调用 |
| 自愈 | Pod 消失 → 立即重建，NAS volume 完整恢复 | SelfHeal 仅首次计数 |
| 健康检查 | Container Ready + CrashLoop + 重启次数 | 可配 worker (env `HEALTH_CHECK_WORKERS`，默认 50) |
| knownBots | 共享 ConfigMap，仅 config 变更时重建 | 消除 O(N²)，不每次 reconcile 重建 |
| Config Hash | 只在配置变更时重建 Pod + 写 ConfigMap | 避免无谓重启和 API 调用 |
| Pod 重建 | AlreadyExists 自动 requeue，不阻塞 worker | 无 sleep 阻塞 |
| Status 更新 | Patch 替代 Update，减少 conflict | 高并发安全 |
| 指标清理 | 实例删除时移除 FeishuWSConnected/PodRestarts 标签 | 防止指标基数膨胀 |
| 默认值统一 | `resolveImage` / `resolvePrefix` helper 避免空值比较 bug | 消除无限重建循环 |
| Leader Election | 多副本 HA (2 replicas) | 内置 |
| /metrics | 7 个 Prometheus 指标 | metricsserver API (v0.20) |

### 3. CI/CD — GitHub Actions

两条 workflow 分工明确：

| Workflow | 触发方式 | 用途 |
|----------|---------|------|
| `build-deploy.yml` | push main / 手动 | 正式构建 + 自动灰度部署 |
| `feature-branch.yml` | 手动 (任意分支) | Feature branch 快速验证 |

**build-deploy.yml** (正式发布)：

| 功能 | 说明 |
|------|------|
| 自动构建 | push main → 构建 admin + operator 镜像 → 推送 ACR |
| 自动部署 | webhook 触发灰度部署 (可配 4 种模式) |
| 手动触发 | workflow_dispatch：选组件 (all/admin/operator) + 部署模式 |
| 4 种模式 | `normal` (灰度) / `fast` (全量) / `canary-only` / `build-only` |
| 幂等安全 | 相同 tag 不重复部署，secret 验证 |
| 金丝雀验证 | normal 模式 15s 间隔轮询 (最多 90s)，而非固定 sleep |

**feature-branch.yml** (开发者快速验证)：

| 功能 | 说明 |
|------|------|
| 任意分支构建 | 从 GitHub UI 选择分支，手动 Run workflow |
| 构建目标 | `carher-image` (Her 用户镜像) / `admin` / `operator` |
| Tag 格式 | `dev-{branch}-{sha7}` (与正式 `v{date}-{sha}` 隔离) |
| 金丝雀部署 | 勾选 "Deploy to canary" 自动部署到金丝雀组验证 |

> **开发者使用流程**: 推送 feature 分支 → GitHub Actions 页面选 Feature Branch Build → 选分支和目标 → 构建完成后自动部署到金丝雀组 → 在 admin dashboard 查看验证结果

### 4. 灰度部署分组

支持完全自定义的灰度分组，不再限于固定的 canary/early/stable：

| 功能 | 说明 |
|------|------|
| 自定义分组 | 任意名称 (如 `vip`, `test`, `team-a`)，每个分组有 priority 值 |
| 部署顺序 | 按 priority 从小到大逐组部署，每组之间自动健康检查 |
| 内置分组 | `canary` (P10) → `early` (P50) → `stable` (P100)，可自由增删改 |
| 实例分配 | 支持单个/批量将实例移入分组 |
| 前端管理 | Dashboard 可视化创建/删除分组、拖拽分配实例 |
| `stable` 保护 | `stable` 组不可删除，删除其他组时实例自动回归 stable |
| 波次恢复 | `continue` 从暂停的波次恢复，不重放已完成波次 |

示例：把董事长放入 `vip` 组 (priority=5)，灰度部署时 VIP 组最先更新：
```
vip(P5) → canary(P10) → early(P50) → stable(P100)
```

### 5. 安全机制

| 层面 | 机制 | 说明 |
|------|------|------|
| API 认证 | `X-API-Key` Header | 所有写操作 (POST/PUT/DELETE) 需携带 API Key，读操作可选 |
| Webhook 认证 | `DEPLOY_WEBHOOK_SECRET` | GitHub Actions webhook 独立密钥验证 |
| CORS | 可配白名单 | `CORS_ALLOW_ORIGINS` 环境变量，默认仅 `admin.carher.net` |
| Pod Exec | 命令白名单 | 仅允许 `ls`/`cat`/`grep`/`ps` 等诊断命令 (500 字符上限) |
| Secret 存储 | K8s Secret | appSecret 不存 CRD (明文)，通过独立 Secret 注入 |
| 容器运行 | 非 root 用户 | Dockerfile 使用 `carher` 用户运行 |
| Agent 安全 | dry_run + 确认 | 破坏性操作需确认，批量 >10 先汇报计划 |

### 6. 监控告警 — Prometheus + AlertManager

| 指标 | 说明 |
|------|------|
| `carher_instances_total` | 按 phase + deploy_group 统计 |
| `carher_feishu_ws_connected` | 每实例飞书 WS 状态 (0/1) |
| `carher_pod_restarts` | 每实例重启次数 |
| `carher_reconcile_duration_seconds` | reconcile 耗时 |
| `carher_health_check_duration_seconds` | 全量健康检查耗时 |
| `carher_known_bots_total` | knownBots 总数 |
| `carher_self_heal_total` | 自愈次数累计 (仅状态转换时计数，不重复递增) |

| 告警 | 条件 | 严重性 |
|------|------|--------|
| FeishuDisconnected | 单实例断开 5min | warning |
| MassDisconnect | >10 实例断开 2min | critical |
| HighRestarts | 重启 >5 次 | warning |
| HealthCheckSlow | 健康检查 >60s | warning |
| SelfHealSpike | 自愈率 >0.1/s | critical |

## 自愈数据连续性保证

Pod 崩溃 / 节点故障后，Operator 自动重建 Pod。以下数据保证完整恢复：

| 数据类别 | 存储方式 | 跨节点恢复 | 说明 |
|---------|---------|-----------|------|
| 用户会话/记忆 | PVC `carher-{uid}-data` (NAS) | **是** | 独立 PVC，Pod 重建后自动挂载 |
| 运行配置 | ConfigMap (Operator 管理) | **是** | 从 CRD spec 实时生成，config hash 保证一致 |
| appSecret | K8s Secret | **是** | etcd 存储，Pod 通过 Secret 读取 |
| 全员 Skills | NAS PVC `carher-shared-skills` | **是** | ReadWriteMany，所有节点共享 |
| 部门 Skills | NAS PVC `carher-dept-skills` | **是** | ReadWriteMany，所有节点共享 |
| Session 日志 | NAS PVC `carher-shared-sessions` | **是** | 按 uid 子目录隔离 |
| knownBots | 共享 ConfigMap | **是** | Operator 定期重算 |
| Feishu OAuth Token | PVC 内 `/data/.openclaw/credentials/` | **是** | 随用户数据 PVC |

**关键设计**: 所有共享 PVC 统一使用 `alibabacloud-cnfs-nas` StorageClass (`ReadWriteMany`)，确保 Pod 无论调度到哪个节点都能读到完整数据。

### Pod Volume 挂载详情

每个 Her 实例 Pod 挂载以下 7 个 volume：

| Volume | 类型 | 挂载路径 | 说明 |
|--------|------|---------|------|
| `user-data` | PVC `carher-{uid}-data` | `/data/.openclaw` | 用户私有数据 (记忆、token 等) |
| `user-config` | ConfigMap `carher-{uid}-user-config` | `/data/.openclaw/openclaw.json` | 运行配置 (Operator 生成) |
| `base-config` | ConfigMap `carher-base-config` | `/data/.openclaw/carher-config.json` | 共享基础配置 |
| `gcloud-adc` | Secret `carher-gcloud-adc` | `/gcloud/application_default_credentials.json` | GCloud 认证 |
| `shared-skills` | PVC `carher-shared-skills` (NAS) | `/data/.openclaw/skills` | 全员共享 skills (只读) |
| `dept-skills` | PVC `carher-dept-skills` (NAS) | `/data/.agents/skills` | 部门共享 skills (只读) |
| `user-sessions` | PVC `carher-shared-sessions` (NAS) | `/data/.openclaw/sessions` | Session 日志 (按 uid 隔离) |

## 项目结构

```
carher-admin/
├── backend/                     # Python FastAPI 后端
│   ├── main.py                 # API 路由 (60+, API Key 认证, 非阻塞)
│   ├── agent.py                # AI 运维 Agent (LLM → 工具调用, 错误汇报)
│   ├── database.py             # SQLite (schema v3, deploy_group 持久化)
│   ├── deployer.py             # 灰度部署编排器 (动态 wave, 波次恢复)
│   ├── crd_ops.py              # CRD 操作 (admin → K8s API)
│   ├── k8s_ops.py              # 直接 K8s 操作 (legacy 兼容)
│   ├── config_gen.py           # openclaw.json 配置生成 (knownBots 缓存)
│   ├── sync_worker.py          # 后台同步 + NAS 备份
│   ├── models.py               # Pydantic 模型 (含 OpenAPI schema)
│   └── requirements.txt        # Python 依赖
├── frontend/                    # React + Vite + Tailwind
│   └── src/
│       ├── api.js              # API 客户端 (超时 30s, 非 JSON 防御)
│       ├── App.jsx             # 主应用 (URL query 同步 tab/detailId)
│       └── components/
│           ├── Dashboard.jsx    # 仪表盘
│           ├── InstanceList.jsx # 实例列表
│           ├── DeployPage.jsx   # 部署管理 + 分组管理 (波次状态修复)
│           ├── AddInstance.jsx  # 新增实例
│           ├── BatchImport.jsx  # 批量导入
│           ├── HealthCheck.jsx  # 健康检查
│           └── AdminPanel.jsx   # 系统管理
├── operator-go/                 # Go Operator (500+ 规模)
│   ├── api/v1alpha1/types.go   # CRD 类型定义 + DeepCopy (spec/status 不冗余)
│   ├── internal/
│   │   ├── controller/
│   │   │   ├── reconciler.go   # 主 reconciler (Owns Pod, ownerRef, hash 跳过)
│   │   │   ├── health.go       # 可配 worker 并发健康检查 (SelfHeal 去重)
│   │   │   ├── known_bots.go   # goroutine-safe knownBots (Create/Update 错误处理)
│   │   │   ├── config_gen.go   # openclaw.json 配置生成 (resolvePrefix 统一)
│   │   │   └── config_gen_test.go
│   │   └── metrics/metrics.go  # Prometheus 7 指标 (已移除 DeployActive)
│   ├── cmd/main.go             # 入口 (metricsserver.Options, leader election)
│   ├── Dockerfile              # 多阶段构建 (缓存优化, -ldflags -s -w)
│   ├── go.mod / go.sum
│   └── README.md               # Go Operator 详细文档
├── operator/                    # Python kopf Operator (旧版, 兼容保留)
├── k8s/                         # K8s 部署清单
│   ├── crd.yaml                # HerInstance CRD 定义
│   ├── rbac.yaml               # admin RBAC (ServiceAccount + Role + Binding)
│   ├── deployment.yaml         # admin Deployment + PVC + Service (512Mi)
│   ├── operator-rbac.yaml      # operator RBAC (ClusterRole + Binding)
│   ├── operator-deployment.yaml # operator Deployment + metrics Service
│   ├── shared-pvcs.yaml        # 共享 NAS PVC (alibabacloud-cnfs-nas)
│   └── servicemonitor.yaml     # Prometheus ServiceMonitor + AlertRules
├── .github/workflows/
│   ├── build-deploy.yml        # CI/CD (轮询验证, 条件修复)
│   └── feature-branch.yml     # Feature branch 构建 + 金丝雀部署
├── .cursor/skills/
│   └── carher-admin-api/SKILL.md # Cursor 编程调用技能
├── Dockerfile                   # admin 多阶段构建 (非 root, npm ci)
└── deploy.sh                    # 本地一键部署脚本 (参数解析修复)
```

## API 参考

> **OpenAPI Schema**: `GET /openapi.json` — 完整的 JSON Schema，可被 Cursor、Postman、代码生成器等直接消费。
>
> **Cursor Skill**: `.cursor/skills/carher-admin-api/SKILL.md` — 含所有 API 的 curl 示例。
>
> **认证**: 写操作需携带 `X-API-Key` Header (环境变量 `ADMIN_API_KEY`)。读操作无需认证。Webhook 使用独立的 `DEPLOY_WEBHOOK_SECRET`。

### 实例管理

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/instances` | 列出所有实例 (含 Pod 运行状态) |
| GET | `/api/instances/search` | **搜索** — 按 status/model/deploy_group/owner/name/feishu_ws 过滤 |
| GET | `/api/instances/:id` | 实例详情 (含 PVC 状态, knownBots 计数, 30s 缓存) |
| POST | `/api/instances` | 创建实例 (deploy_group 正确持久化) |
| PUT | `/api/instances/:id` | 修改配置 (**全字段**: name/model/owner/provider/prefix/deploy_group) |
| DELETE | `/api/instances/:id?purge=false` | 删除实例 (purge=true 同时删除 PVC) |
| POST | `/api/instances/:id/stop` | 停止 (删 Pod, 保留数据) |
| POST | `/api/instances/:id/start` | 启动 |
| POST | `/api/instances/:id/restart` | 重启 (async, 不阻塞事件循环) |
| GET | `/api/instances/:id/logs?tail=200` | 查看 Pod 日志 |
| GET | `/api/instances/:id/events` | **K8s Events** (Pod 创建/重启/OOM 等事件) |
| GET | `/api/instances/:id/config-preview` | **配置预览** — 生成但不应用 openclaw.json |
| GET | `/api/instances/:id/config-current` | **当前配置** — 已应用的 ConfigMap 内容 |
| POST | `/api/instances/:id/exec` | **Pod Exec** — 白名单命令 (ls/cat/grep/ps 等, 500 字符上限) |
| POST | `/api/instances/batch` | 批量操作 (body: `{ids, action, params}`) |
| POST | `/api/instances/batch-import` | 批量导入 |
| PUT | `/api/instances/:id/deploy-group` | 设置部署分组 (body: `{group}`) |
| POST | `/api/instances/batch-deploy-group` | 批量设置分组 (body: `{ids, group}`) |

### 灰度部署分组

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/deploy-groups` | 列出所有分组 (含实例计数, 按 priority 排序) |
| POST | `/api/deploy-groups` | 创建分组 (body: `{name, priority, description}`) |
| PUT | `/api/deploy-groups/:name` | 修改分组 (body: `{priority?, description?}`) |
| DELETE | `/api/deploy-groups/:name` | 删除分组 (实例自动移入 stable, stable 不可删) |

### 部署流水线

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/deploy` | 启动部署 (body: `{image_tag, mode}`, mode: normal/fast/canary-only) |
| GET | `/api/deploy/status` | 当前部署状态 (含 wave_order, 各组计数, 进度百分比) |
| POST | `/api/deploy/continue` | 继续暂停的部署 (**从暂停波次恢复**, 不重放已完成波次) |
| POST | `/api/deploy/rollback` | 回滚到上一版本 |
| POST | `/api/deploy/abort` | 中止部署 |
| GET | `/api/deploy/history?limit=20` | 部署历史 |
| POST | `/api/deploy/webhook` | GitHub Actions 自动触发 (body: `{image_tag, secret, mode}`) |

### CRD 直查

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/crd/instances` | 列出所有 CRD (spec + status，直接读 K8s etcd) |
| GET | `/api/crd/instances/:uid` | 单个 CRD 详情 (含 metadata.generation) |

### 系统

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | 集群状态 |
| GET | `/api/stats` | **统计汇总** — 模型/提供商/前缀/分组分布 + 当前镜像 |
| GET | `/api/health` | 全量健康检查 (飞书 WS + 记忆库 + 模型) |
| GET | `/api/known-bots` | **knownBots 注册表** — 全局 bot appId→name 映射 |
| GET | `/api/next-id` | 下一个可用 ID |
| POST | `/api/sync/force` | 强制全量 ConfigMap 同步 |
| GET | `/api/sync/check` | DB ↔ K8s 一致性检查 |
| GET | `/api/audit?instance_id=&limit=50` | 审计日志 |
| POST | `/api/import-from-k8s` | 从现有 ConfigMap 导入到 DB (一次性迁移) |
| POST | `/api/backup` | **手动备份** — 触发 SQLite → NAS 备份 |

### AI 运维 Agent

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/agent` | **自然语言运维** — 支持中英文，自动调用 API (body: `{message, dry_run?}`) |
| GET | `/api/agent/capabilities` | Agent 能力清单 + 示例 |

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

# Go Operator 单元测试
cd operator-go
go test ./internal/controller/ -v
```

## 部署到 K8s

```bash
# 一键部署 admin + operator
./deploy.sh

# 或分步：
kubectl apply -f k8s/crd.yaml                  # 1. 安装 HerInstance CRD
kubectl apply -f k8s/shared-pvcs.yaml           # 2. 创建共享 NAS PVC (skills + sessions)
kubectl apply -f k8s/operator-rbac.yaml         # 3. Operator 权限
kubectl apply -f k8s/operator-deployment.yaml   # 4. 部署 Go Operator
kubectl apply -f k8s/rbac.yaml                  # 5. Admin 权限
kubectl apply -f k8s/deployment.yaml            # 6. 部署 Admin Dashboard
kubectl apply -f k8s/servicemonitor.yaml        # 7. Prometheus 监控 + 告警规则

# 配置 admin 认证密钥
kubectl create secret generic carher-admin-secrets -n carher \
  --from-literal=deploy-webhook-secret=YOUR_WEBHOOK_SECRET \
  --from-literal=admin-api-key=YOUR_API_KEY

# 迁移现有 bare Pod 实例到 CRD
python -m operator.migrate --dry-run     # 预览
python -m operator.migrate               # 执行
```

## 使用 HerInstance CRD

```bash
# 查看所有实例
kubectl get her -n carher
# NAME     USER   NAME   MODEL   PHASE     FEISHU      GROUP    IMAGE         AGE
# her-14   14     张三    gpt     Running   Connected   stable   v20260328     5d
# her-99   99     VIP    sonnet  Running   Connected   vip      v20260329     2d

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

# 移动到 VIP 灰度组
kubectl patch her her-14 -n carher --type merge -p '{"spec":{"deployGroup":"vip"}}'

# 删除实例 (PVC 保留)
kubectl delete her her-14 -n carher
```

## HerInstance CRD Schema

### spec (期望状态, 用户写入)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `userId` | integer | (必填) | 实例唯一 ID |
| `name` | string | (必填) | 用户名 |
| `model` | string | `gpt` | 模型 (gpt / sonnet / opus) |
| `appId` | string | (必填) | 飞书 App ID |
| `appSecretRef` | string | `carher-{uid}-secret` | K8s Secret 名 (存 app_secret) |
| `prefix` | string | `s1` | 服务器前缀 (s1/s2/s3) |
| `owner` | string | `""` | 飞书用户 open_id (竖线分隔多个) |
| `provider` | string | `openrouter` | AI 提供商 (openrouter / anthropic) |
| `botOpenId` | string | `""` | 飞书 Bot Open ID |
| `deployGroup` | string | `stable` | 灰度分组 (任意自定义名称) |
| `image` | string | `v20260328` | 镜像 tag |
| `paused` | boolean | `false` | true 时 Operator 不维护 Pod |

### status (运行状态, Operator 写入)

| 字段 | 类型 | 说明 |
|------|------|------|
| `phase` | string | Pending / Running / Failed / Stopped / Paused |
| `podIP` | string | Pod IP 地址 |
| `node` | string | 所在 K8s 节点 |
| `restarts` | integer | 容器重启次数 |
| `feishuWS` | string | Connected / Disconnected / Unknown |
| `memoryDB` | boolean | 记忆库是否存在 |
| `lastHealthCheck` | string | 最近健康检查时间 (UTC) |
| `message` | string | 附加信息 (错误原因等) |
| `configHash` | string | ConfigMap 内容 hash (变更检测) |

## AI 运维 Agent

内嵌的 AI Agent 支持自然语言操作集群（中英文），底层通过 LLM 理解意图后调用 REST API 执行。

### 使用方式

```bash
# 自然语言查询
curl -X POST https://admin.carher.net/api/agent \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_KEY" \
  -d '{"message":"当前有多少实例在运行？飞书断连的有哪些？"}'

# 执行操作 (Agent 自动识别意图并调用 API)
curl -X POST https://admin.carher.net/api/agent \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_KEY" \
  -d '{"message":"把用户 14 移到 VIP 组"}'

# Dry run — 只描述会做什么，不执行
curl -X POST https://admin.carher.net/api/agent \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_KEY" \
  -d '{"message":"重启所有飞书断连的实例","dry_run":true}'
```

### Agent 能力

| 类别 | 示例 |
|------|------|
| 查询 | "当前集群状态" "查看实例 14 详情" "有哪些飞书断连的" |
| 生命周期 | "重启实例 25" "停止所有 Failed 的实例" "启动 carher-14" |
| 部署 | "部署 v20260329 到金丝雀组" "查看当前部署状态" |
| 分组 | "把 14 移到 VIP 组" "创建 test 分组 优先级 5" |
| 诊断 | "分析 carher-25 的日志" "为什么 14 号飞书断连了" |
| 统计 | "当前有多少实例在运行" "各模型使用分布" |

### 安全机制

- 所有 Agent API 调用需 `X-API-Key` 认证
- 破坏性操作 (delete/purge) 需要确认
- 批量操作 >10 实例时先汇报计划
- `dry_run=true` 只返回执行计划不执行
- Pod exec 仅允许白名单命令 (ls/cat/grep/ps 等)
- batch_action 失败不再静默吞掉，返回每个失败 UID 和原因

## 程序化调用 (Cursor / MCP)

所有 API 均有完整的 Pydantic 类型定义，FastAPI 自动生成 OpenAPI 3.0 schema：

```bash
# 获取完整 OpenAPI schema (JSON)
curl -s https://admin.carher.net/openapi.json | jq

# 获取 Swagger UI
open https://admin.carher.net/docs

# 获取 ReDoc
open https://admin.carher.net/redoc
```

Cursor 可通过 `.cursor/skills/carher-admin-api/SKILL.md` 直接消费所有 API。

## 环境变量

| 变量 | 组件 | 说明 |
|------|------|------|
| `CARHER_ADMIN_DB_DIR` | admin | SQLite 存储路径 (默认 `/data/carher-admin`) |
| `CARHER_ADMIN_BACKUP_DIR` | admin | NAS 备份路径 (默认 `/nas-backup/carher-admin`) |
| `DEPLOY_WEBHOOK_SECRET` | admin | GitHub webhook 验证密钥 (K8s Secret 注入) |
| `ADMIN_API_KEY` | admin | API 认证密钥 (写操作, K8s Secret 注入, 可选) |
| `CORS_ALLOW_ORIGINS` | admin | CORS 白名单 (逗号分隔, 默认 `https://admin.carher.net`) |
| `FEISHU_DEPLOY_WEBHOOK` | admin | 飞书群 webhook URL (部署通知) |
| `DEPLOY_HEALTH_WAIT_CANARY` | admin | 金丝雀健康检查等待秒数 (默认 30) |
| `DEPLOY_HEALTH_WAIT` | admin | 普通波次健康检查等待秒数 (默认 15) |
| `AGENT_LLM_API_KEY` | admin | AI Agent LLM API Key (OpenRouter/OpenAI) |
| `AGENT_LLM_BASE_URL` | admin | LLM API Base URL (默认 OpenRouter) |
| `AGENT_MODEL` | admin | LLM 模型名 (默认 openai/gpt-4o) |
| `DEPLOY_BATCH_SIZE` | admin | 部署批次大小 (默认 50) |
| `HEALTH_CHECK_WORKERS` | operator | 并发健康检查 goroutine 数 (默认 50) |

## GitHub Secrets 配置

在 https://github.com/guangzhou/carher-admin/settings/secrets/actions 配置：

| Secret | 说明 |
|--------|------|
| `ACR_USERNAME` | 阿里云 ACR 登录用户名 |
| `ACR_PASSWORD` | 阿里云 ACR 登录密码 |
| `DEPLOY_WEBHOOK_SECRET` | 与 K8s Secret 中 `deploy-webhook-secret` 值一致 |

## 性能优化记录

六轮 Go Operator review（R1–R6）+ Python Admin 优化，共 19+ 修复。关键优化项：

### Go Operator (R3–R6)

| 优化 | 原问题 | 改进 | 影响 |
|------|--------|------|------|
| Owns(&Pod{}) + ownerRef | Pod 被删除后需等 30s 健康检查才发现 | Pod 删除/驱逐立即触发 reconcile | 自愈从 30s → 秒级 |
| ConfigMap hash 跳过 | 每次 reconcile 都写 ConfigMap | hash 相同跳过写操作 | 500 实例省 500 次 K8s API 调用/轮 |
| knownBots 按需重建 | 每次 reconcile 都 MarkDirty | 仅 configHash 变更时重建 | 大幅减少 ConfigMap 写入 |
| resolveImage/resolvePrefix | Spec.Image 为空时比较 mismatch → 无限 Pod 重建 | 统一 helper 处理默认值 | 消除循环重建 bug |
| 指标清理 (DeleteLabelValues) | 实例删除后 Prometheus label 永留 | 删除时清理 FeishuWS/PodRestarts | 防止指标基数膨胀 |
| SelfHealTotal 去重 | 每 30s 健康检查都递增 | 仅 Phase ≠ Pending 时首次计数 | 指标真实反映自愈事件 |
| DeployActive 移除 | 注册但从未设值 | 删除死指标 | 指标表干净 |
| Dockerfile 层缓存 | go.mod 变动重新下载所有依赖 | go.mod/go.sum 单独 COPY + `-ldflags -s -w` | 构建时间缩短 ~40% |
| metricsserver API 迁移 | MetricsBindAddress 已弃用 | 使用 controller-runtime v0.20 新 API | 无弃用警告 |

### Python Admin

| 优化 | 原问题 | 改进 | 影响 |
|------|--------|------|------|
| api_health() 读 CRD status | 逐 Pod 读日志 + exec (O(N) K8s 调用) | 单次 list CRD status | 500 实例: 1000+ → 1 次 API 调用 |
| backup_to_nas() 去抖 | 每次 DB 写后立即复制到 NAS | dirty flag + flush_backup() | 批量导入不再触发 1500 次全量复制 |
| deployer 并发 asyncio.gather | 逐实例串行部署/健康检查 | gather 并发 (DEPLOY_BATCH_SIZE=50) | 部署速度提升 ~10x |
| knownBots TTL 缓存 | 每个 API 请求都 collect_known_bots() | 15s TTL 缓存 + invalidate | 消除 O(N) 重复计算 |
| K8s API client 单例 | 每次调用创建新 client 实例 | singleton CoreV1Api/CustomObjectsApi | 减少连接开销 |
| 分页 API | list_instances 返回全量 JSON | offset + limit 参数 | 大实例量下首屏加载快 |
| 前端轮询分离 | 5s 间隔重新加载全量实例列表 | loadStatusOnly 只拉 status + groups | 网络流量减少 ~80% |

## 扩展到 10+ 服务器节点

当前架构（3 节点 ACK）已为 500+ 实例设计。扩展到 10+ 节点时：

### 无需修改即可工作

| 组件 | 原因 |
|------|------|
| Go Operator | 事件驱动 reconcile，不轮询；Leader Election HA；Pod 调度由 K8s scheduler 自动分配节点 |
| NAS PVC (skills/sessions) | `ReadWriteMany`，所有节点自动挂载；`alibabacloud-cnfs-nas` 跨节点共享 |
| HerInstance CRD | K8s etcd 存储，节点数无关 |
| Prometheus + AlertManager | ServiceMonitor 自动发现，节点增加无需配置变更 |
| CI/CD Pipeline | 部署/灰度逻辑与节点数无关，operator 按 CRD 声明调度 |

### 建议改进 (10+ 节点)

| 改进项 | 当前状态 | 建议 | 优先级 |
|--------|---------|------|--------|
| Cloudflare Tunnel | systemd on node 226 | 容器化为 K8s Deployment + 多副本 | **高** — 单点故障 |
| Admin DB (SQLite) | hostPath + nodeSelector 固定节点 | 迁移到 NAS PVC，解除节点亲和性 | **高** — 节点故障影响 admin |
| HEALTH_CHECK_WORKERS | 默认 50 | 按实例数调整 (建议 instances/10) | 中 — 1000+ 时需调 |
| Pod 反亲和 | 无 (K8s scheduler 默认分散) | 对关键实例添加 podAntiAffinity | 低 — 默认已足够 |
| 节点 taint/toleration | 无 | 专用节点池给 Her Pods，隔离 admin/monitoring | 低 — 资源紧张时 |

### 容量估算 (10 节点)

| 资源 | 3 节点 (当前) | 10 节点 |
|------|-------------|---------|
| Her 实例 | ~500 | ~1500–2000 |
| Operator replicas | 2 | 2 (不变，worker 池自动分摊) |
| NAS 容量 | 1TB | 2–3TB (按实例数线性增长) |
| 健康检查周期 | ~10s (50 workers / 500 实例) | ~10s (调 workers 至 150) |
| Prometheus 存储 | 7 指标 × 500 = 3500 series | 7 × 2000 = 14000 series (无需担心) |
