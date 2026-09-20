---
name: codex-multiturn-transport-mechanism
description: |
  codex CLI → ChatGPT 订阅（7 天额度桶）多轮机制的权威判据与速查：逻辑层无状态全量（store:false 写死）、
  encrypted_content 加密推理块（ZDR 设计）、WebSocket 传输层增量（四道闸门 + previous_response_id，
  **增量能跨用户轮**）、426 单向熔断降级 HTTP。当需要：debug codex 客户端流量、自研同款网关/桥
  （zerokey codex bridge、litellm 直转）、判断"历史要不要全量重发 / reasoning 密文块能不能裁 /
  为什么缓存命中低 / 为什么降级成 HTTP"、或与网页版 bpi 网关做机制对比时使用。
  全部结论钉在本地源码 ~/codes/codex（commit 343074d4）file:line + OpenAI 官方文档，不是推理。
---

# codex 多轮机制：无状态全量 × 加密推理块 × WS 增量

> 完整细节（wire 形状 / 状态机 / 算法 / 异常矩阵 / 自研 checklist）在同目录 [reference.md](reference.md)。
> 飞书成文（6 画板开发者版）：https://t83dfrspj4.feishu.cn/docx/N36Gdc741oMG6cxRgaNcFieknlh

## 心智模型：三层各管一段

| 层 | 干什么 | 关键事实 |
|---|---|---|
| **逻辑层** | 每轮请求语义上自包含全量 | `store:false` 写死（client.rs:954）；HTTP 体**没有**任何会话 id 字段（common.rs:252）；历史含每条非系统 item（history.rs:582） |
| **加密层** | 服务端不存对话，思考靠密文块跨轮 | 无条件 `include:["reasoning.encrypted_content"]`（client.rs:927）；客户端只保管不解读；服务端内存解密、不落盘 |
| **传输层** | WS 上增量续发，能省则省 | 四道闸门全过 → 只发 `previous_response_id + 尾部新增 items`；任何存疑退全量 |

**两层解耦是理解一切的钥匙**：传输层怎么省流量，都不改变"每轮都是自包含全量请求"的语义。
HTTP 路径就是逻辑层的裸形态。

## 五条铁律（自研/代理这条链路时不许违反）

1. **reasoning item 原样透传**——不裁剪、不改写、不重排。密文块动一字节 = 服务端解密失败或
   推理断裂，且**丢了不报错**（静默降级，只能效果对比发现）。
2. **`include:["reasoning.encrypted_content"]` 每轮必带**——不带则响应不给密文块，下轮没得回放。
3. **`prompt_cache_key` 会话内稳定**——服务端缓存路由键。官方实测：工具调用场景带推理块
   缓存利用率 40%→80%、SWE-bench +3%；纯聊天无收益。
4. **`x-codex-turn-state` 轮内回带、跨轮丢弃**——响应头下发的 sticky 路由 token，跨轮回带
   违反契约会造成路由 bug（client.rs:269-288 注释明言）。
5. **增量做错宁可不做**——全量永远正确（完整历史在客户端手里）。四道闸门一个不能少。

## 增量四道闸门（client.rs:1244 / 1294）

```
① 上条流正常完成？   oneshot try_recv 拿 {response_id, items_added}；坏流自动拿空 → 全量
② WS 可用？          provider 支持 && 熔断开关没扳过（client.rs:977）
③ 12 字段全等？      model/instructions/tools/tool_choice/parallel_tool_calls/reasoning/
                     store/stream/include/service_tier/prompt_cache_key/text
                     穷尽解构逐一比对；input 和 stream_options/client_metadata 显式豁免（client.rs:309）
④ 前缀逐项全等？     prev_len = last_request.input.len() + items_added.len()
                     input.split_at_checked(prev_len) → (前缀, 增量尾)
                     前缀 vs (上轮input ⧺ 上轮响应新增) 逐项比对，先==再剥内部metadata重比（client.rs:364）
全过 → 发 previous_response_id + 增量尾（零新增也合法）；任一不过 → 同连接全量
```

## ⭐ 增量跨用户轮的机制（最容易搞错）

