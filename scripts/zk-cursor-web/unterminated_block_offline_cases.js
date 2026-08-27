#!/usr/bin/env node
/* unterminated_block_offline_cases.js — 未闭合 ⟦cmd¦run= 块打捞离线单测。
 *
 * 背景(2026-08-27 真流量实锤×2):模型发 9702c/10493c 巨型 heredoc 脚本块,缺闭合 ⟧,
 * V2_RUN_RE 不匹配 → complete-prose 把整坨(prose+裸协议块)交付用户=裸漏。
 *
 * 覆盖:①闭合多行块(heredoc 带 ⟧)正常匹配(不误伤既有路径)②未闭合块不匹配且
 *   lastIndexOf 检测命中 ③prose 前缀抽取正确(闭合块剥净,未闭合块之后全部不进 prose)
 *   ④打捞分支存在且含三不变量:不执行(分支内无 execToToolCall)/重发教学(闭合+分步)/
 *   诚实兜底(截断...未执行)。
 *
 * 用法: node scripts/zk-cursor-web/unterminated_block_offline_cases.js [/path/to/responses.js]
 */
'use strict'
const fs = require('fs')

const SRC = process.argv[2] || '/tmp/resp_live_82.js'
const code = fs.readFileSync(SRC, 'utf8')

// —— 抠真 V2_RUN_RE ——
const _reM = code.match(/const V2_RUN_RE = (\/.*\/)\n/)
if (!_reM) { console.log('[FAIL] cannot grab V2_RUN_RE'); process.exit(1) }
// eslint-disable-next-line no-new-func
const V2_RUN_RE = new Function('return ' + _reM[1])()

let pass = 0, fail = 0
const fails = []
function check(name, cond, why) {
  if (cond) { pass++; console.log(`[PASS] ${name}`) }
  else { fail++; fails.push({ name, why }); console.log(`[FAIL] ${name} — ${why}`) }
}

// ① 闭合多行 heredoc 块正常匹配
{
  const closed = '收尾说明。⟦cmd¦run=cd /tmp && python3 - <<\'PY\'\nprint("hi")\nPY⟧'
  const m = V2_RUN_RE.exec(closed)
  check('closed-multiline-matches', !!(m && m[1].includes('python3')), JSON.stringify(m && m[1].slice(0, 40)))
}

// ② 未闭合块:正则不匹配,lastIndexOf 检测命中
{
  const open = '继续落到原文档:补充模块规模。 ⟦cmd¦run=cd /repo && python3 - <<\'PY\'\nfrom pathlib import Path\nextra = f"""...15...20..."""\n'
  const m = V2_RUN_RE.exec(open)
  check('unterminated-no-match', !m, 'regex must not match without closing ⟧')
  check('unterminated-detected', open.lastIndexOf('⟦cmd¦run=') >= 0, 'lastIndexOf must find open block')
}

// ③ prose 前缀抽取(照搬上线逻辑:slice 到块首,剥闭合块)
{
  const full = '前言正文。⟦ls¦path=/x⟧ 中段。⟦cmd¦run=giant script never closes\nline2\n'
  const _v2open = full.lastIndexOf('⟦cmd¦run=')
  const prosePre = full.slice(0, _v2open).replace(/⟦[\s\S]*?⟧/g, '').trim()
  check('prose-prefix-clean', prosePre === '前言正文。 中段。', JSON.stringify(prosePre))
  check('block-not-in-prose', !prosePre.includes('⟦') && !prosePre.includes('giant'), 'raw block leaked into prose')
}

// ④ 打捞分支不变量(源码断言)
{
  const i = code.indexOf('run-block-unterminated -> same-conv resend')
  check('salvage-branch-exists', i > 0, 'branch marker missing')
  if (i > 0) {
    // 分支体:从检测 if 到诚实兜底 return 之间不得出现 execToToolCall(截断脚本绝不执行)
    const seg = code.slice(code.lastIndexOf('const _v2open', i), code.indexOf('finishWebTools', i + 1) + 400)
    check('salvage-never-executes', !seg.includes('execToToolCall'), 'salvage branch must not execute truncated cmd')
    check('salvage-teaches-close', /UNTERMINATED[\s\S]*closing ⟧[\s\S]*SHORT/.test(seg), 'resend teaching must mention closing + keeping short')
    check('salvage-honest-fallback', seg.includes('被截断,未执行'), 'honest note missing')
    check('salvage-budget-shared', seg.includes('bpiBlockRetried'), 'must share one-shot resend budget')
  }
}

console.log(`\n== ${pass}/${pass + fail} PASS ==`)
if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1) }
console.log('VERDICT: GO (闭合匹配不误伤/未闭合检测/prose前缀净/不执行/教学/诚实兜底/共享预算)')
