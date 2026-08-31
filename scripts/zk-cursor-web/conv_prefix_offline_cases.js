#!/usr/bin/env node
/* conv_prefix_offline_cases.js — 会话复用「最长严格前缀匹配」离线单测。
 *
 * 不重实现:用括号配平从 responses.js 里逐字抠出 findConvSession / saveConvSession 的
 * 真实字节,并按定位串抠出 handler 里那段 instructions 补发逻辑,注入依赖后 eval 出来驱动。
 * 测的是上线那份代码本身,不是镜像。
 *
 * 覆盖:
 *   ① instructions 漂移仍命中(本次 bug 的正脸)
 *   ② 不同会话不撞车(第 0 条相同、第 1 条起分叉)
 *   ③ 多个候选取最长前缀
 *   ④ 历史被改过 → miss
 *   ⑤ 非严格前缀(等长/更短)→ miss
 *   ⑥ TTL 过期 → miss 且过期条目被清
 *   ⑦ instrChanged 标志三态(变/没变/老条目无该字段)
 *   ⑧ 同一 convId 原地更新,不占多个槽
 *   ⑨ LRU 上限 200
 *   ⑩ ZK_CONV_REUSE=0 关闸(find 与 save 都不动)
 *   ⑪ miss 日志能区分原因(stale / notprefix / mismatch)
 *   ⑫ instructions 补发:没变→不带;变了且短→带;变了且超长→退回全量(convSess 置空)
 *   ⑬ 等长严格前缀平局 → 取最近使用的那条(长度仍优先于新旧)
 *
 * 用法: node scripts/zk-cursor-web/conv_prefix_offline_cases.js [/path/to/responses.js]
 */
'use strict'
const fs = require('fs')
const crypto = require('crypto')

const SRC = process.argv[2] || '/tmp/resp_new_82.js'
const code = fs.readFileSync(SRC, 'utf8')

function grabFn(name) {
  const start = code.indexOf('function ' + name + '(')
  if (start < 0) throw new Error('cannot find function ' + name + ' in ' + SRC)
  const open = code.indexOf('{', start)
  let depth = 0
  for (let i = open; i < code.length; i++) {
    if (code[i] === '{') depth++
    else if (code[i] === '}') { depth--; if (depth === 0) return code.slice(start, i + 1) }
  }
  throw new Error('unbalanced braces for ' + name)
}
function grabBetween(a, b) {
  const s = code.indexOf(a)
  if (s < 0) throw new Error('cannot find marker: ' + a)
  const e = code.indexOf(b, s)
  if (e < 0) throw new Error('cannot find end marker: ' + b)
  return code.slice(s, e)
}

const findSrc = grabFn('findConvSession')
const saveSrc = grabFn('saveConvSession')
const instrSrc = grabBetween('const _instrMax = parseInt(process.env.ZK_CONV_INSTR_MAX',
                             'const basePrompt = convSess')

const TTL_MS = 240 * 60 * 1000

function build(env) {
  const cache = new Map()
  const logs = []
  const fakeConsole = { log: (s) => logs.push(String(s)) }
  const fakeProcess = { env: Object.assign({}, env) }
  const itemDigest = (x) => {
    try { return crypto.createHash('sha1').update(typeof x === 'string' ? x : JSON.stringify(x)).digest('hex') }
    catch (_) { return 'x' }
  }
  // eslint-disable-next-line no-new-func
  const factory = new Function(
    '_convCache', '_CONV_TTL_MS', '_itemDigest', 'console', 'process', '_persistConvCache',
    findSrc + '\n' + saveSrc + '\nreturn { findConvSession, saveConvSession }'
  )
  const api = factory(cache, TTL_MS, itemDigest, fakeConsole, fakeProcess, () => {})
  return Object.assign(api, { _convCache: cache, logs, _itemDigest: itemDigest })
}

