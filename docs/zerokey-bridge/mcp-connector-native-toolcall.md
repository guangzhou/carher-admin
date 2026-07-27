# 网页版原生 tool call:MCP Connector 通路(已验证)

**结论:用户的判断是对的 —— `api_tool` 可以注册 lark-cli。**
路径不是"在请求里声明工具",而是"先把 MCP server 注册成 connector"。

全部数据来自 acct87 直连 chatgpt.com(不经过 zerokey pod),2026-07-27。

## 1. 为什么之前判断错了

之前我用 `local_function_names` 在会话请求里声明 `lark_docs_fetch`,被模型拒绝:

> "the available api_tool capabilities in this chat do not include a /lark/cli/docs_fetch tool"

我据此得出"api_tool 不能注册自定义工具"。**这是错的** —— 我只证明了
"运行时会话里不能凭空声明",没有去找注册入口。

`Plugin_Management` 的 4 个能力(模型自述)也确实没有安装能力:

| 函数 | 作用 |
|---|---|
| `get_app_permissions` | 查权限 |
| `get_plugin_dependencies` | 查依赖(明确不安装) |
| `uninstall_app` | 卸载 |
| `update_app_permissions` | 改权限 |

模型明确回答"没有任何能力能从 URL/OpenAPI 注册插件" —— **对话层面确实没有**。
注册入口在 HTTP API 层,不在对话层。这是两个不同的平面,不能互相证否。

## 2. 真正的注册通路

### 2.1 路由发现方法(可复现)

猜路径全部 404。正确做法是从前端 bundle 里提取:

```bash
# 1. 拉首页(带完整 session headers)
# 2. 提取 /cdn/assets/*.js,递归爬 lazy chunk(两轮,得 1142 个文件)
# 3. grep 'aip/[a-zA-Z0-9_/{}$.-]+'
```

得到真实路由(节选):

```
aip/connectors/mcp                      ← 注册入口
aip/connectors/mcp/oauth_config
aip/connectors/mcp/tunnels
aip/connectors/mcp/refresh_actions
aip/connectors/{connector_id}/actions   ← 工具列表
aip/connectors/links/noauth
```

**关键细节:必须带 `x-openai-target-path` / `x-openai-target-route`。**
不带就是 403 + Cloudflare HTML(会被误读成"没权限");带上才拿到真实 JSON。
区分方法:`content-type: text/html` = 没到 API;`application/json` = 到了。

另需 `oai-client-surface: CONNECTOR_SETTING`。

### 2.2 前置条件:开启 developer mode

首次调用 `mcp/tunnels` 返回:

```json
403 {"detail":"Developer mode is required"}
```

**这是真实的功能门,不是 404 —— 说明路由存在、账号只是没开。**

feature 名从 bundle 里取到:`e.DeveloperMode = "developer_mode"`。
参数走 **query 而非 body**(body 会 422 提示 `loc:["query","feature"]`):

```
PATCH /backend-api/settings/account_user_setting?feature=developer_mode&value=true
→ 200 {"developer_mode":true}
```

之后 `mcp/tunnels` → `200 {"tunnels":[]}`。

账号侧支撑证据(`/backend-api/accounts/check/v4-2023-04-27`):
- `plan_type: pro` / `chatgptpro`
- `enabledConnectors` 含 **`mcp_connector`**
- features 含 `plugins_available`、`new_plugin_oauth_endpoint`

### 2.3 注册

```
POST /backend-api/aip/connectors/mcp
{
  "mcp_url": "https://<host>/mcp",
  "name": "...", "description": "...",
  "custom_headers": [],            // 必须是 list,不是 dict
  "auth_request": {"type":"none"}  // 必须是 object,不能是 null
}
```

用公开 MCP server 实测通过:

```
200 {"connector":{"id":"asdk_app_6a677a46fc7c81919b3db2af697031cf",
     "connector_type":"MCP","service":"https://mcp.deepwiki.com",...}}
```

### 2.4 验证 OpenAI 真的抓到了工具 schema

```
GET /backend-api/aip/connectors/{id}/actions
→ 200 {"actions":[{"name":"ask_question",
        "description":"Ask any question about a GitHub repository...",
        "params":{"properties":{"repoName":{...},"question":{...}},
                  "required":["repoName","question"],"type":"object"},
        "is_enabled":true, "is_consequential":true}]}
```

**这是 OpenAI 服务端主动去 MCP server 拉的真实 JSON Schema。**
不是提示词,不是我们伪造的 —— 是协议级的工具定义。

### 2.5 建 link ← **必做,漏了会静默失败**

只 register 会拿到 200,但**模型在会话里看不到这个工具**,
`recipient` 永远只有 `"all"`。我第一次就踩了这个坑,差点误判成"注册无效"。

