#!/usr/bin/env node
/* conv_persist_offline_cases.js — ROI #1 conv 缓存持久化离线单测。
 *
 * 不重实现:用括号配平从 responses.js 里逐字抠出 _persistConvCache / _loadConvCache 的
 * 真实字节,注入依赖(fs / _convCache / _CONV_PERSIST / _CONV_CACHE_FILE / _CONV_TTL_MS /
 * console)后 eval 出来驱动。测的是上线那份代码本身,不是镜像。
 *
 * 覆盖:①往返(save 落盘→新 Map 加载回等价)②TTL 过期项跳过 ③损坏 JSON 冷启动不崩
 *   ④文件缺失冷启动 ⑤畸形条目(缺 convId/digests/count)跳过 ⑥非数组冷启动
 *   ⑦原子写(tmp+rename,中途无半截文件)⑧默认关(_CONV_PERSIST=false 时不读不写)。
 *
 * 用法: node scripts/zk-cursor-web/conv_persist_offline_cases.js [/path/to/responses.js]
 */
'use strict'
const fs = require('fs')
const os = require('os')
const path = require('path')

const SRC = process.argv[2] || '/tmp/resp_live_82.js'
const code = fs.readFileSync(SRC, 'utf8')

// —— 括号配平抠函数体(含 `function NAME(...) { ... }`)——
function grabFn(name) {
  const start = code.indexOf('function ' + name + '(')
  if (start < 0) throw new Error('cannot find function ' + name + ' in ' + SRC)
  const open = code.indexOf('{', start)
  let depth = 0
  for (let i = open; i < code.length; i++) {
    const ch = code[i]
    if (ch === '{') depth++
    else if (ch === '}') { depth--; if (depth === 0) return code.slice(start, i + 1) }
  }
  throw new Error('unbalanced braces for ' + name)
}
const persistSrc = grabFn('_persistConvCache')
const loadSrc = grabFn('_loadConvCache')

const TTL_MS = 240 * 60 * 1000
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'convpersist-'))
const FILE = path.join(tmpDir, 'conv-cache.json')

// 用注入依赖构造真实函数(persist 关=false / 开=true 两态各造一套)
function build(persistOn, file) {
  const cache = new Map()
  // eslint-disable-next-line no-new-func
  const factory = new Function(
    'fs', '_convCache', '_CONV_PERSIST', '_CONV_CACHE_FILE', '_CONV_TTL_MS', 'console',
    persistSrc + '\n' + loadSrc + '\nreturn { _persistConvCache, _loadConvCache, _convCache }'
  )
  return factory(fs, cache, persistOn, file, TTL_MS, console)
}

let pass = 0, fail = 0
const fails = []
function check(name, cond, why) {
  if (cond) { pass++; console.log(`[PASS] ${name}`) }
  else { fail++; fails.push({ name, why }); console.log(`[FAIL] ${name} — ${why}`) }
}
function reset() { try { fs.rmSync(FILE, { force: true }); fs.rmSync(FILE + '.tmp', { force: true }) } catch (_) {} }

// —— ① 往返 ——
;(function roundtrip() {
  reset()
  const A = build(true, FILE)
  const now = Date.now()
  A._convCache.set('k1', { convId: 'conv-aaaa1111', parentId: 'client-created-root', count: 3, digests: ['d0', 'd1', 'd2'], ts: now })
  A._convCache.set('k2', { convId: 'conv-bbbb2222', parentId: 'pm-xyz', count: 1, digests: ['e0'], ts: now })
  A._persistConvCache()
  check('roundtrip-file-written', fs.existsSync(FILE), 'file missing after persist')
  const B = build(true, FILE)
  B._loadConvCache()
  check('roundtrip-count', B._convCache.size === 2, `size=${B._convCache.size}`)
  const g = B._convCache.get('k1')
  check('roundtrip-fields', !!g && g.convId === 'conv-aaaa1111' && g.count === 3 && g.parentId === 'client-created-root' && g.digests.length === 3, `k1=${JSON.stringify(g)}`)
  const g2 = B._convCache.get('k2')
  check('roundtrip-fields2', !!g2 && g2.convId === 'conv-bbbb2222' && g2.parentId === 'pm-xyz', `k2=${JSON.stringify(g2)}`)
})()

