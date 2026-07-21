// Codex token pool — round-robin OAuth tokens for /backend-api/codex/responses.
//
// Token source:  CODEX_TOKEN_DIR — directory of *.json files, each {"access_token":"eyJ…"}
// State source:  CODEX_STATE_FILE — optional, quota-rebalance state.json format.
//                Filters out TOKEN_INVALID / manual_offline accounts automatically.
//                Codex has its own quota bucket (separate from web chat 5h/7d),
//                so OFFLINE-5H / OFFLINE-WEEK accounts are still usable for Codex.
//
// Runtime health: 401 → mark token dead (won't retry until next reload).
//                 429 → mark token cooldown 10min, try next.
//
// The Codex endpoint requires: model=gpt-5.5, store=false, stream=true.

const https = require('https')
const fs = require('fs')
const path = require('path')

const TOKEN_DIR = process.env.CODEX_TOKEN_DIR || ''
const STATE_FILE = process.env.CODEX_STATE_FILE || ''
const CODEX_HOST = 'chatgpt.com'
const CODEX_PATH = '/backend-api/codex/responses'
const COOLDOWN_MS = 10 * 60 * 1000

let tokens = []
let idx = 0
// per-token health: { dead: bool, cooldownUntil: number }
const health = new Map()

function loadState() {
  if (!STATE_FILE) return null
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'))
  } catch (_) {
    return null
  }
}

function loadTokens() {
  if (!TOKEN_DIR) return
  const state = loadState()
  try {
    const files = fs.readdirSync(TOKEN_DIR).filter(f => f.endsWith('.json')).sort()
    const loaded = []
    for (const f of files) {
      const name = f.replace('.json', '')
      // state.json filter: skip TOKEN_INVALID and manual_offline accounts
      if (state && state[name]) {
        const s = state[name]
        const tier = String(s.tier || '').toUpperCase()
        if (tier === 'TOKEN_INVALID') continue
        if (s.manual_offline) continue
      }
      try {
        const data = JSON.parse(fs.readFileSync(path.join(TOKEN_DIR, f), 'utf8'))
        if (data.access_token) {
          loaded.push({ name, token: data.access_token })
        }
      } catch (_) {}
    }
    // clear health marks for tokens that were reloaded (fresh start)
    health.clear()
    tokens = loaded
    if (tokens.length) console.log(`[codex-pool] loaded ${tokens.length} tokens` +
      (state ? ` (state filter: ${STATE_FILE})` : ''))
  } catch (e) {
    console.error(`[codex-pool] load failed: ${e.message}`)
  }
}

function isUsable(name) {
  const h = health.get(name)
  if (!h) return true
  if (h.dead) return false
  if (h.cooldownUntil && Date.now() < h.cooldownUntil) return false
  return true
}

function markDead(name) {
  health.set(name, { dead: true, cooldownUntil: 0 })
  console.log(`[codex-pool] ${name} marked dead (401)`)
}

function markCooldown(name) {
  health.set(name, { dead: false, cooldownUntil: Date.now() + COOLDOWN_MS })
  console.log(`[codex-pool] ${name} cooldown 10min (429)`)
}

function nextUsableToken() {
  const n = tokens.length
  for (let i = 0; i < n; i++) {
    const t = tokens[idx % n]
    idx = (idx + 1) % n
    if (isUsable(t.name)) return t
  }
  return null
}

function hasTokens() {
  return tokens.some(t => isUsable(t.name))
}

function _sendCodex(slot, payload) {
  return new Promise((resolve, reject) => {
    const req = https.request(
      {
        hostname: CODEX_HOST,
        port: 443,
        path: CODEX_PATH,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${slot.token}`,
          'Content-Length': Buffer.byteLength(payload),
        },
      },
      (res) => {
        console.log(`[codex-pool] ${slot.name} → ${res.statusCode}`)
        resolve({ upstream: res, acct: slot.name, statusCode: res.statusCode })
      },
    )
    req.on('error', reject)
    req.write(payload)
    req.end()
  })
}

function normalizeTools(tools) {
  if (!tools) return undefined
  return tools.map((t) => {
    if (t.name) return t
    if (t.function) return { type: 'function', name: t.function.name, description: t.function.description, parameters: t.function.parameters, strict: t.function.strict }
    return t
  })
}

function codexRequest(body, maxRetries) {
  if (maxRetries === undefined) maxRetries = 3

  const input = Array.isArray(body.input)
    ? body.input
    : [{ role: 'user', content: [{ type: 'input_text', text: String(body.input || '') }] }]

  const tools = normalizeTools(body.tools)
  const payload = JSON.stringify({
    model: 'gpt-5.5',
    store: false,
    stream: true,
    input,
    ...(tools ? { tools } : {}),
    ...(body.tool_choice ? { tool_choice: body.tool_choice } : {}),
    ...(body.instructions ? { instructions: body.instructions } : {}),
  })

  async function attempt(tries) {
    const slot = nextUsableToken()
    if (!slot) throw new Error('no usable Codex OAuth tokens (all dead or in cooldown)')

    const result = await _sendCodex(slot, payload)

    if (result.statusCode === 401 && tries < maxRetries) {
      // drain body then retry
      result.upstream.resume()
      markDead(slot.name)
      return attempt(tries + 1)
    }
    if (result.statusCode === 429 && tries < maxRetries) {
      result.upstream.resume()
      markCooldown(slot.name)
      return attempt(tries + 1)
    }
    return result
  }

  return attempt(0)
}

loadTokens()
if (TOKEN_DIR) setInterval(loadTokens, 5 * 60 * 1000)

module.exports = { codexRequest, hasTokens, loadTokens }
