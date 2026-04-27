---
name: add-instances
description: >-
  Batch-create new CarHer bot instances on K8s via Admin API.
  Use when adding new users/her instances to the cluster, onboarding
  new users, or bulk-importing instances with Feishu app credentials.
---

# 批量新增 CarHer 实例

通过 Admin API 批量创建 her 实例，operator 会自动完成 CRD → Deployment → Pod 的全流程。

**全流程仅需 `curl` + `lark-cli`，不依赖 kubectl。** 本地 K8s 隧道不通时照常操作。

## 前置条件

### 获取 API_KEY

优先从 kubectl 获取；kubectl 不可用时用备选方案：

```bash
# 方案 A：kubectl 可用时
API_KEY=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.admin-api-key}' | base64 -d)

# 方案 B：kubectl 不可用（本地隧道未启动）
# 直接用硬编码值（从过往成功 session 或 agent-transcripts 中获取）
API_KEY="bd037b874cb170710c4873b0cdec924539bcddba879f4556ae40875a15e4a1e4"
```

### 验证 API 连通性 + Cloudflare token

```bash
# 快速验证 API_KEY 有效（返回非 401 即可）
curl -s -o /dev/null -w "%{http_code}" -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/next-id

# Cloudflare token 检查（kubectl 可用时）
kubectl exec -n carher deploy/carher-admin -- \
  python -c "import os; print(bool(os.environ.get('CLOUDFLARE_API_TOKEN')))"

# kubectl 不可用时：直接创建，如果返回 503 + CLOUDFLARE_API_TOKEN 提示，
# 说明 token 缺失，需先修 admin secret。
```

## 数据格式

用户通常提供如下信息（多行文本，每个实例一组）：

| 字段 | 说明 | 示例 |
|------|------|------|
| id | 实例 ID（可选，省略则自动分配） | 180 |
| name | 显示名称 | 永康的her |
| app_id | 飞书 App ID | cli_a95e1e0534795cd1 |
| app_secret | 飞书 App Secret | VJJMhZlJ2XYad3Dl4jxTJcj5nTiU1ESq |
| owner | 所属用户（中文名或 ou_xxx，多人用 `\|` 分隔） | 辛永康 |

### Owner open_id 查找规则

> **关键**：飞书 open_id 是 per-app 的，同一个用户在不同飞书应用下有不同的 open_id。
> **必须用该实例自己的 appId + appSecret** 获取 tenant_access_token 后查询。
> **绝对不能**直接用 `lark-cli` 查到的 open_id，因为 lark-cli 使用的是另一个飞书应用。

**完整命令示例**（以 app_id=cli_a9629f10367b1bd8 查找「姚鹏」为例）：

```bash
# 1. 用 lark-cli 查 user_id（跨应用一致）
lark-cli contact +search-user --query "姚鹏"
# → user_id: "debb89ae"

# 2. 用实例自己的凭据换 tenant_access_token
curl -s -X POST "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal" \
  -H "Content-Type: application/json" \
  -d '{"app_id":"cli_a9629f10367b1bd8","app_secret":"uQG8piJkq9tVbQ2czctVpeWojwIga2LT"}'
# → tenant_access_token: "t-g1044nav..."

# 3. 用该 token 查该应用下的 open_id
curl -s "https://open.feishu.cn/open-apis/contact/v3/users/debb89ae?user_id_type=user_id" \
  -H "Authorization: Bearer t-g1044nav..."
# → open_id: "ou_1d72a4547f4ae57c2dd14dc97fea430f"（这才是正确的 per-app open_id）
```

多个 owner 用 `|`（管道符）分隔，例如：`ou_aaa|ou_bbb|ou_ccc`。

## 默认值

| 参数 | 默认值 | 可选值 |
|------|--------|--------|
| provider | litellm | litellm / wangsu / openrouter / anthropic |
| model | opus | gpt / sonnet / opus / gemini（litellm 额外支持 minimax / glm / codex） |
| prefix | s1 | s1 / s2 / s3 |
| deploy_group | stable | stable / test / canary / vip 等 |
| image | （不指定） | operator 自动填充当前线上版本 |

## 执行步骤

### Step 0: 对齐计划

收到用户数据后，**先整理成表格让用户确认**，再执行。注意检查：
- ID 是否重复（两个不同实例用了同一个 ID）
- ID 是否连续（有无空缺）
- name 和 owner 是否对应
- provider / model 用户是否有特殊要求（默认 litellm + opus）

### Step 1: 预检（ID 冲突 + API 连通）

```bash
# 确认 next-id 以及指定 ID 是否已被占用
curl -s -H "X-API-Key: $API_KEY" https://admin.carher.net/api/next-id

# 如果用户指定了 ID（如 201），额外确认该 ID 不存在
curl -s -H "X-API-Key: $API_KEY" "https://admin.carher.net/api/instances/201"
# 期望返回 {"detail":"Instance 201 not found"}
```

