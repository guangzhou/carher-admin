---
name: litellm-key-mapping
description: >-
  View or delete LiteLLM virtual keys and spend for CarHer and non-Her (CLI/tool)
  accounts. Use when the user asks to see, list, check, revoke, or delete LiteLLM
  keys, key mappings, token usage, spend tracking, or mentions "key" + "litellm"
  / "映射" / "消费" / 删除 key / 撤销 key.
---

# 查看 Her 实例 LiteLLM Key 映射

查看所有使用 `provider=litellm` 的 her 实例及其对应的 LiteLLM 虚拟 key。

## 关键概念

- **Key 命名（Her 实例）**：`key_alias` 和 `user_id` 统一为 `carher-{uid}`（如 `carher-1000`）
- **Key 命名（其他）**：CLI/工具等也会在 LiteLLM 里建虚拟 key，例如 `claude-code-*`、`cursor-*` 等；**删除前务必按别名确认**，勿误删 `carher-*`
- **Env 注入**：Operator 向 Pod 注入 `LITELLM_API_KEY` env var（per-instance key），覆盖共享 Secret 中的 master key
- **模型白名单**：每个 key 有 `models` allowlist 限定可访问的 model_name。**每次在 LiteLLM config 新增 model_name 后，必须同步更新 allowlist**，否则 bot 调用该 model 会 `401 key not allowed to access model`（实测 2026-04-21）。批量更新命令见下文"批量同步 allowlist"章节。
- **路由**：sonnet/opus → Wangsu Anthropic Direct 主 + OpenRouter 备；gpt/gemini → OpenRouter 主 + Wangsu 备；minimax/glm/codex → OpenRouter only
- **当前规模**：约 117 个实例使用 litellm

## 前置：kubectl 隧道

先测试连通性：`kubectl get nodes`

若 `connection refused`，按 `k8s-via-bastion` skill 启动 proxy：

```bash
pgrep -af 'jms.*proxy laoyang' >/dev/null \
  || nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &
sleep 2 && kubectl get nodes
```

## 方式 1：kubectl 查 CRD（最权威）

### 列出所有 litellm 实例的 key 映射

```bash
kubectl get her -n carher \
  -o custom-columns='ID:.spec.userId,NAME:.spec.name,PROVIDER:.spec.provider,MODEL:.spec.model,KEY:.spec.litellmKey' \
  | head -1; \
kubectl get her -n carher \
  -o custom-columns='ID:.spec.userId,NAME:.spec.name,PROVIDER:.spec.provider,MODEL:.spec.model,KEY:.spec.litellmKey' \
  --no-headers | grep litellm
```

### 查看单个实例

```bash
kubectl get her her-<ID> -n carher \
  -o jsonpath='ID: {.spec.userId}{"\n"}Name: {.spec.name}{"\n"}Provider: {.spec.provider}{"\n"}Model: {.spec.model}{"\n"}LiteLLM Key: {.spec.litellmKey}{"\n"}'
```

### 统计有 key 和无 key 的 litellm 实例

```bash
echo "=== 有 key ===" && \
kubectl get her -n carher --no-headers \
  -o custom-columns='ID:.spec.userId,NAME:.spec.name,KEY:.spec.litellmKey' \
  | awk '$NF != "<none>" && $NF != ""' | wc -l

echo "=== 无 key (需补发) ===" && \
kubectl get her -n carher --no-headers \
  -o custom-columns='ID:.spec.userId,PROVIDER:.spec.provider,KEY:.spec.litellmKey' \
  | awk '$2 == "litellm" && ($3 == "<none>" || $3 == "")' 
```

## 方式 2：Admin API 查

```bash
API_KEY=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.admin-api-key}' | base64 -d)

# 全部 litellm 实例的 key 映射
curl -s -H "X-API-Key: $API_KEY" https://admin.carher.net/api/crd/instances \
  | jq '[.[] | select(.spec.provider=="litellm") | {id: .spec.userId, name: .spec.name, model: .spec.model, key: .spec.litellmKey}]'

# 单个实例
curl -s -H "X-API-Key: $API_KEY" https://admin.carher.net/api/crd/instances/<ID> \
  | jq '{id: .spec.userId, name: .spec.name, provider: .spec.provider, key: .spec.litellmKey}'
```

## 方式 3：查看 token 消费

```bash
# LiteLLM proxy 侧的 spend 数据（按 key 汇总）
curl -s -H "X-API-Key: $API_KEY" https://admin.carher.net/api/litellm/spend | jq

# 单个 key 的详细信息（从 LiteLLM proxy 直接查）
MASTER_KEY=$(kubectl get secret litellm-secrets -n carher -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
curl -s "http://litellm-proxy.carher.svc:4000/key/info?key=<实例的litellm_key>" \
  -H "Authorization: Bearer $MASTER_KEY" | jq
```

## 方式 4：LiteLLM Web UI

```bash
# port-forward 到本地
kubectl port-forward -n carher svc/litellm-proxy 4000:4000
# 浏览器打开 http://localhost:4000/ui
```

