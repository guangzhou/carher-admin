#!/usr/bin/env node
/* fail_teach_offline_cases.js — ROI #2 失败教学注入层离线单测。
 *
 * 不重实现:括号配平从 responses.js 里逐字抠出 `let describeUnknownItem = (item) => {…}`
 * 的真实字节,注入 process(可切 ZK_FAIL_TEACH / ZK_TR_VIS)后 eval 驱动。测的是上线那份
 * 代码本身。
 *
 * 覆盖:①失败信封(超时/后台化/无输出/command timed out)+ ZK_FAIL_TEACH=1 → 追加教学行;
 *   ②正常结果 + ZK_FAIL_TEACH=1 → 不追加(不误伤);③失败信封 + ZK_FAIL_TEACH 关 → 不追加
 *   (默认零行为差);④正文含 "error/Error" 但非失败信封 → 不追加(误伤防护);⑤教学行内容
 *   含关键行为指令(不被动等死/发下一条 ⟦cmd¦run⟧/重试后才 blocked);⑥[TOOL RESULT] 前缀与
 *   原始 out 内容始终保留(注入是追加而非替换);⑦非工具输出 item(function_call)不受影响;
 *   ⑧失败信封 + ZK_FAIL_TEACH=1 + ZK_TR_VIS=1 → 两注入共存且顺序 out→visNote→failTeach。
 *
 * 用法: node scripts/zk-cursor-web/fail_teach_offline_cases.js [/path/to/responses.js]
 */
'use strict'
const fs = require('fs')

const SRC = process.argv[2] || '/tmp/resp_live_82.js'
const code = fs.readFileSync(SRC, 'utf8')

// —— 括号配平抠 `let describeUnknownItem = (item) => { … }` ——
function grabArrow(name) {
  const anchor = 'let ' + name + ' = '
  const start = code.indexOf(anchor)
  if (start < 0) throw new Error('cannot find ' + anchor + ' in ' + SRC)
  const open = code.indexOf('{', code.indexOf('=>', start))
  let depth = 0
  for (let i = open; i < code.length; i++) {
    const ch = code[i]
    if (ch === '{') depth++
    else if (ch === '}') { depth--; if (depth === 0) return code.slice(code.indexOf('(', start), i + 1) }
  }
  throw new Error('unbalanced braces for ' + name)
}
const arrowSrc = grabArrow('describeUnknownItem')

// 用注入 process 构造真实函数(env 两态各造)
function build(env) {
  const fakeProc = { env }
  // eslint-disable-next-line no-new-func
  return new Function('process', 'return ' + arrowSrc)(fakeProc)
}

let pass = 0, fail = 0
const fails = []
function check(name, cond, why) {
  if (cond) { pass++; console.log(`[PASS] ${name}`) }
  else { fail++; fails.push({ name, why }); console.log(`[FAIL] ${name} — ${why}`) }
}

const TEACH_MARK = 'do NOT wait passively'
const outItem = (output) => ({ type: 'function_call_output', call_id: 'call_x', output })

const FAIL_SAMPLES = [
  'Command "npm install" did not complete in 30000ms',
  'No output was collected from the command',
  'The long-running process was sent to the background',
  'command timed out after 30000 ms',
]
const OK_SAMPLES = [
  'total 24\ndrwxr-xr-x  5 user staff  160 file.txt\nREADME.md',
  'bar\nbaz\n42 files',
]
// 正文含 error 但非失败信封 —— 必须不误伤
const ERRY_OK = 'grep result:\n  app.log: ERROR connection refused\n  2 matches, exit 0'

// —— ① 失败信封 + TEACH 开 → 追加教学行 ——
;(function failOn() {
  const f = build({ ZK_FAIL_TEACH: '1' })
  FAIL_SAMPLES.forEach((s, i) => {
    const r = f(outItem(s))
    check(`fail-teach-on[${i}]`, r.includes(TEACH_MARK), `no teaching for: ${s.slice(0, 40)}`)
  })
})()

// —— ② 正常结果 + TEACH 开 → 不追加 ——
;(function okOn() {
  const f = build({ ZK_FAIL_TEACH: '1' })
  OK_SAMPLES.forEach((s, i) => {
    const r = f(outItem(s))
    check(`ok-no-teach[${i}]`, !r.includes(TEACH_MARK), `false-positive teaching on normal output #${i}`)
  })
})()

// —— ③ 失败信封 + TEACH 关 → 不追加(默认零行为差)——
;(function failOff() {
  const f = build({})
  const r = f(outItem(FAIL_SAMPLES[0]))
  check('fail-teach-off', !r.includes(TEACH_MARK), 'teaching leaked while ZK_FAIL_TEACH unset')
})()

// —— ④ 含 "error/Error" 但非失败信封 → 不误伤 ——
;(function erryOk() {
  const f = build({ ZK_FAIL_TEACH: '1' })
  const r = f(outItem(ERRY_OK))
  check('erry-not-teached', !r.includes(TEACH_MARK), 'teaching false-fired on output merely containing ERROR')
})()

// —— ⑤ 教学行含关键行为指令 ——
;(function content() {
  const f = build({ ZK_FAIL_TEACH: '1' })
  const r = f(outItem(FAIL_SAMPLES[0]))
  check('teach-has-nextcmd', /⟦cmd¦run=\.\.\.⟧/.test(r), 'teaching missing next-command directive')
  check('teach-has-blocked-gate', /retried|blocked/i.test(r), 'teaching missing retry/blocked gate')
  check('teach-not-passive', r.includes('do NOT wait passively'), 'teaching missing anti-passive directive')
})()

// —— ⑥ 前缀与原始 out 始终保留(追加非替换)——
;(function preserve() {
  const f = build({ ZK_FAIL_TEACH: '1' })
  const r = f(outItem(FAIL_SAMPLES[0]))
  check('preserve-prefix', r.startsWith('[TOOL RESULT call_x]'), `prefix lost: ${r.slice(0, 30)}`)
  check('preserve-out', r.includes(FAIL_SAMPLES[0]), 'original output body dropped')
})()

// —— ⑦ 非工具输出 item 不受影响 ——
;(function noncall() {
  const f = build({ ZK_FAIL_TEACH: '1' })
  const r = f({ type: 'function_call', name: 'shell', arguments: '{"cmd":"ls"}' })
  check('function-call-untouched', typeof r === 'string' && r.startsWith('[TOOL CALL]') && !r.includes(TEACH_MARK), `unexpected: ${String(r).slice(0, 40)}`)
})()

// —— ⑧ TEACH + TR_VIS 共存,顺序 out→visNote→failTeach ——
;(function coexist() {
  const f = build({ ZK_FAIL_TEACH: '1', ZK_TR_VIS: '1' })
  const r = f(outItem(FAIL_SAMPLES[0]))
  const iOut = r.indexOf(FAIL_SAMPLES[0])
  const iVis = r.indexOf('the user CANNOT see this tool output')
  const iTeach = r.indexOf(TEACH_MARK)
  check('coexist-both-present', iOut >= 0 && iVis >= 0 && iTeach >= 0, `out=${iOut} vis=${iVis} teach=${iTeach}`)
  check('coexist-order', iOut < iVis && iVis < iTeach, `order out=${iOut} vis=${iVis} teach=${iTeach}`)
})()

console.log(`\n== ${pass}/${pass + fail} PASS ==`)
if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1) }
console.log('VERDICT: GO (失败信封命中/正常不误伤/默认关/error不误伤/教学内容/前缀保留/非调用不动/双注入共存)')
