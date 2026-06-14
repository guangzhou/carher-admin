---
name: owui-casdoor-sso
description: >-
  Casdoor (跑在 198 K3s idp namespace) + 飞书内置 Lark social provider 的全生命周期管理:
  加/改 OAuth provider、application、organization、user; 审计飞书 SSO 登录失败;
  CRUD Casdoor API; 处理 "built-in org 禁止建用户" / "redirect URI 配错" 等典型问题.
  Use when 用户提到 "Casdoor" / "飞书 SSO" / "飞书登录失败" / "Lark provider" /
  "open-webui 登录跳转" / "OIDC redirect URI" / "新建 Casdoor application" /
  "添加 Casdoor provider" / "飞书 app_id app_secret 改了".
---

# Casdoor 飞书 SSO 管理 (198 K3s)

Casdoor v2.45.0 跑在 `idp` namespace, 后端用 `litellm-db` postgres 的 `casdoor` 库,
作为 飞书 OAuth2 → 标准 OIDC 的转换层, OWUI 通过 OIDC 接入。

## 1. 快速入口

```bash
# Casdoor admin UI
http://10.68.13.198:30882    # 用户名 admin, 初始密码 123, 已改为 admin@admin

# OIDC discovery (供 OWUI 等下游用)
curl http://10.68.13.198:30882/.well-known/openid-configuration

# Casdoor pod
scripts/jms ssh AIYJY-litellm "kubectl get pod -n idp -l app=casdoor"
```

**关键文件**:
- 198:`/root/casdoor-manifests/casdoor.yaml` (Deployment/Service/ConfigMap/Secret)
- 198:`/root/keycloak-manifests/.secrets.env` (历史命名遗留, 含 FEISHU_APP_ID / FEISHU_APP_SECRET / CASDOOR_PG_PASSWORD)

## 2. 现状

| 对象 | 名字 | 说明 |
|------|------|------|
| Organization | `cltx` | 飞书登 SSO 的用户落在这, **不要用 built-in** (built-in 禁止建用户) |
| Provider | `lark-provider` | 内置 Lark provider, ClientID = 飞书 app_id `cli_a9278e26f138dbd3` |
| Application | `open-webui` | 给 OWUI 用的 OIDC application, 跟 `cltx` org 绑定 |
| Database | `casdoor` (in litellm-db Postgres) | 用户 `casdoor`, 密码在 `.secrets.env` |

## 3. 典型操作 (API)

Casdoor 没有方便的 CLI, 用 Cookie-based session + curl + python。所有操作走:

```bash
# 0. 登录拿 cookie (后续所有操作复用)
scripts/jms ssh AIYJY-litellm '
COOKIES=/tmp/casdoor-cookies.txt
curl -sS -c $COOKIES -X POST http://10.68.13.198:30882/api/login \
  -H "Content-Type: application/json" \
  -d "{\"application\":\"app-built-in\",\"organization\":\"built-in\",\"username\":\"admin\",\"password\":\"admin@admin\",\"type\":\"login\",\"signinMethod\":\"Password\"}" > /dev/null
echo "logged in"
'
```

### 3.1 查所有 application

```bash
scripts/jms ssh AIYJY-litellm "curl -sS -b /tmp/casdoor-cookies.txt \
  http://10.68.13.198:30882/api/get-applications | \
  python3 -c 'import sys,json
d = json.load(sys.stdin)
for a in d.get(\"data\",[]) or []:
    print(a[\"name\"], \"|\", a.get(\"clientId\",\"\")[:20], \"|\", a.get(\"organization\",\"\"),
          \"| providers:\", [p[\"name\"] for p in (a.get(\"providers\") or [])],
          \"| redirectUris:\", a.get(\"redirectUris\"))'"
```

### 3.2 加新 application (给另一个 OIDC 客户端用)

```python
# 通过 ssh 跑 python 脚本调 /api/add-application
# 模板见 docs/openwebui-litellm-perkey-binding.md 或 git log 找
# 关键字段:
#   organization: 'cltx' (不要 'built-in')
#   providers: [{owner: 'admin', name: 'lark-provider', canSignUp: True, canSignIn: True, ...}]
#   redirectUris: ['https://<client>/oauth/oidc/callback']
#   grantTypes: ['authorization_code', 'refresh_token']
```

完整 add-application 模板已在 [docs/openwebui-litellm-perkey-binding.md §4.1 流程图后的脚本范例]。

### 3.3 改 application 字段 (redirect URI / providers / org)

```python
# GET → 改 → POST update
app = call('/api/get-application?id=admin/open-webui', method='GET')['data']
app['redirectUris'] = sorted(set(app['redirectUris'] + ['https://new.host/oauth/oidc/callback']))
# 或: app['organization'] = 'cltx'
call('/api/update-application?id=admin/open-webui', app)
```

### 3.4 查 cltx 组织下的飞书登录用户

```bash
scripts/jms ssh AIYJY-litellm "kubectl exec -n litellm-product statefulset/litellm-db -- \
  env PGPASSWORD=pro-pg-pass-20260430-46138a20 psql -U litellm -d casdoor -c \
  \"SELECT name, email, lark, signup_application, created_time FROM \\\"user\\\" WHERE owner='cltx';\""
```

字段 `lark` 存的是飞书 open_id (`ou_xxx`), `email` 存的是飞书 enterprise_email。

### 3.5 重启 Casdoor

```bash
scripts/jms ssh AIYJY-litellm "kubectl rollout restart deployment/casdoor -n idp && \
  kubectl rollout status deployment/casdoor -n idp"
```

## 4. 故障排查决策树

### 4.1 用户登录卡在 Casdoor "目前向 'built-in' 组织添加新用户的功能已禁用"

