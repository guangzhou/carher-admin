# Codex 与 Her 双向调用完整方案

更新时间：2026-06-05

> 2026-06-06 评审修订：本方案保留作为完整目标设计；实际开工以文末“评论吸收后的 V2 修订”为准。V2 将 MVP 收窄为“先做 Her 调本地 Codex”，Codex 调 Her 延后到 Phase 2，并新增 Worker worktree/队列/超时/审批等执行层安全边界。

## 1. 背景与目标

CarHer 体系里，Her 是背后运行 OpenClaw 的飞书智能体机器人，具备飞书消息入口、OpenClaw agent run loop、skills、gateway、A2A 插件和 K8s 生命周期管理。Codex 是本地/桌面/CLI 里的代码智能体，能访问本地 repo、shell、git、MCP、app-server，并且已经可以通过供应商 OpenAI-compatible API 工作。

目标是实现双向能力：

1. Codex 可以调用某个 Her，让 Her 以自己的飞书/OpenClaw 身份完成消息、业务问答、飞书资料处理、Her 记忆检索等任务。
2. Her 可以调用本地 Codex，让飞书里的用户触发本地 repo 诊断、改代码、跑测试、产出 diff，并通过飞书完成审批。
3. 能力必须受控、可审计、可灰度，不能让 500+ Her 默认获得远程执行本地命令的能力。

## 2. 关键官方与现有依据

- Codex 官方 App Server 是面向富客户端的双向 JSON-RPC 协议，支持 `thread/start`、`turn/start`、流式事件、命令/文件审批等能力；WebSocket 传输仍应按 experimental 看待，必须有认证和外层安全边界。
- Codex 官方支持 MCP 作为工具扩展面，适合把 Her 暴露成 Codex 的工具集合。
- OpenClaw/Her 侧已有 gateway、A2A 插件、skills、Feishu channel；本 repo 的 `k8s/base-config.yaml` 已启用 Feishu channel、OpenClaw gateway、`a2a-gateway`。
- Operator 为每个 Her 创建稳定 Service，并挂载 `/data/.openclaw/skills`、`/data/.agents/skills`、sessions、shared config 等 volume；适合通过共享 Skill 让 Her 学会代理到 Bridge。
- 飞书官方 Lark OpenAPI MCP 可作为飞书工具补充，但不替代 Her：它解决“Codex 直接操作飞书 API”，Her 解决“以某个 Her 的 agent 身份工作”。

参考资料：

- OpenAI Codex App Server: https://developers.openai.com/codex/app-server
- OpenAI Codex MCP 配置: https://developers.openai.com/codex/config
- Lark OpenAPI MCP: https://github.com/larksuite/lark-openapi-mcp
- OpenClaw MCP/A2A/Gateway 相关本地依据：`k8s/base-config.yaml`、`operator-go/README.md`、`docs/her266-h75-whitebox-test-plan.md`

## 3. 总体架构

核心采用“双桥接、单控制面”的结构：

- Codex 调 Her：`Codex -> carher-her-mcp -> Bridge/Admin -> Her Gateway Proxy -> Her Pod OpenClaw Gateway/A2A`
- Her 调 Codex：`Her Skill -> Bridge/Admin -> Mac Worker -> local codex app-server -> 本地 repo/shell/供应商 API`

关键图 1：总体双向架构

<whiteboard type="blank"></whiteboard>

## 4. Codex 调 Her

### 4.1 使用 MCP 暴露 Her 能力

在本地 Codex 配置里增加一个 MCP server：`carher-her-mcp`。这个 MCP 不直接保存 Her 的 appSecret，也不直接访问 500 个 Pod，而是只调用 Bridge/Admin API。

推荐工具集合：

| Tool | 说明 | 默认风险 |
|---|---|---|
| `her_list` | 列出当前用户可调用的 Her | read |
| `her_status` | 查看 Her 运行状态、Feishu WS、模型、owner | read |
| `her_send` | 向某个 Her 发起一次任务 | action |
| `her_wait` | 等待 Her session 结果 | read |
| `her_read` | 读取 Her session/event 摘要 | read |
| `her_memory_query` | 查询 Her 侧已暴露的记忆/会话摘要 | read/action |

### 4.2 Her 调用协议

MCP 调 Bridge：

