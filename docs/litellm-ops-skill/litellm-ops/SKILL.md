---
name: litellm_ops
description: CarHer LiteLLM Proxy 运维管理 — 模型路由、虚拟 Key 管理、费用追踪、健康检查、路由策略。
---

# LiteLLM Proxy 运维技能

你可以通过调用 LiteLLM Proxy 原生 API 来管理 LLM 模型路由、虚拟 Key 和费用追踪。使用 `exec` 工具执行 curl 命令调用 API。

## 集群架构

### LiteLLM Proxy 部署

| 组件 | 描述 |
|------|------|
| **Deployment** | `litellm-proxy`，1 副本，image: `ghcr.io/berriai/litellm`，版本 1.82.3 |
| **Service** | `litellm-proxy.carher.svc:4000` (ClusterIP) |
| **Database** | `litellm-db` StatefulSet，PostgreSQL，`litellm-db.carher.svc:5432` |
| **ConfigMap** | `litellm-config`：模型列表、路由、fallback 配置 |
| **Secrets** | `litellm-secrets`：LITELLM_MASTER_KEY、DATABASE_URL |
| | `carher-env-keys`：OPENROUTER_API_KEY、WANGSU_API_KEY、WANGSU_DIRECT_API_KEY 等 |
| **资源** | requests: 200m CPU / 512Mi，limits: 2 CPU / 2Gi |
| **探针** | liveness: `/health/liveliness`，readiness: `/health/readiness`，初始延迟 90s |

### 模型路由表

LiteLLM 充当统一 LLM 网关，所有 Her 实例通过 LiteLLM 虚拟 Key 访问模型。

#### Sonnet / Opus — Wangsu Anthropic Direct 主，OpenRouter 备

| 模型名 | 后端 Provider | 上游模型 | 输入成本 $/token | 输出成本 $/token |
|--------|-------------|---------|-----------------|-----------------|
| `claude-opus-4-6` | Wangsu Anthropic Direct (v2) | `anthropic/anthropic.claude-opus-4-6` | $0.000005 | $0.000025 |
| `claude-sonnet-4-6` | Wangsu Anthropic Direct (v2) | `anthropic/anthropic.claude-sonnet-4-6` | $0.000003 | $0.000015 |

Fallback（Wangsu Direct 不可用时自动切换到 OpenRouter）：

| 模型名 | 上游模型 |
|--------|---------|
| `openrouter-claude-opus-4-6` | `openrouter/anthropic/claude-opus-4.6` |
| `openrouter-claude-sonnet-4-6` | `openrouter/anthropic/claude-sonnet-4.6` |

#### GPT / Gemini — OpenRouter 主，Wangsu OpenAI 兼容备

| 模型名 | 后端 Provider | 上游模型 | 输入成本 $/token | 输出成本 $/token |
|--------|-------------|---------|-----------------|-----------------|
| `gpt-5.4` | OpenRouter | `openai/gpt-5.4` | $0.0000025 | $0.000015 |
| `gemini-3.1-pro-preview` | OpenRouter | `google/gemini-3.1-pro-preview` | $0.000002 | $0.000012 |

Fallback（OpenRouter 不可用时自动切换到 Wangsu OpenAI 兼容模式）：

| 模型名 | 上游模型 | API Base |
|--------|---------|----------|
| `wangsu-gpt-5.4` | `custom_openai/gpt-5.4` | `aigateway.edgecloudapp.com/v1/.../cheliantianxia1` |
| `wangsu-gemini-3.1-pro-preview` | `custom_openai/gemini-3.1-pro-preview` | 同上 |

#### Wangsu Anthropic Direct（原生 Anthropic 协议，用于 Cursor/Claude Code）

| 模型名 | 上游模型 |
|--------|---------|
| `anthropic.claude-sonnet-4-6` | `anthropic/anthropic.claude-sonnet-4-6` |
| `anthropic.claude-opus-4-6` | `anthropic/anthropic.claude-opus-4-6` |