// —— ② TTL 过期项跳过 ——
;(function expired() {
  reset()
  const now = Date.now()
  const arr = [
    { key: 'fresh', convId: 'conv-fresh', parentId: 'client-created-root', count: 1, digests: ['a'], ts: now },
    { key: 'old', convId: 'conv-old', parentId: 'client-created-root', count: 1, digests: ['b'], ts: now - TTL_MS - 1000 },
  ]
  fs.writeFileSync(FILE, JSON.stringify(arr))
  const B = build(true, FILE)
  B._loadConvCache()
  check('expired-skipped', B._convCache.size === 1 && B._convCache.has('fresh') && !B._convCache.has('old'), `size=${B._convCache.size} keys=${[...B._convCache.keys()]}`)
})()

// —— ③ 损坏 JSON 冷启动不崩 ——
;(function corrupt() {
  reset()
  fs.writeFileSync(FILE, '{ this is not json ][')
  const B = build(true, FILE)
  let threw = false
  try { B._loadConvCache() } catch (_) { threw = true }
  check('corrupt-no-throw', !threw, 'loadConvCache threw on corrupt file')
  check('corrupt-empty', B._convCache.size === 0, `size=${B._convCache.size}`)
})()

// —— ④ 文件缺失冷启动 ——
;(function missing() {
  reset()
  const B = build(true, FILE)
  let threw = false
  try { B._loadConvCache() } catch (_) { threw = true }
  check('missing-no-throw', !threw, 'threw on missing file')
  check('missing-empty', B._convCache.size === 0, `size=${B._convCache.size}`)
})()

// —— ⑤ 畸形条目跳过 ——
;(function malformed() {
  reset()
  const now = Date.now()
  const arr = [
    { key: 'good', convId: 'conv-good', parentId: 'client-created-root', count: 1, digests: ['a'], ts: now },
    { key: 'no-conv', parentId: 'x', count: 1, digests: ['b'], ts: now },       // 缺 convId
    { key: 'no-digests', convId: 'c', count: 1, ts: now },                       // 缺 digests
    { convId: 'no-key', count: 1, digests: ['d'], ts: now },                     // 缺 key
    { key: 'no-count', convId: 'e', digests: ['f'], ts: now },                   // 缺 count
    { key: 'no-ts', convId: 'g', count: 1, digests: ['h'] },                     // 缺 ts
    null,                                                                        // null 条目
  ]
  fs.writeFileSync(FILE, JSON.stringify(arr))
  const B = build(true, FILE)
  B._loadConvCache()
  check('malformed-only-good', B._convCache.size === 1 && B._convCache.has('good'), `size=${B._convCache.size} keys=${[...B._convCache.keys()]}`)
})()

// —— ⑥ 非数组冷启动 ——
;(function notarray() {
  reset()
  fs.writeFileSync(FILE, JSON.stringify({ k: 'v' }))
  const B = build(true, FILE)
  let threw = false
  try { B._loadConvCache() } catch (_) { threw = true }
  check('notarray-no-throw', !threw, 'threw on non-array')
  check('notarray-empty', B._convCache.size === 0, `size=${B._convCache.size}`)
})()

// —— ⑦ 原子写:persist 后无残留 .tmp ——
;(function atomic() {
  reset()
  const A = build(true, FILE)
  A._convCache.set('k', { convId: 'c', parentId: 'client-created-root', count: 1, digests: ['a'], ts: Date.now() })
  A._persistConvCache()
  check('atomic-no-tmp-left', !fs.existsSync(FILE + '.tmp'), '.tmp residue after persist')
})()

// —— ⑧ 默认关(_CONV_PERSIST=false):不读不写 ——
;(function disabled() {
  reset()
  const A = build(false, FILE)
  A._convCache.set('k', { convId: 'c', parentId: 'client-created-root', count: 1, digests: ['a'], ts: Date.now() })
  A._persistConvCache()
  check('disabled-no-write', !fs.existsSync(FILE), 'wrote file while disabled')
  // 即使文件存在,disabled 时也不加载
  fs.writeFileSync(FILE, JSON.stringify([{ key: 'x', convId: 'c', parentId: 'p', count: 1, digests: ['a'], ts: Date.now() }]))
  const B = build(false, FILE)
  B._loadConvCache()
  check('disabled-no-load', B._convCache.size === 0, `size=${B._convCache.size}`)
})()

try { fs.rmSync(tmpDir, { recursive: true, force: true }) } catch (_) {}

console.log(`\n== ${pass}/${pass + fail} PASS ==`)
if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1) }
console.log('VERDICT: GO (往返/过期/损坏/缺失/畸形/非数组/原子写/默认关 全绿)')