```http
POST /api/her-rpc/sessions
Authorization: Bearer <codex_mcp_token>
Content-Type: application/json

{
  "her_uid": 266,
  "mode": "a2a",
  "prompt": "请用你的飞书身份查一下这个群最近关于 H75 的讨论摘要",
  "caller": {
    "type": "codex",
    "workspace": "carher-admin",
    "thread_id": "local-codex-thread"
  }
}
```

Bridge 负责：

1. 校验 Codex MCP token。
2. 校验调用者是否允许访问目标 Her。
3. 根据 `her_uid` 定位 `carher-{uid}-svc`。
4. 通过 Her Gateway Proxy 调 OpenClaw gateway/A2A。
5. 记录 session 和审计。
6. 将 Her 的回复转回 MCP。

关键图 2：Codex 调 Her 时序

<whiteboard type="blank"></whiteboard>

## 5. Her 调本地 Codex

### 5.1 Her 侧通过 Skill 适配

新增共享 Skill：`codex-remote-control`。它是轻量代理层，不执行本地命令、不读取本地 repo、不保存供应商 API key。

Skill 负责：

- 识别 `/codex` 命令。
- 提取 `her_uid`、`app_id`、`chat_id`、`open_id`、`message_id`、`repo`、`prompt`。
- 调 Bridge 的 `/api/codex/tasks`。
- 把 Bridge 返回的状态、审批、结果渲染为飞书消息或卡片。

支持命令：

```text
/codex repos
/codex workers
/codex carher-admin 查 deploy 风险
/codex status
/codex diff
/codex stop
/codex approve <approval_id>
/codex deny <approval_id>
```

### 5.2 Bridge 到 Mac Worker

Mac Worker 不开放公网端口，而是主动反连 Bridge：

```text
Mac Worker -> wss://codex-bridge.carher.net/workers/connect
```

Mac Worker 本地连接：

```bash
codex app-server \
  --listen ws://127.0.0.1:4500 \
  --ws-auth capability-token \
  --ws-token-file ~/.codex/appserver.token
```

本地供应商 API 配置仍在：

```text
~/.codex/config.toml
CARHER_DEV_KEY
```

关键图 3：Her 调 Codex 时序

<whiteboard type="blank"></whiteboard>

## 6. 接口与数据模型

关键图 4：MCP、Skill、Bridge 接口关系

<whiteboard type="blank"></whiteboard>

### 6.1 Bridge API

#### `POST /api/codex/tasks`

由 Her Skill 调用，创建本地 Codex 任务。

```json
{
  "her_uid": 266,
  "app_id": "cli_xxx",
  "chat_id": "oc_xxx",
  "open_id": "ou_xxx",
  "repo": "carher-admin",
  "prompt": "查一下最近 deploy 相关改动有没有风险",
  "message_id": "om_xxx"
}
```

#### `POST /api/codex/approvals/{approval_id}`

飞书卡片或 `/codex approve` 回传审批。

```json
{
  "decision": "accept",
  "operator_open_id": "ou_xxx",
  "source": "feishu_card"
}
```

#### `POST /api/her-rpc/sessions`

由 Codex MCP 调用，创建 Her 任务。

```json
{
  "her_uid": 266,
  "mode": "a2a",
  "prompt": "请以你的 Her 身份总结最近群聊",
  "caller": {
    "type": "codex",
    "repo": "carher-admin"
  }
}
```

#### `GET /api/her-rpc/sessions/{session_id}`

查询 Her 任务状态和结果。

### 6.2 数据表

#### `bridge_workers`

```text
worker_id
display_name
status
last_seen_at
repos_json
capabilities_json
created_at
updated_at
```

#### `bridge_codex_sessions`

```text
session_id
her_uid
app_id
chat_id
open_id
repo
worker_id
codex_thread_id
status
created_at
updated_at
```

#### `bridge_her_sessions`

```text
session_id
caller_type
caller_subject
her_uid
mode
prompt_hash
status
result_summary
created_at
updated_at
```

#### `bridge_approvals`

```text
approval_id
session_id
kind
command
cwd
reason
status
requested_at
decided_by
decided_at
```

## 7. 权限、审批与审计

关键图 5：权限与审批闭环

<whiteboard type="blank"></whiteboard>

默认策略：