#### 仅 OpenRouter 模型（无 Wangsu fallback）

| 模型名 | 后端模型 | 输入成本 | 输出成本 |
|--------|---------|---------|---------|
| `minimax-m2.7` | `openrouter/minimax/minimax-m2.7` | $0.0000005 | $0.0000015 |
| `glm-5` | `openrouter/z-ai/glm-5` | $0.000001 | $0.000003 |
| `gpt-5.3-codex` | `openrouter/openai/gpt-5.3-codex` | $0.000003 | $0.000015 |

#### 网宿 cheliantianxia1 直通（无 fallback，2026-05-08 新增）

| 模型名 | 上游模型 | API Base | 输入成本 | 输出成本 |
|--------|---------|----------|---------|---------|
| `wangsu-gpt-5.5` | `custom_openai/gpt-5.5` | `aigateway.edgecloudapp.com/v1/.../cheliantianxia1` | $0.000005 | $0.00003 |
| `wangsu-deepseek-v4-pro` | `custom_openai/deepseek-v4-pro` | 同上 | $0.00000171 | $0.00000343 |
| `wangsu-deepseek-v4-flash` | `custom_openai/deepseek-v4-flash` | 同上 | $0.000000143 | $0.000000286 |

#### Embedding

| 模型名 | 后端模型 |
|--------|---------|
| `BAAI/bge-m3` | `openrouter/BAAI/bge-m3` |

### Fallback 路由策略

路由策略现已固定，`litellm_route_policy` 字段为 legacy 保留。

```
claude-opus-4-6        → Wangsu Direct 主 → fallback: openrouter-claude-opus-4-6
claude-sonnet-4-6      → Wangsu Direct 主 → fallback: openrouter-claude-sonnet-4-6
gpt-5.4                → OpenRouter 主    → fallback: wangsu-gpt-5.4
gemini-3.1-pro-preview → OpenRouter 主    → fallback: wangsu-gemini-3.1-pro-preview
```

### 全局设置

| 设置 | 值 |
|------|-----|
| `drop_params` | true（自动丢弃不支持的参数） |
| `num_retries` | 2 |
| `request_timeout` | 300s |
| `stream_timeout` | 120s |
| `store_model_in_db` | false（模型配置来自 ConfigMap，非 DB） |

### CarHer 虚拟 Key 机制

每个 Her 实例有一个独立虚拟 Key（格式 `sk-...`），Key alias 为 `carher-{uid}`。通过 Admin API 或直接调用 LiteLLM API 创建。

每个 Key 绑定：
- `user_id`: `carher-{uid}`
- `models`: 所有可用模型（含 fallback model groups）
- `aliases`: `{}`（不再使用别名映射）
- `router_settings.fallbacks`: 固定 fallback 配置

## API 调用方式

使用 exec 工具执行 curl 命令。所有管理 API 需要 Master Key 认证。
**Master Key 必须从 K8s secret 现取**（密钥会轮换，不要硬编码）：

```bash
MK=$(kubectl get secret litellm-secrets -n carher \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)

curl -s http://litellm-proxy.carher.svc:4000/health/readiness \
  -H "Authorization: Bearer $MK"
```

**Base URL**: `http://litellm-proxy.carher.svc:4000`
**认证**: `Authorization: Bearer $MK`（从 secret 取，见上）

## ChatGPT 上游配额查询

ChatGPT acct 池已迁移到 198 K3s。默认只看 198 上的聚合状态，不再把旧 188 docker 或阿里云 K8s 探测结果作为当前生产池口径。

```bash
./scripts/chatgpt-acct-quota.sh           # 完整列表：acct 邮箱、状态、5h/7d 百分比、reset 距离、订阅到期时间、剩余天数
./scripts/chatgpt-acct-quota.sh --summary # 完整列表 + grouped counts
./scripts/chatgpt-acct-quota.sh --json    # 原始 state.json
```