WS 会话（连接 + last_request + 回执通道）在**轮结束 Drop 时存回**会话级缓存
`ModelClientState.cached_websocket_session`（client.rs:1182），**下一用户轮 new_session take 走**
（client.rs:503）。新轮 input = 旧全历史 + 新用户消息 → 前缀匹配依然成立 → 增量跨轮成立。
唯一不跨轮的是 sticky token（OnceLock 每轮新建）。

## 异常速查（详表见 reference.md §6）

| 触发 | 处理 | 熔断？ |
|---|---|---|
| 建连收 **HTTP 426** | `force_http_fallback`：进程级**单向**熔断，此后一律 HTTP；同请求内无缝续走 | ✅ 唯一熔断点（1675 单一生产位） |
| 建连 401 | 刷 token 后 loop 重试 | ❌ |
| 建连 **Timeout** | `reset_websocket_session` 全清 + 错误上抛，由轮级重试接住（client.rs:1411-1414） | ❌ |
| 连接闲断 | 重建连接 + 清增量状态，本请求全量 | ❌ |
| 属性变/前缀不符/坏流 | 同连接全量，下轮恢复资格 | ❌ |
| 流中途报错（WS/HTTP 同） | **轮级重试循环**：从完整历史重建 prompt 全量重发（session/turn.rs:1363，预算 stream_max_retries=5）；ContextWindowExceeded / UsageLimitReached **不重试**直接上抛 | — |

观测信号：熔断打点 `codex.transport.fallback_to_http` + warn "falling back to HTTP"；
增量放弃 trace "incremental request failed / properties didn't match / items didn't match"。

## 曾翻过的车（判据纪律）

- ❌ "加密是为了省传输" —— 密文同样耗 token。加密是为 **store:false/ZDR 下的推理连续性**。
- ❌ "codex 逻辑无状态 = 每轮全量重发不省传输" —— 逻辑层语义与传输层实发是**两个独立问题**；
  WS 层做增量。机制层结论必须钉到 file:line，不许从"哲学"推"实现"。

## 接入参数（自研直连要用）

- 端点 `https://chatgpt.com/backend-api/codex/responses`（doctor.rs:3799）
- WS 握手头 `OpenAI-Beta: responses_websockets=2026-02-06`（client.rs:158）
- 身份：OAuth Bearer + `ChatGPT-Account-ID` 头（model-provider/auth.rs:106）
- **请求体 zstd 压缩**：ChatGPT 登录（uses_codex_backend）+ openai provider 时请求体走
  Compression::Zstd（client.rs:1434-1440；feature `enable_request_compression` 默认开启，
  features/lib.rs:1096）——7 天份额路径传输 = WS 增量 + zstd **双重压缩**
- prewarm：`response.create` + `generate=false`，等 Completed；失败也算 WS 尝试会触发降级
- 默认 provider 归属已坐实：`built_in_model_providers` 里 `"openai"` 就是
  `create_openai_provider`（model-provider-info/lib.rs:502-506）

## 边界

- 线上真实 WS 增量命中率**未实测**（源码静态分析给不出，需抓真实流量）。
- 服务端连接上下文的存储方式是从客户端契约反推。
- 行号钉 commit 343074d4，漂移后用函数名检索：`get_incremental_items` /
  `responses_request_properties_match` / `force_http_fallback` / `prepare_websocket_request`。
- 已做二次证伪式复核（2026-08-22）：9 条承重结论全站住 + 补 zstd 遗漏 + 2 处修正，
  复核过程与方法见 [reference.md](reference.md) §11。

## 关联

- 对照系统：网页版 bpi 网关（skill `zk-cursor-web-fc-iterate`；机制文档
  https://t83dfrspj4.feishu.cn/docx/Sgn1d87Hqo7ayQxmrIhc3588n9u）——同骨架
  （前缀校验→增量→全量地板），差异根源=谁拥有状态。
- memory: `project_codex_multiturn_mechanism_doc_2026_08_22`
- usage 上报纪律（同族教训）：`feedback_gateway_usage_must_report_client_context_not_sent_prompt`
