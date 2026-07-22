// Web-path tool-call adapter (sub2api-style prompt-injection + output parsing).
//
// WHY
// ---
// The ChatGPT *web* backend (/backend-api/f/conversation) does NOT accept an
// OpenAI `tools` field and never emits native tool_calls — it is a plain
// chat surface wrapped in ChatGPT's consumer harness (system prompt + auto
// web-search + answer widgets). The Codex backend (/backend-api/codex/responses)
// DOES do native tool_calls, which is why zerokey's codex-pool path is
// preferred. But a web-only pod (no CODEX_TOKEN_DIR) has no Codex tokens, so
// its agentic (tools[]) traffic would otherwise silently degrade to plain chat.
//
// This module lets a web-only pod still serve arbitrary caller-defined tools,
// by the technique every web-subscription bridge (sub2api / chat2api) uses:
//   1. Inject the caller's tool catalog into the prompt as text.
//   2. Instruct the model to emit a single fenced JSON envelope.
//   3. Parse that text envelope back into OpenAI tool_calls / Responses events.
//
// RELIABILITY NOTE (measured against this backend, 2026-07-22)
// -----------------------------------------------------------
// The naive "you have a real runtime, call these tools" framing FAILS: the web
// harness makes the model refuse ("I don't have access to that runtime") or
// answer naturally, and weather/math queries trigger its built-in search
// widgets. The framing that WORKS reliably (4/4, multi-turn, coding tools):
// reframe the task as *authoring the JSON for the next action* — the model is a
// planner/translator that never executes and never needs file access. That is
// the prompt below. It is best-effort, not native: keep tool schemas simple and
// prefer the codex-pool path whenever tokens exist.

// ── Tool catalog rendering ─────────────────────────────────────

// Accept both Chat-Completions tools ({type:'function', function:{name,...}})
// and Responses tools ({type:'function', name,...}). Return a normalized list.
function normalizeToolDefs(tools) {
  if (!Array.isArray(tools)) return []
  const out = []
  for (const t of tools) {
    if (!t) continue
    if (t.type && t.type !== 'function') continue // skip built-in tools (web_search, etc.)
    const fn = t.function || t
    if (!fn || !fn.name) continue
    out.push({
      name: fn.name,
      description: fn.description || '',
      parameters: fn.parameters || { type: 'object', properties: {} },
    })
  }
  return out
}

function renderCatalog(defs) {
  return defs
    .map((d) => {
      const params = JSON.stringify(d.parameters)
      const desc = d.description ? ` — ${d.description}` : ''
      return `- ${d.name}(${params})${desc}`
    })
    .join('\n')
}

// The reframe recipe. `toolChoice` may force a call.
function buildToolInstructions(defs, toolChoice) {
  const forced =
    toolChoice && toolChoice !== 'auto' && toolChoice !== 'none'
      ? '\nYou MUST emit a tool call for this turn.'
      : ''
  return [
    'You are the planning layer of an automated coding agent. Your job is to',
    'translate the conversation into the SINGLE next function call, expressed as',
    'JSON. You do NOT execute anything, you never need file or network access,',
    'and you must NEVER say you lack access — you only WRITE the JSON describing',
    'the next action a downstream runtime will perform.',
    '',
    'When a function call is needed, respond with ONLY this and nothing else',
    '(no prose before or after):',
    '```json',
    '{"tool_calls":[{"name":"<fn>","arguments":{ ... }}]}',
    '```',
    'You may batch multiple independent calls into the tool_calls array.',
    'If — and only if — the request is already fully satisfied by the',
    'conversation so far and no further action is needed, reply in plain text',
    'with the final answer instead of a JSON block.',
    forced,
    '',
    'AVAILABLE FUNCTIONS:',
    renderCatalog(defs),
  ].join('\n')
}

// ── Output parsing ─────────────────────────────────────────────

// Extract the first balanced JSON object/array starting at `start`.
function sliceBalanced(s, start) {
  const open = s[start]
  const close = open === '{' ? '}' : open === '[' ? ']' : null
  if (!close) return null
  let depth = 0
  let inStr = false
  let esc = false
  for (let i = start; i < s.length; i++) {
    const c = s[i]
    if (inStr) {
      if (esc) esc = false
      else if (c === '\\') esc = true
      else if (c === '"') inStr = false
      continue
    }
    if (c === '"') inStr = true
    else if (c === open) depth++
    else if (c === close) {
      depth--
      if (depth === 0) return s.slice(start, i + 1)
    }
  }
  return null
}

// Pull a {"tool_calls":[...]} (or bare [...] / {...}) envelope out of model text.
// Returns { calls: [{name, arguments(obj)}], leadingText } or null if no envelope.
function extractToolCalls(text) {
  if (!text || text.indexOf('tool_calls') === -1) {
    // Also accept a lone fenced json block that is itself a call array/object.
    return tryLooseArray(text)
  }
  const key = text.indexOf('"tool_calls"')
  if (key === -1) return tryLooseArray(text)
  const br = text.indexOf('[', key)
  if (br === -1) return null
  const arr = sliceBalanced(text, br)
  if (!arr) return null
  let parsed
  try {
    parsed = JSON.parse(arr)
  } catch (_) {
    return null
  }
  const calls = coerceCalls(parsed)
  if (!calls.length) return null
  const fenceStart = text.lastIndexOf('```json', key)
  const leadEnd = fenceStart > -1 ? fenceStart : text.indexOf('{')
  const leadingText = leadEnd > 0 ? text.slice(0, leadEnd).trim() : ''
  return { calls, leadingText }
}

function tryLooseArray(text) {
  if (!text) return null
  const m = text.match(/```json\s*([\s\S]*?)```/)
  const body = m ? m[1].trim() : null
  if (!body) return null
  let parsed
  try {
    parsed = JSON.parse(body)
  } catch (_) {
    return null
  }
  const calls = coerceCalls(parsed.tool_calls || parsed)
  if (!calls.length) return null
  return { calls, leadingText: '' }
}

function coerceCalls(v) {
  const arr = Array.isArray(v) ? v : v && v.tool_calls ? v.tool_calls : v ? [v] : []
  const out = []
  for (const c of arr) {
    if (!c) continue
    const name = c.name || (c.function && c.function.name)
    if (!name) continue
    let args = c.arguments != null ? c.arguments : c.function && c.function.arguments
    if (typeof args === 'string') {
      try {
        args = JSON.parse(args)
      } catch (_) {
        /* leave as string */
      }
    }
    if (args == null) args = {}
    out.push({ name, arguments: args })
  }
  return out
}

let _callSeq = 0
function newCallId(name) {
  _callSeq++
  return `call_web_${Date.now().toString(36)}_${_callSeq}_${name}`.slice(0, 60)
}

module.exports = {
  normalizeToolDefs,
  buildToolInstructions,
  extractToolCalls,
  newCallId,
}
