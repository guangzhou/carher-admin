'use strict'
/*
 * zk-delta/tests/live_multiturn.js  —— S4 真流量验收
 *
 * 打真模型、真 GPT 网页号池，走 本机小代理 → 198 nginx → zk-delta → LiteLLM → zerokey 网关。
 * 不是合成压测：每一轮的答案都是模型真答出来的，答不出来这条就算红。
 *
 * 用法：
 *   ZKD_KEY=sk-xxx node zk-delta/tests/live_multiturn.js [模型] [轮数] [每轮KB]
 */

const http = require('http')
const https = require('https')

const KEY = process.env.ZKD_KEY || ''
const MODEL = process.argv[2] || 'cursor-web-fc-82-terra'
const TURNS = parseInt(process.argv[3] || '15', 10)
const PAD_KB = parseInt(process.argv[4] || '100', 10)
const SIDE = parseInt(process.env.ZKD_PORT || '8788', 10)

if (!KEY) { console.error('缺 ZKD_KEY'); process.exit(2) }

const TOOLS = [{
  type: 'function',
  function: {
    name: 'shell',
    description: 'Run a shell command and return its output.',
    parameters: {
      type: 'object',
      properties: { command: { type: 'string', description: 'the command line to run' } },
      required: ['command']
    }
  }
}]

function pad (kb, seed) {
  const words = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel',
    'india', 'juliet', 'kilo', 'lima', 'mike', 'november', 'oscar', 'papa']
  let h = 2166136261
  for (const c of String(seed)) { h ^= c.charCodeAt(0); h = Math.imul(h, 16777619) >>> 0 }
  let s = ''
  const n = kb * 1024
  while (s.length < n) { h = (Math.imul(h, 1103515245) + 12345) >>> 0; s += words[h % 16] + ' ' }
  return s.slice(0, n)
}

function post (port, path, buf, headers) {
  return new Promise((resolve, reject) => {
    const r = http.request({
      host: '127.0.0.1', port, path, method: 'POST',
      headers: Object.assign({
        'content-type': 'application/json',
        'content-length': buf.length,
        'authorization': 'Bearer ' + KEY,
        'user-agent': 'Cursor/3.17.19',
        // 我不是真 Cursor：这行让采集门认出我，别把测试流量攒成 ⑨ 的金样
        'x-zkd-synthetic': '1'
      }, headers || {})
    }, (res) => {
      const cs = []
      res.on('data', (c) => cs.push(c))
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(cs) }))
    })
    r.on('error', reject)
    r.end(buf)
  })
}

function getJson (port, path) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port, path }, (res) => {
      const cs = []
      res.on('data', (c) => cs.push(c))
      res.on('end', () => { try { resolve(JSON.parse(Buffer.concat(cs).toString())) } catch (e) { reject(e) } })
    }).on('error', reject)
  })
}

function parseSSE (buf) {
  let text = ''
  let toolDeltas = 0
  let events = 0
  for (const line of buf.toString('utf8').split('\n')) {
    if (!line.startsWith('data: ')) continue
    const d = line.slice(6).trim()
    if (d === '[DONE]') continue
    events++
    try {
      const j = JSON.parse(d)
      const dl = j.choices && j.choices[0] && j.choices[0].delta
      if (dl && dl.content) text += dl.content
      if (dl && dl.tool_calls) toolDeltas++
    } catch (e) {}
  }
  return { text, toolDeltas, events }
}

