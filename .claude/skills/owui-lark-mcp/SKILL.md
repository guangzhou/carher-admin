---
name: owui-lark-mcp
description: >-
  198 K3s 上的 OpenWebUI 怎么操作飞书。两条路:①现役 = 沙箱终端里的 lark-cli,
  已通过 models.default_metadata 全局常驻给所有模型(§0/§8.7/§8.9);
  ②备用 = lark-mcp (官方 @larksuiteoapi/lark-mcp) 走 streamable HTTP 当 MCP 工具,OAuth 已修但授权不持久,现已搁置。
  Use when 用户提到 "OWUI/openwebui 建飞书文档" / "lark-cli" / "模型说它不会用飞书" /
  "新会话又忘了" / "怎么让所有模型都带某个 skill/工具" / "OWUI 默认开终端" /
  "lark-mcp" / "飞书 MCP" / "MCP server 授权" / "扩飞书 scope".
---

# OWUI × 飞书:lark-cli(现役)与 lark-mcp(搁置)

## 0. 先看这里:现在走哪条路

| | ① `lark-cli`(**现役**) | ② `lark-mcp`(**搁置**) |
|---|---|---|
| 载体 | `open-terminal` 沙箱里的 CLI,模型用终端工具跑 | OWUI External Tools 里的 MCP server |
| 身份 | tenant/bot 身份(见 §8.9 的 `permission_grant: skipped` 坑) | 每用户 OAuth(user_access_token) |
| 授权持久性 | 不需要授权,env 里有凭据 | ❌ **内存态,pod 一重启全员重授**(§8.5,未修) |
| 状态 | 2026-09-13 已全局常驻,裸模型一轮就会用 | OAuth 三坑已修(§8.4/§8.6/§8.8),但用户决定先不用 |

> ⏸ 用户口径(2026-09-13):"我现在用 lark-cli 授权挺好了,先不用管它了"。
> **除非用户重新提起,不要主动去推 MCP 授权。** §1~§8 里 MCP 的内容当故障档案读。

**让模型"记住"飞书能力的唯一全局杠杆 = `models.default_metadata`**,完整配方在 **§8.7**:

```
config 表 models.default_metadata = {"skillIds":["<lark Skill id>"], "terminalId":"open-terminal-main"}
```
- `skillIds` → 模型常驻拿到 lark-cli 说明书
- `terminalId` → 前端自动选中终端 ⇒ 请求体才有 `terminal_id` ⇒ 终端工具才会挂上
- ⛔ `models.default_params` 不在注入路径上,往里写等于没写

---

## 1~11:lark-mcp 部署与故障档案

跑在 198 K3s `open-webui` ns, 1 副本, 暴露 streamable HTTP MCP 协议给 OWUI 用。
OWUI v0.6.31+ 原生支持外接 MCP server, 大模型自己决定何时调飞书 API。

## 1. 快速入口

```bash
scripts/jms ssh AIYJY-litellm "kubectl get pod -n open-webui -l app=lark-mcp -o wide"

# MCP endpoint
http://lark-mcp.open-webui.svc.cluster.local:3000/mcp    # in-cluster
# 外部访问 (调试): 暴露 NodePort 或 port-forward

# 实时日志
scripts/jms ssh AIYJY-litellm "kubectl logs -n open-webui -l app=lark-mcp -f --tail=50"

# 协议握手验证
kubectl run mcp-test --rm -i --restart=Never --image=curlimages/curl:8.10.1 --quiet -n open-webui --command -- \
  curl -sS -X POST http://lark-mcp.open-webui.svc.cluster.local:3000/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
# 期望返回 {"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{...}},"serverInfo":{...}}}
```

**关键文件**:
- 198:`/root/lark-mcp/Dockerfile` (基于 node:20-slim + 阿里云 mirror)
- 198:`/root/lark-mcp-manifests/lark-mcp.yaml` (Deployment + Service + Secret)

## 2. 飞书 app 凭据

