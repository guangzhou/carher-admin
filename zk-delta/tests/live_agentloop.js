'use strict'
/*
 * zk-delta/tests/live_agentloop.js —— 门② 真功能对照
 *
 * 门② 的原话是"shell 命令和飞书文档创建必须稳定出结果"。光比 payload 字节不够，
 * 得让模型真的发工具调用、我真的执行、把结果喂回去，看它能不能把活干完。
 *
 * 关键是**对照**：同一个任务，一条腿走今天的地址（基准），一条腿走 zk-delta。
 * 只有"基准能做到而 zk-delta 做不到"才叫回退；两条腿都做不到那是别的问题，不算我引入的。
 *
 * 用法：
 *   ZKD_KEY=sk-xxx node zk-delta/tests/live_agentloop.js [模型] [最大步数]
 */

const http = require('http')
const https = require('https')
const { execFileSync } = require('child_process')
const fs = require('fs')
const os = require('os')
const path = require('path')

const KEY = process.env.ZKD_KEY || ''
const MODEL = process.argv[2] || 'cursor-web-fc-82-terra'
const MAX_STEPS = parseInt(process.argv[3] || '6', 10)
const SIDE_PORT = parseInt(process.env.ZKD_PORT || '8788', 10)

if (!KEY) { console.error('缺 ZKD_KEY'); process.exit(2) }

// ---------- 沙箱：只在一个临时目录里跑，命令白名单 ----------
const SANDBOX = fs.mkdtempSync(path.join(os.tmpdir(), 'zkd-agent-'))
fs.writeFileSync(path.join(SANDBOX, 'alpha.txt'), 'A')
fs.writeFileSync(path.join(SANDBOX, 'bravo.md'), 'B')
fs.mkdirSync(path.join(SANDBOX, 'charlie'))

// 默认只放只读命令。ZKD_ALLOW_LARK=1 时额外放行 lark-cli（门② 点名要验飞书建文档）。
const ALLOW = process.env.ZKD_ALLOW_LARK === '1'
  ? /^(ls|pwd|cat|echo|wc|head|find|stat|file|lark-cli)\b/
  : /^(ls|pwd|cat|echo|wc|head|find|stat|file)\b/

function runShell (cmd) {
  const c = String(cmd || '').trim()
  if (!ALLOW.test(c)) return `refused: 这个测试沙箱只允许 ${ALLOW.source} 开头的只读命令，收到的是: ${c}`
  try {
    return execFileSync('/bin/bash', ['-lc', c], {
      cwd: SANDBOX, timeout: 15000, maxBuffer: 1 << 20, encoding: 'utf8'
    }).slice(0, 4000)
  } catch (e) {
    return 'error: ' + String(e.stderr || e.message).slice(0, 1000)
  }
}

const TOOLS = [{
  type: 'function',
  function: {
    name: 'shell',
    description: 'Run a shell command in the user\'s working directory and return its stdout.',
    parameters: {
      type: 'object',
      properties: { command: { type: 'string', description: 'the command line to run' } },
      required: ['command']
    }
  }
}]

// ---------- 两条腿 ----------
const LEGS = {
  today: { label: '基准(今天的地址)', host: 'cc.auto-link.com.cn', port: 443, base: '/pro/v1', tls: true },
  delta: { label: 'zk-delta',        host: '127.0.0.1',           port: SIDE_PORT, base: '/v1', tls: false }
}

function post (leg, p, buf) {
  return new Promise((resolve, reject) => {
    const mod = leg.tls ? https : http
    const r = mod.request({
      host: leg.host, port: leg.port, path: leg.base + p, method: 'POST',
      headers: {
        'content-type': 'application/json',
        'content-length': buf.length,
        authorization: 'Bearer ' + KEY,
        'user-agent': 'Cursor/3.17.19'
      }
    }, (res) => {
      const cs = []
      res.on('data', (c) => cs.push(c))
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(cs) }))
    })
    r.on('error', reject)
    r.end(buf)
  })
}

/** 把 SSE 流拼回一条 assistant 消息（正文 + tool_calls） */
function parseSSE (buf) {
  let content = ''
  const calls = []
  for (const line of buf.toString('utf8').split('\n')) {
    if (!line.startsWith('data: ')) continue
    const d = line.slice(6).trim()
    if (d === '[DONE]') continue
    let j
    try { j = JSON.parse(d) } catch (e) { continue }
    const dl = j.choices && j.choices[0] && j.choices[0].delta
    if (!dl) continue
    if (dl.content) content += dl.content
    for (const tc of dl.tool_calls || []) {
      const i = tc.index == null ? 0 : tc.index
      calls[i] = calls[i] || { id: '', type: 'function', function: { name: '', arguments: '' } }
      if (tc.id) calls[i].id = tc.id
      if (tc.function && tc.function.name) calls[i].function.name = tc.function.name
      if (tc.function && tc.function.arguments) calls[i].function.arguments += tc.function.arguments
    }
  }
  return { content, calls: calls.filter(Boolean) }
}

