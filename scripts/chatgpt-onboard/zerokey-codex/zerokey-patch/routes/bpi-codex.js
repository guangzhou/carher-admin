// bpi-codex.js — 把网页模型吐出的 BPI 块编译成 Codex 的 exec 工具调用。
//
// 为什么需要这个（2026-08-07 实测链条）
// ------------------------------------
// 1. Codex 的 responses-lite 线路把工具塞在 `input` 的 `additional_tools` 项里，
//    顶层 `tools` 不存在（codex-rs/core/src/client.rs:869）。zerokey 的
//    responses.js 只读顶层 -> 读到空 -> 一个字的工具都没注入。
// 2. 模型手里真的没有工具，于是如实回答「我没有本地文件写入能力」。
//    实测：CLI->zerokey 拒答 4/5，同一份载荷 Desktop->acct 工具调用 5/5。
// 3. 之前的补丁反着来：`Use your shell to accomplish the task below` 把模型指向
//    它**自己**的沙箱，等于坐实了"我碰不到你的机器"。实测拒答 5/5。
//
// 真正的解法不是在用户消息里"劝"模型，而是把 ChatGPT 的**消费级外壳**换掉：
// 通过 `PATCH /backend-api/user_system_messages` 把 agent 契约写进账号级自定义
// 指令。实测（同一 pod、同一句话、消息里零契约）：
//
//     账号指令为空  -> 拒答 5/5
//     写入 BPI 契约 -> BPI 块 5/5，零拒答
//     未改的对照号  -> 拒答 5/5
//
// 契约生效后模型吐的是 `⟦write¦path=…¦content=…⟧` 这类块，本模块负责把它编译成
// Codex 认得的 `custom_tool_call(exec)`。
//
// exec 的契约（读 additional_tools 里 exec 的 description 原文，非推测）
// --------------------------------------------------------------------
// * 接受**原始 JavaScript 源文本**（不是 JSON、不是代码围栏），语法是
//   `SOURCE: /[\s\S]+/`，放行一切。
// * 子工具挂在全局 `tools` 上，实测该请求暴露：
//     tools.exec_command({cmd: string})   // shell
//     tools.apply_patch(input: string)    // 文件增删改
//   （另有 update_plan / view_image / web__run 等，本模块暂不用。）
//
// 安全性：本模块**只在响应里出现 BPI 块时**才动手。没写账号级契约的号根本不会
// 吐这种块，那些请求原样透传 —— 所以可以先全量部署本模块，再逐号开契约。

'use strict'

const OPEN = '⟦'   // ⟦
const CLOSE = '⟧'  // ⟧
const SEP = '¦'    // ¦

// 只编译"会产生副作用/需要真机执行"的块。ask/todos 这类纯交互块留给文本层，
// 硬翻成 shell 反而会骗客户端执行奇怪的东西。
const EXECUTABLE = new Set(['read', 'write', 'replace', 'ls', 'mkdir', 'glob', 'grep', 'cmd'])

