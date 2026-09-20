---
name: owui-casdoor-sso
description: >-
  OWUI (chat.auto-link.com.cn) 的飞书 SSO 登录。**现役 = 飞书直连**(OWUI 原生 feishu
  provider, 见 §9), Casdoor 已不在登录路径上只作回滚。含: 登录报"邮箱或密码不对"的真实根因
  (claim 名不对, 文案会撒谎)、卡"账号待激活"、非 admin 看不到模型列表、按钮显示 feishu
  不显示飞书、Recreate 被僵尸 Pod 卡成 0 副本; §1~§8 是 Casdoor 那条路的档案。
  Use when 用户提到 "飞书 SSO" / "飞书登录失败" / "OWUI 登录" / "邮箱或密码不对" /
  "账号待激活" / "看不到模型" / "Casdoor" / "Lark provider" / "OIDC redirect URI" /
  "飞书 app_id app_secret 改了".
---

# Casdoor 飞书 SSO 管理 (198 K3s)

> ## ⛔ 2026-09-14：OWUI 已不走 Casdoor 了，改成飞书直连
> `chat.auto-link.com.cn` 现在用 OWUI **原生 `feishu` provider**，链路是 `飞书 → OWUI`，
> Casdoor **不在登录路径上**（进程仍在跑，只作为回滚路径保留）。
> 配置与踩坑全文见本文件末尾 **§9 飞书直连（现役）**。
> 下面 §1~§8 是 Casdoor 那条路的档案，只有回滚时才用得上。


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
# PG 口令走 env，不写进文档/命令行。先在本地 shell:  export LITELLM_PG_PW=<litellm-db-0 口令>
scripts/jms ssh AIYJY-litellm "kubectl exec -n litellm-product statefulset/litellm-db -- \
  env PGPASSWORD=\"\$LITELLM_PG_PW\" psql -U litellm -d casdoor -c \
  \"SELECT name, email, lark, signup_application, created_time FROM \\\"user\\\" WHERE owner='cltx';\""
```

> ⛔ 这段以前把 PG 口令明文写在文档里（2026-09-20 摘掉）。**口令必须轮转** ——
> 原值在 5 个历史提交里，且已推到 `origin/main`，摘当前树不等于消除暴露。

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


---

# 9. 飞书直连（现役，2026-09-14）

OWUI v0.11.3 自带原生 `feishu` provider（`config.py:2750` 的 `load_oauth_providers()`），
飞书 OAuth2 不用再经 Casdoor 翻译成 OIDC。切换后 Casdoor 从登录路径上整个摘掉。

## 9.1 现役配置

```bash
kubectl set env deployment/open-webui -n open-webui \
  FEISHU_CLIENT_ID="$FEISHU_APP_ID" \
  FEISHU_CLIENT_SECRET="$FEISHU_APP_SECRET" \
  FEISHU_REDIRECT_URI="https://chat.auto-link.com.cn/oauth/feishu/login/callback" \
  FEISHU_OAUTH_SCOPE="contact:user.base:readonly contact:user.email:readonly" \
  OAUTH_SUB_CLAIM=open_id \
  OAUTH_EMAIL_CLAIM=email \
  OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true \
  OAUTH_PROVIDER_NAME="飞书" \
  OPENID_PROVIDER_URL- OAUTH_CLIENT_ID- OAUTH_CLIENT_SECRET- OAUTH_SCOPES-
```
凭据源：`198:/root/keycloak-manifests/.secrets.env`（`set -a; . <file>; set +a`，不要回显）。

飞书开放平台白名单要有（只比 scheme+host+path，query 忽略）：
`https://chat.auto-link.com.cn/oauth/feishu/login/callback`
（顺带把 `/oauth/feishu/callback` 也加上，OWUI `main.py:2800` 有个 Legacy 路由）。

