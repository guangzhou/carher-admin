---
name: reset-instance-owner
description: >-
  Reset / hand over a CarHer her instance to a new master while keeping the
  same instance id, prefix, OAuth callback URL and image. Purges all old
  user data (memory, workspace, agents, feishu-user-tokens, LiteLLM key)
  and re-creates the instance with the new name + per-app owner open_id +
  (optionally new) Feishu app credentials. Use when the user says
  "her 换主人了" / "重置 her" / "her 给别人用了" / "her 接手" / "transfer her" /
  any time an existing instance must be re-issued to a different owner with
  all old data wiped clean. Distinct from `add-instances` (brand-new),
  `clone-instance-memory` (copy data → new id), and
  `carher-instance-config-override` (change one field, keep data).
---

# 重置 Her 实例（换主人）

老主人不再使用某个 her，新主人接手。本 skill 在**保留实例 ID / prefix / OAuth callback URL** 的前提下，完成"清空老主人全部数据 + 切换到新主人"。

## 与其他 skill 的边界

| 场景 | Skill |
|------|-------|
| 全新建 her | `add-instances` |
| 拷贝源 her 数据 → 新 ID | `clone-instance-memory` |
| **同 ID 换主人（销毁旧数据）** | **本 skill** |
| 改单实例某字段、保留数据 | `carher-instance-config-override` |

## 必备信息

| 字段 | 说明 |
|------|------|
| 实例 ID | 要换主人的 her id（如 41） |
| 新主人飞书姓名 | 用于查 per-app open_id |
| 新主人显示名 | 用于 `name`，例如 `杨丞的her` |
| 新 app_id / app_secret | 取决于新主人是否换飞书应用（详见下） |
| 新 prefix / model / provider / deploy_group | 选填，**默认与原实例对齐** |

## 关键判断：飞书 app 是否换？

| 情况 | 飞书侧动作 | 备注 |
|------|------------|------|
| **不换 app** | ✅ 零配置 | 最常见、最省事 |
| **换 app** | 新主人在新应用后台配置 ① OAuth 回调 URL = `https://{prefix}-u{id}-auth.carher.net/feishu/oauth/callback`；② 机器人事件订阅 | callback URL 与 ID/prefix 绑定，**不会随 app 换而变** |

> **prefix 不要随便改**，否则 callback URL 变，飞书侧旧配置全部失效。除非用户明确要求改，否则原样保留。

## 方案对比

| 方案 | 做法 | 推荐度 |
|------|------|--------|
| **A. purge 重建** | `DELETE ?purge=true` 连 PVC 删干净 → `batch-import` 重建 → PUT 对齐镜像 | ⭐ **首选** |
| B. 原地清数据 | stop → 临时 Pod 挂 PVC `rm -rf` 用户目录 → PUT 改 name/owner/app_* → start | 仅在 PVC 上有需要保留的非用户数据时用 |

A 优于 B 的理由：
- PVC 全新空白，零残留（尤其 `feishu-user-tokens/` 这类敏感目录漏清会让新主人用旧主人身份调飞书 API）
- LiteLLM 虚拟 key 自动重建，spend 归零，老主人消费不会糊到新主人
- 操作只有 3 个 API 调用，简单不易漏

## 完整流程（方案 A）

### Step 0: 取 API_KEY

```bash
API_KEY=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.admin-api-key}' | base64 -d)
# kubectl 不可用时见 add-instances skill 的硬编码备份
```

### Step 1: 现状盘点

```bash
curl -s -H "X-API-Key: $API_KEY" "https://admin.carher.net/api/instances/{ID}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for k in ['id','name','app_id','model','provider','prefix','owner','deploy_group','status','image','feishu_ws','oauth_url','paused']:
    print(f'  {k}: {d.get(k, \"N/A\")}')
"
```

记录：`prefix`（决定 callback URL）、`image`（重建后必须对齐）、`model` / `provider` / `deploy_group` / 旧 `app_id`。
注意 `prefix` 字段在响应中可能显示 `N/A`，从 `oauth_url` 反推（`s2-u{id}-...` → prefix=`s2`）。

### Step 2: 验证新 app 凭据 + 查新主人 per-app open_id

> 飞书 open_id 是 per-app 的，**必须用要写入实例的那个 app 的 appId+appSecret 去查**。`lark-cli` 给的 open_id 是 lark-cli app 的，绝不能用。

```bash
APP_ID="<要写入实例的 app_id>"
APP_SECRET="<对应 app_secret>"

TOKEN=$(curl -s -X POST 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal' \
  -H 'Content-Type: application/json' \
  -d "{\"app_id\":\"$APP_ID\",\"app_secret\":\"$APP_SECRET\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['tenant_access_token'])")

USER_ID=$(lark-cli contact +search-user --query "新主人姓名" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['users'][0]['user_id'])")

NEW_OWNER=$(curl -s "https://open.feishu.cn/open-apis/contact/v3/users/$USER_ID?user_id_type=user_id" \
  -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['user']['open_id'])")
echo "new owner = $NEW_OWNER"
```

