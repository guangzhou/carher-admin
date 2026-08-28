#!/usr/bin/env node
'use strict'
/* conn_watchdog_offline_cases.js — injected 轮连接器引用泄漏看门狗(结构+门控断言)。
 *
 * 背景(2026-08-28 深夜实锤):rendezvous PREFERRED 路模型侧首次 fire ×2 后,连接器存活
 * 40min 零 release 零 teardown。机制:seam3 injected 提前 return 不 release(等 turn-2 在
 * L827 配平),turn-2 永不来(客户端弃单/探针不喂)→ conn-mgr refCount 永卡 ≥1 → 连接器
 * 不死(违反"用完即拆"纪律,orphan 只有 pod 重启才清)。
 *
 * 修:turn-1 injected 在 ctx 上挂幂等 connRelease + 看门狗 setTimeout(默认 10min,
 * ZK_MCP_REL_WATCHDOG_MS 调期,unref 不阻退出);turn-2 HIT 走同一幂等出口,先到先放不双放。
 *
 * 用法: node scripts/zk-cursor-web/conn_watchdog_offline_cases.js [/path/to/responses.js]
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

// —— ① turn-1 injected 分支:幂等 release + 看门狗 ——
const injIdx = code.indexOf("console.log('[mcp-bridge] turn-1 injected call_id=")
check('injected-branch-present', injIdx >= 0, 'injected branch missing')
const inj = code.slice(injIdx, injIdx + 1600)
check('idempotent-release-hook', /_bctx\.connRelease = \(\) => \{\s*\n\s*if \(_bctx\._connReleased\) return\s*\n\s*_bctx\._connReleased = true/.test(inj),
  'ctx-carried idempotent connRelease missing (双放防线)')
check('watchdog-timer-armed', /setTimeout\(\(\) => \{[\s\S]*?_bctx\.connRelease\(\)\s*\n\s*\}, _wdMs\)/.test(inj),
  'watchdog timer must call connRelease at expiry')
check('watchdog-logs-leak', /watchdog release \(turn-2 never arrived\)/.test(inj),
  'watchdog fire must log (soak 可统计泄漏轮)')
check('watchdog-unref', /if \(_wdT\.unref\) _wdT\.unref\(\)/.test(inj), 'timer must unref (不阻进程退出)')
check('injected-still-returns', /return\s*\n\s*\}/.test(inj), 'injected branch must still early-return (turn-2 配平语义不变)')

// —— ② 门控真值:默认 600000,下限 60000,垃圾回默认 ——
const wdM = /const _wdMs = (Math\.max\(60000, parseInt\(process\.env\.ZK_MCP_REL_WATCHDOG_MS \|\| '600000', 10\) \|\| 600000\))/.exec(code)
check('watchdog-gate-line', !!wdM, '_wdMs gate line missing or shape drifted')
if (wdM) {
  const evalGate = (v) => {
    const old = process.env.ZK_MCP_REL_WATCHDOG_MS
    if (v === undefined) delete process.env.ZK_MCP_REL_WATCHDOG_MS
    else process.env.ZK_MCP_REL_WATCHDOG_MS = v
    // eslint-disable-next-line no-eval
    try { return eval(wdM[1]) } finally {
      if (old === undefined) delete process.env.ZK_MCP_REL_WATCHDOG_MS
      else process.env.ZK_MCP_REL_WATCHDOG_MS = old
    }
  }
  check('wd-default-10min', evalGate(undefined) === 600000, 'default must be 600000')
  check('wd-floor-60s', evalGate('5000') === 60000, 'must clamp to 60s floor (误配不抖)')
  check('wd-garbage-default', evalGate('abc') === 600000, 'garbage must fall back to default')
  check('wd-custom', evalGate('120000') === 120000, 'custom value must apply')
}

// —— ③ turn-2 HIT 侧:同一幂等出口,老 ctx 兼容 ——
const t2Idx = code.indexOf('[mcp-bridge] turn-2 continuation call_id=')
check('turn2-branch-present', t2Idx >= 0, 'turn-2 HIT branch missing')
const t2 = code.slice(t2Idx, t2Idx + 700)
check('turn2-uses-idempotent-exit', /if \(_ctx\.connRelease\) \{ _ctx\.connRelease\(\) \}/.test(t2),
  'turn-2 must route through ctx.connRelease (先到先放不双放)')
check('turn2-legacy-fallback', /else if \(mcpConnMgr\) \{ try \{ mcpConnMgr\.release\(process\.env\.ZK_MCP_ACCOUNT/.test(t2),
  'legacy direct-release fallback must remain (无 connRelease 的旧 ctx)')

// —— ④ 非 injected 路不受影响:seam3 尾部 release 原样(fallback/prose/empty 轮当场配平)——
check('seam3-tail-release-kept', /if \(mcpConnMgr && _connAccount\) \{ try \{ mcpConnMgr\.release\(_connAccount\) \} catch \(_\) \{\} \}/.test(code),
  'seam3 tail direct release must remain for non-injected turns')

console.log(`\n== ${pass}/${pass + fail} PASS ==`)
if (fail) { console.log('FAILS:', JSON.stringify(fails, null, 2)); process.exit(1) }
console.log('VERDICT: GO (幂等release/看门狗armed+log+unref/门控默认10min下限60s/turn-2同出口+老ctx兼容/非injected路原样)')