async function runTask (legKey, task, expect) {
  const leg = LEGS[legKey]
  const messages = [
    { role: 'system', content: 'You are a coding assistant with a shell tool. Use it to answer questions about the user\'s directory.' },
    { role: 'user', content: task }
  ]
  const trace = []
  let answer = ''
  let steps = 0
  let toolRuns = 0
  let hardFail = null

  for (steps = 1; steps <= MAX_STEPS; steps++) {
    const raw = Buffer.from(JSON.stringify({
      model: MODEL, messages, stream: true, tools: TOOLS,
      tool_choice: 'auto', reasoning_effort: 'low'
    }), 'utf8')

    let r
    try { r = await post(leg, '/chat/completions', raw) } catch (e) {
      hardFail = 'transport: ' + e.message; break
    }
    if (r.status !== 200) {
      hardFail = `HTTP ${r.status}: ` + r.body.toString('utf8').slice(0, 300); break
    }
    const { content, calls } = parseSSE(r.body)
    trace.push({ step: steps, contentLen: content.length, nCalls: calls.length })

    if (calls.length === 0) { answer = content; break }

    messages.push({ role: 'assistant', content: content || null, tool_calls: calls })
    for (const c of calls) {
      let cmd = ''
      try { cmd = JSON.parse(c.function.arguments || '{}').command || '' } catch (e) { cmd = c.function.arguments }
      const out = runShell(cmd)
      toolRuns++
      trace[trace.length - 1].cmd = cmd
      messages.push({ role: 'tool', tool_call_id: c.id || ('call_' + toolRuns), content: out })
    }
  }

  const hit = expect.filter((w) => answer.includes(w))
  return { legKey, label: leg.label, steps, toolRuns, answer, hardFail, trace, hit, ok: !hardFail && hit.length === expect.length }
}

async function main () {
  // 任务档案：ZKD_TASK=shell（默认）| lark
  // task 是 legKey 的函数——飞书那条两条腿必须建两篇不同标题的文档，否则分不清哪篇是谁建的。
  const STAMP = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, '')
  const larkTitle = (leg) => `zk-delta门②验证-${leg}-${STAMP}`
  const PROFILES = {
    shell: {
      task: () => '用 shell 工具运行 ls，然后把这个目录里所有条目的名字原样告诉我。不要省略，不要只说数量。',
      expect: () => ['alpha.txt', 'bravo.md', 'charlie'],
      note: '沙箱内容: alpha.txt / bravo.md / charlie/'
    },
    lark: {
      task: (leg) => '用 shell 工具调用 lark-cli 在飞书里新建一个空白文档，标题是 ' + larkTitle(leg) + '。' +
        '命令形如 `lark-cli docs +create --title "<标题>" --as user`。' +
        '建完后把返回结果里的文档 URL 原样告诉我（必须包含 https:// 开头的完整链接）。',
      expect: () => ['https://'],
      note: '会在飞书里真建文档，标题 zk-delta门②验证-<腿>-' + STAMP
    }
  }
  const PROF = PROFILES[process.env.ZKD_TASK || 'shell']
  if (!PROF) { console.error('未知 ZKD_TASK'); process.exit(2) }

  console.log(`门② 真功能对照   model=${MODEL}   task=${process.env.ZKD_TASK || 'shell'}`)
  console.log(PROF.note)
  console.log(`沙箱=${SANDBOX}`)
  console.log('='.repeat(96))

  const results = []
  for (const legKey of ['today', 'delta']) {
    process.stdout.write(`\n--- ${LEGS[legKey].label} ---\n`)
    const t0 = Date.now()
    const r = await runTask(legKey, PROF.task(legKey), PROF.expect(legKey))
    const dt = ((Date.now() - t0) / 1000).toFixed(1)
    results.push(r)
    for (const s of r.trace) {
      console.log(`  步${s.step}  正文${String(s.contentLen).padStart(5)}字  工具调用${s.nCalls}` +
        (s.cmd ? `  → ${JSON.stringify(s.cmd)}` : ''))
    }
    if (r.hardFail) console.log(`  硬失败: ${r.hardFail}`)
    console.log(`  用时 ${dt}s  工具实际执行 ${r.toolRuns} 次`)
    console.log(`  最终答案: ${JSON.stringify(r.answer.slice(0, 300))}`)
    console.log(`  命中期望 ${r.hit.length}/${PROF.expect(legKey).length}  [${r.hit.join(', ')}]`)
    console.log(`  → ${r.ok ? '完成' : '未完成'}`)
  }

  const [base, delta] = results
  console.log('\n' + '='.repeat(96))
  console.log(`基准   : ${base.ok ? '完成' : '未完成'}   工具执行 ${base.toolRuns} 次`)
  console.log(`zk-delta: ${delta.ok ? '完成' : '未完成'}   工具执行 ${delta.toolRuns} 次`)

  // 判据：只有"基准能做到、zk-delta 做不到"才算门② 回退。
  const regressed = base.ok && !delta.ok
  const bothFail = !base.ok && !delta.ok
  console.log('='.repeat(96))
  if (regressed) {
    console.log('门② = FAIL（回退）：基准能做完，走 zk-delta 做不完。')
  } else if (bothFail) {
    console.log('门② = 无结论：两条腿都没做完，说明这是链路本身的问题，不是 zk-delta 引入的。')
    console.log('        （不算通过，也不算回退。要单独查基准为什么不行。）')
  } else if (delta.ok && !base.ok) {
    console.log('门② = PASS（且基准反而没做完，值得单独看一眼基准）')
  } else {
    console.log('门② = PASS：两条腿都把任务做完了，且答案都命中了全部期望内容。')
  }
  try { fs.rmSync(SANDBOX, { recursive: true, force: true }) } catch (e) {}
  process.exit(regressed || bothFail ? 1 : 0)
}

main().catch((e) => { console.error(e); process.exit(2) })
