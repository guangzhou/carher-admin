// Responses API route for zerokey-serve.
//
// Accepts POST /v1/responses (OpenAI Responses API format), converts to
// internal chatgptApi.chatCompletion(), and returns in Responses API format.
// When tools[] is present and CODEX_TOKEN_DIR is configured, forwards to the
// Codex endpoint (gpt-5.5) via round-robin OAuth tokens for native tool_call.

const crypto = require('crypto')
const { readSSE } = require('../utils/sse-reader')
const { acquireSlot } = require('../utils/rate-limiter')
const { resolveModel } = require('./raw')
const { codexRequest, hasTokens } = require('./codex-pool')
// 容错 require：本 CM 被 26 个 zero pod 共用，但把 bpi-codex.js 从 /patch cp 到
// /app/routes 的那行是**逐个 Deployment 的启动参数**。只更新 CM 而没改某个 pod 的
// 启动参数时，那个 pod 一旦因别的原因重启就会加载到这份新代码却找不到模块 ——
// 硬 require 会让它直接起不来（今天已经在 litellm 回调上踩过同款）。
// 找不到就退化成"永不编译"，行为与改动前完全一致。
let compileToExec = () => ({ js: null, blocks: [], leftover: null })
let prepareCodexInput = (items) => items
let needsEscalation = () => false
let firstAsk = () => null
let extractSpawns = () => []
let ESCALATE = ''
let escalatePrompt = null
let stripCanvas = (t) => t
try {
  ;({ compileToExec, prepareCodexInput, needsEscalation, firstAsk, ESCALATE,
      escalatePrompt, makeCitationFilter, stripCitations, stripCanvas,
      extractSpawns, CITE_FREE_HINT } = require('./bpi-codex'))
} catch (e) {
  console.warn('[bpi] bpi-codex.js not mounted, BPI compilation disabled:', e.message)
}
const { normalizeToolDefs, buildToolInstructions, extractToolCalls, newCallId, detectShellTool, execCommandFromData, execToToolCall, estimateTokens } = require('./web-tools')

// ── Input parsing ──────────────────────────────────────────────

function textOfContent(content) {
  if (content == null) return ''
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content
      .map((p) => {
        if (typeof p === 'string') return p
        if (p && p.type === 'input_text') return p.text || ''
        if (p && p.type === 'output_text') return p.text || ''
        if (p && p.text) return p.text
        return ''
      })
      .join('')
  }
  return String(content)
}

function flattenInput(input, instructions) {
  const parts = []
  if (instructions) {
    parts.push(`SYSTEM: ${instructions}`)
  }
  if (typeof input === 'string') {
    parts.push(`USER: ${input}`)
  } else if (Array.isArray(input)) {
    for (const item of input) {
      if (typeof item === 'string') {
        parts.push(`USER: ${item}`)
        continue
      }
      const role = String(item.role || 'user').toUpperCase()
      const text = textOfContent(item.content)
      parts.push(`${role}: ${text}`)
    }
  }
  return parts.join('\n\n')
}

// ── Response builder ────────────────────────────────────────────