```
POST /backend-api/aip/connectors/links/noauth
{ "connector_id":"asdk_app_...", "name":"deepwiki_link",
  "action_names":["ask_question","read_wiki_contents","read_wiki_structure"] }
→ 200 {"id":"link_6a677e7f...","auth_type":"NONE","actions":[...]}
```

### 2.6 会话内真实调用(已验证)

建完 link 后同一个提问,SSE 里出现:

```
"recipient": "api_tool.call_tool"     ← 原生工具调用
```

模型发出的调用体:

```json
{"path":"/deepwiki_probe/link_6a677e7f29b88191a6acb9f7256dd1df/read_wiki_structure",
 "args":{"repoName":"openai/openai-python"}}
```

`api_tool` 返回的**真实 MCP 数据**(`role:"tool"`, `content_type:"code"`):

```
{"result":"Available pages for openai/openai-python:\n\n- 1 Overview\n
  - 1.1 Architecture Overview\n  - 1.2 Project Configuration...
```

**调用路径格式:`/<connector_name>/<link_id>/<action_name>`,
参数是 JSON Schema 校验过的结构化 `args`。**

这是完整闭环:我们注册的 server → OpenAI 抓 schema → 模型协议级调用 →
真实数据返回。没有一个环节靠提示词。

探针 connector 已 `DELETE` 清理(200,复查 `actions` 返回
`"Connector not found"` 确认已消失)。

## 3. 这为什么是本质突破

| | exec-harvest(现状) | MCP connector |
|---|---|---|
| 工具怎么来 | 提示词诱导模型用自己的沙箱 | 服务端注册,协议级 |
| 工具名 | 服务端固定(`container.exec`) | **我们自己定义** |
| 参数 | 从 shell 命令文本里 harvest | **JSON Schema 强校验** |
| 多轮 | 靠"像不像拒绝"的正则判定 | 协议自带 |
| 失败模式 | 模型改成叙述 → 静默断链 | 无此路径 |

exec-harvest 全部脆弱性的根因是"工具调用是骗出来的"。MCP 通路让它变成
**真的**,`_looks_like_refusal()` 这类启发式判定从根上不再需要。

## 4. 对 lark-cli 的意义

lark-cli 背后是飞书开放 API —— **本来就是公网线上服务**,满足 MCP 的硬要求
(远程 HTTPS,不支持本地 stdio)。所以路线是:

1. 把 lark-cli 能力包成一个远程 MCP server(Streamable HTTP)
2. `POST /aip/connectors/mcp` 注册
3. `GET /{id}/actions` 确认 schema 被抓到
4. bridge 把 caller 的 tool 声明映射到这些 action

**注意区分**:MCP 要求"OpenAI 服务器能访问到"。飞书 API 满足;
但"在用户本机跑任意 shell 命令"不满足 —— 那部分仍然只能 exec-harvest。
两者不是替代关系,是各管一段。

## 5. 尚未验证(不要当成已完成)

- [x] ~~注册后模型在会话里实际调用~~ → **已验证**,见 §2.6
- [x] ~~`is_consequential: true` 是否需交互授权~~ → **不需要**,
      三个 action 全是 `is_consequential: true`,仍直接调用成功(无授权弹窗)
- [x] ~~`developer_mode` 是否随 session 失效~~ → **账号级且持久**。
      去掉全部一次性头(sentinel / turnstile / echo-logs / trace-id,34→27 个)
      仍返回 `已开启` —— 说明只需身份凭证,**pod 侧自动化可行**
- [x] ~~connector 作用域~~ → **`owners: [{"type":"USER","id":"user-..."}]`**,
      即**每个账号必须各自注册一次**(47 账号 → 47 次注册 + 47 次建 link)。
      好消息:操作幂等、可脚本化、无需人工点击
- [ ] 自建 lark MCP server 尚未实现
- [ ] connector 是账号级 —— 与"每账号一进程"的池模型如何配合

## 6. 方法论教训

三次同形错误(见 `refusal-detection-postmortem.md` §8)之后又犯一次:
**在对话层被拒绝,就断言整个能力不存在。**

正确做法:一个能力被拒绝时,先问"我探的是哪个平面"。
对话层拒绝 ≠ HTTP API 层不存在。这次是用户坚持"本来就是线上服务"才找回来的。

区分信号的硬标准:
- `403` + HTML → 没到 API,**信息量为零**
- `403` + JSON `"X is required"` → **路由存在,是功能门**
- `404` + `"Not Found"` → 路由不存在
- `404` + `"Connector not found"` → 路由存在,把路径段当 ID 解析了
- `422` + `loc` → **已到达目标 handler**,只是 schema 不对(最强的正信号)