async function main () {
  console.log(`真流量多轮验收  model=${MODEL}  turns=${TURNS}  pad=${PAD_KB}KB/轮`)
  console.log('='.repeat(96))

  const messages = [{
    role: 'system',
    content: 'You are a coding assistant.\n<workspace_context>\n' + pad(PAD_KB * 2, 'sys') + '\n</workspace_context>'
  }]

  const rows = []
  let bad = 0

  for (let t = 1; t <= TURNS; t++) {
    // 每轮追加一段"IDE 上下文 + 一个真问题"，模仿 Cursor 每轮重发的形状
    messages.push({
      role: 'user',
      content: '<open_and_recently_viewed_files>\n' + pad(PAD_KB, 'u' + t) +
        `\n</open_and_recently_viewed_files>\n\n第 ${t} 轮：只回复一个词，就是数字 ${t} 的英文单词（one/two/three...），不要任何别的字。`
    })

    const body = {
      model: MODEL,
      messages,
      stream: true,
      tools: TOOLS,
      tool_choice: 'auto',
      reasoning_effort: 'low',
      stream_options: { include_usage: true }
    }
    const raw = Buffer.from(JSON.stringify(body), 'utf8')

    const m0 = await getJson(SIDE, '/metrics.json')
    const t0 = Date.now()
    let r
    try { r = await post(SIDE, '/v1/chat/completions', raw) } catch (e) {
      console.log(`轮${t}  ERROR ${e.message}`); bad++; break
    }
    const dt = (Date.now() - t0) / 1000
    const m1 = await getJson(SIDE, '/metrics.json')

    const up = m1.bytes_uplink - m0.bytes_uplink
    const { text, events } = parseSSE(r.body)
    const rebuilt = r.headers['x-zk-rebuilt-bytes'] ? parseInt(r.headers['x-zk-rebuilt-bytes'], 10) : null
    const mode = (m1.delta_sent > m0.delta_sent) ? 'delta'
      : (m1.full_sent > m0.full_sent) ? 'full' : 'passthru'

    const identical = rebuilt === null ? null : (rebuilt === raw.length)
    const answered = text.trim().length > 0
    if (r.status !== 200 || !answered || identical === false) bad++

    rows.push({ t, status: r.status, mode, orig: raw.length, up, rebuilt, identical, dt, text: text.trim().slice(0, 40), events })
    console.log(
      `轮${String(t).padStart(2)}  ${r.status}  ${mode.padEnd(8)}` +
      `  原始${String(raw.length).padStart(9)}B  出网${String(up).padStart(8)}B` +
      `  省${String(raw.length ? (100 - up * 100 / raw.length).toFixed(1) : '-').padStart(5)}%` +
      `  重建${rebuilt === null ? '  -  ' : (identical ? '逐字节相同' : '不一致!')}` +
      `  ${dt.toFixed(1)}s  答:${JSON.stringify(text.trim().slice(0, 24))}`)

    if (r.status !== 200) {
      console.log('   响应体:', r.body.toString('utf8').slice(0, 400))
      break
    }
    messages.push({ role: 'assistant', content: text })
  }

  console.log('='.repeat(96))
  const first = rows[0]; const last = rows[rows.length - 1]
  const totOrig = rows.reduce((a, r) => a + r.orig, 0)
  const totUp = rows.reduce((a, r) => a + r.up, 0)
  const n400 = rows.filter((r) => r.status === 400).length
  const nIdent = rows.filter((r) => r.identical === true).length
  const nNotIdent = rows.filter((r) => r.identical === false).length
  const nAnswered = rows.filter((r) => r.text.length > 0).length

  console.log(`G1  400 计数 = ${n400}   （目标 0）`)
  console.log(`G2  出网增长 第${last.t}轮/第1轮 = ${(last.up / first.up).toFixed(3)}x   （目标 <2）`)
  console.log(`    累计 本来要发 ${(totOrig / 1048576).toFixed(2)}MB → 实际出网 ${(totUp / 1048576).toFixed(2)}MB，省 ${(100 - totUp * 100 / totOrig).toFixed(1)}%`)
  console.log(`门① 重建逐字节相同 ${nIdent}/${rows.length}，不一致 ${nNotIdent}   （不一致必须为 0）`)
  console.log(`门② 有实际答案的轮次 ${nAnswered}/${rows.length}`)

  const green = n400 === 0 && nNotIdent === 0 && nAnswered === rows.length && rows.length === TURNS && (last.up / first.up) < 2
  console.log('='.repeat(96))
  console.log(green ? 'VERDICT = PASS' : 'VERDICT = FAIL')
  process.exit(green ? 0 : 1)
}

main().catch((e) => { console.error(e); process.exit(2) })
