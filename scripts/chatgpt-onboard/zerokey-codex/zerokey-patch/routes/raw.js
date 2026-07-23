// Raw passthrough mode for zerokey.
//
// Activated when the client sends Authorization: Bearer raw  (req.ide === 'raw')
// or Bearer codex. It does NOT touch the VS Code / ToolCompiler path.
//
// Differences vs the VS Code path:
//   - No tool-grammar injection (instructions.md / ¦ syntax). The original
//     OpenAI messages are flattened verbatim and sent to chatgpt.com.
//   - Stateless: every request opens a FRESH web conversation and sends the
//     full message history (standard OpenAI /chat/completions semantics),
//     instead of reusing one long-lived web conversation.
//   - Per-request model selection via req.body.model.
//   - Supports both stream:true and stream:false.

const { readSSE } = require('../utils/sse-reader')
const { acquireSlot } = require('../utils/rate-limiter')
const { normalizeToolDefs, buildToolInstructions, extractToolCalls, newCallId, detectShellTool, execCommandFromData, execToToolCall, buildUsage } = require('./web-tools')

// Real web slugs available to this account (chatgpt.com/backend-api/models).
const WEB_MODELS = [
  'gpt-5-6-thinking',
  'gpt-5-6-pro',
  'gpt-5-5-pro',
  'gpt-5-5-thinking',
  'gpt-5-5',
  'gpt-5-5-instant',
  'gpt-5-4-pro',
  'gpt-5-4-thinking',
  'gpt-5-4-t-mini',
  'gpt-5-3',
  'gpt-5-3-instant',
  'gpt-5-3-mini',
  'gpt-5-2',
  'gpt-5-1',
  'gpt-5',
  'gpt-5-mini',
  'o3',
  'o3-pro',
  'gpt-4-5',
  'research',
  'agent-mode',
]

// Friendly aliases → web slug (OpenAI-ish names some clients hardcode).
// 5.6: the web backend serves the sol/terra/luna tunings NATIVELY via the PLAIN
// dot slugs `gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna` (inline streaming,
// verified on aliyun + 225). Do NOT append `-wm` (that's the with-memory variant
// which streams via a conduit handoff our replay can't follow → empty). So plain
// sol/terra/luna pass through verbatim; only strip the litellm `chatgpt-` prefix.
const ALIASES = {
  'gpt-5.6': 'gpt-5-6-thinking',
  'gpt-5-6': 'gpt-5-6-thinking',
  'gpt-5.6-pro': 'gpt-5-6-pro',
  'chatgpt-gpt-5.6-sol': 'gpt-5.6-sol',
  'chatgpt-gpt-5.6-terra': 'gpt-5.6-terra',
  'chatgpt-gpt-5.6-luna': 'gpt-5.6-luna',
  'gpt-5.5': 'gpt-5-5',
  'gpt-5.5-pro': 'gpt-5-5-pro',
  'gpt-5.4': 'gpt-5-4-thinking',
  'gpt-4o': 'gpt-5-mini',
  'gpt-4.5': 'gpt-4-5',
  'o3-mini': 'gpt-5-3-mini',
  'deep-research': 'research',
  auto: null,
  default: null,
}

// Resolve a client-supplied model name to a web slug.
// Returns null → use the captured template model (gpt-5-5-pro).
function resolveModel(m) {
  if (!m) return process.env.ZK_DEFAULT_MODEL || null
  if (WEB_MODELS.includes(m)) return m
  if (Object.prototype.hasOwnProperty.call(ALIASES, m)) return ALIASES[m]
  // Unknown: pass through verbatim (web backend will validate) unless empty.
  return m
}

function textOf(content) {
  if (content == null) return ''
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content
      .map((p) => (typeof p === 'string' ? p : p && (p.text || p.content || '')) || '')
      .join('')
  }
  return String(content)
}

// Flatten OpenAI messages into one plain prompt, no tool grammar injected.
function flatten(messages) {
  return messages
    .map((m) => {
      const role = String(m.role || 'user').toUpperCase()
      const body = textOf(m.content)
      if (m.role === 'tool') {
        return `TOOL_RESULT(${m.tool_call_id || ''}): ${body}`
      }
      return `${role}: ${body}`
    })
    .join('\n\n')
}