**根因**: open-webui application 错绑 built-in org, Casdoor 拒在 built-in 建新用户

**修法**: 把 application org 改成 `cltx` (§3.3 + `app['organization']='cltx'`)

### 4.2 OWUI 登录页没显示飞书 SSO 按钮

**根因**: OWUI 没接到 Casdoor OIDC, 或 OAUTH_PROVIDER_NAME 没设

**排查**:
```bash
# 1. OWUI env
scripts/jms ssh AIYJY-litellm "kubectl exec -n open-webui deployment/open-webui -- env | grep -E 'OPENID|OAUTH'"
# 2. OWUI 能否调 Casdoor discovery
scripts/jms ssh AIYJY-litellm "kubectl exec -n open-webui deployment/open-webui -- curl -sS -o /dev/null -w '%{http_code}' http://10.68.13.198:30882/.well-known/openid-configuration"
# 期望: 200
```

### 4.3 飞书授权完跳回 Casdoor 报错 / 跳不到 OWUI

**根因 1**: Casdoor application 的 `redirectUris` 列表没有当前 OWUI URL

**根因 2**: 飞书后台 redirect URI 白名单没配 `http://10.68.13.198:30882/callback`

**修法**:
- Casdoor: §3.3 加 OWUI 的 callback URL
- 飞书: 开 [飞书开放平台](https://open.feishu.cn) → app `cli_a9278e26f138dbd3` → 安全设置 → 重定向 URL → 加 `http://10.68.13.198:30882/callback`

### 4.4 callback 502 (即使 Casdoor + OWUI 都通) 🔴

**99% 真根因**: OWUI `ENABLE_OAUTH_ID_TOKEN_COOKIE=true` 默认行为, set 完整 OIDC id_token (2-6KB) 进 Set-Cookie, 公司 LB nginx 默认 `large_client_header_buffers` 撑不住给 502。

**一行修复**:
```bash
scripts/jms ssh AIYJY-litellm "kubectl set env -n open-webui deployment/open-webui \
  ENABLE_OAUTH_ID_TOKEN_COOKIE=false"
```

**易混淆的诊断陷阱**:
- 198 nginx access log 显示 OWUI 返回 307 不代表浏览器看到 307 (上游 LB 可能改 status)
- fake code `curl /oauth/oidc/callback?code=fake` 不会触发问题 (OWUI 错误分支不设大 cookie)
- 必须看 OWUI 真实 callback 源码 (`oauth.py:1780-1830`) 看 `set_cookie('oauth_id_token', ...)` 才能定位

**完整决策树**: 见 [[owui-ops]] §2.5

## 5. Casdoor 内部 admin 密码改

```bash
# Casdoor admin UI: 右上头像 → My Account → Password 字段 → Save
# 不能用 admin API 改, 这是 UI flow
```

或直接改 postgres (危险, 当前 password_type=plain):
```sql
UPDATE "user" SET password='new-strong-pw' WHERE owner='built-in' AND name='admin';
```

**密码 password_type=plain 是默认配置**, 强烈建议改成 bcrypt 但需要 Casdoor admin UI 操作。

## 6. 关键约束

1. **新用户必须落 `cltx` org**, 不能用 built-in (built-in 是 Casdoor 自身管理 org)
2. **redirect URI 三段一致**: OWUI WEBUI_URL / Casdoor application.redirectUris / 飞书后台白名单 都要写全完整 URL
3. **飞书 app_id/app_secret 一改**: 必须同步改 Casdoor lark-provider 配置 + 198 节点 `.secrets.env`
4. **password_type=plain**: 直接看明文; 不要把 Casdoor 数据库暴露给非 ops

## 7. 踩过的坑

1. **Casdoor 默认 admin 密码是 `123`** (写死), 不读 env `CASDOOR_ADMIN_PASSWORD`. 登进去**立刻在 UI 改**
2. **Casdoor v2.45.0 镜像不带 sqlite3 driver**: panic `unknown driver "sqlite3"`. 必须配 postgres + 创建专用 user/db
3. **app.conf 的 dataSourceName 不带 dbname 时 fallback 到 localhost:5432**: 必须显式 `host=... port=5432 dbname=casdoor`
4. **K8s envFrom secretRef 的 env 引用语法**: yaml args 数组里写 `$(VAR_NAME)`, K8s 会替换; 但本地 shell `$VAR` 会被本地 shell 提前展开变空, 必须用 `set -a + . secrets.env + set +a` 或在远端写 python 脚本
5. **Casdoor `built-in` org 默认禁建用户**: 第一次飞书登录会卡在"该功能已禁用"; **必须新建 cltx 等业务 org**
6. **Casdoor lark-provider 字段名**: ClientID 填飞书 app_id, ClientSecret 填飞书 app_secret. 同时飞书后台要给该 app 加 `contact:user.email:readonly` + `contact:user.base:readonly` scope
7. **OWUI v0.9.5 不支持子路径**: Casdoor 可以挂子路径但 OWUI 不行, 索性 Casdoor 也走独立端口 30882 不挂子路径
8. **OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true 默认行为**: 飞书登的 email 跟现有 OWUI user.email 匹配 → 合并; 没匹配 → 建新 user. 切环境前手动改 user 表 email 字段是关键

## 8. 相关 skill / docs

- [[owui-ops]] - OWUI 端 OAuth env / 用户表管理
- [[owui-key-swap-proxy]] - 反代 (准入闸门 + 模型 filter)
- 主文档: `docs/openwebui-litellm-perkey-binding.md`
- 上游 Casdoor 文档: https://casdoor.org/docs/provider/oauth/lark/
