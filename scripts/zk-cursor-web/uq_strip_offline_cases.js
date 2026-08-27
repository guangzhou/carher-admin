#!/usr/bin/env node
/* uq_strip_offline_cases.js — <user_query> 信封剥离离线单测(ZK_STRIP_UQ 门控)。
 *
 * 括号配平从 responses.js 逐字抠 `if (process.env.ZK_STRIP_UQ === '1' && convSess ...)` 真块,
 * 注入 process/convSess/prompt/console 驱动。测的是上线那份门控本身。
 *
 * 覆盖:①开+增量轮→包装剥净,正文与 USER: 行标保留 ②行内散文提及("denoted by the
 *   <user_query> tag")不动 ③默认关→原样 ④首轮(convSess=null)→原样 ⑤多个包装块全剥
 *   ⑥无标签 prompt 零改动(且不打日志)。
 *
 * 用法: node scripts/zk-cursor-web/uq_strip_offline_cases.js [/path/to/responses.js]
 */
'use strict'
const fs = require('fs')

const SRC = process.argv[2] || '/tmp/resp_live_82.js'
const code = fs.readFileSync(SRC, 'utf8')

const anchor = "if (process.env.ZK_STRIP_UQ === '1' && convSess"
function grabIfBlock(a) {
  const start = code.indexOf(a)
  if (start < 0) throw new Error('cannot find ' + a)
  const open = code.indexOf('{', start)
  let depth = 0
  for (let i = open; i < code.length; i++) {
    const ch = code[i]
    if (ch === '{') depth++
    else if (ch === '}') { depth--; if (depth === 0) return code.slice(start, i + 1) }
  }
  throw new Error('unbalanced braces')
}
const block = grabIfBlock(anchor)

function run(env, convSess, prompt) {
  const logs = []
  // eslint-disable-next-line no-new-func
  const fn = new Function('process', 'convSess', 'prompt', 'console', block + '\n return prompt')
  const out = fn({ env }, convSess, prompt, { log: (s) => logs.push(s) })
  return { out, logs }
}

let pass = 0, fail = 0
const fails = []
function check(name, cond, why) {
  if (cond) { pass++; console.log(`[PASS] ${name}`) }
  else { fail++; fails.push({ name, why }); console.log(`[FAIL] ${name} — ${why}`) }
}

const SESS = { convId: 'conv-x', count: 8 }
const WRAPPED = 'USER: <user_query>\n帮我梳理代码结构 go\n</user_query>'
const PROSE = 'the user request is denoted by the <user_query> tag in the message.'

// ① 开+增量轮:包装剥净,正文/行标保留
{
  const { out, logs } = run({ ZK_STRIP_UQ: '1' }, SESS, WRAPPED)
  check('strip-on-delta', out === 'USER: 帮我梳理代码结构 go', JSON.stringify(out))
  check('strip-logs', logs.length === 1 && /uq-strip/.test(logs[0]), JSON.stringify(logs))
}
// ② 行内散文提及不动
{
  const { out } = run({ ZK_STRIP_UQ: '1' }, SESS, PROSE)
  check('prose-mention-untouched', out === PROSE, JSON.stringify(out))
}
// ③ 默认关→原样
{
  const { out } = run({}, SESS, WRAPPED)
  check('default-off-noop', out === WRAPPED, 'stripped while gate off')
}
// ④ 首轮→原样(即便开)
{
  const { out } = run({ ZK_STRIP_UQ: '1' }, null, WRAPPED)
  check('first-turn-noop', out === WRAPPED, 'stripped on first turn')
}
// ⑤ 多块全剥
{
  const two = WRAPPED + '\n\nUSER: <user_query>\nsecond one\n</user_query>'
  const { out } = run({ ZK_STRIP_UQ: '1' }, SESS, two)
  check('multi-block-strip', out === 'USER: 帮我梳理代码结构 go\n\nUSER: second one', JSON.stringify(out))
}
// ⑥ 无标签零改动且不打日志
{
  const plain = 'USER: just text, no tags'
  const { out, logs } = run({ ZK_STRIP_UQ: '1' }, SESS, plain)
  check('no-tags-noop-silent', out === plain && logs.length === 0, JSON.stringify({ out, logs }))
}

console.log(`\n== ${pass}/${pass + fail} PASS ==`)
if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1) }
console.log('VERDICT: GO (增量剥净/散文不动/默认关/首轮不动/多块/无标签静默)')