// instructions 补发那段是 handler 内联代码,单独按外部变量注入驱动
function runInstrBlock(convSess, instructions, envMax) {
  const logs = []
  // eslint-disable-next-line no-new-func
  const f = new Function('convSess', 'instructions', '_convDelta', 'console', 'process',
    instrSrc + '\nreturn { convSess, _convInstr, _convDelta }')
  const out = f(convSess, instructions, [{ x: 1 }],
    { log: (s) => logs.push(String(s)) },
    { env: envMax === undefined ? {} : { ZK_CONV_INSTR_MAX: String(envMax) } })
  return Object.assign(out, { logs })
}

let pass = 0, fail = 0
const fails = []
function check(name, cond, detail) {
  if (cond) { pass++; console.log(`  ok   ${name}`) }
  else { fail++; fails.push({ name, detail }); console.log(`  FAIL ${name} — ${detail}`) }
}

const FRAME = { role: 'system', content: '<framework> identical across all Cursor chats </framework>' }
const msg = (t) => ({ role: 'user', content: t })

// ── ① instructions 漂移仍命中 —— 本次 bug 的正脸 ──────────────────────
;(function instrDriftStillHits() {
  console.log('\n① instructions 漂移仍命中')
  const A = build({})
  const hist = [FRAME, msg('q1'), msg('a1')]
  A.saveConvSession(hist, 'INSTRUCTIONS VERSION A', 'conv-aaa', 'p1')
  const next = hist.concat([msg('q2')])
  const hit = A.findConvSession(next, 'INSTRUCTIONS VERSION B — Cursor 换了一版')
  check('drift-hits', hit !== null, 'returned null — 这就是线上那个 bug')
  check('drift-right-conv', hit && hit.convId === 'conv-aaa', hit && hit.convId)
  check('drift-count', hit && hit.count === 3, hit && String(hit.count))
  check('drift-flags-changed', hit && hit.instrChanged === true, hit && String(hit.instrChanged))
})()

// ── ② 不同会话不撞车(第 0 条完全相同) ────────────────────────────────
;(function noCollision() {
  console.log('\n② 不同会话不撞车')
  const A = build({})
  const h1 = [FRAME, msg('聊天一的问题')]
  const h2 = [FRAME, msg('聊天二的问题')]
  A.saveConvSession(h1, 'i', 'conv-111', 'p')
  A.saveConvSession(h2, 'i', 'conv-222', 'p')
  check('two-slots', A._convCache.size === 2, `size=${A._convCache.size}`)
  const r1 = A.findConvSession(h1.concat([msg('追问一')]), 'i')
  const r2 = A.findConvSession(h2.concat([msg('追问二')]), 'i')
  check('collision-1', r1 && r1.convId === 'conv-111', r1 && r1.convId)
  check('collision-2', r2 && r2.convId === 'conv-222', r2 && r2.convId)
})()

// ── ③ 多候选取最长前缀 ────────────────────────────────────────────────
;(function longestWins() {
  console.log('\n③ 多候选取最长前缀')
  const A = build({})
  const base = [FRAME, msg('q1')]
  const longer = base.concat([msg('a1'), msg('q2')])
  A.saveConvSession(base, 'i', 'conv-short', 'p')
  A.saveConvSession(longer, 'i', 'conv-long', 'p')
  const r = A.findConvSession(longer.concat([msg('q3')]), 'i')
  check('longest-wins', r && r.convId === 'conv-long', r && `${r.convId} count=${r.count}`)
  check('longest-count', r && r.count === 4, r && String(r.count))
})()

// ── ④ 历史被改过 → miss ───────────────────────────────────────────────
;(function historyEdited() {
  console.log('\n④ 历史被改过 → miss')
  const A = build({})
  const hist = [FRAME, msg('q1'), msg('a1')]
  A.saveConvSession(hist, 'i', 'conv-x', 'p')
  const tampered = [FRAME, msg('q1-被改过了'), msg('a1'), msg('q2')]
  check('edited-miss', A.findConvSession(tampered, 'i') === null, 'should be null')
  check('edited-log-mismatch', A.logs.some((l) => /\[conv\] miss .*mismatch=1/.test(l)),
    JSON.stringify(A.logs.slice(-1)))
})()

