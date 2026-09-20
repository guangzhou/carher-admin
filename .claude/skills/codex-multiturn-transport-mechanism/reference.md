# codex 多轮机制完整参考（开发者级）

> 证据基线：本地开源仓库 `~/codes/codex`，commit `343074d4`（2026-08-22 逐条 grep 复核）；
> OpenAI 官方文档（developers.openai.com reasoning 指南 + Cookbook reasoning_items，198 直连抓取）。
> 行号会随上游漂移，函数名/字段名是更稳的检索键。
> 飞书成文（含 6 张画板）：https://t83dfrspj4.feishu.cn/docx/N36Gdc741oMG6cxRgaNcFieknlh

## 0. 链路定位与接入参数

对象：**codex CLI → ChatGPT 订阅账号（7 天额度桶）** 官方链路 = 源码 `create_openai_provider`
（`requires_openai_auth: true`，model-provider-info/src/lib.rs:418）。

| 参数 | 值（证据） |
|---|---|
| 端点 | `https://chatgpt.com/backend-api/codex/responses`（cli/doctor.rs:3799；agent-identity/lib.rs:61 列为 Production） |
| 传输 | 默认 Responses over WebSocket；HTTP+SSE 作地板（lib.rs:418 `supports_websockets: true`） |
| provider 归属 | `built_in_model_providers` 里 `"openai"` = `create_openai_provider`（lib.rs:502-506）——ChatGPT 登录即走此 provider |
| WS 握手头 | `OpenAI-Beta: responses_websockets=2026-02-06`（client.rs:158、1169） |
| 身份 | OAuth Bearer + `ChatGPT-Account-ID` 头（model-provider/src/auth.rs:106） |
| **请求体压缩** | ChatGPT 登录（uses_codex_backend）+ openai provider → `Compression::Zstd`（client.rs:1434-1440）；feature `enable_request_compression` **默认开启**（features/src/lib.rs:1096，Stage::Stable）。传输 = WS 增量 + zstd 双重压缩 |
| sticky 路由 | 响应头 `x-codex-turn-state`，轮内回带、跨轮禁止（client.rs:146、269-288） |
| HTTP 重试预算默认 | 请求重试 4 / 流中断重试 5 / 流空闲超时 300s（lib.rs:27-29，config.toml 可覆盖） |

## 1. 逻辑层：每轮自包含全量

- `store: false` **无条件写死**于请求构造（client.rs:954），非配置项。
- `input[]` = 完整历史：用户消息、助手消息、reasoning 块、工具调用/结果，一条不少
  （history.rs:582 注释："API messages include every non-system item"）。
- HTTP 请求体 `ResponsesApiRequest`（codex-api/src/common.rs:252）**结构上不存在**任何
  会话 id 字段——想有状态都没有语法。

### HTTP 体 14 字段（common.rs:252-275）

| 字段 | codex 取值 | 增量比对 | 备注 |
|---|---|---|---|
| `model` | 模型 slug | ✅ | |
| `instructions` | 系统指令 | ✅ | 空串不序列化 |
| `input` | 完整历史 item 数组 | 单独处理（前缀切分） | |
| `tools` | 工具集 | ✅ | |
| `tool_choice` | `"auto"` 写死 | ✅ | client.rs:951 |
| `parallel_tool_calls` | bool | ✅ | |
| `reasoning` | effort+summary | ✅ | |
| `store` | **false 写死** | ✅ | client.rs:954 |
| `stream` | **true 写死** | ✅ | |
| `stream_options` | 流选项 | ❌ 显式豁免 | 只影响本次交付（client.rs:357 注释） |
| `include` | `["reasoning.encrypted_content"]` 写死 | ✅ | client.rs:927 |
| `service_tier` | 档位 | ✅ | |
| `prompt_cache_key` | 缓存路由键 | ✅ | 会话内必须稳定 |
| `text` | 文本控制 | ✅ | |
| `client_metadata` | 客户端元数据 | ❌ 显式豁免 | |

### item id 处理

- 发送前：非 codex 前缀的 item id 清掉（`prepare_response_items_for_request`，client.rs:966-971）。
- WS 发送时摘走 original_item_ids、发完还原（client.rs:1757-1762）。

## 2. 加密推理块（encrypted_content）

**问题**：推理模型每轮产生思维链；网页版存服务端，codex 的服务端被 store:false 关掉记忆。
**解法**：服务端用只有 OpenAI 持有的密钥把推理**加密成密文块**返还；客户端当普通历史 item 保管
（读不懂解不开），下轮随全量历史原样带回；服务端**内存解密**、续接思路、用完即弃、不落盘。

item 形状：`{"type":"reasoning", "encrypted_content":"gAAAA...", "summary":[...]}`

