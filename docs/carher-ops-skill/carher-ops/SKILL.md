---
name: carher_ops
description: CarHer K8s 集群运维管理 — 通过 Admin API 查询集群状态、管理 Her 实例、部署、健康检查、费用追踪。
---

# CarHer Ops 运维技能

你可以通过调用 CarHer Admin API 来管理整个 K8s 集群。使用 `exec` 工具执行 curl 命令调用 API。

## 集群架构

- **Namespace**: `carher`
- 500+ Her 实例，每个实例 = HerInstance CRD → Operator 自动创建 Deployment + Service + ConfigMap + PVC
- **Operator**: Go 控制器，2 副本，30s 健康检查，50 并发 worker
- **Admin API**: `http://carher-admin-svc.carher:8900`
- **LiteLLM Proxy**: 统一 LLM 路由，每实例独立虚拟 key 做费用追踪
- **Cloudflare Tunnel**: 每实例公网 OAuth 回调路由

### 每个 Her 实例

- **Deployment** `carher-{uid}`: 主容器 + config-reloader sidecar + inject-secret init
- **Service** `carher-{uid}-svc`: 端口 18789(gateway) / 18790(realtime) / 8000(frontend) / 8080(ws-proxy) / 18891(oauth) / 18795(a2a)
- **ConfigMap** `carher-{uid}-user-config`: openclaw.json 模板
- **PVC** `carher-{uid}-data`: 20Gi NAS
- **Secret** `carher-{uid}-secret`: 飞书 app_secret

### 健康标准

| 指标 | 正常 | 异常 |
|------|------|------|
| Pod Ready | 2/2 | <2/2 |
| CRD Phase | Running | Failed/Pending/CrashLoopBackOff |
| 飞书 WebSocket | Connected | Disconnected |

## API 调用方式

使用 exec 工具执行 curl 命令。所有请求必须带认证 header。

```bash
curl -s http://carher-admin-svc.carher:8900/api/status \
  -H "X-API-Key: bd037b874cb170710c4873b0cdec924539bcddba879f4556ae40875a15e4a1e4"
```

## 安全规则

- 重启/停止前先确认目标实例
- 批量操作（>5 个）先列出受影响实例让用户确认
- 删除必须二次确认，默认 purge=false 保留数据
- 不暴露 API Key、app_secret 等敏感值
- 部署操作需确认 image_tag 来自 `her/carher` 镜像

## 完整 API 参考

### 集群状态与监控

| 用途 | 命令 |
|------|------|
| 集群状态 | `GET /api/status` |
| 统计汇总（模型/provider/组分布、当前镜像） | `GET /api/stats` |
| 全量健康检查 | `GET /api/health` |
| 集群资源概览 | `GET /api/metrics/overview` |
| 各节点 CPU/内存 | `GET /api/metrics/nodes` |
| 所有 Pod 指标 | `GET /api/metrics/pods` |
| PVC 存储 | `GET /api/metrics/storage` |
| 节点历史指标 | `GET /api/metrics/history/nodes?hours=24` (1-168h) |
| knownBots 注册表 | `GET /api/known-bots` |
| 审计日志 | `GET /api/audit?limit=50&instance_id=14` |

### 实例查询

| 用途 | 命令 |
|------|------|
| 列出实例 | `GET /api/instances?offset=0&limit=50` (limit=0 全部) |
| 搜索实例 | `GET /api/instances/search?status=Running&feishu_ws=Disconnected` |
| 实例详情 | `GET /api/instances/{uid}` |
| 下个可用 ID | `GET /api/next-id` |
| CRD 列表 | `GET /api/crd/instances` |
| CRD 详情 | `GET /api/crd/instances/{uid}` |

搜索筛选（AND 组合）: `status`(Running/Stopped/Failed/Paused), `model`(gpt/sonnet/opus/gemini/minimax/glm/codex), `deploy_group`, `owner`, `name`, `feishu_ws`(Connected/Disconnected), `offset`, `limit`

### 实例生命周期

| 用途 | 命令 |
|------|------|
| 停止 | `POST /api/instances/{uid}/stop` |
| 启动 | `POST /api/instances/{uid}/start` |
| 重启 | `POST /api/instances/{uid}/restart` |
| 删除 | `DELETE /api/instances/{uid}?purge=false` |

### 实例诊断

| 用途 | 命令 |
|------|------|
| Pod 日志 | `GET /api/instances/{uid}/logs?tail=100` |
| K8s 事件 | `GET /api/instances/{uid}/events?limit=20` |
| 预览配置 | `GET /api/instances/{uid}/config-preview` |
| 当前配置 | `GET /api/instances/{uid}/config-current` |
| 实时指标 | `GET /api/instances/{uid}/metrics` |
| 历史指标 | `GET /api/instances/{uid}/metrics/history?hours=24` |
| Pod 执行命令 | `POST /api/instances/{uid}/exec` body: `{"command":"ls /data"}` |

exec 白名单: ls, cat, head, tail, grep, wc, df, du, ps, uptime, env, echo, test, stat, find, node --version, npm --version, openclaw

### 创建实例

```bash
curl -X POST http://carher-admin-svc.carher:8900/api/instances \
  -H "Content-Type: application/json" \
  -H "X-API-Key: bd037b874cb170710c4873b0cdec924539bcddba879f4556ae40875a15e4a1e4" \
  -d '{
    "name": "用户名",
    "app_id": "cli_xxx",
    "app_secret": "xxx",
    "model": "gpt",
    "provider": "litellm",
    "prefix": "s1",
    "owner": "ou_xxx",
    "deploy_group": "stable"
  }'
```

