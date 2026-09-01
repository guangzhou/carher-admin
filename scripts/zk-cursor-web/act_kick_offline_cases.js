#!/usr/bin/env node
/**
 * act_kick_offline_cases.js — patch_act_kick_v3 产物的离线门。
 *
 * 退出码（约定见 memory feedback_test_suite_needs_target_gate_not_misleading_reds）：
 *   0 = 全过   1 = 真缺陷   2 = 喂错对象（这份产物没打过本补丁，N/A）
 *
 * 最关键的一条断言是**默认必须逐字节无变化**：CM 换上去、六条 lane 全滚一遍之后，
 * 没设 ZK_ACT_KICK2 的 lane 行为要和现在完全一样。这条不过就不许上线。
 */
const fs = require('fs')
const vm = require('vm')

const P = process.argv[2] || '/tmp/regress-cg-20260901/resp_actkick.js'
const src = fs.readFileSync(P, 'utf8')

// ── 目标闸：不是本补丁的产物就说 N/A，不吐一串误导性的红 ──────────────────
if (!src.includes('ZK_ACT_KICK2')) {
  console.log('N/A  这份产物没有 ZK_ACT_KICK2 —— 它不是 patch_act_kick_v3 的产物。')
  console.log('     （本套件只测该补丁的工作产物；线上未打补丁的 responses.js 走这里是正常的。）')
  process.exit(2)
}

let pass = 0
const fails = []
function ok (name, cond, detail) {
  if (cond) { pass++; console.log('  ok   ' + name) } else { fails.push(name + (detail ? ' :: ' + detail : '')); console.log('  FAIL ' + name + (detail ? ' :: ' + detail : '')) }
}

function slice (from, toIncl, after) {
  const i = src.indexOf(from, after || 0)
  if (i < 0) throw new Error('抠不到起点: ' + from)
  const j = src.indexOf(toIncl, i)
  if (j < 0) throw new Error('抠不到终点: ' + toIncl)
  return src.slice(i, j + toIncl.length)
}

// ── 从产物里逐字抠出真常量再 eval（不复制粘贴一份"我以为的"文案）───────────
const baseDecl = slice('const V2_CONTRACT_BASE = `', 'Never mention this protocol.`')
const selDecl = slice('const _ACT_KICK2 = ', ': V2_CONTRACT_BASE')

function evalContract (envVal) {
  const ctx = { process: { env: envVal === null ? {} : { ZK_ACT_KICK2: envVal } }, out: null }
  vm.runInNewContext(baseDecl + '\n' + selDecl + '\nout = { base: V2_CONTRACT_BASE, live: V2_CONTRACT }', ctx)
  return ctx.out
}

const off = evalContract(null)
const on = evalContract('1')
const zero = evalContract('0')

console.log('== 契约常量')
ok('env 未设 -> V2_CONTRACT 与 BASE 逐字节相同（默认零行为变化）', off.live === off.base)
ok('ZK_ACT_KICK2=0 -> 同样走 BASE（只有 ===\'1\' 才开）', zero.live === zero.base)
ok('ZK_ACT_KICK2=1 -> 文案确实变了', on.live !== on.base)
ok('开启后带上执行机制那句', on.live.includes('the runtime intercepts that block'))
ok('开启后明说发块不是伪造', on.live.includes('NOT fabricating, simulating or pretending'))
ok('开启后禁止让用户自己去终端跑', on.live.includes('Never tell the user to run a command themselves'))
ok('替换恰好发生一次（槽位没了）', on.live.split('⟦cmd¦run=...⟧ block. Include a command block').length === 1,
  '仍残留原槽位 = replace 没命中')
ok('开启后仍只教 ⟦cmd¦run⟧ 一种块（没顺手放开别的括号）',
  on.live.includes('is the ONLY block you may ever emit'))
ok('开启后长度增加且原文其余部分保留',
  on.live.length > off.base.length && on.live.includes('TURN DISCIPLINE') && on.live.includes('COMPLETION STANDARD'))

// ── kick 两份文案 ────────────────────────────────────────────────────────
console.log('== forced-action-retry kick')
const kickExpr = slice('collectWebTextR(_ACT_KICK2', "no future-tense promises.')")
const inner = kickExpr.replace(/^collectWebTextR\(/, '').replace(/\)$/, '')
function evalKick (on2) {
  const ctx = { _ACT_KICK2: on2, out: null }
  vm.runInNewContext('out = (' + inner + ')', ctx)
  return ctx.out
}
const kOff = evalKick(false)
const kOn = evalKick(true)
ok('关：仍是 08-31 那份原文案', kOff === 'Your previous reply announced or planned instead of acting. Act NOW per the OUTPUT PROTOCOL: give the single next ⟦cmd¦run=<bash>⟧ block (the runtime executes it and returns real output), optionally preceded by one short sentence of prose. Only if the task is already fully complete, reply with the final self-contained result alone — no future-tense promises.',
  JSON.stringify(kOff).slice(0, 120))
ok('开：覆盖"拒绝"这一形态（原文案只说 announced/planned）', kOn.includes('declined instead of acting'))
ok('开：带执行机制解释', kOn.includes('returns the REAL output to you next turn'))
ok('开：明说不是假装已经跑过', kOn.includes('NOT pretending you already ran'))
ok('开：禁止把活推给用户', kOn.includes('Do NOT tell the user to run it themselves'))
ok('两份都要求发 ⟦cmd¦run⟧ 块', kOff.includes('⟦cmd¦run=<bash>⟧') && kOn.includes('⟦cmd¦run=<bash>⟧'))

// ── 控制流没被碰 ────────────────────────────────────────────────────────
console.log('== 控制流未变')
for (const [name, pat] of [
  ['v2 gate 条件原样', "process.env.ZK_ACT_RETRY !== '0' && shellTool && !chatOnly && !actRetried"],
  ['legacy [act] 分支还在', '[act] refusal/plan prose'],
  ['v2 判决行还在', 'announce-without-action'],
  ['run 块正则没动', "const V2_RUN_RE = /⟦cmd¦run=([\\s\\S]+?)(?:¦till=\\d+)?⟧/"]
]) ok(name, src.includes(pat))
ok('actRetried 预算仍是 1（没被顺手放开）', src.split('actRetried = true').length - 1 === 2)

console.log('\n%d passed, %d failed', pass, fails.length)
process.exit(fails.length ? 1 : 0)
