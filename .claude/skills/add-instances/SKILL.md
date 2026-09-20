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
> **绝对不能**直接用 `lark-cli contact +search-user` 返回的 open_id，因为 lark-cli 用的是另一个飞书应用。

**推荐流程：union_id 中转**（跨 app 不变）。两步搞定：

**Step A**：用 `lark-cli api` 通过姓名直接拿 **union_id**（返回 JSON 的 `id` 字段就是 union_id，**不是** `open_id`）：

```bash
lark-cli api POST /open-apis/contact/v3/users/search \
  --params '{"user_id_type":"union_id","page_size":5}' \
  --data '{"query":"姚鹏"}'
# data.items[].id = on_xxx （union_id，跨 app 不变）
# data.items[].meta_data.i18n_names.zh_cn = "姚鹏" （用于精确匹配同名）
```

> ⚠️ **不要用 `lark-cli contact +search-user`** — 它返回的是 lark-cli app 自己的 open_id（无 union_id 字段），换 app 不能用。
> ⚠️ **不要用 `lark-cli contact +get-user --user-id ou_xxx`** — 对非自己用户返回字段被裁剪（无 union_id）。

**Step B**：用实例自己的 tenant_access_token + union_id 换 per-app open_id：

```bash
TOKEN=$(curl -s -X POST "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal" \
  -H "Content-Type: application/json" \
  -d '{"app_id":"cli_a9629f10367b1bd8","app_secret":"uQG8piJkq9tVbQ2czctVpeWojwIga2LT"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['tenant_access_token'])")

curl -s "https://open.feishu.cn/open-apis/contact/v3/users/on_xxx?user_id_type=union_id" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['user']['open_id'])"
# → ou_xxx （这才是 per-app open_id）
```

多个 owner 用 `|`（管道符）分隔，例如：`ou_aaa|ou_bbb|ou_ccc`。

### 批量场景：一次性脚本（N 人 × 1 app 或 N 人 × M apps）

```python
python3 - <<'PYEOF'
import subprocess, json, urllib.request

names = ["陈铭","陈嘉俊","戴鑫剑","刘晓龙"]  # 待加 owner
apps = {  # 目标 app credentials
    234: ("cli_aa850acb2a7c1cc6", "qJNT5sMZe5y2c167KGN0obLjS44GjB1l"),
}

# Step A: 批量查 union_id（精确匹配同名）
union_ids = {}
for name in names:
    r = subprocess.run(
        ["lark-cli","api","POST","/open-apis/contact/v3/users/search",
         "--params",'{"user_id_type":"union_id","page_size":10}',
         "--data", json.dumps({"query": name})],
        capture_output=True, text=True)
    items = json.loads(r.stdout).get("data",{}).get("items",[])
    matched = [i for i in items if i["meta_data"]["i18n_names"]["zh_cn"] == name]
    if len(matched) == 1:
        union_ids[name] = matched[0]["id"]
    else:
        print(f"⚠️ {name}: {len(matched)} 个精确同名，需人工区分 email/部门")
        for m in matched[:5]:
            print(f"   id={m['id']} email={m['meta_data'].get('enterprise_mail_address')}")

# Step B: 每个 app 自取 token 换 per-app open_id
def get_token(app_id, app_secret):
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))["tenant_access_token"]

for her_id, (app_id, app_secret) in apps.items():
    tok = get_token(app_id, app_secret)
    ou_list = []
    for name, uid in union_ids.items():
        req = urllib.request.Request(
            f"https://open.feishu.cn/open-apis/contact/v3/users/{uid}?user_id_type=union_id",
            headers={"Authorization": f"Bearer {tok}"})
        oid = json.load(urllib.request.urlopen(req))["data"]["user"]["open_id"]
        ou_list.append(oid)
        print(f"her-{her_id} | {name}: {oid}")
    print(f"\nher-{her_id} owner string: {'|'.join(ou_list)}")
PYEOF
```

**踩坑提醒**：
- Step A 必须精确匹配 `meta_data.i18n_names.zh_cn`，否则同姓名（如多个"刘强"）会拿到第一个 fuzzy 候选 → 加错人
- Step B 的 token 是 per-app 的，**不能复用**；多 app 时每个 app 单独 fetch token

