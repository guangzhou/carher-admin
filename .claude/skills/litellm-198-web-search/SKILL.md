---
name: litellm-198-web-search
description: >-
  198 LiteLLM 上「联网搜索到底生不生效」的总图：每个客户端（Cursor / Codex CLI / Codex Desktop /
  Claude Code / multica）怎么发搜索、落到哪个后端（kiro / grok sub2api / wangsu / DeepSeek 官方）、
  谁真搜谁假装搜，以及一条命令的回归 `scripts/litellm-198-web-search-regress.py`。
  Use when 用户问「搜索生效了吗」「为什么 opus 落到 wangsu/grok」「模型说搜过但答的是旧信息」
  「X 模型能不能联网」，或改动 kiro.rs 搜索回合 / grok_web_search_preview_rename /
  chat_annotation_nest callback 之后验收。her 实例 / 杭州 bot 的 perplexity 名搜索走
  hermestest-web-search-provider（§先决定改哪一层、§7 杭州）。
---

# 198 联网搜索：谁真搜、谁假装搜

作用域：198，ns `litellm-product`。改兜底链看 [[litellm-198-fallback-chain]]，
kiro.rs 构建上线看 [[kiro-rs-ops]] §C。

## 0. 先跑回归，别凭印象答

```bash
scp scripts/litellm-198-web-search-regress.py cltx@10.68.13.198:~/wsr/
ssh cltx@10.68.13.198 'python3 ~/wsr/litellm-198-web-search-regress.py; rm -rf ~/wsr'
```

7 例：kiro opus/sonnet Messages 混合工具、Cursor 搜索子请求、Codex Desktop Responses 混合、
Claude Code 子请求、grok 直选 `web_search_preview`、阴性对照。10-15 基线 **ALL PASS**。

## 1. 判「真搜了」只认协议级证据，三家字段不一样

| 后端 | 证据在哪 |
|---|---|
| Anthropic 形（kiro / wangsu） | `server_tool_use` + `web_search_tool_result` 块；`usage.server_tool_use.web_search_requests` |
| OpenAI Responses 形 | `output[]` 里的 `web_search_call` 项 |
| xAI（grok via sub2api） | `usage.num_server_side_tools_used`、`server_side_tool_usage_details.web_search_calls` |

🔴 **只查一种字段会判错。** 10-15 我只看了 Anthropic 那个字段，就把 grok-4.7 真搜的 10 条
（每条 3~8 次 `web_search_calls`）报成「没搜」，又编了一个「grok-4.7 不走 sub2api」的原因。
其实那 10 条全是 `sa/grok-4.7`，callback 日志在 4 个 pod 上都记了 `renamed`。
SpendLogs 查法：`response->'usage'->>'num_server_side_tools_used'`。

🔴 **200 + 回答里带链接 ≠ 搜了。** 下面这些都会 200，回答用「搜索结果」的口吻，还附 nobelprize.org
之类的通用链接：grok 走 Messages、x.ai 收到 `web_search_preview`、DeepSeek 官方 Responses。
🔴 **题目必须是训练截止之后的事实**，并且同时打一个不带搜索工具的阴性对照
（[[feedback_search_probe_needs_post_cutoff_fact]]）。

## 2. 每个客户端怎么发、落到哪（10-15 实测）

| 客户端 | 发什么 | 现在的结果 |
|---|---|---|
| Codex CLI / multica-agent-sdk | Messages，函数工具 + `web_search_20250305` 同一个请求 | kiro 搜索回合 ✅ |
| Codex Desktop | Responses，函数工具 + `{"type":"web_search"}` | kiro 搜索回合 ✅（LiteLLM 转成 Messages 给 kiro） |
| Cursor（选 opus/sonnet） | 主请求只有函数工具；WebSearch 单独发一条 Responses「替我搜」+ `web_search_preview` | kiro 搜索回合 ✅ |
| Cursor（选 grok） | 同上，落 sub2api | callback 改名后 x.ai 真搜 ✅ |
| Claude Code | `Perform a web search for the query: …`，只带一个 web_search 工具 | kiro 老路径（原样返回结果列表）✅ |

## 3. 各后端的搜索能力

- **kiro（opus-5 / 5.5、sonnet-5.5 等 kiro-claude-*）**：Kiro 服务端 MCP `web_search`
  （`https://q.us-east-1.amazonaws.com/mcp`），约 1.5s、10 条结果。闲置号 10 次后 10min 用量仍 0.0
  ⇒ **大概率不扣 credit**（缺阳性对照）。混合工具靠 kiro.rs 补丁 0002 的搜索回合，
  见 `k8s/kiro-rs/patches/README.md`。