必填: name, app_id, app_secret。其他有默认值。

返回: `{"id":N, "oauth_url":"https://s1-uN-auth.carher.net/feishu/oauth/callback", "cloudflare":{"ok":true}}`

**provider**: openrouter / anthropic / wangsu / litellm
**model**: gpt(GPT-5.4) / sonnet(Claude Sonnet 4.6) / opus(Claude Opus 4.6) / gemini(Gemini 3.1 Pro) / minimax(仅litellm) / glm(仅litellm) / codex(仅litellm)

### 更新实例

```bash
curl -X PUT http://carher-admin-svc.carher:8900/api/instances/{uid} \
  -H "Content-Type: application/json" \
  -H "X-API-Key: bd037b874cb170710c4873b0cdec924539bcddba879f4556ae40875a15e4a1e4" \
  -d '{"model": "sonnet", "provider": "litellm"}'
```

可更新字段: name, model, provider, owner, deploy_group, image, app_id, app_secret, prefix, bot_open_id, litellm_route_policy

### 批量操作

```bash
curl -X POST http://carher-admin-svc.carher:8900/api/instances/batch \
  -H "Content-Type: application/json" \
  -H "X-API-Key: bd037b874cb170710c4873b0cdec924539bcddba879f4556ae40875a15e4a1e4" \
  -d '{"ids": [14, 25, 30], "action": "restart"}'
```

action: stop / start / restart / delete / update

批量更新: `{"ids":[14,25], "action":"update", "params":{"model":"sonnet"}}`

### 批量创建

```bash
curl -X POST http://carher-admin-svc.carher:8900/api/instances/batch-import \
  -H "Content-Type: application/json" \
  -H "X-API-Key: bd037b874cb170710c4873b0cdec924539bcddba879f4556ae40875a15e4a1e4" \
  -d '{"instances":[{"name":"用户A","app_id":"cli_xxx","app_secret":"xxx","model":"gpt","provider":"litellm","prefix":"s1"}]}'
```

### 部署组

| 用途 | 命令 |
|------|------|
| 列出分组 | `GET /api/deploy-groups` |
| 创建分组 | `POST /api/deploy-groups` body: `{"name":"vip","priority":5}` |
| 移动实例 | `PUT /api/instances/{uid}/deploy-group` body: `{"group":"vip"}` |
| 批量移动 | `POST /api/instances/batch-deploy-group` body: `{"ids":[14,25],"group":"canary"}` |

优先级越小越先部署。默认波次: canary → early → stable

### 部署

| 用途 | 命令 |
|------|------|
| 触发部署 | `POST /api/deploy` body: `{"image_tag":"v20260409-xxx","mode":"normal"}` |
| 部署状态 | `GET /api/deploy/status` |
| 继续部署 | `POST /api/deploy/continue` |
| 回滚 | `POST /api/deploy/rollback` |
| 中止 | `POST /api/deploy/abort` |
| 部署历史 | `GET /api/deploy/history?limit=20` |
| 可用镜像 | `GET /api/image-tags?limit=30` |
| 同步 ACR 镜像 | `POST /api/image-tags/sync` |

mode: normal(canary→early→stable) / fast(全量批次50) / canary-only / group:\<name\>

### LiteLLM

| 用途 | 命令 |
|------|------|
| 生成 key | `POST /api/litellm/keys/generate?uid=100` |
| 批量生成 | `POST /api/litellm/keys/generate-batch` |
| 费用查询 | `GET /api/litellm/spend` |

### 系统

| 用途 | 命令 |
|------|------|
| 强制 ConfigMap 同步 | `POST /api/sync/force` |
| 一致性检查 | `GET /api/sync/check` |
| 备份数据库 | `POST /api/backup` |
| Cloudflare 同步 | `POST /api/cloudflare/sync` |
| CI 触发构建 | `POST /api/ci/trigger-build` |
| CI 运行记录 | `GET /api/ci/runs?per_page=10` |

## 常用场景

### 集群巡检
1. `GET /api/stats` → 总数、运行数、模型分布
2. `GET /api/health` → 找 feishu_ws=false（飞书断连）
3. `GET /api/metrics/overview` → 资源使用率

### 找飞书断连的实例
`GET /api/instances/search?status=Running&feishu_ws=Disconnected`

### 排查实例问题
1. `GET /api/instances/{uid}` → 状态、重启次数
2. `GET /api/instances/{uid}/logs?tail=50` → 日志
3. `GET /api/instances/{uid}/events` → K8s 事件

### 批量重启断连实例
1. 搜索断连: `GET /api/instances/search?feishu_ws=Disconnected`
2. 列出给用户确认
3. `POST /api/instances/batch` `{"ids":[...], "action":"restart"}`

### 创建新 Her
需要: 名字、飞书 App ID、App Secret
```bash
POST /api/instances
{"name":"xxx", "app_id":"cli_xxx", "app_secret":"xxx", "model":"gpt", "provider":"litellm", "prefix":"s1"}
```
创建后告知用户 OAuth URL。

### 部署新版本
1. 查镜像: `GET /api/image-tags`
2. canary 先行: `POST /api/deploy` `{"image_tag":"xxx","mode":"canary-only"}`
3. 观察: `GET /api/deploy/status`
4. 全量: `POST /api/deploy/continue`
