# lark MCP connector 已打通:ChatGPT 网页版原生调用飞书

**结论:ChatGPT 网页版已能通过协议级 tool call 调用我们自己的飞书 MCP server。**
实测返回真实飞书数据(群名 `her产品测试群`、真实 `chat_id`、消息历史)。
**没有一个环节靠提示词诱导。**

2026-07-27 实测,acct87 直连 chatgpt.com(不经 zerokey pod)。
前置机制见 `mcp-connector-native-toolcall.md`。

## 1. 关键决策:复用已有的 lark-mcp,不新写

**差点犯的错:准备从零写一个 lark MCP server。**
先查仓库发现 `.cursor/skills/owui-lark-mcp` —— **198 上已经跑了 64 天**的
官方 `@larksuiteoapi/lark-mcp` v0.5.1,streamable HTTP,原本给 OWUI 用。

所以本次工作量只剩"把它安全地暴露到公网"。**动手前先查现有资产。**

```
ns open-webui / deploy lark-mcp / svc ClusterIP 10.43.98.74:3000
tools: preset.default,preset.doc.default,preset.base.default,
       preset.calendar.default,preset.im.default   → 24 个工具
```

## 2. 暴露方案(用户决策)

OpenAI 硬要求**公网 HTTPS**。198 已有 nginx + `cc.auto-link.com.cn`
(公网可达、TLS 有效,实测 `https=200 tls=0`),所以挂在它下面。

**鉴权:密钥 URL 路径。** 因为实测 `custom_headers` 被服务端 gate 拦掉:

```
POST /aip/connectors/mcp  with custom_headers:[{name,value}]
→ 403 {"detail":"Custom MCP headers are not enabled"}
```

该 gate 不是用户可开的 setting(枚举里没有对应 feature),**所以密钥头这条路不通**。

用户选择:**保留全部 24 个工具**(含 `im_v1_message_create` 发消息、
`bitable_*_create/update` 写多维表格)。

> ⚠️ **安全边界(必须清楚)**:lark-mcp **自身没有任何鉴权**,
> 密钥路径是**唯一**凭据,且它持有飞书 app 凭据的**读+写**权限。
> 路径泄漏 = 飞书文档/多维表格可写 + 可发飞书消息。
> 轮换方式:重新生成路径段并重新 provision。

nginx 块见 `/etc/nginx/sites-enabled/cc.auto-link.com.cn.conf`
的 `BEGIN lark-mcp` ~ `END lark-mcp`(备份 `/root/cc.conf.bak-*`)。

**密钥路径不入库。** 唯一来源是 198 上的 nginx 配置本身:

```bash
scripts/jms ssh AIYJY-litellm \
  "grep -oE '/lark-mcp-[a-f0-9]{32}/' /etc/nginx/sites-enabled/cc.auto-link.com.cn.conf | head -1"
```

本仓库、本文档、git 历史均**不含**该路径(已 grep 全库确认)。

## 3. 踩到的三个坑(全部有对照实验)

### 坑 1:`Accept` 头不匹配 → 424 / upstream 400

OpenAI 注册时报:

```
424 {"detail":{"type":"mcp_error","kind":"http","upstream_status":400,
     "developer_message":"Client error '400 Bad Request' for url ..."}}
```

复现:lark-mcp v0.5.1 要求 `Accept` **同时**含 `application/json` 和
`text/event-stream`,但 `openai-mcp/1.0.0` 只发 `application/json`:

```
Accept: application/json          → {"code":-32000,"message":"Not Acceptable:
                                     Client must accept both ..."}
Accept: application/json, text/event-stream → 200
```

修:nginx 边缘补齐 `proxy_set_header Accept "application/json, text/event-stream";`

**规范层结论(查了 MCP 官方 spec 2025-06-18 "Transports" 章)**:

> 2. The client **MUST** include an `Accept` header, listing both `application/json` and
>    `text/event-stream` as supported content types.

所以 **`openai-mcp/1.0.0` 违反了 MCP 规范**,lark-mcp 拒绝它是**正确行为**。
我们在 nginx 补 Accept 是给客户端擦屁股,不是绕过服务端 bug ——
这个定性很重要:将来 OpenAI 修了客户端,这条 nginx 规则**仍然安全**
(补一个本就该有的头,幂等)。

顺带确认 spec 还要求 `MCP-Protocol-Version` 头,且服务端在缺失时
**SHOULD** 假定 `2025-03-26`。lark-mcp 走 SDK 默认,当前无影响。