// Handle a raw-passthrough chat completion. `chatgptApi` is the shared instance.
async function rawComplete(req, res, chatgptApi) {
  const { messages = [], stream = false } = req.body
  if (!messages.length) {
    return res.status(400).json({
      error: { message: 'messages is required and must be a non-empty array', type: 'invalid_request_error' },
    })
  }

  const model = resolveModel(req.body.model)
  const prompt = flatten(messages)

  // Web tool-injection (chat/completions): when the caller passes tools[], the
  // web backend can't do native tool_calls, so inject the reframed catalog and
  // parse the JSON envelope back out at finish(). Best-effort; see web-tools.js.
  const webToolDefs =
    Array.isArray(req.body.tools) && req.body.tools.length > 0
      ? normalizeToolDefs(req.body.tools)
      : []
  const useWebTools = webToolDefs.length > 0
  // exec-harvest: if the caller has a shell-like tool, don't fight the web model's
  // built-in code-interpreter — let it emit its `container.exec` command and re-emit
  // that as the caller's shell tool_call. Falls back to envelope injection otherwise.
  const shellTool = useWebTools ? detectShellTool(webToolDefs) : null
  const promptForSend = !useWebTools
    ? prompt
    : shellTool
      ? `Use your shell to accomplish the task below. Prefer a single shell command.\n\n${prompt}`
      : `${buildToolInstructions(webToolDefs, req.body.tool_choice)}\n\n${prompt}`

  await acquireSlot('ChatGPT')

  // Long-prompt → file attachment. The web /f/conversation endpoint caps a
  // single inline message (~128k chars → 413 message_length_exceeds_limit).
  // Above ZK_INLINE_MAX chars, upload the prompt as a .txt attachment and send
  // a short instruction inline. Zero-regression: short prompts unchanged.
  const INLINE_MAX = parseInt(process.env.ZK_INLINE_MAX || '100000', 10)
  let sendPrompt = promptForSend
  let attachments = null
  if (promptForSend.length > INLINE_MAX) {
    try {
      const buf = Buffer.from(promptForSend, 'utf8')
      const up = await chatgptApi.uploadFile(buf, {
        fileName: `conversation-${Date.now()}.txt`,
        mimeType: 'text/plain',
        useCase: 'my_files',
      })
      // Real web attachment structure: id + mimeType + name + size only.
      // library_file_id is NOT needed (verified via gptchat2api-cf reference).
      attachments = [
        {
          id: up.id,
          size: up.size,
          name: up.name,
          mimeType: up.mimeType,
        },
      ]
      sendPrompt =
        'The full conversation/context is in the attached text file ' +
        `(${up.name}). Read it and respond to the latest request in it.`
      console.log(`[raw] long prompt ${promptForSend.length} chars → uploaded as ${up.id}`)
    } catch (e) {
      // Upload failed → fall back to inline (may 413, then litellm fallback
      // handles it). Don't hard-fail the request here.
      console.log(`[raw] file upload failed, falling back to inline: ${e.message}`)
    }
  }

  let upstream
  try {
    // Stateless: fresh conversation each call (chatSessionId=null), full history in prompt.
    upstream = await chatgptApi.chatCompletion(sendPrompt, null, 'client-created-root', model, attachments)
  } catch (e) {
    return res.status(502).json({ error: { message: e.message, type: 'upstream_error' } })
  }

  const id = 'chatcmpl-' + Date.now().toString(36)
  const created = Math.floor(Date.now() / 1000)
  const mdl = req.body.model || model || 'chatgpt-web'
  let full = ''
  let started = false
  let finished = false
  let harvestedCmd = null   // exec-harvest: first container.exec command seen

  if (stream) {
    res.setHeader('Content-Type', 'text/event-stream')
    res.setHeader('Cache-Control', 'no-cache')
    res.setHeader('Connection', 'keep-alive')
    res.setHeader('Access-Control-Allow-Origin', '*')
  }

  const onText = (t) => {
    if (!t) return
    full += t
    // Web-tools: buffer silently — the text may be a JSON tool envelope that we
    // only emit (as tool_calls) once complete.
    if (!stream || useWebTools) return
    const delta = started ? { content: t } : { role: 'assistant', content: t }
    started = true
    res.write(
      `data: ${JSON.stringify({
        id,
        object: 'chat.completion.chunk',
        created,
        model: mdl,
        choices: [{ index: 0, delta, finish_reason: null }],
      })}\n\n`,
    )
  }

  // Collect the full web reply for one prompt (used by the web-tools retry).
  // Returns accumulated text; never streams. Isolated from the main readSSE.
  async function collectWebText(promptText) {
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

  // Emit the final chat/completions payload for the web-tools path given parsed
  // calls (or null → plain text `content`).
  const emitWebToolsResult = (parsed, content) => {
    if (parsed && parsed.calls.length) {
      const tool_calls = parsed.calls.map((c, i) => ({
        index: i,
        id: newCallId(c.name),
        type: 'function',
        function: {
          name: c.name,
          arguments: typeof c.arguments === 'string' ? c.arguments : JSON.stringify(c.arguments),
        },
      }))
      const usage = buildUsage(promptForSend, JSON.stringify(tool_calls))
      if (stream) {
        res.write(`data: ${JSON.stringify({ id, object: 'chat.completion.chunk', created, model: mdl,
          choices: [{ index: 0, delta: { role: 'assistant', content: null, tool_calls }, finish_reason: null }] })}\n\n`)
        res.write(`data: ${JSON.stringify({ id, object: 'chat.completion.chunk', created, model: mdl,
          choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }], usage })}\n\n`)
        res.write('data: [DONE]\n\n')
        res.end()
      } else {
        res.json({ id, object: 'chat.completion', created, model: mdl,
          choices: [{ index: 0, message: { role: 'assistant', content: null, tool_calls }, finish_reason: 'tool_calls' }],
          usage })
      }
      return
    }
    const usage = buildUsage(promptForSend, content)
    if (stream) {
      res.write(`data: ${JSON.stringify({ id, object: 'chat.completion.chunk', created, model: mdl,
        choices: [{ index: 0, delta: { role: 'assistant', content }, finish_reason: null }] })}\n\n`)
      res.write(`data: ${JSON.stringify({ id, object: 'chat.completion.chunk', created, model: mdl,
        choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage })}\n\n`)
      res.write('data: [DONE]\n\n')
      res.end()
    } else {
      res.json({ id, object: 'chat.completion', created, model: mdl,
        choices: [{ index: 0, message: { role: 'assistant', content }, finish_reason: 'stop' }],
        usage })
    }
  }

  const finish = () => {
    if (finished) return
    finished = true

    // ── exec-harvest branch: re-emit the model's container.exec command as the
    //    caller's shell tool_call. ──
    if (shellTool && harvestedCmd) {
      const tc = execToToolCall(shellTool, harvestedCmd)
      const tool_calls = [{ index: 0, id: tc.id, type: 'function',
        function: { name: tc.name, arguments: JSON.stringify(tc.arguments) } }]
      const usage = buildUsage(promptForSend, JSON.stringify(tc.arguments))
      if (stream) {
        res.write(`data: ${JSON.stringify({ id, object: 'chat.completion.chunk', created, model: mdl,
          choices: [{ index: 0, delta: { role: 'assistant', content: null, tool_calls }, finish_reason: null }] })}\n\n`)
        res.write(`data: ${JSON.stringify({ id, object: 'chat.completion.chunk', created, model: mdl,
          choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }], usage })}\n\n`)
        res.write('data: [DONE]\n\n')
        res.end()
      } else {
        res.json({ id, object: 'chat.completion', created, model: mdl,
          choices: [{ index: 0, message: { role: 'assistant', content: null, tool_calls }, finish_reason: 'tool_calls' }],
          usage })
      }
      return
    }

    // ── Web-tools branch: parse buffered text → OpenAI tool_calls ──
    if (useWebTools) {
      const parsed = extractToolCalls(full)
      if (parsed && parsed.calls.length) return emitWebToolsResult(parsed, null)
      // No envelope on the first pass. One escalated retry before giving up as
      // plain text — fixes accounts whose web harness refuses on the soft prompt.
      const escalated = `${buildToolInstructions(webToolDefs, 'required', true)}\n\n${prompt}`
      collectWebText(escalated)
        .then((text2) => {
          const p2 = extractToolCalls(text2)
          if (p2 && p2.calls.length) return emitWebToolsResult(p2, null)
          // Still nothing → return best plain text we have (prefer non-empty).
          emitWebToolsResult(null, full || text2 || '')
        })
        .catch(() => emitWebToolsResult(null, full || ''))
      return
    }

    if (stream) {
      res.write(
        `data: ${JSON.stringify({
          id,
          object: 'chat.completion.chunk',
          created,
          model: mdl,
          choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
          usage: buildUsage(promptForSend, full),
        })}\n\n`,
      )
      res.write('data: [DONE]\n\n')
      res.end()
    } else {
      res.json({
        id,
        object: 'chat.completion',
        created,
        model: mdl,
        choices: [
          { index: 0, message: { role: 'assistant', content: full }, finish_reason: 'stop' },
        ],
        usage: buildUsage(promptForSend, full),
      })
    }
  }

  await readSSE(upstream, {
    onData: (d) => {
      if (!d || finished) return
      // exec-harvest: grab the model's first container.exec shell command and finish.
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
        res.write(`data: ${JSON.stringify({ error: { message: err.message } })}\n\n`)
        res.end()
      } else {
        res.status(502).json({ error: { message: err.message, type: 'upstream_error' } })
      }
    },
    isDone: () => finished,
  })
}

module.exports = { rawComplete, resolveModel, WEB_MODELS }
