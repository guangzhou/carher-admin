#!/usr/bin/env node
/* contract_diet_offline_cases.js — ROI #3.5 契约瘦身离线单测。
 *
 * 不重实现:括号配平从 responses.js 逐字抠出 `if (proto2) { … }` 真分支 + 两个契约常量
 * (V2_CONTRACT / V2_CONTRACT_MINI),注入 process/convSess/basePrompt/console 后 eval 驱动。
 * 测的是上线那份门控本身。
 *
 * 覆盖:①DIET=1 且 convSess 命中(增量轮)→ prompt 用 MINI 不用 full ②DIET=1 但首轮
 *   (convSess=null)→ 仍用 full(握手轮必须全份)③DIET 关(默认)→ 每轮全份=零行为差
 *   ④MINI 含 6 条 load-bearing 不变量 ⑤MINI 显著短于 full(≥50% 省)⑥basePrompt 始终保留
 *   (契约是追加非替换)。
 *
 * 用法: node scripts/zk-cursor-web/contract_diet_offline_cases.js [/path/to/responses.js]
 */
'use strict'
const fs = require('fs')

const SRC = process.argv[2] || '/tmp/resp_live_82.js'
const code = fs.readFileSync(SRC, 'utf8')

// —— 括号配平抠 `if (proto2) { … }`(到匹配右括号)——
function grabIfBlock(anchor) {
  const start = code.indexOf(anchor)
  if (start < 0) throw new Error('cannot find ' + anchor)
  const open = code.indexOf('{', start)
  let depth = 0
  for (let i = open; i < code.length; i++) {
    const ch = code[i]
    if (ch === '{') depth++
    else if (ch === '}') { depth--; if (depth === 0) return code.slice(start, i + 1) }
  }
  throw new Error('unbalanced braces for ' + anchor)
}
const protoBlock = grabIfBlock('if (proto2) {')

// —— 逐字抠两个契约常量(eval 取真值,不手抄)——
function grabConstExpr(name, endMarker) {
  const re = new RegExp('const\\s+' + name + '\\s*=\\s*([\\s\\S]*?)\\n' + endMarker)
  const m = code.match(re)
  if (!m) throw new Error('cannot grab const ' + name)
  // 剥掉尾随的整行 // 注释(否则会注释掉包裹用的右括号)
  const expr = m[1].split('\n').filter((ln) => !/^\s*\/\//.test(ln)).join('\n').trim()
  // eslint-disable-next-line no-new-func
  return new Function('return (' + expr + ')')()
}
const V2_CONTRACT = grabConstExpr('V2_CONTRACT', 'const V2_HANDSHAKE')
const V2_CONTRACT_MINI = grabConstExpr('V2_CONTRACT_MINI', 'const V2_RUN_RE')

// 用真分支构造 driver:注入依赖,返回 prompt
function runGate(env, convSess, basePrompt) {
  const fakeProc = { env }
  // eslint-disable-next-line no-new-func
  const fn = new Function(
    'process', 'convSess', 'basePrompt', 'V2_CONTRACT', 'V2_CONTRACT_MINI', 'console',
    'let prompt; const proto2 = true; ' + protoBlock + '\n return prompt'
  )
  const quietConsole = { log() {} }
  return fn(fakeProc, convSess, basePrompt, V2_CONTRACT, V2_CONTRACT_MINI, quietConsole)
}

let pass = 0, fail = 0
const fails = []
function check(name, cond, why) {
  if (cond) { pass++; console.log(`[PASS] ${name}`) }
  else { fail++; fails.push({ name, why }); console.log(`[FAIL] ${name} — ${why}`) }
}

const BASE = 'BASEPROMPT_MARKER'
const SESS = { convId: 'conv-aaaa1111', count: 8 }

// —— ① DIET=1 + convSess → MINI ——
;(function dietDelta() {
  const p = runGate({ ZK_CONTRACT_DIET: '1' }, SESS, BASE)
  check('diet-delta-uses-mini', p.includes(V2_CONTRACT_MINI) && !p.includes(V2_CONTRACT), 'delta turn did not swap to mini')
  check('diet-delta-keeps-base', p.startsWith(BASE), 'basePrompt dropped')
})()

// —— ② DIET=1 但首轮(convSess=null)→ full ——
;(function dietFirstTurn() {
  const p = runGate({ ZK_CONTRACT_DIET: '1' }, null, BASE)
  check('diet-first-turn-full', p.includes(V2_CONTRACT), 'first turn (handshake) must carry full contract')
})()

// —— ③ DIET 关(默认)→ 每轮全份(即便 convSess 命中)——
;(function dietOff() {
  const p = runGate({}, SESS, BASE)
  check('diet-off-full-on-delta', p.includes(V2_CONTRACT) && !p.includes(V2_CONTRACT_MINI), 'default-off leaked mini (behavior drift)')
})()

// —— ④ MINI 含 6 条不变量 ——
;(function invariants() {
  const need = ['⟦cmd¦run', 'FAKE', '如上', 'IF AND ONLY IF', 'fully resolved', '⟦ask⟧', 'Never mention']
  need.forEach((k) => check(`mini-has[${k}]`, V2_CONTRACT_MINI.includes(k), `mini missing invariant: ${k}`))
})()

// —— ⑤ MINI ≥50% 短于 full ——
;(function shorter() {
  const ratio = V2_CONTRACT_MINI.length / V2_CONTRACT.length
  check('mini-≥50%-shorter', ratio <= 0.5, `mini/full=${ratio.toFixed(2)} (full=${V2_CONTRACT.length} mini=${V2_CONTRACT_MINI.length})`)
})()

console.log(`\n== ${pass}/${pass + fail} PASS ==`)
if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1) }
console.log(`VERDICT: GO (增量轮换MINI/首轮全份/默认关零漂移/6不变量/省${100 - Math.round(V2_CONTRACT_MINI.length / V2_CONTRACT.length * 100)}%/base保留)`)