**官方口径**（reasoning 指南 + Cookbook reasoning_items）：
- 专为 **store:false / 零数据保留（ZDR）** 客户端设计；明文返还思维链 = 暴露未对齐审查的原始推理。
- 收益（官方实测，未独立复现）：工具调用场景带推理块 **SWE-bench +3%、缓存利用率 40%→80%**；
  纯聊天无收益——推理块只在两次工具调用之间承载思路。

**落地三铁律**：
1. 绝不裁剪/改写 reasoning item——动一字节解密失败或推理断裂。
2. `include` 每轮必带，否则响应不含密文块，下轮无法回放。
3. 密文块丢失**不报错**——请求依然合法，静默失去推理连续性（收益归零），
   日志无任何信号，只能效果对比发现。

## 3. 状态机：三层生命周期

```
会话级 ModelClientState（进程会话存续期）           client.rs:216-218
 ├─ cached_websocket_session（跨轮接力棒）
 │    = WS 连接 + last_request + last_response 回执通道
 └─ disable_websockets 熔断开关（AtomicBool，单向）

     ↓ 轮开始 new_session() std::mem::take 取走     client.rs:503/515
     ↑ 轮结束 impl Drop 存回                          client.rs:1182

轮级 ModelClientSession（每用户轮新建）
 ├─ 接过来的 WebsocketSession
 └─ turn_state: OnceLock<String>（sticky token，每轮新建 → 唯一不跨轮的东西）

请求级（一轮内模型↔工具往返 N 次）
 ├─ 发完无条件存 last_request                        client.rs:1763
 └─ 挂 last_response oneshot 通道；流正常完成才兑现 {response_id, items_added}
```

**三个推论（都有代码背书）**：
1. **增量跨用户轮成立**：轮 N Drop 存回 → 轮 N+1 take 走，连接和 last_request 都在；
   新轮 input = 旧全历史 + 新用户消息 → 前缀匹配成立。调用链坐实：每轮
   `new_session()`（session/turn.rs:164，启动预热的 session 经 `prewarmed_client_session`
   传给首轮；compact 请求同样走此机制 compact.rs:260）。
2. **sticky token 不跨轮**：注释明言跨轮回带"违反客户端/服务端契约，会造成路由 bug"
   （client.rs:272-274）。
3. **坏流自动禁增量**：response_id 经 oneshot 在流完成时才兑现；上条流半途而废 →
   `try_recv` 拿空 → 全量（client.rs:1284-1292）。无需显式"标记上轮失败"。

## 4. 传输层：WS 帧、预热、增量算法

### WS 帧形状

`ResponseCreateWsRequest`（common.rs:302）= HTTP 14 字段 + 2 个 WS 专属字段：
- `previous_response_id: Option<String>` —— 增量锚点
- `generate: Option<bool>` —— 预热标记

HTTP 体→WS 帧默认转换 previous_response_id=None = 全量帧（common.rs:277-297）。

### zstd 请求压缩（二次校验时补上的遗漏）

ChatGPT 登录（`CodexAuth::uses_codex_backend`）+ openai provider 时，请求体走
`Compression::Zstd`（`responses_request_compression`，client.rs:1434-1440）。
开关是 feature `enable_request_compression`，**默认开启**（features/src/lib.rs:1096，
Stage::Stable）。所以 7 天额度桶路径的传输是 **WS 增量 + zstd 双重压缩**：
增量砍掉重复历史，zstd 再压掉剩余载荷的冗余。API-key 直连（非 codex backend）不压缩。

### 预热（prewarm）

- 轮执行前尽力而为发 `response.create` + `generate=false`：只建连不推理，
  等 Completed 事件才继续（client.rs:15-19 模块注释；等待逻辑 1856-1864）。
- 不算推理请求、不进 rollout trace；但**算本轮第一次 WS 尝试**——失败同样触发降级（client.rs:23）。
- 预热响应 id 可当 previous_response_id 用；此时 trace 补记逻辑全量（client.rs:1704-1708）。
- `preconnect_websocket`（client.rs:1325+）只建连不发 payload。

### 增量算法 7 步（get_incremental_items client.rs:1244；prepare_websocket_request 1294）

| 步 | 动作 | 失败即全量的原因 |
|---|---|---|
| 1 | `try_recv` 上条流回执 `{response_id, items_added}` | 拿不到 = 上条流没跑完/失败 |
| 2 | 12 字段 properties 与 last_request 全等（client.rs:309，**穷尽解构**——新增字段编译期强制决定是否比对） | 换模型/改工具/调 reasoning 都算变 |
| 3 | `prev_len = last_request.input.len() + items_added.len()`（checked_add） | 溢出防御 |
| 4 | `input.split_at_checked(prev_len)` → (前缀, 增量尾) | 本轮更短（历史被压缩/编辑）切不动 |
| 5 | 前缀 vs `last_request.input ⧺ items_added` **逐项**比对；先 `==`，不等再剥内部 chat metadata 透传字段重比（client.rs:364） | 任何一项不等 = 历史被改 |
| 6 | response_id 为空 → 放弃（client.rs:1311） | 没锚点 |
| 7 | 发 `previous_response_id + 增量尾`；**零新增合法**（allow_empty_delta，重试场景） | — |

