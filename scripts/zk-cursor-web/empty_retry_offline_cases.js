#!/usr/bin/env node
'use strict'
/* empty_retry_offline_cases.js — 空轮重试梯子(ZK_EMPTY_RETRY)结构+门控断言。
 *
 * 背景(2026-08-28 用户实锤 15:12:10→15:12:28):桥 seam3 路 turn-1 空轮此前直达
 * `finishPlainText('本轮上游未产出内容')`,零重试(非桥 violation 路有同会话重发预算 1,
 * 桥路没接)。同会话重发对空轮实测无效(08-28 前两例死轮重发同样 200+零正文)→ 梯子
 * 换 **fresh conv** 全量重掷(换信号非重掷同骰),救回按三态收口,救不回才诚实报错。
 *
 * 断言面:①门控默认 0=零行为差(行为断言:eval 真值行) ②_rebuildFullPrompt 公式
 * (convSess 全量 flatten/首轮复用 prompt + strip execenv + 桥感知契约选择器)
 * ③seam3 梯子(有界循环/writableEnded 守卫/fresh-conv/收养/三态收口/诚实兜底保留/
 * 心跳清理/无裸 finishPlain) ④violation 路重掷(共享计数有界/重进 finish()/收养/
 * then+catch 双路清理) 。
 *
 * 用法: node scripts/zk-cursor-web/empty_retry_offline_cases.js [/path/to/responses.js]
 */
const fs = require('fs')
const SRC = process.argv[2] || '/tmp/resp_er.js'
const code = fs.readFileSync(SRC, 'utf8')

