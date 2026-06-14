# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What This Repo Is

CarHer Admin — 管理 500+ CarHer（飞书 AI 助手）实例的全生命周期平台，由三个独立组件构成：

1. **carher-admin** — Python FastAPI 后端 + React 前端，提供 Web Dashboard 和 60+ REST API
2. **carher-operator** (`operator-go/`) — Go controller-runtime K8s Operator，watch HerInstance CRD 并管理 Pod 生命周期
3. **cloudflared** — K8s Deployment，Admin 自动维护其 ConfigMap，提供 `*.carher.net` 接入

## Commands

### Backend (Python)

```bash
# 运行所有测试
cd /path/to/carher-admin
python -m pytest backend/tests/ -v

# 运行单个测试文件
python -m pytest backend/tests/test_config_gen.py -v

# 本地启动后端（需要 K8s 集群访问）
uvicorn backend.main:app --host 0.0.0.0 --port 8900 --reload
```

### Frontend (React)

```bash
cd frontend
npm install
npm run dev      # 本地开发，proxy 到 backend:8900
npm run build    # 生产构建，输出到 frontend/dist/
```

### Operator (Go)

```bash
cd operator-go
go mod tidy
go test ./internal/controller/ -v   # 单元测试
go build -o operator ./cmd/main.go  # 本地构建
```

## Codex Working Discipline

These rules adapt the Karpathy-inspired `CLAUDE.md` guidance for Codex in this repo. Source snapshot: `docs/references/karpathy-claude.md`.

### Think Before Coding

- Do not silently assume business intent, deployment target, cluster safety, or data ownership. If ambiguity could change the implementation or operational risk, state the assumption or ask first.
- When several interpretations are plausible, list the meaningful options and choose the smallest one that satisfies the request.
- For any causal claim, follow the repo's Diagnosis Discipline below before proposing a patch.

### Simplicity First

- Write the minimum code that solves the requested problem; avoid speculative features, abstractions, flags, and configurability.
- Prefer existing helpers, module boundaries, and style over new framework or architecture choices.
- If a solution starts growing broad, pause and restate the simpler path before continuing.

### Surgical Changes

- Touch only files and lines required by the request. Do not opportunistically refactor, reformat, rename, or clean adjacent code.
- Remove only unused imports, variables, or helper code made obsolete by your own change. Mention unrelated dead code instead of deleting it.
- Every changed line should trace directly to the user request, a failing verification, or a repo-specific safety rule.

### Goal-Driven Execution

- Convert work into verifiable outcomes: reproduce bugs before fixing when feasible, then run the narrowest meaningful test or command.
- For multi-step work, keep a short plan and update it as steps complete.
- Do not call work done until verification is complete or the reason it could not be verified is clearly stated.

## Deployment Isolation (Critical)

**这是两条完全独立的部署流水线，绝不能混用：**

### carher-admin + carher-operator

- 镜像：`her/carher-admin`、`her/carher-operator`
- **必须在构建服务器 `47.84.112.136` 上用 nerdctl 构建**，禁止在本地 Mac 构建（架构不匹配）
- **不走 CI/CD**，GitHub Actions 不构建也不部署 admin/operator
- 脚本 `deploy.sh` 仅供参考流程，实际在服务器上执行

### carher 主程序（bot 实例镜像 `her/carher`）

- 构建自 `docker/Dockerfile`（在 CarHer 主程序仓库，非本仓库）
- CI/CD 自动触发（push main 且 `docker/**` 或 `configs/**` 有变更）
- 也可通过 Admin API `POST /api/deploy` 手动触发
- image tag 与 admin commit 无关

### K8s 镜像拉取规则

- K8s Pod 的镜像**必须通过 ACR VPC 内网**拉取（`cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com`）
- 禁止在 Deployment/Job 中直接引用 `ghcr.io`、`docker.io` 等公网仓库
- 第三方镜像需先在构建服务器拉取、tag 成 ACR 格式后推送

## Local Skills And Runbooks

- H75/Dify/OpenClaw/Hermes single-Her rollout artifacts are owned by this repo, not by the upstream `../CarHer` repo.
- Use `.codex/skills/carher-h75-dify-single-her-rollout/SKILL.md` for this workflow; do not add session-specific rollout skills under `../CarHer/.agents/skills` or upstream docs.
- Use `docs/her266-h75-session-artifacts.md` as the artifact index, `docs/her266-h75-dify-retrospective.md` as the retrospective, and `scripts/her266-h75/README.md` as the executable runbook.
- Upstream references copied from `../CarHer` live under `docs/references/carher-upstream/` and should be treated as read-only snapshots unless deliberately refreshed.
- Never place tokens, full chat IDs, cookies, AK/SK, API keys, or temporary login links in skills, docs, diagrams, or script examples.

### 零中断部署规则

- 禁止手动 `kubectl delete pod` 正在服务的 Pod，必须依赖 Deployment 滚动更新
- 操作变更时使用 `kubectl apply` 或 `kubectl set image`，用 `kubectl rollout status` 监控

