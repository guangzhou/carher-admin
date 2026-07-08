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

// Real web slugs available to this account (chatgpt.com/backend-api/models).
const WEB_MODELS = [
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
const ALIASES = {
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

  await acquireSlot('ChatGPT')

  // Long-prompt → file attachment. The web /f/conversation endpoint caps a
  // single inline message (~128k chars → 413 message_length_exceeds_limit).
  // Above ZK_INLINE_MAX chars, upload the prompt as a .txt attachment and send
  // a short instruction inline. Zero-regression: short prompts unchanged.
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
      console.log(`[raw] long prompt ${prompt.length} chars → uploaded as ${up.id}`)
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

  if (stream) {
    res.setHeader('Content-Type', 'text/event-stream')
    res.setHeader('Cache-Control', 'no-cache')
    res.setHeader('Connection', 'keep-alive')
    res.setHeader('Access-Control-Allow-Origin', '*')
  }

  const onText = (t) => {
    if (!t) return
    full += t
    if (!stream) return
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

  const finish = () => {
    if (finished) return
    finished = true
    if (stream) {
      res.write(
        `data: ${JSON.stringify({
          id,
          object: 'chat.completion.chunk',
          created,
          model: mdl,
          choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
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
        usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
      })
    }
  }

  await readSSE(upstream, {
    onData: (d) => {
      if (!d || finished) return
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