**备份/回滚**：`198:/root/owui-sso-backups/open-webui.20260914-104213.yaml`，
回滚 = `kubectl apply -f <该文件>`。Casdoor 一直没停，回滚即可用。

## 9.2 ⛔ 报错文案会撒谎：三个不同的坑共用同一句话

`The email or password provided is incorrect.` = `ERROR_MESSAGES.INVALID_CRED`，
`utils/oauth.py` 里**好几个分支**都抛它。**不要照字面去查密码**，
唯一判据是 pod 日志里紧挨着的那行 `OAuth callback failed, XXX is missing: {user_data}`，
它会把飞书返回的**完整 claim 形状**打出来。

```bash
kubectl logs -n open-webui deployment/open-webui --tail=200 | grep -iE 'oauth|is missing'
```

实测飞书 `/authen/v1/user_info` 返回（**顶层平铺，没有 `data` 包装**）：
```
{'avatar_big':..., 'avatar_middle':..., 'avatar_thumb':..., 'avatar_url':...,
 'email': 'x@auto-link.com.cn', 'en_name': '刘国现', 'name': '刘国现',
 'open_id': 'ou_...', 'tenant_key': '...', 'union_id': 'on_...', 'user_id': 'b31fb4g4'}
```
⇒ **邮箱字段就叫 `email`**。我按飞书文档习惯填 `OAUTH_EMAIL_CLAIM=enterprise_email` ⇒
`email is missing` ⇒ 用户看到"邮箱或密码不对"。改成 `email` 即修好。
`open_id` 在，所以 `OAUTH_SUB_CLAIM=open_id` 是对的
（provider 内置默认是 `sub_claim: 'user_id'`，全局 `OAUTH_SUB_CLAIM` 会压过它）。

## 9.3 账号连续性：只能靠 `OAUTH_MERGE_ACCOUNTS_BY_EMAIL`

`Users.get_user_by_oauth_sub(provider, sub)` **按 provider 名查**，
老账号存的是 `{"oidc": {"sub": "ou_..."}}`，provider 换成 `feishu` 后必然查不中。
⇒ `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true` 是**必须的**，否则全员被当新用户重建。
判据：切换后 `user` 表**行数不变**（实测仍是 2 行，没冒出新账号）。

## 9.4 登录成功却卡"账号待激活"

不是登录失败，是 OWUI 的新用户审批：`DEFAULT_USER_ROLE=pending`。

⚠️ **光改环境变量没用** —— 这个是 PersistentConfig，首次启动后以 `webui.db`
`config` 表的 `ui.default_user_role` 为准，**db 那份压过 env**。两边都要改：

```bash
kubectl set env deployment/open-webui -n open-webui DEFAULT_USER_ROLE=user
# 且：
python -c "import sqlite3; c=sqlite3.connect('/app/backend/data/webui.db'); \
  c.execute(\"update config set value='\\\"user\\\"' where key='ui.default_user_role'\"); c.commit()"
# 已经卡住的存量用户：
#   update user set role='user' where role='pending'
```
改 db 前先 `cp webui.db webui.db.bak-role-<ts>`。

判断"哪些设置是 PersistentConfig"：`config.py` 里出现在 `'xxx.yyy': ENV_NAME` 那张
映射表里的（3190~3220 行附近），一律 db 优先。

## 9.5 非 admin 用户看不到任何模型（0 个）

**跟角色无关，也不是权限没给。** `utils/models.py:get_filtered_models` 的原话：

```python
elif user.role == 'admin':
    # No DB entry means no access control configured yet;
    # only admins can see unconfigured models.
```

LiteLLM 连接透传进来的 210 个模型在 `model` 表里**没有行** ⇒ OWUI 判定"未配置访问控制"
⇒ 只给 admin 看。给用户加组、加 access_grant 都没用（那是给"有 db 行的模型"用的）。