数据源是 `JSZX-AI-03:/home/cltx/.chatgpt-quota/state/state.json`，由远端 `quota-rebalance.py` 每 5 分钟维护。`take/online/paused/offline` 以 state 里的 `paused` 和 `manual_offline` 为准；不要在本地用固定 95% 阈值二次推断，否则 manual resume 到 99% 后会误报为不可接流量。

默认必须直接运行 `./scripts/chatgpt-acct-quota.sh` 并原样输出完整列表；不要临时拼 `jms`、`kubectl`、`python` 或 heredoc 重做表格。用户明确要汇总时再使用 `--summary`。
脚本优先使用仓库内 `scripts/jms`，避免 PATH 上旧 `jms` 入口导致 JumpServer token 登录失败。
shell wrapper 会把 `scripts/chatgpt_acct_quota_view.py` 传到 JSZX-AI-03 执行；渲染和邮箱解析逻辑统一维护在 Python 脚本里，不要再把大段 heredoc 塞回 shell。
邮箱列运行时从可读 `.creds` 或 198 pod `auth.json` claims 解出；不要在 skill 文档中硬编码真实邮箱。

旧脚本 `scripts/chatgpt-acct-usage.sh` 只保留 legacy/raw 调试入口，默认会转到 `scripts/chatgpt-acct-quota.sh`。

## 安全规则

- 不暴露 Master Key、API Key 等敏感值给用户
- 删除/重置 Key 前先确认影响范围
- 不随意 reset 全局 spend（不可逆）
- 修改模型配置前确认不影响在线实例
- block key 前确认用户知晓该操作会中断服务

---

## 完整 API 参考

### 健康检查

| 用途 | 方法 | 路径 | 说明 |
|------|------|------|------|
| Readiness | GET | `/health/readiness` | 快速就绪检查，返回版本、DB 连接状态 |
| Liveness | GET | `/health/liveliness` | 存活检查 |
| 全面健康检查 | GET | `/health` | 对所有模型发真实请求测试，耗时较长（~45s），返回 healthy/unhealthy endpoints |
| 健康检查历史 | GET | `/health/history?model=xxx&limit=10` | 历史健康检查记录 |
| 最近健康状态 | GET | `/health/latest` | 最近一次健康检查快照 |
| 服务健康 | GET | `/health/services?service=datadog` | 外部服务检查 |
| 测试模型连接 | POST | `/health/test_connection` | body: `{"litellm_params": {"model": "...", "api_key": "..."}}` |
| 活跃回调 | GET | `/active/callbacks` | 当前注册的回调列表 |

### Key 管理

| 用途 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 创建 Key | POST | `/key/generate` | body 见下方 |
| Key 详情 | GET | `/key/info?key=sk-xxx` | 查询单个 key 信息（spend、models、metadata） |
| Key 列表 | GET | `/key/list?page=1&size=50` | 分页列表，返回 `{keys, total_count, current_page, total_pages}` |
| Key 别名列表 | GET | `/key/aliases?page=1&size=50&search=carher` | 按别名搜索 |
| 更新 Key | POST | `/key/update` | body: `{"key": "sk-xxx", ...fields}` |
| 批量更新 | POST | `/key/bulk_update` | body: `{"keys": ["sk-a","sk-b"], ...fields}` |
| 删除 Key | POST | `/key/delete` | body: `{"keys": ["sk-xxx"]}` |
| 重新生成 | POST | `/key/regenerate?key=sk-xxx` | 生成新 key 值，旧值作废 |
| 封禁 Key | POST | `/key/block` | body: `{"key": "sk-xxx"}` |
| 解封 Key | POST | `/key/unblock` | body: `{"key": "sk-xxx"}` |
| Key 健康 | POST | `/key/health` | body: `{"keys": ["sk-xxx"]}` |
| 重置 Key 费用 | POST | `/key/{key}/reset_spend` | 将该 key 的 spend 归零 |

#### 创建 Key body 示例

