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

```bash
curl -s http://litellm-proxy.carher.svc:4000/health/readiness \
  -H "Authorization: Bearer sk-carher-litellm-7c5e14f76cc7718def67ccfae6f00707"
```

**Base URL**: `http://litellm-proxy.carher.svc:4000`
**认证**: `Authorization: Bearer sk-carher-litellm-7c5e14f76cc7718def67ccfae6f00707`

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

---

## 常用运维场景

### 1. LiteLLM Proxy 健康巡检

```bash
# 快速检查
curl -s http://litellm-proxy.carher.svc:4000/health/readiness \
  -H "Authorization: Bearer sk-carher-litellm-7c5e14f76cc7718def67ccfae6f00707"

# 全面检查（会向所有模型发请求，耗时 ~45s）
curl -s http://litellm-proxy.carher.svc:4000/health \
  -H "Authorization: Bearer sk-carher-litellm-7c5e14f76cc7718def67ccfae6f00707"
```

返回中 `healthy_count` / `unhealthy_count` 可快速判断。unhealthy 的模型会有 `error` 字段说明原因。

### 2. 查看所有模型及状态

```bash
# 模型列表
curl -s http://litellm-proxy.carher.svc:4000/v1/models \
  -H "Authorization: Bearer sk-carher-litellm-7c5e14f76cc7718def67ccfae6f00707"

# 模型组详情（含 provider、RPM/TPM）
curl -s http://litellm-proxy.carher.svc:4000/model_group/info \
  -H "Authorization: Bearer sk-carher-litellm-7c5e14f76cc7718def67ccfae6f00707"
```

### 3. 查看费用 Top-N

```bash
# 按 key 费用排行
curl -s "http://litellm-proxy.carher.svc:4000/spend/keys?limit=20" \
  -H "Authorization: Bearer sk-carher-litellm-7c5e14f76cc7718def67ccfae6f00707"
```

返回 `[{key_alias: "carher-89", spend: 626.43}, ...]`，key_alias 格式为 `carher-{uid}`，可直接映射到 Her 实例。

### 4. 查看某个实例的费用明细

```bash
# 先查 key 信息
curl -s "http://litellm-proxy.carher.svc:4000/key/info?key=sk-实例的key" \
  -H "Authorization: Bearer sk-carher-litellm-7c5e14f76cc7718def67ccfae6f00707"

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
  -H "Authorization: Bearer sk-carher-litellm-7c5e14f76cc7718def67ccfae6f00707" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "carher-500",
    "key_alias": "carher-500",
    "models": ["claude-opus-4-6","claude-sonnet-4-6","gpt-5.4","gemini-3.1-pro-preview","minimax-m2.7","glm-5","gpt-5.3-codex","BAAI/bge-m3","openrouter-claude-opus-4-6","openrouter-claude-sonnet-4-6","wangsu-gpt-5.4","wangsu-gemini-3.1-pro-preview"],
    "router_settings": {"fallbacks": [{"claude-opus-4-6":["openrouter-claude-opus-4-6"]},{"claude-sonnet-4-6":["openrouter-claude-sonnet-4-6"]},{"gpt-5.4":["wangsu-gpt-5.4"]},{"gemini-3.1-pro-preview":["wangsu-gemini-3.1-pro-preview"]}]}
  }'
```

### 6. 封禁/解封某个实例的 Key

```bash
# 封禁
curl -X POST http://litellm-proxy.carher.svc:4000/key/block \
  -H "Authorization: Bearer sk-carher-litellm-7c5e14f76cc7718def67ccfae6f00707" \
  -H "Content-Type: application/json" \
  -d '{"key": "sk-被封禁的key"}'

# 解封
curl -X POST http://litellm-proxy.carher.svc:4000/key/unblock \
  -H "Authorization: Bearer sk-carher-litellm-7c5e14f76cc7718def67ccfae6f00707" \
  -H "Content-Type: application/json" \
  -d '{"key": "sk-被封禁的key"}'
```

### 7. 检查特定模型是否可用

```bash
curl -s "http://litellm-proxy.carher.svc:4000/health?model=claude-sonnet-4-6" \
  -H "Authorization: Bearer sk-carher-litellm-7c5e14f76cc7718def67ccfae6f00707"
```

### 8. 路由策略说明（legacy）

路由策略现已固定：Sonnet/Opus → Wangsu Direct，GPT/Gemini → OpenRouter。
`litellm_route_policy` 字段保留但不再影响实际路由。

### 9. 费用预估

```bash
curl -X POST http://litellm-proxy.carher.svc:4000/spend/calculate \
  -H "Authorization: Bearer sk-carher-litellm-7c5e14f76cc7718def67ccfae6f00707" \
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
