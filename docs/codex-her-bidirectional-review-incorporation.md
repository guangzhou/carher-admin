## 2026-06-06 评论吸收后的 V2 修订

这次评论的核心判断是正确的：原方案方向可行，但第一版不能按“双向完整平台”开工，必须收窄 MVP，并把安全边界从文档原则落到执行层。V2 修订保留“双向能力”的最终目标，但调整落地顺序和默认边界。

### 1. 批判性吸收结论

| 评论点 | 处理 | V2 调整 |
|---|---|---|
| 先做 Her 调本地 Codex，再做 Codex 调 Her | 采纳 | Phase 1 只做 `CodexOpsHer -> Bridge -> Worker -> local Codex`；Codex 调 Her 延后到 Phase 2 |
| 第一版不改 Operator | 采纳 | 不新增 CRD 字段，不做 per-Her env 注入；权限配置先放 Bridge DB/YAML |
| Gateway token 不能复用共享 token | 采纳 | Bridge 在 K8s 内访问 Her gateway；追加 per-Her capability token / mTLS / NetworkPolicy 作为平台化要求 |
| open_id/chat_id 不能信任 Skill 上报 | 采纳 | Bridge 以 per-Her token 和请求来源认证为准；`open_id/chat_id` 是审计字段和策略输入，不是唯一鉴权 |
| app-server 不等于普通远程执行 API | 部分采纳 | MVP 用 app-server 保留原生 stream/approval；长期保留 Codex SDK/CLI worker 备选 |
| 审批不能只靠关键词 | 采纳 | Worker 侧必须有 worktree、命令 allowlist、cwd 限制、超时、kill、diff 限制 |
| 本地 repo 并发会互相污染 | 采纳 | `max_concurrent_per_repo=1`，默认每任务临时 worktree，结果以 diff/patch 回传 |
| Codex 调 Her 权限更复杂 | 采纳 | Phase 2 只开放 read/受控 action；移除第一版 `her_memory_query` |
| Her Gateway Proxy 第一版可不独立做 | 采纳 | Phase 1 Bridge 在 K8s 内直接路由 `carher-{uid}-svc:18789/18795`；Proxy 延后抽象 |

### 2. V2 MVP 范围

第一版只解决一个问题：

```text
飞书里的 CodexOpsHer 安全触发本地 Codex 完成代码任务。
```

MVP 包含：

- 一个 Her：`CodexOpsHer`
- 一个 Worker：`liuguoxian-macbook` 或指定 devbox
- 一个 repo：`carher-admin`
- 命令：`/codex repos`、`/codex <repo> <task>`、`/codex status`、`/codex stop`
- 审批：`allow once`、`deny`
- 执行：临时 worktree、单 repo 串行、超时、diff 摘要
- 审计：session、approval、command、diff summary、operator open_id

MVP 不包含：

- 500+ Her 全量开启
- 普通 Her 默认安装 `/codex` 能力
- Codex 调任意 Her 读群聊/记忆
- Her Pod 内运行 Codex
- 暴露 `codex app-server` 公网端口
- 直接在主工作区并发改代码

关键图 11：V2 MVP 与安全边界

<whiteboard type="blank"></whiteboard>

### 3. 修订后的落地顺序

#### Phase 0：Worker 链路 POC

- Bridge 提供 `/workers/connect`。
- Worker 主动 WSS 反连。
- Worker 本机连接 `codex app-server`。
- Bridge 能创建 thread、接收 stream、处理 stop。
- 不接飞书、不接 Her。

#### Phase 1：Her 调本地 Codex

- 建立 `CodexOpsHer`。
- 安装 `codex-remote-control` Skill 或命令硬路由。
- Bridge 校验 per-Her token，映射出 `her_uid`。
- open_id/chat_id 只作为审计和策略输入。
- Worker 每个任务创建临时 worktree。
- Codex 输出、审批、diff 摘要回飞书。

#### Phase 2：Codex 调 Her

- 实现 `carher-her-mcp`。
- 只开放 `her_list`、`her_status`、`her_send_to_codexops`、`her_wait`。
- 不开放 `her_memory_query` 原始记忆查询。
- 不允许 Codex 直接连 Pod；所有调用仍过 Bridge。

#### Phase 3：平台化

- Bridge 状态接入 carher-admin。
- 增加 Workers、Sessions、Approvals 管理页。
- 增加 repo/worker/her 策略模板。
- 再评估 Her Gateway Proxy、mTLS、NetworkPolicy、per-Her capability token。

### 4. 修订后的接口约束

#### `/api/codex/tasks`

Her Skill 可以提交：

```json
{
  "repo": "carher-admin",
  "prompt": "查 deploy 风险",
  "open_id": "ou_xxx",
  "chat_id": "oc_xxx",
  "message_id": "om_xxx"
}
```

但 Bridge 不信任请求体里的 `her_uid/app_id` 作为事实来源。真实 Her 身份来自：

```text
Authorization: Bearer <per-her-token>
token -> her_uid/app_id/allowed_commands
```

Bridge 校验：

- token 是否有效、是否绑定该 Her。
- Her 是否启用 Codex。
- open_id 是否允许操作该 repo。
- chat_id 是否允许触发此类任务。
- repo 是否在 Worker allowlist 内。

#### Worker 能力注册

Worker 注册时必须声明：

```json
{
  "worker_id": "liuguoxian-macbook",
  "repos": ["carher-admin"],
  "max_concurrent_per_repo": 1,
  "execution_mode": "temporary_worktree",
  "diff_limit_kb": 512,
  "turn_timeout_seconds": 1800,
  "supports_kill_turn": true
}
```

#### Worker 执行硬边界

- 每任务独立 worktree。
- 只允许 repo allowlist 内的 cwd。
- 命令 allowlist 和 denylist 在 Worker 本地执行。
- `git push`、`kubectl apply`、`kubectl set image`、`ssh` 必须二次确认。
- `git reset --hard`、`kubectl delete pod`、`rm -rf`、`curl | sh` 默认拒绝。
- 输出和日志脱敏后再回传 Bridge。
- 超过 diff 大小限制只回摘要，不回完整 patch。

### 5. 对原方案的保留与降级

保留：

- Bridge Server
- Mac Worker 主动反连
- `codex app-server` 本地监听
- `CodexOpsHer`
- `carher-her-mcp` 作为 Phase 2
- 统一审计和审批

降级：

- Her Gateway Proxy 从 Phase 1 降到 Phase 3。
- `her_memory_query` 从第一版工具列表移除。
- Operator/CRD 扩展从第一版移除。
- 多 Her、多 repo、多 Worker 从第一版移除。

### 6. V2 推荐决策

按评论修正后，推荐正式执行顺序是：

```text
Phase 0 POC -> Phase 1 Her 调 Codex MVP -> 稳定运行 -> Phase 2 Codex 调 Her -> Phase 3 平台化
```

一句话：先把“飞书里安全地调本地 Codex 改代码”跑通，并确保 worktree、队列、审批、审计都成立；再补 Codex 调 Her。双向目标保留，但不双向同时开工。
