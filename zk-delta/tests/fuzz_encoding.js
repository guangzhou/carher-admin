'use strict'
/*
 * zk-delta/tests/fuzz_encoding.js —— 编码对抗性性质测试（验收项 ⑩）
 *
 * 为什么要有这个：
 *   ⑨（真实抓包金样）现在是红的，因为采集必须先把 Cursor 指到小代理。
 *   但 ⑨ 想抓的风险，本质不是"Cursor 说了什么话"，而是"Cursor 那一侧的 JSON
 *   序列化器写出来的字节，我这边能不能原样复现"。那是一个**编码**问题，
 *   不是一个**内容**问题。所以在拿到真实抓包之前，可以先用对抗性编码把这条
 *   风险面系统性地打一遍——而且打得比几十条真实样本更全。
 *
 *   这不能替代 ⑨。真实抓包还要采，采到了再重跑。这条只是让"没采到之前"
 *   不等于"没验过"。
 *
 * 要证的性质（对任意输入字节都必须成立）：
 *
 *   P1 【绝不说谎】proveRoundTrip 说 ok 的，走完整条增量链路之后，
 *      服务端重建出来的字节必须与原始字节完全相同。
 *      —— 说 ok 却重建错，是唯一会静默污染上游的失败模式，必须为 0。
 *
 *   P2 【拒得干净】proveRoundTrip 说 not ok 的，理由必须是已知那几种之一，
 *      不能是抛异常，也不能是没定义的字符串。拒了就走原样透传，本来就安全。
 *
 *   P3 【摘要不塌】两条 stringify 结果不同的 item，摘要必须不同。
 *      摘要相等被当成"同一条历史"，塌了就会把 A 的历史接到 B 的会话上。
 *
 *   P4 【增量与全量同解】把 items 从任意位置切成"服务端已存的前缀 + 本轮增量"，
 *      重建结果必须与一次性全量重建的结果逐字节相同。
 *
 * 用法：node zk-delta/tests/fuzz_encoding.js       （单独跑，自带汇总）
 *       run.js 里会 require 进去作为验收项 ⑩
 */

const F = require('../common/framing')

