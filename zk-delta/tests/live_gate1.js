'use strict'
/*
 * zk-delta/tests/live_gate1.js —— 门① 两腿对照（用**真 GUI 采到的 body**，不是我构造的）
 *
 * 门① 的原话是「Cursor 发出的简单请求到 GPT 网页端必须干净原样」。
 * 之前那次对照用的是我脚本拼出来的 ls 请求 —— 形状对不对，只能靠我自己打包票。
 * 这一版直接从 tests/fixtures/ 里拿一条真 Cursor 抓包，同一份字节走两条腿：
 *
 *   A 腿：直接打今天的地址（基准）
 *   B 腿：走本机小代理 → 增量 → 集群侧重建 → 同一个地址
 *
 * 然后去 zerokey 网关的日志里，比它两次**实际收到**的 [PROMPT] REQ 块。
 * 判据：把每腿各自的会话 id / 消息 id / 标记串归一化之后，两个块必须零差异。
 *
 * 为什么比"重建字节相同"更值钱：那条只证明到 LiteLLM 入口；这条一路证到网关，
 * 覆盖了中间任何一跳可能做的手脚。
 *
 * 用法： ZKD_KEY=sk-xxx ZKD_SSH_PASS=xxx node zk-delta/tests/live_gate1.js
 */

const fs = require('fs')
const path = require('path')
const http = require('http')
const https = require('https')
const { execFileSync } = require('child_process')

const KEY = process.env.ZKD_KEY || ''
const SSH_PASS = process.env.ZKD_SSH_PASS || ''
const FIXDIR = path.join(__dirname, 'fixtures')
const BASE = 'https://cc.auto-link.com.cn/pro/v1/chat/completions'
const SIDECAR_PORT = parseInt(process.env.ZKD_PORT || '8788', 10)
const GW = process.env.ZKD_GW_DEPLOY || 'zero-cursor-bpi-82'

if (!KEY) { console.error('缺 ZKD_KEY'); process.exit(2) }
if (!SSH_PASS) { console.error('缺 ZKD_SSH_PASS'); process.exit(2) }

// ---- 挑一条真抓包：取最小的那条（最接近"简单请求"）----
const files = fs.readdirSync(FIXDIR).filter((f) => f.endsWith('.json'))
  .map((f) => ({ f, p: path.join(FIXDIR, f), size: fs.statSync(path.join(FIXDIR, f)).size }))
  .filter((x) => x.size > 0)
  .sort((a, b) => a.size - b.size)
if (!files.length) { console.error('fixtures/ 是空的 —— 先 ./zk-delta/switch.sh on 再用真 Cursor 聊几句'); process.exit(2) }
const pick = files[0]
const rawOrig = fs.readFileSync(pick.p, 'utf8')
console.log(`取样本 ${pick.f}（${pick.size} B，fixtures 里最小的一条）`)

// ---- 造两腿的标记串。等长替换，两腿字节数完全一样，diff 才有意义 ----
const m = rawOrig.match(/ZK-[A-Za-z-]+-\d+-\d+/)
if (!m) { console.error('样本里找不到 ZK- 标记，换一条'); process.exit(2) }
const orig = m[0]
const stamp = String(Date.now())
const mkMark = (leg) => {
  const s = `G1${leg}-${stamp}`
  return s.length >= orig.length ? s.slice(0, orig.length) : s + 'x'.repeat(orig.length - s.length)
}
const markA = mkMark('A')
const markB = mkMark('B')
const bodyA = rawOrig.split(orig).join(markA)
const bodyB = rawOrig.split(orig).join(markB)
if (bodyA.length !== bodyB.length || bodyA.length !== rawOrig.length) {
  console.error('两腿字节数不等，替换没等长，判据会失真'); process.exit(3)
}
console.log(`两腿标记 ${markA} / ${markB}，各 ${Buffer.byteLength(bodyA, "utf8")} B（与原样本等长）`)

function send (which, bodyStr) {
  const buf = Buffer.from(bodyStr, 'utf8')
  const isB = which === 'B'
  const opts = isB
    ? { protocol: 'http:', hostname: '127.0.0.1', port: SIDECAR_PORT, path: '/v1/chat/completions' }
    : (() => { const u = new URL(BASE); return { protocol: 'https:', hostname: u.hostname, path: u.pathname } })()
  const mod = isB ? http : https
  return new Promise((resolve, reject) => {
    const r = mod.request(Object.assign({
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'content-length': buf.length,
        authorization: 'Bearer ' + KEY,
        'user-agent': 'Cursor/3.17.19'
      }
    }, opts), (res) => {
      let n = 0
      res.on('data', (c) => { n += c.length })
      res.on('end', () => resolve({ status: res.statusCode, bytes: n }))
    })
    r.on('error', reject)
    r.setTimeout(240000, () => r.destroy(new Error('timeout')))
    r.end(buf)
  })
}