## 默认值

| 参数 | 默认值 | 可选值 |
|------|--------|--------|
| provider | litellm | litellm / wangsu / openrouter / anthropic |
| model | gpt | gpt / sonnet / opus / gemini（litellm 额外支持 minimax / glm / codex） |
| prefix | s1 | s1 / s2 / s3 |
| deploy_group | stable | stable / test / canary / vip 等 |
| image | **以 carher-1000 为准**（见 Step 0.5） | operator 默认填充值可能落后于线上推进版本，必须跟 carher-1000 对齐 |
| LiteLLM 预算 | **$100/天 + budget_duration=1d**（见 Step 3.6 强制步骤）| Admin API 自动生成 `carher-{uid}` 虚拟 key 后**不会自动设预算**，必须用 `scripts/litellm-key-budget.py --apply` 补 |

## 推荐路径：H75 新建自动化流水线（2026-06-04 起）

新建 Her 不再只是 `batch-import`。当前标准流程必须一次性收敛到 H75 基线：

```text
create -> h75-hardening -> generated-config-fix -> budget -> readiness-gates
```

优先使用脚本：

```bash
scripts/create-h75-her.py \
  --id 271 \
  --name "奕达的her" \
  --app-id "cli_xxx" \
  --app-secret "xxx" \
  --owner-name "朱奕达"
```

如果已经解析好该 App 下的 per-app owner open_id，可直接传：

```bash
scripts/create-h75-her.py \
  --id 271 \
  --name "奕达的her" \
  --app-id "cli_xxx" \
  --app-secret "xxx" \
  --owner-open-id "ou_xxx"
```

有真实工作群 chat_id 时再加：

```bash
  --home-channel "oc_xxx"
```

脚本会自动完成：

- 用目标 App 的 `app_id/app_secret` 解析 owner per-app `open_id`。
- 调 Admin API `batch-import` 创建实例，默认 `provider=litellm`、`model=gpt`、`deploy_group=beta-h75-<id>`。
- 设置目标 H75 镜像 `h75-runtime-fa244014-hermestest75-20260602` 和 `carher.io/runtime-profile=h75-openclaw`。
- 通过 K8s Job 运行 `scripts/h75-batch-upgrade.py --include-target-crd --only <id>`，执行完整 H75 hardening。
- 设置 LiteLLM 默认预算 `$100/1d`。
- 修正生成态 `workflow/dify-config.json`，确保 `dify_base_url` 和 `lifecycle_base_url` 使用 K8s 内网地址。
- 等待 OpenClaw `/healthz` live。
- 验证 Hermes Feishu deps、Dify health、H75 env、base-config、writable mounts。

脚本最终 JSON 中必须重点看：

| 字段 | 通过标准 |
|---|---|
| `openclaw_ready.ok` | `true` |
| `deployment_hardening.image_ok` | `true` |
| `deployment_hardening.base_config` | `carher-base-config-h75` |
| `deployment_hardening.openai_base_ok` | `true` |
| `deployment_hardening.dify_base_ok` | `true` |
| `deployment_hardening.runtime_plugins_refresh` | `0` |
| `deployment_hardening.prod_key_matches_litellm` | `true` |
| `deployment_hardening.copy_deps_init` | `true` |
| `deployment_hardening.readonly_h75_mounts` | `[]` |
| `runtime_probes.hermes_deps_ok` | `true` |
| `runtime_probes.dify_health_ok` | `true` |

如果脚本失败，按失败 gate 处理，不要回到散装手工流程：

