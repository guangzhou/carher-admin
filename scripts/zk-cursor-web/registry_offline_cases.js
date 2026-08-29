#!/usr/bin/env node
/* registry_offline_cases.js — B线统一工具注册表 + ⟦call⟧ 动词离线单测(ZK_TOOL_REGISTRY)。
 *
 * codex 骨架搬运(源码锚点见深读笔记第 6 趟):内置与 MCP 工具同一张表分发;
 * ⟦call¦<Tool>¦{json}⟧ 与 ⟦cmd¦run⟧ 同族;MCP 工具名便捷形折成 CallMcpTool
 * (Cursor 自执行,零本地件);失败=教学重发一次(预算 1)→ 诚实交付。
 *
 * 用法: node scripts/zk-cursor-web/registry_offline_cases.js [/path/to/responses.js]
 *   样本: /tmp/tooldiet/cap-6.json(198 真抓包,含真 MCP 目录)
 * 控制组:指向 B 线前字节(仅 A 线)必须 FAIL。
 */
'use strict'
const fs = require('fs')

const SRC = process.argv[2] || '/tmp/tooldiet/resp_bline.js'
const code = fs.readFileSync(SRC, 'utf8')
const CAP = JSON.parse(fs.readFileSync('/tmp/tooldiet/cap-6.json', 'utf8'))

let pass = 0, fail = 0
const fails = []
function check(name, ok, why) {
  if (ok) { pass++; console.log('  PASS ' + name) }
  else { fail++; fails.push({ name, why }); console.log('  FAIL ' + name + (why ? ' — ' + why : '')) }
}

// ── 抠模块件(控制组在此 FAIL)────────────────────────────────────────────
const mMod = code.match(/const V2_CALL_RE = [\s\S]*?\nfunction mcpServerOfTool\(input, toolName\) \{[\s\S]*?\n\}/)
check('S0 B线模块件存在', !!mMod, '控制组应在此 FAIL')
if (!mMod) { console.log(`\n${pass} pass / ${fail} fail`); process.exit(1) }
const mToc = code.match(/function textOfContent\(content\) \{[\s\S]*?\n\}/)
check('S1 textOfContent 可抠', !!mToc)
const api = new Function(mToc[0] + '\n' + mMod[0] + '\nreturn { V2_CALL_RE, V2_CALL_ADDON, buildToolRegistry, mcpServerOfTool }')()

// ── 动词正则 ──────────────────────────────────────────────────────────────
{
  const m = api.V2_CALL_RE.exec('前置说明\n⟦call¦Read¦{"path":"/a.txt","limit":5}⟧\n后缀')
  check('1a 基本匹配 名+JSON', !!m && m[1] === 'Read' && JSON.parse(m[2]).path === '/a.txt')
  const m2 = api.V2_CALL_RE.exec('⟦call¦CallMcpTool¦{"server":"s","toolName":"t","arguments":{"a":{"b":1}}}⟧')
  check('1b 嵌套 JSON 完整捕获', !!m2 && JSON.parse(m2[2]).arguments.a.b === 1)
  check('1c 未闭合不匹配', !api.V2_CALL_RE.test('⟦call¦Read¦{"path":"/a"'))
  check('1d 中文名拒绝(需注册名)', !api.V2_CALL_RE.test('⟦call¦读文件¦{}⟧'))
}
// ── 注册表 ────────────────────────────────────────────────────────────────
{
  const reg = api.buildToolRegistry(CAP.tools)
  check('2a 19 工具全入表', reg.size === 19, 'size=' + reg.size)
  check('2b 大小写不敏感命中', reg.get('callmcptool') === 'CallMcpTool' && reg.get('shell') === 'Shell')
  const reg2 = api.buildToolRegistry([{ function: { name: 'Wrapped' } }, null, {}])
  check('2c function 包裹形+脏项容错', reg2.size === 1 && reg2.get('wrapped') === 'Wrapped')
}
// ── MCP 目录查服务器(原样目录 + 瘦身后目录都要命中)────────────────────
{
  const item0 = CAP.input[0]
  check('3a 真目录:browser_navigate→cursor-ide-browser', api.mcpServerOfTool([item0], 'browser_navigate') === 'cursor-ide-browser')
  check('3b 真目录:move_agent_to_root→cursor-app-control', api.mcpServerOfTool([item0], 'move_agent_to_root') === 'cursor-app-control')
  check('3c 未知名→null', api.mcpServerOfTool([item0], 'nope_tool') === null)
  // 瘦身后形态:name/tools 属性保留 → 仍可查(A/B 两线协同不变量)
  const dietText = (typeof item0.content === 'string' ? item0.content : item0.content.map((c) => c.text || '').join('\n'))
    .replace(/ serverUseInstructions="[\s\S]*?"(\s*\/>)/g, ' serverUseInstructions="x"$1')
  check('3d 瘦身后目录仍可查', api.mcpServerOfTool([{ type: 'message', content: dietText }], 'browser_click') === 'cursor-ide-browser')
}
// ── 契约增补 ──────────────────────────────────────────────────────────────
{
  const a = api.V2_CALL_ADDON
  check('4a 增补 ≤460c', a.length <= 460, 'len=' + a.length)
  check('4b 含 CallMcpTool 例 + 单块条款', a.includes('CallMcpTool') && a.includes('ONE block per reply TOTAL'))
  check('4c 契约链 4 处 + 重建路 2 处接线', (code.match(/_cAdd/g) || []).length >= 5 && (code.match(/V2_CALL_ADDON : ''/g) || []).length >= 3,
    JSON.stringify({ cAdd: (code.match(/_cAdd/g) || []).length, addon: (code.match(/V2_CALL_ADDON : ''/g) || []).length }))
}
// ── 分支结构 ──────────────────────────────────────────────────────────────
{
  check('5a 门控默认关', code.includes("process.env.ZK_TOOL_REGISTRY === '1'"))
  const iCall = code.indexOf('[registry] complete-call')
  const iUnterm = code.indexOf('── 未闭合 run 块打捞')
  const iCmd = code.indexOf('[turn-verdict-v2] complete-run (')
  check('5b 分支位序:cmd 先 → call 次 → 打捞后', iCmd > 0 && iCall > iCmd && iUnterm > iCall)
  check('5c 教学重发一次性闸', code.includes('let callBlockRetried = false') && code.includes('callBlockRetried = true'))
  check('5d 教学文本带可用名清单', code.includes('Available tool names (exact)'))
  check('5e 便捷形折叠 CallMcpTool', code.includes("_cvReg.get('callmcptool')") && code.includes('{ server: _srv, toolName: _cvName, arguments: _cvArgs }'))
  check('5f 耗尽后诚实交付非假绿', code.includes('call-block invalid (retry exhausted)'))
  check('5g prose 随调用交付(leadingText)', /leadingText: _cvProse/.test(code))
}

console.log(`\n${pass} pass / ${fail} fail`)
if (fail) { console.log(JSON.stringify(fails, null, 1)); process.exit(1) }
