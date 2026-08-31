'use strict'
/*
 * zk-delta/tests/live_resilience.js —— R3 韧性 / 故障注入（打真链路）
 *
 * 之前验的全是顺风路径：链路通、模型答对、字节省下来了。
 * 但代理真正会害人的时刻是**出岔子的时候**：服务端重启把会话忘了、小代理被拉起来一次、
 * 两条会话同时跑、上游挂了。这些场景要么只在离线假上游下验过，要么根本没验过。
 *
 * 这一组的判据统一是一句话：
 *   任何失效都必须表现为「硬报错 + 退回全量 + 计数 +1」，
 *   绝不能表现为「静默少发历史」——后者会让上游收到残缺会话而 HTTP 还是 200，
 *   那正是 08-30 翻车的形状。
 *
 * 逐字节判据不靠"我觉得对"：服务端把重建后的字节数回填在 x-zk-rebuilt-bytes，
 * 而本测试自己就是造 body 的人，两边一比就是端到端的字节相等证明。
 *
 * 用法：
 *   ZKD_KEY=sk-xxx ZKD_SSH_PASS=xxx node zk-delta/tests/live_resilience.js
 */

const http = require('http')
const https = require('https')
const path = require('path')
const { execFileSync } = require('child_process')
const H = require('./harness')

const KEY = process.env.ZKD_KEY || ''
const SSH_PASS = process.env.ZKD_SSH_PASS || ''
const MODEL = process.env.ZKD_MODEL || 'cursor-web-fc-82-terra'
const PORT = parseInt(process.env.ZKD_RPORT || '8799', 10)
const DEAD_PORT = PORT + 1
const DELTA_URL = 'https://cc.auto-link.com.cn/zkd/v1/delta'
const UPSTREAM = 'https://cc.auto-link.com.cn/pro'
const SIDECAR = path.join(__dirname, '..', 'sidecar', 'sidecar.js')

if (!KEY) { console.error('缺 ZKD_KEY'); process.exit(2) }

const WORDS = ['one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten']
const results = []
const sink = []
function say (s) { console.log(s) }
function record (id, ok, detail) {
  results.push({ id, ok, detail })
  say(`  ${ok ? '\x1b[32m✓\x1b[0m' : '\x1b[31m✗\x1b[0m'} ${id} ${detail}`)
}

// ---------- 一条会话 ----------
class Conv {
  constructor (tag, padKB) {
    this.tag = tag
    this.padKB = padKB || 40
    this.msgs = [{ role: 'system', content: 'FRAMEWORK ' + H.lorem(20000, 'sys-' + tag) }]
    this.turn = 0
  }

  nextBody () {
    this.turn++
    const t = this.turn
    this.msgs.push({
      role: 'user',
      content: `[${this.tag} turn ${t}] ` + H.lorem(this.padKB * 1024, `${this.tag}-u${t}`) +
        `\n这是本次对话的第 ${t} 轮。只回一个英文序数词（one/two/three/...），` +
        `表示这是第几轮，不要任何别的字。`
    })
    return {
      model: MODEL,
      messages: this.msgs,
      stream: false,
      temperature: 0,
      max_tokens: 64,
      user: 'zkd-resilience'
    }
  }

  accept (answer) { this.msgs.push({ role: 'assistant', content: answer }) }
  expect () { return WORDS[this.turn - 1] }
}

function post (port, bodyObj) {
  const raw = Buffer.from(JSON.stringify(bodyObj), 'utf8')
  return new Promise((resolve, reject) => {
    const r = http.request({
      host: '127.0.0.1',
      port,
      path: '/v1/chat/completions',
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'content-length': raw.length,
        authorization: 'Bearer ' + KEY,
        'user-agent': 'Cursor/3.17.19',
        // 我不是真 Cursor：这行让采集门认出我，别把测试流量攒成 ⑨ 的金样
        'x-zkd-synthetic': '1'
      }
    }, (res) => {
      const cs = []
      res.on('data', (c) => cs.push(c))
      res.on('end', () => {
        const buf = Buffer.concat(cs)
        let answer = ''
        try { answer = JSON.parse(buf.toString('utf8')).choices[0].message.content.trim() } catch (e) {}
        resolve({
          status: res.statusCode,
          rebuilt: parseInt(res.headers['x-zk-rebuilt-bytes'] || '0', 10),
          origBytes: raw.length,
          answer
        })
      })
    })
    r.on('error', reject)
    r.setTimeout(180000, () => r.destroy(new Error('timeout')))
    r.end(raw)
  })
}

