'use strict'
/*
 * zk-delta/sidecar/sidecar.js  —— 本机小代理
 *
 * Cursor 把 BYOK 地址指到 http://127.0.0.1:8788/v1，其余什么都不改。
 * 小代理负责：把这一轮相对上一轮新增的那几条消息挑出来，只把新增的发到 198。
 *
 * 三条硬规矩，写死在代码里：
 *
 *  1) 不能逐字节复现的请求，一律原样透传。
 *     每一发请求都先在本地做一次 拆开→重建→序列化 的往返，
 *     只有结果与 Cursor 原始字节完全相同，才允许走增量。
 *     不相同就当没有 zk-delta 这回事，原样发给今天的地址。门① 因此不可破。
 *
 *  2) 认不出基线一律硬报错，不静默降级。
 *     服务端回 409 时，小代理立刻重发全量并拿新 handle，同时两侧各记一条计数。
 *     绝不出现"服务端悄悄少发了历史、上游看到的东西变了、我们还以为一切正常"。
 *
 *  3) 会话身份靠全前缀摘要匹配，不靠任何有损哈希。
 *     Cursor 每条会话的第 0 条框架消息都一样，按它建 key 会让所有会话撞在一起。
 *
 * 环境变量：
 *   ZKD_PORT       本机监听端口，默认 8788
 *   ZKD_DELTA_URL  增量服务端地址（新加的 location）
 *   ZKD_UPSTREAM   今天在用的地址的 base（不含 /v1），回落时走它
 *   ZKD_OFF=1      整体关掉增量，退化成纯透传（等于今天的形态）
 *   ZKD_CAPTURE    设成目录名则把原始 body 落盘，用来做金样测试
 *   ZKD_CAPTURE_MAX      最多采几条，默认 40（金样几十条就够，别把磁盘写爆）
 *   ZKD_CAPTURE_MAX_MB   最多采多少 MB，默认 512
 *   ZKD_MAX_CONV   本地记多少条会话，默认 60
 */

const http = require('http')
const https = require('https')
const fs = require('fs')
const path = require('path')
const { URL } = require('url')
const F = require('../common/framing')

const PORT = parseInt(process.env.ZKD_PORT || '8788', 10)
const DELTA_URL = process.env.ZKD_DELTA_URL || 'https://cc.auto-link.com.cn/zkd/v1/delta'
const UPSTREAM = process.env.ZKD_UPSTREAM || 'https://cc.auto-link.com.cn/pro'
const OFF = process.env.ZKD_OFF === '1'
const CAPTURE = process.env.ZKD_CAPTURE || ''
// 采集必须封顶：真实 body 每条几 MB、Cursor 每轮一发，不封顶跑一天能把本机磁盘写爆。
// 而 ⑨ 那条金样测试几十条样本就够用了。
const CAP_MAX = parseInt(process.env.ZKD_CAPTURE_MAX || '40', 10)
// 封顶要按**磁盘上已有多少**算，不是按本进程采了多少。原先写 `capLeft = CAP_MAX`，
// 于是每次重启小代理额度就重新给满 40 —— 重启 N 次能攒 40N 条，封顶等于没封，
// 而封顶存在的理由（别把磁盘写爆）恰恰是跨重启才成立的。
let capLeft = CAP_MAX
if (CAPTURE) {
  try {
    const have = require('fs').readdirSync(CAPTURE).filter((f) => f.endsWith('.json')).length
    capLeft = Math.max(0, CAP_MAX - have)
  } catch (e) { /* 目录还不存在 = 一条都没采过，额度就是满的 */ }
}
let capBytesLeft = parseInt(process.env.ZKD_CAPTURE_MAX_MB || '512', 10) * 1048576
const MAX_CONV = parseInt(process.env.ZKD_MAX_CONV || '60', 10)

const dUrl = new URL(DELTA_URL)
const uUrl = new URL(UPSTREAM)