当前**复用 Casdoor SSO 同一个 app** `cli_a9278e26f138dbd3` (历史决策, 关注点未隔离)。

凭据存 K8s `lark-mcp-secrets`:
- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`

## 3. 必须的飞书 scope (你去飞书后台审批)

到 [飞书开放平台](https://open.feishu.cn) → app `cli_a9278e26f138dbd3` → **权限管理** → 申请:

| 业务 | scope | 用途 |
|------|-------|------|
| **Docs (只读)** | `docx:document:readonly` | 读 docx 文档 |
| **Docs (读写)** | `docx:document` | 创建 / 修改 文档 |
| **Docs 媒体** | `docx:document.media:download` | 文档图片附件下载 |
| **Wiki** | `wiki:wiki:readonly` + `wiki:node:read` | 知识库浏览 |
| **Bitable (只读)** | `bitable:app:readonly` | 多维表格读 |
| **Bitable (读写)** | `bitable:app` | 多维表格 CRUD |
| **Sheets (只读)** | `sheets:spreadsheet:readonly` | 表格读 |
| **Sheets (读写)** | `sheets:spreadsheet` | 表格读写 |
| **Drive** | `drive:drive:readonly` + `drive:file:readonly` | 云空间浏览 |
| **IM** | `im:message`, `im:chat:readonly` | (谨慎) 发消息、读群聊 |
| **Calendar** | `calendar:calendar`, `calendar:calendar.event:create` | 日程读写 |

**审批可能要 1-3 工作日**。审批通过后 `lark-mcp` Pod **重启即可生效** (token 是 tenant_access_token, 启动时拉一次):

```bash
kubectl rollout restart deployment/lark-mcp -n open-webui
```

## 4. tools preset 调整

`-t` / `--tools` 参数控制启用哪些 tools。默认配置 (`/root/lark-mcp-manifests/lark-mcp.yaml`):

```yaml
args:
  - mcp
  - --mode
  - streamable
  - --host
  - "0.0.0.0"
  - --port
  - "3000"
  - --app-id
  - $(FEISHU_APP_ID)
  - --app-secret
  - $(FEISHU_APP_SECRET)
  - --tools
  - "preset.default,preset.doc.default,preset.base.default,preset.calendar.default,preset.im.default"
  - --language
  - "zh"
```

**preset 速查**:
- `preset.default` - 常用基础工具
- `preset.doc.default` - 文档 Docs
- `preset.base.default` - 多维表格 Bitable
- `preset.calendar.default` - 日历
- `preset.im.default` - 即时消息
- `preset.task.default` - 任务

加 preset 改 manifest 后 `kubectl apply`。如果 V8 heap OOM (`Reached heap limit`), 调大:
```yaml
env:
  - name: NODE_OPTIONS
    value: --max-old-space-size=3000   # 默认 1.4GB 不够
resources:
  limits:
    memory: 4Gi