| 失败 gate | 常见原因 | 处理 |
|---|---|---|
| `openclaw_ready` | OpenClaw 仍在启动窗口，或前置 bootstrap 失败 | 先看 pod 日志是否到 `http server listening` / `ws client ready`；不要过早 curl 下结论 |
| `deployment_hardening.runtime_plugins_refresh` | operator/admin 新建模板回写为 `1` | 重新跑 hardening Job；最终必须是 `0` |
| `deployment_hardening.copy_deps_init` / `runtime_probes.hermes_deps_ok` | 新建模板覆盖掉 Hermes deps initContainer 或 `PYTHONPATH` | 重新跑 hardening Job，不做当前 pod 热安装替代 |
| `runtime_probes.dify_health_ok` | 生成态 `workflow/dify-config.json` 仍是公网 URL，或 Dify API 瞬时 DB 连接异常 | 脚本会先修生成态内网 URL；如果 bootstrap 日志是 `psycopg2.OperationalError server closed the connection unexpectedly`，重启该 Her 重试一次 |
| `deployment_hardening.readonly_h75_mounts` | 旧 template 的 `readOnly:true` 没被 strategic merge 删除 | hardening executor 会用更强检查；必要时 JSON Patch 替换 `volumeMounts` |

边界说明：

- `oauth_callback_http=502` 在当前 H75 参考实例上也可能出现；它属于 callback/tunnel 路由单独问题，不等同于 Her runtime 创建失败。需要 OAuth 时再专项排查。
- 没有真实 `home_channel` 时，脚本会输出 `feishu_group_smoke=not_self_tested/no_home_channel`；不能声称群 `@` 通过。
- 如果用户只提供群名，不提供真实 chat_id，不能猜测。
- 新群默认不自动开放 `group-at`；必须群主/owner 显式开启群管理模式后再验证。

## 手动 fallback 执行步骤

### Step 0: 对齐计划

收到用户数据后，**先整理成表格让用户确认**，再执行。注意检查：
- ID 是否重复（两个不同实例用了同一个 ID）
- ID 是否连续（有无空缺）
- name 和 owner 是否对应
- provider / model 用户是否有特殊要求（默认 litellm + gpt）

### Step 0.5: 查 carher-1000 当前 image（**强制步骤，不可跳过**）

新建实例的 image **必须跟 carher-1000 对齐**——operator 的默认 image 字段往往落后于线上实际推进的版本，
直接用默认值会让新实例跑老镜像，跟 stable 群组其他人不一致（bug fix、新功能、配置兼容性都会跟不上）。

```bash
curl -s -H "X-API-Key: $API_KEY" "https://admin.carher.net/api/instances/1000" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('target image:', d.get('image','N/A'))"
```

记下这个 `target_image`，Step 3.5 要用。

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
    {"id":201,"name":"姚鹏的her","app_id":"cli_a9629f10367b1bd8","app_secret":"uQG8piJkq9tVbQ2czctVpeWojwIga2LT","owner":"ou_1d72a4547f4ae57c2dd14dc97fea430f","provider":"litellm","model":"gpt"}
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

### Step 3.5: 对齐 image 到 carher-1000（**强制步骤**）

batch-import 不接受 `image` 字段，必须在创建后用 PUT 单独 patch。用 Step 0.5 拿到的 `target_image`：

```bash
TARGET_IMAGE="fix-compact-eb348941"  # 来自 Step 0.5，每次都要重查不要硬编码
for id in 231 232 233; do
  curl -s -X PUT "https://admin.carher.net/api/instances/$id" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $API_KEY" \
    -d "{\"image\":\"$TARGET_IMAGE\"}" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'  {d.get(\"id\")}: {d.get(\"action\")} image={d.get(\"changes\",{}).get(\"image\",\"N/A\")}')"
done
```

operator 看到 image 变更会重建 Pod；Step 4 验证时 image 字段需匹配 `target_image`。

### Step 3.6: 给新 her 的 LiteLLM key 设 $100/天预算（**强制步骤**，2026-05-23 起）

`provider=litellm` 时 Admin API 自动生成 `carher-{uid}` virtual key，**但不自动设预算**——历史上多次出现新 her 无限额跑爆。**必须**在 batch-import 之后手动加 $100/day + budget_duration=1d，跟 [[litellm-budget-mgmt]] skill 的默认策略对齐。

```bash
# 用专用脚本：自动 port-forward + 只补无限额的（idempotent，安全 re-run）
scripts/litellm-key-budget.py --apply

# 想精确指定（如 batch-import 刚加的 ID）：
scripts/litellm-key-budget.py --apply --key carher-234 --key carher-235 --key carher-236

# 检查全集群当前限额状态：
scripts/litellm-key-budget.py --inspect
```

