// test_scenarios.js — 场景回放语料库（系统性防御第 4 支柱）
//
// 与 test_bpi.js 的分工：
//   test_bpi.js       单元级 —— 每个函数的输入输出契约
//   test_scenarios.js 场景级 —— **每个修过的线上事故的完整现场形状**，
//                     跑整条 prepareCodexInput -> (模拟模型回复) -> 出站过滤链
//
// 规则（这是架构约定，不是建议）：
//   每修一个线上 bug，必须在这里加一个场景 —— 用**当时的真实载荷形状**，
//   断言**当时的期望行为**。以后任何改动跑一遍全部历史现场，
//   "修 A 弄坏 B"在提交前就被抓住，而不是等下一个用户撞上。
//
// 用法： node test_scenarios.js
'use strict'
const assert = require('assert')
const P = require('./bpi-codex.js')

let pass = 0, fail = 0
function scenario(name, fn) {
  try { fn(); pass++; console.log('  ✅', name) }
  catch (e) { fail++; console.log('  ❌', name, '\n     ', e.message) }
}
const msg = (role, text) => ({ type: 'message', role, content: [{ type: 'input_text', text }] })
const toolCall = (id, js) => ({ type: 'custom_tool_call', call_id: id, name: 'exec', input: js })
const toolOut = (id, text) => ({ type: 'custom_tool_call_output', call_id: id, output: [{ type: 'input_text', text }] })
const CO = '', CS = '', CE = ''
const ENV = msg('user', '<environment_context><cwd>/Users/u/codes/repo</cwd><shell>zsh</shell></environment_context>')
const TOOLS = { type: 'additional_tools', tools: [{ name: 'exec' }] }

console.log('== 场景回放：每个线上事故的现场形状 ==\n')

// ── 2026-08-07：工具结果拼成空 "USER: "，模型答非所问 ──
scenario('S1 工具结果无 role 无 content -> 回放成可读文本，不拼空', () => {
  const r = P.replayItemToText(toolOut('c1', JSON.stringify({ output: 'total 512\ndrwxr-xr-x', exit_code: 0 })))
  assert.ok(r && r.text.includes('total 512'), '工具结果丢失')
  assert.ok(r.text.includes(P.RESULT_HEAD))
})

// ── 2026-08-08：印刷体撇号拒答漏判 ──
scenario('S2 印刷体撇号拒答 -> 仍触发重试', () => {
  assert.ok(P.needsEscalation('I do’t have an active file-editing tool connection', false))
})

// ── 2026-08-08：末尾只剩 filecite / 回答带标记 ──
scenario('S3 引用标记完整/跨分片/断尾 -> 剥净且正文无损', () => {
  const s = '甲=7741 ' + CO + 'filecite' + CS + 'turn0file0' + CE + ' 完毕'
  assert.equal(P.stripCitations(s), '甲=7741  完毕')
  const f = P.makeCitationFilter()
  let out = f.push('结论：' + CO + 'filecite' + CS + 'turn0')
  const t = f.flush()
  assert.equal(out + t.text, '结论：')
  assert.ok(t.truncated)
})

// ── 2026-08-09 上午：/init 反问 cwd（鸡生蛋）──
scenario('S4 没有 AGENTS.md 的仓库 -> cwd 仍从协议块读出', () => {
  const env = P.parseEnvironment([TOOLS, ENV, msg('user', 'Generate a file named AGENTS.md…')])
  assert.equal(env.cwd, '/Users/u/codes/repo')
  assert.ok(P.handsBlock(env).includes('/Users/u/codes/repo'))
})

// ── 2026-08-09 上午：apply_patch 返回 {} 模型看不出成功 ──
scenario('S5 write 成功返回 {} -> 翻译成人话，BPI 标记不喂回', () => {
  const d = P.distillExecOutput('BPI(write):\n{}')
  assert.ok(d.includes('文件已写入'), d)
  assert.ok(!d.includes('BPI('), 'BPI 内部标记漏给模型')
})

