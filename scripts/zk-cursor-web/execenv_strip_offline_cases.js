#!/usr/bin/env node
/* execenv_strip_offline_cases.js — proto2 任务路剥 [EXECUTION ENVIRONMENT] 硬规则离线单测(ZK_STRIP_EXECENV)。
 *
 * 背景(2026-08-28 canary 82 全窗口日志实锤):
 *   LiteLLM hook 注入的 `SYSTEM: [EXECUTION ENVIRONMENT] … your reply for this turn must be
 *   the tool call(s) and nothing else` 与 V2 契约("正文是用户唯一可见,⟦cmd¦run⟧ 是唯一执行
 *   通道")在 proto2 首轮同场打架 → 模型被逼进"只准发工具调用但网页版无该通道"的死角。
 *   数据:3 个空轮死亡(violation raw="" 且重发仍空)全部为隐式握手首轮(chatSessionId:null),
 *   增量轮几十轮近乎免疫;死轮上游 200 + th-pulse 流 20-27s + RES DONE + 零正文,窗口内无 403。
 *   chatOnly 路(_chatOnlyize)与纯聊天重发路(r15)早已剥同一段,唯 proto2 任务路漏剥。
 *
 * 断言面:
 *   1) 门控关 = 零行为差;开 = 首轮形态剥净、其余系统提示与契约完整保留。
 *   2) 正则与 _chatOnlyize 逐字同源(源文件字面断言)。
 *   3) proto2 主分支 5 处 prompt 装配全部走剥后的 _p2base;重建路(conv 失效全量重发)同样剥
 *      (r9 纪律:主路径与重发路径必须同一套变换)。
 *
 * 用法: node scripts/zk-cursor-web/execenv_strip_offline_cases.js [/path/to/responses.js]
 * 全过退出码 0,任一失败退 1。
 */
'use strict'
const fs = require('fs')

const SRC = process.argv[2] || '/tmp/resp_live_82.js'
const code = fs.readFileSync(SRC, 'utf8')