```

## 5. 镜像 build / 升级

```bash
scripts/jms ssh AIYJY-litellm '
NEW=v0.1.1
cd /root/lark-mcp
# 改 Dockerfile 里 lark-mcp@latest 改 lark-mcp@<具体版本>
docker build -t 127.0.0.1:5000/lark-mcp:$NEW .
docker push 127.0.0.1:5000/lark-mcp:$NEW
kubectl set image -n open-webui deployment/lark-mcp lark-mcp=127.0.0.1:5000/lark-mcp:$NEW
kubectl rollout status deployment/lark-mcp -n open-webui
'
```

**禁止 alpine base**: `keytar` native 模块在 alpine 缺 python+make+g+++libsecret-dev, 编译失败。固定 `node:20-slim` (debian)。

## 6. OWUI 接 MCP (Phase 5c)

OWUI v0.6.31+ Admin Panel → Settings → **External Tools** → Add MCP server:

```
Type: streamable_http
URL: http://lark-mcp.open-webui.svc.cluster.local:3000/mcp
Authentication: None
```

或通过 OWUI API 自动加 (脚本化, 待补)。

加完后:
- Admin Panel → Workspace → Models → 创建或编辑模型, **Tools** 里勾 `lark-mcp`
- 用户在对话框点 "Tool" 按钮启用 lark 工具
- 模型问答时自动 tool_call 调飞书

## 7. 典型对话效果验证

启用 lark MCP 后:
```
用户: 帮我看一下 "Q3 OKR 规划" 文档讲了什么
模型: (自动 tool_call: lark.search → 找文档 token → lark.doc.read → 拿内容)
模型: 这份文档主要讲了三个目标: ...
```

## 8. 故障排查

### 8.1 lark-mcp Pod 起不来 CrashLoopBackOff

```bash
kubectl logs -n open-webui -l app=lark-mcp --previous | tail -30
```

常见原因:
- **OOM**: `Reached heap limit Allocation failed - JavaScript heap out of memory` → 加 `NODE_OPTIONS=--max-old-space-size=3000` + memory limit 4Gi
- **app_id 无效**: `FEISHU_APP_ID/FEISHU_APP_SECRET invalid` → 检查 secret 内容
- **machine-id warn**: `StorageManager Failed to initialize encryption: Cannot spawn a message bus without a machine-id` — **可忽略**, 只 disable User Access Token store, tenant_access_token 模式正常

### 8.2 OWUI 调 MCP 工具失败

```bash
# 1. OWUI Pod 能否调 lark-mcp
kubectl exec -n open-webui deployment/open-webui -- curl -sS -o /dev/null -w '%{http_code}\\n' \
  http://lark-mcp.open-webui.svc.cluster.local:3000/mcp -X POST \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# 期望: 200

# 2. lark-mcp 实时日志看请求是否到达
kubectl logs -n open-webui -l app=lark-mcp -f
```

### 8.3 工具调用返回飞书权限错误

99% scope 不够。回 §3 重新审批所需 scope, audit 通过后 `kubectl rollout restart deployment/lark-mcp -n open-webui`。

### 8.4 OWUI 点授权报 `{"detail":"Failed to re-register OAuth client"}` (500)

**症状**: 浏览器打开 `https://chat.auto-link.com.cn/oauth/clients/mcp:lark-mcp/authorize` 直接吐这段 JSON。

**根因**: MCP SDK `@modelcontextprotocol/sdk/dist/cjs/server/auth/router.js:45-54` 用
`new URL('/authorize', baseUrl || issuer)` 构造元数据端点。`/authorize` 是**绝对路径**,
URL 解析把 issuer 里的 `/lark-mcp-<hash>` 前缀**整段丢掉** → 发布出去的
`authorization_endpoint` / `token_endpoint` / `registration_endpoint` 全是根级
`https://cc.auto-link.com.cn/{authorize,token,register}`,而 nginx 只路由带前缀那条 → 根级 404
→ OWUI 动态客户端注册(DCR)吃到 nginx 404 HTML → 500。

传 `baseUrl` 没用(绝对路径同样吃掉 path)。CM `lark-mcp-public-oauth-20260906` 里的 cjs
只改了 `issuerUrl`/`callbackUrl`,管不到这三个端点。

**⛔ 不能在生成处修**: SDK 在 `router.js:74-85` 用 `new URL(metadata.authorization_endpoint).pathname`
**挂载真实路由**。改生成处会把真实路由也挪到带前缀的路径,而 nginx 那条 location 又会 rewrite 掉前缀
→ 全部 404。**只能改"发布出去的那份文档"。**

**已装的修法** (2026-09-13, 198 nginx, 备份 `/etc/nginx/backups/`):
在 `location = /.well-known/oauth-authorization-server/lark-mcp-<hash>` 及其 `/mcp` 变体里加

```nginx
proxy_set_header Accept-Encoding "";
sub_filter_types application/json;
sub_filter_once off;
sub_filter "https://cc.auto-link.com.cn/authorize" "https://cc.auto-link.com.cn/lark-mcp-<hash>/authorize";
sub_filter "https://cc.auto-link.com.cn/token"     "https://cc.auto-link.com.cn/lark-mcp-<hash>/token";
sub_filter "https://cc.auto-link.com.cn/register"  "https://cc.auto-link.com.cn/lark-mcp-<hash>/register";
```