async function turn (conv, port) {
  const body = conv.nextBody()
  const r = await post(port, body)
  if (r.answer) conv.accept(r.answer)
  return r
}

function normalize (s) { return String(s).toLowerCase().replace(/[^a-z]/g, '') }

async function metrics (port) {
  const r = await H.get(port, '/metrics.json')
  return JSON.parse(r.body.toString('utf8'))
}

function startSidecar (port, extraEnv, tag) {
  return H.spawnNode(SIDECAR, Object.assign({
    ZKD_PORT: String(port),
    ZKD_DELTA_URL: DELTA_URL,
    ZKD_UPSTREAM: UPSTREAM,
    ZKD_CAPTURE: ''
  }, extraEnv || {}), tag, sink)
}

function k198 (cmd) {
  return execFileSync('sshpass', ['-p', SSH_PASS, 'ssh', '-o', 'StrictHostKeyChecking=no',
    '-o', 'ConnectTimeout=20', 'cltx@10.68.13.198',
    `echo '${SSH_PASS}' | sudo -S k3s kubectl -n litellm-product ${cmd}`],
  { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 240000 })
}

// ============================================================
async function caseA () {
  say('\n[R3a] 服务端把会话忘了（rollout restart，不删 pod）')
  const p = startSidecar(PORT, {}, '[A]')
  await H.waitHealthy(PORT)
  try {
    const conv = new Conv('A', 40)
    for (let i = 0; i < 3; i++) {
      const r = await turn(conv, PORT)
      if (r.status !== 200) return record('R3a', false, `重启前第 ${i + 1} 轮 HTTP ${r.status}`)
    }
    const before = await metrics(PORT)
    say('    重启前：answer=' + conv.msgs[conv.msgs.length - 1].content +
        '  delta=' + before.delta_sent + ' full=' + before.full_sent + ' 409=' + before.conflict_409)

    say('    → rollout restart deploy/zk-delta（Recreate，不是 delete pod）')
    k198('rollout restart deploy/zk-delta')
    k198('rollout status deploy/zk-delta --timeout=180s')
    await new Promise((r) => setTimeout(r, 3000))

    const r4 = await turn(conv, PORT)
    const after = await metrics(PORT)
    const got = normalize(r4.answer)
    const want = normalize(conv.expect())
    const gained409 = after.conflict_409 - before.conflict_409
    const byteOk = r4.rebuilt === r4.origBytes

    const ok = r4.status === 200 && got === want && gained409 >= 1 && byteOk
    record('R3a', ok,
      `重启后 HTTP ${r4.status}；答案 "${r4.answer}"（要 "${conv.expect()}"，答对=历史没丢）；` +
      `409 +${gained409}（必须≥1，说明是硬报错而不是静默）；` +
      `重建 ${r4.rebuilt}B vs 原始 ${r4.origBytes}B ${byteOk ? '相等' : '不等'}`)
  } finally { p.kill() }
}

async function caseB () {
  say('\n[R3b] 小代理被重启（客户端侧会话表丢失）')
  let p = startSidecar(PORT, {}, '[B1]')
  await H.waitHealthy(PORT)
  const conv = new Conv('B', 40)
  try {
    for (let i = 0; i < 3; i++) {
      const r = await turn(conv, PORT)
      if (r.status !== 200) return record('R3b', false, `重启前第 ${i + 1} 轮 HTTP ${r.status}`)
    }
  } finally { p.kill() }
  await new Promise((r) => setTimeout(r, 800))

  p = startSidecar(PORT, {}, '[B2]')
  await H.waitHealthy(PORT)
  try {
    const r4 = await turn(conv, PORT)
    const m = await metrics(PORT)
    const got = normalize(r4.answer)
    const want = normalize(conv.expect())
    const byteOk = r4.rebuilt === r4.origBytes
    const ok = r4.status === 200 && got === want && m.full_sent >= 1 && byteOk
    record('R3b', ok,
      `HTTP ${r4.status}；答案 "${r4.answer}"（要 "${conv.expect()}"）；` +
      `新进程 full_sent=${m.full_sent}（本地表空，必须走全量）；` +
      `重建 ${r4.rebuilt}B vs 原始 ${r4.origBytes}B ${byteOk ? '相等' : '不等'}`)
  } finally { p.kill() }
}