token 拿到 = app_secret 有效；同时也确认新 app_id+secret 是真实匹配的一对。

### Step 3: purge 删除旧实例

```bash
curl -s -X DELETE -H "X-API-Key: $API_KEY" \
  "https://admin.carher.net/api/instances/{ID}?purge=true"
sleep 5
curl -s -H "X-API-Key: $API_KEY" "https://admin.carher.net/api/instances/{ID}"
# 期望返回 {"detail":"Instance {ID} not found"}
```

`purge=true` 同时删除 CRD、Pod、Service、ConfigMap、Secret、PVC、LiteLLM 虚拟 key。

### Step 4: 重建（同 ID）

```bash
curl -s -X POST "https://admin.carher.net/api/instances/batch-import" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "instances": [{
      "id": {ID},
      "name": "{新主人显示名}",
      "prefix": "{原 prefix}",
      "app_id": "'$APP_ID'",
      "app_secret": "'$APP_SECRET'",
      "owner": "'$NEW_OWNER'",
      "model": "{原 model}",
      "provider": "{原 provider}",
      "deploy_group": "{原 group}"
    }]
  }' | python3 -m json.tool
```

返回必须满足 `cloudflare.ok=true`，否则停下排查（参考 `cloudflare-tunnel-routing` skill）。

### Step 5: 对齐镜像（仅当原实例非默认镜像）

```bash
curl -s -X PUT "https://admin.carher.net/api/instances/{ID}" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"image":"{原 image tag}"}'
```

不对齐镜像会导致 operator 用 base-config 当前默认镜像，行为可能漂移。

### Step 6: 等就绪 + Live 验证

```bash
for i in $(seq 1 18); do
  out=$(curl -s -H "X-API-Key: $API_KEY" "https://admin.carher.net/api/instances/{ID}" \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print("{}|{}".format(d.get("status","?"), d.get("feishu_ws","?")))')
  st=$(echo "$out" | cut -d'|' -f1)
  ws=$(echo "$out" | cut -d'|' -f2)
  printf "  [%2ds] %s %s\n" $((i*5)) "$st" "$ws"
  [ "$st" = "Running" ] && [ "$ws" = "Connected" ] && break
  sleep 5
done

# callback live check：期望 400（不是 502/404）
curl -sS -o /dev/null -w "%{http_code}\n" \
  "https://{prefix}-u{ID}-auth.carher.net/feishu/oauth/callback?code=test&state=test"
```

> ⚠️ zsh 中 `status` 是只读变量，循环里用 `st` 代替。

### Step 7: 回执给新主人

- **不换 app**：飞书侧零配置，直接 @ {新主人}的her 开始聊
- **换 app**：先在新飞书应用后台配 ①OAuth 回调 URL ②机器人事件订阅 → 让新主人访问 `oauth_url` 走一次飞书登录完成绑定

## 必清目录清单（仅当走方案 B）

方案 A 自动通过 PVC purge 解决；方案 B 必须显式清以下路径，**漏一个就有数据/身份残留风险**：

| 路径 | 内容 | 漏清后果 |
|------|------|----------|
| `workspace/MEMORY.md` `SOUL.md` `USER.md` `IDENTITY.md` | bot 人格 / 用户画像 | bot 仍记得老主人 |
| `memory/` | 语义记忆 SQLite | 旧对话被检索回来 |
| `agents/` | 对话历史 | 同上 |
| `feishu-user-tokens/` | 飞书用户 access_token | **新主人用旧主人身份调飞书 API** |
| `feishu-groups/` `feishu-doc-backups/` `feishu-*-cache.json` `feishu-sent-messages.json` | 飞书会话/缓存 | 历史群/消息混入 |
| `identity/` `canvas/` `cron/` `extensions/` `subagents/` `tasks/` `media/` `browser/` `devices/` `delivery-queue/` `compaction-reports/` `exec-approvals.json` `.voice-token` | 其他运行时数据 | 残留行为 |

不需要清（由 ConfigMap / 共享 PVC / operator 自动覆盖）：`openclaw.json*`、`carher-config.json`、`shared-config.json5`、`skills/`、`sessions/`、`logs/`、`update-check.json`。

## 常见坑

| 坑 | 后果 / 防范 |
|------|-------------|
| 用 `lark-cli` 的 open_id 写 owner | open_id 是 per-app 的，新 owner 不会被识别为主人；必须用实例自己的 token 查 |
| 重建忘了 PUT image | 落到 base-config 默认镜像，与原版本不一致 |
| 改了 prefix | callback URL 变，飞书侧旧配置全部失效；保持原 prefix 才能"飞书侧零配置" |
| `DELETE` 漏 `purge=true` | PVC + LiteLLM key 残留，老主人 memory / feishu token / spend 进入新实例 |
| 先建新再删旧（顺序错） | 同 app_id 双订阅飞书事件，消息错乱；必须**先 DELETE 再 batch-import** |
| 新老主人共用 app 时仍重新配飞书后台 | 完全没必要，浪费新主人时间 |
