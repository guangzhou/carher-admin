/**
 * zerokey-serve-codex.js — headless launcher for zerokey (no inquirer wizard).
 *
 * Drop this file INTO the zerokey repo root and run it from there so the
 * relative requires resolve. It reads temp/users.json (seeded by the capture
 * script), picks the chatgpt user, and serves the OpenAI-compatible API on
 * $PORT (default 8123) bound to 0.0.0.0 so Codex on another host can reach it.
 *
 * ENV:
 *   PORT     listen port (default 8123)
 *   ZK_USER  username key under users.json.chatgpt (default: first key)
 */
const express = require('express')
const fs = require('fs')
const path = require('path')

const modelsRouter = require('./routes/models')
const healthRouter = require('./routes/health')
const { buildChatGPTRouter } = require('./routes/chatgpt')

const PORT = parseInt(process.env.PORT || '8123', 10)
const usersFile = path.join(__dirname, 'temp', 'users.json')

if (!fs.existsSync(usersFile)) {
  console.error(`[fatal] ${usersFile} not found — run the capture step first`)
  process.exit(1)
}

const all = JSON.parse(fs.readFileSync(usersFile, 'utf8'))
const cg = all.chatgpt || {}
const userKey = process.env.ZK_USER || Object.keys(cg)[0]
const user = cg[userKey]
if (!user || !user.parsedFetch) {
  console.error(`[fatal] no chatgpt user "${userKey}" with parsedFetch in users.json`)
  process.exit(1)
}

const session = {
  name: 'codex',
  chatSessionId: null,
  parentMessageId: null,
  createdAt: new Date().toISOString(),
  lastUsed: new Date().toISOString(),
}
const saveSession = () => {
  try {
    user.sessions = [session]
    fs.writeFileSync(usersFile, JSON.stringify(all, null, 2))
  } catch (e) {
    console.error('[warn] saveSession failed:', e.message)
  }
}

const app = express()
app.use(express.json({ limit: '50mb' }))
app.use((req, res, next) => {
  const a = req.headers.authorization || ''
  req.ide = a.startsWith('Bearer ') ? a.slice(7).trim().toLowerCase() : 'vscode'
  next()
})
app.use('/v1/models', modelsRouter)
app.use('/', healthRouter)

;(async () => {
  try {
    const r = await buildChatGPTRouter(user.parsedFetch, session, saveSession)
    app.use('/v1/chat/completions', r)
    app.listen(PORT, '0.0.0.0', () => {
      console.log(`\n✅ zerokey-codex (user=${userKey}) on http://0.0.0.0:${PORT}`)
      console.log(`   POST http://0.0.0.0:${PORT}/v1/chat/completions`)
    })
  } catch (e) {
    console.error('[fatal] failed to start:', e.message)
    process.exit(1)
  }
})()
