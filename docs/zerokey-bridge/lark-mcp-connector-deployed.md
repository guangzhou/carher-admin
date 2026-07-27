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

### 坑 2:keepalive 复用连接 → 200/400 **交替**

修了坑 1 仍然 424。access.log 显示 OpenAI 每次发**两个**请求,
第一个 200、第二个 400,且两个请求 **完全一样**(都 972 字节、无 session id)。

关键对照实验:

| 路径 | 连发结果 |
|---|---|
| 经 nginx(`keepalive 8`) | `200 400 200 400` —— 严格交替,与间隔无关(试过 3s) |
| **直连 pod ClusterIP** | **`200 200 200 200`** |

→ 不是 lark-mcp 的问题,是 nginx 复用上游连接导致的。

修:upstream 去掉 `keepalive`,并在 location 里 `proxy_set_header Connection "";`
(原来的 `Connection $connection_upgrade` 会重新启用复用)。
修完连发 6 次全 200。

**这里差点误判成"lark-mcp 有状态 bug"** —— 靠"直连 vs 经代理"的对照组才定位对。

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