## Architecture

### 数据流与存储

| 数据 | 存储位置 | 写入方 | 读取方 |
|------|---------|--------|--------|
| 实例配置 | HerInstance CRD (etcd) — source of truth | Admin API | Operator |
| 实例状态 (phase, feishuWS) | CRD status | Operator | Admin Dashboard |
| appSecret | K8s Secret | Admin | Operator |
| knownBots（全局 bot 注册表）| 共享 ConfigMap | Operator | 各实例 Pod |
| 用户数据（记忆/会话）| PVC `carher-{uid}-data`（NAS 20Gi）| Pod | Pod |
| 共享 Skills | NAS PVC `carher-shared-skills` (RWX) | 管理员 | 各实例 Pod |
| 审计/部署/分支规则/灰度分组 | SQLite（hostPath，写后备份到 NAS）| Admin API | Admin Dashboard |
| 实时指标 | Prometheus | Operator `/metrics` | Grafana |

SQLite 放在 hostPath 而非 NAS，避免 NFS 锁问题；每次写入后异步备份到 NAS，启动时若本地缺失则从 NAS 恢复。

### Operator Reconcile 核心流程

触发源：CRD spec 变更（watch）、Pod/Service 事件（`Owns(&Pod{})`）、30s 定期健康检查（兜底）。

```
CRD spec → 读 appSecret Secret → 生成 openclaw.json → 计算 configHash
  → hash 未变：跳过 ConfigMap 写入（500 实例省 500 次 API 调用）
  → hash 变更：写 ConfigMap + MarkDirty knownBots
→ 确保 ClusterIP Service（每实例，稳定 IP）
→ Pod 不存在：确保 PVC → 创建 Pod（ownerRef → CRD）
→ Pod 存在且 image/configHash 变更：删旧 Pod（AlreadyExists → RequeueAfter 3s）
→ Patch CRD status
```

### Pod 的 7 个 Volume Mount

| Volume | 来源 | 挂载路径 |
|--------|------|---------|
| user-data | PVC (NAS 20Gi) | `/data/.openclaw` |
| user-config | ConfigMap (per-user) | `/data/.openclaw/openclaw.json` |
| base-config | ConfigMap (shared) | `/data/.openclaw/carher-config.json` |
| gcloud-adc | Secret | `/gcloud/application_default_credentials.json` |
| shared-skills | NAS PVC (RWX) | `/data/.openclaw/skills` |
| dept-skills | NAS PVC (RWX) | `/data/.agents/skills` |
| user-sessions | NAS PVC (RWX) | `/data/.openclaw/sessions` |

ConfigMap 更新后通过 reloader sidecar（Node.js）每 5s 检测 hash 变化并注入 appSecret，使用 `writeFileSync` 而非 rename（SubPath bind mount 不跟随 inode 变更）。

### carher-admin 后端模块职责

- `main.py` — FastAPI 路由、JWT 认证、webhook 验证
- `database.py` — SQLite 操作，所有写入后触发 NAS 备份
- `config_gen.py` — DB 行 → `openclaw.json`（纯函数，可单元测试）
- `crd_ops.py` — HerInstance CRD CRUD
- `k8s_ops.py` — ConfigMap / Pod / PVC 生命周期
- `deployer.py` — 灰度部署编排（按 priority 分组、波次控制）
- `cloudflare_ops.py` — Cloudflare DNS + Tunnel ingress 同步
- `sync_worker.py` — 后台重试 + 一致性检查
- `metrics.py` — 60s 采样写入 SQLite `metrics_history`

### 前端组件结构

`frontend/src/components/` 下按页面组织：`Dashboard`、`InstanceList`、`InstanceDetail`、`DeployPage`、`AddInstance`、`BatchImport`、`SettingsPage`、`HealthCheck`、`LogViewer`、`LoginPage`。API 调用统一在 `src/api.js`，模型常量在 `src/models.js`。

## Diagnosis Discipline

任何"X 导致 Y"形式的归因，必须展开三段式再下结论：

1. **假设**：明确写出"X 导致 Y"
2. **证伪条件**：如果假设错误，数据应呈现什么形态
3. **数据**：实际数据落在哪里，给出可复现的查询路径

数据与假设矛盾时，直接抛弃假设重起，禁止事后兜底。提出任何补丁前，先审计当前路径上已有的补丁（callback、monkey-patch、env var 等），每一个用三段式确认是否仍然成立。

## Key K8s Manifests

```
k8s/
├── crd.yaml                # HerInstance CRD 定义
├── deployment.yaml         # carher-admin Deployment
├── operator-deployment.yaml# carher-operator Deployment (2 replicas + leader election)
├── operator-rbac.yaml      # Operator 所需 ClusterRole
├── shared-pvcs.yaml        # shared-skills / dept-skills / sessions PVC
├── cloudflared.yaml        # cloudflared Deployment
├── litellm-proxy.yaml      # LiteLLM Proxy Deployment
└── base-config.yaml        # 共享 ConfigMap (carher-config.json)
```