### ⚠️ UI 搜索 / 历史消费的几个反直觉点

| 你想做的事 | 在 UI 上的真实行为 | 怎么绕开 |
|---|---|---|
| 在 "Search by Alias" 输入 `claude-code-zhangsan` 找 key | 走 `GET /key/list?key_alias=<exact>` **精确匹配**（不是 LIKE / 子串） | DB 里实际是带 4 字符随机后缀的 `claude-code-zhangsan-50gj`（老 keygen 历史包袱）。**完整粘贴带后缀的 alias 才能命中**；找不到后缀时用 CLI：`curl -s "http://127.0.0.1:4000/spend/keys?limit=600" -H "Authorization: Bearer $MK" \| jq -r '.[]\|select(.key_alias\|test("zhangsan";"i"))\|.key_alias'` |
| 用某个 `sk-...` user-key 查自己的 "Logs / 消费明细" | LiteLLM 1.82.x 的"Logs"页走 `GET /spend/logs/v2?api_key=...`，**这是 admin-only 端点**，user-key role=`unknown` → 401 → 前端渲染空 | (a) 走旧版 `GET /spend/logs?api_key=<token>&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` —— **owner 可见** ✓ ；(b) 让前端走 carher-admin 后端代查（master key 走服务端） |
| 找 owner 自己的最近一笔调用 | UI 默认时间窗口 = "今天 UTC"，BJT 用户在 BJT 早 8:00 前（= UTC 还是昨天）打的请求看不到 | UI 显式选时间窗口；或直接 SQL（见 `litellm-ops` skill 里 "用户具体 case：怎么定位他/她的请求"），注意 `LiteLLM_SpendLogs.startTime` 是 **naive UTC**，BJT 时间要先 -8h 代入 |
| 看一笔流式调用是否"卡住" | 客户端感知 "T1 → T2 持续在打字" 像多次调用，但 SpendLogs 一次 HTTP 只写一行，时间戳是 **`startTime`** | 每行 `dur_s = endTime - startTime` 才是这次流式的实际持续；46s 输出 2.7k token 是 sonnet 流式的正常体感 |

> 历史踩坑（2026-04-28 实测）：用户报"前端搜不到 `claude-code-liuguoxian`、消费明细打不开"，根因是上面表里前两行的组合：① UI 别名搜精确匹配 + 别名带 `-50gj` 后缀；② 客户端用 user-key 调 `/spend/logs/v2` 被 admin 校验 401。

### Owner 自助查自己的历史消费（最稳的 user-key 路径）

如果你拿的是某把 user-key 的明文 `sk-...`（**不是** master key），不要打 `/spend/logs/v2`，改用旧版：

```bash
# 通过 cloudflare 公网入口直接验证（无需 kubectl）
KEY="sk-..."   # 用户自己的 key

# 单天明细
curl -s "https://litellm.carher.net/spend/logs?api_key=$KEY&start_date=2026-04-28&end_date=2026-04-29" \
  -H "Authorization: Bearer $KEY" | jq

# key 自身基本信息（含 spend、max_budget、budget_reset_at）
curl -s "https://litellm.carher.net/key/info?key=$KEY" \
  -H "Authorization: Bearer $KEY" | jq '.info'
```

> 注：`/spend/logs` 接受的 `api_key` 既可以是明文 `sk-...` 也可以是 64 位 hash（token）。`/key/info?key=...` 同理，但带 `sk-...` 形式时 LiteLLM 会**先 hash 再查**，所以两种都行；管理脚本里统一用 hash（token）以避免明文落盘/落 kubectl logs。

## 补发缺失的 key

如果发现有 litellm 实例没有 key：

```bash
# 单个实例补发
curl -X POST "https://admin.carher.net/api/litellm/keys/generate?uid=<ID>" \
  -H "X-API-Key: $API_KEY"

# 批量补发（所有缺 key 的 litellm 实例）
curl -X POST https://admin.carher.net/api/litellm/keys/generate-batch \
  -H "X-API-Key: $API_KEY"
```

## 快速汇总模板

查完后向用户汇总：

```
LiteLLM Key 映射（共 N 个 litellm 实例）:

| ID   | 名称         | 模型  | Key (脱敏)      | 状态   |
|------|-------------|-------|-----------------|--------|
| 1000 | 国现的her    | gpt   | sk-vr...3D9r_eBw | 已配置 |
| ...  | ...         | ...   | ...             | ...    |

无 key 实例: M 个（需补发）
```

注意：**不要在汇总中展示完整 key**，只展示前 4 位 + 后 6 位脱敏格式。
完整 key 仅在排查问题时通过 kubectl 或 API 单独查看。

---

## 删除虚拟 key（按 key_alias，含非 Her 账户）

用于撤销某批账户的 API key（例如离职、轮换、误发 key）。**破坏性操作**：删除后该 key 立即失效，需重新走生成流程。

### 1. 前置

