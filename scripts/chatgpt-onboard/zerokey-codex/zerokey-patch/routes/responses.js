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
let ESCALATE = ''
let escalatePrompt = null
try {
  ;({ compileToExec, prepareCodexInput, needsEscalation, firstAsk, ESCALATE,
      escalatePrompt } = require('./bpi-codex'))
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

    await acquireSlot('ChatGPT')

    const INLINE_MAX = parseInt(process.env.ZK_INLINE_MAX || '100000', 10)
    let sendPrompt = prompt
    let attachments = null
    if (prompt.length > INLINE_MAX) {
      try {
        const buf = Buffer.from(prompt, 'utf8')
        const up = await chatgptApi.uploadFile(buf, {
          fileName: `conversation-${Date.now()}.txt`,
          mimeType: 'text/plain',
          useCase: 'my_files',
        })
        attachments = [{ id: up.id, size: up.size, name: up.name, mimeType: up.mimeType }]
        sendPrompt =
          'The full conversation/context is in the attached text file ' +
          `(${up.name}). Read it and respond to the latest request in it.`
        console.log(`[responses] long prompt ${prompt.length} chars → uploaded as ${up.id}`)
      } catch (e) {
        console.log(`[responses] file upload failed, falling back to inline: ${e.message}`)
      }
    }

    let upstream
    try {
      upstream = await chatgptApi.chatCompletion(sendPrompt, null, 'client-created-root', model, attachments)
    } catch (e) {
      return res.status(502).json({ error: { message: e.message, type: 'upstream_error' } })
    }

    const respId = 'resp_' + crypto.randomBytes(12).toString('hex')
    const msgId = 'msg_' + crypto.randomBytes(12).toString('hex')
    const created = Math.floor(Date.now() / 1000)
    const mdl = req.body.model || model || 'chatgpt-web'
    let full = ''
    let started = false
    let finished = false

    if (stream) {
      res.setHeader('Content-Type', 'text/event-stream')
      res.setHeader('Cache-Control', 'no-cache')
      res.setHeader('Connection', 'keep-alive')
      res.setHeader('Access-Control-Allow-Origin', '*')

      // response.created
      const respShell = {
        id: respId, object: 'response', created_at: created, status: 'in_progress',
        model: mdl, output: [],
        usage: null,
      }
      // OpenAI spec shape: {type, response:{...}}. The bare object (no `type`)
      // is unparseable by strict consumers — notably LiteLLM's responses
      // streaming logger keys off data.type, so a missing `type` on the
      // completed event means the spend log is never written (silent no-op).
      res.write(`event: response.created\ndata: ${JSON.stringify({
        type: 'response.created', response: respShell,
      })}\n\n`)

      // Web-tools: we cannot stream raw text (it may be a JSON tool envelope) —
      // buffer everything and emit the parsed result at finish(). Skip the
      // message-shell preamble; the item type isn't known until parse time.
      if (!useWebTools) {
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

    const onText = (t) => {
      if (!t) return
      full += t
      started = true
      if (useWebTools) { wtPump(false); return }
      if (!stream) return
      if (bpiFrozen || full.trimStart().startsWith('\u27E6')) { bpiFrozen = true; return }
      res.write(`event: response.output_text.delta\ndata: ${JSON.stringify({
        type: 'response.output_text.delta', item_id: msgId,
        output_index: 0, content_index: 0, delta: t,
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
      if (codexLite && !bpiRetried && needsEscalation(full)) {
        bpiRetried = true
        console.log('[bpi] refusal detected -> escalated retry')
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
