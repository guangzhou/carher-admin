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

如果报 `connection refused`，建立隧道：

```bash
SSHPASS='uGTdq>hn4ps4gwivjs' sshpass -e ssh \
  -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
  -L 16443:172.16.1.163:6443 -N root@43.98.160.216 &
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
