'use strict'
/*
 * zk-delta/tests/harness.js
 * 起一套真的三段链路：假上游 ← 真服务端 ← 真小代理。
 * 全部走真 HTTP，不打桩、不 mock 内部函数 —— 要验的就是"上游实际收到的字节"。
 */

const http = require('http')
const { spawn } = require('child_process')
const path = require('path')

/** 假上游：把收到的每一发原始字节完整记下来，然后回一段 SSE。 */
function startRecorder (port, label) {
  const got = []
  const srv = http.createServer((req, res) => {
    const cs = []
    req.on('data', (c) => cs.push(c))
    req.on('end', () => {
      const body = Buffer.concat(cs)
      got.push({ label, path: req.url, method: req.method, headers: req.headers, body })
      const sse = 'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n' +
                  'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n' +
                  'data: [DONE]\n\n'
      res.writeHead(200, { 'content-type': 'text/event-stream' })
      res.end(sse)
    })
  })
  srv.on('clientError', (e, sock) => { try { sock.destroy() } catch (_) {} })
  return new Promise((resolve) => srv.listen(port, '127.0.0.1', () => resolve({ srv, got, port, label })))
}

function req (opts, body) {
  return new Promise((resolve, reject) => {
    const r = http.request(opts, (res) => {
      const cs = []
      res.on('data', (c) => cs.push(c))
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(cs) }))
    })
    r.on('error', reject)
    r.end(body)
  })
}

function post (port, p, body, headers) {
  const b = Buffer.isBuffer(body) ? body : Buffer.from(body, 'utf8')
  return req({
    host: '127.0.0.1', port, path: p, method: 'POST',
    headers: Object.assign({
      'content-type': 'application/json',
      'content-length': b.length,
      'authorization': 'Bearer sk-test-zkd',
      'user-agent': 'Cursor/3.17.19'
    }, headers || {})
  }, b)
}

function get (port, p) {
  return req({ host: '127.0.0.1', port, path: p, method: 'GET' })
}

async function waitHealthy (port, tries) {
  for (let i = 0; i < (tries || 60); i++) {
    try { const r = await get(port, '/healthz'); if (r.status === 200) return true } catch (e) {}
    await new Promise((r) => setTimeout(r, 100))
  }
  throw new Error('service on ' + port + ' never became healthy')
}

function spawnNode (file, env, tag, sink) {
  const p = spawn(process.execPath, [file], {
    env: Object.assign({}, process.env, env),
    stdio: ['ignore', 'pipe', 'pipe']
  })
  const onLine = (buf) => { for (const l of buf.toString().split('\n')) if (l.trim()) sink.push(tag + ' ' + l) }
  p.stdout.on('data', onLine)
  p.stderr.on('data', onLine)
  return p
}

/**
 * 造一个形状贴近 Cursor 真实请求的 body。
 * 真实事故里第 1 轮 620,932 字节、每轮恒定 +505,700 字节，这里按同量级造。
 */
function makeCursorBody (nTurns, opts) {
  opts = opts || {}
  const frameworkChars = opts.frameworkChars || 480000
  const perTurnChars = opts.perTurnChars || 250000

  const tools = []
  for (let i = 0; i < 19; i++) {
    tools.push({
      type: 'function',
      function: {
        name: ['shell', 'read_file', 'write_file', 'edit_file', 'grep', 'glob', 'ls', 'todo_write',
          'web_search', 'fetch_rules', 'codebase_search', 'delete_file', 'run_terminal_cmd',
          'create_diagram', 'search_replace', 'multi_tool_use', 'apply_patch', 'list_dir', 'reapply'][i],
        description: 'tool ' + i + ' ' + 'd'.repeat(600),
        parameters: {
          type: 'object',
          properties: { arg: { type: 'string', description: 'a'.repeat(300) } },
          required: ['arg']
        }
      }
    })
  }

  const messages = [{ role: 'system', content: 'FRAMEWORK\n' + lorem(frameworkChars, 'sys') }]
  for (let t = 1; t <= nTurns; t++) {
    messages.push({ role: 'user', content: `[turn ${t}] ` + lorem(perTurnChars, 'u' + t) })
    if (t < nTurns) {
      messages.push({
        role: 'assistant',
        content: `answer ${t} ` + lorem(Math.floor(perTurnChars / 4), 'a' + t),
        tool_calls: [{ id: 'call_' + t, type: 'function', function: { name: 'shell', arguments: JSON.stringify({ arg: 'ls -la /turn' + t }) } }]
      })
      messages.push({ role: 'tool', tool_call_id: 'call_' + t, content: 'total ' + t + '\n' + lorem(2000, 't' + t) })
    }
  }

  // 键序刻意打散，用来验证"重建后键序不变"
  return {
    model: 'cursor-web-fc-82-terra',
    messages,
    stream: true,
    tools,
    tool_choice: 'auto',
    reasoning_effort: 'medium',
    max_tokens: 32000,
    temperature: 0,
    stream_options: { include_usage: true },
    user: 'zkd-test-user'
  }
}

/** 确定性伪随机文本：同一个 seed 永远产出同一串。
 *  真实会话里第 t+1 轮的历史消息与第 t 轮逐字节相同，fixture 必须复现这一点，
 *  否则前缀天然对不上，测出来的"省了多少带宽"是假的。 */
function lorem (n, seed) {
  const words = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel',
    'india', 'juliet', 'kilo', 'lima', 'mike', 'november', 'oscar', 'papa']
  let h = 2166136261
  for (const ch of String(seed == null ? 'x' : seed)) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619) >>> 0 }
  let s = ''
  while (s.length < n) {
    h = (Math.imul(h, 1103515245) + 12345) >>> 0
    s += words[h % words.length] + ' '
  }
  return s.slice(0, n)
}

module.exports = { startRecorder, post, get, waitHealthy, spawnNode, makeCursorBody, lorem, req }
