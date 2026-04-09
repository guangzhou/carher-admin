---
name: add-instances
description: >-
  Batch-create new CarHer bot instances on K8s via Admin API.
  Use when adding new users/her instances to the cluster, onboarding
  new users, or bulk-importing instances with Feishu app credentials.
---

# 批量新增 CarHer 实例

通过 Admin API 批量创建 her 实例，operator 会自动完成 CRD → Deployment → Pod 的全流程。

## 前置条件

```bash
API_KEY=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.admin-api-key}' | base64 -d)
```

## 数据格式

用户通常提供如下信息：

| 字段 | 说明 | 示例 |
|------|------|------|
| id | 实例 ID（可选，省略则自动分配） | 173 |
| name | 显示名称 | 永康的her |
| app_id | 飞书 App ID | cli_a95e1e0534795cd1 |
| app_secret | 飞书 App Secret | VJJMhZlJ2XYad3Dl4jxTJcj5nTiU1ESq |
| owner | 所属用户（中文名或 ou_xxx） | 辛永康 |

## 默认值

| 参数 | 默认值 | 可选值 |
|------|--------|--------|
| provider | wangsu | wangsu / openrouter / anthropic / litellm |
| model | opus | opus / sonnet / gpt / gemini |
| prefix | s1 | s1 / s2 / s3 |
| deploy_group | stable | stable / test / canary / vip 等 |

## 执行步骤

### Step 1: 确认 next-id（可选）

如果用户指定了 ID，先确认不会冲突：

```bash
curl -s -H "X-API-Key: $API_KEY" https://admin.carher.net/api/next-id
```

### Step 2: batch-import 创建实例

```bash
curl -s -X POST "https://admin.carher.net/api/instances/batch-import" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
  "instances": [
    {
      "id": 173,
      "name": "永康的her",
      "app_id": "cli_a95e1e0534795cd1",
      "app_secret": "VJJMhZlJ2XYad3Dl4jxTJcj5nTiU1ESq",
      "owner": "辛永康",
      "deploy_group": "test"
    }
  ]
}'
```

每个实例支持的字段：`id`、`name`、`model`、`app_id`、`app_secret`、`prefix`、`owner`、`provider`、`deploy_group`。未提供的字段使用上述默认值。

> **LiteLLM 注意事项**：当 `provider=litellm` 时，Admin API 会在创建实例时自动调用 LiteLLM proxy 生成一个 per-instance 虚拟 key（存入 DB `litellm_key` 和 CRD `spec.litellmKey`），用于独立的 token 消费追踪。无需手动创建 key。

### Step 3: 验证创建结果

```bash
# 检查 CRD 状态
kubectl get herinstances -n carher | grep -E "her-(173|174|175)"

# 检查 Pod 是否 Running
kubectl get pods -n carher | grep -E "carher-(173|174|175)"

# 通过 API 检查实例详情
curl -s -H "X-API-Key: $API_KEY" https://admin.carher.net/api/instances/173
```

正常情况下 Pod 会在 30s 内变为 Running (2/2)。

### Step 4: 确认 OAuth 回调地址

创建成功后返回的 `oauth_url` 需要配置到对应飞书应用的重定向 URL：

```
https://s1-u{id}-auth.carher.net/feishu/oauth/callback
```

## 批量更新已有实例

如果需要创建后修改属性（如 deploy_group、model）：

```bash
curl -s -X POST "https://admin.carher.net/api/instances/batch" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"ids":[173,174,175],"action":"update","params":{"deploy_group":"test"}}'
```

## 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| Pod 一直 Pending | 节点资源不足 | `kubectl describe pod carher-{id} -n carher` 查看事件 |
| Pod CrashLoopBackOff | app_id/app_secret 错误 | 检查飞书凭据，`PUT /api/instances/{id}` 修正 |
| 创建返回 409 | ID 已存在 | 使用不同 ID 或先删除旧实例 |
