# CarHer Admin Dashboard

CarHer K8s 实例管理面板 — Web UI + REST API + SQLite 数据库。

## 架构

```
┌─────────────┐     ┌──────────┐     ┌────────────┐
│  React SPA  │────▶│ FastAPI  │────▶│  SQLite    │  ← source of truth
│  (Tailwind) │     │  backend │     │  (hostPath) │
└─────────────┘     └────┬─────┘     └─────┬──────┘
                         │                  │
                         ▼                  ▼ backup
                  ┌─────────────┐    ┌────────────┐
                  │ K8s API     │    │  NAS       │
                  │ ConfigMap   │    │  (backup)  │
                  │ Pod / PVC   │    └────────────┘
                  └─────────────┘
```

**DB 是唯一 source of truth**，ConfigMap 是派生产物：
- 每次写 DB 后，自动生成 `openclaw.json` 并写入 ConfigMap
- `knownBots` 从 DB 实时计算（一条 SQL），不再每个用户存一份
- 后台 worker 每 60s 自动重试失败的同步

## 功能

- **仪表盘** — 集群概览、节点分布、Pod 统计
- **实例管理** — 列表、搜索、过滤、单个/批量 启动/停止/重启/删除
- **新增实例** — 表单创建，自动分配 ID，生成 OAuth URL
- **批量导入** — CSV 上传/粘贴 → 预览 → 一键创建
- **配置编辑** — 在线修改模型、Owner，自动重建 Pod
- **日志查看** — 实时日志、自动刷新
- **健康检查** — 飞书 WS / 记忆库 / 模型加载 三项全检
- **系统管理** — 强制同步、一致性检查、K8s 导入、审计日志

## 风险应对

| 风险 | 策略 |
|------|------|
| SQLite + NFS 不兼容 | SQLite 在 hostPath（本地盘），NAS 仅做备份 |
| DB ↔ ConfigMap 不一致 | `sync_status` 标记 + 后台 worker 自动重试 + 手动强制同步 |
| 数据丢失 | 三层：hostPath → NAS 备份 → ConfigMap 兜底反向导入 |
| Admin 单点故障 | Deployment 自动拉起，Her Pod 不依赖 Admin |

## 项目结构

```
├── backend/
│   ├── main.py              # FastAPI 入口 + API 路由
│   ├── database.py          # SQLite 数据层 + 备份
│   ├── config_gen.py        # DB → openclaw.json 生成器
│   ├── k8s_ops.py           # K8s API 操作（Pod/ConfigMap/PVC）
│   ├── sync_worker.py       # 后台同步 worker + 一致性检查
│   ├── models.py            # Pydantic 数据模型
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── App.jsx          # 主应用 + 导航
│   │   ├── api.js           # API client
│   │   └── components/      # 页面组件
│   ├── package.json
│   └── vite.config.js
├── k8s/
│   ├── rbac.yaml            # ServiceAccount + Role
│   └── deployment.yaml      # Deployment + Service + PVC
├── Dockerfile               # 多阶段构建
└── deploy.sh                # 一键构建+推送+部署
```

## 本地开发

```bash
# Backend（需要 kubeconfig 或 in-cluster）
cd backend
pip install -r requirements.txt
CARHER_ADMIN_DB_DIR=/tmp/carher-admin CARHER_ADMIN_BACKUP_DIR=/tmp/carher-admin-bak \
  uvicorn backend.main:app --reload --port 8900

# Frontend（另一个终端）
cd frontend
npm install
npm run dev
```

## 部署到 K8s

```bash
./deploy.sh
```

## API

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
| GET | `/api/status` | 集群状态 |
| GET | `/api/health` | 健康检查 |
| POST | `/api/sync/force` | 强制全量同步 |
| GET | `/api/sync/check` | 一致性检查 |
| GET | `/api/audit` | 审计日志 |
| POST | `/api/import-from-k8s` | 从 ConfigMap 导入 DB |
