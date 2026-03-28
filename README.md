# CarHer Admin Dashboard

CarHer K8s 实例管理面板 — Web UI + REST API。

## 功能

- **仪表盘** — 集群概览、节点分布、Pod 统计
- **实例管理** — 列表、搜索、过滤、单个/批量 启动/停止/重启/删除
- **新增实例** — 表单创建，自动分配 ID，生成 OAuth URL
- **批量导入** — CSV 上传/粘贴 → 预览 → 一键创建
- **配置编辑** — 在线修改模型、Owner，自动重建 Pod
- **日志查看** — 实时日志、自动刷新
- **健康检查** — 飞书 WS / 记忆库 / 模型加载 三项全检

## 技术栈

- **Backend**: Python FastAPI + kubernetes client（in-cluster ServiceAccount）
- **Frontend**: React 19 + Vite + Tailwind CSS
- **部署**: 单 Docker 镜像，K8s Deployment + RBAC

## 项目结构

```
├── backend/
│   ├── main.py              # FastAPI 入口 + 路由
│   ├── k8s_ops.py           # K8s 操作封装
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
│   └── deployment.yaml      # Deployment + Service
├── Dockerfile               # 多阶段构建
└── deploy.sh                # 一键构建+推送+部署
```

## 本地开发

```bash
# Backend
cd backend
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8900

# Frontend（另一个终端）
cd frontend
npm install
npm run dev
```

前端 dev server 自动代理 `/api` 到 `localhost:8900`。

## 部署到 K8s

```bash
# 一键构建 + 推送 ACR + 部署
./deploy.sh

# 仅构建
./deploy.sh --build-only

# 仅部署（镜像已推送）
./deploy.sh --deploy-only
```

部署后添加 Cloudflare Tunnel route：

```bash
# 在 K8s 节点上
cloudflared tunnel route dns carher-k8s admin.carher.net
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