const M = {
  started_at: new Date().toISOString(),
  req_total: 0,
  capture_skipped_ua: 0,
  capture_skipped_synthetic: 0,
  delta_sent: 0,
  full_sent: 0,
  passthru: 0,
  fallback_by_reason: {},
  conflict_409: 0,
  conflict_by_reason: {},
  delta_server_error: 0,
  bytes_original: 0,   // Cursor 本来要发出去的字节
  bytes_uplink: 0,     // 实际出网字节
  conv_live: 0
}
function bump (o, k) { o[k] = (o[k] || 0) + 1 }
function log (ev, kv) {
  const p = ['[zkd-sidecar]', ev]
  for (const k of Object.keys(kv || {})) p.push(k + '=' + kv[k])
  console.log(p.join(' '))
}

// ---------- 本地会话表 ----------
/** handle -> { handle, digests, count, templateDigest, arrayKey, busy, ts } */
const convs = new Map()
function convList () { return Array.from(convs.values()) }
function trimConvs () {
  while (convs.size > MAX_CONV) {
    const k = convs.keys().next().value
    if (k === undefined) break
    convs.delete(k)
  }
  M.conv_live = convs.size
}

// ---------- 工具 ----------
function readBody (req) {
  return new Promise((resolve, reject) => {
    const cs = []
    req.on('data', (c) => cs.push(c))
    req.on('end', () => resolve(Buffer.concat(cs)))
    req.on('error', reject)
  })
}

function outHeaders (req, len, targetHost) {
  const h = {}
  for (const k of Object.keys(req.headers)) {
    const lk = k.toLowerCase()
    if (lk === 'host' || lk === 'content-length' || lk === 'connection' ||
        lk === 'transfer-encoding') continue
    h[k] = req.headers[k]
  }
  h['content-length'] = String(len)
  h['host'] = targetHost
  return h
}

function doRequest (target, method, headers, bodyBuf, onResponse, onError) {
  const mod = target.protocol === 'https:' ? https : http
  const r = mod.request({
    protocol: target.protocol,
    hostname: target.hostname,
    port: target.port || (target.protocol === 'https:' ? 443 : 80),
    method,
    path: target.pathname + (target.search || ''),
    headers
  }, onResponse)
  r.on('error', onError)
  M.bytes_uplink += bodyBuf.length
  r.end(bodyBuf)
  return r
}

function pipeBack (res, ures) {
  const h = Object.assign({}, ures.headers)
  delete h['transfer-encoding']
  delete h['connection']
  res.writeHead(ures.statusCode, h)
  ures.pipe(res)
}

// ---------- 原样透传（等于今天的形态） ----------
function passthru (req, res, subPath, raw, reason) {
  M.passthru++
  if (reason) { bump(M.fallback_by_reason, reason); log('passthru', { reason, bytes: raw.length, path: subPath }) }
  const t = new URL(uUrl.toString())
  t.pathname = uUrl.pathname.replace(/\/$/, '') + subPath
  doRequest(t, req.method, outHeaders(req, raw.length, t.host), raw,
    (ures) => pipeBack(res, ures),
    (e) => { log('passthru_error', { msg: e.message }); if (!res.headersSent) { res.writeHead(502, { 'content-type': 'application/json' }); res.end(JSON.stringify({ error: { message: 'zk-delta sidecar passthru failed: ' + e.message, code: '502' } })) } })
}

// ---------- 发一个 envelope ----------
function sendEnvelope (req, res, env, meta, onConflict) {
  const buf = Buffer.from(JSON.stringify(env), 'utf8')
  const h = outHeaders(req, buf.length, dUrl.host)
  h['content-type'] = 'application/json'
  h['x-zkd-client'] = 'sidecar/1'

  doRequest(dUrl, 'POST', h, buf, (ures) => {
    if (ures.statusCode === 409) {
      const cs = []
      ures.on('data', (c) => cs.push(c))
      ures.on('end', () => {
        let reason = 'unknown'
        try { reason = JSON.parse(Buffer.concat(cs).toString('utf8')).error.reason || 'unknown' } catch (e) {}
        M.conflict_409++
        bump(M.conflict_by_reason, reason)
        log('conflict409', { reason, handle: String(env.handle).slice(0, 12) })
        if (env.handle) convs.delete(env.handle)
        onConflict(reason)
      })
      return
    }
    if (ures.statusCode >= 500 && !res.headersSent) {
      M.delta_server_error++
      const cs = []
      ures.on('data', (c) => cs.push(c))
      ures.on('end', () => {
        log('delta_server_5xx', { code: ures.statusCode })
        passthru(req, res, meta.subPath, meta.raw, 'delta_server_5xx')
      })
      return
    }
    // 成功：记账并把响应原样回吐
    const nh = ures.headers['x-zk-handle']
    if (nh && ures.statusCode >= 200 && ures.statusCode < 300) {
      convs.delete(nh)
      convs.set(nh, {
        handle: nh,
        digests: meta.digests,
        count: meta.digests.length,
        templateDigest: meta.templateDigest,
        arrayKey: meta.arrayKey,
        ts: Date.now()
      })
      trimConvs()
    }
    if (meta.mode === 'delta') M.delta_sent++; else M.full_sent++
    log('turn', {
      mode: meta.mode,
      handle: String(nh || '').slice(0, 12),
      items: meta.digests.length,
      base: env.base_count || 0,
      orig: meta.raw.length,
      up: buf.length,
      save: meta.raw.length ? (100 - buf.length * 100 / meta.raw.length).toFixed(1) + '%' : '-',
      code: ures.statusCode
    })
    pipeBack(res, ures)
  }, (e) => {
    M.delta_server_error++
    log('delta_server_error', { msg: e.message })
    if (!res.headersSent) passthru(req, res, meta.subPath, meta.raw, 'delta_server_unreachable')
  })
}