function buildResponsesRoute(chatgptApi) {
  const express = require('express')
  const router = express.Router()

  router.post('/', async (req, res) => {
    const { input, instructions, stream = false } = req.body
    if (input == null || (typeof input === 'string' && !input) ||
        (Array.isArray(input) && input.length === 0)) {
      return res.status(400).json({
        error: { message: 'input is required', type: 'invalid_request_error' },
      })
    }

    // ── Codex path: tools present → forward to Codex endpoint via OAuth pool ──
    if (Array.isArray(req.body.tools) && req.body.tools.length > 0 && hasTokens()) {
      return handleCodex(req, res)
    }

    // ── Web tool-injection path: tools present but NO Codex tokens (web-only
    //    pod). Fall back to sub2api-style prompt-injection so agentic traffic
    //    still gets tool_calls instead of silently degrading to plain chat. ──
    const _webToolDefs =
      Array.isArray(req.body.tools) && req.body.tools.length > 0
        ? normalizeToolDefs(req.body.tools)
        : []
    const _validNames = new Set(_webToolDefs.map((d) => d.name))
    const useWebTools = _webToolDefs.length > 0 && !hasTokens()

    const model = resolveModel(req.body.model)
    // Codex responses-lite 载荷：剥掉它写给真 codex 后端的 developer 指令，
    // 末尾补一句"本轮你连着用户这台机器"。非 Codex 载荷原样返回。
    // 实测（真实 82KB 载荷，n>=3）：原样 1/3 出工具、只交手 2/3、
    // 去指令+交手 5/6 且零拒答。
    const codexInput = prepareCodexInput(input)
    const codexLite = codexInput !== input   // prepareCodexInput 对非 Codex 载荷返回同一引用
    // 整段对话此前是否已经跑过工具（有 custom_tool_call_output / function_call_output）。
    // 假完成判据要用它：没跑过工具却说"已创建/测试通过" = 编的。
    const hadToolResult = Array.isArray(input) && input.some(
      (it) => it && (it.type === 'custom_tool_call_output' || it.type === 'function_call_output'))
    let bpiRetried = false
    const basePrompt = flattenInput(codexInput, instructions)
    let prompt = basePrompt

    // exec-harvest: shell-like caller tool → let the web model use its own
    // code-interpreter and re-emit its container.exec command as the caller's
    // shell tool_call. Otherwise fall back to JSON-envelope injection.
    const shellTool = useWebTools ? detectShellTool(_webToolDefs) : null
    let harvestedCmd = null
    if (useWebTools && shellTool) {
      prompt = `Use your shell to accomplish the task below. Prefer a single shell command.\n\n${basePrompt}`
    } else if (useWebTools) {
      const toolInstr = buildToolInstructions(_webToolDefs, req.body.tool_choice)
      prompt = `${toolInstr}\n\n${basePrompt}`
    }

    // ── 先开流，再去等上游 ────────────────────────────────────────
    // 2026-08-08 实测：从客户端发出到收到第一个事件，短请求 3.55s、长材料 5.53s。
    // 原因是 acquireSlot(排队等号) + chatCompletion(建网页会话、可能还要上传附件)
    // **全部 await 完**才写第一个字节。而 `response.created` 里没有一个字段依赖
    // 上游（id 本地生成），完全可以立刻发。改后 0.17s。
    //
    // ⚠️ 别把这个当成"UI 白屏被治好了"。读源码证伪过：`response.created` 在客户端
    //    是**空操作**(core/src/session/turn.rs:2251)，也不计入首 token
    //    (turn_timing.rs:388)；转圈是本地 TurnStarted 在发请求**之前**就点亮的
    //    (core/src/tasks/regular.rs:49)。所以这一改的实际收益是**传输层**的：
    //    头早发出、连接早建立，中间的代理/CDN 不会因为迟迟没有字节而掐断，
    //    也让下面的进度通道有机会在等待期播报。想改界面文案得靠 progress()。
    const respId = 'resp_' + crypto.randomBytes(12).toString('hex')
    const msgId = 'msg_' + crypto.randomBytes(12).toString('hex')
    const created = Math.floor(Date.now() / 1000)
    const mdl = req.body.model || model || 'chatgpt-web'

    if (stream) {
      res.setHeader('Content-Type', 'text/event-stream')
      res.setHeader('Cache-Control', 'no-cache')
      res.setHeader('Connection', 'keep-alive')
      res.setHeader('Access-Control-Allow-Origin', '*')
      if (res.flushHeaders) res.flushHeaders()
      res.write(`event: response.created\ndata: ${JSON.stringify({
        type: 'response.created',
        response: { id: respId, object: 'response', created_at: created,
                    status: 'in_progress', model: mdl, output: [], usage: null },
      })}\n\n`)
      res.write(`event: response.in_progress\ndata: ${JSON.stringify({
        type: 'response.in_progress',
        response: { id: respId, object: 'response', created_at: created,
                    status: 'in_progress', model: mdl, output: [], usage: null },
      })}\n\n`)
    }

    // ── 把等待期变成"有进度的等待" ────────────────────────────────
    // 读 Codex 源码确认的三件事：
    //  1. `response.created` 在客户端是**空操作**(core/src/session/turn.rs:2251
    //     `ResponseEvent::Created => {}`)，也不计入首 token(turn_timing.rs:388)。
    //     转圈是本地 TurnStarted 点亮的(core/src/tasks/regular.rs:49)，跟服务端无关。
    //     —— 所以想改界面，得靠别的通道。
    //  2. 唯一能改状态行文案的是 `response.reasoning_summary_text.delta`
    //     (codex-api/src/sse/responses.rs:358)，**必须同时带 delta 和 summary_index**，
    //     缺一个整条事件被丢弃。
    //  3. TUI 用 `extract_first_bold` 从文本里抠 `**...**` 当状态行
    //     (tui/src/chatwidget/streaming.rs:244-248)，没有加粗就一直显示 "Working"。
    // zerokey 本来一条推理都不产出，这个通道完全空着，正好拿来播报网关状态。
    //
    // ⚠️ delta 前必须先有 active reasoning item，否则 debug 版直接 panic
    //    (turn.rs:2613 `error_or_panic("ReasoningSummaryDelta without active item")`)。
    //    所以顺序是 output_item.added(reasoning) -> delta -> output_item.done。
    //    正文 message 因此排到 output_index 1。
    // **默认关闭，只给排障用**（`ZK_PROGRESS=1` 打开）。
    // 2026-08-08 上线当天用户就撞上了：他在做正经事（"写到飞书文档"），状态行却
    // 挂着「正在排队等号」——那是**运维视角的调试话**，不该出现在用户界面上。
    // 而且更糟的是状态行会**一直停留在最后一句**（TUI 只在有新的加粗标题时才换，
    // tui/src/chatwidget/streaming.rs:244-268），于是"正在排队等号"会一直挂着读秒，
    // 比什么都不显示还差。Codex 自带的 "Working" 反而更得体。
    const SHOW_PROGRESS = stream && !useWebTools && process.env.ZK_PROGRESS === '1'
    const rsnId = 'rs_' + crypto.randomBytes(12).toString('hex')
    let rsnOpen = false
    let progressSteps = 0
    function progress(bold, detail) {
      if (!SHOW_PROGRESS) return
      try {
        if (!rsnOpen) {
          rsnOpen = true
          res.write(`event: response.output_item.added\ndata: ${JSON.stringify({
            type: 'response.output_item.added', output_index: 0,
            item: { type: 'reasoning', id: rsnId, summary: [], content: [] },
          })}\n\n`)
          res.write(`event: response.reasoning_summary_part.added\ndata: ${JSON.stringify({
            type: 'response.reasoning_summary_part.added', item_id: rsnId,
            output_index: 0, summary_index: 0, part: { type: 'summary_text', text: '' },
          })}\n\n`)
        }
        progressSteps += 1
        res.write(`event: response.reasoning_summary_text.delta\ndata: ${JSON.stringify({
          type: 'response.reasoning_summary_text.delta', item_id: rsnId,
          output_index: 0, summary_index: 0,
          delta: `**${bold}**${detail ? '\n' + detail : ''}\n`,
        })}\n\n`)
      } catch (_) { /* 进度是锦上添花，绝不能因此把主流程搞挂 */ }
    }
    function progressClose() {
      if (!rsnOpen) return 0
      rsnOpen = false
      try {
        res.write(`event: response.output_item.done\ndata: ${JSON.stringify({
          type: 'response.output_item.done', output_index: 0,
          item: { type: 'reasoning', id: rsnId, summary: [], content: [] },
        })}\n\n`)
      } catch (_) { /* 同上 */ }
      return 1
    }

    // 排队等一个空闲账号 —— 池子忙的时候这一步就能等好几秒，得让用户看到
    progress('Preparing', null)
    await acquireSlot('ChatGPT')

    const INLINE_MAX = parseInt(process.env.ZK_INLINE_MAX || '100000', 10)
    let sendPrompt = prompt
    let attachments = null
    if (prompt.length > INLINE_MAX) {
      // 3MB 级别的上传能花十几秒，这是最需要播报的一段
      progress('Reading context', null)
      try {
        const buf = Buffer.from(prompt, 'utf8')
        const up = await chatgptApi.uploadFile(buf, {
          fileName: `conversation-${Date.now()}.txt`,
          mimeType: 'text/plain',
          useCase: 'my_files',
        })
        attachments = [{ id: up.id, size: up.size, name: up.name, mimeType: up.mimeType }]
        // (进度点已移除：上传完成不值得单独报一条)
        sendPrompt =
          'The full conversation/context is in the attached text file ' +
          `(${up.name}). Read it and respond to the latest request in it.` +
          // 网页版一引用文件就吐私有区引用标记，而且流经常**断在标记中间**
          // （实测 status=completed 却只有起始符没有结束符）。从源头少产生：
          // A/B n=16，截断 9/16 -> 2/16，三标记全中 1/16 -> 16/16。
          (CITE_FREE_HINT ? '\n' + CITE_FREE_HINT : '')
        console.log(`[responses] long prompt ${prompt.length} chars → uploaded as ${up.id}`)
      } catch (e) {
        console.log(`[responses] file upload failed, falling back to inline: ${e.message}`)
      }
    }

    progress('Thinking', null)
    let upstream
    try {
      upstream = await chatgptApi.chatCompletion(sendPrompt, null, 'client-created-root', model, attachments)
    } catch (e) {
      // 流已经开了（头早已发出），不能再改状态码 —— 只能在流里报错并收尾，
      // 否则客户端会拿到一个"半开着又突然断掉"的连接。
      if (stream) {
        progressClose()
        // ⚠️ 必须用 `response.failed`，不能用 `{type:'error'}`。
        // 读 Codex 源码确认（codex-api/src/sse/responses.rs:330-472）：客户端只认
        // 12 个事件 type，**里面没有 `error`** —— 未知 type 走 `_ =>` 分支被静默
        // 忽略(:470)。然后流一关，客户端判定"stream closed before
        // response.completed"(:519-523) → 这是**可重试**错误 → 把整个请求重发一遍。
        // 我们的载荷动辄十几万字符，白白再打一趟。
        // 另外 error 必须挂在 `response` 对象里，客户端是 `event.response.get("error")`
        // 才读得到(:393)。code 也别乱填：`context_length_exceeded` /
        // `insufficient_quota` / `server_is_overloaded` / `slow_down` 都是
        // **不重试**的语义(:628-647)，上游只是抽风的话不该用它们。
        res.write(`event: response.failed\ndata: ${JSON.stringify({
          type: 'response.failed',
          response: {
            id: respId, object: 'response', created_at: created, status: 'failed',
            model: mdl, output: [], usage: null,
            error: { code: 'upstream_error', message: e.message },
          },
        })}\n\n`)
        res.write('data: [DONE]\n\n')
        return res.end()
      }
      return res.status(502).json({ error: { message: e.message, type: 'upstream_error' } })
    }

    let full = ''
    let started = false
    let finished = false

    if (stream) {
      // 头和 response.created / in_progress 已经在上游调用之前发过了（见上方）。
      // 严格消费者要求 {type, response:{...}} 这个形状：LiteLLM 的 responses
      // 流式记账 keys off data.type，缺 type 会导致 spend log 静默不写。
      // Web-tools: we cannot stream raw text (it may be a JSON tool envelope) —
      // buffer everything and emit the parsed result at finish(). Skip the
      // message-shell preamble; the item type isn't known until parse time.
      if (!useWebTools) {
        // 进度用的 reasoning item 必须在正文 message 开始**之前**关掉 ——
        // 客户端只维护一个 active_item(turn.rs:2363-2439)，两个同时开着会让
        // 后面的 output_text.delta 挂到错的 item 上。
        // （output_index 客户端根本不读：它的 SSE 结构体里没声明这个字段，
        //   codex-api/src/sse/responses.rs:163-178，全库解析层 0 次引用。
        //   所以正文不用改成 index 1，只要保证先关再开。）
        progressClose()
        // output_item.added
        const msgShell = {
          type: 'message', id: msgId, role: 'assistant', content: [], status: 'in_progress',
        }
        res.write(`event: response.output_item.added\ndata: ${JSON.stringify({
          type: 'response.output_item.added', item: msgShell, output_index: 0,
        })}\n\n`)

        // content_part.added
        res.write(`event: response.content_part.added\ndata: ${JSON.stringify({
          type: 'response.content_part.added', item_id: msgId,
          output_index: 0, content_index: 0, part: { type: 'output_text', text: '' },
        })}\n\n`)
      }
    }

    // ── Web-tools progressive streaming ──────────────────────────────
    // Previously a web-tools turn buffered EVERYTHING and emitted only at
    // finish(): measured 26.5s of blank screen, then the whole answer in 1.9s.
    // The reason for buffering is real (the text may be a JSON tool envelope and
    // the item type is unknown until parsed), but it is decidable early: the
    // envelope instructions demand "ONLY this block and nothing before or after
    // it", so a reply that begins with prose is an answer, not a call.
    //
    // Two-stage safety, because extractToolCalls also supports `leadingText`
    // (an envelope AFTER some prose):
    //   1. buffer DECIDE_AT chars, then commit to streaming only if the text
    //      cannot be an envelope start;
    //   2. keep watching: the instant envelope markers appear, FREEZE (emit no
    //      more deltas) and let finish() decide. Anything already streamed is
    //      exactly the leadingText, so no output is lost or duplicated.
    const DECIDE_AT = 80
    const HOLD_TAIL = 24
    let wtDecided = false     // have we committed to streaming?
    // 账号级 agent 契约生效后，模型吐的是 ⟦…⟧ 块而不是散文。一旦认出来就停止
    // 往外吐 text delta —— 否则客户端会先看到一段块文本、再收到工具调用。
    // 与下面 wtFrozen 是同一个套路，只是判据不同（这条不依赖 tools 是否存在，
    // 因为 Codex responses-lite 的顶层 tools 恒为空）。
    let bpiFrozen = false
    // codexLite 会话全程缓冲正文（见 onText 注释）
    const bpiBuffered = codexLite && stream
    let wtFrozen = false      // envelope spotted -> stop streaming
    let wtSent = 0            // chars of `full` already streamed
    let wtOpened = false      // message shell emitted?

    const looksLikeEnvelope = (txt) => {
      const h = txt.replace(/^[\s\uFEFF]+/, '')
      if (h.startsWith('```') || h.startsWith('{') || h.startsWith('[')) return true
      return txt.indexOf('tool_calls') !== -1
    }

    const wtOpen = () => {
      if (wtOpened) return
      wtOpened = true
      const msgShell = {
        type: 'message', id: msgId, role: 'assistant', content: [], status: 'in_progress',
      }
      res.write(`event: response.output_item.added\ndata: ${JSON.stringify({
        type: 'response.output_item.added', item: msgShell, output_index: 0,
      })}\n\n`)
      res.write(`event: response.content_part.added\ndata: ${JSON.stringify({
        type: 'response.content_part.added', item_id: msgId,
        output_index: 0, content_index: 0, part: { type: 'output_text', text: '' },
      })}\n\n`)
    }

    const wtPump = (isFinal) => {
      if (!stream || wtFrozen) return
      if (!wtDecided) {
        if (!isFinal && full.length < DECIDE_AT) return
        if (looksLikeEnvelope(full)) { wtFrozen = true; return }
        wtDecided = true
      } else if (looksLikeEnvelope(full.slice(wtSent))) {
        // Envelope started mid-answer: freeze; finish() emits the calls and
        // treats what we streamed as leadingText.
        wtFrozen = true
        return
      }
      const upto = isFinal ? full.length : Math.max(0, full.length - HOLD_TAIL)
      if (upto <= wtSent) return
      const chunk = full.slice(wtSent, upto)
      wtSent = upto
      wtOpen()
      res.write(`event: response.output_text.delta\ndata: ${JSON.stringify({
        type: 'response.output_text.delta', item_id: msgId,
        output_index: 0, content_index: 0, delta: chunk,
      })}\n\n`)
    }

    // 引用标记会**跨分片**到达（'file' 一片、'cite\ue202turn0' 又一片），
    // 所以整条流共用一个剥离器实例，由它缓冲；逐片正则会把标记切两半各漏一截。
    const citeFilter = makeCitationFilter ? makeCitationFilter() : null

    const onText = (t) => {
      if (!t) return
      full += t
      started = true
      if (useWebTools) { wtPump(false); return }
      if (!stream) return
      // codexLite（真 Codex agent 会话）：**全程缓冲，不边流边发**。
      // 2026-08-09 用户 /init 现场：模型先说"已完成：AGENTS.md 已写入…"再吐 ⟦write⟧ 块。
      // 旧判据只认"块在开头"，那段散文已经流给用户了、块又被编译执行 ——
      // 用户看到"已完成"但活还在做，下一轮再来一遍，连着 4~5 次重复宣告，
      // 还把 "BPI(write) 返回 {}" 这种内部叙述漏了出去。
      // 账号契约规定"要么只输出块、要么只答话，绝不混"，流完之前判断不了是哪种，
      // 那就别猜 —— 缓冲到 finish() 再定。普通聊天(非 codexLite)不受影响，照旧逐片流。
      if (bpiBuffered) return
      if (bpiFrozen || full.trimStart().startsWith('\u27E6')) { bpiFrozen = true; return }
      const delta = citeFilter ? citeFilter.push(t) : t
      if (!delta) return          // 整片都是标记内容，这一片不发
      res.write(`event: response.output_text.delta\ndata: ${JSON.stringify({
        type: 'response.output_text.delta', item_id: msgId,
        output_index: 0, content_index: 0, delta,
      })}\n\n`)
    }

    // One-shot re-send for the web-tools retry: collect full text, no streaming.
    async function collectWebTextR(promptText) {
      let up
      try {
        up = await chatgptApi.chatCompletion(promptText, null, 'client-created-root', model, null)
      } catch (e) {
        return ''
      }
      let acc = ''
      let done = false
      await readSSE(up, {
        onData: (d) => {
          if (!d || done) return
          if (d.p === '/message/content/parts/0' && d.o === 'append') return (acc += d.v || '')
          if (typeof d.v === 'string' && !d.o && !d.p) return (acc += d.v)
          if (d.o === 'patch' && Array.isArray(d.v)) {
            for (const op of d.v) {
              if (op.p === '/message/content/parts/0' && op.o === 'append') acc += op.v || ''
              if (op.p === '/message/status' && op.o === 'replace' && op.v === 'finished_successfully') done = true
            }
          }
          if (d.type === 'message_stream_complete') done = true
        },
        onDone: () => { done = true },
        onError: () => { done = true },
        isDone: () => done,
      })
      return acc
    }

    // 统一的"发一组 output item"出口，避免流式/非流式两份重复代码。
    function emitItems(res, ctx) {
      const { stream, respId, created, mdl, usage, items } = ctx
      if (stream) {
        items.forEach((item, idx) => {
          res.write(`event: response.output_item.added\ndata: ${JSON.stringify({
            type: 'response.output_item.added', item, output_index: idx,
          })}\n\n`)
          res.write(`event: response.output_item.done\ndata: ${JSON.stringify({
            type: 'response.output_item.done', item, output_index: idx,
          })}\n\n`)
        })
        res.write(`event: response.completed\ndata: ${JSON.stringify({
          type: 'response.completed',
          response: { id: respId, object: 'response', created_at: created,
                      status: 'completed', model: mdl, output: items, usage },
        })}\n\n`)
        res.end()
      } else {
        res.json({ id: respId, object: 'response', created_at: created,
                   status: 'completed', model: mdl, output: items, usage })
      }
    }

    const finish = () => {
      if (finished) return
      finished = true

      // 剥掉网页版引用标记（\ue200filecite\ue202turn0file0\ue201 这类）。
      // 在这里统一剥一次，下游全部受益：BPI 编译、拒答检测、最终 output text。
      // 流式那条已经逐片剥过了，这里剥的是 `full`（用于 output/编译），
      // 两者不冲突 —— 客户端看到的和 output 里记的因此一致。
      if (stripCitations) {
        const before = full.length
        full = stripCitations(full)
        // 画布围栏（网页版文档功能）也剥掉，别把 ":::writing{...}" 漏给用户。
        // 注意只剥围栏、保留正文 —— 内容本身是用户要的东西。
        if (stripCanvas) full = stripCanvas(full)
        // 未闭合的残段（"末尾只剩 filecite"那种）在 flush 里丢弃并计数
        const tail = citeFilter ? citeFilter.flush() : { truncated: false, dropped: 0 }
        if (before !== full.length || tail.truncated) {
          console.log(`[cite] 剥离 ${before - full.length} 字符`
            + (tail.truncated ? `，上游断在标记中间(丢弃 ${tail.dropped})` : ''))
        }
      }

      // Estimate usage — the web backend returns none, so without this LiteLLM
      // bills 0 (esp. streaming). input from the sent prompt, output from the
      // model's text/command (small; input dominates on agentic requests).
      const mkUsage = (out) => {
        const i = estimateTokens(prompt)
        const o = estimateTokens(out)
        return {
          input_tokens: i, output_tokens: o, total_tokens: i + o,
          input_token_details: { cached_tokens: 0 },
          output_token_details: { reasoning_tokens: 0 },
        }
      }
      const usage = mkUsage(full)

      // ── BPI -> Codex exec：账号级契约生效后模型吐 ⟦…⟧ 块，编译成
      //    custom_tool_call(exec)。没开契约的号不会吐这种块，compileToExec
      //    返回 js=null，下面原样走普通文本路径，行为完全不变。 ──
      // ── 拒答升级重试（一次）──
      // 上线实测：契约+剥指令+交手之后仍有约 1/3 轮次不动手，其中一半是
      // "我没有权限/终端"。这类回复对客户端毫无价值，重发一次比原样返回强。
      // 与 web-tools.js 里 JSON 那条路的 escalate 是同一思路。
      if (codexLite && !bpiRetried && needsEscalation(full, hadToolResult)) {
        bpiRetried = true
        console.log('[bpi] refusal/false-complete detected -> escalated retry')
        // 只带"最后一条用户消息 + 交手 + 升级指令"，不要把 107KB 全history 再发一遍
        const esc = escalatePrompt
          ? escalatePrompt(codexInput, ESCALATE)
          : `${basePrompt}\n\n${ESCALATE}`
        console.log(`[bpi] escalate prompt ${esc.length} chars (was ${basePrompt.length})`)
        collectWebTextR(esc)
          .then((t2) => { if (t2 && t2.trim()) full = t2; finished = false; finish() })
          .catch(() => { finished = false; finish() })
        return
      }

      // ⟦ask⟧ -> Codex 原生 request_user_input（否则整块会当文本漏给用户看）
      const ask = firstAsk(full)
      if (ask && !compileToExec(full).js) {
        const fc = {
          type: 'function_call', id: 'fc_' + crypto.randomBytes(12).toString('hex'),
          call_id: 'call_' + crypto.randomBytes(12).toString('hex'),
          name: ask.name, arguments: JSON.stringify(ask.arguments), status: 'completed',
        }
        console.log('[bpi] ask -> request_user_input')
        return emitItems(res, { stream, respId, created, mdl, usage, items: [fc] })
      }

      // ⟦spawn⟧ -> Codex 原生 spawn_agent（agents 层，架构与 ask 完全对称）。
      // 编排在客户端：Codex 自己 fork 上下文、跑子线程、把结果回灌 —— 我们零编排。
      // 与 exec 互斥判据同 ask：同轮既有 spawn 又有可执行块时，exec 优先
      //（模型自己能干的活不该转包，spawn 只在"独立子任务"时有意义）。
      const spawns = extractSpawns(full)
      if (spawns.length && !compileToExec(full).js) {
        const items = spawns.map((s) => ({
          type: 'function_call', id: 'fc_' + crypto.randomBytes(12).toString('hex'),
          call_id: 'call_' + crypto.randomBytes(12).toString('hex'),
          name: s.name, arguments: JSON.stringify(s.arguments), status: 'completed',
        }))
        console.log(`[bpi] ${items.length} spawn(s) -> spawn_agent`)
        return emitItems(res, { stream, respId, created, mdl, usage, items })
      }

      const bpi = compileToExec(full)
      if (bpi.js) {
        const callId = 'call_' + crypto.randomBytes(12).toString('hex')
        const item = {
          type: 'custom_tool_call', id: 'ctc_' + crypto.randomBytes(12).toString('hex'),
          call_id: callId, name: 'exec', input: bpi.js, status: 'completed',
        }
        console.log(`[bpi] compiled ${bpi.blocks.length} block(s) -> exec`)
        if (stream) {
          res.write(`event: response.output_item.added\ndata: ${JSON.stringify({
            type: 'response.output_item.added', item, output_index: 0,
          })}\n\n`)
          res.write(`event: response.output_item.done\ndata: ${JSON.stringify({
            type: 'response.output_item.done', item, output_index: 0,
          })}\n\n`)
          res.write(`event: response.completed\ndata: ${JSON.stringify({
            type: 'response.completed',
            response: { id: respId, object: 'response', created_at: created,
                        status: 'completed', model: mdl, output: [item], usage },
          })}\n\n`)
          res.end()
        } else {
          res.json({ id: respId, object: 'response', created_at: created,
                     status: 'completed', model: mdl, output: [item], usage })
        }
        return
      }

      // ── exec-harvest: re-emit the model's container.exec command as the
      //    caller's shell function_call. ──
      if (shellTool && harvestedCmd) {
        const tc = execToToolCall(shellTool, harvestedCmd)
        const parsed = { calls: [{ name: tc.name, arguments: tc.arguments }], leadingText: '' }
        return finishWebTools(res, { stream, respId, msgId, created, mdl, usage: mkUsage(JSON.stringify(tc.arguments)), full: '', parsed, wt: { sent: wtSent, opened: wtOpened } })
      }

      // ── Web-tools branch: parse the buffered text into function_call items ──
      if (useWebTools) {
        const parsed = extractToolCalls(full, _validNames)
        if (parsed && parsed.calls.length) {
          return finishWebTools(res, { stream, respId, msgId, created, mdl, usage, full, parsed, wt: { sent: wtSent, opened: wtOpened } })
        }
        // No envelope on the first pass → one escalated retry before falling
        // back to plain text (fixes accounts whose harness refuses softly).
        const escalated = `${buildToolInstructions(_webToolDefs, 'required', true)}\n\n${basePrompt}`
        collectWebTextR(escalated)
          .then((text2) => {
            const p2 = extractToolCalls(text2, _validNames)
            finishWebTools(res, {
              stream, respId, msgId, created, mdl, usage,
              full: full || text2 || '', parsed: p2 && p2.calls.length ? p2 : null,
              wt: { sent: wtSent, opened: wtOpened },
            })
          })
          .catch(() =>
            finishWebTools(res, { stream, respId, msgId, created, mdl, usage, full: full || '', parsed: null, wt: { sent: wtSent, opened: wtOpened } }),
          )
        return
      }

      if (stream) {
        // codexLite 全程缓冲的，最后一次性把正文作为一个 delta 补发出去
        // （不发 delta 的话客户端只能靠 done 事件拿全文，渲染时机会怪）。
        if (bpiBuffered && full) {
          // full 在 finish() 开头已经过 stripCitations，直接发
          res.write(`event: response.output_text.delta\ndata: ${JSON.stringify({
            type: 'response.output_text.delta', item_id: msgId,
            output_index: 0, content_index: 0, delta: full,
          })}\n\n`)
        }
        // output_text.done
        res.write(`event: response.output_text.done\ndata: ${JSON.stringify({
          type: 'response.output_text.done', item_id: msgId,
          output_index: 0, content_index: 0, text: full,
        })}\n\n`)

        // content_part.done
        res.write(`event: response.content_part.done\ndata: ${JSON.stringify({
          type: 'response.content_part.done', item_id: msgId,
          output_index: 0, content_index: 0, part: { type: 'output_text', text: full },
        })}\n\n`)

        // output_item.done
        const doneMsg = {
          type: 'message', id: msgId, role: 'assistant',
          content: [{ type: 'output_text', text: full }], status: 'completed',
        }
        res.write(`event: response.output_item.done\ndata: ${JSON.stringify({
          type: 'response.output_item.done', item: doneMsg, output_index: 0,
        })}\n\n`)

        // response.completed — MUST carry `type` and wrap the response object
        // in `response:{...}` (OpenAI spec). LiteLLM's responses streaming
        // logger only fires spend-log accounting when it parses a chunk whose
        // data.type == "response.completed"; the previous bare payload had no
        // `type`, so plain-text completions were never billed/logged (only the
        // finishWebTools path, which already used this shape, logged).
        res.write(`event: response.completed\ndata: ${JSON.stringify({
          type: 'response.completed',
          response: {
            id: respId, object: 'response', created_at: created, status: 'completed',
            model: mdl, output: [doneMsg], usage,
          },
        })}\n\n`)
        res.end()
      } else {
        res.json({
          id: respId, object: 'response', created_at: created, status: 'completed',
          model: mdl,
          output: [{
            type: 'message', id: msgId, role: 'assistant',
            content: [{ type: 'output_text', text: full }], status: 'completed',
          }],
          usage,
        })
      }
    }

    await readSSE(upstream, {
      onData: (d) => {
        if (!d || finished) return
        // exec-harvest: capture the model's first container.exec command and finish.
        if (shellTool && !harvestedCmd) {
          const cmd = execCommandFromData(d)
          if (cmd) { harvestedCmd = cmd; return finish() }
        }
        if (d.p === '/message/content/parts/0' && d.o === 'append') return onText(d.v)
        if (typeof d.v === 'string' && !d.o && !d.p) return onText(d.v)
        if (d.o === 'patch' && Array.isArray(d.v)) {
          for (const op of d.v) {
            if (finished) break
            if (op.p === '/message/content/parts/0' && op.o === 'append') onText(op.v)
            if (op.p === '/message/status' && op.o === 'replace' && op.v === 'finished_successfully') finish()
          }
        }
        if (d.type === 'message_stream_complete') finish()
      },
      onDone: finish,
      onError: (err) => {
        if (finished) return
        finished = true
        if (stream) {
          res.write(`event: error\ndata: ${JSON.stringify({ error: { message: err.message } })}\n\n`)
          res.end()
        } else {
          res.status(502).json({ error: { message: err.message, type: 'upstream_error' } })
        }
      },
      isDone: () => finished,
    })
  })

  // ── Web tool-injection finish: emit Responses function_call items (or a
  //    plain message when the model chose to answer directly). ──
  function finishWebTools(res, ctx) {
    const { stream, respId, msgId, created, mdl, usage, full, parsed, wt } = ctx
    const wtSentLen = (wt && wt.sent) || 0
    const wtWasOpen = !!(wt && wt.opened)
    const output = []

    if (parsed && parsed.calls.length) {
      if (parsed.leadingText) {
        output.push({
          type: 'message', id: msgId, role: 'assistant',
          content: [{ type: 'output_text', text: parsed.leadingText }], status: 'completed',
        })
      }
      for (const c of parsed.calls) {
        output.push({
          type: 'function_call',
          id: 'fc_' + crypto.randomBytes(12).toString('hex'),
          call_id: newCallId(c.name),
          name: c.name,
          arguments: typeof c.arguments === 'string' ? c.arguments : JSON.stringify(c.arguments),
          status: 'completed',
        })
      }
    } else {
      // No envelope → plain answer.
      output.push({
        type: 'message', id: msgId, role: 'assistant',
        content: [{ type: 'output_text', text: full }], status: 'completed',
      })
    }

    // Reconcile with whatever was already streamed live. Without this the
    // client would receive the answer twice: once as deltas, once as a fresh
    // message item here.
    if (stream && wtWasOpen) {
      const first = output[0]
      if (first && first.type === 'message') {
        const whole = first.content[0].text || ''
        const rest = whole.length > wtSentLen ? whole.slice(wtSentLen) : ''
        if (rest) {
          // flush the held-back tail as one final delta
          res.write(`event: response.output_text.delta\ndata: ${JSON.stringify({
            type: 'response.output_text.delta', item_id: msgId,
            output_index: 0, content_index: 0, delta: rest,
          })}\n\n`)
        }
        res.write(`event: response.output_text.done\ndata: ${JSON.stringify({
          type: 'response.output_text.done', item_id: msgId,
          output_index: 0, content_index: 0, text: whole,
        })}\n\n`)
        res.write(`event: response.content_part.done\ndata: ${JSON.stringify({
          type: 'response.content_part.done', item_id: msgId, output_index: 0,
          content_index: 0, part: { type: 'output_text', text: whole },
        })}\n\n`)
        res.write(`event: response.output_item.done\ndata: ${JSON.stringify({
          type: 'response.output_item.done', output_index: 0,
          item: { ...first, content: [{ type: 'output_text', text: whole }] },
        })}\n\n`)
        output.shift()          // already delivered; don't re-emit below
      }
    }

    if (stream) {
      let idx = wtWasOpen ? 1 : 0
      for (const item of output) {
        res.write(`event: response.output_item.added\ndata: ${JSON.stringify({
          type: 'response.output_item.added', item, output_index: idx,
        })}\n\n`)
        if (item.type === 'function_call') {
          res.write(`event: response.function_call_arguments.delta\ndata: ${JSON.stringify({
            type: 'response.function_call_arguments.delta', item_id: item.id,
            output_index: idx, delta: item.arguments,
          })}\n\n`)
          res.write(`event: response.function_call_arguments.done\ndata: ${JSON.stringify({
            type: 'response.function_call_arguments.done', item_id: item.id,
            output_index: idx, arguments: item.arguments,
          })}\n\n`)
        }
        res.write(`event: response.output_item.done\ndata: ${JSON.stringify({
          type: 'response.output_item.done', item, output_index: idx,
        })}\n\n`)
        idx++
      }
      res.write(`event: response.completed\ndata: ${JSON.stringify({
        type: 'response.completed',
        response: {
          id: respId, object: 'response', created_at: created, status: 'completed',
          model: mdl, output, usage,
        },
      })}\n\n`)
      res.end()
    } else {
      res.json({
        id: respId, object: 'response', created_at: created, status: 'completed',
        model: mdl, output, usage,
      })
    }
  }

  // ── Codex forwarding (native tool_call via OAuth pool) ────────
  async function handleCodex(req, res) {
    const { stream = false } = req.body
    let result
    try {
      result = await codexRequest(req.body)
    } catch (e) {
      // 这条路由（Codex OAuth 直连）此时**还没开流**，头也没发，
      // 所以正常返回 502 JSON。别照抄网页会话那条的"往流里报错"。
      return res.status(502).json({ error: { message: e.message, type: 'upstream_error' } })
    }

    const { upstream, acct, statusCode } = result
    if (statusCode !== 200) {
      let body = ''
      upstream.on('data', (c) => (body += c))
      upstream.on('end', () => {
        console.log(`[codex] ${acct} error ${statusCode}: ${body.slice(0, 200)}`)
        try {
          res.status(statusCode).json(JSON.parse(body))
        } catch (_) {
          res.status(statusCode).json({ error: { message: body, type: 'upstream_error' } })
        }
      })
      return
    }

    if (stream) {
      res.setHeader('Content-Type', 'text/event-stream')
      res.setHeader('Cache-Control', 'no-cache')
      res.setHeader('Connection', 'keep-alive')
      res.setHeader('Access-Control-Allow-Origin', '*')
      res.setHeader('X-Codex-Account', acct)
      upstream.pipe(res)
      return
    }

    // Non-streaming: buffer Codex SSE → collect output items + response.completed → return JSON
    let buf = ''
    let completed = null
    const outputItems = []
    upstream.on('data', (chunk) => {
      buf += chunk.toString()
      const lines = buf.split('\n')
      buf = lines.pop()
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        try {
          const d = JSON.parse(line.slice(6))
          if (d.type === 'response.output_item.done' && d.item) outputItems.push(d.item)
          if (d.type === 'response.completed' && d.response) completed = d.response
        } catch (_) {}
      }
    })
    upstream.on('end', () => {
      if (completed) {
        if (outputItems.length) completed.output = outputItems
        res.json(completed)
      } else {
        res.status(502).json({ error: { message: 'no completed response from Codex', type: 'upstream_error' } })
      }
    })
  }

  return router
}

module.exports = { buildResponsesRoute }