### Step 2: 查找 owner per-app open_id

按照上面「Owner open_id 查找规则」的三步流程操作。**每个实例都必须用自己的 app 凭据查**。

### Step 3: batch-import 创建实例

**必须显式指定 `provider` 和 `model`**，不要依赖后端默认值。
**owner 字段传 per-app 的 `ou_xxx`**，不要传中文名：

```bash
curl -s -X POST "https://admin.carher.net/api/instances/batch-import" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
  "instances": [
    {"id":201,"name":"姚鹏的her","app_id":"cli_a9629f10367b1bd8","app_secret":"uQG8piJkq9tVbQ2czctVpeWojwIga2LT","owner":"ou_1d72a4547f4ae57c2dd14dc97fea430f","provider":"litellm","model":"opus"}
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
      "id": 201,
      "status": "created",
      "managed_by": "operator",
      "oauth_url": "https://s1-u201-auth.carher.net/feishu/oauth/callback",
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

### Step 4: 验证创建结果

**优先用 Admin API 验证**（不依赖 kubectl）：

```bash
# 查实例详情：status, feishu_ws, image, model, provider, owner
curl -s -H "X-API-Key: $API_KEY" "https://admin.carher.net/api/instances/201" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
for k in ['id','name','model','provider','owner','deploy_group','status','image','feishu_ws']:
    print(f'  {k}: {d.get(k, \"N/A\")}')
print(f'  oauth_url: {d.get(\"oauth_url\", \"N/A\")}')
"

# 查 CRD 状态（phase, feishuWS, image）
curl -s -H "X-API-Key: $API_KEY" "https://admin.carher.net/api/crd/instances/201" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
status = d.get('status', {})
print(f'phase: {status.get(\"phase\",\"N/A\")}')
print(f'feishuWS: {status.get(\"feishuWS\",\"N/A\")}')
print(f'image: {d.get(\"spec\",{}).get(\"image\",\"N/A\")}')
"
```

> **Pod 刚启动时 feishu_ws 可能为 Disconnected**，等 10-15 秒后重查通常会变 Connected。

kubectl 可用时也可以批量检查：

```bash
for i in $(seq 200 201); do
  kubectl get pod -n carher -l user-id=$i --no-headers 2>/dev/null \
    | awk -v id=$i '{printf "carher-%-4d %s %s\n", id, $2, $3}'
done
```

正常标准：`status=Running`，`feishu_ws=Connected`，`cloudflare.ok=true`。

### Step 5: 确认 OAuth 回调地址 + Live 验证

创建成功后返回的 `oauth_url` 需要配置到对应飞书应用的重定向 URL：

```
https://s1-u{id}-auth.carher.net/feishu/oauth/callback
```

实际连通性验证：

```bash
# 正常结果应为 HTTP 400（无效测试 code），而不是 404 或 502
# ⚠️ Pod 刚启动时可能返回 502，等 10 秒后重试
curl -sS -o /dev/null -w "%{http_code}\n" \
  "https://s1-u201-auth.carher.net/feishu/oauth/callback?code=test&state=test"
```

| HTTP 码 | 含义 |
|---------|------|
| 400 | 正常（Pod 在线，code 无效） |
| 502 | Pod 还在启动，等 10 秒重试 |
| 404 | Cloudflare DNS/tunnel 未同步，检查 `cloudflare` 字段 |

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
| kubectl connection refused | 本地到 ACK 的隧道/port-forward 未启动 | 不影响创建流程，全部走 Admin API（`admin.carher.net`） |
| Pod 一直 Pending | 节点资源不足 | `curl .../api/instances/{id}/events` 查看事件，或 kubectl describe |
| Pod CrashLoopBackOff / Error | 镜像版本过旧 | `curl -X PUT .../api/instances/{id} -d '{"image":"<latest>"}'` |
| 创建返回 409 | ID 已存在 | 使用不同 ID 或先删除旧实例 |
| 创建返回 503 `CLOUDFLARE_API_TOKEN` | `carher-admin` 没有 Cloudflare token | 修复 `carher-admin-secrets.cloudflare-api-token`，重启 `carher-admin` 后再创建 |
| `cloudflare.ok=false` | 实例已创建，但 DNS/远程 ingress 同步失败 | 先修 Cloudflare，再执行 `POST /api/cloudflare/sync`，然后重新验证 callback URL 是否返回 400 |
| OAuth callback 返回 502 | Pod 刚启动，服务还没就绪 | 等待 10-15 秒后重试，正常会变成 400 |
| `field messages is required` 报错 | 网宿 API 兼容性问题（已禁用） | 确认 provider=litellm，路由全走 OpenRouter |
| LiteLLM key 未生成 | Admin API 调用 LiteLLM proxy 失败 | `curl -X POST "https://admin.carher.net/api/litellm/keys/generate?uid={id}" -H "X-API-Key: $API_KEY"` |
