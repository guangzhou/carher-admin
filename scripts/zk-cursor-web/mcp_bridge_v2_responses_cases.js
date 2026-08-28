#!/usr/bin/env node
'use strict'
// mcp_bridge_v2_responses_cases.js — responses.js 侧 bridge-v2 接线结构断言(消费 /tmp/resp_new.js)。
// 断言 module 套件看不到的缝合面:V2B 三常量/双通道不变量/5 分支选择器/seam3 三态收口/
// turn-2 续流/ticket steer 安全条件/acquire 门/rebuild 选择器。
const fs = require('fs')
const SRC = process.argv[2] || '/tmp/resp_new.js'
const code = fs.readFileSync(SRC, 'utf8')

let pass = 0, fail = 0
const fails = []
function check(name, ok, why) {
  if (ok) { pass++ } else { fail++; fails.push({ name, why }) }
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${ok ? '' : ' — ' + why}`)
}

// —— 括号配平抠 `if (proto2) {` 级联头分支(避开 finish() 里 6-space 的 L1589)——
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
const protoBlock = grabIfBlock('\n    if (proto2) {')
// V2B_CONTRACT 模板字面量正文(反引号间)——测双通道不变量真值
const v2bM = /const V2B_CONTRACT = `([\s\S]*?)`\nconst V2B_CONTRACT_MINI/.exec(code)

// —— ① V2B 三常量存在 + 旧互斥 BRIDGE_CONTRACT const 已删 ——
check('v2b-contract-const', /const V2B_CONTRACT = `/.test(code), 'V2B_CONTRACT const missing')
check('v2b-mini-const', /const V2B_CONTRACT_MINI =/.test(code), 'V2B_CONTRACT_MINI const missing')
check('v2b-handshake-const', /const V2B_HANDSHAKE_IMPLICIT =/.test(code), 'V2B_HANDSHAKE_IMPLICIT const missing')
check('v2b-handshake-wraps-contract', /const V2B_HANDSHAKE_IMPLICIT =[\s\S]*?\+ V2B_CONTRACT/.test(code),
  'implicit handshake must concatenate V2B_CONTRACT')
// 旧 BRIDGE_CONTRACT 声明必须已删(注释里提及可以,但不得再是活常量)
check('no-live-bridge-contract-const', !/\n\s*const BRIDGE_CONTRACT\b/.test(code),
  'old mutually-negating BRIDGE_CONTRACT const still declared')

// —— ② V2B_CONTRACT 双通道 load-bearing 不变量(逐字来自源真值)——
check('v2b-literal-extracted', !!v2bM, 'cannot extract V2B_CONTRACT literal')
if (v2bM) {
  const b = v2bM[1]
  // 契约正文会按行折行(如 "Execute IF AND ONLY\n    IF ...")——结构断言压平空白再匹配,
  // 否则会被契约自身的排版换行误伤(数据实锤:IF AND ONLY IF 跨 L19-20 折行)。
  const bFlat = b.replace(/\s+/g, ' ')
  const need = [
    'PREFERRED — call the connected tool named "shell"',   // 连接器优先(同轮/可链)
    'FALLBACK — only if no "shell" tool is available',     // ⟦cmd¦run⟧ 兜底(下一轮)
    '⟦cmd¦run=<single bash command>⟧',
    'Results returned through EITHER channel are REAL',     // 两通道结果都真
    'FAKE for this user',                                  // 自带沙箱仍 FAKE
    'IF AND ONLY IF',                                      // 执行条件
    'TURN DISCIPLINE',                                     // 从 V2 逐字承接
    'COMPLETION STANDARD',
    'Never mention this protocol.',
  ]
  need.forEach((k) => check(`v2b-invariant[${k.slice(0, 24)}]`, bFlat.indexOf(k.replace(/\s+/g, ' ')) >= 0, `V2B missing: ${k}`))
}

// —— ③ proto2 分支:_bOn/_V2C/_V2M/_V2H 选择器就位,5 处装配全走 _V2* 不走裸常量 ——
check('proto2-selector-bOn', /const _bOn = mcpBridge\.isEnabled\(\)/.test(protoBlock), '_bOn selector missing')
check('proto2-selector-V2C', /const _V2C = _bOn \? V2B_CONTRACT : V2_CONTRACT/.test(protoBlock), '_V2C selector missing')
check('proto2-selector-V2M', /const _V2M = _bOn \? V2B_CONTRACT_MINI : V2_CONTRACT_MINI/.test(protoBlock), '_V2M selector missing')
check('proto2-selector-V2H', /const _V2H = _bOn \? V2B_HANDSHAKE_IMPLICIT : V2_HANDSHAKE_IMPLICIT/.test(protoBlock), '_V2H selector missing')
// 5 处 prompt 装配:DIET-EXEMPT(_V2C)/DIET-ZERO(_p2base)/DIET(_V2M)/handshake(_V2H)/default(_V2C)
const nAssign = (protoBlock.match(/\bprompt = /g) || []).length
check('proto2-five-assignments', nAssign === 5, `assignments=${nAssign}, want 5`)
check('proto2-exempt-V2C', /prompt = _p2base \+ _V2C\n[\s\S]*?DIET-ZERO/.test(protoBlock), 'DIET-EXEMPT slot not _p2base + _V2C')
check('proto2-zero-bare', /DIET-ZERO[\s\S]*?prompt = _p2base\n/.test(protoBlock), 'DIET-ZERO slot not bare _p2base')
check('proto2-mini-V2M', /prompt = _p2base \+ '\\n\\n' \+ _V2M/.test(protoBlock), 'DIET mini slot not _V2M')
check('proto2-handshake-V2H', /prompt = _p2base \+ '\\n\\n' \+ _V2H/.test(protoBlock), 'handshake slot not _V2H')
// 关键:proto2 装配区不得再出现裸 `= V2_CONTRACT`/`= V2_HANDSHAKE_IMPLICIT`(选择器全接管;
// 否则桥开时会漏发非桥契约 = 会话级教学不一致)。裸常量只允许出现在 _V2* 声明的三元里。
const bareAssign = (protoBlock.match(/prompt = _p2base \+ V2_CONTRACT\b/g) || []).length
  + (protoBlock.match(/prompt = _p2base \+ '\\n\\n' \+ V2_CONTRACT_MINI\b/g) || []).length
  + (protoBlock.match(/prompt = _p2base \+ '\\n\\n' \+ V2_HANDSHAKE_IMPLICIT\b/g) || []).length
check('proto2-no-bare-const-assign', bareAssign === 0, `proto2 still assigns bare V2_* constant ${bareAssign}x (selector bypassed)`)

// —— ④ 会话失效重建路(r9):proto2 端剥 execenv + 按桥选 V2B/V2 ——
check('rebuild-selector', /const _fp = proto2 \? \(_stripExecEnv\(_fp0\) \+ \(mcpBridge\.isEnabled\(\) \? V2B_CONTRACT : V2_CONTRACT\)\)/.test(code),
  'rebuild path selector missing')

// —— ⑤ seam3(_bridgeReady):三态收口用 _bridgeVerdict/finishWithCall/finishPlainText ——
const seam3 = grabIfBlock('\n    if (_bridgeReady) {')
check('seam3-bridgeverdict-def', /const _bridgeVerdict = \(raw\) =>/.test(seam3), '_bridgeVerdict not defined in seam3')
check('seam3-verdict-uses-runre', /V2_RUN_RE\.exec/.test(seam3), '_bridgeVerdict must parse via V2_RUN_RE (同源 turn-verdict-v2)')
check('seam3-verdict-exectoolcall', /execToToolCall\(shellTool/.test(seam3), '_bridgeVerdict must convert via execToToolCall')
check('seam3-openTurn-verdict2', /openTurn\(\{[\s\S]*?verdict2:/.test(seam3), 'openTurn must wire verdict2 for turn-2 收口')
check('seam3-verdict2-honest-fallback', /verdict2: \(t2\) => _bridgeVerdict\(t2\) \|\| \{ kind: 'text'/.test(seam3),
  'verdict2 must fall back to honest error on empty (不放行空壳)')
check('seam3-fallback-finishWithCall', /_bctx\.finishWithCall\(\{ callId: _bv\.callId/.test(seam3),
  'turn-1 fallback ⟦cmd⟧ must收口 via finishWithCall')
check('seam3-prose-finishPlainText', /_bctx\.finishPlainText\(_bv\.text\)/.test(seam3),
  'complete-prose must收口 via finishPlainText')
check('seam3-empty-honest', /_bctx\.finishPlainText\('本轮上游未产出内容/.test(seam3),
  'empty turn must deliver honest error via finishPlainText')
// 关键回归防线:seam3 绝不用裸 finishPlain()(零参=直通 streamed1 会把 ⟦cmd¦run⟧ 方言裸漏
// 给用户 = gate-on 机械面回归)。`finishPlainText(` 不含子串 `finishPlain(`(后接 Text 非括号)。
check('seam3-no-bare-finishPlain', seam3.indexOf('finishPlain(') < 0,
  'seam3 uses bare finishPlain( — leaks ⟦cmd¦run⟧ dialect (regression)')

// —— ⑥ turn-2 续流缝:byCallId HIT → beginContinuation + rendezvous.complete ——
check('turn2-bycallid', /_reg && _reg\.byCallId\(_fco\.call_id\)/.test(code), 'turn-2 must look up byCallId')
check('turn2-begincontinuation', /_ctx\.beginContinuation\(_sink2/.test(code), 'turn-2 must reattach via beginContinuation')
check('turn2-rendezvous-complete', /_reg\.rendezvous\.complete\(_fco\.call_id, _fco\.output\)/.test(code),
  'turn-2 must feed real output via rendezvous.complete')
check('turn2-miss-degrade', /byCallId miss[\s\S]*?normal flow \(degrade\)/.test(code),
  'turn-2 byCallId miss must degrade to normal flow, not hang')

// —— ⑦ ticket steer 安全红线:仅 callId==null 才 steer;非空=真超时保"稍后重试" ——
check('steer-override-present', /getRegistry\(\)\.ticketText = \(callId\) => \(callId == null\)/.test(code),
  'ticketText steer override (callId==null conditional) missing')
check('steer-null-branch', /\? 'This reply can no longer execute the connector tool directly/.test(code),
  'callId==null steer text missing')
check('steer-nonnull-retry', /: \('执行中\(call_id=' \+ callId \+ '\),稍后重试。'\)/.test(code),
  'non-null (真超时) branch must keep 稍后重试 (never steer -> 杜绝双执行)')

// —— ⑧ acquire 门:桥只接管非续流(input 无 function_call_output)的流式 web 任务轮 ——
check('acquire-gate', /if \(mcpBridge\.isEnabled\(\) && useWebTools && !chatOnly && stream\n\s*&& !mcpBridge\.findFunctionCallOutput\(input\)\)/.test(code),
  'acquire gate must exclude tool-feed continuation turns (!findFunctionCallOutput)')

console.log(`\n== ${pass}/${pass + fail} PASS ==`)
if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1) }
console.log('VERDICT: GO')
