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

# 新增 her 前，必须确认 admin 已加载 Cloudflare token；
# 否则创建接口现在会直接返回 503，避免静默生成 404 callback。
kubectl exec -n carher deploy/carher-admin -- \
  python -c "import os; print(bool(os.environ.get('CLOUDFLARE_API_TOKEN')))"
```

## 数据格式

用户通常提供如下信息（多行文本，每个实例一组）：

| 字段 | 说明 | 示例 |
|------|------|------|
| id | 实例 ID（可选，省略则自动分配） | 180 |
| name | 显示名称 | 永康的her |
| app_id | 飞书 App ID | cli_a95e1e0534795cd1 |
| app_secret | 飞书 App Secret | VJJMhZlJ2XYad3Dl4jxTJcj5nTiU1ESq |
| owner | 所属用户（中文名或 ou_xxx） | 辛永康 |

## 默认值

| 参数 | 默认值 | 可选值 |
|------|--------|--------|
| provider | litellm | litellm / wangsu / openrouter / anthropic |
| model | gpt | gpt / sonnet / opus / gemini（litellm 额外支持 minimax / glm / codex） |
| prefix | s1 | s1 / s2 / s3 |
| deploy_group | stable | stable / test / canary / vip 等 |
| image | upgrade-0402-8ef16fb | 当前线上版本，operator 自动填充 |

## 执行步骤

### Step 0: 对齐计划

收到用户数据后，**先整理成表格让用户确认**，再执行。注意检查：
- ID 是否重复（两个不同实例用了同一个 ID）
- ID 是否连续（有无空缺）
- name 和 owner 是否对应

### Step 1: 确认 next-id（可选）

如果用户指定了 ID，先确认不会冲突：

```bash
curl -s -H "X-API-Key: $API_KEY" https://admin.carher.net/api/next-id
```

### Step 2: batch-import 创建实例

**必须显式指定 `provider` 和 `model`**，不要依赖后端默认值：

```bash
curl -s -X POST "https://admin.carher.net/api/instances/batch-import" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
  "instances": [
    {"id":180,"name":"系统需求部的her","app_id":"cli_xxx","app_secret":"xxx","owner":"秦建国","provider":"litellm","model":"gpt"},
    {"id":181,"name":"李杰的her","app_id":"cli_yyy","app_secret":"yyy","owner":"李杰","provider":"litellm","model":"gpt"}
  ]
}'
```

每个实例支持的字段：`id`、`name`、`model`、`app_id`、`app_secret`、`prefix`、`owner`、`provider`、`deploy_group`。

> **LiteLLM 自动处理**：当 `provider=litellm` 时：
> - Admin API 自动生成 per-instance 虚拟 key（`carher-{uid}`），存入 CRD `spec.litellmKey`
> - Operator 向 Pod 注入 `LITELLM_API_KEY` env var，覆盖共享 master key
> - Key 允许 7 个 chat 模型 + `BAAI/bge-m3` embedding
> - 路由：全部 7 个模型走 OpenRouter（网宿已禁用）
> - 无需手动创建 key

**创建响应必须检查 `cloudflare` 字段**：

```json
{
  "results": [
    {
      "id": 180,
      "status": "created",
      "managed_by": "operator",
      "oauth_url": "https://s1-u180-auth.carher.net/feishu/oauth/callback",
      "cloudflare": {
        "ok": true,
        "message": "DNS + remote tunnel ingress synced"
      }
    }
  ]
}
```

- `cloudflare.ok=true`：说明 DNS + 远程 tunnel ingress 已同步
- `cloudflare.ok=false`：实例虽已创建，但 callback 可能仍会 `404`，先修 Cloudflare 再继续
- 如果接口直接返回 `503` 且提示 `CLOUDFLARE_API_TOKEN`，不要重试创建；先修 admin secret 并重启 `carher-admin`

### Step 3: 验证创建结果

```bash
# 先看 API 返回里的 cloudflare 状态
curl -s -X POST "https://admin.carher.net/api/instances/batch-import" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"instances":[{"id":180,"name":"系统需求部的her","app_id":"cli_xxx","app_secret":"xxx","owner":"秦建国","provider":"litellm","model":"gpt"}]}' \
  | jq '.results[] | {id,status,oauth_url,cloudflare}'

# 批量检查 Pod（替换 ID 范围）
for i in $(seq 180 193); do
  kubectl get pod -n carher -l user-id=$i --no-headers 2>/dev/null \
    | awk -v id=$i '{printf "carher-%-4d %s %s\n", id, $2, $3}'
done

# 批量检查 CRD 状态
for i in $(seq 180 193); do
  kubectl get her her-$i -n carher \
    -o jsonpath="her-$i: phase={.status.phase} ws={.status.feishuWS} image={.spec.image}" 2>/dev/null
  echo ""
done
```

正常标准：`cloudflare.ok=true`，Pod `2/2 Running`，飞书 WS `Connected`，image = `upgrade-0402-8ef16fb`。

### Step 4: 确认 OAuth 回调地址 + Live 验证

创建成功后返回的 `oauth_url` 需要配置到对应飞书应用的重定向 URL：

```
https://s1-u{id}-auth.carher.net/feishu/oauth/callback
```

实际连通性验证：

```bash
# 正常结果应为 HTTP 400（无效测试 code），而不是 404
curl -sS -o /dev/null -w "%{http_code}\n" \
  "https://s1-u180-auth.carher.net/feishu/oauth/callback?code=test&state=test"
```

## 批量更新已有实例

如果需要创建后修改属性（如 deploy_group、model）：

```bash
curl -s -X POST "https://admin.carher.net/api/instances/batch" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"ids":[180,181,182],"action":"update","params":{"deploy_group":"test"}}'
```

## 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| Pod 一直 Pending | 节点资源不足 | `kubectl describe pod carher-{id} -n carher` 查看事件 |
| Pod CrashLoopBackOff / Error | 镜像版本过旧（不兼容 shared-config 新 key） | `kubectl patch her her-{id} -n carher --type merge -p '{"spec":{"image":"upgrade-0402-8ef16fb"}}'` |
| 创建返回 409 | ID 已存在 | 使用不同 ID 或先删除旧实例 |
| 创建返回 503 `CLOUDFLARE_API_TOKEN` | `carher-admin` 没有 Cloudflare token | 修复 `carher-admin-secrets.cloudflare-api-token`，重启 `carher-admin` 后再创建 |
| `cloudflare.ok=false` | 实例已创建，但 DNS/远程 ingress 同步失败 | 先修 Cloudflare，再执行 `POST /api/cloudflare/sync`，然后重新验证 callback URL 是否返回 400 |
| `field messages is required` 报错 | 网宿 API 兼容性问题（已禁用） | 确认 provider=litellm，路由全走 OpenRouter |
| LiteLLM key 未生成 | Admin API 调用 LiteLLM proxy 失败 | `curl -X POST "https://admin.carher.net/api/litellm/keys/generate?uid={id}" -H "X-API-Key: $API_KEY"` |