- **grok（sub2api 池，x.ai 原生搜索）**：只有 **Responses + `{"type":"web_search"}`** 会搜。
  `web_search_preview` 会被**静默忽略**，现由 callback `grok_web_search_preview_rename` 改名
  （只认 api_base 含 `sub2api` ⇒ 用户定的边界，**grok 只走 sub2api，别扩 gate**）。
  走 **Messages 时 grok-4.20 / grok-build 不搜**，回答还自称已联网。
  `grok-4.20-multi-agent` 带任何函数工具都 400 `Client-side tools for multi-agent models require beta access`
  ⇒ **不能当兜底**（10-14 试过，那一跳 0.5s 必挂）。
  但它是 **chat 入口、不带任何工具也会自己搜** 的唯一 grok 组 ⇒ her 实例 / 杭州 bot 的 perplexity 名都映射到它
  （见 [[hermestest-web-search-provider]]）。10-21 实测：单次 9~35s、10~27 次搜索、10~45 万 prompt token、$0.04~0.57。
  🔴 它走 chat→Responses 桥回来的 `annotations` 是**平铺**的（`{type,url,title,start_index,end_index}`），
  严格按 OpenAI 规范解析的客户端（杭州 gw litellm 1.101.0）会 500；10-21 起 callback `chat_annotation_nest`
  在 198 出口补嵌套 `url_citation:{…}`（加法，平铺字段保留）。
- **wangsu5 claude-opus-5**：原生搜索，单发 `web_search_requests=2` 证实过，约 $0.5/条。
  线上 235 条兜底的 response 里看不到搜索块，是截断还是确实没搜，**没定论**。
- **DeepSeek 官方**（10-17 开放搜索，10-20 用 `scripts/litellm-198-deepseek-search-probe.sh` 直连复测）：
  - ✅ `deepseek-v4-pro` + Responses `tools:[{"type":"web_search"}]`：真搜（`output[]` 有 `web_search_call`），约 4s。
  - ⛔ `deepseek-flash`（= v4.1-flash，也就是我们在用的）：Responses / chat `web_search_options` / Anthropic 端点**全部忽略搜索**，
    只会答「尚未公布」。官方文档也写了 web_search 仅 v4-pro。**flash 想联网只能靠外部搜索工具喂结果**。
  - ⚠️ Anthropic 端点 + `web_search_20250305`（pro）：同一天一轮结果全是 `web_search_tool_result_error: unavailable`、
    一轮正常 10 条 ⇒ 不稳。`usage.server_tool_use.web_search_requests>0` **不等于搜到**，要数 `web_search_result` 条数。
  - ✅ 独立接口 `POST /v1/tools/search`（0.6s，结果带 date/summary）、`/v1/tools/fetch`（转 Markdown），各 $0.75/千次。
  - litellm 桥 `openai/responses/deepseek-v4-pro` + `web_search_options:{}` 从 chat 入口也能搜（12s）。
  - 198 上 pro 档 09-09 起全员撤权；10-20 用户再次确认 carher-14 只开 `deepseek-v4.1-flash`、不开 pro。
  - 10-15 那次「DeepSeek 不搜」测的是 `deepseek-v4.1-flash-responses`（flash），结论对 flash 仍然成立。
  - 客户端侧官方接法：OpenClaw 的 DeepSeek 搜索插件见 [[hermestest-web-search-provider]] §6（截至 10-20 未发版）。

## 4. 兜底链（DB `router_settings.fallbacks`，不在 CM）

`kiro-claude-opus-5` 和 `kiro-claude-opus-5-5` 现在都是 `sa-grok-4.20 → wangsu5`
（opus-5 原来直连 wangsu5，10-15 05:34 UTC 应用户要求改成和 5.5 一样，备份 `198:~/fb-opus5-grok-20261015/rs-pre.json`；
5.5 在 10-14 改成 multi-agent 后又回滚过）。
搜索回合上线后，带搜索工具的请求基本不触发兜底。还会触发的是 kiro 上游 `500 MODEL_TEMPORARILY_UNAVAILABLE`
（high load，9 次重试耗尽），这时 opus-5 / 5.5 的用户实际拿到的是 grok-4.20 的回答（走 Messages，不搜），用户看不出来。

判「kiro 接住了」：响应头 `x-litellm-attempted-fallbacks: 0`，并且 SpendLogs `model_id` 以 `kiro-rs/` 开头。

🔴 `sa-grok-4.20-multi-agent → deepseek-v4.1-flash`（全局）：grok 池 503 时搜索请求会被 flash 接住，
**200、不带引用、答「尚未公布」**，用户看不出来没搜。判据：SpendLogs `model_group=deepseek-v4.1-flash`
且请求名是 perplexity 名。要不要改成「搜不了就报错」10-21 还没定。

## 5. 未做 / 已知缺口

- kiro 搜索回合是**整轮缓冲**：带搜索工具的请求首字延迟等于整轮耗时（简单问题 6~30s，多次搜索 90s+，期间 25s 发一次 ping）。
  下一步是最后一轮逐字流式（[[kiro-rs-ops]] 搜索回合一节）。
- DeepSeek flash 不搜（见 §3）；要联网就得在客户端侧接搜索工具，或用 v4-pro。
- MCP 扣不扣 credit 缺阳性对照：在闲置号上发一条普通对话，等 10min 看读数涨不涨。
- grok multi-agent 搜索依赖 sub2api 池：10-21 两次塌到 1 条腿（14:50 / 18:14），14 和杭州同时 503 或落 flash。
  池子薄时先 `sub2api-grok-onboard.py health` 看真腿数（从 Mac 跑设 `S2A_REMOTE_DIR=<临时目录>`，否则会覆盖 198 cron 副本）。
- hermestest-14 插件有 30s 硬超时，multi-agent 重查询常超 30s ⇒ 198 success、客户端超时。要改得动实例配置，用户 10-14 定过不改 pod。
