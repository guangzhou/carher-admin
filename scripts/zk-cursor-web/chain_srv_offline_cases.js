#!/usr/bin/env node
// chain_srv_offline_cases.js — 件A chain-srv(服务端 previous_response_id 链式)离线单测。
// 纪律:抠的是补丁后那份真字节(逐字 regex 提取 + eval),不是自造格式;含真回路与负对照。
// 用法: node chain_srv_offline_cases.js /path/to/resp_chain_work.js
'use strict'
const fs = require('fs')

const file = process.argv[2] || '/tmp/resp_chain_work.js'
const src = fs.readFileSync(file, 'utf8')

// ── 目标闸:喂错文件要说"不适用",不能吐一串误导性 FAIL ──────────────────────
// 09-01 实际踩到:批量回归把线上 responses.js 喂进来,于是 8 条断言集体红。
// 那不是缺陷,是量错了对象——而这种红最危险:下一步很容易变成"改产品去满足断言"。
// chain-srv 是 ZK_CHAIN_SRV 门控的服务端补丁,只存在于补丁后的工作产物里;
// 线上 responses.js 压根没有这段(2026-09-01 起服务端那半已回滚)。
// 退出码分家:0=全过 / 1=真缺陷 / 2=喂错对象(N/A)。
if (!src.includes('_CHAIN_SRV') && !src.includes('chain-srv')) {
  console.log('N/A: %s 里没有 chain-srv 补丁痕迹(ZK_CHAIN_SRV/_chainRemember)。', file)
  console.log('    本套件只测 chain-srv 工作产物(如 /tmp/resp_chain_work.js);')
  console.log('    线上 responses.js 不含这段(服务端那半已回滚)→ 无可测对象,不算缺陷。')
  process.exit(2)
}

let pass = 0, fail = 0
const fails = []
function check(name, cond, why) {
  if (cond) { pass++; console.log('[PASS] ' + name) }
  else { fail++; fails.push({ name, why }); console.log('[FAIL] ' + name + ' — ' + (why || '')) }
}

// ── ① 结构断言:四个补丁点都在、且只在一处 ──
check('helpers 声明存在', src.includes("const _CHAIN_SRV = process.env.ZK_CHAIN_SRV === '1'"))
check('destructure 已是 let', src.includes('let { input, instructions, stream = false } = req.body'))
check('const destructure 已不存在', !src.includes('const { input, instructions, stream = false } = req.body'))
check('前置重建块存在', src.includes('chain-srv 前置重建'))
check('remember 挂在 respId 之后', /const mdl = req\.body\.model \|\| model \|\| 'chatgpt-web'\n\s*_chainRemember\(respId, input, instructions\)/.test(src))
check('404 错误形状含 code', src.includes("code: 'previous_response_not_found'"))
check('_chainRemember 恰 1 处定义', (src.match(/function _chainRemember/g) || []).length === 1)
check('_chainRemember 恰 1 处调用', (src.match(/^\s*_chainRemember\(respId/gm) || []).length === 1)

// ── ② 抠真字节:helpers 函数体 eval(带假 env/假 _CONV_TTL_MS)──
function extract(re, label) {
  const m = src.match(re)
  if (!m) throw new Error('extract fail: ' + label)
  return m[0]
}
const helperCode = extract(/const _CHAIN_SRV[\s\S]*?function _chainLookup[\s\S]*?\n}/, 'helpers')

function makeSandbox(gateOn, ttlMs) {
  const sandbox = { process: { env: { ZK_CHAIN_SRV: gateOn ? '1' : '0' } }, Map, Date, console }
  const code = '(function(){ const _CONV_TTL_MS = ' + ttlMs + ';\n' + helperCode +
    '\nreturn { _chainRemember, _chainLookup, _chainMap, _CHAIN_SRV } })()'
  // eslint-disable-next-line no-eval
  return (function () { const process = sandbox.process; return eval(code) })()
}