let pass = 0, fail = 0
const fails = []
function check(name, ok, why) {
  if (ok) { pass++ } else { fail++; fails.push({ name, why }) }
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${ok ? '' : ' — ' + why}`)
}

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

// —— ① 门控:默认 '0' = 关;真值行为断言(eval 真值行,非手抄)——
const gateM = /const _emptyRetryMax = (Math\.max\(0, parseInt\(process\.env\.ZK_EMPTY_RETRY \|\| '0', 10\) \|\| 0\))/.exec(code)
check('gate-line-present', !!gateM, '_emptyRetryMax gate line missing or shape drifted')
if (gateM) {
  const evalGate = (v) => {
    const old = process.env.ZK_EMPTY_RETRY
    if (v === undefined) delete process.env.ZK_EMPTY_RETRY
    else process.env.ZK_EMPTY_RETRY = v
    // eslint-disable-next-line no-eval
    try { return eval(gateM[1]) } finally {
      if (old === undefined) delete process.env.ZK_EMPTY_RETRY
      else process.env.ZK_EMPTY_RETRY = old
    }
  }
  check('gate-default-off', evalGate(undefined) === 0, 'unset must be 0 (off)')
  check('gate-zero-off', evalGate('0') === 0, '"0" must be 0')
  check('gate-two', evalGate('2') === 2, '"2" must be 2')
  check('gate-garbage-off', evalGate('abc') === 0, 'garbage must fall back to 0')
  check('gate-negative-clamped', evalGate('-3') === 0, 'negative must clamp to 0')
}

// —— ② _rebuildFullPrompt 公式 ——
const rbM = /const _rebuildFullPrompt = \(\) => \{[\s\S]*?\n    \}/.exec(code)
check('rebuild-helper-present', !!rbM, '_rebuildFullPrompt missing')
if (rbM) {
  const rb = rbM[0]
  check('rebuild-convSess-flatten', /convSess \? flattenInput\(dietCursorInput\(dietOldToolOutputs\(codexInput\)\), instructions\) : basePrompt/.test(rb),
    'delta turn must full-flatten (r15: delta 开新会话丢上下文), first turn uses basePrompt')
  check('rebuild-strips-execenv', /_rp = _stripExecEnv\(_rp\)/.test(rb), 'must strip [EXECUTION ENVIRONMENT] (r15 半只脚还在坑里)')
  check('rebuild-bridge-aware-contract', /mcpBridge\.isEnabled\(\) \? V2B_CONTRACT : V2_CONTRACT/.test(rb),
    'contract selector must be bridge-aware (同 conv 失效重建路公式)')
}

// —— ③ seam3 梯子 ——
const seam3 = grabIfBlock('\n    if (_bridgeReady) {')
check('seam3-ladder-loop', /for \(let _k = 1; _k <= _emptyRetryMax && !res\.writableEnded; _k\+\+\)/.test(seam3),
  'ladder loop missing, unbounded, or lacks writableEnded guard (客户端弃单禁重掷)')
check('seam3-fresh-conv-reroll', /collectWebTextR\(convSess \? _rebuildFullPrompt\(\) : prompt, true\)/.test(seam3),
  'reroll must be fresh-conv (true) with first-turn prompt reuse / delta rebuild')
check('seam3-adopts-new-conv', /saveConvSession\(input, instructions, _rr\.convId, _rr\.parentId\)/.test(seam3),
  'rescued reroll must adopt new conv (r15 纪律)')
check('seam3-rescue-call', /_bctx\.finishWithCall\(\{ callId: _rv\.callId/.test(seam3), 'rescued kind:call must finishWithCall')
check('seam3-rescue-prose', /rescued attempt \$\{_k\} -> prose[\s\S]*?_bctx\.finishPlainText\(_rv\.text\)/.test(seam3),
  'rescued kind:text must finishPlainText with clean text')
check('seam3-honest-error-kept', /_bctx\.finishPlainText\('本轮上游未产出内容/.test(seam3),
  'honest error fallback must remain (梯子穷尽后不假绿)')
check('seam3-heartbeat-cleared', /const _hbe = setInterval[\s\S]*?clearInterval\(_hbe\)/.test(seam3),
  'reroll heartbeat must be cleared after await')
check('seam3-no-bare-finishPlain', seam3.indexOf('finishPlain(') < 0,
  'seam3 must not use bare finishPlain( (⟦cmd¦run⟧ 方言裸漏回归)')
// 梯子在 else(真空)分支内:fallback/prose 收口路不得被包进循环(结构:fallback 日志行在循环体外)
check('seam3-fallback-outside-ladder', seam3.indexOf('turn-1 fallback ⟦cmd⟧') < seam3.indexOf('[empty-retry] seam3'),
  'fallback ⟦cmd⟧ path must stay before/outside the empty ladder')

// —— ④ violation 路重掷 ——
const vIdx = code.indexOf('violation fresh-conv attempt')
check('violation-reroll-present', vIdx >= 0, 'violation-path reroll missing')
const vSeg = code.slice(Math.max(0, vIdx - 600), vIdx + 1600)
check('violation-shared-counter-bounded', /if \(emptyRerolls < _emptyRetryMax && !res\.writableEnded\)/.test(vSeg),
  'must bound by shared emptyRerolls counter (跨 finish() 重入有界) + writableEnded guard')
check('violation-counter-incremented', /emptyRerolls\+\+/.test(vSeg), 'counter must increment before reroll')
check('violation-fresh-conv', /collectWebTextR\(convSess \? _rebuildFullPrompt\(\) : prompt, true\)/.test(vSeg),
  'violation reroll must be fresh-conv with same prompt formula')
check('violation-adopts-new-conv', /saveConvSession\(input, instructions, r2\.convId, r2\.parentId\)/.test(vSeg),
  'rescued reroll must adopt new conv')
check('violation-reenters-finish', /finished = false; finish\(\)/.test(vSeg),
  'rescued text must re-enter finish() (走完整 verdict 机器,不另起解析器)')
check('violation-catch-also-reenters', /\.catch\(\(\) => \{ if \(_hbe2\) clearInterval\(_hbe2\); finished = false; finish\(\) \}\)/.test(vSeg),
  'catch path must clear heartbeat and re-enter finish() (不悬挂)')
check('violation-returns-after-reroll', /\.catch\(\(\) => \{[\s\S]*?\}\)\n          return/.test(vSeg),
  'reroll branch must return (不得掉进诚实报错)')
check('violation-honest-log-counts-rerolls', /still empty after resend\$\{emptyRerolls \? `\+\$\{emptyRerolls\} rerolls` : ''\}/.test(code),
  'final honest-error log must count rerolls (soak 可统计)')

// —— ⑤ 既有不变量不回归:同会话重发预算 1 仍在梯子之前 ——
check('same-conv-resend-kept', code.indexOf('violation (empty/undeliverable) -> same-conv resend (budget 1)') >= 0
  && code.indexOf('violation (empty/undeliverable)') < vIdx,
  'same-conv resend (budget 1) must remain and run before fresh-conv rerolls')

console.log(`\n== ${pass}/${pass + fail} PASS ==`)
if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1) }
console.log('VERDICT: GO (门控默认关/重建公式/seam3梯子有界+收养+三态收口+诚实兜底/violation重掷有界重进finish/预算1保留)')
