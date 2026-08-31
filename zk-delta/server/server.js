'use strict'
/*
 * zk-delta/server/server.js  —— 集群侧的增量终结点
 *
 * 职责：收增量 → 重建出与 Cursor 原始 body 逐字节相同的全量 → 原样转发给 LiteLLM
 *       → 把上游响应（含 SSE 流）原样回吐。
 *
 * 它对上游（LiteLLM、zerokey 网关、GPT 网页）是完全透明的：上游收到的字节
 * 和今天没有 zk-delta 时收到的字节一模一样。所以门① 不可能被它破坏。
 *
 * 失效一律硬报错 409 delta_base_not_found，绝不"当全量转发"。
 * 依据：静默失忆正是 08-30 那次翻车的根因。
 *
 * 环境变量：
 *   ZKD_PORT           监听端口，默认 8788
 *   ZKD_UPSTREAM       上游 base，默认 http://litellm-proxy.litellm-product.svc.cluster.local:4000
 *   ZKD_MAX_CONV       最多缓存多少条会话，默认 400
 *   ZKD_MAX_BYTES      会话缓存总字节上限，默认 800MB
 *   ZKD_TTL_MIN        会话闲置多久淘汰，默认 240 分钟
 */

const http = require('http')
const https = require('https')
const crypto = require('crypto')
const { URL } = require('url')
const F = require('../common/framing')

const PORT = parseInt(process.env.ZKD_PORT || '8788', 10)
const UPSTREAM = process.env.ZKD_UPSTREAM || 'http://litellm-proxy.litellm-product.svc.cluster.local:4000'
const MAX_CONV = parseInt(process.env.ZKD_MAX_CONV || '400', 10)
const MAX_BYTES = parseInt(process.env.ZKD_MAX_BYTES || String(800 * 1024 * 1024), 10)
const TTL_MS = parseInt(process.env.ZKD_TTL_MIN || '240', 10) * 60 * 1000

const up = new URL(UPSTREAM)
const upMod = up.protocol === 'https:' ? https : http

// ---------- 计数器（G4：每一次降级都要有数） ----------
const M = {
  started_at: new Date().toISOString(),
  req_total: 0,
  req_delta: 0,
  req_full: 0,
  rebuild_ok: 0,
  reject_409: 0,
  reject_by_reason: {},
  upstream_2xx: 0,
  upstream_non2xx: 0,
  // 非 2xx 不能一锅炖。401/403 是客户端凭据问题，我们只是忠实透传，不算 zk-delta 的毛病；
  // 400 才是要盯死的那个形状（Cursor 长会话撞入口闸门那条线）；5xx 是真出事。
  // 混成一个数的后果：巡检见 401 就报漂移，喊几次狼之后就没人看了。
  upstream_by_status: {},
  bytes_in: 0,          // 客户端 → 本服务（广域网这一跳）
  bytes_out: 0,         // 本服务 → LiteLLM（集群内这一跳）
  conv_live: 0,
  conv_evicted: 0
}
function bump (obj, k) { obj[k] = (obj[k] || 0) + 1 }

// ---------- 会话存储 ----------
/** handle -> { items, template, templateDigest, digests, arrayKey, authHash, bytes, ts } */
const store = new Map()
let storeBytes = 0

function evictIfNeeded () {
  const now = Date.now()
  for (const [h, s] of store) {
    if (now - s.ts > TTL_MS) { storeBytes -= s.bytes; store.delete(h); M.conv_evicted++ }
  }
  // Map 保插入序；重新 set 会把条目移到尾部，所以队首就是最久没用的
  while (store.size > MAX_CONV || storeBytes > MAX_BYTES) {
    const k = store.keys().next().value
    if (k === undefined) break
    storeBytes -= store.get(k).bytes
    store.delete(k)
    M.conv_evicted++
  }
  M.conv_live = store.size
}

function touch (h) {
  const s = store.get(h)
  if (s) { store.delete(h); s.ts = Date.now(); store.set(h, s) }
  return s
}

function newHandle () { return 'zd_' + crypto.randomBytes(16).toString('hex') }
function authHashOf (req) {
  return F.sha256hex(String(req.headers['authorization'] || '')).slice(0, 32)
}

// ---------- 工具 ----------
function readBody (req, cap) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let n = 0
    req.on('data', (c) => {
      n += c.length
      if (cap && n > cap) { reject(new Error('body_too_large')); req.destroy(); return }
      chunks.push(c)
    })
    req.on('end', () => resolve(Buffer.concat(chunks)))
    req.on('error', reject)
  })
}

function sendJson (res, code, obj) {
  const b = Buffer.from(JSON.stringify(obj), 'utf8')
  res.writeHead(code, { 'content-type': 'application/json', 'content-length': b.length })
  res.end(b)
}