- 只有 `CodexOpsHer` 开启 `/codex`。
- 普通 Her 默认 `codex.enabled=false`。
- 只有白名单 `open_id` 可以调用指定 repo。
- `session_key = her_uid + chat_id + open_id + repo`。
- Bridge 和 Codex approval 双层拦截高风险命令。
- Bridge 不保存供应商 API key。
- Her Skill 不执行 shell。
- Her Gateway 不裸露公网。

高风险默认禁止：

```text
git reset --hard
kubectl delete pod
rm -rf
docker system prune
curl ... | sh
```

高风险必须二次确认：

```text
git push
kubectl apply
kubectl set image
ssh
nerdctl build/push
```

## 8. K8s 落点

关键图 6：K8s 部署拓扑

<whiteboard type="blank"></whiteboard>

### 8.1 新增组件

| 组件 | 位置 | 职责 |
|---|---|---|
| `codex-bridge` | K8s 或公网服务器 | 控制面、API、审计、Worker registry |
| `her-gateway-proxy` | K8s carher namespace | 按 Her uid 路由到 OpenClaw gateway/A2A |
| `carher-her-mcp` | 本地 Codex 机器 | MCP server，Codex 调 Her |
| `codex-worker` | Mac/devbox | 连接 Bridge 与本地 codex app-server |
| `codex-remote-control` Skill | shared skills | Her 调 Codex 的代理适配 |

### 8.2 Operator 与 Config

第一版尽量不改 Operator。只需要：

- 通过现有 shared skills 路径分发 `codex-remote-control`。
- 给 `CodexOpsHer` 或少量 Her 注入：

```text
CODEX_ENABLED=true
CODEX_BRIDGE_URL=https://codex-bridge.carher.net
CODEX_BRIDGE_TOKEN_SECRET=<per-her-token>
```

如需平台化，再扩展 CRD：

```yaml
spec:
  codex:
    enabled: true
    allowedRepos:
      - carher-admin
    bridgeSecretRef: codex-bridge-her-266
```

但第一版建议先不加 CRD 字段，避免影响 500+ Her reconcile 面。

## 9. 分阶段落地

### Phase 0：验证链路

- 本地启动 `codex app-server`。
- 启动 Mac Worker 主动反连 Bridge。
- Bridge 能创建 Codex thread 并收到 streamed events。
- 不接飞书、不接 Her。

### Phase 1：Her 调 Codex

- 新建 `CodexOpsHer`。
- 安装 `codex-remote-control` Skill。
- 支持 `/codex carher-admin ...`。
- 支持飞书输出、stop、approval allow once/deny。

### Phase 2：Codex 调 Her

- 实现 `carher-her-mcp`。
- 支持 `her_list`、`her_status`、`her_send`、`her_wait`。
- 只允许 Codex 调 `CodexOpsHer` 或少量白名单 Her。

### Phase 3：平台化治理

- Bridge 审计接入 carher-admin。
- Admin 页面显示 Workers、Sessions、Approvals。
- 支持多 repo、多 worker、多 Her 白名单。

### Phase 4：谨慎开放

- 开放给少数运维/研发 Her。
- 增加 repo/命令策略模板。
- 接入飞书告警和异常回滚。

## 10. 验收标准

### Codex 调 Her

- Codex 通过 MCP 能列出白名单 Her。
- Codex 能向 Her 发起一次任务，并拿到 OpenClaw 回复。
- 非白名单 Her 返回 403。
- Her 离线时返回明确错误，不反复重试。

### Her 调 Codex

- 飞书 `/codex carher-admin ...` 能创建 Codex session。
- Mac Worker 能收到任务并启动本地 Codex turn。
- Codex 输出能回飞书。
- 命令审批卡片能 allow once/deny。
- `stop` 能中断当前 turn。

### 安全

- Bridge 不保存供应商 API key。
- 普通 Her 默认不能 `/codex`。
- 高风险命令被拒绝或需要二次确认。
- 审计日志包含 open_id、her_uid、repo、worker_id、command、decision。

## 11. 推荐最终取舍

第一版只做：

```text
CodexOpsHer + Bridge Server + Mac Worker + carher-her-mcp
```

不要做：

```text
500+ Her 全量开启 /codex
Her Pod 内跑 Codex
公网暴露 codex app-server
Codex 直接连每个 Her Pod
```

这个方案把“交互入口、控制面、执行面、治理面”分开：Her 负责飞书交互，Bridge 负责路由与审计，Worker 负责本地 Codex 执行，Codex MCP 负责把 Her 变成可调用工具。