// ---------- 确定性随机（不用 Math.random，失败必须可复现） ----------
function mulberry32 (a) {
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

// ---------- 对抗性字面量池 ----------
// 全部写成 **JSON 源文本片段**，而不是 JS 值——因为要考的正是"源文本 → 解析 →
// 再序列化"这一趟里字节会不会变。写成 JS 值就把要考的东西提前抹平了。

const NUMS = [
  '1', '0', '-0', '1.0', '1.5', '1e3', '1E3', '1e+3', '1.230', '0.1',
  '1e400',            // 解析成 Infinity，再序列化变 null —— 必须被抓
  '1e-400',           // 下溢成 0
  '5e-324',           // 最小次正规数
  '12345678901234567890',   // 超出 double 精度，回写会变
  '2.220446049250313e-16',
  '-1.7976931348623157e308',
  '3.0000000000000004'
]

const STRS = [
  '"hi"',
  '"é"',                    // 字面非 ASCII
  '"\\u00e9"',              // 转义写法的同一个字符 —— 再序列化会变成字面 é
  '"\\ud83d\\ude00"',       // 代理对（emoji）
  '"😀"',                   // 同一个 emoji 的字面写法
  '"\\ud800"',              // 落单代理项
  '"\\u0000"',              // NUL
  '"\\u2028"',              // 行分隔符
  '"\\u007f"',              // DEL：stringify 不转义，源文本转义了 → 会变
  '"a\\/b"',                // 转义斜杠：JSON 合法，stringify 不会写回转义
  '"a\\nb"',
  '"a\\tb"',
  '"引号\\"里面"',
  '"\\\\"',
  '""',
  '"' + 'x'.repeat(600) + '"'
]

const ATOMS = ['null', 'true', 'false']

function pick (rnd, arr) { return arr[Math.floor(rnd() * arr.length) % arr.length] }

/** 随机生成一段 JSON 源文本（对象 / 数组 / 标量都可能） */
function genValueText (rnd, depth) {
  const r = rnd()
  if (depth <= 0 || r < 0.45) {
    const k = rnd()
    if (k < 0.4) return pick(rnd, STRS)
    if (k < 0.8) return pick(rnd, NUMS)
    return pick(rnd, ATOMS)
  }
  if (r < 0.72) {
    const n = Math.floor(rnd() * 4)
    const parts = []
    for (let i = 0; i < n; i++) parts.push(genValueText(rnd, depth - 1))
    return '[' + parts.join(',') + ']'
  }
  const n = Math.floor(rnd() * 4)
  const parts = []
  const keys = ['a', 'b', 'role', 'content', 'type', '1', '02', 'k-x', '键']
  for (let i = 0; i < n; i++) {
    parts.push('"' + pick(rnd, keys) + '":' + genValueText(rnd, depth - 1))
  }
  return '{' + parts.join(',') + '}'
}

/** 生成一条像 Cursor 请求的 body 源文本（对抗池：故意塞刁钻字面量） */
function genBodyText (rnd) {
  const n = 1 + Math.floor(rnd() * 5)
  const items = []
  for (let i = 0; i < n; i++) {
    const shape = rnd()
    if (shape < 0.6) {
      items.push('{"role":"' + (i % 2 ? 'assistant' : 'user') + '","content":' + pick(rnd, STRS) + '}')
    } else {
      items.push('{"role":"user","content":' + genValueText(rnd, 3) + '}')
    }
  }
  const key = rnd() < 0.5 ? 'messages' : 'input'
  const head = '{"model":"cursor-web-fc-82-terra","' + key + '":[' + items.join(',') + ']'
  const tail = ',"stream":true,"temperature":' + pick(rnd, NUMS) +
    ',"extra":' + genValueText(rnd, 2) + '}'
  return head + tail
}

// ---------- 干净池：真实形状 ----------
// 这一池是**用 JS 值构造、再 JSON.stringify 出来**的，也就是任何正常客户端
// （包括 Cursor）写出来的那种紧凑 JSON。它们必须 100% 走进增量——
// 走不进去就意味着 zk-delta 在真实流量上会静默退化成全量转发，一点带宽都省不到。
// 内容照样放 emoji、非 ASCII、深嵌套、长文本，只是编码形式是规范的。

const CLEAN_TEXTS = [
  '你好，帮我看下这个函数', 'refactor this', '日志里有 é 和 emoji 😀 还有 \n 换行',
  'a'.repeat(1200), '{"looks":"like json but is a string"}', '', 'tab\there',
  '中文标点，。！？和 ASCII 混排', 'path/with/slashes', 'quote " inside'
]

function genCleanValue (rnd, depth) {
  const r = rnd()
  if (depth <= 0 || r < 0.5) {
    const k = rnd()
    if (k < 0.5) return pick(rnd, CLEAN_TEXTS)
    if (k < 0.7) return Math.floor(rnd() * 100000)
    if (k < 0.8) return rnd() < 0.5
    if (k < 0.9) return null
    return Math.round(rnd() * 10000) / 100      // 两位小数，规范打印
  }
  if (r < 0.75) {
    const n = Math.floor(rnd() * 4)
    const out = []
    for (let i = 0; i < n; i++) out.push(genCleanValue(rnd, depth - 1))
    return out
  }
  const n = Math.floor(rnd() * 4)
  const out = {}
  const keys = ['type', 'text', 'id', 'name', 'arguments', 'cache_control', '键']
  for (let i = 0; i < n; i++) out[pick(rnd, keys)] = genCleanValue(rnd, depth - 1)
  return out
}

/** 生成一条干净的、真实形状的 body（返回其紧凑 JSON 文本） */
function genCleanBodyText (rnd) {
  const n = 1 + Math.floor(rnd() * 8)
  const items = []
  for (let i = 0; i < n; i++) {
    const it = { role: i % 2 ? 'assistant' : 'user' }
    if (rnd() < 0.65) it.content = pick(rnd, CLEAN_TEXTS)
    else it.content = [{ type: 'text', text: pick(rnd, CLEAN_TEXTS) }]
    if (rnd() < 0.3) it.tool_calls = [{ id: 'call_' + Math.floor(rnd() * 1e9).toString(36), type: 'function', function: { name: 'shell', arguments: JSON.stringify({ cmd: 'ls' }) } }]
    if (rnd() < 0.2) it.extra = genCleanValue(rnd, 2)
    items.push(it)
  }
  const body = {}
  body.model = 'cursor-web-fc-82-terra'
  if (rnd() < 0.5) body.temperature = 0
  body[rnd() < 0.5 ? 'messages' : 'input'] = items
  body.stream = true
  if (rnd() < 0.6) body.tools = [{ type: 'function', function: { name: 'shell', description: pick(rnd, CLEAN_TEXTS), parameters: { type: 'object', properties: { cmd: { type: 'string' } } } } }]
  if (rnd() < 0.3) body.reasoning_effort = 'medium'
  return JSON.stringify(body)
}

/** 明确构造的、必须被处理正确的刁钻样本（不靠随机撞） */
function handCrafted () {
  const K = 'messages'
  const wrap = (itemsText, extra) =>
    '{"model":"m","' + K + '":[' + itemsText + ']' + (extra || '') + '}'
  return [
    ['紧凑基线', wrap('{"role":"user","content":"hi"}')],
    ['美化缩进', JSON.stringify({ model: 'm', messages: [{ role: 'user', content: 'hi' }] }, null, 2)],
    ['冒号后带空格', '{"model": "m", "messages": [{"role": "user"}]}'],
    ['重复键', wrap('{"role":"user","role":"assistant"}')],
    ['整数样式的键排前', '{"b":1,"1":2,"messages":[{"x":1}],"a":3}'],
    ['转义 unicode', wrap('{"c":"\\u00e9"}')],
    ['字面 unicode', wrap('{"c":"é"}')],
    ['代理对转义', wrap('{"c":"\\ud83d\\ude00"}')],
    ['落单代理项', wrap('{"c":"\\ud800"}')],
    ['转义斜杠', wrap('{"c":"a\\/b"}')],
    ['DEL 字符转义', wrap('{"c":"\\u007f"}')],
    ['1.0 形式的数', wrap('{"n":1.0}')],
    ['科学计数', wrap('{"n":1e3}')],
    ['溢出成 Infinity', wrap('{"n":1e400}')],
    ['超精度整数', wrap('{"n":12345678901234567890}')],
    ['负零', wrap('{"n":-0}')],
    ['空数组不进增量', '{"model":"m","messages":[]}'],
    ['没有数组键', '{"model":"m","prompt":"hi"}'],
    ['顶层是数组', '[{"role":"user"}]'],
    ['压根不是 JSON', 'not json at all'],
    ['数组键在最后', '{"model":"m","stream":true,"messages":[{"x":1}]}'],
    ['数组键在最前', '{"messages":[{"x":1}],"model":"m"}'],
    ['深层嵌套', wrap('{"a":{"b":{"c":{"d":[1,2,{"e":"f"}]}}}}')],
    ['item 是标量', wrap('"just a string",42,null')],
    ['item 是数组', wrap('[1,2,3],{"role":"user"}')]
  ]
}

const KNOWN_REASONS = new Set([
  'not_json', 'not_object', 'no_array_key', 'empty_array', 'byte_mismatch'
])

/**
 * 走一遍完整的增量链路（把网络那一跳如实模拟成 stringify → parse），
 * 返回服务端最终会交给 LiteLLM 的字节。
 *
 * splitAt = 服务端已经存了多少条历史；剩下的当本轮增量发过去。
 */
function throughPipeline (proof, splitAt) {
  // —— 客户端：装信封 ——
  const stored = proof.items.slice(0, splitAt)
  const delta = proof.items.slice(splitAt)
  const envText = JSON.stringify({
    v: F.PROTO_VERSION,
    array_key: proof.arrayKey,
    template: proof.template,
    delta_items: delta,
    base_count: splitAt
  })
  // —— 网络：JSON 过一趟 ——
  const env = JSON.parse(envText)
  // —— 服务端：用"自己存的历史"+"收到的增量"重建 ——
  //    stored 也走一趟 JSON，模拟它当初也是这么传过来、被存下的
  const storedSrv = JSON.parse(JSON.stringify(stored))
  const itemsSrv = storedSrv.concat(env.delta_items)
  return JSON.stringify(F.rebuildBody(env.template, env.array_key, itemsSrv))
}

function run (ok) {
  let checked = 0
  let proved = 0
  let refused = 0
  const lies = []          // P1 反例：说 ok 却重建错
  const badReasons = []    // P2 反例
  const crashes = []
  const deltaDiffs = []    // P4 反例

  const cases = []
  for (const [name, text] of handCrafted()) cases.push({ name, text, pool: 'hand' })
  const rnd = mulberry32(0x5eed)
  for (let i = 0; i < 4000; i++) cases.push({ name: 'hostile#' + i, text: genBodyText(rnd), pool: 'hostile' })
  const rnd2 = mulberry32(0xc0ffee)
  for (let i = 0; i < 2000; i++) cases.push({ name: 'clean#' + i, text: genCleanBodyText(rnd2), pool: 'clean' })

  let cleanTotal = 0
  let cleanProved = 0
  const cleanRefused = []

  for (const c of cases) {
    checked++
    if (c.pool === 'clean') cleanTotal++
    let proof
    try {
      proof = F.proveRoundTrip(c.text)
    } catch (e) {
      crashes.push(c.name + ' :: ' + e.message)
      continue
    }

    if (!proof.ok) {
      refused++
      if (c.pool === 'clean' && cleanRefused.length < 5) cleanRefused.push(c.name + ' :: ' + proof.reason)
      if (!KNOWN_REASONS.has(proof.reason)) badReasons.push(c.name + ' :: ' + proof.reason)
      continue
    }

    proved++
    if (c.pool === 'clean') cleanProved++
    // P1：全量走一遍（服务端什么都没存）
    let full
    try {
      full = throughPipeline(proof, 0)
    } catch (e) {
      crashes.push(c.name + ' :: pipeline ' + e.message)
      continue
    }
    if (full !== c.text) {
      if (lies.length < 5) lies.push(c.name + ' :: 长度 ' + c.text.length + '→' + full.length)
      continue
    }

    // P4：在每一个切点上，增量重建都必须与全量重建同解
    for (let s = 0; s <= proof.items.length; s++) {
      const inc = throughPipeline(proof, s)
      if (inc !== full) { deltaDiffs.push(c.name + ' :: splitAt=' + s); break }
    }
  }

  ok('⑩a P1 说 ok 的一律逐字节重建正确（n=' + proved + '，含 ' + handCrafted().length + ' 条手工刁钻样本）',
    lies.length === 0, lies.join(' | '))
  ok('⑩b P2 拒绝理由全部在已知集合内（拒了 ' + refused + ' / ' + checked + '）',
    badReasons.length === 0, badReasons.slice(0, 5).join(' | '))
  ok('⑩c 全程零异常', crashes.length === 0, crashes.slice(0, 3).join(' | '))
  ok('⑩d P4 任意切点的增量重建 === 全量重建', deltaDiffs.length === 0, deltaDiffs.slice(0, 5).join(' | '))

  // P3：摘要不塌。拿全部样本里出现过的 item 去两两对撞。
  const seen = new Map()   // digest -> stringify 文本
  let collisions = 0
  for (const c of cases) {
    let p
    try { p = F.proveRoundTrip(c.text) } catch (e) { continue }
    if (!p.ok) continue
    for (const it of p.items) {
      const s = JSON.stringify(it)
      const d = F.itemDigest(it)
      const prev = seen.get(d)
      if (prev === undefined) seen.set(d, s)
      else if (prev !== s) collisions++
    }
  }
  ok('⑩e P3 摘要不塌（互不相同的 item 文本 ' + seen.size + ' 条，零碰撞）', collisions === 0, '碰撞 ' + collisions)

  // 覆盖率自检 1：干净的真实形状必须全部走进增量。
  // 这条不是"测试自检"，它本身就是产品判据——干净 body 被拒 = 静默退化成全量转发，
  // zk-delta 等于白装。所以要求 100%，不留余量。
  ok('⑩f 干净紧凑的真实形状 100% 走进增量（n=' + cleanTotal + '）',
    cleanProved === cleanTotal, cleanRefused.join(' | '))

  // 覆盖率自检 2：对抗池里如果一条都没走进增量，⑩a 就是空转的绿。
  ok('⑩f2 对抗池里仍有样本走进增量（否则 ⑩a 是空转）',
    proved - cleanProved > 50, '对抗池进增量 ' + (proved - cleanProved) + ' 条')

  // 反向自检：手工样本里那些"本来就该被拒"的，必须真的被拒。
  // 注意：落单代理项 "\ud800" **不在**这张表里 —— JSON.stringify 从 ES2019 起是
  // well-formed 的，落单代理会被写成 \ud800 转义形式，与源文本一致，所以它本来
  // 就能逐字节复现，是合法进增量的。第一版把它列成"必须拒绝"是我判据写错了。
  const mustRefuse = ['美化缩进', '冒号后带空格', '重复键', '转义 unicode', '溢出成 Infinity',
    '超精度整数', '空数组不进增量', '没有数组键', '顶层是数组', '压根不是 JSON',
    '转义斜杠', 'DEL 字符转义', '1.0 形式的数', '科学计数']
  const shouldHaveRefused = []
  for (const [name, text] of handCrafted()) {
    if (!mustRefuse.includes(name)) continue
    const p = F.proveRoundTrip(text)
    if (p.ok) shouldHaveRefused.push(name)
  }
  ok('⑩g 已知不可逐字节复现的写法确实被拒（' + mustRefuse.length + ' 条）',
    shouldHaveRefused.length === 0, '漏网: ' + shouldHaveRefused.join(','))

  return { checked, proved, refused }
}

module.exports = { run, handCrafted, throughPipeline }

if (require.main === module) {
  let pass = 0; let fail = 0
  const failures = []
  const ok = (name, cond, detail) => {
    if (cond) { pass++; console.log('  ✓ ' + name) } else {
      fail++; failures.push(name + (detail ? ' :: ' + detail : ''))
      console.log('  ✗ ' + name + (detail ? ' :: ' + detail : ''))
    }
  }
  console.log('zk-delta 编码对抗性性质测试\n' + '='.repeat(60))
  const st = run(ok)
  console.log('\n样本 ' + st.checked + ' 条：进增量 ' + st.proved + '，退全量 ' + st.refused)
  console.log('PASS ' + pass + ' / FAIL ' + fail)
  if (fail) { console.log('\n失败项：'); failures.forEach((f) => console.log('  - ' + f)) }
  process.exit(fail ? 1 : 0)
}