解：`BYPASS_MODEL_ACCESS_CONTROL=true`（在 `env.py`，不是 PersistentConfig，env 直接生效）。
⚠️ 代价：租户内任何人都能看到全部模型，要做细粒度就得给每个模型建 `model` 行 + `access_grant`。

**判据必须是逐用户实打 `/api/models`**，不能只看配置：
```python
# pod 内，必须 cd /app/backend 否则 import 不到 open_webui
from datetime import timedelta
from open_webui.utils.auth import create_token
import json, urllib.request
tok = create_token(data={"id": "<该用户 id>"}, expires_delta=timedelta(minutes=10))
req = urllib.request.Request("http://127.0.0.1:8080/api/models", headers={"Authorization": "Bearer "+tok})
print(len(json.load(urllib.request.urlopen(req))["data"]))
```
实测：改前 admin 210 / 普通用户 **0**；改后两边都是 210。

## 9.6 用户名显示成 `4ae52ag1` 这种 id

`OAUTH_UPDATE_NAME_ON_LOGIN` 默认 **False**（`config.py:2626`）⇒ 老账号的名字
（Casdoor 时代留下的）**永远不刷新**。设成 `true` 后下次登录自动变成飞书的 `name`。
配套 `OAUTH_UPDATE_PICTURE_ON_LOGIN=true` 同理。

## 9.7 登录按钮显示 `feishu` 而不是 `飞书`

`OAUTH_PROVIDER_NAME` **只作用于 `oidc`** —— `config.py:2745` 只给 `OAUTH_PROVIDERS['oidc']`
塞了 `'name'` 键，feishu 那个字典（2769）没有。而 `main.py:2290` 是
`{name: provider.get('name', name) ...}` ⇒ 取不到就退回 key。

**不要整文件 COPY `config.py` 去改**（升级镜像会静默回退上游改动）。
`load_oauth_providers()` 在模块导入时就跑完，而 `/api/config` 是**请求时**才读那个 dict
⇒ 启动后往 dict 里补一个键即可，不碰镜像里任何文件：

```bash
# 文件落在 PVC 上，升级镜像/重启都不丢
/app/backend/data/pypatch/sitecustomize.py
kubectl set env deployment/open-webui -n open-webui PYTHONPATH=/app/backend/data/pypatch
```
`sitecustomize.py` 包一层 `builtins.__import__`，等 `open_webui.config` 进 `sys.modules`
后给 `OAUTH_PROVIDERS['feishu']` 补 `name`（取 `OAUTH_PROVIDER_NAME`），补完撤钩子。
撤销 = 删掉 `PYTHONPATH` 这个 env。
判据：`curl -s https://chat.auto-link.com.cn/api/config` → `{"providers":{"feishu":"飞书"}}`。

## 9.8 这个 Deployment 是 `strategy: Recreate`,会被僵尸 Pod 卡死

滚动更新后变成 **0 副本、站点直接打不开**，`kubectl describe` 显示
`NewReplicaSet: <none>`、所有 RS 的 DESIRED 都是 0，但 `spec.replicas=1`、没有 HPA。

根因：`app=open-webui` 选择器下挂着一个几天前的 **`Completed` 僵尸 Pod**，
Recreate 要等旧 Pod 全部消失才建新的。判据：删掉它之后新 Pod **20 秒内**就起来了。

```bash
kubectl get pod -n open-webui -l app=open-webui   # 找 STATUS=Completed 的
kubectl delete pod <那个> -n open-webui           # 它不接流量,不违反"禁删正在服务的 Pod"
```
⇒ **每次动 open-webui 之前先扫一眼有没有 Completed 残留。**

## 9.9 已知降级

- **登出只是 OWUI 本地登出**，飞书那边的会话还在。飞书没有标准 `end_session` 端点，
  `OPENID_PROVIDER_URL` 已移除，启动日志那条 `logout will not work` 警告是预期的。
- Casdoor 仍在 `idp` ns 空转，**只作回滚用**。确认稳定后再决定要不要下线。