用 `sub_filter` 派生而非静态 `return 200` 写死 JSON —— 写死的会在 lark-mcp 升级后报旧真相。

**验收三腿**(全部要过):
```bash
curl -sS https://cc.auto-link.com.cn/.well-known/oauth-authorization-server/lark-mcp-<hash>   # 三个端点都带前缀
curl -sS -X POST https://cc.auto-link.com.cn/lark-mcp-<hash>/register -H 'Content-Type: application/json' \
  -d '{"client_name":"probe","redirect_uris":["https://chat.auto-link.com.cn/oauth/clients/mcp%3Alark-mcp/callback"],"grant_types":["authorization_code"],"response_types":["code"],"token_endpoint_auth_method":"none"}'   # 201 + client_id
curl -sS -o /dev/null -w '%{http_code}\n' "https://cc.auto-link.com.cn/lark-mcp-<hash>/authorize?client_id=<上一步>&response_type=code&code_challenge=E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM&code_challenge_method=S256&redirect_uri=..."   # 302 → open.feishu.cn
```
**终判仍要用户在 OWUI 里实打一次创建文档**,探针全绿不算数。

### 8.5 `invalid_client Invalid client_id` / 重启后全员要重新授权

lark-mcp 容器日志固定有:
```
[StorageManager] Failed to initialize encryption: Cannot spawn a message bus without a machine-id
[StorageManager] ⚠️ Builtin User Access Token Store will be disabled. but you can still use it with memory store
```
⇒ `isInitializedStorageSuccess=false` ⇒ **DCR 注册的 client 和用户飞书 token 全在内存**
(`dist/auth/store.js:12` 的 `storageDataCache`)。

**后果**: lark-mcp pod 每重启一次,OWUI 存的 `client_id` 就失效 → preflight
(`oauth.py:_preflight_authorization_url`) 报 `invalid_client Invalid client_id` → 触发重注册;
所有用户的飞书授权也一并清空,要重点一次授权。

**注意**: 光往镜像里塞 `/etc/machine-id` **不够** —— keytar 还要一个 secret service 守护进程
(gnome-keyring/dbus),容器里没有。真要持久化得 patch `storage-manager.js` 改成
env 里取固定 key + STORAGE_DIR 落 PVC。**未做**。

### 8.6 点授权跳飞书后报 20029「重定向 URL 有误,请联系应用管理员」

飞书白名单里没有 lark-mcp 的回调地址。**要加的就是这一条**(开放平台 → app
`cli_a9278e26f138dbd3` → 安全设置 → 重定向 URL):

```
https://cc.auto-link.com.cn/lark-mcp-<hash>/callback
```

**匹配规则(2026-09-13 实测,阳性对照用 Casdoor 已白名单的 `https://dash.auto-link.com.cn/callback`)**:

| 变形 | 结果 |
|---|---|
| 白名单原值 | PASS |
| 原值 + 任意 query | **PASS** ⇒ query 不参与匹配 |
| 原值 + 结尾 `/` | PASS |
| 原值 + 更深一层 path | 20029 ⇒ 不做前缀匹配 |
| https → http | 20029 |

⇒ 只比 **scheme + host + path**。所以实际发出去的
`.../callback?redirect_uri=https%3A%2F%2Fchat.auto-link.com.cn%2F...` 那串 query **不用管**,
填不带 query 的裸地址即可。

**不用登录就能验白名单的探针**(198 上跑;`open.feishu.cn` 会 302 到 `accounts.feishu.cn`,
**必须 `-L`**,否则 body 空会误读成"探针不通"):

```bash
enc=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$1")
curl -sSL "https://open.feishu.cn/open-apis/authen/v1/index?app_id=cli_a9278e26f138dbd3&redirect_uri=$enc" \
  | grep -q 20029 && echo NOT-WHITELISTED || echo WHITELISTED
```