// ── 2026-08-09 下午：/init 失忆死循环（压缩丢一切）──
scenario('S6 /init 现场：5轮大ls+write后压缩 -> 台账+write结果都在', () => {
  const big = JSON.stringify({ output: 'x'.repeat(16000), exit_code: 0 })
  const items = [msg('developer', 'You are Codex…'), ENV, msg('user', 'Generate AGENTS.md…')]
  for (let i = 0; i < 5; i++) {
    items.push(toolCall('c' + i, `text(await tools.exec_command({cmd: "ls -la /repo/d${i}"}));`))
    items.push(toolOut('c' + i, big))
  }
  items.push(toolCall('cw', 'text(await tools.apply_patch("*** Begin Patch\\n*** Add File: /repo/AGENTS.md\\n+# x\\n*** End Patch"));'))
  items.push(toolOut('cw', '{}'))
  const out = P.compactInput(items)
  const flat = out.map((it) => ((it.content || [])[0] || {}).text || '').join('\n')
  assert.ok(flat.includes(P.LEDGER_HEAD), '台账丢了 -> 失忆循环')
  const outs = out.filter((it) => it.type === 'custom_tool_call_output')
  assert.equal(outs.length, 1)
  assert.equal(outs[0].call_id, 'cw', 'write 那轮必须保留原文')
})

// ── 2026-08-09 下午：/init 收尾被 ABS_PATH_RE 误杀 ──
scenario('S7 真干完活的最终答复（带路径）-> 放行不打回', () => {
  const t = '已在 /Users/u/codes/repo/AGENTS.md 创建仓库贡献指南。'
  assert.ok(!P.needsEscalation(t, true), '合法收尾被打回 -> 死循环')
  assert.ok(P.needsEscalation(t, false), '0 工具报路径该拦')
})

// ── 2026-08-09 晚：引用剥离器吞正文（飞书回答剩半句）──
scenario('S8 孤立起始符+中文正文 -> 一字不丢', () => {
  const s = '需要指定目标飞书文档（' + CO + '链接或文档ID以及写入位置我会写入统计结果'
  const want = '需要指定目标飞书文档（链接或文档ID以及写入位置我会写入统计结果'
  assert.equal(P.stripCitations(s), want)
  const f = P.makeCitationFilter()
  let out = ''
  for (const ch of s) out += f.push(ch)
  out += f.flush().text
  assert.equal(out, want, '流式路径吞正文')
})

// ── 2026-08-09 晚：守卫兜底（未来的"吃正文"bug 自动回退）──
scenario('S9 假想的坏过滤器吃掉 60% 正文 -> 守卫回退+保正文', () => {
  const raw = '这是一段很长的正文'.repeat(50)
  const eaten = raw.slice(0, Math.floor(raw.length * 0.4))   // 假设某过滤器吞了 60%
  const g = P.guardOutbound(raw, eaten, 0)
  assert.ok(g.fellBack, '守卫没拦住未申报的大额丢失')
  assert.equal(g.text, raw, '回退后应是（最小清洗的）原文')
})

// ── 画布泄漏（2026-08-09）──
scenario('S10 画布围栏 -> 判"没动手"，剥围栏保正文', () => {
  const t = ':::writing{variant="document" id="1"}\n# Guidelines\n正文\n:::'
  assert.ok(P.needsEscalation(t, false))
  const stripped = P.stripCanvas(t)
  assert.ok(stripped.includes('正文') && !stripped.includes(':::'))
})

