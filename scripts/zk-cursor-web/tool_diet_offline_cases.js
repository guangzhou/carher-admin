#!/usr/bin/env node
/* tool_diet_offline_cases.js — A线目录瘦身离线单测(ZK_TOOL_DIET)。
 *
 * 背景(2026-08-29 真流量实锤):
 *   客户端 <mcp_server_catalog>(实测 5910c,其中 browser 服务器 serverUseInstructions
 *   4861c)+ instructions 里 <mcp_meta_tools> 教学 2895c,压重任务首轮——带目录首轮
 *   空率 3/8 vs 不带 4/45。dietCursorInput 的保留名单把目录整块原样放行(08-21 名单
 *   定型时目录尚小),瘦身后的框架消息 6236c 里目录占 5910c(95%)。
 *
 * 改法(dietMcpText,挂 _stripExecEnv 咽喉 + chat-fallback 路):
 *   目录:服务器名 + tools 清单逐字保留(即索引),serverUseInstructions 截 140c;
 *   meta:整块换紧凑版(保留 GetMcpTools/CallMcpTool/mcp_auth 语义——按需取参数走
 *   Cursor 原生 GetMcpTools,不发明新机制)。门控 ZK_TOOL_DIET 默认关 = 零行为差。
 *
 * 用法: node scripts/zk-cursor-web/tool_diet_offline_cases.js [/path/to/responses.js]
 *   样本: /tmp/tooldiet/catalog_raw.txt /tmp/tooldiet/meta_raw.txt(198 真抓包)
 * 全过退出码 0,任一失败退 1。控制组用法:指向未打补丁字节,套件必须 FAIL。
 */
'use strict'
const fs = require('fs')

const SRC = process.argv[2] || '/tmp/tooldiet/resp_tooldiet.js'
const code = fs.readFileSync(SRC, 'utf8')
const CATALOG = fs.readFileSync('/tmp/tooldiet/catalog_raw.txt', 'utf8')
const META = fs.readFileSync('/tmp/tooldiet/meta_raw.txt', 'utf8')

let pass = 0, fail = 0
const fails = []
function check(name, ok, why) {
  if (ok) { pass++; console.log('  PASS ' + name) }
  else { fail++; fails.push({ name, why }); console.log('  FAIL ' + name + (why ? ' — ' + why : '')) }
}

// ── 抠函数源(控制组:未打补丁字节抠不到 → 立即 FAIL)─────────────────
const mFn = code.match(/const MCP_META_COMPACT = [\s\S]*?\nfunction dietMcpText\(s\) \{[\s\S]*?\n\}/)
check('S0 补丁函数存在于目标字节', !!mFn, 'dietMcpText 未找到(控制组应在此 FAIL)')
if (!mFn) { console.log(`\n${pass} pass / ${fail} fail`); process.exit(1) }

const sandbox = { process: { env: {} }, console: { log: () => {} } }
const fn = new Function('process', 'console', mFn[0] + '\nreturn { dietMcpText, MCP_META_COMPACT }')
const makeApi = (env) => fn({ env }, sandbox.console)

// ── 用例 ────────────────────────────────────────────────────────────────
const PROMPT = 'SYSTEM: head text\n' + META + '\nmiddle text keep me\n' + CATALOG + '\nUSER: 建个飞书文档'

{ // 1 门控关 = 逐字节原样
  const api = makeApi({})
  check('1 门控关零行为差', api.dietMcpText(PROMPT) === PROMPT)
}
const api = makeApi({ ZK_TOOL_DIET: '1' })
const out = api.dietMcpText(PROMPT)
{ // 2 目录压缩落区间
  const cat = out.match(/<mcp_server_catalog>[\s\S]*?<\/mcp_server_catalog>/)[0]
  check('2 目录 5910c 压到 900-1600c', cat.length >= 900 && cat.length <= 1600, 'got ' + cat.length)
}
{ // 3 索引逐字保留:服务器名 + tools 清单
  check('3a 服务器名保留', out.includes('name="cursor-app-control"') && out.includes('name="cursor-ide-browser"'))
  const toolsAttrs = CATALOG.match(/ tools="[^"]*"/g) || []
  check('3b 全部 tools 清单逐字保留', toolsAttrs.length === 2 && toolsAttrs.every((t) => out.includes(t)), String(toolsAttrs.length))
}
{ // 4 短说明(app-control 182c)原样不动
  const short = CATALOG.match(/name="cursor-app-control"[\s\S]*?\/>/)[0]
  check('4 短说明整段原样', out.includes(short))
}
{ // 5 长说明被截且带截断记号
  const b = out.match(/name="cursor-ide-browser"[\s\S]*?\/>/)[0]
  check('5 长说明截 140c 带记号', b.includes('…"') && b.length < 500, 'len=' + b.length)
}
{ // 6 meta 块换紧凑版且语义保留
  check('6a meta 换紧凑版', out.includes(api.MCP_META_COMPACT) && !out.includes('supports four modes'))
  check('6b 紧凑版语义齐(GetMcpTools/CallMcpTool/mcp_auth)',
    ['GetMcpTools', 'CallMcpTool', 'mcp_auth'].every((k) => api.MCP_META_COMPACT.includes(k)))
}
{ // 7 周边文本零误伤
  check('7 周边文本逐字保留', out.startsWith('SYSTEM: head text\n') && out.includes('\nmiddle text keep me\n') && out.endsWith('USER: 建个飞书文档'))
}
{ // 8 幂等
  check('8 幂等 f(f(x))===f(x)', api.dietMcpText(out) === out)
}
{ // 9 无块文本原样
  const plain = 'hello no blocks here'
  check('9 无块文本原样', api.dietMcpText(plain) === plain)
}
{ // 10 残破目录(缺闭合)容错原样
  const broken = 'x <mcp_server_catalog> unclosed serverUseInstructions="' + 'y'.repeat(500) + '" /> tail'
  check('10 残破目录容错', api.dietMcpText(broken) === broken)
}
{ // 11 整体节省量:PROMPT 级 ≥6KB
  check('11 整轮节省 ≥6000c', PROMPT.length - out.length >= 6000, 'saved=' + (PROMPT.length - out.length))
}
// ── 结构断言:两处挂点在位 ───────────────────────────────────────────────
check('12a _stripExecEnv 咽喉挂点', /const _stripExecEnv = \(p\) => \{\n\s*p = \(typeof dietMcpText === 'function'\) \? dietMcpText\(p\) : p/.test(code))
check('12b chat-fallback 挂点', /_fbPrompt = \(typeof dietMcpText === 'function'\) \? dietMcpText\(_fbPrompt\) : _fbPrompt/.test(code))

console.log(`\n${pass} pass / ${fail} fail`)
if (fail) { console.log(JSON.stringify(fails, null, 1)); process.exit(1) }