### 坑 2:`Connection: $connection_upgrade` → 200/400 **交替**

> **⚠️ 本节已修正。** 第一版我归因为"nginx keepalive 复用连接",
> 后来做了变量隔离实验,证明**那个归因是错的**。修复恰好有效,但原因说错了。
> 记录在此因为错误的原因说明会误导后人。

修了坑 1 仍然 424。access.log 显示 OpenAI 每次发**两个**请求,
第一个 200、第二个 400,两个请求**完全一样**(都 972 字节)。

**变量隔离实验**(每次只改一个变量):

| 实验 | 配置 | 结果 |
|---|---|---|
| 直连 pod(裸 curl) | — | `200 200 200 200` |
| 直连 pod + `Connection: upgrade` | — | `200 200` |
| 直连 pod + `Connection: close` | — | `200 200 200` |
| A | keepalive **on**,`Connection: ""` | **`200 200 200 200`** |
| B | keepalive off,`Connection: $connection_upgrade` + `Upgrade` | **`200 400 200 400`** |
| C | keepalive off,只留 `Connection: $connection_upgrade` | **`200 400 200 400`** |

→ **keepalive 不是原因(实验 A 全 200);`Connection: $connection_upgrade` 才是(C 复现)。**

机制:配置里的 map 是

```nginx
map $http_upgrade $connection_upgrade { default upgrade; ""  close; }
```

OpenAI 的 MCP client 不发 `Upgrade`,所以 `$http_upgrade=""` → 转发
`Connection: close` 给上游。带调试日志抓到决定性证据:

```
POST /mcp st=200 ureq=keep-alive rlen=417 blen=223
POST /mcp st=400 ureq=close     rlen=417 blen=0     ← 请求长度一样,响应体 0 字节
```

`blen=0` 很关键:**SDK 的三个 400 都带 JSON body**
(`createJsonErrorResponse`),所以这个空 body 的 400 不是 SDK 发的,
是 lark-mcp 的 Express 层在连接被判定关闭后产生的。

**上游源码层面的根因**(读了 `@larksuiteoapi/lark-mcp` 0.5.1 +
`@modelcontextprotocol/sdk` 1.29.0 源码):

`transport/streamable.js` 每个 POST 都 `new StreamableHTTPServerTransport({sessionIdGenerator: undefined})`
—— 即**无状态模式**,并且 `res.on('close', () => { transport.close(); server.close() })`。
SDK `webStandardStreamableHttp.js:137-140`:

```js
// In stateless mode (no sessionIdGenerator), each request must use a fresh transport.
if (!this.sessionIdGenerator && this._hasHandledRequest) {
    throw new Error('Stateless transport cannot be reused across requests.')
}
```

无状态 transport 一次性,而 `Connection: close` 让 `res` 的 close 事件时序
与下一个请求交错,于是每隔一个请求就落在已 close 的 transport 上。

修:`proxy_set_header Connection "";`(这是修复的**充分且必要**部分)。
upstream 去掉 `keepalive` 是保守起见的冗余措施,实验 A 显示它并非必需。

**方法论**:第一次我用"经 nginx vs 直连 pod"的对照组定位,那只证明了
"nginx 加了什么东西",却把它误读成"keepalive"。**对照组只能缩小范围,
要确定单个变量必须逐个开关。** 这正是本仓库
`refusal-detection-postmortem.md` §8 反复强调的同一个错误。

### 坑 3:我自己把 `proxy_pass` 删掉了

清理注释乱码时用正则整块替换,**漏掉了 `proxy_pass http://lark_mcp;`**,
于是 nginx 去当静态文件服务器:

```
error.log: open() "/usr/share/nginx/html/mcp" failed (2: No such file...)
→ 404
```

教训:**404 要看 error.log 判断是"路由没匹配"还是"匹配了但没 proxy_pass"**。
两者表象一样,原因完全不同。整块替换后必须回读确认关键指令还在。

## 4. 端到端验证结果

```bash
node mcp-connector-cli.js --session sess.json provision \
  --url "https://cc.auto-link.com.cn/lark-mcp-<secret>/mcp" --name lark
```

→ 24 个 action 全部被 OpenAI 抓到 schema,link 建成。

会话内实测:

| 测试 | 结果 |
|---|---|
| `im_v1_chat_list` | ✅ 真实数据:`her产品测试群`、真实 `chat_id`、飞书 CDN 头像 URL |
| `im_v1_chat_list` → `im_v1_message_list`(**链式两步**) | ✅ 两次都成功,10 条消息 `has_more:true` |
| `docx_builtin_search` | ⚠️ `Current user_access_token is invalid or expired` |
| 重复验证 | ✅ 2/2 成功 |

模型发出的调用形态:

```
"recipient": "api_tool.call_tool"
{"paths":["lark"],"query":"im_v1_chat_list"}
```

nginx 侧确认收到真实调用(`st=200` ×3,请求体 742/1140/1736 字节递增)。

**链式两步成功 = G2b(多步 toolcall)在协议层成立**,不需要
`_looks_like_refusal()` 那套正则判定。

## 4.5 最小闭环(pod 账号,2026-07-28)

在**真实 pod 账号 acct100** 上跑通全链路,不只是 acct87 探针:

```
1. lark-mcp 公网端点           HTTP 200 (MCP initialize)
2. provision --apply           connector=asdk_app_6a681563…  link=link_6a68157e…
3. GET {id}/actions            OpenAI 抓到 24 个 action schema
4. 会话内真调用                recipient: "api_tool.call_tool"  ✅
5. 模型发出的调用体            {"path":"/lark/link_6a68157e…/im_v1_chat_list",
                                "args":{"params":{"page_size":1,…}}}
6. 返回真实飞书数据            has_more:true,items[].avatar=s3-imfile.feishucdn.com
7. 模型最终回答                "…产品测试群"
```

### 闭环过程中修掉两个批量前必须修的问题

**① 409 被误判成失败(会让批量误报)。** 同名 connector 已存在时 API 回:

```json
409 {"detail":{"message":"Connector with name 'lark' already exists",
              "existing_connector_id":"asdk_app_6a681563…"}}
```

**这不是失败** —— 而且响应体直接给了既有 id。原先归到 `OTHER` → 批量重跑会把
"已经装好了"报成"装不上"。现在归为 `EXISTS` 并复用该 id,**provision 变成幂等的**。
(之前文档里"不幂等、重复跑会堆积"的说法据此更正。)

**② 批量只信 provision 自己的输出。** `完成:` 只说明它三步都 2xx,
不代表 OpenAI 侧真抓到了 schema —— 实测 `links/noauth` **接受不存在的 action 名
仍回 200**,所以 link 成功 ≠ 工具可用。
现在注册后**独立再查一次 `actions`**,低于 `--min-actions` 记 FAIL 而非 OK。
lark-mcp 用 `--min-actions 24` 卡住。

## 5. 已知限制(不要当成已解决)

1. **`docx_builtin_search` / `docx_builtin_import` 需要 user_access_token**,
   当前只有 app token → 这两个工具不可用。
   `docx_v1_document_rawContent`(读文档正文)走 app token,**未单独实测**。
   注意:飞书 app 还需被授权访问目标文档/知识库。

2. **偶发 "blocked by OpenAI's safety checks"**。一次出现、紧接两次成功
   (2/2),所以是**间歇性**而非配置退化。原因未查明,需要长跑统计。
   这会直接影响 G2 的成功率口径,**不能忽略**。

3. **connector 是账号级**(`owners:[{type:"USER"}]`)。
   47 个 pod 账号需各自 provision 一次(幂等、可脚本化)。

4. **`custom_headers` 被服务端 gate**,当前无法用密钥头鉴权 → 只能靠密钥路径。

5. lark-mcp 单副本、无鉴权、无限流。若成为常态依赖需考虑可用性与滥用防护。

## 6. 回滚

```bash
# 1. 删 connector(在 ChatGPT 侧)
node mcp-connector-cli.js --session sess.json delete <connector_id>

# 2. 撤 nginx 路由
ssh 198 "sed -i '/# BEGIN lark-mcp/,/# END lark-mcp/d' \
  /etc/nginx/sites-enabled/cc.auto-link.com.cn.conf && \
  sed -i '/upstream lark_mcp/d' /etc/nginx/sites-enabled/cc.auto-link.com.cn.conf && \
  nginx -t && nginx -s reload"

# 或直接恢复备份 /root/cc.conf.bak-<timestamp>
```

lark-mcp 本身未改动(只是从 ClusterIP 多了一条 nginx 入口),
OWUI 的用法完全不受影响。