```json
{
  "user_id": "carher-100",
  "key_alias": "carher-100",
  "metadata": {
    "instance": "carher-100",
    "owner_name": "用户名",
    "litellm_route_policy": "openrouter_first"
  },
  "models": [
    "claude-opus-4-6", "claude-sonnet-4-6", "gpt-5.4",
    "gemini-3.1-pro-preview", "minimax-m2.7", "glm-5",
    "gpt-5.3-codex", "BAAI/bge-m3",
    "wangsu-gpt-5.5", "wangsu-deepseek-v4-pro", "wangsu-deepseek-v4-flash",
    "openrouter-claude-opus-4-6", "openrouter-claude-sonnet-4-6",
    "wangsu-gpt-5.4", "wangsu-gemini-3.1-pro-preview"
  ],
  "aliases": {},
  "router_settings": {
    "fallbacks": [
      {"claude-opus-4-6": ["openrouter-claude-opus-4-6"]},
      {"claude-sonnet-4-6": ["openrouter-claude-sonnet-4-6"]},
      {"gpt-5.4": ["wangsu-gpt-5.4"]},
      {"gemini-3.1-pro-preview": ["wangsu-gemini-3.1-pro-preview"]}
    ]
  }
}
```

`litellm_route_policy` 字段为 legacy 保留，不再影响实际路由。所有 Key 使用相同的固定 fallback 配置。

### 费用追踪

| 用途 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 各 Key 费用排行 | GET | `/spend/keys?limit=50` | 按 spend 降序，返回 `[{token, key_name, key_alias, spend}]` |
| 请求级费用日志 | GET | `/spend/logs?api_key=sk-xxx&start_date=2026-04-01&end_date=2026-04-12` | 单条请求粒度 |
| 费用日志 v2 | GET | `/spend/logs/v2?user_id=carher-100&min_spend=0.01` | 更灵活筛选 |
| 按标签统计 | GET | `/spend/tags?start_date=2026-04-01&end_date=2026-04-12` | 按 tag 聚合 |
| 全局费用报告 | GET | `/global/spend/report?group_by=api_key&start_date=2026-04-01&end_date=2026-04-12` | 按 key/user/team 聚合 |
| 全局标签费用 | GET | `/global/spend/tags?start_date=2026-04-01&end_date=2026-04-12` | 全局按 tag 聚合 |
| 费用预估 | POST | `/spend/calculate` | body: `{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"..."}]}` |
| 全局费用重置 | POST | `/global/spend/reset` | **危险操作**：清零所有 spend 记录 |

### 模型管理

| 用途 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 模型列表 | GET | `/v1/models` | OpenAI 兼容格式，返回 model id 列表 |
| 模型详情 | GET | `/model/info` | 所有模型的完整配置信息（含 cost、provider） |
| 单模型详情 | GET | `/model/info?litellm_model_id=xxx` | 指定模型 |
| 模型组信息 | GET | `/model_group/info` | 各模型组的 provider、TPM/RPM |
| 添加模型 | POST | `/model/new` | body: `{"model_name":"...","litellm_params":{"model":"...","api_key":"..."}}` |
| 更新模型 | POST | `/model/update` | 更新现有模型配置 |
| 删除模型 | POST | `/model/delete` | body: `{"id":"model-id"}` |

### 路由管理

| 用途 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 路由设置 | GET | `/router/settings` | 当前路由策略、可用字段、描述 |
| Fallback 列表 | GET | `/fallback/{model}` | 查看指定模型的 fallback 链 |
| 创建 Fallback | POST | `/fallback` | body: `{"model":"xxx","fallbacks":["yyy"]}` |
| 删除 Fallback | DELETE | `/fallback/{model}` | 移除指定模型的 fallback |

### 预算管理