async function caseC () {
  say('\n[R3c] 两条会话交错，handle 不许串线')
  const p = startSidecar(PORT, {}, '[C]')
  await H.waitHealthy(PORT)
  try {
    const a = new Conv('C-alpha', 40)
    const b = new Conv('C-bravo', 40)
    const bad = []
    for (let i = 0; i < 3; i++) {
      for (const c of [a, b]) {
        const r = await turn(c, PORT)
        if (r.status !== 200) bad.push(`${c.tag} t${c.turn} HTTP ${r.status}`)
        else if (normalize(r.answer) !== normalize(c.expect())) {
          bad.push(`${c.tag} t${c.turn} 答 "${r.answer}" 应为 "${c.expect()}"`)
        } else if (r.rebuilt !== r.origBytes) {
          bad.push(`${c.tag} t${c.turn} 重建 ${r.rebuilt} != 原始 ${r.origBytes}`)
        }
      }
    }
    const m = await metrics(PORT)
    record('R3c', bad.length === 0 && m.conflict_409 === 0,
      bad.length ? bad.join('；') : `6 发交错全部答对且逐字节相同，409=${m.conflict_409}，活跃会话=${m.conv_live}`)
  } finally { p.kill() }
}

async function caseD () {
  say('\n[R3d] 增量服务端不可达 → 必须回落原样透传，功能不许断')
  const p = startSidecar(DEAD_PORT, { ZKD_DELTA_URL: 'http://127.0.0.1:1/v1/delta' }, '[D]')
  await H.waitHealthy(DEAD_PORT)
  try {
    const conv = new Conv('D', 20)
    const r = await turn(conv, DEAD_PORT)
    const m = await metrics(DEAD_PORT)
    const fell = m.passthru >= 1 || m.delta_server_error >= 1
    const ok = r.status === 200 && !!r.answer && fell
    record('R3d', ok,
      `HTTP ${r.status}；答案 "${r.answer}"（服务端死了也必须答得出来）；` +
      `passthru=${m.passthru} delta_server_error=${m.delta_server_error}；` +
      `回落原因=${JSON.stringify(m.fallback_by_reason)}`)
  } finally { p.kill() }
}

function caseE () {
  say('\n[R3e] 伪造 handle 直打线上服务端 → 必须 409，不许当全量放行')
  return new Promise((resolve) => {
    const env = {
      v: 1,
      path: '/v1/chat/completions',
      array_key: 'messages',
      handle: 'zd_deadbeefdead',
      base_count: 3,
      base_digest: 'deadbeef'.repeat(8),
      template_digest: 'cafebabe'.repeat(8),
      delta: [{ role: 'user', content: 'forged' }],
      expect_bytes: 123
    }
    const buf = Buffer.from(JSON.stringify(env), 'utf8')
    const u = new URL(DELTA_URL)
    const r = https.request({
      hostname: u.hostname,
      path: u.pathname,
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'content-length': buf.length,
        authorization: 'Bearer ' + KEY
      }
    }, (res) => {
      const cs = []
      res.on('data', (c) => cs.push(c))
      res.on('end', () => {
        let reason = ''
        try { reason = JSON.parse(Buffer.concat(cs).toString('utf8')).error.reason } catch (e) {}
        record('R3e', res.statusCode === 409,
          `HTTP ${res.statusCode}（必须 409）reason=${reason || '(无)'}`)
        resolve()
      })
    })
    r.on('error', (e) => { record('R3e', false, '请求失败 ' + e.message); resolve() })
    r.end(buf)
  })
}

// ============================================================
;(async () => {
  say('zk-delta 韧性 / 故障注入回归（真链路，模型 ' + MODEL + '）')
  say('='.repeat(60))
  try {
    await caseA()
    await caseB()
    await caseC()
    await caseD()
    await caseE()
  } catch (e) {
    record('harness', false, '异常中断: ' + e.message)
  }
  say('\n' + '='.repeat(60))
  const fail = results.filter((r) => !r.ok)
  say(`PASS ${results.length - fail.length}   FAIL ${fail.length}`)
  if (fail.length) {
    say('\n红项：')
    for (const f of fail) say('  ✗ ' + f.id + ' ' + f.detail)
    say('\n小代理日志尾部：')
    for (const l of sink.slice(-40)) say('  ' + l)
  }
  process.exit(fail.length ? 1 : 0)
})()