### 8.7 新会话又不会建飞书文档了 / 怎么让所有模型常驻一个 Skill(**全局杠杆 SOP**)

**不是装丢了**。`open-terminal` Deployment 用 `OPEN_TERMINAL_NPM_PACKAGES='@larksuite/cli'`
注入 CLI,`/home` 挂 PVC `open-terminal-home`,都是持久的。

真相是 **OWUI 没有任何常驻指令告诉模型"有个 Linux 沙箱、里面装了 lark CLI、飞书文档就用它建"**。
实测 `webui.db`:

```
skill 表  0 行
prompt 表 0 行
model 表只有 test / gpt-5.6-sol-lark,两个 params 都是 {}   ← 没有 system prompt
models.default_params = {}                                  ← 没有全局兜底 system prompt
gpt-5.6-luna 在 model 表里根本没有记录                        ← 裸透传,零配置
```

每个新会话都是白纸。上次能建成功是因为用户在那轮对话里现场教了它。

**两个独立成因,提示词只能治一个**:

| 成因 | 证据 | 只加 system prompt 能不能治 |
|---|---|---|
| (a) 根本没挂终端工具 | `middleware.py:2936` `if terminal_id and terminal_capability:`,而 `terminal_id` 来自请求体、是**每个会话手选**的 | ❌ 不能 |
| (b) 没有常驻指令说"有 lark-cli" | `skill`/`prompt` 表 0 行、model params `{}` | ✅ 能 |

**全局唯一的杠杆 = `models.default_metadata`**(config 表一行)。`utils/models.py:335` 把它 merge 进
**每个模型**的 `info.meta`(已有同名 key 不覆盖),而:
- `meta.skillIds` → `middleware.py:2735` 读它挂 Skill
- `meta.terminalId` → 前端选模型时 `Ai.set(terminalId)` 自动选中终端 ⇒ 请求体就带上 `terminal_id`

⚠️ `models.default_params` **不参与注入**,是死路,别往那写。

**修法(2026-09-13 已装,全局生效)**:

1. `webui.db` 建 Skill 行 `lark-cli 飞书操作`(id `a3428e6a-9536-4b42-b790-f8a481b8643a`),
   内容 = §8.9 那套(命令名 `lark-cli`、`+` 快捷命令、`@file` 传多行、bot 身份坑)。
2. 建 `access_grant`(`resource_type='skill'`, `principal_type='user'`, `principal_id='*'`,
   `permission='read'`)—— 否则 `SkillsTable.get_skills` 会把它过滤掉,只有 owner 看得见。
3. `config` 表 `models.default_metadata` 从 `{}` 改成
   `{"skillIds":["a3428e6a-..."],"terminalId":"open-terminal-main"}`。
4. `kubectl rollout restart deployment/open-webui -n open-webui`(配置有缓存,不重启不生效)。

备份 `/app/backend/data/webui.db.bak-larkskill-<ts>`(pod 内);回滚 = 把它拷回去再 rollout restart。

**验收(真实用法,不是探针)**:拿**裸模型** `gpt-5.6-luna`(之前就是它回"我无法访问飞书")
打 `/api/chat/completions`,一轮就吐出
`lark-cli docs +create --title "..." --doc-format markdown --content @/tmp/xxx.md`,
`prompt_tokens` 3730(注入发生)。照它吐的命令实跑,文档建出来了。

早先还给 `gpt-5.6-sol-lark` 单独写过 `params.system`(备份 `webui.db.bak-larkprompt-<ts>`),
现在是冗余的,留着不冲突(per-model 优先于 default_metadata)。

⚠️ 改 `webui.db` 时 **`kubectl exec -i` 会吃掉外层 `ssh 'bash -s' <<EOF` 的 stdin**,
python heredoc 会静默不执行(退出码还是 0)。走 base64 单行传进去:

```bash
# 本机写好 /tmp/x.py,然后:
B64=$(base64 < /tmp/x.py | tr -d '\n')
scripts/jms ssh AIYJY-litellm \
  "kubectl exec -n open-webui deployment/open-webui -- sh -c 'echo $B64 | base64 -d > /tmp/x.py && python /tmp/x.py'"
```

**表结构(照抄用)**:
```sql
skill        (id, user_id, name UNIQUE, description, content, meta JSON, is_active, updated_at, created_at)
access_grant (id, resource_type, resource_id, principal_type, principal_id, permission, created_at)
config       (key, value, updated_at)        -- value 是 JSON 文本
```
公开授权那行 = `resource_type='skill'`, `principal_type='user'`, `principal_id='*'`, `permission='read'`。

**一条命令验证全局是否真的铺开了**(pod 内跑,自签 admin JWT 打 `/api/models`):
```python
from datetime import timedelta; from open_webui.utils.auth import create_token
import json, urllib.request
tok = create_token(data={"id": "<admin user id>"}, expires_delta=timedelta(minutes=10))
req = urllib.request.Request("http://127.0.0.1:8080/api/models", headers={"Authorization": "Bearer "+tok})
for m in json.load(urllib.request.urlopen(req))["data"]:
    meta = (m.get("info") or {}).get("meta") or {}
    print(m["id"], meta.get("skillIds"), meta.get("terminalId"))
```
⚠️ 要 `cd /app/backend` 且脚本落在 `/app/backend/` 下才 import 得到 `open_webui`(`/tmp` 下会 ModuleNotFoundError)。
⚠️ 这只证明**元数据铺到了**;终判还得拿裸模型真打一句"创建一个飞书文档"看它吐不吐命令。

**注入形态的一个细节**:`middleware.py` 里,非 `@mention` 且开着 builtin tools 时,
模型先只看到 Skill 的 **name + description**(manifest),要不要读全文由它自己决定。
所以 **description 必须自带触发词**("飞书/Lark/创建文档/多维表格"),写得含糊会不触发。

### 8.8 白名单加完后改报 `OAuth callback failed: invalid_request ... client_id ... received undefined`

**这不是鉴权问题,是 body 根本没送到上游。** 根因在 sidecar `policy-proxy`
(CM `lark-mcp-policy-proxy`):它先 `req.on('data')` 把请求体缓冲进 `chunks`,
转发时非 JSON 分支却走 `req.pipe(upstream)` —— 流已经被抽干,**转发 0 字节**。

JSON 请求(`/register`、`/mcp`)走"解析成对象再重新序列化"那条路,一直正常;
只有 **form-urlencoded 的 OAuth `/token`** 静默变空 ⇒ `express.urlencoded` 得到 `{}` ⇒
MCP SDK `clientAuth.js` 的 zod 报 `client_id: expected string, received undefined` ⇒ 400。

**证伪腿(先做这个,别急着改鉴权)**:把凭据放 body(client_secret_post)和放 Basic 头各打一次
`/token`。若返回**一模一样**的 `client_id undefined`,就不是鉴权方式不匹配 —— 是 body 没到。

**已装的修法** (2026-09-13,备份 `198:/root/lark-mcp-backups/`):

```js
function proxyRequest(req, res, body, rawChunks) {   // ← 多接一个 rawChunks
  ...
  if (body !== null) upstream.write(JSON.stringify(body));
  else if (rawChunks && rawChunks.length) upstream.write(Buffer.concat(rawChunks));
  upstream.end();
}
// 调用处: proxyRequest(req, res, body, chunks);
```

回滚:`kubectl apply -f /root/lark-mcp-backups/lark-mcp-policy-proxy.<ts>.yaml`
+ `kubectl rollout restart deployment/lark-mcp -n open-webui`。

**验收(两种形状必须给出不同的错)**:
```
client_secret_post → {"error":"server_error"} / PKCE validation failed   ← 已过 clientAuth,正常
client_secret_basic → 仍报 client_id undefined                            ← SDK 确实不支持 Basic,但 OWUI 不用它
```
OWUI `oauth.py:680` 默认 `client_secret_post`,且我们发布的元数据里
`token_endpoint_auth_methods_supported` 第一项就是它,所以不受影响。