预期看到 `✓ carher-NNN → $100.0/1d (was 无限额)`。`/key/update` 是热更新无需重启。

3 个特批高额度白名单（不会被覆盖）：carher-2 ($300), carher-11 ($200), carher-94 ($150)。要给某个 her 改成非默认值，用 `scripts/litellm-key-budget.py --apply --force --key carher-NNN --budget 200`。

### Step 4: 验证创建结果

**优先用 Admin API 验证**（不依赖 kubectl）。批量场景推荐合并 status + callback HTTP 一次循环：

```bash
sleep 12
for id in 214 215; do
  echo "=== Instance $id ==="
  curl -s -H "X-API-Key: $API_KEY" "https://admin.carher.net/api/instances/$id" \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)
for k in ['id','name','model','provider','owner','deploy_group','status','image','feishu_ws']:
    print(f'  {k}: {d.get(k, \"N/A\")}')
print(f'  oauth_url: {d.get(\"oauth_url\", \"N/A\")}')
"
  echo "  callback: $(curl -sS -o /dev/null -w "%{http_code}" \
    "https://s1-u${id}-auth.carher.net/feishu/oauth/callback?code=test&state=test")"
done
```

需要更细粒度时也可单独查 CRD 状态：

```bash
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

> **Pod 启动时间可能差异较大**：通常 10-15 秒内 `feishu_ws=Connected`、callback=400；
> 偶发情况（节点冷启 / 镜像 pull）可能拖到 30 秒以上。
> 如果第一次轮询有实例还是 `Disconnected` / 502，再 `sleep 20` 重跑同一段循环即可，不要急着排错。

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

## 已有实例追加 / 修改 owner

向已有实例追加新 owner（典型场景：新同事入职、跨部门授权访问某 her）。

> **关键事实**（2026-05-22 实测确认）：
> - `owner` 字段在 ConfigMap 里实际落到 `channels.feishu.dm.allowFrom` 和 `commands.ownerAllowFrom`
> - reloader sidecar 每 5s 检测 ConfigMap hash 变化并热加载，**用户不重启 pod 即可生效**
> - kubectl patch CRD spec.owner 即可触发 operator 重写 ConfigMap

### 推荐：用 `scripts/add-her-owners.py`（一键批量）

```bash
# 单实例
scripts/add-her-owners.py --id 180 --names "刘晓龙,金志刚,吕丹萍"

# stdin 批量（一行一个实例，name 用 , / ， / 、 分隔皆可）
cat <<'EOF' | scripts/add-her-owners.py
180: 刘晓龙、金志刚、吕丹萍
185: 张三、李四
EOF

# YAML 批量 + 同名歧义消除
cat <<'EOF' > /tmp/owners.yaml
- id: 180
  add: [刘晓龙, 金志刚, 吕丹萍]
- id: 185
  add: [刘强, 张三]
  user_ids: {刘强: a1b2c3d4}   # 跨 app 不变,精确指定避免拿到第一个 fuzzy
EOF
scripts/add-her-owners.py --file /tmp/owners.yaml

# Dry-run：把 union_id + per-app open_id 都解析出来,但不 patch CRD
scripts/add-her-owners.py --file /tmp/owners.yaml --dry-run
```

脚本自动处理：取 spec / 解 union_id (精确同名匹配) / 拿 per-app open_id / 已存在 owner 去重 / kubectl patch / 等 8s 验证 ConfigMap `channels.feishu.dm.allowFrom` count。

输出报告示例：
```
=== her-180 [OK] ===
  before=15  after=16  added=1  already-owner=1  unresolved=0
  + added: 徐敏
  · already owner (skipped): 刘晓龙

=== her-73 [DRY-RUN] ===
  before=2  after=2  added=0  already-owner=1  unresolved=1
  ⚠ unresolved (NOT added):
      - 不存在的人xx: NOT FOUND
```

exit code 1 = 有任意名字未解析 / patch 失败 / 验证 ConfigMap count 对不上。

### 手动流程（脚本跑不起来时的 fallback）

```bash
HER_ID=180
NEW_NAMES=("刘晓龙" "金志刚" "吕丹萍")  # 待追加