// ── 2026-08-09 深夜：创建飞书文档 —— 空承诺拖延三连 ──
scenario('S11 空承诺/敷衍 -> 判拖延重试；能力陈述/收尾不误伤', () => {
  // 现场原句，全是拖延（有无工具史都非法）
  assert.ok(P.needsEscalation('可以继续', true), '「可以继续」放过了')
  assert.ok(P.needsEscalation('我现在直接用本机已授权的 lark-cli 创建飞书文档，并先读取文档技能说明以确保调用方式正确。', true))
  assert.ok(P.needsEscalation('好的，接下来我先检查 lark-cli 当前可用的文档创建接口', false))
  // 不该误伤的：能力陈述 / 第二人称 / 真收尾 / 提问
  assert.ok(!P.needsEscalation('我可以协助创建、整理和写入飞书文档，请告诉我标题。', false), '能力陈述被误伤')
  assert.ok(!P.needsEscalation('你可以运行 lark-cli doc create 来创建。', false), '第二人称被误伤')
  assert.ok(!P.needsEscalation('已创建飞书文档，标题《周报》，文档 ID doccnXXXX。', true), '真收尾被误伤')
})

// ── 2026-08-10 架构级：usage 必须报客户端真实上下文，否则原生自动压缩瞎掉 ──
scenario('S13 usage 口径：报真实上下文（不是压缩后 prompt）', () => {
  // Codex 的原生 auto-compact 由 last_token_usage.total_tokens 驱动
  //（context_manager/history.rs:323）—— 也就是我们回报的 usage。
  // 报压缩后的值 = 客户端永远以为上下文没满 = 它的原生压缩永远不触发。
  const big = JSON.stringify({ output: 'x'.repeat(30000), exit_code: 0 })
  const items = [msg('developer', 'sys'), ENV, msg('user', '任务')]
  for (let i = 0; i < 4; i++) {
    items.push(toolCall('c' + i, `text(await tools.exec_command({cmd:"ls /d${i}"}));`))
    items.push(toolOut('c' + i, big))
  }
  const realChars = P.measureItems(items)
  assert.ok(realChars > 100000, '度量应看到真实体量: ' + realChars)
  // 压缩后发出去的远小于真实历史 —— 这正是不能拿它当 usage 的原因
  const compacted = P.compactInput(items)
  assert.ok(P.measureItems(compacted) < realChars / 2, '压缩确实缩小了发送量')
  // 工具结果/调用都必须被计入（历史上漏算过，导致压缩不触发）
  assert.ok(P.itemChars(toolOut('x', big)) > 29000, '工具结果没被计入度量')
  assert.ok(P.itemChars(toolCall('x', 'abc')) > 0, '工具调用没被计入度量')
  // 未知形状不许静默算 0（宁可高估）
  assert.ok(P.itemChars({ type: 'brand_new_shape', payload: 'y'.repeat(500) }) > 400,
    '未知形状被算成 0 -> 又一次静默漏算')
})
scenario('S12 cmd 参数外层引号剥掉，不再 command not found', () => {
  const r = P.compileToExec("⟦cmd¦run='git status --short'⟧")
  assert.ok(r.js.includes('"git status --short"'), '外层单引号没剥: ' + r.js)
  assert.ok(!r.js.includes("'git"), '引号还在')
  // 内部引号（合法 shell 引用）不能动
  const r2 = P.compileToExec('⟦cmd¦run=grep -r \'TODO\' src/⟧')
  assert.ok(r2.js.includes("grep -r 'TODO' src/"), '内部引号被误剥: ' + r2.js)
})

// ── 2026-08-10：上游截断残句反复出现（out=4~14 无句末标点）──
scenario('S14 疑似截断残句 -> 重试；带标点短收尾不误伤', () => {
  for (const t of ['可以创建', '我目前在这个对话环境里没有可', '我可以协助创建飞'])
    assert.ok(P.needsEscalation(t, true), '残句放过了: ' + t)
  for (const t of ['已完成。', 'O(n log n)。'])
    assert.ok(!P.needsEscalation(t, true), '合法短收尾被误伤: ' + t)
})

console.log(`\n=== 场景回放 ${pass} passed, ${fail} failed ===`)
process.exit(fail ? 1 : 0)