// ── ⑤ 非严格前缀(等长 / 更短)→ miss ────────────────────────────────
;(function notStrictPrefix() {
  console.log('\n⑤ 非严格前缀 → miss')
  const A = build({})
  const hist = [FRAME, msg('q1'), msg('a1')]
  A.saveConvSession(hist, 'i', 'conv-x', 'p')
  check('equal-len-miss', A.findConvSession(hist, 'i') === null, '等长应 miss(没有新 item 可发)')
  check('shorter-miss', A.findConvSession([FRAME, msg('q1')], 'i') === null, '更短应 miss')
  check('notprefix-log', A.logs.some((l) => /\[conv\] miss .*notprefix=1/.test(l)),
    JSON.stringify(A.logs.slice(-1)))
})()

// ── ⑥ TTL 过期 → miss 且被清 ─────────────────────────────────────────
;(function ttl() {
  console.log('\n⑥ TTL 过期')
  const A = build({})
  const hist = [FRAME, msg('q1')]
  A.saveConvSession(hist, 'i', 'conv-old', 'p')
  A._convCache.get('conv-old').ts = Date.now() - TTL_MS - 1000
  const r = A.findConvSession(hist.concat([msg('q2')]), 'i')
  check('ttl-miss', r === null, 'should be null')
  check('ttl-purged', A._convCache.size === 0, `size=${A._convCache.size}`)
  check('ttl-log-stale', A.logs.some((l) => /\[conv\] miss .*stale=1/.test(l)),
    JSON.stringify(A.logs.slice(-1)))
})()

// ── ⑦ instrChanged 三态 ───────────────────────────────────────────────
;(function instrFlag() {
  console.log('\n⑦ instrChanged 三态')
  const A = build({})
  const hist = [FRAME, msg('q1')]
  const next = hist.concat([msg('q2')])
  A.saveConvSession(hist, 'SAME', 'conv-s', 'p')
  check('flag-unchanged', A.findConvSession(next, 'SAME').instrChanged === false, 'should be false')
  check('flag-changed', A.findConvSession(next, 'OTHER').instrChanged === true, 'should be true')
  // 老持久化条目没有 instrDigest 字段 → 不判变化
  delete A._convCache.get('conv-s').instrDigest
  check('flag-legacy-entry', A.findConvSession(next, 'WHATEVER').instrChanged === false,
    '老条目无 instrDigest 时不该判成变化')
})()

// ── ⑧ 同一 convId 原地更新 ────────────────────────────────────────────
;(function inPlace() {
  console.log('\n⑧ 同一 convId 原地更新')
  const A = build({})
  const h = [FRAME, msg('q1')]
  A.saveConvSession(h, 'iA', 'conv-same', 'p')
  A.saveConvSession(h.concat([msg('a1'), msg('q2')]), 'iB', 'conv-same', 'p2')
  check('one-slot', A._convCache.size === 1, `size=${A._convCache.size} —— 旧版这里会变成 2`)
  check('updated-count', A._convCache.get('conv-same').count === 4,
    String(A._convCache.get('conv-same').count))
})()

// ── ⑨ LRU 上限 200 ────────────────────────────────────────────────────
;(function lru() {
  console.log('\n⑨ LRU 上限 200')
  const A = build({})
  for (let i = 0; i < 250; i++) A.saveConvSession([FRAME, msg('q' + i)], 'i', 'conv-' + i, 'p')
  check('lru-cap', A._convCache.size <= 200, `size=${A._convCache.size}`)
  check('lru-keeps-newest', A._convCache.has('conv-249'), 'newest evicted')
  check('lru-drops-oldest', !A._convCache.has('conv-0'), 'oldest kept')
})()

// ── ⑩ 关闸 ────────────────────────────────────────────────────────────
;(function off() {
  console.log('\n⑩ ZK_CONV_REUSE=0 关闸')
  const A = build({ ZK_CONV_REUSE: '0' })
  A.saveConvSession([FRAME, msg('q1')], 'i', 'conv-z', 'p')
  check('off-no-save', A._convCache.size === 0, `size=${A._convCache.size}`)
  check('off-no-find', A.findConvSession([FRAME, msg('q1'), msg('q2')], 'i') === null, 'should be null')
  check('off-silent', A.logs.length === 0, JSON.stringify(A.logs))
})()

