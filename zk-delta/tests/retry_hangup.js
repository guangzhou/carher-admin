'use strict'
/*
 * zk-delta/tests/retry_hangup.js —— 上游把连接掐断时的自动重发一次（2026-10-27）
 *
 * 背景：LiteLLM worker 偶发崩溃会把在途连接全断，zk-delta 看到 `socket hang up`。
 * 要证三件事：
 *   R1 还没回过任何字节就被断 ⇒ 自动重发一次，客户端拿到 200，上游收到的两发逐字节相同
 *   R2 第二发也被断 ⇒ 客户端拿到 502（不无限重试），计数 retry_fail +1
 *   R3 已经开始回字节后才断 ⇒ 不重发（会重复内容），上游只收到 1 发
 *   R4 ZKD_RETRY_ONCE=0 ⇒ 第一次断就 502，上游只收到 1 发
 * 只起本地进程，不打任何真上游。
 */
const http = require('http')
const path = require('path')
const H = require('./harness')
const F = require('../common/framing')

function flakyUpstream (port, plan) {
  // plan: 每一发怎么处理：'drop' = 收完 body 直接毁掉 socket；'midstream' = 回头 + 一个 chunk 后毁掉；'ok' = 正常 SSE
  const got = []
  let i = 0
  const srv = http.createServer((req, res) => {
    const cs = []
    req.on('data', (c) => cs.push(c))
    req.on('end', () => {
      got.push(Buffer.concat(cs))
      const act = plan[Math.min(i++, plan.length - 1)]
      if (act === 'drop') return req.socket.destroy()
      res.writeHead(200, { 'content-type': 'text/event-stream' })
      if (act === 'midstream') { res.write('data: {"choices":[{"delta":{"content":"par"}}]}\n\n'); return setTimeout(() => req.socket.destroy(), 50) }
      res.end('data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n')
    })
  })
  return new Promise((r) => srv.listen(port, '127.0.0.1', () => r({ srv, got })))
}

// harness.post 只听 end，响应中途被 RST 时既不 resolve 也不 reject ⇒ 这里自己写一个会报出连接被断的
function postRaw (port, p, body) {
  return new Promise((resolve) => {
    const b = Buffer.from(body, 'utf8')
    const r = http.request({ host: '127.0.0.1', port, path: p, method: 'POST',
      headers: { 'content-type': 'application/json', 'content-length': b.length, authorization: 'Bearer sk-test-zkd', 'x-zkd-synthetic': '1' } }, (res) => {
      const cs = []
      res.on('data', (c) => cs.push(c))
      res.on('end', () => resolve({ status: res.statusCode, body: Buffer.concat(cs) }))
      res.on('error', (e) => resolve({ status: 'conn_error:' + (e.code || e.message), partial: res.statusCode, body: Buffer.concat(cs) }))
    })
    r.on('error', (e) => resolve({ status: 'conn_error:' + (e.code || e.message), body: Buffer.from('') }))
    r.end(b)
  })
}

function fullEnvelope () {
  const body = { model: 'm', stream: true, messages: [{ role: 'user', content: 'hi ' + Math.random() }] }
  const raw = JSON.stringify(body)
  const p = F.proveRoundTrip(raw)
  return { raw, env: { v: F.PROTO_VERSION, path: '/v1/chat/completions', array_key: 'messages', template: p.template, template_digest: F.templateDigest(p.template), delta: p.items, expect_bytes: Buffer.byteLength(raw) } }
}

async function scenario (ok, name, plan, env, check) {
  const base = { R1: 18960, R2: 18962, R3: 18964, R4: 18966 }[name]
  const PU = base; const PS = base + 1
  const up = await flakyUpstream(PU, plan)
  const logs = []
  const srv = H.spawnNode(path.join(__dirname, '../server/server.js'), Object.assign({ ZKD_PORT: String(PS), ZKD_UPSTREAM: 'http://127.0.0.1:' + PU }, env || {}), '[srv]', logs)
  try {
    await H.waitHealthy(PS)
    const { raw, env: e } = fullEnvelope()
    let r
    const t0 = Date.now()
    try {
      r = await Promise.race([postRaw(PS, '/zkd/v1/delta', JSON.stringify(e)),
        new Promise((resolve) => setTimeout(() => resolve({ status: 'HANG', body: Buffer.from('') }), 5000))])
    } catch (err) { r = { status: 'conn_error:' + err.message, body: Buffer.from('') } }
    r.ms = Date.now() - t0
    await new Promise((res) => setTimeout(res, 150))
    const m = JSON.parse((await H.get(PS, '/metrics.json')).body.toString())
    check({ r, got: up.got, raw, m, logs })
  } finally {
    srv.kill(); up.srv.closeAllConnections && up.srv.closeAllConnections()
    await new Promise((r) => up.srv.close(() => r()))
  }
}

async function run (ok) {
  await scenario(ok, 'R1', ['drop', 'ok'], {}, ({ r, got, raw, m, logs }) => {
    ok('R1 首发被断 ⇒ 自动重发，客户端拿到 200', r.status === 200, String(r.status))
    ok('R1 上游共收到 2 发', got.length === 2, String(got.length))
    ok('R1 两发逐字节都 = Cursor 原始 body', got.every((b) => b.toString() === raw))
    ok('R1 计数 conn_error=1 retry_ok=1', m.upstream_conn_error === 1 && m.upstream_retry_ok === 1, JSON.stringify([m.upstream_conn_error, m.upstream_retry_ok]))
    ok('R1 日志带 who= 和 handle=', logs.some((l) => /upstream_error_retry .*who=[0-9a-f]{12}.*handle=zd_/.test(l)))
    ok('R1 结束后 inflight 回到 0', m.inflight === 0, String(m.inflight))
  })
  await scenario(ok, 'R2', ['drop', 'drop', 'ok'], {}, ({ r, got, m }) => {
    ok('R2 两发都被断 ⇒ 客户端 502', r.status === 502, String(r.status))
    ok('R2 只重发一次（上游收到 2 发，不是 3 发）', got.length === 2, String(got.length))
    ok('R2 计数 retry_fail=1', m.upstream_retry_fail === 1, String(m.upstream_retry_fail))
  })
  await scenario(ok, 'R3', ['midstream', 'ok'], {}, ({ r, got, m }) => {
    ok('R3 已开始回字节再断 ⇒ 不重发（上游只收到 1 发）', got.length === 1, String(got.length))
    ok('R3 retry_ok=0、midstream_abort=1', m.upstream_retry_ok === 0 && m.upstream_midstream_abort === 1, JSON.stringify([m.upstream_retry_ok, m.upstream_midstream_abort]))
    ok('R3 客户端连接被立刻断开（不挂着）', String(r.status).startsWith('conn_error') && r.ms < 2000, String(r.status) + ' ' + r.ms + 'ms')
  })
  await scenario(ok, 'R4', ['drop', 'ok'], { ZKD_RETRY_ONCE: '0' }, ({ r, got }) => {
    ok('R4 关掉开关 ⇒ 首次断就 502', r.status === 502, String(r.status))
    ok('R4 上游只收到 1 发', got.length === 1, String(got.length))
  })
}
module.exports = { run }
if (require.main === module) {
  let fail = 0
  run((n, c, d) => { console.log((c ? '  ✓ ' : '  ✗ ') + n + (c || !d ? '' : ' :: ' + d)); if (!c) fail++ }).then(() => process.exit(fail ? 1 : 0))
}