发完（增量或全量帧）**无条件**存 last_request（client.rs:1763）——下个请求永远有比对基准。

## 5. 时序（一次工具调用轮 + 跨轮）

```
用户轮 N：
  prewarm: response.create generate=false ——— 等 Completed
  R1(全量): instructions + 完整历史 + tools + store:false
    ← SSE: 密文推理块 + function_call（response_id=A；响应头下发 x-codex-turn-state）
  本地执行工具，历史追加 call + output
  R2(增量): previous_response_id=A + 只带工具输出（回带 turn-state；密文块不重发，服务端从连接上下文拼回）
    ← SSE: 最终回答（response_id=B）
  轮结束：Drop 存回缓存（连接 + last_request）

用户轮 N+1：
  new_session 接过缓存（sticky token 丢弃，其余保留）
  R3(增量·跨轮): previous_response_id=B + 只带新用户消息
```

R2 是增量收益最大处——历史越长省得越多，工具调用恰是历史暴涨场景。
对上官方"缓存 40%→80% 只在工具调用场景兑现"的口径。

## 6. 异常处理矩阵

| 异常 | 处理（代码点） | 恢复 | 观测信号 |
|---|---|---|---|
| **服务端拒 WS**（建连收 HTTP 426 Upgrade Required） | FallbackToHttp（client.rs:1671-1676，**唯一生产位**）→ `force_http_fallback`：熔断 swap true + 清缓存会话（532-553）；**同请求内**无缝续走 HTTP（1915-1921） | **不恢复**：本进程此后一律 HTTP 全量 | 计数器 `codex.transport.fallback_to_http`；warn "falling back to HTTP" |
| **建连 401** | is_recoverable_auth_error → 刷 token → loop 重试（1677-1692） | 刷新成功续 WS，**不熔断** | auth recovery telemetry |
| **建连 Timeout** | `reset_websocket_session` 全清增量状态 + 错误上抛（1411-1414），由轮级重试接住 | 重试轮全量，**不熔断** | ApiError::Transport(Timeout) |
| **连接闲断/被掐** | 取连接时 `is_closed()` → needs_new：清 last_request/回执通道，重建（1387-1394） | 本请求全量，之后自动回增量；**不熔断** | trace 日志 |
| **上条流半途而废** | oneshot 未兑现 → try_recv 空 → 闸门①不过（1284） | 本请求全量 | trace "incremental request failed" |
| **属性变更/前缀不符**（换模型、历史被压缩/编辑） | 闸门③/④不过 → 同连接全量（1251、1263） | 本请求全量，下轮恢复资格 | trace "properties didn't match" / "items didn't match" |
| **预热失败** | 算第一次 WS 尝试，按建连异常处理（23、1866-1869） | 426→熔断；其余→重试 | 同建连异常 |
| **流中途报错**（WS/HTTP 同一套） | **轮级重试循环**：从完整历史重建 prompt 全量重发（session/turn.rs:1363+，预算 stream_max_retries=5）；**不可重试**：ContextWindowExceeded、UsageLimitReached 直接上抛 | 重试轮全量（无状态天然支持） | retry telemetry |
| **密文推理块丢失** | **不报错**，服务端续不上思路 | 该段推理不可恢复；后续轮正常 | **无信号**（危险） |
| **审计/重放** | rollout trace 永远记**逻辑层全量**，非 WS 压缩帧（1704-1708 注释） | — | trace 文件 |

哲学：**增量是优化，全量是地板**。误判为延续概率为零（逐项全等做不了假），
漏判代价只是多发一次全量。完整历史始终在客户端手里——任何状态丢失都不可能丢上下文。

## 7. 与网页版 bpi 网关对比

| | codex（7 天额度桶） | 网页网关 bpi（zero-cursor-bpi:8201） |
|---|---|---|
| 状态归属 | 客户端持全量；服务端只在连接内记上轮 | 服务端持久对话树；网关只存指纹 |
| 增量锚点 | previous_response_id（连接级；**跨用户轮有效**——缓存接力） | conversation_id + parent_message_id（跨连接持久） |
| 防错校验 | 12 字段全等 + 前缀逐项结构比对 | 逐 item SHA-1 指纹前缀全等 |
| 跨轮推理 | 加密块随历史回放 | 服务端本来就记得 |
| 兜底方向 | 比对失败→同连接全量；426→单向熔断 HTTP | 失效→删缓存+全量新会话，可反复恢复 |
| 会话寿命 | 一条 WS 连接 | TTL 240 分钟 |

