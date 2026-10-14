# kiro.rs 本地补丁

上游 `hank9999/kiro.rs`，构建树在 `188:/Data/kiro-build/kiro.rs/`（基于 `5ca5703`）。
线上镜像 = 上游 + 下面补丁按序打上。

| 补丁 | 内容 | 镜像 |
|---|---|---|
| `0001-carher-base-models.patch` | opus-5.5 / sonnet-5.5 映射、透传模型表、`/v1/models` 条目（09-29 / 10-04） | `master-sonnet55` |
| `0002-websearch-server-tool-loop.patch` | 服务端 web_search 搜索回合（10-15） | `master-websearch-loop` |

## 0002 做了什么

请求带 Anthropic 服务端搜索工具（`web_search_20250305` 等）时，Kiro 原样转发会
400 `Invalid tool use format`，LiteLLM 静默兜底到 grok / wangsu（grok 走 Messages 不搜还假装搜过）。

- `converter.rs`：服务端搜索工具改写成带 `{query}` schema 的普通函数工具；
  历史里客户端回显的 `server_tool_use` / `web_search_tool_result` 拍平成文本（srvtoolu id 不进 Kiro 历史）。
- `search_loop.rs`（新）：整轮缓冲的搜索回合。模型调 web_search ⇒ Kiro MCP 搜 ⇒ tool_result 交回 ⇒ 续调，
  最多 max_uses（缺省 5、硬上限 10）次搜索 / 8 轮。客户端只看到原生 `server_tool_use` + `web_search_tool_result`。
  流式先发 message_start，等待期间 25s ping，最后一次性发完。第一轮上游错误仍以 HTTP 状态码返回（LiteLLM 照常兜底）。
- `websearch.rs`：老路径（Claude Code 的 `Perform a web search for the query:` 子请求）保留；
  修 `stream=false` 也回 SSE 的 bug（改回 JSON）。
- `handlers.rs`：`/v1/messages` 与 `/cc/v1/messages` 都先判搜索回合，再判老路径。

验证：cargo test 239 = 224+15 新增通过，失败集合与基线同 8 条；隔离容器 E2E（真 Kiro 上游）9 例全过；
生产经 LiteLLM 4 例 `attempted-fallbacks: 0`。

## 打补丁 / 构建

```bash
cd /Data/kiro-build && cp -a kiro.rs kiro-new && cd kiro-new
patch -p1 < 0002-websearch-server-tool-loop.patch   # 路径前缀是 kiro.rs/ → kiro-ws/，用 -p1
docker build -t kiro-rs:master-<tag> .
```

上线流程见 skill `kiro-rs-ops` §C。