- 能连集群：`kubectl get nodes`（若 `connection refused`，先按上文建 SSH 隧道）。
- 本地访问 LiteLLM：`kubectl port-forward -n carher svc/litellm-proxy 4000:4000`（本地端口可换，下文用 `http://127.0.0.1:4000`）。
- Master key：`MASTER_KEY=$(kubectl get secret litellm-secrets -n carher -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)`

### 2. 按关键词查别名（粗筛）

`GET /key/aliases` 的 `search` 为子串匹配，可多次换关键词：

```bash
curl -s "http://127.0.0.1:4000/key/aliases?page=1&size=100&search=<关键词>" \
  -H "Authorization: Bearer $MASTER_KEY" | jq
```

### 3. 解析可删除的内部 key id（`token`）

`/key/list` 返回的是内部 id 列表，**不适合**按业务别名筛选。应使用 **`GET /spend/keys`**：每条记录含 `key_alias`、`token`（64 位十六进制，即删除接口要用的 id）、脱敏 `key_name`。

按 **前缀** 过滤示例（按需改前缀）：

```bash
curl -s "http://127.0.0.1:4000/spend/keys?limit=500" \
  -H "Authorization: Bearer $MASTER_KEY" -o /tmp/spend_keys.json

python3 << 'PY'
import json
with open("/tmp/spend_keys.json") as f:
    rows = json.load(f)
for r in rows:
    a = r.get("key_alias") or ""
    if a.startswith("PREFIX1") or a.startswith("PREFIX2"):
        print(r["key_alias"], r["token"])
PY
```

删除前向用户列出 **`key_alias` 列表**，确认无 `carher-*` 误选。

### 4. 单条校验（可选）

```bash
curl -s "http://127.0.0.1:4000/key/info?key=<token>" \
  -H "Authorization: Bearer $MASTER_KEY" | jq
```

### 5. 调用删除

```bash
curl -s -X POST "http://127.0.0.1:4000/key/delete" \
  -H "Authorization: Bearer $MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"keys":["<token1>","<token2>"]}' | jq
```

成功时响应里一般有 `deleted_keys` 数组。再用 `key/aliases?search=...` 或上一步脚本复查，确认目标别名已消失。

### 6. 注意

- **不要用**误传的完整 `sk-...` 贴到聊天或日志；运维脚本里只处理 `token` 或脱敏名。
- Her 实例的 key 若需作废，应优先走业务侧（更新 CRD `litellmKey` / Admin 流程），避免只删 LiteLLM 侧导致集群与代理不一致；**本小节针对独立虚拟 key（如 CLI 账户）为主**。

---

## 批量同步 allowlist（新增 model 后必做）

**触发场景**：给 `k8s/litellm-proxy.yaml` 的 `model_list` 增加了新 model（比如 `openrouter-claude-opus-4-7`、`anthropic.openrouter.claude-*`）后，**所有现有 virtual key 的 `models` allowlist 需要补上新 model**，否则 bot 调新 model 会 `401 key not allowed to access model`。

### 一次性批量补齐（从 DB 拉所有 key + node 并发调 /key/update）

```bash
MK=$(kubectl get secret litellm-secrets -n carher -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
mkdir -p /tmp/allowlist

# 1. 拉所有需要更新的 key（一般是 carher-* + claude-code-*）
kubectl exec litellm-db-0 -n carher -- psql -U litellm -d litellm -tA --field-separator="|" -c "
SELECT key_alias, token, array_to_json(models)
FROM \"LiteLLM_VerificationToken\"
WHERE key_alias LIKE 'carher-%' OR key_alias LIKE 'claude-code-%';
" > /tmp/allowlist/keys.txt

# 2. 计算每个 key 需要补的 model 并生成 task.tsv
# 3. 在 pod 里用 node 并发 8 路调 /key/update（/key/update 接受 token 而非明文 sk-*）
# 参考脚本：carher-memorysearch-config skill 下的批量更新模板
```

**关键**：LiteLLM 的 `/key/update` 接受 `{"key": <token-hash>, "models": [...]}`；`models` 是**完整替换**的目标 allowlist，不是增量。所以需要先查当前 models 再 merge。

**实测数据**（2026-04-21）：197 个 key 并发 8 路同步，6 秒完成。

### 新增 model 的完整 checklist

以后在 LiteLLM 加 model 的工作必须包含：

- [ ] `k8s/litellm-proxy.yaml` 的 `model_list` 新增条目
- [ ] 如果新 model 属于 Anthropic 家族，配 `extra_headers` + `cache_control_injection_points`
- [ ] （可选）`router_settings.fallbacks` 加 fallback 链
- [ ] **批量同步所有 key 的 `models` allowlist 加上新 model_name**（否则 401）
- [ ] canary 验证（见 [litellm-hook-dev](../litellm-hook-dev/SKILL.md) 的 canary 流程）
- [ ] rollout 主 Deployment
- [ ] 验证实测一次新 model 调用 HTTP 200