### 8.9 lark CLI 在沙箱里的正确命令名

`open-terminal` 里装的是 `@larksuite/cli@1.0.94`,**可执行文件叫 `lark-cli`,不是 `lark`**
(`/usr/bin/lark-cli` → `/usr/lib/node_modules/@larksuite/cli/scripts/run.js`)。
`FEISHU_APP_ID`/`FEISHU_APP_SECRET` 已注入 env,开箱即用,不用 login。

CLI 自带 agent 向导:`lark-cli --help` / `lark-cli <domain> --help` /
`lark-cli schema <svc>.<res>.<method>` / `lark-cli skills read lark-doc`。
优先用 `+` 开头的快捷命令。建文档实测通过:

```bash
lark-cli docs +create --title "标题" --doc-format markdown --content @/tmp/body.md
```

⚠️ 默认以 **bot 身份**创建,返回里 `permission_grant.status = skipped` —— 文档建出来了但
**当前用户没被授权**,要么 `lark-cli auth login`,要么在飞书 UI 里手动共享。

## 9. 关键约束

1. **token 是 app-level tenant_access_token**: 任何 OWUI 用户调工具都用同一个 app token; 看到的资源 = 该 app 已被 add_member 的资源 (不是单个员工的飞书可见范围)
2. **写操作有风险**: 用户问"帮我把文档删掉"模型可能真删. 一期建议只开 readonly scope (`*:readonly`)
3. **app token 全自动**: lark-mcp 启动时拉 tenant_access_token, 2 小时自动 refresh; 不需要存 auth.json
4. **不能跟 Casdoor SSO 共用 token mode 字段**: token-mode 默认 auto, 不要改成 user_access_token (OAuth Beta, 需要每个用户单独走 OAuth)

## 10. 踩过的坑

1. **lark-mcp v0.5.1 默认 transport=stdio**: 必须显式 `--mode streamable` + `--host 0.0.0.0` + `-p 3000`, 否则 OWUI 接不上
2. **OOM 在 lark-mcp 加载工具列表时**: V8 heap 默认不够, 必须 `--max-old-space-size=3000`
3. **K8s envFrom secretRef args 引用**: yaml args 数组里 `$(FEISHU_APP_ID)` 才会替换 (K8s native substitution), shell `$` 不会
4. **alpine vs debian**: alpine 缺 keytar native deps, 用 node:20-slim 比较省事
5. **larksuite/cli vs lark-openapi-mcp 区别**: 前者是 CLI + AI Agent Skills (给 humans 用), 后者是 MCP server (给 LLM 用)。
   ⚠️ 2026-09-13 反转:**现役是前者** —— 让模型在沙箱里当人用 CLI,比 MCP 那套 OAuth 靠谱得多(见 §0)
6. **machine-id 警告可忽略**: keytar 系统密钥环不可用, 但 lark-mcp 只用 app-level token, 不需要 user OAuth keyring
7. **"模型说它不会"先别信是模型笨**: 先查工具挂没挂上(`terminal_id` 在不在请求体里),再查有没有常驻指令。
   这俩是**独立的两个成因**,只改提示词治不了前者(§8.7)

## 11. 相关 skill / docs

- [[owui-ops]] - OWUI 端启用 External Tools / MCP server
- [[owui-casdoor-sso]] - 飞书 app 凭据共用 (复用 Casdoor 同一个 app)
- 主文档: `docs/openwebui-litellm-perkey-binding.md`
- 上游: https://github.com/larksuite/lark-openapi-mcp
- 飞书开放平台: https://open.feishu.cn

⚠️ 本 skill 原先只存在于 `.cursor/skills/`,**Claude Code 加载不到**。
2026-09-13 已在 `.claude/skills/owui-lark-mcp` 建软链指回这份,两边同源、不会漂移。

