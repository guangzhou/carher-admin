---
name: litellm-key-mapping
description: >-
  View LiteLLM virtual key mappings and spend for CarHer bot instances.
  Use when the user asks to see, list, or check LiteLLM keys, key mappings,
  token usage, spend tracking, or mentions "key" + "litellm" / "映射" / "消费".
---

# 查看 Her 实例 LiteLLM Key 映射

查看所有使用 `provider=litellm` 的 her 实例及其对应的 LiteLLM 虚拟 key。

## 关键概念

- **Key 命名**：`key_alias` 和 `user_id` 统一为 `carher-{uid}`（如 `carher-1000`）
- **Env 注入**：Operator 向 Pod 注入 `LITELLM_API_KEY` env var（per-instance key），覆盖共享 Secret 中的 master key
- **模型白名单**：每个 key 允许 7 个 chat 模型 + 1 个 embedding（`BAAI/bge-m3`）
- **路由**：gpt/sonnet/opus/gemini → Wangsu 主 + OpenRouter 备；minimax/glm/codex → OpenRouter only
- **当前规模**：约 101 个实例使用 litellm

## 前置：kubectl 隧道

先测试连通性：`kubectl get nodes`

如果报 `connection refused`，建立隧道：

```bash
SSHPASS='5ip0krF>qazQjcvnqc' sshpass -e ssh \
  -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
  -p 1023 -L 16443:172.16.1.163:6443 -N root@47.84.112.136 &
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
| 1000 | 国现的her    | opus  | sk-vr...3D9r_eBw | 已配置 |
| ...  | ...         | ...   | ...             | ...    |

无 key 实例: M 个（需补发）
```

注意：**不要在汇总中展示完整 key**，只展示前 4 位 + 后 6 位脱敏格式。
完整 key 仅在排查问题时通过 kubectl 或 API 单独查看。