同骨架（前缀校验→增量→全量地板）；差异根源唯一变量 = **谁拥有状态**。

## 8. 自研同款 checklist

**必须（正确性）**：
1. 每轮全量回放历史，reasoning item 原样透传
2. `store:false` + `include:["reasoning.encrypted_content"]` 每轮带
3. `prompt_cache_key` 会话内稳定
4. 非本系统前缀 item id 发送前摘除
5. 轮内回带 `x-codex-turn-state`、跨轮丢弃
6. usage 按客户端真实上下文口径上报（低报会废掉客户端原生压缩——
   memory `feedback_gateway_usage_must_report_client_context_not_sent_prompt`）

**选做（传输优化，做错宁可不做）**：
7. 四道闸门一个不能少；"上条流成功才兑现锚点"用回执通道天然实现，别用布尔标记
8. 发完无条件存 last_request
9. 逐项全等比对，禁止"长度相同就算匹配"之类近似
10. 426 熔断做成进程级单向开关，不做自动回切（上游拒 WS 有部署原因，反复探测是骚扰）
11. 断连重建只清增量状态、不熔断

## 9. 诚实边界

1. 线上真实 WS 增量命中率**未实测**（源码静态分析给不出，需抓真实 codex 客户端流量）。
2. 服务端"连接上下文存储上轮请求"是从客户端协议契约反推，服务端实现不可见。
3. 3% / 40→80% 为 OpenAI 自报口径。
4. 行号钉 commit 343074d4；漂移后检索函数名：`get_incremental_items`、
   `responses_request_properties_match`、`force_http_fallback`、`prepare_websocket_request`、
   `response_items_equal_ignoring_internal_metadata`。

## 10. 判据纪律（这轮翻过的两次车）

- ❌ "加密是为了省传输" → 密文同样耗 token；加密为 **ZDR 下推理连续性**。
- ❌ "逻辑无状态 = 每轮全量重发不省传输" → 逻辑语义与传输实发是两个独立问题，WS 层做增量。

教训：机制层结论必须钉到 file:line；"哲学"推不出"实现"；
"逻辑层"与"传输层"必须分开问、分开证。

## 11. 二次证伪式复核记录（2026-08-22）

对增量/加密/异常处理的全部承重结论回源码找反例，结果：**9 条全站住 + 1 遗漏 + 2 修正**
（本文各节已按此修订，此处留档复核过程本身）。

**站住的 9 条**：
1. 7 天桶路径 = 内置 openai provider，WS 支持成立——且补齐了此前缺的证据链环节：
   `built_in_model_providers` 里 `"openai"` 就是 `create_openai_provider`
   （lib.rs:502-506）。此前只从 requires_openai_auth 反推，现在是直接证据。
2. `include:["reasoning.encrypted_content"]` 无条件（读 905-935 整段上下文，无 if 包裹）。
3. `store:false` / 14 字段无会话 id / tool_choice:"auto" 写死。
4. 增量四道闸门与 7 步算法。
5. 增量跨用户轮：调用链完整（new_session 每轮 turn.rs:164 → Drop 存回 client.rs:1182）。
6. 426 是 FallbackToHttp **唯一生产位**（client.rs:1675 单点 return，全文件 grep 确认）。
7. 401 刷 token 重试不熔断（1677-1692）。
8. 断连 is_closed → 重建 + 清增量状态，不熔断（1387-1394）。
9. sticky token 不跨轮（OnceLock 每轮新建 + 269-288 契约注释）。

**1 处遗漏（已补 §0/§4）**：zstd 请求压缩——WS 增量 + zstd 双重压缩，feature 默认开启。

**2 处修正（已改 §0/§6）**：
- 账号头生产写法 `ChatGPT-Account-ID`（model-provider/auth.rs:106），原引测试代码小写形式。
- 异常矩阵细化：建连 **Timeout** → reset_websocket_session 全清 + 上抛、不熔断（1411-1414）；
  **流中途报错**（WS/HTTP 同一套）→ 轮级重试循环从完整历史全量重发
  （session/turn.rs:1363+，预算 stream_max_retries=5），
  ContextWindowExceeded / UsageLimitReached **不重试**直接上抛。

**复核方法**（可复用）：对每条断言问三件事——① 这行代码有没有条件包裹（读上下文段而非单行 grep）；
② 这个函数的调用方是谁、是不是测试代码（grep caller 并排除 tests）；
③ "唯一/无条件/永远"这类强词有没有第二生产位（全文件 grep 构造子/return 位点）。
本轮的 provider 归属和 zstd 都是靠 ② 挖出来的。