function shq(s) {
  // POSIX 单引号转义：把 ' 换成 '\''
  return "'" + String(s).replace(/'/g, "'\\''") + "'"
}

function jsStr(s) {
  return JSON.stringify(String(s))
}

/** 解析一个 BPI 块体（不含首尾括号）成 {name, params}。 */
function parseBlock(body) {
  const parts = body.split(SEP)
  const name = parts.shift().trim()
  const params = {}
  for (const p of parts) {
    const i = p.indexOf('=')
    if (i < 0) continue
    params[p.slice(0, i).trim()] = p.slice(i + 1)
  }
  return { name, params }
}

/** 从整段文本里抽出所有 BPI 块。返回 [{name, params, raw}]。 */
function extractBlocks(text) {
  const out = []
  if (typeof text !== 'string' || text.indexOf(OPEN) < 0) return out
  let i = 0
  while (true) {
    const s = text.indexOf(OPEN, i)
    if (s < 0) break
    // 反引号包裹的是示例，不是调用（上游 2bfc012d 同款约定）
    if (s > 0 && text[s - 1] === '`') { i = s + 1; continue }
    const e = text.indexOf(CLOSE, s + 1)
    if (e < 0) break
    const body = text.slice(s + 1, e)
    const blk = parseBlock(body)
    if (blk.name) { blk.raw = text.slice(s, e + 1); out.push(blk) }
    i = e + 1
  }
  return out
}

/** 生成 apply_patch 的补丁信封。`content` 的每一行前缀 '+'。 */
function addFilePatch(path, content) {
  const lines = String(content).split('\n').map((l) => '+' + l)
  return ['*** Begin Patch', '*** Add File: ' + path, ...lines, '*** End Patch'].join('\n')
}

function updateFilePatch(path, oldStr, newStr) {
  const del = String(oldStr).split('\n').map((l) => '-' + l)
  const add = String(newStr).split('\n').map((l) => '+' + l)
  return ['*** Begin Patch', '*** Update File: ' + path, '@@', ...del, ...add, '*** End Patch'].join('\n')
}

/** 单个 BPI 块 -> 一行 JS。不认识的返回 null。 */
function blockToJs(blk) {
  const p = blk.params
  switch (blk.name) {
    case 'cmd':
      if (!p.run) return null
      return `await tools.exec_command({cmd: ${jsStr(p.run)}})`
    case 'ls':
      if (!p.path) return null
      return `await tools.exec_command({cmd: ${jsStr('ls -la ' + shq(p.path))}})`
    case 'mkdir':
      if (!p.path) return null
      return `await tools.exec_command({cmd: ${jsStr('mkdir -p ' + shq(p.path))}})`
    case 'read': {
      if (!p.path) return null
      const from = parseInt(p.from, 10)
      const to = parseInt(p.to, 10)
      const cmd = Number.isFinite(from) && Number.isFinite(to)
        ? `sed -n ${from},${to}p ${shq(p.path)}`
        : `cat ${shq(p.path)}`
      return `await tools.exec_command({cmd: ${jsStr(cmd)}})`
    }
    case 'glob': {
      if (!p.pattern) return null
      const max = parseInt(p.max, 10)
      const n = Number.isFinite(max) ? max : 200
      return `await tools.exec_command({cmd: ${jsStr(`ls -1d ${p.pattern} 2>/dev/null | head -n ${n}`)}})`
    }
    case 'grep': {
      const q = p.queryR || p.query
      if (!q) return null
      const max = parseInt(p.max, 10)
      const n = Number.isFinite(max) ? max : 200
      const glob = p.glob ? ` --glob ${shq(p.glob)}` : ''
      const flag = p.queryR ? '' : ' -F'
      return `await tools.exec_command({cmd: ${jsStr(`rg -n${flag}${glob} -- ${shq(q)} | head -n ${n}`)}})`
    }
    case 'write':
      if (!p.path || p.content === undefined) return null
      return `await tools.apply_patch(${jsStr(addFilePatch(p.path, p.content))})`
    case 'replace':
      if (!p.path || p.old === undefined || p.new === undefined) return null
      return `await tools.apply_patch(${jsStr(updateFilePatch(p.path, p.old, p.new))})`
    default:
      return null
  }
}

/**
 * 把一段模型输出编译成 Codex 的 exec 调用。
 *
 * 返回 { js, blocks, leftover }：
 *   js       - 要塞进 custom_tool_call.input 的 JavaScript；无可执行块时为 null
 *   blocks   - 命中的块（供日志/计数）
 *   leftover - 去掉已编译块之后剩下的文本（通常为空；非空说明模型混了说明文字，
 *              按上游 output_contract 那是违约，但我们保留下来当普通文本回传）
 */
function compileToExec(text) {
  const blocks = extractBlocks(text).filter((b) => EXECUTABLE.has(b.name))
  if (!blocks.length) return { js: null, blocks: [], leftover: text }
  const lines = []
  for (const b of blocks) {
    const js = blockToJs(b)
    if (js) lines.push(`text(${jsStr('BPI(' + b.name + '):')});`, js.replace(/^await /, 'text(await ') + ');')
  }
  if (!lines.length) return { js: null, blocks: [], leftover: text }
  let leftover = text
  for (const b of blocks) leftover = leftover.split(b.raw).join('')
  return { js: lines.join('\n'), blocks, leftover: leftover.trim() }
}

module.exports = { compileToExec, extractBlocks, blockToJs, addFilePatch, updateFilePatch, OPEN, CLOSE, SEP }
