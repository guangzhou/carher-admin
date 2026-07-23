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
const { normalizeToolDefs, buildToolInstructions, extractToolCalls, newCallId, detectShellTool, execCommandFromData, execToToolCall } = require('./web-tools')

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
    const useWebTools = _webToolDefs.length > 0 && !hasTokens()

    const model = resolveModel(req.body.model)
    const basePrompt = flattenInput(input, instructions)
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
      res.write(`event: response.created\ndata: ${JSON.stringify(respShell)}\n\n`)

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

    const onText = (t) => {
      if (!t) return
      full += t
      started = true
      if (!stream || useWebTools) return
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

    const finish = () => {
      if (finished) return
      finished = true

      const usage = {
        input_tokens: 0, output_tokens: 0, total_tokens: 0,
        input_token_details: { cached_tokens: 0 },
        output_token_details: { reasoning_tokens: 0 },
      }

      // ── exec-harvest: re-emit the model's container.exec command as the
      //    caller's shell function_call. ──
      if (shellTool && harvestedCmd) {
        const tc = execToToolCall(shellTool, harvestedCmd)
        const parsed = { calls: [{ name: tc.name, arguments: tc.arguments }], leadingText: '' }
        return finishWebTools(res, { stream, respId, msgId, created, mdl, usage, full: '', parsed })
      }

      // ── Web-tools branch: parse the buffered text into function_call items ──
      if (useWebTools) {
        const parsed = extractToolCalls(full)
        if (parsed && parsed.calls.length) {
          return finishWebTools(res, { stream, respId, msgId, created, mdl, usage, full, parsed })
        }
        // No envelope on the first pass → one escalated retry before falling
        // back to plain text (fixes accounts whose harness refuses softly).
        const escalated = `${buildToolInstructions(_webToolDefs, 'required', true)}\n\n${basePrompt}`
        collectWebTextR(escalated)
          .then((text2) => {
            const p2 = extractToolCalls(text2)
            finishWebTools(res, {
              stream, respId, msgId, created, mdl, usage,
              full: full || text2 || '', parsed: p2 && p2.calls.length ? p2 : null,
            })
          })
          .catch(() =>
            finishWebTools(res, { stream, respId, msgId, created, mdl, usage, full: full || '', parsed: null }),
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

        // response.completed
        res.write(`event: response.completed\ndata: ${JSON.stringify({
          id: respId, object: 'response', created_at: created, status: 'completed',
          model: mdl, output: [doneMsg], usage,
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
    const { stream, respId, msgId, created, mdl, usage, full, parsed } = ctx
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

    if (stream) {
      let idx = 0
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