| 用途 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 创建预算 | POST | `/budget/new` | body: `{"max_budget": 100.0, "budget_duration": "monthly"}` |
| 更新预算 | POST | `/budget/update` | 修改已有预算 |
| 预算详情 | POST | `/budget/info` | body: `{"budgets": ["budget-id"]}` |
| 预算列表 | GET | `/budget/list` | 所有预算 |
| 删除预算 | POST | `/budget/delete` | body: `{"id": "budget-id"}` |

### 用户管理（LiteLLM 内部用户，对应 CarHer 实例）

| 用途 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 用户列表 | GET | `/user/list` | 所有 LiteLLM 用户 |
| 用户详情 | GET | `/user/info?user_id=carher-100` | 单个用户的 key、spend |
| 用户详情 v2 | GET | `/v2/user/info?user_id=carher-100` | 增强版 |
| 创建用户 | POST | `/user/new` | body: `{"user_id":"carher-100"}` |
| 更新用户 | POST | `/user/update` | 更新用户设置 |
| 删除用户 | POST | `/user/delete` | body: `{"user_ids":["carher-100"]}` |
| 用户每日活跃 | GET | `/user/daily/activity?user_id=carher-100&start_date=2026-04-01&end_date=2026-04-12` | 每日用量 |

### CarHer Admin API 中的 LiteLLM 接口

Admin API (`http://carher-admin-svc.carher:8900`) 封装了 LiteLLM Key 的生命周期管理：

| 用途 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 生成 Key | POST | `/api/litellm/keys/generate?uid=100` | 为指定实例创建虚拟 Key |
| 批量生成 | POST | `/api/litellm/keys/generate-batch` | 为所有 litellm provider 实例批量生成 |
| 费用摘要 | GET | `/api/litellm/spend` | 各实例费用汇总（聚合 spend/keys 数据） |

调用 Admin API 时使用：
```bash
curl -s http://carher-admin-svc.carher:8900/api/litellm/spend \
  -H "X-API-Key: bd037b874cb170710c4873b0cdec924539bcddba879f4556ae40875a15e4a1e4"
```

## 198 Pro Cursor / Claude Code 组织与 Team 同步

198 `litellm-product` 的 cursor / claude-code 账户同步以飞书多维表为准：

- Base token: `DlT9bsrwMad12VsogEpcK9Ptncc`
- Table id: `tblJT2s6Y6xjYj5A`
- 字段：`姓名`、`姓名.部门`、`部门`、`邮箱前缀`、`账户类型`、`key_alias`、`状态`
- 只处理状态以 `有效` 开头，且 `key_alias` 为 `cursor-*` / `claude-code-*` 的记录
- Organization 固定为 `org-wuxi-chelian` / `无锡车联`
- Team 使用飞书当前部门；空部门或 `未分组` 统一显示为 `上海车联`
- `上海车联` 必须复用 legacy `未分组` 的 team id：`team-e86ea3f280d920a2`，不要按 `上海车联` 重新 hash 出新 team

推荐脚本：

```bash
python3 scripts/litellm-dev-org-team-sync.py --namespace litellm-product
python3 scripts/litellm-dev-org-team-sync.py --namespace litellm-product --apply --cleanup-legacy-dept-orgs
```

生产同步原则：

- 先 dry-run，确认 `missing_rows=0`、`ambiguous_rows=0`、`conflicts=0` 后再 apply
- 不修改 raw key、token hash、models allowlist、budget、blocked、spend、rate limit
- 不默认重启 `litellm-product` proxy；UI 缓存旧值时先接受延迟，低峰再单独评估 rollout
- `cursor-linsen` 这类一个 Base alias 命中多把 key 但 `user_id` 相同的情况，视为同一账户多 key，可全部同步
- 多命中且 `user_id` 不同必须拒绝 apply

公网 Admin API 可用于应急只读/部分写入：