let pass = 0, fail = 0
const fails = []
function check(name, ok, why) {
  if (ok) { pass++ } else { fail++; fails.push({ name, why }) }
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${ok ? '' : ' — ' + why}`)
}

// —— (a) 源文件结构断言 ——
const RE_LIT = '/\\[EXECUTION ENVIRONMENT\\][\\s\\S]*?(?:\\n\\n|$)/'
const reCount = code.split(RE_LIT).length - 1
// _chatOnlyize + r15 纯聊天重发 + _stripExecEnv = 3 处逐字同源
check('regex-same-source-x3', reCount === 3, `found ${reCount} literal occurrences, want 3`)

const fnM = /const _stripExecEnv = \(p\) => \{[\s\S]*?\n    \}/.exec(code)
check('helper-present', !!fnM, 'const _stripExecEnv not found')

// proto2 主分支:抠 `} else if (proto2) {` 到下一个 `} else if (chatOnly) {`
const p2i = code.indexOf('} else if (proto2) {')
const p2j = code.indexOf('} else if (chatOnly) {', p2i)
check('proto2-branch-found', p2i >= 0 && p2j > p2i, `p2i=${p2i} p2j=${p2j}`)
const p2 = code.slice(p2i, p2j)
const nAssign = (p2.match(/prompt = /g) || []).length
const nP2base = (p2.match(/prompt = _p2base/g) || []).length
check('proto2-all-assignments-stripped', nAssign === 5 && nP2base === 5,
  `assignments=${nAssign} via _p2base=${nP2base}, want 5/5`)
check('proto2-no-raw-baseprompt-assign', !/prompt = basePrompt/.test(p2),
  'raw `prompt = basePrompt` still present in proto2 branch')
check('proto2-strip-applied-once', /const _p2base = _stripExecEnv\(basePrompt\)/.test(p2),
  '_p2base = _stripExecEnv(basePrompt) not found')

// 重建路(r9):proto2 分支必须剥 _fp0
check('rebuild-path-stripped', /const _fp = proto2 \? \(_stripExecEnv\(_fp0\) \+ V2_CONTRACT\)/.test(code),
  'rebuild path does not strip _fp0')

// —— (b) 行为断言:eval 抠出的 helper,按真实病例形状喂 ——
// eslint-disable-next-line no-eval
const _stripExecEnv = eval('(' + fnM[0].replace('const _stripExecEnv = ', '') + ')')

// 实锤形状:11:33:48 失败首轮 [PROMPT] REQ 逐字开头
const EXECENV = '[EXECUTION ENVIRONMENT] You have a live runtime that really executes every tool in the tool list; tool results are returned to you. To do anything, emit a tool call. Hard rules that OVERRIDE any conflicting guidance below: do NOT narrate, do NOT write status updates, plans, or summaries, do NOT ask the user to run or paste anything, and NEVER say a tool/shell/file is unavailable — it is available via the tools. When the task needs an action, your reply for this turn must be the tool call(s) and nothing else.'
const FRAMEWORK = 'You are an AI coding assistant, powered by cursor-web-fc-82-terra.\n\nYou are pair programming with a USER to solve their coding task.'
const UQ = 'USER: 随便写个飞书文档，我只是说测试'
const FIRSTTURN = 'SYSTEM: ' + EXECENV + '\n\n' + FRAMEWORK + '\n\n' + UQ
const DELTA = UQ  // 增量轮:只有新用户消息,不含框架

function withGate(v, fn) {
  const old = process.env.ZK_STRIP_EXECENV
  if (v === undefined) delete process.env.ZK_STRIP_EXECENV
  else process.env.ZK_STRIP_EXECENV = v
  try { return fn() } finally {
    if (old === undefined) delete process.env.ZK_STRIP_EXECENV
    else process.env.ZK_STRIP_EXECENV = old
  }
}

// 1) 门控关(未设) = 零行为差
check('gate-off-unset-noop', withGate(undefined, () => _stripExecEnv(FIRSTTURN)) === FIRSTTURN, 'mutated with gate unset')
// 2) 门控关(=0) = 零行为差
check('gate-off-zero-noop', withGate('0', () => _stripExecEnv(FIRSTTURN)) === FIRSTTURN, 'mutated with gate=0')
// 3) 门控开:首轮形态剥净,框架与用户消息逐字保留
const s1 = withGate('1', () => _stripExecEnv(FIRSTTURN))
check('gate-on-execenv-removed', s1.indexOf('[EXECUTION ENVIRONMENT]') < 0, 'EXECENV remains: ' + JSON.stringify(s1.slice(0, 80)))
check('gate-on-framework-kept', s1.indexOf(FRAMEWORK) >= 0, 'framework damaged')
check('gate-on-uq-kept', s1.indexOf(UQ) >= 0, 'user query damaged')
check('gate-on-exact-shape', s1 === 'SYSTEM: ' + FRAMEWORK + '\n\n' + UQ, 'unexpected residue: ' + JSON.stringify(s1.slice(0, 60)))
// 4) 门控开:增量轮(不含该段) no-op
check('gate-on-delta-noop', withGate('1', () => _stripExecEnv(DELTA)) === DELTA, 'delta turn mutated')
// 5) 门控开:段落在末尾无 \n\n 收尾 → 剥到串尾(与 _chatOnlyize 同语义)
const TAIL = FRAMEWORK + '\n\n' + EXECENV
const s5 = withGate('1', () => _stripExecEnv(TAIL))
check('gate-on-tail-stripped', s5.indexOf('[EXECUTION ENVIRONMENT]') < 0 && s5.indexOf(FRAMEWORK) >= 0, 'tail form not stripped cleanly')
// 6) 契约拼接完整性:剥后 + V2 契约,契约文本零损伤(从源文件抠 V2_CONTRACT 真值)
const vcM = /const V2_CONTRACT = `([\s\S]*?)`/.exec(code)
check('v2-contract-extracted', !!vcM, 'V2_CONTRACT not found in source')
if (vcM) {
  const assembled = s1 + '\n\n' + vcM[1]
  check('assembled-contract-intact', assembled.indexOf('OUTPUT PROTOCOL') >= 0 && assembled.indexOf('⟦cmd¦run=') >= 0
    && assembled.indexOf('[EXECUTION ENVIRONMENT]') < 0, 'assembled prompt broken')
}
// 7) 幂等:剥两次 = 剥一次
check('idempotent', withGate('1', () => _stripExecEnv(s1)) === s1, 'second strip mutated')

console.log(`\n== ${pass}/${pass + fail} PASS ==`)
if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1) }
console.log('VERDICT: GO (execenv-strip:门控开关/首轮剥净/增量no-op/尾部形态/契约完整/幂等/5处装配+重建路结构断言)')
