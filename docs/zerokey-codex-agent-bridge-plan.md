# zerokey 翻译桥 × Codex 全 Agent — 落地方案

> **文档性质**：可执行落地方案（供团队审阅 / PR 讨论）  
> **状态**：Phase 0 已落地（zerokey 对话）；Phase 1–4 待实施（翻译桥）  
> **飞书版**：[zerokey 翻译桥 × Codex 全 Agent 落地方案](https://t83dfrspj4.feishu.cn/docx/C4jcdpP0DoBy55x63TDcDsoynMc)  
> **相关**：[zerokey 索引](./zerokey-codex-artifacts.md) · [主 runbook](./chatgpt-web-to-codex-zerokey.md) · [设计备忘](./zerokey-codex-agent-bridge-design.md)

---

## 1. 背景与目标

### 1.1 要解决的问题

CarHer 团队已有两条 LLM 额度线：

| 线路 | 路径 | 能力 | 限制 |
|------|------|------|------|
| **Codex 5h/7d** | 198 LiteLLM → chatgpt-acct 池 → `/backend-api/codex/responses` | 全 Agent（apply_patch、shell） | 5h 滚动 + 周 cap |
| **网页 Chat 额度** | 188 zerokey → `f/conversation` → 198 `zerokey-*` 模型 | 对话 / 补代码正常 | Agent 工具链不完整 |

**缺口**：Codex CLI 2026 起只走 `/v1/responses`，依赖 `apply_patch`（freeform `custom_tool_call`）和 `shell`。当前 zerokey 经 LiteLLM + `Bearer raw` 会 **丢弃工具链**，无法完成本地改文件 Agent loop。

### 1.2 本方案目标

新增专用 **翻译桥**（`zerokey-codex-responses-bridge`），让 Codex CLI 在 **不消耗 Codex 5h/7d** 的前提下，用 **网页 Chat 额度** 跑完整 Agent：

- 本地 `apply_patch` diff UI + sandbox 执行
- `shell` / 多轮 tool 循环
- hooks、审批、rollout 日志等与现 Codex 工作流一致

### 1.3 成功标准（Definition of Done）

| 级别 | 验收项 | 通过条件 |
|------|--------|----------|
| P0 | 单文件创建 | `codex -p zerokey-agent` 在空目录创建文件，TUI 出现 apply_patch diff |
| P1 | 多文件 patch | 跨 3+ 文件 refactor，V4A hunk 正确反译 |
| P1 | shell 工具 | 跑 pytest / npm test 并据结果继续改代码 |
| P2 | 长会话 | 20+ 轮 tool 不丢 history、不串会话 |
| 运维 | 隔离 | 198 LiteLLM 其它用户与 zerokey 对话路径零回归 |

### 1.4 明确不做

- 不把翻译桥塞进 LiteLLM `model_list`（避免再次 drop tools）
- 不用 gpt2agent MCP 替代主 Agent loop（远程沙箱 ≠ 本地 Codex）
- 不改动 carher-admin / operator / bot 部署流水线

---

## 2. 现状与缺口

### 2.1 已落地（Phase 0）

| 组件 | 位置 | 能力 |
|------|------|------|
| zerokey kristine | 188 `:8123` | raw 对话 + vscode ToolCompiler |
| zerokey timothy | 188 `:8124` | 同上，独立 session |
| LiteLLM 8 模型 | 198 `:30402` | `zerokey-*` + `use_chat_completions_api: true` |
| session 刷新 | 188 cron 6h | `refresh.sh` 原子换 users.json |
| Codex 对话 profile | Mac `config.toml` | `wire_api=responses` → 198 → zerokey 模型 |

### 2.2 为何 LiteLLM 现有 zerokey 不够

| 环节 | 现象 |
|------|------|
| Codex CLI | 只发 `/v1/responses`，工具含 `apply_patch`（custom_tool_call） |
| LiteLLM 桥 | `use_chat_completions_api` 丢弃 apply_patch / shell / custom（[PR #28696](https://github.com/BerriAI/litellm/pull/28696)、[#29281](https://github.com/BerriAI/litellm/pull/29281)） |
| zerokey `Bearer raw` | 无 ToolCompiler；消息压平为纯文本 |
| 结果 | 对话 OK；Agent 改文件链断裂 |

### 2.3 诊断三段式（已证伪「LiteLLM+raw 即可 Agent」）

- **假设**：LiteLLM 桥 + zerokey raw 可实现 Codex Agent。
- **证伪条件**：若成立，上游应保留 structured tool_calls；Codex 侧应收到 `custom_tool_call` SSE。
- **数据**：LiteLLM 源码 drop tools + raw 无 parser → 假设不成立 → **必须绕开 LiteLLM 做专用桥**。

### 2.4 zerokey 两条上游路径对比

| Authorization | 行为 | Agent 桥应走哪条 |
|---------------|------|-------------------|
| `Bearer raw` | 无工具语法；无状态全量 history | ❌ 不够 |
| `Bearer vscode` | ToolCompiler + 有状态 web session | ⚠️ PoC 可用，workspace XML 偏重 |
| `Bearer codex`（待实现） | ToolCompiler + 无状态 + Codex 轻量 profile | ✅ 目标上游 |

---

## 3. 目标架构

### 3.1 端到端数据流

```text
┌─────────────────────────────────────────────────────────────────┐
│  Mac 开发者                                                      │
│  Codex CLI (wire_api=responses)                                 │
│       │ POST /v1/responses                                      │
│       ▼                                                         │
│  zerokey-codex-responses-bridge  127.0.0.1:8788                 │
│       │ ① Responses input → chat messages                       │
│       │ ② Authorization: Bearer codex（PoC 可用 vscode）       │
│       │ ③ tool_calls → custom_tool_call (apply_patch V4A)      │
└───────┼─────────────────────────────────────────────────────────┘
        │ POST /v1/chat/completions
        ▼
┌─────────────────────────────────────────────────────────────────┐
│  188 JSZX-AI-03                                                  │
│  zerokey :8123 | :8124  ←  capture + refresh.sh (cron 6h)       │
└───────┼─────────────────────────────────────────────────────────┘
        │ /backend-api/f/conversation + ToolCompiler ⟦tool¦param⟧
        ▼
   chatgpt.com（网页 Chat 额度）

┌─────────────────────────────────────────────────────────────────┐
│  198 LiteLLM Pro — 不变                                          │
│  zerokey-* 对话模型 → Bearer raw → 8123/8124                    │
│  （Cursor / 其它用户；Agent 桥不走此路）                          │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 组件职责

| 组件 | 职责 | 部署位置 |
|------|------|----------|
| Codex CLI | Agent loop、本地 sandbox、apply_patch 执行 | Mac |
| 翻译桥 | Responses ↔ Chat Completions；tool 双向反译；SSE 对齐 | Mac 本地（PoC）→ 可选 198 sidecar |
| zerokey | OAuth/sentinel 回放；ToolCompiler 注入/解析 | 188 Docker |
| 198 LiteLLM | **仅** zerokey 对话/补代码；**不**承载 Agent 桥 | K3s `litellm-product` |

### 3.3 与现有路径的关系

**保留不变**

- 198 → zerokey 8 模型（对话）
- chatgpt-acct Codex 5h/7d 池
- 188 capture / refresh 栈

**新增**

- Mac 本地 bridge `:8788`
- zerokey `Bearer codex` 模式
- Codex profile `zerokey-agent`

### 3.4 为何桥先放 Mac 而非 LiteLLM

1. Agent 流量个人化、低并发；loopback 延迟最低
2. 避免污染 198 共享 proxy 配置
3. 桥接逻辑需频繁迭代；Mac 调试快
4. 稳定后可做 198 sidecar（独立 NodePort，仍不进 LiteLLM model_list）

---

## 4. 翻译桥设计

### 4.1 协议方向

| 方向 | 输入 | 输出 |
|------|------|------|
| Codex → 桥 → zerokey | `POST /v1/responses`：`input[]`、tools、stream | `POST /v1/chat/completions`：messages、function tools |
| zerokey → 桥 → Codex | SSE：OpenAI chat delta + tool_calls | SSE：`response.custom_tool_call_input.delta` 等 Responses 事件 |

### 4.2 工具反译矩阵（核心）

| zerokey ToolCompiler 输出 | Codex 期望 | 备注 |
|---------------------------|------------|------|
| `replace_string_in_file` | `custom_tool_call` name=`apply_patch` | 拼 V4A unified diff 到 `input` |
| `create_file` | `apply_patch`（Add File hunk） | 空文件 + 全文 |
| `delete_file` | `apply_patch`（Delete File） | 需完整测试矩阵 |
| `run_in_terminal` | `shell` / `shell_command` | stdout/stderr 回填 assistant |
| `read_file` / `list_dir` / `grep_search` | Codex 内置 read 类 tool | 若 Codex 启用则直传；否则桥本地执行后注入 |

### 4.3 SSE 硬性要求

流式 **必须** 发 `response.custom_tool_call_input.delta`，**禁止** 降级为 `function_call` + 空 `{}`。

参考：[9router #1371](https://github.com/decolua/9router/issues/1371) — Codex TUI 依赖 freeform patch 流式输入。

### 4.4 推荐 fork 基座

| 项目 | 语言 | 选用理由 |
|------|------|----------|
| [deepseek-responses-proxy](https://github.com/holo-q/deepseek-responses-proxy) | Python | 已处理 apply_patch freeform；**PoC 首选** |
| [api2codex](https://github.com/talkcozy/api2codex) | Python/FastAPI | 单文件，易改 upstream |
| [va-ai-api-bridge](https://github.com/jazzenchen/va-ai-api-bridge) | Rust | 长期稳定可选 |

共同改动：upstream 换成 `http://10.68.13.188:8123/v1`，Header `Authorization: Bearer codex`（PoC 阶段可用 `vscode`）。

### 4.5 V4A apply_patch 反译算法（要点）

1. 解析 ToolCompiler 块：`⟦replace_string_in_file¦path¦old¦new⟧`
2. 生成 V4A：`*** Update File: path` + context lines + `-old` + `+new`
3. 多 hunk 合并为单次 `apply_patch` input（Codex 偏好单 call）
4. 流式：按字符/chunk 推送 `custom_tool_call_input.delta`
5. tool 结果回传：Codex sandbox 输出 → chat `tool` role message → 下一轮 upstream

### 4.6 桥服务默认配置

```yaml
listen: 127.0.0.1:8788
upstream_base: http://10.68.13.188:8123/v1   # timothy:8124
upstream_auth: codex                          # Phase2 前用 vscode
default_model: gpt-5-5
timeout_seconds: 300
stream: true
```

---

## 5. zerokey 侧改动

### 5.1 新增 Bearer codex 模式

在 `zerokey-patch/routes/chatgpt.js` 中：**不要**把 `codex` 放进现有 `RAW_IDES`（那会走 raw 无工具）。

当前 `RAW_IDES` 含 `codex`，实现桥之前需 **从 RAW_IDES 移除 `codex`**，改为独立分支。

### 5.2 行为规格

| 项 | vscode（现） | codex（新） |
|----|--------------|-------------|
| ToolCompiler | ✅ | ✅ |
| 会话 | 有状态 parentMessageId | **无状态**：每轮 chatSessionId=null，全量 history |
| Prompt 包装 | VS Code workspace XML | Codex 轻量 profile（弱化 IDE XML） |
| 模型透传 | ✅ | ✅ |

### 5.3 代码改动清单

- [ ] `routes/chatgpt.js`：codex 分支 → 新 handler（ToolCompiler + stateless）
- [ ] `zerokey-serve-codex.js`：日志说明 Bearer codex
- [ ] `lib/engine` 或 IDE profile：新增 codex prompt 模板
- [ ] 188 上 rebuild 容器 + 冒烟 curl
- [ ] 文档同步 [zerokey-codex-artifacts.md](./zerokey-codex-artifacts.md)

### 5.4 188 部署命令

```bash
cd ~/zerokey-codex
# 从 carher-admin 同步 zerokey-patch 后
docker compose build zerokey && docker compose up -d zerokey
curl -s localhost:8123/health

# 验证 ToolCompiler + codex bearer（示例）
curl -N -X POST localhost:8123/v1/chat/completions \
  -H "Authorization: Bearer codex" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5-5","messages":[{"role":"user","content":"List files in ."}],"stream":true}'
```

---

## 6. 分阶段实施

### 6.1 总览

| Phase | 内容 | 负责 | 预估 |
|-------|------|------|------|
| 0 | zerokey 对话 + 198 注册 | — | **已完成** |
| 1 | 188 验证 vscode/codex ToolCompiler SSE | 运维 | 0.5d |
| 2 | Mac fork 桥 PoC + 单文件 Agent | 开发 | 2–3d |
| 3 | zerokey Bearer codex + V4A 完整矩阵 | 开发 | 2d |
| 4 | 可选 198 sidecar + 内网 Codex profile | 运维 | 1d |

### 6.2 Phase 1 — 上游形态验证

- [ ] 188 curl：`Bearer vscode` 多轮带 tool 请求，保存 SSE 样例
- [ ] 确认 ToolCompiler 输出工具名与 §4.2 矩阵一致
- [ ] 确认 stream 结束时有完整 tool_calls finish_reason
- [ ] 记录失败模式：网页限流、sentinel 403、session 过期

### 6.3 Phase 2 — Mac 桥 PoC

- [ ] fork [deepseek-responses-proxy](https://github.com/holo-q/deepseek-responses-proxy)
- [ ] 改 upstream → `188:8123` + `Bearer vscode`
- [ ] 实现 `replace_string_in_file` → `apply_patch` 最小反译
- [ ] 启动 bridge `:8788`，curl `/v1/responses` 非流 + 流式
- [ ] Codex profile `zerokey-agent` → `http://127.0.0.1:8788/v1`
- [ ] 空目录单文件创建回归（P0 DoD）

### 6.4 Phase 3 — zerokey codex 模式 + 加固

- [ ] 合入 Bearer codex 无状态 profile（含从 RAW_IDES 移除 codex）
- [ ] 桥切换 `upstream_auth=codex`
- [ ] 多文件 / delete_file / 大 patch 测试矩阵
- [ ] shell 工具往返 10 轮稳定性
- [ ] 仓库脚本 + skill 更新

### 6.5 Phase 4 — 可选生产化

- [ ] 198 部署 bridge sidecar（独立 Deployment，NodePort 8788，仅 VPN/内网）
- [ ] Mac config 改指向 198 sidecar（出差/多机）
- [ ] 监控：bridge 5xx、zerokey health、session 年龄

### 6.6 里程碑门禁

| 里程碑 | 动作 |
|--------|------|
| Phase 2 完成 | 团队内 1 人 Dogfood 1 周 |
| Phase 3 完成 | 文档定稿 + 可选替换部分 Codex 5h/7d 日常开发 |
| Phase 4 | 仅当多人需要内网 sidecar 时做 |

---

## 7. 部署与配置

### 7.1 拓扑与端口

| 节点 | 服务 | 地址 | 说明 |
|------|------|------|------|
| Mac | zerokey-codex-responses-bridge | `127.0.0.1:8788` | Codex 专用，不经 LiteLLM |
| 188 | zerokey kristine | `10.68.13.188:8123` | Agent 桥默认 upstream |
| 188 | zerokey timothy | `10.68.13.188:8124` | 备用账号 |
| 198 | LiteLLM Pro | `10.68.13.198:30402` | 仅 zerokey 对话，Agent 不走此路 |

### 7.2 Mac — 翻译桥启动（PoC）

```bash
cd ~/zerokey-codex-responses-bridge
python -m venv .venv && source .venv/bin/activate
pip install -e .

export BRIDGE_UPSTREAM=http://10.68.13.188:8123/v1
export BRIDGE_UPSTREAM_AUTH=codex    # Phase2 前用 vscode
export BRIDGE_LISTEN=127.0.0.1:8788

uvicorn bridge.main:app --host 127.0.0.1 --port 8788
```

Mac 需能访问 188 内网（VPN / 跳板 `./scripts/jms`）。**不可**公网暴露 bridge。

### 7.3 Mac — Codex config.toml

```toml
# 对话仍用现 zerokey LiteLLM profile
[profiles.zerokey]
model = "zerokey-gpt-5.5"
model_provider = "carher_dev"

# Agent + 网页额度（翻译桥）
[profiles.zerokey-agent]
model = "gpt-5-5"
model_provider = "zerokey_bridge"

[model_providers.zerokey_bridge]
name = "zerokey Agent Bridge"
base_url = "http://127.0.0.1:8788/v1"
wire_api = "responses"
requires_openai_auth = false
```

### 7.4 使用方式

```bash
# 日常对话（LiteLLM zerokey）
codex -p zerokey

# 本地 Agent 改代码（网页额度）
codex -p zerokey-agent

# 5h/7d 未耗尽时仍用 chatgpt-acct
codex -p default
```

### 7.5 198 sidecar（Phase 4 可选）

- 独立 Deployment `zerokey-bridge`，镜像自建 push ACR VPC
- NodePort 例如 `30788`，**不**写入 `litellm-config`
- 环境变量 upstream 指向 `188:8123`
- 仅内网/VPN 访问；不做 cloudflared 公网暴露

---

## 8. 验证与回归

### 8.1 分层验证

| 层 | 命令/动作 | 期望 |
|----|-----------|------|
| 188 health | `curl localhost:8123/health` | 200 OK |
| zerokey codex SSE | chat/completions + Bearer codex + stream | 见 tool_calls delta |
| bridge /v1/responses | curl `127.0.0.1:8788` | Responses JSON/SSE 形态 |
| Codex P0 | 空目录 `codex -p zerokey-agent` 创建 README | TUI apply_patch diff |
| 198 回归 | `zerokey-gpt-5.5` responses 仍 OK | 不影响其它用户 |

### 8.2 bridge 冒烟（Mac）

```bash
curl -s -X POST http://127.0.0.1:8788/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5-5","input":"Say OK-ZK-AGENT","stream":false}'

curl -N -X POST http://127.0.0.1:8788/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5-5","input":"Create hello.txt with hi","stream":true}'
```

### 8.3 Agent 回归用例

- [ ] P0：单文件 create + write
- [ ] P1：修改已有文件 3 处 hunk
- [ ] P1：run shell pytest 并根据失败修复
- [ ] P2：10 文件 repo 小 refactor
- [ ] 负例：patch 冲突时 Codex 能否 self-heal

### 8.4 198 LiteLLM 隔离回归

在 `AIYJY-litellm` 上执行：

```bash
MK=$(kubectl get secret litellm-secrets -n litellm-product -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
curl -s -X POST -H "Authorization: Bearer $MK" localhost:30402/v1/responses \
  -d '{"model":"zerokey-gpt-5.5","input":"Say OK","stream":false}'
curl -s -H "Authorization: Bearer $MK" localhost:30402/v1/models | grep zerokey
```

---

## 9. 运维与刷新

### 9.1 session 生命周期（188）

Agent 桥 **不改变** 现有 refresh 逻辑。188 cron 每 6h：

1. `refresh.sh` → capture → 校验 → 原子替换 `users.json` → 重启 zerokey
2. kristine `:8123`、timothy `:8124` 各自独立
3. `cf_clearance` 绑定 188 出口 IP，capture 必须在 188 执行

### 9.2 故障 playbook

| 症状 | 可能原因 | 动作 |
|------|----------|------|
| 403 / sentinel | session 过期、IP 漂移 | 手动 `capture-manual.sh` 或等 cron |
| Empty SSE | 网页限流、模型 slug 错 | 查 zerokey 日志；换 timothy 端口试 |
| tool 乱码 | 桥反译 bug | 抓 SSE 样例；fix V4A 映射 |
| Codex 无 diff | 降级为 function_call | 检查 custom_tool_call delta |
| 198 zerokey 404 | stale manifest apply | `litellm-register-zerokey.py --apply --sync-manifest` |

### 9.3 与 LiteLLM 变更隔离

- 改 zerokey 条目 **只** 用 `litellm-register-zerokey.py`
- **禁止** `kubectl apply` 旧 manifest 覆盖 live cm
- 翻译桥 **禁止** 注册进 litellm model_list

### 9.4 监控建议

- 188：`/health` + Docker restart 计数
- bridge：请求延迟 P95、upstream 5xx 率
- 业务：Agent 任务成功率（weekly spot check）

---

## 10. 风险、回滚与决策

### 10.1 风险矩阵

| 风险 | 影响 | 缓解 |
|------|------|------|
| 网页模型非 Codex 训练分布 | 复杂 repo Agent 成功率低于官方 Codex 后端 | 保留 chatgpt-acct 5h/7d 作 fallback |
| OpenAI 改 web 协议 | capture + ToolCompiler 同时挂 | refresh 告警；upstream zerokey 版本 pin |
| V4A 反译边界 bug | 错误 patch 破坏文件 | git 干净工作区；Codex 审批；测试矩阵 |
| 账号 ToS | 网页自动化风险 | 专用 Plus 号；不用于生产 bot |
| 并发限流 | 多人共端口 session 争用 | Agent 桥个人 Mac；高并发加账号端口 |

### 10.2 回滚步骤

1. Codex 改回 `-p zerokey`（仅对话）或 chatgpt-acct（全 Agent + 5h/7d）
2. 停止 Mac bridge 进程
3. 188 zerokey 若 codex 模式异常 → revert patch + `docker compose rebuild`
4. 198 若误改 cm → 从 `~/zerokey-litellm-backups/` 恢复

### 10.3 与 gpt2agent 的边界

| | 翻译桥 + zerokey | gpt2agent MCP |
|--|------------------|-----------------|
| 角色 | Codex **主 model** | 附加 MCP 工具 |
| 本地 apply_patch | ✅ | ❌ |
| 额度 | 网页 Chat（经 zerokey） | 网页 Chat（MCP 调用时） |
| 推荐 | **本方案 — 全 Agent 目标** | DR/调研副脑，非替代 |

### 10.4 架构决策记录（ADR）

| ID | 决策 |
|----|------|
| ADR-001 | Agent 网页额度走专用 Responses 桥，不经 LiteLLM |
| ADR-002 | PoC 桥部署 Mac 本地；198 sidecar 延后 |
| ADR-003 | zerokey 新增 Bearer codex 无状态 ToolCompiler，不污染 raw/vscode 路径 |

---

## 11. 附录

### 11.1 仓库路径

| 类型 | 路径 |
|------|------|
| 索引 | `docs/zerokey-codex-artifacts.md` |
| 主 runbook | `docs/chatgpt-web-to-codex-zerokey.md` |
| 设计备忘 | `docs/zerokey-codex-agent-bridge-design.md` |
| **本落地方案** | `docs/zerokey-codex-agent-bridge-plan.md` |
| 脚本 bundle | `scripts/chatgpt-onboard/zerokey-codex/` |
| 198 注册 | `ops/litellm-register-zerokey.py` |
| Skill | `.codex/skills/chatgpt-web-to-codex-zerokey/SKILL.md` |

### 11.2 常用命令速查

```bash
# JMS 跳板（勿裸 jms）
./scripts/jms ssh JSZX-AI-03
./scripts/jms ssh AIYJY-litellm

# 198 注册 zerokey
./scripts/jms scp scripts/chatgpt-onboard/zerokey-codex/ops/litellm-register-zerokey.py \
  AIYJY-litellm:/tmp/litellm-register-zerokey.py
./scripts/jms ssh AIYJY-litellm \
  'python3 /tmp/litellm-register-zerokey.py --apply --sync-manifest'

# 188 新账号
cd ~/zerokey-codex/ops && ./add-account.sh <id> <email> '<mail_pw>' '<chatgpt_pw>' [port]
```

### 11.3 参考链接

- [deepseek-responses-proxy](https://github.com/holo-q/deepseek-responses-proxy)
- [api2codex](https://github.com/talkcozy/api2codex)
- [LiteLLM #28696](https://github.com/BerriAI/litellm/pull/28696) — drop apply_patch
- [9router #1371](https://github.com/decolua/9router/issues/1371) — custom_tool_call delta

---

*文档版本：2026-06-18 · 与飞书版同步 · 关联 zerokey 多账号栈（commit 483b4aa+）*