- `/key/list?return_full_object=true&page=1&size=100` 可拉 key alias、token hash、user_id、metadata；不要使用过大的 `size`，LiteLLM 可能返回 422
- `/user/list` 适合拉 user 列表，但它的 `organization_id` / `team_id` 视图不可靠
- `/user/info?user_id=...` 是 user 字段最终校验口径；若 `/user/list` 显示旧值，要用 `/user/info` 抽样或全量复核
- `/key/update` 可以安全写 `organization_id` 和 metadata；带 `team_id` 时会校验 TeamTable.members
- LiteLLM v1.89.0 的 `/team/member_add` 和 `/team/bulk_member_add` 可能返回 200 但只更新 `UserTable.teams` / membership 视图，不会同步 `LiteLLM_TeamTable.members`
- 因此当 `/key/update` 设置 `team_id` 返回 `User=... is not a member of the team=...` 时，不要反复调用 member_add；需要走 DB 事务补齐 TeamTable.members / members_with_roles / TeamMembership，再更新 key.team_id

如果 JumpServer/JMS 不通：

- `jms ssh AIYJY-litellm` 默认走 KoKo `10.68.13.189:2222`；公网 API 可以继续做 metadata/email/org 修复，但不能可靠完成 key.team_id 的最后校准
- `https://cc.auto-link.com.cn/pro` 可用当前有效 admin key 调 LiteLLM API；admin key 不要写进文档或输出给用户
- 旧硬编码 `sk-chatgpt-198-...` master key 可能已失效，必须以当前 K8s secret 或已验证 admin key 为准
- 公网 API 部分写入后，最终要保留 dry-run / apply / final-verify 产物路径，方便 DB 通道恢复后补剩余 `VerificationToken.team_id`

最终验证口径：

- key：`/key/list?return_full_object=true` 检查 `org_id`、`team_id`、metadata `email` / `department` / `team_alias`
- user：优先 `/user/info?user_id=...` 检查 `user_email`、`organization_id`、`team_id`、metadata
- team：admin 成员必须同时体现在 `admins` 和 `members_with_roles.role=admin`；AI 技术院目前 `liuguoxian` / `zhuyida` 的 cursor 与 claude-code user 都是 admin
- smoke：只做只读 info 或最小推理请求，不要为了 UI 即时刷新强行重启线上 proxy

---

## 常用运维场景

### 1. LiteLLM Proxy 健康巡检

```bash
# 快速检查
curl -s http://litellm-proxy.carher.svc:4000/health/readiness \
  -H "Authorization: Bearer $MK"

# 全面检查（会向所有模型发请求，耗时 ~45s）
curl -s http://litellm-proxy.carher.svc:4000/health \
  -H "Authorization: Bearer $MK"
```

返回中 `healthy_count` / `unhealthy_count` 可快速判断。unhealthy 的模型会有 `error` 字段说明原因。

### 2. 查看所有模型及状态

```bash
# 模型列表
curl -s http://litellm-proxy.carher.svc:4000/v1/models \
  -H "Authorization: Bearer $MK"

# 模型组详情（含 provider、RPM/TPM）
curl -s http://litellm-proxy.carher.svc:4000/model_group/info \
  -H "Authorization: Bearer $MK"
```

### 3. 查看费用 Top-N

```bash
# 按 key 费用排行
curl -s "http://litellm-proxy.carher.svc:4000/spend/keys?limit=20" \
  -H "Authorization: Bearer $MK"
```

返回 `[{key_alias: "carher-89", spend: 626.43}, ...]`，key_alias 格式为 `carher-{uid}`，可直接映射到 Her 实例。

### 4. 查看某个实例的费用明细

```bash
# 先查 key 信息
curl -s "http://litellm-proxy.carher.svc:4000/key/info?key=sk-实例的key" \
  -H "Authorization: Bearer $MK"

# 或通过 Admin API 汇总
curl -s http://carher-admin-svc.carher:8900/api/litellm/spend \
  -H "X-API-Key: bd037b874cb170710c4873b0cdec924539bcddba879f4556ae40875a15e4a1e4"
```

### 5. 为新实例创建 Key

推荐通过 Admin API（自动处理 route policy 和 fallback）：