// ── ⑫ instructions 补发决策 ───────────────────────────────────────────
;(function instrCarry() {
  console.log('\n⑫ instructions 补发决策')
  const S = { convId: 'c', count: 2, instrChanged: true }
  const Snc = { convId: 'c', count: 2, instrChanged: false }

  const a = runInstrBlock(Snc, 'short instructions', undefined)
  check('carry-unchanged-none', a._convInstr === null, String(a._convInstr))
  check('carry-unchanged-keeps-sess', a.convSess !== null, 'sess dropped')

  const b = runInstrBlock(S, 'short instructions', undefined)
  check('carry-changed-short', b._convInstr === 'short instructions', String(b._convInstr))
  check('carry-changed-keeps-sess', b.convSess !== null, 'sess dropped')
  check('carry-log', b.logs.some((l) => /carried in delta/.test(l)), JSON.stringify(b.logs))

  const big = 'x'.repeat(9000)
  const c = runInstrBlock(S, big, 8192)
  check('carry-toolong-drops-sess', c.convSess === null, '超长应退回全量')
  check('carry-toolong-no-instr', c._convInstr === null, String(c._convInstr))
  check('carry-toolong-log', c.logs.some((l) => /too long/.test(l)), JSON.stringify(c.logs))

  // 阈值可调:同样 9000 字符,把上限调到 10000 就该带上
  const d = runInstrBlock(S, big, 10000)
  check('carry-env-tunable', d._convInstr === big && d.convSess !== null, 'env 未生效')
})()

// ── ⑬ 等长严格前缀平局 → 取最近使用的那条 ─────────────────────────────
//    2026-08-31 现场抓到:两次探针发了完全相同的文本,第 6 轮同时匹配上两条会话
//    (都是 10 项等长严格前缀),首个胜出 → 新一轮被接到了上一次那条旧会话上。
;(function tieBreakByRecency() {
  console.log('\n⑬ 等长前缀平局取最近')
  const A = build({})
  const hist = [FRAME, msg('同一句开场白'), msg('OK')]
  A.saveConvSession(hist, 'i', 'conv-old', 'p')      // 先存旧的
  A.saveConvSession(hist, 'i', 'conv-new', 'p')      // 同样的历史,另一条会话
  A._convCache.get('conv-old').ts = Date.now() - 60000   // 旧的确实更早
  check('tie-two-cands', A._convCache.size === 2, `size=${A._convCache.size}`)
  const r = A.findConvSession(hist.concat([msg('新一轮')]), 'i')
  check('tie-picks-newest', r && r.convId === 'conv-new',
    r ? `选中 ${r.convId} —— 平局取了旧的,新一轮会接到别的会话上` : 'null')
  // 反向:把旧的 ts 调到更新,就该选它 —— 证明判据真的看 ts,不是碰巧的 Map 顺序
  A._convCache.get('conv-old').ts = Date.now() + 1000
  const r2 = A.findConvSession(hist.concat([msg('新一轮')]), 'i')
  check('tie-follows-ts', r2 && r2.convId === 'conv-old', r2 && r2.convId)
  // 长度优先于新旧:更短但更新的不许赢过更长的
  const A2 = build({})
  const base = [FRAME, msg('q1')]
  A2.saveConvSession(base.concat([msg('a1'), msg('q2')]), 'i', 'conv-long', 'p')
  A2.saveConvSession(base, 'i', 'conv-shortbutnew', 'p')   // 后存 = ts 更新
  const r3 = A2.findConvSession(base.concat([msg('a1'), msg('q2'), msg('q3')]), 'i')
  check('length-beats-recency', r3 && r3.convId === 'conv-long', r3 && r3.convId)
})()

console.log(`\n== ${pass}/${pass + fail} PASS ==`)
if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1) }
console.log('VERDICT: GO (漂移命中/不撞车/最长前缀/改史 miss/非严格前缀/TTL/标志三态/原地更新/LRU/关闸/补发决策/平局取最近 全绿)')