// ---------- 主处理 ----------
async function handle (req, res) {
  const u = new URL(req.url, 'http://x')
  const p = u.pathname

  if (p === '/healthz') {
    res.writeHead(200, { 'content-type': 'application/json' })
    return res.end(JSON.stringify({ ok: true, off: OFF, delta_url: DELTA_URL, upstream: UPSTREAM, conv_live: convs.size }))
  }
  if (p === '/metrics.json') {
    M.conv_live = convs.size
    if (CAPTURE) {
      // 采集进度也露出来，否则"到底采够没有"只能去 ls 目录
      M.capture_dir = CAPTURE
      M.capture_taken = CAP_MAX - capLeft
      M.capture_left = capLeft
    }
    res.writeHead(200, { 'content-type': 'application/json' })
    return res.end(JSON.stringify(M, null, 2))
  }

  const isDeltaPath = req.method === 'POST' && /^\/v1\/(chat\/completions|responses)$/.test(p)
  const raw = await readBody(req)
  M.req_total++
  M.bytes_original += raw.length

  // ⑨ 要的是**真实 Cursor 抓包**金样，所以采集有三道门：
  //   1) UA 必须是 Cursor。之前没这道门，switch.sh on 里那条自检 curl（93 B 的 "hi"）
  //      也被采了进去，fixtures 里躺着三条我自己造的样本——拿它们让 ⑨ 转绿就是发假绿灯。
  //   2) 必须是有正文的聊天请求。之前没这道门，Cursor 启动时那发 GET /v1/models
  //      会落成一个 0 字节文件，⑨ 一 JSON.parse 就崩。
  //   3) 不许带 x-zkd-synthetic: 1。加这道门的原因：上面第 1 道门认的是 UA，
  //      而 tests/ 里每个 live 脚本都写死 'user-agent': 'Cursor/3.17.19'（为了走同一条
  //      代码路径），所以第 1 道门认不出我自己 —— 08-31 复查时 40 个采集额度里
  //      有 17 个是我自己的测试脚本吃掉的，把池子占满冻死，真 body 再也进不来。
  //      **门认的东西必须是被验对象无法满足的；UA 由我自己写，就不是那种东西。**
  const capUA = /Cursor/i.test(String(req.headers['user-agent'] || ''))
  const capSynthetic = String(req.headers['x-zkd-synthetic'] || '') === '1'
  const capShape = isDeltaPath && raw.length > 0
  if (CAPTURE && capShape && !capUA) M.capture_skipped_ua++
  if (CAPTURE && capShape && capSynthetic) M.capture_skipped_synthetic++
  if (CAPTURE && capUA && capShape && !capSynthetic && capLeft > 0 && capBytesLeft > 0) {
    try {
      fs.mkdirSync(CAPTURE, { recursive: true })
      const base = `${Date.now()}-${String(M.req_total).padStart(4, '0')}${p.replace(/\//g, '_')}.json`
      const fn = path.join(CAPTURE, base)
      fs.writeFileSync(fn, raw)
      // 出处在采集当场记下来。事后靠 body 大小猜是真抓包还是我自己造的，
      // 是猜；这一行才是证据。⑨ 只回灌 manifest 里 provenance=gui 的条目。
      let ntools = -1
      try { ntools = (JSON.parse(raw).tools || []).length } catch (e) {}
      fs.appendFileSync(path.join(CAPTURE, '_manifest.jsonl'), JSON.stringify({
        // provenance 只写小代理**真知道**的事：这一发过了采集门。它无法证明请求
        // 来自真 GUI（UA 可以伪造），写成 'gui' 就是没证据的断言。tools / bytes
        // 原样记下来，一条 93B/tools:0 的东西混进来时人一眼能看见。
        file: base, bytes: raw.length, provenance: 'gate_passed',
        ua: String(req.headers['user-agent'] || ''), tools: ntools,
        ts: new Date().toISOString()
      }) + '\n')
      capLeft--
      capBytesLeft -= raw.length
      if (capLeft === 0 || capBytesLeft <= 0) {
        // 采够了就自己停手。真实 body 每条几 MB，Cursor 每轮一发，
        // 不封顶的话跑一天能把本机磁盘写爆——而金样测试几十条就够了。
        log('capture_done', { dir: CAPTURE, files: CAP_MAX - capLeft, why: capLeft === 0 ? 'file_cap' : 'byte_cap' })
      }
    } catch (e) { log('capture_error', { msg: e.message }) }
  }

  if (OFF || !isDeltaPath) return passthru(req, res, p + (u.search || ''), raw, OFF ? 'off' : null)

  // ---- 第 1 环：本地自证往返 ----
  const proof = F.proveRoundTrip(raw)
  if (!proof.ok) return passthru(req, res, p, raw, 'roundtrip_' + proof.reason)

  const digests = F.itemDigests(proof.items)
  const tdig = F.templateDigest(proof.template)

  const sendFull = (why) => {
    const env = {
      v: F.PROTO_VERSION,
      path: p,
      array_key: proof.arrayKey,
      handle: null,
      base_count: 0,
      base_digest: F.prefixDigest([], 0),
      template_digest: tdig,
      template: proof.template,
      delta: proof.items,
      expect_bytes: raw.length
    }
    sendEnvelope(req, res, env,
      { mode: 'full', raw, subPath: p, digests, templateDigest: tdig, arrayKey: proof.arrayKey },
      () => passthru(req, res, p, raw, 'full_also_409'))
    if (why) log('full_because', { why })
  }

  // ---- 找最长前缀会话 ----
  const cand = convList().filter((c) => c.arrayKey === proof.arrayKey && !c.busy)
  const best = F.findLongestPrefix(cand, digests)
  if (!best) return sendFull('no_prefix_match')

  const env = {
    v: F.PROTO_VERSION,
    path: p,
    array_key: proof.arrayKey,
    handle: best.handle,
    base_count: best.count,
    base_digest: F.prefixDigest(digests, best.count),
    template_digest: tdig,
    delta: proof.items.slice(best.count),
    expect_bytes: raw.length
  }
  // template 变了才带上（工具集/参数变化时）
  if (tdig !== best.templateDigest) env.template = proof.template

  best.busy = true
  const clearBusy = () => { const c = convs.get(best.handle); if (c) c.busy = false }
  res.on('close', clearBusy)
  res.on('finish', clearBusy)

  sendEnvelope(req, res, env,
    { mode: 'delta', raw, subPath: p, digests, templateDigest: tdig, arrayKey: proof.arrayKey },
    (reason) => { clearBusy(); sendFull('after_409_' + reason) })
}

const server = http.createServer((req, res) => {
  handle(req, res).catch((e) => {
    log('handler_error', { msg: e.message })
    if (!res.headersSent) { res.writeHead(500, { 'content-type': 'application/json' }); res.end(JSON.stringify({ error: { message: 'zk-delta sidecar: ' + e.message, code: '500' } })) }
  })
})
server.requestTimeout = 0
server.headersTimeout = 0
server.timeout = 0

if (require.main === module) {
  server.listen(PORT, '127.0.0.1', () => {
    log('listen', { port: PORT, delta: DELTA_URL, upstream: UPSTREAM, off: OFF, capture: CAPTURE || '-' })
  })
}

module.exports = { server, M, convs }