```bash
curl -X POST "http://carher-admin-svc.carher:8900/api/litellm/keys/generate?uid=500" \
  -H "X-API-Key: bd037b874cb170710c4873b0cdec924539bcddba879f4556ae40875a15e4a1e4"
```

如需直接调用 LiteLLM API：

```bash
curl -X POST http://litellm-proxy.carher.svc:4000/key/generate \
  -H "Authorization: Bearer $MK" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "carher-500",
    "key_alias": "carher-500",
    "models": ["claude-opus-4-6","claude-sonnet-4-6","gpt-5.4","gemini-3.1-pro-preview","minimax-m2.7","glm-5","gpt-5.3-codex","BAAI/bge-m3","wangsu-gpt-5.5","wangsu-deepseek-v4-pro","wangsu-deepseek-v4-flash","openrouter-claude-opus-4-6","openrouter-claude-sonnet-4-6","wangsu-gpt-5.4","wangsu-gemini-3.1-pro-preview"],
    "router_settings": {"fallbacks": [{"claude-opus-4-6":["openrouter-claude-opus-4-6"]},{"claude-sonnet-4-6":["openrouter-claude-sonnet-4-6"]},{"gpt-5.4":["wangsu-gpt-5.4"]},{"gemini-3.1-pro-preview":["wangsu-gemini-3.1-pro-preview"]}]}
  }'
```

### 6. 封禁/解封某个实例的 Key

```bash
# 封禁
curl -X POST http://litellm-proxy.carher.svc:4000/key/block \
  -H "Authorization: Bearer $MK" \
  -H "Content-Type: application/json" \
  -d '{"key": "sk-被封禁的key"}'

# 解封
curl -X POST http://litellm-proxy.carher.svc:4000/key/unblock \
  -H "Authorization: Bearer $MK" \
  -H "Content-Type: application/json" \
  -d '{"key": "sk-被封禁的key"}'
```

### 7. 检查特定模型是否可用

```bash
curl -s "http://litellm-proxy.carher.svc:4000/health?model=claude-sonnet-4-6" \
  -H "Authorization: Bearer $MK"
```

### 8. 路由策略说明（legacy）

路由策略现已固定：Sonnet/Opus → Wangsu Direct，GPT/Gemini → OpenRouter。
`litellm_route_policy` 字段保留但不再影响实际路由。

### 9. 费用预估

```bash
curl -X POST http://litellm-proxy.carher.svc:4000/spend/calculate \
  -H "Authorization: Bearer $MK" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-4-6", "messages": [{"role":"user","content":"一段1000字的消息"}]}'
```

### 10. 重启 LiteLLM Proxy

如果 LiteLLM Proxy 异常需要重启，通过 Admin API 操作 K8s（非直接操作 LiteLLM API）：

```bash
# 通过 kubectl（需要 K8s 权限）
kubectl rollout restart deployment litellm-proxy -n carher

# 或通知管理员手动操作
```

---

## K8s 资源摘要

```
Namespace: carher

Deployment:  litellm-proxy     (1 副本, ghcr.io/berriai/litellm)
StatefulSet: litellm-db        (1 副本, PostgreSQL)
Service:     litellm-proxy     (ClusterIP 4000)
             litellm-db        (ClusterIP 5432)
ConfigMap:   litellm-config    (模型列表+路由)
Secret:      litellm-secrets   (LITELLM_MASTER_KEY, DATABASE_URL)
             litellm-db-credentials (PG user/pass/db)
             carher-env-keys   (OPENROUTER_API_KEY, WANGSU_API_KEY, WANGSU_DIRECT_API_KEY, etc.)
```

### 当前已知问题（来自 /health 检查）

`gpt-5.4` 和 `gpt-5.3-codex` 健康检查可能显示 unhealthy，原因是 OpenRouter 的 OpenAI 模型要求 `max_output_tokens >= 16`，而健康检查使用 `max_tokens=1`。实际使用不受影响。