# 1. 取实例 appId + appSecret + 当前 owner
SPEC=$(kubectl get herinstance her-$HER_ID -n carher -o json)
APP_ID=$(echo "$SPEC" | python3 -c "import sys,json; print(json.load(sys.stdin)['spec']['appId'])")
CURRENT=$(echo "$SPEC" | python3 -c "import sys,json; print(json.load(sys.stdin)['spec'].get('owner',''))")
SECRET_REF=$(echo "$SPEC" | python3 -c "import sys,json; print(json.load(sys.stdin)['spec']['appSecretRef'])")
APP_SECRET=$(kubectl get secret -n carher $SECRET_REF -o json | python3 -c "
import sys, json, base64
d = json.load(sys.stdin)['data']
k = 'app_secret' if 'app_secret' in d else 'appSecret'
print(base64.b64decode(d[k]).decode())
")

# 2. 走「Owner open_id 查找规则」拿 per-app open_id（pipe 分隔 NEW）
NEW="ou_aaa|ou_bbb|ou_ccc"

# 3. 去重合并
MERGED=$(python3 -c "
existing = '$CURRENT'.split('|')
new = '$NEW'.split('|')
seen, out = set(), []
for o in existing + new:
    if o and o not in seen:
        seen.add(o); out.append(o)
print('|'.join(out))
")

# 4. patch CRD
kubectl patch herinstance her-$HER_ID -n carher --type=merge \
  -p "{\"spec\":{\"owner\":\"$MERGED\"}}"

# 5. 验证
sleep 8
kubectl get cm -n carher carher-${HER_ID}-user-config -o jsonpath='{.data.openclaw\.json}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('allowFrom count:', len(d['channels']['feishu']['dm']['allowFrom']))"
```

### 踩坑提醒

1. **必须去重**：直接 `CURRENT|NEW` 拼接会让已存在的 owner 重复出现。今天 her-180 实测追加 14 人时有 2 人已存在
2. **不需要 pod 重启**（除非要立刻测试）：ConfigMap 热加载 5s 内生效；如果用户报"权限变更不立即生效"才考虑 rollout restart
3. **CRD 名是 `her-{uid}`，Deployment / Pod 名是 `carher-{uid}`**（不一致），patch CRD 用 `kubectl patch herinstance her-XX`，重启 pod 用 `kubectl rollout restart deployment/carher-XX`
4. **同姓同名同部门**会让 union_id 抓到第一个 fuzzy 候选 → 加错人；脚本会直接报 AMBIGUOUS 拒绝处理，必须在 YAML 里用 `user_ids:` 字段精确指定（lark-cli user_id 跨 app 不变）

## 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| kubectl connection refused | 本地到 ACK 的隧道/port-forward 未启动 | 不影响创建流程，全部走 Admin API（`admin.carher.net`） |
| Pod 一直 Pending | 节点资源不足 | `curl .../api/instances/{id}/events` 查看事件，或 kubectl describe |
| Pod CrashLoopBackOff / Error | 镜像版本过旧 | `curl -X PUT .../api/instances/{id} -d '{"image":"<latest>"}'` |
| 创建返回 409 | ID 已存在 | 使用不同 ID 或先删除旧实例 |
| 创建返回 503 `CLOUDFLARE_API_TOKEN` | `carher-admin` 没有 Cloudflare token | 修复 `carher-admin-secrets.cloudflare-api-token`，重启 `carher-admin` 后再创建 |
| `cloudflare.ok=false` | 实例已创建，但 DNS/远程 ingress 同步失败 | 先修 Cloudflare，再执行 `POST /api/cloudflare/sync`，然后重新验证 callback URL 是否返回 400 |
| OAuth callback 返回 502 | Pod 刚启动，服务还没就绪 | 等待 10-30 秒后重试，正常会变成 400；超过 60 秒还 502 才需要排查 |
| `field messages is required` 报错 | 网宿 API 兼容性问题（已禁用） | 确认 provider=litellm，路由全走 OpenRouter |
| LiteLLM key 未生成 | Admin API 调用 LiteLLM proxy 失败 | `curl -X POST "https://admin.carher.net/api/litellm/keys/generate?uid={id}" -H "X-API-Key: $API_KEY"` |