function reject409 (res, reason, detail) {
  M.reject_409++
  bump(M.reject_by_reason, reason)
  log('reject', { reason, detail: detail || '' })
  sendJson(res, 409, {
    error: {
      type: 'delta_base_not_found',
      reason,
      detail: detail || '',
      message: 'zk-delta 认不出这个增量基线，客户端必须重发全量。本服务绝不把认不出的增量当全量转发。',
      code: '409'
    }
  })
}

function log (ev, kv) {
  const parts = ['[zkd]', ev]
  for (const k of Object.keys(kv || {})) parts.push(k + '=' + kv[k])
  console.log(parts.join(' '))
}

// ---------- 转发 ----------
function forward (req, res, path, bodyBuf, extraRespHeaders) {
  const headers = {}
  for (const k of Object.keys(req.headers)) {
    const lk = k.toLowerCase()
    if (lk === 'host' || lk === 'content-length' || lk === 'connection' ||
        lk === 'transfer-encoding' || lk.startsWith('x-zk-')) continue
    headers[k] = req.headers[k]
  }
  headers['content-length'] = String(bodyBuf.length)
  headers['host'] = up.host

  M.bytes_out += bodyBuf.length

  const opts = {
    protocol: up.protocol,
    hostname: up.hostname,
    port: up.port || (up.protocol === 'https:' ? 443 : 80),
    method: req.method,
    path: (up.pathname === '/' ? '' : up.pathname.replace(/\/$/, '')) + path,
    headers
  }

  const ureq = upMod.request(opts, (ures) => {
    if (ures.statusCode >= 200 && ures.statusCode < 300) M.upstream_2xx++
    else { M.upstream_non2xx++; bump(M.upstream_by_status, String(ures.statusCode)) }
    const h = Object.assign({}, ures.headers, extraRespHeaders || {})
    delete h['transfer-encoding']
    delete h['connection']
    res.writeHead(ures.statusCode, h)
    ures.pipe(res)
  })
  ureq.on('error', (e) => {
    log('upstream_error', { msg: e.message })
    if (!res.headersSent) sendJson(res, 502, { error: { type: 'zkd_upstream_error', message: e.message, code: '502' } })
    else res.end()
  })
  ureq.end(bodyBuf)
  return ureq
}

// ---------- 主处理 ----------
async function handleDelta (req, res) {
  let envBuf
  try { envBuf = await readBody(req, 64 * 1024 * 1024) } catch (e) {
    return sendJson(res, 413, { error: { type: 'zkd_body_too_large', message: e.message, code: '413' } })
  }
  M.bytes_in += envBuf.length
  M.req_total++

  let env
  try { env = JSON.parse(envBuf.toString('utf8')) } catch (e) {
    return reject409(res, 'envelope_not_json')
  }
  if (env.v !== F.PROTO_VERSION) return reject409(res, 'proto_version_mismatch', String(env.v))

  const path = String(env.path || '/v1/chat/completions')
  if (!/^\/v1\/(chat\/completions|responses)$/.test(path)) {
    return reject409(res, 'path_not_allowed', path)
  }
  const arrayKey = String(env.array_key || 'messages')
  if (arrayKey !== 'messages' && arrayKey !== 'input') return reject409(res, 'array_key_not_allowed', arrayKey)

  const ah = authHashOf(req)
  const delta = Array.isArray(env.delta) ? env.delta : null
  if (!delta) return reject409(res, 'delta_not_array')

  let items, template, tdig, handle

  if (!env.handle) {
    // ---- 全量首轮：客户端必须带 template ----
    M.req_full++
    if (!env.template || typeof env.template !== 'object') return reject409(res, 'template_missing_on_full')
    template = env.template
    tdig = F.templateDigest(template)
    if (env.template_digest && env.template_digest !== tdig) {
      return reject409(res, 'template_digest_mismatch')
    }
    items = delta
    handle = newHandle()
  } else {
    // ---- 增量轮 ----
    M.req_delta++
    handle = String(env.handle)
    const s = touch(handle)
    if (!s) return reject409(res, 'handle_unknown', handle)
    if (s.authHash !== ah) return reject409(res, 'handle_auth_mismatch', handle)
    if (s.arrayKey !== arrayKey) return reject409(res, 'array_key_changed', handle)

    const baseCount = Number(env.base_count)
    if (!Number.isInteger(baseCount) || baseCount !== s.items.length) {
      return reject409(res, 'base_count_mismatch', `client=${env.base_count} server=${s.items.length}`)
    }
    const myPrefix = F.prefixDigest(s.digests, baseCount)
    if (myPrefix !== env.base_digest) {
      return reject409(res, 'base_digest_mismatch', `client=${String(env.base_digest).slice(0, 16)} server=${myPrefix.slice(0, 16)}`)
    }

    // template 可以中途变（Cursor 换了工具集/参数），变了就必须重新带上来
    if (env.template_digest && env.template_digest === s.templateDigest) {
      template = s.template
      tdig = s.templateDigest
    } else {
      if (!env.template || typeof env.template !== 'object') {
        return reject409(res, 'template_changed_but_absent',
          `client=${String(env.template_digest).slice(0, 16)} server=${String(s.templateDigest).slice(0, 16)}`)
      }
      template = env.template
      tdig = F.templateDigest(template)
      if (env.template_digest && env.template_digest !== tdig) return reject409(res, 'template_digest_mismatch')
    }

    items = s.items.concat(delta)
  }

  // ---- 重建。这一行的输出就是 LiteLLM 会收到的字节 ----
  const fullStr = JSON.stringify(F.rebuildBody(template, arrayKey, items))
  const fullBuf = Buffer.from(fullStr, 'utf8')
  M.rebuild_ok++

  // 客户端自证过的字节数，用来做端到端核对（可选字段）
  if (env.expect_bytes != null && Number(env.expect_bytes) !== fullBuf.length) {
    return reject409(res, 'rebuilt_size_mismatch', `client=${env.expect_bytes} server=${fullBuf.length}`)
  }

  const digests = F.itemDigests(items)
  const newCount = items.length

  log('turn', {
    handle: handle.slice(0, 12),
    mode: env.handle ? 'delta' : 'full',
    base: env.handle ? env.base_count : 0,
    delta_items: delta.length,
    total_items: newCount,
    up_in: envBuf.length,
    up_out: fullBuf.length,
    ratio: envBuf.length ? (fullBuf.length / envBuf.length).toFixed(1) + 'x' : '-'
  })

  // 只在上游返回 2xx 之后才提交会话状态；上游失败则两侧都不前进，
  // 下一发的前缀校验会自动把两侧拉回一致（对不上就 409 → 客户端全量）。
  const ureq = forward(req, res, path, fullBuf, {
    'x-zk-handle': handle,
    'x-zk-count': String(newCount),
    'x-zk-rebuilt-bytes': String(fullBuf.length)
  })
  ureq.on('response', (ures) => {
    if (ures.statusCode >= 200 && ures.statusCode < 300) {
      const old = store.get(handle)
      if (old) storeBytes -= old.bytes
      const bytes = fullBuf.length
      store.delete(handle)
      store.set(handle, { items, template, templateDigest: tdig, digests, arrayKey, authHash: ah, bytes, ts: Date.now() })
      storeBytes += bytes
      evictIfNeeded()
    }
  })
}

