# `cursor-g-5.6-pro` 全池空回的根因：`stream_handoff`（2026-09-01）

## 现象

用户面菜单里 `cursor-g-5.6-pro` **点了没反应**。六条 lane 一律
HTTP 200 + `output_tokens=0`，和当初退役 `xhigh` 时一模一样的"假装成功"形态。

## 三段式

**假设**：pro 的真身 `gpt-5-6-pro` 用的是 `stream_handoff` —— 主 SSE 流在
handoff 事件处就收口，正文在另一条通道上继续生成，而我们的读流器只消费第一条流。

**证伪条件**：
- (a) 上游根本不认这个 slug（模型下线）→ `/backend-api/models` 里不该有它；
- (b) 正文其实在主流里、是我们丢了 → 主流里应该能翻出 assistant 文本；
- (c) 压根没生成 → 会话里不该出现完成的 assistant 消息。

**数据**（都在 lane 82 的 pod 里用真登录态打的）：

- (a) **证伪**。`/backend-api/models` HTTP 200，`gpt-5-6-pro` 在列，
  title `GPT-5.6 Pro`，`max_tokens` 410000。
- (b) **证伪**。同一段代码、只换 model 的对照：

  | model | 事件数 | 关键事件 |
  |---|---|---|
  | `gpt-5-6`（对照） | 21 | `"v,c"`×8 有正文增量，收在 `message_stream_complete` |
  | `gpt-5-6-pro`（嫌疑） | 12 | ev9 = `stream_handoff`，**没有** `message_stream_complete`，2.4s 就收口 |

  `stream_handoff` 事件体：

  ```json
  {"type":"stream_handoff","conversation_id":"…","turn_exchange_id":"…",
   "options":[{"type":"resume_sse_endpoint","topic_id":"conversation-turn-…"},
              {"type":"subscribe_ws_topic","topic_id":"conversation-turn-…"}]}
  ```

- (c) **证伪**。handoff 之后轮询 `/backend-api/conversation/<id>`：
  t=14s / 21s 还是 `tool:in_progress`，t=28s 变成
  `assistant:finished_successfully`，正文正是探针暗号 `PROBEOK`。

**结论**：**不是账号、不是 slug 下线**，是 pro 走了「延迟/交接式」出流形状，
我们的重放没跟。这与 `raw.js` 里记的 `-wm` conduit-handoff 是同一家族。

## 两条修法（未动，等拍板）

**A. 跟着 `resume_sse_endpoint` 续流。** 卡住了：7 种 URL/参数组合全被拒——
`GET /backend-api/conversation/resume` 对 `topic_id`、`topic`、
`topic_id+conversation_id+resume_conversation_token`、`resume_token`、`token`、
`topic_id+resume_token`、`conversation_id+topic_id` 一律 400
`{"detail":"Invalid conversation resume"}`；`POST` 405；
`/backend-api/f/conversation/resume` 405；`/backend-api/conversation/<id>/resume` 404。
要走这条得先反编译网页客户端把参数形状挖出来。

**B. 看到 `stream_handoff` 就转轮询。** 已实测可行：轮询
`/backend-api/conversation/<id>` 到 assistant 消息 `finished_successfully`
再交付正文，一个平凡 prompt 28s 出字。

**B 的隔离性是构造性的**：这条分支只有在流里出现 `stream_handoff` 事件时才可能进入，
而对照实测 sol / luna / instant / 5.5 **从不产生**该事件 —— 所以门①（干净载荷）
与门②（功能不回退）在构造上不受影响。代价是 pro 档天生慢（28s 级），
要给它单独的超时预算。

**C. 照 `xhigh` 的先例退役这一档**，菜单里去掉。

改 CM 得走六步 + 临时真 key 回归，且要用户点头，所以**目前没动**。

## 复现要点

在 lane 82 pod 内用真登录态发 `/backend-api/f/conversation`，读流必须用真签名：

```js
const { readSSE } = require('/app/utils/sse-reader')
readSSE(body, { isDone: () => done, onDone: fin,
                onError: (e) => { … }, onData: (d) => { … } })
```

（`readSSE(body, cb)` 这种写法会报 `onError is not a function`，那不是上游的错。）