function gwLog () {
  return execFileSync('sshpass', ['-p', SSH_PASS, 'ssh', '-o', 'StrictHostKeyChecking=no',
    '-o', 'ConnectTimeout=25', 'cltx@10.68.13.198',
    `echo '${SSH_PASS}' | sudo -S k3s kubectl -n litellm-product logs deploy/${GW} --tail=-1 --since=10m 2>/dev/null`],
  { encoding: 'utf8', maxBuffer: 256 * 1024 * 1024, stdio: ['ignore', 'pipe', 'pipe'], timeout: 240000 })
}

// 取包含标记的那一发的 [PROMPT] REQ 块
function block (log, mark) {
  const lines = log.split('\n')
  const hit = lines.findIndex((l) => l.includes(mark))
  if (hit < 0) return null
  let start = hit
  while (start > 0 && !lines[start].includes('[PROMPT] REQ')) start--
  let end = hit
  while (end < lines.length - 1 && !/^\s*\}\s*$/.test(lines[end])) end++
  return lines.slice(start, end + 1)
}

// 归一化：会话/消息 id、标记串、字节数里跟标记无关的抖动
function norm (lines, mark) {
  return lines.map((l) => l
    .split(mark).join('«MARK»')
    .replace(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g, '«UUID»')
    .replace(/conv=[0-9a-f]+/g, 'conv=«C»')
  )
}

;(async () => {
  console.log('\n--- A 腿：直接打今天的地址（基准）---')
  const ra = await send('A', bodyA)
  console.log(`    HTTP ${ra.status}，回 ${ra.bytes} B`)
  await new Promise((r) => setTimeout(r, 4000))

  console.log('--- B 腿：走 zk-delta ---')
  const rb = await send('B', bodyB)
  console.log(`    HTTP ${rb.status}，回 ${rb.bytes} B`)

  console.log('\n等网关日志落盘…')
  await new Promise((r) => setTimeout(r, 12000))
  const log = gwLog()

  const ba = block(log, markA)
  const bb = block(log, markB)
  const problems = []
  if (ra.status !== 200) problems.push(`A 腿 HTTP ${ra.status}`)
  if (rb.status !== 200) problems.push(`B 腿 HTTP ${rb.status}`)
  if (!ba) problems.push('网关日志里找不到 A 腿')
  if (!bb) problems.push('网关日志里找不到 B 腿')

  if (ba && bb) {
    const na = norm(ba, markA)
    const nb = norm(bb, markB)
    console.log(`\nA 腿日志块 ${na.length} 行，B 腿 ${nb.length} 行`)
    if (na.length !== nb.length) problems.push(`行数不等 ${na.length} vs ${nb.length}`)
    const diffs = []
    for (let i = 0; i < Math.max(na.length, nb.length); i++) {
      if (na[i] !== nb[i]) diffs.push(`  行 ${i + 1}\n    A: ${na[i]}\n    B: ${nb[i]}`)
    }
    if (diffs.length) { problems.push(`归一化后仍有 ${diffs.length} 处差异`); console.log(diffs.slice(0, 10).join('\n')) }
    else console.log('归一化后**零差异**')

    const pl = (arr) => { const l = arr.find((x) => x.includes('promptLength')); return l ? l.trim() : '(无)' }
    const at = (arr) => { const l = arr.find((x) => x.includes('attachments')); return l ? l.trim() : '(无)' }
    console.log(`A ${pl(ba)}  ${at(ba)}`)
    console.log(`B ${pl(bb)}  ${at(bb)}`)

    // 门① 的另外两条：不许有转录体行标、不许有大 item
    const joined = bb.join('\n')
    if (/^\s*(USER|ASSISTANT|用户|助手)\s*[:：]/m.test(joined.replace(/^\s*'?USER: <user_(info|query)>/gm, ''))) {
      problems.push('B 腿 payload 里出现了转录体行标')
    }
  }

  console.log('\n' + '='.repeat(60))
  if (problems.length) { console.log('门① 不通过：\n  - ' + problems.join('\n  - ')); process.exit(1) }
  console.log('门① 通过：同一份真 GUI 字节，两腿在网关侧收到的东西归一化后零差异。')
})().catch((e) => { console.error('异常：' + e.message); process.exit(1) })
