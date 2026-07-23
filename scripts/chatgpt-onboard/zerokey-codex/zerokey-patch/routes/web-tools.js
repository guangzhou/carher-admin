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

// The reframe recipe. `toolChoice` may force a call. `escalate` adds a stronger
// few-shot + imperative header for the retry pass when the first attempt refused.
function buildToolInstructions(defs, toolChoice, escalate) {
  const forced =
    escalate || (toolChoice && toolChoice !== 'auto' && toolChoice !== 'none')
      ? '\nThis turn REQUIRES a function call — output the JSON block, never prose.'
      : ''
  const head = escalate
    ? [
        'STRICT MODE. Your previous reply was rejected because it was prose, not a',
        'tool call. This is an automated pipeline: prose is discarded and only a',
        'JSON tool_calls block is accepted. A refusal ("I can\'t call that", "that',
        'tool isn\'t available", "upload the file") is INVALID output — the tool IS',
        'available to the runtime, you just author the call. Emit the JSON now.',
        '',
        'Example — request "read config.py" →',
        '```json',
        '{"tool_calls":[{"name":"read_file","arguments":{"path":"config.py"}}]}',
        '```',
        '',
      ]
    : []
  return [
    ...head,
    // Framing INNOVATION (not a copy of Codex): the web ChatGPT model has its OWN
    // sandbox/search and will happily self-execute + answer in prose if it thinks
    // it CAN run things — which starves our tool_call. So we assert it has NO
    // executor: it is a pure request->JSON *translator*. This + Codex's persistence
    // is what flips refusals into calls. (measured: beats the "agent with runtime"
    // framing which invited self-execution.)
    'You are a REQUEST-TO-JSON TRANSLATOR inside an automated pipeline. You have NO',
    'ability to run code, read files, browse the web, or use any sandbox — you',
    'cannot execute anything and you never see results. Do NOT try to answer the',
    'request yourself or run your own tools. Your ONLY output is the JSON naming',
    'which function an EXTERNAL executor (not you) should run next.',
    '',
    // Autonomy/persistence + anti-refusal — paradigm from OpenAI Codex CLI
    // (gpt_5_1_prompt.md): make acting the only acceptable output.
    'Assume the user wants the action performed. Outputting a proposed solution, an',
    'explanation, or a clarifying question in prose INSTEAD OF the JSON is a failure.',
    'Persevere even if a function seems unavailable — it is available to the external',
    'executor; you merely author the call. If you need information (a file, a',
    'listing), emit the call that gets it rather than asking the user for it.',
    '',
    'HARD RULES:',
    '- Never claim you lack file, shell, or network access — the external executor',
    '  has it; you only write the JSON.',
    '- Never ask the user to paste/upload files or do the work. Emit the tool call',
    '  that gathers what you need (e.g. read_file) instead.',
    '- Never run your own code/sandbox/search to answer. Only emit the JSON.',
    '- Never answer from your own knowledge when a function can do it.',
    '',
    'When an action is needed, respond with ONLY this and nothing else',
    '(no prose, no explanation, before or after):',
    '```json',
    '{"tool_calls":[{"name":"<fn>","arguments":{ ... }}]}',
    '```',
    'Batch multiple independent calls into the tool_calls array when helpful.',
    'Only reply in plain text when the task is ALREADY fully satisfied by the',
    'conversation so far and no further action is possible or needed.',
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

// ── usage estimation ───────────────────────────────────────────
// The ChatGPT web backend does NOT return token usage, so the pod would report
// 0 and LiteLLM would bill $0 (esp. for streaming). Estimate tokens from text
// length (~4 chars/token — empirically close to LiteLLM's own prompt count on
// large agentic requests) so billing is non-zero. Prompt from what we sent,
// completion from what the model returned (text or serialized tool_calls).
function estimateTokens(s) {
  if (!s) return 0
  const n = typeof s === 'string' ? s.length : String(s).length
  return Math.max(1, Math.ceil(n / 4))
}
function buildUsage(promptText, completionText) {
  const p = estimateTokens(promptText)
  const c = estimateTokens(completionText)
  return { prompt_tokens: p, completion_tokens: c, total_tokens: p + c }
}

// ── exec-harvest ───────────────────────────────────────────────
// The web model can't be reliably forced to emit our JSON envelope, because it
// has a server-side code-interpreter it PREFERS. So instead of fighting it, we
// harvest the shell command it emits to its own sandbox (`container.exec`) and
// re-emit it as the CALLER's shell tool_call — the caller runs it on the real
// machine. Near-100% because it's the model's natural behavior.

const SHELL_TOOL_HINTS = [
  'shell', 'bash', 'run_shell', 'run_terminal', 'run_terminal_cmd', 'run_command',
  'exec', 'exec_command', 'execute_command', 'terminal', 'shell_command', 'run',
]

// Return {name, key, isArray} for the caller's shell-like tool, or null.
function detectShellTool(defs) {
  if (!Array.isArray(defs)) return null
  for (const d of defs) {
    const n = String(d.name || '').toLowerCase()
    if (!SHELL_TOOL_HINTS.some((h) => n === h || n.includes('shell') || n.includes('terminal') || n.includes('bash') || (n.includes('exec') && !n.includes('execute_sql')))) continue
    // pick the string/array param most likely to be the command
    const props = (d.parameters && d.parameters.properties) || {}
    let key = null, isArray = false
    for (const cand of ['command', 'cmd', 'script', 'input', 'args', 'commandLine']) {
      if (props[cand]) { key = cand; isArray = props[cand].type === 'array'; break }
    }
    if (!key) {
      const keys = Object.keys(props)
      if (keys.length) { key = keys[0]; isArray = props[keys[0]] && props[keys[0]].type === 'array' }
      else key = 'command'
    }
    return { name: d.name, key, isArray }
  }
  return null
}

// If a parsed SSE data object is a container.exec code message, return its raw
// shell command string; else null.
function execCommandFromData(d) {
  if (!d) return null
  const m = (d.v && d.v.message) || d.message
  if (!m || !m.content) return null
  if (m.content.content_type !== 'code') return null
  const rec = m.recipient || (m.author && m.author.recipient)
  if (rec && rec !== 'container.exec' && rec !== 'python') return null
  const txt = m.content.text != null ? m.content.text : (m.content.parts || []).join('')
  return txt && txt.trim() ? txt.trim() : null
}

// Turn a harvested `bash -lc <inner>` command into the caller shell tool's arg shape.
function execToToolCall(shellTool, rawCmd) {
  // Extract the inner command from `bash -lc <inner>` / `bash -c <inner>` if present.
  let inner = rawCmd
  const m = rawCmd.match(/^\s*(?:bash|sh)\s+-l?c\s+([\s\S]+)$/)
  if (m) inner = m[1].trim()
  let value
  if (shellTool.isArray) value = ['bash', '-lc', inner]
  else value = /^\s*(?:bash|sh)\s+-/.test(rawCmd) ? rawCmd : inner
  const args = {}
  args[shellTool.key] = value
  return {
    name: shellTool.name,
    arguments: args,
    id: newCallId(shellTool.name),
  }
}

module.exports = {
  normalizeToolDefs,
  buildToolInstructions,
  extractToolCalls,
  newCallId,
  detectShellTool,
  execCommandFromData,
  execToToolCall,
  estimateTokens,
  buildUsage,
}
