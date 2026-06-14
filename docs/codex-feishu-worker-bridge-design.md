# 飞书驱动 Codex Worker Bridge 方案

更新时间：2026-06-04

## 目标

在没有 ChatGPT 账号、使用供应商 OpenAI-compatible API 的前提下，把 Codex 的移动使用入口放到飞书里。飞书负责发起任务、查看输出、审批命令；真正执行 Codex 的环境仍然是本地 Mac 或 devbox。

## 核心结论

采用“公网 Bridge Server + Mac Worker 主动反连”的架构：

- 飞书只访问公网 Bridge Server。
- Mac Worker 主动建立到 Bridge Server 的 WSS 长连接。
- Mac Worker 连接本机 `codex app-server`，执行真实 Codex 会话。
- 供应商 API key、repo、shell 权限都留在 Mac/devbox 本地。
- Bridge Server 做飞书事件、权限、会话、审计、Worker 路由和审批中转。

关键架构图见下方飞书画板。

<whiteboard type="blank"></whiteboard>

## 架构分层

### 1. 飞书入口层

飞书用户在群聊或私聊里使用命令：

```text
/codex carher-admin 查一下最近 deploy 相关改动有没有风险
/codex status
/codex diff
/codex stop
/codex approve <approval_id>
/codex deny <approval_id>
```

飞书开放平台把消息事件和卡片回调 POST 到 Bridge Server：

```text
POST /feishu/events
POST /feishu/card-callback
```

### 2. Bridge Server 控制平面

Bridge Server 跑在公网服务器，例如：

```text
https://codex-bridge.carher.net
wss://codex-bridge.carher.net/workers/connect
```

职责：

- 校验飞书事件签名和 challenge。
- 根据 `open_id`、`chat_id`、`app_id` 做权限判断。
- 维护 Worker 在线表、repo 能力表和心跳。
- 解析 `/codex` 命令并创建 Codex session。
- 把任务通过 WSS 下发给合适的 Mac Worker。
- 把 Worker 回传的 Codex 输出、命令请求、diff 事件转成飞书消息或卡片。
- 记录审计日志：发起人、repo、worker、命令、审批人、结果。

Bridge Server 不保存供应商 API key，不直接访问本地 repo，不直接执行 shell。

### 3. Mac Worker 执行平面

Mac Worker 跑在本地 Mac 或 devbox 上，主动连接 Bridge Server：

```text
Mac Worker -> wss://codex-bridge.carher.net/workers/connect
```

职责：

- 注册自身身份和 repo 能力。
- 连接本机 `codex app-server`。
- 接收 Bridge Server 下发的任务。
- 把 Codex streamed events、approval request、diff 更新回传给 Bridge Server。
- 断线后自动重连。

示例注册信息：

```json
{
  "type": "worker_hello",
  "worker_id": "liuguoxian-macbook",
  "repos": [
    {
      "name": "carher-admin",
      "path": "/Users/Liuguoxian/codes/carher-admin",
      "branch": "main"
    }
  ],
  "capabilities": {
    "codex_app_server": true,
    "shell": true,
    "git": true
  }
}
```

### 4. Codex 本地服务层

Mac 上启动官方本地协议服务：

```bash
codex app-server \
  --listen ws://127.0.0.1:4500 \
  --ws-auth capability-token \
  --ws-token-file ~/.codex/appserver.token
```

`codex app-server` 只监听 `127.0.0.1`。供应商 API 配置仍来自：

```text
~/.codex/config.toml
CARHER_DEV_KEY
```

## Her 如何区分

第一版不要让 500+ Her 都直接控制 Codex。建议先建立一个专用 Her：

```text
CodexOpsHer
```

它是“飞书里的 Codex 控制台”。其他 Her 暂不直接开放 Codex 能力。

长期如果要支持多个 Her，使用三层身份：

```text
Her identity -> Feishu chat/user -> Codex worker/repo/session
```

每次 session 记录：

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
```

路由规则：

1. 从飞书事件识别 `app_id`。
2. 由 `app_id -> her_uid` 查回 Her 实例。
3. 校验该 Her 是否启用 Codex 能力。
4. 校验发送人 `open_id` 是否可操作目标 repo。
5. 找到支持该 repo 的在线 Worker。
6. 创建 session 并下发任务。

## 审批模型

Codex 请求执行命令或修改文件时，Worker 把 approval request 回传到 Bridge Server。Bridge Server 发送飞书卡片：

```text
Codex 请求执行命令

Worker: liuguoxian-macbook
Repo: carher-admin
CWD: /Users/Liuguoxian/codes/carher-admin
Command:
python -m pytest backend/tests/test_deployer.py -v

Reason:
验证 deployer 灰度分组逻辑

[允许一次] [拒绝]
```

审批原则：

- 默认只支持“允许一次”和“拒绝”。
- 不在第一版支持“本会话允许所有命令”。
- 高风险命令必须强制拒绝或二次确认。
- `git reset --hard`、`kubectl delete pod`、`rm -rf`、`docker system prune` 等命令默认禁止。
- `kubectl set image`、`kubectl apply`、`ssh`、`git push` 需要高风险策略和单独审批。

## 数据模型

Bridge Server 建议使用 SQLite 或 Postgres。

### workers

```text
id
display_name
status
last_seen_at
repos_json
worker_pool
created_at
updated_at
```

### codex_sessions

```text
id
her_uid
app_id
feishu_chat_id
feishu_open_id
worker_id
repo
cwd
codex_thread_id
status
created_at
updated_at
```

### codex_events

```text
id
session_id
type
payload_json
created_at
```

### approval_requests

```text
id
session_id
worker_id
command
cwd
reason
status
requested_at
decided_by
decided_at
```

## MVP 范围

第一版保持极小：

- 只支持一个 Worker：`liuguoxian-macbook`。
- 只支持一个 repo：`carher-admin`。
- 只允许一个飞书用户：owner 的 `open_id`。
- 只支持一个专用 Her：`CodexOpsHer`。
- 支持新建任务、流式输出、停止任务。
- 支持命令审批的“允许一次”和“拒绝”。
- 不支持 `git push`。
- 不支持生产 K8s 改动。
- 不接 500+ 普通 Her。

## 演进路径

1. 跑通 Bridge Server、Mac Worker、Codex app-server 三段链路。
2. 跑通飞书消息命令和 Codex 输出回传。
3. 加入 approval 卡片。
4. 接入 carher-admin 审计记录。
5. 支持多 repo。
6. 支持多 Worker。
7. 支持 CodexOpsHer 管理页面。
8. 谨慎开放给部分 Her 或部分飞书群。

## 风险与边界

- Mac 离线时不能执行任务。
- Bridge Server 不能直接持有供应商 API key。
- 不允许把 `codex app-server` 裸露公网。
- 不允许 500+ Her 默认获得远程执行能力。
- 飞书卡片审批不是安全边界的全部，Bridge Server 仍必须做命令策略校验。
- 所有审批、命令、文件修改都必须进入审计日志。

## 推荐落地顺序

先做专用 `CodexOpsHer`，让飞书成为移动入口；Bridge Server 做中央控制平面；Mac Worker 主动反连，执行本地 Codex。该路线不依赖 ChatGPT 账号，不重复造移动 App 轮子，也不会把 500+ Her 全部暴露成远程命令入口。