// ---------- HTTP 服务 ----------
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://x')
  // nginx 用 location ^~ /zkd/ 转过来，路径上带着 /zkd 前缀。
  // 健康检查和指标两种写法都认，免得挂探针时还要记住带不带前缀。
  const bare = u.pathname.startsWith('/zkd/') ? u.pathname.slice(4) : u.pathname
  if (bare === '/healthz') {
    return sendJson(res, 200, { ok: true, proto: F.PROTO_VERSION, conv_live: store.size, store_bytes: storeBytes })
  }
  if (bare === '/metrics.json') {
    M.conv_live = store.size
    return sendJson(res, 200, Object.assign({}, M, { store_bytes: storeBytes }))
  }
  if (bare === '/metrics') {
    M.conv_live = store.size
    const lines = []
    for (const k of Object.keys(M)) {
      if (typeof M[k] === 'number') lines.push(`zkd_${k} ${M[k]}`)
    }
    for (const k of Object.keys(M.reject_by_reason)) lines.push(`zkd_reject{reason="${k}"} ${M.reject_by_reason[k]}`)
    lines.push(`zkd_store_bytes ${storeBytes}`)
    const b = Buffer.from(lines.join('\n') + '\n', 'utf8')
    res.writeHead(200, { 'content-type': 'text/plain; version=0.0.4', 'content-length': b.length })
    return res.end(b)
  }
  if (req.method === 'POST' && u.pathname === '/zkd/v1/delta') {
    return handleDelta(req, res).catch((e) => {
      log('handler_error', { msg: e.message })
      if (!res.headersSent) sendJson(res, 500, { error: { type: 'zkd_internal', message: e.message, code: '500' } })
    })
  }
  // 其余路径原样透传给 LiteLLM，方便把整个 zk-delta 当唯一入口
  readBody(req, 256 * 1024 * 1024).then((b) => {
    M.bytes_in += b.length
    forward(req, res, u.pathname + u.search, b, {})
  }).catch(() => {
    if (!res.headersSent) sendJson(res, 413, { error: { type: 'zkd_body_too_large', code: '413' } })
  })
})

server.requestTimeout = 0
server.headersTimeout = 0
server.timeout = 0
server.keepAliveTimeout = 75000

if (require.main === module) {
  server.listen(PORT, '0.0.0.0', () => {
    log('listen', { port: PORT, upstream: UPSTREAM, max_conv: MAX_CONV, ttl_min: TTL_MS / 60000 })
  })
  setInterval(evictIfNeeded, 60000).unref()
}

module.exports = { server, M, store }