// 门控关:remember 是 no-op
{
  const s = makeSandbox(false, 999999)
  s._chainRemember('resp_a', [{ x: 1 }], 'ins')
  check('② 门关 remember no-op', s._chainMap.size === 0, 'size=' + s._chainMap.size)
}
// 门控开:真回路 remember→lookup→内容一致
{
  const s = makeSandbox(true, 999999)
  const items = [{ type: 'message', role: 'user', content: 'A' }, { type: 'message', role: 'user', content: 'B' }]
  s._chainRemember('resp_b', items, 'INS')
  const hit = s._chainLookup('resp_b')
  check('② 真回路 hit', !!hit && hit.input.length === 2 && hit.instructions === 'INS', JSON.stringify(hit))
  check('② 未知 id miss', s._chainLookup('resp_nope') === null)
}
// TTL 过期清除
{
  const s = makeSandbox(true, -1) // 立即过期
  s._chainRemember('resp_c', [{ a: 1 }], '')
  check('② TTL 过期 -> null 且清条目', s._chainLookup('resp_c') === null && s._chainMap.size === 0)
}
// LRU cap 100
{
  const s = makeSandbox(true, 999999)
  for (let i = 0; i < 105; i++) s._chainRemember('r' + i, [{ i }], '')
  check('② LRU cap=100', s._chainMap.size === 100, 'size=' + s._chainMap.size)
  check('② 最老被逐出', s._chainLookup('r0') === null && !!s._chainLookup('r104'))
}

// ── ③ 抠真字节:前置重建块,mock req/res 跑 hit/miss 两路 ──
const blockCode = extract(/if \(_CHAIN_SRV && req\.body\.previous_response_id\) \{[\s\S]*?full=' \+ input\.length\)\n    \}/, 'preblock')
function runBlock(gateOn, prevId, bodyInput, stored) {
  const s = makeSandbox(gateOn, 999999)
  if (stored) s._chainRemember(stored.id, stored.input, stored.instructions)
  const req = { body: { previous_response_id: prevId, input: bodyInput } }
  let statusCode = null, jsonBody = null
  const res = { status(c) { statusCode = c; return this }, json(b) { jsonBody = b; return this } }
  let input = bodyInput, instructions = null
  const _CHAIN_SRV = s._CHAIN_SRV, _chainLookup = s._chainLookup
  const wrapped = '(function(req,res){ let input = req.body.input, instructions = null;\n' +
    blockCode + '\nreturn { input, instructions, statusCode: null } })'
  // eslint-disable-next-line no-eval
  const fn = eval(wrapped)
  const out = fn(req, res)
  return { out, statusCode, jsonBody }
}
// hit:重建 = stored + delta,instructions 继承
{
  const stored = { id: 'resp_h', input: [{ m: 'old1' }, { m: 'old2' }], instructions: 'SYS' }
  const r = runBlock(true, 'resp_h', [{ m: 'new' }], stored)
  check('③ hit 重建顺序 stored+delta', r.out && r.out.input.length === 3 && r.out.input[2].m === 'new', JSON.stringify(r.out && r.out.input))
  check('③ hit instructions 继承', r.out.instructions === 'SYS', String(r.out.instructions))
}
// hit:字符串 input 包装成 message item
{
  const stored = { id: 'resp_s', input: [{ m: 'x' }], instructions: '' }
  const r = runBlock(true, 'resp_s', '你好', stored)
  check('③ 字符串 delta 包装', r.out.input.length === 2 && r.out.input[1].type === 'message', JSON.stringify(r.out.input[1]))
}
// miss:400 + 标准错误形状(400 而非 404:litellm 对 404 内部重试 3 次且计入 allowed_fails
// 触发 deployment 冷却 — 08-30 实测把 82-terra 打进 10min+ 内存冷却;400=BadRequest 不重试不冷却。
// 客户端回退契约认 error.code=previous_response_not_found,不认 HTTP 状态。)
{
  const r = runBlock(true, 'resp_missing', [{ m: 'n' }], null)
  check('③ miss -> 400', r.statusCode === 400, 'status=' + r.statusCode)
  check('③ miss 错误形状', r.jsonBody && r.jsonBody.error && r.jsonBody.error.code === 'previous_response_not_found', JSON.stringify(r.jsonBody))
}
// 门关 + 带 prev id:整块跳过(零行为差)
{
  const r = runBlock(false, 'resp_h', [{ m: 'n' }], null)
  check('③ 门关零行为差', r.statusCode === null && r.out.input.length === 1, JSON.stringify(r))
}

// ── ④ 负对照:未打补丁的字节(去掉标记)应 FAIL 结构断言 ──
{
  const virgin = src.replace(/chain-srv/g, 'XXXX')
  check('④ 负对照(控制组)', !virgin.includes('chain-srv'))
}

console.log('\n== ' + pass + '/' + (pass + fail) + ' PASS ==')
if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 1)); process.exit(1) }
console.log('VERDICT: GO')
